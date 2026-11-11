"""PBR texture ports over OXAL's existing native surface graph.

asset_id=oxal.library.crafter_pbr; version=1; policy=REUSE_DATABLOCK;
scope=one asset transaction; source=asset/library/pbr/SOURCE.json.
Only authored parser data is reused. No Crafter Python API is imported.
"""

from pathlib import Path
from typing import Any, cast

import bpy

FORMAT_ITEMS = (
    ("lab_PBR_1.3", "lab PBR 1.3", "Standard lab PBR 1.3 texture channels"),
    ("old_PBR", "old PBR", "Legacy PBR texture channels"),
)
DEFAULT_FORMAT = "lab_PBR_1.3"
PROPERTY = "oxal_mc_studio_pbr_format"
VERSION = "oxal_mc_studio_pbr_version"
ADAPTER = "oxal_mc_studio_pbr_adapter"
ROLE = "oxal_mc_studio_pbr_texture_role"
SOURCE = "oxal_mc_studio_pbr_source"
RESOURCE = Path(__file__).resolve().parents[2] / "asset/library/pbr/crafter_pbr.blend"
_RAW = ("Normal", "Normal Alpha", "PBR", "PBR Alpha")
_DECODED = (
    ("Parsed Normal", "NodeSocketVector", (0.0, 0.0, 1.0)),
    ("Metallic", "NodeSocketFloat", 0.0),
    ("Roughness", "NodeSocketFloat", 0.5),
    ("IOR", "NodeSocketVector", (1.5, 1.5, 1.5)),
    ("IOR-K", "NodeSocketVector", (0.0, 0.0, 0.0)),
    ("AO", "NodeSocketFloat", 1.0),
    ("Height", "NodeSocketFloat", 1.0),
    ("SSS", "NodeSocketFloat", 0.0),
    ("Emission Strength", "NodeSocketFloat", 0.0),
    ("Porosity", "NodeSocketFloat", 0.0),
)


def _format(value):
    if value not in {item[0] for item in FORMAT_ITEMS}:
        raise ValueError("未知 PBR 解析格式")
    return value


def companions(path):
    """Resolve companions of the explicitly selected color image.

    A user selecting a PNG called ``icon_a.png`` selected a color source, even
    if a file called ``icon.png`` happens to exist beside it. Its companions
    are therefore ``icon_a_n.png`` and ``icon_a_s.png``/``icon_a_a.png``.
    """
    path = Path(path)
    stem = path.stem
    found = {}
    normal = path.with_name(stem + "_n.png")
    if normal.is_file() and normal.resolve() != path.resolve():
        found["normal"] = normal
    for suffix in ("_s.png", "_a.png"):
        candidate = path.with_name(stem + suffix)
        if candidate.is_file() and candidate.resolve() != path.resolve():
            found["pbr"] = candidate
    return found


def compatible(material, *, tree=None):
    if material is None:
        return False
    tree = tree or material.node_tree
    if tree is None:
        return False
    nodes = [node for node in tree.nodes if node.get(ADAPTER)]
    return len(nodes) == 1 and nodes[0].type == "GROUP" and nodes[0].node_tree is not None


def _parser(transaction, format):
    format = _format(format)
    cache = getattr(transaction, "_oxal_pbr_parsers", None)
    if cache is None:
        cache = {}
        transaction._oxal_pbr_parsers = cache
    if format in cache:
        return cache[format]
    requested = "C-" + format
    before = {group.as_pointer() for group in bpy.data.node_groups}
    with cast(Any, bpy.data.libraries.load)(str(RESOURCE), link=False) as (source, loaded):
        if requested not in source.node_groups:
            raise ValueError("随包 PBR 解析器资源不完整")
        loaded.node_groups = [requested]
    parser = loaded.node_groups[0]
    if parser is None:
        raise ValueError("无法读取随包 PBR 解析器")
    pending, checked = [parser], set()
    while pending:
        group = pending.pop()
        if group.as_pointer() in checked:
            continue
        checked.add(group.as_pointer())
        if group.as_pointer() in before or group.get(SOURCE) != "Crafter-0.9.8-20261028":
            raise ValueError("PBR 追加依赖来源不明确，未接管现有节点组")
        transaction.track(group)
        group.use_fake_user = False
        pending.extend(node.node_tree for node in group.nodes if node.type == "GROUP" and node.node_tree is not None)
    cache[format] = parser
    return parser


def _socket(group, name, direction, kind, default=None):
    item = group.interface.new_socket(name=name, in_out=direction, socket_type=kind)
    if default is not None:
        item.default_value = default
    return item


