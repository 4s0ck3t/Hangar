"""Thumbnail generation for Hangar, with a graceful fallback chain.

For each asset we try, in order:
  1. Images / textures  -> downscale directly with Pillow.
  2. Models             -> a sibling preview image (same stem, or a
                           preview/thumbnail file in the same folder).
  3. Models             -> an offscreen trimesh render (best effort).
When all of that fails the API reports no thumbnail and the UI draws a
format badge instead, so the grid never shows broken images.
"""

import hashlib
import logging
import os
import re
from pathlib import Path

from store import THUMB_DIR

log = logging.getLogger(__name__)

# OpenCV's OpenEXR codec is opt-in and must be enabled before cv2 is first
# imported (we import it lazily in _read_hdri_array, below).
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

THUMB_SIZE = (512, 512)
SIBLING_NAMES = ("preview", "thumbnail", "thumb", "render")
SIBLING_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# Bump this version when the thumbnail algorithm for a kind changes so that
# stale cached previews are automatically replaced on next access.
_THUMB_VERSIONS = {"hdri": "2"}


def _thumb_path(asset):
    ver = _THUMB_VERSIONS.get(asset["kind"], "1")
    key = f"v{ver}:{asset['path']}:{asset['mtime']}".encode("utf-8")
    digest = hashlib.sha1(key).hexdigest()[:16]
    return THUMB_DIR / f"{digest}.jpg"


def has_cached_thumb(asset):
    """True when a thumbnail JPEG is already on disk for this asset."""
    try:
        return _thumb_path(asset).exists()
    except Exception:
        return False


def get_or_make(asset):
    """Return a path to a cached JPEG thumbnail, or None if unavailable."""
    out = _thumb_path(asset)
    if out.exists():
        return out
    source_maker = {
        "texture": _from_image,
        "hdri": _from_image,
        "model": _from_model,
        "material": lambda a, o: None,
    }.get(asset["kind"], lambda a, o: None)
    try:
        ok = source_maker(asset, out)
        if ok:
            return out
        if asset["kind"] == "hdri":
            log.warning("HDR/EXR thumb failed for %s (backends: %s)",
                        asset.get("path", "?"), _hdri_backends())
    except Exception:
        log.exception("thumb generation error for %s", asset.get("path", "?"))
    return None


def save_thumbnail_bytes(asset, data):
    """Persist a provided image (raw bytes, e.g. a snapshot from the 3D viewer)
    as this asset's cached thumbnail. Best-effort; returns True on success."""
    import io
    from PIL import Image
    try:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            return _save_downscaled(img, _thumb_path(asset))
    except Exception:
        log.exception("save_thumbnail_bytes failed for %s", asset.get("path", "?"))
        return False


def _save_downscaled(img, out):
    from PIL import Image
    # Composite transparent images onto a dark background so JPEG is clean.
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (40, 40, 44))
        alpha = img.split()[-1]
        bg.paste(img.convert("RGB"), mask=alpha)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail(THUMB_SIZE, Image.LANCZOS)
    img.save(out, "JPEG", quality=86)
    return True


def _hdri_backends():
    """Return a list of available HDR/EXR decoding backends (for diagnostics)."""
    backs = []
    try:
        import cv2 as _cv2  # noqa: F401
        backs.append("cv2")
    except ImportError:
        pass
    try:
        import imageio.v3  # noqa: F401
        backs.append("imageio")
    except ImportError:
        pass
    return backs or ["none"]


def _from_image(asset, out):
    path = asset["path"]
    if asset["ext"] in (".hdr", ".exr"):
        return _from_hdri(path, out)
    from PIL import Image
    try:
        with Image.open(path) as img:
            return _save_downscaled(img, out)
    except Exception:
        return False


