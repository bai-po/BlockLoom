"""Explicit sky-mode snapshots, cache confirmation and transactional execution."""

import json
from collections.abc import Mapping

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import require_owned
from ...util.resources import INSTANCE, VERSION
from . import sky_schema, sky_state

MODES = {"LIGHT": "cloud_light", "ADVANCED": "cloud_advanced", "SHADOW": "cloud_shadow"}


def cache_capability(scene):
    """Read the actual host enum; newer capacities are never invented."""
    settings = getattr(scene, "eevee", None)
    prop = settings.bl_rna.properties.get("shadow_pool_size") if settings else None
    if (settings is None or prop is None or prop.type != "ENUM" or prop.is_readonly
            or settings.is_property_readonly("shadow_pool_size")):
        raise ValueError("当前构建不支持调整 Eevee 阴影缓存，未启用云模式")
    current = str(settings.shadow_pool_size)
    try:
        capacity = int(current)
    except ValueError as error:
        raise ValueError("无法识别当前阴影缓存容量，未修改场景") from error
    choices = [
        (item.identifier, item.name)
        for item in prop.enum_items
        if item.identifier.isdigit() and int(item.identifier) >= max(1024, capacity)
    ]
    choices.sort(key=lambda row: int(row[0]))
    if not choices:
        raise ValueError("当前构建没有至少 1 GB 且不低于现值的阴影缓存选项")
    return current, choices


def actual_structure(controller):
    return {
        name: controller[sky_schema.PARAMETERS[name][2]]
        for name in sky_schema.CLOUD_STRUCTURE_KEYS
    }


def normalize_modes(settings):
    """The legacy UI's three lighting choices are mutually exclusive."""
    result = dict(settings)
    selected = next((name for name in ("cloud_advanced", "cloud_shadow", "cloud_light") if result[name]), None)
    for name in MODES.values():
        result[name] = name == selected
    return result


def require_cache(scene, settings):
    """Backstop every structural entry; presets cannot silently raise a cache."""
    shadow = settings["cloud_enabled"] and (
        settings["cloud_advanced"] or settings["cloud_shadow"] or (
            not settings["cloud_shadow_auto"] and settings["cloud_light_shadow"]))
    if scene.render.engine.startswith("BLENDER_EEVEE") and shadow:
        current, _choices = cache_capability(scene)
        if int(current) < 1024:
            raise ValueError("请先通过云模式按钮确认至少 1 GB 阴影缓存，再应用此设置")
    if settings["cloud_advanced"] and not scene.render.engine.startswith("BLENDER_EEVEE"):
        raise ValueError("高级云光照仅用于 Eevee；未切换渲染引擎")


def request(scene, intent):
    """Read-only click-time plan. No pending request or scene write is created."""
    from . import sky, sky_linking, sky_repair

    if intent not in {*MODES, "MASTER", "REPAIR", "LIGHT_SHADOW_AUTO", "LIGHT_SHADOW"}:
        raise ValueError("未知天空模式")
    root = sky.root_for(scene)
    if root is None:
        raise ValueError("请先加载 OXAL 天空")
    controller = sky.controller_for(root)
    require_owned(root, (root, controller))
    before = actual_structure(controller)
    after = normalize_modes(before)
    if intent == "REPAIR":
        sky_repair.preflight(scene, root, controller)
    else:
        if root.get(VERSION) != sky.RESOURCE_VERSION:
            raise ValueError("请先点击“修复已有 Beta 天空”，再切换云模式")
        if intent in MODES:
            key = MODES[intent]
            enabled = not bool(before[key])
            for name in MODES.values():
                after[name] = name == key and enabled
        elif intent == "MASTER":
            after["cloud_enabled"] = not bool(before["cloud_enabled"])
        elif intent == "LIGHT_SHADOW_AUTO":
            after["cloud_shadow_auto"] = not bool(before["cloud_shadow_auto"])
        else:
            after["cloud_light_shadow"] = not bool(before["cloud_light_shadow"])
    eevee = scene.render.engine.startswith("BLENDER_EEVEE")
    if intent != "REPAIR":
        after, _old, _changed = sky.cloud_structure_plan(scene, root, controller, after)
    if after["cloud_advanced"] and not eevee:
        raise ValueError("高级云光照仅用于 Eevee；未切换渲染引擎")
    needs_shadow = after["cloud_enabled"] and (
        after["cloud_shadow"] or after["cloud_advanced"]
        or (not after["cloud_shadow_auto"] and after["cloud_light_shadow"])
    )
    cache, choices = cache_capability(scene) if eevee and needs_shadow else (None, [])
    return {
        "scene_uid": scene.session_uid,
        "root_uid": root.session_uid,
        "instance_id": root[INSTANCE],
        "resource_version": root[VERSION],
        "engine": scene.render.engine,
        "intent": intent,
        "before": before,
        "after": after,
        "lighting_links": sky_linking.snapshot(root, controller, after),
        "cache": cache,
        "cache_choices": choices,
        "needs_confirmation": cache is not None and int(cache) < 1024,
    }


def execute(scene, options: Mapping[str, object]):
    from . import sky, sky_repair

    ensure_mutation_allowed()
    expected = options.get("snapshot")
    if not isinstance(expected, dict):
        raise TypeError("缺少天空模式快照，请从面板重新操作")
    current = request(scene, expected.get("intent"))
    # JSON snapshots normalize tuple/list containers. Compare their values only.
    def authorization(plan):
        return {key: value for key, value in plan.items() if key != "root_uid"}
    if json.dumps(authorization(current), sort_keys=True) != json.dumps(authorization(expected), sort_keys=True):
        raise ValueError("确认期间场景、天空模式或缓存已改变，请重新操作")
    selected = options.get("cache_size")
    if current["needs_confirmation"] and options.get("cache_confirmed") is not True:
        raise ValueError("启用前需要明确确认将 Eevee 阴影缓存提高至至少 1 GB")
    if selected is None and current["cache"] is not None:
        selected = current["cache"]
    if current["cache"] is not None:
        allowed = {row[0] for row in current["cache_choices"]}
        if selected not in allowed:
            raise ValueError("所选缓存不受当前构建支持、低于 1 GB 或会降低现值")
    root = sky.root_for(scene)
    controller = sky.controller_for(root)
    stored = sky_state.capture_inputs(scene)
    old_cache = current["cache"]
    try:
        # Cache mutation is explicit, after confirmation and complete preflight.
        if old_cache is not None and selected != old_cache:
            scene.eevee.shadow_pool_size = selected
        values = getattr(scene, sky_state.VALUES)
        for name in sky_schema.CLOUD_STRUCTURE_KEYS:
            if name not in sky.CLOUD_WINDOW_KEYS:
                setattr(values, name, current["after"][name])
        if current["intent"] == "REPAIR":
            result = sky_repair.apply(scene, root, controller, current["after"])
        else:
            result = sky.replace_cloud_structure(scene, current["after"])
    except Exception:
        if old_cache is not None:
            scene.eevee.shadow_pool_size = old_cache
        sky_state.restore_inputs(scene, stored)
        raise
    return result
