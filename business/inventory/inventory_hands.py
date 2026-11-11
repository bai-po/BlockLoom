"""Vanilla empty-right-arm presentation on the original native inventory bones."""

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import bpy
from mathutils import Matrix

from ...util.resources import AppendTransaction
from .inventory import _driver, _property
from .inventory_native import control_key, control_path

ANCHOR = "oxal_inventory_hand_anchor"
HAND = "oxal_inventory_empty_right_arm"
UNIT = .05 / 1.4


def _rest_arm():
    # Minecraft first-person arm placement: Y-up model pixels in camera space.
    def rotate(axis, degrees):
        return Matrix.Rotation(math.radians(degrees), 4, axis)
    return (Matrix.Translation((.64, -.6, -.72)) @ rotate("Y", 45)
            @ Matrix.Translation((-1, 3.6, 3.5)) @ rotate("Z", 120)
            @ rotate("X", 200) @ rotate("Y", -135)
            @ Matrix.Translation((5.6, 0, 0))
            @ Matrix.Diagonal((1 / 16, 1 / 16, 1 / 16, 1))
            @ Matrix.Translation((-5, 2, 0)))


def native_channels(rig):
    from .inventory_slots import quantity_path
    collection = rig.data.collections.get("物品栏位置")
    if collection:
        collection.is_visible = False
    status = rig.data.collections.get("状态栏控制器")
    if status:
        for item in tuple(rig.data.collections_all):
            item.is_solo = item == status
        status.is_visible = True
    for name in (*(str(i) + "号物品栏" for i in range(1, 10)), "物品栏副手"):
        rig.data.bones[name].hide = False
        rig.data.bones[name].hide_select = False
    for slot in (*range(1, 10), "OFFHAND"):
        name = str(slot)
        if "oxal_inventory_bound_" + name not in rig:
            _property(rig, "oxal_inventory_bound_" + name, False)
        occupied = "oxal_inventory_occupied_" + name
        if occupied not in rig:
            _property(rig, occupied, False)
            _driver(rig, '["' + occupied + '"]', "bound and count>0", [("bound", rig, '["oxal_inventory_bound_' + name + '"]'), ("count", rig, quantity_path(name))])
    if "oxal_inventory_selected_slot" not in rig:
        _property(rig, "oxal_inventory_selected_slot", 1, 1, 9)
        _driver(rig, '["oxal_inventory_selected_slot"]', "min(9,max(1,round(-v*8/2.25)+1))", [("v", rig, 'pose.bones["物品栏控制器"].location[2]')])
    if "oxal_inventory_main_occupied" not in rig:
        _property(rig, "oxal_inventory_main_occupied", False)
        variables = [("s", rig, '["oxal_inventory_selected_slot"]')]
        variables += [("b" + str(i), rig, '["oxal_inventory_occupied_' + str(i) + '"]') for i in range(1, 10)]
        expression = "".join("b" + str(i) + " if s<" + str(i + .5) + " else " for i in range(1, 9)) + "b9"
        _driver(rig, '["oxal_inventory_main_occupied"]', expression, variables)


