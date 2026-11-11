"""Native sky controls backed by persistent scene inputs and deferred commands."""

from typing import Any, Final, cast

import bpy
from bpy.props import CollectionProperty, StringProperty
from bpy.types import Context, PropertyGroup, Scene, UILayout

from ..business.sky import sky_inputs
from ..business.sky.sky import RESOURCE_VERSION, controller_for, root_for
from ..business.sky.sky_modes import actual_structure
from ..business.sky.sky_state import SLOTS, VALUES, effective_values
from ..core.runtime import mutation_allowed
from ..module import sky as sky_module
from ..util.resources import VERSION
from . import common

STATE_PROPERTY: Final = "oxal_mc_studio_sky_ui"

FIELDS: Final[dict[str, tuple[str, bool | int | float | str]]] = {
    "presets_open": ("工程天空参数保存", False),
    "time_open": ("时间", False),
    "transition_open": ("日出 / 日落过渡时间", False),
    "celestial_open": ("太阳与月亮", False),
    "sun_open": ("太阳参数", False),
    "moon_open": ("月亮参数", False),
    "stars_open": ("星空", False),
    "volume_open": ("体积雾", False),
    "fog_open": ("天空雾参数", False),
    "tyndall_open": ("丁达尔雾参数", False),
    "cloud_open": ("云", False),
    "preset_keyframes": ("关键帧自动切换", False),
    "preset_slot": ("保存值", 1),
    "time": ("时间", 8.0),
    "auto_time": ("自动时间", False),
    "start_time": ("起始时间", 8.0),
    "frames_per_hour": ("每小时帧数", 100.0),
    "sunrise_start": ("日出开始时间", 5.0),
    "sunrise_end": ("日出完成时间", 8.0),
    "sunset_start": ("日落开始时间", 18.0),
    "night_start": ("夜晚完成过渡时间", 20.0),
    "azimuth": ("天体方位（太阳/月亮）", 0.0),
    "celestial_3d": ("使用3D日月贴图", False),
    "celestial_rotation": ("旋转日月", True),
    "sun_energy": ("日光强度", 6.5),
    "ambient_energy": ("环境强度", 0.8),
    "sun_size": ("太阳画面大小", 1.0),
    "sun_brightness": ("太阳贴图亮度", 6.0),
    "sun_saturation": ("日光饱和度", 1.0),
    "day_saturation": ("白天天空饱和度", 1.0),
    "sun_fade": ("太阳渐显开始", 5.5),
    "sun_transition": ("太阳过渡时长", 2.0),
    "moon_energy": ("月光强度", 0.8),
    "moon_phase": ("月相", 4),
    "moon_brightness": ("月亮贴图亮度", 6.0),
    "moon_size": ("月亮大小", 1.0),
    "night_ambient": ("月夜环境光", 0.08),
    "moon_saturation": ("月光饱和度", 1.0),
    "night_saturation": ("夜晚天空饱和度", 1.0),
    "moon_temperature": ("月光冷暖", 0.25),
    "moon_softness": ("月光柔和", 0.1),
    "moon_fade": ("月亮渐显开始", 17.5),
    "moon_transition": ("月亮过渡时长", 2.0),
    "stars_enabled": ("星空开关", True),
    "stars_brightness": ("星空亮度", 3.0),
    "stars_density": ("星空密度", 85.0),
    "stars_size": ("星点大小", 0.085),
    "stars_fade": ("星空渐显开始", 18.0),
    "stars_transition": ("星空过渡时长", 2.0),
    "stars_speed": ("星空时间速度", 0.2),
    "fog_enabled": ("启用天空雾", True),
    "fog_distance": ("起始距离", 130.0),
    "fog_transition": ("过渡距离", 150.0),
    "fog_density": ("纹理浓度", 0.35),
    "fog_base": ("基础浓度", 0.0),
    "fog_gradient": ("雾渐变高度", 500.0),
    "fog_bottom": ("底部高度", 0.0),
    "fog_scattering": ("散射方向", 0.25),
    "fog_eevee": ("Eevee倍率", 1.0),
    "tyndall_enabled": ("启用丁达尔雾", True),
    "tyndall_density": ("浓度", 0.0008),
    "tyndall_night": ("夜晚密度倍率", 0.5),
    "tyndall_scattering": ("散射方向", 0.72),
    "tyndall_range": ("范围", 734.46),
    "tyndall_height": ("高度", 150.0),
    "tyndall_bottom": ("底部高度", 0.0),
    "tyndall_eevee": ("Eevee倍率", 1.0),
    "tyndall_cycles": ("Cycles倍率", 1.0),
    "cloud_light": ("启用云光照", True),
    "cloud_advanced": ("启用Eevee高级云光照", False),
    "cloud_shadow": ("启用云阴影", False),
    "cloud_light_auto": ("云光照自动过渡", True),
    "cloud_shadow_auto": ("云光照阴影自动跟随高级模式", True),
    "cloud_light_shadow": ("云光照阴影（手动）", False),
    "cloud_day_energy": ("云光照日间强度", 3.5),
    "cloud_night_energy": ("云光照夜间强度", 1.0),
    "cloud_day_angle": ("云光照日间角度", 60.0),
    "cloud_night_angle": ("云光照夜间角度", -60.0),
    "cloud_sunrise": ("日出过渡倍率", 2.0),
    "cloud_sunset": ("日落过渡倍率", 4.0),
    "cloud_moving": ("自动云移动", False),
    "cloud_height": ("云高度", 50.0),
    "cloud_rotation": ("云旋转角度", 0.0),
    "cloud_distance": ("云距离（N×N）", 4),
    "cloud_shadow_distance": ("云阴影距离（N×N）", 4),
    "cloud_speed": ("基础速度", 50.0),
    "cloud_speed_mode": ("速度模式", "SAME"),
    "cloud_offset": ("手动云位移", 0.0),
}


