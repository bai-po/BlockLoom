"""Native full-face shadow for the fifth expression on the eight-head human."""

from pathlib import Path
from typing import Any, cast

import bpy
from mathutils import Vector

from .human_eye_selector import mode_expression
from .human_source import SKIN_ROLE

_FIELD = "BP_FaceShadowCoordinates"


def _texture(transaction, group, x, y):
    path = Path(__file__).resolve().parents[2] / "asset/human/face_shadow_mask.png"
    image = transaction.track(bpy.data.images.load(str(path), check_existing=False))
    image.name = "白珀 · 黑脸遮罩"
    image.colorspace_settings.name = "Non-Color"
    image.pack()
    image.filepath = "//" + path.name
    nodes, link = group.nodes, group.links.new
    coords = nodes.new("ShaderNodeCombineXYZ")
    coords.location = (-350, -520)
    link(x, coords.inputs["X"])
    link(y, coords.inputs["Y"])
    uv = nodes.new("ShaderNodeVectorMath")
    uv.location, uv.operation = (-150, -520), "ADD"
    uv.inputs[1].default_value = (.5, .5, 0)
    link(coords.outputs[0], uv.inputs[0])
    tex = nodes.new("ShaderNodeTexImage")
    tex.location = (40, -180)
    tex.name = tex.label = "黑脸遮罩贴图"
    tex.image, tex.extension, tex.interpolation = image, "EXTEND", "Linear"
    link(uv.outputs[0], tex.inputs["Vector"])
    return tex.outputs["Color"]


def follow_face(transaction, rig, objects):
    head, = [o for o in objects if o.type == "MESH" and "V3_HeadVariant" in o.data.attributes]
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned

        require_owned(transaction.owner, (head, head.data))
        require_no_external_users(cast(bpy.types.Scene, bpy.context.scene), transaction.owner, (head.data,))
    armature, = [m for m in head.modifiers if m.type == "ARMATURE" and m.object == rig]
    slot = list(head.modifiers).index(armature)
    mesh = head.data
    matrix = rig.matrix_world.inverted_safe() @ head.matrix_world
    basis = mesh.shape_keys.reference_key.data if mesh.shape_keys else mesh.vertices
    variants = mesh.attributes["V3_HeadVariant"]
    # T7 supplies the authored bounds of the flat front for every eye type.
    indices = {i for f in mesh.polygons if variants.data[f.index].value == 7 for i in f.vertices}
    front = [matrix @ basis[i].co for i in indices]
    left, right = min(p.x for p in front), max(p.x for p in front)
    bottom, top = min(p.z for p in front), max(p.z for p in front)
    depth = min(p.y for p in front)
    height, width = top-bottom, right-left
    if min(height, width) < 1e-5:
        raise ValueError("人模正面尺寸无效：" + head.name)
    # Schema 7 already marked the front in Z. Preserve that exact footprint,
    # including the boundary between the face and the sides of the base head.
    field = mesh.attributes.get(_FIELD)
    if field is None:
        values = [0.0] * (3 * len(mesh.loops))
        depths = [(matrix @ v.co).y for v in basis]
        for face in mesh.polygons:
            if max(depths[i] for i in face.vertices) <= depth + .035 * height:
                for loop in face.loop_indices:
                    values[3*loop+2] = 1.0
        field = mesh.attributes.new(_FIELD, "FLOAT_VECTOR", "CORNER")
        transaction.on_rollback(lambda: mesh.attributes.remove(mesh.attributes[_FIELD]))
        field.data.foreach_set("vector", values)
    elif field.domain != "CORNER" or field.data_type != "FLOAT_VECTOR":
        raise ValueError("人模黑脸坐标属性已被修改：" + head.name)

    group = transaction.track(bpy.data.node_groups.new("白珀表情 · 脸部渐变坐标", "GeometryNodeTree"))
    for direction in ("INPUT", "OUTPUT"):
        group.interface.new_socket(name="Geometry", in_out=direction, socket_type="NodeSocketGeometry")
    nodes, link = group.nodes, group.links.new

    def node(kind, x, y):
        item: Any = nodes.new(kind)
        item.location = (x, y)
        return item

    position = node("GeometryNodeInputPosition", -800, 0).outputs[0]
    coords = node("ShaderNodeCombineXYZ", -140, -100)
    for index, (axis, extent, center) in enumerate((
            (0, width, (left+right)/2), (2, height, (bottom+top)/2))):
        dot = node("ShaderNodeVectorMath", -600, -165*index)
        dot.operation = "DOT_PRODUCT"
        dot.inputs[1].default_value = [matrix[axis][j]/extent for j in range(3)]
        link(position, dot.inputs[0])
        offset = node("ShaderNodeMath", -390, -165*index)
        offset.operation = "ADD"
        link(dot.outputs["Value"], offset.inputs[0])
        offset.inputs[1].default_value = (matrix[axis][3]-center)/extent
        link(offset.outputs[0], coords.inputs[index])
    saved = node("GeometryNodeInputNamedAttribute", -650, -380)
    saved.data_type = "FLOAT_VECTOR"
    saved.inputs["Name"].default_value = _FIELD
    components = node("ShaderNodeSeparateXYZ", -390, -380)
    link(saved.outputs["Attribute"], components.inputs[0])
    link(components.outputs["Z"], coords.inputs["Z"])
    store = node("GeometryNodeStoreNamedAttribute", 100, 130)
    store.data_type, store.domain = "FLOAT_VECTOR", "CORNER"
    store.inputs["Name"].default_value = _FIELD
    link(node("NodeGroupInput", -175, 180).outputs["Geometry"], store.inputs["Geometry"])
    link(coords.outputs[0], store.inputs["Value"])
    link(store.outputs["Geometry"], node("NodeGroupOutput", 340, 130).inputs["Geometry"])
    # Measure the actual lid surface after facial deformation, in head space before posing.
    mod = head.modifiers.new("白珀 · 黑脸渐变", "NODES")
    transaction.on_rollback(lambda: head.modifiers.remove(mod))
    mod.node_group = group
    head.modifiers.move(len(head.modifiers)-1, slot)


