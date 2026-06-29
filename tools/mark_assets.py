"""Trial: mark objects in a .blend as Asset-Browser assets and generate previews.

Run with Blender, NOT plain Python:

    blender -b "D:\\Scripts\\3D - Futuristic\\Futuristic.blend" \
            --factory-startup -P tools/mark_assets.py -- [options]

Options (after the `--`):
    --inplace            overwrite the source .blend (default: write a copy)
    --out PATH           explicit output path (implies a copy)
    --collections        mark top-level COLLECTIONS instead of objects
    --types T1,T2        object types to mark (default: MESH)
    --no-preview         skip per-asset preview generation (faster, no thumbs)

Why this exists: heavy scenes like Futuristic.blend blow the whole-scene
thumbnail render timeout. Marking individual objects/collections as assets lets
Blender generate a light *per-asset* preview (object on a neutral backdrop)
instead of rendering the entire scene — far cheaper, even on a weak GPU. The
generated previews live inside the .blend and Blender's Asset Browser shows
them too.

This MODIFIES .blend files, so by default it writes to `<stem>.assets.blend`
and leaves your original untouched. Validate the copy in Blender, then re-run
with --inplace once you trust it.
"""
import sys
import os

import bpy


def _args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    opts = {
        "inplace": "--inplace" in argv,
        "collections": "--collections" in argv,
        "preview": "--no-preview" not in argv,
        "out": None,
        "types": {"MESH"},
    }
    if "--out" in argv:
        opts["out"] = argv[argv.index("--out") + 1]
    if "--types" in argv:
        opts["types"] = {t.strip().upper()
                         for t in argv[argv.index("--types") + 1].split(",") if t.strip()}
    return opts


def _generate_preview(datablock):
    """Generate an asset preview for one datablock in background mode.

    Preview generation is normally async (timer-driven); in `-b` mode we force
    it synchronously. The operator + context-override spelling changed across
    versions, so try the known forms in turn."""
    # Blender 4.x: temp_override
    try:
        with bpy.context.temp_override(id=datablock):
            bpy.ops.ed.lib_id_generate_preview()
        return True
    except Exception:
        pass
    # Older dict-override spelling
    try:
        bpy.ops.ed.lib_id_generate_preview({"id": datablock})
        return True
    except Exception:
        pass
    return False


def main():
    opts = _args()
    src = bpy.data.filepath
    marked = 0
    previews = 0

    if opts["collections"]:
        targets = list(bpy.data.collections)
        label = "collections"
    else:
        # Top-level objects (no parent) of the requested types.
        targets = [o for o in bpy.data.objects
                   if o.parent is None and o.type in opts["types"]]
        label = "objects (%s)" % ",".join(sorted(opts["types"]))

    print("HANGAR_MARK: %d %s to consider" % (len(targets), label), flush=True)

    for db in targets:
        try:
            if db.asset_data is None:        # not already an asset
                db.asset_mark()
            marked += 1
            if opts["preview"] and _generate_preview(db):
                previews += 1
        except Exception as e:
            print("HANGAR_MARK: skip %r — %s" % (getattr(db, "name", "?"), e), flush=True)

    # Let any queued preview jobs flush before we save.
    if opts["preview"]:
        try:
            bpy.ops.wm.previews_ensure()
        except Exception:
            pass

    # Decide where to save.
    if opts["out"]:
        dst = opts["out"]
    elif opts["inplace"]:
        dst = src
    else:
        stem, ext = os.path.splitext(src)
        dst = stem + ".assets" + ext

    bpy.ops.wm.save_as_mainfile(filepath=dst, compress=False)
    print("HANGAR_MARK: marked=%d previews=%d saved=%s"
          % (marked, previews, dst), flush=True)


if __name__ == "__main__":
    main()
