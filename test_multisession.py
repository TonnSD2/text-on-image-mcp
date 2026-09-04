#!/usr/bin/env python3
"""Multi-scene regression test. Spawns its own server (port 8098, temp
TOI_DATA, two users) and verifies: per-scene isolation, cross-workspace
isolation, per-MCP-session fallback, persistence across reconnects with the
same ?scene=, undo per scene, and true parallel mutation of two scenes.

Usage: python test_multisession.py     (starts/stops its own server)
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx2 as httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE = Path(__file__).resolve().parent
PORT = 8098
URL = f"http://127.0.0.1:{PORT}/mcp"
TOK_A, TOK_B = "tokA-ms", "tokB-ms"


def payload(res) -> dict:
    sc = (getattr(res, "structured_content", None)
          or getattr(res, "structuredContent", None))
    if sc is not None:
        sc = dict(sc)
        if "result" in sc and isinstance(sc["result"], dict):
            return sc["result"]
        return sc
    if res.content and res.content[0].text:
        return json.loads(res.content[0].text)
    raise RuntimeError("empty tool result")


def make_photo(directory: Path) -> str:
    from PIL import Image
    img = Image.new("RGB", (400, 300), (10, 40, 90))
    out = directory / "photo.png"
    img.save(out)
    return str(out)


class Client:
    """One MCP connection; scene addressed via URL query (token + scene)."""

    def __init__(self, token: str, scene: str | None = None):
        q = f"token={token}" + (f"&scene={scene}" if scene else "")
        self.url = f"{URL}?{q}"
        self._stack: list = []

    async def __aenter__(self):
        client = httpx.AsyncClient(timeout=30)
        ctx1 = streamable_http_client(self.url, http_client=client)
        r, w = await ctx1.__aenter__()
        ctx2 = ClientSession(r, w)
        s = await ctx2.__aenter__()
        await s.initialize()
        self._stack = [ctx2, ctx1]
        self.session = s
        return self

    async def __aexit__(self, *exc):
        for ctx in self._stack:
            await ctx.__aexit__(*exc)

    async def call(self, name, **kw):
        res = await self.session.call_tool(name, kw)
        assert not res.is_error, f"{name} FAILED: {res.content}"
        return payload(res)


async def main() -> None:
    media = Path(tempfile.mkdtemp(prefix="toi-ms-media-"))
    outside_dir = Path(tempfile.mkdtemp(prefix="toi-ms-out-"))
    photo = make_photo(media)             # inside the media root
    outside = make_photo(outside_dir)     # outside: must be rejected
    data = tempfile.mkdtemp(prefix="toi-ms-data-")
    env = {**os.environ, "TOI_USERS": f"{TOK_A}=alice,{TOK_B}=bob",
           "TOI_DATA": data, "TOI_MEDIA_ROOT": str(media),
           "PYTHONPATH": str(BASE)}
    proc = subprocess.Popen(
        [sys.executable, str(BASE / "server.py"), "--port", str(PORT)],
        env=env, stdout=subprocess.DEVNULL,
        stderr=open("/tmp/ms-server.log", "wb"))
    try:
        for _ in range(50):  # wait until the port answers (any status)
            try:
                async with httpx.AsyncClient() as c:
                    await c.post(URL, timeout=1)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                await asyncio.sleep(0.2)
        else:
            raise RuntimeError("server did not start")

        # ---- 1. isolation of two explicit scenes (same user)
        async with Client(TOK_A, "job-1") as j1, \
                Client(TOK_A, "job-2") as j2:
            await j1.call("load_image", path=photo)
            await j2.call("load_image", path=photo)
            await j1.call("add_text", text="ONLY-J1", family="Inter",
                          size=30, x=10, y=10)
            await j2.call("add_text", text="ONLY-J2", family="Inter",
                          size=30, x=10, y=10)
            s1 = (await j1.call("get_state"))
            s2 = (await j2.call("get_state"))
            assert [o["text"] for o in s1["objects"]] == ["ONLY-J1"]
            assert [o["text"] for o in s2["objects"]] == ["ONLY-J2"]
            p1 = (await j1.call("render"))["image_path"]
            p2 = (await j2.call("render"))["image_path"]
            assert p1 != p2, "scenes share output.png"
            info = await j1.call("scene_info")
            assert info["workspace"] == "alice" and info["scene"] == "job-1"
            print("1: explicit ?scene= isolation + scene_info OK")

            # ---- 2. parallel mutation of the two scenes
            await asyncio.gather(*[
                c.call("add_text", text=f"P{k}", family="Inter",
                       size=20, x=10, y=10)
                for k, c in enumerate((j1, j2, j1, j2, j1, j2))])
            s1 = (await j1.call("get_state"))
            s2 = (await j2.call("get_state"))
            assert len(s1["objects"]) == len(s2["objects"]) == 4, \
                (len(s1["objects"]), len(s2["objects"]))
            print("2: parallel mutations of two scenes OK")

            # ---- 3. undo is per scene
            obj_id = s2["objects"][0]["id"]
            await j1.call("undo")
            s1 = (await j1.call("get_state"))
            s2 = (await j2.call("get_state"))
            assert len(s1["objects"]) == 3 and len(s2["objects"]) == 4
            print("3: per-scene undo OK")

        # ---- 4. same ?scene= after reconnect -> state survives
        async with Client(TOK_A, "job-2") as j2:
            s2 = (await j2.call("get_state"))
            assert len(s2["objects"]) == 4, "scene not persisted on disk"
            await j2.call("move_text", object_id=obj_id, dx=5, dy=0)
            print("4: reconnect with same ?scene= keeps scene OK")

        # ---- 5. no ?scene= -> per-MCP-session scene (no silent sharing)
        async with Client(TOK_A) as c1, Client(TOK_A) as c2:
            i1 = await c1.call("scene_info")
            i2 = await c2.call("scene_info")
            assert i1["scene"] != i2["scene"], "session fallback shared!"
            await c1.call("load_image", path=photo)
            await c1.call("add_text", text="C1", family="Inter", size=20,
                          x=5, y=5)
            s2 = (await c2.call("get_state"))
            assert s2["objects"] == [], "conn2 sees conn1's scene"
            print("5: no ?scene= -> isolated per-session scenes OK")

        # ---- 6. cross-workspace isolation (bob vs alice, same key)
        async with Client(TOK_B, "job-1") as bob:
            sb = (await bob.call("get_state"))
            assert sb["objects"] == [], "bob sees alice's job-1"
            await bob.call("load_image", path=photo)
            bi = await bob.call("scene_info")
            assert bi["workspace"] == "bob"
            print("6: workspace isolation across tokens OK")

        # ---- 7. auth still enforced
        async with httpx.AsyncClient() as c:
            r = await c.post(URL, timeout=5)
            assert r.status_code == 401, r.status_code
        print("7: unauthenticated request rejected OK")

        # ---- 8. TOI_MEDIA_ROOT confinement of client-supplied paths
        link = media / "escape.png"      # symlink inside root -> file outside
        os.symlink(outside, link)
        async with Client(TOK_A, "media") as m:
            await m.call("load_image", path=photo)                  # OK
            await m.call("add_image", asset=photo, x=10, y=10, w=100)  # OK
            for bad, why in ((outside, "outside file"),
                             (str(link), "symlink escape"),
                             (str(media / ".." / "x" / "photo.png"),
                              "dot-dot traversal")):
                res = await m.session.call_tool("load_image", {"path": bad})
                assert res.is_error, f"load_image allowed {why}: {res.content}"
                res = await m.session.call_tool(
                    "add_image", {"asset": bad, "x": 0, "y": 0, "w": 50})
                assert res.is_error, f"add_image allowed {why}: {res.content}"
            st = await m.call("get_state")   # OK-path state intact
            assert st["image"] == str(Path(photo).resolve())
        print("8: TOI_MEDIA_ROOT confinement OK")

        print("\nMULTISCENE TEST PASSED")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(media, ignore_errors=True)
        shutil.rmtree(outside_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())