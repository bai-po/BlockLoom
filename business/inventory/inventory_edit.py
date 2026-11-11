"""Explicit independent-inventory commands and conservative Beta migration."""

import ast
import json
import re
from pathlib import Path
from typing import Any, cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.drivers import capture as _curves
from ...util.drivers import restore as _restore_curves
from ...util.ownership import (
    owned_ids,
    prune_owned,
    release_owned,
    remove_unused_owned,
    require_no_external_users,
    require_owned,
    seal_owned,
)
from ...util.resources import (
    AppendTransaction,
    live_ids,
    resolve_scene,
    resolve_view_layer,
)
from .inventory import _driver, _validate_camera
from .inventory_native import clean_derived, driver_baseline, driver_owners, owner_roles
from .inventory_slots import HOST, resolve_inventory


def _name(value):
    return re.sub(r"\.\d{3}$", "", value or "")


def _signature(spec):
    parsed = ast.parse(spec["expression"] or "0", mode="eval")
    used = {node.id for node in ast.walk(parsed) if isinstance(node, ast.Name)}
    return (spec["type"], ast.dump(parsed), tuple((v["name"], v["type"], tuple((_name(t["id"]), t["path"], t["bone"], t["transform"], t["space"]) for t in v["targets"])) for v in spec["variables"] if v["name"] in used))


def _plain(value):
    if isinstance(value, bpy.types.ID):
        return value
    if hasattr(value, "items"):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "to_list"):
        return _plain(value.to_list())
    return value


class EditJournal:
    def __init__(self, root):
        self.root = root
        self.items = driver_owners((root, *owned_ids(root)))
        self.props = {item: {k: _plain(v) for k, v in item.items()} for item in self.items}
        self.drivers = {item: _curves(item) for item in self.items}
        self.constraints = [(owner, tuple(owner.constraints)) for item in self.items if isinstance(item, bpy.types.Object) for owner in (item, *(tuple(item.pose.bones) if item.pose else ()))]
        self.constraint_targets = {constraint: cast(Any, constraint).target for _owner, constraints in self.constraints for constraint in constraints if constraint.bl_rna.properties.get("target") is not None}
        self.object_data = {item: item.data for item in self.items if isinstance(item, bpy.types.Object)}
        self.object_slots = {obj: [(slot.link, slot.material) for slot in obj.material_slots] for obj in self.object_data}
        self.object_flags = {obj: obj.visible_shadow for obj in self.object_data}
        self.modifiers = {obj: tuple(obj.modifiers) for obj in self.object_data}
        self.modifier_targets = {modifier: cast(Any, modifier).object for modifiers in self.modifiers.values() for modifier in modifiers if modifier.type == "ARMATURE"}
        self.groups = {obj: tuple(obj.vertex_groups) for obj in self.object_data}
        self.bones = {bone: (bone.hide, bone.hide_select, bone.use_deform) for item in self.items if isinstance(item, bpy.types.Armature) for bone in item.bones}
        self.collections = {collection: (collection.is_visible, collection.is_solo) for item in self.items if isinstance(item, bpy.types.Armature) for collection in item.collections_all}

    def restore(self):
        for obj, data in self.object_data.items():
            obj.data = data
        for obj, slots in self.object_slots.items():
            for slot, (link, material) in zip(obj.material_slots, slots, strict=True):
                slot.link = cast(Any,link)
                slot.material = material
            obj.visible_shadow = self.object_flags[obj]
            for modifier in tuple(obj.modifiers):
                if modifier not in self.modifiers[obj]:
                    obj.modifiers.remove(modifier)
            for group in reversed(tuple(obj.vertex_groups)):
                if group not in self.groups[obj]:
                    obj.vertex_groups.remove(group)
        for modifier, target in self.modifier_targets.items():
            cast(Any, modifier).object = target
        for bone, flags in self.bones.items():
            bone.hide, bone.hide_select, bone.use_deform = flags
        for collection, flags in self.collections.items():
            collection.is_visible, collection.is_solo = flags
        for item in live_ids(self.items):
            for name in tuple(item.keys()):
                if name not in self.props[item]:
                    del item[name]
            for name, value in self.props[item].items():
                if _plain(item.get(name)) != value:
                    item[name] = value
            _restore_curves(item, self.drivers[item])
        for owner, original in self.constraints:
            for constraint in tuple(owner.constraints):
                if constraint not in original:
                    owner.constraints.remove(constraint)
        for constraint, target in self.constraint_targets.items():
            cast(Any, constraint).target = target


