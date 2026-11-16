"""Hit-test the visible model and hand a verified native control to Blender's transform tool."""

from typing import TYPE_CHECKING, Any, cast

import bpy
from bpy.props import StringProperty
from bpy.types import Operator
from bpy_extras import view3d_utils
from mathutils import Vector

from ..business.human import viewport_character_state as state
from ..business.human import viewport_pose as pose
from ..core.runtime import get_bus, mutation_allowed
from ..module import human

if TYPE_CHECKING:
    from bpy.stub_internal.rna_enums import OperatorReturnItems


def _pick(context, xy, selected):
    region, view = context.region, context.region_data
    if selected:
        root, rig = selected
        bone = rig.pose.bones.get("根")
        if bone is not None:
            point = view3d_utils.location_3d_to_region_2d(
                region, view, rig.matrix_world @ bone.head
            )
            if (
                point
                and (point - Vector(xy)).length
                <= 9 * context.preferences.system.ui_scale
            ):
                return root, rig, "ROOT"
    direction = view3d_utils.region_2d_to_vector_3d(region, view, xy)
    origin = view3d_utils.region_2d_to_origin_3d(region, view, xy)
    hit, _, _, _, obj, _ = context.scene.ray_cast(
        context.evaluated_depsgraph_get(), origin, direction
    )
    if hit and obj and obj.type == "MESH":
        original = obj.original
        part = pose.body_part(original)
        found = pose.character_of(context.scene, original) if part else None
        if found and found == selected:
            return *found, part
    return None


