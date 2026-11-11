"""Declarative world models and native rope attachments for library assets."""

import math
from typing import cast

import bpy
from bpy.types import CopyLocationConstraint
from mathutils import Matrix, Vector

from . import asset_catalog as catalog
from . import asset_geometry as geometry
from . import asset_rigs


def grip(identifier):
    short = identifier.split(":")[-1]
    if short.endswith("_sword") or short == "fishing_rod":
        return Vector((-.28125, 0, -.28125))
    if short == "shield":
        return Vector((0, .125, 0))
    node = catalog.read(catalog.resource(identifier, "items"))["model"]
    descriptors = catalog.item_nodes(node, context="thirdperson_righthand")
    if not descriptors:
        return Vector()
    model_id = descriptors[0].get("base", descriptors[0].get("model"))
    if not isinstance(model_id, str):
        return Vector()
    data = catalog.model(model_id)
    transform = data.get("display", {}).get("thirdperson_righthand", {})
    rotation = Matrix.Identity(3)
    for axis, degrees in zip("XYZ", transform.get("rotation", (0, 0, 0)), strict=True):
        rotation @= Matrix.Rotation(math.radians(degrees), 3, axis)
    point = rotation.inverted() @ (Vector((0, -2, 0)) - Vector(transform.get("translation", (0, 0, 0)))) / 16
    scale = transform.get("scale", (1, 1, 1))
    point = Vector(tuple(point[i] / scale[i] for i in range(3)))
    if short == "trident":
        point += Vector((.5, .5, .5))
        return Vector((0, 0, point.y))
    return Vector((point.x, -point.z, point.y))


def world_model(transaction, collection, root, identifier, material_for):
    """Build the original physical family instead of its flat GUI icon."""
    from .asset_special import entity

    short = identifier.split(":")[-1]
    if short in {"item_frame", "glow_item_frame"}:
        descriptor = {"model": "minecraft:block/" + short}
        obj = geometry.elements(transaction, short, catalog.model(descriptor["model"]), material_for, descriptor)
        collection.objects.link(obj)
        obj.parent = root
        return True
    if short.endswith(("_boat", "_raft")):
        chest = "_chest_" in short
        raft = short.endswith("_raft")
        name = "chest_raft" if chest and raft else "raft" if raft else "chest_boat" if chest else "boat"
        wood = short.split("_chest_")[0] if chest else short.removesuffix("_raft").removesuffix("_boat")
        key = "minecraft:entity/" + ("chest_boat/" if chest else "boat/") + wood
        parts = entity(transaction, collection, root, name, material_for(key))
        normalize_world(root)
        if chest:
            from .asset_hinges import chest_motion
            chest_motion(root,parts,collection,lid_name="chest_lid",lock_name="chest_lock",bottom_name="chest_bottom",correct_hinge=True)
        root["auto_paddles"], root["划桨速度"] = False, 1.0
        for label, bone_key, sign in (("左桨", "left_paddle", -1), ("右桨", "right_paddle", 1)):
            root[label + "角度"] = 0.0
            if bone_key not in parts:
                continue
            # The model rotation is the paddle's rest frame. A local joint
            # below it keeps angle zero from overwriting the authored Euler.
            rest = parts[bone_key]
            rest["oxal_mc_studio_model_part"] = bone_key+" · 原始姿态"
            motion = transaction.track(bpy.data.objects.new(bone_key+" · 动作",None))
            collection.objects.link(motion)
            motion["oxal_mc_studio_model_part"] = bone_key
            for child in tuple(rest.children):
                child.parent = motion
            motion.parent = rest
            parts[bone_key] = motion
            from .asset_effects import native
            native(parts[bone_key], "rotation_euler", "(-.6544984695+.3926990817*sin(frame*speed*4)) if automatic else angle*pi/180", root,
                   [("speed", "划桨速度"), ("automatic", "auto_paddles"), ("angle", label + "角度")], 0)
            native(parts[bone_key], "rotation_euler", f"({sign}*.7853981634*sin(frame*speed*4+1)) if automatic else angle*pi/180", root,
                   [("speed", "划桨速度"), ("automatic", "auto_paddles"), ("angle", label + "角度")], 2)
        return True
    if short.endswith("minecart"):
        entity(transaction, collection, root, "cart", material_for("minecraft:entity/minecart/minecart"))
        normalize_world(root)
        payload = {"chest_minecart": "chest", "hopper_minecart": "hopper", "furnace_minecart": "furnace", "tnt_minecart": "tnt", "command_block_minecart": "command_block"}.get(short)
        if payload == "chest":
            parts = entity(transaction, collection, root, "chest", material_for("minecraft:entity/chest/normal"), upright=1)
            parts["chest"].location = (-.375, .375, .375)
            parts["chest"].scale = (.75, .75, .75)
            from .asset_hinges import chest_motion
            chest_motion(root,parts,collection)
            for key in ("lid", "lock"):
                parts[key]["oxal_mc_studio_model_part"] = "载物_" + key
        elif payload:
            descriptors, _ = catalog.plan("minecraft:" + payload)
            for index, descriptor in enumerate(descriptors):
                obj = geometry.elements(transaction, "载物_" + payload + str(index), catalog.model(descriptor["model"]), material_for, descriptor)
                collection.objects.link(obj)
                obj.parent, obj.location, obj.scale = root, (0, 0, .375), (.75, .75, .75)
        return True
    if short == "armor_stand":
        entity(transaction, collection, root, "armor_stand", material_for("minecraft:entity/armorstand/armorstand"))
        normalize_world(root)
        return True
    return False


