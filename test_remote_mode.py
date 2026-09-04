#!/usr/bin/env python3
"""Remote-mode regression test. Spawns its own server (port 8099, temp
TOI_DATA, TOI_REMOTE_MODE=1) and verifies the hosted deployment contract:
path-based tools are gone from tools/list; the whole infographic flow works
with byte payloads only (load_image_data / add_image_data); bad payloads are
rejected; uploads are isolated per scene.

Usage: python test_remote_mode.py     (starts/stops its own server)
"""
from __future__ import annotations

import asyncio
import base64
import io
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
PORT = 8099
URL = f"http://127.0.0.1:{PORT}/mcp"
TOK = "tokRm"


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


def make_photo_b64(color=(10, 40, 90), size=(400, 300)) -> str:
    from PIL import Image
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class Client:
    def __init__(self, scene: str | None = None):
        q = f"token={TOK}" + (f"&scene={scene}" if scene else "")
        self.url = f"{URL}?{q}"
        self._stack: list = []

    async def __aenter__(self):
        client = httpx.AsyncClient(timeout=60)
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
    data = tempfile.mkdtemp(prefix="toi-rm-data-")
    env = {**os.environ, "TOI_USERS": f"{TOK}=me",
           "TOI_DATA": data, "TOI_REMOTE_MODE": "1",
           "PYTHONPATH": str(BASE)}
    proc = subprocess.Popen(
        [sys.executable, str(BASE / "server.py"), "--port", str(PORT)],
        env=env, stdout=subprocess.DEVNULL,
        stderr=open("/tmp/rm-server.log", "wb"))
    try:
        for _ in range(50):  # wait until the port answers
            try:
                async with httpx.AsyncClient() as c:
                    await c.post(URL, timeout=1)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                await asyncio.sleep(0.2)
        else:
            raise RuntimeError("server did not start")

        photo_b64 = make_photo_b64()
        logo_b64 = make_photo_b64((200, 30, 30), (80, 80))

        async with Client("job-1") as c:
            # ---- 1. path-based tools must not be exposed
            names = {t.name for t in (await c.session.list_tools()).tools}
            assert "load_image" not in names and "add_image" not in names, \
                "path tools leaked into remote-mode tools/list"
            assert {"load_image_data", "add_image_data"} <= names
            assert len(names) == 36, f"expected 36 tools, got {len(names)}"
            print("1: tools/list = 36, no path tools OK")

            # ---- 2. calling a removed tool fails
            res = await c.session.call_tool("load_image",
                                            {"path": "/etc/passwd"})
            assert res.is_error, "load_image callable in remote mode!"
            print("2: load_image call rejected OK")

            # ---- 3. full flow with bytes only
            st = await c.call("load_image_data", image_base64=photo_b64,
                              filename="photo.png")
            assert st["state"]["width"] == 400, st["state"]["width"]
            await c.call("add_text", text="REMOTE-OK", family="Inter",
                         size=36, x=20, y=20)
            obj = await c.call("add_image_data", image_base64=logo_b64,
                               x=10, y=200, w=60)
            assert obj["object"]["type"] == "image"
            out = await c.call("render")
            assert out["image_base64"], "no render output"
            assert Path(out["image_path"]).stat().st_size > 0
            print("3: base64 load -> text -> overlay -> render OK")

            # ---- 4. uploads live inside the scene dir, isolated per scene
            info = await c.call("scene_info")
            up = Path(info["dir"]) / "uploads"
            assert up.is_dir() and any(up.iterdir()), "no uploads stored"
        async with Client("job-2") as c2:
            d2 = Path((await c2.call("scene_info"))["dir"])
            up2 = d2 / "uploads"
            assert not up2.is_dir() or not any(up2.iterdir()), \
                "uploads leaked across scenes"
            s2 = await c2.call("get_state")
            assert not s2.get("image"), "scene-2 has an image"
            print("4: uploads isolated per scene OK")

        # ---- 5. bad payloads rejected, data-URL accepted
        async with Client("job-3") as c3:
            for bad, why in (("not-base64-!!!", "invalid base64"),
                             (base64.b64encode(b"hello world").decode(),
                              "not an image"),
                             ("", "empty")):
                res = await c3.session.call_tool(
                    "load_image_data", {"image_base64": bad})
                assert res.is_error, f"accepted {why}"
            st = await c3.call("load_image_data",
                               image_base64=f"data:image/png;base64,{photo_b64}")
            assert st["state"]["width"] == 400
            print("5: bad payloads rejected, data-URL accepted OK")

        print("\nREMOTE MODE TEST PASSED")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())