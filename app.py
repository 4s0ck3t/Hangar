"""Hangar — local-first 3D asset manager.

Run as a desktop app:   python desktop.py
Run as a local web app: python app.py  (opens in your browser)
"""

import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
import zipfile

from flask import Flask, jsonify, request, send_file, send_from_directory

import store
import scanner
import thumbs

__version__ = "0.14.10"

HOST = "127.0.0.1"
PORT = int(os.environ.get("HANGAR_PORT", "7575"))
BLENDER_QUEUE = store.DATA_DIR / "blender_queue.jsonl"


def _accessible(path):
    """os.path.exists that also copes with Windows paths longer than 260 chars.

    The plain call reports such a path as missing even when the drive is mounted
    (a deeply-nested asset pack easily exceeds MAX_PATH), which made the drawer
    wrongly flag files as inaccessible and skip loading their .blend panel. The
    \\?\ extended-length prefix lifts that limit."""
    if not path:
        return False
    if os.path.exists(path):
        return True
    if os.name == "nt":
        try:
            p = os.path.abspath(path)
            if not p.startswith("\\\\?\\"):
                p = "\\\\?\\" + p
            return os.path.exists(p)
        except OSError:
            pass
    return False

# How many Blender renders to run at once for the Regenerate-previews pass. Each
# render is its own Blender process (CPU/RAM bound), so default to a few, capped,
# and overridable via HANGAR_RENDER_WORKERS for beefier or leaner machines.
RENDER_WORKERS = max(1, min(4, (os.cpu_count() or 2) - 1))
try:
    RENDER_WORKERS = max(1, int(os.environ.get("HANGAR_RENDER_WORKERS", RENDER_WORKERS)))
except ValueError:
    pass

# When frozen by PyInstaller the static files live under sys._MEIPASS.
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
# Ensure the web-app manifest is served with a sensible type (Python's mimetypes
# doesn't know .webmanifest by default), so Edge/Chrome honour it for the icon.
mimetypes.add_type("application/manifest+json", ".webmanifest")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
store.init_db()

# ---- background scan state ------------------------------------------------
SCAN = {"running": False, "scanned": 0, "total": 0,
        "current": "", "library": "", "indexed": 0, "unavailable": [],
        "finished_at": 0}
SCAN_LOCK = threading.Lock()


def _run_scan(libs):
    """libs: list of (path, name). Runs in a daemon thread."""
    with SCAN_LOCK:
        SCAN.update(running=True, scanned=0, total=0, current="",
                    library="", indexed=0, unavailable=[])
    total = 0
    for path, _ in libs:
        try:
            total += scanner.count_files(path)
        except Exception:
            pass
    with SCAN_LOCK:
        SCAN["total"] = total
    counter = {"n": 0}

    def on_file(full):
        counter["n"] += 1
        with SCAN_LOCK:
            SCAN["scanned"] = counter["n"]
            SCAN["current"] = full

    indexed = 0
    unavailable = []
    for path, name in libs:
        with SCAN_LOCK:
            SCAN["library"] = name
        try:
            n = scanner.scan_library(path, on_file=on_file)
            if n is None:
                unavailable.append(name)  # folder unreachable — assets kept, not wiped
            else:
                indexed += n
        except Exception as e:
            print(f"[Hangar] scan error in {path}: {e}")
    with SCAN_LOCK:
        SCAN.update(running=False, indexed=indexed, current="",
                    unavailable=unavailable, finished_at=time.time())
    # Pre-bake thumbnails in the background so the first browse is instant
    # (the Connecter trick — generate once up front, not lazily on scroll).
    _start_warm()
    _start_meta_index()   # index .blend asset metadata for search


def _start_scan(libs):
    with SCAN_LOCK:
        if SCAN["running"]:
            return False
    threading.Thread(target=_run_scan, args=(libs,), daemon=True).start()
    return True


# ---- background thumbnail warming -----------------------------------------
# After a scan, walk every asset and generate its thumbnail ahead of time so
# the grid reads cached JPEGs off disk instead of rendering during a scroll.
# Cheap previews (images, embedded .blend/glTF thumbnails) run first; the slow
# Blender renders for formats with no embedded preview run last, one at a time,
# yielding between each so they never starve the UI or swarm like a passive
# scroll-render would.
WARM = {"running": False, "done": 0, "total": 0, "rendered": 0, "failed": 0,
        "current": "", "blender": False, "last_error": "", "by_ext": {},
        "finished_at": 0}
WARM_LOCK = threading.Lock()
WARM_GEN = 0  # bumped on each new scan so an in-flight warm pass bows out


def _run_warm(generation):
    try:
        all_targets = store.iter_thumb_targets()
    except Exception as e:
        print(f"[Hangar] warm: could not list assets: {e}")
        with WARM_LOCK:
            WARM.update(running=False, finished_at=time.time())
        return
    targets = []
    for asset in all_targets:
        if generation != WARM_GEN:
            return
        try:
            if not thumbs.has_cached_thumb(asset):
                targets.append(asset)
        except Exception:
            targets.append(asset)
    blender_ok = thumbs.blender_available()
    with WARM_LOCK:
        WARM.update(running=True, done=0, total=len(targets), rendered=0,
                    failed=0, current="", blender=blender_ok, last_error="",
                    by_ext={}, finished_at=0)
    done = rendered = failed = 0
    last_error = ""
    # Per-extension outcome tally so diagnostics can show exactly what happened
    # to e.g. every .usd: how many ended up with a preview vs none, and how.
    by_ext = {}

    def tally(ext, key):
        s = by_ext.setdefault(ext, {"n": 0, "thumb": 0, "render": 0, "fail": 0})
        s["n"] += 1
        if key:
            s[key] += 1

    for asset in targets:
        if generation != WARM_GEN:        # a newer scan started — let it take over
            break
        done += 1
        ext = asset["ext"]
        try:
            with WARM_LOCK:
                WARM.update(done=done - 1, current=asset["path"],
                            last_error=last_error, by_ext=dict(by_ext))
            if thumbs.has_cached_thumb(asset):
                tally(ext, "thumb")
            elif thumbs.get_or_make(asset) is not None:
                tally(ext, "thumb")
            elif asset["kind"] == "model" and asset["ext"] in thumbs.BLENDER_RENDER_EXTS:
                # No embedded/sibling/trimesh preview (USD, FBX, Alembic…) — fall
                # back to a real Blender render, but only here in the background,
                # throttled, so the slow path never swarms.
                if not blender_ok:
                    failed += 1; tally(ext, "fail")
                    last_error = ("Blender not found — install it or set its path "
                                  "in Hangar to preview USD/FBX/Alembic models.")
                elif thumbs.render_model_preview(asset):
                    rendered += 1; tally(ext, "render")
                else:
                    failed += 1; tally(ext, "fail")
                    last_error = (thumbs.LAST_RENDER_ERROR
                                  or "Render produced no image") + f"  [{asset['path']}]"
                    print(f"[Hangar] warm render failed: {asset['path']} — "
                          f"{thumbs.LAST_RENDER_ERROR}")
                time.sleep(0.15)          # keep the render pass low-priority
            else:
                failed += 1
                tally(ext, "fail")        # no preview path for this kind/ext
        except Exception as e:
            failed += 1; tally(ext, "fail")
            last_error = f"{e}  [{asset.get('path', '?')}]"
            print(f"[Hangar] warm error on {asset.get('path', '?')}: {e}")
        with WARM_LOCK:
            WARM.update(done=done, rendered=rendered, failed=failed,
                        current=asset["path"], last_error=last_error,
                        by_ext=dict(by_ext))
    with WARM_LOCK:
        WARM.update(running=False, current="", last_error=last_error,
                    by_ext=dict(by_ext), finished_at=time.time())


def _start_warm():
    global WARM_GEN
    WARM_GEN += 1
    # Flip running on synchronously so a status poll fired right after a scan
    # finishes can't slip through the gap before the thread sets it.
    with WARM_LOCK:
        WARM["running"] = True
    threading.Thread(target=_run_warm, args=(WARM_GEN,), daemon=True).start()


# ---- pages ----------------------------------------------------------------

@app.get("/")
def index():
    path = os.path.join(app.static_folder, "index.html")
    with open(path, "r", encoding="utf-8") as fh:
        html = fh.read()
    html = html.replace('href="style.css"', f'href="style.css?v={__version__}"')
    html = html.replace('src="app.js"', f'src="app.js?v={__version__}"')
    return html, 200, {"Cache-Control": "no-store"}


# ---- state / dashboard ----------------------------------------------------

@app.get("/api/state")
def state():
    return jsonify({
        "version": __version__,
        "libraries": store.list_libraries(),
        "tags": store.list_tags(),
        "collections": store.list_collections(),
        "categories": store.list_categories(),
        "category_folders": store.category_folder_counts(),
        "counts": store.kind_counts(),
        "blender_queue": str(BLENDER_QUEUE),
        "blender_render": thumbs.blender_available(),
        "blender_render_exts": sorted(thumbs.BLENDER_RENDER_EXTS),
        "hdri_backends": thumbs._hdri_backends(),
        "desktop": bool(os.environ.get("HANGAR_DESKTOP")),
    })