def _legacy_allowed(root):
    metadata = json.loads((Path(__file__).resolve().parents[2] / "asset/inventory/DERIVED.json").read_text("utf-8"))
    baseline = {(s["role"], s["path"], s["index"]): s for s in metadata["legacy_baseline"]}
    actual = driver_baseline(owned_ids(root))
    owners = {role: owner for owner, role in owner_roles(owned_ids(root)).items()}
    allowed = set()
    for spec in actual:
        contract = baseline.get((spec["role"], spec["path"], spec["index"]))
        if contract is None:
            continue  # Preserve unrelated user-authored native animation.
        if _signature(spec) != _signature(contract):
            raise ValueError("原生驱动已被用户修改，未覆盖：" + spec["owner"] + " / " + spec["path"])
        allowed.add((owners[spec["role"]], spec["path"], spec["index"]))
    return allowed


def _follow(root, reference):
    camera = root.get("独立物品栏相机")
    if camera is None:
        raise ValueError("实例缺少私有相机")
    require_owned(root, (camera, camera.data))
    _validate_camera(reference)
    visited, pending = set(), [reference]
    while pending:
        item = pending.pop()
        if item in visited:
            continue
        if item == camera:
            raise ValueError("参考相机存在循环跟随，未绑定")
        visited.add(item)
        if item.parent:
            pending.append(item.parent)
        pending.extend(target for c in item.constraints if (target := getattr(c, "target", None)) is not None)
    if reference.data == camera.data:
        raise ValueError("参考相机不能是库存私有相机或其子级")
    name = "OXAL · 参考相机原生跟随"
    constraint = camera.constraints.get(name)
    old_reference = root.get("独立物品栏参考相机")
    if constraint and (constraint.type != "COPY_TRANSFORMS" or constraint.target != old_reference or constraint.owner_space != "WORLD" or constraint.target_space != "WORLD" or constraint.mix_mode != "REPLACE" or constraint.mute or abs(constraint.influence - 1) > 1e-7):
        raise ValueError("现有相机跟随约束与目标冲突，未覆盖")
    fields = ("lens", "sensor_width", "sensor_height", "shift_x", "shift_y", "clip_start", "clip_end")
    from ..human.human_armor import animated
    if constraint is None and any(animated(camera, field) for field in ("location", "rotation_euler", "rotation_quaternion", "scale")):
        raise ValueError("私有相机已有用户变换动画，未覆盖参考绑定")
    scale_name = "OXAL · 相机尺度归一"
    normalization = camera.constraints.get(scale_name)
    if normalization and (normalization.type != "LIMIT_SCALE" or normalization.owner_space != "WORLD" or any(not getattr(normalization, "use_" + bound + "_" + axis) or abs(getattr(normalization, bound + "_" + axis) - 1.0) > 1e-7 for axis in ("x", "y", "z") for bound in ("min", "max"))):
        raise ValueError("私有相机尺度约束已修改，未覆盖")
    for field in fields:
        if animated(camera.data, field):
            existing = next((f for f in camera.data.animation_data.drivers if f.data_path == field), None)
            expected = old_reference.data if old_reference is not None and constraint is not None else reference.data
            if existing is None or existing.driver.expression != "value" or len(existing.driver.variables) != 1 or existing.driver.variables[0].targets[0].id != expected or existing.driver.variables[0].targets[0].data_path != field:
                raise ValueError("私有相机已有用户投影动画，未覆盖：" + field)
    if constraint is None:
        constraint = camera.constraints.new("COPY_TRANSFORMS")
        constraint.name = name
    constraint.target = reference
    if normalization is None:
        normalization = camera.constraints.new("LIMIT_SCALE")
        normalization.name = scale_name
        normalization.owner_space = "WORLD"
        for axis in ("x", "y", "z"):
            for bound in ("min", "max"):
                setattr(normalization, "use_" + bound + "_" + axis, True)
                setattr(normalization, bound + "_" + axis, 1.0)
    for field in fields:
        _driver(camera.data, field, "value", [("value", reference.data, field)])
    root["独立物品栏参考相机"] = reference


