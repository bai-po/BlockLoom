"""Copy only a surface's opacity DAG into a private outline material.

asset_id: oxal.outline.alpha; version: 1; policy: NEW_INSTANCE;
scope: caller-owned outline material. Source: the actual base shader/animation.
Shader groups are flattened. Images and original animation remain exact native
references; no BSDF, tint/trim color branch, Action or NLA is duplicated.
"""

from dataclasses import dataclass
from typing import Any

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ..enchantment import enchantment

MAX_NODES = 512
MAX_DEPTH = 64
_NUMERIC = {
    "ShaderNodeMath", "ShaderNodeVectorMath", "ShaderNodeMix", "ShaderNodeMixRGB",
    "ShaderNodeSeparateXYZ", "ShaderNodeCombineXYZ", "ShaderNodeSeparateRGB", "ShaderNodeCombineRGB",
    "ShaderNodeSeparateColor", "ShaderNodeCombineColor", "ShaderNodeRGBToBW", "ShaderNodeMapRange",
    "ShaderNodeMapping", "ShaderNodeVectorTransform", "ShaderNodeTexCoord", "ShaderNodeUVMap",
    "ShaderNodeTexImage", "ShaderNodeAttribute", "ShaderNodeVertexColor", "ShaderNodeNewGeometry",
    "ShaderNodeLightPath", "ShaderNodeLayerWeight", "ShaderNodeFresnel", "ShaderNodeInvert",
    "ShaderNodeClamp", "ShaderNodeGamma", "ShaderNodeHueSaturation", "ShaderNodeValToRGB",
    "ShaderNodeTexNoise", "ShaderNodeTexWhiteNoise", "ShaderNodeTexVoronoi", "ShaderNodeTexWave",
    "ShaderNodeTexGradient", "ShaderNodeTexChecker", "ShaderNodeTexBrick",
}
_OPAQUE = {
    "BSDF_DIFFUSE", "BSDF_GLOSSY", "BSDF_ANISOTROPIC", "BSDF_GLASS", "BSDF_REFRACTION",
    "BSDF_TRANSLUCENT", "BSDF_TOON", "BSDF_VELVET", "BSDF_HAIR", "BSDF_HAIR_PRINCIPLED",
    "EMISSION", "SUBSURFACE_SCATTERING",
}
_UI = {
    "name", "label", "location", "location_absolute", "width", "width_hidden", "height",
    "select", "hide", "show_options", "show_preview", "show_texture", "color", "use_custom_color",
    "warning_propagation", "parent", "dimensions",
}


@dataclass
class _Context:
    tree: Any
    owner: Any
    key: tuple
    inputs: dict