@app.get("/api/diagnostics")
def diagnostics():
    """Everything useful for troubleshooting, surfaced in-app so users can copy
    it without hunting for files. Includes the desktop launcher log (which
    records why the native window fell back to Edge/browser)."""
    info = [
        f"Hangar {__version__}",
        f"platform: {platform.platform()}",
        f"python: {sys.version.split()[0]}  frozen: {bool(getattr(sys, 'frozen', False))}",
        f"desktop mode: {bool(os.environ.get('HANGAR_DESKTOP'))}",
        f"PYTHONNET_PYDLL: {os.environ.get('PYTHONNET_PYDLL', '<unset>')}",
        f"data dir: {store.DATA_DIR}",
        f"blender: {thumbs.find_blender() or '<not found>'}",
        f"hdri backends: {thumbs._hdri_backends()}",
        f"render workers: {RENDER_WORKERS}",
        f"render timeout: {thumbs.RENDER_TIMEOUT}s",
        f"gpu: {', '.join(thumbs.system_gpus()) or '<unknown>'}",
        f"last render gpu: {thumbs.LAST_RENDER_GPU or '<no render yet this session>'}",
    ]
    with WARM_LOCK:
        w = dict(WARM)
    info.append(
        f"preview warm: {'running' if w['running'] else 'idle'} "
        f"{w['done']}/{w['total']} baked, {w['rendered']} rendered, "
        f"{w['failed']} failed (blender={w['blender']})")
    try:
        with store.connect() as conn:
            rows = conn.execute(
                "SELECT id, path, ext, kind, mtime FROM assets "
                "WHERE missing=0 AND kind='hdri'").fetchall()
        hdri_counts = {}
        for r in rows:
            a = dict(r)
            s = hdri_counts.setdefault(a["ext"], {"n": 0, "cached": 0})
            s["n"] += 1
            if thumbs.has_cached_thumb(a):
                s["cached"] += 1
        for ext in sorted(hdri_counts):
            s = hdri_counts[ext]
            info.append(f"  hdri cache {ext}: {s['cached']}/{s['n']} cached")
    except Exception as e:
        info.append(f"hdri cache diagnostics failed: {e!r}")
    # Per-extension outcome — shows e.g. whether .usd got previews or not.
    for ext in sorted(w.get("by_ext", {})):
        s = w["by_ext"][ext]
        info.append(
            f"  {ext}: {s['n']} files — {s['thumb']} preview, "
            f"{s['render']} rendered, {s['fail']} none")
    if w["last_error"]:
        info.append(f"last preview error: {w['last_error']}")
    try:
        import webview  # noqa: F401
        info.append(f"pywebview: {getattr(webview, '__version__', '?')}")
    except Exception as e:
        info.append(f"pywebview import FAILED: {e!r}")
    logs = {}
    for label, path in (("desktop.log", store.DATA_DIR / "desktop.log"),
                        ("last_render.log", store.DATA_DIR / "last_render.log")):
        try:
            logs[label] = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            logs[label] = "(none)"
    return jsonify({"info": "\n".join(info), "logs": logs})


@app.get("/api/scan/status")
def scan_status():
    with SCAN_LOCK:
        data = dict(SCAN)
    data["pct"] = round(100 * data["scanned"] / data["total"], 1) if data["total"] else 0
    with WARM_LOCK:
        warm = dict(WARM)
    warm["pct"] = round(100 * warm["done"] / warm["total"], 1) if warm["total"] else 0
    data["warm"] = warm
    return jsonify(data)


# ---- native folder picker -------------------------------------------------

@app.post("/api/pick-folder")
def pick_folder():
    """Open a native OS folder chooser (browser-mode fallback).

    The desktop app runs in an Edge/Chrome --app window (no JS bridge), so this
    Tk-based server-side picker handles folder selection there and in a plain
    browser. The frontend falls back to a path prompt if Tk is unavailable.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Choose an asset folder")
        root.destroy()
        return jsonify({"path": path or None, "cancelled": not path})
    except Exception as e:
        return jsonify({"path": None, "error": str(e),
                        "unsupported": True}), 200


# ---- libraries ------------------------------------------------------------

@app.post("/api/libraries")
def add_library():
    data = request.get_json(force=True)
    raw = (data.get("path") or "").strip()
    if not raw:
        return jsonify({"error": "Choose a folder to add."}), 400
    path = Path(raw).expanduser()
    if not path.exists() or not path.is_dir():
        return jsonify({"error": f"That folder doesn't exist: {raw}"}), 400
    lib = store.add_library(str(path), data.get("name"))
    _start_scan([(lib["path"], lib["name"])])
    return jsonify({"library": lib, "scanning": True})


@app.delete("/api/libraries/<int:library_id>")
def remove_library(library_id):
    store.remove_library(library_id)
    return jsonify({"ok": True})


@app.post("/api/scan")
def rescan():
    libs = [(l["path"], l["name"]) for l in store.list_libraries()]
    if not libs:
        return jsonify({"scanning": False, "error": "No folders to scan."})
    started = _start_scan(libs)
    return jsonify({"scanning": started})


# ---- assets ---------------------------------------------------------------

@app.get("/api/assets")
def list_assets():
    q = request.args
    assets, total = store.query_assets(
        search=q.get("search", "").strip(),
        kind=q.get("kind", "").strip(),
        ext=q.get("ext", "").strip(),
        tag=q.get("tag", "").strip(),
        collection=q.get("collection", "").strip(),
        category=q.get("category", "").strip(),
        folder=q.get("folder", "").strip(),
        favorite=q.get("favorite") == "1",
        sort=q.get("sort", "name"),
        limit=int(q.get("limit", 300)),
        offset=int(q.get("offset", 0)),
        group=q.get("group", "").strip(),
        set_key=q.get("set_key", "").strip(),
        with_categories=q.get("with_categories") == "1",
        subtype=q.get("subtype", "").strip(),
        resolution=q.get("resolution", "").strip(),
        missing=q.get("missing") == "1",
        duplicates=q.get("duplicates") == "1",
    )
    return jsonify({"assets": assets, "total": total})


@app.delete("/api/assets/missing")
def purge_missing():
    """Remove all missing (file-not-found) assets from the index permanently."""
    n = store.delete_missing()
    return jsonify({"ok": True, "deleted": n})


@app.get("/api/facets")
def facets():
    """Subtype + resolution facets available for the faceted filter strip,
    optionally scoped to a kind (?kind=texture)."""
    return jsonify(store.facet_counts(request.args.get("kind", "").strip()))


@app.get("/api/assets/<int:asset_id>/set")
def asset_set(asset_id):
    """All texture maps belonging to the same material set as this asset."""
    return jsonify({"members": store.set_members(asset_id)})


@app.get("/api/assets/<int:asset_id>")
def asset_detail(asset_id):
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["kind"] == "model" and not asset["stats_done"]:
        v, f = scanner.compute_stats(asset)
        store.save_stats(asset_id, v, f)
        asset["vertices"], asset["faces"], asset["stats_done"] = v, f, 1
    # Whether the file is reachable right now, so the drawer can flag it.
    asset["exists"] = _accessible(asset["path"])
    # Whether a thumbnail is already cached, so the drawer can show it instantly
    # instead of re-running the 3D viewer on every open.
    asset["has_thumb"] = thumbs.has_cached_thumb(asset)
    # Count "Mark as Asset" datablocks in .blend files (parsed once, cached).
    if (asset["ext"] == ".blend" and asset["blend_assets"] is None
            and asset["exists"]):
        n = thumbs.count_blend_marked_assets(asset["path"])
        store.save_blend_asset_count(asset_id, n)
        asset["blend_assets"] = n
    return jsonify(asset)


_NO_CACHE = {"Cache-Control": "no-store"}

@app.get("/api/thumb/<int:asset_id>")
def thumb(asset_id):
    asset = store.get_asset(asset_id)
    if not asset:
        return "", 404, _NO_CACHE
    path = thumbs.get_or_make(asset)
    if not path:
        return "", 404, _NO_CACHE
    # The tile URL carries the asset's mtime (?v=) and an explicit rebake bust
    # (?t=), so its content never changes under a given URL — let the browser
    # keep it for a week instead of revalidating every JPEG on each grid load,
    # which is what made a big library feel slow to open.
    return send_file(path, mimetype="image/jpeg", max_age=604800)


@app.post("/api/assets/<int:asset_id>/favorite")
def favorite(asset_id):
    value = bool(request.get_json(force=True).get("value"))
    store.set_favorite(asset_id, value)
    return jsonify({"ok": True, "favorite": value})


@app.post("/api/assets/<int:asset_id>/tags")
def set_tags(asset_id):
    tags = request.get_json(force=True).get("tags", [])
    store.set_asset_tags(asset_id, tags)
    return jsonify({"ok": True, "tags": store.get_asset(asset_id)["tags"]})


@app.post("/api/assets/<int:asset_id>/collection")
def collection_membership(asset_id):
    data = request.get_json(force=True)
    store.set_collection_membership(
        data["collection"].strip(), asset_id, add=data.get("add", True)
    )
    return jsonify({"ok": True})


@app.post("/api/assets/<int:asset_id>/category")
def category_membership(asset_id):
    data = request.get_json(force=True)
    store.set_category_membership(
        data["category"].strip(), asset_id, add=data.get("add", True)
    )
    return jsonify({"ok": True, "categories": store.get_asset(asset_id)["categories"]})


# ---- tags & collections ---------------------------------------------------

@app.post("/api/assets/batch/tag")
def batch_tag():
    data = request.get_json(force=True)
    ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
    tag = (data.get("tag") or "").strip()
    if not ids or not tag:
        return jsonify({"error": "ids and tag required"}), 400
    store.batch_add_tag(ids, tag)
    return jsonify({"ok": True, "count": len(ids)})


@app.post("/api/assets/batch/collection")
def batch_collection():
    data = request.get_json(force=True)
    ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
    collection = (data.get("collection") or "").strip()
    if not ids or not collection:
        return jsonify({"error": "ids and collection required"}), 400
    for aid in ids:
        store.set_collection_membership(collection, aid, add=True)
    return jsonify({"ok": True, "count": len(ids)})


@app.post("/api/assets/batch/remove")
def batch_remove():
    data = request.get_json(force=True)
    ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
    if not ids:
        return jsonify({"error": "ids required"}), 400
    for aid in ids:
        store.remove_asset(aid)
    return jsonify({"ok": True, "removed": len(ids)})


@app.post("/api/tags")
def new_tag():
    data = request.get_json(force=True)
    store.create_tag(data["name"], data.get("color", "#8A8F9A"))
    return jsonify({"ok": True, "tags": store.list_tags()})


@app.post("/api/collections")
def new_collection():
    store.create_collection(request.get_json(force=True)["name"])
    return jsonify({"ok": True, "collections": store.list_collections()})


@app.post("/api/categories")
def new_category():
    data = request.get_json(force=True)
    store.create_category(
        data.get("name", ""), data.get("icon", ""),
        data.get("keywords", ""), data.get("kind", ""),
    )
    return jsonify({"ok": True, "categories": store.list_categories()})


@app.post("/api/categories/auto")
def auto_categorize():
    """Re-apply category keyword rules across the whole library (back-fill)."""
    result = store.auto_categorize_all()
    return jsonify({"ok": True, **result, "categories": store.list_categories()})


@app.post("/api/categories/<int:category_id>/keywords")
def update_category_keywords(category_id):
    data = request.get_json(force=True)
    store.update_category(category_id, data.get("keywords", ""))
    return jsonify({"ok": True, "categories": store.list_categories()})


@app.delete("/api/categories/<int:category_id>")
def delete_category(category_id):
    store.remove_category(category_id)
    return jsonify({"ok": True, "categories": store.list_categories()})


@app.post("/api/categories/reorder")
def reorder_categories_route():
    """Persist a drag-reordered sidebar. Body: {"order": [id, id, …]}."""
    data = request.get_json(force=True)
    store.reorder_categories(data.get("order", []))
    return jsonify({"ok": True, "categories": store.list_categories()})


@app.post("/api/categories/<int:category_id>/parent")
def set_category_parent_route(category_id):
    """Nest a category under another (Furniture > Chairs), or clear its nesting.
    Body: {"parent_id": <id> | null}."""
    data = request.get_json(force=True)
    parent_id = data.get("parent_id")
    parent_id = int(parent_id) if parent_id not in (None, "") else None
    ok, error = store.set_category_parent(category_id, parent_id)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "categories": store.list_categories()})


@app.post("/api/assets/batch/category")
def batch_category():
    data = request.get_json(force=True)
    ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
    category = (data.get("category") or "").strip()
    add = data.get("add", True)
    if not ids or not category:
        return jsonify({"error": "ids and category required"}), 400
    for aid in ids:
        store.set_category_membership(category, aid, add=add)
    return jsonify({"ok": True, "count": len(ids)})


# ---- OS integration -------------------------------------------------------

@app.get("/api/assets/<int:asset_id>/file")
def serve_asset_file(asset_id):
    """Stream a GLB/GLTF/FBX file to the in-drawer three.js viewer."""
    asset = store.get_asset(asset_id)
    if not asset:
        return "", 404
    if asset["ext"] not in (".glb", ".gltf", ".fbx"):
        return jsonify({"error": "Only GLB/GLTF/FBX files are viewable"}), 400
    path = asset["path"]
    if not os.path.exists(path):
        return "", 404
    mime = {
        ".glb": "model/gltf-binary",
        ".gltf": "model/gltf+json",
        ".fbx": "application/octet-stream",
    }[asset["ext"]]
    return send_file(path, mimetype=mime)


@app.post("/api/assets/<int:asset_id>/thumb")
def save_thumb(asset_id):
    """Cache a viewer-rendered preview (data URL) as this asset's thumbnail, so
    the grid shows it and re-opening is instant."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    data = request.get_json(force=True) or {}
    img = data.get("image", "")
    if "," in img:  # strip a data:image/...;base64, prefix
        img = img.split(",", 1)[1]
    import base64
    try:
        raw = base64.b64decode(img)
    except Exception:
        return jsonify({"error": "Bad image data."}), 400
    ok = thumbs.save_thumbnail_bytes(asset, raw)
    return jsonify({"ok": bool(ok)})


