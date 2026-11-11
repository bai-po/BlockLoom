"""Explicit rate sampling, published as a private native Action clock."""

import hashlib
import json
import re
from typing import Any, cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.ownership import require_owned
from .asset_geometry import Creation, driver

CLOCK = "oxal_mc_studio_native_playback_clock"
TIME = "局部帧"
ACTIVE = "时间窗有效"
RATE = "粒子播放倍率"
META = "oxal_mc_studio_playback_"
ROLE = "oxal_mc_studio_native_clock"


def request(scene, root, options):
    from .asset_torch_particles import request as window_request

    return window_request(scene, root, options)


def _fingerprint(clock):
    animation = clock.animation_data
    if animation is None or animation.action is None or animation.nla_tracks:
        raise ValueError("原生粒子时钟的动画绑定已改变")
    curves = action_fcurves(animation.action, slot=getattr(animation, "action_slot", None))
    if len(curves) != 1 or curves[0].data_path != '["'+TIME+'"]' or curves[0].modifiers:
        raise ValueError("原生粒子时钟含用户曲线或修改器，未覆盖")
    curve = curves[0]
    return hashlib.sha256(json.dumps([
        curve.extrapolation,
        [(tuple(cast(Any, point.co)), tuple(cast(Any, point.handle_left)), tuple(cast(Any, point.handle_right)), point.interpolation)
         for point in curve.keyframe_points],
    ]).encode()).hexdigest()


def _write_action(tx, clock, samples):
    animation = clock.animation_data_create()
    old_action, old_slot, old_value = animation.action, getattr(animation, "action_slot", None), clock[TIME]

    def restore():
        animation.action = old_action
        if old_action is not None and hasattr(animation, "action_slot"):
            animation.action_slot = old_slot
        clock[TIME] = old_value

    tx.on_rollback(restore)
    animation.action = None
    try:
        for frame, value in samples:
            clock[TIME] = value
            if not clock.keyframe_insert('["'+TIME+'"]', frame=frame):
                raise ValueError("原生粒子时钟关键帧创建失败")
    finally:
        # This owner was explicitly detached first; keyframe_insert created the
        # Action on this exact binding, rather than a scene-wide ID difference.
        if animation.action is not None and animation.action != old_action:
            tx.track(animation.action)
    for curve in action_fcurves(animation.action, slot=getattr(animation, "action_slot", None)):
        curve.extrapolation = "CONSTANT"
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"


def _samples(scene, root, settings):
    from .asset_torch_particles import sample_clock

    return sample_clock(scene, root, settings["start"], settings["end"], settings["fps"], settings["fps_base"],
                        constant=settings["rate"], expected_signature=settings["rate_signature"])[0]


def create(tx, scene, collection, root, options):
    if RATE not in root:
        root[RATE] = 1.0
        root.id_properties_ui(RATE).update(min=0.0, max=8.0)
    source = options.get("_particles_source_root", root)
    if not isinstance(source, bpy.types.Object):
        raise TypeError("粒子时钟源不是可核验的资产根")
    if source != root:
        require_owned(source, (source,))
    settings = request(scene, source, options)
    samples = _samples(scene, source, settings)
    clock = tx.track(bpy.data.objects.new("粒子 · 原生积分时钟", None))
    collection.objects.link(clock)
    clock.parent = root
    clock[ROLE] = True
    clock["oxal_mc_studio_model_part"] = "原生粒子播放时钟"
    clock.hide_render, clock.hide_select, clock.empty_display_size = True, True, 0.0
    clock[TIME] = float(settings["start"])
    clock[ACTIVE] = True
    for key, value in settings.items():
        clock[META+key] = value
    _write_action(tx, clock, samples)
    curve = driver(clock, '["'+ACTIVE+'"]', "frame>=a and frame<=b")
    for name, key in (("a", "start"), ("b", "end")):
        variable = curve.driver.variables.new()
        variable.name, variable.type = name, "SINGLE_PROP"
        variable.targets[0].id = clock
        variable.targets[0].data_path = '["'+META+key+'"]'
    clock[META+"fingerprint"] = _fingerprint(clock)
    root[CLOCK] = clock
    return clock


def retarget(curve, clock):
    """Bind a freshly constructed native driver to the sampled local frame."""
    if not re.search(r"\bframe\b", curve.driver.expression):
        return curve
    name = "local_clock"
    if curve.driver.variables.get(name) is not None:
        raise ValueError("新粒子驱动的时钟变量重名")
    expression = re.sub(r"\bframe\b", name, curve.driver.expression)
    if len(expression.encode()) > 255:
        raise ValueError("粒子时钟驱动超过原生长度限制")
    variable = curve.driver.variables.new()
    variable.name, variable.type = name, "SINGLE_PROP"
    variable.targets[0].id = clock
    variable.targets[0].data_path = '["'+TIME+'"]'
    curve.driver.expression = expression
    return curve


