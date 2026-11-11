"""Sky parameter contract. Native data paths are an asset interface, not code hooks."""

from typing import Any

# key: (caption, default, native controller input)
PARAMETERS: dict[str, tuple[str, Any, str]] = {
    "time": ("时间", 8.0, "时间"),
    "auto_time": ("自动时间", False, "自动时间"),
    "start_time": ("起始时间", 8.0, "起始时间"),
    "frames_per_hour": ("每小时帧数", 100.0, "每小时帧数"),
    "sunrise_start": ("日出开始时间", 5.0, "日出开始时间"),
    "sunrise_end": ("日出完成时间", 8.0, "日出完成时间"),
    "sunset_start": ("日落开始时间", 18.0, "日落开始时间"),
    "night_start": ("夜晚完成过渡时间", 20.0, "夜晚完成过渡时间"),
    "azimuth": ("天体方位（太阳/月亮）", 0.0, "天体方位"),
    "celestial_3d": ("使用3D日月贴图", False, "使用3D日月贴图"),
    "celestial_rotation": ("旋转日月", True, "日月45度旋转"),
    "sun_energy": ("日光强度", 6.5, "太阳强度"),
    "ambient_energy": ("环境强度", 0.8, "环境强度"),
    "sun_size": ("太阳画面大小", 1.0, "太阳画面大小"),
    "sun_brightness": ("太阳贴图亮度", 6.0, "太阳画面亮度"),
    "sun_saturation": ("日光饱和度", 1.0, "太阳光照饱和度"),
    "day_saturation": ("白天天空饱和度", 1.0, "白天天空饱和度"),
    "sun_fade": ("太阳渐显开始", 5.5, "太阳渐显开始"),
    "sun_transition": ("太阳过渡时长", 2.0, "太阳过渡时长"),
    "moon_energy": ("月光强度", 0.8, "月光强度"),
    "moon_phase": ("月相", 4, "月相"),
    "moon_brightness": ("月亮贴图亮度", 6.0, "月亮亮度"),
    "moon_size": ("月亮大小", 1.0, "月亮大小"),
    "night_ambient": ("月夜环境光", 0.08, "月夜环境光"),
    "moon_saturation": ("月光饱和度", 1.0, "月光蓝色饱和度"),
    "night_saturation": ("夜晚天空饱和度", 1.0, "夜晚天空饱和度"),
    "moon_temperature": ("月光冷暖", 0.25, "月光冷暖"),
    "moon_softness": ("月光柔和", 0.1, "月光柔和"),
    "moon_fade": ("月亮渐显开始", 17.5, "月亮渐显开始"),
    "moon_transition": ("月亮过渡时长", 2.0, "月亮过渡时长"),
    "stars_enabled": ("星空开关", True, "星空开关"),
    "stars_brightness": ("星空亮度", 3.0, "星空亮度"),
    "stars_density": ("星空密度", 85.0, "星空密度"),
    "stars_size": ("星点大小", 0.085, "星点大小"),
    "stars_fade": ("星空渐显开始", 18.0, "星空渐显开始"),
    "stars_transition": ("星空过渡时长", 2.0, "星空过渡时长"),
    "stars_speed": ("星空时间速度", 0.2, "星空时间速度"),
    "fog_enabled": ("启用天空雾", True, "天空雾启用"),
    "fog_distance": ("起始距离", 130.0, "雾起始距离"),
    "fog_transition": ("过渡距离", 150.0, "雾过渡距离"),
    "fog_density": ("纹理浓度", 0.35, "雾浓度"),
    "fog_base": ("基础浓度", 0.0, "天空雾基础浓度"),
    "fog_gradient": ("雾渐变高度", 500.0, "雾高度"),
    "fog_bottom": ("底部高度", 0.0, "天空雾底部高度"),
    "fog_scattering": ("散射方向", 0.25, "雾散射方向"),
    "fog_eevee": ("Eevee倍率", 1.0, "天空雾Eevee倍率"),
    "fog_height": ("高度", 500.0, "oxal_mc_studio_fog_height"),
    "fog_range": ("覆盖范围", 1024.0, "oxal_mc_studio_fog_range"),
    "tyndall_enabled": ("启用丁达尔雾", True, "丁达尔雾启用"),
    "tyndall_density": ("浓度", 0.0008, "丁达尔雾浓度"),
    "tyndall_night": ("夜晚密度倍率", 0.5, "丁达尔夜晚密度倍率"),
    "tyndall_scattering": ("散射方向", 0.72, "丁达尔散射方向"),
    "tyndall_range": ("范围", 734.46, "丁达尔雾范围"),
    "tyndall_height": ("高度", 150.0, "丁达尔雾高度"),
    "tyndall_bottom": ("底部高度", 0.0, "丁达尔雾底部高度"),
    "tyndall_eevee": ("Eevee倍率", 1.0, "丁达尔Eevee倍率"),
    "tyndall_cycles": ("Cycles倍率", 1.0, "丁达尔Cycles倍率"),
    "cloud_enabled": ("启用云", True, "oxal_mc_studio_cloud_enabled"),
    "cloud_light": ("启用云光照", True, "云光照启用"),
    "cloud_advanced": ("启用Eevee高级云光照", False, "Eevee高级云光照"),
    "cloud_shadow": ("启用云阴影", False, "云阴影"),
    "cloud_light_auto": ("云光照自动过渡", True, "云光照自动过渡"),
    "cloud_shadow_auto": ("云光照阴影自动跟随高级模式", True, "云光照阴影自动控制"),
    "cloud_light_shadow": ("云光照阴影（手动）", False, "oxal_mc_studio_cloud_shadow_manual"),
    "cloud_day_energy": ("云光照日间强度", 3.5, "云光照日间强度"),
    "cloud_night_energy": ("云光照夜间强度", 1.0, "云光照夜间强度"),
    "cloud_day_angle": ("云光照日间角度", 60.0, "云光照日间角度"),
    "cloud_night_angle": ("云光照夜间角度", -60.0, "云光照夜间角度"),
    "cloud_sunrise": ("日出过渡倍率", 2.0, "云光照日出过渡倍率"),
    "cloud_sunset": ("日落过渡倍率", 4.0, "云光照日落过渡倍率"),
    "cloud_moving": ("自动云移动", False, "云移动启用"),
    "cloud_height": ("云高度", 50.0, "云高度"),
    "cloud_rotation": ("云旋转角度", 0.0, "云旋转角度"),
    "cloud_distance": ("云距离（N×N）", 4, "云距离"),
    "cloud_shadow_distance": ("云阴影距离（N×N）", 4, "云阴影距离"),
    "cloud_speed": ("基础速度", 50.0, "云移动速度"),
    "cloud_speed_mode": ("速度模式", "SAME", "云移动速度模式"),
    "cloud_offset": ("手动云位移", 0.0, "云手动X位移"),
}