def _read_hdri_array(path):
    """Decode a .hdr / .exr to an RGB ndarray (float for HDR data, else uint8).

    OpenCV reads both formats natively (EXR via OPENCV_IO_ENABLE_OPENEXR, set at
    module load) and returns float32 BGR with the real dynamic range intact.
    imageio only handles .hdr and hands back already-tone-mapped uint8, so it's
    a last resort; PIL can read neither format on its own.
    """
    try:
        import cv2
        img = cv2.imread(path, cv2.IMREAD_ANYCOLOR | cv2.IMREAD_ANYDEPTH)
        if img is not None:
            if img.ndim == 3 and img.shape[2] >= 3:
                img = cv2.cvtColor(img[..., :3], cv2.COLOR_BGR2RGB)
            return img
    except Exception:
        pass
    try:
        import imageio.v3 as iio
        return iio.imread(path)
    except Exception:
        return None


def _from_hdri(path, out):
    """Tone-map a high-dynamic-range image down to a clean LDR JPEG preview."""
    arr = _read_hdri_array(path)
    if arr is None:
        return False
    import numpy as np
    from PIL import Image
    # imageio already returns LDR uint8 — tone-mapping it again blows it out to
    # white, so only the float (true-HDR) path gets the Reinhard curve.
    if not np.issubdtype(arr.dtype, np.integer):
        arr = np.nan_to_num(arr.astype("float32"), posinf=0.0, neginf=0.0)
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        arr = np.clip(arr[..., :3], 0.0, None)
        # Reinhard tone-map + gamma keeps bright skies and windows readable.
        tm = arr / (1.0 + arr)
        arr = np.clip(tm ** (1 / 2.2) * 255.0, 0, 255).astype("uint8")
    elif arr.ndim == 3:
        arr = arr[..., :3]
    return _save_downscaled(Image.fromarray(arr), out)


def _from_model(asset, out):
    sibling = _find_sibling_preview(asset["path"])
    if sibling:
        from PIL import Image
        with Image.open(sibling) as img:
            return _save_downscaled(img, out)
    if asset["ext"] == ".blend":
        # Most .blend files embed a preview image Blender wrote on save.
        img = extract_blend_thumbnail(asset["path"])
        if img is not None:
            return _save_downscaled(img, out)
        # No embedded preview — a real render is offered on demand from the
        # detail drawer (render_blend_preview), not run passively here.
        return False
    return _render_model(asset, out)


def _find_sibling_preview(model_path):
    p = Path(model_path)
    stem = p.stem.lower()
    folder = p.parent
    candidates = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.suffix.lower() not in SIBLING_EXTS:
            continue
        name = entry.stem.lower()
        # same-name image beside the model, or a generic preview file.
        if name == stem or name in SIBLING_NAMES or name.startswith(stem + "_"):
            candidates.append(entry)
    if candidates:
        # Prefer an exact stem match for accuracy.
        candidates.sort(key=lambda e: (e.stem.lower() != stem, len(e.stem)))
        return candidates[0]
    return None


def _extract_gltf_texture(path):
    """Pull the first base-color texture out of a .gltf/.glb file.

    Returns a PIL Image or None. Works without OpenGL — just reads the
    binary/JSON data that trimesh already parsed.
    """
    try:
        import trimesh
        from PIL import Image
        import io
        scene = trimesh.load(path, force="scene", process=False)
        # Iterate geometries looking for a PBR base-color texture.
        geoms = list(scene.geometry.values()) if hasattr(scene, "geometry") else []
        for geom in geoms:
            mat = getattr(geom, "visual", None)
            if mat is None:
                continue
            # TextureVisuals exposes .material.baseColorTexture (trimesh >=4)
            material = getattr(mat, "material", None)
            if material is None:
                continue
            tex = getattr(material, "baseColorTexture", None)
            if tex is None:
                # Older trimesh: try .image attribute
                tex = getattr(material, "image", None)
            if tex is not None:
                if isinstance(tex, Image.Image):
                    return tex
                # Some versions return raw bytes
                if isinstance(tex, (bytes, bytearray)):
                    return Image.open(io.BytesIO(tex))
        return None
    except Exception:
        return None


