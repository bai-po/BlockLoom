"""Source spin-attack shells as an independent, entirely native effect."""

import json
import math

from ...core.runtime import ensure_mutation_allowed
from . import asset_catalog as catalog
from .asset_geometry import driver
from .asset_lightning import _property, _variables
from .asset_special import _entity_mesh

IDENTIFIER = "oxal:riptide"


def requires_rig(identifier):
    return False


def build_entry(transaction, scene, collection, root, identifier, options, material_for):
    if identifier != IDENTIFIER:
        return False
    ensure_mutation_allowed()
    spec = catalog.read(catalog.ROOT / "native_effect/riptide_profile42.json")
    fields = (("显示", True, 0, 1), ("阶段", 2, 0, 3), ("阶段进度", 1.0, 0, 1),
              ("大小", 1.0, 0, 100), ("旋转倍率", 1.0, -100, 100),
              ("开始帧", float(scene.frame_current), -1048574, 1048574))
    for key, value, minimum, maximum in fields:
        _property(root, key, value, minimum, maximum)
    root["oxal_mc_studio_effect_fields"] = json.dumps([field[0] for field in fields], ensure_ascii=False)
    texture = "minecraft:" + spec["texture"].removeprefix("textures/").removesuffix(".png")
    original = material_for(texture)
    material = transaction.track(original.copy())
    material.name = "激流 · 原版双层旋壳"
    material.surface_render_method = "BLENDED"
    for index, box in enumerate(spec["boxes"]):
        obj = _entity_mesh(transaction, "激流旋壳 · " + str(index + 1),
                           [{"from": box["origin"], "size": box["size"], "uv": box["uv"]}],
                           spec["atlas"], material, -1)
        collection.objects.link(obj)
        obj.parent = root
        obj["oxal_mc_studio_effect_geometry"] = True
        for axis in range(3):
            expression = f"{box['scale']}*size*(min(max(p,0),1) if phase==1 else 1-min(max(p,0),1) if phase==3 else 1 if phase==2 else 0)"
            curve = driver(obj, "scale", expression, index=axis)
            _variables(curve, (("size", root, '["大小"]'), ("p", root, '["阶段进度"]'),
                               ("phase", root, '["阶段"]')))
        expression = f"{math.radians(box['degrees_per_tick'])}*(frame-start)*20*base/max(fps,1)*speed"
        curve = driver(obj, "rotation_euler", expression, index=2)
        _variables(curve, (("start", root, '["开始帧"]'), ("speed", root, '["旋转倍率"]'),
                           ("base", scene, "render.fps_base"), ("fps", scene, "render.fps")))
        for path in ("hide_render", "hide_viewport"):
            curve = driver(obj, path, "not(show and 0<phase<4 and size>0)")
            _variables(curve, (("show", root, '["显示"]'), ("phase", root, '["阶段"]'),
                               ("size", root, '["大小"]')))
    return True