FIELDS.update(
    {
        "cloud_enabled": ("启用云", True),
        "fog_height": ("高度", 500.0),
        "fog_range": ("覆盖范围", 1024.0),
    }
)
SHORT = {
    "start_time": "起始",
    "frames_per_hour": "帧/时",
    "sun_energy": "日光",
    "ambient_energy": "环境",
    "sun_size": "贴图大小",
    "sun_brightness": "贴图亮度",
    "sun_saturation": "日光饱和度",
    "day_saturation": "天空饱和度",
    "sun_fade": "渐显",
    "sun_transition": "时长",
    "moon_energy": "月光",
    "night_ambient": "环境",
    "moon_size": "贴图大小",
    "moon_brightness": "贴图亮度",
    "night_saturation": "夜空饱和度",
    "moon_temperature": "冷暖",
    "moon_softness": "柔和",
    "moon_fade": "渐显",
    "moon_transition": "时长",
    "stars_brightness": "亮度",
    "stars_density": "密度",
    "stars_size": "大小",
    "stars_speed": "速度",
    "stars_fade": "渐显",
    "stars_transition": "时长",
    "fog_density": "纹理",
    "fog_base": "浓度",
    "fog_bottom": "底部高度",
    "tyndall_bottom": "底部高度",
    "cloud_height": "高度",
    "cloud_rotation": "旋转",
    "cloud_distance": "云 N×N",
    "cloud_shadow_distance": "阴影 N×N",
}
FULL = {
    "moon_phase",
    "stars_enabled",
    "fog_gradient",
    "fog_scattering",
    "fog_eevee",
    "tyndall_night",
    "tyndall_scattering",
    "tyndall_eevee",
    "tyndall_cycles",
    "sunrise_start",
    "sunrise_end",
    "fog_distance",
    "fog_transition",
}


class OXALMCSTUDIO_PG_sky_preset_item(PropertyGroup):
    label: StringProperty(options={"SKIP_SAVE"})
    filename: StringProperty(options={"SKIP_SAVE"})


class OXALMCSTUDIO_PG_sky_ui(PropertyGroup):
    """Only folds, library rows and selection are transient."""


OXALMCSTUDIO_PG_sky_ui.__annotations__ = common.annotations(
    {k: v for k, v in FIELDS.items() if k.endswith("_open")}
)
OXALMCSTUDIO_PG_sky_ui.__annotations__["items"] = CollectionProperty(
    type=OXALMCSTUDIO_PG_sky_preset_item, options={"SKIP_SAVE"}
)


class _SkyUI(common.UI):
    def __init__(self, context, state, folds):
        super().__init__(context, state, FIELDS, SHORT, FULL)
        self.folds = folds
        self.base = getattr(context.scene, VALUES)

    def prop(self, layout, name, *args, **kwargs):
        previous = self.state
        if name in {"preset_slot", "preset_keyframes", "cloud_distance", "cloud_shadow_distance"}:
            self.state = self.base
        try:
            index = sky_inputs.group_index(self.state)
            field = sky_inputs.key(name, index)
            if field in self.context.scene and name not in {"cloud_distance","cloud_shadow_distance"}:
                text = kwargs.pop("text",args[0] if args else FIELDS[name][0])
                value = getattr(self.state,name)
                slider = kwargs.pop("slider",isinstance(value,(int,float)) and not isinstance(value,bool))
                if kwargs.get("expand") and text == "":
                    layout.prop(self.context.scene,sky_inputs.path(name,index),slider=slider,**kwargs)
                else:
                    layout.prop(self.context.scene,sky_inputs.path(name,index),text=text,slider=slider,**kwargs)
                return
            return super().prop(layout, name, *args, **kwargs)
        finally:
            self.state = previous

    def section(self, layout, name, icon="NONE"):
        box = layout.box()
        box.prop(self.folds, name, text=FIELDS[name][0], icon=icon, emboss=False)
        return box if getattr(self.folds, name) else None