@app.post("/api/assets/<int:asset_id>/reveal")
def reveal(asset_id):
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    path = asset["path"]
    sysname = platform.system()
    devnull = subprocess.DEVNULL
    try:
        if sysname == "Darwin":
            subprocess.Popen(["open", "-R", path], stdin=devnull, stdout=devnull, stderr=devnull)
        elif sysname == "Windows":
            # NOTE: do NOT pass _no_window() here — its STARTUPINFO carries
            # SW_HIDE, which Explorer honours by opening its own window hidden, so
            # the reveal appears to do nothing. Explorer is a GUI app with no
            # console to suppress, so a plain launch is correct.
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)],
                             stdin=devnull, stdout=devnull, stderr=devnull)
        else:
            import shutil as _shutil
            fm = (["nautilus", "--select", path] if _shutil.which("nautilus") else
                  ["dolphin", "--select", path] if _shutil.which("dolphin") else
                  ["nemo", path] if _shutil.which("nemo") else
                  ["thunar", path] if _shutil.which("thunar") else
                  ["xdg-open", os.path.dirname(path)])
            subprocess.Popen(fm, stdin=devnull, stdout=devnull, stderr=devnull)
    except Exception as e:
        return jsonify({"error": f"Couldn't open the file manager: {e}"}), 500
    return jsonify({"ok": True})


@app.post("/api/assets/<int:asset_id>/preview/clear")
def clear_preview(asset_id):
    """Delete this asset's cached thumbnail and immediately re-bake it from the
    source. For a .blend that means falling back to the preview embedded in the
    file (Blender's own thumbnail) — the fix for a tile that cached blank or
    stale. Returns whether a file was removed and whether a fresh thumb was made."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    removed = thumbs.delete_cached_thumb(asset)
    rebaked = thumbs.get_or_make(asset) is not None
    return jsonify({"ok": True, "removed": bool(removed), "rebaked": bool(rebaked)})


@app.post("/api/assets/<int:asset_id>/rename")
def rename_asset(asset_id):
    """Rename the asset's file on disk (keeping its extension and folder) and
    update the library row. Body: {"name": "<new base name, no extension>"}."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    raw = (request.get_json(silent=True) or {}).get("name", "")
    new_base = str(raw).strip()
    # Strip an extension the user may have typed; we always keep the original one.
    if new_base.lower().endswith(asset["ext"].lower()):
        new_base = new_base[: -len(asset["ext"])]
    new_base = new_base.strip()
    if not new_base:
        return jsonify({"error": "Please enter a name."}), 400
    # No path traversal or directory separators — this renames in place only.
    if any(c in new_base for c in '/\\:*?"<>|') or new_base in (".", ".."):
        return jsonify({"error": "Name contains characters that aren't allowed."}), 400
    old_path = asset["path"]
    if not os.path.exists(old_path):
        return jsonify({"error": "File isn't accessible right now."}), 400
    new_path = os.path.join(os.path.dirname(old_path), new_base + asset["ext"])
    if os.path.normcase(new_path) == os.path.normcase(old_path):
        return jsonify({"ok": True, "name": asset["name"], "path": old_path,
                        "unchanged": True})
    if os.path.exists(new_path):
        return jsonify({"error": "A file with that name already exists here."}), 409
    try:
        os.rename(old_path, new_path)
    except OSError as e:
        return jsonify({"error": f"Couldn't rename the file: {e}"}), 500
    store.rename_asset(asset_id, new_path, new_base)
    return jsonify({"ok": True, "name": new_base, "path": new_path})


@app.post("/api/assets/<int:asset_id>/move-to-folder")
def move_to_folder(asset_id):
    """Physically move the asset's file into an existing folder (keeping its
    name) and update the library row. Body: {"folder": "<destination dir>"}.

    Used by the right-click "Move to category → folder" action, so a file can be
    filed into one of the folders already represented in that category."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    dest_dir = str((request.get_json(silent=True) or {}).get("folder", "")).strip()
    if not dest_dir or not os.path.isdir(dest_dir):
        return jsonify({"error": "That folder isn't accessible."}), 400
    old_path = asset["path"]
    if not os.path.exists(old_path):
        return jsonify({"error": "File isn't accessible right now."}), 400
    new_path = os.path.join(dest_dir, os.path.basename(old_path))
    if os.path.normcase(os.path.normpath(new_path)) == os.path.normcase(os.path.normpath(old_path)):
        return jsonify({"ok": True, "path": old_path, "unchanged": True})
    if os.path.exists(new_path):
        return jsonify({"error": "A file with that name already exists in that folder."}), 409
    try:
        shutil.move(old_path, new_path)          # handles cross-drive moves
    except OSError as e:
        return jsonify({"error": f"Couldn't move the file: {e}"}), 500
    store.rename_asset(asset_id, new_path, asset["name"])
    return jsonify({"ok": True, "path": new_path})


@app.post("/api/assets/<int:asset_id>/open-file")
def open_file(asset_id):
    """Open the asset's file with the OS default application (what double-clicking
    it would do). Backs the clickable path in the detail drawer."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    path = asset["path"]
    if not os.path.exists(path):
        return jsonify({"error": "File isn't accessible right now."}), 400
    sysname = platform.system()
    devnull = subprocess.DEVNULL
    try:
        if sysname == "Windows":
            os.startfile(os.path.normpath(path))     # noqa: only exists on Windows
        elif sysname == "Darwin":
            subprocess.Popen(["open", path], stdin=devnull, stdout=devnull, stderr=devnull)
        else:
            subprocess.Popen(["xdg-open", path], stdin=devnull, stdout=devnull, stderr=devnull)
    except Exception as e:
        return jsonify({"error": f"Couldn't open the file: {e}"}), 500
    return jsonify({"ok": True})


