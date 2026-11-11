"""Compile independent native rope coordinates and endpoint directions.

oxal.asset.rope / schema 1 / NEW_INSTANCE / instance-owned generated data.
The immutable reference is local.31's seven control rest points and interaction:
endpoint direction changes the sag, while intermediate offsets remain additive.
All evaluation below uses native FCurves; Python only prepares explicit imports.
"""

import json
import math
from typing import Any, cast

import bpy
from mathutils import Matrix, Vector

from ...util import drivers
from ...util.animation import action_fcurves
from ...util.ownership import owns, require_owned
from . import asset_geometry as geometry
from . import asset_rigs

SCHEMA = "oxal_mc_studio_rope_schema"
ROLE = "oxal_mc_studio_rope_role"
CONTRACT = "oxal_mc_studio_rope_curve_contract"


def layout(fishing, *, legacy=False):
    label = "鱼线" if fishing else "拴绳"
    names = [label + "起点", *[label + f"控制 {i}" for i in range(1, 6)], "鱼钩" if fishing else "拴绳终点"]
    tip = (.40625, 0, .40625) if legacy else (.375, 0, .4375)
    start, end = (Vector(tip), Vector((.7, 0, -.6))) if fishing else (Vector(), Vector((1.4, 0, 0)))
    depth = .18 if fishing else .25
    points = [start.lerp(end, i / 6) - Vector((0, 0, depth * i * (6 - i) / 9)) for i in range(7)]
    return label, names, points


def _property(name, axis=None):
    return "[" + json.dumps(name, ensure_ascii=False) + "]" + (f"[{axis}]" if axis is not None else "")


def _bone(name, field, axis=None):
    return "pose.bones[" + json.dumps(name, ensure_ascii=False) + "]." + field + (f"[{axis}]" if axis is not None else "")


def _drive(owner, path, expression, variables, axis=None):
    curve = owner.driver_add(path) if axis is None else owner.driver_add(path, axis)
    geometry.initialize_driver(curve)
    curve.driver.type = "SCRIPTED"
    curve.driver.expression = expression
    if curve.driver.expression != expression:
        raise ValueError("绳线原生表达式超出宿主容量，未截断")
    for name, target, data_path in variables:
        variable = curve.driver.variables.new()
        variable.name, variable.type = name, "SINGLE_PROP"
        variable.targets[0].id_type = target.id_type
        variable.targets[0].id = target
        variable.targets[0].data_path = data_path


def _array(helper, name, values):
    helper[name] = [float(value) for value in values]


def _rotate(helper, key, source, rig, bone):
    """Euler XYZ as three short native rotations, avoiding expression limits."""
    previous = source
    for axis, letter in enumerate("XYZ"):
        name = key + letter
        _array(helper, name, (0, 0, 0))
        for component in range(3):
            first, second = (axis + 1) % 3, (axis + 2) % 3
            variables = [("v", helper, _property(previous, component))]
            expression = "v"
            if component != axis:
                variables = [("u", helper, _property(previous, first)), ("v", helper, _property(previous, second)),
                             ("a", rig, _bone(bone, "rotation_euler", axis))]
                expression = "u*cos(a)-v*sin(a)" if component == first else "u*sin(a)+v*cos(a)"
            _drive(helper, _property(name), expression, variables, component)
        previous = name
    return previous


def curve_record(curve):
    """Only structural settings: animated output coordinates stay native."""
    return json.dumps({"dimensions": curve.dimensions, "resolution": curve.resolution_u,
                       "render_resolution": curve.render_resolution_u, "twist": curve.twist_mode,
                       "splines": [(spline.type, spline.use_cyclic_u, spline.resolution_u,
                                    [(point.handle_left_type, point.handle_right_type, point.radius, point.tilt,
                                      point.weight_softbody) for point in spline.bezier_points]) for spline in curve.splines]},
                      sort_keys=True, separators=(",", ":"))


