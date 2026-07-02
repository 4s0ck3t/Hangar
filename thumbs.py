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
import json
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
_THUMB_VERSIONS = {"hdri": "2", "model": "7"}


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


# Human-readable labels for the preview source written alongside each thumbnail,
# so the drawer can tell the user exactly what they're looking at.
_PREVIEW_SOURCE_LABELS = {
    "embedded": "Embedded .blend thumbnail (128 px, from the file)",
    "render": "Hangar render",
    "sibling": "Sibling preview image next to the file",
    "image": "Source image",
}


def _thumb_meta_path(thumb_path):
    p = Path(thumb_path)
    return p.with_name(p.stem + ".meta.json")


def _write_thumb_source(thumb_path, source, engine=None):
    """Record how a cached thumbnail was produced (embedded vs Hangar render, and
    the engine for a render) in a sidecar JSON, so the UI can show it. Best-effort."""
    try:
        meta = {"source": source}
        if engine:
            meta["engine"] = engine
        with open(_thumb_meta_path(thumb_path), "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
    except Exception:
        pass


def preview_source(asset):
    """Describe the preview currently cached for an asset:
    {"source", "engine", "label", "has_thumb"}. Reads the sidecar written when the
    thumbnail was made; falls back to an unknown source if a thumb exists without
    one (e.g. baked by an older build)."""
    out = _thumb_path(asset)
    if not out.exists():
        return {"has_thumb": False, "source": None, "engine": None,
                "label": "No preview cached yet"}
    source = engine = None
    try:
        with open(_thumb_meta_path(out), "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        source = meta.get("source")
        engine = meta.get("engine")
    except Exception:
        pass
    label = _PREVIEW_SOURCE_LABELS.get(source, "Cached preview")
    if source == "render" and engine:
        label = f"Hangar render · {engine}"
    return {"has_thumb": True, "source": source, "engine": engine, "label": label}


def delete_cached_thumb(asset):
    """Remove this asset's cached thumbnail JPEG, if present.

    Returns True when a file was actually deleted. The next get_or_make() call
    re-bakes the thumbnail from source — for a .blend that re-reads the preview
    embedded in the file, which is the fix for a tile that cached blank/stale."""
    try:
        out = _thumb_path(asset)
        if out.exists():
            out.unlink()
            _thumb_meta_path(out).unlink(missing_ok=True)
            return True
    except Exception:
        log.exception("delete_cached_thumb failed for %s", asset.get("path", "?"))
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


def _save_downscaled(img, out, min_side=0):
    from PIL import Image
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    # Composite transparent images onto a dark background so JPEG is clean.
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (40, 40, 44))
        alpha = img.split()[-1]
        bg.paste(img.convert("RGB"), mask=alpha)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    # Tiny embedded previews (e.g. a .blend's 128px thumbnail) look blocky when
    # the browser stretches them across a HiDPI tile. Upscale once with LANCZOS
    # to min_side so the stored JPEG carries the tile at ~1:1 instead — softer
    # than a true render, but far cleaner than nearest-ish browser upscaling.
    if min_side and max(img.size) < min_side:
        scale = min_side / max(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)),
                         Image.LANCZOS)
    img.thumbnail(THUMB_SIZE, Image.LANCZOS)
    img.save(out, "JPEG", quality=90)
    return True


def _hdri_backends():
    """Return a list of available HDR/EXR decoding backends (for diagnostics)."""
    backs = ["radiance-hdr"]
    try:
        import OpenEXR as _OpenEXR  # noqa: F401
        import Imath as _Imath  # noqa: F401
        backs.append("openexr")
    except ImportError:
        pass
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
    if asset["ext"] == ".exr":
        return _from_openexr(path, out) or _from_hdri(path, out)
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
        pass
    return None


def _from_openexr(path, out):
    """Decode OpenEXR files via the lightweight OpenEXR bindings.

    Most EXRs in asset packs are texture maps rather than lat-long worlds. We
    still preview them by reading RGB/Y as float, sampling down to thumbnail
    size, and using the same gentle gamma curve as ordinary image maps.
    """
    try:
        import OpenEXR
        import Imath
        import numpy as np
        from PIL import Image

        exr = OpenEXR.InputFile(str(path))
        header = exr.header()
        dw = header["dataWindow"]
        width = dw.max.x - dw.min.x + 1
        height = dw.max.y - dw.min.y + 1
        channels = header.get("channels", {})
        names = set(channels.keys())
        pixel_type = Imath.PixelType(Imath.PixelType.FLOAT)

        def read_channel(name):
            raw = exr.channel(name, pixel_type)
            return np.frombuffer(raw, dtype=np.float32).reshape(height, width)

        if {"R", "G", "B"}.issubset(names):
            planes = [read_channel("R"), read_channel("G"), read_channel("B")]
        elif "Y" in names:
            y = read_channel("Y")
            planes = [y, y, y]
        elif names:
            first = read_channel(sorted(names)[0])
            planes = [first, first, first]
        else:
            return False

        step = max(1, int(np.ceil(max(width, height) / THUMB_SIZE[0])))
        arr = np.stack([p[::step, ::step] for p in planes], axis=-1)
        arr = np.nan_to_num(arr, posinf=1.0, neginf=0.0)
        arr = np.clip(arr, 0.0, None)
        if float(arr.max(initial=0.0)) > 1.0:
            arr = arr / (1.0 + arr)
        arr = np.clip(arr ** (1 / 2.2) * 255.0, 0, 255).astype("uint8")
        return _save_downscaled(Image.fromarray(arr), out)
    except Exception:
        return False


def _read_radiance_header(fh):
    def _readline(fh):
        line = fh.readline()
        if not line:
            return None
        return line.decode("ascii", "ignore").strip()

    resolution = ""
    while True:
        line = _readline(fh)
        if line is None:
            break
        if line.startswith(("-Y ", "+Y ")):
            resolution = line
            break
    if not resolution:
        return None
    parts = resolution.split()
    if len(parts) != 4 or parts[0] not in ("-Y", "+Y") or parts[2] not in ("+X", "-X"):
        return None
    height = int(parts[1])
    width = int(parts[3])
    if width <= 0 or height <= 0:
        return None
    return width, height, parts


def _read_radiance_rgbe(path):
    """Read Radiance RGBE pixels as bytes, without numpy/cv2/imageio."""
    with open(path, "rb") as fh:
        header = _read_radiance_header(fh)
        if not header:
            return None
        width, height, parts = header

        rgbe = bytearray(width * height * 4)
        for y in range(height):
            head = fh.read(4)
            if len(head) < 4:
                return None
            if width >= 8 and width <= 0x7fff and head[0] == 2 and head[1] == 2:
                scan_width = (head[2] << 8) | head[3]
                if scan_width != width:
                    return None
                scan = [bytearray(width), bytearray(width), bytearray(width), bytearray(width)]
                for channel in range(4):
                    x = 0
                    while x < width:
                        b = fh.read(1)
                        if not b:
                            return None
                        code = b[0]
                        if code > 128:
                            count = code - 128
                            val = fh.read(1)
                            if not val or x + count > width:
                                return None
                            scan[channel][x:x + count] = bytes([val[0]]) * count
                            x += count
                        else:
                            count = code
                            vals = fh.read(count)
                            if len(vals) != count or x + count > width:
                                return None
                            scan[channel][x:x + count] = vals
                            x += count
                row = y * width * 4
                for x in range(width):
                    i = row + x * 4
                    rgbe[i] = scan[0][x]
                    rgbe[i + 1] = scan[1][x]
                    rgbe[i + 2] = scan[2][x]
                    rgbe[i + 3] = scan[3][x]
            else:
                rest = fh.read(width * 4 - 4)
                if len(rest) != width * 4 - 4:
                    return None
                row = y * width * 4
                rgbe[row:row + width * 4] = head + rest
    return width, height, parts, rgbe


def _from_radiance_hdr(path, out):
    """Tone-map a Radiance .hdr file using only the standard library + Pillow."""
    import math
    from PIL import Image

    decoded = _read_radiance_rgbe(path)
    if not decoded:
        return False
    width, height, parts, rgbe = decoded
    step = max(1, math.ceil(max(width, height) / THUMB_SIZE[0]))
    out_w = math.ceil(width / step)
    out_h = math.ceil(height / step)
    rgb = bytearray(out_w * out_h * 3)
    gamma = 1 / 2.2
    tone_lut = {}

    def tone(e, v):
        table = tone_lut.get(e)
        if table is None:
            scale = math.ldexp(1.0, e - (128 + 8))
            table = bytearray(256)
            for raw in range(256):
                linear = max(0.0, raw * scale)
                mapped = linear / (1.0 + linear)
                table[raw] = max(0, min(255, round((mapped ** gamma) * 255)))
            tone_lut[e] = table
        return table[v]

    for y in range(out_h):
        src_y = min(height - 1, y * step)
        sy = height - 1 - src_y if parts[0] == "+Y" else src_y
        for x in range(out_w):
            src_x = min(width - 1, x * step)
            sx = width - 1 - src_x if parts[2] == "-X" else src_x
            si = (sy * width + sx) * 4
            di = (y * out_w + x) * 3
            e = rgbe[si + 3]
            if not e:
                continue
            rgb[di] = tone(e, rgbe[si])
            rgb[di + 1] = tone(e, rgbe[si + 1])
            rgb[di + 2] = tone(e, rgbe[si + 2])
    return _save_downscaled(Image.frombytes("RGB", (out_w, out_h), bytes(rgb)), out)