def _blend_info(asset):
    """Merge the pure-Python .blend inspection (marked-asset names + missing
    textures) with the exported preview manifest (which assets have a thumbnail
    cached). Returns the dict the drawer renders, or None if not a .blend."""
    if asset["ext"] != ".blend" or not _accessible(asset["path"]):
        return None
    info = thumbs.inspect_blend(asset["path"]) or {
        "count": 0, "assets": [], "missing_textures": []}
    preview_list = thumbs.blend_asset_previews(asset["path"])
    previews = {p["name"]: p for p in preview_list}
    # The render manifest keys previews on Blender's object name; the DNA parse
    # derives names from the raw datablock. They usually agree, but a normalized
    # (case/punctuation-insensitive) index is a safety net so a rendered preview
    # is never hidden by a trivial name difference.
    def _norm(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())
    previews_norm = {_norm(p["name"]): p for p in preview_list}

    # Marked datablocks that have been extracted to their own .blend file get a
    # green tick in the drawer. We match on base name: "bed_01_01" is "owned" if
    # a "bed_01_01.blend" exists anywhere in the library (excluding self).
    self_name = asset["name"].lower()
    blend_names = store.existing_blend_names()
    matched = set()
    for a in info["assets"]:
        preview = previews.get(a["name"]) or previews_norm.get(_norm(a["name"]), {})
        a["has_thumb"] = preview.get("has_thumb", False)
        # The exact manifest key to request the PNG with (may differ from the
        # DNA-parsed display name when the normalized fallback matched).
        a["preview_name"] = preview.get("name", a["name"])
        a["thumb_mtime"] = preview.get("mtime", 0)
        a["preview_source"] = preview.get(
            "preview_source",
            "No rendered asset preview; showing type badge",
        )
        name_l = a["name"].lower()
        a["has_individual"] = name_l in blend_names and name_l != self_name
        if preview:
            matched.add(_norm(preview.get("name", a["name"])))
    # A successful render must never be hidden by a name-match miss (or by the DNA
    # parse coming back empty): surface any manifest preview the loop above didn't
    # already attach to an asset, so what Blender rendered always appears.
    for p in preview_list:
        if p.get("has_thumb") and _norm(p["name"]) not in matched:
            info["assets"].append({
                "name": p["name"], "kind": p.get("kind", "Object"),
                "has_thumb": True, "preview_name": p["name"],
                "thumb_mtime": p.get("mtime", 0),
                "preview_source": p.get("preview_source", ""),
                "has_individual": p["name"].lower() in blend_names
                                  and p["name"].lower() != self_name,
            })
            matched.add(_norm(p["name"]))
    info["previews_ready"] = any(p.get("has_thumb") for p in preview_list)
    # What the main tile preview for this .blend is currently sourced from
    # (embedded thumbnail vs a Hangar render, and which engine) so the drawer can
    # tell the user instead of leaving them guessing.
    info["preview"] = thumbs.preview_source(asset)
    # Keep the file's searchable metadata blob current whenever we inspect it.
    try:
        store.set_blend_meta(asset["id"], _blend_search_text(info))
    except Exception:
        pass
    return info


def _blend_search_text(info):
    """Flatten a .blend's marked-asset metadata into one searchable string
    (asset names, tags, authors, catalogs, descriptions) for the search index."""
    parts = []
    for a in (info.get("assets") or []):
        for key in ("name", "author", "catalog", "description"):
            v = a.get(key)
            if v:
                parts.append(str(v))
        for t in (a.get("tags") or []):
            parts.append(str(t))
    # De-dup while preserving order, cap length so a huge scene can't bloat the row.
    seen, out = set(), []
    for p in parts:
        k = p.lower()
        if k not in seen:
            seen.add(k); out.append(p)
    return " ".join(out)[:4000]


# ---- background: index .blend metadata for search -------------------------
_META_LOCK = threading.Lock()
_META_GEN = 0


def _run_meta_index(generation):
    """Inspect every indexed .blend (cheap once inspect_blend is cached) and store
    its aggregated metadata so search reaches inside files the user hasn't opened."""
    try:
        targets = store.blend_meta_targets()
    except Exception:
        return
    for t in targets:
        if generation != _META_GEN:
            return                                   # a newer scan superseded us
        try:
            info = thumbs.inspect_blend(t["path"])
            if info is not None:
                store.set_blend_meta(t["id"], _blend_search_text(info))
        except Exception:
            pass
        time.sleep(0.02)                             # stay low-priority


def _start_meta_index():
    global _META_GEN
    with _META_LOCK:
        _META_GEN += 1
        gen = _META_GEN
    threading.Thread(target=_run_meta_index, args=(gen,), daemon=True).start()


@app.get("/api/assets/<int:asset_id>/blend-info")
def blend_info(asset_id):
    """Marked-asset names (+ whether a preview is cached) and missing textures
    for a .blend. Parsed on demand so it's always current."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    info = _blend_info(asset)
    if info is None:
        return jsonify({"error": "Not a reachable .blend file."}), 400
    return jsonify(info), 200, _NO_CACHE


@app.get("/api/assets/<int:asset_id>/blend-asset-thumb")
def blend_asset_thumb(asset_id):
    """Serve one marked datablock's exported preview PNG (by ?name=)."""
    asset = store.get_asset(asset_id)
    if not asset:
        return "", 404, _NO_CACHE
    name = request.args.get("name", "")
    p = thumbs.blend_asset_thumb_path(asset["path"], name) if name else None
    if not p:
        return "", 404, _NO_CACHE
    # URL carries the preview's mtime (?v=), so it's safe to cache for a week and
    # skip the per-image revalidation when reopening a .blend's asset gallery.
    return send_file(str(p), mimetype="image/png", max_age=604800)


@app.post("/api/assets/<int:asset_id>/mark-assets")
def mark_assets(asset_id):
    """Mark this .blend's top-level objects (or collections) as Asset-Browser
    assets, generate per-asset previews, and save the file in place. Modifies
    the source .blend. `target` body field: "objects" (default) | "collections"."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["ext"] != ".blend":
        return jsonify({"error": "Only .blend files can be marked."}), 400
    if not thumbs.blender_available():
        return jsonify({"ok": False, "blender": False,
                        "error": "Blender wasn't found — set its path first."}), 200
    target = (request.get_json(silent=True) or {}).get("target", "objects")
    if target not in ("objects", "collections"):
        target = "objects"
    result = thumbs.mark_blend_assets(asset["path"], target)
    if result.get("ok"):
        # Refresh the cached marked-asset count so the drawer/grid badge updates.
        n = thumbs.count_blend_marked_assets(asset["path"])
        store.save_blend_asset_count(asset_id, n)
        result["blend_assets"] = n
    return jsonify(result), 200


@app.post("/api/assets/<int:asset_id>/unmark-assets")
def unmark_assets(asset_id):
    """Clear Blender Asset-Browser marks from a .blend by datablock type.
    Modifies the source .blend. `target`: objects | collections | materials | all."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["ext"] != ".blend":
        return jsonify({"error": "Only .blend files can be unmarked."}), 400
    if not thumbs.blender_available():
        return jsonify({"ok": False, "blender": False,
                        "error": "Blender wasn't found â€” set its path first."}), 200
    target = (request.get_json(silent=True) or {}).get("target", "collections")
    if target not in ("objects", "collections", "materials", "all"):
        target = "collections"
    result = thumbs.unmark_blend_assets(asset["path"], target)
    if result.get("ok"):
        n = thumbs.count_blend_marked_assets(asset["path"])
        store.save_blend_asset_count(asset_id, n)
        result["blend_assets"] = n
    return jsonify(result), 200


