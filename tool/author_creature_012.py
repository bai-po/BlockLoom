"""Build the 0.1.2 creature source projects and isolated component library in Blender 4.5."""

import argparse
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

import bpy
from mathutils import Vector

PROJECT = Path(__file__).resolve().parents[1]
VERSION = "0.1.2"
RIG_TAG = "oxal_humanoid_mob"


def localize_brute(scratch, library):
    """The baby brute and piglin share a single editable rig in the previous assets."""
    name = ("幼年" if library else "") + "白珀生物 · piglin_brute"
    root = bpy.data.collections[name]
    entries = [root]
    if library:
        entries.extend(c for c in bpy.data.collections if c.name.startswith("BL.baby_piglin_brute."))
    # Native append remaps the complete dependency graph, including drivers, GN object
    # sockets and bone constraints that are not discoverable from parenting alone.
    staged = scratch / ("standalone_brute_" + ("library" if library else "source") + "_test.blend")
    bpy.data.libraries.write(str(staged), set(entries), compress=True)
    names = [c.name for c in entries]
    owners = [cast(Any, bpy.context.scene).collection, *bpy.data.collections]
    parents = {c: [parent for parent in owners if parent not in entries and parent.children.get(c.name) == c]
               for c in entries}
    for i, c in enumerate(entries):
        c.name = "原始幼年蛮兵模板 " + str(i)
    with cast(Any, bpy.data.libraries.load(str(staged))) as (_, incoming):
        incoming.collections = list(names)
    for old, into, name in zip(entries, cast(list[bpy.types.Collection], incoming.collections), names):
        assert into.name == name, (name, into.name)
        for parent in parents[old]:
            parent.children.link(into)
            parent.children.unlink(old)
    bpy.data.batch_remove(ids=entries)
    special = bpy.data.collections[names[0]]
    rig = find_rig(special)
    regular = bpy.data.collections[("幼年" if library else "") + "白珀生物 · piglin"]
    if rig == find_rig(regular):
        raise ValueError("幼年猪灵与蛮兵的可编辑骨架仍存在共享")
    # An indirectly appended parent empties collection may otherwise retain an absolute
    # library path. Appended data used by these local authoring projects must be local.
    assert rig.library is None and rig.data.library is None


def find_rig(collection) -> Any:
    matches = [obj for obj in collection.all_objects if obj.type == "ARMATURE" and obj.get(RIG_TAG)]
    if len(matches) != 1:
        raise ValueError((collection.name, [o.name for o in matches]))
    return matches[0]


def official_weights(obj, rig, side):
    # All weighted vertices in the human source's native L/R官方 legs follow this
    # original IK binding bone. Following the local Z midpoint tears the knee.
    bone = side + ".小腿-官方版IK-绑定"
    pose = rig.pose.bones[bone]
    if not pose.bone.use_deform or not any(c.type == "IK" and c.subtarget == side + ".脚控制器" for c in pose.constraints):
        raise ValueError((rig.name, bone, "缺少官方腿 IK 绑定"))
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    for mod in list(obj.modifiers):
        if mod.type in {"ARMATURE", "LATTICE", "CORRECTIVE_SMOOTH", "NODES"}:
            obj.modifiers.remove(mod)
    obj.vertex_groups.clear()
    group = obj.vertex_groups.new(name=bone)
    group.add(range(len(obj.data.vertices)), 1.0, "REPLACE")
    mod = obj.modifiers.new("官方腿骨", "ARMATURE")
    mod.object = rig


def leg_visibility(obj, rig, official):
    for path in ("hide_viewport", "hide_render"):
        anim = obj.animation_data
        old = anim.drivers.find(path) if anim else None
        if old and old.driver.type != "SCRIPTED":
            raise ValueError((obj.name, path, old.driver.type))
        expr = old.driver.expression if old else str(int(getattr(obj, path)))
        driver = obj.driver_add(path).driver
        driver.type = "SCRIPTED"
        var = driver.variables.new()
        var.name = "bl_leg_mode"
        var.targets[0].id = rig
        var.targets[0].data_path = '["blockloom_official"]'
        driver.expression = "(" + expr + ") or " + ("not bl_leg_mode" if official else "bl_leg_mode")
        if len(driver.expression) >= 255:
            raise ValueError((obj.name, path, "腿部可见性驱动超长"))