def _mask_group(transaction):
    group = transaction.track(bpy.data.node_groups.new("白珀表情 · 黑脸范围", "ShaderNodeTree"))
    for name in ("宽度", "高度"):
        socket = group.interface.new_socket(name=name, in_out="INPUT", socket_type="NodeSocketFloat")
        socket.default_value = 1
    group.interface.new_socket(name="遮罩", in_out="OUTPUT", socket_type="NodeSocketFloat")
    nodes, link = group.nodes, group.links.new

    def node(kind, x, y):
        item: Any = nodes.new(kind)
        item.location = (x, y)
        return item

    def math(operation, a, b, x, y):
        n = node("ShaderNodeMath", x, y)
        n.operation = operation
        for s, value in zip(n.inputs, (a, b)):
            if isinstance(value, (float, int)):
                s.default_value = value
            else:
                link(value, s)
        return n.outputs[0]

    def ramp(value, low, high, start, end, x, y):
        n = node("ShaderNodeMapRange", x, y)
        n.interpolation_type = "SMOOTHSTEP"
        link(value, n.inputs["Value"])
        for label, v in zip(("From Min", "From Max", "To Min", "To Max"), (low, high, start, end)):
            n.inputs[label].default_value = v
        return n.outputs["Result"]

    inputs = node("NodeGroupInput", -1000, 0).outputs
    attr = node("ShaderNodeAttribute", -1000, 280)
    attr.attribute_name = _FIELD
    xyz = node("ShaderNodeSeparateXYZ", -780, 300)
    link(attr.outputs["Vector"], xyz.inputs[0])
    axes = []
    for label, coord, y in (("宽度", "X", 15), ("高度", "Y", -180)):
        size = math("ABSOLUTE", inputs[label], 0, -780, y)
        size = math("MAXIMUM", size, .00001, -580, y)
        axes.append(math("DIVIDE", xyz.outputs[coord], size, -365, y))
    x = math("ABSOLUTE", axes[0], 0, -165, 100)
    z = axes[1]
    horizontal = ramp(x, .50, .54, 1, 0, 40, 330)
    forehead = _texture(transaction, group, axes[0], z)
    top = ramp(z, .50, .54, 1, 0, 40, -560)
    face = math("MULTIPLY", xyz.outputs["Z"], horizontal, 280, 330)
    fade = math("MULTIPLY", forehead, top, 280, 0)
    result = math("MULTIPLY", face, fade, 480, 170)
    link(result, node("NodeGroupOutput", 690, 170).inputs["遮罩"])
    return group