def _render_model(asset, out):
    """Thumbnail for a 3-D model.

    1. For GLTF/GLB: try to extract the embedded base-colour texture (no GL needed).
    2. Offscreen render via trimesh (needs GL — works on Windows with pyopengl).
    Falls back silently so the grid always shows at least a format badge.
    """
    from PIL import Image
    import io

    # GLTF/GLB — extract embedded texture first (fast, no OpenGL needed).
    if asset["ext"] in (".gltf", ".glb"):
        tex = _extract_gltf_texture(asset["path"])
        if tex is not None:
            return _save_downscaled(tex, out)

    # Generic offscreen render via trimesh (requires working GL context).
    # NB: Blender is deliberately NOT used here — passively rendering during a
    # grid scroll spawned a swarm of slow Blender processes. Formats trimesh
    # can't read (USD/USDA/USDC, FBX, Alembic…) are rendered on demand instead,
    # once, when the asset's detail drawer is opened (see app.render_preview).
    try:
        import trimesh
        scene = trimesh.load(asset["path"], force="scene")
        png = scene.save_image(resolution=THUMB_SIZE, visible=False)
        if png:
            with Image.open(io.BytesIO(png)) as img:
                if _save_downscaled(img, out):
                    return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Blender (.blend) preview support
# ---------------------------------------------------------------------------
import struct
import shutil
import platform
import glob
import subprocess
import tempfile

import store

_BLENDER_CACHE = {"path": None, "checked": False}


def extract_blend_thumbnail(path):
    """Return the preview image embedded in a .blend file, or None.

    Blender writes a thumbnail into a 'TEST' file-block when it saves a file
    (this is the same image its own File Browser shows). We parse the .blend
    header, skip 'REND' blocks, and read width/height + RGBA pixels from the
    'TEST' block. No Blender process required. Handles uncompressed, gzip, and
    zstd-compressed files (Blender 3.0+ "Compress", via _zstd_decompress).
    """
    from PIL import Image
    f = None
    try:
        f = open(path, "rb")
        head = f.read(12)
        if head[:2] == b"\x1f\x8b":              # gzip-compressed .blend
            import gzip
            f.close()
            f = gzip.open(path, "rb")
            head = f.read(12)
        elif head[:4] == b"\x28\xb5\x2f\xfd":    # zstd (Blender 3.0+ default)
            import io
            f.close()
            raw = _zstd_decompress(path)
            if raw is None:
                return None
            f = io.BytesIO(raw)
            head = f.read(12)
        if head[:7] != b"BLENDER":
            return None                          # not a .blend (unknown packing)
        is_64 = head[7:8] == b"-"                # '_' = 4-byte ptr, '-' = 8-byte
        endian = "<" if head[8:9] == b"v" else ">"
        bhead_size = 24 if is_64 else 20
        code = length = None
        while True:
            bhead = f.read(bhead_size)
            if len(bhead) < bhead_size:
                return None
            code = bhead[:4]
            length = struct.unpack(endian + "i", bhead[4:8])[0]
            if code == b"REND":                  # render-info block, skip it
                f.seek(length, 1)
                continue
            break
        if code != b"TEST":                      # no embedded thumbnail
            return None
        dims = f.read(8)
        if len(dims) < 8:
            return None
        w, h = struct.unpack(endian + "2i", dims)
        if w <= 0 or h <= 0 or w > 4096 or h > 4096 or (length - 8) != w * h * 4:
            return None
        raw = f.read(w * h * 4)
        if len(raw) < w * h * 4:
            return None
        img = Image.frombytes("RGBA", (w, h), raw)
        return img.transpose(Image.FLIP_TOP_BOTTOM)   # stored bottom-up
    except Exception:
        return None
    finally:
        try:
            if f:
                f.close()
        except Exception:
            pass


def _blend_field_ident(name):
    """The bare C identifier from a DNA field name (`*mat[4]` -> `mat`)."""
    m = re.search(r"[A-Za-z_]\w*", name)
    return m.group(0) if m else ""


