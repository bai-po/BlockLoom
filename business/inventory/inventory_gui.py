"""Private native SLOT canvases, measured in the authored HUD's GUI pixels.

The creation transaction owns every changed mesh/material. Source model frames,
native binding constraints and the separate held representation stay intact.
"""

import hashlib
import json
import math
from typing import Any

import bpy
from mathutils import Matrix, Vector

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import owned_ids, require_owned
from ...util.resources import INSTANCE
from ..asset.asset_slot_geometry import matrices, points

GUI_SCHEMA = "oxal_mc_studio_slot_gui_schema"
GUI_MATRIX = "oxal_mc_studio_slot_gui_generated_matrix"
GUI_SHADE = "OXAL 库存固定明暗"
GUI_SHADE_BACK = "OXAL 库存背面固定明暗"
GUI_MATERIAL = "oxal_mc_studio_inventory_gui_material"
PROJECTION = "oxal_mc_studio_inventory_gui_projection"
DISPLAY_ROLE = "库存 · GUI显示变换"
DISPLAY_BAKED = "oxal_mc_studio_slot_gui_display_baked"
FRAME_MATRIX = "oxal_offhand_frame_local_matrix_local58"
ALPHA_BOUNDS = "oxal_offhand_alpha_uv_bounds_local58"
CAMERA_BASIS = Matrix.Rotation(-math.pi / 2, 4, "X")


def legacy_canvas(target):
    """Exact released 0.1.2 C@S recipe, solely for conservative upgrades."""
    rest = target.rig.data.bones[target.gui_bone].matrix_local.to_3x3().inverted()
    desired = Matrix(((0, -1, 0), (1, 0, 0), (0, 0, 1)))
    result = (rest @ desired).to_4x4()
    result.translation = rest @ Vector((.002 if target.slot == "OFFHAND" else -.01, 0, .006))
    return result @ Matrix.Diagonal((target.gui_unit, target.gui_unit * .001, target.gui_unit, 1))


def _pixel_metrics(target):
    frames = [obj for obj in target.root.all_objects if obj.type == "MESH" and FRAME_MATRIX in obj]
    if len(frames) != 1:
        raise ValueError("物品栏没有唯一的原生副手像素参照")
    frame = frames[0]
    require_owned(target.root, (frame, frame.data))
    images = {node.image for material in frame.data.materials if material and material.node_tree
              for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image}
    bounds = [image[ALPHA_BOUNDS] for image in images if ALPHA_BOUNDS in image]
    if len(bounds) != 1 or len(bounds[0]) != 4:
        raise ValueError("物品栏副手图集缺少已验证的像素边界")
    values = frame[FRAME_MATRIX]
    matrix = Matrix(tuple(tuple(values[row * 4 + col] for col in range(4)) for row in range(4)))
    height = (matrix.to_3x3() @ Vector((0, 2 * (bounds[0][3] - bounds[0][2]), 0))).length
    width = (matrix.to_3x3() @ Vector((2 * (bounds[0][1] - bounds[0][0]), 0, 0))).length
    if height <= 1e-8:
        raise ValueError("物品栏像素高度无效")
    return height / 22, width / (height / 22)


def _pixel_unit(target):
    return _pixel_metrics(target)[0]


def _depths(target, camera):
    graph = bpy.context.evaluated_depsgraph_get()
    inverse = camera.evaluated_get(graph).matrix_world.inverted_safe()
    bars = [obj for obj in target.root.all_objects if obj.get("oxal_hotbar_cell_count1") == 9]
    token = "着色器数字_副手_" if target.slot == "OFFHAND" else "着色器数字_物品" + str(int(target.slot)).zfill(2) + "_"
    counts = [obj for obj in target.root.all_objects if obj.type == "MESH" and token in str(obj.get("oxal_inventory_native_role", ""))]
    if len(bars) != 1 or len(counts) != 1:
        raise ValueError("物品栏数字与底板的原生深度参照不唯一")
    def values(obj):
        evaluated = obj.evaluated_get(graph)
        return [-(inverse @ evaluated.matrix_world @ vertex.co).z for vertex in evaluated.data.vertices]
    foreground, background = max(values(counts[0])), min(values(bars[0]))
    if background - foreground <= 1e-7:
        raise ValueError("物品栏数字与底板之间没有可用的图标深度")
    return foreground, background


