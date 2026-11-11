"""Explicit native depth repair for the authored 2.4.3 closed-eye covers.

asset_id: oxal.human.eye_depth; version: 1; policy: ACTIVATE_EXISTING;
scope: one proven human; source: the pinned 2.4.3 body Mesh/Keys/UV/GN.
Only the second coplanar lid cover moves, during the last tenth of closing.
"""

import hashlib
import json
from array import array
from typing import Any, cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.geometry_nodes import get_input, input_path, set_input
from ...util.ownership import require_no_external_users, require_owned
from ..asset.asset_geometry import Creation
from .human_source import HEAD_SOURCE, model_version
from .human_state import SOURCE_NAME

MARK = "oxal_human_eye_depth_schema"
_ATTRS = ("Human243_EyeDepth_L", "Human243_EyeDepth_R")
_SOCKETS = ("闭眼微深度 · 左", "闭眼微深度 · 右")
_MESH_HASH = "b175b2ab38402d7571d5c63514368a9723463f7f2ffbdf0524d578b633d55d63"
_BASE_GRAPH_HASH = "7e8e97b23009c4e14c1b08655b20d8d283ab4314b6ca6513c1f2c08280ce6d6e"
_GRAPH_HASH = "4e744cf49759a258de9f444ef36961c477788e232fc3afc9d29dd8e1a276ec7d"
_KEY_DRIVERS_HASH = "ee529cc06974c7edf55d077cf55a0d767c50010312f35238f2e556e7361ed1de"
_EPSILON = 0.00015
# Authored source polygon indices, second cover only. No eyeball/brow faces.
_FACES = ((998, 999, 1514, 1515, 1777, 1778, 2043, 2044, 2302, 2303),
          (990, 991, 1507, 1508, 1770, 1771, 2036, 2037, 2295, 2296))


def _mesh_hash(mesh):
    digest = hashlib.sha256()

    def values(data, field, count, kind="f"):
        result = array(kind, [0]) * count
        data.foreach_get(field, result)
        digest.update(result.tobytes())

    values(mesh.vertices, "co", len(mesh.vertices) * 3)
    values(mesh.edges, "vertices", len(mesh.edges) * 2, "i")
    values(mesh.loops, "vertex_index", len(mesh.loops), "i")
    values(mesh.polygons, "loop_total", len(mesh.polygons), "i")
    keys = mesh.shape_keys
    if keys is None or len(keys.key_blocks) != 141:
        raise ValueError("头部形态键与原始 2.4.3 不一致，未修改")
    for key in keys.key_blocks:
        digest.update((key.name + "\0" + key.relative_key.name + "\0").encode())
        values(key.data, "co", len(key.data) * 3)
    for uv in mesh.uv_layers:
        digest.update(uv.name.encode())
        values(uv.data, "uv", len(uv.data) * 2)
    for name in ("V3_HeadVariant", "V3_TransparentFill", "V3_EyeWindowUV",
                 *(f"V3_UV_{i}" for i in range(8)),
                 *(f"Human243_HD_UV_{i}" for i in range(8))):
        attr = mesh.attributes.get(name)
        if attr is None:
            raise ValueError("头部原生 UV/分支属性缺失，未修改：" + name)
        digest.update((name + attr.domain + attr.data_type).encode())
        vector = attr.data_type in {"FLOAT_VECTOR", "FLOAT2"}
        values(attr.data, "vector" if vector else "value",
               len(attr.data) * (2 if attr.data_type == "FLOAT2" else 3 if vector else 1),
               "f" if vector else "i")
    return digest.hexdigest()


