"""Persistent workbench choices and explicit pose keys on one character rig."""

from math import isfinite

import bpy
from mathutils import Matrix, Quaternion, Vector

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.animation_edit import require_keyframe_edit

_AUTO = "blockloom_character_record"
_ROTATE = "blockloom_character_rotate"
_WRIST = "blockloom_character_wrist"
WRISTS = ("L", "R", "H")


def auto(rig):
    return bool(rig.get(_AUTO, False))


def rotate(rig):
    return bool(rig.get(_ROTATE, False))


def wrist(rig):
    value = rig.get(_WRIST, "L") if rig is not None else "L"
    return value if value in WRISTS else "L"


def set_option(rig, command, value):
    ensure_mutation_allowed()
    if command == "WRIST" and value in WRISTS:
        rig[_WRIST] = value
    elif command in {"AUTO", "ROTATE"} and isinstance(value, bool):
        rig[_AUTO if command == "AUTO" else _ROTATE] = value
    else:
        raise ValueError("无效的角色面板选项")


def _properties(bone, transform):
    from .viewport_pose import rotation_path
    if transform is None:
        return ("location", rotation_path(bone), "scale")
    if transform == "MOVE":
        return ("location",)
    if transform == "ROTATE":
        return (rotation_path(bone),)
    if transform == "SCALE":
        return ("scale",)
    raise ValueError("未知姿态变换")


def _key_targets(rig, bones, transform, channels):
    if not bones or any(bone.id_data != rig for bone in bones):
        raise ValueError("请只选择同一角色的控制骨")
    if channels is None:
        return [(b, p) for b in bones for p in _properties(b, transform)]
    if not isinstance(channels, dict) or set(channels) != {b.name for b in bones}:
        raise ValueError("控制骨与关键帧通道不匹配")
    targets = []
    for b in bones:
        props = channels[b.name]
        if not isinstance(props, list) or not props or any(p not in _properties(b, None) for p in props):
            raise ValueError("无效的姿态通道")
        targets.extend((b, p) for p in dict.fromkeys(props))
    return targets


def preflight_keys(rig, bones, transform=None, *, channels=None):
    paths = [b.path_from_id(p) for b, p in _key_targets(rig, bones, transform, channels)]
    for path in paths:
        require_keyframe_edit(rig, path)
    anim = rig.animation_data
    if anim:
        for curve in action_fcurves(anim.action, slot=anim.action_slot):
            if curve.data_path in paths and (curve.lock or (curve.group and curve.group.lock)):
                raise ValueError("控制骨的动画通道已锁定：" + curve.data_path)
    return paths


def insert_keys(rig, bones, frame, transform=None, *, channels=None):
    """Key only these bones on their own rig; do not apply a scene keying set."""
    ensure_mutation_allowed()
    if not isfinite(frame):
        raise ValueError("无效的关键帧位置")
    paths = preflight_keys(rig, bones, transform, channels=channels)
    anim = rig.animation_data
    prior_action = anim.action if anim else None
    # Recover pre-existing keys at this frame if an RNA insertion fails mid-batch.
    prior = {}
    for curve in action_fcurves(prior_action, slot=anim.action_slot if anim else None):
        if curve.data_path in paths:
            for p in curve.keyframe_points:
                if abs(p.co.x - frame) < .0001:
                    prior[curve.data_path, curve.array_index] = (p.co.copy(), p.handle_left.copy(), p.handle_right.copy())
    try:
        for bone, prop in _key_targets(rig, bones, transform, channels):
            if not bone.keyframe_insert(data_path=prop, frame=frame, group=bone.name):
                raise ValueError("无法给控制骨插帧：" + bone.name + " / " + prop)
        if prior_action is None:
            from ...util.ownership import seal_owned
            from .viewport_pose import character_of
            found = character_of(bpy.context.scene, rig)
            if found and found[0] is not None:
                seal_owned(found[0], (rig.animation_data.action,))
    except Exception:
        anim = rig.animation_data
        if anim and anim.action and prior_action is None:
            created = anim.action
            anim.action = None
            if not created.users:
                bpy.data.actions.remove(created)
        elif anim and anim.action:
            for curve in action_fcurves(anim.action, slot=anim.action_slot):
                if curve.data_path not in paths:
                    continue
                key = next((p for p in curve.keyframe_points if abs(p.co.x-frame) < .0001), None)
                if key is not None:
                    old = prior.get((curve.data_path, curve.array_index))
                    if old is None:
                        rig.keyframe_delete(data_path=curve.data_path, index=curve.array_index, frame=frame)
                    else:
                        key.co, key.handle_left, key.handle_right = old
        raise


def _rotation(bone, delta):
    from .viewport_pose import rotation_path
    prop = rotation_path(bone)
    current = bone.matrix_basis.to_quaternion()
    desired = delta @ current
    if prop == "rotation_euler":
        value = desired.to_euler(bone.rotation_mode, bone.rotation_euler)
        return tuple(old if locked else new for old, new, locked in zip(bone.rotation_euler, value, bone.lock_rotation))
    if not bone.lock_rotations_4d:
        angles = delta.to_euler("XYZ")
        for axis, locked in enumerate(bone.lock_rotation):
            if locked:
                angles[axis] = 0
        desired = angles.to_quaternion() @ current
    desired.make_compatible(current)
    if prop == "rotation_axis_angle":
        axis, angle = desired.to_axis_angle()
        values = (angle, *axis)
    else:
        values = tuple(desired)
    if bone.lock_rotations_4d:
        locks = (bone.lock_rotation_w, *bone.lock_rotation)
        values = tuple(old if lock else new for old, new, lock in zip(getattr(bone, prop), values, locks))
    return values


def apply_transform(rig, bone, transform, vector, frame, *, record):
    """Apply a confirmed view-space drag without invoking Blender's global auto-key."""
    ensure_mutation_allowed()
    if record:
        preflight_keys(rig, (bone,), transform)
    if len(vector) != 3 or not all(isfinite(x) for x in vector):
        raise ValueError("无效的视口变换")
    delta = Vector(vector)
    if delta.length_squared < 1e-16:
        return False
    prop = _properties(bone, transform)[0]
    old = tuple(getattr(bone, prop))
    world = rig.matrix_world @ bone.matrix
    if transform == "MOVE":
        changed = world.copy()
        changed.translation += delta
    else:
        rotation = Quaternion(delta.normalized(), delta.length)
        changed = rotation.to_matrix().to_4x4() @ world
        changed.translation = world.translation

    def local(matrix: Matrix):
        return rig.convert_space(pose_bone=bone, matrix=matrix, from_space="WORLD", to_space="LOCAL")

    initial, final = local(world), local(changed)
    if transform == "MOVE":
        diff = final.translation - initial.translation
        value = tuple(old[n] + (0 if locked else diff[n]) for n, locked in enumerate(bone.lock_location))
    else:
        value = _rotation(bone, final.to_quaternion() @ initial.to_quaternion().inverted())
    if all(abs(a-b)<1e-7 for a,b in zip(old, value)):
        return False
    setattr(bone, prop, value)
    if record:
        try:
            insert_keys(rig, (bone,), frame, transform)
        except Exception:
            setattr(bone, prop, old)
            raise
    return True