def normalize_world(root, factor=1.0):
    """Center an authored world family horizontally and put its floor at zero."""
    def matrix(obj):
        return (matrix(obj.parent) if obj.parent is not None and obj.parent != root else Matrix.Identity(4)) @ obj.matrix_parent_inverse @ obj.matrix_basis
    points = [matrix(obj) @ vertex.co for obj in root.children_recursive if obj.type == "MESH" for vertex in obj.data.vertices]
    if not points:
        return
    low = Vector(tuple(min(point[i] for point in points) for i in range(3)))
    high = Vector(tuple(max(point[i] for point in points) for i in range(3)))
    shift = Vector((-(low.x + high.x) / 2, -(low.y + high.y) / 2, -low.z)) * factor
    for obj in tuple(root.children):
        obj.location = obj.location * factor + shift
        obj.scale *= factor


def prepare_grip(root, identifier):
    short = identifier.split(":")[-1]
    if short not in asset_rigs.ITEMS:
        return
    point = grip(identifier)
    if short.endswith("_spear"):
        point = _spear_axis_grip(root, point)
    root["oxal_mc_studio_grip"] = [point[i] for i in range(3)]
    root["握持支点"] = [point[i] for i in range(3)]
    if identifier.split(":")[-1] == "trident":
        for obj in tuple(root.children_recursive):
            if obj.type == "MESH":
                obj.data.transform(Matrix.Translation(-point))
        root["oxal_mc_studio_raw_grip"] = [point[i] for i in range(3)]
        root["oxal_mc_studio_grip"] = (0.0, 0.0, 0.0)
        root["握持支点"] = (0.0, 0.0, 0.0)
    if short in {"lead", "fishing_rod"}:
        root["body_visible"] = True
        if short == "fishing_rod":
            root["line_cast"] = False
        else:
            root["rope_visible"], root["fence_variant"] = True, False
        for obj in tuple(root.children_recursive):
            if obj.type == "MESH":
                obj["oxal_mc_studio_rope_body"] = True
                for path in ("hide_render", "hide_viewport"):
                    geometry.driver(obj, path, "not v", root, "body_visible")


