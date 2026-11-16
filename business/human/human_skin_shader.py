"""EEVEE-compatible sampling for BaiPo 2.4.8's unified skin material."""

_LIMITS = "Human247_LidCover_Limits"
_FLAGS = {
    "Human247_LidCover_Long": "仅长眼皮使用额头投影",
    "Human247_LidCover_Upper": "仅修改上眼皮",
}
_SCHEMA = "bp_lid_uv_mask_schema"


def validate_lid_faces(mesh):
    """The two original face flags must equal Limits.X > 0 on this mesh."""
    names = (_LIMITS, *_FLAGS)
    attrs = [mesh.attributes.get(name) for name in names]
    if not any(attrs):
        return  # Body and optional parts have zero for every missing attribute.
    for name, attr, kind in zip(names, attrs, ("FLOAT_VECTOR", "FLOAT", "FLOAT")):
        if attr is None or (attr.domain, attr.data_type) != ("FACE", kind):
            raise ValueError(f"网格 {mesh.name} 的眼皮属性 {name} 已改变")
    limits, long_lid, upper_lid = attrs
    for limit, long_flag, upper_flag in zip(limits.data, long_lid.data, upper_lid.data):
        if not (long_flag.value == upper_flag.value == (limit.vector.x > 0)):
            raise ValueError(f"网格 {mesh.name} 的眼皮蒙版经过自定义")


def _source_inputs(group):
    readers = {}
    for node in group.nodes:
        if node.type != "ATTRIBUTE" or node.attribute_name not in (_LIMITS, *_FLAGS):
            continue
        if node.attribute_name in readers or node.attribute_type != "GEOMETRY" or node.mute:
            raise ValueError("眼皮属性读取方式已改变")
        readers[node.attribute_name] = node
    if readers.keys() != {_LIMITS, *_FLAGS}:
        raise ValueError("缺少原生眼皮蒙版节点")
    vector_links = readers[_LIMITS].outputs["Vector"].links
    if (len(vector_links) != 1 or vector_links[0].is_muted
            or vector_links[0].to_node.type != "SEPXYZ" or vector_links[0].to_node.mute):
        raise ValueError("眼皮 UV 范围连接已改变")
    targets = []
    for name, expected_target in _FLAGS.items():
        reader = readers[name]
        links = [link for socket in reader.outputs for link in socket.links]
        if (len(links) != 1 or links[0].is_muted or links[0].from_socket.name not in {"Fac", "Factor"}
                or links[0].to_node.type != "MIX" or links[0].to_node.name != expected_target
                or links[0].to_socket.identifier != "Factor_Float"):
            raise ValueError(f"眼皮蒙版 {name} 的连接已改变")
        targets.append((reader, links[0].to_socket))
    return vector_links[0].to_node.outputs["X"], targets


def lid_group(material):
    """Return the original group *node* to replace, or None if already repaired."""
    if not material.get("bp_face_brow_shader") or not material.node_tree:
        raise ValueError("未识别白珀 2.4.8 眉纹材质")
    candidates = [node for node in material.node_tree.nodes
                  if node.type == "GROUP" and node.node_tree
                  and any(n.type == "ATTRIBUTE" and n.attribute_name == _LIMITS
                          for n in node.node_tree.nodes)]
    if len(candidates) != 1:
        raise ValueError("未找到唯一的白珀原生眼皮节点组")
    node = candidates[0]
    if node.node_tree.get(_SCHEMA) == 1:
        if any(n.type == "ATTRIBUTE" and n.attribute_name in _FLAGS for n in node.node_tree.nodes):
            raise ValueError("已修复的眼皮节点组重新引用了旧蒙版，请检查自定义连接")
        return None
    _source_inputs(node.node_tree)
    return node


def compact_lid_group(group):
    """Edit a native source or a private copy after validating the bound meshes."""
    first_u, targets = _source_inputs(group)
    mask = group.nodes.new("ShaderNodeMath")
    mask.label = "上眼皮蒙版 · 额头 UV 有效"
    mask.operation = "GREATER_THAN"
    mask.inputs[1].default_value = 0.0
    mask.location = targets[0][0].location
    group.links.new(first_u, mask.inputs[0])
    for reader, socket in targets:
        group.links.new(mask.outputs[0], socket)
        group.nodes.remove(reader)
    group[_SCHEMA] = 1
