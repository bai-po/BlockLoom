"""Native textured pupil expressions on the existing eight-head mesh."""

from math import cos, pi, sin
from pathlib import Path
from typing import Any, cast

import bpy
from mathutils import Vector

from ...util.geometry_nodes import input_path
from .human_face_switches import drive_switch

_ROLE = "BP_EyeSpriteFace"
_CENTER = "BP_EyeSpriteCenterOffset"
_RADIUS = "BP_EyeSpriteRadius"
_UV = "BP_EyeSpriteUV"
_RAW_BROWS = ("BP_RawBrowMain", "BP_RawBrowSecond")
_RAW_BROW_UV = "BP_RawBrowUV"
_MODIFIER = "白珀 · 原生瞳孔表情"


def _heart():
    # Minecraft draws the full heart over its container to obtain the black rim.
    folder = Path(__file__).resolve().parents[2] / "asset/library/assets/minecraft/textures/gui/sprites/hud/heart"
    layers = []
    for name in ("container.png", "full.png"):
        image = bpy.data.images.load(str(folder / name), check_existing=False)
        try:
            if tuple(image.size) != (9, 9):
                raise ValueError("原版满心图标尺寸不符")
            layers.append(tuple(cast(Any, image.pixels)))
        finally:
            bpy.data.images.remove(image)
    back, front = layers
    return [value for i in range(0, len(back), 4)
            for value in (front if front[i+3] else back)[i:i+4]]


def _spiral():
    size = 15
    points = []
    for i in range(401):
        t = i / 400
        angle = t * 4.15 * pi + .45 * pi
        radius = .18 + 6.4*t
        points.append((7 + radius*cos(angle), 7 + radius*sin(angle)))
    return [value for y in range(size) for x in range(size)
            for value in (.15, .10, .21, float(any((a-x)**2 + (b-y)**2 <= .75**2 for a, b in points)))]


def sprite_images(transaction):
    """Return the two packed sources shared by this rig's pupils and diagram."""
    existing = getattr(transaction, "_baipo_eye_sprites", None)
    if existing is not None:
        return existing
    result = []
    for name, size, pixels in (("MC 满心", 9, _heart()), ("蚊香眼", 15, _spiral())):
        image = transaction.track(bpy.data.images.new("白珀眼睛 · " + name, size, size, alpha=True))
        image.pixels.foreach_set(pixels)
        image.pack()
        result.append(image)
    transaction._baipo_eye_sprites = tuple(result)
    return transaction._baipo_eye_sprites


def _material(transaction, mesh, image, active_uv, *, spiral=False):
    mat = transaction.track(bpy.data.materials.new(image.name))
    mat.use_nodes = True
    nodes, link = mat.node_tree.nodes, mat.node_tree.links.new
    surface = next(n for n in nodes if n.type == "BSDF_PRINCIPLED")
    texture = nodes.new("ShaderNodeTexImage")
    texture.image, texture.interpolation, texture.extension = image, "Closest", "CLIP"
    uv = nodes.new("ShaderNodeUVMap")
    # EEVEE does not bind a ninth UV layer on the eight-map human head.
    # The eye modifier writes the icon's local coordinates into this existing
    # renderable layer on the pupil faces only, including Workbench Texture.
    uv.uv_map = active_uv
    link(uv.outputs["UV"], texture.inputs["Vector"])
    link(texture.outputs["Color"], surface.inputs["Base Color"])
    link(texture.outputs["Alpha"], surface.inputs["Alpha"])
    if spiral:
        from .human_spiral_shader import install as install_spiral_shader
        install_spiral_shader(transaction, mat, uv.outputs["UV"], texture)
    nodes.active = texture
    index = len(mesh.materials)
    mesh.materials.append(mat)
    transaction.on_rollback(lambda: mesh.materials.pop(index=index))
    return index