@app.post("/api/assets/<int:asset_id>/asset-meta")
def set_asset_meta(asset_id):
    """Write Asset-Browser metadata (author/description/license/copyright/tags)
    onto one marked datablock in a .blend and save in place. Body: {name, kind,
    author?, description?, license?, copyright?, tags?[]}. Modifies the source."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["ext"] != ".blend":
        return jsonify({"error": "Only .blend assets carry metadata."}), 400
    if not thumbs.blender_available():
        return jsonify({"ok": False, "blender": False,
                        "error": "Blender wasn't found — set its path first."}), 200
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    kind = (body.get("kind") or "Object").strip()
    if not name:
        return jsonify({"error": "Which asset? (missing name)"}), 400
    meta = {k: body.get(k, "") for k in ("author", "description", "license", "copyright")}
    tags = body.get("tags")
    if isinstance(tags, list):
        meta["tags"] = [str(t).strip() for t in tags if str(t).strip()]
    result = thumbs.write_blend_asset_meta(asset["path"], name, kind, meta)
    if result.get("ok"):
        # Saving the .blend bumps its mtime, so inspect_blend re-parses; refresh the
        # search index and hand back the updated per-asset info.
        info = _blend_info(asset)
        if info is not None:
            result["assets"] = info.get("assets", [])
    return jsonify(result), 200


@app.post("/api/assets/<int:asset_id>/extract-asset")
def extract_asset(asset_id):
    """Save one marked datablock out to its own .blend next to the source file,
    then index it so it appears in the library (and lights its green tick).
    Body: {"name": "<datablock name>", "kind": "Object"|"Collection"}."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["ext"] != ".blend":
        return jsonify({"error": "Only .blend files can be extracted from."}), 400
    if not thumbs.blender_available():
        return jsonify({"ok": False, "blender": False,
                        "error": "Blender wasn't found — set its path first."}), 200
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    kind = body.get("kind", "Object")
    if not name:
        return jsonify({"error": "No asset name given."}), 400
    if kind not in ("Object", "Collection"):
        kind = "Object"
    # Sanitise the datablock name into a safe sibling filename.
    safe = "".join("_" if c in '/\\:*?"<>|' else c for c in name).strip()
    if not safe:
        return jsonify({"error": "That asset name can't be used as a filename."}), 400
    out_path = os.path.join(os.path.dirname(asset["path"]), safe + ".blend")
    if os.path.exists(out_path):
        return jsonify({"error": f"“{safe}.blend” already exists here."}), 409
    result = thumbs.extract_blend_asset(asset["path"], name, kind, out_path)
    if not result.get("ok"):
        return jsonify(result), 200
    # Index the new file immediately so the library + green tick update without
    # waiting for a rescan.
    try:
        st = os.stat(out_path)
        store.upsert_asset({
            "path": out_path, "name": safe, "ext": ".blend",
            "kind": scanner.EXT_KIND.get(".blend", "model"),
            "size": st.st_size, "mtime": st.st_mtime,
        })
    except Exception:
        pass
    result["extracted_name"] = safe
    return jsonify(result), 200


@app.post("/api/assets/<int:asset_id>/generate-asset-previews")
def generate_asset_previews(asset_id):
    """Render a 256×256 EEVEE thumbnail for each marked mesh object in a .blend
    and cache them. Non-destructive — never modifies the source file."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["ext"] != ".blend":
        return jsonify({"error": "Only .blend files are supported."}), 400
    if not thumbs.blender_available():
        return jsonify({"ok": False, "blender": False,
                        "error": "Blender wasn't found — set its path first."}), 200
    result = thumbs.generate_blend_asset_previews(asset["path"])
    return jsonify(result), 200


@app.post("/api/open-data-dir")
def open_data_dir():
    """Open Hangar's data folder (~/.hangar: SQLite index, thumbnail cache,
    Blender queue) in the OS file manager so the user can inspect it."""
    folder = str(store.DATA_DIR)
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            subprocess.run(["open", folder], check=False)
        elif sysname == "Windows":
            subprocess.run(["explorer", os.path.normpath(folder)], check=False)
        else:
            subprocess.run(["xdg-open", folder], check=False)
    except Exception as e:
        return jsonify({"error": f"Couldn't open the folder: {e}"}), 500
    return jsonify({"ok": True, "path": folder})


def _queue_blender(entry):
    """Append one instruction to the Blender bridge queue file."""
    entry.setdefault("ts", time.time())
    with open(BLENDER_QUEUE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _import_entry(asset, place_at_cursor=False, mode="append"):
    # "mode" only matters for .blend (Append copies the datablock in; Link keeps
    # a live reference to the source file, like Blender's own Asset Browser
    # drag-drop) — the bridge ignores it for every other format.
    if mode not in ("append", "link"):
        mode = "append"
    return {"action": "import", "path": asset["path"], "ext": asset["ext"],
            "place_at_cursor": bool(place_at_cursor), "mode": mode}


def _material_entry(asset, to_selection=True):
    """Assemble an apply_material queue entry from an asset's texture set."""
    members = store.set_members(asset["id"]) or [asset]
    maps = {}
    for m in members:
        role = (m.get("map_role") or "").strip()
        if role and role != "other" and role not in maps:
            maps[role] = m["path"]
    if not maps:  # lone texture with no recognised role — use it as base colour
        maps["diffuse"] = asset["path"]
    # set_key is "folder|basename"; the basename is the nicer material name.
    name = (asset.get("set_key") or asset["name"]).split("|")[-1]
    return {"action": "apply_material", "name": name, "maps": maps,
            "to_selection": bool(to_selection)}


def _hdri_entry(asset, strength=1.0):
    return {"action": "set_world_hdri", "path": asset["path"],
            "strength": float(strength)}


@app.post("/api/assets/<int:asset_id>/send-blender")
def send_blender(asset_id):
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["kind"] != "model":
        return jsonify({"error": "Only model files can be sent to Blender."}), 400
    data = request.get_json(silent=True) or {}
    _queue_blender(_import_entry(asset, data.get("place_at_cursor", False),
                                 data.get("mode", "append")))
    return jsonify({"ok": True, "queued": asset["name"]})


# Per-extension Blender import operators, newest-API first with an older
# fallback (4.x renamed several to the `wm.*_import` family). A .blend is opened
# directly and never appears here.
_IMPORT_OPS = {
    ".fbx":  ["bpy.ops.import_scene.fbx(filepath=P)"],
    ".obj":  ["bpy.ops.wm.obj_import(filepath=P)", "bpy.ops.import_scene.obj(filepath=P)"],
    ".gltf": ["bpy.ops.import_scene.gltf(filepath=P)"],
    ".glb":  ["bpy.ops.import_scene.gltf(filepath=P)"],
    ".stl":  ["bpy.ops.wm.stl_import(filepath=P)", "bpy.ops.import_mesh.stl(filepath=P)"],
    ".ply":  ["bpy.ops.wm.ply_import(filepath=P)", "bpy.ops.import_mesh.ply(filepath=P)"],
    ".dae":  ["bpy.ops.wm.collada_import(filepath=P)"],
    ".usd":  ["bpy.ops.wm.usd_import(filepath=P)"],
    ".usda": ["bpy.ops.wm.usd_import(filepath=P)"],
    ".usdc": ["bpy.ops.wm.usd_import(filepath=P)"],
    ".usdz": ["bpy.ops.wm.usd_import(filepath=P)"],
}


def _blender_import_expr(ext, path):
    """A --python-expr script that imports `path` into a fresh Blender session,
    trying each operator until one succeeds (Blender-version tolerance). Returns
    None for an extension Blender can't import on its own."""
    ops = _IMPORT_OPS.get(ext)
    if not ops:
        return None
    lines = ["import bpy", f"P = {path!r}", "ok = False"]
    for op in ops:
        lines += ["if not ok:", "    try:", f"        {op}", "        ok = True",
                  "    except Exception as e:", "        print('Hangar import failed:', e)"]
    return "\n".join(lines)


@app.post("/api/assets/<int:asset_id>/open-blender")
def open_blender(asset_id):
    """Launch Blender on this asset: a .blend opens directly; any other model
    opens a fresh Blender and imports it. Unlike send-blender (which needs the
    add-on running in an already-open Blender), this starts Blender itself."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["kind"] != "model":
        return jsonify({"error": "Only model files can be opened in Blender."}), 400
    blender = thumbs.find_blender()
    if not blender:
        return jsonify({"error": "Blender not found — set its path first.",
                        "need_blender": True}), 400
    ext = (asset["ext"] or "").lower()
    devnull = subprocess.DEVNULL
    try:
        if ext == ".blend":
            subprocess.Popen([blender, asset["path"]],
                             stdin=devnull, stdout=devnull, stderr=devnull,
                             **thumbs._no_window())
        else:
            expr = _blender_import_expr(ext, asset["path"])
            if not expr:
                return jsonify({"error": f"Blender can't open {ext} directly."}), 400
            subprocess.Popen([blender, "--python-expr", expr],
                             stdin=devnull, stdout=devnull, stderr=devnull,
                             **thumbs._no_window())
    except Exception as e:
        return jsonify({"error": f"Couldn't launch Blender: {e}"}), 500
    return jsonify({"ok": True, "opened": asset["name"]})


@app.post("/api/assets/<int:asset_id>/send-material")
def send_material(asset_id):
    """Send a texture set to Blender as a ready-built Principled-BSDF material.

    Gathers every map in the asset's texture set (diffuse/roughness/normal/…)
    and hands the role→path mapping to the bridge addon, which wires the node
    graph and (by default) applies it to the current selection.
    """
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["kind"] not in ("texture", "material"):
        return jsonify({"error": "Only textures or materials can be sent as a material."}), 400
    data = request.get_json(silent=True) or {}
    entry = _material_entry(asset, data.get("to_selection", True))
    _queue_blender(entry)
    return jsonify({"ok": True, "maps": sorted(entry["maps"].keys())})


@app.post("/api/assets/<int:asset_id>/send-hdri")
def send_hdri(asset_id):
    """Set an HDRI as the Blender scene's world/environment lighting."""
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["kind"] != "hdri":
        return jsonify({"error": "Only HDRIs can be set as world lighting."}), 400
    data = request.get_json(silent=True) or {}
    _queue_blender(_hdri_entry(asset, data.get("strength", 1.0)))
    return jsonify({"ok": True})