class _Opacity:
    def __init__(self, transaction, material, source):
        self.transaction = transaction
        self.material = material
        self.tree = material.node_tree
        self.source = source
        self.nodes = {}
        self.outputs = {}
        self.pending = set()
        self.animation = {}
        self.created = 0

    def node(self, kind, name):
        self.created += 1
        if self.created > MAX_NODES:
            raise ValueError("描边透明度节点超过512上限，未复制完整着色器")
        node = self.tree.nodes.new(kind)
        node.name = "Outline Alpha " + name
        return node

    def animated(self, context, path):
        tree = context.tree
        if tree not in self.animation:
            channels = list(action_fcurves(tree.animation_data.action,
                            slot=getattr(tree.animation_data, "action_slot", None))) if tree.animation_data else []
            if tree.animation_data:
                channels.extend(tree.animation_data.drivers)
                strips = [strip for track in tree.animation_data.nla_tracks for strip in track.strips]
                while strips:
                    strip = strips.pop()
                    channels.extend(action_fcurves(strip.action, slot=getattr(strip, "action_slot", None)))
                    strips.extend(getattr(strip, "strips", ()))
            self.animation[tree] = {curve.data_path for curve in channels}
        return path in self.animation[tree]

    def bridge(self, target, attribute, source, source_attribute, context, *, source_path=None):
        path = source.path_from_id(source_attribute) if source_path is None else source_path
        if not self.animated(context, path):
            setattr(target, attribute, getattr(source, source_attribute))
            return
        value = getattr(source, source_attribute)
        indices = range(len(value)) if hasattr(value, "__len__") and not isinstance(value, str) else (None,)
        owner = context.owner
        if context.tree.is_embedded_data:
            if not isinstance(owner, bpy.types.Material) or owner.node_tree != context.tree:
                raise ValueError("透明度动画缺少精确原材质拥有者，未连接嵌入节点ID")
            path = "node_tree." + path
        for index in indices:
            curve = target.driver_add(attribute) if index is None else target.driver_add(attribute, index)
            curve.driver.type = "SCRIPTED"
            curve.driver.expression = "value"
            variable = curve.driver.variables.new()
            variable.name, variable.type = "value", "SINGLE_PROP"
            variable.targets[0].id_type = owner.id_type
            variable.targets[0].id = owner
            variable.targets[0].data_path = path + ("[" + str(index) + "]" if index is not None else "")

    def feed(self, socket, value):
        if isinstance(value, bpy.types.NodeSocket):
            self.tree.links.new(value, socket)
        elif isinstance(value, tuple) and socket.type not in {"RGBA", "VECTOR"}:
            if len(value) == 4:
                constant = self.node("ShaderNodeRGB", "Color Conversion")
                constant.outputs[0].default_value = value
            elif len(value) == 3:
                constant = self.node("ShaderNodeCombineXYZ", "Vector Conversion")
                for axis in range(3):
                    constant.inputs[axis].default_value = value[axis]
            else:
                raise ValueError("透明度常量维度不支持原生转换")
            self.tree.links.new(constant.outputs[0], socket)
        elif isinstance(value, (int, float)) and socket.type in {"RGBA", "VECTOR"}:
            constant = self.node("ShaderNodeValue", "Scalar Conversion")
            constant.outputs[0].default_value = value
            self.tree.links.new(constant.outputs[0], socket)
        else:
            socket.default_value = value

    def math(self, operation, *values):
        node = self.node("ShaderNodeMath", operation)
        node.operation = operation
        for socket, value in zip(node.inputs, values):
            self.feed(socket, value)
        return node.outputs[0]

    def group(self, node, context, socket):
        group = node.node_tree
        if group is None or len(context.key) >= MAX_DEPTH:
            raise ValueError("透明度节点组缺失或递归过深，未修改底材")
        if group in context.key:
            raise ValueError("透明度节点组循环引用，未复制")
        outputs = [part for part in group.nodes if part.type == "GROUP_OUTPUT" and part.is_active_output]
        if len(outputs) != 1:
            raise ValueError("透明度节点组没有唯一有效输出")
        internal = next((part for part in outputs[0].inputs if part.identifier == socket.identifier), None)
        if internal is None:
            raise ValueError("透明度节点组输出接口不匹配")
        inputs = {part.identifier: (context, part) for part in node.inputs}
        return _Context(group, group, (*context.key, group, node.as_pointer()), inputs), internal

    def input(self, socket, context):
        if socket.is_linked:
            if len(socket.links) != 1:
                raise ValueError("透明度输入包含多条连接，未猜测使用顺序")
            return self.output(socket.links[0].from_socket, context)
        if not hasattr(socket, "default_value"):
            raise ValueError("透明度输入没有可读取的原生值")
        if self.animated(context, socket.path_from_id("default_value")):
            kind = "ShaderNodeRGB" if socket.type == "RGBA" else "ShaderNodeValue" if socket.type in {"VALUE", "INT", "BOOLEAN"} else None
            if kind is None:
                if socket.type == "VECTOR":
                    node = self.node("ShaderNodeCombineXYZ", "Animated Vector")
                    path = socket.path_from_id("default_value")
                    owner = context.owner
                    if context.tree.is_embedded_data:
                        path = "node_tree." + path
                    for axis in range(3):
                        curve = node.inputs[axis].driver_add("default_value")
                        curve.driver.type, curve.driver.expression = "SCRIPTED", "value"
                        variable = curve.driver.variables.new()
                        variable.name, variable.type = "value", "SINGLE_PROP"
                        target = variable.targets[0]
                        target.id_type, target.id, target.data_path = owner.id_type, owner, path + "[" + str(axis) + "]"
                    return node.outputs[0]
                raise ValueError("透明度包含不支持的动画输入类型：" + socket.type)
            node = self.node(kind, "Animated Input")
            self.bridge(node.outputs[0], "default_value", socket, "default_value", context)
            return node.outputs[0]
        value = socket.default_value
        return tuple(value) if hasattr(value, "__len__") and not isinstance(value, str) else value

    def output(self, socket, context):
        key = (context.key, socket.as_pointer())
        if key in self.outputs:
            return self.outputs[key]
        node = socket.node
        if node.type == "GROUP_INPUT":
            binding = context.inputs.get(socket.identifier)
            if binding is None:
                raise ValueError("透明度组输入缺少实际实例参数")
            result = self.input(binding[1], binding[0])
        elif node.type == "GROUP":
            child, internal = self.group(node, context, socket)
            result = self.input(internal, child)
        elif node.type == "REROUTE":
            result = self.input(node.inputs[0], context)
        elif node.type in {"VALUE", "RGB"}:
            if self.animated(context, socket.path_from_id("default_value")):
                clone = self.node(node.bl_idname, node.name)
                self.bridge(clone.outputs[0], "default_value", socket, "default_value", context)
                result = clone.outputs[0]
            else:
                value = socket.default_value
                result = tuple(value) if hasattr(value, "__len__") and not isinstance(value, str) else value
        else:
            result = self.clone(node, context).outputs[list(node.outputs).index(socket)]
        self.outputs[key] = result
        return result

    def clone(self, source, context):
        if source.bl_idname not in _NUMERIC:
            raise ValueError("描边透明度不支持节点：" + source.bl_idname)
        key = (context.key, source.as_pointer())
        if key in self.pending:
            raise ValueError("透明度节点循环依赖，未复制")
        if key in self.nodes:
            return self.nodes[key]
        self.pending.add(key)
        node = self.node(source.bl_idname, source.name)
        self.nodes[key] = node
        first = ("data_type", "input_type", "operation", "mode", "blend_type")
        properties = {prop.identifier: prop for prop in source.bl_rna.properties
                      if not prop.is_readonly and not prop.identifier.startswith("bl_")
                      and prop.identifier not in _UI and prop.type in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}}
        for name in (*[name for name in first if name in properties], *[name for name in properties if name not in first]):
            self.bridge(node, name, source, name, context)
        for name in ("image", "object", "texture"):
            if hasattr(source, name):
                setattr(node, name, getattr(source, name))
        for name in ("image_user", "texture_mapping", "color_mapping"):
            original, target = getattr(source, name, None), getattr(node, name, None)
            if original is None:
                continue
            if name == "color_mapping" and original.use_color_ramp:
                raise ValueError("图像颜色映射渐变影响透明度，需明确的Alpha节点，未近似复制")
            for prop in original.bl_rna.properties:
                if prop.is_readonly or prop.type not in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}:
                    continue
                path = source.path_from_id() + "." + name + "." + prop.identifier
                self.bridge(target, prop.identifier, original, prop.identifier, context, source_path=path)
        if source.type == "VALTORGB":
            ramp, target = source.color_ramp, node.color_ramp
            target.color_mode, target.hue_interpolation, target.interpolation = ramp.color_mode, ramp.hue_interpolation, ramp.interpolation
            while len(target.elements) > 2:
                target.elements.remove(target.elements[-1])
            for index, part in enumerate(ramp.elements):
                other = target.elements[index] if index < 2 else target.elements.new(part.position)
                self.bridge(other, "position", part, "position", context)
                self.bridge(other, "color", part, "color", context)
        for index, socket in enumerate(source.inputs):
            if not socket.enabled or getattr(socket, "is_unavailable", False):
                continue
            if hasattr(socket, "default_value") or socket.is_linked:
                self.feed(node.inputs[index], self.input(socket, context))
        self.pending.remove(key)
        return node

    def opacity(self, socket, context, depth=0):
        if depth >= MAX_DEPTH:
            raise ValueError("透明度着色器递归过深")
        node = socket.node
        if node.type in {"BSDF_TRANSPARENT", "HOLDOUT"}:
            return 0.0
        if node.type == "BSDF_PRINCIPLED":
            return self.input(node.inputs["Alpha"], context)
        if node.type in _OPAQUE:
            return 1.0
        if node.type == "REROUTE":
            return self.opacity(node.inputs[0].links[0].from_socket, context, depth + 1) if node.inputs[0].is_linked else 0.0
        if node.type == "GROUP":
            child, internal = self.group(node, context, socket)
            return self.opacity(internal.links[0].from_socket, child, depth + 1) if internal.is_linked else 0.0
        if node.type in {"MIX_SHADER", "ADD_SHADER"}:
            start = 1 if node.type == "MIX_SHADER" else 0
            values = [self.opacity(part.links[0].from_socket, context, depth + 1) if part.is_linked else 0.0
                      for part in node.inputs[start:start + 2]]
            if node.type == "ADD_SHADER":
                if all(isinstance(value, (int, float)) for value in values):
                    return max(values)
                return self.math("MAXIMUM", *values)
            if all(isinstance(value, (int, float)) for value in values) and values[0] == values[1]:
                # A mix of equally opaque closures does not depend on its
                # color/trim factor. Do not visit that unrelated branch.
                return values[0]
            factor = self.input(node.inputs[0], context)
            factor = (min(1.0, max(0.0, factor)) if isinstance(factor, (int, float))
                      else self.math("MINIMUM", 1, self.math("MAXIMUM", 0, factor)))
            return self.math("ADD", self.math("MULTIPLY", values[0], self.math("SUBTRACT", 1, factor)),
                             self.math("MULTIPLY", values[1], factor))
        raise ValueError("描边透明度不支持着色闭包：" + node.bl_idname)


