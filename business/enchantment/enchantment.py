"""Native glint layers shared by assets and imported armor.

Only explicit tools call this service. It prepares private materials and returns
them to the caller's publication transaction; it never assigns material slots,
discovers scene ownership, registers callbacks, or changes an existing Action.
"""

import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.node_layout import is_layout_frame
from ...util.packed_images import semantic_filepath

PREFIX = "OXAL Glint "
VERSION = 2
DEFAULT_COLOR = (0.72, 0.36, 1.0, 1.0)
DEFAULT_BRIGHTNESS = 0.73
DEFAULT_PIXEL_BRIGHTNESS_MULTIPLIER = 2.0 / DEFAULT_BRIGHTNESS
_SCHEMA = "oxal_mc_studio_glint_schema"
_PROFILE = "oxal_mc_studio_glint_profile"
_OWNER = "oxal_mc_studio_glint_controls"
_SCENE = "oxal_mc_studio_glint_scene"
_CONTRACT = "oxal_mc_studio_glint_contract"
_BASE = "oxal_mc_studio_glint_base"
_BASE_HASH = "oxal_mc_studio_glint_base_hash"
_PRIVATE_HASH = "oxal_mc_studio_glint_private_base_hash"
_SOURCE_NODE = "oxal_mc_studio_glint_source_node"
_SOURCE_SOCKET = "oxal_mc_studio_glint_source_socket"
_GROUPS = "oxal_mc_studio_glint_alpha_groups"
_BASE_REFERENCES = "oxal_mc_studio_glint_base_references"
_PRIVATE_REFERENCES = "oxal_mc_studio_glint_private_references"
_BASE_MATERIAL_HASH = "oxal_mc_studio_glint_base_material_hash"
_PRIVATE_MATERIAL_HASH = "oxal_mc_studio_glint_private_material_hash"
_BASE_REUSE = "oxal_mc_studio_glint_base_reuse"
_PATTERN_SCALE = "oxal_mc_studio_glint_pattern_scale"
SOURCE_UV = "oxal_mc_studio_glint_uv_name"
PIXEL_BRIGHTNESS = "oxal_mc_studio_glint_pixel_brightness"
OUTLINE_SHADER = "oxal_mc_studio_glint_outline_schema"
OUTLINE_ATTRIBUTE = "oxal_enchantment_outline"
OUTLINE_SOLID_ATTRIBUTE = "oxal_enchantment_outline_solid"
FEATURE_DEFAULTS = {
    "glint_pixel_enabled": False,
    "glint_pixel_size": 1,
    "glint_pixel_speed": 1.0,
    "glint_pixel_brightness_multiplier": DEFAULT_PIXEL_BRIGHTNESS_MULTIPLIER,
    "glint_outline_enabled": False,
    "glint_outline_quality_normals": True,
    "glint_outline_width": 0.006,
    "glint_outline_color": (0.72, 0.36, 1.0, 1.0),
    "glint_outline_brightness": 1.0,
    "glint_geometry_enabled": False,
    "glint_geometry_outer": 0.0,
    "glint_geometry_inner": 0.01,
    "glint_geometry_random_enabled": False,
    "glint_geometry_random_factor": 0.5,
    "glint_geometry_height_ratio": 1.25,
    "glint_geometry_seed": 0,
    "glint_geometry_center_falloff": 0.0,
    "glint_geometry_center_range": 1.0,
}
BLOCK_PATTERN_SCALE = 0.0625
ORIGINAL_IMAGE = "oxal_mc_studio_glint_original_image"
_ROOT = Path(__file__).resolve().parents[2] / "asset/library"
_NODE_OPTIONS = (
    "operation", "blend_type", "data_type", "mode", "interpolation",
    "projection", "projection_blend", "extension", "vector_type", "uv_map",
    "attribute_name", "from_instancer", "distribution", "use_clamp",
    "filepath", "script_path", "mute", "use_alpha", "invert",
)
_SOURCE_TYPES = frozenset({
    "BSDF_PRINCIPLED", "BSDF_TRANSPARENT", "BSDF_DIFFUSE", "EMISSION",
    "OUTPUT_MATERIAL", "TEX_IMAGE", "TEX_COORD", "UVMAP", "MAPPING",
    "MATH", "VECTOR_MATH", "MIX_RGB", "MIX", "RGB", "VALUE", "VALTORGB",
    "GROUP", "GROUP_INPUT", "GROUP_OUTPUT", "REROUTE", "MIX_SHADER", "ADD_SHADER",
    "SEPARATE_XYZ", "COMBINE_XYZ", "SEPRGB", "COMBRGB", "SEPARATE_COLOR",
    "COMBINE_COLOR", "NORMAL_MAP", "BUMP", "GAMMA", "HUE_SAT", "INVERT",
    "LIGHT_PATH", "MAP_RANGE", "RGBTOBW", "VECT_TRANSFORM", "ATTRIBUTE", "FRAME",
})
_UI_NODE_FIELDS = {
    "name", "name_full", "label", "location", "location_absolute", "dimensions",
    "width", "width_hidden", "height", "color", "select", "hide", "show_options",
    "show_preview", "show_texture", "use_custom_color", "warning_propagation",
}


def _track(transaction, item):
    transaction.track(item)
    return item


def control_defaults(owner) -> tuple[str, ...]:
    """Add controls only; callers journal and restore these keys on failure."""
    ensure_mutation_allowed()
    defaults = {
        "glint_enabled": True,
        "glint_color": DEFAULT_COLOR,
        "glint_brightness": DEFAULT_BRIGHTNESS,
        "glint_gradient": 0.0,
        **FEATURE_DEFAULTS,
    }
    added = []
    for key, value in defaults.items():
        if key not in owner:
            owner[key] = value
            added.append(key)
            if key in {"glint_color", "glint_outline_color"}:
                owner.id_properties_ui(key).update(subtype="COLOR", min=0, max=1, default=value)
            elif key == "glint_brightness":
                owner.id_properties_ui(key).update(min=0, max=10, soft_max=10, default=value)
            elif key == "glint_gradient":
                owner.id_properties_ui(key).update(min=0, max=1)
            elif key == "glint_pixel_size":
                owner.id_properties_ui(key).update(min=1, max=16, default=1)
            elif key == "glint_pixel_speed":
                owner.id_properties_ui(key).update(min=0, max=10, default=1.0)
            elif key == "glint_pixel_brightness_multiplier":
                owner.id_properties_ui(key).update(min=0, max=100, soft_max=10, default=value)
            elif key == "glint_outline_width":
                owner.id_properties_ui(key).update(min=0, max=0.25, soft_max=0.1, default=value)
            elif key == "glint_outline_brightness":
                owner.id_properties_ui(key).update(min=0, soft_max=4)
            elif key in {"glint_geometry_outer", "glint_geometry_inner"}:
                owner.id_properties_ui(key).update(min=0, max=1, soft_max=0.25)
            elif key in {"glint_geometry_random_factor", "glint_geometry_center_falloff"}:
                owner.id_properties_ui(key).update(min=0, max=1)
            elif key == "glint_geometry_height_ratio":
                owner.id_properties_ui(key).update(min=0, max=5)
            elif key == "glint_geometry_seed":
                owner.id_properties_ui(key).update(min=0, max=100000)
            elif key == "glint_geometry_center_range":
                owner.id_properties_ui(key).update(min=0, max=5)
    if added:
        owner.update_tag()
    return tuple(added)


