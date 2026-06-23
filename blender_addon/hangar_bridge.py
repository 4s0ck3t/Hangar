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
    "version": (1, 1, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar (N) > Hangar",
    "description": "Receive assets, materials and HDRIs sent from the Hangar asset manager.",
    "category": "Import-Export",
}

import json
import os
from pathlib import Path

import bpy

QUEUE_PATH = Path(os.environ.get("HANGAR_HOME", Path.home() / ".hangar")) / "blender_queue.jsonl"
POLL_SECONDS = 1.0
_state = {"offset": 0, "running": False}


def _selected_meshes(context):
    return [o for o in context.selected_objects if o.type == "MESH"]


def _place_new_at_cursor(context, before):
    """Move objects added since `before` (a set of names) to the 3D cursor."""
    cur = context.scene.cursor.location
    for obj in context.scene.objects:
        if obj.name not in before and obj.parent is None:
            obj.location = obj.location + cur


def _load_image(path, non_color=False):
    img = bpy.data.images.load(path, check_existing=True)
    if non_color:
        try:
            img.colorspace_settings.name = "Non-Color"
        except Exception:
            pass
    return img


def _apply_material(maps, name, to_selection):
    """Build a Principled-BSDF material from a Hangar texture set and either
    assign it to the selected meshes or leave it as a ready-to-use datablock.

    `maps` is a dict of role -> file path, roles matching Hangar's scanner:
    diffuse, roughness, metallic, normal, ao, displacement, specular.
    """
    mat = bpy.data.materials.new(name=name or "Hangar Material")
    mat.use_nodes = True
    nt = mat.node_tree
    nodes, links = nt.nodes, nt.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputMaterial"); out.location = (700, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (300, 0)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    def tex(path, non_color, y):
        n = nodes.new("ShaderNodeTexImage")
        n.location = (-700, y)
        try:
            n.image = _load_image(path, non_color)
        except Exception as e:
            print(f"[Hangar] couldn't load {path}: {e}")
            nodes.remove(n)
            return None
        return n

    y = 500
    wired = []
    # Base Color — multiplied by AO when both are present (Quixel-style).
    diff = tex(maps["diffuse"], False, y) if maps.get("diffuse") else None
    if diff:
        wired.append("diffuse"); y -= 280
        ao = tex(maps["ao"], True, y) if maps.get("ao") else None
        if ao:
            wired.append("ao"); y -= 280
            mix = nodes.new("ShaderNodeMixRGB"); mix.blend_type = "MULTIPLY"
            mix.inputs["Fac"].default_value = 1.0; mix.location = (-150, 450)
            links.new(diff.outputs["Color"], mix.inputs["Color1"])
            links.new(ao.outputs["Color"], mix.inputs["Color2"])
            links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
        else:
            links.new(diff.outputs["Color"], bsdf.inputs["Base Color"])
    if maps.get("metallic"):
        n = tex(maps["metallic"], True, y)
        if n: wired.append("metallic"); y -= 280; links.new(n.outputs["Color"], bsdf.inputs["Metallic"])
    if maps.get("roughness"):
        n = tex(maps["roughness"], True, y)
        if n: wired.append("roughness"); y -= 280; links.new(n.outputs["Color"], bsdf.inputs["Roughness"])
    if maps.get("specular") and "Specular" in bsdf.inputs:  # renamed in Blender 4.x
        n = tex(maps["specular"], True, y)
        if n: wired.append("specular"); y -= 280; links.new(n.outputs["Color"], bsdf.inputs["Specular"])
    if maps.get("normal"):
        n = tex(maps["normal"], True, y)
        if n:
            wired.append("normal"); y -= 280
            nm = nodes.new("ShaderNodeNormalMap"); nm.location = (-150, n.location[1])
            links.new(n.outputs["Color"], nm.inputs["Color"])
            links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    if maps.get("displacement"):
        n = tex(maps["displacement"], True, y)
        if n:
            wired.append("displacement")
            dsp = nodes.new("ShaderNodeDisplacement"); dsp.location = (300, -400)
            dsp.inputs["Scale"].default_value = 0.05
            links.new(n.outputs["Color"], dsp.inputs["Height"])
            links.new(dsp.outputs["Displacement"], out.inputs["Displacement"])
            try:
                mat.cycles.displacement_method = "BOTH"
            except Exception:
                pass

    targets = _selected_meshes(bpy.context) if to_selection else []
    for obj in targets:
        obj.data.materials.clear()
        obj.data.materials.append(mat)
    where = (f"applied to {len(targets)} object(s)" if targets
             else "added to the material list")
    print(f"[Hangar] Material '{mat.name}' built ({', '.join(wired) or 'no maps'}) — {where}")
    return mat


def _set_world_hdri(path, strength=1.0):
    """Use an HDRI as the scene's environment lighting."""
    scene = bpy.context.scene
    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("World"); scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    nodes, links = nt.nodes, nt.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputWorld"); out.location = (400, 0)
    bg = nodes.new("ShaderNodeBackground"); bg.location = (150, 0)
    env = nodes.new("ShaderNodeTexEnvironment"); env.location = (-250, 0)
    try:
        env.image = _load_image(path)
    except Exception as e:
        print(f"[Hangar] couldn't load HDRI {path}: {e}")
        return
    bg.inputs["Strength"].default_value = strength
    links.new(env.outputs["Color"], bg.inputs["Color"])
    links.new(bg.outputs["Background"], out.inputs["Surface"])
    print(f"[Hangar] World HDRI set to {os.path.basename(path)}")


def _import_file(path, ext, place_at_cursor=False):
    ext = ext.lower()
    before = {o.name for o in bpy.context.scene.objects}
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
        if place_at_cursor:
            _place_new_at_cursor(bpy.context, before)
        print(f"[Hangar] Imported {os.path.basename(path)}")
    except Exception as e:
        print(f"[Hangar] Failed to import {path}: {e}")


def _dispatch(entry):
    """Route one queue entry to the right handler based on its action."""
    action = entry.get("action", "import")
    if action == "import":
        _import_file(entry["path"], entry.get("ext", ""),
                     place_at_cursor=entry.get("place_at_cursor", False))
    elif action == "apply_material":
        _apply_material(entry.get("maps", {}), entry.get("name", ""),
                        entry.get("to_selection", True))
    elif action == "set_world_hdri":
        _set_world_hdri(entry["path"], entry.get("strength", 1.0))
    else:
        print(f"[Hangar] Unknown action: {action}")


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
                            _dispatch(entry)
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
