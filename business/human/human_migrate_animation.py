"""Copy compatible native animation into a newly appended human rig.

This module never edits the source or creates shared Action bindings. The
caller owns the append transaction and its rollback/Undo boundary.
"""

import ast
import re
from collections.abc import Mapping
from typing import Any

import bpy

from ...util.animation import action_fcurves

_TRANSFORMS = frozenset({
    "location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle",
    "scale", "delta_location", "delta_rotation_euler", "delta_rotation_quaternion",
    "delta_scale",
})
_BONE_TRANSFORMS = frozenset({
    "location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale",
})
_BONE_PATH = re.compile(r'^pose\.bones\[("(?:[^"\\]|\\.)*")\](.*)$')
_PROPERTY_PATH = re.compile(r'^\[("(?:[^"\\]|\\.)*")\]$')
_STRIP_SETTINGS = (
    "extrapolation", "blend_type", "use_reverse", "use_sync_length",
    "influence", "strip_time", "use_animated_time_cyclic", "mute", "select",
)
_SCALARS = (bool, int, float, str)
_ACTION_FIELDS = ("name", "use_frame_range", "use_cyclic", "frame_start", "frame_end", "frame_range", "id_root")
_SLOT_FIELDS = ("identifier", "target_id_type", "name_display", "handle")
_ANIMATION_SETTINGS = ("action_extrapolation", "action_blend_type", "action_influence", "use_nla")
_STRIP_TIMING = ("action_frame_start", "action_frame_end", "repeat", "scale", "frame_start", "frame_end")
_NATIVE_PRIMITIVES = frozenset({"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"})
_FINGERPRINT_EXCLUDED = frozenset({"rna_type", "id_data", "is_valid", "co_ui"})


def _rig(value: Any) -> None:
    if not isinstance(value, bpy.types.Object) or value.type != "ARMATURE" or value.pose is None:
        raise ValueError("动画迁移需要有效的主骨架")


def _bindings(source: Any) -> tuple[Any, ...]:
    animation = source.animation_data
    if animation is None:
        return ()
    return (
        animation,
        *(strip for track in animation.nla_tracks for strip in track.strips),
    )


def _slot(binding: Any) -> Any:
    action = binding.action
    slot = getattr(binding, "action_slot", None)
    if (action is not None and getattr(action, "slots", ())
            and (slot is None or slot not in tuple(action.slots))):
        raise ValueError("旧人模 Action 的绑定槽位缺失，未猜测动画所属对象")
    return slot


def _check_values(value: Any) -> None:
    if isinstance(value, bpy.types.ID):
        raise TypeError("旧人模 Action 自定义数据含 ID 引用，无法安全复制")
    if isinstance(value, Mapping) or hasattr(value, "items"):
        for part in value.values():
            _check_values(part)
    elif isinstance(value, (tuple, list)):
        for part in value:
            _check_values(part)


def _check_modifier_references(modifier: Any, visited: set[int]) -> None:
    pointer = modifier.as_pointer()
    if pointer in visited:
        return
    visited.add(pointer)
    for prop in modifier.bl_rna.properties:
        if prop.identifier in {"rna_type", "id_data"} or prop.type not in {"POINTER", "COLLECTION"}:
            continue
        value = getattr(modifier, prop.identifier)
        for item in tuple(value) if prop.type == "COLLECTION" else (value,):
            if isinstance(item, bpy.types.ID):
                raise TypeError("旧人模动画修改器引用其它数据，无法安全重映射")
            if item is not None and hasattr(item, "bl_rna"):
                _check_modifier_references(item, visited)


def _allowed_path(curve: Any, target: Any) -> bool:
    data_path = curve.data_path
    if data_path not in _TRANSFORMS:
        match = _BONE_PATH.fullmatch(data_path)
        if match is None:
            return False
        name = ast.literal_eval(match[1])
        bone = target.pose.bones.get(name)
        if bone is None:
            return False
        suffix = match[2]
        if suffix.startswith(".") and suffix[1:] in _BONE_TRANSFORMS:
            pass
        else:
            custom = _PROPERTY_PATH.fullmatch(suffix)
            if custom is None or not isinstance(bone.get(ast.literal_eval(custom[1])), _SCALARS):
                return False
    try:
        value = target.path_resolve(data_path)
    except (ValueError, KeyError, TypeError):
        return False
    if isinstance(value, (bool, int, float)):
        return curve.array_index == 0
    try:
        return 0 <= curve.array_index < len(value) and isinstance(value[curve.array_index], (bool, int, float))
    except (TypeError, IndexError):
        return False


