"""Upgrade one selected, managed creature while keeping the original collection intact."""

import re
from typing import cast

import bpy
from bpy.types import ViewLayer

from ...util.ownership import require_owned
from ...util.resources import INSTANCE
from ..human.human import BINDING, _constraint_animated
from ..human.human_compat import LegacyHuman
from ..human.human_compat_controls import _foot_value, _leg_value
from . import creatures
from .creature_parts import ARMOR, option_value


def _entry(root):
    for name in creatures.catalog():
        species = name.rsplit(" · ", 1)[-1]
        key = ("baby_" if name.startswith("幼年") else "") + species
        if key == root.get("blockloom_creature_key"):
            return name
    fallback = re.sub(r"\.\d+$", "", root.name)
    if fallback in creatures.catalog():
        return fallback
    raise ValueError("找不到此旧生物的原始目录身份")


def _style_values(root, rig):
    if root.get("blockloom_parts_version"):
        fields = ["arm_mode", "leg_mode", "name"]
        if rig.get("oxal_humanoid_can_fold"):
            fields.append("player_arms")
        if rig.get("oxal_humanoid_armor_supported"):
            fields.extend("armor_" + slot for slot in sorted(ARMOR))
        values = {field: option_value(root, rig, field) for field in fields}
    else:
        values = {
            "arm_mode": "SLIM" if rig.get("blockloom_slim") else "WIDE",
            "leg_mode": "OFFICIAL" if rig.get("blockloom_official") else "NORMAL",
            "name": bool(rig.get("blockloom_name")),
        }
        if rig.get("oxal_humanoid_can_fold"):
            values["player_arms"] = bool(rig.get("oxal_humanoid_player_arms"))
        if rig.get("oxal_humanoid_armor_supported"):
            for slot in sorted(ARMOR):
                values["armor_" + slot] = bool(rig.get("oxal_humanoid_armor_" + slot))
    target = LegacyHuman(rig, "", tuple(root.all_objects), (root,), True, "")
    for name, value in (("leg_fixed", _leg_value(target)), ("foot_fixed", _foot_value(target))):
        if value is not None:
            values[name] = value
    return values


def upgrade(scene, root, rig, options):
    if not creatures.needs_upgrade(root):
        raise ValueError("所选生物已使用新版资源")
    if bpy.context.mode != "OBJECT":
        raise ValueError("请先切换到物体模式，再升级所选生物")
    if root.get("blockloom_upgraded_to") in {r[INSTANCE] for r in creatures.instances(scene)}:
        raise ValueError("该旧实例已有升级副本")
    require_owned(root, (rig, rig.data))
    internal_parent = rig.parent is not None and rig.parent in set(root.all_objects)
    if rig.parent is not None and internal_parent and (rig.parent.animation_data or rig.animation_data and
                            (rig.animation_data.action or rig.animation_data.nla_tracks)):
        raise ValueError("旧骨架的父对象与动画相互依赖，无法独立升级")
    name = _entry(root)
    values = _style_values(root, rig)
    attached = []
    for obj in scene.objects:
        c = obj.constraints.get(BINDING)
        if c and c.type == "CHILD_OF" and c.target == rig:
            if obj.library or obj.override_library or tuple(obj.users_scene) != (scene,) or _constraint_animated(obj, c):
                raise ValueError("物品绑定为共享、只读或含约束动画，无法安全转移：" + obj.name)
            attached.append(c)
    old_flags = (root.hide_viewport, root.hide_render)
    previous = root.get("blockloom_upgraded_to")
    selected = tuple(bpy.context.selected_objects)
    layer = cast(ViewLayer, bpy.context.view_layer)
    active = layer.objects.active
    result = creatures.load(scene, {**options, "creature": name, "position": list(rig.matrix_world.translation)},
                            source_rig=rig, bake_parent=internal_parent)
    new_root = next(c for c in creatures.instances(scene) if c[INSTANCE] == options["creation_id"])
    new_rig = new_root[creatures.RIG]
    try:
        for field, value in values.items():
            creatures._style(scene, new_root, new_rig, field, value)
        for prop, value in rig.items():
            if (prop.startswith(("oxal_humanoid_", "blockloom_heat")) and isinstance(value, (float, int, bool))
                    and prop in new_rig and isinstance(new_rig[prop], (float, int, bool))):
                new_rig[prop] = value
        for c in attached:
            if c.subtarget not in new_rig.pose.bones:
                raise ValueError("新版缺少原物品绑定控制骨：" + c.subtarget)
            c.target = new_rig
        root.hide_viewport = True
        root.hide_render = True
        root["blockloom_upgraded_to"] = new_root[INSTANCE]
    except Exception:
        for c in attached:
            c.target = rig
        root.hide_viewport, root.hide_render = old_flags
        if previous is not None:
            root["blockloom_upgraded_to"] = previous
        else:
            root.pop("blockloom_upgraded_to", None)
        creatures.edit(scene, {"action": "CREATURE_REMOVE", "instance": new_root[INSTANCE]})
        for obj in selected:
            obj.select_set(True)
        layer.objects.active = active
        raise
    skipped = result["skipped_curves"]
    return {"message": f"已升级{creatures.display_name(name)}至 v{creatures.CREATURE_VERSION}，旧合集已隐藏；"
                       f"复制 {result['copied_actions']} 个动作，转移 {len(attached)} 个物品绑定"
                       + ("。保留在旧动作中的通道：" + "、".join(result["skipped_paths"]) if skipped else "")}