def upgrade_mask(transaction, objects):
    head, = [o for o in objects if o.type == "MESH" and "V3_HeadVariant" in o.data.attributes]
    material, = {s.material for s in head.material_slots if s.material and s.material.get(SKIN_ROLE) == "skin"}
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users

        require_no_external_users(cast(bpy.types.Scene, bpy.context.scene), transaction.owner, (material,))
    mask, = [n for n in material.node_tree.nodes if n.type == "GROUP" and n.label == "总眼睛控制黑脸范围"]
    original = mask.node_tree
    # Keep edits elsewhere in the user's group and all other instances intact.
    group = transaction.track(original.copy())
    ramps = [n for n in group.nodes if n.type == "MAP_RANGE" and n.interpolation_type == "SMOOTHSTEP"
             and all(not n.inputs[k].is_linked and abs(n.inputs[k].default_value - v) < 1e-5
                     for k, v in (("From Min", -.44), ("From Max", .10), ("To Min", 0), ("To Max", .90)))]
    if len(ramps) != 1 or not ramps[0].inputs["Value"].is_linked:
        raise ValueError("黑脸渐变节点已被自定义，请保留该实例：" + material.name)
    ramp, = ramps
    y = ramp.inputs["Value"].links[0].from_socket
    x, = [n for n in group.nodes if n.type == "MATH" and n.operation == "DIVIDE" and n != y.node]
    targets = [edge.to_socket for edge in ramp.outputs["Result"].links]
    texture = _texture(transaction, group, x.outputs[0], y)
    group.nodes.remove(ramp)
    for target in targets:
        group.links.new(texture, target)
    transaction.on_rollback(lambda: setattr(mask, "node_tree", original))
    mask.node_tree = group


def install(transaction, rig, objects):
    head, = [o for o in objects if o.type == "MESH" and "V3_HeadVariant" in o.data.attributes]
    material, = {s.material for s in head.material_slots if s.material and s.material.get(SKIN_ROLE) == "skin"}
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned

        items = [material, head.data]
        require_owned(transaction.owner, items)
        require_no_external_users(cast(bpy.types.Scene, bpy.context.scene), transaction.owner, items)
    tree = material.node_tree
    surface, = [n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"]
    color = surface.inputs["Base Color"]
    if len(color.links) != 1:
        raise ValueError("人模原生肤色连接已被修改：" + material.name)
    original = color.links[0].from_socket
    # The old tile was retired before schema 6. Its permanently zero attribute
    # still consumed an EEVEE vertex input. Free that input for the new mask.
    retired, = [n for n in tree.nodes if n.type == "ATTRIBUTE" and n.attribute_name == "BP_RetiredBrowTile"]
    for edge in list(retired.outputs["Fac"].links):
        source, dest = edge.from_socket, edge.to_socket
        previous = dest.default_value
        def restore(a=source, b=dest, value=previous):
            b.default_value = value
            tree.links.new(a, b)
        transaction.on_rollback(restore)
        tree.links.remove(edge)
        dest.default_value = 0

    def node(kind, label, x, y):
        n: Any = tree.nodes.new(kind)
        transaction.on_rollback(lambda: tree.nodes.remove(n))
        n.label = label
        n.location = surface.location_absolute + Vector((x, y))
        return n

    mask = node("ShaderNodeGroup", "总眼睛控制黑脸范围", -565, -320)
    mask.node_tree = _mask_group(transaction)
    for channel, axis in (("宽度", 0), ("高度", 2)):
        socket = mask.inputs[channel]
        driver = socket.driver_add("default_value").driver
        transaction.on_rollback(lambda s=socket: s.driver_remove("default_value"))
        driver.type = "AVERAGE"
        target = driver.variables.new().targets[0]
        target.id = rig
        target.data_path = rig.pose.bones["眼睛"].path_from_id("scale") + f"[{axis}]"
    strength = node("ShaderNodeMath", "黑脸开关", -335, -300)
    strength.operation = "MULTIPLY"
    tree.links.new(mask.outputs["遮罩"], strength.inputs[0])
    enabled = strength.inputs[1]
    driver = enabled.driver_add("default_value").driver
    transaction.on_rollback(lambda: enabled.driver_remove("default_value"))
    driver.type = "SCRIPTED"
    driver.expression = f"({mode_expression(driver, rig)}) == 4"
    mix = node("ShaderNodeMixRGB", "黑脸", -120, -130)
    mix.inputs[2].default_value = (0, 0, 0, 1)
    tree.links.new(strength.outputs[0], mix.inputs[0])
    tree.links.new(original, mix.inputs[1])
    transaction.on_rollback(lambda: tree.links.new(original, color))
    tree.links.new(mix.outputs[0], color)
