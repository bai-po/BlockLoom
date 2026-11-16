"""Four native eye expressions and independently rotatable eye controls."""

from math import isclose

import bpy
from mathutils import Vector

from ...util.geometry_nodes import input_path
from .human_armor import animated
from .human_face_controls import _keep, _place, bounds
from .human_face_single import SWITCH as SKIN_SWITCH
from .human_face_single import threshold as skin_threshold
from .human_face_switches import _edit

SWITCH = "眼睛表情"
TRACK = "眼睛表情·轨道"
_TRAVEL = "眼睛表情 · 关闭／闭眼／红心／蚊香"
FIVE_STOPS = "眼睛表情 · 关闭／闭眼／红心／蚊香／黑脸"
BROW_SWITCH = "眉毛开关"
BROW_TRACK = "眉毛开关·轨道"
BROW_THRESHOLD = -.02


def positions(rig) -> tuple[float, ...]:
    """Local Z centres, including the fifth stop after the native rig upgrade."""
    limits = rig.pose.bones[SWITCH].constraints
    from .human_solid_brow import SIX
    if limit := limits.get(SIX):
        return (*[limit.min_z * i / 4 for i in range(5)], limit.max_z)
    limit = limits.get(FIVE_STOPS)
    if limit:
        return tuple(limit.min_z * index / 4 for index in range(5))
    bottom = limits[_TRAVEL].min_z
    return (0.0, bottom / 3, 2 * bottom / 3, bottom)


def mode_expression(driver, rig, name="emote"):
    variable = driver.variables.new()
    variable.name, variable.type = name, "TRANSFORMS"
    target = variable.targets[0]
    target.id, target.bone_target = rig, SWITCH
    target.transform_type, target.transform_space = "LOC_Z", "LOCAL_SPACE"
    stops = positions(rig)
    return f"min({len(stops)-1},max(0,floor(-{variable.name}/{-stops[1]:.9g}+0.5)))"


def drive_mode(transaction, owner, path, rig):
    if animated(owner, path):
        raise ValueError("眼睛表情与已有动画冲突：" + path)
    driver = owner.driver_add(path).driver
    transaction.on_rollback(lambda: owner.driver_remove(path))
    driver.type = "SCRIPTED"
    driver.expression = mode_expression(driver, rig)


def _cell_shape(transaction, count, label):
    mesh = bpy.data.meshes.new("白珀面控 · " + label)
    transaction.track(mesh)
    vertices = [(x, 0, i/count-.5) for i in range(count+1) for x in (-.5, .5)]
    edges = [(2*i, 2*i+1) for i in range(count+1)] + [(i, i+2) for i in range(2*count)]
    mesh.from_pydata(vertices, edges, [])
    obj = bpy.data.objects.new("WGT·白珀" + label, mesh)
    transaction.track(obj)
    return obj


def _native_cape_shape(bone, name, width, height, rail):
    from .human import _matches

    shape = bone.custom_shape
    if (not shape or shape.type != "MESH" or not _matches(shape, name)
            or bone.custom_shape_transform or bone.use_custom_shape_bone_size
            or bone.custom_shape_translation.length > 1e-6
            or any(abs(x) > 1e-6 for x in bone.custom_shape_rotation_euler)
            or any(abs(x - 1) > 1e-6 for x in bone.custom_shape_scale_xyz)):
        return False
    corners = [(-1, 1), (1, 1), (1, -1), (-1, -1)]
    outline = [(0, 1), (1, 2), (2, 3), (3, 0)]
    if rail:
        corners.extend([(-1, 0), (1, 0)])
        outline.append((4, 5))
    mesh = shape.data
    return (len(mesh.vertices) == len(corners)
            and all((v.co - Vector((x * width / 2, 0, z * height / 2))).length < 1e-5
                    for v, (x, z) in zip(mesh.vertices, corners))
            and sorted(tuple(sorted(e.vertices)) for e in mesh.edges) == sorted(tuple(sorted(e)) for e in outline))


