"""Native face widgets and shape keys shared by the human and humanoid assets."""

from math import radians
from typing import cast

import bpy
from mathutils import Euler, Matrix, Vector

SCHEMA = "bp_face_controls_schema"
CURRENT_SCHEMA = 12
CREATURE_SCHEMA = 5
CREATURE_REPAIRABLE_SCHEMAS = (1, 2, 3, 4)
REPAIRABLE_SCHEMAS = (*CREATURE_REPAIRABLE_SCHEMAS, CREATURE_SCHEMA, 6, 7, 8, 9, 10, 11)
TRACK, SWITCH = "眉纹切换·轨道", "眉纹切换"
ANGLE = radians(30)
SIDES = ("L", "R")
def bounds(bone):
    """Visible rest rectangle, including the widget's own rotation and scale."""
    unit = bone.bone.length if bone.use_custom_shape_bone_size else 1.0
    matrix = ((bone.custom_shape_transform or bone).bone.matrix_local
              @ Matrix.Translation(bone.custom_shape_translation * unit)
              @ Euler(bone.custom_shape_rotation_euler).to_matrix().to_4x4()
              @ Matrix.Diagonal((*[s * unit for s in bone.custom_shape_scale_xyz], 1)))
    points = [matrix @ v.co for v in bone.custom_shape.data.vertices]
    return tuple((min(p[i] for p in points), max(p[i] for p in points)) for i in range(3))


def _keep(transaction, item, *fields):
    for name in fields:
        value = getattr(item, name)
        if hasattr(value, "copy"):
            value = value.copy()
        elif not isinstance(value, (str, int, float, bool)):
            value = tuple(value)
        transaction.on_rollback(lambda o=item, n=name, v=value: setattr(o, n, v))


def _place(transaction, bone, point, width, height, color):
    _keep(transaction, bone, "custom_shape_scale_xyz", "custom_shape_translation")
    bone.custom_shape_scale_xyz = (width, 1, height)
    bone.custom_shape_translation = bone.bone.matrix_local.inverted() @ Vector(point)
    _keep(transaction, bone.color, "palette")
    bone.color.palette = color.palette
    if color.palette == "CUSTOM":
        _keep(transaction, bone.color.custom, "normal", "select", "active")
        for channel in ("normal", "select", "active"):
            setattr(bone.color.custom, channel, getattr(color.custom, channel))


def _layout(transaction, rig, human):
    bones = rig.pose.bones
    for side in SIDES:
        lid = bones[side + ".眼皮"]
        brow, lower = bones[side + ".眉毛"], bones[side + ".下眼皮"]
        if human:
            (xmin, xmax), (front, _), (_, zmax) = bounds(lid)
            span, x = xmax - xmin, (xmax + xmin) / 2
            bottom = bounds(bones["面部控制器位置"])[2][0]
            brow_pos = (x, front - .01, zmax + span * .073)
            outer = 1 if side == "L" else -1
            low_pos = (x + outer * span * .14, front - .01, bottom + span * .06)
        else:
            # Species use their own native eye windows, including juveniles.
            brow_pos, low_pos = brow.bone.head_local, lower.bone.head_local
            span = brow.custom_shape_scale_xyz.x / .9
        _place(transaction, brow, brow_pos, span * .91, span * .043, lid.color)
        _place(transaction, lower, low_pos, span, span * .036, lid.color)
        _keep(transaction, brow, "lock_rotation", "lock_rotation_w")
        brow.lock_rotation, brow.lock_rotation_w = (True, False, True), False
        limit = brow.constraints.new("LIMIT_ROTATION")
        transaction.on_rollback(lambda b=brow, c=limit: b.constraints.remove(c))
        limit.name = "眉毛 · 正面倾角"
        limit.owner_space = "LOCAL"
        limit.use_transform_limit = True
        limit.use_limit_x = limit.use_limit_y = limit.use_limit_z = True
        limit.min_y, limit.max_y = -ANGLE, ANGLE



def _shape(transaction, obj, name, coordinates, rig, control, channel, expression):
    if name in obj.data.shape_keys.key_blocks:
        raise ValueError("面部已有同名形态键：" + name)
    key = obj.shape_key_add(name=name, from_mix=False)

    def undo():
        key.driver_remove("value")
        obj.shape_key_remove(key)

    transaction.on_rollback(undo)
    key.slider_min = -1
    for index, point in coordinates.items():
        key.data[index].co = point
    driver = key.driver_add("value").driver
    driver.type = "SCRIPTED"
    var = driver.variables.new()
    var.name, var.type = "v", "TRANSFORMS"
    target = var.targets[0]
    target.id, target.bone_target = rig, control
    target.transform_space, target.transform_type = "LOCAL_SPACE", channel
    driver.expression = expression


