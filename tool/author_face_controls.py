"""Offline authoring of the native BaiPo facial controls and packed brow atlas.

Call on an editable working copy in Blender 4.5. The saved rigs and shaders do
not depend on this module, drivers installed by an add-on, or external textures.
"""

import json
import runpy
import sys
from contextlib import ExitStack
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import bpy
from mathutils import Vector

SWITCH = "眉纹切换"
TRACK = "眉纹切换·轨道"
MASK = "BP_BrowSurface"
UV = "BP_BrowUV"
ENABLED = "bp_face_brow_style"
TRAVEL = 0.144
SIDES = (("L", "左"), ("R", "右"))


def _driver(owner, path, rig, name, expression, kind="LOC_Z"):
    curve = owner.driver_add(path)
    driver = curve.driver
    driver.type = "SCRIPTED"
    var = driver.variables.new()
    var.name = "v"
    var.type = "TRANSFORMS"
    target = var.targets[0]
    target.id = rig
    target.bone_target = name
    target.transform_type = kind
    target.transform_space = "LOCAL_SPACE"
    driver.expression = expression


def _widget(name, width, height):
    obj = bpy.data.objects.get(name)
    if obj:
        return obj
    mesh = bpy.data.meshes.new(name)
    corners = [
        (-width / 2, 0, -height / 2),
        (width / 2, 0, -height / 2),
        (width / 2, 0, height / 2),
        (-width / 2, 0, height / 2),
    ]
    mesh.from_pydata(corners, [(0, 1), (1, 2), (2, 3), (3, 0)], [])
    return bpy.data.objects.new(name, mesh)


def _limit(bone, low, high):
    c = bone.constraints.new("LIMIT_LOCATION")
    c.name = "面控 · 局部行程"
    c.owner_space = "LOCAL"
    c.use_transform_limit = True
    for axis in "xyz":
        setattr(c, "use_min_" + axis, True)
        setattr(c, "use_max_" + axis, True)
    c.min_z, c.max_z = low, high
    bone.lock_location = (True, True, False)
    bone.lock_rotation = bone.lock_scale = (True, True, True)
    bone.lock_rotation_w = True


def size_brow_selector(rig):
    """Measure the displayed head selector outline in native armature units."""
    head = rig.pose.bones["面部切换框"]
    scale = head.custom_shape_scale_xyz
    size = head.bone.length if head.use_custom_shape_bone_size else 1.0
    vertices = head.custom_shape.data.vertices

    def half_span(axis):
        values = [vertex.co[axis] for vertex in vertices]
        return (max(values) - min(values)) * abs(scale[axis]) * size * .5

    width, height = half_span(0), half_span(2)
    rig.pose.bones[TRACK].custom_shape_scale_xyz = (width, 1, height)
    knob = min(width*.75, height*.2)
    rig.pose.bones[SWITCH].custom_shape_scale_xyz = (knob, 1, knob)


