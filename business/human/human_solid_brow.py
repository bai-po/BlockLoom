"""Sixth native face-slider position, solid brow material and bare single-eye diagram."""
from array import array

import bpy

from ...util.geometry_nodes import input_path
from . import human_eye_selector as selector
from .human_face_controls import _keep, bounds
from .human_face_preview import _channel, _uv_tree

SIX = "眼睛表情 · 六档（含纯色眉纹）"


def _rail(tx, rig):
    rail, knob = (rig.pose.bones[n] for n in (selector.TRACK, selector.SWITCH))
    limit = knob.constraints[selector.FIVE_STOPS]
    step = -limit.min_z / 4
    _keep(tx, limit, "max_z", "name")
    limit.max_z, limit.name = step, SIX
    old = rail.custom_shape
    tx.on_rollback(lambda: setattr(rail, "custom_shape", old))
    rail.custom_shape = selector._cell_shape(tx, 6, "眼睛表情六档轨道")
    # Keep the five old bone coordinates/keyframes. Compress only the drawing
    # so both rails keep their common top and bottom edges.
    low, high = bounds(rail)[2]
    old_center = sum(bounds(knob)[2]) / 2
    base = knob.custom_shape_translation.z + high - 1.5*(high-low)/6 - old_center
    from .human_face_switches import _edit

    helper = "眼睛表情·滑柄显示"
    with _edit(rig) as bones:
        original = bones[selector.SWITCH]
        visual = bones.new(helper)
        visual.head, visual.tail, visual.roll = original.head, original.tail, original.roll
        visual.parent, visual.use_deform = original.parent, False

    def remove_helper():
        with _edit(rig) as bones:
            bones.remove(bones[helper])
    tx.on_rollback(remove_helper)
    knob = rig.pose.bones[selector.SWITCH]
    visual = rig.pose.bones[helper]
    visual.bone.hide = True
    constraint = visual.constraints.new("COPY_LOCATION")
    constraint.target, constraint.subtarget = rig, selector.SWITCH
    constraint.owner_space = constraint.target_space = "LOCAL"
    constraint.influence = 5/6
    _keep(tx, knob, "custom_shape_scale_xyz", "custom_shape_translation")
    tx.on_rollback(lambda: setattr(knob, "custom_shape_transform", None))
    knob.custom_shape_transform = visual
    knob.custom_shape_translation.z = base
    knob.custom_shape_scale_xyz *= 5/6
    return step/2


def _expressions(tx, obj, rig, threshold):
    for curve in obj.animation_data.drivers if obj.animation_data else ():
        d = curve.driver
        if not any(v.type == "TRANSFORMS" and v.targets[0].bone_target == selector.SWITCH
                   and v.targets[0].id == rig for v in d.variables):
            continue
        if 'emote' not in d.variables or 'v' not in d.variables:
            continue
        # Reuse the exact authored gate and side direction for each eye.
        from re import search
        gate = search(r"v\s*(?:<=|>=)\s*-?[\d.]+", d.expression)
        if not gate:
            raise ValueError("无法识别原有逐眼开关：" + curve.data_path)
        before = d.expression
        _keep(tx, d, "expression")
        d.expression = f"(1 if {gate.group()} else 0) if emote>{threshold:.9g} else ({before})"


