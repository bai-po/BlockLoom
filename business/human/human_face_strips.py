"""Two native textured brow strips, driven by the existing expression slider."""

import bpy
from mathutils import Vector

FIELDS = ("BP_BrowMainOffset", "BP_BrowSecondOffset", "BP_BrowParkOffset", "BP_BrowRestOffset")
SIDE = "BP_BrowLeft"
EXPRESSION_SCALE = 0.75
# A small clearance above the native brow plane keeps the closed lid visible
# right up to the strip edges, even when looking along the cheek.
BROW_CLEARANCE = 0.0035
TIP_SEPARATION = 0.0008


def offsets(points):
    """The two segments share the inner corner. The upper segment is longer."""
    x0, x1 = min(p.x for p in points.values()), max(p.x for p in points.values())
    z0, z1 = min(p.z for p in points.values()), max(p.z for p in points.values())
    width, height = max(x1-x0, .001), max(z1-z0, .001)
    cx, cz = (x1+x0)/2, (z1+z0)/2
    sign = 1 if cx >= 0 else -1
    inner_x = x0 if sign > 0 else x1
    # Slanted native brows can have their innermost and lowest coordinates on
    # different vertices. Anchor both strips to the same actual mesh corner.
    corner = min(points.values(), key=lambda p: ((p.x-inner_x)/width)**2 + ((p.z-z0)/height)**2)
    for i, p in points.items():
        u = sign*(p.x-corner.x)/width
        thickness = (p.z-corner.z)/height
        joint = Vector((cx-sign*.48*width, p.y-BROW_CLEARANCE*width, cz-.02*width))
        # The inner bottom corner is common to both strips. Their overlapping
        # faces separate in depth toward the outer tips, never at the joint.
        seam = joint + Vector((sign*.0525*width, 0, -.15*width))
        span = width * EXPRESSION_SCALE
        main = seam + Vector((sign*(1.17*u-.105*thickness)*span, 0, (.43*u+.30*thickness)*span))
        second = seam + Vector((sign*(.99*u+.15*thickness)*span, -TIP_SEPARATION*u*span, (-.62*u+.26*thickness)*span))
        parked = Vector((0, max(.18, .9*width), .35*width))

        yield i, (main-p, second-p, parked, Vector(((p.x-cx)*.10, 0, 0)))


def build_tree(transaction):
    group = transaction.track(bpy.data.node_groups.new("白珀面控 · 双眉贴片", "GeometryNodeTree"))
    def socket(name, kind, direction="INPUT"):
        return group.interface.new_socket(name=name, in_out=direction, socket_type=kind)
    socket("几何", "NodeSocketGeometry")
    active = [socket(s + "眼表情", "NodeSocketBool") for s in ("左", "右")]
    angles = [socket(s + "眉角", "NodeSocketFloat") for s in ("左", "右")]
    axis = socket("面部旋转轴", "NodeSocketVector")
    socket("几何", "NodeSocketGeometry", "OUTPUT")
    nodes, link = group.nodes, group.links.new
    src = nodes.new("NodeGroupInput")
    out = nodes.new("NodeGroupOutput")
    def field(name, data_type):
        reader = nodes.new("GeometryNodeInputNamedAttribute")
        reader.data_type = data_type
        reader.inputs["Name"].default_value = name
        return reader.outputs["Attribute"]
    def choose(kind, when_false, when_true, on):
        sel = nodes.new("GeometryNodeSwitch")
        sel.input_type = kind
        link(on, sel.inputs["Switch"])
        if when_false is not None:
            link(when_false, sel.inputs["False"])
        link(when_true, sel.inputs["True"])
        return sel.outputs[0]
    split = nodes.new("GeometryNodeSeparateGeometry")
    split.domain = "FACE"
    link(src.outputs["几何"], split.inputs["Geometry"])
    link(field("BP_BrowSurface", "FLOAT"), split.inputs["Selection"])
    original = split.outputs["Selection"]
    angle = choose("FLOAT", src.outputs["右眉角"], src.outputs["左眉角"], field(SIDE, "BOOLEAN"))

    def translate(geometry, vector, *, rotate=False):
        if rotate:
            rot = nodes.new("ShaderNodeVectorRotate")
            rot.rotation_type = "AXIS_ANGLE"
            link(vector, rot.inputs["Vector"])
            link(angle, rot.inputs["Angle"])
            link(src.outputs["面部旋转轴"], rot.inputs["Axis"])
            vector = rot.outputs["Vector"]
        move = nodes.new("GeometryNodeSetPosition")
        link(geometry, move.inputs["Geometry"])
        link(vector, move.inputs["Offset"])
        return move.outputs[0]

    main, secondary, park, neutral = [field(n, "FLOAT_VECTOR") for n in FIELDS]
    join = nodes.new("GeometryNodeJoinGeometry")
    state = choose("BOOLEAN", src.outputs["右眼表情"], src.outputs["左眼表情"], field(SIDE, "BOOLEAN"))
    hidden = nodes.new("FunctionNodeBooleanMath")
    hidden.operation = "AND"
    link(state, hidden.inputs[0])
    link(field("BP_BrowDetached", "BOOLEAN"), hidden.inputs[1])
    remove = nodes.new("GeometryNodeDeleteGeometry")
    remove.domain = "FACE"
    link(translate(src.outputs["几何"], neutral, rotate=True), remove.inputs["Geometry"])
    link(hidden.outputs[0], remove.inputs["Selection"])
    link(remove.outputs[0], join.inputs["Geometry"])
    def sides(geometry):
        separate = nodes.new("GeometryNodeSeparateGeometry")
        separate.domain = "FACE"
        link(geometry, separate.inputs["Geometry"])
        link(field(SIDE, "BOOLEAN"), separate.inputs["Selection"])
        return separate.outputs["Selection"], separate.outputs["Inverted"]
    for label, brow in zip(("左", "右"), sides(original)):
        enabled = src.outputs[label + "眼表情"]
        for geometry in (choose("GEOMETRY", None, translate(brow, main, rotate=True), enabled),
                         choose("GEOMETRY", translate(brow, park), translate(brow, secondary, rotate=True), enabled)):
            link(geometry, join.inputs["Geometry"])
    link(join.outputs[0], out.inputs[0])
    return group, [s.identifier for s in (*active, *angles, axis)]