def _from_hdri(path, out):
    """Tone-map a high-dynamic-range image down to a clean LDR JPEG preview."""
    arr = _read_hdri_array(path)
    if arr is None:
        if str(path).lower().endswith(".hdr"):
            return _from_radiance_hdr(path, out)
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
            if _save_downscaled(img, out):
                _write_thumb_source(out, "sibling")
                return True
            return False
    if asset["ext"] == ".blend":
        # Most .blend files embed a viewport preview in the TEST block (what
        # Blender's file browser shows). Prefer that for speed; only fall
        # through to a full Blender background render when none is present.
        img = extract_blend_thumbnail(asset["path"])
        if img is not None:
            img = _strip_light_bg(img)
            if _save_downscaled(img, out, min_side=THUMB_SIZE[0]):
                _write_thumb_source(out, "embedded")
                return True
        return False
    return _render_model(asset, out)


# Texture-map name hints, so a material/surface set previews as its COLOUR map
# rather than (whichever has the shortest filename) its normal/roughness/AO map.
_COLOR_MAP_HINTS = ("basecolor", "base_color", "albedo", "diffuse", "color",
                    "_col", "_alb", "_diff")
_OTHER_MAP_HINTS = ("normal", "_nrm", "_nor", "roughness", "_rough", "_rgh",
                    "metal", "_ao", "occlusion", "displace", "height", "_disp",
                    "gloss", "specular", "_spec", "opacity", "_orm", "bump")


def _map_rank(name):
    """0 = colour/albedo map (best preview), 2 = an obvious non-colour map, 1 = other."""
    if any(h in name for h in _COLOR_MAP_HINTS):
        return 0
    if any(h in name for h in _OTHER_MAP_HINTS):
        return 2
    return 1


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
        # Prefer an exact stem match, then a colour/albedo map (the right preview
        # for a material/surface set), then the shortest name.
        candidates.sort(key=lambda e: (e.stem.lower() != stem,
                                       _map_rank(e.stem.lower()),
                                       len(e.stem)))
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


# Formats trimesh reads reliably. Everything else in BLENDER_RENDER_EXTS
# (USD/USDA/USDC/USDZ, FBX, Alembic, DAE, 3DS, X3D) must go to Blender — for
# those, trimesh.load(..., force="scene") returns an EMPTY scene instead of
# raising, and save_image then writes a blank thumbnail that silently masks the
# real render. So we never hand those extensions to trimesh.
_TRIMESH_EXTS = {".obj", ".stl", ".ply", ".gltf", ".glb"}


def _render_model(asset, out):
    """Thumbnail for a 3-D model.

    1. For GLTF/GLB: try to extract the embedded base-colour texture (no GL needed).
    2. For other trimesh-readable formats: offscreen render via trimesh (needs GL).
    Formats only Blender can read return False here so the caller falls back to a
    Blender render — never a blank trimesh image.
    """
    from PIL import Image
    import io

    # GLTF/GLB — extract embedded texture first (fast, no OpenGL needed).
    if asset["ext"] in (".gltf", ".glb"):
        tex = _extract_gltf_texture(asset["path"])
        if tex is not None:
            return _save_downscaled(tex, out)

    if asset["ext"] not in _TRIMESH_EXTS:
        return False        # USD/FBX/Alembic/… — let the caller use Blender

    # Generic offscreen render via trimesh (requires working GL context). An
    # empty result is treated as failure so a blank image never gets cached.
    try:
        import trimesh
        scene = trimesh.load(asset["path"], force="scene")
        if scene is None or (hasattr(scene, "is_empty") and scene.is_empty):
            return False
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

# Per-render Blender timeout (seconds). Heavy authored .blend scenes (lots of
# geometry / high-res textures) can take a while, especially on weaker GPUs, and
# a too-short timeout kills the render so the tile falls back to the blurry 128px
# embedded preview. Overridable via HANGAR_RENDER_TIMEOUT.
RENDER_TIMEOUT = 1800
try:
    RENDER_TIMEOUT = max(30, int(os.environ.get("HANGAR_RENDER_TIMEOUT", RENDER_TIMEOUT)))
except ValueError:
    pass


def _no_window():
    """subprocess kwargs that suppress the console window a child process would
    otherwise pop on Windows — without it, every background Blender render flashes
    a cmd window that steals keyboard focus from whatever the user is typing in."""
    if platform.system() != "Windows":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW   # honour wShowWindow
    si.wShowWindow = 0                              # SW_HIDE
    # CREATE_NO_WINDOW (0x08000000): the child gets no console at all.
    return {"startupinfo": si, "creationflags": 0x08000000}


def system_gpus():
    """Best-effort list of the machine's GPU name(s), without launching Blender.
    Used for the diagnostics panel and for a render-farm worker to report what
    hardware it has. Returns [] if it can't tell."""
    sysname = platform.system()
    try:
        if sysname == "Windows":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_VideoController | "
                 "ForEach-Object { $_.Name }"],
                capture_output=True, text=True, timeout=12, **_no_window()).stdout
        elif sysname == "Darwin":
            raw = subprocess.run(["system_profiler", "SPDisplaysDataType"],
                                 capture_output=True, text=True, timeout=12).stdout
            out = "\n".join(l.split(":", 1)[1] for l in raw.splitlines()
                            if "Chipset Model:" in l)
        else:  # Linux
            raw = subprocess.run(["lspci"], capture_output=True, text=True,
                                 timeout=12).stdout
            out = "\n".join(l.split(": ", 1)[-1] for l in raw.splitlines()
                            if "VGA compatible" in l or "3D controller" in l)
        return [l.strip() for l in out.splitlines() if l.strip()]
    except Exception:
        return []


def _strip_light_bg(img, lum_thresh=190, tol=40):
    """BFS flood-fill from the 4 corners; makes connected near-white/light-grey
    pixels transparent so they composite onto Hangar's dark tile background.
    Returns the image unchanged if the corners are already dark."""
    import numpy as np
    from PIL import Image
    from collections import deque
    arr = np.array(img.convert("RGBA"), dtype=np.int16)
    h, w = arr.shape[:2]
    corners = arr[[0, 0, h - 1, h - 1], [0, w - 1, 0, w - 1], :3]
    bg = corners.mean(axis=0)                    # estimated background RGB
    if float(bg.mean()) < lum_thresh:
        return img                               # already dark — leave it alone
    visited = np.zeros((h, w), dtype=bool)
    mask = np.zeros((h, w), dtype=bool)
    q = deque([(0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)])
    while q:
        y, x = q.popleft()
        if visited[y, x]:
            continue
        visited[y, x] = True
        diff = float(np.abs(arr[y, x, :3] - bg).mean())
        if diff > tol:
            continue
        mask[y, x] = True
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx]:
                q.append((ny, nx))
    arr[mask, 3] = 0
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def extract_blend_thumbnail(path):
    """Return the preview image embedded in a .blend file, or None.

    Blender writes a thumbnail into a 'TEST' file-block when it saves a file
    (this is the same image its own File Browser shows). We parse the .blend
    header, skip 'REND' blocks, and read width/height + RGBA pixels from the
    'TEST' block. No Blender process required. Handles uncompressed, gzip, and
    zstd-compressed files (Blender 3.0+ "Compress", via _zstd_decompress).
    """
    from PIL import Image
    path = _fs(path)
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
            out = subprocess.run([zstd_cli, "-dc", path], capture_output=True,
                                 **_no_window())
            if out.returncode == 0 and out.stdout[:7] == b"BLENDER":
                return out.stdout
        except Exception:
            pass
    return None


def _fs(path):
    """Long-path-safe filesystem path. On Windows, open()/gzip/exists choke on
    paths over 260 chars unless prefixed with the extended-length marker — and a
    UNC share (\\\\server\\share\\...) needs the \\\\?\\UNC\\ form, NOT plain
    \\\\?\\. Deeply-nested packs on a network share hit both, so a file that's
    actually present reads as missing without this."""
    if os.name != "nt" or not path:
        return path
    try:
        p = os.path.abspath(path)
    except OSError:
        return path
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):                      # UNC: \\server\share\...
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def _blend_decompress(path):
    """Return the fully-decompressed bytes of a .blend (handles raw, gzip, and
    zstd), or None if it isn't a parseable .blend."""
    path = _fs(path)
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic[:2] == b"\x1f\x8b":                      # gzip-compressed .blend
        import gzip
        with gzip.open(path, "rb") as g:
            data = g.read()
    elif magic == b"\x28\xb5\x2f\xfd":                # zstd (Blender 3.0+ default)
        data = _zstd_decompress(path)
    else:
        with open(path, "rb") as fh:
            data = fh.read()
    if not data or data[:7] != b"BLENDER":            # unknown packing / not a blend
        return None
    return data