def canvas_matrix(target, representation=None):
    """Pure current-pose pixel canvas; no item JSON display is included."""
    unit = _pixel_unit(target)
    profile = representation.get("oxal_mc_studio_slot_visual_profile", "ITEM") if representation else "ITEM"
    lift = 0 if profile == "BLOCK" else .5
    camera = target.root.get("独立物品栏相机")
    if target.view_mode != "FIRST_PERSON" or camera is None:
        rest = target.rig.data.bones[target.gui_bone].matrix_local.to_3x3().inverted()
        result = (rest @ Matrix(((0, -1, 0), (1, 0, 0), (0, 0, 1)))).to_4x4()
        result.translation = rest @ Vector((-.0001, 0, unit * lift))
        return result @ Matrix.Diagonal((unit, unit * .00001, unit, 1))
    require_owned(target.root, (camera, camera.data))
    graph = bpy.context.evaluated_depsgraph_get()
    rig, cam = target.rig.evaluated_get(graph), camera.evaluated_get(graph)
    bone = rig.matrix_world @ rig.pose.bones[target.gui_bone].matrix
    head = cam.matrix_world.inverted_safe() @ bone.translation
    if -head.z <= 1e-8:
        raise ValueError("物品栏槽位位于相机后方")
    foreground, background = _depths(target, camera)
    depth = (foreground + background) / 2
    factor = depth / -head.z
    unit_world = (rig.matrix_world.to_3x3() @ Vector((0, 0, unit))).length
    head *= factor
    head.y += lift * unit_world * factor
    world = cam.matrix_world.to_quaternion().to_matrix().to_4x4() @ CAMERA_BASIS
    world = world @ Matrix.Scale(unit_world * factor, 4)
    world.translation = cam.matrix_world @ head
    return bone.inverted_safe() @ world


def _display_matrix(pivot, target):
    vertices = points(pivot)
    if not vertices:
        raise ValueError("格子图标没有可用几何")
    low = Vector(tuple(min(v[i] for v in vertices) for i in range(3)))
    high = Vector(tuple(max(v[i] for v in vertices) for i in range(3)))
    extent = high - low
    profile = pivot.get("oxal_mc_studio_slot_visual_profile", "ITEM")
    block = profile == "BLOCK"
    if block:
        orientation = (Matrix.Rotation(math.pi / 6, 4, "X") @ Matrix.Rotation(math.radians(225), 4, "Y") @ CAMERA_BASIS)
    else:
        thin = min(range(3), key=lambda axis: extent[axis])
        orientation = (Matrix(((0, 1, 0, 0), (0, 0, 1, 0), (1, 0, 0, 0), (0, 0, 0, 1))) if thin == 0
                       else CAMERA_BASIS if thin == 1 else Matrix.Identity(4))
        oriented = [orientation @ v for v in vertices]
        width = max(v.x for v in oriented) - min(v.x for v in oriented)
        height = max(v.y for v in oriented) - min(v.y for v in oriented)
        identifier = str(pivot.get("oxal_mc_studio_gui_identifier", ""))
        if profile == "WEAPON" and not identifier.endswith(":shield") and height > width * 1.4:
            orientation = Matrix.Rotation(-math.pi / 4, 4, "Z") @ orientation
    corners = [Vector((x, y, z)) for x in (low.x, high.x) for y in (low.y, high.y) for z in (low.z, high.z)]
    if block and pivot.get("oxal_mc_studio_slot_canvas_kind") == "BLOCK":
        corners = [Vector((x, y, z)) for x in (-.5, .5) for y in (-.5, .5) for z in (-.5, .5)]
    bounds = [orientation @ v for v in corners]
    width = max(v.x for v in bounds) - min(v.x for v in bounds)
    height = max(v.y for v in bounds) - min(v.y for v in bounds)
    fraction = (10 * (math.cos(math.pi / 6) + math.sqrt(2) * math.sin(math.pi / 6)) / 22) if block else 16 / 22
    unit = _pixel_unit(target)
    pitch = (target.rig.data.bones['2号物品栏'].head_local - target.rig.data.bones['1号物品栏'].head_local).length
    horizontal = _pixel_metrics(target)[1] if target.slot == "OFFHAND" else pitch / unit
    scale = min(horizontal * fraction / max(width, 1e-8), 22 * fraction / max(height, 1e-8))
    visible = [orientation @ v for v in vertices]
    center = Vector(tuple((min(v[i] for v in visible) + max(v[i] for v in visible)) / 2 for i in range(3)))
    camera = Matrix.Translation(-center * scale) @ Matrix.Scale(scale, 4) @ orientation
    return CAMERA_BASIS.inverted() @ camera, orientation