def install(transaction, rig, obj, data, graph, stroke):
    from ...util.geometry_nodes import input_path, set_input
    from .human_face_switches import drive_switch

    mesh = obj.data
    for name, kind in [(name, "FLOAT_VECTOR") for name in FIELDS] + [(SIDE, "BOOLEAN"), ("BP_BrowDetached", "BOOLEAN")]:
        if mesh.attributes.get(name):
            raise ValueError("眉毛贴片网格属性已被占用：" + name)
        mesh.attributes.new(name, kind, "POINT")
        transaction.on_rollback(lambda n=name: mesh.attributes.remove(mesh.attributes[n]))
    for i, (left, detached, vectors) in data.items():
        mesh.attributes["BP_BrowDetached"].data[i].value = detached
        mesh.attributes[SIDE].data[i].value = left
        for name, vector in zip(FIELDS, vectors):
            mesh.attributes[name].data[i].vector = vector
    tree, (left, right, left_angle, right_angle, axis) = graph
    mod = obj.modifiers.new("白珀 · 主眉与副眉", "NODES")
    mod.node_group = tree
    transaction.on_rollback(lambda: obj.modifiers.remove(mod))
    obj.modifiers.move(len(obj.modifiers)-1, 0)
    to_mesh = obj.matrix_world.to_3x3().inverted() @ rig.matrix_world.to_3x3()
    set_input(mod, axis, tuple((to_mesh @ rig.data.bones["L.眉毛"].matrix_local.to_3x3().col[1]).normalized()))
    for prop, side in ((left, "L"), (right, "R")):
        path = input_path(mod, prop)
        drive_switch(obj, path, rig, side, stroke)
        transaction.on_rollback(lambda p=path: obj.driver_remove(p))
    for prop, name, kind, expr in [(left_angle, "L.眉毛", "ROT_Y", "v"),
                                   (right_angle, "R.眉毛", "ROT_Y", "v")]:
        path = input_path(mod, prop)
        drv = obj.driver_add(path).driver
        transaction.on_rollback(lambda p=path: obj.driver_remove(p))
        var = drv.variables.new()
        var.name, var.type = "v", "TRANSFORMS"
        target = var.targets[0]
        target.id, target.bone_target = rig, name
        target.transform_space, target.transform_type = "LOCAL_SPACE", kind
        drv.expression = expr
