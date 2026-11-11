"""Bounded native face relief on a borrowed render object's evaluated surface.

asset_id: oxal.enchantment.external.relief; asset_version: 1;
policy: NEW_INSTANCE on explicit preparation; scope: one exact selected Object
and its private effects controller. Only our modifier and NodeTree are edited;
the user's Mesh, UVs, shape keys, armature and Actions remain their original IDs.
"""

from typing import Any

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import require_owned
from . import enchantment_outline as contracts

RECORD = "oxal_external_relief_state"
MARK = "oxal_external_relief_schema"
MAX_CELLS = 16384
SUBDIVISION = "glint_geometry_subdivision"


def _clone(value) -> Any:
    if isinstance(value, bpy.types.ID):
        return value
    if hasattr(value, "items"):
        return {key: _clone(part) for key, part in value.items()}
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return [_clone(part) for part in value]


def prepared(owner):
    """Presentation metadata only; no validation, evaluation or source reads."""
    row = owner.get(RECORD)
    return bool(row and row.get("schema") == 1 and row.get("active")
                and isinstance(row.get("group"), bpy.types.NodeTree))


def _position(target, modifier=None):
    from .enchantment_external_assigners import MARK as ADAPTER

    others = tuple(item for item in target.modifiers if item != modifier)
    adapter = [item for item in others if item.type == "NODES" and item.node_group and item.node_group.get(ADAPTER)]
    if len(adapter) > 1 or adapter and others[-1] != adapter[0]:
        raise ValueError("外部材质适配器顺序已变化，未排列用户修改器")
    return len(others) - bool(adapter)


def validate(transaction, scene, target, owner):
    ensure_mutation_allowed()
    if (owner.get("oxal_external_target") != target or scene.objects.get(target.name) != target
            or target.library or target.override_library):
        raise ValueError("外部3D凹凸目标或场景已改变，未接管")
    row = owner.get(RECORD)
    if row is None:
        if any(item.type == "NODES" and item.node_group and item.node_group.get(MARK) for item in target.modifiers):
            raise ValueError("目标存在未登记的外部3D凹凸，未按名称接管")
        return None
    if not hasattr(row, "get") or row.get("schema") != 1:
        raise ValueError("未知外部3D凹凸版本")
    group = row.get("group")
    modifier = next((item for item in target.modifiers if item.persistent_uid == row.get("modifier_uid")), None)
    if not isinstance(group, bpy.types.NodeTree):
        raise TypeError("外部3D凹凸实际节点引用已失效")
    if group not in transaction.ids:
        require_owned(owner, (group,))
    if (modifier is None or modifier.type != "NODES" or modifier.node_group != group
            or group.get(MARK) != 1 or not contracts.contract_matches(group)
            or not contracts.modifier_contract_matches(modifier, row.get("settings"))
            or list(target.modifiers).index(modifier) != _position(target, modifier)):
        raise ValueError("外部3D凹凸节点、参数或顺序含用户修改，未覆盖")
    prefix = modifier.path_from_id()
    from .enchantment import _curves

    animation = target.animation_data
    if any(curve.data_path.startswith(prefix) for curve in
           (*_curves(target), *(animation.drivers if animation else ()))):
        raise ValueError("3D凹凸修改器有独立动画，请改用附魔参数通道；原动画未修改")
    return modifier


def _journal(transaction, owner):
    existed, previous = RECORD in owner, _clone(owner.get(RECORD))

    def restore():
        if existed:
            owner[RECORD] = previous
        elif RECORD in owner:
            del owner[RECORD]

    transaction.on_rollback(restore)


def _publish(transaction, target, owner, modifier, group, *, active, levels):
    _journal(transaction, owner)
    if modifier is None:
        modifier = target.modifiers.new("附魔 · 原生3D凹凸", "NODES")
        identity = modifier.persistent_uid

        def restore_created():
            actual = next((item for item in target.modifiers if item.persistent_uid == identity), None)
            if actual is not None and actual.type == "NODES" and actual.node_group == group:
                target.modifiers.remove(actual)

        transaction.on_rollback(restore_created)
        position = _position(target, modifier)
        target.modifiers.move(len(target.modifiers) - 1, position)
    else:
        previous = modifier.node_group
        transaction.on_rollback(lambda: setattr(modifier, "node_group", previous))
    modifier.node_group = group
    owner[RECORD] = {"schema": 1, "active": active, "group": group,
                     "modifier_uid": modifier.persistent_uid, "subdivision": levels,
                     "settings": contracts._modifier_contract(modifier)}
    target.update_tag()