def _remember(pivot, matrix):
    pivot[GUI_SCHEMA] = 2
    pivot[GUI_MATRIX] = [float(value) for row in matrix for value in row]


def _clone_groups(transaction, tree, copies):
    for node in tree.nodes:
        if node.type != "GROUP" or node.node_tree is None:
            continue
        source = node.node_tree
        private = copies.get(source)
        if private is None:
            private = transaction.track(source.copy())
            copies[source] = private
            if private.animation_data:
                for curve in private.animation_data.drivers:
                    for variable in curve.driver.variables:
                        for target in variable.targets:
                            if target.id == source:
                                target.id = private
            _clone_groups(transaction, private, copies)
        node.node_tree = private


def _unlit(tree, shade, seen=None):
    from ..enchantment import enchantment
    from ..human.human_armor import animated
    seen = set() if seen is None else seen
    if tree in seen:
        return
    seen.add(tree)
    for node in tuple(tree.nodes):
        if node.name.startswith(enchantment.PREFIX):
            continue
        if node.type == "GROUP" and node.node_tree:
            _unlit(node.node_tree, shade, seen)
        if node.type not in {"BSDF_PRINCIPLED", "BSDF_DIFFUSE", "EMISSION"}:
            continue
        color = node.inputs["Base Color"] if node.type == "BSDF_PRINCIPLED" else node.inputs["Color"]
        for socket in (color, *((node.inputs['Alpha'],) if node.type == 'BSDF_PRINCIPLED' else ())):
            if not socket.is_linked and animated(tree, socket.path_from_id('default_value')):
                raise ValueError('图标底材有直接插帧的未连接颜色或透明输入，保留现有材质，未覆盖')
        if node.type == "EMISSION":
            destination = color
        else:
            emission = tree.nodes.new("ShaderNodeEmission")
            destination = emission.inputs["Color"]
            destination.default_value = color.default_value
            if color.is_linked:
                tree.links.new(color.links[0].from_socket, destination)
            result = emission.outputs[0]
            if node.type == "BSDF_PRINCIPLED":
                alpha = node.inputs["Alpha"]
                transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
                mix = tree.nodes.new("ShaderNodeMixShader")
                mix.inputs[0].default_value = alpha.default_value
                if alpha.is_linked:
                    tree.links.new(alpha.links[0].from_socket, mix.inputs[0])
                tree.links.new(transparent.outputs[0], mix.inputs[1])
                tree.links.new(result, mix.inputs[2])
                result = mix.outputs[0]
            for link in tuple(node.outputs[0].links):
                tree.links.new(result, link.to_socket)
        if shade:
            attribute = tree.nodes.new("ShaderNodeVertexColor")
            attribute.layer_name = GUI_SHADE
            back = tree.nodes.new('ShaderNodeVertexColor')
            back.layer_name = GUI_SHADE_BACK
            geometry = tree.nodes.new('ShaderNodeNewGeometry')
            face = tree.nodes.new('ShaderNodeMixRGB')
            tree.links.new(geometry.outputs['Backfacing'], face.inputs[0])
            tree.links.new(attribute.outputs['Color'], face.inputs[1])
            tree.links.new(back.outputs['Color'], face.inputs[2])
            multiply = tree.nodes.new("ShaderNodeMixRGB")
            multiply.blend_type = "MULTIPLY"
            multiply.inputs[0].default_value = 1
            multiply.inputs[1].default_value = destination.default_value
            if destination.is_linked:
                tree.links.new(destination.links[0].from_socket, multiply.inputs[1])
            tree.links.new(face.outputs[0], multiply.inputs[2])
            tree.links.new(multiply.outputs[0], destination)


