"""Sidebar binding selector, backed by the same rig as the character card."""

from typing import Any, cast

import bpy
from bpy.props import EnumProperty

from ..business.human import viewport_character_state as state
from ..business.human import viewport_pose
from ..module import human

_ITEMS = (("L", "左手", ""), ("R", "右手", ""), ("H", "头", ""))


def property_for_character(*, creature=False):
    def target(context, owner):
        if creature:
            from ..business.asset.creatures import RIG, selected_instance
            root = selected_instance(context.scene, context.selected_objects or ())
            return (root, root[RIG]) if root and root.get(RIG) else None
        from ..business.human.human import rig_of
        from .instances import current_human
        root, _legacy = current_human(context, owner)
        rig = rig_of(root) if root else None
        return (root, rig) if rig else None

    def get_value(owner):
        context = cast(Any, bpy.context)
        current = target(context, owner) if getattr(context, "scene", None) else None
        return state.WRISTS.index(state.wrist(current[1])) if current else 0

    def set_value(owner, index):
        context = cast(Any, bpy.context)
        current = target(context, owner) if getattr(context, "scene", None) else None
        if current and 0 <= index < len(state.WRISTS):
            value = state.WRISTS[index]
            if state.wrist(current[1]) != value:
                human.commands.submit(context.scene.session_uid, {
                    "action": "viewport_pose", "command": "WRIST", "value": value,
                    "target": viewport_pose.target(*current),
                    "area": str(context.area.as_pointer()) if context.area else "",
                })

    return EnumProperty(name="绑定位置", items=_ITEMS, get=get_value, set=set_value, options={"SKIP_SAVE"})
