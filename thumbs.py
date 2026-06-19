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
import os
from pathlib import Path

from store import THUMB_DIR

THUMB_SIZE = (512, 512)
SIBLING_NAMES = ("preview", "thumbnail", "thumb", "render")
SIBLING_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _thumb_path(asset):
    key = f"{asset['path']}:{asset['mtime']}".encode("utf-8")
    digest = hashlib.sha1(key).hexdigest()[:16]
    return THUMB_DIR / f"{digest}.jpg"


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
        if source_maker(asset, out):
            return out
    except Exception:
        pass
    return None


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


def _from_image(asset, out):
    path = asset["path"]
    ext = asset["ext"]
    if ext in (".hdr", ".exr"):
        # Try imageio first (handles HDR tone-mapping cleanly).
        try:
            import imageio.v3 as iio
            import numpy as np
            from PIL import Image
            data = iio.imread(path)
            if data.ndim == 3:
                data = data[..., :3]
            data = data.astype("float32")
            # Reinhard tone-map + gamma to keep previews readable.
            tm = data / (1.0 + data)
            tm = np.clip(tm ** (1 / 2.2) * 255, 0, 255).astype("uint8")
            return _save_downscaled(Image.fromarray(tm), out)
        except ImportError:
            pass  # imageio not installed — try PIL directly
        except Exception:
            return False
        # Fallback: PIL can read some EXR/HDR files with its own plugins.
        try:
            from PIL import Image
            with Image.open(path) as img:
                return _save_downscaled(img, out)
        except Exception:
            return False
    from PIL import Image
    try:
        with Image.open(path) as img:
            return _save_downscaled(img, out)
    except Exception:
        return False


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
    try:
        import trimesh
        scene = trimesh.load(asset["path"], force="scene")
        png = scene.save_image(resolution=THUMB_SIZE, visible=False)
        if not png:
            return False
        with Image.open(io.BytesIO(png)) as img:
            return _save_downscaled(img, out)
    except Exception:
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
    'TEST' block. No Blender process required. Handles gzip-compressed files;
    zstd-compressed files (Blender 3.0+ "Compress") are skipped gracefully.
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
        if head[:7] != b"BLENDER":
            return None                          # not a .blend (or zstd-packed)
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
            pats = [
                r"C:\Program Files\Blender Foundation\Blender*\blender.exe",
                r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe",
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


# Script handed to `blender -b file.blend -P <script> -- <out.png>`.
# Frames the scene with a fresh camera (if none exists) and does a fast,
# lighting-free Workbench render.
_BLEND_RENDER_SCRIPT = r'''
import bpy, sys, math
from mathutils import Vector

def main():
    argv = sys.argv
    out = argv[argv.index("--") + 1:][0]
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

main()
'''


def render_blend(blend_path, out_jpg):
    """Render a .blend preview with a background Blender process and save it to
    the JPEG cache path. Best-effort; returns True on success."""
    blender = find_blender()
    if not blender:
        return False
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        script = os.path.join(td, "hangar_blend_thumb.py")
        png = os.path.join(td, "thumb.png")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_BLEND_RENDER_SCRIPT)
        try:
            subprocess.run(
                [blender, "-b", blend_path, "-P", script, "--", png],
                timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            return False
        if os.path.exists(png):
            try:
                with Image.open(png) as img:
                    return _save_downscaled(img, out_jpg)
            except Exception:
                return False
    return False


def render_blend_preview(asset):
    """On-demand render for one .blend asset. Returns the cached path or None."""
    out = _thumb_path(asset)
    if render_blend(asset["path"], out):
        return out
    return None
