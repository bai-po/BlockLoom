"""Native asset library controls; drawing never reads files or mutates scenes."""

import json
import logging
from functools import lru_cache
from typing import Any, cast

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Object, PropertyGroup, UIList

from ..business.asset.asset_effects import CONTROL_LABELS
from ..business.asset.asset_poses import POSES
from ..business.asset.asset_rigs import ALIASES, control_bone_name
from ..business.asset.assets import IDENTIFIER, root_of, roots
from ..core.runtime import mutation_allowed
from ..module import assets
from ..util.resources import INSTANCE, VERSION
from . import asset_rail, character_wrist, common

logger = logging.getLogger(__name__)
STATE_PROPERTY = "oxal_mc_studio_assets_ui"
_enum_cache = {}
_display_cache = {}
_shape_cache = {}
_DESTINATIONS = (("ORIGIN", "原点导入", "", 0), ("CURSOR", "游标导入", "", 1), ("SLOT", "物品栏导入", "", 2), ("DISPLAY", "展示位导入", "", 3))
_destination_cache = {}
_POTION_ITEMS = (("poison","剧毒","",0),)
_SPAWNER_ITEMS = (("pig","猪","",0),("none","无","",1))


def slot_phase_items(_self, context):
    from ..business.asset.asset_slot_states import ITEMS
    active = root_of(getattr(context, "object", None)) if context else None
    return ITEMS[:4] if active is None or active.get(IDENTIFIER) != "minecraft:crossbow" else ITEMS


def spawner_items(self,context):
    return _SPAWNER_ITEMS


def publish_spawner_items(rows):
    global _SPAWNER_ITEMS
    _SPAWNER_ITEMS = tuple((row["key"],row["display"],"",index) for index,row in enumerate(rows))


def potion_items(self,context):
    return _POTION_ITEMS


def publish_potion_items(rows):
    global _POTION_ITEMS
    if not rows:
        return
    _POTION_ITEMS = tuple((row["key"],row["display"],"",index) for index,row in enumerate(sorted(rows,key=lambda row:row["key"] != "poison")))
_DISPLAY_SLOTS = (("1", "槽 1", "", 0), ("2", "槽 2", "", 1), ("3", "槽 3", "", 2))
_STATE_LABELS = {
    "copper_golem_pose":"雕像姿势",
    "facing": "朝向",
    "axis": "轴向",
    "waterlogged": "含水",
    "powered": "充能",
    "lit": "点燃",
    "open": "开启",
    "half": "部分",
    "type": "类型",
    "shape": "形状",
    "hinge": "门轴",
    "part": "部分",
    "age": "生长阶段",
    "level": "等级",
    "rotation": "旋转",
    "north": "北侧",
    "south": "南侧",
    "east": "东侧",
    "west": "西侧",
    "up": "上侧",
    "down": "下侧",
}
_VALUE_LABELS = {
    "standing":"站立", "sitting":"坐姿", "running":"奔跑", "star":"星形",
    "true": "开",
    "false": "关",
    "north": "北",
    "south": "南",
    "east": "东",
    "west": "西",
    "up": "上",
    "down": "下",
    "top": "顶部",
    "bottom": "底部",
    "upper": "上半",
    "lower": "下半",
    "left": "左",
    "right": "右",
    "straight": "直线",
    "inner_left": "内角左",
    "inner_right": "内角右",
    "outer_left": "外角左",
    "outer_right": "外角右",
    "head": "床头",
    "foot": "床尾",
    "double": "双层",
}
CATEGORIES = (
    ("ALL", "全部", ""),
    ("building_blocks", "建筑方块", ""),
    ("colored_blocks", "染色方块", ""),
    ("natural_blocks", "自然方块", ""),
    ("functional_blocks", "功能方块", ""),
    ("redstone_blocks", "红石方块", ""),
    ("tools_and_utilities", "工具与实用物品", ""),
    ("combat", "战斗用品", ""),
    ("food_and_drinks", "食物与饮品", ""),
    ("ingredients", "原材料", ""),
    ("spawn_eggs", "刷怪蛋", ""),
    ("op_blocks", "管理员用品", ""),
)


class OXALMCSTUDIO_PG_asset_row(PropertyGroup):
    identifier: StringProperty()
    label: StringProperty()
    icon_id: IntProperty()
    schema_json: StringProperty(default="{}")
    defaults_json: StringProperty(default="{}")
    shape_schema_json: StringProperty(default="{}")


def variant_items(self, context):
    return _enum_cache.get(self.options_key, (("", "无", ""),))


class OXALMCSTUDIO_PG_asset_variant(PropertyGroup):
    key: StringProperty()
    options_key: StringProperty()
    value: EnumProperty(name="状态", items=variant_items)


def populate_variants(state, schema, values=None):
    state.variants.clear()
    for key, options in schema.items():
        token = key + ":" + "|".join(options)
        _enum_cache[token] = tuple(
            (value, _VALUE_LABELS.get(value, value), "") for value in options
        )
        row = state.variants.add()
        row.key = key
        row.options_key = token
        default = (
            "false"
            if "false" in options
            else "north"
            if "north" in options
            else options[0]
        )
        row.value = (values or {}).get(key, default)


