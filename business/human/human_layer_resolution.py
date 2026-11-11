"""Native 64/128 second-layer cells on the immutable BaiPo surface grid.

asset_id: oxal.human.second_layer_resolution; version: 1; NEW_INSTANCE;
scope: one precisely owned human; source: LAYER_RESOLUTION.json, derived from
the packaged body source. Only explicit import/skin/part operations prepare
descriptors. Playback uses native Geometry Nodes and existing bone weights.
"""

import hashlib
import json
import math
from functools import lru_cache
from typing import Any, cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util import drivers
from ...util.animation import action_fcurves
from ...util.ownership import require_owned
from .human_source import layer_declaration
from .human_state import SOURCE_NAME

RECORD = "oxal_mc_studio_human_layer_resolution"
MARK = "oxal_mc_studio_human_layer_resolution_schema"
ATTR = "oxal_layer_resolution_"
SELECT = ATTR + "cell"
CORNER = ATTR + "corner"

# Blender defines these native enums dynamically. Keep them in the authored
# 4.5-5.2 source/adapter contract while excluding add-on RNA extensions.
_NATIVE_RUNTIME_ENUMS = {
    "GeometryNodeExtrudeMesh": {"mode"},
    "GeometryNodeDeleteGeometry": {"mode", "domain"},
    "GeometryNodeMergeByDistance": {"mode"},
    "FunctionNodeBooleanMath": {"operation"},
    "GeometryNodeSwitch": {"input_type"},
    "GeometryNodeIndexSwitch": {"data_type"},
    "GeometryNodeInputNamedAttribute": {"data_type"},
    "GeometryNodeSeparateGeometry": {"domain"},
    "GeometryNodeStoreNamedAttribute": {"data_type", "domain"},
}


def _plain(value):
    if isinstance(value, bpy.types.ID):
        return "SELF"
    if hasattr(value, "items"):
        return {key: _plain(part) for key, part in value.items()}
    if isinstance(value, (str, int, bool, type(None))):
        return value
    if isinstance(value, float):
        return round(value, 8) if math.isfinite(value) else str(value)
    return [_plain(part) for part in value]


