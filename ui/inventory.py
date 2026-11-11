"""The two independent inventory import views; controls stay on native bones."""

import bpy
from bpy.props import BoolProperty, EnumProperty, PointerProperty, StringProperty
from bpy.types import PropertyGroup

from ..core.runtime import mutation_allowed
from ..module import inventory
from . import common, instances

STATE_PROPERTY = "oxal_mc_studio_inventory_ui"


def _camera_only(_self, value):
    return value.type == "CAMERA"


class OXALMCSTUDIO_PG_inventory_ui(PropertyGroup):
    view_mode: EnumProperty(
        name="导入视角",
        items=(("FIRST_PERSON", "第一人称", "专用相机前的独立HUD"),
               ("THIRD_PERSON", "第三人称", "放置到3D游标的世界空间骨架")),
        default="THIRD_PERSON", options={"SKIP_SAVE"},
    )
    camera: PointerProperty(name="参考相机", type=bpy.types.Object, poll=_camera_only, options={"SKIP_SAVE"})
    hands_enabled: BoolProperty(name="第一人称手部", default=True, options={"SKIP_SAVE"})
    arm_mode: EnumProperty(name="手臂", items=(("SLIM", "3格", ""), ("WIDE", "4格", "")), default="SLIM", options={"SKIP_SAVE"})
    skin_filepath: StringProperty(name="手部皮肤", subtype="FILE_PATH", options={"SKIP_SAVE"})
    slot: EnumProperty(name="目标格子", items=(("CURRENT", "当前选中", "跟随原生选框控制骨"), *((str(i), str(i), "") for i in range(1, 10)), ("OFFHAND", "副手", "")), default="CURRENT", options={"SKIP_SAVE"})


OXALMCSTUDIO_PG_inventory_ui.__annotations__.update(instances.selection_properties("oxal.inventory"))


def draw(layout, context):
    state = getattr(context.window_manager, STATE_PROPERTY)
    box = layout.box()
    box.enabled = mutation_allowed()
    box.prop(state, "view_mode", expand=True)
    try:
        selected = instances.selected_root(context, state, "oxal.inventory")
    except ValueError:
        selected = None
    selected_first_person = selected is not None and selected.get("独立视角") == "FIRST_PERSON"
    if state.view_mode == "FIRST_PERSON" or selected_first_person:
        if state.view_mode != "FIRST_PERSON":
            box.label(text="所选第一人称实例 · 应用参数", icon="CAMERA_DATA")
        box.prop(state, "camera")
        box.label(text="导入留空使用场景相机；应用时请选择参考相机", icon="CAMERA_DATA")
        box.prop(state, "hands_enabled")
        if state.hands_enabled:
            box.prop(state, "arm_mode", expand=True)
            box.prop(state, "skin_filepath")
    box.operator("oxal_mc_studio.import_inventory", text="添加独立物品栏", icon="IMPORT")
    status = inventory.endpoint.state(context.scene.session_uid)
    if status.message:
        box.label(text=status.message, icon="CHECKMARK" if status.succeeded else "INFO")
    box.label(text="通过姿态骨骼及原生自定义属性控制，可直接插帧", icon="BONE_DATA")
    box.template_list("OXALMCSTUDIO_UL_inventory_instances", "", bpy.data, "collections", state, "index", rows=2)
    row = box.row(align=True)
    row.operator("oxal_mc_studio.inventory_command", text="选择实例", icon="RESTRICT_SELECT_OFF").action = "select"
    row.operator("oxal_mc_studio.inventory_command", text="移除实例", icon="TRASH").action = "remove"
    if selected_first_person:
        box.operator("oxal_mc_studio.inventory_preview", text="预览第一人称", icon="CAMERA_DATA")
        row = box.row(align=True)
        row.operator("oxal_mc_studio.inventory_command", text="应用手部", icon="BONE_DATA").action = "hands"
        row.operator("oxal_mc_studio.inventory_command", text="绑定参考相机", icon="CAMERA_DATA").action = "camera"
    command_status = inventory.commands.state(context.scene.session_uid)
    if command_status.message and command_status.message not in {"独立物品栏操作完成：repair", "已修复独立物品栏"}:
        box.label(text=command_status.message, icon="INFO")


def register():
    common.register_state(OXALMCSTUDIO_PG_inventory_ui, STATE_PROPERTY)


def unregister():
    common.unregister_state(OXALMCSTUDIO_PG_inventory_ui, STATE_PROPERTY)