def _bones(rig, anchors):
    """Coordinates are in armature space; preserve all original rest matrices."""
    scene = cast(bpy.types.Scene, bpy.context.scene)
    layer = cast(bpy.types.ViewLayer, bpy.context.view_layer)
    link = rig.name not in layer.objects
    if link:
        scene.collection.objects.link(rig)
    layer.objects.active = rig
    rig.select_set(True)
    rig.hide_set(False)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = rig.data.edit_bones
    eb["头"].use_connect = False
    specs = {}

    def new(name, pos, parent, width, height, low, high):
        if name in eb:
            raise ValueError("重复面控骨：" + name)
        bone = eb.new(name)
        bone.head = pos
        bone.tail = Vector(pos) + Vector((0, -0.08, 0))
        bone.align_roll(Vector((0, 0, 1)))
        bone.parent = eb[parent]
        bone.use_deform = False
        for col in eb[parent].collections:
            col.assign(bone)
        specs[name] = (width, height, low, high)

    for side, _ in SIDES:
        x, y, upper, lower, width = anchors[side]
        new(
            side + ".眉毛", (x, y, upper), "面部控制器位置", width, 0.011, -0.075, 0.115
        )
        new(
            side + ".下眼皮",
            (x, y, lower),
            "面部控制器位置",
            width,
            0.011,
            -0.045,
            0.105,
        )
    switch = eb["面部兼容"].head.copy()
    # The existing back-accessory rail sits immediately to the left of the
    # head selector. Place the half-height brow rail one column farther out.
    switch.x = (
        min(switch.x, eb["披风模式"].head.x if "披风模式" in eb else switch.x) - 0.085
    )
    new(TRACK, switch - Vector((0, 0, TRAVEL / 2)), "头", 0.055, TRAVEL + 0.025, 0, 0)
    new(SWITCH, switch, TRACK, 0.041, 0.041, -TRAVEL, 0)
    bpy.ops.object.mode_set(mode="OBJECT")
    rig.pose.bones["头"].lock_location = (False, False, False)
    for name, (width, height, low, high) in specs.items():
        p = rig.pose.bones[name]
        p.custom_shape = _widget(
            "WGT·白珀面控·横线" if "." in name else "WGT·白珀面控·滑轨", 1, 1
        )
        p.use_custom_shape_bone_size = False
        p.custom_shape_scale_xyz = (width, 1, height)
        p.color.palette = "THEME04" if "下眼皮" in name else "THEME03"
        _limit(p, low, high)
        if name == TRACK:
            p.bone.hide_select = True
    size_brow_selector(rig)
    if link:
        scene.collection.objects.unlink(rig)
    rig["bp_face_controls_schema"] = 1


def _atlas():
    name = "白珀表情 · 内收像素眉"
    img = bpy.data.images.get(name)
    if img:
        return img
    lines = (
        ".......#",
        ".....###",
        "...#####",
        ".#######",
        "########",
        ".#####..",
        "...##...",
        "........",
    )
    img = bpy.data.images.new(name, 8, 8, alpha=True)
    pixels = cast(Any, img.pixels)
    pixels[:] = [
        channel
        for line in reversed(lines)
        for char in line
        for channel in (0.09, 0.055, 0.12, float(char == "#"))
    ]
    img.pack()
    return img