def _mark(transaction, obj, rig):
    mesh = obj.data
    to_rig = rig.matrix_world.inverted() @ obj.matrix_world
    inverse = to_rig.to_3x3().inverted()
    basis = mesh.shape_keys.reference_key
    points = [to_rig @ p.co for p in basis.data]
    attrs = mesh.attributes
    def add(name, kind, domain):
        if name in attrs:
            raise ValueError("已有同名眼部属性：" + name)
        attrs.new(name, kind, domain)
        transaction.on_rollback(lambda: attrs.remove(attrs[name]))
    add(_ROLE, "INT", "FACE")
    add(_CENTER, "FLOAT_VECTOR", "POINT")
    add(_RADIUS, "FLOAT_VECTOR", "POINT")
    add(_UV, "FLOAT_VECTOR", "CORNER")
    for name in _RAW_BROWS:
        add(name, "FLOAT_VECTOR", "POINT")
    add(_RAW_BROW_UV, "FLOAT_VECTOR", "CORNER")
    shape, fill = attrs["V3_HeadVariant"], attrs["V3_TransparentFill"]
    eye_uv = attrs["V3_EyeWindowUV"]
    for variant in range(7):
        faces = [face for face in mesh.polygons if shape.data[face.index].value == variant]
        for sign, label in ((1, "左"), (-1, "右")):
            # Native per-eye gaze keys isolate exactly the pupil polygons even
            # on the shorter, single-row variants. All gaze keys remain intact.
            key = next(k for k in mesh.shape_keys.key_blocks if k.name.startswith(f"T{variant}::单{label}") and "看" in k.name)
            moving = {i for i,(a,b) in enumerate(zip(key.data,basis.data)) if (a.co-b.co).length_squared > 1e-12}
            pupil = [f for f in faces if all(i in moving for i in f.vertices)]
            window = [f for f in faces if fill.data[f.index].value and sign*points[f.vertices[0]].x > 0
                      and all(-.02 <= n <= 1.02 for j in f.loop_indices for n in eye_uv.data[j].vector[:2])]
            if not pupil or not window:
                raise ValueError(f"T{variant} 缺少{label}眼原生眼孔或瞳孔")
            pupil_ids = {i for f in pupil for i in f.vertices}
            aperture = [points[i] for f in window for i in f.vertices]
            x0, x1 = min(p.x for p in aperture), max(p.x for p in aperture)
            z0, z1 = min(p.z for p in aperture), max(p.z for p in aperture)
            px0, px1 = min(points[i].x for i in pupil_ids), max(points[i].x for i in pupil_ids)
            pz0, pz1 = min(points[i].z for i in pupil_ids), max(points[i].z for i in pupil_ids)
            size = min(x1-x0, z1-z0) * .90
            if min(size, px1-px0, pz1-pz0) < 1e-5:
                raise ValueError(f"T{variant} {label}瞳孔尺寸无效")
            for f in window:
                attrs[_ROLE].data[f.index].value = sign*2
            for f in pupil:
                attrs[_ROLE].data[f.index].value = sign
                for i,j in zip(f.vertices,f.loop_indices):
                    p = points[i]
                    u, v = (p.x-px0)/(px1-px0), (p.z-pz0)/(pz1-pz0)
                    attrs[_UV].data[j].vector = (u,v,0)
                    attrs[_CENTER].data[i].vector = inverse @ Vector(((x0+x1)/2-p.x, 0, (z0+z1)/2-p.z))
                    attrs[_RADIUS].data[i].vector = inverse @ Vector(((u-.5)*size, 0, (v-.5)*size))
            if variant == 0:
                depth = sum(p.y for p in aperture)/len(aperture)
                _mark_raw(mesh, points, to_rig, inverse, sign, (x0+x1)/2, (z0+z1)/2, depth, size)
    return tuple((inverse @ Vector((0,-1,0))).normalized())


def _mark_raw(mesh, points, to_rig, inverse, sign, x, z, depth, size):
    """Use an existing flat T7 tile as a source with the original head weights."""
    attrs = mesh.attributes
    variants = attrs["V3_HeadVariant"]
    quads = [f for f in mesh.polygons if variants.data[f.index].value == 7 and len(f.vertices)==4
             and f.area > size*size*.05 and sign*points[f.vertices[0]].x>0]
    def distance(f):
        p = to_rig @ f.center
        return (p.x-x)**2 + (p.z-z)**2 + (p.y-depth)**2
    carrier = min(quads,key=distance)
    attrs[_ROLE].data[carrier.index].value = sign*3
    corners = [points[i] for i in carrier.vertices]
    xmin,xmax = min(p.x for p in corners),max(p.x for p in corners)
    zmin,zmax = min(p.z for p in corners),max(p.z for p in corners)
    if min(xmax-xmin,zmax-zmin)<size*.2:
        raise ValueError("原皮头型缺少前向眼部面片")
    # Copy the actual two authored brow strips, including their original UVs.
    # A complete T7 face has neither pupils nor brows to move in place.
    brow = [(i,j) for f in mesh.polygons if variants.data[f.index].value==0
            and attrs["BP_BrowSurface"].data[f.index].value and sign*points[f.vertices[0]].x>0
            for i,j in zip(f.vertices,f.loop_indices)]
    bx = [points[i].x for i,_ in brow]
    bz = [points[i].z for i,_ in brow]
    for i,j in zip(carrier.vertices, carrier.loop_indices):
        p = points[i]
        u,v = (p.x-xmin)/(xmax-xmin),(p.z-zmin)/(zmax-zmin)
        attrs[_UV].data[j].vector=(u,v,0)
        attrs[_CENTER].data[i].vector=inverse @ Vector((x-p.x,-size*.005,z-p.z))
        attrs[_RADIUS].data[i].vector=inverse @ Vector(((u-.5)*size,0,(v-.5)*size))
        target_x=min(bx)+u*(max(bx)-min(bx))
        target_z=min(bz)+v*(max(bz)-min(bz))
        source,loop=min(brow,key=lambda pair: (points[pair[0]].x-target_x)**2+(points[pair[0]].z-target_z)**2)
        attrs[_RAW_BROW_UV].data[j].vector=(*attrs["V3_UV_0"].data[loop].vector, 0)
        for name,source_name in zip(_RAW_BROWS,("BP_BrowMainOffset","BP_BrowSecondOffset")):
            tip=points[source]+to_rig.to_3x3() @ attrs[source_name].data[source].vector
            # The native expression brow sits above its old pupil. T7's
            # iris is painted on the skin, so centre the expression on it.
            tip.z += z-(min(bz)+max(bz))/2
            tip.y=p.y-size*(.006 if name==_RAW_BROWS[0] else .004)
            attrs[name].data[i].vector=inverse @ (tip-p)