def _clear_cape_overlap(transaction, rig):
    """The full authoring rig retains its cape selector. Runtime bodies may omit it."""
    from .human_back import MODE_BONES

    bones = rig.pose.bones
    rail, knob = (bones.get(name) for name in MODE_BONES)
    widgets = [b for b in (rail, knob) if b and b.custom_shape and b.custom_shape.type == "MESH"]
    track = bounds(bones[TRACK])
    if not any(all(min(track[i][1], rect[i][1]) > max(track[i][0], rect[i][0]) for i in (0, 2))
               for rect in (bounds(b) for b in widgets)):
        return
    frame, head_knob = bounds(bones["面部切换框"]), bounds(bones["面部兼容"])
    width, height = frame[0][1] - frame[0][0], frame[2][1] - frame[2][0]
    size = head_knob[0][1] - head_knob[0][0]
    if (not rail or not knob or not _native_cape_shape(rail, "WGT·背饰两段轨道", width, height, True)
            or not _native_cape_shape(knob, "WGT·背饰滑柄", size, size, False)
            or any(animated(rig, b.path_from_id(prop)) for b in widgets
                   for prop in ("custom_shape_translation", "custom_shape_scale_xyz", "custom_shape_rotation_euler"))):
        raise ValueError("自定义披风控制器与眼睛表情轨道重叠，需先调整其显示位置：" + rig.name)
    gap = frame[0][0] - track[0][1]
    dx = track[0][0] - gap - max(bounds(b)[0][1] for b in widgets)
    for bone in widgets:
        _keep(transaction, bone, "custom_shape_translation")
        bone.custom_shape_translation += bone.bone.matrix_local.to_3x3().inverted() @ Vector((dx, 0, 0))


def _slider(transaction, rig):
    pose = rig.pose.bones
    head_frame, head_knob = pose["面部切换框"], pose["面部兼容"]
    (left, right), (frame_y, _), (low, high) = bounds(head_frame)
    width = right - left
    # Match the column of mouth switches below. The top and bottom straddle
    # the top and midpoint of the original head selector, as in the drawing.
    mouth_bounds = bounds(pose["嘴巴表情切换·三档轨道"])[0]
    x = sum(mouth_bounds) / 2
    top, bottom = high + .2 * width, (high + low) / 2 - .2 * width
    height = top - bottom
    if min(width, height) <= 0 or x + width / 2 >= left:
        raise ValueError("眼部原生控制器的位置与四档滑轨冲突：" + rig.name)
    step = height / 4
    center = Vector((x, frame_y, (bottom + top) / 2))
    start = Vector((x, bounds(head_knob)[1][0], top - step / 2))
    if SWITCH in pose or TRACK in pose:
        raise ValueError("人模已有同名眼睛表情骨：" + rig.name)

    def remove():
        with _edit(rig) as bones:
            for name in (SWITCH, TRACK):
                if name in bones:
                    bones.remove(bones[name])

    transaction.on_rollback(remove)
    with _edit(rig) as bones:
        for name, point in ((TRACK, center), (SWITCH, start)):
            bone = bones.new(name)
            bone.head, bone.tail = point, point + Vector((0, width, 0))
            bone.align_roll(Vector((0, 0, 1)))
            bone.use_deform = False
            bone.parent = bones["头"]
            for collection in bones["面部兼容"].collections:
                collection.assign(bone)

    rail, handle = pose[TRACK], pose[SWITCH]
    rail.custom_shape = _cell_shape(transaction, 4, "眼睛表情四档轨道")
    handle.custom_shape = pose[SKIN_SWITCH].custom_shape
    button_size = min(width * .72, step * .8)
    for pb, point, w, h, reference in (
            (rail, center, width, height, head_frame),
            (handle, start, button_size, button_size, head_knob)):
        pb.use_custom_shape_bone_size = False
        _place(transaction, pb, point, w, h, reference.color)
        pb.custom_shape_wire_width = reference.custom_shape_wire_width
        pb.lock_rotation = pb.lock_scale = (True, True, True)
        pb.lock_rotation_w = True
    rail.bone.hide_select = True
    rail.lock_location = (True, True, True)
    handle.lock_location = (True, True, False)
    limit = handle.constraints.new("LIMIT_LOCATION")
    limit.name, limit.owner_space, limit.use_transform_limit = _TRAVEL, "LOCAL", True
    for axis in "xyz":
        setattr(limit, "use_min_" + axis, True)
        setattr(limit, "use_max_" + axis, True)
    limit.min_z = -3 * step


