"""Explicit, reversible native shadow precision for exact-owned sky lights."""

import math

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import require_owned
from ...util.resources import INSTANCE, VERSION

ORIGINALS = "oxal_mc_studio_sky_shadow_originals"
LIMITS = {"LARGE": 0.01, "WIDE": 0.02}


def lights_for(scene):
    from . import sky
    from .sky_repair import roles_for

    root = sky.root_for(scene)
    if root is None or root.get(VERSION) != sky.RESOURCE_VERSION:
        raise ValueError("请先加载或修复当前 Beta 天空")
    lights = tuple(obj for obj in root.all_objects if obj.type == "LIGHT")
    if len(lights) != 3:
        raise ValueError("天空灯角色不完整或已加入其它灯，未修改投影精度")
    roles = roles_for(root, sky.controller_for(root))
    sun, cloud = roles["sun_mesh"].parent, roles["cloud_light"]
    moon = set(lights) - {sun, cloud}
    if sun not in lights or cloud not in lights or sun == cloud or len(moon) != 1:
        raise ValueError("天空灯角色引用不唯一，未猜测修改对象")
    data = {"sun": sun.data, "moon": moon.pop().data, "cloud": cloud.data}
    if len(set(data.values())) != 3:
        raise ValueError("天空灯共用了数据，未改写共享灯")
    require_owned(root, (root, *lights, *data.values()))
    domain = sky._dependency_domain(root)
    if any(user not in domain for refs in bpy.data.user_map(subset=tuple(data.values())).values() for user in refs):
        raise ValueError("天空灯数据被外部引用，未改变其它对象的阴影")
    return root, data


def _baseline(root, lights):
    saved = root.get(ORIGINALS)
    if saved is None:
        return {role: float(light.shadow_maximum_resolution) for role, light in lights.items()}
    if set(saved) != set(lights) or any(saved[role]["data"] != lights[role] for role in lights):
        raise ValueError("原投影精度的灯引用已改变，未猜测恢复目标")
    return {role: float(saved[role]["value"]) for role in lights}


def request(scene, quality):
    from .sky_repair import _no_animation, curve

    if quality not in {*LIMITS, "ORIGINAL"}:
        raise ValueError("未知云投影精度")
    root, lights = lights_for(scene)
    original = _baseline(root, lights)
    entries = []
    for role, light in sorted(lights.items()):
        prop = light.bl_rna.properties.get("shadow_maximum_resolution")
        if prop is None or prop.type != "FLOAT" or prop.is_readonly or light.is_property_readonly(prop.identifier):
            raise ValueError("当前灯不支持可写的原生阴影精度")
        if light.library or light.override_library:
            raise ValueError("天空灯数据只读，未调整阴影")
        if curve(light, prop.identifier) is not None:
            raise ValueError("天空灯投影精度已有用户驱动，未覆盖")
        _no_animation(light, {prop.identifier})
        after = original[role] if quality == "ORIGINAL" else LIMITS[quality]
        if not math.isfinite(after) or not prop.hard_min <= after <= prop.hard_max:
            raise ValueError("所选投影精度超出当前构建原生 RNA 范围")
        entries.append({"role": role, "before": float(light.shadow_maximum_resolution),
                        "after": after, "original": original[role]})
    return {"scene_uid": scene.session_uid, "root_uid": root.session_uid,
            "instance": root[INSTANCE], "quality": quality, "entries": entries,
            "recorded": ORIGINALS in root}


def apply(scene, options):
    import json

    ensure_mutation_allowed()
    expected = options.get("snapshot")
    if not isinstance(expected, dict):
        raise TypeError("缺少阴影精度快照")
    current = request(scene, expected.get("quality"))
    def authorization(plan):
        return {key: value for key, value in plan.items() if key != "root_uid"}
    if json.dumps(authorization(expected), sort_keys=True) != json.dumps(authorization(current), sort_keys=True):
        raise ValueError("天空灯或投影精度已改变，请重新应用")
    root, lights = lights_for(scene)
    if current["quality"] == "ORIGINAL" and not current["recorded"]:
        return "尚未明确调整投影精度；各灯原值保持"
    targets = lights
    saved = ({key: dict(record.items()) for key, record in root[ORIGINALS].items()}
             if ORIGINALS in root else None)
    try:
        for entry in current["entries"]:
            light = targets[entry["role"]]
            light.shadow_maximum_resolution = entry["after"]
            light.update_tag()
        if saved is None:
            root[ORIGINALS] = {entry["role"]: {"data": targets[entry["role"]], "value": entry["original"]}
                               for entry in current["entries"]}
    except Exception:
        try:
            for entry in current["entries"]:
                light = targets[entry["role"]]
                light.shadow_maximum_resolution = entry["before"]
                light.update_tag()
        finally:
            if saved is None:
                if ORIGINALS in root:
                    del root[ORIGINALS]
            else:
                root[ORIGINALS] = saved
        raise
    return "已明确调整本天空灯的投影精度；阴影缓存容量保持原值"