@app.post("/api/assets/batch/send-blender")
def batch_send_blender():
    """Send a multi-selection to Blender in one go. Each asset is dispatched by
    kind: models import, textures/materials build a material, HDRIs set the
    world. Texture maps that belong to the same set are sent once, as one
    material, rather than per-map."""
    data = request.get_json(force=True)
    ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
    if not ids:
        return jsonify({"error": "ids required"}), 400
    counts = {"model": 0, "material": 0, "hdri": 0, "skipped": 0}
    seen_sets = set()  # collapse a selected texture set to a single material
    for aid in ids:
        asset = store.get_asset(aid)
        if not asset:
            counts["skipped"] += 1
            continue
        kind = asset["kind"]
        if kind == "model":
            _queue_blender(_import_entry(asset))
            counts["model"] += 1
        elif kind in ("texture", "material"):
            key = asset.get("set_key") or f"id:{aid}"
            if key in seen_sets:
                continue
            seen_sets.add(key)
            _queue_blender(_material_entry(asset))
            counts["material"] += 1
        elif kind == "hdri":
            _queue_blender(_hdri_entry(asset))
            counts["hdri"] += 1
        else:
            counts["skipped"] += 1
    return jsonify({"ok": True, **counts})


@app.post("/api/assets/<int:asset_id>/render-blend")
@app.post("/api/assets/<int:asset_id>/render")
def render_model_preview(asset_id):
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["kind"] != "model" or asset["ext"] not in thumbs.BLENDER_RENDER_EXTS:
        return jsonify({"error": "This file can't be rendered by Blender."}), 400
    if not thumbs.blender_available():
        return jsonify({
            "blender": False,
            "error": "Blender wasn't found. Click “Set Blender path” and "
                     "point Hangar at your blender executable.",
        }), 200
    path = thumbs.render_model_preview(asset)
    if not path:
        return jsonify({
            "blender": True,
            "error": thumbs.LAST_RENDER_ERROR or "Render didn't produce an image.",
            "log": str(thumbs.RENDER_LOG),
        }), 200
    return jsonify({"ok": True})


# ---- batch preview regeneration -------------------------------------------
# Right-click → "Regenerate previews" on a multi-selection. A POOL of up to
# RENDER_WORKERS background threads drains the queue, each running its own Blender
# render concurrently, and reports progress through /status so the status bar
# shows live feedback. Firing another selection while one is running APPENDS to
# the queue (total grows, pool tops up) — it doesn't get rejected.
REGEN = {"running": False, "done": 0, "total": 0, "ok": 0, "failed": 0,
         "current": "", "last_error": "", "finished_at": 0}
REGEN_LOCK = threading.Lock()
REGEN_QUEUE = []          # pending asset ids, drained concurrently by the pool
REGEN_ACTIVE = 0          # live LOCAL worker threads

# ---- render farm (LAN/Tailscale workers sharing the same queue) ------------
# Remote workers (other machines that can see the same asset paths, e.g. the NAS
# over Tailscale) claim chunks off REGEN_QUEUE, render locally and post the JPEG
# back. They share the queue with the local pool, so progress accounting and the
# status bar cover both. All farm state lives under REGEN_LOCK with the queue.
FARM = {}                 # worker_id -> {name, gpu, last_seen, done, failed}
FARM_LEASES = {}          # asset_id -> {"worker": id, "ts": float}
FARM_LOCAL_ONLY = set()   # ids a remote couldn't reach (path not visible) → local
FARM_CHUNK = 30           # jobs handed out per claim (tunable from the UI)
FARM_TOKEN = os.environ.get("HANGAR_FARM_TOKEN", "")
LEASE_TIMEOUT = 300       # requeue a job if its worker goes silent this long


def _regen_maybe_finish():
    """Caller holds REGEN_LOCK. The run is done only when the queue is empty, no
    local worker is rendering, AND no farm worker has an outstanding lease."""
    if (REGEN["running"] and not REGEN_QUEUE
            and REGEN_ACTIVE == 0 and not FARM_LEASES):
        REGEN.update(running=False, current="", finished_at=time.time())


def _regen_topup_locked():
    """Caller holds REGEN_LOCK. Returns how many local workers to spawn to cover
    the pending queue (capped at RENDER_WORKERS). Start the threads outside the
    lock."""
    global REGEN_ACTIVE
    want = min(RENDER_WORKERS, len(REGEN_QUEUE))
    n = max(0, want - REGEN_ACTIVE)
    REGEN_ACTIVE += n
    return n


def _regen_worker():
    global REGEN_ACTIVE
    while True:
        with REGEN_LOCK:
            if not REGEN_QUEUE:
                # Decrement and the queue-empty check happen under the SAME lock
                # as batch_render's top-up, so a late append can never strand
                # work: either we still see it here, or batch_render sees
                # running=False and starts a fresh pool.
                REGEN_ACTIVE -= 1
                _regen_maybe_finish()
                return
            aid = REGEN_QUEUE.pop(0)
        asset = store.get_asset(aid)
        ok = False
        err = ""
        if not asset:
            err = f"Asset {aid} not found."
        elif asset["kind"] != "model" or asset["ext"] not in thumbs.BLENDER_RENDER_EXTS:
            err = f"Can't render {asset.get('path', aid)}"
        elif not thumbs.blender_available():
            err = "Blender not found — set its path in settings."
        else:
            with REGEN_LOCK:
                REGEN["current"] = asset["path"]
            try:
                if thumbs.render_model_preview(asset):
                    ok = True
                else:
                    err = (thumbs.LAST_RENDER_ERROR
                           or "Render produced no image") + f"  [{asset['path']}]"
            except Exception as e:
                err = f"{e}  [{asset['path']}]"
        with REGEN_LOCK:
            REGEN["done"] += 1
            if ok:
                REGEN["ok"] += 1
            else:
                REGEN["failed"] += 1
                REGEN["last_error"] = err
        time.sleep(0.02)              # tiny yield so the UI stays responsive


@app.post("/api/assets/batch/render")
def batch_render():
    global REGEN_ACTIVE
    data = request.get_json(force=True)
    ids = data.get("ids") or []
    if not ids:
        return jsonify({"ok": False, "error": "No assets selected."}), 200
    if not thumbs.blender_available():
        return jsonify({"ok": False, "blender": False,
                        "error": "Blender wasn't found. Set its path in settings "
                                 "to regenerate previews."}), 200
    with REGEN_LOCK:
        fresh = not REGEN["running"]
        if fresh:
            # Previous run (if any) is finished — start counters over.
            REGEN.update(running=True, done=0, total=0, ok=0, failed=0,
                         current="", last_error="", finished_at=0)
            REGEN_QUEUE.clear()
        REGEN_QUEUE.extend(ids)
        REGEN["total"] += len(ids)
        total = REGEN["total"]
        # Top the local pool up (capped at RENDER_WORKERS). Remote farm workers,
        # if any are connected, also claim from this same queue.
        to_start = _regen_topup_locked()
    for _ in range(to_start):
        threading.Thread(target=_regen_worker, daemon=True).start()
    return jsonify({"ok": True, "count": len(ids), "total": total,
                    "queued": not fresh, "workers": RENDER_WORKERS})


@app.get("/api/assets/batch/render/status")
def batch_render_status():
    now = time.time()
    with REGEN_LOCK:
        s = dict(REGEN)
        s["farm_workers"] = sum(
            1 for w in FARM.values() if now - w.get("last_seen", 0) < LEASE_TIMEOUT)
        s["farm_inflight"] = len(FARM_LEASES)
    s["pct"] = round(100 * s["done"] / s["total"]) if s["total"] else 0
    return jsonify(s)


# ---- render-farm coordinator endpoints ------------------------------------
def _farm_authed():
    supplied = request.headers.get("X-Hangar-Farm-Token", "")
    if FARM_TOKEN:
        return supplied == FARM_TOKEN
    # Keep single-machine usage zero-config, but don't accept unauthenticated
    # render-farm calls from LAN/Tailscale clients.
    remote = request.remote_addr or ""
    return remote in ("127.0.0.1", "::1", "localhost")


def _farm_touch_locked(wid):
    """Refresh a worker's last_seen and keep all its in-flight leases alive — any
    contact from a worker proves it's still chewing through its chunk."""
    now = time.time()
    w = FARM.get(wid)
    if w is not None:
        w["last_seen"] = now
    for v in FARM_LEASES.values():
        if v["worker"] == wid:
            v["ts"] = now


@app.post("/api/farm/register")
def farm_register():
    if not _farm_authed():
        return jsonify({"ok": False, "error": "bad token"}), 403
    d = request.get_json(force=True)
    wid = (d.get("worker_id") or "").strip()
    if not wid:
        return jsonify({"ok": False, "error": "worker_id required"}), 200
    with REGEN_LOCK:
        w = FARM.setdefault(wid, {"done": 0, "failed": 0})
        w.update(name=d.get("name") or wid, gpu=d.get("gpu") or "?",
                 last_seen=time.time())
    return jsonify({"ok": True, "lease_timeout": LEASE_TIMEOUT, "chunk": FARM_CHUNK})