def _digest(value):
    return hashlib.sha256(json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _attribute(attribute):
    rows = []
    for item in attribute.data:
        rows.append([(prop.identifier, getattr(item, prop.identifier)) for prop in item.bl_rna.properties
                     if prop.identifier != "rna_type" and prop.type in {"BOOLEAN", "INT", "FLOAT", "STRING"}])
    return [attribute.data_type, attribute.domain, rows]


def fingerprint(obj, *, exclude=()):
    """Actual native mesh data, independent of display names and selection."""
    mesh = obj.data
    topology = {"position", ".edge_verts", ".corner_vert", ".corner_edge", "material_index"}
    attributes = {attr.name: _attribute(attr) for attr in mesh.attributes
                  if attr.name not in {*topology, *exclude, *mesh.uv_layers.keys()}
                  and not attr.name.startswith((".select_", ".hide_", ".uv_select_"))}
    return _digest({
        "vertices": [tuple(vertex.co) for vertex in mesh.vertices],
        "edges": [tuple(edge.vertices) for edge in mesh.edges],
        "faces": [(tuple(poly.vertices), poly.material_index, poly.use_smooth) for poly in mesh.polygons],
        "uv": {layer.name: [tuple(item.uv) for item in layer.data] for layer in mesh.uv_layers},
        "groups": [group.name for group in obj.vertex_groups],
        "weights": [[(group.group, group.weight) for group in vertex.groups] for vertex in mesh.vertices],
        "attributes": attributes,
        "normals": [tuple(normal.vector) for normal in mesh.corner_normals] if mesh.has_custom_normals else [],
    })


@lru_cache(maxsize=2)
def _declarations(source):
    record = json.loads(source.read_text("utf8"))
    if record.get("asset_id") != "oxal.human.second_layer_resolution" or record.get("version") != 1:
        raise ValueError("人模二层分辨率声明不兼容")
    return record["objects"]


def declarations(root=None):
    return _declarations(layer_declaration(root))


def _source(obj):
    values = [modifier for modifier in obj.modifiers if modifier.type == "NODES"
              and modifier.node_group and modifier.node_group.get("bp_second_layer_geometry")]
    if len(values) > 1:
        raise ValueError("人模二层几何来源不唯一，未修改：" + obj.name)
    return values[0] if values else None


def _track(transaction, value):
    transaction.track(value)
    return value


def _proof(transaction, fresh_ids):
    return set(fresh_ids) | set(getattr(transaction, "ids", ())) | set(getattr(transaction, "owned", ()))


def _require(root, items, proof):
    existing = tuple(item for item in items if item not in proof)
    if existing:
        require_owned(root, existing)


def _record(obj) -> dict[str, Any] | None:
    def snapshot(value):
        if isinstance(value, bpy.types.ID):
            return value
        if hasattr(value, "items"):
            return {key: snapshot(part) for key, part in value.items()}
        if hasattr(value, "to_list"):
            return value.to_list()
        return value
    value = obj.get(RECORD)
    if value is None:
        return None
    if not hasattr(value, "items"):
        raise ValueError("二层分辨率登记损坏，未接管")
    return cast(dict[str, Any], snapshot(value))


def _own_curves(root, obj, modifier):
    from .human import parameter_owner
    controls = parameter_owner(root)
    prefix = modifier.path_from_id() + "."
    captured = drivers.capture(obj)
    captured["curves"] = [curve for curve in captured["curves"] if curve["path"].startswith(prefix)]
    for curve in captured["curves"]:
        for variable in curve["variables"]:
            for target in variable["targets"]:
                if target.get("id") != controls:
                    raise ValueError("二层分辨率驱动包含外部目标，未覆盖")
                target["id"] = "CONTROLS"
    return _digest(captured)


def _tree_contract(tree, *, legacy45=False):
    from ..outline import outline_compat

    def properties(owner):
        return [(prop.identifier, getattr(owner, prop.identifier)) for prop in owner.bl_rna.properties
                # Foreign add-ons can extend Node RNA without changing this
                # graph. Those fields are not part of the authored contract;
                # reading them could also invoke an unrelated Python getter.
                if not prop.is_readonly
                and (not prop.is_runtime or (
                    prop.type == "ENUM"
                    and prop.identifier in _NATIVE_RUNTIME_ENUMS.get(owner.bl_idname, ())))
                and prop.type in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}
                and prop.identifier not in {"name", "label", "location", "width", "height", "select", "hide", "color", "use_custom_color"}
                and not prop.identifier.startswith("bl_")]
    animated = set()
    animation = tree.animation_data
    if legacy45 and animation:
        animated.update(curve.data_path for curve in animation.drivers)
        bindings = [animation, *(strip for track in animation.nla_tracks for strip in track.strips)]
        while bindings:
            binding = bindings.pop()
            animated.update(curve.data_path for curve in action_fcurves(binding.action, slot=getattr(binding, "action_slot", None)))
            bindings.extend(getattr(binding, "strips", ()))
    nodes = []
    for node in tree.nodes:
        scalar = properties(node)
        values = [(index, socket.default_value) for index, socket in enumerate((*tuple(node.inputs), *tuple(node.outputs)))
                  if hasattr(socket, "default_value")]
        if legacy45:
            scalar, values = outline_compat.node_record(node, scalar, values, animated)
        nodes.append((node.name, node.bl_idname, scalar, values))
    def identifier(node, socket, output):
        return outline_compat.socket_identifier(node, socket.identifier, output=output) if legacy45 else socket.identifier
    return _digest([nodes, [(link.from_node.name, identifier(link.from_node, link.from_socket, True),
                            link.to_node.name, identifier(link.to_node, link.to_socket, False)) for link in tree.links]])


def _tree_contract_matches(tree, expected):
    """Match native bytes or the exact tested 4.5 RNA migration candidate."""
    return expected == _tree_contract(tree) or expected == _tree_contract(tree, legacy45=True)


