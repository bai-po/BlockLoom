"""Explicitly emitted, prebuilt native lightning flashes.

The immutable renderer profile supplies shape and tick timing. Creation occurs
only in an asset transaction. A trident emits a separate world-space instance;
later weapon movement cannot drag a previously emitted bolt around the scene.
"""

import json
import math
from functools import lru_cache

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import require_owned
from . import asset_catalog as catalog
from .asset_geometry import driver, mesh_object
from .asset_states import register_control

IDENTIFIER = "oxal:lightning"
_TRIDENT = "minecraft:trident"


@lru_cache(maxsize=1)
def profile():
    return catalog.read(catalog.ROOT / "native_effect/lightning_profile42.json")


class _Random:
    """The specified 48-bit Java PRNG; bounded samples include rejection."""

    def __init__(self, seed):
        self.state = (int(seed) ^ 0x5DEECE66D) & ((1 << 48) - 1)

    def bits(self, count):
        self.state = (self.state * 0x5DEECE66D + 0xB) & ((1 << 48) - 1)
        return self.state >> (48 - count)

    def integer(self, bound):
        if bound <= 0:
            raise ValueError("随机取样范围必须为正数")
        if bound & (bound - 1) == 0:
            return (bound * self.bits(31)) >> 31
        while True:
            value = self.bits(31)
            remainder = value % bound
            if value - remainder + bound - 1 < (1 << 31):
                return remainder

    def long(self):
        high, low = self.bits(32), self.bits(32)
        high -= (high >= (1 << 31)) * (1 << 32)
        low -= (low >= (1 << 31)) * (1 << 32)
        return (high << 32) + low


def schedule(seed):
    """Produce finite flash intervals in ticks; playback needs no observer."""
    spec, random = profile(), _Random(seed)
    flashes = 1 + random.integer(spec["flash_random_bound"])
    shape_seed = int(seed)
    life, tick, start = spec["initial_life"], 0, 0
    result = []
    while tick < 256:
        tick += 1
        life -= 1
        if life >= 0:
            continue
        if start is not None:
            result.append((start, tick, shape_seed))
            start = None
        if flashes == 0:
            return tuple(result)
        if life < -random.integer(spec["delay_random_bound"]):
            flashes -= 1
            life, shape_seed, start = spec["reflash_life"], random.long(), tick
    raise ValueError("雷电闪烁资源超出有限时间预算")


def geometry(seed):
    """Build the source renderer's tapered rectangular ribbons in Blender Z."""
    spec = profile()
    count, step = spec["segments"], spec["segment_height"]
    random = _Random(seed)
    offsets, x, y = {}, 0, 0
    for index in reversed(range(count)):
        offsets[index] = (x, y)
        x += random.integer(spec["main_jitter_bound"]) - spec["main_jitter_bound"] // 2
        y += random.integer(spec["main_jitter_bound"]) - spec["main_jitter_bound"] // 2
    origin = (x, y)
    paths = []
    random = _Random(seed)
    for branch in range(spec["branches"]):
        top = count - 1 - branch
        bottom = 0 if branch == 0 else top - spec["branch_segments"] + 1
        px, py = (offsets[top][axis] - origin[axis] for axis in (0, 1))
        for index in range(top, bottom - 1, -1):
            previous = (px, py)
            bound = spec["main_jitter_bound"] if branch == 0 else spec["branch_jitter_bound"]
            px += random.integer(bound) - bound // 2
            py += random.integer(bound) - bound // 2
            paths.append((branch, index, previous, (px, py)))
    vertices, faces = [], []
    for layer in range(spec["layers"]):
        width = spec["width_base"] + layer * spec["width_layer"]
        for branch, index, high, low in paths:
            high_width = width * (1 + index * spec["width_taper"] if branch == 0 else 1)
            low_width = width * (1 + (index - 1) * spec["width_taper"] if branch == 0 else 1)
            corners = ((-1, -1), (1, -1), (1, 1), (-1, 1))
            at = len(vertices)
            vertices.extend((low[0] + sx * low_width, low[1] + sy * low_width, index * step)
                            for sx, sy in corners)
            vertices.extend((high[0] + sx * high_width, high[1] + sy * high_width, (index + 1) * step)
                            for sx, sy in corners)
            faces.extend((at + i, at + (i + 1) % 4, at + (i + 1) % 4 + 4, at + i + 4)
                         for i in range(4))
    return vertices, faces


def _property(root, key, value, minimum, maximum):
    root[key] = value
    if not isinstance(value, bool):
        root.id_properties_ui(key).update(min=minimum, max=maximum)


