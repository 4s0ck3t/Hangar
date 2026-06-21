# CLAUDE.md — Hangar

Context for future AI sessions. **README.md is the canonical user-facing doc**; this file
is the engineering/architecture companion. Keep both updated when behavior changes.

## What Hangar is
A **local-first desktop asset manager for 3D work** (models, textures, HDRIs, materials) with
a dark dev-tool UI and a Blender bridge. Nothing is uploaded — it only reads and indexes files
on disk. Files are never moved or copied.

Stack: **Python + Flask** backend, **SQLite** index, **vanilla-JS** frontend (no build step),
**pywebview** for the native window, and an installable **Blender add-on**.

## Architecture
| File | Role |
|------|------|
| `desktop.py` | pywebview native-window launcher (standalone app). Sets `HANGAR_DESKTOP=1`, runs Flask in a daemon thread, exposes `Api.pick_folder` to JS via `window.pywebview.api`. |
| `app.py` | Flask server: REST API, background scan thread (`SCAN` dict + `SCAN_LOCK`), OS integration (reveal in file manager), native folder picker (tkinter fallback for browser mode), `.blend` render endpoint. Host `127.0.0.1`, port `HANGAR_PORT` (default 7575). |
| `scanner.py` | `os.walk` folder walking + extension→kind classification. `count_files` (fast denominator pre-pass) then `scan_library` (stat + upsert). Mesh vertex/face stats computed lazily on first asset open via `compute_stats` (optional `trimesh`). |
| `thumbs.py` | Thumbnail chain + caching (`~/.hangar/thumbs/<sha1>.jpg`). `.blend` embedded-preview extraction (`extract_blend_thumbnail`, pure-Python TEST-block parse), Blender discovery (`find_blender`), on-demand headless Workbench render (`_BLEND_RENDER_SCRIPT`). |
| `store.py` | SQLite index: libraries, assets, tags, collections, categories, settings. Schema + queries. Keyword-based auto-categorization engine. Data dir `~/.hangar` (override `HANGAR_HOME`). |
| `static/` | UI — `index.html`, `style.css`, `app.js`. No build step, no framework. |
| `blender_addon/hangar_bridge.py` | Installable Blender add-on. N-panel + `bpy.app.timers` queue watcher tailing `~/.hangar/blender_queue.jsonl`. No ports/network. |

## Data flow
- **Index:** add folder → `store.add_library` → background `_run_scan` (count → walk → `upsert_asset`) → status-bar polls `/api/scan/status`.
- **Thumbnails:** `/api/thumb/<id>` → `thumbs.get_or_make` → cached JPEG or 404 (UI draws a format badge — grid never shows broken images).
- **Send to Blender:** `/api/assets/<id>/send-blender` appends a JSONL line to the queue file; the add-on tails it and imports into the open scene.
- **.blend preview:** embedded thumbnail is read instantly on scan; if absent, drawer "Render preview" → `/api/assets/<id>/render-blend` → headless Blender Workbench render.

## Auto-categorization (keyword rule engine)
Connecter-style rule-based classification — deterministic, no ML.
- Each row in `categories` has a `keywords` column (comma-separated). `_matchers(conn)`
  builds & caches `{category_id: (name, set(keywords))}` from the DB; the cache is
  invalidated (`_invalidate_matchers`) on any category create/edit/remove so user
  categories participate exactly like the 12 seeded defaults (`DEFAULT_CATEGORIES`).
- `_match_category_ids(path, matchers)` tokenizes the lower-cased path on non-alnum
  and matches each keyword as a whole token with singular/plural tolerance (so a
  `vehicles/` folder hits the `vehicle` keyword and `car_sedan` hits `car`).
- `_auto_categorize(conn, asset_id, path)` runs inside `upsert_asset` for **every**
  asset kind (not just models) — adds memberships only, never removes.
- `auto_categorize_all()` re-applies rules across the whole index (back-fill after
  adding/editing keywords); returns `{links_added, assets_matched}`. Idempotent —
  `INSERT OR IGNORE` + `rowcount` so a second run adds 0.
- **Migration:** `init_db` `ALTER TABLE categories ADD COLUMN keywords` for pre-keyword
  DBs, then back-fills seeded keywords only where blank (never clobbers user edits).
- Endpoints: `POST /api/categories` (accepts `keywords`), `POST /api/categories/auto`,
  `POST /api/categories/<id>/keywords`. UI: ⚡ button (`#autoClassifyBtn`) + per-row ✎
  (`.cat-kw`) keyword editor in the sidebar; `list_categories` returns `keywords`.

## Conventions (keep these)
- **Local-first:** never upload, move, or copy user files. Everything Hangar writes lives in `~/.hangar`.
- **No build step:** frontend is plain HTML/CSS/JS served from `static/`. Don't introduce a bundler.
- **Graceful fallbacks:** thumbnail generation must never throw to the user — every failure returns None and the UI shows a badge.
- **Optional deps:** `trimesh`, `imageio`, `numpy` are OPTIONAL. Core (Flask, Pillow, pywebview) must work without them; guard optional imports in try/except and degrade.
- **PyInstaller-aware:** static files resolve via `sys._MEIPASS` when frozen (see `app.py` BASE_DIR).

## Run
```bash
pip install -r requirements.txt
python desktop.py     # native window
python app.py         # browser at http://127.0.0.1:7575
```
Env: `HANGAR_HOME` (data dir), `HANGAR_PORT` (port), `HANGAR_BLENDER` (blender executable),
`HANGAR_DESKTOP` (set by desktop.py).

## Build (Windows)
```bash
pyinstaller --noconsole --name Hangar --add-data "static;static" desktop.py
```
`;` is the Windows add-data separator (`:` on macOS/Linux). Build per-platform on the target OS.
Target needs the Edge WebView2 runtime (default on Win10/11).

## TODO (from handoff — confirm with owner before any large refactor)
1. **Verify on Windows:** standalone window (`desktop.py`), native folder picker (pywebview dialog + tkinter fallback), background scan + bottom status-bar progress, thumbnail sizing.
2. **Test on-demand `.blend` render** (drawer → "Render preview"): the `_BLEND_RENDER_SCRIPT` auto-creates/frames a camera + headless Workbench render — UNTESTED. Fix camera framing / engine selection / headless issues.
3. **Build `Hangar.exe`** with PyInstaller; confirm `static/` bundling + WebView2 runtime.
4. **Stretch (ask first):** "Render all previews" batch job (reuse scan + status-bar pattern); Three.js viewer in detail drawer for GLB/OBJ/STL/PLY; zstd `.blend` embedded-preview support (`zstandard`); auto-import of Higgsfield GLB exports; Unreal import bridge mirroring the Blender queue watcher.

## Known doc/code drift to reconcile
- README lists Materials as `.sbsar .mat .mtl`, but `scanner.MATERIAL_EXTS` is only `.sbsar .mat` (no `.mtl`). README models list shows `.usd .usdz`; code also indexes `.usda .usdc`. Align README and `scanner.py` either way.

## Status
Backend + web UI working, tested in a sandbox. NOT yet verified on real Windows hardware;
Blender-render path unrun (no Blender in sandbox).