def _unlock_roll(transaction, rig, eye):
    limits = [c for c in eye.constraints if c.type == "LIMIT_ROTATION"]
    if len(limits) != 1 or limits[0].owner_space != "LOCAL":
        raise ValueError("眼睛的局部旋转约束已自定义：" + eye.name)
    limit = limits[0]
    # Y is the outward face normal. Put it outside the middle Euler axis so
    # locking X/Z does not fold a turn past 90 degrees back onto itself.
    _keep(transaction, limit, "use_limit_y", "euler_order")
    limit.use_limit_y, limit.euler_order = False, "YXZ"
    _keep(transaction, eye, "lock_rotation")
    eye.lock_rotation = (eye.lock_rotation[0], False, eye.lock_rotation[2])
    if eye.rotation_mode == "QUATERNION" and all(
            not animated(rig, eye.path_from_id(path))
            for path in ("rotation_quaternion", "rotation_euler", "rotation_axis_angle", "rotation_mode")):
        _keep(transaction, eye, "rotation_mode")
        eye.rotation_mode = "YXZ"


def _original_brow_drivers(rig, objects):
    result = []
    legacy = bool(rig.get("bp_face_legacy_keys"))
    for obj in objects:
        mod = obj.modifiers.get("白珀 · 主眉与副眉")
        if not mod:
            continue
        for side, label, op in (("L", "左眼表情", ">="), ("R", "右眼表情", "<=")):
            name = side + ".眉纹切换"
            limit = rig.pose.bones[name].constraints[0]
            threshold = (limit.max_x if side == "L" else limit.min_x) / 2
            socket = mod.node_group.interface.items_tree[label]
            curve = obj.animation_data.drivers.find(input_path(mod, socket.identifier)) if obj.animation_data else None
            if not curve or curve.driver.type != "SCRIPTED":
                raise ValueError("缺少原生眉纹驱动：" + obj.name + " / " + name)
            driver = curve.driver
            controls = [("v", name, "LOC_X")]
            expression = driver.expression
            if legacy:
                controls.append(("master", "眉纹切换", "LOC_Z"))
                suffix = ") or master < -0.072"
                if not (expression.startswith("(") and expression.endswith(suffix)):
                    raise ValueError("旧共用眉纹驱动表达式已自定义：" + obj.name + " / " + name)
                expression = expression[1:-len(suffix)]
            parts = expression.split()
            if len(parts) != 3 or parts[:2] != ["v", op] or not isclose(float(parts[2]), threshold, abs_tol=1e-7):
                raise ValueError("眉纹驱动表达式已自定义：" + obj.name + " / " + name)
            variables = [(v.name, v.targets[0].bone_target, v.targets[0].transform_type) for v in driver.variables
                         if v.type == "TRANSFORMS" and v.targets[0].id == rig
                         and v.targets[0].transform_space == "LOCAL_SPACE"]
            if variables != controls or len(variables) != len(driver.variables):
                raise ValueError("眉纹驱动引用已自定义：" + obj.name + " / " + name)
            result.append((side, driver))
    if not result:
        raise ValueError("缺少原生眉纹控制：" + rig.name)
    return result


def _using_brows(rig):
    pose = rig.pose.bones
    for side, sign in (("L", 1), ("R", -1)):
        pb = pose[side + ".眉纹切换"]
        limit = pb.constraints[0]
        if (animated(rig, pb.path_from_id("location"))
                or sign * pb.location.x >= max(abs(limit.min_x), abs(limit.max_x)) / 2):
            return True
    master = pose.get("眉纹切换")
    return bool(master and rig.get("bp_face_legacy_keys")
                and (animated(rig, master.path_from_id("location")) or master.location.z < -.072))