def _source_image(material):
    """One unambiguous flat sampler can define authored texel heights."""
    from .enchantment import PREFIX

    if material is None or not material.use_nodes or material.node_tree is None:
        return None
    images, seen = [], set()

    def visit(tree):
        if tree in seen:
            return
        seen.add(tree)
        for node in tree.nodes:
            if node.name.startswith(PREFIX):
                continue
            if (node.type == "TEX_IMAGE" and node.image is not None and node.projection == "FLAT"
                    and node.image.source in {"FILE", "GENERATED"}):
                images.append(node)
            elif node.type == "GROUP" and node.node_tree:
                visit(node.node_tree)

    visit(material.node_tree)
    return images[0] if len(images) == 1 else None


def _height(graph, target, geometry):
    """Original UV texels when known, otherwise a stable native cell field."""
    position = graph.node("GeometryNodeInputPosition", "Relief Source Position").outputs[0]
    cells = graph.vector("FLOOR", graph.vector("SCALE", position, 16), name="Fixed Relief Cells")
    noise = graph.node("ShaderNodeTexNoise", "Relief Fallback Heights")
    noise.noise_dimensions = "4D"
    noise.inputs["Detail"].default_value = 0
    noise.inputs["Scale"].default_value = .73
    graph.feed(noise.inputs["Vector"], cells)
    graph.feed(noise.inputs["W"], graph.value("glint_geometry_seed"))
    height = noise.outputs["Fac"]
    uv_layers = getattr(target.data, "uv_layers", ())
    uv_name = next((layer.name for layer in uv_layers if layer.active_render), None)
    if uv_name is None and uv_layers:
        uv_name = uv_layers[0].name
    if uv_name:
        uv = graph.attribute(uv_name, "FLOAT_VECTOR")
        slot_index = graph.node("GeometryNodeInputMaterialIndex", "Relief Original Material Slot").outputs[0]
        for index, slot in enumerate(target.material_slots):
            source = _source_image(slot.material)
            if source is None:
                continue
            sampler_uv = uv
            if source.inputs["Vector"].is_linked:
                coordinate = source.inputs["Vector"].links[0].from_socket
                if coordinate.node.type == "UVMAP":
                    name = coordinate.node.uv_map or uv_name
                    sampler_uv = graph.attribute(name, "FLOAT_VECTOR")
                elif coordinate.node.type != "TEX_COORD" or coordinate.name != "UV":
                    # A procedural/animated mapping is preserved by the base
                    # material. Relief deliberately uses the stable fallback
                    # field rather than guessing that shader coordinate DAG.
                    continue
            texture = graph.node("GeometryNodeImageTexture", "Relief Original Image " + str(index))
            texture.inputs["Image"].default_value = source.image
            texture.interpolation, texture.extension = "Closest", source.extension
            graph.feed(texture.inputs["Vector"], sampler_uv)
            luminance = graph.node("ShaderNodeRGBToBW", "Relief Original Luminance " + str(index))
            graph.feed(luminance.inputs[0], texture.outputs["Color"])
            switch = graph.node("GeometryNodeSwitch", "Relief Material " + str(index))
            switch.input_type = "FLOAT"
            graph.feed(switch.inputs["Switch"], graph.math("COMPARE", slot_index, index))
            graph.feed(switch.inputs["False"], height)
            graph.feed(switch.inputs["True"], graph.math("MULTIPLY", luminance.outputs[0], texture.outputs["Alpha"]))
            height = switch.outputs[0]
    factor = graph.math("MULTIPLY", graph.value("glint_geometry_random_enabled"), graph.value("glint_geometry_random_factor"))
    random = graph.math("MULTIPLY", graph.math("SUBTRACT", noise.outputs["Fac"], .5),
                        graph.math("MULTIPLY", factor, graph.value("glint_geometry_height_ratio")))
    amount = graph.math("MAXIMUM", .05, graph.math("ADD", graph.math("ADD", .25, graph.math("MULTIPLY", height, .75)), random))
    bounds = graph.node("GeometryNodeBoundBox", "Relief Original Bounds")
    graph.feed(bounds.inputs["Geometry"], geometry)
    center = graph.vector("SCALE", graph.vector("ADD", bounds.outputs["Min"], bounds.outputs["Max"]), .5)
    extent = graph.vector("LENGTH", graph.vector("SUBTRACT", bounds.outputs["Max"], bounds.outputs["Min"]))
    radius = graph.math("MAXIMUM", 1e-6, graph.math("MULTIPLY", extent, graph.math("MAXIMUM", .0001, graph.value("glint_geometry_center_range"))))
    falloff = graph.math("MINIMUM", 1, graph.math("DIVIDE", graph.vector("DISTANCE", position, center), radius))
    return graph.math("MULTIPLY", amount,
                      graph.math("SUBTRACT", 1, graph.math("MULTIPLY", falloff, graph.value("glint_geometry_center_falloff"))))