def _action(layout, label, action, icon="NONE"):
    item = layout.operator("oxal_mc_studio.sky_command", text=label, icon=cast(Any, icon))
    item.action = action


def _mode(layout, label, mode, active: Any = False):
    layout.operator_context = "INVOKE_DEFAULT"
    item = layout.operator("oxal_mc_studio.sky_cloud_mode", text=label, depress=bool(active))
    item.mode = mode


def draw(layout: UILayout, context: Context):
    folds = getattr(context.window_manager, STATE_PROPERTY)
    scene = cast(Scene, context.scene)
    state = getattr(scene, VALUES)
    effective = effective_values(scene)
    ui = _SkyUI(context, effective, folds)
    box = layout.box()
    box.enabled = mutation_allowed() and not sky_module.busy(scene.session_uid)
    needs_activation = False
    root = None
    structure = None
    try:
        root = root_for(scene)
        if root is not None:
            structure = actual_structure(controller_for(root))
        needs_activation = sky_module.needs_activation(scene)
    except ValueError as exc:
        box.enabled = False
        ui.note(layout, str(exc), "ERROR")
    load_row = box.row()
    load_row.enabled = not sky_module.busy()
    load_row.operator("oxal_mc_studio.load_sky", text="加载 / 激活 OXAL 天空", icon="IMPORT")
    if needs_activation:
        ui.note(box, "当前 World 或参数绑定未激活，请点击加载/激活", "ERROR")
    if root is not None and root.get(VERSION) != RESOURCE_VERSION:
        ui.note(box, "已有 Beta 天空需要明确修复；保留动画、材质及绑定，冲突拒绝。", "ERROR")
        _mode(box, "修复已有 Beta 天空", "REPAIR")
    elif root is not None:
        _mode(box, "检查 / 修复已加载天空", "REPAIR")
    shading = getattr(getattr(context, "space_data", None), "shading", None)
    if shading is not None and shading.type == "MATERIAL" and not shading.use_scene_world:
        ui.note(box, "材质预览尚未使用场景 World，无法显示此天空。", "INFO")
        box.prop(shading, "use_scene_world", text="材质预览使用场景 World")
    elif shading is not None and shading.type == "RENDERED" and not shading.use_scene_world_render:
        ui.note(box, "渲染预览尚未使用场景 World，无法显示此天空。", "INFO")
        box.prop(shading, "use_scene_world_render", text="渲染预览使用场景 World")
    if effective != state:
        ui.note(box, f"正在编辑有效保存值 {state.preset_slot}；云模式及范围为全局结构。", "BOOKMARKS")
    if sky_module.busy():
        ui.note(box, "正在加载天空…", "TIME")
    else:
        for endpoint in (sky_module.endpoint, sky_module.commands):
            status = endpoint.state(scene.session_uid)
            if status.message:
                ui.note(
                    box, status.message, "CHECKMARK" if status.succeeded else "ERROR"
                )
    actions = box.row(align=True) if ui.wide() else box.column(align=True)
    _action(actions, "自动辉光", "glow", "NODE_COMPOSITING")
    _action(actions, "恢复默认", "reset", "LOOP_BACK")
    _action(box, "删除天空", "remove", "TRASH")

    library = box.box()
    library.label(text="天空预设库 · .oxalsky", icon="WORLD")
    library.operator(
        "oxal_mc_studio.sky_library", text="刷新预设库", icon="FILE_REFRESH"
    ).action = "refresh"
    row = library.row()
    ui.listing(row.column(), folds, "sky_presets", rows=4)
    buttons = row.column(align=True)
    for text, icon in (
        ("导入", "IMPORT"),
        ("导出", "EXPORT"),
        ("应用", "CHECKMARK"),
        ("删除", "TRASH"),
    ):
        if text in {"导入", "导出"}:
            operation = buttons.operator(
                "oxal_mc_studio.sky_preset_file", text=text, icon=cast(Any, icon)
            )
            operation.action = "import_preset" if text == "导入" else "export_preset"
        else:
            operation = buttons.operator(
                "oxal_mc_studio.sky_library", text=text, icon=cast(Any, icon)
            )
            operation.action = "apply_preset" if text == "应用" else "delete_preset"

    presets = ui.section(box, "presets_open", "BOOKMARKS")
    if presets is not None:
        ui.prop(presets, "preset_keyframes", icon="KEYINGSET")
        ui.prop(presets, "preset_slot")
        slots = getattr(scene, SLOTS)
        filled = len(slots) >= state.preset_slot and slots[state.preset_slot - 1].filled
        ui.note(
            presets,
            f"保存值 {state.preset_slot}：{'已保存' if filled else '空'}",
            "BOOKMARKS",
        )
        _action(presets, "保存当前有效参数", "save_slot", "ADD")
        _action(presets, "读取当前保存值", "apply_slot", "IMPORT")
        _action(presets, "清除当前保存值", "clear_slot", "TRASH")
        _action(presets, "插入切换关键帧", "key_slot", "KEY_HLT")
        ui.note(
            presets,
            "自动切换使用恒定插值；云结构不参与逐帧切换，手动读取预设时应用。",
        )

    time = ui.section(box, "time_open", "TIME")
    clock = effective
    hour = (
        (
            clock.start_time
            + (scene.frame_current - scene.frame_start) / max(clock.frames_per_hour, 1)
        )
        % 24
        if clock.auto_time
        else clock.time
    )
    is_day = 6 <= hour % 24 < 18
    if time is not None:
        ui.note(
            time,
            f"{'白天' if is_day else '夜晚'}设置（{hour % 24:.2f} 时）",
            "LIGHT_SUN" if is_day else "LIGHT_AREA",
        )
        ui.prop(time, "auto_time", icon="PLAY", toggle=True)
        if effective.auto_time:
            ui.fields(time, "start_time", "frames_per_hour")
        else:
            ui.prop(time, "time")
        transition = ui.section(time, "transition_open", "TIME")
        if transition is not None:
            ui.fields(
                transition,
                "sunrise_start",
                "sunrise_end",
                "sunset_start",
                "night_start",
            )

    celestial = ui.section(box, "celestial_open", "WORLD")
    if celestial is not None:
        ui.prop(celestial, "azimuth")
        ui.prop(celestial, "celestial_3d", icon="IMAGE_DATA")
        ui.prop(celestial, "celestial_rotation", icon="GESTURE_ROTATE")
        settings = ui.section(
            celestial,
            "sun_open" if is_day else "moon_open",
            "LIGHT_SUN" if is_day else "LIGHT_AREA",
        )
        if settings is not None:
            if is_day:
                ui.fields(
                    settings,
                    "sun_energy",
                    "ambient_energy",
                    "sun_size",
                    "sun_brightness",
                    "sun_saturation",
                    "day_saturation",
                    "sun_fade",
                    "sun_transition",
                )
            else:
                ui.fields(
                    settings,
                    "moon_phase",
                    "moon_energy",
                    "night_ambient",
                    "moon_brightness",
                    "moon_size",
                    "moon_saturation",
                    "night_saturation",
                    "moon_temperature",
                    "moon_softness",
                    "moon_fade",
                    "moon_transition",
                )
        if not is_day:
            stars = ui.section(celestial, "stars_open", "WORLD")
            if stars is not None:
                ui.fields(
                    stars,
                    "stars_enabled",
                    "stars_brightness",
                    "stars_density",
                    "stars_size",
                    "stars_speed",
                    "stars_fade",
                    "stars_transition",
                )

    eevee = scene.render.engine.startswith("BLENDER_EEVEE")
    volume = ui.section(box, "volume_open", "VOLUME_DATA")
    if volume is not None:
        fog_box = volume.box()
        ui.prop(fog_box, "fog_enabled")
        if effective.fog_enabled:
            fog = ui.section(fog_box, "fog_open", "MOD_FLUIDSIM")
            if fog is not None:
                ui.paired(fog, ("fog_height", "高度"), ("fog_range", "覆盖范围"))
                ui.fields(
                    fog,
                    "fog_distance",
                    "fog_transition",
                    "fog_density",
                    "fog_base",
                    "fog_gradient",
                    "fog_bottom",
                    "fog_scattering",
                )
                if eevee:
                    ui.fields(fog, "fog_eevee")
        tyndall_box = volume.box()
        ui.prop(tyndall_box, "tyndall_enabled")
        if effective.tyndall_enabled:
            tyndall = ui.section(tyndall_box, "tyndall_open", "VOLUME_DATA")
            if tyndall is not None:
                ui.paired(
                    tyndall, ("tyndall_height", "高度"), ("tyndall_range", "覆盖范围")
                )
                ui.fields(
                    tyndall,
                    "tyndall_density",
                    "tyndall_night",
                    "tyndall_scattering",
                    "tyndall_bottom",
                )
                if eevee:
                    ui.fields(tyndall, "tyndall_eevee")
                elif scene.render.engine == "CYCLES":
                    ui.fields(tyndall, "tyndall_cycles")

    cloud = ui.section(box, "cloud_open", "PARTICLES")
    if cloud is not None:
        modes = cloud.column(align=True)
        modes.enabled = root is not None and root.get(VERSION) == RESOURCE_VERSION
        applied = structure or {}
        _mode(modes, "启用云", "MASTER", applied.get("cloud_enabled"))
        _mode(modes, "启用云光照", "LIGHT", applied.get("cloud_light"))
        if eevee:
            _mode(modes, "启用 Eevee 高级云光照", "ADVANCED", applied.get("cloud_advanced"))
        _mode(modes, "启用云阴影", "SHADOW", applied.get("cloud_shadow"))
        if eevee:
            ui.note(cloud, "云距离和阴影距离越大，渲染开销越高。", "INFO")
            quality = cloud.box()
            quality.enabled = root is not None and root.get(VERSION) == RESOURCE_VERSION
            quality.label(text="阴影投影精度 · 明确应用")
            for key, label in (("ORIGINAL", "恢复各灯原值"), ("LARGE", "适合大范围 · 0.01"),
                               ("WIDE", "更大范围 · 0.02")):
                quality.operator("oxal_mc_studio.sky_shadow_quality", text=label).quality = key
            ui.note(quality, "数值更大时阴影更粗、占缓存更少；影响本天空灯投射的所有物体阴影。原值默认保留。", "INFO")
        if effective.auto_time:
            ui.note(cloud, "云光照自动过渡：随天空自动时间启用", "CHECKMARK")
        else:
            ui.prop(cloud, "cloud_light_auto")
        _mode(modes, "云光照阴影自动跟随高级模式", "LIGHT_SHADOW_AUTO", applied.get("cloud_shadow_auto"))
        manual = modes.row()
        manual.enabled = not applied.get("cloud_shadow_auto", True)
        _mode(manual, "云光照阴影（手动）", "LIGHT_SHADOW", applied.get("cloud_light_shadow"))
        if effective.cloud_light_auto or effective.auto_time:
            for title, energy, angle, transition in (
                ("日间 / 日出", "cloud_day_energy", "cloud_day_angle", "cloud_sunrise"),
                (
                    "夜间 / 日落",
                    "cloud_night_energy",
                    "cloud_night_angle",
                    "cloud_sunset",
                ),
            ):
                ui.note(cloud, title)
                ui.paired(cloud, (energy, "强度"), (angle, "角度"))
                ui.fields(cloud, transition)
        ui.prop(cloud, "cloud_moving")
        ui.fields(cloud, "cloud_height", "cloud_rotation")
        ui.fields(cloud, "cloud_distance", "cloud_shadow_distance")
        _action(cloud, "应用云范围", "cloud_apply", "CHECKMARK")
        pending = structure is not None and any(getattr(state, key) != structure[key] for key in ("cloud_distance", "cloud_shadow_distance"))
        ui.note(cloud, "云范围待应用" if pending else "云范围已应用", "TIME" if pending else "CHECKMARK")
        ui.note(cloud, "模式点击立即应用；范围需明确应用。关闭及范围外对象实际移除，不支持结构关键帧。", "INFO")
        if effective.cloud_moving:
            ui.prop(cloud, "cloud_speed")
        ui.prop(cloud.row(align=True), "cloud_speed_mode", text="", expand=True)
        ui.prop(
            cloud,
            "cloud_offset",
            text="手动云位移（叠加）" if effective.cloud_moving else "手动云位移",
        )


def register():
    bpy.utils.register_class(OXALMCSTUDIO_PG_sky_preset_item)
    try:
        common.register_state(OXALMCSTUDIO_PG_sky_ui, STATE_PROPERTY)
    except Exception:
        bpy.utils.unregister_class(OXALMCSTUDIO_PG_sky_preset_item)
        raise


def unregister():
    common.unregister_state(OXALMCSTUDIO_PG_sky_ui, STATE_PROPERTY)
    bpy.utils.unregister_class(OXALMCSTUDIO_PG_sky_preset_item)