def _curves(tree):
    animation = tree.animation_data
    if animation is None:
        return ()
    result = list(action_fcurves(animation.action, slot=getattr(animation, "action_slot", None)))
    strips = [strip for track in animation.nla_tracks for strip in track.strips]
    while strips:
        strip = strips.pop()
        result.extend(action_fcurves(strip.action, slot=getattr(strip, "action_slot", None)))
        strips.extend(getattr(strip, "strips", ()))
    return tuple(result)


def _plain(value):
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if isinstance(value, float):
        return round(value, 9) if math.isfinite(value) else str(value)
    try:
        return [_plain(part) for part in value]
    except TypeError:
        return str(value)


def _node_parameters(node, animated):
    result = []
    for prop in node.bl_rna.properties:
        key = prop.identifier
        if (key in _UI_NODE_FIELDS or key.startswith("bl_") or prop.is_readonly
                or prop.type not in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}):
            continue
        if node.path_from_id(key) not in animated:
            result.append((key, _plain(getattr(node, key))))
    return result


def _rna_values(owner, *, excluded=()):
    return [(prop.identifier, _plain(getattr(owner, prop.identifier)))
            for prop in owner.bl_rna.properties
            if not prop.is_readonly and prop.identifier not in excluded
            and prop.type in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}]


def _legacy_light_path_output(node, socket, index, animated, *, strict=True):
    """Recognize only the tested 4.5 -> 5.x native Light Path addition.

    Portal Depth did not exist in 4.5's saved digest. It may be omitted from
    that historical candidate only with its exact native default, no links,
    and no Action/NLA/driver channel. All original fields stay in the digest.
    """
    if node.bl_idname != "ShaderNodeLightPath" or index != 14:
        return False
    expected = (
        "Is Camera Ray", "Is Shadow Ray", "Is Diffuse Ray", "Is Glossy Ray",
        "Is Singular Ray", "Is Reflection Ray", "Is Transmission Ray", "Is Volume Scatter Ray",
        "Ray Length", "Ray Depth", "Diffuse Depth", "Glossy Depth", "Transparent Depth", "Transmission Depth",
        "Portal Depth",
    )
    if (bpy.app.version[:2] not in {(5, 0), (5, 1), (5, 2)}
            or tuple(part.identifier for part in node.outputs) != expected
            or socket.is_linked or socket.default_value != 0.0
            or socket.path_from_id("default_value") in animated):
        if strict:
            raise ValueError("Light Path 新增输出含用户改动或宿主接口未知，未忽略其合同字段")
        return False
    return True


def _tree_record(tree, *, generated=False, seen=None, legacy_light_path=False, legacy_animation=False):
    seen = set() if seen is None else seen
    if tree.as_pointer() in seen:
        raise ValueError("材质节点组递归，未改写附魔")
    seen = {*seen, tree.as_pointer()}
    animated = {curve.data_path for curve in _curves(tree)}
    if tree.animation_data:
        animated.update(curve.data_path for curve in tree.animation_data.drivers)
    selected = {
        node for node in tree.nodes
        if not is_layout_frame(node) and node.name.startswith(PREFIX) == generated
    }
    records = []
    for node in sorted(selected, key=lambda item: item.name):
        values = []
        for index, socket in enumerate(node.inputs):
            if not hasattr(socket, "default_value"):
                continue
            path = socket.path_from_id("default_value")
            if path not in animated:
                values.append((index, _plain(socket.default_value)))
        record = [node.name, node.bl_idname, values,
                  [(key, _plain(getattr(node, key))) for key in _NODE_OPTIONS if hasattr(node, key)]]
        record.append(_node_parameters(node, animated))
        for key in ("image_user", "color_mapping", "texture_mapping"):
            nested = getattr(node, key, None)
            if nested is not None:
                record.append((key, [(prop, value) for prop, value in _rna_values(nested)
                                     if node.path_from_id() + "." + key + "." + prop not in animated]))
        record.append([(index, _plain(socket.default_value))
                       for index, socket in enumerate(node.outputs)
                       if hasattr(socket, "default_value")
                       and not (legacy_light_path and _legacy_light_path_output(node, socket, index, animated))
                       and socket.path_from_id("default_value") not in animated])
        ramp = getattr(node, "color_ramp", None)
        if ramp is not None:
            record.append((ramp.color_mode, ramp.hue_interpolation, ramp.interpolation,
                           [(None if element.path_from_id("position") in animated else element.position,
                             None if element.path_from_id("color") in animated else tuple(element.color))
                            for element in ramp.elements]))
        if not generated and getattr(node, "image", None) is not None:
            image = node.image
            record.append((semantic_filepath(image), tuple(image.size), image.colorspace_settings.name))
        group = getattr(node, "node_tree", None)
        if group is not None:
            record.append(_tree_record(group, generated=generated, seen=seen, legacy_light_path=legacy_light_path,
                                       legacy_animation=legacy_animation))
        records.append(record)
    links = sorted(
        (link.from_node.name, list(link.from_node.outputs).index(link.from_socket),
         link.to_node.name, list(link.to_node.inputs).index(link.to_socket))
        for link in tree.links
        if ((link.from_node in selected or link.to_node in selected) if generated
            else (link.from_node in selected and link.to_node in selected))
    )
    drivers = []
    if tree.animation_data:
        for curve in tree.animation_data.drivers:
            belongs = curve.data_path.startswith('nodes["' + PREFIX)
            if belongs != generated:
                continue
            drivers.append((curve.data_path, curve.array_index, curve.driver.type,
                            curve.driver.expression, curve.driver.use_self, curve.mute,
                            curve.extrapolation,
                            [_rna_values(point) for point in curve.keyframe_points],
                            [tuple(point.co) for point in curve.sampled_points],
                            [(modifier.type, _rna_values(modifier)) for modifier in curve.modifiers],
                            [(variable.name, variable.type,
                              [_rna_values(target) for target in variable.targets])
                             for variable in curve.driver.variables]))
    animation_settings = []
    animation = tree.animation_data
    if animation and (animation.action is not None or animation.nla_tracks) and (not generated or legacy_animation):
        animation_settings.append((getattr(getattr(animation, "action_slot", None), "identifier", None),
                                   animation.action_blend_type, animation.action_extrapolation,
                                   animation.action_influence, animation.use_nla))
        animation_settings.extend(("track", index, track.mute, track.is_solo)
                                  for index, track in enumerate(animation.nla_tracks))
        strips = [(f"{ti}/{si}", strip) for ti, track in enumerate(animation.nla_tracks) for si, strip in enumerate(track.strips)]
        while strips:
            role, strip = strips.pop()
            animation_settings.append((role, strip.type,
                                       [(key, _plain(getattr(strip, key))) for key in (
                                           "frame_start", "frame_end", "action_frame_start", "action_frame_end",
                                           "scale", "repeat", "blend_type", "extrapolation", "mute") if hasattr(strip, key)],
                                       getattr(getattr(strip, "action_slot", None), "identifier", None)))
            strips.extend((role + "/" + str(index), child) for index, child in enumerate(getattr(strip, "strips", ())))
    return [records, links, sorted(drivers), animation_settings]