def _blend_index(data):
    """Parse a decompressed .blend into (blocks, structs, types, type_lengths,
    endian, ptr_size, by_addr), where blocks = [(sdna_index, body_offset,
    length), ...] and by_addr maps a block's original memory address → (body,
    length) so char*/ListBase pointers can be followed to the block they name.
    Returns None if the DNA1 block can't be found or parsed."""
    ptr_size = 8 if data[7:8] == b"-" else 4
    endian = "<" if data[8:9] == b"v" else ">"
    ptr_fmt = endian + ("Q" if ptr_size == 8 else "I")
    bhead = 16 + ptr_size
    blocks, dna, by_addr = [], None, {}
    pos, n = 12, len(data)
    while pos + bhead <= n:
        code = data[pos:pos + 4]
        length = struct.unpack(endian + "i", data[pos + 4:pos + 8])[0]
        old_addr = struct.unpack(ptr_fmt, data[pos + 8:pos + 8 + ptr_size])[0]
        sdna_index = struct.unpack(
            endian + "i", data[pos + 8 + ptr_size:pos + 12 + ptr_size])[0]
        body = pos + bhead
        if code[:4] == b"DNA1":
            dna = data[body:body + length]
        if code[:4] == b"ENDB":
            break
        blocks.append((sdna_index, body, length))
        if old_addr:
            by_addr[old_addr] = (body, length)
        pos = body + length
    if dna is None:
        return None
    structs, types, type_lengths = _parse_blend_sdna(dna, endian, ptr_size)
    return blocks, structs, types, type_lengths, endian, ptr_size, by_addr


def _struct_index(structs, types, name):
    """Index of the struct whose type is `name` (e.g. 'ID', 'Image'), or None."""
    return next((i for i, s in enumerate(structs) if types[s[0]] == name), None)


def _field_offset(structs, type_lengths, ptr_size, struct_idx, *idents):
    """Byte offset of the first field in structs[struct_idx] whose bare C
    identifier matches any of `idents`, or None. Relies on makesdna's
    no-implicit-padding layout (sum field sizes in declaration order)."""
    off = 0
    for ftype, fname in structs[struct_idx][1]:
        if _blend_field_ident(fname) in idents:
            return off
        off += _blend_field_size(fname, ftype, type_lengths, ptr_size)
    return None


def _read_cstr(data, start, limit):
    """Decode a NUL-terminated byte string at `start`, scanning at most `limit`
    bytes, as UTF-8 (Blender's on-disk encoding)."""
    end = data.find(b"\x00", start, start + limit)
    if end < 0:
        end = start + limit
    return data[start:end].decode("utf-8", "replace")


def _follow_str(data, ptr, by_addr):
    """Read the NUL-terminated string a char* points at (its own data block)."""
    if not ptr or ptr not in by_addr:
        return ""
    body, length = by_addr[ptr]
    return _read_cstr(data, body, length).strip()


def _read_asset_meta(data, meta_ptr, by_addr, endian, ptr_size, meta_offs, tag_name_off):
    """Read one marked datablock's AssetMetaData — author, description, copyright,
    license, tags, and catalog name — by following pointers out of the metadata
    block. Blender stores each text field as a char* to its own block and the
    tags as a ListBase of AssetTag; catalog_simple_name is inline. Best-effort:
    returns whatever it can, empty strings/list for anything absent."""
    out = {"author": "", "description": "", "copyright": "", "license": "",
           "tags": [], "catalog": ""}
    if not meta_ptr or meta_ptr not in by_addr:
        return out
    body, length = by_addr[meta_ptr]
    ptr_fmt = endian + ("Q" if ptr_size == 8 else "I")

    def follow_field(fld):
        off = meta_offs.get(fld)
        if off is None or off + ptr_size > length:
            return ""
        p = struct.unpack(ptr_fmt, data[body + off:body + off + ptr_size])[0]
        return _follow_str(data, p, by_addr)

    for fld in ("author", "description", "copyright", "license"):
        out[fld] = follow_field(fld)

    coff = meta_offs.get("catalog_simple_name")
    if coff is not None and coff < length:
        out["catalog"] = _read_cstr(data, body + coff, 64).strip()

    # tags: ListBase {void *first, *last} — walk the AssetTag.next chain.
    toff = meta_offs.get("tags")
    if toff is not None and tag_name_off is not None and toff + ptr_size <= length:
        cur = struct.unpack(ptr_fmt, data[body + toff:body + toff + ptr_size])[0]
        seen = set()
        while cur and cur in by_addr and cur not in seen and len(out["tags"]) < 64:
            seen.add(cur)
            tbody, tlen = by_addr[cur]
            nm = _read_cstr(data, tbody + tag_name_off, 64).strip()
            if nm:
                out["tags"].append(nm)
            cur = struct.unpack(ptr_fmt, data[tbody:tbody + ptr_size])[0]  # AssetTag.next
    return out


# ID-name 2-char type prefixes → friendly kind, for "Mark as Asset" datablocks.
_ID_CODE_KIND = {
    "OB": "Object", "GR": "Collection", "MA": "Material", "ME": "Mesh",
    "WO": "World", "NT": "Node group", "AC": "Action", "BR": "Brush",
    "IM": "Image", "TE": "Texture", "SC": "Scene", "GD": "Grease pencil",
}


# Bump when _inspect_blend_uncached's result schema changes, so on-disk caches
# from older builds (e.g. ones predating missing_textures) are recomputed even
# when the .blend file itself is unchanged.
_INSPECT_CACHE_VERSION = 6