def build(transaction, collection, rig, line, fishing):
    """Prepare a fresh curve and its own native dependency graph."""
    label, names, points = layout(fishing)
    helper = transaction.track(bpy.data.objects.new(label + " · 原生方向计算（内部）", None))
    collection.objects.link(helper)
    helper.parent, helper.hide_render, helper.hide_select = rig, True, True
    helper.empty_display_size = 0
    helper[ROLE] = "kinematics"
    helper["oxal_mc_studio_model_part"] = label + " · 原生方向计算"
    line[SCHEMA], line[ROLE] = 1, "line"
    chord = points[-1] - points[0]
    direction = (points[-1] - points[-2]).normalized()
    normal = direction - chord * (direction.dot(chord) / chord.length_squared)
    for key, values in (("chord", chord), ("rest_direction", direction), ("normal", normal)):
        _array(helper, key, values)
    helper["length"], helper["projection"] = chord.length, 0.0
    for axis in range(3):
        _drive(helper, _property("chord"), f"e-s+{float(chord[axis])!r}",
               [("s", rig, _bone(names[0], "location", axis)), ("e", rig, _bone(names[-1], "location", axis))], axis)
    rotated = _rotate(helper, "end_rest_", "rest_direction", rig, names[-1])
    components = [(letter, helper, _property("chord", axis)) for axis, letter in enumerate("xyz")]
    _drive(helper, _property("length"), "sqrt(x*x+y*y+z*z)", components)
    _drive(helper, _property("projection"), "(x*a+y*b+z*c)/max(x*x+y*y+z*z,1e-12)", components +
           [(letter, helper, _property(rotated, axis)) for axis, letter in enumerate("abc")])
    for axis in range(3):
        _drive(helper, _property("normal"), "v-c*p", [("v", helper, _property(rotated, axis)),
               ("c", helper, _property("chord", axis)), ("p", helper, _property("projection"))], axis)
    for index in range(1, 6):
        t = index / 6
        offset = points[index] - points[0].lerp(points[-1], t)
        parallel = offset.dot(chord) / chord.length_squared
        perpendicular = offset - chord * parallel
        coefficient = perpendicular.dot(normal) / normal.length_squared / chord.length
        for axis in range(3):
            _drive(rig, _bone(f"{label} · 自动基点 {index}", "location"),
                   f"s+{float(points[0][axis] - points[index][axis])!r}+{t + parallel!r}*c+{coefficient!r}*l*n",
                   [("s", rig, _bone(names[0], "location", axis)), ("c", helper, _property("chord", axis)),
                    ("l", helper, _property("length")), ("n", helper, _property("normal", axis))], axis)
    spline = line.data.splines[0]
    for index, (point, name) in enumerate(zip(spline.bezier_points, names, strict=True)):
        point.handle_left_type = point.handle_right_type = "FREE"
        tangent_name = f"tangent_{index}"
        _array(helper, tangent_name, (0, 0, 0))
        if index == 6:
            # The endpoint's actual pose matrix carries parent rotation too.
            for axis in range(3):
                _drive(helper, _property(tangent_name), "+".join(f"m{k}*{float(direction[k])!r}" for k in range(3)),
                       [(f"m{k}", rig, _bone(name, f"matrix[{k}][{axis}]")) for k in range(3)], axis)
            helper["end_direction_length"], helper["end_segment_length"] = 1.0, 1.0
            _drive(helper, _property("end_direction_length"), "sqrt(x*x+y*y+z*z)",
                   [(letter, helper, _property(tangent_name, axis)) for axis, letter in enumerate("xyz")])
            _drive(helper, _property("end_segment_length"), "sqrt((a-x)**2+(b-y)**2+(c-z)**2)",
                   [(letter, rig, _bone(names[5 + side], "head", axis)) for side, letters in enumerate(("xyz", "abc"))
                    for axis, letter in enumerate(letters)])
            tangent = tangent_name
        else:
            before, after = max(0, index - 1), min(6, index + 1)
            for axis in range(3):
                _drive(helper, _property(tangent_name), "b-a", [("a", rig, _bone(names[before], "head", axis)),
                       ("b", rig, _bone(names[after], "head", axis))], axis)
            tangent = _rotate(helper, f"control_{index}_", tangent_name, rig, name)
        for axis in range(3):
            _drive(point, "co", "p", [("p", rig, _bone(name, "head", axis))], axis)
            variables = [("p", rig, _bone(name, "head", axis)), ("v", helper, _property(tangent, axis))]
            length = ""
            if index == 6:
                length = "*s/max(n,1e-12)"
                variables += [("s", helper, _property("end_segment_length")), ("n", helper, _property("end_direction_length"))]
            divisor = 3 if index in {0, 6} else 6
            for side, sign in (("handle_left", "-"), ("handle_right", "+")):
                _drive(point, side, f"p{sign}v{length}/{divisor}", variables, axis)
    line[CONTRACT] = curve_record(line.data)
    # Keep local control axes and Action paths intact; only the endpoint glyph
    # faces the same working plane as the intermediate rings.
    rig.pose.bones[names[-1]].custom_shape_rotation_euler.x = math.pi / 2
    return helper


