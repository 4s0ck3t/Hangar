"""Filesystem scanner + classification for Hangar.

Scanning is intentionally cheap: it only reads file metadata (size, mtime) so
that indexing a large library stays fast. Expensive work (mesh vertex/face
counts) is computed lazily on first asset open and cached in the DB.
"""

import os
import re
from pathlib import Path

import store

# Texture map-role detection. A material is usually delivered as several maps
# sharing a base name (wood_diffuse, wood_normal, wood_roughness…). We strip the
# role + resolution tokens to recover that shared base so the maps collapse into
# one tile, and remember the role + a sort order (lower = better thumbnail, so the
# colour/diffuse map represents the set).
MAP_ROLES = {
    # token: (canonical role, order)
    "diffuse": ("diffuse", 0), "diff": ("diffuse", 0), "albedo": ("diffuse", 0),
    "alb": ("diffuse", 0), "basecolor": ("diffuse", 0), "color": ("diffuse", 0),
    "col": ("diffuse", 0), "colour": ("diffuse", 0),
    "emission": ("emission", 5), "emissive": ("emission", 5), "emit": ("emission", 5),
    "glow": ("emission", 5),
    "specular": ("specular", 10), "spec": ("specular", 10),
    "gloss": ("gloss", 12), "glossiness": ("gloss", 12),
    "ao": ("ao", 15), "occlusion": ("ao", 15), "ambientocclusion": ("ao", 15),
    "occ": ("ao", 15),
    "roughness": ("roughness", 20), "rough": ("roughness", 20), "rgh": ("roughness", 20),
    "metallic": ("metallic", 22), "metalness": ("metallic", 22), "metal": ("metallic", 22),
    "met": ("metallic", 22),
    "normal": ("normal", 25), "nrm": ("normal", 25), "nor": ("normal", 25),
    "norm": ("normal", 25), "normalgl": ("normal", 25), "normaldx": ("normal", 25),
    "displacement": ("displacement", 30), "disp": ("displacement", 30),
    "height": ("displacement", 30), "bump": ("displacement", 30),
    "opacity": ("opacity", 35), "alpha": ("opacity", 35), "mask": ("opacity", 35),
    "transparency": ("opacity", 35),
}
_RES_TOKEN = re.compile(r"^\d+k$")  # resolution suffix like 2k / 4k / 8k

# Subtype tokens — textures that are decals or atlases are still "texture" kind
# (so the four top-level buckets stay Models/Textures/HDRIs/Materials) but get a
# subtype facet so they can be filtered on their own.
_SUBTYPE_TOKENS = {
    "decal": "decal", "decals": "decal",
    "atlas": "atlas", "atlases": "atlas", "atlasses": "atlas",
    "trimsheet": "atlas", "trim": "atlas",
}
# Bare pixel dimensions seen in texture names → a tidy resolution label.
_PIXELS_TO_RES = {
    "256": "256", "512": "512", "1024": "1k", "2048": "2k",
    "4096": "4k", "8192": "8k", "16384": "16k",
}


def texture_facets(folder, name_noext):
    """(subtype, resolution) for a texture/HDRI file, derived from its name and
    immediate parent folder. Both default to "" when nothing is recognised.

    - subtype: 'decal' or 'atlas' when a matching token appears in the file name
      or the containing folder name.
    - resolution: a tidy label ('2k', '4k', '512', …) lifted from a `<n>k` token
      or a bare power-of-two pixel dimension (2048 → '2k').
    """
    name_tokens = [t for t in re.split(r"[^a-z0-9]+", name_noext.lower()) if t]
    folder_tokens = [t for t in re.split(r"[^a-z0-9]+", os.path.basename(folder).lower()) if t]
    subtype = ""
    for t in name_tokens + folder_tokens:
        if t in _SUBTYPE_TOKENS:
            subtype = _SUBTYPE_TOKENS[t]
            break
    resolution = ""
    for t in name_tokens:  # prefer an explicit Nk token
        if _RES_TOKEN.match(t):
            resolution = t
            break
    if not resolution:
        for t in name_tokens:
            if t in _PIXELS_TO_RES:
                resolution = _PIXELS_TO_RES[t]
                break
    return subtype, resolution


def texture_set_info(folder, name_noext):
    """(set_key, map_role, map_order) for a texture file.

    Splits the base name into tokens, lifts out the *last* recognised map-role
    token (suffix convention) plus any resolution tokens, and keys the remaining
    base to its folder so a material's maps share one set_key.
    """
    tokens = [t for t in re.split(r"[^a-z0-9]+", name_noext.lower()) if t]
    role, order = "", 50
    for t in tokens:  # last match wins — role tokens are conventionally suffixes
        if t in MAP_ROLES:
            role, order = MAP_ROLES[t]
    base = [t for t in tokens
            if t not in MAP_ROLES and not _RES_TOKEN.match(t)]
    set_base = "_".join(base) if base else "_".join(tokens)
    set_key = os.path.normpath(folder).lower() + "|" + set_base
    return set_key, role, order

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
    # If the folder isn't reachable (drive unplugged, share down, moved, no
    # permission), DON'T walk it — otherwise mark_missing would flag every asset
    # as gone and they'd silently disappear. Signal "unavailable" with None so
    # callers can surface it instead.
    if not os.path.isdir(library_path):
        return None
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
            name_noext = os.path.splitext(fname)[0]
            kind = EXT_KIND[ext]
            meta = {
                "path": full,
                "name": name_noext,
                "ext": ext,
                "kind": kind,
                "size": st.st_size,
                "mtime": st.st_mtime,
            }
            # Group texture maps of the same material into one set.
            if kind == "texture":
                sk, role, order = texture_set_info(root, name_noext)
                meta["set_key"], meta["map_role"], meta["map_order"] = sk, role, order
            # Decal/atlas subtype + resolution facets for images (textures + HDRIs).
            if kind in ("texture", "hdri"):
                subtype, resolution = texture_facets(root, name_noext)
                meta["subtype"], meta["resolution"] = subtype, resolution
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
        n = scan_library(lib["path"], on_file=on_file)
        if n:  # None = unavailable folder; skip
            total += n
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
