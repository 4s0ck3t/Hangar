"""Hangar — local-first 3D asset manager.

Run as a desktop app:   python desktop.py
Run as a local web app: python app.py  (opens in your browser)
"""

import json
import os
import platform
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

import store
import scanner
import thumbs

__version__ = "0.13.29"

HOST = "127.0.0.1"
PORT = int(os.environ.get("HANGAR_PORT", "7575"))
BLENDER_QUEUE = store.DATA_DIR / "blender_queue.jsonl"

# When frozen by PyInstaller the static files live under sys._MEIPASS.
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

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


def _start_scan(libs):
    with SCAN_LOCK:
        if SCAN["running"]:
            return False
    threading.Thread(target=_run_scan, args=(libs,), daemon=True).start()
    return True


# ---- pages ----------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---- state / dashboard ----------------------------------------------------

@app.get("/api/state")
def state():
    return jsonify({
        "version": __version__,
        "libraries": store.list_libraries(),
        "tags": store.list_tags(),
        "collections": store.list_collections(),
        "categories": store.list_categories(),
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
    ]
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
    )
    for a in assets:
        a["has_thumb"] = thumbs.has_cached_thumb(a)
    return jsonify({"assets": assets, "total": total})


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
    asset["exists"] = os.path.exists(asset["path"])
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
    return send_file(path, mimetype="image/jpeg")


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


@app.post("/api/assets/batch/category")
def batch_category():
    data = request.get_json(force=True)
    ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
    category = (data.get("category") or "").strip()
    if not ids or not category:
        return jsonify({"error": "ids and category required"}), 400
    for aid in ids:
        store.set_category_membership(category, aid, add=True)
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
    try:
        if sysname == "Darwin":
            subprocess.run(["open", "-R", path], check=False)
        elif sysname == "Windows":
            subprocess.run(["explorer", "/select,", os.path.normpath(path)], check=False)
        else:
            import shutil as _shutil
            # Try file-manager-specific "select" flags, fall back to opening the folder.
            if _shutil.which("nautilus"):
                subprocess.Popen(["nautilus", "--select", path])
            elif _shutil.which("dolphin"):
                subprocess.Popen(["dolphin", "--select", path])
            elif _shutil.which("nemo"):
                subprocess.Popen(["nemo", path])
            elif _shutil.which("thunar"):
                subprocess.Popen(["thunar", path])
            else:
                subprocess.run(["xdg-open", os.path.dirname(path)], check=False)
    except Exception as e:
        return jsonify({"error": f"Couldn't open the file manager: {e}"}), 500
    return jsonify({"ok": True})


@app.post("/api/assets/<int:asset_id>/send-blender")
def send_blender(asset_id):
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["kind"] != "model":
        return jsonify({"error": "Only model files can be sent to Blender."}), 400
    entry = {"action": "import", "path": asset["path"],
             "ext": asset["ext"], "ts": time.time()}
    with open(BLENDER_QUEUE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return jsonify({"ok": True, "queued": asset["name"]})


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


def _version_tuple(s):
    import re
    nums = re.findall(r"\d+", s or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def _fetch_latest_release():
    import urllib.request
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Hangar-updater",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    try:
        rel = _fetch_latest_release()
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
    import zipfile
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
            with tarfile.open(dest, "r:gz") as t:
                t.extractall(folder)
        else:
            with zipfile.ZipFile(dest) as z:
                z.extractall(folder)
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
        _reveal_path(exe or folder)
    except Exception as e:
        with UPDATE_LOCK:
            UPDATE.update(running=False, done=False, error=str(e))


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


def _schedule_self_shutdown(delay=0.8):
    def _bye():
        time.sleep(delay)
        try:
            if WINDOW_PROC is not None:
                WINDOW_PROC.terminate()
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()


def _open_browser():
    time.sleep(0.8)
    webbrowser.open(f"http://{HOST}:{PORT}")


def run_server(open_browser=False):
    if open_browser:
        threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    print(f"\n  Hangar running at http://{HOST}:{PORT}")
    print(f"  Library + index stored in {store.DATA_DIR}\n")
    run_server(open_browser="--no-browser" not in sys.argv)
