"""Fit the closed-eye strips to the native eye window of each human head."""

from mathutils import Vector

from ...util.geometry_nodes import get_input, input_path, set_input
from .human_face_strips import FIELDS

_UV64 = "BP_ClosedBrowUV64"
_UV128 = "BP_ClosedBrowUV128"
_CLOSED = "BP_ClosedBrow"
_LAYOUT = "采用内置皮肤眉位"
_MODIFIER = "白珀 · 主眉与副眉"


def set_skin_layout(root, *, builtin, changed=None, fresh=()):
    """Custom skins follow the authored UV of the eye type selected by the player."""
    from ...util.ownership import require_owned

    heads = [o for o in root.all_objects if o.type == "MESH" and _UV64 in o.data.attributes]
    if not heads:
        return  # Existing native heads from before the alternate brow UVs.
    head, = heads
    if head not in fresh:
        require_owned(root, (head,))
    mod = head.modifiers[_MODIFIER]
    layout = mod.node_group.interface.items_tree.get(_LAYOUT)
    if layout is None:
        return
    key = layout.identifier
    current = get_input(mod, key)
    if current != builtin:
        if changed is not None:
            changed.append(((mod, key), current))
        set_input(mod, key, builtin)


def scale_closed_strips(tree, geometry, growth):
    """Scale both strips around one evaluated pivot per eye, after head selection."""
    nodes, link = tree.nodes, tree.links.new

    def flag(name):
        n = nodes.new("GeometryNodeInputNamedAttribute")
        n.data_type = "BOOLEAN"
        n.inputs["Name"].default_value = name
        return n.outputs["Attribute"]

    def split(source, mask):
        n = nodes.new("GeometryNodeSeparateGeometry")
        n.domain = "FACE"
        link(source, n.inputs["Geometry"])
        link(mask, n.inputs["Selection"])
        return n.outputs["Selection"], n.outputs["Inverted"]

    closed, left = flag(_CLOSED), flag("BP_BrowLeft")
    strips, _ = split(geometry, closed)
    left_strips, right_strips = split(strips, left)
    pivot = nodes.new("GeometryNodeSwitch")
    pivot.input_type = "VECTOR"
    link(left, pivot.inputs["Switch"])
    for part, pin in ((left_strips, "True"), (right_strips, "False")):
        box = nodes.new("GeometryNodeBoundBox")
        link(part, box.inputs["Geometry"])
        center = nodes.new("ShaderNodeVectorMath")
        center.operation = "MULTIPLY_ADD"
        # Centre = min + (max - min)/2, also valid on a rotated brow.
        extent = nodes.new("ShaderNodeVectorMath")
        extent.operation = "SUBTRACT"
        link(box.outputs["Max"], extent.inputs[0])
        link(box.outputs["Min"], extent.inputs[1])
        link(extent.outputs[0], center.inputs[0])
        center.inputs[1].default_value = (.5, .5, .5)
        link(box.outputs["Min"], center.inputs[2])
        link(center.outputs[0], pivot.inputs[pin])
    radius = nodes.new("ShaderNodeVectorMath")
    radius.operation = "SUBTRACT"
    link(nodes.new("GeometryNodeInputPosition").outputs[0], radius.inputs[0])
    link(pivot.outputs[0], radius.inputs[1])
    move = nodes.new("GeometryNodeSetPosition")
    link(geometry, move.inputs["Geometry"])
    link(closed, move.inputs["Selection"])
    link(growth(radius.outputs[0]), move.inputs["Offset"])
    return move.outputs["Geometry"]


def _bounds(points):
    return [(min(p[i] for p in points), max(p[i] for p in points)) for i in (0, 2)]


def _frame(points):
    (x0, x1), (z0, z1) = _bounds(points)
    return Vector(((x0+x1)/2, (z0+z1)/2)), Vector((x1-x0, z1-z0))