def _graph_hash(group):
    def value(item):
        if isinstance(item, bpy.types.ID):
            return "ID"
        if isinstance(item, (str, bool, int, float)) or item is None:
            return item
        return list(item)

    def socket_id(node, socket):
        # 5.2 removes inactive Compare sockets and renames A_INT/B_INT to A/B.
        # Match the actual typed input, retaining every active link and value.
        if node.bl_idname == "FunctionNodeCompare":
            return socket.name + ":" + socket.type
        return socket.identifier

    nodes = []
    for node in group.nodes:
        nodes.append((node.name, node.bl_idname,
                      [(key, getattr(node, key)) for key in
                       ("operation", "data_type", "input_type", "domain", "mode",
                        "clamp", "interpolation_type") if hasattr(node, key)],
                      [(socket_id(node, s), value(s.default_value)) for s in node.inputs
                       if hasattr(s, "default_value") and not s.is_linked
                       and not (node.bl_idname == "FunctionNodeCompare" and s.is_unavailable)]))
    links = sorted((link.from_node.name, socket_id(link.from_node, link.from_socket),
                    link.to_node.name, socket_id(link.to_node, link.to_socket)) for link in group.links)
    interface = [(s.name, s.identifier, s.in_out, s.socket_type)
                 for s in group.interface.items_tree if s.item_type == "SOCKET"]
    return hashlib.sha256(json.dumps((nodes, links, interface), ensure_ascii=False,
                                    sort_keys=True).encode()).hexdigest()


def _roles(head):
    result = []
    for faces in _FACES:
        points = {vertex for index in faces for vertex in head.data.polygons[index].vertices}
        result.append([float(i in points) for i in range(len(head.data.vertices))])
    return result


def _key_drivers_hash(keys, rig):
    rows = []
    for curve in keys.animation_data.drivers if keys.animation_data else ():
        variables = []
        for variable in curve.driver.variables:
            targets = []
            for target in variable.targets:
                if target.id != rig:
                    raise ValueError("头部形态键存在外部驱动目标，未修改")
                targets.append((target.data_path, target.bone_target,
                                target.transform_type, target.transform_space))
            variables.append((variable.name, variable.type, targets))
        rows.append((curve.data_path, curve.array_index, curve.mute,
                     curve.driver.type, curve.driver.expression, variables))
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def _validate(head, rig):
    if head.type != "MESH" or head.data.library or head.data.override_library:
        raise ValueError("头部不是可编辑的原生网格")
    if _mesh_hash(head.data) != _MESH_HASH:
        raise ValueError("头部 Mesh/141 个形态键/UV 已修改，未覆盖用户数据")
    modifiers = tuple(head.modifiers)
    if len(modifiers) != 2 or [m.type for m in modifiers] != ["NODES", "ARMATURE"]:
        raise ValueError("请先移除头部描边或其它修改器，再修复闭眼叠面")
    modifier = modifiers[0]
    group = modifier.node_group
    if group is None or modifiers[1].object != rig:
        raise ValueError("头部原生几何节点或骨架绑定已修改")
    if group.animation_data and (group.animation_data.action or group.animation_data.nla_tracks
                                 or group.animation_data.drivers):
        raise ValueError("头部节点组带自定义动画，未修改")
    for name in ("L.眼皮", "R.眼皮"):
        if rig.pose.bones.get(name) is None:
            raise ValueError("原生眼皮控制器缺失：" + name)
    schema = head.get(MARK)
    if schema not in (None, 1) or group.get(MARK) != schema:
        raise ValueError("头部闭眼修订登记不一致")
    expected = _GRAPH_HASH if schema == 1 else _BASE_GRAPH_HASH
    if _graph_hash(group) != expected:
        raise ValueError("头部原生节点已修改，未覆盖")
    if schema == 1:
        for name, expected_values in zip(_ATTRS, _roles(head), strict=True):
            attr = head.data.attributes.get(name)
            if (attr is None or attr.domain != "POINT" or attr.data_type != "FLOAT"
                    or [v.value for v in attr.data] != expected_values):
                raise ValueError("头部闭眼微深度属性已修改")
    elif any(name in head.data.attributes for name in _ATTRS):
        raise ValueError("存在未登记的闭眼属性，未覆盖")
    keys = head.data.shape_keys
    if keys.animation_data and (keys.animation_data.action or keys.animation_data.nla_tracks):
        raise ValueError("头部 Key 带自定义 Action/NLA，请先保留独立副本")
    if _key_drivers_hash(keys, rig) != _KEY_DRIVERS_HASH:
        raise ValueError("头部原生形态键驱动已修改，未覆盖")
    curves = tuple(head.animation_data.drivers) if head.animation_data else ()
    switch_path = input_path(modifier, "Socket_2")
    switches = [curve for curve in curves if curve.data_path == switch_path]
    if len(switches) != 1:
        raise ValueError("头型控制器驱动缺失")
    switch = switches[0]
    driver = switch.driver
    if (switch.mute or driver.expression != "min(7,max(0,floor(-z*25+0.00001)))"
            or driver.type != "SCRIPTED" or len(driver.variables) != 1):
        raise ValueError("头型控制器驱动已修改")
    variable = driver.variables[0]
    target = variable.targets[0]
    if (variable.name != "z" or variable.type != "SINGLE_PROP" or target.id != rig
            or target.data_path != 'pose.bones["面部兼容"].location[2]'):
        raise ValueError("头型控制器目标已修改")
    return modifier, schema == 1


