"""Keep the original painted eyes from showing through a closed alternate head."""

from typing import cast

import bpy
from mathutils import Vector

from ...util.geometry_nodes import get_input, input_path, set_input

_REGION = "BP_AlternateLidRegion"
_SKIN_UV = "V6_CurrentSkinUV"
_LID_UV = "Human247_LidCover_CurrentUV"
_STATIC_UV = "Human247_StaticBrow_CurrentUV"


def _mark(transaction, rig, head):
    mesh = head.data
    if _REGION in mesh.attributes:
        raise ValueError("八型头部已有同名闭眼区域：" + _REGION)
    marker = mesh.attributes.new(_REGION, "INT", "FACE")
    transaction.on_rollback(lambda: mesh.attributes.remove(mesh.attributes[_REGION]))
    transform = rig.matrix_world.inverted() @ head.matrix_world
    coords = [transform @ p.co for p in mesh.shape_keys.reference_key.data]
    attrs = mesh.attributes
    variants = attrs["V3_HeadVariant"]
    for face in mesh.polygons:
        v = variants.data[face.index].value
        if not 1 <= v <= 6 or attrs["BP_BrowSurface"].data[face.index].value:
            continue
        pts = [coords[i] for i in face.vertices]
        center = sum(pts, Vector())/len(pts)
        # Camera-facing skin tiles and native lids only. The mask excludes
        # both side walls, the outer head layer and every raw T7 half.
        if max(p.y for p in pts)-min(p.y for p in pts) > .015 or center.y > -.49:
            continue
        upper = attrs["Human247_LidCover_Upper"].data[face.index].value
        uv = mesh.uv_layers[f"V3_UV_{v}"].data
        x, y = sum((uv[j].uv for j in face.loop_indices), Vector((0, 0))) * (64/len(pts))
        in_default_eyes = (9 <= x < 11 or 13 <= x < 15) and 49 <= y < 52
        if upper or in_default_eyes:
            marker.data[face.index].value = (1 if upper else 2) * (1 if center.x > 0 else -1)
        elif int(x) in (8, 11, 12, 15) and 49 <= y < 54:
            # These HD faces extend up to .01 texel beyond the side of the
            # eye window. Remember their own column before eye filtering.
            xs = [uv[j].uv.x * 64 for j in face.loop_indices]
            if max(xs)-min(xs) < 1.09:
                marker.data[face.index].value = int(x) * (1 if center.x > 0 else -1)


def preserve_shared_skin(transaction, rig, objects):
    """A skin polygon spanning both eyes must keep its UVs during either blink."""
    head, = [obj for obj in objects if obj.type == "MESH" and "V3_HeadVariant" in obj.data.attributes]
    mesh = head.data
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned
        require_owned(transaction.owner, (head, mesh))
        require_no_external_users(cast(bpy.types.Scene, bpy.context.scene), transaction.owner, (mesh,))
    transform = rig.matrix_world.inverted() @ head.matrix_world
    positions = [(transform @ vertex.co).x for vertex in mesh.shape_keys.reference_key.data]
    regions = mesh.attributes[_REGION].data
    shared = {}
    for face in mesh.polygons:
        role = regions[face.index].value
        if role:
            xs = [positions[index] for index in face.vertices]
            if min(xs) < -.001 and max(xs) > .001:
                shared[face.index] = role

    def restore():
        for index, role in shared.items():
            mesh.attributes[_REGION].data[index].value = role
        mesh.update()

    transaction.on_rollback(restore)
    for index in shared:
        regions[index].value = 0
    mesh.update()