@app.post("/api/farm/claim")
def farm_claim():
    if not _farm_authed():
        return jsonify({"ok": False, "error": "bad token"}), 403
    wid = (request.get_json(force=True).get("worker_id") or "").strip()
    jobs = []
    with REGEN_LOCK:
        if wid not in FARM:
            return jsonify({"ok": False, "error": "register first"}), 200
        _farm_touch_locked(wid)
        taken, skipped = [], []
        while REGEN_QUEUE and len(taken) < FARM_CHUNK:
            aid = REGEN_QUEUE.pop(0)
            (skipped if aid in FARM_LOCAL_ONLY else taken).append(aid)
        for aid in reversed(skipped):     # local-only — leave for the local pool
            REGEN_QUEUE.insert(0, aid)
        now = time.time()
        for aid in taken:
            asset = store.get_asset(aid)
            if asset:
                FARM_LEASES[aid] = {"worker": wid, "ts": now}
                jobs.append({"id": aid, "path": asset["path"], "ext": asset["ext"]})
            elif REGEN["running"]:        # unknown asset — count it off
                REGEN["done"] += 1
                REGEN["failed"] += 1
        # Local-only items may have been left behind with no local worker running.
        to_start = _regen_topup_locked()
        _regen_maybe_finish()
    for _ in range(to_start):
        threading.Thread(target=_regen_worker, daemon=True).start()
    return jsonify({"ok": True, "jobs": jobs})


@app.post("/api/farm/result/<int:asset_id>")
def farm_result(asset_id):
    if not _farm_authed():
        return jsonify({"ok": False, "error": "bad token"}), 403
    wid = request.args.get("worker", "")
    data = request.get_data()             # raw JPEG bytes
    asset = store.get_asset(asset_id)
    saved = False
    if asset and data:
        try:
            saved = thumbs.save_thumbnail_bytes(asset, data)
        except Exception:
            saved = False
    with REGEN_LOCK:
        lease = FARM_LEASES.pop(asset_id, None)
        _farm_touch_locked(wid)
        w = FARM.get(wid)
        if lease is not None:
            REGEN["done"] += 1
            if saved:
                REGEN["ok"] += 1
                if w:
                    w["done"] = w.get("done", 0) + 1
            else:
                REGEN["failed"] += 1
                if w:
                    w["failed"] = w.get("failed", 0) + 1
                REGEN["last_error"] = f"farm result not saved for asset {asset_id}"
        _regen_maybe_finish()
    return jsonify({"ok": saved})


@app.post("/api/farm/fail/<int:asset_id>")
def farm_fail(asset_id):
    if not _farm_authed():
        return jsonify({"ok": False, "error": "bad token"}), 403
    wid = request.args.get("worker", "")
    reason = (request.get_json(silent=True) or {}).get("reason", "")
    to_start = 0
    with REGEN_LOCK:
        lease = FARM_LEASES.pop(asset_id, None)
        _farm_touch_locked(wid)
        w = FARM.get(wid)
        if reason == "unreachable":
            # The remote can't see this path (e.g. a local-drive asset) — hand it
            # back for LOCAL rendering only so it doesn't bounce around the farm.
            FARM_LOCAL_ONLY.add(asset_id)
            REGEN_QUEUE.append(asset_id)
            to_start = _regen_topup_locked()
        elif lease is not None:
            REGEN["done"] += 1
            REGEN["failed"] += 1
            if w:
                w["failed"] = w.get("failed", 0) + 1
            REGEN["last_error"] = f"farm: {reason or 'render failed'} [asset {asset_id}]"
        _regen_maybe_finish()
    for _ in range(to_start):
        threading.Thread(target=_regen_worker, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/farm/workers")
def farm_workers():
    now = time.time()
    with REGEN_LOCK:
        workers = [{
            "id": wid, "name": w.get("name", wid), "gpu": w.get("gpu", "?"),
            "done": w.get("done", 0), "failed": w.get("failed", 0),
            "claimed": sum(1 for v in FARM_LEASES.values() if v["worker"] == wid),
            "online": (now - w.get("last_seen", 0)) < LEASE_TIMEOUT,
        } for wid, w in FARM.items()]
        chunk = FARM_CHUNK
    workers.sort(key=lambda x: (not x["online"], x["name"]))
    return jsonify({"workers": workers, "chunk": chunk, "token_required": bool(FARM_TOKEN)})


@app.post("/api/farm/chunk")
def farm_set_chunk():
    global FARM_CHUNK
    try:
        n = int(request.get_json(force=True).get("chunk"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "chunk must be a number"}), 200
    with REGEN_LOCK:
        FARM_CHUNK = max(1, min(500, n))
        chunk = FARM_CHUNK
    return jsonify({"ok": True, "chunk": chunk})


# Source modules a standalone render worker needs. Resolved from BASE_DIR so the
# bundle works both from source and from the frozen build (added via --add-data).
_WORKER_BUNDLE_FILES = ["worker_main.py", "worker.py", "store.py", "thumbs.py", "scanner.py"]


def _worker_run_bat(coord, token):
    tok = f" --token {token}" if token else ""
    return ("@echo off\r\n"
            "REM Hangar render worker - needs Python 3.10+ and Blender installed.\r\n"
            "pip install -r requirements.txt\r\n"
            f"python worker_main.py --coordinator {coord}{tok}\r\n"
            "pause\r\n")


def _worker_run_sh(coord, token):
    tok = f" --token {token}" if token else ""
    return ("#!/usr/bin/env bash\n"
            "# Hangar render worker - needs Python 3.10+ and Blender installed.\n"
            "set -e\n"
            'cd "$(dirname "$0")"\n'
            "pip install -r requirements.txt\n"
            f"exec python3 worker_main.py --coordinator {coord}{tok}\n")


def _worker_readme(coord, token):
    tok = f"Farm token: {token}\n" if token else ""
    return (
        "Hangar render worker\n====================\n\n"
        "Runs on another machine to help Hangar generate previews. It must be\n"
        "able to SEE THE SAME ASSET PATHS as Hangar (e.g. the NAS share over\n"
        "Tailscale) and have Blender installed.\n\n"
        "Requirements: Python 3.10+, Blender, and `pip install -r requirements.txt`.\n\n"
        f"Coordinator (this Hangar): {coord}\n{tok}\n"
        "Run it:\n"
        "  Windows:      double-click run.bat\n"
        "  macOS/Linux:  ./run.sh\n\n"
        "Or manually:\n"
        f"  python worker_main.py --coordinator {coord}\n\n"
        "It registers, claims chunks of render jobs, renders each with Blender,\n"
        "and posts the JPEG back. Leave it running; Ctrl+C to stop.\n")


@app.get("/api/farm/worker-download")
def farm_worker_download():
    """Bundle the standalone render worker (source modules + run scripts + readme,
    pre-filled with this Hangar's address) into a zip to copy onto a remote
    machine. Far lighter than shipping the whole GUI app."""
    import io
    import zipfile
    coord = request.host_url.rstrip("/")
    missing = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in _WORKER_BUNDLE_FILES:
            p = os.path.join(BASE_DIR, f)
            if os.path.exists(p):
                z.write(p, f"HangarWorker/{f}")
            else:
                missing.append(f)
        z.writestr("HangarWorker/requirements.txt", "Pillow>=10.0\nzstandard>=0.21\n")
        z.writestr("HangarWorker/run.bat", _worker_run_bat(coord, FARM_TOKEN))
        sh = zipfile.ZipInfo("HangarWorker/run.sh")
        sh.external_attr = 0o755 << 16            # keep the +x bit on unzip
        z.writestr(sh, _worker_run_sh(coord, FARM_TOKEN))
        z.writestr("HangarWorker/README.txt", _worker_readme(coord, FARM_TOKEN))
    if missing:
        app.logger.warning("worker bundle missing source files "
                           "(frozen build needs --add-data): %s", missing)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="HangarWorker.zip")


def _farm_reaper():
    """Requeue jobs whose worker has gone silent past the lease timeout, and make
    sure the local pool is running to pick them up. Runs forever in the background."""
    while True:
        time.sleep(15)
        to_start = 0
        with REGEN_LOCK:
            now = time.time()
            stale = [aid for aid, v in FARM_LEASES.items()
                     if now - v["ts"] > LEASE_TIMEOUT]
            for aid in stale:
                del FARM_LEASES[aid]
                REGEN_QUEUE.append(aid)
            if stale:
                REGEN["last_error"] = f"requeued {len(stale)} stalled farm job(s)"
                to_start = _regen_topup_locked()
            _regen_maybe_finish()
        for _ in range(to_start):
            threading.Thread(target=_regen_worker, daemon=True).start()


threading.Thread(target=_farm_reaper, daemon=True).start()


@app.post("/api/settings/blender")
def set_blender():
    """Set (or clear) the Blender executable path used for previews."""
    data = request.get_json(force=True)
    ok, resolved = thumbs.set_blender_path(data.get("path", ""))
    if not ok:
        return jsonify({"ok": False,
                        "error": "That path doesn't exist."}), 200
    return jsonify({"ok": True, "blender": resolved,
                    "available": thumbs.blender_available()})