def selected_changed(self, context):
    self.variant_instance = ""
    self.shape_schema_json = "{}"
    self.shape_choice = "0"
    if 0 <= self.index < len(self.rows):
        row = self.rows[self.index]
        self.shape_schema_json = row.shape_schema_json
        self.shape_choice = "0"
        populate_variants(
            self, json.loads(row.schema_json), json.loads(row.defaults_json)
        )
    else:
        self.variants.clear()


def catalog_changed(self, context):
    """A committed UI edit uses the same explicit submission as its button."""
    if context is None or context.scene is None or context.window_manager is None:
        return
    state = getattr(context.window_manager, STATE_PROPERTY, None)
    if state is None or state.as_pointer() != self.as_pointer():
        return
    operation = cast(Any, bpy.ops).oxal_mc_studio.asset_action
    if operation.poll():
        operation("EXEC_DEFAULT", action="CATALOG")


def library_changed(self, context):
    if not self.library_open or context is None or context.scene is None:
        return
    operation = cast(Any, bpy.ops).oxal_mc_studio.asset_action
    if operation.poll():
        operation("EXEC_DEFAULT", action="CATALOG_OPEN")


def shape_choices(self, context):
    from ..business.asset.asset_catalog import shape_items
    values = {row.key: row.value for row in self.variants}
    token = self.shape_schema_json + json.dumps(values, sort_keys=True)
    if token not in _shape_cache:
        _shape_cache[token] = shape_items(json.loads(self.shape_schema_json), values)
    return _shape_cache[token]


def publish_shapes(state, specification, value=0):
    state.shape_schema_json = json.dumps(specification)
    choices = shape_choices(state, None)
    selected = str(value)
    state.shape_choice = selected if selected in {row[0] for row in choices} else choices[0][0]


def destination_items(self, context):
    from ..business.asset.asset_targets import selected_targets
    available = selected_targets(context) if context is not None else {}
    token = tuple(key for key in ("SLOT", "DISPLAY") if key in available)
    if token not in _destination_cache:
        _destination_cache[token] = tuple(item for item in _DESTINATIONS if item[0] in {"ORIGIN", "CURSOR", *token})
    return _destination_cache[token]