def _private_material(transaction, original, shade):
    from ..asset.assets import _private_material as private_copy
    from ..enchantment import enchantment
    glint = enchantment.inspect(original)
    material = private_copy(transaction, original)
    copies = {}
    _clone_groups(transaction, material.node_tree, copies)
    _group_references(material, copies)
    _unlit(material.node_tree, shade)
    material[GUI_MATERIAL] = 2
    if glint:
        enchantment.stamp(material)
    return material


def _group_references(material, copies):
    key = 'oxal_mc_studio_glint_alpha_groups'
    groups = material.get(key)
    if groups:
        material[key] = {name: {field: copies.get(value, value) if isinstance(value, bpy.types.ID) else value
                               for field, value in group.items()} for name, group in groups.items()}


def _plain(value: Any) -> Any:
    if isinstance(value, bpy.types.ID):
        return value
    if hasattr(value, 'items'):
        return {key: _plain(item) for key, item in value.items()}
    if hasattr(value, 'to_list'):
        return value.to_list()
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def bridge_existing(transaction, owner, previous, staged):
    """Upgrade a proven private generated GUI material without changing its ID.

    Old nodes, native Action/NLA and editable inputs remain. Only new shader
    nodes and private group instances are added, with an exact rollback journal.
    """
    if staged.get(GUI_MATERIAL) != 2 or previous.get(GUI_MATERIAL) == 2:
        return previous
    from ..enchantment import enchantment
    if previous not in transaction.ids:
        require_owned(owner, (previous,))
    domain = {owner, *owned_ids(owner), *transaction.ids}
    users = bpy.data.user_map(subset=(previous,)).get(previous, ())
    if any(user not in domain for user in users):
        raise ValueError('旧图标材质仍被其它实例引用，未修改')
    gui = owner.get('oxal_inventory_gui')
    if gui is None:
        raise ValueError('旧图标材质没有明确的GUI归属')
    gui_objects = {gui, *gui.children_recursive}
    for obj in bpy.data.objects:
        if obj not in gui_objects and any(slot.material == previous for slot in obj.material_slots):
            raise ValueError('旧图标材质与手持物或其它对象共享，未影响其材质')
    tree = previous.node_tree
    if tree is None:
        raise ValueError('旧图标材质没有原生节点表面，未替换')
    glint = enchantment.inspect(previous)
    nodes = set(tree.nodes)
    links = [(link.from_socket, link.to_socket) for link in tree.links]
    groups = {node: node.node_tree for node in nodes if node.type == 'GROUP'}
    metadata = _plain(dict(previous.items()))
    def rollback():
        for node in tuple(tree.nodes):
            if node not in nodes:
                tree.nodes.remove(node)
        for node, source in groups.items():
            node.node_tree = source
        tree.links.clear()
        for source, target in links:
            tree.links.new(source, target)
        for key in tuple(previous.keys()):
            if key not in metadata:
                del previous[key]
        for key, value in metadata.items():
            previous[key] = value
        previous.update_tag()
    transaction.on_rollback(rollback)
    copies = {}
    _clone_groups(transaction, tree, copies)
    _group_references(previous, copies)
    shade = staged.get('oxal_mc_studio_inventory_gui_shaded', False)
    _unlit(tree, shade)
    previous[GUI_MATERIAL] = 2
    previous['oxal_mc_studio_inventory_gui_shaded'] = bool(shade)
    if glint:
        enchantment.stamp(previous)
    from ...util.shader_layout import arrange_owned_shaders

    arrange_owned_shaders((previous,), on_rollback=transaction.on_rollback)
    previous.update_tag()
    return previous


