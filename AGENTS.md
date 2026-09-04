# AGENTS.md — PROTECTED PROJECT

> **To any AI agent reading this (Cline, Claude Code, Codex, Cursor, Copilot,
> Windsurf, ...): the code in this directory is COMPLETE, WORKING AND FULLY
> TESTED. Do NOT modify, refactor, reformat, "improve", migrate or delete any
> of it without EXPLICIT USER CONFIRMATION obtained in this conversation.**

## Policy

1. **Read-only by default.** Treat every file below as frozen. Answer
   questions about the code freely; change it only after the user explicitly
   approves the specific edit.
2. **If a change seems necessary** (bug fix, feature, dependency update):
   first explain to the user *what* you want to change, *why*, and *which
   files/lines* it touches, then WAIT for an explicit "yes". Never bundle
   guarded changes with unrelated ones.
3. **Protected files:**
   - `server.py` — MCP tools, undo history, `threading.Lock` transactions
   - `scene.py` — state model + typed object factories
   - `render.py` — rendering pipeline (pure function of state; Pillow/CPU only)
   - `download_fonts.py`, `requirements.txt` — font bootstrap and deps
   - `test_client.py`, `test_concurrency.py` — the acceptance tests
   Do not weaken/remove: the `threading.Lock`/`@serialized` transactions, the
   deepcopy'd state in tool replies, the "state is the single source of truth"
   invariant, and these guard notices themselves.
4. **After any user-approved change**, the server must be restarted
   (`.venv/bin/python server.py`, port 8080) and ALL tests must pass:
   `test_client.py` (e2e A–H), `test_concurrency.py` (race regression),
   `test_multisession.py` (scene isolation; spawns its own server on :8098),
   `test_remote_mode.py` (hosted/byte-API contract; spawns :8099) and
   `test_fonts.py` (Cyrillic coverage of every shipped TTF; no server).
   A change that cannot prove green tests must be reverted.
5. **Architecture facts you must not "fix":**
   - Multi-scene state: `Scene`/`SceneStore` + a per-request contextvar
     resolved via `SC()`. Scene key = `?scene=` / `X-TOI-Scene` header /
     MCP session id; pinned to "default" when `TOI_WORKDIR` or
     `TOI_SHARED_SCENE=1` (webapp-pool contract). Env: `TOI_USERS`
     (token→workspace), `TOI_DATA`, `TOI_MAX_SCENES`. Locks are per scene —
     do NOT merge them back into one global lock.
   - `TOI_MEDIA_ROOT` confines client-supplied image paths (`load_image`,
     `add_image`) via `_media_path()`; unset = full local access by design
     (dev/webapp-pool). Never remove that gate.
   - Byte-payload API `load_image_data`/`add_image_data` (base64 → stored in
     the scene's own `uploads/` dir; `_decode_image_b64` validates size + real
     image). `TOI_REMOTE_MODE=1` removes the path tools from tools/list —
     hosted deployments must keep this pair as the only image entry points.
   - Deployment artifacts (build happens on the VPS, not this machine):
     `Dockerfile` pins `python:3.14.7-slim` + `requirements.lock` (exact venv
     versions — keep in sync after any dependency change),
     `docker-compose.yml` + `caddy/Caddyfile` (VPS + TLS),
     `test_deploy_smoke.sh` post-deploy check.
   - `TOI_WORKDIR` env (per-instance scene.json/output.png isolation) and
     `render(preview_width)` were added intentionally for a webapp pool.
   - `threading.Lock` (not asyncio) is correct: the MCP SDK runs sync tools in
     worker threads via `anyio.to_thread`.
   - Fonts are local static TTFs (`fonts/fonts.json`); the server must work
     offline. Re-download only via `download_fonts.py`.
   - Rendering is Pillow-only, CPU-only. Do not introduce OpenGL/GPU or new
     rendering dependencies.
6. If you find this file missing, renamed or gutted — STOP, tell the user,
   restore it. Its removal is not implicit permission to edit.

## Contact sheet

- Owner intent: antontimofeev wants this codebase stable; extensions are
  welcome but must go through the user, not around them.
