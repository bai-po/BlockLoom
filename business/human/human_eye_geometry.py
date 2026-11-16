"""Per-variant UVs, two-axis pupil scaling and lid-front eye expressions."""

from typing import Any, cast

import bpy
from mathutils import Vector

from ...util.geometry_nodes import get_input, input_path, set_input
from .human_eye_glyphs import _MODIFIER, _RADIUS, _RAW_BROWS, _ROLE

_NATIVE = "BP_EyePupilRadius"
_SHRINK = "BP_EyeLegacyShrinkCorrection"
_FRONT = "BP_EyeLidFrontOffset"
_RAW_BROW_RADIUS = "BP_RawBrowCarrierRadius"


def _point_fields(transaction, head, rig):
    mesh = head.data
    basis = mesh.shape_keys.reference_key
    transform = rig.matrix_world.inverted() @ head.matrix_world
    inverse = transform.to_3x3().inverted()
    coords = [transform @ p.co for p in basis.data]
    for name in (_NATIVE, _SHRINK, _FRONT, _RAW_BROW_RADIUS):
        if mesh.attributes.get(name):
            raise ValueError("眼部缩放属性已被占用：" + name)
        mesh.attributes.new(name, "FLOAT_VECTOR", "POINT")
        transaction.on_rollback(lambda n=name: mesh.attributes.remove(mesh.attributes[n]))
    native, shrink, front = (mesh.attributes[n].data for n in (_NATIVE, _SHRINK, _FRONT))
    variant = mesh.attributes["V3_HeadVariant"].data
    roles = mesh.attributes[_ROLE].data
    lids = mesh.attributes["Human247_LidCover_Limits"].data
    for v in range(7):
        key = mesh.shape_keys.key_blocks[f"T{v}::缩小眼睛"]
        driver = mesh.shape_keys.animation_data.drivers.find(key.path_from_id("value"))
        if not driver or len(driver.driver.variables) != 1:
            raise ValueError("原生眼睛缩放驱动已自定义：" + key.name)
        source = driver.driver.variables[0].targets[0]
        if (source.id != rig or source.bone_target != "眼睛" or source.transform_type != "SCALE_AVG"
                or driver.driver.expression.replace(" ", "") != "(-var+1)*5" or driver.mute
                or (key.slider_min, key.slider_max) != (0, 1)):
            raise ValueError("原生眼睛缩放曲线已自定义：" + key.name)
        faces = [p for p in mesh.polygons if variant[p.index].value == v]
        for side in (-1, 1):
            pupil = {i for p in faces if roles[p.index].value == side for i in p.vertices}
            covers = {i for p in faces if lids[p.index].vector.x > 0
                      and side * coords[p.vertices[0]].x > 0 for i in p.vertices}
            if not pupil or not covers:
                raise ValueError(f"T{v} 缺少原生瞳孔或眼皮")
            prefix = f"T{v}::闭{'左' if side == 1 else '右'}"
            closure = next(k for k in mesh.shape_keys.key_blocks if k.name in (prefix, prefix+"眼"))
            closed = [transform @ closure.data[i].co for i in covers]
            # Mark the front of this particular lid, including its tilt when closed.
            depth = min(p.y for p in closed) - .00065
            x = (max(coords[i].x for i in pupil) + min(coords[i].x for i in pupil)) / 2
            z = (max(coords[i].z for i in pupil) + min(coords[i].z for i in pupil)) / 2
            for i in pupil:
                p = coords[i]
                native[i].vector = inverse @ Vector((p.x-x, 0, p.z-z))
                shrink[i].vector = basis.data[i].co - key.data[i].co
                front[i].vector = inverse @ Vector((0, depth-p.y, 0))
    for side in (-1, 1):
        carrier, = (p for p in mesh.polygons if roles[p.index].value == 3*side)
        # Each strip is a copy of the same T7 face. Scale the two about
        # their combined bounds so the inside corner stays joined.
        tips = [transform @ (basis.data[i].co + mesh.attributes[name].data[i].vector)
                for name in _RAW_BROWS for i in carrier.vertices]
        center_x = (min(p.x for p in tips) + max(p.x for p in tips)) / 2
        center_z = (min(p.z for p in tips) + max(p.z for p in tips)) / 2
        for i in carrier.vertices:
            p = coords[i]
            mesh.attributes[_RAW_BROW_RADIUS].data[i].vector = inverse @ Vector((p.x-center_x, 0, p.z-center_z))
    return tuple((inverse @ Vector(axis)).normalized() for axis in ((1, 0, 0), (0, 0, 1)))