def _brows(rig, objects):
    """Use only the vertices driven by each original, per-variant brow lift."""
    result = []
    for obj in objects:
        if obj.type != "MESH" or not obj.data.shape_keys:
            continue
        keys = obj.data.shape_keys
        curves = {c.data_path: c for c in keys.animation_data.drivers} if keys.animation_data else {}
        for key in keys.key_blocks:
            if not key.name.endswith(("眉高", "眉高微调")):
                continue
            curve = curves.get(key.path_from_id("value"))
            if not curve or len(curve.driver.variables) != 1:
                raise ValueError("眉高驱动已自定义：" + key.name)
            source = curve.driver.variables[0].targets[0]
            if source.id != rig or source.bone_target not in {"L.眉毛", "R.眉毛"}:
                raise ValueError("眉高骨架引用有冲突：" + obj.name)
            result.append((obj, key, source.bone_target))
    if not result:
        raise ValueError("没有找到可独立旋转的原生眉毛：" + rig.name)
    return result


def _brow_points(rig, obj, lift):
    basis = obj.data.shape_keys.reference_key
    to_rig = rig.matrix_world.inverted() @ obj.matrix_world
    points = {v.index: to_rig @ p.co for v, p, q in zip(obj.data.vertices, basis.data, lift.data)
              if (p.co - q.co).length > 1e-6}
    if not points:
        raise ValueError("原生眉高形态键没有控制顶点：" + obj.name + " / " + lift.name)
    return points, to_rig.to_3x3()


def _rotate(transaction, rig, obj, lift, control):
    basis = obj.data.shape_keys.reference_key
    points, to_rig = _brow_points(rig, obj, lift)
    to_mesh = to_rig.inverted()
    center = sum(points.values(), Vector()) / len(points)
    axis = rig.data.bones[control].matrix_local.to_3x3().col[1].normalized()
    # Rodrigues' rotation in the face plane. Two independent shape keys retain
    # the native eyelid animation and add the user-controlled signed angle.
    tangent = {i: axis.cross(p - center) for i, p in points.items()}
    for suffix, expression, offsets in (
            ("·倾角", "sin(v)", tangent),
            ("·倾角收缩", "1-cos(v)", {i: axis.cross(v) for i, v in tangent.items()})):
        coords = {i: basis.data[i].co + to_mesh @ delta for i, delta in offsets.items()}
        _shape(transaction, obj, lift.name + suffix, coords, rig, control, "ROT_Y", expression)
    return points, to_mesh


def _materials(objects):
    return {slot.material for obj in objects for slot in obj.material_slots
            if slot.material and slot.material.get("bp_face_brow_shader")}


def _retire_brow_tile(transaction, material):
    if material.get("bp_face_native_lids"):
        return
    tree = material.node_tree
    region, = [n for n in tree.nodes if n.type == "ATTRIBUTE" and n.attribute_name == "BP_BrowSurface"]
    _keep(transaction, region, "attribute_name")
    region.attribute_name = "BP_RetiredBrowTile"
    transaction.on_rollback(lambda: material.__delitem__("bp_face_native_lids"))
    material["bp_face_native_lids"] = 1

