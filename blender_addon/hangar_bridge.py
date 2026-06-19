"""Hangar Bridge — Blender companion addon.

Install:  Edit > Preferences > Add-ons > Install… > pick this file > enable it.
Use:      In the 3D viewport press N, open the "Hangar" tab, click "Connect".
          Now "Send to Blender" in Hangar imports straight into your scene.

It works by tailing a small queue file that the Hangar app appends to, so no
network setup or ports are needed — just two local apps sharing one file.
"""

bl_info = {
    "name": "Hangar Bridge",
    "author": "Hangar",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar (N) > Hangar",
    "description": "Receive assets sent from the Hangar asset manager.",
    "category": "Import-Export",
}

import json
import os
from pathlib import Path

import bpy

QUEUE_PATH = Path(os.environ.get("HANGAR_HOME", Path.home() / ".hangar")) / "blender_queue.jsonl"
POLL_SECONDS = 1.0
_state = {"offset": 0, "running": False}


def _import_file(path, ext):
    ext = ext.lower()
    try:
        if ext == ".obj":
            # Blender 4.x uses wm.obj_import; older uses import_scene.obj.
            if hasattr(bpy.ops.wm, "obj_import"):
                bpy.ops.wm.obj_import(filepath=path)
            else:
                bpy.ops.import_scene.obj(filepath=path)
        elif ext == ".fbx":
            bpy.ops.import_scene.fbx(filepath=path)
        elif ext in (".gltf", ".glb"):
            bpy.ops.import_scene.gltf(filepath=path)
        elif ext == ".stl":
            if hasattr(bpy.ops.wm, "stl_import"):
                bpy.ops.wm.stl_import(filepath=path)
            else:
                bpy.ops.import_mesh.stl(filepath=path)
        elif ext == ".ply":
            if hasattr(bpy.ops.wm, "ply_import"):
                bpy.ops.wm.ply_import(filepath=path)
            else:
                bpy.ops.import_mesh.ply(filepath=path)
        elif ext in (".usd", ".usda", ".usdc", ".usdz"):
            bpy.ops.wm.usd_import(filepath=path)
        elif ext == ".abc":
            bpy.ops.wm.alembic_import(filepath=path)
        elif ext == ".dae":
            bpy.ops.wm.collada_import(filepath=path)
        elif ext == ".blend":
            # Append every object from the .blend file's Object directory.
            with bpy.data.libraries.load(path, link=False) as (src, dst):
                dst.objects = list(src.objects)
            for obj in dst.objects:
                if obj is not None:
                    bpy.context.collection.objects.link(obj)
        else:
            print(f"[Hangar] Unsupported format: {ext}")
            return
        print(f"[Hangar] Imported {os.path.basename(path)}")
    except Exception as e:
        print(f"[Hangar] Failed to import {path}: {e}")


def _poll():
    if not _state["running"]:
        return None
    try:
        if QUEUE_PATH.exists():
            with open(QUEUE_PATH, "r", encoding="utf-8") as fh:
                fh.seek(_state["offset"])
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            entry = json.loads(line)
                            if entry.get("action") == "import":
                                _import_file(entry["path"], entry.get("ext", ""))
                        except Exception as e:
                            print(f"[Hangar] Bad queue entry: {e}")
                _state["offset"] = fh.tell()
    except Exception as e:
        print(f"[Hangar] Poll error: {e}")
    return POLL_SECONDS


class HANGAR_OT_connect(bpy.types.Operator):
    bl_idname = "hangar.connect"
    bl_label = "Connect to Hangar"
    bl_description = "Start watching for assets sent from Hangar"

    def execute(self, context):
        if _state["running"]:
            self.report({"INFO"}, "Hangar already connected")
            return {"FINISHED"}
        # Skip anything already in the queue so we don't re-import history.
        _state["offset"] = QUEUE_PATH.stat().st_size if QUEUE_PATH.exists() else 0
        _state["running"] = True
        bpy.app.timers.register(_poll, first_interval=POLL_SECONDS)
        self.report({"INFO"}, "Hangar connected")
        return {"FINISHED"}


class HANGAR_OT_disconnect(bpy.types.Operator):
    bl_idname = "hangar.disconnect"
    bl_label = "Disconnect"
    bl_description = "Stop watching for assets"

    def execute(self, context):
        _state["running"] = False
        self.report({"INFO"}, "Hangar disconnected")
        return {"FINISHED"}


class HANGAR_PT_panel(bpy.types.Panel):
    bl_label = "Hangar"
    bl_idname = "HANGAR_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hangar"

    def draw(self, context):
        layout = self.layout
        connected = _state["running"]
        row = layout.row()
        row.label(text="Connected" if connected else "Not connected",
                  icon="LINKED" if connected else "UNLINKED")
        if connected:
            layout.operator("hangar.disconnect", icon="X")
        else:
            layout.operator("hangar.connect", icon="PLAY")
        layout.separator()
        box = layout.box()
        box.label(text="Queue file:", icon="FILE")
        box.label(text=str(QUEUE_PATH))


classes = (HANGAR_OT_connect, HANGAR_OT_disconnect, HANGAR_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    _state["running"] = False
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