def _graph(group):
    nodes, link = group.nodes, group.links.new
    source, = [n for n in nodes if n.type == "GROUP_INPUT"]
    output, = [n for n in nodes if n.type == "GROUP_OUTPUT" and n.is_active_output]
    geometry = output.inputs[0].links[0].from_socket

    def attr(name, kind):
        n = nodes.new("GeometryNodeInputNamedAttribute")
        n.data_type = kind
        n.inputs["Name"].default_value = name
        return n.outputs["Attribute"]

    def math(op, a, b):
        n = nodes.new("ShaderNodeMath")
        n.operation = op
        link(a, n.inputs[0])
        if isinstance(b, (int, float)):
            n.inputs[1].default_value = b
        else:
            link(b, n.inputs[1])
        return n.outputs[0]

    role = attr(_REGION, "INT")
    left = math("GREATER_THAN", role, 0)
    closed = nodes.new("GeometryNodeSwitch")
    closed.input_type = "BOOLEAN"
    link(left, closed.inputs["Switch"])
    channels = []
    for label, pin in (("左", "True"), ("右", "False")):
        socket = group.interface.new_socket(name=label+"眼已闭合", in_out="INPUT", socket_type="NodeSocketBool")
        channels.append(socket.identifier)
        link(source.outputs[socket.identifier], closed.inputs[pin])
    which = math("ABSOLUTE", role, 0)
    upper = math("MULTIPLY", math("COMPARE", which, 1), closed.outputs[0])
    skin = math("MULTIPLY", math("COMPARE", which, 2), closed.outputs[0])

    for name, mask in ((_LID_UV, upper), (_SKIN_UV, skin), (_STATIC_UV, skin)):
        original = attr(name, "FLOAT_VECTOR")
        component = nodes.new("ShaderNodeSeparateXYZ")
        link(original, component.inputs[0])
        clean = nodes.new("ShaderNodeCombineXYZ")
        link(component.outputs["X"], clean.inputs["X"])
        # Use the same side's own lower cheek texel as the default head's
        # native blink. The original skin image and the open pose stay intact.
        clean.inputs["Y"].default_value = 48.5/64
        store = nodes.new("GeometryNodeStoreNamedAttribute")
        store.domain, store.data_type = "CORNER", "FLOAT_VECTOR"
        store.inputs["Name"].default_value = name
        link(geometry, store.inputs["Geometry"])
        link(mask, store.inputs["Selection"])
        link(clean.outputs[0], store.inputs["Value"])
        geometry = store.outputs["Geometry"]
    adjacent = math("MULTIPLY", math("GREATER_THAN", which, 7), closed.outputs[0])
    original = nodes.new("ShaderNodeSeparateXYZ")
    link(attr(_SKIN_UV, "FLOAT_VECTOR"), original.inputs[0])
    x = math("MULTIPLY", original.outputs["X"], 64)
    x = math("MAXIMUM", x, math("ADD", which, .005))
    x = math("MINIMUM", x, math("ADD", which, .995))
    safe = nodes.new("ShaderNodeCombineXYZ")
    link(math("MULTIPLY", x, 1/64), safe.inputs["X"])
    link(original.outputs["Y"], safe.inputs["Y"])
    store = nodes.new("GeometryNodeStoreNamedAttribute")
    store.domain, store.data_type = "CORNER", "FLOAT_VECTOR"
    store.inputs["Name"].default_value = _SKIN_UV
    link(geometry, store.inputs["Geometry"])
    link(adjacent, store.inputs["Selection"])
    link(safe.outputs[0], store.inputs["Value"])
    geometry = store.outputs["Geometry"]
    link(geometry, output.inputs[0])
    return channels


def install(transaction, rig, head):
    _mark(transaction, rig, head)
    mod, = [m for m in head.modifiers if m.type == "NODES" and "单眼原皮侧" in m.node_group.interface.items_tree]
    original = mod.node_group
    group = transaction.track(original.copy())
    values = {s.identifier: get_input(mod, s.identifier) for s in original.interface.items_tree
              if s.item_type == "SOCKET" and s.in_out == "INPUT" and hasattr(s, "default_value")}
    sockets = _graph(group)

    def assign(tree):
        mod.node_group = tree
        for name, value in values.items():
            set_input(mod, name, value)

    transaction.on_rollback(lambda: assign(original))
    assign(group)
    for name, socket in zip(("左", "右"), sockets, strict=True):
        path = input_path(mod, socket)
        driver = head.driver_add(path).driver
        transaction.on_rollback(lambda p=path: head.driver_remove(p))
        var = driver.variables.new()
        var.name, var.type = "blink", "SINGLE_PROP"
        target = var.targets[0]
        target.id_type, target.id = "KEY", head.data.shape_keys
        target.data_path = head.data.shape_keys.key_blocks[f"T0::闭{name}眼"].path_from_id("value")
        driver.expression = "blink >= 0.999"


