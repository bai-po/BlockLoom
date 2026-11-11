"""Flat Scene ID-property inputs remain native after RNA classes are removed."""

import json
from functools import lru_cache
from typing import Any

import bpy

from ...util.animation import action_fcurves
from .sky_schema import (
    CLOUD_STRUCTURE_KEYS,
    PARAMETERS,
    RANGES,
    SOFT_RANGES,
    SPEED_ITEMS,
)

PREFIX = "oxal_mc_studio_sky_input__"
VALUES = "oxal_mc_studio_sky_values"
SLOTS = "oxal_mc_studio_sky_slots"


def key(name, index=None):
    return PREFIX + ("base" if index is None else "slot" + str(index)) + "__" + name


def path(name, index=None):
    return '[' + json.dumps(key(name,index)) + ']'


@lru_cache(maxsize=1)
def managed_keys():
    fields = (*PARAMETERS,"preset_slot","preset_keyframes")
    return frozenset((*[key(name) for name in fields],
                      *[key(name,index) for index in range(10) for name in (*fields,"filled")]))


def group_index(group):
    # RNA path discovery searches the owner's properties, including unrelated
    # add-on collections. Compare only our Scene's known groups instead. Do not
    # cache pointers: removing slots, Undo or loading a file changes their IDs.
    scene = group.id_data
    if not isinstance(scene, bpy.types.Scene):
        raise ValueError("天空参数不是当前场景的已知保存值")  # noqa: TRY004 -- Preserve unknown-group failure semantics.
    pointer = group.as_pointer()
    if pointer == getattr(scene, VALUES).as_pointer():
        return None
    for index, slot in enumerate(getattr(scene, SLOTS)):
        if index >= 10:
            break
        if pointer in (slot.as_pointer(), slot.values.as_pointer()):
            return index
    raise ValueError("天空参数不是当前场景的已知保存值")


def getter(name, default):
    def read(group):
        return group.id_data.get(key(name,group_index(group)),group.get(name,default))
    return read


def setter(name):
    def write(group, value):
        group[name] = value
        field = key(name,group_index(group))
        if field in group.id_data:
            group.id_data[field] = value
    return write


def configure(scene, name, value, index=None):
    field = key(name,index)
    existing = field in scene
    scene[field] = value
    caption,default,_ = PARAMETERS.get(name,(name,value,name))
    settings: dict[str, Any] = {"description":caption}
    if not existing:
        settings["default"] = value
    if isinstance(default,str):
        settings["items"] = list(SPEED_ITEMS)
    elif not isinstance(default,bool):
        if name == "preset_slot":
            low,high = 1,10
        else:
            low,high = RANGES[name]
        soft_low,soft_high = SOFT_RANGES.get(name,(low,high))
        settings.update(min=low,max=high,soft_min=soft_low,soft_max=soft_high)
    scene.id_properties_ui(field).update(**settings)


@lru_cache(maxsize=1)
def aliases():
    names = (*PARAMETERS,"preset_slot","preset_keyframes")
    result = {}
    for name in names:
        result[VALUES+'.'+name] = path(name)
        result['["'+VALUES+'"]["'+name+'"]'] = path(name)
    for index in range(10):
        result[f'{SLOTS}[{index}].filled'] = path('filled',index)
        result[f'["{SLOTS}"][{index}]["filled"]'] = path('filled',index)
        for name in names:
            result[f'{SLOTS}[{index}].values.{name}'] = path(name,index)
            result[f'["{SLOTS}"][{index}]["values"]["{name}"]'] = path(name,index)
    return result


def legacy_animation(scene):
    animation = scene.animation_data
    if animation is None:
        return False
    bindings = [animation]
    strips = [strip for track in animation.nla_tracks for strip in track.strips]
    while strips:
        strip = strips.pop()
        bindings.append(strip)
        strips.extend(getattr(strip,"strips",()))
    return any(curve.data_path in aliases() for binding in bindings
               for curve in action_fcurves(binding.action,slot=getattr(binding,"action_slot",None)))


def migration_plan(scene, allowed_targets=()):
    """Read-only channel validation; preserve Actions, slots and nested NLA."""
    mapping = aliases()
    allowed = {target.as_pointer() for target,_ in allowed_targets}
    users = bpy.data.user_map(subset=(scene,)).get(scene,set())
    checked = set()
    for user in (scene,*users):
        for owner in (user,getattr(user,"data",None),getattr(user,"node_tree",None)):
            if owner is None or owner.as_pointer() in checked:
                continue
            checked.add(owner.as_pointer())
            animation = getattr(owner,"animation_data",None)
            for curve in animation.drivers if animation else ():
                if owner == scene and curve.data_path in mapping:
                    raise ValueError("天空旧参数含用户场景驱动，未迁移其绑定")
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if target.id == scene and target.data_path in mapping and target.as_pointer() not in allowed:
                            raise ValueError("旧天空参数被未知用户驱动引用，未改写或迁移："+owner.name)
    animation = scene.animation_data
    if animation is None:
        return []
    def nested(strips):
        for strip in strips:
            yield strip
            yield from nested(getattr(strip,"strips",()))
    bindings = [animation,*[strip for track in animation.nla_tracks for strip in nested(track.strips)]]
    result,seen,channels = [],set(),{}
    for binding in bindings:
        action = binding.action
        slot = getattr(binding,"action_slot",None)
        if action is None or (action.as_pointer(),getattr(slot,"handle",None)) in seen:
            continue
        seen.add((action.as_pointer(),getattr(slot,"handle",None)))
        curves = action_fcurves(action,slot=slot)
        for curve in curves:
            target = mapping.get(curve.data_path,curve.data_path)
            if target.startswith('["'+PREFIX):
                channels.setdefault((target,curve.array_index),set()).add(curve.data_path)
        updates = [(curve,mapping[curve.data_path]) for curve in curves if curve.data_path in mapping]
        if not updates:
            continue
        # More than this Scene's actual bindings is a shared user Action.
        local_users = sum(candidate.action == action for candidate in bindings)
        if action.library or action.override_library or action.users-int(action.use_fake_user)>local_users:
            raise ValueError("天空动画Action只读或被其它使用者共享，未迁移")
        destinations = {}
        for curve in curves:
            target = mapping.get(curve.data_path,curve.data_path)
            signature = target,curve.array_index
            if signature in destinations:
                raise ValueError("天空动画同时存在旧路径与持久路径，未覆盖Action/NLA")
            destinations[signature] = curve
        for curve,target in updates:
            if any(target == path(name,index) for name in CLOUD_STRUCTURE_KEYS for index in (None,*range(10))):
                raise ValueError("天空结构参数已有动画，未迁移或执行结构修改")
            result.append((curve,curve.data_path,target))
    if any(len(paths)>1 for paths in channels.values()):
        raise ValueError("天空Action/NLA之间同时存在旧路径与持久路径，未迁移动画")
    return result


def apply_migration(plan, restore=False):
    for curve,before,after in plan:
        curve.data_path = before if restore else after
