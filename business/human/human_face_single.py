"""A native three-position skin selector for BaiPo's eight-head mesh."""

from typing import Any, cast

import bpy
from mathutils import Vector

from ...util.geometry_nodes import get_input, input_path, set_input
from .human_face_controls import _place
from .human_face_switches import _edit

SWITCH = "单眼皮肤"
TRACK = "单眼皮肤·轨道"
_SIDE = "BP_SkinFaceHalf"
_EYE_SAMPLE = "BP_SingleEyeSample"
_VARIANT = "V3_HeadVariant"
_CROSSING = 2


def threshold(rig):
    return rig.pose.bones[SWITCH].constraints[0].max_x / 2


def _slider(transaction, rig, thumb):
    to_rig = rig.matrix_world.inverted() @ thumb.matrix_world
    corners = [to_rig @ vertex.co for vertex in thumb.data.vertices]
    left, right = min(p.x for p in corners), max(p.x for p in corners)
    bottom = min(p.z for p in corners)
    width = right - left
    height = width * .19
    button = height * .82
    depth = sum(p.y for p in corners) / len(corners)
    center = Vector(((left + right) / 2, depth, bottom - height * .68))
    stroke = (width - button) / 2 - width * .012
    if width < 1e-4 or any(name in rig.data.bones for name in (TRACK, SWITCH)):
        raise ValueError("单眼滑轨与现有人模控制器冲突：" + rig.name)

    def remove():
        with _edit(rig) as bones:
            for name in (SWITCH, TRACK):
                if name in bones:
                    bones.remove(bones[name])

    transaction.on_rollback(remove)
    with _edit(rig) as bones:
        for name in (TRACK, SWITCH):
            bone = bones.new(name)
            bone.head = center
            bone.tail = center + Vector((0, height, 0))
            bone.align_roll(Vector((0, 0, 1)))
            bone.use_deform = False
            bone.parent = bones["头"]
            for collection in bones["面部兼容"].collections:
                collection.assign(bone)

    reference = rig.pose.bones["L.眉纹切换"]
    rail, handle = (rig.pose.bones[n] for n in (TRACK, SWITCH))
    for pb, w, h in ((rail, width, height), (handle, button, button)):
        pb.custom_shape = reference.custom_shape
        pb.use_custom_shape_bone_size = False
        _place(transaction, pb, center, w, h, reference.color)
        pb.custom_shape_wire_width = reference.custom_shape_wire_width
        pb.lock_rotation = pb.lock_scale = (True, True, True)
        pb.lock_rotation_w = True
    rail.bone.hide_select = True
    rail.lock_location = (True, True, True)
    handle.lock_location = (False, True, True)
    limit = handle.constraints.new("LIMIT_LOCATION")
    limit.name = "单眼皮肤 · 左中右"
    limit.owner_space = "LOCAL"
    limit.use_transform_limit = True
    for axis in "xyz":
        setattr(limit, "use_min_" + axis, True)
        setattr(limit, "use_max_" + axis, True)
    limit.min_x, limit.max_x = -stroke, stroke


def _drive(transaction, owner, path, rig, expression):
    from .human_armor import animated

    if animated(owner, path):
        raise ValueError("单眼控制与已有动画或驱动冲突：" + path)
    curve = owner.driver_add(path)
    transaction.on_rollback(lambda: owner.driver_remove(path))
    driver = curve.driver
    var = driver.variables.new()
    var.name, var.type = "slide", "TRANSFORMS"
    target = var.targets[0]
    target.id, target.bone_target = rig, SWITCH
    target.transform_type, target.transform_space = "LOC_X", "LOCAL_SPACE"
    driver.expression = expression


def _face_halves(transaction, rig, head):
    mesh = head.data
    variants = mesh.attributes[_VARIANT]
    if any(name in mesh.attributes for name in (_SIDE, _EYE_SAMPLE)) or variants.domain != "FACE":
        raise ValueError("人模头型网格与单眼皮肤标识冲突：" + head.name)
    matrix = rig.matrix_world.inverted() @ head.matrix_world
    x = [(matrix @ v.co).x for v in mesh.vertices]
    # Measure the symmetry plane from the flat, unanimated T7 front.
    vanilla = [x[i] for p in mesh.polygons if variants.data[p.index].value == 7 for i in p.vertices]
    if not vanilla:
        raise ValueError("八型头部缺少原皮面")
    width = max(vanilla) - min(vanilla)
    mid = (max(vanilla) + min(vanilla)) / 2
    sides = []
    eye_samples = []
    for face in mesh.polygons:
        variant = variants.data[face.index].value
        coords = [x[i] - mid for i in face.vertices]
        if variant < 0:
            sides.append(0)  # The shared outer shell belongs to both states.
        elif min(coords) < -width * .02 and max(coords) > width * .02:
            # Full-width eye backplates are split only while one eye is disabled.
            sides.append(_CROSSING)
        else:
            sides.append(1 if sum(coords) > 0 else -1)
        # Some authored eye whites are one quad sampling a single pixel for
        # both eyes. Other forms also have small eye tiles mapped across the
        # nose. Keep the nose seam, eyebrows outside the eye window and T7
        # out of the correction. Full-resolution coordinates are tested below.
        eye_tile = False
        if 0 <= variant < 7 and min(abs(v) for v in coords) > width * .04:
            uv = mesh.attributes[f"V3_UV_{variant}"]
            samples = [uv.data[i].vector for i in face.loop_indices]
            eye_tile = all(8/64 <= p.x <= 16/64 and 1-16/64 <= p.y <= 1-10/64 for p in samples)
        eye_samples.append(sides[-1] == _CROSSING or eye_tile)
    attr = mesh.attributes.new(_SIDE, "INT", "FACE")
    transaction.on_rollback(lambda: mesh.attributes.remove(mesh.attributes[_SIDE]))
    attr.data.foreach_set("value", sides)
    eye_attr = mesh.attributes.new(_EYE_SAMPLE, "BOOLEAN", "FACE")
    transaction.on_rollback(lambda: mesh.attributes.remove(mesh.attributes[_EYE_SAMPLE]))
    eye_attr.data.foreach_set("value", eye_samples)
    return tuple(matrix.to_3x3().row[0]), mid - matrix.translation.x


