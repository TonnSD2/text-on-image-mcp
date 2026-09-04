# GUARD: This file is protected — see AGENTS.md. AI agents: do NOT modify,
# refactor or delete without the user's EXPLICIT in-conversation confirmation.
"""Scene state: JSON is the single source of truth, rendering lives in
render.py (image = pure function of this state; Pillow/CPU only)."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from PIL import ImageFont

BASE = Path(__file__).resolve().parent
FONTS_DIR = BASE / "fonts"
MANIFEST_PATH = FONTS_DIR / "fonts.json"
DEFAULT_SCENE_PATH = BASE / "scene.json"
DEFAULT_OUTPUT_PATH = BASE / "output.png"

# MCP-facing anchor names -> Pillow anchor strings
ANCHORS = {
    "top-left": "la", "top-center": "ma", "top-right": "ra",
    "center-left": "la", "center": "mm", "center-right": "ra",
    "bottom-left": "lo", "bottom-center": "mo", "bottom-right": "ro",
}

FAKE_ITALIC_SHEAR = 0.25

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_manifest_cache: dict[str, Any] | None = None


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    s = color.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore


def load_manifest() -> dict[str, Any]:
    global _manifest_cache
    if _manifest_cache is None:
        if not MANIFEST_PATH.exists():
            raise RuntimeError(
                "fonts/fonts.json not found - run: python download_fonts.py")
        _manifest_cache = json.loads(MANIFEST_PATH.read_text())
    return _manifest_cache


def font_path(family: str, bold: bool, italic: bool) -> tuple[str, bool, bool]:
    """Resolve TTF for family/style; returns (path, faux_bold, faux_italic)."""
    manifest = load_manifest()
    if family not in manifest:
        raise ValueError(
            f"Unknown font family '{family}'. Available: "
            + ", ".join(sorted(manifest)))
    style = {(0, 0): "regular", (0, 1): "italic",
             (1, 0): "bold", (1, 1): "bold_italic"}[(int(bold), int(italic))]
    styles = manifest[family]
    if style in styles:
        return str(BASE / styles[style]), False, False
    if bold and italic and "bold" in styles:
        return str(BASE / styles["bold"]), False, True
    if bold and "regular" in styles:
        return str(BASE / styles["regular"]), True, italic
    if italic and "regular" in styles:
        return str(BASE / styles["regular"]), False, True
    return str(BASE / styles["regular"]), bold, italic


def get_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    key = (path, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(path, size)
    return _font_cache[key]


def default_state() -> dict:
    return {"image": None, "width": 0, "height": 0, "effects": [],
            "objects": []}


def load_state(path: Path = DEFAULT_SCENE_PATH) -> dict:
    if Path(path).exists():
        state = json.loads(Path(path).read_text())
        state.setdefault("effects", [])   # v1 scenes stay compatible
        return state
    return default_state()


def save_state(state: dict, path: Path = DEFAULT_SCENE_PATH) -> None:
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False))


def new_id(prefix: str = "t") -> str:
    return f"{prefix}{uuid.uuid4().hex[:6]}"


def _check_anchor(anchor: str) -> None:
    if anchor not in ANCHORS:
        raise ValueError(f"Bad anchor '{anchor}'. Use one of: "
                         + ", ".join(ANCHORS))


# --------------------------------------------------------------------------
# object factories (type is the discriminator; render.py knows each type)
# --------------------------------------------------------------------------

def make_text_object(text: str, family: str, size: int, x: float, y: float,
                     bold: bool = False, italic: bool = False,
                     color: str = "#ffffff", anchor: str = "top-left",
                     angle: float = 0, outline: dict | None = None,
                     line_spacing: float = 1.2, align: str = "left",
                     opacity: float = 1.0, runs: list | None = None) -> dict:
    _check_anchor(anchor)
    return {
        "id": new_id(), "type": "text", "text": text, "family": family,
        "size": int(size), "bold": bool(bold), "italic": bool(italic),
        "x": float(x), "y": float(y), "anchor": anchor, "color": color,
        "angle": float(angle), "outline": outline, "glow": None,
        "line_spacing": float(line_spacing), "align": align,
        "opacity": float(opacity), "shadow": None,
        # rich runs: [{text, family?, size?, bold?, italic?, color?}, ...]
        # when present they replace `text`/`family`/... on one shared baseline
        "runs": runs,
    }


def make_shape_object(kind: str, x: float, y: float, w: float, h: float,
                      fill: str = "#ffffff", stroke: str | None = None,
                      stroke_width: int = 0, corner_radius: float = 12,
                      points: list | None = None, sides: int = 6,
                      rotation: float = 0, opacity: float = 1.0,
                      fill_gradient: dict | None = None) -> dict:
    from render import KINDS
    if kind not in KINDS:
        raise ValueError(f"Bad kind '{kind}'. Use one of: "
                         + ", ".join(sorted(KINDS)))
    if kind in ("line", "arrow", "polygon"):
        if not points or len(points) < 2:
            raise ValueError(f"kind '{kind}' requires 'points' [[x,y],...]")
    if fill_gradient:
        kind_g = fill_gradient.get("kind", "linear")
        if kind_g not in ("linear", "radial"):
            raise ValueError("fill_gradient.kind must be linear|radial")
        for key in ("from", "to"):
            if not fill_gradient.get(key):
                raise ValueError(f"fill_gradient needs '{key}' color")
    return {
        "id": new_id("s"), "type": "shape", "kind": kind,
        "x": float(x), "y": float(y), "w": float(w), "h": float(h),
        "fill": fill, "stroke": stroke, "stroke_width": int(stroke_width),
        "corner_radius": float(corner_radius), "points": points,
        "sides": int(sides), "sides_rotation": 0, "rotation": float(rotation),
        "opacity": float(opacity), "shadow": None,
        "fill_gradient": fill_gradient,
    }


def make_badge_object(text: str, x: float, y: float, family: str = "Montserrat",
                      size: int = 36, bold: bool = True, color: str = "#e53935",
                      text_color: str = "#ffffff", opacity: float = 1.0) -> dict:
    return {
        "id": new_id("b"), "type": "badge", "text": text, "x": float(x),
        "y": float(y), "family": family, "size": int(size), "bold": bool(bold),
        "color": color, "text_color": text_color, "padding_x": size * 0.9,
        "padding_y": size * 0.45, "opacity": float(opacity), "shadow": None,
    }


def make_callout_object(text: str, x: float, y: float, family: str = "Inter",
                        size: int = 30, color: str = "#1e88e5",
                        text_color: str = "#ffffff", tail: str = "down",
                        opacity: float = 1.0) -> dict:
    if tail not in ("down", "up", "left", "right"):
        raise ValueError("tail must be down|up|left|right")
    return {
        "id": new_id("c"), "type": "callout", "text": text, "x": float(x),
        "y": float(y), "family": family, "size": int(size), "bold": True,
        "color": color, "text_color": text_color, "tail": tail,
        "corner_radius": 14, "opacity": float(opacity), "shadow": None,
    }


def make_image_object(asset: str, x: float, y: float, w: int,
                      h: int | None = None, fit: str = "contain",
                      corner_radius: int = 0, opacity: float = 1.0) -> dict:
    if fit not in ("contain", "cover"):
        raise ValueError("fit must be contain|cover")
    if not Path(asset).exists():
        raise FileNotFoundError(f"Image not found: {asset}")
    return {
        "id": new_id("i"), "type": "image", "asset": str(Path(asset).resolve()),
        "x": float(x), "y": float(y), "w": int(w),
        "h": int(h) if h is not None else None, "fit": fit,
        "corner_radius": int(corner_radius), "opacity": float(opacity),
        "shadow": None,
    }

