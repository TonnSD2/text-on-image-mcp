#!/usr/bin/env python3
# GUARD: This file is protected — see AGENTS.md. AI agents: do NOT modify,
# refactor or delete without the user's EXPLICIT in-conversation confirmation.
"""Text-on-image MCP server (Streamable HTTP transport).

Run:  python server.py [--host 127.0.0.1] [--port 8080]
Client: http://<host>:<port>/mcp

State model: scene.json is the single source of truth; output.png is a pure
function of it (render.py). Every tool mutates the JSON, snapshots it for
undo, and re-renders. get_state()/set_state() expose the JSON to the client.
"""
from __future__ import annotations

import argparse
import base64
import contextvars
import copy
import functools
import hashlib
import hmac
import os
import re
import threading
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import parse_qs

from mcp.server.mcpserver import MCPServer

import render as R
import scene as S

# ---- Multi-scene state ------------------------------------------------------
# A Scene is one isolated workspace slice: state dict + undo/redo + lock +
# its own scene.json/output.png. Clients address a scene explicitly via
# ?scene=<key> (or X-TOI-Scene header) — stable across reconnects — or
# implicitly by their MCP session id, so two parallel generations never share
# (or overwrite) a scene unless a client deliberately reuses the same key.
# Legacy webapp-pool contract: TOI_WORKDIR (one process = one worker = one
# scene) or explicit TOI_SHARED_SCENE=1 pin the single "default" scene.
HISTORY_LEN = 25
MAX_SCENES = int(os.environ.get("TOI_MAX_SCENES", "16"))

_wd = os.environ.get("TOI_WORKDIR")
BASE_DIR = Path(_wd) if _wd else S.DEFAULT_SCENE_PATH.parent
if _wd:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
LEGACY_DEFAULT = bool(_wd) or os.environ.get("TOI_SHARED_SCENE") == "1"
DATA_ROOT = Path(os.environ.get("TOI_DATA") or (BASE_DIR / "sessions"))


def _segment(name: str) -> str:
    """Filesystem-safe single path segment for a client-supplied key."""
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name or ""):
        return name
    return hashlib.sha1((name or "").encode()).hexdigest()[:16]


# Client-supplied image paths (load_image/add_image) are confined to
# TOI_MEDIA_ROOT when set: mandatory before exposing the server remotely.
# Unset keeps full local-file access (dev / webapp-pool contract unchanged).
MEDIA_ROOT = (Path(os.environ["TOI_MEDIA_ROOT"]).expanduser().resolve()
              if os.environ.get("TOI_MEDIA_ROOT") else None)


def _media_path(path: str) -> Path:
    """Resolve a client path, rejecting anything outside MEDIA_ROOT."""
    p = Path(path).expanduser().resolve()
    if MEDIA_ROOT is not None and not p.is_relative_to(MEDIA_ROOT):
        raise ValueError(
            f"Path {path!r} is outside the media root ({MEDIA_ROOT}); "
            "place images under TOI_MEDIA_ROOT")
    return p


# Byte-payload image API for hosted/remote deployments: the client sends the
# image itself (base64 / data-URL), the server stores it inside the current
# scene's uploads/ dir — the client never sees or supplies any path.
# TOI_REMOTE_MODE=1 additionally removes the path-based tools from tools/list.
REMOTE_MODE = os.environ.get("TOI_REMOTE_MODE") == "1"
MAX_UPLOAD_MB = int(os.environ.get("TOI_MAX_UPLOAD_MB", "20"))


def _decode_image_b64(image_base64: str) -> bytes:
    """Decode a base64/data-URL payload with size and real-image validation."""
    data = (image_base64 or "").strip()
    if data.startswith("data:") and "," in data:
        data = data.split(",", 1)[1]
    data = "".join(data.split())  # tolerate wrapped/newlined base64
    limit = MAX_UPLOAD_MB * 1024 * 1024
    if len(data) > limit * 4 // 3 + 4:  # cheap pre-decode guard
        raise ValueError(f"Image exceeds TOI_MAX_UPLOAD_MB={MAX_UPLOAD_MB}")
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception as exc:
        raise ValueError("image_base64 is not valid base64") from exc
    if not raw or len(raw) > limit:
        raise ValueError(f"Image empty or over TOI_MAX_UPLOAD_MB={MAX_UPLOAD_MB}")
    import io
    from PIL import Image
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.verify()  # real image header + structure, not just bytes
    except Exception as exc:
        raise ValueError("image_base64 does not contain a decodable image") from exc
    return raw


def _save_upload(raw: bytes, filename: str = "") -> Path:
    """Store uploaded bytes under the current scene's uploads/ (isolated)."""
    stem, ext = os.path.splitext(filename or "")
    if ext.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        ext = ".png"
    name = hashlib.sha1(raw).hexdigest()[:12] + (f"-{_segment(stem)}" if stem else "") + ext
    up = SC().scene_path.parent / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    p = up / name
    p.write_bytes(raw)
    return p