def _adapter(transaction, format):
    group = transaction.track(bpy.data.node_groups.new("CI-OXAL Surface", "ShaderNodeTree"))
    group[ADAPTER] = 1
    for name, kind, default in (("Base Color", "NodeSocketColor", (1, 1, 1, 1)),
                                ("Alpha", "NodeSocketFloat", 1.0)):
        _socket(group, name, "INPUT", kind, default)
        _socket(group, name, "OUTPUT", kind)
    _socket(group, "Normal", "INPUT", "NodeSocketColor", (.5, .5, 1, 1))
    _socket(group, "Normal Alpha", "INPUT", "NodeSocketFloat", 1.0)
    _socket(group, "PBR", "INPUT", "NodeSocketColor", (.5, .04 if format == DEFAULT_FORMAT else 0, 0, 1))
    _socket(group, "PBR Alpha", "INPUT", "NodeSocketFloat", 1.0)
    for name, kind, default in _DECODED:
        _socket(group, name, "OUTPUT", kind, default)
    source = group.nodes.new("NodeGroupInput")
    target = group.nodes.new("NodeGroupOutput")
    parser = group.nodes.new("ShaderNodeGroup")
    parser.name = "C-PBR_Parser"
    parser.node_tree = _parser(transaction, format)
    source.location, parser.location, target.location = (-460, 0), (-220, -160), (40, 0)
    group.links.new(source.outputs["Alpha"], target.inputs["Alpha"])
    for name in _RAW:
        group.links.new(source.outputs[name], parser.inputs[name])
    for name, _kind, _default in _DECODED:
        group.links.new(parser.outputs[name], target.inputs[name])
    ao = group.nodes.new("ShaderNodeMixRGB")
    ao.name, ao.blend_type = "OXAL PBR Ambient Occlusion", "MULTIPLY"
    ao.inputs[0].default_value = 1.0
    group.links.new(source.outputs["Base Color"], ao.inputs[1])
    group.links.new(parser.outputs["AO"], ao.inputs[2])
    group.links.new(ao.outputs[0], target.inputs["Base Color"])
    return group


def _upstream(tree, node, socket, fallback):
    links = node.inputs[socket].links
    return links[0].from_socket if links else fallback


def install(transaction, material, path, *, key="", scene=None, format=DEFAULT_FORMAT, tree=None, maps=None):
    """Add raw PBR ports while preserving OXAL's Tint/BSDF/alpha consumers.

    The caller owns this newly created/private material and its transaction.
    Constant default parser inputs require no companion image samples. Raw
    ports retain the native interface used by existing OXAL materials.
    """
    format = _format(format)
    tree = tree or material.node_tree
    if compatible(material, tree=tree):
        return refresh(transaction, material, path, key=key, scene=scene, format=format, tree=tree, maps=maps)
    if tree is None:
        raise ValueError("PBR 材质缺少节点树")
    source = tree.nodes.get("OXAL Source Texture")
    tint = tree.nodes.get("OXAL Tint")
    shaders = [node for node in tree.nodes if node.type == "BSDF_PRINCIPLED"]
    if source is None or source.type != "TEX_IMAGE" or tint is None or len(shaders) != 1:
        raise ValueError("材质不是可识别的 OXAL 基础表面，未覆盖用户节点")
    shader = shaders[0]
    controlled = ("Normal", "Metallic", "Roughness", "IOR", "Subsurface Weight")
    if any(shader.inputs[name].is_linked for name in controlled):
        raise ValueError("材质已有用户 PBR 输入连线，未覆盖原底材")
    protected = {shader.inputs[name].path_from_id("default_value") for name in controlled}
    animation = tree.animation_data
    if animation is not None:
        from ...util.animation import action_fcurves

        strips = [strip for track in animation.nla_tracks for strip in track.strips]
        bindings = [animation]
        while strips:
            strip = strips.pop()
            bindings.append(strip)
            strips.extend(getattr(strip, "strips", ()))
        if any(curve.data_path in protected for curve in animation.drivers) or any(
            curve.data_path in protected for binding in bindings
            for curve in action_fcurves(binding.action, slot=getattr(binding, "action_slot", None))
        ):
            raise ValueError("材质 PBR 参数已有动画，未覆盖原底材")
    color = _upstream(tree, tint, 1, source.outputs["Color"])
    alpha = _upstream(tree, shader, "Alpha", source.outputs["Alpha"])
    adapter = tree.nodes.new("ShaderNodeGroup")
    adapter.name = "OXAL Crafter Surface"
    adapter[ADAPTER] = 1
    adapter.node_tree = _adapter(transaction, format)
    tree.links.new(color, adapter.inputs["Base Color"])
    tree.links.new(alpha, adapter.inputs["Alpha"])
    tree.links.new(adapter.outputs["Base Color"], tint.inputs[1])
    tree.links.new(adapter.outputs["Alpha"], shader.inputs["Alpha"])
    for output, name in (("Parsed Normal", "Normal"), ("Metallic", "Metallic"),
                         ("Roughness", "Roughness"), ("IOR", "IOR"), ("SSS", "Subsurface Weight")):
        tree.links.new(adapter.outputs[output], shader.inputs[name])
    # Keep the original emissive color/tint link and any explicit intensity.
    emission = tree.nodes.new("ShaderNodeMath")
    emission.name, emission.operation = "OXAL PBR Emission", "ADD"
    strength = shader.inputs["Emission Strength"]
    if strength.is_linked:
        tree.links.new(strength.links[0].from_socket, emission.inputs[0])
    else:
        emission.inputs[0].default_value = strength.default_value
    tree.links.new(adapter.outputs["Emission Strength"], emission.inputs[1])
    tree.links.new(emission.outputs[0], strength)
    if not shader.inputs["Emission Color"].is_linked:
        tree.links.new(tint.outputs[0], shader.inputs["Emission Color"])
    material[VERSION] = 1
    material[PROPERTY] = format
    return refresh(transaction, material, path, key=key, scene=scene, format=format, tree=tree, maps=maps)