def _shader(material, skin_uv):
    if material.get("bp_face_brow_shader"):
        return
    tree = material.node_tree
    dest = next(
        (n.inputs["Base Color"] for n in tree.nodes if n.type == "BSDF_PRINCIPLED"),
        None,
    )
    if dest is None or not dest.is_linked:
        raise ValueError("未识别眉纹源材质：" + material.name)

    def node(kind, label):
        n = tree.nodes.new(kind)
        n.label = label
        return n

    source = dest.links[0].from_socket
    mask = node("ShaderNodeAttribute", "眉区")
    mask.attribute_name = MASK
    active = node("ShaderNodeAttribute", "眉纹开关")
    active.attribute_type = "OBJECT"
    active.attribute_name = ENABLED
    times = node("ShaderNodeMath", "当前眉区")
    times.operation = "MULTIPLY"
    tree.links.new(active.outputs["Fac"], times.inputs[0])
    tree.links.new(mask.outputs["Fac"], times.inputs[1])
    group = bpy.data.node_groups.get("白珀 · 眉纹贴图")
    if not group:
        group = bpy.data.node_groups.new("白珀 · 眉纹贴图", "ShaderNodeTree")
        interface = group.interface
        assert interface is not None
        interface.new_socket(
            name="颜色", in_out="OUTPUT", socket_type="NodeSocketColor"
        )
        interface.new_socket(
            name="墨迹", in_out="OUTPUT", socket_type="NodeSocketFloat"
        )
        coord = cast(bpy.types.ShaderNodeAttribute, group.nodes.new("ShaderNodeAttribute"))
        coord.attribute_name = UV
        image = cast(bpy.types.ShaderNodeTexImage, group.nodes.new("ShaderNodeTexImage"))
        image.image = _atlas()
        image.interpolation = "Closest"
        image.extension = "EXTEND"
        output = group.nodes.new("NodeGroupOutput")
        assert coord.outputs is not None and image.inputs is not None and image.outputs is not None
        group.links.new(coord.outputs["Vector"], image.inputs["Vector"])
        group.links.new(image.outputs["Color"], output.inputs[0])
        group.links.new(image.outputs["Alpha"], output.inputs[1])
    tile = node("ShaderNodeGroup", "第二张眉纹")
    tile.node_tree = group
    # On this face only, sample adjacent forehead pixels as the clean background.
    # Reuse each native color sampler so skin replacement and native color effects
    # continue to operate on the live atlas.
    images = [
        n
        for n in tree.nodes
        if n.type == "TEX_IMAGE" and n.image and n.outputs["Color"].is_linked
    ]
    for image in images:
        vec = image.inputs["Vector"]
        src = (
            vec.links[0].from_socket
            if vec.is_linked
            else node("ShaderNodeTexCoord", "原生 UV").outputs["UV"]
        )
        mix = node("ShaderNodeMixRGB", "眉底肤色")
        tree.links.new(times.outputs[0], mix.inputs[0])
        tree.links.new(src, mix.inputs[1])
        mix.inputs[2].default_value = (*skin_uv, 0, 1)
        tree.links.new(mix.outputs[0], vec)
    blend = node("ShaderNodeMath", "眉纹遮罩")
    blend.operation = "MULTIPLY"
    tree.links.new(times.outputs[0], blend.inputs[0])
    tree.links.new(tile.outputs[1], blend.inputs[1])
    color = node("ShaderNodeMixRGB", "附加眉纹")
    tree.links.new(blend.outputs[0], color.inputs[0])
    tree.links.new(source, color.inputs[1])
    tree.links.new(tile.outputs[0], color.inputs[2])
    tree.links.new(color.outputs[0], dest)
    material["bp_face_brow_shader"] = 1


def _style_on(obj, rig):
    if ENABLED not in obj:
        obj[ENABLED] = 0.0
        _driver(obj, '["' + ENABLED + '"]', rig, SWITCH, "v < -0.072")


def _mark_brow(obj, faces, coord, skin_uv):
    if not faces:
        return
    mesh = obj.data
    region = mesh.attributes.get(MASK) or mesh.attributes.new(MASK, "FLOAT", "FACE")
    field = mesh.attributes.get(UV) or mesh.attributes.new(UV, "FLOAT_VECTOR", "CORNER")
    # Adding a custom data layer can invalidate Python attribute handles.
    region = mesh.attributes[MASK]
    field = mesh.attributes[UV]
    points = [coord(mesh.vertices[i].co) for p in faces for i in p.vertices]
    left, right = min(abs(p.x) for p in points), max(abs(p.x) for p in points)
    floor, ceiling = min(p.z for p in points), max(p.z for p in points)
    for p in faces:
        region.data[p.index].value = 1.0
        for li in p.loop_indices:
            c = coord(mesh.vertices[mesh.loops[li].vertex_index].co)
            field.data[li].vector = (
                (abs(c.x) - left) / max(1e-6, right - left),
                (c.z - floor) / max(1e-6, ceiling - floor),
                0,
            )
    for index in {p.material_index for p in faces}:
        _shader(mesh.materials[index], skin_uv)


def _delta(key, basis, epsilon=1e-5):
    return {
        i
        for i, point in enumerate(key.data)
        if (point.co - basis.data[i].co).length > epsilon
    }


def _move_key(obj, rig, name, indices, up, control, amount):
    if not indices:
        return
    k = obj.shape_key_add(name=name, from_mix=False)
    k.slider_min = -1
    k.slider_max = 1
    orig = obj.data.shape_keys.reference_key
    for i in indices:
        k.data[i].co = orig.data[i].co + up * amount
    _driver(k, "value", rig, control, f"max(-1,min(1,v/{amount:.6g}))")
    return k


