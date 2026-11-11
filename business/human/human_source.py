"""Versioned native templates and explicit skin roles for BaiPo instances.

Template selection does not authorize ownership or migrate an existing model.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import bpy

from ...util.resources import VERSION

CURRENT = "2.4.7"
LEGACY = "2.4.0"
UNIFIED_VERSIONS = frozenset({"2.4.2", "2.4.3", "2.4.4", "2.4.5", "2.4.6", "2.4.7"})
HEAD_HD_MARKER = "oxal_human_head_hd_uv_schema"
DATA = Path(__file__).resolve().parents[2] / "asset/human"
HEAD_SOURCE = "头部 · 八型单网格"
SKIN_ROLE = "oxal_mc_studio_human_skin_role"
SKIN_INPUT = "oxal_mc_studio_human_skin_input"


@dataclass(frozen=True)
class NativeProfile:
    version: str
    collection: str
    body: str
    armor: str
    armor_metadata: str
    layer_metadata: str


_PROFILES = {
    LEGACY: NativeProfile(LEGACY, "baip人模2.4.0", "legacy/oxal_baipo_mc_body.blend", "legacy/oxal_armor_parts.blend", "legacy/ARMOR.json", "legacy/LAYER_RESOLUTION.json"),
    "2.4.2": NativeProfile("2.4.2", "baip人模2.4.2", "legacy/oxal_baipo_mc_body_2_4_2.blend", "oxal_armor_parts_2_4_2.blend", "ARMOR_2_4_2.json", "LAYER_RESOLUTION_UNIFIED.json"),
    "2.4.3": NativeProfile("2.4.3", "baip人模2.4.3", "legacy/oxal_baipo_mc_body_2_4_3.blend", "oxal_armor_parts_2_4_2.blend", "ARMOR_2_4_2.json", "LAYER_RESOLUTION_UNIFIED.json"),
    "2.4.4": NativeProfile("2.4.4", "baip人模2.4.4", "legacy/oxal_baipo_mc_body_2_4_4.blend", "oxal_armor_parts_2_4_2.blend", "ARMOR_2_4_2.json", "LAYER_RESOLUTION_UNIFIED.json"),
    "2.4.5": NativeProfile("2.4.5", "baip人模2.4.5", "legacy/oxal_baipo_mc_body_2_4_5.blend", "oxal_armor_parts_2_4_2.blend", "ARMOR_2_4_2.json", "LAYER_RESOLUTION_UNIFIED.json"),
    "2.4.6": NativeProfile("2.4.6", "baip人模2.4.6", "legacy/oxal_baipo_mc_body_2_4_6.blend", "oxal_armor_parts_2_4_2.blend", "ARMOR_2_4_2.json", "LAYER_RESOLUTION_UNIFIED.json"),
    CURRENT: NativeProfile(CURRENT, "baip人模2.4.7", "oxal_baipo_mc_body_2_4_7.blend", "oxal_armor_parts_2_4_2.blend", "ARMOR_2_4_2.json", "LAYER_RESOLUTION_UNIFIED.json"),
}


def profile(root=None) -> NativeProfile:
    version = CURRENT if root is None else root.get(VERSION)
    if version not in _PROFILES:
        raise ValueError("人模资源版本未知，未使用其它版本模板修改现有实例")
    return _PROFILES[version]


def model_version(root=None):
    return profile(root).version


def root_name(root=None):
    return profile(root).collection


def body_path(root=None):
    return DATA / profile(root).body


def armor_path(root):
    return DATA / profile(root).armor


def armor_declaration(root):
    return DATA / profile(root).armor_metadata


def layer_declaration(root=None):
    return DATA / profile(root).layer_metadata


def is_skin_material(material):
    if material.get(SKIN_ROLE) == "skin":
        return True
    from .human import source_name

    return "白珀人模V2.0" in source_name(material)


def skin_nodes(material: Any) -> tuple[bpy.types.ShaderNodeTexImage, ...]:
    if not material.node_tree:
        return ()
    if material.get(SKIN_ROLE) == "skin":
        marked = tuple(node for node in material.node_tree.nodes if node.get(SKIN_INPUT) == 1)
        samplers = tuple(node for node in material.node_tree.nodes if node.type == "TEX_IMAGE")
        if len(marked) != 1 or marked != samplers:
            raise ValueError("人模统一皮肤采样器的来源角色已改变，未替换皮肤")
        return cast(tuple[bpy.types.ShaderNodeTexImage, ...], marked)
    return tuple(cast(bpy.types.ShaderNodeTexImage, node) for node in material.node_tree.nodes if node.type == "TEX_IMAGE")


def skin_materials(root):
    current = model_version(root) in UNIFIED_VERSIONS
    materials = {slot.material for obj in tuple(root.all_objects) for slot in obj.material_slots
                 if slot.material and is_skin_material(slot.material)}
    if not materials or (current and (len(materials) != 1 or next(iter(materials)).get(SKIN_ROLE) != "skin")):
        raise ValueError("人模统一皮肤材质的来源角色缺失或不唯一")
    return materials


def skin_image(root):
    images = {node.image for material in skin_materials(root) for node in skin_nodes(material)}
    if len(images) != 1 or None in images:
        raise ValueError("人模主皮肤图像缺失或不唯一")
    return next(iter(images))