def _update_graph(tree, active_uv, axes):
    from .human_face_brow_mode import scale_closed_strips

    nodes, link = tree.nodes, tree.links.new
    source, = (n for n in nodes if n.type == "GROUP_INPUT")
    material, = (n for n in nodes if n.bl_idname == "GeometryNodeSetMaterialIndex")
    sprite_position = material.inputs["Geometry"].links[0].from_node
    rotation, = (n for n in nodes if n.bl_idname == "ShaderNodeVectorRotate")
    gt, = (n for n in nodes if n.type == "MATH" and n.operation == "GREATER_THAN"
           and n.inputs[1].default_value == 1 and n.inputs[0].is_linked)
    mode = gt.inputs[0].links[0].from_socket
    source_geometry = source.outputs["几何"]
    old_geometry_inputs = [edge.to_socket for edge in source_geometry.links]
    active_outputs = [edge.to_socket for edge in gt.outputs[0].links]

    def scalar(op, a, b):
        node = nodes.new("ShaderNodeMath")
        node.operation = op
        link(a, node.inputs[0])
        if isinstance(b, (int, float)):
            node.inputs[1].default_value = b
        else:
            link(b, node.inputs[1])
        return node.outputs[0]

    def vector(op, a, b):
        node = nodes.new("ShaderNodeVectorMath")
        node.operation = op
        link(a, node.inputs[0])
        target = node.inputs["Scale" if op == "SCALE" else 1]
        if isinstance(b, Vector):
            target.default_value = b
        else:
            link(b, target)
        return node.outputs["Value" if op == "DOT_PRODUCT" else "Vector"]

    def attr(name, kind):
        node = nodes.new("GeometryNodeInputNamedAttribute")
        node.data_type = kind
        node.inputs["Name"].default_value = name
        return node.outputs["Attribute"]

    def parameter(name, default):
        socket = tree.interface.new_socket(name=name, in_out="INPUT", socket_type="NodeSocketFloat")
        socket.default_value = default
        return source.outputs[socket.identifier]

    active = scalar("MULTIPLY", gt.outputs[0], scalar("LESS_THAN", mode, 4))
    for target in active_outputs:
        link(active, target)
    scales = {}
    role = attr(_ROLE, "INT")
    left = scalar("MAXIMUM", scalar("GREATER_THAN", role, 0), attr("BP_BrowLeft", "BOOLEAN"))
    for direction in ("宽度", "高度"):
        select = nodes.new("GeometryNodeSwitch")
        select.input_type = "FLOAT"
        link(left, select.inputs["Switch"])
        for side, label, pin in (("L", "左", "True"), ("R", "右", "False")):
            amount = parameter(label + "眼" + direction, 1)
            scales[side, direction] = amount.identifier
            link(amount, select.inputs[pin])
        scales[direction] = scalar("SUBTRACT", select.outputs[0], 1)

    def growth(radius):
        factors = [scalar("MULTIPLY", vector("DOT_PRODUCT", radius, axis), scales[direction])
                   for axis, direction in zip(axes, ("宽度", "高度"))]
        components = []
        for axis, factor in zip(axes, factors):
            node = nodes.new("ShaderNodeVectorMath")
            node.operation = "SCALE"
            node.inputs[0].default_value = axis
            link(factor, node.inputs["Scale"])
            components.append(node.outputs[0])
        return vector("ADD", *components)

    # All eight source UV maps remain intact on the original mesh. Material
    # Preview and Workbench Texture can now both use the evaluated active UV.
    uv = nodes.new("GeometryNodeStoreNamedAttribute")
    uv.data_type, uv.domain = "FLOAT2", "CORNER"
    uv.inputs["Name"].default_value = active_uv
    link(scale_closed_strips(tree, source_geometry, growth), uv.inputs["Geometry"])
    link(attr("V6_CurrentSkinUV", "FLOAT_VECTOR"), uv.inputs["Value"])
    # The unified skin shader cuts out these triangles completely. Workbench
    # cannot execute its alpha mask, so omit their geometry on every live eye.
    hole = nodes.new("GeometryNodeDeleteGeometry")
    hole.domain = "FACE"
    link(uv.outputs[0], hole.inputs["Geometry"])
    link(scalar("COMPARE", scalar("ABSOLUTE", role, 0), 2), hole.inputs["Selection"])
    legacy = parameter("原生缩眼形态", 0)
    normal = vector("SCALE", growth(attr(_NATIVE, "FLOAT_VECTOR")), scalar("COMPARE", active, 0))
    offset = vector("ADD", normal, vector("SCALE", attr(_SHRINK, "FLOAT_VECTOR"), legacy))
    adjusted = nodes.new("GeometryNodeSetPosition")
    link(hole.outputs[0], adjusted.inputs["Geometry"])
    link(offset, adjusted.inputs["Offset"])
    for target in old_geometry_inputs:
        link(adjusted.outputs[0], target)

    radius = attr(_RADIUS, "FLOAT_VECTOR")
    link(vector("ADD", radius, growth(radius)), rotation.inputs["Vector"])
    raw_origin = attr(_RAW_BROW_RADIUS, "FLOAT_VECTOR")
    for name in _RAW_BROWS:
        field, = (n.outputs["Attribute"] for n in nodes if n.bl_idname == "GeometryNodeInputNamedAttribute"
                  and n.inputs["Name"].default_value == name)
        target, = (edge.to_socket for edge in field.links if edge.to_socket.name == "Offset")
        link(vector("ADD", field, growth(vector("ADD", raw_origin, field))), target)
    front = parameter("眼皮外显示", 0)
    old_offset = sprite_position.inputs["Offset"].links[0].from_socket
    link(vector("ADD", old_offset, vector("SCALE", attr(_FRONT, "FLOAT_VECTOR"), front)),
         sprite_position.inputs["Offset"])
    return scales, legacy.identifier, front.identifier