def _rna(owner):
    values = {}
    for prop in owner.bl_rna.properties:
        if prop.is_readonly or prop.identifier in {"rna_type", "name", "show_expanded", "is_active", "active"} or prop.type == "COLLECTION":
            continue
        value = getattr(owner, prop.identifier)
        if getattr(prop, "is_array", False):
            value = value.copy() if hasattr(value, "copy") else tuple(value)
        values[prop.identifier] = value
    return values


def _equal(first, second):
    if isinstance(first, dict):
        return isinstance(second, dict) and first.keys() == second.keys() and all(_equal(value, second[key]) for key, value in first.items())
    if isinstance(first, set):
        return first == second
    if isinstance(first, (int, float)) and isinstance(second, (int, float)):
        return abs(first - second) < 1e-6
    if not isinstance(first, (str, bpy.types.ID)) and hasattr(first, "__len__"):
        first, second = cast(Any, first), cast(Any, second)
        return len(first) == len(second) and all(_equal(a, b) for a, b in zip(first, second, strict=True))
    return first == second


def _actions(owner):
    animation = owner.animation_data
    if animation is None:
        return ()

    def nested(strips):
        for strip in strips:
            yield strip
            yield from nested(getattr(strip, "strips", ()))

    bindings = [animation, *[strip for track in animation.nla_tracks for strip in nested(track.strips)]]
    return tuple(curve for binding in bindings for curve in action_fcurves(binding.action, slot=getattr(binding, "action_slot", None)))


def _legacy_check(transaction, line, incoming, rig, fishing):
    label, names, points = layout(fishing, legacy=True)
    curve = line.data
    spline = curve.splines[0] if len(curve.splines) == 1 else None
    if (spline is None or spline.type != "BEZIER" or spline.use_cyclic_u or len(spline.bezier_points) != 7
            or curve.dimensions != "3D" or curve.resolution_u != 64 or curve.render_resolution_u != 64):
        raise ValueError("旧绳线曲线结构已改变，未覆盖")
    for point, coordinate in zip(spline.bezier_points, points, strict=True):
        if (point.handle_left_type != "AUTO" or point.handle_right_type != "AUTO"
                or (point.co - coordinate).length > 1e-6 or abs(point.radius - 1) > 1e-6
                or abs(point.tilt) > 1e-6 or abs(point.weight_softbody) > 1e-6):
            raise ValueError("旧绳线控制点或切线含用户编辑，未覆盖")
    # Ask this Blender runtime for its own AUTO handles. This also catches
    # data-level edits without baking a host-version tangent algorithm in Python.
    proof_data = transaction.track(bpy.data.curves.new("OXAL · 绳线来源校验", "CURVE"))
    proof = transaction.track(bpy.data.objects.new(proof_data.name, proof_data))
    try:
        incoming.users_collection[0].objects.link(proof)
        proof_data.dimensions = "3D"
        expected_spline = proof_data.splines.new("BEZIER")
        expected_spline.bezier_points.add(6)
        for point, coordinate in zip(expected_spline.bezier_points, points, strict=True):
            point.co = coordinate
            point.handle_left_type = point.handle_right_type = "AUTO"
        if any((point.handle_left - expected.handle_left).length > 1e-6
               or (point.handle_right - expected.handle_right).length > 1e-6
               for point, expected in zip(spline.bezier_points, expected_spline.bezier_points, strict=True)):
            raise ValueError("旧绳线自动切线含未知编辑，未覆盖")
    finally:
        transaction.forget(proof)
        transaction.forget(proof_data)
        bpy.data.batch_remove(ids=(proof, proof_data))
    hooks = [modifier for modifier in line.modifiers if modifier.type == "HOOK"]
    if len(hooks) != 7 or [modifier.type for modifier in line.modifiers] != ["HOOK"] * 7 + ([] if fishing else ["NODES"]):
        raise ValueError("旧绳线修改器结构已改变，未覆盖")
    for index, (hook, name) in enumerate(zip(hooks, names, strict=True)):
        expected = incoming.modifiers.new(name, "HOOK")
        try:
            expected.object, expected.subtarget = rig, name
            expected.matrix_inverse = rig.data.bones[name].matrix_local.inverted()
            expected.vertex_indices_set([3 * index, 3 * index + 1, 3 * index + 2])
            if (not _equal(_rna(hook), _rna(expected))
                    or tuple(hook.vertex_indices) != tuple(expected.vertex_indices)):
                raise ValueError("旧绳线Hook绑定或设置已改变，未覆盖")
        finally:
            incoming.modifiers.remove(expected)
    bases = []
    for index in range(1, 6):
        base = rig.pose.bones[f"{label} · 自动基点 {index}"]
        if not _equal(base.matrix_basis, Matrix.Identity(4)):
            raise ValueError("旧绳线内部骨骼含用户姿态，未覆盖")
        if len(base.constraints) != 2:
            raise ValueError("旧绳线中段含用户约束，未覆盖")
        saved = []
        for constraint, name, weight in zip(base.constraints, (names[0], names[-1]), (1., index / 6), strict=True):
            expected = incoming.parent.pose.bones[base.name].constraints.new("COPY_LOCATION")
            try:
                expected.target, expected.subtarget = rig, name
                expected.owner_space = expected.target_space = "POSE"
                expected.influence = weight
                if constraint.type != "COPY_LOCATION" or not _equal(_rna(constraint), _rna(expected)):
                    raise ValueError("旧绳线中段绑定已改变，未覆盖")
            finally:
                incoming.parent.pose.bones[base.name].constraints.remove(expected)
            saved.append((constraint.name, _rna(constraint)))
        bases.append((base.name, saved))
    return hooks, bases