def _graph(transaction, axis, first_slot, active_uv):
    group = transaction.track(bpy.data.node_groups.new("白珀眼睛 · 独立图案与旋转", "GeometryNodeTree"))
    def socket(name, kind, direction="INPUT"):
        return group.interface.new_socket(name=name, in_out=direction, socket_type=kind)
    socket("几何", "NodeSocketGeometry")
    channels = [socket(label + field, kind) for field, kind in (("眼表情", "NodeSocketInt"), ("眼旋转", "NodeSocketFloat")) for label in ("左","右")]
    socket("几何", "NodeSocketGeometry", "OUTPUT")
    nodes, link = group.nodes, group.links.new
    src, out = nodes.new("NodeGroupInput"), nodes.new("NodeGroupOutput")

    def attribute(name, kind):
        node = nodes.new("GeometryNodeInputNamedAttribute")
        node.data_type = kind
        node.inputs["Name"].default_value = name
        return node.outputs["Attribute"]

    def math(op, a, b=0):
        node = nodes.new("ShaderNodeMath")
        node.operation = op
        link(a, node.inputs[0])
        if isinstance(b,(float,int)):
            node.inputs[1].default_value = b
        else:
            link(b,node.inputs[1])
        return node.outputs[0]

    role = attribute(_ROLE, "INT")
    is_left = math("GREATER_THAN", role, 0)
    def side(field, kind):
        switch = nodes.new("GeometryNodeSwitch")
        switch.input_type = kind
        link(is_left, switch.inputs["Switch"])
        link(src.outputs["右" + field], switch.inputs["False"])
        link(src.outputs["左" + field], switch.inputs["True"])
        return switch.outputs[0]

    mode, angle = side("眼表情","INT"), side("眼旋转","FLOAT")
    active = math("GREATER_THAN", mode, 1)
    which = math("ABSOLUTE", role)
    is_pupil = math("MULTIPLY", math("COMPARE", which, 1), active)
    # Native fill triangles can carry the painted iris at 128px. They
    # occupy the same aperture as the eye; remove them only on the active
    # side. The lids, skin around the socket, and raw T7 side stay in place.
    hole = nodes.new("GeometryNodeDeleteGeometry")
    hole.domain = "FACE"
    link(src.outputs["几何"], hole.inputs["Geometry"])
    link(math("MULTIPLY", math("COMPARE", which, 2), active), hole.inputs["Selection"])
    def split(geometry, condition):
        separate = nodes.new("GeometryNodeSeparateGeometry")
        separate.domain = "FACE"
        link(geometry,separate.inputs["Geometry"])
        link(condition,separate.inputs["Selection"])
        return separate.outputs["Selection"],separate.outputs["Inverted"]
    def join(*inputs):
        combine=nodes.new("GeometryNodeJoinGeometry")
        for inp in inputs:
            link(inp,combine.inputs[0])
        return combine.outputs[0]

    # A replaced skin half (and both halves of T7) has no animated pupil.
    # Duplicate its carrier; preserve every original skin pixel underneath.
    # Output geometry, not the single-eye slider, decides which surface exists.
    pupil, ordinary = split(hole.outputs[0],is_pupil)
    carrier,_ = split(ordinary,math("COMPARE",which,3))
    raw_glyph,_ = split(carrier,active)
    raw_brow,_ = split(carrier,math("COMPARE",mode,1))
    rotate = nodes.new("ShaderNodeVectorRotate")
    rotate.rotation_type = "AXIS_ANGLE"
    link(attribute(_RADIUS, "FLOAT_VECTOR"), rotate.inputs["Vector"])
    rotate.inputs["Axis"].default_value = axis
    link(angle, rotate.inputs["Angle"])
    offset = nodes.new("ShaderNodeVectorMath")
    offset.operation = "ADD"
    link(attribute(_CENTER, "FLOAT_VECTOR"), offset.inputs[0])
    link(rotate.outputs[0], offset.inputs[1])
    move = nodes.new("GeometryNodeSetPosition")
    link(join(pupil,raw_glyph), move.inputs["Geometry"])
    link(offset.outputs[0], move.inputs["Offset"])
    material = nodes.new("GeometryNodeSetMaterialIndex")
    link(move.outputs[0], material.inputs["Geometry"])
    link(math("ADD", math("GREATER_THAN", mode, 2), first_slot), material.inputs["Material Index"])
    def write_uv(geometry, name, source):
        store = nodes.new("GeometryNodeStoreNamedAttribute")
        store.data_type, store.domain = "FLOAT2", "CORNER"
        store.inputs["Name"].default_value = name
        link(geometry, store.inputs["Geometry"])
        link(attribute(source,"FLOAT_VECTOR"),store.inputs["Value"])
        return store.outputs[0]
    icon = write_uv(material.outputs[0], active_uv, _UV)
    brow = write_uv(write_uv(raw_brow, "V6_CurrentSkinUV", _RAW_BROW_UV),active_uv,_RAW_BROW_UV)
    strips = []
    for field in _RAW_BROWS:
        move_brow=nodes.new("GeometryNodeSetPosition")
        link(brow,move_brow.inputs["Geometry"])
        link(attribute(field,"FLOAT_VECTOR"),move_brow.inputs["Offset"])
        strips.append(move_brow.outputs[0])
    combined=join(ordinary,icon,*strips)
    # Bypass both separation and join for the normal neutral head.
    bypass=nodes.new("GeometryNodeSwitch")
    bypass.input_type="GEOMETRY"
    link(math("MAXIMUM",src.outputs["左眼表情"],src.outputs["右眼表情"]),bypass.inputs["Switch"])
    link(src.outputs["几何"],bypass.inputs["False"])
    link(combined,bypass.inputs["True"])
    link(bypass.outputs[0],out.inputs["几何"])
    return group, [s.identifier for s in channels]