def _project_geometry(gui, target):
    """Encode a homogeneous GUI pass into private mesh coordinates once.

    Blocks have an orthographic GUI pass. Other stock models retain their
    perspective rays while fitting between the native digits and background.
    The native pixel canvas itself stays an ordinary, uniform TRS transform.
    """
    if target.view_mode != 'FIRST_PERSON':
        return
    camera = target.root['独立物品栏相机']
    graph = bpy.context.evaluated_depsgraph_get()
    rig, cam = target.rig.evaluated_get(graph), camera.evaluated_get(graph)
    bone = rig.matrix_world @ rig.pose.bones[target.gui_bone].matrix
    canvas = cam.matrix_world.inverted_safe() @ bone @ canvas_matrix(target, gui)
    local = matrices(gui)
    samples = {obj: (canvas @ matrix, [canvas @ matrix @ vertex.co for vertex in obj.data.vertices]) for obj, matrix in local.items()}
    all_points = [point for _matrix, vertices in samples.values() for point in vertices]
    foreground, background = _depths(target, camera)
    depth = (foreground + background) / 2
    thickness = max(point.z for point in all_points) - min(point.z for point in all_points)
    compression = min(1., (background - foreground) * .9 / max(thickness, 1e-12))
    block = gui.get('oxal_mc_studio_slot_visual_profile') == 'BLOCK'
    projected = {}
    for obj, (matrix, vertices) in samples.items():
        result = []
        for point in vertices:
            old_depth = -point.z
            if old_depth <= camera.data.clip_start:
                raise ValueError('格子图标的原始投影跨越相机近裁剪面')
            new_depth = depth + (old_depth - depth) * compression
            denominator = depth if block else old_depth
            result.append(Vector((point.x * new_depth / denominator, point.y * new_depth / denominator, -new_depth)))
        projected[obj] = (matrix, result)
    # Center the actual projected envelope, rather than a hidden bounding-box
    # corner. Use one analytic pixel-plane fit shared by all model components.
    plane = [point * (depth / -point.z) for _matrix, vertices in projected.values() for point in vertices]
    center = Vector(((min(p.x for p in plane) + max(p.x for p in plane)) / 2,
                     (min(p.y for p in plane) + max(p.y for p in plane)) / 2, -depth))
    origin = canvas.translation
    fraction = (10 * (math.cos(math.pi / 6) + math.sqrt(2) * math.sin(math.pi / 6)) / 22) if block else 16 / 22
    pitch = (target.rig.data.bones['2号物品栏'].head_local - target.rig.data.bones['1号物品栏'].head_local).length
    width_pixels = _pixel_metrics(target)[1] if target.slot == 'OFFHAND' else pitch / _pixel_unit(target)
    pixel_size = canvas.to_3x3().col[0].length
    factor = min(1., width_pixels * fraction * pixel_size / max(max(p.x for p in plane) - min(p.x for p in plane), 1e-12),
                 22 * fraction * pixel_size / max(max(p.y for p in plane) - min(p.y for p in plane), 1e-12))
    for obj, (matrix, vertices) in projected.items():
        del vertices
        record = {'schema': 1, 'matrix': [float(value) for row in matrix for value in row],
                  'depth': depth, 'compression': compression, 'center': [float(center[i]) for i in range(3)], 'origin': [float(origin[i]) for i in range(3)],
                  'factor': factor, 'block': block, 'near': float(camera.data.clip_start)}
        result = _projection_points(record, [vertex.co for vertex in obj.data.vertices])
        for vertex, point in zip(obj.data.vertices, result, strict=True):
            vertex.co = point
        obj.data.update()
        if obj.get('oxal_mc_studio_pixel_topology'):
            record['signature'] = _projection_signature(record)
            obj[PROJECTION] = json.dumps(record, separators=(',', ':'), sort_keys=True)


def _projection_signature(record):
    payload = {key: value for key, value in record.items() if key != 'signature'}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf8')).hexdigest()