def prepare_repair(transaction, root, mapping):
    """Prove an exact rope before the shared rebuild publishes anything."""
    pairs = [(incoming, current) for incoming, current in mapping.items() if incoming.type == "CURVE" and SCHEMA in incoming]
    if not pairs:
        return None
    if len(pairs) != 1:
        raise ValueError("绳线来源不唯一，未修复")
    incoming, line = pairs[0]
    rig = mapping.get(incoming.parent, incoming.parent)
    require_owned(root, (rig, rig.data, line, line.data))
    if line.type != "CURVE" or line.data.library or any(obj != line and obj.data == line.data for obj in bpy.data.objects):
        raise ValueError("绳线曲线数据被共享或外链，未覆盖")
    if any(curve.data_path.startswith("splines[") for curve in _actions(line.data)):
        raise ValueError("绳线曲线含用户Action/NLA动画，未覆盖")
    fishing = root.get("oxal_mc_studio_library_identifier") == "minecraft:fishing_rod"
    label, names, points = layout(fishing, legacy=SCHEMA not in line)
    mode_paths = {_bone(name, "rotation_mode") for name in names}
    if any(rig.pose.bones[name].rotation_mode != "XYZ" for name in names) or any(curve.data_path in mode_paths for curve in _actions(rig)):
        raise ValueError("绳线控制骨旋转模式含用户修改或动画，未覆盖")
    for index, name in enumerate(names):
        bone = rig.data.bones[name]
        parent = "总控制" if index in {0, 6} else f"{label} · 自动基点 {index}"
        if ((bone.head_local - points[index]).length > 1e-6 or abs(bone.length - .1) > 1e-6
                or bone.parent is None or bone.parent.name != parent or bone.use_deform):
            raise ValueError("绳线源控制骨结构含用户修改，未覆盖")
        if index in {0, 6}:
            continue
        base = rig.data.bones[parent]
        if ((base.head_local - points[0].lerp(points[-1], index / 6)).length > 1e-6
                or abs(base.length - .1) > 1e-6 or base.parent is None or base.parent.name != "总控制" or base.use_deform):
            raise ValueError("绳线源内部骨骼结构含用户修改，未覆盖")
    internal_paths = {_bone(f"{label} · 自动基点 {index}", field) for index in range(1, 6)
                      for field in ("location", "rotation_euler", "rotation_quaternion", "scale")}
    if any(curve.data_path in internal_paths for curve in _actions(rig)):
        raise ValueError("绳线内部骨骼含用户Action/NLA动画，未覆盖")
    hooks, bases = [], []
    if SCHEMA in line:
        if line[SCHEMA] != 1 or line.get(CONTRACT) != curve_record(line.data):
            raise ValueError("绳线生成曲线设置已改变，未覆盖")
        if any(modifier.type == "HOOK" for modifier in line.modifiers):
            raise ValueError("绳线含未知Hook，未覆盖")
    else:
        if any(curve.data_path.startswith("splines[") for curve in getattr(line.data.animation_data, "drivers", ())):
            raise ValueError("旧绳线含未知切线驱动，未覆盖")
        hooks, bases = _legacy_check(transaction, line, incoming, rig, fishing)
    curve_snapshot = drivers.capture(line.data)
    point_snapshots = [{field: tuple(getattr(point, field)) if field in {"co", "handle_left", "handle_right"} else getattr(point, field)
                        for field in ("co", "handle_left", "handle_right", "handle_left_type", "handle_right_type")}
                       for point in line.data.splines[0].bezier_points]
    hook_snapshots = [(hook.name, _rna(hook), tuple(hook.vertex_indices), hook.show_expanded, hook.is_active) for hook in hooks]
    published = [False]

    def rollback():
        if not published[0]:
            return
        drivers.restore(line.data, curve_snapshot)
        for point, values in zip(line.data.splines[0].bezier_points, point_snapshots, strict=True):
            point.handle_left_type = point.handle_right_type = "FREE"
            for field in ("co", "handle_left", "handle_right", "handle_left_type", "handle_right_type"):
                setattr(point, field, values[field])
        active_hook = None
        for name, values, indices, expanded, active in hook_snapshots:
            hook = line.modifiers.new(name, "HOOK")
            for field, value in values.items():
                setattr(hook, field, value)
            hook.vertex_indices_set(indices)
            hook.show_expanded = expanded
            if active:
                active_hook = hook
            actual_index = next(index for index, modifier in enumerate(line.modifiers) if modifier == hook)
            line.modifiers.move(actual_index, indices[0] // 3)
        if active_hook is not None:
            active_hook.is_active = True
        for name, constraints in bases:
            pose = rig.pose.bones[name]
            for constraint in tuple(pose.constraints):
                pose.constraints.remove(constraint)
            for name, values in constraints:
                constraint = pose.constraints.new("COPY_LOCATION")
                constraint.name = name
                for field in ("target", "subtarget", *[key for key in values if key not in {"target", "subtarget"}]):
                    setattr(constraint, field, values[field])

    transaction.on_rollback(rollback)
    tail = rig.pose.bones[names[-1]]
    shape = tail.custom_shape
    expected_shape = [(-.2, 0, 0), (.2, 0, 0), (0, -.2, 0), (0, .2, 0),
                      (-.14, -.14, 0), (.14, -.14, 0), (.14, .14, 0), (-.14, .14, 0)]
    old_flat = (SCHEMA not in line and shape is not None and owns(root, shape) and shape.type == "MESH"
                and len(shape.data.vertices) == 8 and all((vertex.co - Vector(point)).length < 1e-6
                for vertex, point in zip(shape.data.vertices, expected_shape, strict=True))
                and all(abs(value) < 1e-6 for value in tail.custom_shape_rotation_euler))
    return incoming, line, rig, bases, hooks, published, old_flat, names[-1]


def publish_repair(plan, mapping):
    """Replace only proven generated channels, retaining actual Curve Data ID."""
    if plan is None:
        return
    incoming, line, rig, bases, hooks, published, old_flat, tail_name = plan
    published[0] = True
    for name, _constraints in bases:
        for constraint in tuple(rig.pose.bones[name].constraints):
            rig.pose.bones[name].constraints.remove(constraint)
    for hook in hooks:
        line.modifiers.remove(hook)
    records = asset_rigs.driver_records(incoming.data)
    for _path, _axis, _kind, _expression, variables in records:
        for _name, _type, targets in variables:
            for target in targets:
                target["id"] = mapping.get(target.get("id"), target.get("id"))
    channels = {(record[0], record[1]) for record in records}
    preserved = [record for record in asset_rigs.driver_records(line.data) if (record[0], record[1]) not in channels]
    for point in line.data.splines[0].bezier_points:
        point.handle_left_type = point.handle_right_type = "FREE"
    asset_rigs.install_drivers(line.data, [*preserved, *records])
    line[SCHEMA], line[ROLE], line[CONTRACT] = 1, "line", curve_record(line.data)
    if old_flat:
        rig.pose.bones[tail_name].custom_shape_rotation_euler.x = math.pi / 2
    line.data.update_tag()