def install(transaction, rig, objects):
    from .human_eye_selector import BROW_SWITCH, BROW_THRESHOLD

    head, = [o for o in objects if o.type == "MESH" and _MODIFIER in o.modifiers]
    mod = head.modifiers[_MODIFIER]
    source = mod.node_group
    if source.get("bp_pupil_scaling") == 1:
        return
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned
        require_owned(transaction.owner, (head, head.data))
        require_no_external_users(cast(Any, bpy.context.scene), transaction.owner, (head.data,))
    axes = _point_fields(transaction, head, rig)
    graph = transaction.track(source.copy())
    values = {s.identifier: get_input(mod, s.identifier) for s in source.interface.items_tree
              if s.item_type == "SOCKET" and s.in_out == "INPUT" and hasattr(s, "default_value")}
    active_uv = next(layer.name for layer in head.data.uv_layers if layer.active_render)
    scales, shrink, front = _update_graph(graph, active_uv, axes)

    def bind(group):
        mod.node_group = group
        for name, value in values.items():
            set_input(mod, name, value)

    transaction.on_rollback(lambda: bind(source))
    bind(graph)
    # Preserve all four existing key positions. The newly extended fifth
    # detent belongs to the whole face and must pass through unmodified pupils.
    for label in ("左眼表情", "右眼表情"):
        path = input_path(mod, graph.interface.items_tree[label].identifier)
        curve = head.animation_data.drivers.find(path)
        if not curve or curve.driver.type != "SCRIPTED":
            raise ValueError("找不到已有的眼睛表情驱动：" + label)
        d = curve.driver
        old = d.expression
        prefix = "max(0,floor(-emote/"
        if "min(3," + prefix in old:
            transaction.on_rollback(lambda d=d, old=old: setattr(d, "expression", old))
            d.expression = old.replace("min(3," + prefix, "min(4," + prefix)
        elif "min(4," + prefix not in old:
            raise ValueError("眼睛表情档位驱动已自定义：" + label)

    def driver(socket):
        path = input_path(mod, socket)
        curve = head.driver_add(path)
        transaction.on_rollback(lambda: head.driver_remove(path))
        return curve.driver

    for side in ("L", "R"):
        for direction, index in (("宽度", 0), ("高度", 2)):
            d = driver(scales[side, direction])
            for name, bone in (("eye", side + ".眼睛"), ("both", "眼睛")):
                var = d.variables.new()
                var.name, var.type = name, "SINGLE_PROP"
                target = var.targets[0]
                target.id, target.data_path = rig, rig.pose.bones[bone].path_from_id("scale") + f"[{index}]"
            d.expression = "both * eye"
    d = driver(shrink)
    var = d.variables.new()
    var.name, var.type = "original", "SINGLE_PROP"
    target = var.targets[0]
    target.id_type, target.id = "KEY", head.data.shape_keys
    target.data_path = head.data.shape_keys.key_blocks["T0::缩小眼睛"].path_from_id("value")
    d.expression = var.name
    d = driver(front)
    var = d.variables.new()
    var.name, var.type = "brow", "TRANSFORMS"
    target = var.targets[0]
    target.id, target.bone_target = rig, BROW_SWITCH
    target.transform_type, target.transform_space = "LOC_Z", "LOCAL_SPACE"
    d.expression = f"brow <= {BROW_THRESHOLD:.9g}"
    graph["bp_pupil_scaling"] = 1