# ---- in-app updater -------------------------------------------------------
# Checks GitHub Releases, downloads + extracts the new build to a SIBLING folder
# (never overwrites the running install, so a failed update can't brick it), then
# reveals/launches it. The new instance binds a free port (see desktop.py), so it
# can run alongside the old one.
GITHUB_REPO = "4s0ck3t/Hangar"
UPDATE = {"running": False, "pct": 0, "done": False, "path": None,
          "folder": None, "exe": None, "error": None}
UPDATE_LOCK = threading.Lock()

_RELEASE_CACHE: dict = {}          # {"data": ..., "ts": float}
_RELEASE_CACHE_TTL = 3600          # re-fetch at most once per hour


def _version_tuple(s):
    import re
    nums = re.findall(r"\d+", s or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def _fetch_latest_release(force=False):
    import urllib.request
    now = time.time()
    cached = _RELEASE_CACHE.get("data")
    # The hour-long cache exists to throttle the *automatic* boot check. An
    # explicit "Check for updates" click must hit GitHub fresh, otherwise it
    # keeps reporting a stale "you're on the latest" for up to an hour after a
    # release ships.
    if not force and cached and now - _RELEASE_CACHE.get("ts", 0) < _RELEASE_CACHE_TTL:
        return cached
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Hangar-updater",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    _RELEASE_CACHE["data"] = data
    _RELEASE_CACHE["ts"] = now
    return data


def _downloads_dir():
    home = os.path.expanduser("~")
    dl = os.path.join(home, "Downloads")
    if os.path.isdir(dl):
        return dl
    return home if os.path.isdir(home) else str(store.DATA_DIR)


def _reveal_path(path):
    sysname = platform.system()
    try:
        if sysname == "Windows":
            subprocess.run(["explorer", "/select,", os.path.normpath(path)], check=False)
        elif sysname == "Darwin":
            subprocess.run(["open", "-R", path], check=False)
        else:
            opener = "xdg-open"
            subprocess.run([opener, os.path.dirname(path)], check=False)
    except Exception:
        pass


def _platform_asset(assets):
    """Pick the release asset for the current OS: Windows -> the .zip, Linux ->
    the .tar.gz, falling back to any archive if the named one is missing."""
    sysname = platform.system()
    ext = {"Windows": ".zip", "Linux": ".tar.gz", "Darwin": ".zip"}.get(sysname, ".zip")
    kw = {"Windows": "windows", "Linux": "linux", "Darwin": "mac"}.get(sysname, "")
    names = [(a, (a.get("name") or "").lower()) for a in assets]
    by_ext = [a for a, n in names if n.endswith(ext)]
    preferred = [a for a in by_ext if kw in (a.get("name") or "").lower()]
    if preferred:
        return preferred[0]
    if by_ext:
        return by_ext[0]
    # last resort: any archive at all
    arch = [a for a, n in names if n.endswith((".zip", ".tar.gz", ".tgz"))]
    return arch[0] if arch else None


@app.get("/api/update/check")
def update_check():
    force = request.args.get("force") in ("1", "true", "yes")
    try:
        rel = _fetch_latest_release(force=force)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't reach GitHub: {e}"}), 200
    latest = (rel.get("tag_name") or "").lstrip("v")
    asset = _platform_asset(rel.get("assets", []))
    return jsonify({
        "ok": True,
        "current": __version__,
        "latest": latest,
        "update_available": _version_tuple(latest) > _version_tuple(__version__),
        "notes": rel.get("body", ""),
        "html_url": rel.get("html_url", ""),
        "asset_url": asset.get("browser_download_url") if asset else None,
        "asset_name": asset.get("name") if asset else None,
        "asset_size": asset.get("size") if asset else None,
    })


def _do_update_download(url, name, version):
    import urllib.request
    import tarfile
    import shutil
    dest = os.path.join(_downloads_dir(), name)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Hangar-updater"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            got = 0
            with open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    with UPDATE_LOCK:
                        UPDATE["pct"] = int(got * 100 / total) if total else 0
        # Extract to a fresh sibling folder so the running install is untouched.
        folder = os.path.join(_downloads_dir(), f"Hangar-{version}")
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
        low = name.lower()
        if low.endswith((".tar.gz", ".tgz")):
            _safe_extract_tar(dest, folder)
        else:
            _safe_extract_zip(dest, folder)
        # The launcher binary: Hangar.exe on Windows, Hangar on Linux/macOS.
        exe_name = "Hangar.exe" if platform.system() == "Windows" else "Hangar"
        exe = os.path.join(folder, exe_name)
        if not os.path.exists(exe):
            # Some archives nest everything under a top folder — search one level.
            for root, _dirs, files in os.walk(folder):
                if exe_name in files:
                    exe = os.path.join(root, exe_name)
                    break
            else:
                exe = None
        if exe and platform.system() != "Windows":
            try:
                os.chmod(exe, 0o755)
            except Exception:
                pass
        with UPDATE_LOCK:
            UPDATE.update(running=False, done=True, pct=100,
                          path=dest, folder=folder, exe=exe)
        # Don't pop an Explorer window here — the "Restart" button launches the
        # new exe directly (update_launch). Explorer is only used as a fallback
        # there if the exe can't be found.
    except Exception as e:
        with UPDATE_LOCK:
            UPDATE.update(running=False, done=False, error=str(e))


def _is_within_dir(base, candidate):
    base = os.path.abspath(base)
    candidate = os.path.abspath(candidate)
    try:
        return os.path.commonpath([base, candidate]) == base
    except ValueError:
        return False


def _safe_extract_tar(archive, folder):
    import tarfile
    with tarfile.open(archive, "r:gz") as t:
        for member in t.getmembers():
            target = os.path.join(folder, member.name)
            if (member.name.startswith(("/", "\\")) or member.issym()
                    or member.islnk() or not _is_within_dir(folder, target)):
                raise RuntimeError(f"Unsafe path in update archive: {member.name}")
        t.extractall(folder)


def _safe_extract_zip(archive, folder):
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            target = os.path.join(folder, member.filename)
            if (member.filename.startswith(("/", "\\")) or
                    not _is_within_dir(folder, target)):
                raise RuntimeError(f"Unsafe path in update archive: {member.filename}")
        z.extractall(folder)


@app.post("/api/update/download")
def update_download():
    data = request.get_json(force=True) or {}
    url = data.get("url")
    name = data.get("name") or "Hangar-windows.zip"
    version = (data.get("version") or "latest").lstrip("v")
    if not url:
        return jsonify({"ok": False, "error": "No download URL provided."}), 200
    with UPDATE_LOCK:
        if UPDATE["running"]:
            return jsonify({"ok": True, "running": True})
        UPDATE.update(running=True, pct=0, done=False, path=None,
                      folder=None, exe=None, error=None)
    threading.Thread(target=_do_update_download, args=(url, name, version),
                     daemon=True).start()
    return jsonify({"ok": True, "running": True})


@app.get("/api/update/status")
def update_status():
    with UPDATE_LOCK:
        return jsonify(dict(UPDATE))


@app.post("/api/update/launch")
def update_launch():
    with UPDATE_LOCK:
        exe = UPDATE.get("exe")
        folder = UPDATE.get("folder")
    if not exe or not os.path.exists(exe):
        if folder:
            _reveal_path(folder)
        return jsonify({"ok": False,
                        "error": "Couldn't find the new Hangar.exe — opening the folder so you can run it."}), 200
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200
    # Hand over: close this (old) instance + its window once the response is sent,
    # so the freshly launched version takes over without two windows lingering.
    _schedule_self_shutdown()
    return jsonify({"ok": True})


# Set by desktop.py to the Edge/Chrome --app window subprocess (if that path is
# used), so we can close the window on handover. None for the in-process
# pywebview window (os._exit closes it) or a plain browser tab.
WINDOW_PROC = None
WINDOW_PROFILE = None


def _terminate_window_processes():
    """Best-effort close for the Chromium --app window used by frozen builds.

    Chromium's launcher process often exits immediately after handing off to the
    real browser process, so WINDOW_PROC alone is not enough. The app window uses
    a unique user-data-dir per Hangar launch; matching that command-line flag lets
    us close the old window during update handoff without touching other browser
    windows.
    """
    try:
        if WINDOW_PROC is not None and WINDOW_PROC.poll() is None:
            WINDOW_PROC.terminate()
    except Exception:
        pass
    profile = WINDOW_PROFILE
    if not profile:
        return
    try:
        if platform.system() == "Windows":
            quoted = "'" + os.path.normpath(profile).replace("'", "''") + "'"
            ps = (
                "$profile = " + quoted + "; "
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) } | "
                "ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }"
            )
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-Command", ps],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        else:
            subprocess.run(["pkill", "-f", profile], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
    except Exception:
        pass


def _schedule_self_shutdown(delay=0.8):
    def _bye():
        time.sleep(delay)
        _terminate_window_processes()
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()


def _open_browser():
    time.sleep(0.8)
    webbrowser.open(f"http://{HOST}:{PORT}")


def run_server(open_browser=False):
    if open_browser:
        threading.Thread(target=_open_browser, daemon=True).start()
    # Warm any thumbnails missing from a previous run (e.g. a library indexed
    # before pre-baking existed, or new files added while Hangar was closed).
    # has_cached_thumb makes this a cheap no-op once everything is baked.
    _start_warm()
    _start_meta_index()   # index .blend asset metadata for search
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    print(f"\n  Hangar running at http://{HOST}:{PORT}")
    print(f"  Library + index stored in {store.DATA_DIR}\n")
    run_server(open_browser="--no-browser" not in sys.argv)