def inspect_blend(path):
    """Pure-Python inspection of a .blend, cached on disk by (mtime, size).

    The parse decompresses and walks the whole file, which is slow for big
    asset packs — so the result is cached next to the preview manifest and
    only recomputed when the file changes. Returns the same dict as
    :func:`_inspect_blend_uncached`, or None if the file can't be parsed."""
    try:
        st = os.stat(_fs(path))
    except OSError:
        return _inspect_blend_uncached(path)
    cache_file = _blend_asset_dir(path) / "inspect.json"
    try:
        with open(cache_file, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        # The version guard matters as much as mtime/size: when the parse gains a
        # new field (e.g. missing_textures), an unchanged file would otherwise
        # keep serving the old dict that lacks it. Bumping _INSPECT_CACHE_VERSION
        # forces a recompute so new fields actually surface.
        if (cached.get("version") == _INSPECT_CACHE_VERSION
                and cached.get("mtime") == st.st_mtime
                and cached.get("size") == st.st_size):
            return cached["result"]                  # fresh objects from JSON
    except Exception:
        pass                                         # no/stale/corrupt cache
    result = _inspect_blend_uncached(path)
    if result is not None:
        try:
            _blend_asset_dir(path).mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as fh:
                json.dump({"version": _INSPECT_CACHE_VERSION,
                           "mtime": st.st_mtime, "size": st.st_size,
                           "result": result}, fh)
        except Exception:
            log.exception("inspect_blend cache write failed for %s", path)
    return result


def _inspect_blend_uncached(path):
    """Pure-Python inspection of a .blend. Returns a dict with:

        count            int  — datablocks flagged "Mark as Asset"
        assets           [{"name", "kind"}]  — those datablocks, named
        missing_textures [{"name", "path"}]  — Image datablocks whose file is
                               referenced but absent on disk (and not packed)

    No Blender process required. Returns None if the file can't be parsed
    (truncated, unknown packing, or a pre-2.90 DNA layout we can't read)."""
    try:
        data = _blend_decompress(path)
        if data is None:
            return None
        idx = _blend_index(data)
        if idx is None:
            return None
        blocks, structs, types, type_lengths, endian, ptr_size, by_addr = idx
        ptr_fmt = endian + ("Q" if ptr_size == 8 else "I")

        id_idx = _struct_index(structs, types, "ID")
        if id_idx is None:
            return None
        asset_off = _field_offset(structs, type_lengths, ptr_size, id_idx, "asset_data")
        name_off = _field_offset(structs, type_lengths, ptr_size, id_idx, "name")
        if name_off is None:
            return None

        # AssetMetaData / AssetTag field offsets, for reading per-asset metadata
        # (author, description, license, tags, catalog) — resolved by name so the
        # layout stays version-robust.
        meta_idx = _struct_index(structs, types, "AssetMetaData")
        meta_offs = {}
        if meta_idx is not None:
            for fld in ("author", "description", "copyright", "license",
                        "tags", "catalog_simple_name"):
                meta_offs[fld] = _field_offset(structs, type_lengths, ptr_size,
                                               meta_idx, fld)
        tag_idx = _struct_index(structs, types, "AssetTag")
        tag_name_off = (_field_offset(structs, type_lengths, ptr_size, tag_idx, "name")
                        if tag_idx is not None else None)

        # Image-struct field offsets, for missing-texture detection.
        img_idx = _struct_index(structs, types, "Image")
        img_path_off = img_pack_off = img_packs_off = None
        if img_idx is not None:
            img_path_off = _field_offset(structs, type_lengths, ptr_size,
                                         img_idx, "filepath", "name")
            img_pack_off = _field_offset(structs, type_lengths, ptr_size,
                                         img_idx, "packedfile")
            # Modern Blender packs into a `packedfiles` ListBase; the deprecated
            # `packedfile` pointer can be NULL even for a packed image.
            img_packs_off = _field_offset(structs, type_lengths, ptr_size,
                                          img_idx, "packedfiles")

        base = os.path.dirname(path)
        count = 0
        assets = []
        missing = []
        packed_tex = 0        # Image datablocks with pixels embedded in the .blend
        external_tex = 0      # Image datablocks referencing an external file
        seen_paths = set()
        for sdna_index, body, length in blocks:
            if sdna_index <= 0 or sdna_index >= len(structs):
                continue
            stype, fields = structs[sdna_index]
            if not fields or types[fields[0][0]] != "ID":
                continue                             # not a top-level datablock

            # --- "Mark as Asset" datablocks (named) ---
            if asset_off is not None and asset_off + ptr_size <= length:
                meta_ptr = struct.unpack(
                    ptr_fmt, data[body + asset_off:body + asset_off + ptr_size])[0]
                if meta_ptr:
                    count += 1
                    raw = _read_cstr(data, body + name_off, 66)  # MAX_ID_NAME
                    code, nm = raw[:2], raw[2:]
                    entry = {"name": nm or raw,
                             "kind": _ID_CODE_KIND.get(code, code or "?")}
                    try:
                        entry.update(_read_asset_meta(
                            data, meta_ptr, by_addr, endian, ptr_size,
                            meta_offs, tag_name_off))
                    except Exception:
                        log.exception("asset-metadata read failed in %s", path)
                    assets.append(entry)

            # --- missing textures (Image datablocks) ---
            # Match on sdna_index (the block's struct-table index), which is the
            # same namespace _struct_index() returns. `stype` is the struct's
            # *type* index — a different table — so comparing it to img_idx never
            # matched and no missing texture was ever reported.
            #
            # Isolated in its own try/except: a single odd image path (e.g. one
            # that makes os.path.exists raise) must not abort the whole parse and
            # take the marked-asset list down with it.
            if (img_idx is not None and sdna_index == img_idx
                    and img_path_off is not None
                    and img_path_off + 1024 <= length):
                try:
                    # Packed images carry their pixels inside the .blend — they're
                    # self-contained, never missing. Check BOTH the deprecated
                    # `packedfile` pointer and the modern `packedfiles` ListBase
                    # (its `first` pointer), since either can hold the packed data.
                    is_packed = False
                    if img_pack_off is not None and img_pack_off + ptr_size <= length:
                        if struct.unpack(ptr_fmt, data[body + img_pack_off:
                                                       body + img_pack_off + ptr_size])[0]:
                            is_packed = True
                    if (not is_packed and img_packs_off is not None
                            and img_packs_off + ptr_size <= length):
                        if struct.unpack(ptr_fmt, data[body + img_packs_off:
                                                       body + img_packs_off + ptr_size])[0]:
                            is_packed = True
                    if is_packed:
                        packed_tex += 1
                        continue
                    fp = _read_cstr(data, body + img_path_off, 1024).strip()
                    # Skip empty (generated/viewer images) and UDIM/sequence tokens
                    # whose on-disk name we can't resolve to one concrete file.
                    if not fp or "<" in fp:
                        continue
                    external_tex += 1        # a linked (external-file) texture
                    resolved = fp
                    if resolved.startswith("//"):    # Blender = relative to .blend
                        resolved = os.path.join(base, resolved[2:].lstrip("/\\"))
                    resolved = os.path.normpath(resolved.replace("\\", os.sep))
                    if resolved in seen_paths:
                        continue
                    seen_paths.add(resolved)
                    if not os.path.exists(resolved):
                        nm = _read_cstr(data, body + name_off, 66)[2:]
                        missing.append({"name": nm or os.path.basename(fp), "path": fp})
                except Exception:
                    log.exception("missing-texture check failed in %s", path)

        if asset_off is None:
            count = 0                                # file predates asset system
        return {"count": count, "assets": assets, "missing_textures": missing,
                "packed_textures": packed_tex, "external_textures": external_tex}
    except Exception:
        log.exception("inspect_blend failed for %s", path)
        return None


def count_blend_marked_assets(path):
    """Count datablocks flagged "Mark as Asset" inside a .blend. Returns an int,
    or None if the file can't be parsed. Thin wrapper over inspect_blend()."""
    info = inspect_blend(path)
    return None if info is None else info["count"]


# ---- "Mark as Asset" writing + per-asset preview extraction ----------------
# Marking can only be done through Blender (bpy), and it modifies the source
# .blend. We also export each marked datablock's preview thumbnail to a cache
# dir (keyed by the file's path+mtime) so the drawer can show a gallery without
# re-opening Blender on every view.
BLEND_ASSET_DIR = store.DATA_DIR / "blend_assets"


def _blend_asset_dir(path):
    """Cache directory for one .blend's exported asset previews + manifest.
    Keyed by path only — mtime is intentionally excluded so the manifest
    survives the save that Blender performs when marking assets."""
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]
    return BLEND_ASSET_DIR / digest


# Run with: blender -b <src.blend> -P <script> -- <outdir> <action> <target>
#   action: "mark"    = mark top-level objects/collections, then export previews
#           "unmark"  = clear asset marks from objects/collections/materials/all
#           "extract" = export previews for already-marked datablocks only
#   target: "objects" | "collections" | "materials" | "all"
# Writes <outdir>/manifest.json = [{"name","kind","file"}] and one PNG per asset.
_MARK_ASSETS_SCRIPT = r'''
import bpy, sys, os, json, re

argv = sys.argv[sys.argv.index("--") + 1:]
outdir, action, target = argv[0], argv[1], (argv[2] if len(argv) > 2 else "objects")
os.makedirs(outdir, exist_ok=True)

KIND = {"objects": "Object", "collections": "Collection", "materials": "Material"}


def gen_preview(db):
    """Force a synchronous asset preview in background mode. Operator spelling
    changed across versions, so try the known forms."""
    try:
        with bpy.context.temp_override(id=db):
            bpy.ops.ed.lib_id_generate_preview()
        return True
    except Exception:
        pass
    try:
        bpy.ops.ed.lib_id_generate_preview({"id": db})
        return True
    except Exception:
        return False


def export_preview(db, path):
    """Write a datablock's preview image to a PNG via a scratch bpy image
    (no Pillow in Blender's Python). Returns True on a non-empty preview."""
    pv = getattr(db, "preview", None)
    if pv is None:
        return False
    w, h = tuple(pv.image_size)
    if w <= 0 or h <= 0:
        return False
    try:
        px = list(pv.image_pixels_float)
    except Exception:
        return False
    if not px or not any(px[3::4]):           # fully transparent => no real preview
        return False
    img = bpy.data.images.new("_hangar_pv", w, h, alpha=True)
    try:
        img.pixels = px
        img.filepath_raw = path
        img.file_format = 'PNG'
        img.save()
        return True
    finally:
        bpy.data.images.remove(img)


def main():
    marked = 0
    unmarked = 0
    if action == "mark":
        if target == "collections":
            targets = list(bpy.data.collections)
        else:
            targets = [o for o in bpy.data.objects
                       if o.parent is None and o.type == 'MESH']
        for db in targets:
            try:
                if db.asset_data is None:
                    db.asset_mark()
                marked += 1
            except Exception as e:
                print("HANGAR_MARK_SKIP:", getattr(db, "name", "?"), e, flush=True)
        # Save without generating previews — preview generation triggers full
        # Cycles renders in background mode and can crash. Names are enough;
        # Blender generates previews the next time the file is opened normally.
        try:
            bpy.ops.wm.save_mainfile(compress=bpy.data.use_autopack)
        except Exception as e:
            print("HANGAR_MARK_SAVE_FAIL:", e, flush=True)

    if action == "unmark":
        targets = []
        if target in ("objects", "all"):
            targets.extend(bpy.data.objects)
        if target in ("collections", "all"):
            targets.extend(bpy.data.collections)
        if target in ("materials", "all"):
            targets.extend(bpy.data.materials)
        for db in targets:
            try:
                if getattr(db, "asset_data", None) is not None:
                    db.asset_clear()
                    unmarked += 1
            except Exception as e:
                print("HANGAR_UNMARK_SKIP:", getattr(db, "name", "?"), e, flush=True)
        try:
            bpy.ops.wm.save_mainfile(compress=bpy.data.use_autopack)
        except Exception as e:
            print("HANGAR_UNMARK_SAVE_FAIL:", e, flush=True)

    # Write manifest of marked datablocks (names + kinds only; no preview export).
    manifest = []
    for coll, kind in ((bpy.data.objects, "Object"),
                       (bpy.data.collections, "Collection"),
                       (bpy.data.materials, "Material")):
        for db in coll:
            if getattr(db, "asset_data", None) is None:
                continue
            manifest.append({"name": db.name, "kind": kind, "file": None})

    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    print("HANGAR_MARK_DONE: marked=%d unmarked=%d assets=%d"
          % (marked, unmarked, len(manifest)), flush=True)


main()
'''


