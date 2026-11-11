"""Packed banner attachment, motion and frame contracts using native drivers."""

import itertools
import json
from contextlib import suppress

import bpy
from mathutils import Euler, Matrix, Vector

from ...core.runtime import ensure_mutation_allowed
from . import asset_catalog as catalog
from .asset_states import register_control

SCHEMA = "oxal_mc_studio_banner_frame_schema"
WIDGET = "oxal_mc_studio_banner_frame_widget"
POINTS = "oxal_mc_studio_banner_frame_points"
_CONVERSION = Matrix(((1, 0, 0), (0, 0, 1), (0, -1, 0)))


def supports(identifier):
    return identifier.split(":")[-1].endswith("_banner")


requires_rig = supports


def _driver(owner, path, expression, targets, index):
    with suppress(TypeError, RuntimeError):
        owner.driver_remove(path, index)
    curve = owner.driver_add(path, index)
    curve.driver.type = "SCRIPTED"
    curve.driver.expression = expression
    for variable in tuple(curve.driver.variables):
        curve.driver.variables.remove(variable)
    for name, item, data_path in targets:
        variable = curve.driver.variables.new()
        variable.name, variable.type = name, "SINGLE_PROP"
        variable.targets[0].id_type = item.id_type
        variable.targets[0].id = item
        variable.targets[0].data_path = data_path


def _part_points(model, part_name):
    model = catalog.read(catalog.ROOT / "entity_geometry.json")[model]
    found = []
    def visit(name, data, parent):
        local = Matrix.Translation(_CONVERSION @ Vector(data.get("pivot", (0, 0, 0))) / 16)
        rotation = _CONVERSION @ Euler(data.get("rotation", (0, 0, 0)), "XYZ").to_matrix() @ _CONVERSION.inverted()
        transform = parent @ local @ rotation.to_4x4()
        if name == part_name:
            for cube in data.get("cubes", ()):
                for corner in itertools.product((0, 1), repeat=3):
                    raw = Vector(tuple(cube["from"][axis] + cube["size"][axis] * corner[axis] for axis in range(3)))
                    found.append(transform @ (_CONVERSION @ raw / 16))
        for label, child in data.get("children", {}).items():
            visit(label, child, transform)
    visit("root", model["root"], Matrix.Identity(4))
    if not found:
        raise ValueError("原版旗帜资源缺少部件：" + part_name)
    return found


def _bounds(points):
    return Vector(tuple(min(point[axis] for point in points) for axis in range(3))), Vector(tuple(max(point[axis] for point in points) for axis in range(3)))


def wall_offset():
    floor_low, floor_high = _bounds(_part_points("banner", "bar"))
    wall_low, wall_high = _bounds(_part_points("wall_banner", "bar"))
    delta = ((wall_low + wall_high) - (floor_low + floor_high)) * (1 / 3)
    low, high = _bounds(_part_points("flag", "flag"))
    extents = high - low
    normal = Vector(tuple(float(axis == min(range(3), key=lambda index: extents[index])) for axis in range(3)))
    low, high = _bounds(_part_points("banner", "pole"))
    extents = high - low
    up = Vector(tuple(float(axis == max(range(3), key=lambda index: extents[index])) for axis in range(3)))
    up = (up - normal * up.dot(normal)).normalized()
    offset = delta - normal * delta.dot(normal) - up * delta.dot(up)
    return Vector(tuple(0.0 if abs(value) < 1e-12 else value for value in offset))


