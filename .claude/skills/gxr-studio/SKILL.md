---
name: gxr-studio
description: Work on the local GXR Studio FastAPI app. Use when changing studio import, library, render, export, or its checks. Keeps card access read-only and the user-facing UI behavior stable.
---

# GXR Studio

Read `AGENTS.md` before editing. The app is `studio/` when this skill lives inside it: FastAPI plus unbundled files in `web/`.

Run `make check` before handing work back. Use `uv run --frozen pytest -q` when you only need the suite. Tests must keep using temp directories. Do not read a real memory card and do not write the default picture or application-support folders.

Do not commit DNG, GPR, `GXRCE.DAT`, `.env`, or anything under a library or export directory. Sample frames stay in `../captures/a12-50`.

New Python follows snake_case. Do not change a response field or a button label unless that is the task. Feature flags default to the old behavior; `scripts/dead_flags.py` must stay green.