def _blend_field_size(name, type_index, type_lengths, ptr_size):
    """Byte size of a DNA struct field, honouring pointers and array dims.

    Blender's makesdna emits naturally-aligned structs with no implicit
    padding, so summing field sizes in declaration order yields correct member
    offsets."""
    mult = 1
    for n in re.findall(r"\[(\d+)\]", name):
        mult *= int(n)
    if name.startswith("*") or "(*" in name:        # pointer / fn-pointer
        base = ptr_size
    else:
        base = type_lengths[type_index]
    return base * mult


def _parse_blend_sdna(dna, endian, ptr_size):
    """Parse a DNA1 block body into (structs, types, type_lengths).

    structs[i] = (type_index, [(field_type_index, field_name), ...]) in the
    same order as the file's STRC table (a block's sdna_index indexes this)."""
    p = 4                                            # skip 'SDNA'
    def u32():
        nonlocal p
        v = struct.unpack(endian + "i", dna[p:p + 4])[0]; p += 4; return v
    def align4():
        nonlocal p
        p = (p + 3) & ~3

    assert dna[p:p + 4] == b"NAME"; p += 4
    n = u32()
    names = []
    for _ in range(n):
        end = dna.index(b"\x00", p)
        names.append(dna[p:end].decode("ascii", "replace")); p = end + 1
    align4()

    assert dna[p:p + 4] == b"TYPE"; p += 4
    n = u32()
    types = []
    for _ in range(n):
        end = dna.index(b"\x00", p)
        types.append(dna[p:end].decode("ascii", "replace")); p = end + 1
    align4()

    assert dna[p:p + 4] == b"TLEN"; p += 4
    type_lengths = list(struct.unpack(endian + "%dh" % len(types),
                                      dna[p:p + 2 * len(types)]))
    p += 2 * len(types); align4()

    assert dna[p:p + 4] == b"STRC"; p += 4
    n = u32()
    structs = []
    for _ in range(n):
        stype, nfields = struct.unpack(endian + "2h", dna[p:p + 4]); p += 4
        fields = []
        for _ in range(nfields):
            ftype, fname = struct.unpack(endian + "2h", dna[p:p + 4]); p += 4
            fields.append((ftype, names[fname]))
        structs.append((stype, fields))
    return structs, types, type_lengths


def _zstd_decompress(path):
    """Fully decompress a zstd .blend (Blender 3.0+ default compression).

    Blender writes a sequence of standard zstd frames, so a streaming decoder
    reads it end-to-end. Prefers the `zstandard` package; falls back to the
    `zstd` CLI. Returns the raw bytes, or None when no decoder is available."""
    try:
        import zstandard
        with open(path, "rb") as fh:
            return zstandard.ZstdDecompressor().stream_reader(fh).read()
    except ImportError:
        pass
    except Exception:
        return None
    zstd_cli = shutil.which("zstd")
    if zstd_cli:
        try:
            out = subprocess.run([zstd_cli, "-dc", path], capture_output=True)
            if out.returncode == 0 and out.stdout[:7] == b"BLENDER":
                return out.stdout
        except Exception:
            pass
    return None