class OXALMCSTUDIO_OT_viewport_pose(Operator):
    bl_idname = "oxal_mc_studio.viewport_pose"
    bl_label = "角色姿态"
    bl_description = "操作所选原控制骨，拖动后松开确认，Esc 或右键取消"
    action: StringProperty(default="KEY", options={"SKIP_SAVE"})
    part: StringProperty(default="", options={"SKIP_SAVE"})



    @classmethod
    def poll(cls, context: Any):
        return context.mode in {"OBJECT", "POSE"} and mutation_allowed()

    def execute(self, context: Any) -> "set[OperatorReturnItems]":
        found = pose.selected_character(context)
        if not found:
            self.report({"WARNING"}, "请选择一个白珀人模或生物")
            return {"CANCELLED"}
        root, rig = found
        try:
            options = {
                "action": "viewport_pose",
                "command": self.action,
                "target": pose.target(root, rig),
                "area": str(context.area.as_pointer()),
            }
            if self.action == "AUTO":
                options["enabled"] = not state.auto(rig)
            elif self.action == "KEY":
                names = (
                    [
                        pose.control(
                            rig, self.part, state.rotate(rig)
                        )[0]
                    ]
                    if self.part
                    else pose.selected_names(rig)
                )
                if not names:
                    raise ValueError("请先点击身体部位或选择原控制骨，再插入关键帧")
                options.update(bones=names, frame=context.scene.frame_current_final)
            else:
                raise ValueError("请从三维视图拖动角色部位")
            human.commands.submit(context.scene.session_uid, options)
        except (ValueError, RuntimeError) as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}

    def invoke(self, context: Any, event: Any) -> "set[OperatorReturnItems]":
        if self.action != "DRAG":
            return self.execute(context)
        if context.region_data is None or context.region.type != "WINDOW":
            return {"PASS_THROUGH"}
        xy = (event.mouse_region_x, event.mouse_region_y)
        found = pose.selected_character(context)
        if self.part:
            picked = (*found, self.part) if found else None
        else:
            from ..ui.workbench import character_interactive, over_character_card

            if (
                not found or not pose.body_enabled(found[1])
                or not character_interactive(context)
                or over_character_card(context, xy)
            ):
                return {"PASS_THROUGH"}
            picked = _pick(context, xy, found)
        if not picked:
            return {"PASS_THROUGH"}
        root, rig, part = cast(Any, picked)
        try:
            bone, transform = pose.control(
                rig, part, state.rotate(rig)
            )
            pose.guard_transform(
                rig,
                rig.pose.bones[bone],
                transform,
                state.auto(rig),
            )
            self._rig_uid = rig.session_uid
            self._options = {
                "action": "viewport_pose",
                "command": "SELECT",
                "target": pose.target(root, rig),
                "area": str(context.area.as_pointer()),
                "bones": [bone],
                "transform": transform,
                "frame": context.scene.frame_current_final,
            }
            self._scene_uid = context.scene.session_uid
            self._xy = xy
            self._anchor = rig.matrix_world @ rig.pose.bones[bone].head
            self._normal = context.region_data.view_rotation @ Vector((0, 0, 1))
            if self._native_ready(context):
                return self._native(context)
            if not self._ready(context):
                self._selection_request = human.pose_selection.submit(
                    self._scene_uid, self._options
                )
            context.window_manager.modal_handler_add(self)
        except (RuntimeError, ValueError) as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def _ready(self, context):
        rig = context.view_layer.objects.active
        if (
            not rig
            or rig.session_uid != self._rig_uid
            or context.mode != "POSE"
            or len(context.objects_in_mode or ()) != 1
        ):
            return False
        return pose.selected_names(rig) == self._options["bones"]

    def _native_ready(self, context):
        return (self._ready(context) and not context.scene.tool_settings.use_keyframe_insert_auto
                and pose.native_visible(context.view_layer.objects.active))

    def _native(self, context) -> "set[OperatorReturnItems]":
        rig = context.view_layer.objects.active
        pose.guard_transform(
            rig,
            rig.pose.bones[self._options["bones"][0]],
            self._options["transform"],
            state.auto(rig),
        )
        # Invoke Blender's native modal transform, which handles live deformation, lock flags,
        # constraints, animation, right-click/Escape and Undo. Never stream scene writes on hover.
        opts: dict[str, Any] = {
            "release_confirm": True,
            "use_proportional_edit": False,
            "mirror": False,
        }
        move = self._options["transform"] == "MOVE"
        if not move:
            opts.update(orient_axis="Z", orient_type="VIEW")
        if state.auto(rig):
            from .viewport_character_keys import invoke_recorded

            chosen = [rig.pose.bones[self._options["bones"][0]]]
            status = invoke_recorded(context, rig, chosen, self._options["transform"], opts)
        elif move:
            status = cast(Any, bpy.ops).transform.translate("INVOKE_DEFAULT", **opts)
        else:
            status = cast(Any, bpy.ops).transform.rotate("INVOKE_DEFAULT", **opts)
        if "RUNNING_MODAL" not in status:
            raise ValueError("Blender 未启动姿态变换")
        return {"FINISHED"}

    def cancel(self, context: Any) -> None:
        request = getattr(self, "_selection_request", None)
        if request:
            get_bus().cancel(request, reason="用户取消本次角色选择")

    def modal(self, context: Any, event: Any) -> "set[OperatorReturnItems]":
        if (
            context.scene.session_uid != self._scene_uid
            or not mutation_allowed()
            or event.type in {"ESC", "RIGHTMOUSE", "WINDOW_DEACTIVATE"}
        ):
            self.cancel(context)
            return {"CANCELLED"}
        xy = Vector((event.mouse_region_x, event.mouse_region_y))
        delta = xy - Vector(self._xy)
        try:
            if event.type == "MOUSEMOVE" and self._native_ready(context):
                return self._native(context)
            if event.type == "LEFTMOUSE" and event.value == "RELEASE":
                if delta.length >= 2:
                    if self._options["transform"] == "MOVE":
                        project = view3d_utils.region_2d_to_location_3d
                        vector = project(
                            context.region, context.region_data, xy, self._anchor
                        ) - project(
                            context.region, context.region_data, self._xy, self._anchor
                        )
                    else:
                        vector = self._normal * ((delta.x + delta.y) * 0.012)
                    human.commands.submit(
                        self._scene_uid,
                        dict(
                            self._options,
                            command="TRANSFORM",
                            vector=[vector.x, vector.y, vector.z],
                        ),
                    )
                return {"FINISHED"}
        except (ValueError, RuntimeError) as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}


_keys = []


def register():
    bpy.utils.register_class(OXALMCSTUDIO_OT_viewport_pose)
    try:
        from . import viewport_character_keys
        viewport_character_keys.register()
        config = cast(Any, bpy.context.window_manager).keyconfigs.addon
        if config is not None:
            keymap = config.keymaps.new(name="3D View", space_type="VIEW_3D")
            key = keymap.keymap_items.new(
                OXALMCSTUDIO_OT_viewport_pose.bl_idname, "LEFTMOUSE", "PRESS", head=True
            )
            _keys.append((keymap, key))
            key.properties.action = "DRAG"
    except Exception:
        unregister()
        raise


def unregister():
    for km, key in _keys:
        km.keymap_items.remove(key)
    _keys.clear()
    from . import viewport_character_keys
    viewport_character_keys.unregister()
    bpy.utils.unregister_class(OXALMCSTUDIO_OT_viewport_pose)