def _patch(transaction, head, rig):
    """Validated source derivation; caller proves source/instance ownership."""
    modifier, ready = _validate(head, rig)
    if ready:
        _validate_drivers(head, modifier, rig)
        return False
    old_group = modifier.node_group
    new_group = old_group.copy()
    transaction.track(new_group)
    added_paths = []
    legacy_fields = (set(modifier.keys())
                     if modifier.bl_rna.properties.get("properties") is None else None)
    old_inputs = {socket.identifier: get_input(modifier, socket.identifier)
                  for socket in old_group.interface.items_tree
                  if socket.item_type == "SOCKET" and socket.in_out == "INPUT"
                  and hasattr(socket, "default_value")}

    def restore():
        for path in added_paths:
            head.driver_remove(path)
        modifier.node_group = old_group
        if legacy_fields is not None:
            for field in set(modifier.keys()) - legacy_fields:
                del modifier[field]
        for field, value in old_inputs.items():
            set_input(modifier, field, value)
        for name in _ATTRS:
            attr = head.data.attributes.get(name)
            if attr is not None:
                head.data.attributes.remove(attr)
        if MARK in head:
            del head[MARK]

    transaction.on_rollback(restore)
    for name, values in zip(_ATTRS, _roles(head), strict=True):
        attr = head.data.attributes.new(name, "FLOAT", "POINT")
        attr.data.foreach_set("value", values)
    nodes, links = new_group.nodes, new_group.links
    output = next(node for node in nodes if node.bl_idname == "NodeGroupOutput")
    origin = output.inputs[0].links[0].from_socket
    source = next(node for node in nodes if node.bl_idname == "NodeGroupInput")
    terms = []
    for side, (name, attr_name, bone) in enumerate(zip(_SOCKETS, _ATTRS, ("L.眼皮", "R.眼皮"), strict=True)):
        socket = new_group.interface.new_socket(name=name, in_out="INPUT", socket_type="NodeSocketFloat")
        socket.min_value, socket.max_value = 0, 1
        mask = nodes.new("GeometryNodeInputNamedAttribute")
        mask.data_type = "FLOAT"
        mask.inputs["Name"].default_value = attr_name
        smooth = nodes.new("ShaderNodeMapRange")
        smooth.interpolation_type = "SMOOTHSTEP"
        smooth.clamp = True
        smooth.inputs["From Min"].default_value = 0.9
        smooth.inputs["From Max"].default_value = 1.0
        links.new(source.outputs[socket.identifier], smooth.inputs["Value"])
        product = nodes.new("ShaderNodeMath")
        product.operation = "MULTIPLY"
        links.new(mask.outputs["Attribute"], product.inputs[0])
        links.new(smooth.outputs["Result"], product.inputs[1])
        terms.append(product.outputs[0])
        for index, node in enumerate((mask, smooth, product)):
            node.location = (1100 + index * 210, -side * 350 - 450)
        # The new group must be bound before driver_add resolves its sockets.
        modifier.node_group = new_group
        for field, value in old_inputs.items():
            set_input(modifier, field, value)
        path = input_path(modifier, socket.identifier)
        added_paths.append(path)
        curve = head.driver_add(path)
        curve.keyframe_points.clear()
        curve.driver.type = "SCRIPTED"
        curve.driver.expression = "min(1,max(0,-z*10))"
        variable = curve.driver.variables.new()
        variable.name, variable.type = "z", "TRANSFORMS"
        target = variable.targets[0]
        target.id, target.bone_target = rig, bone
        target.transform_type, target.transform_space = "LOC_Z", "LOCAL_SPACE"
    add = nodes.new("ShaderNodeMath")
    add.operation = "ADD"
    links.new(terms[0], add.inputs[0])
    links.new(terms[1], add.inputs[1])
    scale = nodes.new("ShaderNodeMath")
    scale.operation = "MULTIPLY"
    scale.inputs[1].default_value = _EPSILON
    links.new(add.outputs[0], scale.inputs[0])
    vector = nodes.new("ShaderNodeCombineXYZ")
    links.new(scale.outputs[0], vector.inputs[0])
    position = nodes.new("GeometryNodeSetPosition")
    links.new(origin, position.inputs["Geometry"])
    links.new(vector.outputs[0], position.inputs["Offset"])
    links.new(position.outputs["Geometry"], output.inputs[0])
    for index, node in enumerate((add, scale, vector, position, output)):
        node.location = (1750 + index * 210, -350)
    head[MARK] = new_group[MARK] = 1
    head.data.update()
    head.update_tag()
    return True