def pending(scene, root):
    clock = root.get(CLOCK)
    if not isinstance(clock, bpy.types.Object):
        return True
    try:
        settings = request(scene, root, {})
        return (any(clock.get(META+key) != value for key, value in settings.items()
                    if key != "rate" or not settings["rate_signature"])
                or not settings["start"] <= scene.frame_current <= settings["end"])
    except (ValueError, TypeError, ReferenceError):
        return True


def rebuild(scene, root, options):
    from .asset_contracts import PROPERTY, stamp, validate

    ensure_mutation_allowed()
    validate(root)
    clock = root.get(CLOCK)
    if not isinstance(clock, bpy.types.Object):
        raise TypeError("原生粒子时钟已删除，请显式修复实例")
    _fingerprint(clock)
    animation = clock.animation_data
    if animation is None or animation.action is None:
        raise ValueError("原生粒子时钟缺少生成动画")
    require_owned(root, (root, clock, animation.action))
    if clock.get(META+"fingerprint") != _fingerprint(clock):
        raise ValueError("原生粒子时钟被用户修改，保留原曲线")
    settings = request(scene, root, options)
    samples = _samples(scene, root, settings)
    previous = {key: clock.get(META+key) for key in (*settings, "fingerprint")}
    old_contract, old_refs = root.get(PROPERTY), dict(root.get("oxal_mc_studio_contract_refs", {}).items())
    old_rate = root.get(RATE, 1.0)

    with Creation() as tx:
        tx.owner = root

        def restore():
            for key, value in previous.items():
                clock[META+key] = value
            root[PROPERTY] = old_contract
            root["oxal_mc_studio_contract_refs"] = old_refs
            root[RATE] = old_rate

        tx.on_rollback(restore)
        _write_action(tx, clock, samples)
        for key, value in settings.items():
            clock[META+key] = value
        if not settings["rate_signature"]:
            root[RATE] = settings["rate"]
        clock[META+"fingerprint"] = _fingerprint(clock)
        stamp(root)
        tx.commit()
    clock.update_tag()
    root.update_tag()
    return {"message": "已重建原生粒子播放时钟", "instance_id": root.get("oxal_mc_studio_instance_id", "")}


def publish_staged(tx, owner, previous, following):
    """Retain the exact generated clock Object while replacing its sampled Action."""
    if not previous.get(ROLE) or not following.get(ROLE):
        raise ValueError('原生辅助时钟角色无法确认，未替换动画')
    if owner.get(CLOCK)!=previous or following not in tx.ids:
        raise ValueError('原生辅助时钟实际绑定无法确认，未替换动画')
    old=previous.animation_data;new=following.animation_data
    if old is None or new is None or old.action is None or new.action is None:
        raise ValueError('原生辅助时钟缺少生成Action')
    require_owned(owner,(previous,old.action))
    if new.action not in tx.ids:raise ValueError('暂存时钟Action缺少创建来源')
    if previous.get(META+'fingerprint')!=_fingerprint(previous) or following.get(META+'fingerprint')!=_fingerprint(following):
        raise ValueError('原生辅助时钟含用户曲线修改，未覆盖')
    for key in ('action_blend_type','action_extrapolation','action_influence','use_nla'):
        if getattr(old,key)!=getattr(new,key):raise ValueError('原生辅助时钟的动画混合配置已改变，未覆盖')
    from .asset_rebuild import _value
    props=_value(dict(previous.items()));action,slot=old.action,getattr(old,'action_slot',None)
    def restore():
        old.action=action
        if hasattr(old,'action_slot'):old.action_slot=slot
        for key in tuple(previous.keys()):
            if key not in props:del previous[key]
        for key,value in props.items():previous[key]=value
        previous.update_tag()
    tx.on_rollback(restore)
    # Never edit or remove the old Action: ownership pruning keeps external
    # users, while native Undo retains its original actual ID binding.
    old.action=new.action
    if hasattr(old,'action_slot'):old.action_slot=new.action_slot
    previous[TIME]=following[TIME]
    for key,value in following.items():
        if key.startswith(META):previous[key]=value
    previous[META+'fingerprint']=_fingerprint(previous)
    previous.update_tag()