def count_blend_marked_assets(path):
    """Count datablocks flagged "Mark as Asset" inside a .blend file.

    Pure-Python: parses the .blend's DNA, finds the `asset_data` pointer offset
    within the embedded `ID` of every datablock, and tallies the non-null ones.
    No Blender process required. Returns an int, or None if the file can't be
    parsed (e.g. zstd-compressed, truncated, or pre-2.90 layout we can't read).
    """
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
        if magic[:2] == b"\x1f\x8b":                 # gzip-compressed .blend
            import gzip
            with gzip.open(path, "rb") as g:
                data = g.read()
        elif magic == b"\x28\xb5\x2f\xfd":           # zstd (Blender 3.0+ default)
            data = _zstd_decompress(path)
            if data is None:
                return None
        else:
            with open(path, "rb") as fh:
                data = fh.read()

        if data[:7] != b"BLENDER":                   # unknown packing / not a blend
            return None
        ptr_size = 8 if data[7:8] == b"-" else 4
        endian = "<" if data[8:9] == b"v" else ">"
        bhead = 16 + ptr_size

        # Pass 1: index every file-block; capture the DNA1 body.
        blocks, dna = [], None
        pos = 12
        n = len(data)
        while pos + bhead <= n:
            code = data[pos:pos + 4]
            length = struct.unpack(endian + "i", data[pos + 4:pos + 8])[0]
            sdna_index = struct.unpack(
                endian + "i", data[pos + 8 + ptr_size:pos + 12 + ptr_size])[0]
            body = pos + bhead
            if code[:4] == b"DNA1":
                dna = data[body:body + length]
            if code[:4] == b"ENDB":
                break
            blocks.append((sdna_index, body, length))
            pos = body + length
        if dna is None:
            return None

        structs, types, type_lengths = _parse_blend_sdna(dna, endian, ptr_size)

        # Offset of `asset_data` inside the `ID` struct (absent pre-2.90).
        id_idx = next((i for i, s in enumerate(structs)
                       if types[s[0]] == "ID"), None)
        if id_idx is None:
            return None
        asset_off = None
        off = 0
        for ftype, fname in structs[id_idx][1]:
            if _blend_field_ident(fname) == "asset_data":
                asset_off = off
                break
            off += _blend_field_size(fname, ftype, type_lengths, ptr_size)
        if asset_off is None:
            return 0                                 # file predates asset system

        # Datablocks embed `ID id;` as their first member (offset 0), so the
        # asset_data pointer sits at `asset_off` from the block start.
        ptr_fmt = endian + ("Q" if ptr_size == 8 else "I")
        count = 0
        for sdna_index, body, length in blocks:
            if sdna_index <= 0 or sdna_index >= len(structs):
                continue
            stype, fields = structs[sdna_index]
            if not fields or types[fields[0][0]] != "ID":
                continue                             # not a top-level datablock
            if asset_off + ptr_size > length:
                continue
            if struct.unpack(ptr_fmt, data[body + asset_off:
                                           body + asset_off + ptr_size])[0]:
                count += 1
        return count
    except Exception:
        return None


def find_blender():
    """Locate a Blender executable: env override, saved setting, PATH, then
    common install locations. Cached for the process."""
    if _BLENDER_CACHE["checked"]:
        return _BLENDER_CACHE["path"]
    found = None
    for cand in (os.environ.get("HANGAR_BLENDER"), store.get_setting("blender_path")):
        if cand and os.path.exists(cand):
            found = cand
            break
    if not found:
        found = shutil.which("blender")
    if not found:
        sysname = platform.system()
        pats = []
        if sysname == "Windows":
            pf = os.environ.get("ProgramFiles", r"C:\Program Files")
            pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
            local = os.environ.get("LOCALAPPDATA", "")
            pats = [
                os.path.join(pf, r"Blender Foundation\Blender*\blender.exe"),
                os.path.join(pf, r"Blender Foundation\Blender\blender.exe"),
                os.path.join(pf86, r"Steam\steamapps\common\Blender\blender.exe"),
                os.path.join(pf, r"Steam\steamapps\common\Blender\blender.exe"),
                # Microsoft Store / winget installs land under the user profile.
                os.path.join(local, r"Microsoft\WindowsApps\blender.exe"),
                os.path.join(local, r"Programs\Blender Foundation\Blender*\blender.exe"),
            ]
        elif sysname == "Darwin":
            pats = [
                "/Applications/Blender.app/Contents/MacOS/Blender",
                "/Applications/Blender*/Blender.app/Contents/MacOS/Blender",
            ]
        else:
            pats = ["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"]
        hits = []
        for p in pats:
            hits += glob.glob(p)
        if hits:
            hits.sort(reverse=True)              # prefer the newest version
            found = hits[0]
    _BLENDER_CACHE.update(path=found, checked=True)
    return found


def blender_available():
    return find_blender() is not None


def reset_blender_cache():
    _BLENDER_CACHE.update(path=None, checked=False)