def _validate_drivers(head, modifier, rig):
    curves = tuple(head.animation_data.drivers) if head.animation_data else ()
    for name, bone in zip(_SOCKETS, ("L.眼皮", "R.眼皮"), strict=True):
        socket = next(s for s in modifier.node_group.interface.items_tree if s.name == name)
        path = input_path(modifier, socket.identifier)
        candidates = [curve for curve in curves if curve.data_path == path]
        if len(candidates) != 1:
            raise ValueError("头部闭眼原生驱动缺失")
        curve = candidates[0]
        driver = curve.driver
        if (curve.mute or driver.type != "SCRIPTED" or driver.expression != "min(1,max(0,-z*10))"
                or len(driver.variables) != 1):
            raise ValueError("头部闭眼原生驱动已修改")
        variable = driver.variables[0]
        target = variable.targets[0]
        if (variable.name != "z" or variable.type != "TRANSFORMS" or target.id != rig
                or target.bone_target != bone or target.transform_type != "LOC_Z"
                or target.transform_space != "LOCAL_SPACE"):
            raise ValueError("头部闭眼驱动目标已修改")


def prepare(root: Any, rig: Any) -> dict[str, Any]:
    """Repair one proven existing instance; callers provide native Undo/dispatch."""
    ensure_mutation_allowed()
    if bpy.context.mode != "OBJECT":
        raise ValueError("请先进入物体模式再修复闭眼叠面")
    if model_version(root) != "2.4.3" or rig not in tuple(root.all_objects):
        raise ValueError("此修订仅适用于当前 2.4.3 人模")
    heads = [obj for obj in root.all_objects if obj.get(SOURCE_NAME) == HEAD_SOURCE]
    if len(heads) != 1:
        raise ValueError("头部来源角色不唯一，未接管")
    head = heads[0]
    modifier, ready = _validate(head, rig)
    group = modifier.node_group
    require_owned(root, (root, rig, rig.data, head, head.data, head.data.shape_keys, group))
    require_no_external_users(cast(Any, bpy.context.scene), root, (head, head.data, head.data.shape_keys, group))
    if any(obj != head and obj.type == "MESH" and obj.data == head.data for obj in bpy.data.objects):
        raise ValueError("头部网格被其它对象共享，未修改")
    if ready:
        _validate_drivers(head, modifier, rig)
        return {"changed": False, "schema": 1}
    with Creation() as transaction:
        transaction.owner = root
        _patch(transaction, head, rig)
        _validate(head, rig)
        _validate_drivers(head, modifier, rig)
        transaction.commit()
    return {"changed": True, "schema": 1}