def _human(rig, obj):
    keys = obj.data.shape_keys
    b = keys.reference_key
    mat = rig.matrix_world.inverted() @ obj.matrix_world
    coord = lambda v: mat @ v
    up = mat.to_3x3().inverted() @ Vector((0, 0, 1))
    anchors = {}
    patches = []
    fields = []
    for n in range(7):
        for side, zh in SIDES:
            prefix = f"T{n}::"
            brow = next(
                k
                for k in keys.key_blocks
                if k.name.startswith(prefix) and zh + "眉+13" in k.name
            )
            brow_ids = _delta(brow, b)
            polys = [
                p for p in obj.data.polygons if all(i in brow_ids for i in p.vertices)
            ]
            patches.append(polys)
            lid = next(
                k
                for k in keys.key_blocks
                if k.name.startswith(prefix) and "闭" + zh in k.name
            )
            moves = _delta(lid, b)
            # The native blink already raises the lower rim; translate that same
            # lower set manually without changing the original eyelid response.
            lower = {
                i
                for i in moves
                if (mat.to_3x3() @ (lid.data[i].co - b.data[i].co)).z > 1e-5
            }
            if not lower:
                floor = min(coord(b.data[i].co).z for i in moves)
                lower = {i for i in moves if coord(b.data[i].co).z <= floor + 0.045}
            fields.append((side, n, brow_ids, lower))
            if n == 0:
                points = [coord(b.data[i].co) for i in brow_ids]
                xs = [p.x for p in points]
                anchors[side] = (
                    (min(xs) + max(xs)) * 0.5,
                    min(p.y for p in points) - 0.06,
                    max(p.z for p in points) + 0.035,
                    max(
                        coord(b.data[i].co).z
                        for i in lower
                        if (mat.to_3x3() @ (lid.data[i].co - b.data[i].co)).z > 0.02
                    ),
                    0.85 * (max(xs) - min(xs)),
                )
    _bones(rig, anchors)
    _style_on(obj, rig)
    for polys in patches:
        _mark_brow(obj, polys, coord, (12.5 / 64, 1 - 8.5 / 64))
    for side, n, brow, lower in fields:
        _move_key(obj, rig, f"T{n}::{side}眉高", brow, up, side + ".眉毛", 0.115)
        _move_key(obj, rig, f"T{n}::{side}下眼皮", lower, up, side + ".下眼皮", 0.105)


def author_human_face(rig, root):
    head = next(o for o in root.all_objects if o.name.startswith("头部 · 八型单网格"))
    _human(rig, head)
    compact_human_skin(root)
    refine_controls(rig, root)


def refine_controls(rig, root):
    repo = Path(__file__).resolve().parents[1]
    with ExitStack() as rollback:
        def track(item):
            rollback.callback(bpy.data.batch_remove, ids=[item])
            return item

        journal = SimpleNamespace(owner=None, on_rollback=rollback.callback, track=track)
        sys.path.insert(0, str(repo.parent))
        try:
            module = import_module(repo.name + ".business.human.human_face_controls")
            module.refine(journal, rig, root.all_objects)
        finally:
            sys.path.pop(0)
        rollback.pop_all()


def compact_human_skin(root):
    # This offline tool also runs as a standalone Blender script without loading the add-on.
    shader = runpy.run_path(str(Path(__file__).resolve().parents[1]
                                / "business/human/human_skin_shader.py"))
    [skin] = {slot.material for obj in root.all_objects for slot in obj.material_slots
              if slot.material and slot.material.get("oxal_mc_studio_human_skin_role") == "skin"}
    group_node = shader["lid_group"](skin)
    if group_node is None:
        return
    meshes = {obj.data for obj in root.all_objects if obj.type == "MESH"
              and any(slot.material == skin for slot in obj.material_slots)}
    for mesh in meshes:
        shader["validate_lid_faces"](mesh)
    shader["compact_lid_group"](group_node.node_tree)