def _pack_middle_eye_region(transaction, head):
    """Carry the face role in the unused Z component of the existing 2D skin UV."""
    mod = head.modifiers["白珀 · 黑脸渐变"]
    old = mod.node_group
    group = transaction.track(old.copy())
    transaction.on_rollback(lambda: setattr(mod, "node_group", old))
    mod.node_group = group
    nodes, link = group.nodes, group.links.new
    output, = [n for n in nodes if n.type == "GROUP_OUTPUT" and n.is_active_output]
    geometry = output.inputs[0].links[0].from_socket

    def field(name, kind):
        n = nodes.new("GeometryNodeInputNamedAttribute")
        n.data_type = kind
        n.inputs["Name"].default_value = name
        return n.outputs["Attribute"]

    def math(op, a, b):
        n = nodes.new("ShaderNodeMath")
        n.operation = op
        link(a, n.inputs[0])
        if isinstance(b, (int, float)):
            n.inputs[1].default_value = b
        else:
            link(b, n.inputs[1])
        return n.outputs[0]

    eligible = math("COMPARE", field("V3_HeadVariant", "INT"), 5)
    for name in ("BP_ClosedBrow", "BP_RawBrowMain", "BP_RawBrowSecond"):
        eligible = math("MULTIPLY", eligible, math("LESS_THAN", field(name, "BOOLEAN"), .5))
    role = math("MULTIPLY", eligible, math("ADD", field("BP_BrowSurface", "FLOAT"), 1))
    original = nodes.new("ShaderNodeSeparateXYZ")
    link(field(_SKIN_UV, "FLOAT_VECTOR"), original.inputs[0])
    uv = nodes.new("ShaderNodeCombineXYZ")
    for axis in ("X", "Y"):
        link(original.outputs[axis], uv.inputs[axis])
    link(role, uv.inputs["Z"])
    store = nodes.new("GeometryNodeStoreNamedAttribute")
    store.domain, store.data_type = "CORNER", "FLOAT_VECTOR"
    store.inputs["Name"].default_value = _SKIN_UV
    link(uv.outputs[0], store.inputs["Value"])
    link(geometry, store.inputs["Geometry"])
    link(store.outputs[0], output.inputs[0])


