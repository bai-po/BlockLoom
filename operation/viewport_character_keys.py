"""Record confirmed native pose transforms on the character that started them."""

import json
from typing import TYPE_CHECKING, Any, cast

import bpy
from bpy.props import StringProperty
from bpy.types import Macro, Operator

from ..business.human import viewport_character_state as state
from ..business.human import viewport_pose as pose
from ..core.runtime import mutation_allowed
from ..module import human

if TYPE_CHECKING:
    from bpy.stub_internal.rna_enums import OperatorReturnItems


class OXALMCSTUDIO_OT_pose_record_move(Macro):
    bl_idname = "oxal_mc_studio.pose_record_move"
    bl_label = "移动角色控制骨"
    bl_options = {"INTERNAL"}  # noqa: RUF012 -- the EMT key insertion records the completed pose for Undo.


class OXALMCSTUDIO_OT_pose_record_rotate(Macro):
    bl_idname = "oxal_mc_studio.pose_record_rotate"
    bl_label = "旋转角色控制骨"
    bl_options = {"INTERNAL"}  # noqa: RUF012 -- the EMT key insertion records the completed pose for Undo.


class OXALMCSTUDIO_OT_pose_record_scale(Macro):
    bl_idname = "oxal_mc_studio.pose_record_scale"
    bl_label = "缩放角色控制骨"
    bl_options = {"INTERNAL"}  # noqa: RUF012 -- the EMT key insertion records the completed pose for Undo.


_NATIVE = {
    "MOVE": ("G", "transform.translate", OXALMCSTUDIO_OT_pose_record_move),
    "ROTATE": ("R", "transform.rotate", OXALMCSTUDIO_OT_pose_record_rotate),
    "SCALE": ("S", "transform.resize", OXALMCSTUDIO_OT_pose_record_scale),
}


def _channel(bone, transform):
    if transform == "ROTATE":
        return pose.rotation_path(bone)
    return "location" if transform == "MOVE" else "scale"


def invoke_recorded(context, rig, bones, transform, native_options):
    found = pose.character_of(context.scene, rig)
    if not found or not state.auto(rig) or context.scene.tool_settings.use_keyframe_insert_auto:
        raise ValueError("所选角色的自动记录状态已改变")
    if human.commands.busy(context.scene.session_uid):
        raise ValueError("请等待当前角色操作完成")
    token = pose.target(found[0], rig)
    pose._resolve(context.scene, token)
    before = {}
    for b in bones:
        props = [_channel(b, transform)]
        # A pivot away from the bone origin changes its position as well.
        if transform != "MOVE":
            props.append("location")
        before[b.name] = {p: tuple(getattr(b, p)) for p in props}
    state.preflight_keys(rig, bones, transform)
    snapshot = {
        "scene": context.scene.session_uid,
        "area": str(context.area.as_pointer()),
        "frame": context.scene.frame_current_final,
        "target": token,
        "transform": transform,
        "before": before,
    }
    _, native, macro = _NATIVE[transform]
    macro_op = getattr(cast(Any, bpy.ops).oxal_mc_studio, macro.bl_idname.split(".")[1])
    options = {
        "TRANSFORM_OT_" + native.split(".")[1]: native_options,
        cast(Any, bpy.ops).oxal_mc_studio.pose_record_confirm.idname(): {"snapshot": json.dumps(snapshot)},
    }
    return macro_op("INVOKE_DEFAULT", **options)