def _set_base_image(p: Path) -> dict:
    """Shared core of load_image/load_image_data."""
    from PIL import Image
    with Image.open(p) as im:
        w, h = im.size
    _snapshot()
    SC().state.clear()
    SC().state.update({"image": str(p), "width": w, "height": h,
                   "effects": [], "objects": []})
    return _commit()


class Scene:
    """One isolated scene: state + undo/redo + lock + its own files."""

    def __init__(self, workspace: str, key: str):
        self.workspace, self.key = workspace, key
        if workspace == "default" and key == "default":
            self.dir = BASE_DIR  # legacy layout: <workdir>/scene.json
        else:
            self.dir = DATA_ROOT / _segment(workspace) / _segment(key)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.scene_path = self.dir / "scene.json"
        self.output_path = self.dir / "output.png"
        self.state: dict = S.load_state(self.scene_path)
        self.undo: deque = deque(maxlen=HISTORY_LEN)
        self.redo: deque = deque(maxlen=HISTORY_LEN)
        self.lock = threading.Lock()


class SceneStore:
    """LRU registry of live scenes. Disk is the source of truth (every
    mutation commits through _commit), so evicting an idle scene loses
    nothing except its in-memory undo history."""

    def __init__(self, max_scenes: int):
        self.max_scenes = max(1, max_scenes)
        self._live: "OrderedDict[tuple[str, str], Scene]" = OrderedDict()
        self._op = threading.Lock()

    def get(self, workspace: str, key: str) -> Scene:
        ident = (workspace, key)
        with self._op:
            sc = self._live.get(ident)
            if sc is None:
                sc = Scene(workspace, key)
                self._live[ident] = sc
            self._live.move_to_end(ident)
            while len(self._live) > self.max_scenes:
                victim = next((k for k in self._live if k != ident), None)
                if victim is None:
                    break
                self._live.pop(victim)  # caller may still hold this scene
            return sc

    def count(self) -> int:
        with self._op:
            return len(self._live)


_store = SceneStore(MAX_SCENES)
_current: contextvars.ContextVar[tuple[str, str] | None] = \
    contextvars.ContextVar("toi_scene", default=None)


def SC() -> Scene:
    """Scene of the current request (set by BearerAuthMiddleware). Outside a
    routed request (stdio mode / import-time) falls back to the default
    scene, preserving the single-tenant behavior."""
    ident = _current.get()
    if ident is None:
        ident = ("default", "default")
    return _store.get(*ident)


def _auth_tokens() -> dict[str, str]:
    """token -> workspace, from TOI_USERS='tokA=alice,tokB=bob' plus the
    legacy single TOI_AUTH_TOKEN (maps to workspace 'default')."""
    out: dict[str, str] = {}
    for pair in os.environ.get("TOI_USERS", "").split(","):
        tok, sep, ws = pair.partition("=")
        if sep and tok.strip():
            out[tok.strip()] = ws.strip() or "default"
    legacy = os.environ.get("TOI_AUTH_TOKEN", "").strip()
    if legacy:
        out.setdefault(legacy, "default")
    return out