def _material(tx, head, rig, threshold):
    mod = head.modifiers["白珀 · 原生瞳孔表情"]
    old = mod.node_group
    tree = tx.track(old.copy())
    tx.on_rollback(lambda: setattr(mod, "node_group", old))
    mod.node_group = tree
    mat = tx.track(bpy.data.materials.new("闭眼眉纹 · 纯色"))
    mat.use_nodes = True
    mat.diffuse_color = (0, 0, 0, 1)
    color = mat.node_tree.nodes.new("ShaderNodeRGB")
    color.label = color.name = "闭眼眉纹颜色"
    color.outputs[0].default_value = (0, 0, 0, 1)
    shader = mat.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Roughness"].default_value = 1
    shader.inputs["Specular IOR Level"].default_value = 0
    mat.node_tree.links.new(color.outputs[0], shader.inputs["Base Color"])
    mats = head.data.materials
    index = len(mats)
    mats.append(mat)
    tx.on_rollback(lambda: mats.pop(index=index))

    nodes, link = tree.nodes, tree.links.new
    inp = next(n for n in nodes if n.type == "GROUP_INPUT")
    toggle = tree.interface.new_socket(name="纯色闭眼眉纹",in_out="INPUT",socket_type="NodeSocketBool")
    path = input_path(mod, toggle.identifier)
    d = head.driver_add(path).driver
    tx.on_rollback(lambda: head.driver_remove(path))
    _channel(d, "slide", rig, selector.SWITCH, "LOC_Z")
    d.expression = f"slide>{threshold:.9g}"
    def assign(source, selection):
        node = nodes.new("GeometryNodeSetMaterialIndex")
        link(source, node.inputs["Geometry"])
        link(selection, node.inputs["Selection"])
        node.inputs["Material Index"].default_value = index
        return node.outputs[0]
    # T7/raw-side copies are created on a separate branch. Assign only their
    # two strips, never the original face that supplied the carrier.
    for field in ("BP_RawBrowMain", "BP_RawBrowSecond"):
        attr = next(n for n in nodes if n.bl_idname=="GeometryNodeInputNamedAttribute"
                    and n.inputs["Name"].default_value==field)
        move = next(n for n in nodes if n.bl_idname=="GeometryNodeSetPosition" and
                    any(e.from_node==attr or any(f.from_node==attr for s in e.from_node.inputs for f in s.links)
                        for e in n.inputs["Offset"].links))
        source = move.inputs["Geometry"].links[0].from_socket
        link(assign(source, inp.outputs[toggle.identifier]), move.inputs["Geometry"])
    output = next(n for n in nodes if n.type=="GROUP_OUTPUT" and n.is_active_output)
    active = nodes.new("FunctionNodeBooleanMath")
    active.operation = "AND"
    attr = nodes.new("GeometryNodeInputNamedAttribute")
    attr.data_type = "BOOLEAN"
    attr.inputs["Name"].default_value = "BP_ClosedBrow"
    link(attr.outputs["Attribute"],active.inputs[0])
    link(inp.outputs[toggle.identifier],active.inputs[1])
    link(assign(output.inputs[0].links[0].from_socket,active.outputs[0]),output.inputs[0])


def _diagram(tx, board):
    from .human_eye_diagram import _MODIFIER, _TILE
    node = board.active_material.node_tree.nodes["头型与左右眼示意"]
    mod = board.modifiers[_MODIFIER]
    old, old_tree = node.image, mod.node_group
    image = tx.track(old.copy())
    width,height = image.size
    assert (width,height)==(_TILE*11,_TILE*8)
    buf = array("f",[0])*(width*height*4)
    old.pixels.foreach_get(buf)
    original = next(n.image for n in board.active_material.node_tree.nodes
                    if n.type=="TEX_IMAGE" and n.image and tuple(n.image.size)==(8,64))
    rows=[]
    for t in range(8):
        yy=[j for j in range(8) if min(original.pixels[((8*t+j)*8+1)*4:((8*t+j)*8+1)*4+3])>.99]
        lo,hi=(min(yy),max(yy)+1) if yy else (1,3)
        rows.append((lo,hi))
        for y in range(_TILE):
            off=((t*_TILE+y)*width)*4
            for src,dst in ((0,9),(7,10)):
                start=off+src*_TILE*4
                row=buf[start:start+_TILE*4]
                if lo*16<=y<hi*16:
                    for x in (16,80):
                        row[x*4:(x+32)*4]=row[60*4:61*4]*32
                start=off+dst*_TILE*4
                buf[start:start+len(row)]=row
    image.pixels.foreach_set(buf)
    image.pack()
    group,_=_uv_tree(tx,board.data.uv_layers.active.name,image,None,eye_rows=tuple(reversed(rows)),brow_layers=True)
    tx.on_rollback(lambda:setattr(node,"image",old))
    tx.on_rollback(lambda:setattr(mod,"node_group",old_tree))
    node.image,mod.node_group=image,group


def install(tx,rig,objects):
    head, = [o for o in objects if o.modifiers.get("白珀 · 原生瞳孔表情")]
    board, = [o for o in objects if o.modifiers.get("图示 · 头型与左右眼")]
    if tx.owner:
        from ...util.ownership import require_no_external_users
        require_no_external_users(bpy.context.scene,tx.owner,(head.data,board.active_material))
    threshold=_rail(tx,rig)
    _expressions(tx,head,rig,threshold)
    _expressions(tx,board,rig,threshold)
    _material(tx,head,rig,threshold)
    _diagram(tx,board)