def validate(source: Any, target: Any = None) -> None:
    """Reject unsupported native state before the caller mutates either model."""
    _rig(source)
    if target is not None:
        _rig(target)
        if target == source:
            raise ValueError("迁移目标必须是本次新建的人模")
    animation = source.animation_data
    if animation is not None:
        if animation.use_tweak_mode:
            raise ValueError("请先退出旧人模的 NLA 调整模式，再迁移")
        for track in animation.nla_tracks:
            for strip in track.strips:
                if strip.type != "CLIP" or strip.action is None:
                    raise ValueError("旧人模包含 NLA Meta／Transition，当前迁移仅支持 Action Clip")
                if (strip.modifiers or strip.fcurves or strip.use_animated_time
                        or strip.use_animated_influence):
                    raise ValueError("旧人模 NLA 含条带修改器或时间／权重动画，请先处理后再迁移")
    drivers = set()
    if target is not None and target.animation_data is not None:
        drivers = {(curve.data_path, curve.array_index) for curve in target.animation_data.drivers}
    checked = set()
    for binding in _bindings(source):
        action = binding.action
        if action is None:
            continue
        slot = _slot(binding)
        for curve in action_fcurves(action, slot=slot):
            for modifier in curve.modifiers:
                _check_modifier_references(modifier, set())
            if (target is not None and _allowed_path(curve, target)
                    and (curve.data_path, curve.array_index) in drivers):
                raise ValueError(f"旧人模关键帧与新版原生驱动冲突：{curve.data_path}")
        if action in checked:
            continue
        checked.add(action)
        _check_values(dict(action.items()))
        if any(marker.camera is not None for marker in getattr(action, "pose_markers", ())):
            raise ValueError("旧人模 Action 标记引用相机，当前迁移不复制外部引用")


def estimate(source: Any) -> dict[str, int]:
    """Return plain source counts without creating animation or evaluating frames."""
    validate(source)
    animation = source.animation_data
    bindings = {}
    for binding in _bindings(source):
        if binding.action is not None:
            slot = _slot(binding)
            bindings[(binding.action.as_pointer(), slot.identifier if slot else "")] = binding
    return {
        "bones": len(source.pose.bones),
        "curves": sum(len(action_fcurves(binding.action, slot=_slot(binding))) for binding in bindings.values()),
        "actions": len(bindings),
        "nla_tracks": len(animation.nla_tracks) if animation else 0,
        "nla_strips": sum(len(track.strips) for track in animation.nla_tracks) if animation else 0,
    }


def _plain_native(value: Any) -> Any:
    """Convert native RNA/IDProperty values without invoking registered getters."""
    if value is None or isinstance(value, _SCALARS):
        return value
    if isinstance(value, bpy.types.ID):
        raise TypeError("旧人模动画快照含未声明的 ID 引用")
    if isinstance(value, Mapping) or hasattr(value, "items"):
        return {key: _plain_native(part) for key, part in sorted(value.items())}
    if isinstance(value, (set, frozenset)):
        return sorted(_plain_native(part) for part in value)
    return [_plain_native(part) for part in value]