def _source_face_count(transaction, scene, target, owner, modifier):
    """Count this frame's real pre-relief surface without mutating the user ID.

    Even an instance-only GN source must be realized for an honest count. The
    probe shares the source Mesh/Key/Action read-only; its Object and small GN
    tree are explicitly created and retired before any resource publication.
    """
    from .enchantment_external_assigners import _validate as validate_adapter

    adapter, _ = validate_adapter(transaction, target, owner)
    modifiers = tuple(target.modifiers)
    cutoff = modifiers.index(modifier) if modifier is not None else len(modifiers)
    if modifier is not None:
        permitted_tail = (modifier, adapter) if adapter is not None else (modifier,)
        if modifiers[cutoff:] != permitted_tail:
            raise ValueError("3D凹凸后存在未知修改器，未采样不明来源")
    elif adapter is not None:
        cutoff -= 1
    probe = transaction.track(target.copy())
    tree = None
    evaluated = None
    try:
        # Removing copied modifiers does not touch the originals or their
        # shared node groups; all user modifiers preceding relief stay intact.
        for part in tuple(probe.modifiers)[cutoff:]:
            probe.modifiers.remove(part)
        probe.hide_viewport = False
        probe.hide_render = False
        probe.hide_select = True
        scene.collection.objects.link(probe)
        tree = transaction.track(bpy.data.node_groups.new("3D凹凸 · 临时来源计数", "GeometryNodeTree"))
        tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        source = tree.nodes.new("NodeGroupInput")
        output = tree.nodes.new("NodeGroupOutput")
        realize = tree.nodes.new("GeometryNodeRealizeInstances")
        tree.links.new(source.outputs["Geometry"], realize.inputs["Geometry"])
        tree.links.new(realize.outputs["Geometry"], output.inputs["Geometry"])
        probe.modifiers.new("3D凹凸 · 来源计数", "NODES").node_group = tree
        graph = bpy.context.evaluated_depsgraph_get()
        evaluated = probe.evaluated_get(graph)
        mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
        return len(mesh.polygons) if mesh is not None else 0
    finally:
        if evaluated is not None:
            evaluated.to_mesh_clear()
        # Forget before native removal so later rollback never dereferences a
        # freed probe. Removal failures restore the explicit creation proof.
        for item, collection in ((probe, bpy.data.objects), (tree, bpy.data.node_groups)):
            if item is None:
                continue
            transaction.forget(item)
            try:
                collection.remove(item)
            except Exception:
                transaction.track(item)
                raise