class BearerAuthMiddleware:
    """Pure-ASGI auth + scene-routing gate (always installed around the app).

    Auth: `Authorization: Bearer <token>` or `?token=<token>` (clients that
    cannot set headers). Tokens come from _auth_tokens(); each maps to a
    workspace. Empty token set = auth off (single "default" workspace).
    Comparison is constant-time over every token (no early exit).

    Routing: resolves the scene key — ?scene= / X-TOI-Scene header /
    mcp-session-id header — and stores (workspace, key) in a contextvar the
    tools read via SC(). With LEGACY_DEFAULT (TOI_WORKDIR / TOI_SHARED_SCENE)
    the key is pinned to "default" unless given explicitly.
    """

    def __init__(self, app, tokens: dict[str, str]):
        self.app = app
        self.tokens = {tok.encode(): ws for tok, ws in tokens.items()}

    def _workspace(self, got: bytes) -> str | None:
        ws: str | None = None
        for tok, w in self.tokens.items():  # check all: no timing oracle
            if hmac.compare_digest(got, b"Bearer " + tok):
                ws = w
        return ws

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        qs = parse_qs(scope.get("query_string", b"").decode())
        got = headers.get(b"authorization", b"")
        if not got and qs.get("token"):
            got = f"Bearer {qs['token'][0]}".encode()
        if self.tokens:
            ws = self._workspace(got)
            if ws is None:
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [
                                (b"www-authenticate", b"Bearer"),
                                (b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        else:
            ws = "default"
        key = ((qs.get("scene") or [None])[0]
               or headers.get(b"x-toi-scene", b"").decode() or None)
        if not key and LEGACY_DEFAULT:
            key = "default"
        if not key:
            key = headers.get(b"mcp-session-id", b"").decode() or "default"
        tok = _current.set((ws, key))
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(tok)

mcp = MCPServer("text-on-image")

# The SDK runs sync tools on worker threads (anyio.to_thread), so concurrent
# tools/call can genuinely interleave. Each tool body is a transaction under
# the lock of ITS OWN scene: one client never blocks, waits on, or observes
# partial state of another client's scene. find -> snapshot -> mutate -> save
# -> render -> reply, all against the same Scene (SC(), request contextvar).


def serialized(fn):
    """Run the whole tool body under the current scene's lock (sync; threads)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with SC().lock:
            return fn(*args, **kwargs)
    return wrapper


def _find(object_id: str) -> dict:
    objs = SC().state["objects"]
    for obj in objs:
        if obj["id"] == object_id:
            return obj
    raise ValueError(f"No object with id '{object_id}'. "
                     f"Known ids: {[o['id'] for o in objs]}")


def _snapshot() -> None:
    sc = SC()
    sc.undo.append(copy.deepcopy(sc.state))
    sc.redo.clear()


def _commit(mutated: dict | None = None) -> dict:
    """Persist + re-render the current scene; compact result for the client."""
    sc = SC()
    S.save_state(sc.state, sc.scene_path)
    path = R.render(sc.state, sc.output_path)
    S.save_state(sc.state, sc.scene_path)  # render() updates width/height
    # Reply with a snapshot: the JSON conversion of the result happens after
    # the lock is released, it must not read the live dict.
    out = {"image_path": path, "state": copy.deepcopy(sc.state)}
    if mutated:
        out["object"] = mutated
    return out


@mcp.tool()
def list_fonts() -> dict:
    """List all installed font families and which styles each provides."""
    manifest = S.load_manifest()
    return {"count": len(manifest),
            "families": {f: sorted(s) for f, s in manifest.items()}}


@mcp.tool()
@serialized
def load_image(path: str) -> dict:
    """Set the base image (png/jpg/webp path, see TOI_MEDIA_ROOT), clear objects/effects."""
    p = _media_path(path)
    if not p.exists():
        raise FileNotFoundError(f"Image not found: {p}")
    return _set_base_image(p)


@mcp.tool()
@serialized
def add_text(text: str, family: str, size: int, x: float, y: float,
             bold: bool = False, italic: bool = False, color: str = "#ffffff",
             anchor: str = "top-left", angle: float = 0,
             outline_width: int = 0, outline_color: str = "#000000",
             line_spacing: float = 1.2, align: str = "left",
             opacity: float = 1.0, runs: list | None = None) -> dict:
    """Add a text object. anchor: top-left|top-center|top-right|center-left|
    center|center-right|bottom-left|bottom-center|bottom-right. angle rotates
    text; outline_width>0 adds a stroke; align is per-line (left|center|right).
    runs: optional rich-text segments on shared baselines, e.g.
    [{"text":"до","size":40},{"text":"100%","size":120,"bold":True}] - they
    override text/family/size/bold/italic/color per segment (angle ignored)."""
    outline = {"width": int(outline_width), "color": outline_color} if outline_width else None
    runs = _validate_runs(runs)
    obj = S.make_text_object(text, family, size, x, y, bold, italic, color,
                             anchor, angle, outline, line_spacing, align,
                             opacity, runs)
    _snapshot()
    SC().state["objects"].append(obj)
    return _commit(obj)


def _validate_runs(runs: list | None) -> list | None:
    if runs is None:
        return None
    if not isinstance(runs, list) or not runs:
        raise ValueError("runs must be a non-empty list of {text, ...} dicts")
    allowed = ("text", "family", "size", "bold", "italic", "color")
    out = []
    for r in runs:
        if not isinstance(r, dict) or "text" not in r:
            raise ValueError("each run must be a dict with a 'text' key")
        if r.get("family") is not None:
            S.font_path(r["family"], False, False)  # early family validation
        out.append({k: v for k, v in r.items() if k in allowed})
    return out


@mcp.tool()
@serialized
def add_shadow(object_id: str, dx: float = 4, dy: float = 6, blur: float = 8,
               color: str = "#000000", opacity: float = 0.6) -> dict:
    """Add/replace a drop shadow on any object (text, shape, badge, image)."""
    obj = _find(object_id)
    _snapshot()
    obj["shadow"] = {"dx": dx, "dy": dy, "blur": blur, "color": color,
                     "opacity": max(0.0, min(1.0, opacity))}
    return _commit(obj)


@mcp.tool()
@serialized
def add_glow(object_id: str, blur: float = 12, color: str = "#ffffff",
             opacity: float = 0.8) -> dict:
    """Add/replace a glow (blurred silhouette behind the object)."""
    obj = _find(object_id)
    _snapshot()
    obj["glow"] = {"blur": blur, "color": color,
                   "opacity": max(0.0, min(1.0, opacity))}
    return _commit(obj)


@mcp.tool()
@serialized
def remove_shadow(object_id: str) -> dict:
    """Remove drop shadow and glow from an object."""
    obj = _find(object_id)
    _snapshot()
    obj["shadow"] = None
    obj["glow"] = None
    return _commit(obj)


# --- state primitives (F): opacity, z-order, relative transforms ----------

@mcp.tool()
@serialized
def set_opacity(object_id: str, opacity: float) -> dict:
    """Set per-object opacity 0..1 (works for every object type)."""
    obj = _find(object_id)
    _snapshot()
    obj["opacity"] = max(0.0, min(1.0, opacity))
    return _commit(obj)


@mcp.tool()
@serialized
def bring_to_front(object_id: str) -> dict:
    """Move an object to the top of the z-order (rendered last)."""
    obj = _find(object_id)
    _snapshot()
    SC().state["objects"].remove(obj)
    SC().state["objects"].append(obj)
    return _commit(obj)


@mcp.tool()
@serialized
def send_to_back(object_id: str) -> dict:
    """Move an object to the back of the z-order (rendered first)."""
    obj = _find(object_id)
    _snapshot()
    SC().state["objects"].remove(obj)
    SC().state["objects"].insert(0, obj)
    return _commit(obj)


@mcp.tool()
@serialized
def move_object(object_id: str, index: int) -> dict:
    """Reposition an object in the z-order list (0 = back)."""
    obj = _find(object_id)
    _snapshot()
    SC().state["objects"].remove(obj)
    SC().state["objects"].insert(index, obj)
    return _commit(obj)


@mcp.tool()
@serialized
def move_text(object_id: str, dx: float = 0, dy: float = 0) -> dict:
    """Move any object by a relative offset (10px left = dx=-10)."""
    obj = _find(object_id)
    _snapshot()
    _translate(obj, dx, dy)
    return _commit(obj)


# --- alignment helpers: no new rendering, pure geometry over state ----------

def _translate(obj: dict, dx: float, dy: float) -> None:
    """Shift an object, including its absolute `points` if it has any."""
    obj["x"] = float(obj.get("x", 0)) + dx
    obj["y"] = float(obj.get("y", 0)) + dy
    if obj.get("points"):
        obj["points"] = [[px + dx, py + dy] for px, py in obj["points"]]


def _bbox_of(obj: dict) -> tuple[float, float, float, float]:
    """(l, t, r, b) of any object, in canvas px."""
    t = obj.get("type")
    if t == "text":
        bb = R.text_bbox(SC().state, obj)
        if not bb:
            raise ValueError(f"Object '{obj['id']}' renders empty - no bbox")
        return tuple(float(v) for v in bb)
    if t == "shape":
        if obj.get("points"):
            xs = [p[0] for p in obj["points"]]
            ys = [p[1] for p in obj["points"]]
            return (min(xs), min(ys), max(xs), max(ys))
        return (obj["x"], obj["y"], obj["x"] + obj["w"], obj["y"] + obj["h"])
    if t == "image":
        from PIL import Image
        with Image.open(obj["asset"]) as im:
            iw, ih = im.size
        w = float(obj["w"])
        h = float(obj["h"]) if obj.get("h") else w * ih / iw
        return (obj["x"], obj["y"], obj["x"] + w, obj["y"] + h)
    if t in ("badge", "callout"):
        font = S.get_font(
            S.font_path(obj["family"], obj.get("bold", True), False)[0],
            int(obj["size"]))
        tw, th = R._text_metrics(font, obj["text"], 1.15)
        px = float(obj.get("padding_x", obj["size"] * 0.9))
        py = float(obj.get("padding_y", obj["size"] * 0.45))
        return (obj["x"], obj["y"], obj["x"] + tw + 2 * px,
                obj["y"] + th + 2 * py)
    raise ValueError(f"Cannot measure object type '{t}'")


_ALIGN_EDGES = ("left", "right", "top", "bottom", "center_x", "center_y",
                "below", "above", "left_of", "right_of")


@mcp.tool()
@serialized
def align_object(object_id: str, edge: str, target: str = "canvas",
                 gap: float = 0) -> dict:
    """Align one object to the canvas or to another object (its id).
    edge: left|right|top|bottom|center_x|center_y (same-edge alignment;
    target=canvas: gap = margin from canvas edge; target=object: gap =
    signed offset from that object's edge) | below|above|left_of|right_of
    (stack against the target edge with gap px between; the other axis
    stays unchanged; needs an object target)."""
    obj = _find(object_id)
    if edge not in _ALIGN_EDGES:
        raise ValueError(f"Bad edge '{edge}'. Use: " + ", ".join(_ALIGN_EDGES))
    if edge in ("below", "above", "left_of", "right_of") and target == "canvas":
        raise ValueError(f"edge '{edge}' needs an object target (not canvas)")
    l, t, r, b = _bbox_of(obj)
    dx = dy = 0.0
    if target == "canvas":
        W, H = SC().state.get("width", 0), SC().state.get("height", 0)
        if not W or not H:
            raise ValueError("No base image loaded - cannot align to canvas")
        dx = {"left": gap - l, "right": (W - gap) - r,
              "center_x": W / 2 - (l + r) / 2}.get(edge, 0.0)
        dy = {"top": gap - t, "bottom": (H - gap) - b,
              "center_y": H / 2 - (t + b) / 2}.get(edge, 0.0)
    else:
        if target == obj["id"]:
            raise ValueError("Cannot align an object to itself")
        tl, tt, tr, tb = _bbox_of(_find(target))
        dx = {"left": tl + gap - l, "right": tr + gap - r,
              "center_x": (tl + tr) / 2 + gap - (l + r) / 2,
              "left_of": tl - gap - r, "right_of": tr + gap - l}.get(edge, 0.0)
        dy = {"top": tt + gap - t, "bottom": tb + gap - b,
              "center_y": (tt + tb) / 2 + gap - (t + b) / 2,
              "below": tb + gap - t, "above": tt - gap - b}.get(edge, 0.0)
    _snapshot()
    _translate(obj, dx, dy)
    return _commit(obj)


@mcp.tool()
@serialized
def align_group(ids: list, edge: str) -> dict:
    """Align several objects to one common line (the first id defines it).
    edge: left|right|top|bottom|center_x|center_y."""
    if not ids or len(ids) < 2:
        raise ValueError("align_group needs 2+ object ids")
    if edge not in ("left", "right", "top", "bottom", "center_x", "center_y"):
        raise ValueError("Bad edge for align_group")
    objs = [_find(i) for i in ids]
    l, t, r, b = _bbox_of(objs[0])
    ref = {"left": l, "right": r, "top": t, "bottom": b,
           "center_x": (l + r) / 2, "center_y": (t + b) / 2}[edge]
    _snapshot()
    for o in objs[1:]:
        ol, ot, orr, ob = _bbox_of(o)
        cur = {"left": ol, "right": orr, "top": ot, "bottom": ob,
               "center_x": (ol + orr) / 2, "center_y": (ot + ob) / 2}[edge]
        if edge in ("left", "right", "center_x"):
            _translate(o, ref - cur, 0)
        else:
            _translate(o, 0, ref - cur)
    return _commit({"aligned": [o["id"] for o in objs]})


@mcp.tool()
@serialized
def distribute(ids: list, axis: str = "horizontal",
               mode: str = "equal_gap") -> dict:
    """Evenly space 3+ objects along an axis. mode: equal_gap (first/last
    keep position, empty space between equalized) or equal_centers (centers
    evenly spaced between first and last center). Objects are processed in
    their current positional order along the axis."""
    if len(ids) < 3:
        raise ValueError("distribute needs 3+ object ids")
    if axis not in ("horizontal", "vertical"):
        raise ValueError("axis must be horizontal|vertical")
    if mode not in ("equal_gap", "equal_centers"):
        raise ValueError("mode must be equal_gap|equal_centers")
    objs = [_find(i) for i in ids]
    horiz = axis == "horizontal"
    bbs = [_bbox_of(o) for o in objs]
    lo = (lambda bb: bb[0]) if horiz else (lambda bb: bb[1])
    hi = (lambda bb: bb[2]) if horiz else (lambda bb: bb[3])
    move = (lambda o, d: _translate(o, d, 0)) if horiz else \
        (lambda o, d: _translate(o, 0, d))
    order = sorted(range(len(objs)), key=lambda i: (lo(bbs[i]) + hi(bbs[i])) / 2)
    _snapshot()
    if mode == "equal_gap":
        start = min(lo(bbs[i]) for i in order)
        span = max(hi(bbs[i]) for i in order) - start
        sizes = [hi(bbs[i]) - lo(bbs[i]) for i in order]
        gap = (span - sum(sizes)) / (len(order) - 1)
        pos = start
        for i, size in zip(order, sizes):
            move(objs[i], pos - lo(bbs[i]))
            pos += size + gap
    else:
        c0 = (lo(bbs[order[0]]) + hi(bbs[order[0]])) / 2
        c1 = (lo(bbs[order[-1]]) + hi(bbs[order[-1]])) / 2
        step = (c1 - c0) / (len(order) - 1)
        for k, i in enumerate(order):
            move(objs[i], c0 + k * step - (lo(bbs[i]) + hi(bbs[i])) / 2)
    return _commit({"distributed": [o["id"] for o in objs]})


@mcp.tool()
@serialized
def scale_text(object_id: str, factor: float = 1.0, size: int | None = None) -> dict:
    """Scale an object's font/size: factor=2 doubles, or pass absolute size px."""
    obj = _find(object_id)
    _snapshot()
    obj["size"] = int(size) if size is not None else max(
        1, int(round(obj["size"] * factor)))
    if obj["type"] == "badge":  # keep padding proportional
        obj["padding_x"] = obj["size"] * 0.9
        obj["padding_y"] = obj["size"] * 0.45
    return _commit(obj)


@mcp.tool()
@serialized
def resize_object(object_id: str, w: float | None = None,
                  h: float | None = None) -> dict:
    """Set absolute box size (px) for a shape or image object."""
    obj = _find(object_id)
    if obj["type"] not in ("shape", "image"):
        raise ValueError("resize_object works on shape/image objects")
    _snapshot()
    if w is not None:
        obj["w"] = float(w)
    if h is not None:
        obj["h"] = float(h)
    return _commit(obj)


@mcp.tool()
@serialized
def auto_fit_text(object_id: str, max_width: float) -> dict:
    """Shrink a text object's font size until its widest line fits max_width px."""
    from PIL import ImageFont
    obj = _find(object_id)
    if obj["type"] != "text":
        raise ValueError("auto_fit_text works on text objects")
    _snapshot()
    for size in range(obj["size"], 8, -1):
        if obj.get("runs"):
            obj["size"] = size
            if R.text_runs_width(obj) <= max_width:
                break
            continue
        path, _, _ = S.font_path(obj["family"], obj["bold"], obj["italic"])
        font = S.get_font(path, size)
        widest = max(font.getlength(line) for line in obj["text"].split("\n"))
        if widest <= max_width:
            obj["size"] = size
            break
    return _commit(obj)



# --- composite primitives (B+C+D) ------------------------------------------

@mcp.tool()
@serialized
def add_shape(kind: str, x: float, y: float, w: float, h: float,
              fill: str = "#ffffff", stroke: str | None = None,
              stroke_width: int = 0, corner_radius: float = 12,
              rotation: float = 0, opacity: float = 1.0,
              fill_gradient: dict | None = None) -> dict:
    """Add a shape. kind: rectangle|rounded_rectangle|ellipse|regular_polygon.
    For line/arrow/polygon use add_arrow / add_polygon instead.
    fill_gradient overrides fill: {"kind":"linear"|"radial","from":"#hex",
    "to":"#hex","angle":0} (angle: 0=from-top->to-bottom, rotates clockwise;
    radial: from at center)."""
    obj = S.make_shape_object(kind, x, y, w, h, fill, stroke, stroke_width,
                              corner_radius, None, 6, rotation, opacity,
                              fill_gradient)
    _snapshot()
    SC().state["objects"].append(obj)
    return _commit(obj)


@mcp.tool()
@serialized
def add_polygon(points: list, fill: str = "#ffffff", stroke: str | None = None,
                stroke_width: int = 0, opacity: float = 1.0) -> dict:
    """Add a free polygon. points: [[x,y],...] (3+). Bounding box is derived."""
    xs = [p[0] for p in points]; ys = [p[1] for p in points]
    obj = S.make_shape_object("polygon", min(xs), min(ys),
                              max(xs) - min(xs), max(ys) - min(ys), fill,
                              stroke, stroke_width, points=points, opacity=opacity)
    _snapshot()
    SC().state["objects"].append(obj)
    return _commit(obj)


@mcp.tool()
@serialized
def add_arrow(x1: float, y1: float, x2: float, y2: float, color: str = "#ffffff",
              stroke_width: int = 5, head_size: int = 0, opacity: float = 1.0) -> dict:
    """Add an arrow from (x1,y1) to (x2,y2) with a triangular head."""
    obj = S.make_shape_object("arrow", 0, 0, 0, 0, fill=color,
                              points=[[x1, y1], [x2, y2]], opacity=opacity)
    obj["stroke_width"] = int(stroke_width)
    if head_size:
        obj["head_size"] = int(head_size)
    _snapshot()
    SC().state["objects"].append(obj)
    return _commit(obj)


@mcp.tool()
@serialized
def add_badge(text: str, x: float, y: float, family: str = "Montserrat",
              size: int = 36, bold: bool = True, color: str = "#e53935",
              text_color: str = "#ffffff", opacity: float = 1.0) -> dict:
    """Add a pill badge; plate auto-sizes to fit the text at (x,y)=top-left."""
    obj = S.make_badge_object(text, x, y, family, size, bold, color,
                              text_color, opacity)
    _snapshot()
    SC().state["objects"].append(obj)
    return _commit(obj)


@mcp.tool()
@serialized
def add_callout(text: str, x: float, y: float, family: str = "Inter",
                size: int = 30, color: str = "#1e88e5",
                text_color: str = "#ffffff", tail: str = "down",
                opacity: float = 1.0) -> dict:
    """Add a callout bubble with a pointer tail (down|up|left|right)."""
    obj = S.make_callout_object(text, x, y, family, size, color, text_color,
                                tail, opacity)
    _snapshot()
    SC().state["objects"].append(obj)
    return _commit(obj)


@mcp.tool()
@serialized
def add_image(asset: str, x: float, y: float, w: int, h: int | None = None,
              fit: str = "contain", corner_radius: int = 0,
              opacity: float = 1.0) -> dict:
    """Overlay another image (logo/watermark/avatar). fit: contain|cover."""
    obj = S.make_image_object(str(_media_path(asset)), x, y, w, h, fit,
                              corner_radius, opacity)
    _snapshot()
    SC().state["objects"].append(obj)
    return _commit(obj)


@mcp.tool()
@serialized
def load_image_data(image_base64: str, filename: str = "") -> dict:
    """Set base image from base64 (or data: URL) bytes — no file paths.

    Stores the image inside the scene's own uploads dir; recommended entry
    point for hosted deployments (TOI_REMOTE_MODE=1)."""
    raw = _decode_image_b64(image_base64)
    return _set_base_image(_save_upload(raw, filename))


@mcp.tool()
@serialized
def add_image_data(image_base64: str, x: float, y: float, w: int,
                   h: int | None = None, fit: str = "contain",
                   corner_radius: int = 0, opacity: float = 1.0) -> dict:
    """Overlay an image from base64 bytes (logo/watermark; no file paths)."""
    raw = _decode_image_b64(image_base64)
    obj = S.make_image_object(str(_save_upload(raw, "")), x, y, w, h, fit,
                              corner_radius, opacity)
    _snapshot()
    SC().state["objects"].append(obj)
    return _commit(obj)



# --- edits, effects, history, state ----------------------------------------

@mcp.tool()
@serialized
def update_text(object_id: str, text: str | None = None, family: str | None = None,
                size: int | None = None, x: float | None = None,
                y: float | None = None, bold: bool | None = None,
                italic: bool | None = None, color: str | None = None,
                anchor: str | None = None, angle: float | None = None,
                line_spacing: float | None = None, align: str | None = None,
                opacity: float | None = None, runs: list | None = None) -> dict:
    """Set any absolute properties of an object (only provided ones change).
    runs=[] clears rich segments (falls back to plain text)."""
    obj = _find(object_id)
    if anchor is not None and anchor not in S.ANCHORS:
        raise ValueError(f"Bad anchor '{anchor}'.")
    _snapshot()
    if runs is not None:
        obj["runs"] = None if runs == [] else _validate_runs(runs)
    for key, val in (("text", text), ("family", family), ("size", size),
                     ("x", x), ("y", y), ("bold", bold), ("italic", italic),
                     ("color", color), ("anchor", anchor), ("angle", angle),
                     ("line_spacing", line_spacing), ("align", align),
                     ("opacity", opacity)):
        if val is not None:
            obj[key] = val
    return _commit(obj)


@mcp.tool()
@serialized
def update_shape(object_id: str, fill: str | None = None,
                 stroke: str | None = None, stroke_width: int | None = None,
                 corner_radius: float | None = None, rotation: float | None = None,
                 opacity: float | None = None,
                 fill_gradient: dict | None = None) -> dict:
    """Set absolute style fields of a shape/image/badge object.
    fill_gradient: replace the gradient; pass {} to remove it."""
    obj = _find(object_id)
    _snapshot()
    if fill_gradient is not None:
        if fill_gradient:
            S.make_shape_object(obj.get("kind", "rectangle"), 0, 0, 1, 1,
                                fill_gradient=fill_gradient)  # validate
            obj["fill_gradient"] = fill_gradient
        else:
            obj.pop("fill_gradient", None)  # {} removes the gradient
    for key, val in (("fill", fill), ("stroke", stroke),
                     ("stroke_width", stroke_width),
                     ("corner_radius", corner_radius),
                     ("opacity", opacity)):
        if val is not None:
            obj[key] = val
    if rotation is not None:
        obj["rotation"] = rotation
    return _commit(obj)


@mcp.tool()
@serialized
def apply_effect(kind: str, factor: float = 1.0, angle: float = 0,
                 direction: str = "h", width: int = 0, height: int = 0,
                 box: list | None = None, px: int = 20, color: str = "#000000",
                 strength: float = 0.5, radius: float = 15) -> dict:
    """Apply a global photo effect. kind: brightness|contrast|saturation|
    sharpness (factor), grayscale|sepia|flip (direction h|v), rotate (angle),
    resize (width,height), crop|blur_area (box [x,y,w,h]), pad (px,color),
    tint|vignette (color,strength / strength, radius for blur_area)."""
    eff: dict = {"kind": kind}
    if kind in ("brightness", "contrast", "saturation", "sharpness"):
        eff["factor"] = factor
    elif kind == "rotate":
        eff["angle"] = angle
    elif kind == "flip":
        eff["direction"] = direction
    elif kind == "resize":
        eff["width"], eff["height"] = width, height
    elif kind in ("crop", "blur_area"):
        eff["box"] = box
        if kind == "blur_area":
            eff["radius"] = radius
    elif kind == "pad":
        eff["px"], eff["color"] = px, color
    elif kind == "tint":
        eff["color"], eff["strength"] = color, strength
    elif kind == "vignette":
        eff["strength"] = strength
    _snapshot()
    R.apply_effect(_load_base(), eff)  # validate early
    SC().state["effects"].append(eff)
    return _commit({"effect": eff})


def _load_base():
    from PIL import Image
    with Image.open(SC().state["image"]) as im:
        return im.convert("RGBA")


@mcp.tool()
@serialized
def clear_effects() -> dict:
    """Remove all global photo effects."""
    _snapshot()
    SC().state["effects"] = []
    return _commit()


@mcp.tool()
@serialized
def delete_object(object_id: str) -> dict:
    """Delete an object from the scene."""
    obj = _find(object_id)
    _snapshot()
    SC().state["objects"].remove(obj)
    return _commit()


@mcp.tool()
@serialized
def clear_objects() -> dict:
    """Remove all objects (keeps base image and effects)."""
    _snapshot()
    SC().state["objects"] = []
    return _commit()



@mcp.tool()
@serialized
def undo() -> dict:
    """Undo the last change (up to 25 steps)."""
    if not SC().undo:
        raise ValueError("Nothing to undo")
    SC().redo.append(copy.deepcopy(SC().state))
    SC().state.clear()
    SC().state.update(SC().undo.pop())
    return _commit()


@mcp.tool()
@serialized
def redo() -> dict:
    """Redo a previously undone change."""
    if not SC().redo:
        raise ValueError("Nothing to redo")
    SC().undo.append(copy.deepcopy(SC().state))
    SC().state.clear()
    SC().state.update(SC().redo.pop())
    return _commit()


@mcp.tool()
@serialized
def get_state() -> dict:
    """Return the full scene JSON (image, effects, all objects with styles)."""
    return copy.deepcopy(SC().state)


@mcp.tool()
@serialized
def measure_text(object_id: str) -> dict:
    """Return the rendered tight bbox [l,t,r,b] of a text object (for layout)."""
    obj = _find(object_id)
    if obj["type"] != "text":
        raise ValueError("measure_text works on text objects")
    return {"object_id": object_id, "bbox": R.text_bbox(SC().state, obj)}


@mcp.tool()
@serialized
def set_state(state: dict) -> dict:
    """Replace the whole scene state with client-provided JSON and re-render."""
    if "objects" not in state:
        raise ValueError("state must contain an 'objects' list")
    _snapshot()
    SC().state.clear()
    SC().state.update(state)
    SC().state.setdefault("effects", [])
    return _commit()


@mcp.tool()
@serialized
def scene_info() -> dict:
    """Diagnostics of the current scene: workspace, scene key, storage dir,
    object count, undo depth and how many scenes are live in this process."""
    sc = SC()
    return {"workspace": sc.workspace, "scene": sc.key, "dir": str(sc.dir),
            "objects": len(SC().state["objects"]), "undo_steps": len(sc.undo),
            "live_scenes": _store.count(),
            "pinned_default": LEGACY_DEFAULT}


@mcp.tool()
@serialized
def render(preview_width: int = 1024) -> dict:
    """Re-render the scene; returns output path plus base64 PNG and state.
    preview_width adds a downscaled JPEG (preview_base64/preview_mime) meant
    for LLM vision to keep request payloads small; 0 disables it.
    image_base64 always stays full-resolution."""
    path = R.render(SC().state, SC().output_path)
    S.save_state(SC().state, SC().scene_path)
    out = {"image_path": path, "mime": "image/png",
           "image_base64": base64.b64encode(Path(path).read_bytes()).decode("ascii"),
           "state": copy.deepcopy(SC().state)}
    if preview_width and preview_width > 0:
        import io
        from PIL import Image
        with Image.open(path) as im:
            if im.width > preview_width:
                ratio = preview_width / im.width
                im = im.resize((preview_width,
                                max(1, int(im.height * ratio))), Image.BICUBIC)
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=80)
        out["preview_mime"] = "image/jpeg"
        out["preview_base64"] = base64.b64encode(buf.getvalue()).decode("ascii")
    return out


# TOI_REMOTE_MODE=1 (hosted deployment): the byte-payload tools above fully
# replace the path-based ones — remove them from tools/list so remote agents
# never see or can call a local-filesystem API.
if REMOTE_MODE:
    mcp.remove_tool("load_image")
    mcp.remove_tool("add_image")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    tokens = _auth_tokens()
    print(f"text-on-image MCP server: http://{args.host}:{args.port}/mcp"
          + (f"  [bearer auth ON, {len(tokens)} token(s)]" if tokens
             else "  [no auth]")
          + ("  [pinned default scene]" if LEGACY_DEFAULT else
             "  [scenes by ?scene=/session id]"))
    # Always behind the routing gate (auth may be off, scene routing is not):
    # it stamps each request's (workspace, scene) contextvar. uvicorn runs the
    # Starlette lifespan because non-http scopes pass through untouched.
    import uvicorn
    app = BearerAuthMiddleware(mcp.streamable_http_app(host=args.host),
                               tokens)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")