def _native_primitives(owner: Any, fields: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Read only built-in RNA fields; runtime properties can have foreign getters."""
    return {
        prop.identifier: _plain_native(getattr(owner, prop.identifier))
        for prop in owner.bl_rna.properties
        if (not prop.is_runtime and prop.type in _NATIVE_PRIMITIVES
            and prop.identifier not in _FINGERPRINT_EXCLUDED
            and (fields is None or prop.identifier in fields))
    }


def _modifier_fingerprint(owner: Any, visited: set[int]) -> dict[str, Any]:
    pointer = owner.as_pointer()
    if pointer in visited:
        raise ValueError("旧人模动画修改器包含循环引用，无法确认复制内容")
    visited.add(pointer)
    try:
        result = {"rna_type": owner.bl_rna.identifier, "values": _native_primitives(owner)}
        children = {}
        for prop in owner.bl_rna.properties:
            if (prop.is_runtime or prop.identifier in _FINGERPRINT_EXCLUDED
                    or prop.type not in {"POINTER", "COLLECTION"}):
                continue
            value = getattr(owner, prop.identifier)
            items = tuple(value) if prop.type == "COLLECTION" else (value,)
            if any(isinstance(item, bpy.types.ID) for item in items):
                raise TypeError("旧人模动画修改器引用其它数据，无法确认复制内容")
            children[prop.identifier] = [
                _modifier_fingerprint(item, visited) if item is not None else None for item in items
            ]
        result["children"] = children
        return result
    finally:
        visited.remove(pointer)


def _curve_fingerprint(curve: Any) -> dict[str, Any]:
    return {
        "values": _native_primitives(curve),
        "group": curve.group.name if curve.group is not None else None,
        "keyframes": [_native_primitives(point) for point in curve.keyframe_points],
        "samples": [_native_primitives(point) for point in curve.sampled_points],
        "modifiers": [_modifier_fingerprint(modifier, set()) for modifier in curve.modifiers],
    }


def _groups_fingerprint(groups: Any) -> list[dict[str, Any]]:
    return [{"values": _native_primitives(group), "colors": _native_primitives(group.colors)} for group in groups]


def _action_fingerprint(action: Any) -> dict[str, Any]:
    layers = getattr(action, "layers", ())
    result = {
        "uid": action.session_uid,
        "values": _native_primitives(action, _ACTION_FIELDS),
        "properties": _plain_native(dict(action.items())),
        "slots": [_native_primitives(slot, _SLOT_FIELDS) for slot in getattr(action, "slots", ())],
        "markers": [{"values": _native_primitives(marker),
                     "camera": marker.camera.session_uid if marker.camera is not None else None}
                    for marker in getattr(action, "pose_markers", ())],
        "layers": [{"values": _native_primitives(layer), "strips": [
            {"values": _native_primitives(strip), "channelbags": [
                {"values": _native_primitives(bag), "groups": _groups_fingerprint(bag.groups),
                 "curves": [_curve_fingerprint(curve) for curve in bag.fcurves]}
                for bag in getattr(strip, "channelbags", ())]}
            for strip in layer.strips]}
            for layer in layers],
    }
    if not layers:
        result["groups"] = _groups_fingerprint(getattr(action, "groups", ()))
        result["curves"] = [_curve_fingerprint(curve) for curve in action_fcurves(action)]
    return result


def fingerprint(source: Any) -> dict[str, Any]:
    """Freeze all source values consumed by animation copying as plain data.

    This reads the original native state only, without frame evaluation or data
    creation. Evaluated validity/user counts and foreign registered RNA fields
    do not describe the copied animation and are deliberately excluded.
    """
    _rig(source)
    actions = {}

    def binding_fingerprint(binding: Any) -> dict[str, Any]:
        action = binding.action
        slot = _slot(binding)
        if action is not None:
            identity = str(action.session_uid)
            if identity not in actions:
                actions[identity] = _action_fingerprint(action)
        return {
            "action": action.session_uid if action is not None else None,
            "slot": _native_primitives(slot, _SLOT_FIELDS) if slot is not None else None,
        }

    animation = source.animation_data
    result = {
        "object": {
            "values": _native_primitives(source, ("rotation_mode", "parent_type", "parent_bone")),
            "basis": _plain_native(source.matrix_basis),
            "parent_inverse": _plain_native(source.matrix_parent_inverse),
            "parent": source.parent.session_uid if source.parent is not None else None,
        },
        "pose": [{"name": bone.name, "rotation_mode": bone.rotation_mode,
                  "basis": _plain_native(bone.matrix_basis),
                  "properties": {key: value for key, value in sorted(bone.items()) if isinstance(value, _SCALARS)}}
                 for bone in source.pose.bones],
        "animation": None,
        "actions": actions,
    }
    if animation is not None:
        result["animation"] = {
            **binding_fingerprint(animation),
            "values": _native_primitives(animation, (*_ANIMATION_SETTINGS, "use_tweak_mode")),
            "tracks": [{"values": _native_primitives(track, ("name", "mute", "is_solo", "select", "lock")),
                        "strips": [{**binding_fingerprint(strip),
                                    "values": _native_primitives(strip, (
                                        "name", "type", *_STRIP_SETTINGS, *_STRIP_TIMING,
                                        "use_auto_blend", "blend_in", "blend_out",
                                        "use_animated_time", "use_animated_influence"))}
                                   for strip in track.strips]}
                       for track in animation.nla_tracks],
        }
    return result


def copy_animation(tx: Any, source: Any, target: Any) -> dict[str, int]:
    """Copy native Actions/Clip NLA and matching pose values only to new owned IDs."""
    validate(source, target)
    if target not in tx.owned:
        raise ValueError("动画迁移目标不属于本次追加事务")
    animation = target.animation_data
    if animation is not None and (animation.action is not None or animation.nla_tracks):
        raise ValueError("新版模板已有 Action／NLA，未覆盖模板动画")
    counts = {
        "matched_bones": 0, "copied_curves": 0, "skipped_curves": 0,
        "copied_actions": 0, "nla_tracks": 0, "nla_strips": 0,
        "copied_properties": 0,
    }
    for old in source.pose.bones:
        new = target.pose.bones.get(old.name)
        if new is None:
            continue
        new.rotation_mode = old.rotation_mode
        new.matrix_basis = old.matrix_basis.copy()
        counts["matched_bones"] += 1
        for name, value in old.items():
            if isinstance(value, _SCALARS) and isinstance(new.get(name), _SCALARS):
                new[name] = value
                counts["copied_properties"] += 1
    old_animation = source.animation_data
    if old_animation is None:
        return counts
    animation = target.animation_data_create()
    copies: dict[tuple[int, str], tuple[Any, Any]] = {}

    def duplicate(binding: Any) -> tuple[Any, Any]:
        original = binding.action
        if original is None:
            return None, None
        original_slot = _slot(binding)
        identifier = original_slot.identifier if original_slot else ""
        identity = (original.as_pointer(), identifier)
        if identity in copies:
            return copies[identity]
        copied = original.copy()
        tx.track(copied)
        copied.use_fake_user = False
        copied.name = original.name + " · 新版迁移"
        copied_slot = None
        if original_slot is not None:
            copied_slot = next(slot for slot in copied.slots if slot.identifier == identifier)
            for slot in tuple(copied.slots):
                if slot != copied_slot:
                    copied.slots.remove(slot)
        layers = getattr(copied, "layers", ())
        collections: list[Any] = ([bag.fcurves for layer in layers for strip in layer.strips
                                   for bag in getattr(strip, "channelbags", ())]
                                  if layers else [getattr(copied, "fcurves", ())])
        for curves in collections:
            for curve in tuple(curves):
                if _allowed_path(curve, target):
                    counts["copied_curves"] += 1
                else:
                    curves.remove(curve)
                    counts["skipped_curves"] += 1
        counts["copied_actions"] += 1
        copies[identity] = copied, copied_slot
        return copied, copied_slot

    for old_track in old_animation.nla_tracks:
        track = animation.nla_tracks.new()
        track.name = old_track.name
        counts["nla_tracks"] += 1
        for old_strip in old_track.strips:
            action, slot = duplicate(old_strip)
            strip = track.strips.new(old_strip.name, int(old_strip.frame_start), action)
            if slot is not None:
                strip.action_slot = slot
            for name in _STRIP_SETTINGS:
                setattr(strip, name, getattr(old_strip, name))
            # Native setters adjust adjacent timing fields. Restore the source
            # action interval before the final, possibly fractional strip bounds.
            # Filtering unrelated bones can shrink the copied Action's natural
            # range. Expand its clip interval before moving the start past it.
            strip.action_frame_end = max(strip.action_frame_end, old_strip.action_frame_end)
            strip.action_frame_start = old_strip.action_frame_start
            strip.action_frame_end = old_strip.action_frame_end
            strip.repeat = old_strip.repeat
            strip.scale = old_strip.scale
            strip.frame_start = old_strip.frame_start
            strip.frame_end = old_strip.frame_end
            strip.use_auto_blend = old_strip.use_auto_blend
            strip.blend_in = old_strip.blend_in
            strip.blend_out = old_strip.blend_out
            for name in ("action_frame_start", "action_frame_end", "repeat", "scale", "frame_start", "frame_end"):
                if abs(getattr(strip, name) - getattr(old_strip, name)) > 1e-4:
                    raise ValueError("新版 NLA 时间区间无法完整复制，迁移已取消")
            counts["nla_strips"] += 1
        for name in ("mute", "is_solo", "select", "lock"):
            setattr(track, name, getattr(old_track, name))
    action, slot = duplicate(old_animation)
    animation.action = action
    if slot is not None:
        animation.action_slot = slot
    for name in ("action_extrapolation", "action_blend_type", "action_influence", "use_nla"):
        setattr(animation, name, getattr(old_animation, name))
    return counts