def _validate(root, obj, source, row, proof):
    if row.get("source_tree_contract") and not _tree_contract_matches(source.node_group, row["source_tree_contract"]):
        raise ValueError("人模原生二层节点已被修改，未覆盖：" + obj.name)
    record = _record(obj)
    if record is None:
        if any(mod.type == "NODES" and mod.node_group and mod.node_group.get(MARK) for mod in obj.modifiers):
            raise ValueError("二层分辨率登记缺失，未接管修改器")
        if fingerprint(obj) != row["fingerprint"]:
            raise ValueError("人模二层网格、UV或权重含用户修改，未覆盖：" + obj.name)
        return None
    modifier = obj.modifiers.get(record.get("modifier", ""))
    tree = record.get("group")
    if (obj.data != record.get("mesh") or modifier is None or modifier.type != "NODES"
            or modifier.node_group != tree or not isinstance(tree, bpy.types.NodeTree) or tree.get(MARK) != 1
            or source.name != record.get("source") or list(obj.modifiers).index(modifier) >= list(obj.modifiers).index(source)):
        raise ValueError("人模二层分辨率归属或顺序已改变，未覆盖")
    if (record.get("resolution") not in {64, 128}
            or any(item != modifier and item.type == "NODES" and item.node_group and item.node_group.get(MARK) for item in obj.modifiers)):
        raise ValueError("二层分辨率包含额外或未知修改器，未接管")
    _require(root, (modifier.node_group, obj.data), proof)
    if tree.animation_data or not _tree_contract_matches(tree, record.get("tree_contract")):
        raise ValueError("二层分辨率节点含用户修改，未覆盖")
    attributes = dict(record.get("attributes", {}).items())
    if any(name not in obj.data.attributes or _digest(_attribute(obj.data.attributes[name])) != signature
           for name, signature in attributes.items()):
        raise ValueError("二层分辨率像素描述含用户修改，未覆盖")
    if fingerprint(obj, exclude=attributes) != row["fingerprint"] or record.get("drivers") != _own_curves(root, obj, modifier):
        raise ValueError("二层网格或原生分辨率驱动含用户修改，未覆盖")
    paths = {curve.data_path for curve in obj.animation_data.drivers} if obj.animation_data else set()
    for name in ("show_viewport", "show_render"):
        if modifier.path_from_id(name) not in paths and getattr(modifier, name) != record.get("flags", {}).get(name):
            raise ValueError("二层分辨率固定可见性已被用户改变，未覆盖")
    return modifier


def _describe(transaction, obj, row):
    mesh = _track(transaction, obj.data.copy())
    attributes = {}
    def attr(name, datatype, domain):
        if mesh.attributes.get(name) is not None:
            raise ValueError("像素描述属性与既有数据冲突，未覆盖")
        value = mesh.attributes.new(name, datatype, domain)
        attributes[name] = value
        return value
    attr(SELECT, "BOOLEAN", "FACE")
    attr(CORNER, "INT", "CORNER")
    for i in range(4):attr(ATTR + "vertex_" + str(i), "INT", "FACE")
    uv_names = {name: [ATTR + "uv_" + str(index) + "_" + str(i) for i in range(4)]
          for index, name in enumerate(mesh.uv_layers.keys())}
    for names in uv_names.values():
        for name in names:attr(name, "FLOAT_VECTOR", "FACE")
    # Native attribute creation can reallocate the containing CustomData array.
    # Re-acquire every RNA wrapper after the last allocation.
    selected, corner = mesh.attributes[SELECT], mesh.attributes[CORNER]
    indices = [mesh.attributes[ATTR + "vertex_" + str(i)] for i in range(4)]
    uv = {name: [mesh.attributes[value] for value in names] for name, names in uv_names.items()}
    for face in mesh.polygons:
        for i, loop in enumerate(face.loop_indices):
            corner.data[loop].value = i
    for cell in row["cells"]:
        index = cell["face"]
        selected.data[index].value = True
        for i in range(4):
            indices[i].data[index].value = cell["vertices"][i]
            for name, layers in uv.items():
                layers[i].data[index].vector = (*cell["uv"][name][i], 0)
    return mesh, {name: _digest(_attribute(mesh.attributes[name])) for name in attributes}