def _sclera_uv(nodes, link, geometry, state):
    """Keep the live eye's texture reads on its own side of the skin front."""
    def attribute(name, kind):
        reader = nodes.new("GeometryNodeInputNamedAttribute")
        reader.data_type = kind
        reader.inputs["Name"].default_value = name
        return reader.outputs["Attribute"]

    uv_name = "V6_CurrentSkinUV"
    uv = attribute(uv_name, "FLOAT_VECTOR")
    split = nodes.new("ShaderNodeSeparateXYZ")
    link(uv, split.inputs[0])
    # The skin's front is [8,16]/64 in both 64px and HD atlases. Fold the
    # authored sample about its centre only when it points into the eyepatch.
    offset = nodes.new("ShaderNodeMath")
    offset.operation = "SUBTRACT"
    link(split.outputs["X"], offset.inputs[0])
    offset.inputs[1].default_value = 12/64
    side = nodes.new("ShaderNodeMath")
    side.operation = "MULTIPLY"
    link(offset.outputs[0], side.inputs[0])
    link(state, side.inputs[1])
    opposite = nodes.new("ShaderNodeMath")
    opposite.operation = "GREATER_THAN"
    link(side.outputs[0], opposite.inputs[0])
    opposite.inputs[1].default_value = 0
    selection = nodes.new("FunctionNodeBooleanMath")
    selection.operation = "AND"
    link(attribute(_EYE_SAMPLE, "BOOLEAN"), selection.inputs[0])
    link(opposite.outputs[0], selection.inputs[1])
    mirror = nodes.new("ShaderNodeVectorMath")
    mirror.operation = "MULTIPLY_ADD"
    link(uv, mirror.inputs[0])
    mirror.inputs[1].default_value = (-1, 1, 1)
    mirror.inputs[2].default_value = (24/64, 0, 0)
    store = nodes.new("GeometryNodeStoreNamedAttribute")
    store.data_type, store.domain = "FLOAT_VECTOR", "CORNER"
    store.label = "活动眼从本侧皮肤取色"
    store.inputs["Name"].default_value = uv_name
    link(geometry, store.inputs["Geometry"])
    link(selection.outputs[0], store.inputs["Selection"])
    link(mirror.outputs[0], store.inputs["Value"])
    return store.outputs["Geometry"]