def _skin_hide(rig, pose, side):
    bone = pose if hasattr(pose, "hide") else pose.bone
    owner, path = bone.id_data, bone.path_from_id("hide")
    curve = owner.animation_data.drivers.find(path) if owner.animation_data else None
    if not curve or curve.mute or curve.lock or curve.keyframe_points:
        return
    limit = skin_threshold(rig)
    expected = f"slide <= {-limit:.9g}" if side == "R" else f"slide >= {limit:.9g}"
    driver = curve.driver
    if driver.type != "SCRIPTED" or driver.expression != expected or len(driver.variables) != 1:
        return
    var = driver.variables[0]
    target = var.targets[0]
    if ((var.name, var.type, target.id, target.bone_target, target.transform_type, target.transform_space)
            != ("slide", "TRANSFORMS", rig, SKIN_SWITCH, "LOC_X", "LOCAL_SPACE")):
        return
    if len(curve.modifiers) != 1 or curve.modifiers[0].type != "GENERATOR":
        return
    modifier = curve.modifiers[0]
    if (modifier.mode != "POLYNOMIAL" or tuple(modifier.coefficients) != (0, 1)
            or modifier.use_restricted_range or modifier.use_additive or modifier.use_influence or modifier.mute):
        return

    return bone, owner, path, curve


def _show_eye_switch(transaction, rig, side):
    original = _skin_hide(rig, rig.pose.bones[side + ".眉纹切换"], side)
    if not original:
        return
    bone, owner, path, curve = original
    expected = curve.driver.expression

    def restore():
        restored = owner.driver_add(path).driver
        restored_var = restored.variables.new()
        restored_var.name, restored_var.type = "slide", "TRANSFORMS"
        restored_target = restored_var.targets[0]
        restored_target.id, restored_target.bone_target = rig, SKIN_SWITCH
        restored_target.transform_type, restored_target.transform_space = "LOC_X", "LOCAL_SPACE"
        restored.expression = expected

    owner.driver_remove(path)
    transaction.on_rollback(restore)
    _keep(transaction, bone, "hide")
    bone.hide = False


def _show_glyph_rotation(transaction, rig, side, upper):
    original = _skin_hide(rig, rig.pose.bones[side + ".眼睛"], side)
    if not original:
        return
    driver = original[-1].driver
    previous = driver.expression
    _keep(transaction, driver, "expression")
    for source in upper.variables:
        variable = driver.variables.new()
        transaction.on_rollback(lambda v=variable: driver.variables.remove(v))
        variable.name, variable.type = source.name, "TRANSFORMS"
        target, old_target = variable.targets[0], source.targets[0]
        target.id, target.bone_target = rig, old_target.bone_target
        target.transform_type, target.transform_space = old_target.transform_type, "LOCAL_SPACE"
    mode = mode_expression(driver, rig)
    variable = driver.variables[-1]
    transaction.on_rollback(lambda: driver.variables.remove(variable))
    driver.expression = f"({previous}) and not ({mode} >= 2 and ({upper.expression}))"


def install(transaction, rig, objects):
    if SKIN_SWITCH not in rig.pose.bones:
        return
    drivers = _original_brow_drivers(rig, objects)
    preserve_brows = transaction.owner is not None and _using_brows(rig)
    _slider(transaction, rig)
    _clear_cape_overlap(transaction, rig)
    for side in ("L", "R"):
        _unlock_roll(transaction, rig, rig.pose.bones[side + ".眼睛"])
        _show_eye_switch(transaction, rig, side)
        upper = next(driver for name, driver in drivers if name == side)
        _show_glyph_rotation(transaction, rig, side, upper)
    if preserve_brows:
        rig.pose.bones[SWITCH].location.z = positions(rig)[1]
    for _, driver in drivers:
        previous = driver.expression
        mode = mode_expression(driver, rig)
        var = driver.variables[-1]
        transaction.on_rollback(lambda d=driver, v=var, s=previous: _restore_driver(d, v, s))
        driver.expression = f"({previous}) and ({mode} == 1)"


def align_column(transaction, rig):
    """Align both visible rails to the head selector without moving keyed bones."""
    bones = rig.pose.bones
    head = bounds(bones["面部切换框"])[2]
    for track, knob, edge in ((TRACK, SWITCH, 1), (BROW_TRACK, BROW_SWITCH, 0)):
        dz = head[edge] - bounds(bones[track])[2][edge]
        for name in (track, knob):
            bone = bones[name]
            _keep(transaction, bone, "custom_shape_translation")
            bone.custom_shape_translation += bone.bone.matrix_local.to_3x3().inverted() @ Vector((0, 0, dz))


def _restore_driver(driver, variable, expression):
    driver.expression = expression
    driver.variables.remove(variable)