def set_blender_path(path):
    """Persist a user-chosen Blender executable and re-check discovery.

    Returns (ok, resolved_path_or_None). An empty path clears the override and
    falls back to auto-discovery."""
    path = (path or "").strip().strip('"')
    if path and not os.path.exists(path):
        return False, None
    store.set_setting("blender_path", path)
    reset_blender_cache()
    return True, find_blender()


# Model formats Hangar can hand to Blender for an on-demand preview render.
# `.blend` is opened directly; everything else is imported into an empty scene.
BLENDER_RENDER_EXTS = {
    ".blend", ".fbx", ".obj", ".gltf", ".glb", ".stl", ".ply",
    ".dae", ".abc", ".usd", ".usda", ".usdc", ".usdz", ".x3d", ".3ds",
}


# Script handed to `blender -b -P <script> -- <input> <out.png>`.
# Opens (.blend) or imports (everything else) the model, frames it with a
# fresh camera and does a fast, lighting-free Workbench render. Import operator
# names changed across Blender versions, so each format tries new→old in turn.
_MODEL_RENDER_SCRIPT = r'''
import bpy, sys, os
from mathutils import Vector


def _try(*ops_with_kwargs):
    """Call the first import operator that exists and succeeds."""
    last = None
    for getter, kwargs in ops_with_kwargs:
        try:
            op = getter()
        except AttributeError:
            continue
        try:
            op(**kwargs)
            return True
        except Exception as e:
            last = e
    if last:
        raise last
    raise RuntimeError("no import operator available")


def load_model(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
        return
    # Start from a clean, empty scene for imported formats.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if ext == ".fbx":
        _try((lambda: bpy.ops.import_scene.fbx, {"filepath": path}))
    elif ext == ".obj":
        _try((lambda: bpy.ops.wm.obj_import, {"filepath": path}),
             (lambda: bpy.ops.import_scene.obj, {"filepath": path}))
    elif ext in (".gltf", ".glb"):
        _try((lambda: bpy.ops.import_scene.gltf, {"filepath": path}))
    elif ext == ".stl":
        _try((lambda: bpy.ops.wm.stl_import, {"filepath": path}),
             (lambda: bpy.ops.import_mesh.stl, {"filepath": path}))
    elif ext == ".ply":
        _try((lambda: bpy.ops.wm.ply_import, {"filepath": path}),
             (lambda: bpy.ops.import_mesh.ply, {"filepath": path}))
    elif ext == ".dae":
        _try((lambda: bpy.ops.wm.collada_import, {"filepath": path}))
    elif ext == ".abc":
        _try((lambda: bpy.ops.wm.alembic_import, {"filepath": path}))
    elif ext in (".usd", ".usda", ".usdc", ".usdz"):
        _try((lambda: bpy.ops.wm.usd_import, {"filepath": path}))
    elif ext == ".x3d":
        _try((lambda: bpy.ops.import_scene.x3d, {"filepath": path}))
    elif ext == ".3ds":
        _try((lambda: bpy.ops.import_scene.max3ds, {"filepath": path}),
             (lambda: bpy.ops.import_scene.autodesk_3ds, {"filepath": path}))
    else:
        raise RuntimeError("unsupported extension: " + ext)


def frame_and_render(out):
    scene = bpy.context.scene
    objs = [o for o in scene.objects
            if o.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}]

    if scene.camera is None:
        cam_data = bpy.data.cameras.new("HangarCam")
        cam = bpy.data.objects.new("HangarCam", cam_data)
        scene.collection.objects.link(cam)
        scene.camera = cam
        if objs:
            pts = []
            for o in objs:
                for c in o.bound_box:
                    pts.append(o.matrix_world @ Vector(c))
            mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
            mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
            center = (mn + mx) / 2.0
            radius = max((mx - mn).length / 2.0, 0.5)
            cam.data.lens = 50
            d = Vector((1.0, -1.2, 0.8)).normalized()
            cam.location = center + d * (radius * 3.2)
            look = center - cam.location
            cam.rotation_euler = look.to_track_quat('-Z', 'Y').to_euler()

    try:
        scene.render.engine = 'BLENDER_WORKBENCH'
    except Exception:
        pass
    r = scene.render
    r.resolution_x = 512
    r.resolution_y = 512
    r.resolution_percentage = 100
    r.use_file_extension = False
    r.image_settings.file_format = 'PNG'
    r.filepath = out
    bpy.ops.render.render(write_still=True)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    src, out = argv[0], argv[1]
    load_model(src)
    frame_and_render(out)

main()
'''