def _variant(mesh, to_rig, variant, side, faces):
    basis = mesh.shape_keys.reference_key
    keys = mesh.shape_keys.key_blocks
    shell = keys[f"头型状态 {variant} · 原壳体位置"] if variant else basis
    label = "左" if side == 1 else "右"
    blink, = [k for k in keys if k.name.startswith(f"T{variant}::闭{label}")]
    role = mesh.attributes["BP_EyeSpriteFace"]
    window = [to_rig @ shell.data[i].co for f in faces if role.data[f.index].value == 2*side for i in f.vertices]
    if not window:
        raise ValueError(f"T{variant} 缺少{label}眼窗")
    brows = [f for f in faces if mesh.attributes["BP_BrowSurface"].data[f.index].value
             and side*(to_rig @ basis.data[f.vertices[0]].co).x > 0]
    ids = {i for f in brows for i in f.vertices}
    if not brows or not all(mesh.attributes["BP_BrowDetached"].data[i].value for i in ids):
        raise ValueError(f"T{variant} {label}眉片未独立，无法安全控制")
    # Full blink is additive to the variant shell. Fit there so that moving
    # the native eyelid or the independent brow control still moves the strips.
    closed = {i: shell.data[i].co + (blink.data[i].co - basis.data[i].co) for i in ids}
    shape = {name: {i: to_rig @ (p + mesh.attributes[name].data[i].vector) for i, p in closed.items()}
             for name in FIELDS[:2]}
    return _frame(window), _frame([p for points in shape.values() for p in points.values()])[0], shape, brows


def _fit_geometry(transaction, rig, head):
    mesh = head.data
    to_rig = rig.matrix_world.inverted() @ head.matrix_world
    inverse = to_rig.to_3x3().inverted()
    data = mesh.attributes
    for name in (_UV64, _UV128):
        if name in data:
            raise ValueError("已存在同名闭眼眉纹属性：" + name)
        data.new(name, "FLOAT_VECTOR", "CORNER")
        transaction.on_rollback(lambda n=name: data.remove(data[n]))

    original = {name: {} for name in FIELDS[:2]}

    def restore():
        for name, coords in original.items():
            for index, value in coords.items():
                data[name].data[index].vector = value

    transaction.on_rollback(restore)
    face_sets = [[] for _ in range(7)]
    for face in mesh.polygons:
        variant = data["V3_HeadVariant"].data[face.index].value
        if 0 <= variant < len(face_sets):
            face_sets[variant].append(face)

    for side in (1, -1):
        reference = _variant(mesh, to_rig, 0, side, face_sets[0])
        (anchor, size), middle, _, base_faces = reference
        relative = middle - anchor
        for variant, faces in enumerate(face_sets):
            (center, span), current, shape, brows = reference if variant == 0 else _variant(mesh, to_rig, variant, side, faces)
            _copy_brow_uv(mesh, to_rig, base_faces, brows)
            if variant == 0:
                continue
            ratio = min(span.x/size.x, span.y/size.y)
            target = center + relative * ratio
            for name, points in shape.items():
                for i, point in points.items():
                    old = data[name].data[i].vector.copy()
                    at = Vector((point.x, point.z))
                    delta = target + (at-current) * ratio - at
                    original[name][i] = old
                    data[name].data[i].vector = old + inverse @ Vector((delta.x, 0, delta.y))


def _copy_brow_uv(mesh, to_rig, original, dest):
    # Preserve the two separately painted texels (and their HD subpixels).
    # Match face and corner positions, not polygon indices or winding order.
    basis = mesh.shape_keys.reference_key

    def points(face):
        return [to_rig @ basis.data[i].co for i in face.vertices]

    def column(face):
        return sum(p.x for p in points(face))/len(face.vertices)

    for source, face in zip(sorted(original, key=column), sorted(dest, key=column), strict=True):
        src, dst = points(source), points(face)
        origin = sum(src, Vector())/len(src)
        center = sum(dst, Vector())/len(dst)
        for index, point in zip(face.loop_indices, dst):
            j = min(range(len(src)), key=lambda c: (src[c]-origin - (point-center)).length_squared)
            loop = source.loop_indices[j]
            mesh.attributes[_UV64].data[index].vector = (*mesh.uv_layers["V3_UV_0"].data[loop].uv, 0.0)
            mesh.attributes[_UV128].data[index].vector = mesh.attributes["Human243_HD_UV_0"].data[loop].vector