def prepare(root, rig, tx, *, enabled=True, arm_mode="SLIM", skin_filepath="", rollback: Callable[[], None] | None = None):
    if arm_mode not in {"SLIM", "WIDE"}:
        raise ValueError("请选择3格或4格手臂")
    camera = root.get("独立物品栏相机")
    if camera is None or camera.type != "CAMERA":
        raise ValueError("第一人称手部需要实例自己的相机")
    old = root.get(HAND)
    if old is not None:
        from ...util.ownership import require_owned
        require_owned(root, (old, old.data))
        if enabled and (old.get("oxal_inventory_arm_mode") != arm_mode or skin_filepath) and rollback is None:
            raise ValueError("既有手臂替换缺少发布回滚记录，未修改对象")
    native_channels(rig)
    enabled_key = control_key(rig,"oxal_inventory_hands_enabled")
    if enabled_key not in rig:
        _property(rig, enabled_key, enabled)
    else:
        rig[enabled_key] = enabled
    anchor = root.get(ANCHOR)
    if anchor is None:
        anchor = bpy.data.objects.new("第一人称手持锚点", None)
        tx.track(anchor)
        root.objects.link(anchor)
        anchor.parent = camera
        anchor.matrix_parent_inverse = Matrix.Identity(4)
        root[ANCHOR] = anchor
        for axis in range(3):
            expression = "max(1,near*30)" if axis == 2 else "max(1,near*30)*height/(depth*" + repr(2 * math.tan(math.radians(70) / 2)) + ")"
            _driver(anchor, "scale", expression, [("near", camera.data, "clip_start"), ("height", rig.data, '["投影高度"]'), ("depth", rig.data, '["HUD有效深度"]')], axis)
        bone = rig.pose.bones["第一人称手持根"]
        constraint = bone.constraints.new("COPY_TRANSFORMS")
        constraint.name = "OXAL · 原生第一人称手持根"
        constraint.target = anchor
        constraint.owner_space = "WORLD"
        constraint.target_space = "WORLD"
    if old is not None and old.get("oxal_inventory_arm_mode") == arm_mode and not skin_filepath:
        return old
    if not enabled:
        return None
    file = Path(__file__).resolve().parents[2] / "asset/inventory/oxal_inventory_hands.blend"
    mesh_name = "玩家细手右臂" if arm_mode=="SLIM" else "玩家粗手右臂"
    with AppendTransaction(file) as append:
        _unused, _worlds, meshes = append.append_bundle("独立物品栏原生手臂资源", meshes=(mesh_name,))
        mesh = meshes[mesh_name]
        if mesh not in append.owned:
            mesh = mesh.copy()
            append.track(mesh)
        if old is not None:
            materials = [slot.material for slot in old.material_slots]
            mesh.materials.clear()
            for material in materials:
                mesh.materials.append(material)
        mesh.transform(Matrix.Diagonal((2 * UNIT, 2 * UNIT, 2 * UNIT, 1)) @ _rest_arm())
        # Decode and prepare private materials without exposing any temporary
        # Mesh to an existing Object. Late publication still has a rollback;
        # pre-validating a PNG alone cannot protect native cleanup failures.
        if skin_filepath:
            from ..human.human import _png
            image = bpy.data.images.load(str(_png(skin_filepath, skin=True)), check_existing=False)
            append.track(image)
            image.pack()
            for index, original in enumerate(tuple(mesh.materials)):
                if original:
                    material = original.copy()
                    append.track(material)
                    mesh.materials[index] = material
                    for node in cast(Any, material.node_tree).nodes:
                        if node.type == "TEX_IMAGE":
                            node.image = image
        try:
            if old is None:
                obj = bpy.data.objects.new("第一人称 · 空手右臂", mesh)
                append.track(obj)
                root.objects.link(obj)
                obj.parent = rig
                obj.matrix_parent_inverse = Matrix.Identity(4)
                obj.matrix_basis = Matrix.Identity(4)
            else:
                links = [slot.link for slot in old.material_slots]
                obj = old
                obj.data = mesh
                for index, link in enumerate(links):
                    obj.material_slots[index].link = link
                    obj.material_slots[index].material = mesh.materials[index]
            group = obj.vertex_groups.get("物品栏右手") or obj.vertex_groups.new(name="物品栏右手")
            group.add(list(range(len(mesh.vertices))), 1.0, "REPLACE")
            modifier = next((m for m in obj.modifiers if m.type == "ARMATURE"), None) or obj.modifiers.new("原生手臂变形", "ARMATURE")
            cast(Any, modifier).object = rig
            rig.data.bones["物品栏右手"].use_deform = True
            obj["oxal_inventory_arm_mode"] = arm_mode
            obj["oxal_inventory_hand_schema"] = 1
            obj.visible_shadow = False
            root[HAND] = obj
            for field in ("hide_viewport", "hide_render"):
                _driver(obj, field, "not enabled or occupied", [("enabled", rig, control_path(rig,"oxal_inventory_hands_enabled")), ("occupied", rig, '["oxal_inventory_main_occupied"]')])
            append.prune_unused()
            for item in tuple(append.owned):
                tx.track(item)
            append.commit()
        except Exception:
            # This runs before the inner AppendTransaction frees its Mesh.
            # Restoring only in the caller's outer except is too late: Blender
            # may free the existing Object while unlinking its temporary data.
            if rollback is not None:
                rollback()
            raise
    return obj
