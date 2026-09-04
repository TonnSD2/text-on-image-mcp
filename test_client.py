# GUARD: This file is protected — see AGENTS.md. AI agents: do NOT modify,
# refactor or delete without the user's EXPLICIT in-conversation confirmation.
#!/usr/bin/env python3
"""Live e2e test over Streamable HTTP. python server.py & then python test_client.py"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE = Path(__file__).resolve().parent
URL = os.environ.get("TOI_URL", "http://127.0.0.1:8080/mcp")
AUTH = os.environ.get("TOI_AUTH_TOKEN", "").strip()


def make_test_photo() -> str:
    from PIL import Image, ImageDraw
    w, h = 1200, 800
    img = Image.new("RGB", (w, h))
    top, bottom = (30, 60, 114), (160, 220, 240)
    for y in range(h):
        t = y / (h - 1)
        row = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom))
        for x in range(w):
            img.putpixel((x, y), row)
    d = ImageDraw.Draw(img, "RGBA")
    d.ellipse((850, 80, 1150, 380), fill=(255, 214, 90, 220))
    d.polygon([(0, 800), (350, 480), (700, 800)], fill=(40, 90, 70, 200))
    d.polygon([(500, 800), (850, 520), (1200, 800)], fill=(25, 60, 50, 220))
    out = BASE / "test_photo.png"
    img.save(out)
    return str(out)


def make_logo() -> str:
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (300, 120), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 297, 117), radius=24, fill=(17, 122, 102, 255))
    d.text((150, 60), "LOGO", fill="white", anchor="mm")
    out = BASE / "test_logo.png"
    img.save(out)
    return str(out)


def payload(res) -> dict:
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    if sc is not None:
        sc = dict(sc)
        if "result" in sc and isinstance(sc["result"], dict):
            return sc["result"]
        return sc
    if res.content and res.content[0].text:
        return json.loads(res.content[0].text)
    raise RuntimeError("empty tool result")


def connect():
    """streamable_http_client with bearer auth when TOI_AUTH_TOKEN is set."""
    if AUTH:
        try:
            import httpx  # mcp<3
        except ModuleNotFoundError:
            import httpx2 as httpx  # httpx fork used by mcp 2.x
        return streamable_http_client(
            URL, http_client=httpx.AsyncClient(
                headers={"Authorization": f"Bearer {AUTH}"}))
    return streamable_http_client(URL)


async def main() -> None:
    photo = make_test_photo()
    logo = make_logo()
    async with connect() as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"TOOLS ({len(names)}):", ", ".join(names))

            async def call(name, **kw):
                res = await s.call_tool(name, kw)
                assert not res.is_error, f"{name} FAILED: {res.content}"
                return payload(res)

            fonts = await call("list_fonts")
            assert fonts["count"] >= 33, "expected 33+ families"
            for extra in ("Lobster", "Pacifico", "Caveat"):
                assert extra in fonts["families"], f"missing {extra}"
            print(f"\nFONTS: {fonts['count']} OK")
            await call("load_image", path=photo)

            # ---- A: text upgrades
            r1 = await call("add_text", text="SUMMER SALE", family="Montserrat",
                            size=64, x=80, y=80, bold=True, outline_width=3,
                            outline_color="#000000")
            t1 = r1["object"]["id"]
            assert r1["object"]["outline"]["width"] == 3
            r2 = await call("add_text", text="HOT\nDEAL", family="Oswald",
                            size=48, x=60, y=300, italic=True, angle=8,
                            align="center")
            fit = await call("auto_fit_text", object_id=t1, max_width=300)
            assert fit["object"]["size"] < 64, "auto_fit should shrink 64px"
            m = await call("measure_text", object_id=t1)
            assert m["bbox"] and m["bbox"][2] > m["bbox"][0]
            print(f"A: text angle/outline/autofit({fit['object']['size']}px)/measure OK")

            # ---- B: shapes
            sh = await call("add_shape", kind="rounded_rectangle", x=700, y=600,
                            w=200, h=90, fill="#e53935", corner_radius=18,
                            rotation=10, opacity=0.9)
            sid = sh["object"]["id"]
            await call("add_shape", kind="ellipse", x=40, y=600, w=140, h=140,
                       fill="#fbc02d")
            st = await call("add_shape", kind="regular_polygon", x=250, y=600,
                            w=120, h=120, fill="#ffffff")
            await call("add_polygon", points=[[950, 450], [1050, 430], [1030, 520]],
                       fill="#8e24aa", opacity=0.7)
            ar = await call("add_arrow", x1=560, y1=200, x2=720, y2=300,
                            color="#ffeb3b", stroke_width=6)
            print(f"B: rect/ellipse/star/polygon + arrow OK ({sid}, {ar['object']['id']})")

            # ---- C: badge & callout
            bd = await call("add_badge", text="-50%", x=900, y=120, size=40,
                            color="#e91e63")
            bid = bd["object"]["id"]
            co = await call("add_callout", text="только сегодня!", x=560, y=520,
                            size=26, color="#1e88e5", tail="up")
            print(f"C: badge + callout OK ({bid}, {co['object']['id']})")

            # ---- D: overlay image
            im = await call("add_image", asset=logo, x=950, y=620, w=180,
                            corner_radius=14, opacity=0.95)
            print(f"D: overlay image OK ({im['object']['id']})")

            # ---- E: global effects
            await call("apply_effect", kind="brightness", factor=1.08)
            await call("apply_effect", kind="vignette", strength=0.5)
            await call("apply_effect", kind="blur_area", box=[40, 40, 120, 80], radius=10)
            assert len((await call("get_state"))["effects"]) == 3
            print("E: brightness + vignette + blur_area effects OK")

            # ---- F: opacity, z-order, undo/redo
            await call("set_opacity", object_id=sid, opacity=0.5)
            await call("bring_to_front", object_id=bid)
            await call("send_to_back", object_id=sid)
            zst = await call("get_state")
            assert zst["objects"][0]["id"] == sid, "send_to_back failed"
            assert zst["objects"][-1]["id"] == bid, "bring_to_front failed"
            before = len(zst["objects"])
            await call("delete_object", object_id=st["object"]["id"])
            after = len((await call("get_state"))["objects"])
            assert after == before - 1
            await call("undo")
            assert len((await call("get_state"))["objects"]) == before, "undo failed"
            await call("redo")
            assert len((await call("get_state"))["objects"]) == after, "redo failed"
            print("F: opacity + z-order + undo/redo OK")

            # ---- G: rich runs, align/distribute, gradients, script fonts
            gr = await call("add_text", text="до 100%", family="Montserrat",
                            size=120, x=600, y=420, anchor="center",
                            runs=[{"text": "до ", "size": 44},
                                  {"text": "100%", "bold": True},
                                  {"text": " чист", "size": 50, "italic": True}])
            gid = gr["object"]["id"]
            bb = (await call("measure_text", object_id=gid))["bbox"]
            assert abs((bb[0] + bb[2]) / 2 - 600) < 20, "runs anchor=center off"
            gfit = await call("auto_fit_text", object_id=gid, max_width=200)
            assert gfit["object"]["size"] < 120, "runs auto_fit failed"
            await call("update_text", object_id=gid, runs=[])  # back to plain
            assert (await call("measure_text", object_id=gid))["bbox"]
            lob = await call("add_text", text="Old Spice", family="Lobster",
                             size=44, x=600, y=680, anchor="top-center")
            assert lob["object"]["family"] == "Lobster"

            ac = await call("add_shape", kind="rectangle", x=0, y=0,
                            w=100, h=50, fill="#ffffff", opacity=0.85)
            aid = ac["object"]["id"]
            await call("align_object", object_id=aid, edge="center_x")
            aobj = [o for o in (await call("get_state"))["objects"]
                    if o["id"] == aid][0]
            assert abs(aobj["x"] + 50 - 600) < 1, "align canvas center_x failed"
            await call("align_object", object_id=aid, edge="below",
                       target=bid, gap=20)
            aobj = [o for o in (await call("get_state"))["objects"]
                    if o["id"] == aid][0]
            assert aobj["y"] > 120, "align below badge failed"

            d1 = await call("add_shape", kind="ellipse", x=100, y=300, w=40,
                            h=40, fill="#00e5ff")
            d2 = await call("add_shape", kind="ellipse", x=500, y=100, w=40,
                            h=40, fill="#00e5ff")
            d3 = await call("add_shape", kind="ellipse", x=900, y=500, w=40,
                            h=40, fill="#00e5ff")
            dids = [d1["object"]["id"], d2["object"]["id"], d3["object"]["id"]]
            await call("distribute", ids=dids, axis="horizontal",
                       mode="equal_centers")
            ds = sorted(o["x"] for o in (await call("get_state"))["objects"]
                        if o["id"] in dids)
            assert abs((ds[1] - ds[0]) - (ds[2] - ds[1])) < 1, "distribute failed"
            await call("align_group", ids=dids, edge="top")
            ys = [o["y"] for o in (await call("get_state"))["objects"]
                  if o["id"] in dids]
            assert all(abs(y - ys[0]) < 1 for y in ys), "align_group failed"

            gsh = await call("add_shape", kind="rounded_rectangle", x=40,
                             y=60, w=260, h=90, corner_radius=16,
                             fill_gradient={"kind": "linear", "from": "#ffd54f",
                                            "to": "#e53935", "angle": 90})
            assert gsh["object"]["fill_gradient"]["angle"] == 90
            upd = await call("update_shape", object_id=gsh["object"]["id"],
                             fill_gradient={"kind": "radial", "from": "#ffffff",
                                            "to": "#000000"})
            assert upd["object"]["fill_gradient"]["kind"] == "radial"
            rem = await call("update_shape", object_id=gsh["object"]["id"],
                             fill_gradient={})
            assert not rem["object"].get("fill_gradient"), "gradient not removed"
            print(f"G: runs/autofit({gfit['object']['size']}px)/Lobster/"
                  f"align/distribute/gradient OK")

            out = await call("render")
            assert out["image_base64"], "no base64 in render()"
            shutil.copy(out["image_path"], BASE / "final_test_output.png")
            print(f"render OK -> {BASE/'final_test_output.png'}")

            # ---- H: byte-payload tools (hosted-deployment API) in normal mode
            import base64 as b64
            raw = Path(photo).read_bytes()
            h1 = await call("load_image_data",
                            image_base64="data:image/png;base64,"
                                         + b64.b64encode(raw).decode(),
                            filename="upload.png")
            assert h1["state"]["width"] == 1200, "wrong width from bytes"
            assert h1["state"]["objects"] == [], "scene not cleared"
            await call("add_text", text="BYTES-OK", family="Inter", size=40,
                       x=30, y=30)
            assert len((await call("get_state"))["objects"]) == 1
            res = await s.call_tool("load_image_data",
                                    {"image_base64": "!!!garbage!!!"})
            assert res.is_error, "garbage base64 accepted"
            print("H: load_image_data/add_image_data OK")

            print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