def _graph(transaction, obj):
    tree = cast(Any, _track(transaction, bpy.data.node_groups.new("人模二层 · 64像素单元", "GeometryNodeTree")))
    tree[MARK] = 1
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    def node(kind, name):
        value = tree.nodes.new(kind); value.name = name
        return value
    def attribute(name, datatype):
        value = node("GeometryNodeInputNamedAttribute", name)
        value.data_type = datatype; value.inputs["Name"].default_value = name
        return value.outputs["Attribute"]
    def store(geometry, name, datatype, domain, value):
        item = node("GeometryNodeStoreNamedAttribute", "Store " + name)
        item.data_type, item.domain = datatype, domain
        item.inputs["Name"].default_value = name
        tree.links.new(geometry, item.inputs["Geometry"])
        tree.links.new(value, item.inputs["Value"])
        return item.outputs["Geometry"]
    def choose(datatype, values):
        item = node("GeometryNodeIndexSwitch", "Choose Corner")
        item.data_type = datatype
        while len(item.index_switch_items) < 4:item.index_switch_items.new()
        tree.links.new(attribute(CORNER, "INT"), item.inputs["Index"])
        for index, value in enumerate(values):tree.links.new(value, item.inputs[index + 1])
        return item.outputs[0]
    entry, output = node("NodeGroupInput", "Input"), node("NodeGroupOutput", "Output")
    source = entry.outputs["Geometry"]
    selected = node("GeometryNodeSeparateGeometry", "64 Cell Faces");selected.domain = "FACE"
    tree.links.new(source, selected.inputs["Geometry"])
    tree.links.new(attribute(SELECT, "BOOLEAN"), selected.inputs["Selection"])
    split = node("GeometryNodeSplitEdges", "Independent Corners")
    tree.links.new(selected.outputs["Selection"], split.inputs["Mesh"])
    geometry = split.outputs["Mesh"]
    index = choose("INT", [attribute(ATTR + "vertex_" + str(i), "INT") for i in range(4)])
    def sample(value):
        item = node("GeometryNodeSampleIndex", "Sample Original Corner")
        item.data_type, item.domain = "FLOAT_VECTOR" if value.type == "VECTOR" else "FLOAT", "POINT"
        tree.links.new(source, item.inputs["Geometry"])
        tree.links.new(value, item.inputs["Value"])
        tree.links.new(index, item.inputs["Index"])
        return item.outputs["Value"]
    move = node("GeometryNodeSetPosition", "Coarse Outer Corners")
    tree.links.new(geometry, move.inputs["Geometry"])
    tree.links.new(sample(node("GeometryNodeInputPosition", "Original Position").outputs[0]), move.inputs["Position"])
    geometry = move.outputs["Geometry"]
    used = {weight.group for vertex in obj.data.vertices for weight in vertex.groups if weight.weight}
    for group in obj.vertex_groups:
        if group.index in used:
            geometry = store(geometry, group.name, "FLOAT", "POINT", sample(attribute(group.name, "FLOAT")))
    for number, name in enumerate(obj.data.uv_layers.keys()):
        uv = choose("VECTOR", [attribute(ATTR + "uv_" + str(number) + "_" + str(i), "FLOAT_VECTOR") for i in range(4)])
        geometry = store(geometry, name, "FLOAT2", "CORNER", uv)
    tree.links.new(geometry, output.inputs["Geometry"])
    return tree


def _set_visibility(root, obj, modifier, source, resolution):
    from .human import parameter_owner
    controls = parameter_owner(root)
    saved = drivers.capture(obj)
    output = {"animation": saved["animation"], "curves": list(saved["curves"])}
    paths = {modifier.path_from_id(name) for name in ("show_viewport", "show_render")}
    output["curves"] = [curve for curve in output["curves"] if curve["path"] not in paths]
    for name in ("show_viewport", "show_render"):
        path = modifier.path_from_id(name)
        if resolution == 128:
            continue
        original = next((curve for curve in saved["curves"] if curve["path"] == source.path_from_id(name)), None)
        if original is None:
            setattr(modifier, name, getattr(source, name))
            continue
        native = obj.animation_data.drivers.find(source.path_from_id(name))
        if not native.driver.is_simple_expression or native.driver.use_self:
            raise ValueError("原生二层可见性包含非原生驱动，未接管")
        if any(target.get("id") != controls for variable in original["variables"] for target in variable["targets"]):
            raise ValueError("二层可见性不再由此人模控制，未接管")
        output["curves"].append({**original, "path": path})
        setattr(modifier, name, getattr(source, name))
    drivers.restore(obj, output)
    if resolution == 128:
        modifier.show_viewport = modifier.show_render = False