def build(transaction, scene, collection, root, identifier, options=None, material_for=None):
    """Augment only freshly created source parts, before native compilation."""
    if not supports(identifier):
        return False
    ensure_mutation_allowed()
    if root.get(SCHEMA) == 1 and root.get(WIDGET) in transaction.ids:
        return True
    parts = {obj.get("oxal_mc_studio_model_part"): obj for obj in root.children_recursive if obj.type == "EMPTY"}
    if not {"banner_pole", "banner_bar", "flag_flag"} <= parts.keys():
        return False  # A flat context icon has no physical banner hierarchy.
    if any(part not in transaction.ids for part in parts.values()):
        raise ValueError("旗帜部件不属于本次创建")
    offset = wall_offset()
    amplitude_key = "oxal_mc_studio_banner_amplitude"
    declared = json.loads(root.get("oxal_mc_studio_state_controls", "[]"))
    if not any(spec["key"] == amplitude_key for spec in declared):
        register_control(root, amplitude_key, "摆动幅度", "摆动幅度", 0., 1., float(root.get("摆动幅度", .1)))
    flag_root = parts.get("flag_root")
    if flag_root is not None:
        flag_root.driver_remove("rotation_euler", 0)
        flag_root.rotation_euler.x = 0
    flag = parts["flag_flag"]
    _driver(flag, "rotation_euler", "enabled*amount*sin(frame*base/max(fps,1)*2)",
            [("enabled", root, '["banner_motion"]'), ("amount", root, '["摆动幅度"]'), ("base", scene, "render.fps_base"), ("fps", scene, "render.fps")], 0)
    pole = parts["banner_pole"]
    for axis in range(3):
        _driver(pole, "scale", "1-w", [("w", root, '["banner_wall"]')], axis)
    for label in ("banner_bar", "flag_flag"):
        part = parts[label]
        for axis in range(3):
            base = float(part.location[axis])
            _driver(part, "location", repr(base) + "+w*" + repr(float(offset[axis])), [("w", root, '["banner_wall"]')], axis)
    def transform(obj):
        parent = transform(obj.parent) if obj.parent is not None and obj.parent != root else Matrix.Identity(4)
        local = obj.matrix_parent_inverse @ obj.matrix_basis
        if obj == pole:
            local = Matrix.Translation(obj.location) @ obj.rotation_euler.to_matrix().to_4x4()
        if obj == flag:
            local = Matrix.Translation(obj.location) @ Matrix.Diagonal((*obj.scale, 1))
        return parent @ local
    floor, wall = [], []
    for obj in tuple(root.children_recursive):
        if obj.type != "MESH" or obj.get("oxal_mc_studio_effect_geometry"):
            continue
        points = [transform(obj) @ vertex.co for vertex in obj.data.vertices]
        floor.extend(points)
        ancestry = set()
        parent = obj.parent
        while parent is not None and parent != root:
            ancestry.add(parent)
            parent = parent.parent
        if pole not in ancestry:
            wall.extend(point + offset for point in points)
    if not floor or not wall:
        raise ValueError("旗帜控制框资源为空")
    # A real box preserves the local.31 frame contract; the generic asset
    # root cross cannot be resized into a bounding volume.
    vertices = list(itertools.product((-.6, .6), repeat=3))
    edges = [(a, b) for a in range(8) for b in range(a + 1, 8) if sum(vertices[a][axis] != vertices[b][axis] for axis in range(3)) == 1]
    mesh = transaction.track(bpy.data.meshes.new("WGT · 旗帜包围框"))
    mesh.from_pydata(vertices, edges, [])
    widget = transaction.track(bpy.data.objects.new("WGT · 旗帜包围框", mesh))
    widget.hide_render = True
    root[WIDGET] = widget
    root[POINTS] = json.dumps([[list(point) for point in points] for points in (floor, wall)])
    root[SCHEMA] = 1
    return True


augment = build


def post_compile(root, scene=None, options=None):
    if root.get(SCHEMA) != 1:
        return
    ensure_mutation_allowed()
    if root.type != "ARMATURE" or "总控制" not in root.pose.bones or root.get(WIDGET) is None:
        raise ValueError("旗帜缺少原生控制框骨架")
    pose = root.pose.bones["总控制"]
    pose.custom_shape = root[WIDGET]
    pose.use_custom_shape_bone_size = False
    pose.custom_shape_rotation_euler = (0, 0, 0)
    points = [[pose.bone.matrix_local.inverted() @ Vector(point) for point in group] for group in json.loads(root[POINTS])]
    bounds = [_bounds(group) for group in points]
    for axis in range(3):
        centers = [(low[axis] + high[axis]) / 2 for low, high in bounds]
        sizes = [max(.05, high[axis] - low[axis]) * 1.08 / 1.2 for low, high in bounds]
        for name, values in (("custom_shape_translation", centers), ("custom_shape_scale_xyz", sizes)):
            _driver(pose, name, repr(float(values[0])) + "+w*" + repr(float(values[1] - values[0])), [("w", root, '["banner_wall"]')], axis)
    root["旗帜挂墙版本"] = 1
