"""Viewport shortcuts. Buttons invoke the same registered operations as the sidebar."""

from typing import Any, cast

import bpy
from bpy.props import BoolProperty, FloatVectorProperty, StringProperty
from bpy.types import GizmoGroup, Panel, UILayout, WindowManager
from bpy_extras.view3d_utils import location_3d_to_region_2d

from ..core.runtime import mutation_allowed
from . import assets, workbench_character
from . import workbench_card as card

_BUTTON_KEYMAP = "BlockLoom Workbench Buttons"


class OXALMCSTUDIO_PT_quick_tools(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_label = "其他功能"

    def draw(self, context: Any):
        layout = cast(UILayout, self.layout)
        layout.enabled = mutation_allowed()
        layout.operator_context = "INVOKE_DEFAULT"
        layout.operator(
            "oxal_mc_studio.asset_action", text="一键辉光", icon="LIGHT_SUN"
        ).action = "COMP_GLOW"
        layout.operator(
            "oxal_mc_studio.asset_file", text="导入图片资产", icon="IMAGE_DATA"
        ).action = "IMAGE"
        assets.draw_compositor(layout, context.scene)
        row = layout.row(align=True)
        row.popover(panel="OXALMCSTUDIO_PT_quick_character", text="角色功能")
        if context.selected_objects:
            row.popover(panel="OXALMCSTUDIO_PT_quick_enchant", text="附魔")


class OXALMCSTUDIO_PT_quick_enchant(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_label = "附魔"

    def draw(self, context: Any):
        layout = cast(UILayout, self.layout)
        layout.enabled = mutation_allowed()
        for action, label in (
            ("ENCHANT", "添加附魔"),
            ("ENCHANT_PIXELS_PREPARE", "添加像素流光"),
            ("UNENCHANT", "移除附魔"),
        ):
            layout.operator("oxal_mc_studio.asset_action", text=label).action = action
        if not context.space_data.show_gizmo:
            layout.popover(panel="OXALMCSTUDIO_PT_quick_tools", text="其他功能")


class OXALMCSTUDIO_PT_quick_character(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_label = "角色功能"

    def draw(self, context: Any):
        layout = cast(UILayout, self.layout)
        layout.enabled = mutation_allowed()
        layout.operator_context = "INVOKE_DEFAULT"
        target = selected_character(context)
        if target:
            names = dict(_character_buttons())
            for label, rows in workbench_character.sections(context, target):
                layout.label(text=label)
                for buttons in rows:
                    row = layout.row(align=True)
                    for part, text in buttons:
                        if part in {card.KEY, card.AUTO}:
                            row.operator("oxal_mc_studio.viewport_pose", text=text).action = "KEY" if part == card.KEY else "AUTO"
                        else:
                            row.operator("oxal_mc_studio.quick_character_action", text=text).action = names[part]
        else:
            layout.operator("oxal_mc_studio.import_human", text="导入人模", icon="IMPORT")
        layout.popover(panel="OXALMCSTUDIO_PT_quick_tools", text="其他功能")


def _character_buttons():
    yield from ((part, field) for part, (field, _, _) in workbench_character.FIELDS.items())
    yield from ((card.WRIST, "wrist"), (card.BIND, "bind"), (card.UNBIND, "unbind"),
                (card.IK, "ik"), (card.ROTATE, "rotate"), (card.BODY, "body"), (card.BONES, "bones"))


def _sections(context, page, target=None):
    if page == "TOOLS":
        return (
            ("图片资产", (((card.IMAGE, "导入图片资产…"),),)),
            ("合成器", (((card.GLOW, "一键辉光"), (card.OUTLINE, "启用描边")),
                      ((card.EXCLUDE, "排除所选"), (card.RESTORE, "恢复所选")),
                      ((card.CLEAR, "清空排除列表"),))),
        )
    if page == "ENCHANT":
        selected = context.selected_objects
        if not selected:
            return (("请先选择要附魔的物体", ()),)
        target = selected[0].name if len(selected) == 1 else f"已选 {len(selected)} 个物体"
        return (
            ("材质 · " + target, (((card.ADD, "添加附魔"), (card.REMOVE, "移除附魔")),)),
            ("像素效果", (((card.PIXELS, "添加像素流光"),),)),
        )
    if target:
        return workbench_character.sections(context, target)
    return (("白珀人模", (((card.IMPORT, "导入人模"),),)),)


def selected_character(context):
    from ..business.human.viewport_pose import selected_character as resolve

    return resolve(context)


def explicitly_open(context, target):
    wm = context.window_manager
    target_uid = str(target[1].session_uid) if target else ""
    return (wm.blockloom_show_tools
            and wm.blockloom_workbench_region == str(context.region.as_pointer())
            and wm.blockloom_workbench_target == target_uid)


def character_interactive(context):
    from ..business.human.viewport_pose import panel

    target = selected_character(context)
    wm = context.window_manager
    if not target or not context.space_data.show_gizmo or panel(target[1]) != "OPEN":
        return False
    return not explicitly_open(context, target) or (
        wm.blockloom_workbench_page == "CHARACTER")


def over_character_card(context, xy):
    _, cells = card_layout(context)
    return card.hit_part(cells, xy) >= 0


def character_anchor(context, target):
    if target is None or context.region_data is None:
        return None
    rig = target[1]
    head = rig.pose.bones.get("头")
    position = rig.matrix_world @ head.tail if head else rig.matrix_world.translation
    def project(world):
        return location_3d_to_region_2d(context.region, context.region_data, world)

    point = project(position)
    if point is None:
        return None
    extents = [point.x]
    for name in ("L.胳膊控制器", "R.胳膊控制器", "L.大臂", "R.大臂", "L.脚控制器", "R.脚控制器"):
        bone = rig.pose.bones.get(name)
        if bone:
            for end in (bone.head, bone.tail):
                endpoint = project(rig.matrix_world @ end)
                if endpoint is not None:
                    extents.append(endpoint.x)
    return min(extents), max(extents), point.y


def card_layout(context, *, origin=None):
    from ..business.human import viewport_character_state, viewport_pose
    wm = context.window_manager
    target = selected_character(context)
    opened = explicitly_open(context, target)
    follow = character_anchor(context, target)
    if not opened and (target is None or follow is None):
        return 1.0, []
    page = wm.blockloom_workbench_page if opened else "CHARACTER"
    role_page = page == "CHARACTER" and target is not None
    rig = target[1] if target is not None else None
    panel = viewport_pose.panel(rig) if role_page else "OPEN"
    if panel == "CLOSED":
        return 1.0, []
    collapsed = panel == "COLLAPSED" if role_page else opened and wm.blockloom_workbench_collapsed
    sections = () if collapsed else _sections(context, page, target)
    offset = viewport_pose.offset(rig) if role_page else wm.blockloom_character_offset
    if role_page and rig and wm.blockloom_character_drag == str(rig.session_uid):
        offset = wm.blockloom_character_preview_offset
    scale, cells = card.arrange(context, page=page, collapsed=collapsed, sections=sections,
                                follow=follow, origin=origin,
                                title="BlockLoom · 角色交互" if role_page else None,
                                rotate=bool(role_page and viewport_character_state.rotate(rig)), offset=offset)
    if role_page and target is not None and not collapsed and cells and viewport_pose.body_enabled(target[1]):
        rig = target[1]
        bone = rig.pose.bones.get("根")
        if bone:
            point = location_3d_to_region_2d(context.region, context.region_data, rig.matrix_world @ bone.head)
            if point is not None:
                radius = 9 * context.preferences.system.ui_scale
                cells.insert(0, (-1, (point.x - radius, point.y - radius, radius * 2, radius * 2), "root", ""))
    return scale, cells


class OXALMCSTUDIO_GGT_workbench(GizmoGroup):
    bl_idname = "OXALMCSTUDIO_GGT_workbench"
    bl_label = "BlockLoom 悬浮工作台"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_options = {"PERSISTENT"}  # noqa: RUF012 - Blender reads the class-level flag set.

    @classmethod
    def poll(cls, context: Any):
        from ..business.human.viewport_pose import panel

        target = selected_character(context)
        return explicitly_open(context, target) or bool(target and panel(target[1]) != "CLOSED")

    @classmethod
    def setup_keymap(cls, keyconfig):
        # A press opens the popup under a held mouse; its release activates the first row.
        keymap = keyconfig.keymaps.new(_BUTTON_KEYMAP, space_type="EMPTY", region_type="WINDOW")
        if not keymap.keymap_items.get("oxal_mc_studio.quick_move"):
            keymap.keymap_items.new("oxal_mc_studio.quick_move", "LEFTMOUSE", "PRESS")
        if not keymap.keymap_items.get("gizmogroup.gizmo_tweak"):
            keymap.keymap_items.new("gizmogroup.gizmo_tweak", "LEFTMOUSE", "CLICK")
        return keymap

    def setup(self, context):
        surface: Any = self.gizmos.new(card.OXALMCSTUDIO_GT_workbench_card.bl_idname)
        # Blender also looks up the operator for a hovered part when building its tooltip.
        surface.target_set_operator("oxal_mc_studio.quick_move", index=card.TITLE)
        for index, intent in (
            (card.BACKGROUND, "KEEP"), (card.COLLAPSE, "COLLAPSE"), (card.CLOSE, "CLOSE"),
            (card.CHARACTER, "CHARACTER"), (card.ENCHANT, "ENCHANT"), (card.TOOLS, "TOOLS"),
        ):
            surface.target_set_operator("oxal_mc_studio.quick_tools", index=index).intent = intent
        for index, action in (
            (card.ADD, "ENCHANT"), (card.PIXELS, "ENCHANT_PIXELS_PREPARE"), (card.REMOVE, "UNENCHANT"),
            (card.GLOW, "COMP_GLOW"), (card.OUTLINE, "COMP_OUTLINE"), (card.EXCLUDE, "COMP_EXCLUDE"),
            (card.RESTORE, "COMP_RESTORE"), (card.CLEAR, "COMP_CLEAR"),
        ):
            surface.target_set_operator("oxal_mc_studio.asset_action", index=index).action = action
        surface.target_set_operator("oxal_mc_studio.asset_file", index=card.IMAGE).action = "IMAGE"
        surface.target_set_operator("oxal_mc_studio.import_human", index=card.IMPORT)
        for index, action in ((card.KEY, "KEY"), (card.AUTO, "AUTO")):
            surface.target_set_operator("oxal_mc_studio.viewport_pose", index=index).action = action
        for index, name in _character_buttons():
            surface.target_set_operator("oxal_mc_studio.quick_character_action", index=index).action = name

    def draw_prepare(self, context: Any):
        surface: Any = self.gizmos[0]
        surface.scale, surface.cells = card_layout(context)


_classes: tuple[Any, ...] = (
    OXALMCSTUDIO_PT_quick_tools,
    OXALMCSTUDIO_PT_quick_enchant,
    OXALMCSTUDIO_PT_quick_character,
    card.OXALMCSTUDIO_GT_workbench_card,
    OXALMCSTUDIO_GGT_workbench,
)
_keys = []
_properties = {
    "blockloom_show_tools": BoolProperty(default=False, options={"SKIP_SAVE"}),
    "blockloom_workbench_region": StringProperty(options={"SKIP_SAVE"}),
    "blockloom_workbench_page": StringProperty(default="TOOLS", options={"SKIP_SAVE"}),
    "blockloom_workbench_collapsed": BoolProperty(default=False, options={"SKIP_SAVE"}),
    "blockloom_character_offset": FloatVectorProperty(size=2, options={"SKIP_SAVE"}),
    "blockloom_character_drag": StringProperty(options={"SKIP_SAVE"}),
    "blockloom_character_preview_offset": FloatVectorProperty(size=2, options={"SKIP_SAVE"}),
    "blockloom_workbench_target": StringProperty(options={"SKIP_SAVE"}),
    "blockloom_workbench_position": FloatVectorProperty(size=2, options={"SKIP_SAVE"}),
}


def register():
    registered = []
    try:
        for name, prop in _properties.items():
            setattr(WindowManager, name, prop)
        for cls in _classes:
            bpy.utils.register_class(cls)
            registered.append(cls)
        config = cast(WindowManager, bpy.context.window_manager).keyconfigs.addon
        if config:
            km = config.keymaps.new(name="3D View", space_type="VIEW_3D")
            _keys.append(
                (
                    km,
                    km.keymap_items.new(
                        "oxal_mc_studio.quick_tools", "Q", "PRESS", shift=True
                    ),
                )
            )
            _keys.append((km, km.keymap_items.new("oxal_mc_studio.quick_dismiss", "ESC", "PRESS")))
    except Exception:
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        for name in _properties:
            if hasattr(WindowManager, name):
                delattr(WindowManager, name)
        raise


def unregister():
    for km, key in _keys:
        km.keymap_items.remove(key)
    _keys.clear()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    configs = cast(WindowManager, bpy.context.window_manager).keyconfigs
    for config in (configs.default, configs.user):
        if config is not None:
            keymap = config.keymaps.get(_BUTTON_KEYMAP)
            if keymap is not None and (config == configs.default or not keymap.is_user_modified):
                config.keymaps.remove(keymap)
    for name in _properties:
        delattr(WindowManager, name)
    card.clear_graphics()
