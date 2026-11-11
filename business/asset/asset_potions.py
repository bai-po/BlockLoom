"""Native potion overlay palette from the pinned registry declarations.

Only canonical overlay materials receive color. Glass and arrow shafts keep
their own texture, alpha, animation and resource-pack provenance. No callback
decodes identities, replaces materials or computes a color during playback.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from .asset_geometry import driver
from .asset_states import register_control

_BASE = Path(__file__).resolve().parents[2] / "asset/library"
_KINDS = {"potion", "splash_potion", "lingering_potion", "tipped_arrow", "arrow"}
_CHOICE = "药水种类"
_IMAGE = "oxal_mc_studio_potion_palette"
_ROWS = "oxal_mc_studio_potion_definitions"
_PREFIX = "OXAL Potion "


def supports(identifier):
    return str(identifier).split(":")[-1] in _KINDS


def requires_rig(identifier):
    return supports(identifier)


@lru_cache(maxsize=1)
def definitions():
    rows = json.loads((_BASE / "native_effect/potion_definitions217.json").read_text("utf-8"))["definitions"]
    rows.extend(json.loads((_BASE / "potion_definitions.json").read_text("utf-8"))["definitions"])
    names = json.loads((_BASE / "assets/minecraft/lang/zh_cn.json").read_text("utf-8"))
    result, seen = [], set()
    for row in rows:
        key = str(row["key"])
        if key in seen:
            raise ValueError("药水资源存在重复标识")
        seen.add(key)
        base = key.removeprefix("long_").removeprefix("strong_")
        label = names.get("item.minecraft.potion.effect." + base, row["display"])
        if key.startswith("long_"):
            label += "（延长）"
        elif key.startswith("strong_"):
            label += " II"
        result.append({**row, "display": label})
    return tuple(result)


def _linear_color(number):
    channels = [((int(number) >> shift) & 255) / 255 for shift in (16, 8, 0)]
    return tuple(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels)


def _palette(transaction, root, *, raw=False):
    image_key=_IMAGE+"_particle_components" if raw else _IMAGE
    image = root.get(image_key)
    if image is not None:
        return image
    rows = definitions()
    image = bpy.data.images.new("OXAL · 原版药水颜色表", width=1, height=len(rows), alpha=True, float_buffer=True)
    transaction.track(image)
    cast(Any, image.colorspace_settings).name = "Non-Color"
    color=(lambda number:tuple(((int(number)>>shift)&255)/255 for shift in (16,8,0))) if raw else _linear_color
    cast(Any, image.pixels).foreach_set([part for row in rows for part in (*color(row["color"]), 1.0)])
    image.pack()
    root[image_key] = image
    return image


def _install(transaction, root, material, image):
    tree = material.node_tree
    marker = tree.nodes.get(_PREFIX + "Palette")
    if marker is not None:
        return
    tint = tree.nodes.get("OXAL Tint")
    if tint is None or tint.type != "MIX_RGB" or tint.blend_type != "MULTIPLY":
        raise ValueError("药水覆盖材质缺少可验证的原生颜色输入")
    sampler = tree.nodes.new("ShaderNodeTexImage")
    sampler.name = _PREFIX + "Palette"
    sampler.image = image
    sampler.interpolation = "Closest"
    sampler.extension = "EXTEND"
    index = tree.nodes.new("ShaderNodeValue")
    index.name = _PREFIX + "Index"
    driver(index.outputs[0], "default_value", "v", root, _CHOICE)
    y = tree.nodes.new("ShaderNodeMath")
    y.name = _PREFIX + "Row"
    y.operation = "MULTIPLY_ADD"
    y.inputs[1].default_value = 1 / len(definitions())
    y.inputs[2].default_value = 0.5 / len(definitions())
    tree.links.new(index.outputs[0], y.inputs[0])
    uv = tree.nodes.new("ShaderNodeCombineXYZ")
    uv.name = _PREFIX + "Coordinate"
    uv.inputs[0].default_value = 0.5
    tree.links.new(y.outputs[0], uv.inputs[1])
    tree.links.new(uv.outputs[0], sampler.inputs["Vector"])
    tree.links.new(sampler.outputs["Color"], tint.inputs[2])
    material["oxal_mc_studio_potion_overlay"] = True


def apply_palette(transaction, scene, collection, root, identifier, options, material_for):
    if not supports(identifier):
        return
    ensure_mutation_allowed()
    # GUI and held meshes live below distinct transform pivots, but their
    # color belongs to the single independently editable library instance.
    branch = root
    if root.get("oxal_mc_studio_representation") in {"GUI", "HELD", "PROJECTILE"}:
        if root.parent is None:
            raise ValueError("药水显示分支没有资产根")
        root = root.parent
    rows = definitions()
    key = str(options.get("potion_key", "poison")).removeprefix("minecraft:")
    indices = {row["key"]: index for index, row in enumerate(rows)}
    if key not in indices:
        raise ValueError("未知原版药水预设：" + key)
    if _CHOICE not in root:
        register_control(root, "oxal_mc_studio_potion_choice", _CHOICE, "药水种类", 0,
                         len(rows) - 1, indices[key], tuple(row["display"] for row in rows))
        root[_ROWS] = json.dumps(rows, ensure_ascii=False)
        root["oxal_mc_studio_potion_schema"] = 1
    image = _palette(transaction, root)
    if root.get("oxal_mc_studio_projectile_schema") and root.get("oxal_mc_studio_library_identifier","").split(":")[-1] in {"splash_potion","lingering_potion"}:
        path='["瞬时药水"]'
        if not root.animation_data or root.animation_data.drivers.find(path) is None:
            indices=[index for index,row in enumerate(rows) if row.get("instant")]
            expression=" or ".join(f"floor(v+.5)=={index}" for index in indices) or "0"
            driver(root,path,expression,root,_CHOICE)
    materials = {
        material
        for obj in tuple(branch.children_recursive) if obj.type == "MESH"
        for material in obj.data.materials
        if material and material.use_nodes and (material.get("oxal_mc_studio_potion_color")
            or any(token in str(material.get("oxal_mc_studio_texture_key", ""))
                   for token in ("potion_overlay", "tipped_arrow_head")))
    }
    for material in materials:
        palette=_palette(transaction,root,raw=True) if material.get("oxal_mc_studio_native_particle_palette") else image
        _install(transaction, root, material, palette)
    root.update_tag()


def set_preset(scene, root, key):
    """Explicit scalar edit; no resource or geometry mutation is necessary."""
    ensure_mutation_allowed()
    choices = {row["key"]: index for index, row in enumerate(definitions())}
    if not supports(root.get("oxal_mc_studio_library_identifier", "")) or key not in choices:
        raise ValueError("请选择药水或药水箭及有效预设")
    rig = root if root.type == "ARMATURE" else root.get("oxal_mc_studio_control_rig")
    bone = rig.pose.bones.get("控制 · 药水种类") if rig is not None else None
    from ...util.animation_edit import require_property_edit
    if bone is None:
        require_property_edit(root, '["' + _CHOICE + '"]')
        root[_CHOICE] = choices[key]
    else:
        require_property_edit(rig, bone.path_from_id("location"))
        bone.location.x = 0.35 * choices[key] / max(1, len(choices) - 1)
        cast(Any, rig).update_tag()
    root.update_tag()
    return {"message": "已应用原版药水覆盖颜色"}