def build(transaction, new_outline_material, base_material):
    """Return one native float/socket; caller owns publication and rollback."""
    ensure_mutation_allowed()
    if (new_outline_material == base_material or new_outline_material.node_tree is None
            or new_outline_material not in transaction.ids):
        raise ValueError("描边Alpha需要独立的新节点材质")
    if base_material is None:
        return 1.0
    surface = enchantment._surface(base_material)
    shader = surface.links[0].from_socket
    info = enchantment.inspect_source(transaction, base_material)
    if info is not None:
        mix = base_material.node_tree.nodes[enchantment.PREFIX + "Mix"]
        shader = mix.inputs[1].links[0].from_socket
        if base_material.get(enchantment.OUTLINE_SHADER) in {1, 2, 3}:
            outline = base_material.node_tree.nodes.get(enchantment.PREFIX + "Outline Surface")
            if outline is None or shader != outline.outputs[0] or not outline.inputs[1].is_linked:
                raise ValueError("附魔描边原表面已被改写，未猜测透明度")
            shader = outline.inputs[1].links[0].from_socket
    builder = _Opacity(transaction, new_outline_material, base_material)
    context = _Context(base_material.node_tree, base_material, (base_material,), {})
    result = builder.opacity(shader, context)
    if isinstance(result, tuple):
        result = builder.math("MULTIPLY", result, 1.0)
    new_outline_material["oxal_mc_studio_outline_alpha_source"] = base_material
    return result