# Global geometry/mode settings never participate in automatic slot animation.
CLOUD_STRUCTURE_KEYS = (
    "cloud_enabled", "cloud_light", "cloud_advanced", "cloud_shadow",
    "cloud_shadow_auto", "cloud_light_shadow", "cloud_distance", "cloud_shadow_distance",
)

# Native local.31 controller UI limits, including its distinct soft ranges.
RANGES = {
    'time': (0.0, 24.0),
    'start_time': (0.0, 24.0),
    'frames_per_hour': (1.0, 5000.0),
    'sunrise_start': (0.1, 10.0),
    'sunrise_end': (0.2, 12.0),
    'sunset_start': (12.0, 23.0),
    'night_start': (12.1, 23.99),
    'azimuth': (-180.0, 180.0),
    'sun_energy': (0.0, 10.0),
    'ambient_energy': (0.0, 3.0),
    'sun_size': (0.25, 4.0),
    'sun_brightness': (0.0, 10.0),
    'sun_saturation': (0.0, 5.0),
    'day_saturation': (0.0, 3.0),
    'sun_fade': (0.0, 11.0),
    'sun_transition': (0.05, 6.0),
    'moon_energy': (0.0, 10.0),
    'moon_phase': (0, 7),
    'moon_brightness': (0.0, 10.0),
    'moon_size': (0.25, 4.0),
    'night_ambient': (0.0, 0.5),
    'moon_saturation': (0.0, 5.0),
    'night_saturation': (0.0, 3.0),
    'moon_temperature': (0.0, 1.0),
    'moon_softness': (0.1, 15.0),
    'moon_fade': (12.0, 23.0),
    'moon_transition': (0.05, 6.0),
    'stars_brightness': (0.0, 10.0),
    'stars_density': (20.0, 180.0),
    'stars_size': (0.025, 0.16),
    'stars_fade': (16.0, 20.0),
    'stars_transition': (0.25, 4.0),
    'stars_speed': (0.0, 4.0),
    'fog_distance': (0.0, 1000.0),
    'fog_transition': (1.0, 1500.0),
    'fog_density': (0.0, 10.0),
    'fog_base': (0.0, 2.0),
    'fog_gradient': (0.0, 1000.0),
    'fog_bottom': (-1000.0, 1000.0),
    'fog_scattering': (-0.8, 0.8),
    'fog_eevee': (0.0, 4.0),
    'fog_height': (0.01, 1000000.0),
    'fog_range': (0.01, 1000000.0),
    'tyndall_density': (0.0, 0.05),
    'tyndall_night': (0.0, 1.0),
    'tyndall_scattering': (-1.0, 1.0),
    'tyndall_range': (10.0, 5000.0),
    'tyndall_height': (20.0, 1000.0),
    'tyndall_bottom': (-1000.0, 1000.0),
    'tyndall_eevee': (0.0, 4.0),
    'tyndall_cycles': (0.0, 2.0),
    'cloud_day_energy': (0.0, 20.0),
    'cloud_night_energy': (0.0, 20.0),
    'cloud_day_angle': (-180.0, 180.0),
    'cloud_night_angle': (-180.0, 180.0),
    'cloud_sunrise': (0.1, 8.0),
    'cloud_sunset': (0.1, 8.0),
    'cloud_height': (-1000.0, 5000.0),
    'cloud_rotation': (-180.0, 180.0),
    'cloud_distance': (2, 8),
    'cloud_shadow_distance': (2, 8),
    'cloud_speed': (0.0, 10000.0),
    'cloud_offset': (0.0, 1000000.0),
}

