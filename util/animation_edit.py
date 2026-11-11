"""Read-only preflight for explicit RNA writes and keyframe insertion.

No Actions, slots, curves or drivers are created, copied or unbound here.
Callers retain responsibility for the actual write and its transaction.
"""

from typing import Any

import bpy

from .animation import action_fcurves


def _strips(strips):
    for strip in tuple(strips):
        yield strip
        if strip.type == "META":
            yield from _strips(strip.strips)


def _nla(animation, *, active_only=False):
    if animation is None or active_only and not animation.use_nla:
        return
    for track in animation.nla_tracks:
        if active_only and track.mute:
            continue
        for strip in _strips(track.strips):
            if strip.action is not None and not (active_only and strip.mute):
                yield strip


def _has_path(action, slot, path):
    return any(curve.data_path == path for curve in action_fcurves(action, slot=slot))


def _preflight(owner, path):
    if not isinstance(owner, bpy.types.ID) or not isinstance(path, str) or not path:
        raise ValueError("动画写入目标或属性路径无效")
    if owner.library is not None or owner.override_library is not None or not owner.is_editable:
        raise ValueError("属性属于只读或关联数据，未修改")
    animation = getattr(owner, "animation_data", None)
    if animation is not None and any(curve.data_path == path for curve in animation.drivers):
        raise ValueError("该属性已有驱动器，请先解除驱动后再操作：" + path)
    return animation


def require_property_edit(owner: Any, path: str) -> None:
    """Reject writes that native animation would overwrite after evaluation."""
    animation = _preflight(owner, path)
    if animation is None:
        return
    if _has_path(animation.action, animation.action_slot, path):
        raise ValueError("该属性已有关键帧，请通过原动画编辑：" + path)
    if any(_has_path(strip.action, getattr(strip, "action_slot", None), path)
           for strip in _nla(animation, active_only=True)):
        raise ValueError("该属性受 NLA 动画控制，未覆盖：" + path)


def require_keyframe_edit(owner: Any, path: str) -> None:
    """Allow only a local, exclusively bound active Action for key insertion.

    Registry/fake users do not count as animation bindings. Other actual ID
    users and NLA strips do; even separate slots in one shared Action are kept
    intact rather than silently extending a user's shared animation container.
    """
    animation = _preflight(owner, path)
    if animation is None:
        return
    if getattr(animation, "use_tweak_mode", False):
        raise ValueError("请先退出 NLA 调整模式，再插入关键帧")
    if any(_has_path(strip.action, getattr(strip, "action_slot", None), path)
           for strip in _nla(animation, active_only=True)):
        raise ValueError("该属性受 NLA 动画控制，未插入关键帧：" + path)
    action = animation.action
    if action is None:
        return
    if action.library is not None or action.override_library is not None or not action.is_editable:
        raise ValueError("当前 Action 为只读或关联数据，未插入关键帧")
    users = bpy.data.user_map(subset=(action,)).get(action, ())
    for user in {owner, *users}:
        # Embedded node trees may be reported through their owning ID.
        candidates = (user, getattr(user, "node_tree", None), getattr(user, "shape_keys", None))
        for candidate in candidates:
            data = getattr(candidate, "animation_data", None)
            if data is None:
                continue
            if candidate != owner and data.action == action:
                raise ValueError("当前 Action 被其它数据共用，未插入关键帧")
            if any(strip.action == action for strip in _nla(data)):
                raise ValueError("当前 Action 被 NLA 使用，未插入关键帧")