def refine_middle_eye_uv(transaction, objects):
    """Anchor the T5 skin outside the one-pixel eye row during a native blink."""
    from .human_source import SKIN_ROLE
    head, = [o for o in objects if o.type == "MESH" and "V3_HeadVariant" in o.data.attributes]
    material, = {slot.material for slot in head.material_slots
                 if slot.material and slot.material.get(SKIN_ROLE) == "skin"}
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned
        require_owned(transaction.owner, (head, material))
        require_no_external_users(cast(bpy.types.Scene, bpy.context.scene), transaction.owner, (material,))
    _pack_middle_eye_region(transaction, head)
    tree = material.node_tree
    tex, = [n for n in tree.nodes if n.type == "TEX_IMAGE"]
    vector = tex.inputs["Vector"]
    original = vector.links[0].from_socket
    group = transaction.track(bpy.data.node_groups.new("中位小眼 · 闭眼皮肤坐标", "ShaderNodeTree"))
    group.interface.new_socket(name="原 UV", in_out="INPUT", socket_type="NodeSocketVector")
    for side in ("左", "右"):
        group.interface.new_socket(name=side+"眼闭合", in_out="INPUT", socket_type="NodeSocketFloat")
    group.interface.new_socket(name="UV", in_out="OUTPUT", socket_type="NodeSocketVector")
    nodes, link = group.nodes, group.links.new
    inputs = nodes.new("NodeGroupInput")
    output = nodes.new("NodeGroupOutput")

    def attribute(name, socket):
        node = nodes.new("ShaderNodeAttribute")
        node.attribute_name = name
        return node.outputs[socket]

    def calc(operation, a, b):
        node = nodes.new("ShaderNodeMath")
        node.operation = operation
        link(a, node.inputs[0])
        if isinstance(b, (int, float)):
            node.inputs[1].default_value = b
        else:
            link(b, node.inputs[1])
        return node.outputs[0]

    face = nodes.new("ShaderNodeSeparateXYZ")
    link(attribute("BP_FaceShadowCoordinates", "Vector"), face.inputs[0])
    x, y, front = face.outputs
    # 8x8 face at (8,8) in a 64x64 Minecraft skin; T5 has eyes on row 4.
    uv = nodes.new("ShaderNodeCombineXYZ")
    link(calc("ADD", calc("MULTIPLY", x, .125), 12/64), uv.inputs["X"])
    link(calc("ADD", calc("MULTIPLY", y, .125), 52/64), uv.inputs["Y"])
    left = calc("GREATER_THAN", x, 0)
    blink = nodes.new("ShaderNodeMixRGB")
    link(left, blink.inputs[0])
    link(inputs.outputs["右眼闭合"], blink.inputs[1])
    link(inputs.outputs["左眼闭合"], blink.inputs[2])
    packed = nodes.new("ShaderNodeSeparateXYZ")
    link(attribute(_SKIN_UV, "Vector"), packed.inputs[0])
    role = packed.outputs["Z"]
    use = calc("MULTIPLY", calc("GREATER_THAN", role, .5), front)
    ax = calc("ABSOLUTE", x, 0)
    eye = calc("MULTIPLY", calc("LESS_THAN", ax, .375), calc("GREATER_THAN", ax, .125))
    brow = calc("MULTIPLY", eye, calc("GREATER_THAN", role, 1.5))
    use = calc("MULTIPLY", use, calc("LESS_THAN", brow, .5))
    eye = calc("MULTIPLY", eye, calc("LESS_THAN", y, 0))
    eye = calc("MULTIPLY", eye, calc("GREATER_THAN", y, -.125))
    closed_eye = calc("MULTIPLY", eye, calc("GREATER_THAN", blink.outputs[0], .999))
    paint = nodes.new("ShaderNodeCombineXYZ")
    link(uv.inputs["X"].links[0].from_socket, paint.inputs["X"])
    paint.inputs["Y"].default_value = 52.5 / 64
    projected = nodes.new("ShaderNodeMixRGB")
    link(closed_eye, projected.inputs[0])
    link(uv.outputs[0], projected.inputs[1])
    link(paint.outputs[0], projected.inputs[2])
    use = calc("MULTIPLY", use, calc("MAXIMUM", closed_eye, calc("LESS_THAN", eye, .5)))
    mix = nodes.new("ShaderNodeMixRGB")
    link(use, mix.inputs[0])
    link(inputs.outputs["原 UV"], mix.inputs[1])
    link(projected.outputs[0], mix.inputs[2])
    link(mix.outputs[0], output.inputs["UV"])
    from ...util.node_layout import arrange_shader
    arrange_shader(group)
    node = tree.nodes.new("ShaderNodeGroup")
    transaction.on_rollback(lambda: tree.nodes.remove(node))
    node.node_tree = group
    node.label = "中位小眼 · 固定眼外肤色"
    node.location = (tex.location.x - 220, tex.location.y - 150)
    tree.links.new(original, node.inputs["原 UV"])
    transaction.on_rollback(lambda: tree.links.new(original, vector))
    tree.links.new(node.outputs["UV"], vector)
    keys = head.data.shape_keys
    for side in ("左", "右"):
        socket = node.inputs[side+"眼闭合"]
        driver = socket.driver_add("default_value").driver
        transaction.on_rollback(lambda s=socket: s.driver_remove("default_value"))
        driver.type = "AVERAGE"
        target = driver.variables.new().targets[0]
        target.id_type, target.id = "KEY", keys
        target.data_path = keys.key_blocks[f"T5::闭{side}眼"].path_from_id("value")
