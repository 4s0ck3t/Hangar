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
| `desktop.py` | Desktop launcher. Sets `HANGAR_DESKTOP=1`, runs Flask in a daemon thread, then tries window strategies in order: **(1) pywebview native window** (real movable/resizable window; on Windows via WebView2/pythonnet) → **(2) chrome-less Edge/Chrome `--app` window** (`_find_chromium` + `subprocess`, dedicated `--user-data-dir`) → **(3) default browser**. Each falls through on failure so the app always opens. Frozen builds set `PYTHONNET_PYDLL` to the bundled python DLL before any `clr` import (`_hint_pythonnet_pydll`) — a common fix for the .NET loader failing when frozen (`Failed to resolve Python.Runtime.Loader.Initialize`). `desktop.py --selftest` runs the pywebview→WinForms→clr import chain and exits 0/1; the release workflow runs it on the real Windows runner so the build log shows whether the frozen webview actually loads. Folder picking: native dialog via `Api.pick_folder` in webview mode, else the server-side Tk picker (`/api/pick-folder`). |
| `app.py` | Flask server: REST API, background scan thread (`SCAN` dict + `SCAN_LOCK`), OS integration (reveal in file manager), native folder picker (tkinter fallback for browser mode), `.blend` render endpoint. Host `127.0.0.1`, port `HANGAR_PORT` (default 7575). |
| `scanner.py` | `os.walk` folder walking + extension→kind classification. `count_files` (fast denominator pre-pass) then `scan_library` (stat + upsert). Mesh vertex/face stats computed lazily on first asset open via `compute_stats` (optional `trimesh`). |
| `thumbs.py` | Thumbnail chain + caching (`~/.hangar/thumbs/<sha1>.jpg`). `.blend` embedded-preview extraction (`extract_blend_thumbnail`, pure-Python TEST-block parse), Blender discovery (`find_blender`), on-demand headless Workbench render (`_BLEND_RENDER_SCRIPT`). |
| `store.py` | SQLite index: libraries, assets, tags, collections, categories, settings. Schema + queries. Keyword-based auto-categorization engine. Data dir `~/.hangar` (override `HANGAR_HOME`). |
| `static/` | UI — `index.html`, `style.css`, `app.js`. No build step, no framework. |
| `blender_addon/hangar_bridge.py` | Installable Blender add-on. N-panel + `bpy.app.timers` queue watcher tailing `~/.hangar/blender_queue.jsonl`. No ports/network. |

## Data flow
- **Index:** add folder → `store.add_library` → background `_run_scan` (count → walk → `upsert_asset`) → status-bar polls `/api/scan/status`.
- **Thumbnails:** `/api/thumb/<id>` → `thumbs.get_or_make` → cached JPEG or 404 (UI draws a format badge — grid never shows broken images). A tile's right-click menu (`showCategoryMenu`) carries **Reveal in file manager** (`/api/assets/<id>/reveal`) and **Delete preview** (`POST /api/assets/<id>/preview/clear` → `thumbs.delete_cached_thumb` then re-bake via `get_or_make`) — the latter drops a stale/blank cached tile and re-reads the source (for a `.blend`, its embedded preview), then repaints the live tile (`clearAssetPreview`, cache-busted via `thumbBust`).
- **Pre-baking (warm pass):** after every scan, and once at startup, `app._run_warm` (daemon thread, `WARM`/`WARM_LOCK`, generation-guarded) walks `store.iter_thumb_targets()` and calls `get_or_make` for any asset missing a cached thumb — so the first browse reads JPEGs off disk instead of rendering on scroll (the Connecter approach). Cheap previews (images/HDRIs, embedded `.blend`/glTF thumbs) warm first; models with no embedded preview fall back to a throttled Blender render (`render_model_preview`, 0.15s gap, one at a time) so the slow path never swarms. Progress is folded into `/api/scan/status` as `warm:{done,total,rendered,pct,running}`; the status bar shows a "Generating previews — N/M" phase after indexing and repaints the grid as tiles bake.
- **Send to Blender:** `/api/assets/<id>/send-blender` appends a JSONL line to the queue file; the add-on tails it and imports into the open scene.
- **.blend preview:** embedded thumbnail is read instantly on scan; if absent, drawer "Render preview" → `/api/assets/<id>/render-blend` → headless Blender Workbench render. On failure the endpoint returns the real reason (`thumbs.LAST_RENDER_ERROR`, full output in `~/.hangar/last_render.log`); if Blender isn't found the drawer offers **Set Blender path…** → `POST /api/settings/blender` (persisted in `settings`, re-checked via `reset_blender_cache`).

