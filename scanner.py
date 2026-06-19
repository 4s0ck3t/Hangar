"""Filesystem scanner + classification for Hangar.

Scanning is intentionally cheap: it only reads file metadata (size, mtime) so
that indexing a large library stays fast. Expensive work (mesh vertex/face
counts) is computed lazily on first asset open and cached in the DB.
"""

import os
from pathlib import Path

import store

# Extension -> asset kind. Order of dict groups documents intent.
MODEL_EXTS = {
    ".blend", ".fbx", ".obj", ".gltf", ".glb", ".stl", ".ply",
    ".usd", ".usda", ".usdc", ".usdz", ".abc", ".dae", ".3ds",
}
TEXTURE_EXTS = {
    ".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".bmp", ".webp",
}
HDRI_EXTS = {".hdr", ".exr"}
MATERIAL_EXTS = {".sbsar", ".mat", ".mtl"}

EXT_KIND = {}
for e in MODEL_EXTS:
    EXT_KIND[e] = "model"
for e in TEXTURE_EXTS:
    EXT_KIND[e] = "texture"
for e in HDRI_EXTS:
    EXT_KIND[e] = "hdri"
for e in MATERIAL_EXTS:
    EXT_KIND[e] = "material"

ALL_EXTS = set(EXT_KIND)

IGNORE_DIRS = {".git", "__pycache__", ".hangar", "node_modules", ".svn"}
# Texture maps that are part of a model rather than browsable assets in their
# own right would flood the grid; we still index them but they're filterable.

MAX_STATS_BYTES = 250 * 1024 * 1024  # skip mesh-stat parsing above this size


def count_files(library_path):
    """Fast pre-pass: how many indexable files live under this root.

    Used to give the progress bar a real denominator before the slower
    stat/upsert pass runs.
    """
    library_path = str(Path(library_path).expanduser().resolve())
    n = 0
    for root, dirs, files in os.walk(library_path):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for fname in files:
            if os.path.splitext(fname)[1].lower() in ALL_EXTS:
                n += 1
    return n


def scan_library(library_path, on_file=None):
    """Walk one library root, upsert assets, flag anything that vanished.

    on_file(full_path) is called after each indexed file so callers can
    report progress.
    """
    library_path = str(Path(library_path).expanduser().resolve())
    seen = set()
    found = 0
    for root, dirs, files in os.walk(library_path):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ALL_EXTS:
                continue
            full = os.path.join(root, fname)
            try:
                st = os.stat(full)
            except OSError:
                continue
            meta = {
                "path": full,
                "name": os.path.splitext(fname)[0],
                "ext": ext,
                "kind": EXT_KIND[ext],
                "size": st.st_size,
                "mtime": st.st_mtime,
            }
            asset_id = store.upsert_asset(meta)
            seen.add(asset_id)
            found += 1
            if on_file:
                on_file(full)
    store.mark_missing(seen, library_path)
    return found


def scan_all(on_file=None):
    total = 0
    for lib in store.list_libraries():
        total += scan_library(lib["path"], on_file=on_file)
    return total


def compute_stats(asset):
    """Best-effort vertex/face count for a model. Cached by caller.

    Returns (vertices, faces) with None when unavailable. Never raises.
    """
    if asset["kind"] != "model":
        return None, None
    if asset["size"] > MAX_STATS_BYTES:
        return None, None
    try:
        import trimesh  # optional dependency
    except Exception:
        return None, None
    try:
        loaded = trimesh.load(asset["path"], force="mesh", process=False)
        if loaded is None or not hasattr(loaded, "vertices"):
            return None, None
        return int(len(loaded.vertices)), int(len(loaded.faces))
    except Exception:
        return None, None
