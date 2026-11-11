"""Read-only proof for retiring only unchanged legacy celestial helpers."""

import ast
import hashlib
import json
from pathlib import Path

from ...util.node_layout import is_layout_frame
from ...util.ownership import require_owned


def _value(value):
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if isinstance(value, float):
        return round(value, 7)
    return [_value(part) for part in value]


def _animation(owner):
    animation = owner.animation_data
    if animation and (animation.action or any(track.strips for track in animation.nla_tracks)):
        raise ValueError("旧日月辅助对象或材质包含用户 Action/NLA，未隐藏其动画")


def _drivers(owner, roles, *, helper=False):
    from .sky import _native_expression

    _animation(owner)
    animation = owner.animation_data
    curves = tuple(animation.drivers) if animation else ()
    result = []
    for curve in curves:
        if helper and curve.data_path in {"hide_render", "hide_viewport"}:
            continue
        variables = []
        for variable in curve.driver.variables:
            targets = []
            for target in variable.targets:
                role = "SELF" if target.id == owner else roles.get(target.id)
                if role is None:
                    raise ValueError("旧日月辅助对象含无法证明来源的驱动目标，未隐藏")
                targets.append((role, target.data_path, target.bone_target, target.transform_type,
                                target.transform_space, target.rotation_mode))
            variables.append((variable.name, variable.type, targets))
        expression = ast.dump(ast.parse(_native_expression(curve.driver.expression), mode="eval"))
        modifiers = []
        for modifier in curve.modifiers:
            if modifier.type != "GENERATOR":
                raise ValueError("旧日月辅助对象含自定义曲线修改器，未隐藏")
            modifiers.append((modifier.type, modifier.mode, modifier.poly_order,
                              _value(modifier.coefficients), modifier.mute, modifier.influence,
                              modifier.use_additive, modifier.use_restricted_range))
        result.append((curve.data_path, curve.array_index, curve.driver.type, expression,
                       curve.driver.use_self, curve.mute, variables, modifiers))
    return sorted(result)


def _tree(tree, roles, members, seen=None):
    seen = set() if seen is None else seen
    if tree in seen:
        raise ValueError("旧日月材质节点组递归，未隐藏")
    seen = {*seen, tree}
    members.add(tree)
    drivers = _drivers(tree, roles)
    animated = {(entry[0], entry[1]) for entry in drivers}
    records = []
    for node in sorted(tree.nodes, key=lambda node: node.name):
        if is_layout_frame(node):
            continue
        values = []
        for direction in ("inputs", "outputs"):
            for index, socket in enumerate(getattr(node, direction)):
                if hasattr(socket, "default_value"):
                    path = socket.path_from_id("default_value")
                    value = _value(socket.default_value)
                    if isinstance(value, list):
                        value = [(axis, part) for axis, part in enumerate(value) if (path, axis) not in animated]
                    elif (path, 0) in animated:
                        continue
                    values.append((direction, index, socket.identifier, value))
        options: list[tuple[str, object]] = [(key, _value(getattr(node, key))) for key in (
            "operation", "blend_type", "data_type", "vector_type", "interpolation",
            "projection", "projection_blend", "extension", "use_clamp", "uv_map",
        ) if hasattr(node, key)]
        if getattr(node, "color_ramp", None) is not None:
            ramp = node.color_ramp
            options.append(("ramp", (ramp.color_mode, ramp.interpolation, ramp.hue_interpolation,
                                     [(point.position, _value(point.color)) for point in ramp.elements])))
        image = getattr(node, "image", None)
        if image is not None:
            members.add(image)
            if image.packed_file is None or image.is_dirty:
                raise ValueError("旧日月辅助材质的纹理已被替换或解除打包，未隐藏")
            options.append(("image", (tuple(image.size), image.colorspace_settings.name,
                                      hashlib.sha256(image.packed_file.data).hexdigest())))
        group = getattr(node, "node_tree", None)
        child = _tree(group, roles, members, seen) if group else None
        records.append((node.name, node.bl_idname, values, options, child))
    links = sorted((link.from_node.name, list(link.from_node.outputs).index(link.from_socket),
                    link.to_node.name, list(link.to_node.inputs).index(link.to_socket)) for link in tree.links)
    return records, links, drivers