def refresh(transaction, material, path, *, key="", scene=None, format=None, tree=None, maps=None):
    """Replace owned companion samplers in the caller's explicit transaction."""
    from . import asset_geometry as geometry

    format = _format(format or material.get(PROPERTY, DEFAULT_FORMAT))
    tree = tree or material.node_tree
    if not compatible(material, tree=tree):
        raise ValueError("请先明确升级此资产的 PBR 材质")
    adapter = next(node for node in tree.nodes if node.get(ADAPTER))
    if adapter.node_tree not in transaction.ids:
        adapter.node_tree = transaction.track(adapter.node_tree.copy())
    parser = adapter.node_tree.nodes.get("C-PBR_Parser")
    if parser is None or parser.type != "GROUP":
        raise ValueError("PBR 适配器已被改写，未替换用户节点")
    parser.node_tree = _parser(transaction, format)
    material[PROPERTY] = format
    adapter.inputs["PBR"].default_value = (.5, .04 if format == DEFAULT_FORMAT else 0, 0, 1)
    for name in _RAW:
        for link in tuple(adapter.inputs[name].links):
            tree.links.remove(link)
    # Removing a sampler's animation also frees other nodes in this tree.
    # Freeze only the main samplers and their roles before any deletion;
    # keeping all RNA node wrappers would later visit freed animation nodes.
    samplers = tuple((node, role) for node in tree.nodes
                     if node.type == "TEX_IMAGE" and (role := node.get(ROLE)) in {"normal", "pbr"})
    for node, role in samplers:
        geometry.configure_animation(material, path, restore=True, scene=scene, image_node=node, tree=tree,
                                     prefix="OXAL PBR " + role + " Animation ")
        tree.nodes.remove(node)
    source = tree.nodes["OXAL Source Texture"]
    for role, image_path in (companions(path) if maps is None else maps).items():
        node = tree.nodes.new("ShaderNodeTexImage")
        node.name = "OXAL PBR " + role + " Texture"
        node[ROLE] = role
        image = transaction.track(bpy.data.images.load(str(image_path), check_existing=False))
        if image.size[0] < 1 or image.size[1] < 1:
            raise ValueError("PBR 配套贴图无法解码：" + image_path.name)
        image.colorspace_settings.name = "Non-Color"
        image.pack()
        node.image = image
        node.interpolation, node.extension = "Closest", source.extension
        suffix = image_path.stem[len(Path(path).stem):]
        node[geometry.TEXTURE_KEY] = key + suffix if key else ""
        node[geometry.ORIGINAL_PATH] = str(image_path)
        node[geometry.ORIGINAL_IMAGE] = image
        vector = source.inputs["Vector"].links
        mapping = tree.nodes.get("OXAL Animation Current Mapping")
        if mapping is not None:
            vector = mapping.inputs[0].links
        if vector:
            tree.links.new(vector[0].from_socket, node.inputs["Vector"])
        color_name, alpha_name = ("Normal", "Normal Alpha") if role == "normal" else ("PBR", "PBR Alpha")
        tree.links.new(node.outputs["Color"], adapter.inputs[color_name])
        tree.links.new(node.outputs["Alpha"], adapter.inputs[alpha_name])
        geometry.configure_animation(material, image_path, scene=scene, image_node=node, tree=tree,
                                     prefix="OXAL PBR " + role + " Animation ")
    return material