def _variables(curve, entries):
    for name, owner, path in entries:
        variable = curve.driver.variables.new()
        variable.name, variable.type = name, "SINGLE_PROP"
        variable.targets[0].id_type = owner.id_type
        variable.targets[0].id, variable.targets[0].data_path = owner, path


def _material(transaction, root):
    mat = transaction.track(bpy.data.materials.new("雷电 · 原版半透明发光"))
    mat.use_nodes = True
    mat.surface_render_method = "BLENDED"
    tree = mat.node_tree
    tree.nodes.clear()
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs[0].default_value = profile()["rgba"]
    driver(emission.inputs[1], "default_value", "max(v,0)", root, "亮度")
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.inputs[0].default_value = profile()["rgba"][3]
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], output.inputs["Surface"])
    return mat


def requires_rig(identifier):
    return identifier == IDENTIFIER


def build_entry(transaction, scene, collection, root, identifier, options, material_for):
    if identifier != IDENTIFIER:
        return False
    ensure_mutation_allowed()
    seed = options.get("lightning_seed", 42)
    if isinstance(seed, bool) or not isinstance(seed, int) or not -(1 << 63) <= seed < (1 << 63):
        raise ValueError("雷电种子必须是64位整数")
    start = options.get("trigger_frame", scene.frame_current)
    if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start):
        raise ValueError("雷电触发帧无效")
    fields = (("显示", True, 0, 1), ("触发帧", float(start), -1048574, 1048574),
              ("高度", 128.0, 0.01, 4096), ("宽度倍率", 1.0, 0.001, 100),
              ("时间倍率", 1.0, 0.001, 100), ("亮度", 1.0, 0, 100))
    for key, value, minimum, maximum in fields:
        _property(root, key, value, minimum, maximum)
    root["随机种子"] = seed
    root["oxal_mc_studio_effect_fields"] = json.dumps([field[0] for field in fields], ensure_ascii=False)
    mat = _material(transaction, root)
    timeline = schedule(seed)
    for number, (first, end, shape_seed) in enumerate(timeline):
        vertices, faces = geometry(shape_seed)
        obj = mesh_object(transaction, "雷电 · 闪烁 " + str(number + 1), vertices, faces,
                          [[(0, 0), (1, 0), (1, 1), (0, 1)]] * len(faces), [mat], [0] * len(faces))
        collection.objects.link(obj)
        obj.parent = root
        obj["oxal_mc_studio_effect_geometry"] = True
        for axis, key, expression in ((0, "宽度倍率", "max(v,0)"), (1, "宽度倍率", "max(v,0)"),
                                      (2, "高度", "max(v,0)/128")):
            driver(obj, "scale", expression, root, key, axis)
        expression = f"not(show and {first}<=((frame-start)*base/max(fps,1)*20/max(speed,.001))<{end})"
        for path in ("hide_render", "hide_viewport"):
            curve = driver(obj, path, expression)
            _variables(curve, (("show", root, '["显示"]'), ("start", root, '["触发帧"]'),
                               ("speed", root, '["时间倍率"]'), ("base", scene, "render.fps_base"),
                               ("fps", scene, "render.fps")))
    root["oxal_mc_studio_lightning_intervals"] = json.dumps(timeline)
    return True


def build(transaction, scene, collection, root, identifier, options, material_for):
    if identifier == _TRIDENT:
        register_control(root, "oxal_mc_studio_channeling", "引雷42", "引雷", 0, 1, 0)


def emit(scene, source_root, options):
    """Create a world-space bolt in the enclosing explicit Undo operation."""
    from . import assets

    ensure_mutation_allowed()
    if source_root.get("oxal_mc_studio_library_identifier") != _TRIDENT:
        raise ValueError("请选择新版三叉戟实例")
    require_owned(source_root, assets.members(source_root))
    if "position" not in options or "trigger_frame" not in options:
        raise ValueError("引雷请求缺少点击时位置或触发帧")
    result = assets.import_asset(scene, {**options, "identifier": IDENTIFIER, "special": True,
                                        "destination": "CURSOR"})
    # The original rail remains editable; creation is an explicit action.
    rig = source_root if source_root.type == "ARMATURE" else source_root.get("oxal_mc_studio_control_rig")
    bone = rig.pose.bones.get("控制 · 引雷") if rig else None
    if bone:
        bone.location.x = 0.35
        rig.update_tag()
    source_root.update_tag()
    return result