def refine(transaction, rig, objects):
    version = rig.get(SCHEMA)
    if version == CURRENT_SCHEMA:
        return
    if version not in REPAIRABLE_SCHEMAS:
        raise ValueError("无法识别面部控制版本：" + rig.name)
    from . import (
        human_eye_diagram,
        human_eye_glyphs,
        human_eye_selector,
        human_face_mouth,
        human_face_preview,
        human_face_seam,
        human_face_single,
    )

    if version == CREATURE_SCHEMA and human_face_single.SWITCH not in rig.pose.bones:
        if any(o.type == "MESH" and "V3_HeadVariant" in o.data.attributes for o in objects):
            raise ValueError("此人模缺少原有的单眼皮肤控制器：" + rig.name)
        return
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned

        require_owned(transaction.owner, (rig, rig.data))
        require_no_external_users(cast(bpy.types.Scene, bpy.context.scene), transaction.owner, (rig.data,))
    if version < 4:
        _refine_brows(transaction, rig, objects, version)
    if version < 5:
        human_face_seam.install(transaction, rig, objects)
        human_face_single.install(transaction, rig, objects)
        human_face_mouth.install(transaction, rig, objects)
        human_face_preview.install(transaction, rig, objects)
    human = human_face_single.SWITCH in rig.pose.bones
    if human and version < 6:
        human_eye_selector.install(transaction, rig, objects)
        human_eye_glyphs.install(transaction, rig, objects)
        human_eye_diagram.install(transaction, rig, objects)
    if human and version < 7:
        from . import human_eye_geometry, human_face_brow_mode, human_face_shadow

        human_eye_selector.upgrade_controls(transaction, rig)
        human_face_brow_mode.install(transaction, rig, objects)
        human_eye_geometry.install(transaction, rig, objects)
        human_eye_diagram.upgrade_brow_preview(transaction, rig, objects)
        human_face_shadow.install(transaction, rig, objects)
    if human and version < 8:
        from . import human_face_shadow

        if version == 7:
            human_face_shadow.upgrade_mask(transaction, objects)
        human_face_shadow.follow_face(transaction, rig, objects)
    if human and version < 9:
        human_eye_selector.align_column(transaction, rig)
    if human and version < 10:
        from . import human_solid_brow
        human_solid_brow.install(transaction, rig, objects)
    if human and version < 11:
        from . import human_closed_lids
        human_closed_lids.refine_middle_eye_uv(transaction, objects)
    if human and version < 12:
        from . import human_closed_lids
        human_closed_lids.preserve_shared_skin(transaction, rig, objects)
    transaction.on_rollback(lambda: rig.__setitem__(SCHEMA, version))
    rig[SCHEMA] = CURRENT_SCHEMA if human else CREATURE_SCHEMA


def _refine_brows(transaction, rig, objects, version):
    from .human_face_finish import finish, fit_brow_depth

    targets = _brows(rig, objects)
    if version == 3:
        if transaction.owner is not None:
            from ...util.ownership import require_no_external_users, require_owned

            meshes = {obj.data for obj, _, _ in targets}
            require_owned(transaction.owner, (rig, *meshes))
            require_no_external_users(cast(bpy.types.Scene, bpy.context.scene), transaction.owner, meshes)
        fit_brow_depth(transaction, rig, targets)
        return
    from . import human_face_strips as strips
    from .human_face_switches import switches

    objects_by_key = {obj for obj, _, _ in targets}
    eyes = [obj for obj in objects if obj.type == "MESH" and obj.data.shape_keys and any(
        key.name.endswith(("::L下眼皮", "L·下眼皮微调")) for key in obj.data.shape_keys.key_blocks)]
    if not eyes:
        raise ValueError("没有找到原生眼皮：" + rig.name)
    materials = _materials(objects_by_key)
    if not materials:
        raise ValueError("没有找到眉毛原生材质：" + rig.name)
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned

        root = transaction.owner
        meshes = {obj.data for obj in (*objects_by_key, *eyes)}
        require_owned(root, (rig, rig.data, *meshes, *materials))
        require_no_external_users(cast(bpy.types.Scene, bpy.context.scene), root,
                                  [*meshes, *(m.shape_keys for m in meshes), *materials])
    if version == 2:
        finish(transaction, rig, eyes, targets)
        fit_brow_depth(transaction, rig, targets)
        return
    stroke = switches(transaction, rig, objects)
    human = any(k.name.startswith("T0::") for _, k, _ in targets)
    _layout(transaction, rig, human)
    for material in materials:
        _retire_brow_tile(transaction, material)
    graph = strips.build_tree(transaction)
    data = {obj: {} for obj in objects_by_key}
    for obj, key, control in targets:
        points, to_mesh = _rotate(transaction, rig, obj, key, control)

        detached = all(all(i in points for i in p.vertices) for p in obj.data.polygons if any(i in points for i in p.vertices))
        for i, values in strips.offsets(points):
            data[obj][i] = (control.startswith("L."), detached, tuple(to_mesh @ v for v in values))
    for obj, offsets in data.items():
        strips.install(transaction, rig, obj, offsets, graph, stroke)
    finish(transaction, rig, eyes, ())