def _graph(transaction, rig, head):
    from .human_eye_selector import BROW_SWITCH, BROW_THRESHOLD

    mod = head.modifiers[_MODIFIER]
    original = mod.node_group
    group = transaction.track(original.copy())
    nodes, link = group.nodes, group.links.new
    source, = [n for n in nodes if n.type == "GROUP_INPUT"]
    remove, = [n for n in nodes if n.bl_idname == "GeometryNodeDeleteGeometry"]
    hidden = remove.inputs["Selection"].links[0].from_node
    if hidden.bl_idname != "FunctionNodeBooleanMath" or hidden.operation != "AND":
        raise ValueError("原生主眉显隐节点已自定义：" + head.name)
    split, = [n for n in nodes if n.bl_idname == "GeometryNodeSeparateGeometry"
              and n.inputs["Geometry"].links[0].from_node == source]
    parked = [n for n in nodes if n.bl_idname == "GeometryNodeSwitch" and n.input_type == "GEOMETRY"
              and n.inputs["False"].is_linked]
    if len(parked) != 2:
        raise ValueError("原生眉纹的两层停放位置已自定义：" + head.name)
    option = group.interface.new_socket(name="隐藏普通眉毛", in_out="INPUT", socket_type="NodeSocketBool")
    either = nodes.new("FunctionNodeBooleanMath")
    either.operation = "OR"
    state = hidden.inputs[0].links[0].from_socket
    link(state, either.inputs[0])
    link(source.outputs[option.identifier], either.inputs[1])
    link(either.outputs[0], hidden.inputs[0])
    for selector in parked:
        keep = nodes.new("GeometryNodeSwitch")
        keep.input_type = "GEOMETRY"
        keep.label = "隐藏普通眉毛时同时收起备用眉片"
        link(selector.inputs["False"].links[0].from_socket, keep.inputs["False"])
        link(source.outputs[option.identifier], keep.inputs["Switch"])
        link(keep.outputs[0], selector.inputs["False"])

    # Only the copied strips pass through this split. The ordinary editable
    # brows and the head shell keep their own variant-specific UVs.
    branch = split.outputs["Selection"]
    destinations = [edge.to_socket for edge in branch.links]
    layout = group.interface.new_socket(name=_LAYOUT, in_out="INPUT", socket_type="NodeSocketBool")
    layout.default_value = True
    attributes = []
    for name in (_UV64, _UV128):
        node = nodes.new("GeometryNodeInputNamedAttribute")
        node.data_type = "FLOAT_VECTOR"
        node.inputs["Name"].default_value = name
        attributes.append(node.outputs["Attribute"])
    for variant in range(7):
        for name, kind, uv in ((f"V3_UV_{variant}", "FLOAT2", attributes[0]),
                               (f"Human243_HD_UV_{variant}", "FLOAT_VECTOR", attributes[1])):
            store = nodes.new("GeometryNodeStoreNamedAttribute")
            store.domain, store.data_type = "CORNER", kind
            store.inputs["Name"].default_value = name
            link(branch, store.inputs["Geometry"])
            link(source.outputs[layout.identifier], store.inputs["Selection"])
            link(uv, store.inputs["Value"])
            branch = store.outputs["Geometry"]
    for dest in destinations:
        link(branch, dest)

    # Save the expression-only selection before filtering to the chosen head.
    # Downstream, each eye's two strips can share a single pose-space pivot.
    out, = [node for node in nodes if node.type == "GROUP_OUTPUT" and node.is_active_output]
    mark = nodes.new("GeometryNodeStoreNamedAttribute")
    mark.domain, mark.data_type = "FACE", "BOOLEAN"
    mark.inputs["Name"].default_value = _CLOSED
    link(out.inputs["几何"].links[0].from_socket, mark.inputs["Geometry"])
    link(split.inputs["Selection"].links[0].from_socket, mark.inputs["Selection"])
    link(state, mark.inputs["Value"])
    link(mark.outputs[0], out.inputs["几何"])

    transaction.on_rollback(lambda: setattr(mod, "node_group", original))
    mod.node_group = group
    if transaction.owner is not None:
        from .human_source import skin_image

        root = transaction.owner
        set_input(mod, layout.identifier, skin_image(root) == root.get("oxal_human_default_skin"))
    path = input_path(mod, option.identifier)
    driver = head.driver_add(path).driver
    transaction.on_rollback(lambda: head.driver_remove(path))
    var = driver.variables.new()
    var.name, var.type = "brow", "TRANSFORMS"
    target = var.targets[0]
    target.id, target.bone_target = rig, BROW_SWITCH
    target.transform_type, target.transform_space = "LOC_Z", "LOCAL_SPACE"
    driver.expression = f"brow <= {BROW_THRESHOLD:.9g}"


def install(transaction, rig, objects):
    from . import human_closed_lids

    head, = [o for o in objects if o.type == "MESH" and "V3_HeadVariant" in o.data.attributes]
    if transaction.owner is not None:
        from typing import cast

        import bpy

        from ...util.ownership import require_no_external_users, require_owned

        require_owned(transaction.owner, (head, head.data))
        require_no_external_users(cast(bpy.types.Scene, bpy.context.scene), transaction.owner, (head.data,))
    _fit_geometry(transaction, rig, head)
    _graph(transaction, rig, head)
    human_closed_lids.install(transaction, rig, head)