def fingerprint(obj, controller, *, root=None):
    """Source names identify no scene data here: all target roles use exact IDs."""
    members = {obj, obj.data, controller}
    roles = {controller: "CONTROLLER"}
    chain, parent = [], obj
    while parent is not None:
        if parent in chain:
            raise ValueError("旧日月辅助对象父级循环，未隐藏")
        chain.append(parent)
        roles[parent] = "CHAIN_" + str(len(chain) - 1)
        members.add(parent)
        parent = parent.parent
    transforms = []
    for index, member in enumerate(chain):
        if member.constraints:
            raise ValueError("旧日月辅助对象或父级含约束，无法证明源契约，未隐藏")
        if member.modifiers:
            raise ValueError("旧日月辅助对象或父级含修改器，未隐藏用户模型")
        drivers = _drivers(member, roles, helper=index == 0)
        paths = {(entry[0], entry[1]) for entry in drivers}
        values = []
        for name in ("location", "rotation_euler", "rotation_quaternion", "scale",
                     "delta_location", "delta_rotation_euler", "delta_rotation_quaternion", "delta_scale"):
            if name in {"rotation_euler", "delta_rotation_euler"} and member.rotation_mode == "QUATERNION":
                continue
            if name in {"rotation_quaternion", "delta_rotation_quaternion"} and member.rotation_mode != "QUATERNION":
                continue
            values.append((name, [(axis, _value(value)) for axis, value in enumerate(getattr(member, name))
                                  if (name, axis) not in paths]))
        transforms.append((member.type, member.rotation_mode, member.parent_type, member.parent_bone,
                           _value(member.matrix_parent_inverse), values, drivers))
    mesh = obj.data
    if mesh.shape_keys is not None:
        raise ValueError("旧日月辅助对象含形态键，未隐藏用户模型")
    _animation(mesh)
    geometry = ([_value(vertex.co) for vertex in mesh.vertices],
                [(tuple(polygon.vertices), polygon.material_index, polygon.use_smooth) for polygon in mesh.polygons],
                [(layer.name, layer.active_render, [_value(item.uv) for item in layer.data]) for layer in mesh.uv_layers])
    materials = []
    for material in mesh.materials:
        if material is None or not material.use_nodes or material.node_tree is None:
            raise ValueError("旧日月辅助对象的材质已被替换，未隐藏")
        members.add(material)
        _animation(material)
        materials.append((material.surface_render_method, material.use_transparency_overlap,
                          [(key, getattr(material, key)) for key in ("use_backface_culling", "use_backface_culling_shadow",
                                                                    "use_backface_culling_lightprobe_volume") if hasattr(material, key)],
                          _tree(material.node_tree, roles, members)))
    if root is not None:
        # Embedded shader trees are owned by their material, not the ID registry.
        require_owned(root, (item for item in members if not getattr(item, "is_embedded_data", False)))
    record = (transforms, geometry, materials)
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate(root, controller, roles):
    source = Path(__file__).resolve().parents[2] / "asset/sky/SOURCE.json"
    expected = json.loads(source.read_text(encoding="utf8")).get("celestial_helper_contracts", {})
    for key in ("sun_mesh", "moon_mesh"):
        try:
            actual = fingerprint(roles[key], controller, root=root)
        except (SyntaxError, TypeError, ReferenceError) as error:
            raise ValueError("无法证明旧日月辅助对象的源契约，保持场景不变") from error
        if actual not in expected.get(key, ()):
            raise ValueError("旧日月辅助模型、材质或原生绑定已修改，未隐藏用户内容：" + key)