SOFT_RANGES = {
    'sun_saturation': (0.0, 3.0),
    'day_saturation': (0.0, 2.0),
    'sun_fade': (3.0, 7.0),
    'sun_transition': (0.5, 3.0),
    'moon_saturation': (0.0, 3.0),
    'night_saturation': (0.0, 2.0),
    'moon_transition': (0.5, 3.0),
    'stars_speed': (0.0, 2.0),
    'fog_base': (0.0, 0.5),
    'fog_bottom': (-300.0, 300.0),
    'fog_eevee': (0.0, 2.0),
    'fog_height': (1.0, 1000.0),
    'fog_range': (10.0, 5000.0),
    'tyndall_density': (0.0, 0.01),
    'tyndall_scattering': (0.0, 0.9),
    'tyndall_range': (100.0, 1000.0),
    'tyndall_height': (80.0, 500.0),
    'tyndall_bottom': (-300.0, 300.0),
    'tyndall_eevee': (0.0, 2.0),
    'cloud_height': (-100.0, 1000.0),
    'cloud_rotation': (-90.0, 90.0),
    'cloud_speed': (0.0, 500.0),
    'cloud_offset': (0.0, 10000.0),
}
SIGNED = {
    "azimuth",
    "cloud_rotation",
    "cloud_offset",
    "cloud_speed",
    "cloud_height",
    "fog_bottom",
    "tyndall_bottom",
    "cloud_day_angle",
    "cloud_night_angle",
}
SPEED_ITEMS = (
    ("SAME", "高低云同速", "", 0),
    ("HIGH", "高云更快", "", 1),
    ("LOW", "低云更快", "", 2),
)


def snapshot(values) -> dict[str, Any]:
    return {key: getattr(values, key) for key in PARAMETERS}


def validated(values: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete portable snapshot before changing any property."""
    import math

    if set(values) != set(PARAMETERS):
        raise ValueError("天空参数字段与当前格式不一致")
    result = {}
    for key, (_, default, _) in PARAMETERS.items():
        value = values[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise TypeError(f"{key} 必须为布尔值")
        elif isinstance(default, str):
            if value not in {item[0] for item in SPEED_ITEMS}:
                raise ValueError("不支持的云移动速度模式")
        else:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{key} 必须为有限数值")
            if isinstance(default, int) and int(value) != value:
                raise ValueError(f"{key} 必须为整数")
            low, high = RANGES[key]
            if not low <= value <= high:
                raise ValueError(f"{key} 超出允许范围")
        result[key] = value
    if not (
        result["sunrise_start"]
        < result["sunrise_end"]
        <= result["sunset_start"]
        < result["night_start"]
    ):
        raise ValueError("昼夜过渡时间必须依次为日出开始、日出完成、日落开始、夜晚完成")
    return result