def _native_window(obj, left_eye):
    profile = json.loads(obj["oxal_humanoid_head_profile"])
    ax, az = profile["projection_affine"]
    eyes = sorted(profile["eyes"])
    box = eyes[-1] if left_eye else eyes[0]
    if len(eyes) == 1:
        xmin, _, xmax, _ = profile["native_front"]
        center = (xmin + xmax) / 2
        if (sum((box[0], box[2])) / 2 > center) != left_eye:
            box = [2 * center - box[2], box[1], 2 * center - box[0], box[3]]

    def pixel(co):
        return ax[0] * co.x + ax[1] * co.z + ax[2], az[0] * co.x + az[1] * co.z + az[2]

    def local(x, y):
        return Vector(
            (
                (x - ax[2]) / ax[0],
                min(v.co.y for v in obj.data.vertices),
                (y - az[2]) / az[1],
            )
        )

    return box, profile, pixel, local


def _creature_patches(eye, side, native):
    mesh = eye.data
    box, profile, pixel, local = _native_window(eye, side == "L")
    l, t, r, b = box
    headmat = eye.matrix_world
    keys = mesh.shape_keys
    blink = next(
        (
            k
            for k in keys.key_blocks
            if k.name.startswith("闭" + ("左" if side == "L" else "右"))
        ),
        None,
    )

    def find_tiles(y):
        result = []
        for p in mesh.polygons:
            xs, ys = zip(*(pixel(mesh.vertices[i].co) for i in p.vertices))
            x, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            if (
                l - 0.01 < x < r + 0.01
                and abs(cy - y) < 0.18
                and max(xs) - min(xs) < 1.2
                and max(ys) - min(ys) < 1.2
                and abs(p.normal.y) > 0.8
            ):
                result.append(p)
        return result

    row = find_tiles(b + 0.5)
    lower = {
        i
        for p in row
        for i in p.vertices
        if abs(pixel(mesh.vertices[i].co)[1] - b) < 0.13
    }
    if not lower and blink:
        lower = {
            i
            for i in _delta(blink, keys.reference_key)
            if (blink.data[i].co - keys.reference_key.data[i].co).z > 1e-5
        }
    # Several rigs use analytic UV eyelid motion on an unkeyed face. Its
    # lower rim is the top edge of the row of opaque skin below the eye.
    browsets = []
    for obj in native or (eye,):
        data = obj.data
        k = data.shape_keys
        lookup = "左眉+" if side == "L" else "右眉+"
        angles = (
            next((p for p in k.key_blocks if lookup in p.name), None) if k else None
        )
        ids = _delta(angles, k.reference_key) if angles else set()
        if native:
            ids = {
                i
                for i in ids
                if (
                    obj.data.vertices[i].co.x >= -1e-5
                    if side == "L"
                    else obj.data.vertices[i].co.x <= 1e-5
                )
            }
        polys = [p for p in data.polygons if all(i in ids for i in p.vertices)]
        if not ids and not native:
            polys = find_tiles(t - 0.5)
            ids = {i for p in polys for i in p.vertices}
        if ids:
            browsets.append((obj, ids, polys))
    min_skin = profile.get("brow_skin_row")
    bg = (
        (l + 0.5) / profile["atlas"][0],
        1 - ((min_skin if min_skin is not None else t - 2) + 0.5) / profile["atlas"][1],
    )
    return (
        headmat @ local((l + r) / 2, t - 1.15),
        headmat @ local((l + r) / 2, b),
        (headmat @ local(l, t) - headmat @ local(r, t)).length,
        lower,
        browsets,
        bg,
        blink,
    )