def prepare_legs(root, rig, library):
    if library:
        group = bpy.data.collections["BL." + root["blockloom_creature_key"] + ".OFFICIAL"]
        objects = tuple(group.objects)
    else:
        objects = tuple(root.all_objects)
    count = 0
    for obj in objects:
        side = {"left_leg": "L", "right_leg": "R"}.get(obj.get("oxal_humanoid_role"))
        if obj.type != "MESH" or not side or not obj.get("oxal_humanoid_part"):
            continue
        if not library:
            official = obj.copy()
            official.data = obj.data.copy()
            official.name = "官方腿 · " + obj.name
            root.objects.link(official)
            leg_visibility(official, rig, True)
            leg_visibility(obj, rig, False)
        else:
            official = obj
        official_weights(official, rig, side)
        count += 1
    if count < 2:
        raise ValueError((root.name, "缺少左右腿"))
    rig["blockloom_official"] = False
    rig.id_properties_ui("blockloom_official").update(description="官方动画腿型")


def double_sided():
    for material in bpy.data.materials:
        for attr in ("use_backface_culling", "use_backface_culling_shadow", "use_backface_culling_lightprobe_volume"):
            setattr(material, attr, False)


def convert_53(source, output):
    """Save a native 5.3 authoring copy with double-sided probe settings."""
    if bpy.app.version[:2] != (5, 3):
        raise ValueError("转换请使用 Blender 5.3")
    source = source.resolve(strict=True)
    output = output.resolve()
    if output.suffix.lower() != ".blend" or output == source or output.exists():
        raise ValueError("输出必须为新的 .blend 文件，不能覆盖原件或已有副本")
    if not output.parent.is_dir():
        raise ValueError("输出目录不存在")
    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=True, use_scripts=False)
    if not any(o.type == "ARMATURE" and o.get("blockloom_creature_version") == VERSION
               for o in bpy.data.objects):
        raise ValueError("输入工程中找不到 v" + VERSION + " 生物骨架")
    double_sided()
    cast(Any, bpy.context.preferences).filepaths.save_version = 0
    # Keep the temporary file alongside the destination; Blender's relative image
    # paths must resolve from precisely this directory. Publish without overwriting.
    with tempfile.NamedTemporaryFile(suffix=".blend", dir=output.parent, delete=False) as tmp:
        stage = Path(tmp.name)
    try:
        status = cast(Any, bpy.ops).wm.save_as_mainfile(filepath=str(stage), compress=True, copy=True, relative_remap=True)
        if "FINISHED" not in status:
            raise ValueError("保存 Blender 5.3 工程失败")
        try:
            os.link(stage, output)
        except FileExistsError:
            raise
        except OSError:
            with stage.open("rb") as src, output.open("xb") as dst:
                try:
                    shutil.copyfileobj(src, dst)
                except BaseException:
                    dst.close()
                    output.unlink()
                    raise
    finally:
        stage.unlink(missing_ok=True)
    print("Blender 5.3 生物工程副本", output, flush=True)


def link_to_scene(root):
    scene = cast(Any, bpy.context.scene)
    if root not in scene.collection.children_recursive:
        scene.collection.children.link(root)


def separate_villager_overlay(root, rig):
    """Keep the biome/profession overlay clear of the animated eyelid surface."""
    overlay, = [obj for obj in root.all_objects if obj.get("oxal_humanoid_overlay_only")
                and "动态遮挡" in obj.name]
    mesh = overlay.data
    if mesh.get("blockloom_villager_overlay_clearance"):
        return
    offset = overlay.matrix_world.to_3x3().inverted() @ (rig.matrix_world.to_3x3() @ Vector((0, -.002, 0)))
    for vertex in mesh.vertices:
        vertex.co += offset
    mesh.update()
    mesh["blockloom_villager_overlay_clearance"] = .002