class InventoryCommands:
    def edit(self, scene_uid, options):
        ensure_mutation_allowed()
        scene = resolve_scene(scene_uid)
        layer = resolve_view_layer(scene, require_active=True)
        root, rig = resolve_inventory(scene, str(options.get("instance_id", "")))
        action = str(options.get("action", ""))
        if action == "select":
            for obj in tuple(bpy.context.selected_objects):
                obj.select_set(False)
            rig.hide_set(False)
            rig.select_set(True)
            layer.objects.active = rig
            scene["oxal_mc_studio_active_inventory"] = root
            return "已选择独立物品栏"
        if action == "remove":
            if any(obj.get(HOST) == root for obj in scene.objects):
                raise ValueError("物品栏仍有格子资产绑定，请先解除绑定")
            retiring = (root, *root.children_recursive, *root.all_objects)
            require_owned(root, retiring)
            require_no_external_users(scene,root,retiring,scene_properties={"oxal_mc_studio_active_inventory":root})
            released = release_owned(root)
            if scene.get("oxal_mc_studio_active_inventory") == root:
                del scene["oxal_mc_studio_active_inventory"]
            bpy.data.batch_remove(ids=live_ids(retiring))
            remove_unused_owned(released)
            return "已移除独立物品栏"
        if action not in {"repair", "hands", "camera"}:
            raise ValueError("未知物品栏操作")
        require_owned(root, (root, rig, rig.data))
        reference = None
        camera_uid = str(options.get("camera_uid", ""))
        if camera_uid:
            reference = next((obj for obj in scene.objects if str(obj.session_uid) == camera_uid and obj.type == "CAMERA"), None)
            if reference is None:
                raise ValueError("参考相机已删除或不属于当前场景")
        if action == "camera" and reference is None:
            raise ValueError("请明确选择参考相机")
        allowed = _legacy_allowed(root) if action == "repair" and int(root.get("独立物品栏版本", 0)) == 1 else None
        journal = EditJournal(root)
        asset = Path(__file__).resolve().parents[2] / "asset/inventory/oxal_inventory_hands.blend"
        restored = False
        def rollback():
            nonlocal restored
            if not restored:
                journal.restore()
                restored = True
        with AppendTransaction(asset) as tx:
            try:
                if action == "repair" and allowed is not None:
                    clean_derived(owned_ids(root), allowed=allowed)
                    from ..human.human_armor import animated
                    for name, before, after in (("HUD宽度占比", .55, .2854), ("HUD底边距", .035, .001472)):
                        if name in rig.data and abs(float(rig.data[name]) - before) < 1e-7 and not animated(rig.data, '["' + name + '"]'):
                            rig.data[name] = after
                if reference is not None and action in {"repair", "camera"}:
                    _follow(root, reference)
                from .inventory_hands import native_channels
                native_channels(rig)
                if root.get("独立视角") == "FIRST_PERSON" and action in {"repair", "hands"}:
                    from .inventory_hands import prepare
                    prepare(root, rig, tx, enabled=bool(options.get("enabled", True)), arm_mode=str(options.get("arm_mode", "SLIM")), skin_filepath=str(options.get("skin_filepath", "")), rollback=rollback)
                if "oxal_inventory_rig_id" not in rig:
                    from ...util.resources import INSTANCE
                    rig["oxal_inventory_rig_id"] = str(root[INSTANCE]) + ":inventory-rig"
                root["独立物品栏版本"] = 2
                root["oxal_inventory_native_schema"] = 2
                seal_owned(root, tx.owned)
                tx.commit()
            except Exception:
                # Detach existing IDs from temporary data before native cleanup.
                rollback()
                raise
        prune_owned(root)
        rig.update_tag()
        return {"repair":"已修复独立物品栏", "hands":"已应用第一人称手部", "camera":"已绑定参考相机"}[action]