def mark_blend_assets(blend_path, target="objects"):
    """Mark top-level objects (or collections) in a .blend as Asset-Browser
    assets, generate previews, save the file in place, and export the previews
    to this file's cache dir. Returns {"ok", "marked", "assets", "error"}.

    Modifies the source .blend. Best-effort; on failure sets a human error and
    writes Blender output to RENDER_LOG."""
    blender = find_blender()
    if not blender:
        return {"ok": False, "error": "Blender wasn't found — set its path first."}
    if not os.path.exists(blend_path):
        return {"ok": False, "error": "File isn't accessible right now."}
    return _run_blend_assets(blender, blend_path, "mark", target)


def unmark_blend_assets(blend_path, target="collections"):
    """Clear Blender Asset-Browser marks from a .blend and save it in place."""
    blender = find_blender()
    if not blender:
        return {"ok": False, "error": "Blender wasn't found â€” set its path first."}
    if not os.path.exists(blend_path):
        return {"ok": False, "error": "File isn't accessible right now."}
    return _run_blend_assets(blender, blend_path, "unmark", target)


# Write Asset-Browser metadata (author/description/license/copyright/tags) onto a
# named datablock and save the .blend in place. Marks it as an asset first if it
# isn't already. Reads the field values from a JSON file (avoids argv limits /
# quoting for long descriptions).
_WRITE_META_SCRIPT = r'''
import bpy, sys, json
argv = sys.argv[sys.argv.index("--") + 1:]
kind, name, metafile = argv[0], argv[1], argv[2]
with open(metafile, "r", encoding="utf-8") as fh:
    meta = json.load(fh)
db = (bpy.data.collections.get(name) if kind == "Collection"
      else bpy.data.materials.get(name) if kind == "Material"
      else bpy.data.objects.get(name))
if db is None:
    print("HANGAR_META_FAIL: datablock not found:", name)
    sys.exit(0)
if db.asset_data is None:
    db.asset_mark()
ad = db.asset_data
for f in ("author", "description", "license", "copyright"):
    if f in meta and hasattr(ad, f):
        try:
            setattr(ad, f, meta[f] or "")
        except Exception as e:
            print("HANGAR_META_WARN: set %s failed: %s" % (f, e))
if "tags" in meta and meta["tags"] is not None:
    while len(ad.tags):
        ad.tags.remove(ad.tags[0])
    for t in meta["tags"]:
        t = (t or "").strip()
        if t:
            ad.tags.new(t)
try:
    bpy.ops.wm.save_mainfile()
    print("HANGAR_META_DONE:", name)
except Exception as e:
    print("HANGAR_META_FAIL:", e)
'''