def run(source, scratch, library):
    sys.path.insert(0, str(PROJECT / "tool"))
    author_face = importlib.import_module("author_face_controls").author_creature_face
    catalog = json.loads((PROJECT / "asset/creature/catalog.json").read_text("utf8"))
    language = json.loads((PROJECT / "asset/library/assets/minecraft/lang/zh_cn.json").read_text("utf8"))
    name = "creatures_0_1_2.blend" if library else source.name
    stage = scratch / "final_test"
    stage.mkdir(exist_ok=True)
    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=not library, use_scripts=False)
    cast(Any, bpy.context.preferences).filepaths.save_version = 0
    if library or source.stem == "幼年猪灵":
        localize_brute(scratch, library)
    view_layer = cast(Any, bpy.context.view_layer)
    active = view_layer.objects.active
    selected = set(bpy.context.selected_objects)
    hidden = {obj: obj.hide_get(view_layer=view_layer) for obj in view_layer.objects if obj.type == "ARMATURE"}
    targeted = []
    for entry, family in catalog.items():
        if not library and family != source.stem:
            continue
        species = entry.rsplit(" · ", 1)[1]
        root = bpy.data.collections[entry if library else "白珀生物 · " + species]
        label = "僵尸猪人" if species == "zombie_pigman" else language["entity.minecraft." + species]
        rig = find_rig(root)
        link_to_scene(root)
        for item in (root, rig, rig.data):
            item["blockloom_creature_version"] = VERSION
            # Earlier standalone authoring projects inherited human source labels.
            for legacy in ("bp_model_version", "bp_asset_version", "人模版本"):
                if legacy in item:
                    del item[legacy]
        rig.name = "白珀-" + ("幼年" if family.startswith("幼年") else "") + label + "模型"
        rig.data.name = rig.name + "骨架数据"
        prepare_legs(root, rig, library)
        author_face(rig, root)
        if species == "villager":
            separate_villager_overlay(root, rig)
        targeted.append(root)
        print("CREATURE", entry, rig.name, flush=True)
    assert len(targeted) == (len(catalog) if library else sum(f == source.stem for f in catalog.values()))
    double_sided()
    if not library:
        for obj in view_layer.objects:
            if obj.select_get(view_layer=view_layer) != (obj in selected):
                obj.select_set(obj in selected, view_layer=view_layer)
        for obj, value in hidden.items():
            obj.hide_set(value, view_layer=view_layer)
        view_layer.objects.active = active
    if library:
        # Published components are isolated library entries. Runtime links only the
        # selected variant and the current physical style/armor collections.
        entries = {c for c in bpy.data.collections if c.name.startswith("BL.")} | set(targeted)
        # The old author's folded-arm UV baking reference points to a hidden, optional
        # straight arm. The UV is already baked; retaining this editor-only pointer
        # would append the absent arm and break independent removal of the instance.
        for group in entries:
            for obj in group.objects:
                if "oxal_humanoid_skin_source" in obj:
                    del obj["oxal_humanoid_skin_source"]
        bpy.data.libraries.write(str(stage / name), cast(set[bpy.types.ID], entries), compress=True, path_remap="NONE")
    else:
        bpy.ops.wm.save_as_mainfile(filepath=str(stage / name), compress=True, relative_remap=False)
    print("STAGED", stage / name, flush=True)


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--source", required=True, type=Path)
    mode = cli.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scratch", type=Path, help="从上一版本制作新资源时使用的独立暂存目录")
    mode.add_argument("--copy-for-53", type=Path, metavar="NEW_BLEND", help="另存为新的 Blender 5.3 双面材质工程")
    cli.add_argument("--library", action="store_true")
    args = cli.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if args.copy_for_53:
        if args.library:
            cli.error("--library 只适用于 --scratch")
        convert_53(args.source, args.copy_for_53)
    else:
        run(args.source, args.scratch, args.library)