def display_items(self, context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return (("NONE", "请选择展示框或木展示架", ""),)
    values = [("NONE", "请选择展示框或木展示架", "", 0)]
    values.extend(
        (root[INSTANCE], root.name, "", int(root[INSTANCE][:7], 16) + 1)
        for root in roots(scene)
        if root.get("oxal_mc_studio_display_slots")
    )
    _display_cache[scene.session_uid] = tuple(values)
    return _display_cache[scene.session_uid]


def display_target_changed(self, context):
    self.display_slot = "1"


def display_slot_items(self, context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return _DISPLAY_SLOTS[:1]
    target = next(
        (
            root
            for root in roots(scene)
            if root[INSTANCE] == self.display_target
        ),
        None,
    )
    active = root_of(context.object)
    if active is not None and active.get("oxal_mc_studio_display_slots"):
        target = active
    count = int(target.get("oxal_mc_studio_display_slots", 1)) if target else 1
    return _DISPLAY_SLOTS[:count]




@lru_cache(maxsize=1)
def _creature_choices():
    from ..business.asset.creatures import catalog, display_name

    return tuple((name, display_name(name), "minecraft:" + name.rsplit(" · ", 1)[-1])
                 for name in catalog())


def _creature_items(self, context):
    return _creature_choices()


@lru_cache(maxsize=1)
def _creature_categories():
    from ..business.asset.creatures import GROUPS

    return (("ALL", "全部", ""), *((group, group, "") for group in GROUPS))


def _creature_category_items(self, context):
    return _creature_categories()


def _open_creature_catalog(state, context):
    from ..business.asset.creatures import categories

    if not state.creature_open:
        return
    rows = state.creature_rows
    selected = rows[state.creature_index].identifier if 0 <= state.creature_index < len(rows) else ""
    query = state.creature_search.strip().casefold()
    rows.clear()
    selected_index = 0
    for identifier, label, game_id in _creature_choices():
        if state.creature_category not in ("ALL", *categories(identifier)) or query not in (label + " " + game_id).casefold():
            continue
        row = rows.add()
        row.identifier, row.name, row.label = identifier, label + " " + game_id, label
        row.icon_id = assets.creature_icon(identifier)
        if identifier == selected:
            selected_index = len(rows) - 1
    state.creature_index = selected_index


class OXALMCSTUDIO_PG_assets_ui(PropertyGroup):
    variant_instance: StringProperty(default="",options={"SKIP_SAVE"})
    library_open: BoolProperty(name="原版方块物品", update=library_changed, options={"SKIP_SAVE"})
    binding_open: BoolProperty(name="展示框 / 木展示架绑定")
    enchant_open: BoolProperty(name="附魔效果")
    image_open: BoolProperty(name="导入图片资产")
    packs_open: BoolProperty(name="资源包管理")
    rows: CollectionProperty(type=OXALMCSTUDIO_PG_asset_row, options={"SKIP_SAVE"})
    index: IntProperty(default=0, min=0, update=selected_changed)
    variants: CollectionProperty(type=OXALMCSTUDIO_PG_asset_variant, options={"SKIP_SAVE"})
    packs: CollectionProperty(type=OXALMCSTUDIO_PG_asset_row, options={"SKIP_SAVE"})
    pack_index: IntProperty(default=0, min=0)
    category: EnumProperty(name="分类", items=CATEGORIES, default="ALL", update=catalog_changed, options={"SKIP_SAVE"})
    search: StringProperty(name="搜索物品", update=catalog_changed, options={"SKIP_SAVE"})
    catalog_requested: StringProperty(default="", options={"SKIP_SAVE"})
    catalog_loaded: StringProperty(default="", options={"SKIP_SAVE"})
    page: IntProperty(default=0, min=0, options={"SKIP_SAVE"})
    pages: IntProperty(default=1, min=1, options={"SKIP_SAVE"})
    total: IntProperty(default=0, min=0, options={"SKIP_SAVE"})
    special: BoolProperty(name="特殊模型", default=True)
    shape: IntProperty(name="造型编号", min=0)
    shape_schema_json: StringProperty(default="{}", options={"SKIP_SAVE"})
    shape_choice: EnumProperty(name="世界造型", items=shape_choices, default=0)
    complete_structure: BoolProperty(
        name="完整结构",
        default=True,
        description="门、床和双格植物导入完整结构；关闭后仅导入所选部分",
    )
    destination: EnumProperty(
        name="导入位置",
        items=destination_items,
        default=0,
    )
    display_target: EnumProperty(
        name="展示目标", items=display_items, update=display_target_changed
    )
    display_slot: EnumProperty(
        name="展示槽",
        items=display_slot_items,
    )
    display_rotation: IntProperty(name="展示物旋转（0–7）", min=0, max=7)
    quantity: IntProperty(name="数量",default=1,min=1,max=64)
    potion_key: EnumProperty(name="药水预设",items=potion_items,default=0)
    spawn_entity: EnumProperty(name="刷怪笼生物",items=spawner_items,default=0)
    stand_pose: EnumProperty(name="盔甲架姿态",items=tuple((key,label,"") for key,label in POSES),default="RESET")
    slot_phase: EnumProperty(name="持物形态",items=slot_phase_items,default=0)
    book_target: PointerProperty(name="玩家目标", type=Object)
    replace_occupied: BoolProperty(name="替换占用槽",default=False)
    image_kind: EnumProperty(
        name="资产类型",
        items=(("ITEM", "物品", ""), ("BLOCK", "方块", "")),
        default="ITEM",
    )
    compositor_open: BoolProperty(name="合成器描边")
    creature_field: EnumProperty(name="基础功能", items=(
        ("arm_mode", "胳膊", ""), ("leg_mode", "腿", ""),
        ("leg_fixed", "腿部固定", ""), ("foot_fixed", "脚腕固定", "")))
    creature_arm_mode: EnumProperty(name="胳膊", items=(("SLIM", "3格", ""), ("WIDE", "4格", "")))
    creature_leg_mode: EnumProperty(name="腿", items=(("NORMAL", "常规弯曲", ""), ("OFFICIAL", "官方动画", "")))
    creature_leg_fixed: BoolProperty(name="腿部固定")
    creature_foot_fixed: BoolProperty(name="脚腕固定")
    creature_wrist: character_wrist.property_for_character(creature=True)
    creature_armor_open: BoolProperty(name="盔甲设置", options={"SKIP_SAVE"})
    creature_armor_target: EnumProperty(name="盔甲部位", items=(
        ("ALL", "全部", ""), ("HEAD", "头盔", ""), ("CHEST", "胸甲", ""),
        ("LEGS", "护腿", ""), ("FEET", "靴子", "")), default="ALL", options={"SKIP_SAVE"})
    creature_open: BoolProperty(name="生物资产库（测试中）", update=_open_creature_catalog, options={"SKIP_SAVE"})
    creature_category: EnumProperty(name="分类", items=_creature_category_items, default=0,
                                    update=_open_creature_catalog, options={"SKIP_SAVE"})
    creature_search: StringProperty(name="搜索生物", update=_open_creature_catalog, options={"SKIP_SAVE"})
    creature_rows: CollectionProperty(type=OXALMCSTUDIO_PG_asset_row, options={"SKIP_SAVE"})
    creature_index: IntProperty(default=0, min=0, options={"SKIP_SAVE"})
    creature: EnumProperty(name="生物", items=_creature_items)
    image_depth: FloatProperty(name="物品厚度", default=0.03, min=0.0001, soft_min=0.001, max=1)
    relief_cell_size: IntProperty(name="凹凸像素格大小", description="每格每边包含的贴图像素数，在准备凹凸时应用", default=1, min=1, max=16)
    future_pack: BoolProperty(name="用于后续导入")
    pbr_format: EnumProperty(
        name="PBR 格式",
        items=(("lab_PBR_1.3", "lab PBR 1.3", "标准 lab PBR 1.3 贴图解析格式"),
               ("old_PBR", "old PBR", "传统 PBR 通道格式")),
        default="lab_PBR_1.3",
    )


class OXALMCSTUDIO_UL_assets(UIList):
    def draw_item(
        self,
        context,
        layout,
        data,
        item,
        icon,
        active_data,
        active_property,
        index=0,
        flt_flag=0,
    ):
        if item is not None:
            layout.label(text=item.label, icon_value=item.icon_id)


def _action(layout, label, action, icon="NONE"):
    button = layout.operator("oxal_mc_studio.asset_action", text=label, icon=icon)
    button.action = action
    return button


def _section(layout, state, prop):
    header, body = layout.panel_prop(state, prop)
    header.label(text=state.bl_rna.properties[prop].name)
    return body


def draw_compositor(layout, scene):
    from ..business.outline.compositor import (
        outline_color_socket,
        outline_mode_socket,
        outline_width_socket,
    )

    layout.label(text="在最终渲染（F12）中查看描边", icon="RENDER_STILL")
    if scene.render.engine == "CYCLES":
        layout.label(text="Cycles 不支持实时视口预览", icon="INFO")
        layout.label(text="不影响 F12 最终渲染")
    _action(layout, "启用合成器描边", "COMP_OUTLINE", "NODE_COMPOSITING")
    width = outline_width_socket(scene)
    if width is not None:
        row = layout.row()
        row.enabled = not width.is_linked and mutation_allowed()
        row.prop(width, "default_value", text="描边扩展（像素）")
    if outline_color_socket(scene) is not None:
        _action(layout, "移除描边", "COMP_REMOVE", "TRASH")
    mode = outline_mode_socket(scene)
    if mode is not None:
        row = layout.row()
        row.enabled = not mode.is_linked and mutation_allowed()
        row.prop(mode, "default_value", text="全描边（含内部线条）")
    color = outline_color_socket(scene)
    if color is not None:
        row = layout.row()
        row.enabled = not color.is_linked and mutation_allowed()
        row.prop(color, "default_value", text="描边颜色")
    _action(layout, "排除所选物体", "COMP_EXCLUDE", "RESTRICT_RENDER_ON")
    _action(layout, "恢复所选物体描边", "COMP_RESTORE", "RESTRICT_RENDER_OFF")
    _action(layout, "清空排除列表", "COMP_CLEAR", "X")


def draw_creatures(layout, context, *, show_catalog=True):
    from ..business.asset.creature_parts import option_value
    from ..business.asset.creatures import (
        CREATURE_VERSION,
        RIG,
        instances,
        needs_upgrade,
        selected_instance,
    )
    state = getattr(context.window_manager, STATE_PROPERTY)
    layout.label(text="生物功能当前处于测试阶段", icon="INFO")
    if show_catalog:
        layout.prop(state, "creature_category")
        layout.prop(state, "creature_search")
        if state.creature_rows:
            split = layout.split(factor=0.66)
            listing = split.column()
            listing.template_list("OXALMCSTUDIO_UL_assets", "creatures", state, "creature_rows",
                                  state, "creature_index", rows=6)
            actions = split.column(align=True)
            for destination, label, icon in (("ORIGIN", "原点导入", "IMPORT"), ("CURSOR", "游标导入", "PIVOT_CURSOR")):
                _action(actions, label, "CREATURE_IMPORT", icon).destination = destination
            if 0 <= state.creature_index < len(state.creature_rows):
                detail = layout.row()
                detail.active = False
                selected = state.creature_rows[state.creature_index].identifier
                detail.label(text="minecraft:" + selected.rsplit(" · ", 1)[-1], translate=False)
        else:
            layout.label(text="没有匹配的生物", icon="INFO")
    root = selected_instance(context.scene, context.selected_objects)
    if root:
        version = root.get("blockloom_creature_version", "0.1.0")
        layout.label(text="生物模型 v" + str(version))
        if needs_upgrade(root):
            label = "更新所选生物资源" if version == CREATURE_VERSION else "升级所选生物至 v" + CREATURE_VERSION
            _action(layout, label, "CREATURE_UPGRADE", "FILE_REFRESH")
    for old in instances(context.scene):
        if not old.get("blockloom_parts_version"):
            row = layout.column(align=True)
            row.label(text="旧版实例：" + old.name, icon="INFO")
            button = _action(row, "移除这个旧实例", "CREATURE_REMOVE", "TRASH")
            button.creature_instance = old[INSTANCE]
    if root is not None and root.get("blockloom_parts_version"):
        rig = root[RIG]
        _action(layout, "移除所选生物实例", "CREATURE_REMOVE", "TRASH")
        from ..business.human.human_compat import LegacyHuman
        from ..business.human.human_compat_controls import _foot_value, _leg_value

        target = LegacyHuman(rig, "", tuple(root.all_objects), (root,), True, "")
        arm = option_value(root, rig, "arm_mode")
        leg = option_value(root, rig, "leg_mode")
        for field, label, values, current in (
            ("arm_mode", "胳膊", ("4格", "3格"), None if arm is None else arm == "SLIM"),
            ("leg_mode", "腿", ("常规弯曲", "官方动画"), None if leg is None else leg == "OFFICIAL"),
            ("leg_fixed", "腿部固定", ("关", "开"), _leg_value(target)),
            ("foot_fixed", "脚腕固定", ("关", "开"), _foot_value(target)),
        ):
            if current is None:
                continue
            row = layout.row(align=True)
            row.label(text=label)
            for enabled, title in enumerate(values):
                button = row.operator("oxal_mc_studio.asset_action", text=title, depress=bool(enabled) == current)
                button.action = "CREATURE_STYLE"
                button.creature_part = field
                button.creature_enabled = bool(enabled)
        parts = [("name", "名字")]
        if rig.get("oxal_humanoid_can_fold"):
            parts.append(("player_arms", "独立手臂"))
        for field, label in parts:
            present = option_value(root, rig, field)
            if present is None:
                continue
            button = _action(layout, ("移除" if present else "添加") + label, "CREATURE_STYLE",
                             "REMOVE" if present else "ADD")
            button.creature_part = field
            button.creature_enabled = not present
        if rig.get("oxal_humanoid_armor_supported"):
            armor = layout.column(align=True)
            armor.prop(state, "creature_armor_open", icon="TRIA_DOWN" if state.creature_armor_open else "TRIA_RIGHT",
                       emboss=False)
            if state.creature_armor_open:
                box = armor.box()
                box.prop(state, "creature_armor_target")
                field = "armor_" + state.creature_armor_target
                targets = ("HEAD", "CHEST", "LEGS", "FEET") if state.creature_armor_target == "ALL" else (state.creature_armor_target,)
                present_parts = [option_value(root, rig, "armor_" + part) for part in targets]
                row = box.row(align=True)
                for adding, title, icon in ((False, "移除所选", "REMOVE"), (True, "添加所选", "ADD")):
                    col = row.column()
                    col.enabled = not all(present_parts) if adding else any(present_parts)
                    button = _action(col, title, "CREATURE_STYLE", icon)
                    button.creature_part = field
                    button.creature_enabled = adding
        if "blockloom_heat" in rig:
            layout.prop(rig, '["blockloom_heat"]', text="红温", slider=True)
        layout.prop(state, "creature_wrist")
        layout.label(text="同时选择一个生物和要绑定的物品")
        row = layout.row(align=True)
        _action(row, "绑定所选物品", "CREATURE_BIND", "CONSTRAINT")
        _action(row, "解除绑定", "CREATURE_UNBIND", "X")


def draw(layout, context):
    state = getattr(context.window_manager, STATE_PROPERTY)
    layout.enabled = mutation_allowed()
    if not mutation_allowed():
        layout.label(text="渲染期间暂停资产修改", icon="LOCKED")
    status = assets.endpoint.state(context.scene.session_uid)
    from ..business.human.human_selection import armor_snapshot
    if armor_snapshot(context.scene, context.selected_objects) is not None:
        from ..module import human
        status = human.commands.state(context.scene.session_uid)
    if assets.endpoint.busy(context.scene.session_uid):
        layout.label(text="正在处理资产请求…", icon="TIME")
    elif status.message:
        layout.label(
            text=status.message, icon="CHECKMARK" if status.succeeded else "ERROR"
        )
    active = root_of(context.object)
    library = _section(layout, state, "library_open")
    if library is not None:
        library.enabled = not assets.endpoint.busy(context.scene.session_uid)
        library.prop(state, "category")
        library.prop(state, "search")
        _action(library, "刷新资产目录 / 搜索", "CATALOG", "FILE_REFRESH")
        library.label(text=f"Java 26.4 Snapshot 5 · {state.total} 项匹配")
        if state.search.strip():
            library.label(text="全局搜索 · 跨全部分类", icon="VIEWZOOM")
        navigation = library.row(align=True)
        previous = navigation.row(align=True)
        previous.enabled = state.page > 0
        _action(previous, "上一页", "CATALOG_PAGE", "TRIA_LEFT").page_step = -1
        navigation.label(text=f"{state.page + 1} / {state.pages}")
        following = navigation.row(align=True)
        following.enabled = state.page + 1 < state.pages
        _action(following, "下一页", "CATALOG_PAGE", "TRIA_RIGHT").page_step = 1
        content = library.split(factor=.65)
        listing,actions = content.column(),content.column(align=True)
        listing.template_list("OXALMCSTUDIO_UL_assets","assets",state,"rows",state,"index",rows=8)
        actions.prop(state,"special",toggle=True)
        from ..business.asset.asset_targets import selected_targets
        current_targets = selected_targets(context)
        for destination,label,icon in (("ORIGIN","原点导入","WORLD"),("CURSOR","游标导入","PIVOT_CURSOR"),("SLOT","物品栏导入","SEQ_SEQUENCER"),("DISPLAY","展示位导入","IMAGE_DATA")):
            if destination in {"SLOT", "DISPLAY"} and destination not in current_targets:
                continue
            button = _action(actions,label,"IMPORT",icon)
            button.destination = destination
        if 0 <= state.index < len(state.rows):
            library.label(text=state.rows[state.index].identifier)
        if "DISPLAY" in current_targets and active is not None:
            library.label(text="展示目标："+active.name,icon="CONSTRAINT_BONE")
            library.prop(state,"display_slot")
            library.prop(state,"display_rotation")
        inventory_state = getattr(context.window_manager,"oxal_mc_studio_inventory_ui",None)
        if "SLOT" in current_targets and inventory_state is not None:
            library.prop(inventory_state,"slot",text="物品栏格子")
        if "SLOT" in current_targets or "DISPLAY" in current_targets:
            row = library.row(align=True)
            if "SLOT" in current_targets:
                row.prop(state,"quantity")
            row.prop(state,"replace_occupied")
        library.prop(state, "complete_structure")
        selected_id = state.rows[state.index].identifier if 0<=state.index<len(state.rows) else ""
        if selected_id.split(":")[-1] in {"spawner","trial_spawner"} or (active and active.get(IDENTIFIER,"").split(":")[-1] in {"spawner","trial_spawner"}):
            library.prop(state,"spawn_entity")
        if selected_id.split(":")[-1] in {"potion","splash_potion","lingering_potion","tipped_arrow","arrow"} or (active and active.get(IDENTIFIER,"").split(":")[-1] in {"potion","splash_potion","lingering_potion","tipped_arrow","arrow"}):
            library.prop(state,"potion_key")
            if active and active.get(IDENTIFIER,"").split(":")[-1] in {"potion","splash_potion","lingering_potion","tipped_arrow","arrow"}:
                _action(library,"应用药水预设","POTION","COLOR")
        for variant in state.variants:
            variant_row = library.row()
            variant_row.enabled = not (
                state.complete_structure
                and variant.key in {"half", "part"}
                and {value[0] for value in variant_items(variant, context)}
                in ({"lower", "upper"}, {"head", "foot"})
            )
            variant_row.prop(
                variant, "value", text=_STATE_LABELS.get(variant.key, variant.key)
            )
        if len(shape_choices(state, context)) > 1:
            library.prop(state, "shape_choice")
        if active is not None:
            library.label(text="当前实例：" + active.name)
            _action(library,"移除所选资产实例","REMOVE","TRASH")
            _action(library, "升级所选资产材质 / PBR", "PBR_UPGRADE", "MATERIAL")
            if active.get("oxal_mc_studio_slot_phase_schema") == 1:
                from ..business.asset.asset_slot_states import LABELS, PROPERTY
                index = max(0,min(len(LABELS)-1,round(float(active.get(PROPERTY,0)))))
                library.label(text="当前持物形态：" + LABELS[index])
                library.prop(state,"slot_phase")
                _action(library,"应用持物形态","SLOT_PHASE","FILE_REFRESH")
                library.prop(active,'["'+PROPERTY+'"]',text="阶段（可插帧）")
            control_owner = active if active.type == "ARMATURE" else active.get("oxal_mc_studio_control_rig",active)
            from ..business.asset import asset_armor

            if asset_armor.PART in active:
                armor = library.box()
                armor.label(text="白珀盔甲设置", icon="MOD_CLOTH")
                prefix = asset_armor.PREFIX
                for prop, label in (("material", "材质"), ("pattern", "纹饰"),
                                    ("leather_color", "皮革颜色"), ("trim_color", "纹饰颜色"),
                                    ("3d", "3D 盔甲"), ("thickness", "厚度")):
                    if (prop == "leather_color" and active[prefix + "material"] != 0
                            or prop == "trim_color" and active[prefix + "pattern"] == 0
                            or prop == "thickness" and not active[prefix + "3d"]):
                        continue
                    armor.prop(active, '["' + prefix + prop + '"]', text=label)
            if active.get(IDENTIFIER, "").startswith("minecraft:"):
                _action(library, "读取所选资产变体", "READ_VARIANT", "EYEDROPPER")
                apply = library.row()
                apply.enabled = state.variant_instance == active[INSTANCE]
                _action(apply, "应用变体 / 重建几何", "REBUILD", "FILE_REFRESH")
            if str(active.get(VERSION,"")) in {"1", "2"}:
                _action(library,"修复 / 升级Beta实例","REPAIR","TOOL_SETTINGS")
            controls = {
                "jaw_open":"龙头下颚",
                "light_enabled": "灯光",
                "effect_strength": "特效强度",
                "smoke_enabled": "烟雾",
                "effect_speed": "特效速度",
                **CONTROL_LABELS,
            }
            dynamic_specs = json.loads(control_owner.get("oxal_mc_studio_state_controls","[]"))
            dynamic_keys = {value for spec in dynamic_specs for value in (spec["key"],spec["property"])}
            for prop, label in controls.items():
                if prop in dynamic_keys:
                    continue
                if prop in control_owner:
                    specification = ALIASES.get(prop)
                    bone = (control_owner.pose.bones.get(control_bone_name(prop))
                            if specification is not None and control_owner.type == "ARMATURE"
                            else None)
                    if bone is not None:
                        asset_rail.draw(library, bone, label)
                    else:
                        library.prop(control_owner, '["' + prop + '"]', text=label)
            if control_owner.type == "ARMATURE":
                for spec in dynamic_specs:
                    pose = control_owner.pose.bones.get(spec.get("bone","控制 · "+spec["label"]))
                    if pose is not None:
                        asset_rail.draw(library, pose, spec["label"])
                library.label(text="姿态模式可直接拖动原生骨骼控件",icon="POSE_HLT")
                _action(library,"进入原生控件姿态模式","POSE_CONTROLS","POSE_HLT")
            if "展示物品旋转" in active:
                library.prop(active,'["展示物品旋转"]',text="展示物旋转")
            if active.get(IDENTIFIER) == "minecraft:trident":
                _action(library,"预建本帧引雷","LIGHTNING","LIGHT")
            if active.get("oxal_mc_studio_native_book_schema"):
                library.prop(state, "book_target")
                row = library.row(align=True)
                _action(row, "绑定跟随目标", "BOOK_TARGET", "CONSTRAINT")
                _action(row, "解除跟随", "BOOK_TARGET_CLEAR", "X")
            if active.get("oxal_mc_studio_torch209_carrier") or active.get("oxal_mc_studio_brewing42_carrier") or active.get("oxal_mc_studio_native_playback_clock"):
                from ..business.asset.asset_extensions import particles_module
                particle_tool=particles_module(active)
                if particle_tool is not None:
                    try:
                        pending = particle_tool.pending(context.scene,active)
                    except (ValueError, TypeError, ReferenceError) as error:
                        library.label(text="粒子参数待修正：" + str(error),icon="ERROR")
                    else:
                        if pending:
                            library.label(text="时间轴或粒子参数已改变，请显式重建",icon="INFO")
                if active.get("oxal_mc_studio_native_playback_clock") and not (active.get("oxal_mc_studio_torch209_carrier") or active.get("oxal_mc_studio_brewing42_carrier")) and "粒子播放倍率" in active:
                    library.prop(active,'["粒子播放倍率"]',text="粒子播放倍率")
                elif "粒子种子" in active:
                    library.prop(active,'["粒子种子"]',text="粒子种子")
                _action(library,"重建时间轴粒子","PARTICLES_REBUILD","FILE_REFRESH")
            if active.get("oxal_mc_studio_projectile_schema"):
                if active.get("oxal_mc_studio_projectile_schema", 0) >= 3:
                    from ..business.asset.asset_projectile_clock import cache_status
                    library.label(text="阶段2触发并自动播放，阶段3关闭", icon="INFO")
                    try:
                        playback_cache = cache_status(active)
                    except (ValueError, TypeError, ReferenceError) as error:
                        library.label(text="播放缓存不可用：" + str(error), icon="ERROR")
                    else:
                        library.label(text=playback_cache["label"], icon="CHECKMARK" if playback_cache["is_baked"] else "INFO")
                        if playback_cache["is_baked"]:
                            library.label(text="修改控制器前先清除缓存")
                        else:
                            library.label(text="多次触发请为阶段控制器插帧")
                            library.label(text="再次触发：离开2并前进一帧")
                            library.label(text="同帧重试可清除缓存")
                        row = library.row(align=True)
                        _action(row, "烘焙播放缓存", "PROJECTILE_CACHE_BAKE", "REC")
                        _action(row, "清除缓存", "PROJECTILE_CACHE_CLEAR", "X")
                elif active.get(IDENTIFIER) not in {"minecraft:arrow","minecraft:spectral_arrow","minecraft:tipped_arrow"}:
                    library.label(text="修复此Beta实例后，阶段2可自动播放",icon="INFO")
                if active.get(IDENTIFIER) in {"minecraft:arrow","minecraft:spectral_arrow","minecraft:tipped_arrow"}:
                    from ..business.asset.asset_arrow_bake import ENABLED, status
                    library.label(text=status(active,context.scene),icon="INFO")
                    library.label(text="采样场景起止帧，固定一步一帧")
                    _action(library,"完成轨迹后重建飞行粒子","ARROW_BAKE","FILE_REFRESH")
                    if ENABLED in active:
                        library.prop(active,'["'+ENABLED+'"]',text="使用已烘焙粒子")
            if active.get(IDENTIFIER) == "minecraft:armor_stand" and control_owner.type == "ARMATURE":
                library.prop(state,"stand_pose")
                _action(library,"应用原生姿态","ARMOR_STAND_POSE","ARMATURE_DATA")
            native_sliders = {}
            if active.get("oxal_mc_studio_native_prefab"):
                from ..business.asset.asset_native_rigs import editable_fields
                native_sliders = editable_fields(active)
            shown_sliders = set()
            for field in json.loads(active.get("oxal_mc_studio_effect_fields","[]")):
                if field == "命中帧" and active.get("oxal_mc_studio_projectile_schema"):
                    continue
                if field in active:
                    bone_name = native_sliders.get(field)
                    if bone_name and active.pose is not None and bone_name in active.pose.bones:
                        if bone_name not in shown_sliders:
                            asset_rail.draw(library, active.pose.bones[bone_name], field)
                            shown_sliders.add(bone_name)
                    else:
                        library.prop(active,'['+json.dumps(field,ensure_ascii=False)+']',text=field)
    from ..util.ownership import owns

    enchant_active = active if active is not None and owns(active, active) and owns(active, context.object) else None
    creatures = _section(layout, state, "creature_open")
    if creatures is not None:
        draw_creatures(creatures, context)
    image = _section(layout, state, "image_open")
    if image is not None:
        image.prop(state, "image_kind", expand=True)
        if state.image_kind == "ITEM":
            image.prop(state, "image_depth")
        image.operator(
            "oxal_mc_studio.asset_file", text="导入 PNG 图片资产", icon="IMAGE_DATA"
        ).action = "IMAGE"
        image.label(text="动态贴图读取同名 .png.mcmeta")
    enchant = _section(layout, state, "enchant_open")
    if enchant is not None:
        row = enchant.row(align=True)
        _action(row, "一键附魔", "ENCHANT", "ADD")
        _action(row, "取消材质附魔" if enchant_active is not None else "取消附魔", "UNENCHANT", "LOOP_BACK")
        if (enchant_active is not None and enchant_active.type == "MESH"
                and enchant_active.get("oxal_mc_studio_geometry_kind") == "ITEM"):
            from ..business.asset.asset_enchantment import outline_needs_rebind

            needs_rebind = outline_needs_rebind(enchant_active)
            _action(enchant, "重新绑定并恢复描边" if needs_rebind else "添加 / 更新描边",
                    "ENCHANT_OUTLINE_REBIND", "FILE_REFRESH")
            if needs_rebind:
                enchant.label(text="旧描边绑定已失效，请点击上方恢复", icon="INFO")
        else:
            _action(enchant, "准备附魔描边", "ENCHANT_OUTLINE_PREPARE", "MOD_SOLIDIFY")
        from ..business.enchantment.enchantment_targets import pixel_geometry_allowed

        armor_item = enchant_active is not None and enchant_active.get("oxal_mc_studio_geometry_kind") == "ARMOR"
        if armor_item:
            _action(enchant, "准备盔甲像素格流光", "ENCHANT_PIXELS_PREPARE", "SHADING_RENDERED")
            enchant.label(text="3D 盔甲和厚度可在白珀盔甲设置中调整")
        geometry_allowed = enchant_active is not None and not armor_item and pixel_geometry_allowed(enchant_active)
        if geometry_allowed:
            enchant.prop(state, "relief_cell_size", slider=True)
            if enchant_active is not None and "glint_relief_cell_size" in enchant_active:
                n = enchant_active["glint_relief_cell_size"]
                enchant.label(text=f"当前凹凸每格 {n} × {n} 像素")
            _action(enchant, "准备像素3D凹凸", "ENCHANT_3D_PREPARE", "MOD_SOLIDIFY")
        rebuild_row = enchant.row()
        rebuild_row.enabled = enchant_active is not None and enchant_active.get(
            IDENTIFIER, ""
        ).startswith("minecraft:")
        _action(rebuild_row, "重建原版物品几何", "REBUILD", "FILE_REFRESH")
        from ..business.enchantment.enchantment_outline import (
            outline_prepared,
            prepared,
        )
        from .enchantment import draw_controls
        if enchant_active is not None:
            from ..business.enchantment.enchantment import PREFIX
            from ..business.enchantment.enchantment_targets import asset_meshes
            from ..business.outline.outline_solidify import prepared_sprite

            meshes = asset_meshes(enchant_active)
            stable_sprite = any(prepared_sprite(obj) for obj in meshes)
            pixel_materials = tuple({slot.material for obj in meshes for slot in obj.material_slots
                                     if slot.material is not None and slot.material.node_tree is not None
                                     and slot.material.node_tree.nodes.get(PREFIX + "Mix") is not None})
            draw_controls(enchant, enchant_active, prepared=any(prepared(obj) for obj in meshes), geometry_allowed=geometry_allowed,
                          outline_prepared=bool(meshes) and all(outline_prepared(obj) for obj in meshes),
                          stable_sprite=stable_sprite, pixel_materials=pixel_materials)
        else:
            from ..business.human.human import resolve_human
            from ..business.human.human_selection import armor_snapshot
            selection = armor_snapshot(context.scene, context.selected_objects)
            if selection is not None:
                human_root = resolve_human(context.scene, selection["instance_id"])
                from .enchantment import draw_armor_controls

                _action(enchant, "准备所选盔甲像素格流光", "ENCHANT_PIXELS_PREPARE", "SHADING_RENDERED")
                draw_armor_controls(enchant, human_root, selection["parts"])
            else:
                from .enchantment import draw_external

                draw_external(enchant, context)
        enchant.label(text="实例参数支持右键插入关键帧", icon="KEY_HLT")
    compositor = _section(layout, state, "compositor_open")
    if compositor is not None:
        draw_compositor(compositor, context.scene)
    packs = _section(layout, state, "packs_open")
    if packs is not None:
        packs.prop(state, "pbr_format")
        packs.template_list(
            "OXALMCSTUDIO_UL_assets", "packs", state, "packs", state, "pack_index", rows=4
        )
        packs.operator(
            "oxal_mc_studio.asset_file", text="ZIP 材质包入库", icon="ADD"
        ).action = "PACK_ADD"
        packs.prop(state, "future_pack")
        _action(packs, "应用后续导入设置", "PACK_FUTURE")
        _action(packs, "应用到选中资产", "PACK_APPLY", "FILE_REFRESH")
        _action(packs, "恢复选中资产原图", "PACK_RESTORE", "LOOP_BACK")
        _action(packs, "刷新资源包列表", "PACK_LIST", "FILE_REFRESH")
        _action(packs,"移除未使用的库包","PACK_REMOVE","TRASH")
        packs.label(
            text="后续导入："
            + ("材质包" if context.scene.get("oxal_mc_studio_future_pack") else "原版贴图")
        )
    layout.separator()
    _action(layout, "整理工程内插件着色器", "SHADER_LAYOUT", "NODETREE")



CLASSES = (
    OXALMCSTUDIO_PG_asset_row,
    OXALMCSTUDIO_PG_asset_variant,
    OXALMCSTUDIO_PG_assets_ui,
    OXALMCSTUDIO_UL_assets,
)
_registered_classes = []


def register():
    _creature_choices()
    _creature_categories()
    if _registered_classes:
        raise RuntimeError("资产 UI 已登记")
    try:
        for cls in CLASSES:
            if cls is OXALMCSTUDIO_PG_assets_ui:
                common.register_state(cls, STATE_PROPERTY)
            else:
                bpy.utils.register_class(cls)
            _registered_classes.append(cls)
    except Exception as error:
        try:
            unregister()
        except Exception as cleanup_error:
            logger.exception("Asset UI registration rollback failed")
            error.add_note(f"Asset UI cleanup failure: {type(cleanup_error).__name__}: {cleanup_error}")
        raise


def unregister():
    errors = []
    for cls in reversed(tuple(_registered_classes)):
        try:
            if cls is OXALMCSTUDIO_PG_assets_ui:
                common.unregister_state(cls, STATE_PROPERTY)
            else:
                bpy.utils.unregister_class(cls)
            _registered_classes.remove(cls)
        except Exception as error:
            logger.exception("Asset UI unregister failed: %s", cls.__name__)
            errors.append(error)
    if errors:
        raise ExceptionGroup("Asset UI unregister failed", errors)