def upgrade_controls(transaction, rig):
    """Extend the existing four-stop rail without shifting any old keyframes."""
    from . import human_eye_switch_upgrade as migration

    pose = rig.pose.bones
    if BROW_TRACK in pose:
        raise ValueError("眉毛开关轨道已占用：" + rig.name)
    rail, handle = pose[TRACK], pose[SWITCH]
    limit = handle.constraints[_TRAVEL]
    step = -positions(rig)[1]
    (left, right), (depth, _), (low, high) = bounds(rail)
    width, x = right-left, (left+right)/2
    bottom = low-step
    brow_top = bottom-.12*width
    brow_height = min(.10, brow_top-bounds(pose["面部切换框"])[2][0])
    stroke = -2*BROW_THRESHOLD
    if brow_height < stroke*1.65:
        raise ValueError("现有头型轨道与眉毛开关空间冲突：" + rig.name)
    center = Vector((x, depth, brow_top-brow_height/2))
    start = Vector((x, bounds(handle)[1][0], center.z+stroke/2))
    migration.rename(transaction, rig, BROW_SWITCH)
    transaction.on_rollback(lambda shape=rail.custom_shape: setattr(rail, "custom_shape", shape))
    rail.custom_shape = _cell_shape(transaction, 5, "眼睛表情五档轨道")
    _place(transaction, rail, (x, depth, (high+bottom)/2), width, high-bottom, pose["面部切换框"].color)
    _keep(transaction, limit, "min_z", "name")
    limit.min_z, limit.name = -4*step, FIVE_STOPS

    with _edit(rig) as bones:
        # The obsolete far-left master is now the brow switch itself. Its old
        # single-channel animation has already been moved off this control.
        knob = bones[BROW_SWITCH]
        old_parent, old_head, old_tail, old_roll = knob.parent.name, knob.head.copy(), knob.tail.copy(), knob.roll
        def restore_bones():
            with _edit(rig) as edits:
                if BROW_TRACK in edits:
                    edits.remove(edits[BROW_TRACK])
                old_knob = edits[BROW_SWITCH]
                old_knob.parent = edits[old_parent]
                old_knob.head, old_knob.tail, old_knob.roll = old_head, old_tail, old_roll
        transaction.on_rollback(restore_bones)
        frame = bones.new(BROW_TRACK)
        for bone, point in ((frame, center), (knob, start)):
            bone.parent = bones["头"]
            bone.head, bone.tail = point, point+Vector((0,width,0))
            bone.align_roll(Vector((0,0,1)))
            bone.use_deform = False
        for collection in bones[TRACK].collections:
            collection.assign(frame)

    frame, knob = pose[BROW_TRACK], pose[BROW_SWITCH]
    frame.custom_shape = _cell_shape(transaction, 2, "眉毛两档轨道")
    frame.use_custom_shape_bone_size = False
    _place(transaction,frame,center,width,brow_height,rail.color)
    frame.custom_shape_wire_width = rail.custom_shape_wire_width
    frame.bone.hide_select = True
    frame.lock_location = frame.lock_rotation = frame.lock_scale = (True,True,True)
    frame.lock_rotation_w = True
    _keep(transaction, knob, "location", "use_custom_shape_bone_size", "custom_shape_wire_width")
    transaction.on_rollback(lambda shape=knob.custom_shape: setattr(knob, "custom_shape", shape))
    knob.location = (0,0,0)
    knob.custom_shape, knob.use_custom_shape_bone_size = handle.custom_shape, False
    size = min(width*.70, brow_height*.43, stroke*.85)
    _place(transaction, knob, start, size, size, handle.color)
    knob.custom_shape_wire_width = handle.custom_shape_wire_width
    _keep(transaction, knob.bone, "hide", "hide_select")
    knob.bone.hide, knob.bone.hide_select = False, False
    travel = knob.constraints[0]
    if travel.type != "LIMIT_LOCATION" or travel.owner_space != "LOCAL":
        raise ValueError("旧眉纹开关局部约束已自定义：" + rig.name)
    _keep(transaction, travel, "min_z", "max_z", "name")
    travel.min_z, travel.max_z, travel.name = -stroke, 0, "眉毛开关 · 两档"