def _digest(tree, *, generated=False):
    return hashlib.sha256(json.dumps(_tree_record(tree, generated=generated),
                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _material_digest(material, *, legacy45=False):
    def value(item):
        if isinstance(item, bpy.types.ID):
            # Arbitrary user ID properties need exact-reference handling before
            # sharing the old base. Keep the private copy in that case.
            raise TypeError("用户材质包含独立数据引用")
        if hasattr(item, "items"):
            return {key: value(part) for key, part in item.items()}
        if isinstance(item, (str, int, float, bool)) or item is None:
            return _plain(item)
        return [value(part) for part in item]
    try:
        custom = {key: value(part) for key, part in material.items()
                  if not key.startswith("oxal_mc_studio_")}
    except TypeError:
        return ""
    if material.animation_data is not None:
        return ""
    values = _rna_values(material, excluded={"use_fake_user", "tag"})
    if legacy45 and bpy.app.version[:2] in {(5, 0), (5, 1), (5, 2)} and material.use_backface_culling:
        # Native 4.5 files load this culling flag as False on 5.x. Preserve
        # every field and compare the exact historical True-valued candidate.
        values = [(key, True if key == "use_backface_culling_lightprobe_volume" and value is False else value)
                  for key, value in values]
    return hashlib.sha256(json.dumps((values, custom), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def material_contract_matches(material, expected):
    return expected == _material_digest(material) or expected == _material_digest(material, legacy45=True)


def _generated_digest(material, *, legacy_light_path=False, legacy_animation=False):
    from . import enchantment_pixels as pixels
    from . import enchantment_time as native_time
    groups = material.get(_GROUPS)
    records = [_tree_record(material.node_tree, generated=True, legacy_light_path=legacy_light_path,
                            legacy_animation=legacy_animation)]
    if groups:
        records.extend(_tree_record(entry["private"], generated=True, legacy_light_path=legacy_light_path,
                                    legacy_animation=legacy_animation)
                       for entry in groups.values())
    if pixels._revision(material):
        # Revision-specific source selectors are generated semantics too.
        # Legacy VERSION=2 materials retain their original digest bytes.
        records.append([pixels.REVISION, material.get(pixels.ROWS)])
    fallback = pixels.fallback_metadata(material)
    if fallback is not None:
        records.append([pixels.FALLBACK, fallback])
    preparation = pixels.armor_preparation(material)
    if preparation is not None:
        records.append([pixels.ARMOR_PREPARATION, preparation])
    if PIXEL_BRIGHTNESS in material:
        records.append([PIXEL_BRIGHTNESS, material[PIXEL_BRIGHTNESS]])
    timing = native_time.metadata(material)
    if timing is not None:
        records.append([native_time.MARKER, timing])
    return hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _generated_contract_matches(material):
    expected = material.get(_CONTRACT)
    if expected == _generated_digest(material) or expected == _generated_digest(material, legacy_animation=True):
        return True
    if bpy.app.version[:2] not in {(5, 0), (5, 1), (5, 2)}:
        return False
    # Verify the saved historical bytes against the current graph projected
    # through one proven native addition; never bless the current graph anew.
    return (expected == _generated_digest(material, legacy_light_path=True)
            or expected == _generated_digest(material, legacy_light_path=True, legacy_animation=True))


def _references(tree, *, prefix="root", seen=None):
    """Exact IDs complement the scalar graph digest, including identical images."""
    seen = set() if seen is None else seen
    if tree.as_pointer() in seen:
        raise ValueError("材质节点组递归，未恢复旧材质")
    seen = {*seen, tree.as_pointer()}
    result = {}
    for node in tree.nodes:
        if node.name.startswith(PREFIX) or is_layout_frame(node):
            continue
        key = prefix + "/" + node.name
        for name in ("image", "node_tree", "object", "texture"):
            value = getattr(node, name, None)
            if isinstance(value, bpy.types.ID):
                result[key + "/" + name] = value
        group = getattr(node, "node_tree", None)
        if group is not None:
            result.update(_references(group, prefix=key, seen=seen))
    animation = tree.animation_data
    if animation:
        if animation.action is not None:
            result[prefix + "/action"] = animation.action
        strips = [(f"{ti}/{si}", strip) for ti, track in enumerate(animation.nla_tracks) for si, strip in enumerate(track.strips)]
        while strips:
            role, strip = strips.pop()
            if strip.action is not None:
                result[prefix + "/nla/" + role] = strip.action
            strips.extend((role + "/" + str(index), child) for index, child in enumerate(getattr(strip, "strips", ())))
        for index, curve in enumerate(animation.drivers):
            if curve.data_path.startswith('nodes["' + PREFIX):
                continue
            for vi, variable in enumerate(curve.driver.variables):
                for ti, target in enumerate(variable.targets):
                    if target.id is not None:
                        result[f"{prefix}/driver/{index}/{vi}/{ti}"] = target.id
    return result


def _saved_references(values):
    # Native ID-property keys have a small UTF-8 limit; node paths may contain
    # several full-length Chinese names. Keep paths in values, not table keys.
    return {str(index): {"role": role, "target": item}
            for index, (role, item) in enumerate(sorted(values.items()))}


def _same_references(saved, current):
    if saved is None:
        return False
    try:
        expected = {row["role"]: row["target"] for row in saved.values()}
    except (TypeError, KeyError):
        return False
    return set(expected) == set(current) and all(expected[key] == item for key, item in current.items())


def _complete_base_contract(tree, seen=None):
    """Unknown structured node parameters keep the private unwrapped material."""
    seen = set() if seen is None else seen
    if tree.as_pointer() in seen:
        return False
    seen = {*seen, tree.as_pointer()}
    for node in tree.nodes:
        if node.name.startswith(PREFIX) or is_layout_frame(node):
            continue
        if node.type not in _SOURCE_TYPES:
            return False
        color_mapping = getattr(node, "color_mapping", None)
        if color_mapping is not None and color_mapping.use_color_ramp:
            return False
        group = getattr(node, "node_tree", None)
        if group is not None and not _complete_base_contract(group, seen):
            return False
    return True


def _surface(material):
    if not material.use_nodes or material.node_tree is None:
        raise ValueError("附魔目标需要节点材质")
    outputs = [node for node in material.node_tree.nodes
               if node.type == "OUTPUT_MATERIAL" and node.is_active_output]
    if len(outputs) != 1 or len(outputs[0].inputs["Surface"].links) != 1:
        raise ValueError("材质没有唯一已连接的表面输出")
    return outputs[0].inputs["Surface"]


def inspect(material) -> dict[str, Any] | None:
    """Inspect generated structure; this does not grant ownership of a material."""
    if material.node_tree is None or material.node_tree.nodes.get(PREFIX + "Mix") is None:
        return None
    tree = material.node_tree
    mix = tree.nodes[PREFIX + "Mix"]
    surface = _surface(material)
    add = tree.nodes.get(PREFIX + "Add")
    if (surface.links[0].from_socket != mix.outputs[0] or not mix.inputs[1].is_linked
            or add is None or not add.inputs[0].is_linked
            or add.inputs[0].links[0].from_socket != mix.inputs[1].links[0].from_socket):
        raise ValueError("附魔表面或底材连接已变化，保留用户连接，未覆盖")
    if material.get(_SCHEMA) == VERSION:
        from . import enchantment_time as native_time
        from .enchantment_pixels import _revision
        _revision(material)
        trees = [material.node_tree]
        groups = material.get(_GROUPS)
        if groups:
            trees.extend(entry["private"] for entry in groups.values())
        if any(curve.data_path.startswith('nodes["' + PREFIX) for tree in trees for curve in _curves(tree)):
            raise ValueError("附魔生成节点含用户关键帧，请改用实例附魔参数；未删除其动画")
        if PIXEL_BRIGHTNESS in material and (type(material[PIXEL_BRIGHTNESS]) is not int or material[PIXEL_BRIGHTNESS] != 1):
            raise ValueError("未知像素流光亮度节点版本，未覆盖")
        if not _generated_contract_matches(material):
            raise ValueError("附魔生成节点已被修改，保留原材质，未覆盖")
        if native_time.metadata(material) is not None:
            if _SCENE in material:
                raise ValueError("附魔原生时间含未知场景依赖，未覆盖")
            native_time.require_material(material)
        for tree in trees:
            animation = tree.animation_data
            for curve in animation.drivers if animation else ():
                if not curve.data_path.startswith('nodes["' + PREFIX):
                    continue
                for variable in curve.driver.variables:
                    expected = (tree if variable.name == "alpha" else material.get(_OWNER)
                                if variable.name == "value" else material.get(_SCENE))
                    if any(target.id != expected for target in variable.targets):
                        raise ValueError("附魔驱动目标已变化，未覆盖")
        return {"version": VERSION, "profile": material.get(_PROFILE), "owner": material.get(_OWNER)}
    # The first Beta's exact generated graph is a separately recognized input.
    expected = {
        "Texture": "TEX_IMAGE", "UV": "TEX_COORD", "Mapping": "MAPPING",
        "Color": "MIX_RGB", "Gradient": "MIX_RGB", "Emission": "EMISSION",
        "Add": "ADD_SHADER", "Mix": "MIX_SHADER",
    }
    generated = {node.name[len(PREFIX):]: node.type for node in material.node_tree.nodes
                 if node.name.startswith(PREFIX)}
    if generated != expected:
        raise ValueError("无法确认旧 Beta 附魔结构，未覆盖")
    tree = material.node_tree
    mapping = tree.nodes[PREFIX + "Mapping"]
    if (tuple(round(value, 5) for value in mapping.inputs["Scale"].default_value) != (3, 3, 3)
            or abs(mapping.inputs["Rotation"].default_value.z - 0.45) > 1e-5):
        raise ValueError("旧 Beta 附魔映射含用户修改，未覆盖")
    if tree.animation_data:
        expected_expressions = {"frame*.007", "frame*.004", "v"}
        for curve in tree.animation_data.drivers:
            if curve.data_path.startswith('nodes["' + PREFIX) and curve.driver.expression not in expected_expressions:
                raise ValueError("旧 Beta 附魔驱动含用户修改，未覆盖")
    if any(curve.data_path.startswith('nodes["' + PREFIX) for curve in _curves(tree)):
        raise ValueError("旧 Beta 附魔节点有用户关键帧，未覆盖")
    return {"version": 1, "profile": "ITEM", "owner": None}


def inspect_source(transaction, material):
    """Validate an immutable source ID once in this explicit transaction.

    Newly created/private IDs are mutable and always inspected afresh. The
    table lives on the caller's Creation only; subsequent Operators, Undo and
    file loads cannot inherit it. In-place service edits invalidate the entry
    before their first mutation.
    """
    candidates = getattr(transaction, "ids", None)
    if candidates is None or material in candidates:
        return inspect(material)
    table = getattr(transaction, "glint_source_inspections", None)
    if table is None:
        table = transaction.glint_source_inspections = {}
    if material not in table:
        table[material] = inspect(material)
    return table[material]


def invalidate_source(transaction, material):
    table = getattr(transaction, "glint_source_inspections", None)
    if table is not None:
        table.pop(material, None)


def stamp(material) -> None:
    """Seal only a newly prepared, explicitly managed generated layer."""
    ensure_mutation_allowed()
    material[_CONTRACT] = _generated_digest(material, legacy_light_path=True)


def _copy_material(transaction, original):
    result = _track(transaction, original.copy())
    # Actions are immutable to this service and retain their exact bindings.
    # Only self-targeting drivers need rebinding to the private copy.
    for owner in (result, result.node_tree):
        animation = owner.animation_data
        if animation:
            for curve in animation.drivers:
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if target.id == original:
                            target.id = result
                        elif target.id == original.node_tree:
                            target.id = result.node_tree
    return result


class _Graph:
    def __init__(self, tree):
        self.tree = tree

    def node(self, kind, role):
        result = self.tree.nodes.new(kind)
        result.name = PREFIX + role
        result.label = role
        return result

    def feed(self, socket, value):
        if isinstance(value, (int, float)):
            socket.default_value = value
        else:
            self.tree.links.new(value, socket)

    def math(self, role, operation, *values):
        node = self.node("ShaderNodeMath", role)
        node.operation = operation
        for socket, value in zip(node.inputs, values):
            self.feed(socket, value)
        return node.outputs[0]

    def vector(self, role, operation, *values):
        node = self.node("ShaderNodeVectorMath", role)
        node.operation = operation
        for index, value in enumerate(values):
            socket = node.inputs["Scale"] if operation == "SCALE" and index == 1 else node.inputs[index]
            if isinstance(value, tuple):
                socket.default_value = value
            else:
                self.feed(socket, value)
        return node.outputs[0]


def _input(socket, graph):
    if socket.is_linked:
        return socket.links[0].from_socket
    tree = socket.id_data
    path = socket.path_from_id("default_value")
    animated = {curve.data_path for curve in _curves(tree)}
    if tree.animation_data:
        animated.update(curve.data_path for curve in tree.animation_data.drivers)
    if path not in animated:
        return socket.default_value
    # A driver reading another property of its own shader tree creates an
    # evaluated-tree dependency cycle. Do not manufacture such a "bridge".
    # A linked Value/texture/node output is already a safe native graph input.
    raise ValueError("透明度输入含直接关键帧或驱动，请先使用原生值节点连接透明度；现有材质和动画未修改")


def _alpha(transaction, graph, shader, material, copies, counter):
    """Expose opacity through private shader groups without changing shaders."""
    node = shader.node
    role = "Alpha " + str(counter[0])
    counter[0] += 1
    if node.type == "BSDF_TRANSPARENT" or node.type == "HOLDOUT":
        return 0.0
    if node.type == "BSDF_PRINCIPLED":
        return _input(node.inputs["Alpha"], graph)
    if node.type == "REROUTE":
        if not node.inputs[0].is_linked:
            return 0.0
        return _alpha(transaction, graph, node.inputs[0].links[0].from_socket, material, copies, counter)
    if node.type in {"MIX_SHADER", "ADD_SHADER"}:
        start = 1 if node.type == "MIX_SHADER" else 0
        children = [(_alpha(transaction, graph, socket.links[0].from_socket, material, copies, counter)
                     if socket.is_linked else 0.0) for socket in node.inputs[start:start + 2]]
        if node.type == "ADD_SHADER":
            return graph.math(role, "MAXIMUM", *children)
        factor = _input(node.inputs[0], graph)
        a = graph.math(role + " Left", "MULTIPLY", children[0], graph.math(role + " Inverse", "SUBTRACT", 1, factor))
        b = graph.math(role + " Right", "MULTIPLY", children[1], factor)
        return graph.math(role, "ADD", a, b)
    if node.type != "GROUP":
        return 1.0
    original = node.node_tree
    if original is None:
        raise ValueError("材质缺少着色节点组")
    output_index = list(node.outputs).index(shader)
    private = copies.get(original)
    if private is None:
        private = _track(transaction, original.copy())
        copies[original] = private
        groups = dict(material.get(_GROUPS, {}).items()) if material.get(_GROUPS) else {}
        groups[str(len(groups))] = {"original": original, "private": private}
        material[_GROUPS] = groups
        if private.animation_data:
            for curve in private.animation_data.drivers:
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if target.id == original:
                            target.id = private
    node.node_tree = private
    output = next((item for item in private.nodes if item.type == "GROUP_OUTPUT" and item.is_active_output), None)
    if output is None or not output.inputs[output_index].is_linked:
        raise ValueError("材质着色节点组缺少有效输出")
    name = PREFIX + "Opacity " + str(output_index)
    if not any(item.item_type == "SOCKET" and item.in_out == "OUTPUT" and item.name == name for item in private.interface.items_tree):
        opacity = _alpha(transaction, _Graph(private), output.inputs[output_index].links[0].from_socket,
                         material, copies, counter)
        private.interface.new_socket(name=name, in_out="OUTPUT", socket_type="NodeSocketFloat")
        if isinstance(opacity, (int, float)):
            output.inputs[name].default_value = opacity
        else:
            private.links.new(opacity, output.inputs[name])
    return node.outputs[name]


@lru_cache(maxsize=1)
def _atlas():
    path = _ROOT / "glint_atlas179.json"
    return json.loads(path.read_text("utf-8"))["sprites"] if path.is_file() else {}


def _region(material, supplied):
    if supplied is not None:
        values = tuple(supplied)
    else:
        key = str(material.get("oxal_mc_studio_texture_key", "")).split(":", 1)[-1]
        values = tuple(_atlas().get(key, (0, 0, 1, 1)))
    if len(values) != 4 or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
        raise ValueError("附魔图集坐标格式错误")
    return tuple(float(value) for value in values)


def _driver(socket, owner, property_name, *, index=None, scene=None, expression="value"):
    curve = socket.driver_add("default_value") if index is None else socket.driver_add("default_value", index)
    curve.driver.type = "SCRIPTED"
    curve.driver.expression = expression
    variables = [("value", owner, '["' + property_name + '"]')] if owner is not None else []
    if scene is not None:
        variables.extend((name, scene, path) for name, path in (
            ("fps", "render.fps"), ("base", "render.fps_base"), ("first", "frame_start")))
    for name, target, path in variables:
        variable = curve.driver.variables.new()
        variable.name = name
        variable.type = "SINGLE_PROP"
        variable.targets[0].id_type = target.id_type
        variable.targets[0].id = target
        variable.targets[0].data_path = path
    return curve


def _outline_surface(graph, owner, source, opacity):
    """One shared alpha chain and face flag, without extra texture samples."""
    attribute = graph.node("ShaderNodeAttribute", "Outline Face")
    attribute.attribute_name = OUTLINE_ATTRIBUTE
    shell = attribute.outputs["Fac"]
    solid = graph.node("ShaderNodeAttribute", "Outline Solid Band")
    solid.attribute_name = OUTLINE_SOLID_ATTRIBUTE
    geometry = graph.node("ShaderNodeNewGeometry", "Outline Geometry")
    light = graph.node("ShaderNodeLightPath", "Outline Ray")
    front = graph.math("Outline Front", "SUBTRACT", 1, geometry.outputs["Backfacing"])
    lit = graph.math("Outline No Shadow", "SUBTRACT", 1, light.outputs["Is Shadow Ray"])
    enabled = graph.node("ShaderNodeValue", "Outline Enabled")
    _driver(enabled.outputs[0], owner, "glint_outline_enabled", expression="min(max(value,0),1)")
    active = graph.node("ShaderNodeValue", "Outline Glint Enabled")
    _driver(active.outputs[0], owner, "glint_enabled", expression="min(max(value,0),1)")
    mask = graph.math("Outline Alpha", "MULTIPLY", graph.math("Outline Band Opacity", "MAXIMUM", opacity, solid.outputs["Fac"]),
                      graph.math("Outline Visible", "MULTIPLY", front, lit))
    mask = graph.math("Outline Toggle", "MULTIPLY", mask,
                      graph.math("Outline Active", "MULTIPLY", enabled.outputs[0], active.outputs[0]))
    tint = graph.node("ShaderNodeRGB", "Outline Color")
    for axis in range(4):
        curve = _driver(tint.outputs[0], owner, "glint_outline_color", index=axis)
        curve.driver.variables[0].targets[0].data_path += "[" + str(axis) + "]"
    emission = graph.node("ShaderNodeEmission", "Outline Emission")
    graph.tree.links.new(tint.outputs[0], emission.inputs["Color"])
    _driver(emission.inputs["Strength"], owner, "glint_outline_brightness", expression="max(value,0)")
    transparent = graph.node("ShaderNodeBsdfTransparent", "Outline Transparent")
    alpha = graph.node("ShaderNodeMixShader", "Outline Opacity")
    graph.tree.links.new(mask, alpha.inputs[0])
    graph.tree.links.new(transparent.outputs[0], alpha.inputs[1])
    graph.tree.links.new(emission.outputs[0], alpha.inputs[2])
    result = graph.node("ShaderNodeMixShader", "Outline Surface")
    graph.tree.links.new(shell, result.inputs[0])
    graph.tree.links.new(source, result.inputs[1])
    graph.tree.links.new(alpha.outputs[0], result.inputs[2])
    return result.outputs[0], shell


def _unlink_layer(material):
    tree = material.node_tree
    mix = tree.nodes.get(PREFIX + "Mix")
    if mix is None or not mix.inputs[1].is_linked:
        raise ValueError("附魔底材引用已损坏，未移除")
    original = mix.inputs[1].links[0].from_socket
    if material.get(OUTLINE_SHADER) in {1, 2, 3}:
        outline = tree.nodes.get(PREFIX + "Outline Surface")
        if (outline is None or original != outline.outputs[0] or not outline.inputs[1].is_linked):
            raise ValueError("附魔描边底材连接已改变，未覆盖")
        original = outline.inputs[1].links[0].from_socket
        if (original.node.name != material.get(_SOURCE_NODE)
                or list(original.node.outputs).index(original) != material.get(_SOURCE_SOCKET)):
            raise ValueError("附魔描边原底材引用已改变，未覆盖")
    surface = _surface(material)
    tree.links.new(original, surface)
    if any(curve.data_path.startswith('nodes["' + PREFIX) for curve in _curves(tree)):
        raise ValueError("附魔生成节点存在用户动画，保留原状")
    if tree.animation_data:
        for curve in tuple(tree.animation_data.drivers):
            if curve.data_path.startswith('nodes["' + PREFIX):
                tree.animation_data.drivers.remove(curve)
    # The private copy may later build a different native revision with the
    # same generated node names. Retire only those deleted nodes' visual rows;
    # retaining them would compare the new node type to a dead predecessor.
    from ...util.node_layout import META as LAYOUT_META

    layout = tree.get(LAYOUT_META)
    visual_rows = layout.get("nodes") if layout is not None else None
    for node in tuple(tree.nodes):
        if node.name.startswith(PREFIX):
            if visual_rows is not None and node.name in visual_rows:
                del visual_rows[node.name]
            tree.nodes.remove(node)
    from .enchantment_pixels import ARMOR_PREPARATION

    if ARMOR_PREPARATION in material:
        del material[ARMOR_PREPARATION]
    if PIXEL_BRIGHTNESS in material:
        del material[PIXEL_BRIGHTNESS]
    from .enchantment_time import clear

    clear(material)


def _detach_generated_groups(transaction, material):
    """Strip generated group outputs on private copies, never shared old IDs."""
    groups = material.get(_GROUPS)
    if not groups:
        return
    copies = {entry["private"]: _track(transaction, entry["private"].copy()) for entry in groups.values()}
    for tree in (material.node_tree, *copies.values()):
        for node in tree.nodes:
            child = getattr(node, "node_tree", None)
            if child in copies:
                node.node_tree = copies[child]
        if tree.animation_data:
            for curve in tree.animation_data.drivers:
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if target.id in copies:
                            target.id = copies[target.id]
    for tree in copies.values():
        if any(curve.data_path.startswith('nodes["' + PREFIX) for curve in _curves(tree)):
            raise ValueError("附魔生成节点组存在用户动画，保留原状")
        if tree.animation_data:
            for curve in tuple(tree.animation_data.drivers):
                if curve.data_path.startswith('nodes["' + PREFIX):
                    tree.animation_data.drivers.remove(curve)
        for node in tuple(tree.nodes):
            if node.name.startswith(PREFIX):
                tree.nodes.remove(node)
        for item in tuple(tree.interface.items_tree):
            if item.item_type == "SOCKET" and item.name.startswith(PREFIX):
                tree.interface.remove(item)
    material[_GROUPS] = {}


def apply(transaction, owner, material, *, geometry_kind="ITEM", scene=None,
          texture_path=None, original_path=None, atlas_region=None, copy=True, prepare_pixels=False,
          uv_name=None):
    """Prepare one private, native layer; caller owns all publication/rollback."""
    ensure_mutation_allowed()
    if geometry_kind not in {"ITEM", "BLOCK", "ARMOR", "GENERIC"}:
        raise ValueError("未知附魔表面类型")
    if not isinstance(prepare_pixels, bool):
        raise TypeError("像素流光准备开关必须为布尔值")
    if uv_name is not None and (not isinstance(uv_name, str) or not uv_name):
        raise ValueError("物品附魔UV身份必须是非空名称")
    scene = scene or bpy.context.scene
    if scene is None:
        raise ValueError("附魔动画需要明确的场景")
    missing = {"glint_enabled", "glint_color", "glint_brightness", "glint_gradient", *FEATURE_DEFAULTS} - set(owner.keys())
    if missing:
        raise ValueError("请先在事务中初始化附魔控制参数")
    # Existing imported armatures may already have an evaluated copy. Adding
    # ID properties alone does not invalidate that copy in all host versions.
    owner.update_tag()
    current = inspect_source(transaction, material)
    from . import enchantment_pixels as pixels
    from . import enchantment_time as native_time

    timing_current = current and native_time.metadata(material) is not None
    if timing_current:
        native_time.require_material(material, scene=scene)
    want_pixels = geometry_kind != "ARMOR" or prepare_pixels or pixels.requested(material, owner)
    preparation_current = (geometry_kind != "ARMOR" or pixels.armor_preparation(material) == int(want_pixels))
    if (current and current["version"] == VERSION and current["profile"] == geometry_kind
            and current["owner"] == owner
            and material.get(OUTLINE_SHADER) == 4
            and (str(material.get(SOURCE_UV, "")) == (uv_name or "") if geometry_kind == "GENERIC"
                 else uv_name is None or material.get(SOURCE_UV) == uv_name)
            and material.get(_CONTRACT) == _generated_digest(material, legacy_light_path=True)
            and timing_current
            and preparation_current and (pixels.prepared(material) if want_pixels else not pixels._revision(material))
            and (not want_pixels or material.get(PIXEL_BRIGHTNESS) == 1)
            and (geometry_kind != "BLOCK" or material.get(_PATTERN_SCALE) == BLOCK_PATTERN_SCALE)):
        return material
    _surface(material)
    clock = native_time.prepare(transaction, scene)
    if not copy:
        invalidate_source(transaction, material)
    result = _copy_material(transaction, material) if copy else material
    reuse_base = material.get(_BASE_REUSE, True)
    if current:
        _unlink_layer(result)
        # Preserve the *old* seal's editing fact before establishing a new
        # generated revision. Otherwise upgrade -> remove can discard edits.
        reuse_base = (reuse_base and material.get(_PRIVATE_HASH) == _digest(result.node_tree)
                      and material.get(_PRIVATE_MATERIAL_HASH) == _material_digest(material)
                      and _same_references(material.get(_PRIVATE_REFERENCES), _references(result.node_tree)))
        _detach_generated_groups(transaction, result)
    source_signature = _digest(result.node_tree)
    source_references = _references(result.node_tree)
    source = _surface(result).links[0].from_socket
    result[_SOURCE_NODE] = source.node.name
    result[_SOURCE_SOCKET] = list(source.node.outputs).index(source)
    base_material = material.get(_BASE, material) if current else material
    if base_material != result:
        result[_BASE] = base_material
    elif _BASE in result:
        del result[_BASE]
    result[_BASE_HASH] = material.get(_BASE_HASH, source_signature) if current else source_signature
    result[_BASE_REFERENCES] = material.get(_BASE_REFERENCES, _saved_references(source_references)) if current else _saved_references(source_references)
    result[_GROUPS] = {}
    result[_BASE_REUSE] = bool(reuse_base)
    for key in (pixels.MARKER, pixels.ROWS, pixels.FALLBACK, pixels.FALLBACK_ROWS, PIXEL_BRIGHTNESS):
        if key in result:
            del result[key]
    graph = _Graph(result.node_tree)
    if uv_name is not None:
        result[SOURCE_UV] = uv_name
    elif geometry_kind == "GENERIC" and SOURCE_UV in result:
        del result[SOURCE_UV]
    authored_uv = str(result.get(SOURCE_UV, "")) if geometry_kind in {"ITEM", "GENERIC"} else ""
    source_texture = result.node_tree.nodes.get("OXAL Source Texture")
    if geometry_kind == "ITEM" and authored_uv and source_texture is not None and source_texture.type == "TEX_IMAGE" and not source_texture.inputs["Vector"].is_linked:
        # Joining native geometry can reorder UV layers and change the mesh's
        # implicit render UV. Generated sprites have a separate atlas UV for
        # glint; keep the base texture on its original, explicit source map.
        # This generated link disappears with the enchantment layer, restoring
        # the original unlinked input without editing the user's base material.
        source_uv = graph.node("ShaderNodeUVMap", "Source UV")
        source_uv.uv_map = "UVMap"
        graph.tree.links.new(source_uv.outputs[0], source_texture.inputs["Vector"])
    group_copies = {}
    opacity = _alpha(transaction, graph, source, result, group_copies, [0])
    result[_BASE_MATERIAL_HASH] = material.get(_BASE_MATERIAL_HASH, _material_digest(base_material)) if current else _material_digest(base_material)
    result[_PRIVATE_MATERIAL_HASH] = _material_digest(result)
    if authored_uv:
        coordinate = graph.node("ShaderNodeUVMap", "UV")
        coordinate.uv_map = authored_uv
        vector = coordinate.outputs[0]
    else:
        coordinate = graph.node("ShaderNodeTexCoord", "UV")
        vector = coordinate.outputs["Generated" if geometry_kind == "GENERIC" else "Object" if geometry_kind == "BLOCK" else "UV"]
    texture_key = str(result.get("oxal_mc_studio_texture_key", "")).split(":", 1)[-1]
    if geometry_kind == "ITEM" and not authored_uv and not texture_key.startswith("entity/"):
        u, v, width, height = _region(result, atlas_region)
        vector = graph.vector("Atlas Scale", "MULTIPLY", vector, (width, height, 1.0))
        vector = graph.vector("Atlas Offset", "ADD", vector, (u, v, 0.0))
    mapping = graph.node("ShaderNodeMapping", "Mapping")
    mapping.inputs["Rotation"].default_value[2] = math.radians(10)
    # A canonical 16-pixel weapon sprite occupies 16/2048 of the item atlas:
    # its scale-8 glint spans 1/16 texture unit across one block-sized face.
    # Entity density (.5) made placed blocks eight times finer than weapons.
    density = 8.0 if geometry_kind == "ITEM" else BLOCK_PATTERN_SCALE if geometry_kind == "BLOCK" else 0.5
    mapping.inputs["Scale"].default_value = (density,) * 3
    graph.tree.links.new(vector, mapping.inputs["Vector"])
    location = graph.node("ShaderNodeCombineXYZ", "Native Time Vector")
    graph.tree.links.new(native_time.output(graph, "u", "Native Time U"), location.inputs[0])
    graph.tree.links.new(native_time.output(graph, "v", "Native Time V"), location.inputs[1])
    graph.tree.links.new(location.outputs[0], mapping.inputs["Location"])
    suffix = "enchanted_glint_armor.png" if geometry_kind == "ARMOR" else "enchanted_glint_item.png"
    fallback = _ROOT / "assets/minecraft/textures/misc" / suffix
    origin = Path(original_path) if original_path is not None else fallback
    selected = Path(texture_path) if texture_path is not None else origin
    if not selected.is_file() or not origin.is_file():
        raise FileNotFoundError("缺少附魔纹理资源")
    image = _track(transaction, bpy.data.images.load(str(selected), check_existing=False))
    image.pack()
    cast(Any, image.colorspace_settings).name = "Non-Color"
    baseline = image
    if selected.resolve() != origin.resolve():
        baseline = _track(transaction, bpy.data.images.load(str(origin), check_existing=False))
        baseline.pack()
        cast(Any, baseline.colorspace_settings).name = "Non-Color"
    result[ORIGINAL_IMAGE] = baseline
    texture = graph.node("ShaderNodeTexImage", "Texture")
    texture.image = image
    texture.interpolation = "Linear"
    texture.extension = "REPEAT"
    if geometry_kind == "BLOCK" or geometry_kind == "GENERIC" and not authored_uv:
        texture.projection = "BOX"
        texture.projection_blend = 0.15
    graph.tree.links.new(mapping.outputs[0], texture.inputs["Vector"])
    tint = graph.node("ShaderNodeRGB", "Color")
    for axis in range(4):
        curve = _driver(tint.outputs[0], owner, "glint_color", index=axis,
                        expression="value", scene=None)
        curve.driver.variables[0].targets[0].data_path += "[" + str(axis) + "]"
    tinted = graph.vector("Tint", "MULTIPLY", texture.outputs["Color"], tint.outputs[0])
    squared = graph.vector("Color Squared", "MULTIPLY", tinted, tinted)
    monochrome = graph.node("ShaderNodeRGBToBW", "Luminance")
    graph.tree.links.new(texture.outputs["Color"], monochrome.inputs[0])
    luminance = graph.math("Luminance Minimum", "MAXIMUM", monochrome.outputs[0], 0.0001)
    softness = graph.node("ShaderNodeValue", "Gradient")
    _driver(softness.outputs[0], owner, "glint_gradient", expression="min(max(value,0),1)")
    exponent = graph.math("Gradient Exponent", "SUBTRACT", 1,
                          graph.math("Gradient Amount", "MULTIPLY", softness.outputs[0], 0.75))
    adjustment = graph.math("Gradient Adjustment", "DIVIDE",
                            graph.math("Gradient Power", "POWER", luminance, exponent), luminance)
    enabled = graph.node("ShaderNodeValue", "Enabled")
    _driver(enabled.outputs[0], owner, "glint_enabled", expression="min(max(value,0),1)")
    # The reference-derived hull has its own private native material. Keep the
    # user's base and this glint layer free from outline-only face attributes.
    outline_surface = source
    mask = graph.math("Visible Alpha", "MULTIPLY", opacity,
                      graph.math("Pattern Alpha", "GREATER_THAN", texture.outputs["Alpha"], 0.099))
    mask = graph.math("Enabled Alpha", "MULTIPLY", mask, enabled.outputs[0])
    brightness = graph.node("ShaderNodeValue", "Brightness")
    _driver(brightness.outputs[0], owner, "glint_brightness", expression="max(value,0)*max(value,0)")
    strength = graph.math("Radiance", "MULTIPLY", mask,
                         graph.math("Gradient Brightness", "MULTIPLY", brightness.outputs[0], adjustment))
    color = graph.vector("Radiance Color", "SCALE", squared, strength)
    emission_strength = 1
    classic_color = color
    pixel_mode = None
    pixel_source = pixels.build(transaction, result, owner, source, scene, group_copies,
                                generic=geometry_kind == "GENERIC", uv_name=authored_uv or None) if want_pixels else None
    if geometry_kind == "ARMOR":
        if want_pixels and pixel_source is None:
            raise ValueError("盔甲材质没有可用于像素流光的原贴图，未覆盖")
        result[pixels.ARMOR_PREPARATION] = int(want_pixels)
    elif pixels.ARMOR_PREPARATION in result:
        del result[pixels.ARMOR_PREPARATION]
    if pixel_source is not None:
        pixel_tint = graph.vector("Pixel Tint", "MULTIPLY", pixel_source, tint.outputs[0])
        pixel_mask = graph.math("Pixel Alpha", "MULTIPLY", opacity, enabled.outputs[0])
        # Classic glint keeps its authored squared response. Pixel emission
        # uses the base brightness directly, with its own animatable gain.
        pixel_base = graph.node("ShaderNodeValue", "Pixel Base Brightness")
        _driver(pixel_base.outputs[0], owner, "glint_brightness", expression="max(value,0)")
        pixel_multiplier = graph.node("ShaderNodeValue", "Pixel Brightness Multiplier")
        _driver(pixel_multiplier.outputs[0], owner, "glint_pixel_brightness_multiplier", expression="max(value,0)")
        pixel_brightness = graph.math("Pixel Emission Brightness", "MULTIPLY", pixel_base.outputs[0], pixel_multiplier.outputs[0])
        pixel_strength = graph.math("Pixel Brightness", "MULTIPLY", pixel_brightness, pixel_mask)
        mode = graph.node("ShaderNodeValue", "Pixel Mode")
        _driver(mode.outputs[0], owner, "glint_pixel_enabled", expression="min(max(value,0),1)")
        classic_mode = graph.math("Classic Mode", "SUBTRACT", 1, mode.outputs[0])
        color, emission_strength, pixel_mode = pixel_tint, pixel_strength, mode.outputs[0]
        mask = graph.math("Pixel Mask Switch", "ADD",
                          graph.math("Classic Mask", "MULTIPLY", mask, classic_mode),
                          graph.math("Pixel Mask", "MULTIPLY", pixel_mask, mode.outputs[0]))
        result[PIXEL_BRIGHTNESS] = 1
    result[_PRIVATE_HASH] = _digest(result.node_tree)
    result[_PRIVATE_REFERENCES] = _saved_references(_references(result.node_tree))
    emission = graph.node("ShaderNodeEmission", "Emission")
    graph.tree.links.new(color, emission.inputs["Color"])
    graph.feed(emission.inputs["Strength"], emission_strength)
    emission_output = emission.outputs[0]
    if pixel_mode is not None:
        # Mix complete emissions so animated intermediate modes retain the
        # original linear radiance blend without color/strength cross terms.
        classic = graph.node("ShaderNodeEmission", "Classic Emission")
        graph.tree.links.new(classic_color, classic.inputs["Color"])
        classic.inputs["Strength"].default_value = 1
        mode_switch = graph.node("ShaderNodeMixShader", "Pixel Emission Switch")
        graph.tree.links.new(pixel_mode, mode_switch.inputs[0])
        graph.tree.links.new(classic.outputs[0], mode_switch.inputs[1])
        graph.tree.links.new(emission.outputs[0], mode_switch.inputs[2])
        emission_output = mode_switch.outputs[0]
    add = graph.node("ShaderNodeAddShader", "Add")
    graph.tree.links.new(outline_surface, add.inputs[0])
    graph.tree.links.new(emission_output, add.inputs[1])
    mix = graph.node("ShaderNodeMixShader", "Mix")
    graph.tree.links.new(mask, mix.inputs[0])
    graph.tree.links.new(outline_surface, mix.inputs[1])
    graph.tree.links.new(add.outputs[0], mix.inputs[2])
    graph.tree.links.new(mix.outputs[0], _surface(result))
    result[_SCHEMA] = VERSION
    result[_PROFILE] = geometry_kind
    result[_OWNER] = owner
    if _SCENE in result:
        del result[_SCENE]
    native_time.bind(result, clock)
    result[OUTLINE_SHADER] = 4
    if geometry_kind == "BLOCK":
        result[_PATTERN_SCALE] = BLOCK_PATTERN_SCALE
    stamp(result)
    return result


def remove(transaction, owner, material, *, copy=True):
    """Remove only a validated generated layer; preserve the upstream graph."""
    ensure_mutation_allowed()
    current = inspect_source(transaction, material)
    if current is None:
        return material
    if current["version"] == VERSION and current["owner"] != owner:
        raise ValueError("附魔控制归属不匹配，未修改材质")
    if not copy:
        invalidate_source(transaction, material)
    result = _copy_material(transaction, material) if copy else material
    _unlink_layer(result)
    base = material.get(_BASE)
    if (material.get(_BASE_REUSE, True) and base is not None and _complete_base_contract(base.node_tree)
            and _complete_base_contract(result.node_tree)
            and bool(material.get(_BASE_MATERIAL_HASH))
            and material.get(_BASE_MATERIAL_HASH) == _material_digest(base)
            and material.get(_PRIVATE_MATERIAL_HASH) == _material_digest(material)
            and material.get(_BASE_HASH) == _digest(base.node_tree)
            and material.get(_PRIVATE_HASH) == _digest(result.node_tree)
            and _same_references(material.get(_BASE_REFERENCES), _references(base.node_tree))
            and _same_references(material.get(_PRIVATE_REFERENCES), _references(result.node_tree))):
        return base
    _detach_generated_groups(transaction, result)
    for key in tuple(result.keys()):
        if key.startswith("oxal_mc_studio_glint_") and key != SOURCE_UV:
            del result[key]
    return result
