"""Resource submission entry points and their shared native Undo boundary."""

from typing import TYPE_CHECKING, cast

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Area, Context, Operator, Scene

from ..core.importing import ImportEndpoint, find_endpoint
from ..core.runtime import mutation_allowed
from ..module import human, inventory

if TYPE_CHECKING:
    from bpy.stub_internal.rna_enums import OperatorReturnItems


def _ready(context: Context, endpoint: ImportEndpoint) -> bool:
    scene = cast(Scene | None, context.scene)
    return (
        mutation_allowed()
        and scene is not None
        and not scene.library
        and not scene.override_library
        and context.mode == "OBJECT"
        and not endpoint.busy(scene.session_uid)
    )


class OXALMCSTUDIO_OT_import_inventory(Operator):
    bl_idname = "oxal_mc_studio.import_inventory"
    bl_label = "导入独立物品栏"
    bl_description = "追加一个独立的 OXAL 物品栏实例，不替换现有实例"

    @classmethod
    def poll(cls, context: Context) -> bool:
        return _ready(context, inventory.endpoint)

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        scene = cast(Scene, context.scene)
        try:
            display = getattr(context.window_manager, "oxal_mc_studio_inventory_ui", None)
            camera = display.camera if display else None
            if camera is None:
                camera = scene.camera
            options = {
                "view_mode": display.view_mode if display else "THIRD_PERSON",
                "camera_uid": str(camera.session_uid) if camera is not None else "",
                "cursor": [scene.cursor.location[axis] for axis in range(3)],
                "hands_enabled": display.hands_enabled if display else True,
                "arm_mode": display.arm_mode if display else "SLIM",
                "skin_filepath": display.skin_filepath if display else "",
            }
            inventory.endpoint.submit(scene.session_uid, options)
        except (TypeError, ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_inventory_command(Operator):
    bl_idname = "oxal_mc_studio.inventory_command"
    bl_label = "独立物品栏操作"

    action: StringProperty()

    @classmethod
    def poll(cls, context: Context) -> bool:
        return _ready(context, inventory.commands)

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        from ..ui.instances import selected_root
        from ..util.resources import INSTANCE

        try:
            scene = cast(Scene, context.scene)
            state = getattr(context.window_manager, "oxal_mc_studio_inventory_ui", None)
            if state is None:
                raise ValueError("物品栏界面状态尚未就绪")
            root = selected_root(context, state, "oxal.inventory")
            camera = state.camera
            if camera is None and self.action == "repair" and scene.camera != root.get("独立物品栏相机"):
                camera = scene.camera
            options = {"action": self.action, "instance_id": str(root[INSTANCE]), "enabled": bool(state.hands_enabled), "arm_mode": state.arm_mode, "skin_filepath": state.skin_filepath, "camera_uid": str(camera.session_uid) if camera else ""}
            inventory.commands.submit(scene.session_uid, options)
        except (TypeError, ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_inventory_preview(Operator):
    bl_idname = "oxal_mc_studio.inventory_preview"
    bl_label = "预览第一人称"
    bl_description = "当前三维视图使用所选物品栏的专用相机；保留场景相机，可用数字键盘0退出预览"

    @classmethod
    def poll(cls, context: Context) -> bool:
        area = cast(Area | None, context.area)
        return mutation_allowed() and area is not None and area.type == "VIEW_3D"

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        from ..business.inventory.inventory_view import preview
        from ..ui.instances import selected_root

        try:
            state = getattr(context.window_manager, "oxal_mc_studio_inventory_ui", None)
            if state is None:
                raise ValueError("物品栏界面状态尚未就绪")
            root = selected_root(context, state, "oxal.inventory")
            preview(context, root)
        except (TypeError, ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_import_human(Operator):
    bl_idname = "oxal_mc_studio.import_human"
    bl_label = "导入白珀人模"
    bl_description = "按当前初始样式导入一个独立人模，可放置到游标快照位置"

    at_cursor: BoolProperty(default=False, options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context: Context) -> bool:
        return _ready(context, human.endpoint)

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        scene = cast(Scene, context.scene)
        display = getattr(context.window_manager, "oxal_mc_studio_human_ui", None)
        options = {
            "arm_mode": display.arm_mode if display else "SLIM",
            "leg_mode": display.leg_mode if display else "NORMAL",
            "name_enabled": display.name_enabled == "ON" if display else False,
            "leg_fixed": display.leg_fixed == "ON" if display else False,
            "foot_fixed": display.foot_fixed == "ON" if display else False,
            "at_cursor": self.at_cursor,
            "position": [scene.cursor.location[axis] for axis in range(3)]
            if self.at_cursor
            else [0.0, 0.0, 0.0],
        }
        try:
            human.endpoint.submit(scene.session_uid, options)
        except (TypeError, ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_apply_resource_import(Operator):
    bl_idname = "oxal_mc_studio.apply_resource_import"
    bl_label = "导入 OXAL 资源"
    bl_options = {"INTERNAL", "UNDO"}  # noqa: RUF012 -- Blender class RNA metadata.

    topic: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    request_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    scene_uid: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    options_json: StringProperty(default="{}", options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        try:
            endpoint = find_endpoint(self.topic)
        except ValueError:
            return {"CANCELLED"}
        ok = endpoint.apply(self.request_id, self.scene_uid, self.options_json)
        return {"FINISHED"} if ok else {"CANCELLED"}


CLASSES = (
    OXALMCSTUDIO_OT_import_inventory,
    OXALMCSTUDIO_OT_inventory_command,
    OXALMCSTUDIO_OT_inventory_preview,
    OXALMCSTUDIO_OT_import_human,
    OXALMCSTUDIO_OT_apply_resource_import,
)


def register() -> None:
    registered = []
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
    except Exception:
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        raise


def unregister() -> None:
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