def author_creature_face(rig, variant_collection):
    """Upgrade one independently editable source rig and its own mesh controls."""
    objs = tuple(variant_collection.all_objects)
    eyes = [
        o
        for o in objs
        if o.type == "MESH"
        and o.get("oxal_humanoid_part")
        in {"head/eye_control", "head/legacy_bone_control", "head/legacy_flesh_control"}
    ]
    native = [
        o
        for o in objs
        if o.get("oxal_humanoid_part", "").startswith("head/native_brow/")
    ]
    if not eyes:
        raise ValueError("未找到生物面部：" + variant_collection.name)
    cast(bpy.types.ViewLayer, bpy.context.view_layer).update()
    anchors = {}
    missing = []
    for side, _ in SIDES:
        for eye in eyes:
            brow, low, width, lower, brows, bg, blink = _creature_patches(
                eye, side, native
            )
            to_rig = rig.matrix_world.inverted()
            pt = to_rig @ brow
            anchors.setdefault(
                side,
                (
                    pt.x,
                    pt.y - 0.045,
                    pt.z,
                    (to_rig @ low).z,
                    width / rig.matrix_world.to_scale().x * 0.9,
                ),
            )
            up = (
                eye.matrix_world.to_3x3().inverted()
                @ rig.matrix_world.to_3x3()
                @ Vector((0, 0, 1))
            )
            if not lower:
                missing.append((eye.name, side, "lower"))
                continue
            _move_key(
                eye, rig, side + "·下眼皮微调", lower, up, side + ".下眼皮", 0.105
            )
            if blink:
                base = eye.data.shape_keys.reference_key
                distance = sum(
                    (blink.data[i].co - base.data[i].co).dot(up) for i in lower
                ) / len(lower)
                if distance < 0.001:
                    _move_key(
                        eye,
                        rig,
                        side + "·下眼皮联动",
                        lower,
                        up,
                        side + ".眼皮",
                        -0.035,
                    )
                    auto = eye.data.shape_keys.key_blocks[side + "·下眼皮联动"]
                    # Native lid closes over the interval zero to -0.1.
                    for d in eye.data.shape_keys.animation_data.drivers:
                        if d.data_path == auto.path_from_id("value"):
                            d.driver.expression = "max(-1,min(0,v*10))"
            if not brows:
                missing.append((eye.name, side, "brow"))
            for obj, ids, polys in brows:
                obj_up = (
                    obj.matrix_world.to_3x3().inverted()
                    @ rig.matrix_world.to_3x3()
                    @ Vector((0, 0, 1))
                )
                _move_key(
                    obj, rig, side + "·眉高微调", ids, obj_up, side + ".眉毛", 0.115
                )
                _style_on(obj, rig)
                coord = (to_rig @ obj.matrix_world).__matmul__
                _mark_brow(obj, polys, coord, bg)
    if missing:
        raise ValueError("面部缺少可编辑区域：" + repr(missing))
    _bones(rig, anchors)
    refine_controls(rig, variant_collection)
    remove_creature_expressions(rig, variant_collection)


def remove_creature_expressions(rig, root):
    """Retire the generated expression strips while retaining the native lid keys."""
    if not rig.get("oxal_humanoid_mob"):
        raise ValueError("需要生物骨架")
    for obj in root.all_objects:
        modifier = obj.modifiers.get("白珀 · 主眉与副眉")
        if modifier:
            prefix = modifier.path_from_id() + "["
            if obj.animation_data:
                for curve in tuple(obj.animation_data.drivers):
                    if curve.data_path.startswith(prefix):
                        obj.driver_remove(curve.data_path)
            obj.modifiers.remove(modifier)
        if ENABLED in obj:
            obj.driver_remove('["' + ENABLED + '"]')
            del obj[ENABLED]
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo.parent))
    try:
        switches = import_module(repo.name + ".business.human.human_face_switches")
        with switches._edit(rig) as bones:
            for name in ("L.眉纹切换", "R.眉纹切换", SWITCH, TRACK):
                bone = bones.get(name)
                if bone:
                    bones.remove(bone)
    finally:
        sys.path.pop(0)