def _native_tree(transaction, original, plane):
    """Reuse T7's own geometry, material attributes and both calibrated UV sets."""
    tree = transaction.track(original.copy())
    tree.name = "白珀人模 · 双眼与原皮单眼"
    # Animation belongs to the nested original. Its copy has no such nodes.
    tree.animation_data_clear()
    tree.nodes.clear()
    state = tree.interface.new_socket(name="单眼原皮侧", in_out="INPUT", socket_type="NodeSocketInt")
    state.min_value, state.max_value = -1, 1
    nodes, link = tree.nodes, tree.links.new
    src, out = nodes.new("NodeGroupInput"), nodes.new("NodeGroupOutput")
    state_socket = src.outputs[state.identifier]

    def branch(vanilla):
        node = nodes.new("GeometryNodeGroup")
        node.node_tree = original
        node.label = "原皮头型" if vanilla else "当前头型"
        for socket in node.inputs:
            if vanilla and socket.name == "头型":
                socket.default_value = 7
            else:
                link(src.outputs[socket.identifier], socket)
        return node.outputs[0]

    regular, skin = branch(False), branch(True)
    side = nodes.new("GeometryNodeInputNamedAttribute")
    side.data_type = "INT"
    side.inputs["Name"].default_value = _SIDE
    side_value = side.outputs["Attribute"]

    def equal(value, other, operation="EQUAL"):
        node = nodes.new("FunctionNodeCompare")
        node.data_type, node.operation = "INT", operation
        a, b = (next(s for s in node.inputs if s.enabled and s.name == name) for name in ("A", "B"))
        link(value, a)
        if isinstance(other, int):
            b.default_value = other
        else:
            link(other, b)
        return node.outputs["Result"]

    def separate(geometry, condition):
        node = nodes.new("GeometryNodeSeparateGeometry")
        node.domain = "FACE"
        link(geometry, node.inputs["Geometry"])
        link(condition, node.inputs["Selection"])
        return node.outputs["Selection"], node.outputs["Inverted"]

    crossing, tiled = separate(regular, equal(side_value, _CROSSING))
    _, active_half = separate(tiled, equal(side_value, state_socket))
    skin_half, _ = separate(skin, equal(side_value, state_socket))

    # Subdividing the detached full-width backplates yields exact half quads,
    # interpolating their UVs. Keeping an entire plate would fill transparent
    # pixels of an asymmetrical skin from behind the disabled side.
    subdivide = nodes.new("GeometryNodeSubdivideMesh")
    subdivide.inputs["Level"].default_value = 1
    link(crossing, subdivide.inputs["Mesh"])
    dot = nodes.new("ShaderNodeVectorMath")
    dot.operation = "DOT_PRODUCT"
    link(nodes.new("GeometryNodeInputPosition").outputs[0], dot.inputs[0])
    dot.inputs[1].default_value = plane[0]
    positive = nodes.new("ShaderNodeMath")
    positive.operation = "GREATER_THAN"
    link(dot.outputs["Value"], positive.inputs[0])
    positive.inputs[1].default_value = plane[1]
    plate_side = nodes.new("GeometryNodeSwitch")
    plate_side.input_type = "INT"
    link(positive.outputs[0], plate_side.inputs["Switch"])
    plate_side.inputs["False"].default_value = -1
    plate_side.inputs["True"].default_value = 1
    _, active_plate = separate(subdivide.outputs["Mesh"], equal(plate_side.outputs[0], state_socket))

    active = nodes.new("GeometryNodeJoinGeometry")
    link(active_half, active.inputs[0])
    link(active_plate, active.inputs[0])
    join = nodes.new("GeometryNodeJoinGeometry")
    link(_sclera_uv(nodes, link, active.outputs[0], state_socket), join.inputs[0])
    link(skin_half, join.inputs[0])
    enabled = nodes.new("FunctionNodeBooleanMath")
    enabled.operation = "AND"
    link(equal(state_socket, 0, "NOT_EQUAL"), enabled.inputs[0])
    link(equal(src.outputs["头型"], 7, "NOT_EQUAL"), enabled.inputs[1])
    result = nodes.new("GeometryNodeSwitch")
    result.input_type = "GEOMETRY"
    link(enabled.outputs[0], result.inputs["Switch"])
    link(regular, result.inputs["False"])
    link(join.outputs[0], result.inputs["True"])
    link(result.outputs[0], out.inputs[0])
    return tree, state.identifier


def install(transaction, rig, objects):
    heads = [o for o in objects if o.type == "MESH" and _VARIANT in o.data.attributes]
    if not heads:
        return  # Creature skins do not have the human T0–T7 atlas/layout.
    heads = [o for o in heads if any(m.type == "NODES" and m.node_group and
             "头型" in m.node_group.interface.items_tree for m in o.modifiers)]
    thumbs = [o for o in objects if o.type == "MESH" and any(
        s.material and "面部兼容图片" in s.material.name for s in o.material_slots)]
    if len(heads) != 1 or len(thumbs) != 1:
        raise ValueError("单眼皮肤需要唯一的八型头部和原生头型示意图：" + rig.name)
    head = heads[0]
    mod, = [m for m in head.modifiers if m.type == "NODES" and m.node_group and
            "头型" in m.node_group.interface.items_tree]
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned

        require_owned(transaction.owner, [head, head.data, rig, rig.data])
        require_no_external_users(cast(Any, bpy.context.scene), transaction.owner, [head.data, rig.data])
    _slider(transaction, rig, thumbs[0])
    plane = _face_halves(transaction, rig, head)
    original = mod.node_group
    values = {s.identifier: get_input(mod, s.identifier) for s in original.interface.items_tree
              if s.item_type == "SOCKET" and s.in_out == "INPUT" and s.socket_type != "NodeSocketGeometry"}
    tree, socket = _native_tree(transaction, original, plane)

    def bind(group):
        mod.node_group = group
        for name, value in values.items():
            set_input(mod, name, value)

    transaction.on_rollback(lambda: bind(original))
    bind(tree)
    half = threshold(rig)
    _drive(transaction, head, input_path(mod, socket), rig,
           f"1 if slide >= {half:.9g} else (-1 if slide <= {-half:.9g} else 0)")
    for prefix, condition in (("R.", f"slide <= {-half:.9g}"), ("L.", f"slide >= {half:.9g}")):
        for name in ("眼睛", "眼皮", "眉毛", "下眼皮", "眉纹切换"):
            pose = rig.pose.bones[prefix + name]
            bone = pose if hasattr(pose, "hide") else pose.bone
            if not bone.hide:
                transaction.on_rollback(lambda b=bone: setattr(b, "hide", False))
                _drive(transaction, bone.id_data, bone.path_from_id("hide"), rig, condition)