def install(transaction, rig, objects):
    """Replace the existing animated pupils and retain their native gaze/blink."""
    from .human_eye_selector import mode_expression

    heads = [o for o in objects if o.type == "MESH" and "BP_SingleEyeSample" in o.data.attributes]
    if len(heads) != 1:
        raise ValueError("瞳孔表情需要唯一的八型头部：" + rig.name)
    head, = heads
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned
        require_owned(transaction.owner, (head, head.data))
        require_no_external_users(cast(Any, bpy.context.scene), transaction.owner, (head.data,))
    if _MODIFIER in head.modifiers:
        raise ValueError("已有同名瞳孔表情修改器：" + head.name)
    active_uv = head.data.uv_layers.active.name
    axis = _mark(transaction, head, rig)
    heart, spiral = sprite_images(transaction)
    slot = _material(transaction, head.data, heart, active_uv)
    _material(transaction, head.data, spiral, active_uv, spiral=True)
    graph, channels = _graph(transaction,axis,slot,active_uv)
    mod = head.modifiers.new(_MODIFIER, "NODES")
    transaction.on_rollback(lambda: head.modifiers.remove(mod))
    mod.node_group = graph
    # Shapes and the UV selector evaluate first; native armature deformation
    # then carries the new eyes with head rotation and character transforms.
    skeleton = next(i for i,m in enumerate(head.modifiers) if m.type == "ARMATURE" and m.object == rig)
    head.modifiers.move(len(head.modifiers)-1,skeleton)
    for key,angle,side in zip(channels[:2],channels[2:],("L","R")):
        path = input_path(mod, key)
        slider = rig.pose.bones[side+".眉纹切换"].constraints[0]
        drive_switch(head,path,rig,side,max(abs(slider.min_x),abs(slider.max_x)))
        transaction.on_rollback(lambda p=path: head.driver_remove(p))
        driver = head.animation_data.drivers.find(path).driver
        gate = driver.expression
        mode = mode_expression(driver, rig)
        driver.expression = f"({mode}) if ({gate}) else 0"
        path = input_path(mod, angle)
        driver = head.driver_add(path).driver
        transaction.on_rollback(lambda p=path: head.driver_remove(p))
        variable = driver.variables.new()
        variable.name, variable.type = "roll", "TRANSFORMS"
        target = variable.targets[0]
        target.id, target.bone_target = rig, side+".眼睛"
        target.transform_type, target.transform_space = "ROT_Y", "LOCAL_SPACE"
        target.rotation_mode = "YXZ"
        driver.expression = "roll"