## Texture sets (Poly Haven–style collapsing)
A material ships as many maps sharing a base name (`wood_diffuse`, `wood_normal`, `wood_rough`…). `scanner.texture_set_info` strips role + resolution tokens (`MAP_ROLES`, `_RES_TOKEN`) to recover the shared base and keys it to the folder → `set_key`, plus `map_role` and `map_order` (diffuse=0 sorts first). Stored on `assets`. `query_assets(group="set")` uses window functions (`ROW_NUMBER`/`COUNT OVER PARTITION BY set_key`) to return one representative tile per set with `set_count`; the frontend always passes `group=set` (non-texture kinds have a unique `set_key`, so they pass through as 1). The drawer lists all maps via `/api/assets/<id>/set` (`store.set_members`); clicking a map swaps the preview.

## In-app updater & diagnostics
- **Updater:** `GET /api/update/check` reads the latest GitHub release (`GITHUB_REPO`),
  compares tag vs `__version__` (`_version_tuple`, numeric per-component). The frontend
  shows an "⬆ Update to vX" pill (boot-time `checkForUpdate`) → a modal with release notes.
  `POST /api/update/download` runs a background thread (`UPDATE`/`UPDATE_LOCK`, progress via
  `GET /api/update/status`) that downloads the zip to ~/Downloads and **extracts it to a
  sibling `Hangar-<version>` folder** — never overwriting the running install, so a failed
  update can't brick it. `POST /api/update/launch` starts the new exe (which binds a free
  port, so it coexists with the old one). All network is stdlib `urllib`, degrades silently.
- **Diagnostics:** `GET /api/diagnostics` returns version/platform/frozen/pythonnet/blender
  /hdri-backends + `desktop.log` & `last_render.log`. The status-bar **Logs** button opens a
  copy-all modal so users can export the launcher log (incl. why the native window fell back).

## Auto-categorization (keyword rule engine)
Connecter-style rule-based classification — deterministic, no ML. Categories carry a
`kind` scope (`model`/`hdri`/`texture`/`material`, or `""` = shared): a scoped rule only
matches assets of that kind, so HDRI categories (Outdoor, Skies, Indoor, Studio,
Sunrise/Sunset, Night, Urban — modelled on Poly Haven) never collect models and vice
versa. The sidebar renders categories grouped under per-kind subheadings and, when a kind
filter is active, shows only that kind's categories plus the shared group.
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

## Build (CI: Windows + Linux)
`.github/workflows/release-windows.yml` and `release-linux.yml` build on every `v*` tag
(onedir) and attach `Hangar-windows.zip` / `Hangar-linux.tar.gz` to the GitHub release.
By hand:
```bash
pyinstaller --noconsole --name Hangar --add-data "static;static" desktop.py   # Windows
pyinstaller            --name Hangar --add-data "static:static" desktop.py     # Linux
```
`;` is the Windows add-data separator (`:` on macOS/Linux). Build per-platform (no cross-build).
Windows native window needs the Edge WebView2 runtime (default on Win10/11). The Windows build
bundles the ~2 MB WebView2 Evergreen bootstrapper (`--add-data MicrosoftEdgeWebview2Setup.exe;.`,
fetched in CI from `go.microsoft.com/fwlink/p/?LinkId=2124703`); `desktop._ensure_webview2()`
checks the EdgeUpdate registry key and runs it `/silent /install` (per-user) when missing.
Otherwise (and on Linux) desktop.py falls back to the Edge/Chrome `--app` window (with
`--no-sandbox` when running as root), then the browser. No small bundled webview runtime exists
for Linux — a true native window there needs system WebKitGTK + PyGObject.

**Updater needs a PUBLIC repo:** unauthenticated GitHub API + asset downloads 404 on a private
repo, so the update pill never appears. `_platform_asset` selects `.zip` (Windows) / `.tar.gz`
(Linux); the downloader handles both archive types and resolves `Hangar.exe` vs `Hangar`.

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