def _spear_axis_grip(root, anchor):
    """Project the palm onto a measured thin shaft in the actual source image."""
    objects = [obj for obj in root.children_recursive if obj.type == "MESH"]
    image = objects[0].data.materials[0].node_tree.nodes["OXAL Source Texture"].image
    width, height = image.size
    pixels = list(image.pixels)
    samples = [Vector(((x + .5) / width - .5, (y + .5) / width - .5))
               for y in range(height) for x in range(width)
               if pixels[(y * width + x) * 4 + 3] > .1]
    def fit(values):
        center = sum(values, Vector((0, 0))) / len(values)
        xx = sum((p.x - center.x) ** 2 for p in values)
        yy = sum((p.y - center.y) ** 2 for p in values)
        xy = sum((p.x - center.x) * (p.y - center.y) for p in values)
        angle = .5 * math.atan2(2 * xy, xx - yy)
        axis = Vector((math.cos(angle), math.sin(angle)))
        normal = Vector((-axis.y, axis.x))
        span = max(p.dot(axis) for p in values) - min(p.dot(axis) for p in values)
        error = sum(((p - center).dot(normal)) ** 2 for p in values) / len(values)
        return error / max(span * span, 1e-9), center, axis
    if len(samples) < 8:
        raise ValueError("原版矛贴图不足以确认杆轴")
    _, origin, direction = fit(samples)
    ordered = sorted(samples, key=lambda p: (p - origin).dot(direction))
    size = max(8, len(ordered) // 2)
    _, center, axis = min((fit(ordered[:size]), fit(ordered[-size:])), key=lambda result: result[0])
    palm = Vector((anchor.x, anchor.z))
    point = center + axis * (palm - center).dot(axis)
    root["oxal_mc_studio_spear_axis"] = [center.x, center.y, axis.x, axis.y]
    return Vector((point.x, anchor.y, point.y))


def bed_variants(transaction, collection, root, identifier, state, material_for):
    from .asset_special import DYE_COLORS

    short = identifier.split(":")[-1]
    colors = tuple(DYE_COLORS)
    color = short.removesuffix("_bed")
    if color not in colors:
        return
    root["bed_color"] = float(colors.index(color))
    root["选项 · 床颜色"] = "白色 / 橙色 / 品红色 / 淡蓝色 / 黄色 / 黄绿色 / 粉红色 / 灰色 / 淡灰色 / 青色 / 紫色 / 蓝色 / 棕色 / 绿色 / 红色 / 黑色"
    initial_objects = tuple(root.children)
    for index, variant in enumerate(colors):
        if variant == color:
            objects = initial_objects
        else:
            descriptors, _ = catalog.plan("minecraft:" + variant + "_bed", state=state)
            objects = []
            for number, descriptor in enumerate(descriptors):
                data = catalog.model(descriptor["model"])
                obj = geometry.elements(transaction, variant + " bed " + str(number), data, material_for, descriptor)
                collection.objects.link(obj)
                obj.parent = root
                objects.append(obj)
        for obj in objects:
            if obj.type == "MESH":
                obj["oxal_mc_studio_bed_color"] = index
                for path in ("hide_render", "hide_viewport"):
                    geometry.driver(obj, path, f"floor(v+.5)!={index}", root, "bed_color")


def rod_phases(transaction, collection, root, material_for):
    """Prepare both authored rod states once; native visibility selects them."""
    from .asset_effects import native

    rest = [obj for obj in tuple(root.children_recursive) if obj.get("oxal_mc_studio_rope_body")]
    definition = catalog.read(catalog.resource("minecraft:fishing_rod", "items"))["model"]
    descriptors = catalog.item_nodes(definition["on_true"], context="firstperson_righthand")
    if len(descriptors) != 1:
        raise ValueError("鱼竿抛出阶段资源没有唯一模型")
    data = catalog.model(descriptors[0]["model"])
    texture = catalog.texture(data["textures"]["layer0"], data["textures"])
    cast = geometry.sprite(transaction, "鱼竿 · 抛出阶段", material_for(texture))
    assert isinstance(cast.data, bpy.types.Mesh)
    collection.objects.link(cast)
    cast.parent = root
    cast["oxal_mc_studio_rope_body"] = True
    for phase, objects in (("REST", rest), ("CAST", [cast])):
        for obj in objects:
            obj["oxal_mc_studio_fishing_phase"] = phase
            for path in ("hide_render", "hide_viewport"):
                native(obj, path, "not body or " + ("not cast" if phase == "CAST" else "cast"), root,
                       [("body", "body_visible"), ("cast", "line_cast")])
    root["oxal_mc_studio_fishing_states"] = 1


def attachments(transaction, scene, collection, root, identifier, material_for):
    short = identifier.split(":")[-1]
    if short not in {"lead", "fishing_rod"} or root.type != "ARMATURE":
        return
    from . import asset_rope

    fishing = short == "fishing_rod"
    label, names, points = asset_rope.layout(fishing)
    start, end = points[0], points[-1]
    with asset_rigs._edit(root, scene) as bones:
        for index, (name, point) in enumerate(zip(names, points, strict=True)):
            bone = bones.new(name)
            bone.head, bone.tail = point, point + Vector((0, .1, 0))
            bone.parent = bones["总控制"]
            bone.use_deform = False
            if 0 < index < 6:
                base = bones.new(f"{label} · 自动基点 {index}")
                base.head = start.lerp(end, index / 6)
                base.tail = base.head + Vector((0, .1, 0))
                base.parent, base.use_deform = bones["总控制"], False
                bone.parent = base
    collection_bones = root.data.collections["控制器"]
    shapes = {kind: asset_rigs._shape(transaction, kind) for kind in ("root", "joint")}
    root.pose.bones["总控制"].custom_shape_scale_xyz = (.3, .3, .3)
    for index, name in enumerate(names):
        collection_bones.assign(root.data.bones[name])
        shape = shapes["root" if index in {0, 6} else "joint"]
        asset_rigs._style(root.pose.bones[name], shape)
        width = max(max(vertex.co[axis] for vertex in shape.data.vertices) - min(vertex.co[axis] for vertex in shape.data.vertices) for axis in range(3))
        root.pose.bones[name].custom_shape_scale_xyz = (.12 / max(width, 1e-6),) * 3
        root.pose.bones[name].lock_scale = (True, True, True)
    if fishing:
        root.pose.bones[names[0]].lock_location = (True, True, True)
    for index in range(1, 6):
        base = root.pose.bones[f"{label} · 自动基点 {index}"]
        root.data.collections["内部机构（隐藏）"].assign(base.bone)
        base.bone.hide = True
    curve = transaction.track(bpy.data.curves.new("鱼线" if fishing else "拴绳", "CURVE"))
    curve.dimensions, curve.resolution_u, curve.render_resolution_u = "3D", 64, 64
    curve.bevel_depth, curve.bevel_resolution = (.003 if fishing else 0), 1
    spline = curve.splines.new("BEZIER")
    spline.bezier_points.add(6)
    for point, coordinate in zip(spline.bezier_points, points, strict=True):
        point.co = coordinate
        point.handle_left_type = point.handle_right_type = "AUTO"
    line = transaction.track(bpy.data.objects.new(curve.name, curve))
    collection.objects.link(line)
    line.parent = root
    asset_rope.build(transaction, collection, root, line, fishing)
    mat = transaction.track(bpy.data.materials.new("鱼线材质" if fishing else "原版拴绳材质"))
    mat.diffuse_color = (.01, .01, .01, 1) if fishing else (.5, .4, .3, 1)
    mat.use_nodes = True
    shader = next(node for node in mat.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    shader.inputs["Base Color"].default_value = mat.diffuse_color
    curve.materials.append(mat)
    if fishing:
        root["鱼线粗细"] = .003
        root.id_properties_ui("鱼线粗细").update(min=0.0, soft_min=0.0, soft_max=.05, description="鱼线半径；支持原生关键帧")
        geometry.driver(curve, "bevel_depth", "max(0,v)", root, "鱼线粗细")
        for path in ("hide_render", "hide_viewport"):
            geometry.driver(line, path, "not v", root, "鱼线展开")
        hook_material = material_for("minecraft:entity/fishing/fishing_hook")
        obj = geometry.sprite(transaction, "鱼钩 · 可移动旋转", hook_material, .0625)
        mesh = cast(bpy.types.Mesh,obj.data)
        top_z = max(vertex.co.z for vertex in mesh.vertices)
        top = [vertex.co for vertex in mesh.vertices if abs(vertex.co.z-top_z)<1e-6]
        pivot = sum(top,Vector())/len(top)
        transform = Matrix.Scale(.25,4) @ Matrix.Translation(-pivot)
        mesh.transform(transform)
        obj["oxal_mc_studio_sprite_transform"] = [float(value) for row in transform for value in row]
        obj["oxal_mc_studio_fishing_hook"] = True
        collection.objects.link(obj)
        obj.location = end
        asset_rigs._bind(obj, root, "鱼钩")
        from .asset_projectile_effects import billboard
        billboard(transaction,obj,point_at_camera=True)
        for path in ("hide_render", "hide_viewport"):
            geometry.driver(obj, path, "not v", root, "鱼线展开")
    else:
        _lead_ribbon(transaction, line, root, mat)
        from .asset_item_geometry import Cuboid, _surface
        box = Cuboid("绳结", (-3, -8, -3), (6, 8, 6), (0, 0))
        vertices, faces, uv = _surface(box, (32, 32), 0)
        knot_mat = material_for("minecraft:entity/lead_knot/lead_knot")
        knot = geometry.mesh_object(transaction, "栅栏拴绳 · 绳结（无栅栏）", vertices, faces, uv, [knot_mat], [0] * len(faces))
        collection.objects.link(knot)
        knot.parent, knot.location = root, end
        follow = cast(CopyLocationConstraint, knot.constraints.new("COPY_LOCATION"))
        follow.target, follow.subtarget = root, "拴绳终点"
        follow.owner_space = follow.target_space = "WORLD"
        for path in ("hide_render", "hide_viewport"):
            from .asset_effects import native
            native(knot, path, "not fence or not visible", root,
                   [("fence", "fence_variant"), ("visible", "rope_visible")])


def _lead_ribbon(transaction, line, root, material):
    """Native 24-segment crossed ribbon, with endpoint bones and no callbacks."""
    root["拴绳粗细"] = .025
    tree = transaction.track(bpy.data.node_groups.new("拴绳 · 原版24段交叉条带", "GeometryNodeTree"))
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    incoming, outgoing = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")
    sample = tree.nodes.new("GeometryNodeResampleCurve")
    sample.inputs["Count"].default_value = 25
    tree.links.new(incoming.outputs["Geometry"], sample.inputs["Curve"])
    profile = tree.nodes.new("GeometryNodeJoinGeometry")
    for axis in (0, 1):
        node = tree.nodes.new("GeometryNodeCurvePrimitiveLine")
        a, b = [0., 0., 0.], [0., 0., 0.]
        a[axis], b[axis] = -1, 1
        node.inputs["Start"].default_value, node.inputs["End"].default_value = a, b
        tree.links.new(node.outputs["Curve"], profile.inputs["Geometry"])
    transform = tree.nodes.new("GeometryNodeTransform")
    tree.links.new(profile.outputs["Geometry"], transform.inputs["Geometry"])
    for axis in range(3):
        geometry.driver(transform.inputs["Scale"], "default_value", "v", root, "拴绳粗细", axis)
    mesh = tree.nodes.new("GeometryNodeCurveToMesh")
    tree.links.new(sample.outputs["Curve"], mesh.inputs["Curve"])
    tree.links.new(transform.outputs["Geometry"], mesh.inputs["Profile Curve"])
    assign = tree.nodes.new("GeometryNodeSetMaterial")
    assign.inputs["Material"].default_value = material
    tree.links.new(mesh.outputs["Mesh"], assign.inputs["Geometry"])
    dark = transaction.track(material.copy())
    dark.diffuse_color = tuple(value * .7 for value in material.diffuse_color[:3]) + (1,)
    shader = next(node for node in dark.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    shader.inputs["Base Color"].default_value = dark.diffuse_color
    second = tree.nodes.new("GeometryNodeSetMaterial")
    second.inputs["Material"].default_value = dark
    tree.links.new(assign.outputs["Geometry"], second.inputs["Geometry"])
    index = tree.nodes.new("GeometryNodeInputIndex")
    parity = tree.nodes.new("ShaderNodeMath")
    parity.operation, parity.inputs[1].default_value = "MODULO", 2
    tree.links.new(index.outputs["Index"], parity.inputs[0])
    alternate = tree.nodes.new("GeometryNodeSetMaterialIndex")
    tree.links.new(second.outputs["Geometry"], alternate.inputs["Geometry"])
    tree.links.new(parity.outputs[0], alternate.inputs["Material Index"])
    tree.links.new(alternate.outputs["Geometry"], outgoing.inputs["Geometry"])
    modifier = line.modifiers.new("原版拴绳条带", "NODES")
    modifier.node_group = tree
    for path in ("hide_render", "hide_viewport"):
        geometry.driver(line, path, "not v", root, "绳索显示")