# Populated with a human-readable reason whenever the last render failed, so the
# UI/endpoint can tell the user *why* instead of a generic failure. Full Blender
# output is also written to ~/.hangar/last_render.log for deeper debugging.
LAST_RENDER_ERROR = None
RENDER_LOG = store.DATA_DIR / "last_render.log"


def _record_render_log(blender, model_path, proc, exc=None):
    """Persist the Blender invocation + its output to the render log; return a
    short error summary (or None if it looks like it succeeded)."""
    global LAST_RENDER_ERROR
    parts = [f"blender: {blender}", f"model: {model_path}"]
    if exc is not None:
        parts.append(f"exception: {exc!r}")
    if proc is not None:
        parts.append(f"returncode: {proc.returncode}")
        parts.append("--- stdout ---\n" + (proc.stdout or ""))
        parts.append("--- stderr ---\n" + (proc.stderr or ""))
    text = "\n".join(parts)
    try:
        RENDER_LOG.write_text(text, encoding="utf-8")
    except Exception:
        pass
    # Build a concise reason: the last non-empty line of Blender's output is
    # usually the actual error (e.g. "Error: unable to open … import failed").
    if exc is not None:
        LAST_RENDER_ERROR = f"Couldn't launch Blender: {exc}"
        return LAST_RENDER_ERROR
    tail = ""
    if proc is not None:
        for line in reversed(((proc.stderr or "") + (proc.stdout or "")).splitlines()):
            if line.strip():
                tail = line.strip()
                break
    LAST_RENDER_ERROR = tail or "Blender ran but produced no image."
    return LAST_RENDER_ERROR


def render_model(model_path, out_jpg):
    """Render any Blender-importable model (or open a .blend) in a background
    Blender process and save the result to the JPEG cache path. Best-effort;
    returns True on success. On failure, sets LAST_RENDER_ERROR and writes full
    Blender output to RENDER_LOG."""
    global LAST_RENDER_ERROR
    blender = find_blender()
    if not blender:
        LAST_RENDER_ERROR = ("Blender wasn't found. Install Blender, or set its "
                             "path in Hangar, then try again.")
        return False
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        script = os.path.join(td, "hangar_model_thumb.py")
        png = os.path.join(td, "thumb.png")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_MODEL_RENDER_SCRIPT)
        try:
            proc = subprocess.run(
                [blender, "-b", "-P", script, "--", model_path, png],
                timeout=180, capture_output=True, text=True,
            )
        except Exception as e:
            _record_render_log(blender, model_path, None, exc=e)
            return False
        if os.path.exists(png):
            try:
                with Image.open(png) as img:
                    ok = _save_downscaled(img, out_jpg)
                if ok:
                    LAST_RENDER_ERROR = None
                    return True
            except Exception as e:
                _record_render_log(blender, model_path, proc, exc=e)
                return False
        # No PNG (or save failed) — surface why.
        _record_render_log(blender, model_path, proc)
        return False


# Backwards-compatible alias — .blend rendering is just the general path.
def render_blend(blend_path, out_jpg):
    return render_model(blend_path, out_jpg)


def render_model_preview(asset):
    """On-demand Blender render for one model asset. Returns the cached path or
    None. Works for any extension in BLENDER_RENDER_EXTS."""
    out = _thumb_path(asset)
    if render_model(asset["path"], out):
        return out
    return None


# Older name kept so existing callers/imports don't break.
render_blend_preview = render_model_preview
