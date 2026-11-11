"""Native human panel; persistent controls belong to the selected instance."""

import json
from typing import Any, ClassVar, cast

import bpy
from bpy.props import (
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Image, Menu, PropertyGroup, Scene, UIList

from ..business.human.human import builtin_skins, parameter_owner, resolve_human
from ..business.human.human_source import CURRENT, model_version
from ..business.human.human_state import MATERIALS, PARAMETERS, PARTS, PATTERNS, path
from ..core.runtime import mutation_allowed
from ..module import human as human_module
from ..util.resources import INSTANCE
from . import common, instances

STATE_PROPERTY = "oxal_mc_studio_human_ui"
FIELDS = {
    "functions_open": ("人模功能", False), "armor_open": ("盔甲设置", False),
    "layer_open": ("3D二层设置", False), "back_open": ("披风与鞘翅设置", False),
    "outline_open": ("描边设置", False),
    "skins_open": ("皮肤", False), "binding_open": ("物品绑定", False),
    "arm_mode": ("胳膊", "SLIM"), "leg_mode": ("腿", "NORMAL"),
    "name_enabled": ("名字", "OFF"), "leg_fixed": ("腿部固定", "OFF"),
    "foot_fixed": ("脚腕固定", "OFF"), "wrist": ("绑定目标", "L"),
    "armor_target": ("盔甲部位", "ALL"), "armor_material": ("材质", "5"),
    "armor_pattern": ("纹饰", "0"),
}
ENUMS = {
    "arm_mode": ("胳膊", (("SLIM", "3格", ""), ("WIDE", "4格", "")), "SLIM"),
    "leg_mode": ("腿", (("NORMAL", "常规弯曲", ""), ("OFFICIAL", "官方动画", "")), "NORMAL"),
    "wrist": ("绑定目标", (("L", "左手", ""), ("R", "右手", ""), ("H", "头", "")), "L"),
    "armor_target": ("部位", (("ALL", "全部", ""), ("HEAD", "头盔", ""), ("CHEST", "胸甲", ""), ("LEGS", "护腿", ""), ("FEET", "靴子", "")), "ALL"),
    "armor_material": ("材质", tuple((str(i), label, "") for i, label in enumerate(MATERIALS)), "5"),
    "armor_pattern": ("纹饰", tuple((str(i), label, "") for i, label in enumerate(PATTERNS)), "0"),
    "skin_source": ("皮肤列表", (("LIBRARY", "皮肤库", ""), ("RECENT", "最近使用", "")), "LIBRARY"),
}
for _name in ("name_enabled", "leg_fixed", "foot_fixed"):
    ENUMS[_name] = (FIELDS[_name][0], (("OFF", "关", ""), ("ON", "开", "")), "OFF")


class OXALMCSTUDIO_PG_human_file(PropertyGroup):
    label: StringProperty(options={"SKIP_SAVE"})
    filepath: StringProperty(options={"SKIP_SAVE"})


class OXALMCSTUDIO_UL_human_files(UIList):
    def draw_item(self, context: Any, layout: Any, data: Any, item: Any, icon: Any, active_data: Any, active_property: Any, index: int | None = 0, flt_flag: int | None = 0) -> None:
        layout.label(text=item.label, icon="IMAGE_DATA")


class OXALMCSTUDIO_PG_human_ui(PropertyGroup):
    """Only folds, initial import choices and library selection are transient."""


def _style_snapshot(state):
    root, legacy = instances.current_human(bpy.context, state)
    defaults = {name: ENUMS[name][2] for name in ("arm_mode", "leg_mode", "name_enabled", "leg_fixed", "foot_fixed")}
    if legacy is not None:
        from ..business.human import human_compat
        from ..business.human.human_compat_controls import settings
        from ..business.human.human_state import ARMS, LEGS

        defaults.update(arm_mode=human_compat._style(legacy, ARMS), leg_mode=human_compat._style(legacy, LEGS))
        basic = settings(legacy)
        for name in ("name_enabled", "leg_fixed", "foot_fixed"):
            defaults[name] = "MIXED" if basic[name] is None else "ON" if basic[name] else "OFF"
        owner = legacy.rig
    elif root is not None:
        values = parameter_owner(root)
        defaults.update(arm_mode="WIDE" if values.get("oxal_human_arm_mode") else "SLIM",
                        leg_mode="OFFICIAL" if values.get("oxal_human_leg_mode") else "NORMAL")
        for name in ("name_enabled", "leg_fixed", "foot_fixed"):
            defaults[name] = "ON" if values.get("oxal_human_" + name) else "OFF"
        owner = root
    else:
        return "", defaults
    return f"{cast(Scene, bpy.context.scene).session_uid}:{owner.session_uid}", defaults


def _style_property(name):
    items = ENUMS[name][1] + (("MIXED", "混合／未确定", ""),)
    identifiers = tuple(item[0] for item in items)

    def get_value(state):
        try:
            owner, values = _style_snapshot(state)
            if owner and state.style_draft_owner == owner:
                values.update(json.loads(state.style_draft_values))
            return identifiers.index(values[name])
        except (ValueError, TypeError, KeyError):
            return 0

    def set_value(state, value):
        if not 0 <= value < len(identifiers):
            return
        owner, _values = _style_snapshot(state)
        if not owner:
            return
        values = json.loads(state.style_draft_values) if state.style_draft_owner == owner else {}
        values[name] = identifiers[value]
        state.style_draft_owner = owner
        state.style_draft_values = json.dumps(values)

    return EnumProperty(name=ENUMS[name][0], items=items, get=get_value, set=set_value, options={"SKIP_SAVE"})


def _legacy_red_property():
    def get_value(state):
        try:
            from ..business.human.human_compat_controls import settings

            owner, _values = _style_snapshot(state)
            if owner and state.style_draft_owner == owner:
                draft = json.loads(state.style_draft_values)
                if "red" in draft:
                    return float(draft["red"])
            legacy = instances.selected_legacy(bpy.context, state)
            value = settings(legacy)["red"] if legacy is not None else None
            return float(value) if value is not None else 0.0
        except (ValueError, TypeError, KeyError):
            return 0.0

    def set_value(state, value):
        owner, _values = _style_snapshot(state)
        if not owner:
            return
        draft = json.loads(state.style_draft_values) if state.style_draft_owner == owner else {}
        draft["red"] = float(value)
        state.style_draft_owner = owner
        state.style_draft_values = json.dumps(draft)

    return FloatProperty(name="红温", min=0, max=1, get=get_value, set=set_value, options={"SKIP_SAVE"})


def _path_index(rows_name, selected_name):
    def get_index(state):
        return next((i for i, row in enumerate(getattr(state, rows_name)) if row.filepath == getattr(state, selected_name)), -1)

    def set_index(state, index):
        rows = getattr(state, rows_name)
        if 0 <= index < len(rows):
            setattr(state, selected_name, rows[index].filepath)

    return IntProperty(min=-1, get=get_index, set=set_index, options={"SKIP_SAVE"})


OXALMCSTUDIO_PG_human_ui.__annotations__ = common.annotations(FIELDS, ENUMS, {
    "leather_color": ("皮革颜色", (0.5, 0.25, 0.1, 1.0)),
    "trim_color": ("纹饰颜色", (1.0, 0.65, 0.1, 1.0)),
})
OXALMCSTUDIO_PG_human_ui.__annotations__.update({
    "library_rows": CollectionProperty(type=OXALMCSTUDIO_PG_human_file, options={"SKIP_SAVE"}),
    "library_index": IntProperty(default=0, options={"SKIP_SAVE"}),
    "library_kind": StringProperty(default="skins", options={"SKIP_SAVE"}),
    "legacy_skin_image": PointerProperty(type=Image, options={"SKIP_SAVE"}),
    "recent_rows": CollectionProperty(type=OXALMCSTUDIO_PG_human_file, options={"SKIP_SAVE"}),
    "recent_scene_uid": StringProperty(options={"SKIP_SAVE"}),
    "recent_owner_uid": StringProperty(options={"SKIP_SAVE"}),
    "recent_filepath": StringProperty(options={"SKIP_SAVE"}),
    "recent_error": StringProperty(options={"SKIP_SAVE"}),
    "recent_index": _path_index("recent_rows", "recent_filepath"),
    "skin_library_filepath": StringProperty(options={"SKIP_SAVE"}),
    "skin_library_index": _path_index("library_rows", "skin_library_filepath"),
    "style_draft_owner": StringProperty(options={"SKIP_SAVE"}),
    "style_draft_values": StringProperty(default="{}", options={"SKIP_SAVE"}),
    "edit_legacy_red": _legacy_red_property(),
})
OXALMCSTUDIO_PG_human_ui.__annotations__.update({"edit_" + name: _style_property(name)
                                             for name in ("arm_mode", "leg_mode", "name_enabled", "leg_fixed", "foot_fixed")})
OXALMCSTUDIO_PG_human_ui.__annotations__.update(instances.selection_properties("oxal.human"))


def _button(layout: Any, label: str, action: str, icon: str = "NONE", *, operation: str = "oxal_mc_studio.human_command", **values: Any) -> Any:
    depress = bool(values.pop("depress", False))
    op = layout.operator(operation, text=label, icon=icon, depress=depress)
    op.action = action
    for name, value in values.items():
        setattr(op, name, value)
    return op


def _back_button(layout: Any, label: str, action: str, icon: str = "NONE", **values: Any) -> Any:
    return _button(layout, label, action, icon, operation="oxal_mc_studio.human_back_command", **values)


def _file(layout: Any, label: str, action: str, kind: str, icon: str = "FILE_TICK") -> None:
    op = layout.operator("oxal_mc_studio.human_file", text=label, icon=icon)
    op.action, op.kind = action, kind


def _prop(layout: Any, root: Any, name: str, text: str | None = None) -> None:
    layout.prop(parameter_owner(root), path(name), text=text if text is not None else PARAMETERS[name][0])


def _library(layout: Any, state: Any, kind: str) -> None:
    row = layout.row(align=True)
    _file(row, "入库 PNG" if kind == "skins" else "入库 PNG / ZIP", "library_add", kind, "ADD")
    _button(row, "刷新列表", "library_refresh", "FILE_REFRESH", kind=kind)
    if state.library_kind == kind:
        layout.template_list("OXALMCSTUDIO_UL_human_files", kind, state, "library_rows", state, "library_index", rows=3)
        _button(layout, "应用所选", "library_skin" if kind == "skins" else "library_pack", "CHECKMARK", kind=kind)
    if kind == "skins" and (state.library_kind != kind or not state.library_rows):
        row = layout.row(align=True)
        for label, file in builtin_skins():
            _button(row, label, "skin", "IMAGE_DATA", kind=kind, filepath=str(file))
    if kind != "skins":
        row = layout.row(align=True)
        _file(row, "选文件应用", "pack", kind)
        _button(row, "恢复原图", "pack_restore", "LOOP_BACK", kind=kind)


def _skin_browser(ui, layout, owner=None):
    state = ui.state
    tabs = layout.row(align=True)
    _button(tabs, "皮肤库", "skin_library_view", "ASSET_MANAGER", depress=state.skin_source == "LIBRARY")
    _button(tabs, "最近使用", "skin_recent_list", "RECOVER_LAST", depress=state.skin_source == "RECENT")
    split = layout.split(factor=.65) if ui.wide() else None
    listing = split.column() if split else layout.column()
    actions = split.column(align=True) if split else layout.row(align=True)
    if state.skin_source == "LIBRARY":
        if state.library_kind == "skins":
            listing.template_list("OXALMCSTUDIO_UL_human_files", "skin_library", state, "library_rows",
                                  state, "skin_library_index", rows=5, maxrows=5)
        else:
            listing.label(text="点击皮肤库载入列表", icon="INFO")
        apply = actions.column(align=True)
        apply.enabled = owner is not None and state.library_kind == "skins" and bool(state.skin_library_filepath)
        _button(apply, "应用所选", "library_skin", "CHECKMARK", kind="skins")
        _file(actions, "入库 PNG", "library_add", "skins", "ADD")
        _button(actions, "刷新", "skin_library_view", "FILE_REFRESH")
    else:
        current = (owner is not None and state.recent_scene_uid == str(ui.context.scene.session_uid)
                   and state.recent_owner_uid == str(owner.session_uid))
        if current:
            listing.template_list("OXALMCSTUDIO_UL_human_files", "skin_recent", state, "recent_rows",
                                  state, "recent_index", rows=5, maxrows=5)
            if state.recent_error:
                ui.note(listing, state.recent_error, "INFO")
            elif not state.recent_rows:
                listing.label(text="此人模暂无最近使用", icon="INFO")
        else:
            listing.label(text="点击最近使用载入当前人模记录", icon="INFO")
        apply = actions.column(align=True)
        apply.enabled = current and bool(state.recent_filepath) and not state.recent_error
        _button(apply, "应用所选", "recent_skin", "CHECKMARK", kind="skins")
        refresh = actions.column(align=True)
        refresh.enabled = owner is not None
        _button(refresh, "刷新", "skin_recent_list", "FILE_REFRESH")
    choose = actions.column(align=True)
    choose.enabled = owner is not None
    _file(choose, "选择 PNG", "skin", "skins", "FILE_IMAGE")
    default = actions.column(align=True)
    default.enabled = owner is not None
    _button(default, "默认皮肤", "skin", "LOOP_BACK")


def _selected(context: Any, state: Any) -> Any:
    candidate = instances.selected_root(context, state, "oxal.human")
    return resolve_human(context.scene, str(candidate[INSTANCE]))


def _compat_button(layout: Any, label: str, feature: str, *, icon: str = "NONE",
                   values: dict[str, Any] | None = None, parts: tuple[str, ...] = (),
                   filepath: str = "", depress: bool = False, red_popup: bool = False) -> None:
    layout.operator_context = "INVOKE_DEFAULT"
    op = layout.operator("oxal_mc_studio.human_compat", text=label, icon=icon, depress=depress)
    op.feature = feature
    op.values_json = json.dumps(values or {})
    op.parts_json = json.dumps(parts)
    op.filepath = filepath
    op.red_popup = red_popup


class _LegacyChoice(Menu):
    field: ClassVar[str] = ""
    choices: ClassVar[tuple[tuple[str | bool, str], ...]] = ()

    def draw(self, context):
        from ..business.human.human_compat_controls import settings

        layout = self.layout
        if layout is None:
            return
        state = getattr(context.window_manager, STATE_PROPERTY)
        target = instances.selected_legacy(context, state)
        if target is None:
            layout.label(text="请先选中旧人模", icon="INFO")
            return
        current = settings(target)["foot_fixed"] if self.field == "foot_fixed" else None
        animated = self.field == "foot_fixed" and any(animation and (animation.action or animation.nla_tracks)
                                                     for animation in (target.rig.animation_data, target.rig.data.animation_data))
        for value, label in self.choices:
            row = layout.row()
            row.enabled = not animated or value == current
            _compat_button(row, label, "basic_settings", values={self.field: value})
        if animated:
            layout.label(text="已有动画，保留初始绑定", icon="LOCKED")


class OXALMCSTUDIO_MT_legacy_arm(_LegacyChoice):
    bl_label = "胳膊"
    field = "arm_mode"
    choices = (("SLIM", "3格"), ("WIDE", "4格"))


class OXALMCSTUDIO_MT_legacy_leg(_LegacyChoice):
    bl_label = "腿"
    field = "leg_mode"
    choices = (("NORMAL", "常规弯曲"), ("OFFICIAL", "官方动画"))


class OXALMCSTUDIO_MT_legacy_name(_LegacyChoice):
    bl_label = "名字"
    field = "name_enabled"
    choices = ((False, "关"), (True, "开"))


class OXALMCSTUDIO_MT_legacy_leg_fixed(_LegacyChoice):
    bl_label = "腿部固定"
    field = "leg_fixed"
    choices = ((False, "关"), (True, "开"))


class OXALMCSTUDIO_MT_legacy_foot_fixed(_LegacyChoice):
    bl_label = "脚腕固定"
    field = "foot_fixed"
    choices = ((False, "关"), (True, "开"))


LEGACY_MENUS = (OXALMCSTUDIO_MT_legacy_arm, OXALMCSTUDIO_MT_legacy_leg,
                OXALMCSTUDIO_MT_legacy_name, OXALMCSTUDIO_MT_legacy_leg_fixed,
                OXALMCSTUDIO_MT_legacy_foot_fixed)


def _compat_draw(ui: common.UI, box: Any, target: Any) -> None:
    from ..business.human import human_compat

    state = ui.state
    data = human_compat.summary(target)
    content = box.column()
    content.enabled = target.editable
    if target.reason:
        ui.note(box, target.reason, "INFO")
    functions = content.box()
    _button(functions, "人模功能", "legacy_functions", "TRIA_DOWN" if state.functions_open else "TRIA_RIGHT")
    if state.functions_open:
        functions.label(text="所选人模样式")
        from ..business.human.human_compat_controls import can_edit, red_channel

        for menu in LEGACY_MENUS:
            row = functions.row()
            if menu.field in {"name_enabled", "leg_fixed", "foot_fixed"}:
                row.enabled = can_edit(target, menu.field)
            current = data[menu.field]
            caption = next((label for value, label in menu.choices if value == current), "混合／未确定")
            row.label(text=menu.bl_label)
            row.menu(menu.__name__, text=caption)
        channel = red_channel(target)
        if channel is not None:
            functions.prop(channel[0], channel[1], text="红温", slider=True)
        else:
            _compat_button(functions, "红温…", "prepare_controls", red_popup=True)
        ui.note(functions, "脚腕固定修改初始绑定；已有 Action/NLA 时保持原绑定。", "INFO")
        armor = ui.section(functions, "armor_open", "SHADING_RENDERED")
        if armor is not None:
            armor.prop(state, "armor_target")
            parts = PARTS if state.armor_target == "ALL" else (state.armor_target.lower(),)
            available = tuple(part for part in parts if part in data["armor_available"])
            present = tuple(part for part in parts if part in data["armor_parts"])
            controls = armor.column(align=True)
            controls.enabled = bool(available)
            row = controls.row(align=True)
            hide = row.row(align=True)
            hide.enabled = bool(present)
            _compat_button(hide, "隐藏所选", "armor_visibility", values={"enabled": False}, parts=present)
            _compat_button(row, "添加／显示所选", "armor_visibility", values={"enabled": True}, parts=available)
            if present:
                _compat_button(controls, "材质／纹饰／厚度", "armor_settings", icon="PREFERENCES", parts=present)
            if not available:
                armor.label(text="旧人模中没有此部位", icon="INFO")
            elif not data["armor_selector"]:
                ui.note(armor, "按原版本补齐所选部位；材质与厚度沿用原生通道。", "INFO")
        layer = ui.section(functions, "layer_open", "MOD_SOLIDIFY")
        if layer is not None:
            layer.label(text=f'现有二层：{data["layer_count"]} 个')
            controls = layer.column(align=True)
            controls.enabled = bool(data["layer_count"])
            row = controls.row(align=True)
            _compat_button(row, "关闭二层", "layer_visibility", values={"enabled": False})
            _compat_button(row, "开启二层", "layer_visibility", values={"enabled": True})
            if data["layer_3d"]:
                _compat_button(controls, "调整二层3D参数", "layer_settings", icon="MOD_SOLIDIFY")
            elif not data["layer_count"]:
                ui.note(layer, "未确认绑定当前人模的二层对象；未绑定数据保持原状。", "INFO")
            else:
                layer.label(text="此版本没有原生二层3D参数", icon="INFO")
    skin = ui.section(content, "skins_open", "TEXTURE")
    if skin is not None:
        skin.label(text="当前：" + data["skin_label"])
        skin.prop_search(state, "legacy_skin_image", bpy.data, "images", text="原图通道")
        skin.label(text="未指定时自动识别主皮肤", icon="INFO")
        _skin_browser(ui, skin, target.rig)
        skin.label(text="沿用原网格与 UV；不转换旧头型", icon="INFO")


def draw(layout: Any, context: Any) -> None:
    from ..business.human import human_compat

    state = getattr(context.window_manager, STATE_PROPERTY)
    ui = common.UI(context, state, FIELDS)
    box = layout.box()
    box.enabled = mutation_allowed()
    if not mutation_allowed():
        layout.label(text="渲染期间暂停人模修改", icon="RENDER_STILL")
    try:
        root = _selected(context, state)
    except ValueError:
        root = None
    invalid_choice = ""
    try:
        legacy = instances.selected_legacy(context, state)
        if legacy is None and not instances.has_human_choice(state):
            legacy = human_compat.selected(context)
    except ValueError as exc:
        legacy = None
        invalid_choice = str(exc)
    version = legacy.version if legacy is not None else model_version(root) if root is not None else None
    compatible = legacy is not None or root is not None and version != CURRENT
    instances.status(ui, box, human_module.endpoint, "oxal.human", "人模")
    instances.draw(ui, box, "human", (("导入", "IMPORT", "oxal_mc_studio.import_human", False), ("游标", "CURSOR", "oxal_mc_studio.import_human", True)),
                   caption="选中：V" + version if version else "选中：—",
                   description="已开启兼容模式" if compatible else "")
    box.label(text="最新版：V" + CURRENT)
    row = box.row(align=True)
    if legacy is not None:
        _compat_button(row, "选择实例", "select", icon="RESTRICT_SELECT_OFF")
    else:
        row.enabled = root is not None
        _button(row, "选择实例", "select", "RESTRICT_SELECT_OFF")
        _button(row, "移除实例", "remove", "TRASH")
    repair = box.column(align=True)
    repair.enabled = root is not None and legacy is None
    _button(repair, "修复已有 Beta 实例", "repair", "FILE_REFRESH")
    box.operator("oxal_mc_studio.human_migrate", text="迁移旧版人模（2.3.1+）", icon="FILE_REFRESH")
    status = human_module.commands.state(context.scene.session_uid)
    if status.message:
        box.label(text=status.message, icon="CHECKMARK" if status.succeeded else "INFO")
    if invalid_choice:
        ui.note(box, invalid_choice, "INFO")
        return
    if legacy is not None:
        _compat_draw(ui, box, legacy)
        return
    functions = ui.section(box, "functions_open", "TOOL_SETTINGS")
    if functions is not None:
        if root is not None and model_version(root) == "2.4.3":
            _button(functions, "修复头部闭眼叠面", "head_eye_repair", "FILE_REFRESH")
        functions.label(text="所选人模样式" if root else "新实例初始样式")
        for name in ("arm_mode", "leg_mode", "name_enabled", "leg_fixed", "foot_fixed"):
            functions.prop(state, "edit_" + name if root else name)
        if root:
            redness = functions.row(align=True)
            _prop(redness, root, "red", "红温")
            _button(redness, "", "red_key", "KEY_HLT")
            _button(functions, "应用样式与姿态固定", "apply_style", "CHECKMARK")
            functions.label(text="修改脚腕初始绑定前请保持无 Action/NLA", icon="INFO")
        armor = ui.section(functions, "armor_open", "SHADING_RENDERED")
        if armor is not None:
            armor.enabled = root is not None
            armor.prop(state, "armor_target")
            row = armor.row(align=True)
            target_title = {"ALL": "全部", "HEAD": "头盔", "CHEST": "胸甲", "LEGS": "护腿", "FEET": "靴子"}[state.armor_target]
            _button(row, "关闭" + target_title, "armor_disable")
            _button(row, "开启" + target_title, "armor_enable")
            armor.prop(state, "armor_material")
            armor.prop(state, "armor_pattern")
            if state.armor_material == "0":
                armor.prop(state, "leather_color")
            if state.armor_pattern != "0":
                armor.prop(state, "trim_color")
            _button(armor, "应用材质与纹饰到所选部位", "armor_apply", "CHECKMARK")
            if root:
                controller = parameter_owner(root)
                parts = PARTS if state.armor_target == "ALL" else (state.armor_target.lower(),)
                for offset in range(0, len(parts), 2):
                    row = armor.row(align=True)
                    for part in parts[offset:offset + 2]:
                        col = row.column(align=True)
                        title = {"head": "头盔", "chest": "胸甲", "legs": "护腿", "feet": "靴子"}[part]
                        _prop(col, root, part + "_3d", title + " 3D")
                        if parameter_owner(root).get("oxal_human_" + part + "_3d"):
                            _prop(col, root, part + "_thickness", "厚度")
                if state.armor_target != "ALL":
                    from ..business.human.human_armor import objects

                    part = parts[0]
                    if objects(root, part):
                        _prop(armor, root, part + "_enabled", "可见性（可插帧）")
                    else:
                        armor.label(text="尚未添加此部位", icon="INFO")
                from ..business.human.human_armor import objects as armor_objects
                from .enchantment import draw_armor_controls

                enchant = armor.box()
                enchant.label(text="盔甲附魔 · " + target_title)
                has_armor = any(armor_objects(root, part) for part in parts)
                actions = enchant.column(align=True)
                actions.enabled = has_armor
                row = actions.row(align=True)
                _button(row, "一键附魔", "armor_enchant", "SHADING_RENDERED")
                _button(row, "移除附魔", "armor_unenchant", "X")
                _button(actions, "一键添加附魔描边", "armor_enchant_outline_enable", "MOD_SOLIDIFY")
                _button(actions, "一键添加像素格流光", "armor_enchant_pixels_enable", "SHADING_RENDERED")
                if not has_armor:
                    enchant.label(text="先开启所选盔甲部位", icon="INFO")
                draw_armor_controls(enchant, root, parts, ui_state=state)
                from ..business.human.human_armor_outline import (
                    prepared as armor_outline_prepared,
                )

                own_outline = armor.box()
                own_outline.label(text="盔甲描边（仅非3D）")
                if not armor_outline_prepared(root, parts):
                    actions = own_outline.column(align=True)
                    actions.enabled = has_armor
                    _button(actions, "准备所选部位描边", "armor_outline_prepare", "MOD_SOLIDIFY")
                for offset in range(0, len(parts), 2):
                    row = own_outline.row(align=True)
                    for part in parts[offset:offset + 2]:
                        col = row.column(align=True)
                        title = {"head": "头盔", "chest": "胸甲", "legs": "护腿", "feet": "靴子"}[part]
                        if not armor_outline_prepared(root, (part,)):
                            col.label(text=title + "：尚未准备")
                            continue
                        col.enabled = not controller.get("oxal_human_" + part + "_3d", False)
                        _prop(col, root, part + "_outline_follow_human", title + " 跟随人模")
                        independent = col.column(align=True)
                        independent.enabled = not controller.get("oxal_human_" + part + "_outline_follow_human", True)
                        _prop(independent, root, part + "_outline_enabled", title + " 描边")
                        _prop(independent, root, part + "_outline_color", "颜色")
                        _prop(independent, root, part + "_outline_width", "粗细")
                        if "oxal_human_" + part + "_outline_quality_normals" in controller:
                            _prop(independent, root, part + "_outline_quality_normals", "高质量法线")
                if armor_outline_prepared(root):
                    row = own_outline.row(align=True)
                    _button(row, "重建所选描边", "armor_outline_repair", "FILE_REFRESH")
                    _button(row, "移除所选描边", "armor_outline_remove", "X")
            _library(armor, state, "armor")
        layer = ui.section(functions, "layer_open", "MOD_SOLIDIFY")
        if layer is not None and root:
            for name in ("layer_enabled", "outer", "inner", "random_enabled", "random_factor", "height_ratio", "seed", "center_falloff", "center_range"):
                _prop(layer, root, name)
        outline = ui.section(functions, "outline_open", "MOD_SOLIDIFY")
        if outline is not None and root:
            from ..business.human.human_outline import prepared as outline_prepared

            outline.label(text="头部、身体、二层、背饰及跟随的盔甲")
            if outline_prepared(root):
                _prop(outline, root, "outline_enabled", "启用描边")
                row = outline.row(align=True)
                _prop(row, root, "outline_color", "颜色")
                _prop(row, root, "outline_width", "粗细")
                if "oxal_human_outline_quality_normals" in parameter_owner(root):
                    _prop(outline, root, "outline_quality_normals", "高质量法线")
                row = outline.row(align=True)
                _button(row, "重建描边", "outline_repair", "FILE_REFRESH")
                _button(row, "移除描边", "outline_remove", "X")
            else:
                _button(outline, "启用人模描边", "outline_prepare", "MOD_SOLIDIFY")
        back = ui.section(functions, "back_open", "OUTLINER_OB_ARMATURE")
        if back is not None and root:
            current = model_version(root) in {"2.4.2", "2.4.3", "2.4.4", "2.4.5", "2.4.6", "2.4.7"}
            back.label(text="背饰模式（切换时增删专属部件）")
            row = back.row(align=True)
            for value, label in enumerate(("关闭", "鞘翅", "披风") if current else ("关闭", "鞘翅", "单段", "五段")):
                _back_button(row, label, "parameters", values_json=json.dumps({"back_mode": value}), depress=parameter_owner(root).get("oxal_human_back_mode") == value)
            from ..business.human.human import rig_of
            from ..business.human.human_back import control

            current_mode = parameter_owner(root).get("oxal_human_back_mode", 0)
            _back_button(back, "修复当前背饰" if current else "按196修复当前背饰", "parameters", "FILE_REFRESH", values_json=json.dumps({"back_mode": current_mode}))
            if current_mode == 1:
                opening = control(root, rig_of(root), "elytra")
                if opening is not None:
                    back.prop(opening, "location", index=1, text="原生开合位置")
                    row = back.row(align=True)
                    _back_button(row, "收回", "back_control", kind="elytra", value="0")
                    _back_button(row, "展开", "back_control", kind="elytra", value="1")
                    _back_button(back, "定位鞘翅控制骨", "back_control_select", "BONE_DATA", kind="elytra")
            elif current_mode in {2, 3}:
                _back_button(back, "定位披风根控制骨", "back_control_select", "BONE_DATA", kind="cape")
                if current:
                    row = back.row(align=True)
                    for kind, label in (("cape_start", "首点"), ("cape_middle", "中点"), ("cape_target", "尾点")):
                        _back_button(row, label, "back_control_select", "BONE_DATA", kind=kind)
                elif current_mode == 2:
                    _back_button(back, "定位披风目标骨", "back_control_select", "BONE_DATA", kind="cape_target")
                else:
                    row = back.row(align=True)
                    for index in range(1, 7):
                        _back_button(row, str(index), "back_control_select", kind=f"cape_segment_{index}")
                back.label(text="定位控制骨后直接进入姿态模式")
            _prop(back, root, "cape_index")
            for kind, label in (("cape", "披风材质包库"), ("elytra", "鞘翅材质包库")):
                back.label(text=label)
                _library(back, state, kind)
    binding = ui.section(box, "binding_open", "CONSTRAINT")
    if binding is not None:
        binding.enabled = root is not None
        binding.prop(state, "wrist")
        binding.label(text="选择人模以外的物品，再执行绑定")
        row = binding.row(align=True)
        _button(row, "绑定", "bind", "ARMATURE_DATA")
        _button(row, "解除", "unbind", "X")
    skin = ui.section(box, "skins_open", "TEXTURE")
    if skin is not None:
        skin.label(text="当前：" + str(root.get("oxal_human_skin_label", "默认皮肤")) if root else "尚未选择人模")
        if root and root.get("oxal_human_skin_pixels") in {64, 128}:
            pixels = root["oxal_human_skin_pixels"]
            skin.label(text=f"二层3D像素：{pixels}×{pixels}（随皮肤切换）")
        _skin_browser(ui, skin, root)
        skin.label(text="支持 64×64 / 128×128 PNG", icon="INFO")
    ui.note(box, "远离世界原点且手臂接近伸直时，坐标精度误差可能放大为转腰抖动；可将场景移近原点或让手臂略微弯曲。", "INFO")


def register() -> None:
    registered = []
    try:
        for cls in (OXALMCSTUDIO_PG_human_file, OXALMCSTUDIO_UL_human_files, *LEGACY_MENUS):
            bpy.utils.register_class(cls)
            registered.append(cls)
        common.register_state(OXALMCSTUDIO_PG_human_ui, STATE_PROPERTY)
    except Exception:
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        raise


def unregister() -> None:
    common.unregister_state(OXALMCSTUDIO_PG_human_ui, STATE_PROPERTY)
    for cls in reversed((OXALMCSTUDIO_PG_human_file, OXALMCSTUDIO_UL_human_files, *LEGACY_MENUS)):
        bpy.utils.unregister_class(cls)