def apply(transaction, scene, target, owner):
    """Build explicit bounded native relief; all playback remains in Blender."""
    ensure_mutation_allowed()
    modifier = validate(transaction, scene, target, owner)
    if target.type != "MESH":
        raise ValueError("3D凹凸需要网格对象；曲线和文字请先自行建立网格副本，原对象未转换")
    if SUBDIVISION not in owner:
        default = 0 if target.get("oxal_mc_studio_pixel_topology") else 2
        transaction.on_rollback(lambda: owner.__delitem__(SUBDIVISION) if SUBDIVISION in owner else None)
        owner[SUBDIVISION] = default
        owner.id_properties_ui(SUBDIVISION).update(min=0, max=4, soft_max=3, default=default,
                                                  description="明确重新准备3D时应用；原生几何受16384格预算限制")
    levels = owner[SUBDIVISION]
    if isinstance(levels, bool) or not isinstance(levels, int) or not 0 <= levels <= 4:
        raise ValueError("3D凹凸细分级别必须为0至4的整数")
    count = _source_face_count(transaction, scene, target, owner, modifier)
    if not count:
        raise ValueError("3D凹凸需要有面的网格，原对象未修改")
    if count * 4 ** levels > MAX_CELLS:
        raise ValueError(f"3D凹凸预计{count * 4 ** levels}格，超过16384格预算，请降低细分后重新准备")
    graph = contracts.Graph(transaction, owner, "external_relief")
    source = graph.entry.outputs["Geometry"]
    realize = graph.node("GeometryNodeRealizeInstances", "Relief Native Instances")
    graph.feed(realize.inputs["Geometry"], source)
    geometry = realize.outputs["Geometry"]
    size = graph.node("GeometryNodeAttributeDomainSize", "Relief Source Budget")
    size.component = "MESH"
    graph.feed(size.inputs["Geometry"], geometry)
    allowed = graph.math("LESS_THAN", size.outputs["Face Count"], MAX_CELLS // (4 ** levels) + 1)
    nonempty = graph.math("GREATER_THAN", size.outputs["Face Count"], 0)
    allowed = graph.math("MULTIPLY", allowed, nonempty)
    enabled = graph.math("MULTIPLY", graph.value("glint_geometry_enabled"), allowed)
    guarded = graph.node("GeometryNodeSwitch", "Relief Budget Gate")
    guarded.input_type = "GEOMETRY"
    graph.feed(guarded.inputs["Switch"], enabled)
    graph.feed(guarded.inputs["True"], geometry)
    subdivide = graph.node("GeometryNodeSubdivideMesh", "Relief Explicit Subdivision")
    subdivide.inputs["Level"].default_value = levels
    graph.feed(subdivide.inputs["Mesh"], guarded.outputs[0])
    geometry = subdivide.outputs["Mesh"]
    amount = _height(graph, target, geometry)
    from .enchantment_geometry import _capture

    items = [("FLOAT", "Relief Height", amount)]
    uv_layers = tuple(getattr(target.data, "uv_layers", ()))
    items.extend(("VECTOR", "Relief UV " + str(index), graph.attribute(layer.name, "FLOAT_VECTOR"))
                 for index, layer in enumerate(uv_layers))
    geometry, captured = _capture(graph, geometry, "FACE", items)
    normal = graph.node("GeometryNodeInputNormal", "Relief Source Normal").outputs[0]
    outer = graph.value("glint_geometry_outer", "max(value,0)")
    inner = graph.value("glint_geometry_inner", "max(value,0)")
    start = graph.node("GeometryNodeSetPosition", "Relief Inward Base")
    graph.feed(start.inputs["Geometry"], geometry)
    graph.feed(start.inputs["Offset"], graph.vector("SCALE", normal, graph.math("MULTIPLY", -1, outer)))
    extrude = graph.node("GeometryNodeExtrudeMesh", "Relief Actual Pixel Volume")
    extrude.mode = "FACES"
    extrude.inputs["Individual"].default_value = True
    graph.feed(extrude.inputs["Mesh"], start.outputs["Geometry"])
    graph.feed(extrude.inputs["Offset Scale"], graph.math("MULTIPLY", graph.math("ADD", outer, inner), captured[0]))
    geometry = extrude.outputs["Mesh"]
    for index, layer in enumerate(uv_layers):
        geometry = graph.store(geometry, layer.name, "FLOAT2", "CORNER", captured[index + 1], extrude.outputs["Side"])
    switch = graph.node("GeometryNodeSwitch", "Relief Keep Original When Disabled")
    switch.input_type = "GEOMETRY"
    graph.feed(switch.inputs["Switch"], enabled)
    graph.feed(switch.inputs["False"], source)
    graph.feed(switch.inputs["True"], geometry)
    tree = graph.finish(switch.outputs[0], MARK)
    _publish(transaction, target, owner, modifier, tree, active=True, levels=levels)
    return tree


def remove(transaction, scene, target, owner):
    """Retire generated relief without deleting/recreating a native modifier."""
    modifier = validate(transaction, scene, target, owner)
    if modifier is None:
        return False
    graph = contracts.Graph(transaction, owner, "external_relief_passthrough")
    tree = graph.finish(graph.entry.outputs["Geometry"], MARK)
    _publish(transaction, target, owner, modifier, tree, active=False, levels=0)
    return True
