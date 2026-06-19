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
        "current": "", "library": "", "indexed": 0, "finished_at": 0}
SCAN_LOCK = threading.Lock()


def _run_scan(libs):
    """libs: list of (path, name). Runs in a daemon thread."""
    with SCAN_LOCK:
        SCAN.update(running=True, scanned=0, total=0, current="",
                    library="", indexed=0)
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
    for path, name in libs:
        with SCAN_LOCK:
            SCAN["library"] = name
        try:
            indexed += scanner.scan_library(path, on_file=on_file)
        except Exception as e:
            print(f"[Hangar] scan error in {path}: {e}")
    with SCAN_LOCK:
        SCAN.update(running=False, indexed=indexed, current="",
                    finished_at=time.time())


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
        "libraries": store.list_libraries(),
        "tags": store.list_tags(),
        "collections": store.list_collections(),
        "counts": store.kind_counts(),
        "blender_queue": str(BLENDER_QUEUE),
        "blender_render": thumbs.blender_available(),
        "blender_render_exts": sorted(thumbs.BLENDER_RENDER_EXTS),
        "desktop": bool(os.environ.get("HANGAR_DESKTOP")),
    })


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

    In the desktop build the picker is handled by pywebview's native dialog;
    this Tk-based path covers running Hangar in a plain browser.
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
        favorite=q.get("favorite") == "1",
        sort=q.get("sort", "name"),
        limit=int(q.get("limit", 300)),
        offset=int(q.get("offset", 0)),
    )
    return jsonify({"assets": assets, "total": total})


@app.get("/api/assets/<int:asset_id>")
def asset_detail(asset_id):
    asset = store.get_asset(asset_id)
    if not asset:
        return jsonify({"error": "Asset not found."}), 404
    if asset["kind"] == "model" and not asset["stats_done"]:
        v, f = scanner.compute_stats(asset)
        store.save_stats(asset_id, v, f)
        asset["vertices"], asset["faces"], asset["stats_done"] = v, f, 1
    return jsonify(asset)


@app.get("/api/thumb/<int:asset_id>")
def thumb(asset_id):
    asset = store.get_asset(asset_id)
    if not asset:
        return "", 404
    path = thumbs.get_or_make(asset)
    if not path:
        return "", 404
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


# ---- tags & collections ---------------------------------------------------

@app.post("/api/tags")
def new_tag():
    data = request.get_json(force=True)
    store.create_tag(data["name"], data.get("color", "#8A8F9A"))
    return jsonify({"ok": True, "tags": store.list_tags()})


@app.post("/api/collections")
def new_collection():
    store.create_collection(request.get_json(force=True)["name"])
    return jsonify({"ok": True, "collections": store.list_collections()})


# ---- OS integration -------------------------------------------------------

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
            "error": "Blender wasn't found. Install it, or set HANGAR_BLENDER "
                     "to your blender executable, then restart Hangar.",
        }), 200
    path = thumbs.render_model_preview(asset)
    if not path:
        return jsonify({"blender": True,
                        "error": "Render didn't produce an image."}), 200
    return jsonify({"ok": True})


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
