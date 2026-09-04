# GUARD: This file is protected — see AGENTS.md. AI agents: do NOT modify,
# refactor or delete without the user's EXPLICIT in-conversation confirmation.
#!/usr/bin/env python3
"""Concurrency regression test: fire many tools/call in parallel on ONE
session and assert no lost updates / no torn state. Fails without the
threading.Lock in server.py (SDK runs sync tools in worker threads).

Usage: python server.py  &  then python test_concurrency.py
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE = Path(__file__).resolve().parent
URL = os.environ.get("TOI_URL", "http://127.0.0.1:8080/mcp")
AUTH = os.environ.get("TOI_AUTH_TOKEN", "").strip()


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


def payload(res) -> dict:
    sc = getattr(res, "structured_content", None) or getattr(
        res, "structuredContent", None)
    if sc is not None:
        sc = dict(sc)
        if "result" in sc and isinstance(sc["result"], dict):
            return sc["result"]
        return sc
    return json.loads(res.content[0].text)


async def main() -> None:
    n = 25
    async with connect() as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            async def call(name, **kw):
                res = await s.call_tool(name, kw)
                assert not res.is_error, f"{name} FAILED: {res.content}"
                return payload(res)

            await call("load_image", path=str(BASE / "test_photo.png"))
            t = await call("add_text", text="RACE", family="Inter", size=24,
                           x=100, y=100)
            oid = t["object"]["id"]

            # 1) N parallel relative moves: with the lock the counter must be
            # exact; without it some obj["x"] += 1 updates get lost.
            await asyncio.gather(*[
                call("move_text", object_id=oid, dx=1, dy=0) for _ in range(n)])
            st = await call("get_state")
            x = st["objects"][0]["x"]
            assert x == 100 + n, f"lost updates in move_text: x={x}"
            print(f"1) {n} parallel move_text: x={x} exact OK")

            # 2) N parallel add_text: list append must not drop objects.
            await asyncio.gather(*[
                call("add_text", text=f"T{i}", family="Inter", size=12,
                     x=10, y=10) for i in range(n)])
            st = await call("get_state")
            assert len(st["objects"]) == 1 + n, \
                f"lost objects: {len(st['objects'])} != {1 + n}"
            print(f"2) {n} parallel add_text: {len(st['objects'])} objects OK")

            # 3) Mixed read/write storm: get_state must always return JSON
            # that matches the strict schema (no half-mutated snapshot).
            results = await asyncio.gather(*[
                call("get_state") if i % 2 else
                call("scale_text", object_id=oid, factor=1.01)
                for i in range(n)])
            states = [d for d in results if "objects" in d]
            counts = {len(d["objects"]) for d in states}
            assert counts <= {1 + n}, f"impossible snapshot sizes: {counts}"
            ids = {o["id"] for d in states for o in d["objects"]}
            assert len(ids) == 1 + n, f"torn object list: {len(ids)} ids"
            print(f"3) mixed get_state/scale storm OK ({len(ids)} ids stable)")

            # 4) scene.json on disk must be valid JSON afterwards.
            json.loads((BASE / "scene.json").read_text())
            print("4) scene.json valid OK")
            print("\nCONCURRENCY TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