def prepare(transaction, root, objects, image, *, fresh_ids=()):
    """Prepare before publication; the caller commits or rolls back atomically."""
    ensure_mutation_allowed()
    if image is None or tuple(image.size) not in {(64, 64), (128, 128)}:
        raise ValueError("二层像素凹凸仅支持64×64或128×128皮肤")
    resolution = int(image.size[0])
    proof = _proof(transaction, fresh_ids)
    declared = declarations(root)
    plans = []
    for obj in tuple(objects):
        if obj.type != "MESH":continue
        source = _source(obj)
        if source is None:continue
        _require(root, (obj, obj.data, source.node_group), proof)
        label = str(obj.get(SOURCE_NAME, obj.name))
        row = declared.get(label)
        canonical = label
        # Local library appends may have no weak source reference after native
        # Undo/Redo. Resolve collision suffixes only on this operation's proven
        # new object; the immutable topology/UV/weight check below still applies.
        if row is None and obj in proof:
            while canonical not in declared:
                prefix, separator, suffix = canonical.rpartition(".")
                if not separator or not suffix.isdigit():
                    break
                canonical = prefix
            row = declared.get(canonical)
        if row is None or obj.data.shape_keys or obj.data.animation_data:
            raise ValueError("二层网格来源或拓扑动画不受支持，未覆盖：" + obj.name)
        modifier = _validate(root, obj, source, row, proof)
        if any(item not in {modifier} for item in tuple(obj.modifiers)[:list(obj.modifiers).index(source)]):
            raise ValueError("二层像素输入之前有额外用户修改器，未覆盖")
        if modifier is not None:
            paths = {modifier.path_from_id(name) for name in ("show_viewport", "show_render")}
            animation = obj.animation_data
            bindings = [animation] if animation else []
            bindings += [strip for track in animation.nla_tracks for strip in track.strips] if animation else []
            while bindings:
                binding = bindings.pop()
                if any(curve.data_path in paths for curve in action_fcurves(
                        binding.action, slot=getattr(binding, "action_slot", None))):
                    raise ValueError("分辨率修改器包含用户Action/NLA，未覆盖")
                bindings.extend(getattr(binding, "strips", ()))
        if not hasattr(transaction, "on_rollback") and obj not in proof:
            raise TypeError("既有二层调整需要可恢复事务")
        plans.append((obj, source, row, modifier, canonical))
    for obj, source, row, modifier, canonical in plans:
        if obj in proof and canonical != str(obj.get(SOURCE_NAME, obj.name)):
            obj[SOURCE_NAME] = canonical
        if modifier is None and resolution == 128:
            continue
        previous_mesh, previous_record = obj.data, _record(obj)
        previous_drivers = drivers.capture(obj)
        previous_index = list(obj.modifiers).index(modifier) if modifier is not None else None
        previous_flags = (modifier.show_viewport, modifier.show_render) if modifier is not None else None
        created = []
        def restore(obj=obj, modifier=modifier, mesh=previous_mesh, record=previous_record,
                    captured=previous_drivers, index=previous_index, flags=previous_flags, created=created):
            obj.data = mesh
            if modifier is None:
                if created and any(item == created[0] for item in obj.modifiers):
                    obj.modifiers.remove(created[0])
            else:
                assert flags is not None and index is not None
                modifier.show_viewport, modifier.show_render = flags
                if list(obj.modifiers).index(modifier) != index:obj.modifiers.move(list(obj.modifiers).index(modifier), index)
            if record is None:
                if RECORD in obj:del obj[RECORD]
            else:obj[RECORD] = record
            drivers.restore(obj, captured)
        if hasattr(transaction, "on_rollback"):transaction.on_rollback(restore)
        if modifier is None:
            mesh, attributes = _describe(transaction, obj, row)
            obj.data = mesh
            group = _graph(transaction, obj)
            modifier = obj.modifiers.new("人模二层 · 64/128像素", "NODES")
            created.append(modifier)
            modifier.node_group = group
            obj.modifiers.move(list(obj.modifiers).index(modifier), 0)
            record = {"mesh":mesh,"group":group,"modifier":modifier.name,"source":source.name,
                      "attributes":attributes,"tree_contract":_tree_contract(group)}
        else:
            record = _record(obj)
            assert record is not None
        _set_visibility(root, obj, modifier, source, resolution)
        record["resolution"], record["drivers"] = resolution, _own_curves(root, obj, modifier)
        record["flags"] = {name: getattr(modifier, name) for name in ("show_viewport", "show_render")}
        obj[RECORD] = record
        obj.update_tag()


def current_resolution(image):
    """Read-only UI value from the actual skin, without geometry evaluation."""
    return int(image.size[0]) if image is not None and tuple(image.size) in {(64, 64), (128, 128)} else None


def image_for(root):
    """Resolve the actual native layer input, including pre-current_skin Beta."""
    from ...util.geometry_nodes import get_input
    images = set()
    for obj in tuple(root.all_objects):
        source = _source(obj)
        if source is None:
            continue
        require_owned(root, (obj, source.node_group))
        sockets = [socket for socket in source.node_group.interface.items_tree
                   if getattr(socket, "in_out", "") == "INPUT" and socket.name == "Image"]
        if len(sockets) != 1:
            raise ValueError("人模二层皮肤接口不唯一，未选择皮肤")
        image = get_input(source, sockets[0].identifier)
        if not isinstance(image, bpy.types.Image) or current_resolution(image) is None:
            raise ValueError("二层实际皮肤不是64×64或128×128，未替换")
        require_owned(root, (image,))
        images.add(image)
    if len(images) != 1:
        raise ValueError("人模二层使用混合皮肤或缺少有效皮肤，未自动选择")
    image = next(iter(images))
    remembered = root.get("oxal_human_current_skin")
    if remembered is not None and remembered != image:
        raise ValueError("人模当前皮肤记录与实际二层输入不同，未覆盖")
    return image