class OXALMCSTUDIO_OT_pose_record_confirm(Operator):
    bl_idname = "oxal_mc_studio.pose_record_confirm"
    bl_label = "记录已完成的姿态变换"
    bl_options = {"INTERNAL"}  # noqa: RUF012 -- Blender RNA metadata.
    snapshot: StringProperty(options={"SKIP_SAVE", "HIDDEN"})

    def execute(self, context: Any) -> "set[OperatorReturnItems]":
        try:
            data = json.loads(self.snapshot)
            scene = context.scene
            if scene.session_uid != data["scene"] or scene.frame_current_final != data["frame"]:
                raise ValueError("场景或当前帧已改变")
            rig = pose._resolve(scene, data["target"])
            if not state.auto(rig):
                raise ValueError("角色自动记录已关闭")
            transform = data["transform"]
            changed = {}
            for name, properties in data["before"].items():
                bone = rig.pose.bones[name]
                channels = [prop for prop, old in properties.items()
                            if any(abs(a - b) > 1e-6 for a, b in zip(old, getattr(bone, prop)))]
                if channels:
                    changed[name] = channels
            if changed:
                human.commands.submit(scene.session_uid, {
                    "action": "viewport_pose", "command": "KEY", "target": data["target"],
                    "area": data["area"], "bones": list(changed), "frame": data["frame"],
                    "transform": transform, "channels": changed,
                })
        except (KeyError, RuntimeError, ValueError) as exc:
            self.report({"WARNING"}, str(exc))
        return {"FINISHED"}


def _native_binding(context, key, expected):
    """Only extend a shortcut that the active user keymap still assigns to Blender."""
    config = context.window_manager.keyconfigs.user
    for name in ("Pose", "3D View"):
        km = config.keymaps.get(name)
        if km is None:
            continue
        for kmi in km.keymap_items:
            if (
                kmi.type == key and kmi.active and kmi.value in {"ANY", "PRESS"}
                and not (kmi.shift or kmi.ctrl or kmi.alt or kmi.oskey)
                and kmi.key_modifier == "NONE"
                and kmi.idname != OXALMCSTUDIO_OT_pose_record_shortcut.bl_idname
            ):
                if kmi.idname == expected:
                    explicit = kmi.properties.keys()
                    return {p: getattr(kmi.properties, p) for p in explicit}
                return None
    return None


class OXALMCSTUDIO_OT_pose_record_shortcut(Operator):
    bl_idname = "oxal_mc_studio.pose_record_shortcut"
    bl_label = "记录当前角色的控制骨变换"
    bl_options = {"INTERNAL"}  # noqa: RUF012 -- Blender RNA metadata.

    @classmethod
    def poll(cls, context: Any):
        return (
            context.mode == "POSE" and context.area and context.area.type == "VIEW_3D"
            and context.region and context.region.type == "WINDOW"
            and not context.scene.tool_settings.use_keyframe_insert_auto
            and len(context.objects_in_mode or ()) == 1 and state.auto(context.active_object)
            and mutation_allowed()
        )

    def invoke(self, context: Any, event: Any) -> "set[OperatorReturnItems]":
        transform = next((op for op, (key, _, _) in _NATIVE.items() if key == event.type), None)
        if transform is None:
            return {"PASS_THROUGH"}
        opts = _native_binding(context, event.type, _NATIVE[transform][1])
        bones = context.selected_pose_bones or ()
        rig = context.active_object
        if opts is None or not bones or not pose.character_of(context.scene, rig):
            return {"PASS_THROUGH"}
        try:
            status = invoke_recorded(context, rig, bones, transform, opts)
        except (ValueError, RuntimeError) as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"} if "RUNNING_MODAL" in status else status


_registered = []
_keys = []


def register():
    for cls in (OXALMCSTUDIO_OT_pose_record_confirm, OXALMCSTUDIO_OT_pose_record_shortcut):
        bpy.utils.register_class(cls)
        _registered.append(cls)
    for _, native, macro in _NATIVE.values():
        bpy.utils.register_class(macro)
        _registered.append(macro)
        macro.define(native)
        macro.define(OXALMCSTUDIO_OT_pose_record_confirm.bl_idname)
    config = cast(Any, bpy.context.window_manager).keyconfigs.addon
    if config is not None:
        km = config.keymaps.new(name="Pose", space_type="EMPTY")
        for key, _, _ in _NATIVE.values():
            entry = km.keymap_items.new(OXALMCSTUDIO_OT_pose_record_shortcut.bl_idname, key, "PRESS", head=True)
            _keys.append((km, entry))


def unregister():
    for km, entry in _keys:
        km.keymap_items.remove(entry)
    _keys.clear()
    while _registered:
        bpy.utils.unregister_class(_registered.pop())