def _projection_points(record, vertices):
    values = record['matrix']
    matrix = Matrix(tuple(tuple(values[row * 4 + col] for col in range(4)) for row in range(4)))
    inverse = matrix.inverted_safe()
    depth, compression, factor = record['depth'], record['compression'], record['factor']
    center, origin = record['center'], record['origin']
    result = []
    for vertex in vertices:
        point = matrix @ vertex
        old_depth = -point.z
        if old_depth <= record['near']:
            raise ValueError('新图标轮廓跨越已保存投影的相机近裁剪面，未替换')
        new_depth = depth + (old_depth - depth) * compression
        divisor = depth if record['block'] else old_depth
        point = Vector((point.x * new_depth / divisor, point.y * new_depth / divisor, -new_depth))
        ratio = new_depth / depth
        point.x = (origin[0] + (point.x / ratio - center[0]) * factor) * ratio
        point.y = (origin[1] + (point.y / ratio - center[1]) * factor) * ratio
        result.append(inverse @ point)
    return result


def replay_projection(obj, new_mesh, *, transaction=None):
    """Explicit pack preparation: replay on a proven fresh unassigned Mesh.

    No Scene, current camera, UI, material, modifier or old mesh is modified.
    The source renderer's affine sprite frame must have been applied first.
    """
    ensure_mutation_allowed()
    saved = obj.get(PROJECTION)
    if saved is None:
        return False
    if (transaction is None or new_mesh not in transaction.ids or new_mesh == obj.data
            or not isinstance(new_mesh, bpy.types.Mesh) or new_mesh.users != 0):
        raise ValueError('图标投影只能重放到本次事务创建且尚未分配的私有网格')
    if obj.get('oxal_mc_studio_representation') != 'GUI' or not obj.get('oxal_mc_studio_pixel_topology'):
        raise ValueError('图标投影记录没有对应的像素GUI归属')
    try:
        record = json.loads(saved)
        required = {'schema', 'matrix', 'depth', 'compression', 'center', 'origin', 'factor', 'block', 'near', 'signature'}
        if not isinstance(record, dict) or set(record) != required or record['schema'] != 1:
            raise ValueError('schema')
        if record['signature'] != _projection_signature(record) or not isinstance(record['block'], bool):
            raise ValueError('signature')
        for name, count in (('matrix', 16), ('center', 3), ('origin', 3)):
            if (not isinstance(record[name], list) or len(record[name]) != count
                    or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in record[name])):
                raise ValueError(name)
        for name in ('depth', 'compression', 'factor', 'near'):
            value = record[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(name)
        if record['compression'] > 1 or record['factor'] > 1 or record['near'] >= record['depth']:
            raise ValueError('range')
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError('图标的已生成投影记录不完整或已被修改，未替换网格') from error
    result = _projection_points(record, [vertex.co for vertex in new_mesh.vertices])
    for vertex, point in zip(new_mesh.vertices, result, strict=True):
        vertex.co = point
    new_mesh.update()
    return True


def prepare(transaction, scene, source_root, gui, target):
    """Prepare only new GUI data; bind_source publishes the canvas afterwards."""
    del scene
    displays = [obj for obj in gui.children if obj.get("oxal_mc_studio_model_part") == DISPLAY_ROLE]
    if len(displays) != 1:
        raise ValueError("格子图标没有唯一的私有显示变换")
    display = displays[0]
    if gui not in transaction.ids or display not in transaction.ids:
        raise ValueError("只可准备本次创建的格子图标")
    # The raw frame excludes the old item JSON GUI rotation entirely.
    display.matrix_basis = Matrix.Identity(4)
    gui['oxal_mc_studio_gui_identifier'] = source_root.get('oxal_mc_studio_library_identifier', '')
    fitted, orientation = _display_matrix(gui, target)
    display.matrix_basis = fitted
    _remember(display, fitted)
    block = gui.get("oxal_mc_studio_slot_visual_profile") == "BLOCK"
    cache = {}
    lights = (Vector((.2, 1, -.7)).normalized(), Vector((-.2, 1, .7)).normalized())
    # Normals must precede the thin canvas, and include only the declared model
    # frame, never a deformed normal after depth compression.
    display.matrix_basis = Matrix.Identity(4)
    for obj, local in matrices(gui).items():
        if obj not in transaction.ids or obj.data not in transaction.ids or obj.data.users != 1:
            raise ValueError("格子图标几何归属不明确")
        if block:
            attribute = obj.data.color_attributes.get(GUI_SHADE) or obj.data.color_attributes.new(name=GUI_SHADE, type="FLOAT_COLOR", domain="CORNER")
            back = obj.data.color_attributes.get(GUI_SHADE_BACK) or obj.data.color_attributes.new(name=GUI_SHADE_BACK, type="FLOAT_COLOR", domain="CORNER")
            # Adding a CustomData layer can invalidate the first RNA wrapper.
            # Resolve both attributes after allocation before writing corners.
            attribute = obj.data.color_attributes[GUI_SHADE]
            back = obj.data.color_attributes[GUI_SHADE_BACK]
            normals = orientation.to_3x3() @ local.to_3x3().inverted_safe().transposed()
            for polygon in obj.data.polygons:
                normal = (normals @ polygon.normal).normalized()
                amount = min(1., .4 + .6 * sum(max(0., direction.dot(normal)) for direction in lights))
                back_amount = min(1., .4 + .6 * sum(max(0., direction.dot(-normal)) for direction in lights))
                for index in polygon.loop_indices:
                    attribute.data[index].color = (amount, amount, amount, 1)
                    back.data[index].color = (back_amount, back_amount, back_amount, 1)
        for slot in obj.material_slots:
            original = slot.material
            if original is not None:
                if original not in cache:
                    cache[original] = _private_material(transaction, original, block)
                    cache[original]['oxal_mc_studio_inventory_gui_shaded'] = block
                slot.material = cache[original]
        obj.visible_shadow = False
    display.matrix_basis = fitted
    _project_geometry(gui, target)
    _bake_display(transaction, gui, display, fitted)
    _remember(gui, canvas_matrix(target, gui))


def _bake_display(transaction, gui, display, fitted):
    """Keep the GUI display in private geometry, without an extra scene Empty.

    The nonuniform third-person canvas remains on its binding pivot; never
    combine a sheared canvas/display matrix in one Object.matrix_basis.
    """
    from ..asset.asset_slot_geometry import apply

    display.matrix_basis = Matrix.Identity(4)
    local = matrices(gui)
    corrections = {obj: matrix.inverted() @ fitted @ matrix for obj, matrix in local.items()}
    apply(transaction, gui, fitted)
    for obj, correction in corrections.items():
        saved = obj.get(PROJECTION)
        if saved is not None:
            record = json.loads(saved)
            values = record['matrix']
            original = Matrix(tuple(tuple(values[i:i + 4]) for i in range(0, 16, 4)))
            record['matrix'] = [float(value) for row in original @ correction.inverted() for value in row]
            record['signature'] = _projection_signature(record)
            obj[PROJECTION] = json.dumps(record, separators=(',', ':'), sort_keys=True)
    for child in tuple(display.children):
        basis, inverse = child.matrix_basis.copy(), child.matrix_parent_inverse.copy()
        child.parent = gui
        child.matrix_parent_inverse, child.matrix_basis = inverse, basis
    transaction.forget(display)
    bpy.data.objects.remove(display, do_unlink=True)
    gui[DISPLAY_BAKED] = 1


def stage_existing(transaction, scene, staged, source_root, gui):
    from .inventory_slots import HOST, RIG, SLOT, resolve_target
    host = source_root.get(HOST)
    if host is None:
        raise ValueError("格子图标重建缺少实际宿主")
    target = resolve_target(scene, {"inventory_instance_id": host[INSTANCE], "slot": source_root.get(SLOT, "")})
    if target.root != host or source_root.get(RIG) != target.rig:
        raise ValueError("格子图标重建的原生宿主不一致")
    prepare(transaction, scene, staged, gui, target)
    return target
