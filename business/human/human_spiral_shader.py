"""User-keyable rotation of the spiral sprite in the pupil material."""

from typing import Any

import bpy

NODE = "蚊香眼旋转"
ANGLE = "旋转角度"


def install(transaction, mat, uv_socket, texture_node):
    group: Any = bpy.data.node_groups.new("白珀眼睛 · 蚊香纹理旋转", "ShaderNodeTree")
    transaction.track(group)
    group.interface.new_socket(name="UV", in_out="INPUT", socket_type="NodeSocketVector")
    angle = group.interface.new_socket(name=ANGLE, in_out="INPUT", socket_type="NodeSocketFloat")
    angle.subtype = "ANGLE"
    angle.default_value = 0.0
    angle.description = "在眼球骨骼的旋转基础上旋转蚊香纹理，可插入关键帧"
    group.interface.new_socket(name="UV", in_out="OUTPUT", socket_type="NodeSocketVector")
    nodes, link = group.nodes, group.links.new
    src, out = nodes.new("NodeGroupInput"), nodes.new("NodeGroupOutput")
    rotate = nodes.new("ShaderNodeVectorRotate")
    # Rotating sampling coordinates backwards rotates the displayed sprite forwards.
    rotate.rotation_type, rotate.invert = "Z_AXIS", True
    rotate.inputs["Center"].default_value = (.5, .5, 0)
    link(src.outputs["UV"], rotate.inputs["Vector"])
    link(src.outputs[ANGLE], rotate.inputs["Angle"])
    # The furthest opaque texel corner is 7.906 texels from the centre of
    # the 15x15 spiral. A fixed 16-texel window keeps every turn inside the quad.
    fit = nodes.new("ShaderNodeVectorMath")
    fit.operation = "MULTIPLY_ADD"
    fit.inputs[1].default_value = (16/15, 16/15, 1)
    fit.inputs[2].default_value = (-1/30, -1/30, 0)
    link(rotate.outputs["Vector"], fit.inputs[0])
    link(fit.outputs[0], out.inputs["UV"])
    src.location, rotate.location, fit.location, out.location = (-420, 0), (-190, 0), (55, 0), (290, 0)

    tree = mat.node_tree
    control = tree.nodes.new("ShaderNodeGroup")
    control.node_tree, control.name, control.label = group, NODE, NODE
    control.width = 185
    tree.links.new(uv_socket, control.inputs["UV"])
    tree.links.new(control.outputs["UV"], texture_node.inputs["Vector"])
    uv_socket.node.location, control.location, texture_node.location = (-780, 200), (-555, 200), (-290, 200)
    texture_node.extension = "CLIP"
    return control