def write_blend_asset_meta(blend_path, name, kind, meta):
    """Set Asset-Browser metadata on one datablock in a .blend and save in place.
    `meta` may carry author/description/license/copyright/tags. Returns
    {"ok", "error"}. Modifies the source file (via Blender)."""
    blender = find_blender()
    if not blender:
        return {"ok": False, "error": "Blender wasn't found — set its path first."}
    if not os.path.exists(blend_path):
        return {"ok": False, "error": "File isn't accessible right now."}
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        script = os.path.join(td, "hangar_write_meta.py")
        metafile = os.path.join(td, "meta.json")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_WRITE_META_SCRIPT)
        with open(metafile, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        try:
            proc = subprocess.run(
                [blender, "--background", "--factory-startup", "--disable-autoexec",
                 blend_path, "-P", script, "--", kind, name, metafile],
                timeout=RENDER_TIMEOUT, capture_output=True, text=True,
                env=_blender_env(), **_no_window(),
            )
        except subprocess.TimeoutExpired as e:
            _record_render_log(blender, blend_path, None, exc=e)
            return {"ok": False, "error": f"Timed out after {RENDER_TIMEOUT}s."}
        except Exception as e:
            _record_render_log(blender, blend_path, None, exc=e)
            return {"ok": False, "error": f"Couldn't launch Blender: {e}"}
    _record_render_log(blender, blend_path, proc)
    if "HANGAR_META_DONE" in (proc.stdout or ""):
        return {"ok": True}
    if "not found" in (proc.stdout or ""):
        return {"ok": False, "error": f"Couldn't find “{name}” in the file."}
    return {"ok": False, "error": _render_failure_summary(proc)}


# Extract one marked datablock into its own .blend. bpy.data.libraries.write
# writes the named object/collection plus every datablock it depends on (mesh,
# materials, textures), leaving the source file untouched.
_EXTRACT_ASSET_SCRIPT = r'''
import bpy, sys
argv = sys.argv[sys.argv.index("--") + 1:]
out_path, kind, name = argv[0], argv[1], argv[2]
db = (bpy.data.collections.get(name) if kind == "Collection"
      else bpy.data.objects.get(name))
if db is None:
    print("HANGAR_EXTRACT_FAIL: datablock not found:", name)
    sys.exit(0)
try:
    bpy.data.libraries.write(out_path, {db}, fake_user=True, compress=True)
    print("HANGAR_EXTRACT_DONE:", out_path)
except Exception as e:
    print("HANGAR_EXTRACT_FAIL:", e)
'''


def extract_blend_asset(blend_path, name, kind, out_path):
    """Write a single marked datablock (object or collection) from `blend_path`
    out to a new .blend at `out_path`, pulling its dependencies along. Leaves
    the source file untouched. Returns {"ok", "path", "error"}."""
    blender = find_blender()
    if not blender:
        return {"ok": False, "error": "Blender wasn't found — set its path first."}
    if not os.path.exists(blend_path):
        return {"ok": False, "error": "Source file isn't accessible right now."}
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        script = os.path.join(td, "hangar_extract_asset.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_EXTRACT_ASSET_SCRIPT)
        try:
            proc = subprocess.run(
                [blender, "--background", "--factory-startup", "--disable-autoexec",
                 blend_path, "-P", script, "--", out_path, kind, name],
                timeout=RENDER_TIMEOUT, capture_output=True, text=True,
                env=_blender_env(), **_no_window(),
            )
        except subprocess.TimeoutExpired as e:
            _record_render_log(blender, blend_path, None, exc=e)
            return {"ok": False, "error": f"Timed out after {RENDER_TIMEOUT}s."}
        except Exception as e:
            _record_render_log(blender, blend_path, None, exc=e)
            return {"ok": False, "error": f"Couldn't launch Blender: {e}"}
    _record_render_log(blender, blend_path, proc)
    if "HANGAR_EXTRACT_DONE" in (proc.stdout or "") and os.path.exists(out_path):
        return {"ok": True, "path": out_path}
    if "not found" in (proc.stdout or ""):
        return {"ok": False, "error": f"Couldn't find “{name}” in the file."}
    return {"ok": False, "error": _render_failure_summary(proc)}


def extract_blend_asset_previews(blend_path):
    """Export previews for a .blend's already-marked datablocks (no marking, no
    save). Returns the same shape as mark_blend_assets."""
    blender = find_blender()
    if not blender:
        return {"ok": False, "error": "Blender wasn't found — set its path first."}
    if not os.path.exists(blend_path):
        return {"ok": False, "error": "File isn't accessible right now."}
    return _run_blend_assets(blender, blend_path, "extract", "objects")


def _run_blend_assets(blender, blend_path, action, target):
    import subprocess
    import tempfile
    outdir = _blend_asset_dir(blend_path)
    outdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        script = os.path.join(td, "hangar_mark_assets.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_MARK_ASSETS_SCRIPT)
        try:
            proc = subprocess.run(
                [blender, "--background", "--factory-startup", "--disable-autoexec",
                 blend_path, "-P", script, "--", str(outdir), action, target],
                timeout=RENDER_TIMEOUT, capture_output=True, text=True,
                env=_blender_env(), **_no_window(),
            )
        except subprocess.TimeoutExpired as e:
            _record_render_log(blender, blend_path, None, exc=e)
            return {"ok": False, "error": f"Timed out after {RENDER_TIMEOUT}s."}
        except Exception as e:
            _record_render_log(blender, blend_path, None, exc=e)
            return {"ok": False, "error": f"Couldn't launch Blender: {e}"}
    _record_render_log(blender, blend_path, proc)
    m = re.search(r"HANGAR_MARK_DONE: marked=(\d+) unmarked=(\d+)", proc.stdout or "")
    if not m:
        # DONE line never printed — script failed before finishing
        return {"ok": False, "error": _render_failure_summary(proc)}
    marked = int(m.group(1))
    unmarked = int(m.group(2))
    return {"ok": True, "marked": marked, "unmarked": unmarked,
            "assets": blend_asset_previews(blend_path)}


# Per-object EEVEE thumbnail render — opens the .blend, isolates each marked
# mesh, renders 256×256 at 16 samples, writes PNGs + manifest.  Does NOT save
# the .blend so it can never crash from a pending render / save collision.
_PREVIEW_SCRIPT = r'''
import bpy, sys, os, json, re, math
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
outdir = argv[0]
mode = argv[1] if len(argv) > 1 else "auto"
os.makedirs(outdir, exist_ok=True)

scene = bpy.context.scene

# "cpu" forces CPU Cycles (no GPU) — the fallback when an EEVEE/GPU pass crashed
# the whole batch on a flaky/underpowered card; otherwise use fast EEVEE.
if mode == "cpu":
    try:
        scene.render.engine = 'CYCLES'
        scene.cycles.device = 'CPU'
        scene.cycles.samples = 16
        scene.cycles.use_denoising = False
    except Exception:
        pass
else:
    for eng in ('BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE'):
        try:
            scene.render.engine = eng
            break
        except TypeError:
            pass

try: scene.eevee.taa_render_samples = 16
except Exception: pass

scene.render.resolution_x = 256
scene.render.resolution_y = 256
scene.render.resolution_percentage = 100
scene.render.film_transparent = True
scene.render.use_file_extension = False
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.color_mode = 'RGBA'

if scene.camera is None:
    cd = bpy.data.cameras.new("_HCam")
    co = bpy.data.objects.new("_HCam", cd)
    scene.collection.objects.link(co)
    scene.camera = co

if not any(o.type == 'LIGHT' for o in scene.objects):
    ld = bpy.data.lights.new("_HSun", 'SUN')
    ld.energy = 3.5
    lo = bpy.data.objects.new("_HSun", ld)
    lo.rotation_euler = (0.9, 0.15, 0.8)
    scene.collection.objects.link(lo)


def frame_camera(pts):
    mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    center = (mn + mx) / 2.0
    radius = max((mx - mn).length / 2.0, 0.01)
    d = Vector((1.0, -1.2, 0.8)).normalized()
    scene.camera.location = center + d * (radius * 3.2)
    look = center - scene.camera.location
    scene.camera.rotation_euler = look.to_track_quat('-Z', 'Y').to_euler()


scene_meshes = [o for o in scene.objects if o.type == 'MESH']
manifest = []

def is_descendant(child, parent):
    p = child.parent
    while p is not None:
        if p == parent:
            return True
        p = p.parent
    return False


def collection_meshes(collection, seen=None):
    seen = seen or set()
    meshes = []
    if collection is None or collection.name in seen:
        return meshes
    seen.add(collection.name)
    for o in collection.objects:
        if o.type == 'MESH':
            meshes.append(o)
        if getattr(o, "instance_collection", None) is not None:
            meshes.extend(collection_meshes(o.instance_collection, seen))
    for child in collection.children:
        meshes.extend(collection_meshes(child, seen))
    return meshes


def target_meshes(ob):
    meshes = []
    if ob.type == 'MESH':
        meshes.append(ob)
    meshes.extend([o for o in bpy.data.objects
                   if o.type == 'MESH' and is_descendant(o, ob)])
    inst = getattr(ob, "instance_collection", None)
    if inst is not None:
        meshes.extend(collection_meshes(inst))
    uniq = []
    seen = set()
    for mesh in meshes:
        if mesh.name not in seen:
            uniq.append(mesh)
            seen.add(mesh.name)
    return uniq


def make_preview_sphere():
    rings, segments = 18, 36
    verts = []
    faces = []
    for r in range(rings + 1):
        theta = math.pi * r / rings
        z = math.cos(theta)
        radius = math.sin(theta)
        for s in range(segments):
            phi = 2.0 * math.pi * s / segments
            verts.append((radius * math.cos(phi), radius * math.sin(phi), z))
    for r in range(rings):
        for s in range(segments):
            a = r * segments + s
            b = r * segments + ((s + 1) % segments)
            c = (r + 1) * segments + ((s + 1) % segments)
            d = (r + 1) * segments + s
            faces.append((a, b, c, d))
    mesh = bpy.data.meshes.new("_HangarMaterialPreviewMesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    for poly in mesh.polygons:
        poly.use_smooth = True
    obj = bpy.data.objects.new("_HangarMaterialPreview", mesh)
    scene.collection.objects.link(obj)
    return obj


for ob in bpy.data.objects:
    if getattr(ob, 'asset_data', None) is None:
        continue
    meshes = target_meshes(ob)
    if not meshes:
        manifest.append({"name": ob.name, "kind": "Object", "file": None})
        continue
    linked_now = []
    for mesh in meshes:
        if mesh.name not in scene.objects:
            try:
                scene.collection.objects.link(mesh)
                linked_now.append(mesh)
                if mesh not in scene_meshes:
                    scene_meshes.append(mesh)
            except Exception as ex:
                print("HANGAR_PREVIEW_LINK_FAIL:", ob.name, mesh.name, ex, flush=True)
    render_names = {mesh.name for mesh in meshes}
    for o in scene_meshes:
        o.hide_render = (o.name not in render_names)
    for mesh in meshes:
        mesh.hide_render = False
    pts = []
    for mesh in meshes:
        try:
            pts.extend([mesh.matrix_world @ Vector(c[:]) for c in mesh.bound_box])
        except Exception:
            pass
    if not pts:
        manifest.append({"name": ob.name, "kind": "Object", "file": None})
        for o in scene_meshes:
            o.hide_render = False
        for mesh in linked_now:
            try: scene.collection.objects.unlink(mesh)
            except Exception: pass
        continue
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', ob.name)[:80]
    fname = 'OB_%s.png' % safe
    frame_camera(pts)
    scene.render.filepath = os.path.join(outdir, fname)
    try:
        bpy.ops.render.render(write_still=True)
        manifest.append({"name": ob.name, "kind": "Object", "file": fname})
    except Exception as ex:
        print("HANGAR_PREVIEW_RENDER_FAIL:", ob.name, ex, flush=True)
        manifest.append({"name": ob.name, "kind": "Object", "file": None})
    for o in scene_meshes:
        o.hide_render = False
    for mesh in linked_now:
        try: scene.collection.objects.unlink(mesh)
        except Exception: pass

for mat in bpy.data.materials:
    if getattr(mat, 'asset_data', None) is None:
        continue
    for o in scene_meshes:
        o.hide_render = True
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', mat.name)[:80]
    fname = 'MA_%s.png' % safe
    sphere = None
    try:
        sphere = make_preview_sphere()
        sphere.data.materials.append(mat)
        sphere.hide_render = False
        pts = [sphere.matrix_world @ Vector(c[:]) for c in sphere.bound_box]
        frame_camera(pts)
        scene.render.filepath = os.path.join(outdir, fname)
        bpy.ops.render.render(write_still=True)
        manifest.append({"name": mat.name, "kind": "Material", "file": fname})
    except Exception as ex:
        print("HANGAR_PREVIEW_MATERIAL_FAIL:", mat.name, ex, flush=True)
        manifest.append({"name": mat.name, "kind": "Material", "file": None})
    finally:
        if sphere is not None:
            mesh = sphere.data
            try:
                bpy.data.objects.remove(sphere, do_unlink=True)
            except Exception:
                pass
            try:
                bpy.data.meshes.remove(mesh)
            except Exception:
                pass
        for o in scene_meshes:
            o.hide_render = False

for col in bpy.data.collections:
    if getattr(col, 'asset_data', None) is not None:
        manifest.append({"name": col.name, "kind": "Collection", "file": None})

with open(os.path.join(outdir, 'manifest.json'), 'w', encoding='utf-8') as fh:
    json.dump(manifest, fh)
print("HANGAR_PREVIEW_DONE: assets=%d" % len(manifest), flush=True)
'''


def generate_blend_asset_previews(blend_path):
    """Render a 256×256 EEVEE thumbnail for each marked mesh object in a .blend
    and write them + a manifest to the cache dir. Non-destructive (never saves
    the .blend). Returns {"ok", "assets", "error"}."""
    blender = find_blender()
    if not blender:
        return {"ok": False, "error": "Blender wasn't found — set its path first."}
    if not os.path.exists(blend_path):
        return {"ok": False, "error": "File isn't accessible right now."}
    import subprocess, tempfile
    outdir = _blend_asset_dir(blend_path)
    outdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        script = os.path.join(td, "hangar_preview.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_PREVIEW_SCRIPT)

        def _run(mode):
            return subprocess.run(
                [blender, "--background", "--factory-startup", "--disable-autoexec",
                 blend_path, "-P", script, "--", str(outdir), mode],
                timeout=RENDER_TIMEOUT, capture_output=True, text=True,
                env=_blender_env(), **_no_window(),
            )

        try:
            proc = _run("auto")
            # A whole-batch EEVEE/GPU crash (no manifest written) — fall back to
            # CPU Cycles, same as the single-model render path does.
            if ("HANGAR_PREVIEW_DONE" not in (proc.stdout or "")
                    and _should_retry_cpu(proc)):
                first = proc
                proc = _run("cpu")
                proc.stdout = ((first.stdout or "") + "\n--- CPU retry stdout ---\n"
                               + (proc.stdout or ""))
                proc.stderr = ((first.stderr or "") + "\n--- CPU retry stderr ---\n"
                               + (proc.stderr or ""))
        except subprocess.TimeoutExpired as e:
            _record_render_log(blender, blend_path, None, exc=e)
            return {"ok": False, "error": f"Timed out after {RENDER_TIMEOUT}s."}
        except Exception as e:
            _record_render_log(blender, blend_path, None, exc=e)
            return {"ok": False, "error": f"Couldn't launch Blender: {e}"}
    _record_render_log(blender, blend_path, proc)
    m = re.search(r"HANGAR_PREVIEW_DONE: assets=(\d+)", proc.stdout or "")
    if not m:
        return {"ok": False, "error": _render_failure_summary(proc)}
    return {"ok": True, "assets": blend_asset_previews(blend_path)}


def blend_asset_previews(blend_path):
    """Read the cached preview manifest for a .blend (if any). Returns a list of
    {"name", "kind", "has_thumb"} — empty when no previews have been exported."""
    d = _blend_asset_dir(blend_path)
    try:
        with open(d / "manifest.json", "r", encoding="utf-8") as fh:
            entries = json.load(fh)
    except Exception:
        return []

    def _mtime(file):
        try:
            return int((d / file).stat().st_mtime) if file else 0
        except OSError:
            return 0

    return [{"name": e.get("name"), "kind": e.get("kind"),
             "has_thumb": bool(e.get("file")),
             # mtime of the exported PNG so the drawer can bust its cached <img>
             # when previews are regenerated (the URL is otherwise stable).
             "mtime": _mtime(e.get("file")),
             "preview_source": ("Hangar rendered asset preview cache"
                                if e.get("file")
                                else "No rendered asset preview; showing type badge")}
            for e in entries]


def blend_asset_thumb_path(blend_path, name):
    """Absolute path to a single marked datablock's exported preview PNG, or None
    if it hasn't been exported. Looks the name up in the manifest."""
    d = _blend_asset_dir(blend_path)
    try:
        with open(d / "manifest.json", "r", encoding="utf-8") as fh:
            entries = json.load(fh)
    except Exception:
        return None
    for e in entries:
        if e.get("name") == name and e.get("file"):
            p = d / e["file"]
            return p if p.exists() else None
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
# Opens (.blend) or imports (everything else) the model, frames it with a fresh
# camera, then renders with EEVEE (so materials/textures show — Workbench would
# render flat and material-less) under a neutral world + key sun. Imported files
# get our camera/world/sun; an authored .blend keeps its own. Import operator
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


def _scene_points(scene):
    """World-space bounding-box corners of every visible piece of geometry.

    Walks the evaluated dependency graph rather than scene.objects directly, so
    instanced geometry — how USD (and Alembic) commonly import, as empties that
    instance prototype meshes — is included. Without this the camera frames an
    empty scene and the render comes out blank."""
    try:
        bpy.context.view_layer.update()      # make sure instances are evaluated
    except Exception:
        pass
    deps = bpy.context.evaluated_depsgraph_get()
    geom = {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}
    pts = []
    for inst in deps.object_instances:
        ob = inst.object
        if ob is None or ob.type not in geom:
            continue
        mw = inst.matrix_world
        for c in ob.bound_box:
            pts.append(mw @ Vector(c[:]))
    return pts


def _link_blend_fallback_geometry(scene):
    """If a .blend opens to an empty scene, link loose object datablocks so an
    asset-file/object-library .blend can still render a preview."""
    geom = {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}
    linked = 0
    for ob in bpy.data.objects:
        if ob.type not in geom:
            continue
        try:
            ob.hide_render = False
            ob.hide_viewport = False
        except Exception:
            pass
        if ob.name in scene.objects:
            continue
        try:
            scene.collection.objects.link(ob)
            linked += 1
        except RuntimeError:
            pass
        except Exception as ex:
            print("HANGAR_LINK_SKIP:", ob.name, ex, flush=True)
    if linked:
        print("HANGAR_LINKED_LOOSE_GEOMETRY:", linked, flush=True)
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass
    return linked


def _set_cpu_cycles(scene):
    """Use CPU Cycles for authored .blend scenes."""
    try:
        scene.render.engine = 'CYCLES'
        scene.cycles.device = 'CPU'
        scene.cycles.samples = 16
        scene.cycles.preview_samples = 8
        scene.cycles.use_denoising = False
        scene.render.use_persistent_data = False
        return 'CYCLES_CPU'
    except Exception:
        return None


def _set_engine(scene, ext, mode):
    """Pick EEVEE so materials/textures show (Workbench renders flat, no
    materials). The engine id changed across versions — EEVEE Next is
    'BLENDER_EEVEE_NEXT' in 4.2 and 'BLENDER_EEVEE' in 3.x and 5.x — so try the
    known ids until one sticks; fall back to Workbench only if none exist."""
    if ext == ".blend" and mode == "cpu":
        eng = _set_cpu_cycles(scene)
        if eng:
            return eng
    for eng in ('BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE', 'BLENDER_WORKBENCH'):
        try:
            scene.render.engine = eng
            return eng
        except Exception:
            continue
    return None


def _ensure_world(scene):
    """A neutral, mildly bright world so imported models (which arrive with no
    lighting) still read their base colours in EEVEE. Skipped when the file
    already ships a world (e.g. an authored .blend)."""
    if scene.world is not None:
        return
    world = bpy.data.worlds.new("HangarWorld")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.6, 0.6, 0.65, 1.0)
        bg.inputs[1].default_value = 1.2
    scene.world = world


def _ensure_lights(scene):
    """Add a key sun for shape/shading if the scene has none of its own."""
    if any(o.type == 'LIGHT' for o in scene.objects):
        return
    sun_data = bpy.data.lights.new("HangarSun", 'SUN')
    sun_data.energy = 3.5
    sun = bpy.data.objects.new("HangarSun", sun_data)
    sun.rotation_euler = (0.9, 0.15, 0.8)
    scene.collection.objects.link(sun)


def _apply_default_material():
    """Assign a neutral PBR material to every mesh that has no material slots.

    USD/FBX/ABC files sometimes import geometry without any material assignment,
    which makes EEVEE render them as pure black silhouettes. Walking
    bpy.data.objects (not just scene.objects) catches USD prototype meshes that
    are instanced via empties and therefore not directly in the collection."""
    default = None
    for ob in bpy.data.objects:
        if ob.type != 'MESH' or ob.data is None or ob.material_slots:
            continue
        if default is None:
            default = bpy.data.materials.new("HangarDefault")
            default.use_nodes = True
            pbsdf = default.node_tree.nodes.get("Principled BSDF")
            if pbsdf:
                pbsdf.inputs["Base Color"].default_value = (0.65, 0.65, 0.65, 1.0)
                pbsdf.inputs["Roughness"].default_value = 0.5
        ob.data.materials.append(default)


def frame_and_render(out, ext, mode):
    scene = bpy.context.scene

    # No renderable geometry at all — e.g. a USD that's just a material/surface
    # definition (a Megascans texture set), not a model. Don't render a blank;
    # signal it so Hangar can fall back to the set's colour map instead.
    pts = _scene_points(scene)
    if not pts and ext == ".blend":
        _link_blend_fallback_geometry(scene)
        pts = _scene_points(scene)
    if not pts:
        print("HANGAR_NO_GEOMETRY", flush=True)
        return

    if scene.camera is None:
        cam_data = bpy.data.cameras.new("HangarCam")
        cam = bpy.data.objects.new("HangarCam", cam_data)
        scene.collection.objects.link(cam)
        scene.camera = cam
        mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
        center = (mn + mx) / 2.0
        radius = max((mx - mn).length / 2.0, 0.5)
        cam.data.lens = 50
        d = Vector((1.0, -1.2, 0.8)).normalized()
        cam.location = center + d * (radius * 3.2)
        look = center - cam.location
        cam.rotation_euler = look.to_track_quat('-Z', 'Y').to_euler()

    _set_engine(scene, ext, mode)
    _ensure_world(scene)
    _ensure_lights(scene)
    _apply_default_material()
    # Keep EEVEE fast for a thumbnail; transparent film so the object composites
    # cleanly onto Hangar's dark tile background.
    try:
        scene.eevee.taa_render_samples = 16
    except Exception:
        pass
    r = scene.render
    r.engine = scene.render.engine
    r.film_transparent = True
    r.resolution_x = 512
    r.resolution_y = 512
    r.resolution_percentage = 100
    r.use_file_extension = False
    r.image_settings.file_format = 'PNG'
    r.image_settings.color_mode = 'RGBA'
    r.filepath = out
    bpy.ops.render.render(write_still=True)
    # Report which engine + GPU actually did the render, so Hangar can show it.
    # flush=True because Blender (esp. 4.2 in background) block-buffers a piped
    # stdout and hard-exits without flushing, which loses these lines.
    print("HANGAR_ENGINE:", scene.render.engine, flush=True)
    try:
        import gpu
        print("HANGAR_GPU:", gpu.platform.vendor_get(), "::",
              gpu.platform.renderer_get(), flush=True)
    except Exception:
        print("HANGAR_GPU: unavailable", flush=True)
    sys.stdout.flush()


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    src, out = argv[0], argv[1]
    mode = argv[2] if len(argv) > 2 else "auto"
    load_model(src)
    frame_and_render(out, os.path.splitext(src)[1].lower(), mode)

main()
'''


# Populated with a human-readable reason whenever the last render failed, so the
# UI/endpoint can tell the user *why* instead of a generic failure. Full Blender
# output is also written to ~/.hangar/last_render.log for deeper debugging.
LAST_RENDER_ERROR = None
# Engine + GPU the most recent successful render used (parsed from Blender's
# output), so the diagnostics panel can show what's actually doing the work.
LAST_RENDER_GPU = None
RENDER_LOG = store.DATA_DIR / "last_render.log"


def _blender_env():
    """Environment for thumbnail renders.

    Blender ships its own Python. Dropping Python-related variables inherited
    from Hangar avoids leaking stale frozen-app paths into Blender's process.
    """
    env = os.environ.copy()
    for key in ("PYTHONNET_PYDLL", "PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    return env


def _parse_render_gpu(proc):
    """Pull the HANGAR_ENGINE/HANGAR_GPU lines out of Blender's stdout."""
    global LAST_RENDER_GPU
    if not proc or not proc.stdout:
        return
    eng = gpu = None
    for line in proc.stdout.splitlines():
        if line.startswith("HANGAR_ENGINE:"):
            eng = line.split(":", 1)[1].strip()
        elif line.startswith("HANGAR_GPU:"):
            gpu = line.split(":", 1)[1].strip()
    if eng or gpu:
        LAST_RENDER_GPU = f"{eng or '?'} · {gpu or '?'}"


def _render_engine(proc):
    """The engine Blender actually rendered with, from its HANGAR_ENGINE line
    (e.g. BLENDER_EEVEE_NEXT, or CYCLES after a CPU fallback). None if absent."""
    if not proc or not proc.stdout:
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("HANGAR_ENGINE:"):
            return line.split(":", 1)[1].strip()
    return None


def _render_failure_summary(proc=None, exc=None):
    if exc is not None:
        if isinstance(exc, subprocess.TimeoutExpired):
            return (f"Render timed out after {RENDER_TIMEOUT}s. This scene is too "
                    "heavy for a quick thumbnail; raise HANGAR_RENDER_TIMEOUT or "
                    "render it on a faster machine / farm worker.")
        return f"Couldn't launch Blender: {exc}"

    text = ""
    if proc is not None:
        text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    crash = ""
    m = re.search(r"Writing:\s*(.+?\.crash\.txt)", text, re.IGNORECASE)
    if m:
        crash = f" Crash report: {m.group(1).strip()}"
    low = text.lower()
    if "malloc returns null" in low or "out of memory" in low:
        return ("Blender ran out of memory while opening/rendering this scene. "
                "The embedded .blend preview will remain in use." + crash)
    if ("exception_access_violation" in low
            or (proc is not None and proc.returncode not in (None, 0))):
        code = f" (exit code {proc.returncode})" if proc is not None else ""
        return ("Blender crashed while rendering this preview" + code + ". "
                "The embedded .blend preview will remain in use." + crash)

    tail = ""
    for line in reversed(text.splitlines()):
        if line.strip():
            tail = line.strip()
            break
    return tail or "Blender ran but produced no image."


def _is_crash_returncode(rc):
    """True when Blender died on an unhandled exception rather than exiting with a
    clean status. Windows surfaces the NTSTATUS code as a large unsigned int with
    the high bit set (e.g. 0xC0000409 STACK_BUFFER_OVERRUN = 3221226505, or
    0xC0000005 ACCESS_VIOLATION); POSIX reports a fatal signal as a negative code.
    A clean Blender error exit is a small positive code (usually 1)."""
    return rc is not None and (rc < 0 or rc >= 0xC0000000)


def _should_retry_cpu(proc):
    if proc is None:
        return False
    # A hard process crash on a .blend almost always means the GPU/EEVEE path
    # blew up (the MX130 here dies with 0xC0000409 mid-render, usually after a
    # run of "GPUTexture: Blender Texture Not Loaded!"). Retrying on CPU Cycles
    # sidesteps the GPU entirely, so treat any crash code as retry-worthy.
    if _is_crash_returncode(proc.returncode):
        return True
    text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).lower()
    return any(s in text for s in (
        "out of memory",
        "malloc returns null",
        "unknown exception",
        "writing:",
        ".crash.txt",
        "exception_access_violation",
        "gpu_material_compile",
        "internal malloc failed",
        "gputexture",
        "blender texture not loaded",
    ))


def _run_blender_render(blender, script, model_path, png, mode):
    return subprocess.run(
        [blender, "--background", "--factory-startup", "--disable-autoexec",
         "-P", script, "--", model_path, png, mode],
        timeout=RENDER_TIMEOUT, capture_output=True, text=True,
        env=_blender_env(), **_no_window(),
    )


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
    LAST_RENDER_ERROR = _render_failure_summary(proc, exc)
    return LAST_RENDER_ERROR
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
            proc = _run_blender_render(blender, script, model_path, png, "auto")
            if (proc.returncode and os.path.splitext(model_path)[1].lower() == ".blend"
                    and _should_retry_cpu(proc)):
                first = proc
                proc = _run_blender_render(blender, script, model_path, png, "cpu")
                if proc.returncode:
                    proc.stdout = ((first.stdout or "") + "\n--- CPU retry stdout ---\n"
                                   + (proc.stdout or ""))
                    proc.stderr = ((first.stderr or "") + "\n--- CPU retry stderr ---\n"
                                   + (proc.stderr or ""))
        except subprocess.TimeoutExpired as e:
            _record_render_log(blender, model_path, None, exc=e)
            LAST_RENDER_ERROR = (
                f"Render timed out after {RENDER_TIMEOUT}s — this scene is too heavy "
                f"for a quick thumbnail. Raise HANGAR_RENDER_TIMEOUT, or render it on "
                f"a faster GPU / farm worker.")
            return False
        except Exception as e:
            _record_render_log(blender, model_path, None, exc=e)
            return False
        # A material/surface file with no geometry (e.g. a Megascans USD) — the
        # render is intentionally empty. Preview it with the set's colour map.
        if proc is not None and "HANGAR_NO_GEOMETRY" in (proc.stdout or ""):
            sib = _find_sibling_preview(model_path)
            if sib is not None:
                try:
                    with Image.open(sib) as im:
                        if _save_downscaled(im, out_jpg):
                            LAST_RENDER_ERROR = None
                            _write_thumb_source(out_jpg, "sibling")
                            _record_render_log(blender, model_path, proc)
                            return True
                except Exception:
                    pass
            LAST_RENDER_ERROR = ("No geometry in this file — it looks like a "
                                 "material/surface, not a model.")
            _record_render_log(blender, model_path, proc)
            return False
        if os.path.exists(png):
            try:
                with Image.open(png) as img:
                    img.load()
                    # A perfectly flat image means Blender imported the file but
                    # nothing landed in the camera view — common when a USD's up
                    # axis/scale leaves the geometry off-frame, or the import was
                    # empty. Report it instead of caching a blank gray tile.
                    if _is_blank(img):
                        LAST_RENDER_ERROR = (
                            "Imported but the render was empty (nothing in frame "
                            "— check the model's scale / up-axis).")
                        _record_render_log(blender, model_path, proc)
                        return False
                    ok = _save_downscaled(img, out_jpg)
                if ok:
                    LAST_RENDER_ERROR = None
                    _parse_render_gpu(proc)
                    _write_thumb_source(out_jpg, "render", _render_engine(proc))
                    _record_render_log(blender, model_path, proc)
                    return True
            except Exception as e:
                _record_render_log(blender, model_path, proc, exc=e)
                return False
        # No PNG (or save failed) — surface why.
        _record_render_log(blender, model_path, proc)
        return False


def _is_blank(img):
    """True when every pixel is the same colour (a featureless render)."""
    try:
        ex = img.convert("RGB").getextrema()   # ((rmin,rmax),(gmin,gmax),(bmin,bmax))
        return all(lo == hi for lo, hi in ex)
    except Exception:
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
