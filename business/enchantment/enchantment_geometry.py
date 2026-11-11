"""Bounded UV-pixel geometry for explicit enchantment preparation.

asset_id: oxal.enchantment.pixel_geometry; version: 1; policy: NEW_INSTANCE;
scope: one owned Mesh; source: its actual UVs, weights and alpha shader.
Only small face descriptors are prepared in Python. Native fields resample the
input corners every frame, preserving shape/deformation and texture animation.
"""

import math

import bpy

from ...util.animation import action_fcurves
from . import enchantment
from . import enchantment_outline as outline

DATA = "oxal_mc_studio_enchant_pixel_data"
PREFIX = "oxal_enchant_"
MAX_PIXELS = 65536
MAX_SOURCE_FACES = 65536
MAX_ALPHA_NODES = 256
MATERIAL_ATTRIBUTE = "oxal_enchant_generated_source_material_slot"
OUTLINE_PROXY_ATTRIBUTE = "oxal_enchant_generated_outline_proxy"
OUTLINE_PROXY_MARK = "oxal_mc_studio_enchant_outline_proxy_schema"


def _opacity(material):
    node = material.node_tree.nodes.get(enchantment.PREFIX + "Visible Alpha") if material and material.node_tree else None
    if node is None:
        raise ValueError("像素几何需要先应用新版附魔材质")
    return node.inputs[0]


def _images(material):
    found, seen = set(), set()
    def visit(tree):
        if tree.as_pointer() in seen:
            return
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            if node.name.startswith(enchantment.PREFIX):
                continue
            if node.type == "TEX_IMAGE" and node.image is not None:
                found.add(node.image)
            elif node.type == "GROUP" and node.node_tree is not None:
                visit(node.node_tree)
    if material.node_tree is not None:
        visit(material.node_tree)
    return found


def _materials(obj):
    """Native Object-linked slots are the actual rendered material identity."""
    return tuple(slot.material for slot in obj.material_slots)


def needs_contour(obj, *, transaction=None):
    """Tool-only alpha inspection; never called by draw or a timer."""
    if obj.get("oxal_mc_studio_pixel_topology"):
        return any(material and material.get("oxal_mc_studio_texture_animated") for material in _materials(obj))
    cache = getattr(transaction, "enchant_alpha_cache", {}) if transaction is not None else {}
    if transaction is not None:
        transaction.enchant_alpha_cache = cache
    for material in _materials(obj):
        if material is None:
            continue
        for image in _images(material):
            if image not in cache:
                pixels = image.pixels[:]
                cache[image] = any(value < .999 for value in pixels[3::4])
            if cache[image]:
                return True
    return False


def needs_weapon_contour(obj, *, transaction=None):
    """Current-frame alpha for a native animated sprite, never weapon relief.

    Static weapon sprites and authored native weapon models keep their source
    topology. Ownership and shader contracts are checked by the calling Tool;
    these two native sprite/animation declarations only select its flat flow.
    """
    return (obj.type == "MESH" and bool(obj.get("oxal_mc_studio_pixel_topology"))
            and needs_contour(obj, transaction=transaction))


def _mesh_signature(mesh, names):
    result = []
    for name in names:
        attribute = mesh.attributes.get(name)
        if attribute is None:
            return "MISSING"
        result.append((name, attribute.data_type, attribute.domain,
                       [tuple(item.vector) if hasattr(item, "vector") else item.value for item in attribute.data]))
    return outline.digest(result)


def _attribute_values(mesh, names):
    return [(name, mesh.attributes[name].data_type, mesh.attributes[name].domain,
             [tuple(item.vector) if hasattr(item, "vector") else item.value for item in mesh.attributes[name].data]) for name in names]


def _restore_attributes(mesh, names, saved):
    for name in names:
        if mesh.attributes.get(name) is not None:mesh.attributes.remove(mesh.attributes[name])
    for name, datatype, domain, values in saved:
        attribute = mesh.attributes.new(name, datatype, domain)
        for item, value in zip(attribute.data, values, strict=True):
            if hasattr(item, "vector"):item.vector = value
            else:item.value = value


def _saved(obj):
    record = obj.get(DATA)
    return dict(record.items()) if record else None


def validate_data(root, obj, *, transaction=None):
    record = _saved(obj)
    if record is None:
        return
    mesh = record.get("mesh")
    if not isinstance(mesh, bpy.types.Mesh):
        raise TypeError("附魔像素数据缺少实际网格引用")
    outline._owned(root, (mesh,), transaction)
    expected = [PREFIX + part for part in ("nx", "ny", "corner0", "corner1", "corner2", "corner3")]
    expected.extend(PREFIX + f"uv{index}_{corner}" for index in range(len(mesh.uv_layers)) for corner in range(4))
    if list(record.get("attributes", ())) != expected:
        raise ValueError("附魔像素属性列表不符合已知生成合同，未覆盖")
    if record.get("hash") != _mesh_signature(mesh, tuple(record["attributes"])):
        raise ValueError("附魔像素描述属性含用户修改，未覆盖")


def generated_attributes(root, obj, *, transaction=None):
    record = _saved(obj)
    if record is None:
        return ()
    validate_data(root, obj, transaction=transaction)
    if record["mesh"] != obj.data:
        raise ValueError("附魔像素记录与当前网格不一致，未忽略其属性")
    return tuple(record["attributes"])


def _frame_size(material):
    images = _images(material)
    if not images:
        return 16, 16
    width = max(image.size[0] for image in images)
    height = max(image.size[1] for image in images)
    import json
    animation = json.loads(material.get("oxal_mc_studio_texture_animation", "null"))
    if animation:
        width //= animation[0]
        height //= animation[1]
    return width, height


def _prepare(transaction, root, obj):
    source = obj.data
    outline._owned(root, (source,), transaction)
    old = _saved(obj)
    if old:
        validate_data(root, obj, transaction=transaction)
        if source != old["mesh"] and source not in transaction.ids:
            raise ValueError("附魔像素网格被用户替换，未接管")
    if source.animation_data:
        raise ValueError("此网格含网格动画，未替换其像素描述")
    in_place = source.shape_keys is not None
    if in_place:
        permitted = {obj, root, source.shape_keys}
        if any(user not in permitted for user in bpy.data.user_map(subset=(source,)).get(source, ())):
            raise ValueError("形态键网格被其它数据共享，不能保持原Key身份并准备像素")
        if old and source != old["mesh"] and source.shape_keys != old["mesh"].shape_keys:
            raise ValueError("原生形态键在材质准备中被私有复制，未替换其实际绑定")
    if len(source.polygons) > MAX_SOURCE_FACES or not source.uv_layers:
        raise ValueError("像素凹凸需要有效UV，原始面数不得超过65536")
    if any(len(polygon.vertices) != 4 for polygon in source.polygons):
        raise ValueError("此模型含非四边形面，不能可靠重采样UV像素；原模型未修改")
    sizes = [_frame_size(material) if material else (16, 16) for material in _materials(obj)]
    uv = source.uv_layers.active or source.uv_layers[0]
    rows, total = [], 0
    for polygon in source.polygons:
        if polygon.material_index >= len(sizes):
            raise ValueError("像素凹凸原表面缺少实际材质槽，未猜测对应贴图")
        coords = [uv.data[index].uv.copy() for index in polygon.loop_indices]
        width, height = sizes[polygon.material_index]
        a, b = coords[1] - coords[0], coords[3] - coords[0]
        nx = max(1, math.ceil(math.hypot(a.x * width, a.y * height) - 1e-5))
        ny = max(1, math.ceil(math.hypot(b.x * width, b.y * height) - 1e-5))
        total += nx * ny
        rows.append((nx, ny))
    if total > MAX_PIXELS:
        raise ValueError(f"像素凹凸预计{total}面，超过65536面预算；请降低材质分辨率")
    mesh = source if in_place else transaction.track(source.copy())
    previous_names = [name for name in old["attributes"] if mesh.attributes.get(name)] if old else []
    if old and previous_names and (len(previous_names) != len(old["attributes"]) or _mesh_signature(mesh, previous_names) != old["hash"]):
        raise ValueError("本次材质网格副本中的像素属性不完整，未覆盖")
    previous_attributes = _attribute_values(mesh, previous_names) if in_place else []
    names = []
    before = dict(obj[DATA].items()) if DATA in obj else None
    def restore():
        if in_place:
            _restore_attributes(mesh, {*names, *(part[0] for part in previous_attributes)}, previous_attributes)
        obj.data = source
        if before is None:
            if DATA in obj:del obj[DATA]
        else:obj[DATA] = before
    transaction.on_rollback(restore)
    for name in previous_names:mesh.attributes.remove(mesh.attributes[name])
    def attribute(name, datatype, values):
        name = PREFIX + name
        if mesh.attributes.get(name) is not None:
            raise ValueError("网格已有同名用户像素属性，未覆盖")
        result = mesh.attributes.new(name, datatype, "FACE")
        names.append(name)
        for item, value in zip(result.data, values, strict=True):
            if datatype == "FLOAT_VECTOR":
                item.vector = value
            else:
                item.value = value
        return name
    attribute("nx", "INT", [row[0] for row in rows])
    attribute("ny", "INT", [row[1] for row in rows])
    for corner in range(4):
        attribute("corner" + str(corner), "INT", [polygon.vertices[corner] for polygon in source.polygons])
    for index, layer in enumerate(source.uv_layers):
        for corner in range(4):
            attribute(f"uv{index}_{corner}", "FLOAT_VECTOR",
                      [(*layer.data[polygon.loop_indices[corner]].uv, 0) for polygon in source.polygons])
    obj.data = mesh
    obj[DATA] = {"mesh": mesh, "attributes": names, "hash": _mesh_signature(mesh, names), "pixels": total}
    return mesh


class Alpha:
    """Translate only the existing float/color alpha DAG to native fields.

    Animated unlinked values reference their original tree's evaluated RNA;
    the user's Action/NLA is neither copied nor edited. Shader closures are
    already exposed by the glint service as a plain opacity output.
    """
    SUPPORTED = frozenset({"MATH", "VECTOR_MATH", "VECT_MATH", "VALUE", "RGB", "COMBXYZ", "SEPXYZ", "COMBINE_XYZ", "SEPARATE_XYZ",
                           "MIX", "MIX_RGB", "VALTORGB", "RGBTOBW", "CLAMP", "MAP_RANGE"})

    def __init__(self, graph):
        self.graph, self.cache, self.animations = graph, {}, {}
        self.uv_name = "UVMap"
        self.backfacing = 0
        self.backface_used = False

    def animated(self, socket):
        tree = socket.id_data
        if tree not in self.animations:
            animation = tree.animation_data
            curves = list(animation.drivers) if animation else []
            if animation:
                curves.extend(action_fcurves(animation.action, slot=getattr(animation, "action_slot", None)))
                strips = [strip for track in animation.nla_tracks for strip in track.strips]
                while strips:
                    strip = strips.pop()
                    curves.extend(action_fcurves(strip.action, slot=getattr(strip, "action_slot", None)))
                    strips.extend(getattr(strip, "strips", ()))
            self.animations[tree] = {curve.data_path for curve in curves}
        return socket.path_from_id("default_value") in self.animations[tree]

    def default(self, target, source, component=None):
        if not self.animated(source):
            target.default_value = source.default_value if component is None else source.default_value[component]
            return
        value = source.default_value if component is None else source.default_value[component]
        indices = range(len(value)) if hasattr(value, "__len__") and not isinstance(value, str) else (None,)
        for index in indices:
            curve = target.driver_add("default_value") if index is None else target.driver_add("default_value", index)
            curve.driver.type, curve.driver.expression = "SCRIPTED", "value"
            variable = curve.driver.variables.new()
            variable.name, variable.type = "value", "SINGLE_PROP"
            tree = source.id_data
            material = self.graph.embedded_owners.get(tree)
            if tree.is_embedded_data:
                if material is None or material.node_tree != tree:
                    raise ValueError("动态透明参数缺少实际材质引用，未创建临时节点树驱动")
                # Embedded shader trees are not standalone IDs. Undo/Redo can
                # lose a direct NODETREE target; use the actual Material and
                # its native node_tree path so the binding is persistent.
                variable.targets[0].id_type = "MATERIAL"
                variable.targets[0].id = material
                prefix = "node_tree."
            else:
                variable.targets[0].id_type = "NODETREE"
                variable.targets[0].id = tree
                prefix = ""
            selected = index if component is None else component
            variable.targets[0].data_path = prefix + source.path_from_id("default_value") + (f"[{selected}]" if selected is not None else "")

    def input(self, socket, context=()):
        if socket.is_linked:
            return self.output(socket.links[0].from_socket, context)
        if socket.type == "VECTOR":
            node = self.graph.node("ShaderNodeCombineXYZ", "Alpha Vector Constant")
            for index in range(3):self.default(node.inputs[index], socket, component=index)
            return node.outputs[0]
        node = self.graph.node("ShaderNodeValue" if socket.type in {"VALUE", "INT", "BOOLEAN"} else "ShaderNodeRGB", "Alpha Constant")
        self.default(node.outputs[0], socket)
        return node.outputs[0]

    def output(self, socket, context=()):
        node = socket.node
        key = (node.as_pointer(), tuple(part.as_pointer() for part in context), self.backfacing)
        if node.type == "NEW_GEOMETRY" and socket.name == "Backfacing":
            self.backface_used = True
            result = self.graph.node("ShaderNodeValue", "Alpha Surface Side")
            result.outputs[0].default_value = self.backfacing
            return result.outputs[0]
        if node.type == "GROUP":
            output = next(part for part in node.node_tree.nodes if part.type == "GROUP_OUTPUT" and part.is_active_output)
            return self.input(output.inputs[list(node.outputs).index(socket)], (*context, node))
        if node.type == "GROUP_INPUT":
            if not context:raise ValueError("透明组输入没有实例上下文")
            parent = context[-1]
            index = next(i for i, part in enumerate(parent.inputs) if part.identifier == socket.identifier)
            return self.input(parent.inputs[index], context[:-1])
        if node.type in {"TEX_COORD", "UVMAP"}:
            if node.type == "TEX_COORD" and socket.name != "UV":
                raise ValueError("像素透明轮廓需要UV坐标，未猜测其它坐标")
            return self.graph.attribute(node.uv_map if node.type == "UVMAP" and node.uv_map else self.uv_name, "FLOAT_VECTOR")
        if key not in self.cache:
            if len(self.cache) >= MAX_ALPHA_NODES:
                raise ValueError("透明节点图超过256节点预算，未创建高成本轮廓")
            if node.type == "TEX_IMAGE":
                if node.image is None or node.projection != "FLAT" or node.image.source not in {"FILE", "GENERATED"}:
                    raise ValueError("像素轮廓暂不支持此图片坐标/来源")
                result = self.graph.node("GeometryNodeImageTexture", "Alpha " + node.name)
                result.inputs["Image"].default_value = node.image
                result.interpolation, result.extension = node.interpolation, node.extension
                if node.inputs["Vector"].is_linked:
                    self.graph.feed(result.inputs["Vector"], self.input(node.inputs["Vector"], context))
                else:self.graph.feed(result.inputs["Vector"], self.graph.attribute(self.uv_name, "FLOAT_VECTOR"))
            else:
                if node.type not in self.SUPPORTED:
                    raise ValueError("透明度含未支持的节点，保留原模型：" + node.bl_idname)
                result = self.graph.node(node.bl_idname, "Alpha " + node.name)
                for name in ("operation", "data_type", "blend_type", "use_clamp", "clamp", "clamp_factor", "clamp_result", "factor_mode", "interpolation_type"):
                    if hasattr(node, name) and hasattr(result, name):setattr(result, name, getattr(node, name))
                self.cache[key] = result
                for old, new in zip(node.inputs, result.inputs, strict=True):
                    if not hasattr(new, "default_value"):continue
                    if old.is_linked:self.graph.feed(new, self.input(old, context))
                    else:self.default(new, old)
                for old, new in zip(node.outputs, result.outputs, strict=True):
                    if hasattr(old, "default_value"):self.default(new, old)
                if node.type == "VALTORGB":
                    old, new = node.color_ramp, result.color_ramp
                    new.interpolation, new.color_mode, new.hue_interpolation = old.interpolation, old.color_mode, old.hue_interpolation
                    while len(new.elements) > 2:new.elements.remove(new.elements[-1])
                    for index, element in enumerate(old.elements):
                        target = new.elements[index] if index < 2 else new.elements.new(element.position)
                        target.position, target.color = element.position, element.color
            self.cache[key] = result
        result = self.cache[key]
        return result.outputs[socket.name] if node.type == "TEX_IMAGE" else result.outputs[list(node.outputs).index(socket)]


def _bilinear(graph, values, u, v, *, vector=True):
    a = graph.math("SUBTRACT", 1, u)
    b = graph.math("SUBTRACT", 1, v)
    weights = [graph.math("MULTIPLY", a, b), graph.math("MULTIPLY", u, b),
               graph.math("MULTIPLY", u, v), graph.math("MULTIPLY", a, v)]
    parts = [(graph.vector("SCALE", value, weight) if vector else graph.math("MULTIPLY", value, weight))
             for value, weight in zip(values, weights, strict=True)]
    combine = graph.vector if vector else graph.math
    return combine("ADD", combine("ADD", parts[0], parts[1]), combine("ADD", parts[2], parts[3]))


def _capture(graph, geometry, domain, items):
    node = graph.node("GeometryNodeCaptureAttribute", "Capture " + domain)
    node.domain = domain
    node.capture_items.clear()
    for datatype, name, value in items:
        node.capture_items.new(datatype, name)
        graph.feed(node.inputs[name], value)
    graph.feed(node.inputs["Geometry"], geometry)
    return node.outputs["Geometry"], [node.outputs[name] for _datatype, name, _value in items]


def _sample(graph, geometry, value, index, datatype):
    node = graph.node("GeometryNodeSampleIndex", "Original Corner")
    node.data_type, node.domain = datatype, "POINT"
    graph.feed(node.inputs["Geometry"], geometry)
    graph.feed(node.inputs["Value"], value)
    graph.feed(node.inputs["Index"], index)
    return node.outputs["Value"]


def _grid(graph, obj):
    source = graph.entry.outputs["Geometry"]
    index = graph.node("GeometryNodeInputIndex", "Source Vertex Index").outputs[0]
    geometry, ids = _capture(graph, source, "POINT", [("INT", "Original Vertex", index)])
    corners = [graph.attribute(PREFIX + "corner" + str(i), "INT") for i in range(4)]
    position = graph.node("GeometryNodeInputPosition", "Source Position").outputs[0]
    items = [("VECTOR", "Position " + str(i), _sample(graph, source, position, corner, "FLOAT_VECTOR"))
             for i, corner in enumerate(corners)]
    used = {weight.group for vertex in obj.data.vertices for weight in vertex.groups if abs(weight.weight) > 1e-8}
    groups = tuple(group.name for group in obj.vertex_groups if group.index in used)
    if len(groups) > 32:
        raise ValueError("像素凹凸最多支持32个原生顶点组；原绑定未修改")
    for gi, group in enumerate(groups):
        value = graph.attribute(group, "FLOAT")
        items.extend(("FLOAT", f"Weight {gi} {i}", _sample(graph, source, value, corner, "FLOAT"))
                     for i, corner in enumerate(corners))
    material_index = graph.node("GeometryNodeInputMaterialIndex", "Original Material Index").outputs[0]
    items.append(("INT", "Original Material Slot", material_index))
    geometry, captured = _capture(graph, geometry, "FACE", items)
    duplicate = graph.node("GeometryNodeDuplicateElements", "UV Pixel Faces")
    duplicate.domain = "FACE"
    nx, ny = (graph.attribute(PREFIX + axis, "INT") for axis in ("nx", "ny"))
    graph.feed(duplicate.inputs["Geometry"], geometry)
    graph.feed(duplicate.inputs["Amount"], graph.math("MULTIPLY", nx, ny))
    cell = duplicate.outputs["Duplicate Index"]
    cu = graph.math("MAXIMUM", graph.math("COMPARE", ids[0], corners[1]), graph.math("COMPARE", ids[0], corners[2]))
    cv = graph.math("MAXIMUM", graph.math("COMPARE", ids[0], corners[2]), graph.math("COMPARE", ids[0], corners[3]))
    # Compare's default epsilon is .5; source vertex indices are exact ints.
    u = graph.math("DIVIDE", graph.math("ADD", graph.math("MODULO", cell, nx), cu), nx)
    v = graph.math("DIVIDE", graph.math("ADD", graph.math("FLOOR", graph.math("DIVIDE", cell, nx)), cv), ny)
    move = graph.node("GeometryNodeSetPosition", "Interpolated Pixel Corners")
    graph.feed(move.inputs["Geometry"], duplicate.outputs["Geometry"])
    graph.feed(move.inputs["Position"], _bilinear(graph, captured[:4], u, v))
    geometry = move.outputs["Geometry"]
    for li, layer in enumerate(obj.data.uv_layers):
        values = [graph.attribute(PREFIX + f"uv{li}_{corner}", "FLOAT_VECTOR") for corner in range(4)]
        geometry = graph.store(geometry, layer.name, "FLOAT2", "CORNER", _bilinear(graph, values, u, v))
    for gi, group in enumerate(groups):
        values = captured[4 + gi * 4:8 + gi * 4]
        geometry = graph.store(geometry, group, "FLOAT", "POINT", _bilinear(graph, values, u, v, vector=False))
    # Duplicate Elements keeps face indices but drops its material table in
    # supported Blender versions. Bind actual materials before any Join can
    # collapse those unbound indices, retaining the frozen source slot field
    # for alpha selection even if the resulting material table is reordered.
    source_material = captured[-1]
    for index, material in enumerate(_materials(obj)):
        if material is None:
            continue
        assign = graph.node("GeometryNodeSetMaterial", "Original Pixel Material " + str(index))
        assign.inputs["Material"].default_value = material
        graph.feed(assign.inputs["Geometry"], geometry)
        graph.feed(assign.inputs["Selection"], graph.math("COMPARE", source_material, index))
        geometry = assign.outputs["Geometry"]
    return geometry, source_material


def _cutout(graph, obj, geometry, source_material=None):
    source = (graph.node("GeometryNodeInputMaterialIndex", "Source Material").outputs[0]
              if source_material is None else source_material)
    opacity = 0
    sampler = Alpha(graph)
    sampler.uv_name = (obj.data.uv_layers.active or obj.data.uv_layers[0]).name
    for index, material in enumerate(_materials(obj)):
        selected = graph.math("COMPARE", source, index, "Material Selection")
        if material is not None:
            graph.embedded_owners[material.node_tree] = material
        sampler.backface_used = False
        alpha = sampler.input(_opacity(material)) if material is not None else 1
        if sampler.backface_used:
            # A coplanar two-sided source may have different front/back UVs.
            # Keep their union in geometry; the original shader still selects
            # the correct visible side and current animation frame.
            sampler.backfacing = 1
            back = sampler.input(_opacity(material))
            alpha = graph.math("MAXIMUM", alpha, back, "Two Sided Alpha")
            sampler.backfacing = 0
        opacity = graph.math("ADD", opacity, graph.math("MULTIPLY", selected, alpha))
    delete = graph.node("GeometryNodeDeleteGeometry", "Remove Transparent Pixels")
    delete.domain, delete.mode = "FACE", "ALL"
    graph.feed(delete.inputs["Geometry"], geometry)
    graph.feed(delete.inputs["Selection"], graph.math("LESS_THAN", opacity, .001))
    return delete.outputs["Geometry"]


def _connect_contour(graph, obj, geometry):
    """Join only coincident derived corners, preserving corner UV/materials.

    Automatic contour is a surface, rather than independently closed pixels.
    Bound the native tolerance by both the current source extent and the
    smallest authored pixel edge, far below one pixel or a transparent hole.
    The source Mesh/Key and explicit per-pixel relief never pass through this.
    """
    mesh = obj.data
    nx, ny = mesh.attributes[PREFIX + "nx"], mesh.attributes[PREFIX + "ny"]
    steps = []
    for face in mesh.polygons:
        corners = [mesh.vertices[index].co for index in face.vertices]
        for a, b, count in ((0, 1, nx.data[face.index].value), (3, 2, nx.data[face.index].value),
                            (0, 3, ny.data[face.index].value), (1, 2, ny.data[face.index].value)):
            steps.append((corners[a] - corners[b]).length / count)
    cap = min(steps, default=0) * 1e-5
    bounds = graph.node("GeometryNodeBoundBox", "Contour Source Extent")
    graph.feed(bounds.inputs["Geometry"], graph.entry.outputs["Geometry"])
    extent = graph.vector("LENGTH", graph.vector("SUBTRACT", bounds.outputs["Max"], bounds.outputs["Min"]))
    merge = graph.node("GeometryNodeMergeByDistance", "Connected Alpha Contour")
    if merge.bl_rna.properties.get("mode") is not None:
        merge.mode = "ALL"
    else:
        merge.inputs["Mode"].default_value = "All"
    graph.feed(merge.inputs["Geometry"], geometry)
    graph.feed(merge.inputs["Distance"], graph.math("MINIMUM", graph.math("MULTIPLY", extent, 1e-6), cap))
    return merge.outputs["Geometry"]


def _volume(graph, obj, geometry, *, relief, connected=False):
    centers = []
    for index, layer in enumerate(obj.data.uv_layers):
        centers.append(("VECTOR", "Side UV " + str(index), graph.attribute(layer.name, "FLOAT_VECTOR")))
    geometry, uvs = _capture(graph, geometry, "FACE", centers)
    normal = graph.node("GeometryNodeInputNormal", "Pixel Normal").outputs[0]
    if relief:
        outer, inner = graph.value("glint_geometry_outer", "max(value,0)"), graph.value("glint_geometry_inner", "max(value,0)")
        thickness = graph.math("ADD", outer, inner)
        random = graph.node("FunctionNodeRandomValue", "Pixel Heights")
        random.data_type = "FLOAT"
        random.inputs["Min"].default_value, random.inputs["Max"].default_value = -1, 1
        graph.feed(random.inputs["Seed"], graph.value("glint_geometry_seed"))
        graph.feed(random.inputs["ID"], graph.node("GeometryNodeInputIndex", "Pixel ID").outputs[0])
        factor = graph.math("MULTIPLY", graph.value("glint_geometry_random_enabled"), graph.value("glint_geometry_random_factor"))
        factor = graph.math("MULTIPLY", factor, graph.value("glint_geometry_height_ratio"))
        amount = graph.math("ADD", 1, graph.math("MULTIPLY", random.outputs["Value"], factor))
        bounds = graph.node("GeometryNodeBoundBox", "Source Bounds")
        graph.feed(bounds.inputs["Geometry"], graph.entry.outputs["Geometry"])
        center = graph.vector("SCALE", graph.vector("ADD", bounds.outputs["Min"], bounds.outputs["Max"]), .5)
        radius = graph.vector("LENGTH", graph.vector("SUBTRACT", bounds.outputs["Max"], bounds.outputs["Min"]))
        distance = graph.vector("DISTANCE", graph.node("GeometryNodeInputPosition", "Pixel Position").outputs[0], center)
        divisor = graph.math("MULTIPLY", graph.math("MAXIMUM", radius, .000001), graph.math("MAXIMUM", graph.value("glint_geometry_center_range"), .0001))
        falloff = graph.math("MINIMUM", 1, graph.math("DIVIDE", graph.math("MULTIPLY", distance, 2), divisor))
        amount = graph.math("MULTIPLY", amount, graph.math("SUBTRACT", 1, graph.math("MULTIPLY", falloff, graph.value("glint_geometry_center_falloff"))))
        thickness = graph.math("MAXIMUM", .00001, graph.math("MULTIPLY", thickness, graph.math("MAXIMUM", .05, amount)))
        enabled = graph.math("MULTIPLY", graph.value("glint_geometry_enabled"), graph.value("glint_enabled"))
        thickness = graph.math("ADD", .0005, graph.math("MULTIPLY", enabled, graph.math("SUBTRACT", thickness, .0005)))
        start = graph.math("MULTIPLY", -1, graph.math("MULTIPLY", outer, enabled))
    else:
        thickness, start = .0005, 0
    if connected and relief:
        # Keep the real pixel heights as face values before the regional
        # extrusion evaluates them on shared points. This branch is only the
        # outline input; the visible individually extruded pixels stay intact.
        geometry, captured = _capture(graph, geometry, "FACE", [("FLOAT", "Outline Proxy Pixel Height", thickness)])
        thickness = captured[0]
    moved = graph.node("GeometryNodeSetPosition", "Outer Pixel Base")
    graph.feed(moved.inputs["Geometry"], geometry)
    graph.feed(moved.inputs["Offset"], graph.vector("SCALE", normal, start))
    flip = graph.node("GeometryNodeFlipFaces", "Close Pixel Base")
    graph.feed(flip.inputs["Mesh"], moved.outputs["Geometry"])
    extrude = graph.node("GeometryNodeExtrudeMesh", "Pixel Volume")
    extrude.mode = "FACES"
    extrude.inputs["Individual"].default_value = relief and not connected
    graph.feed(extrude.inputs["Mesh"], moved.outputs["Geometry"])
    graph.feed(extrude.inputs["Offset Scale"], thickness)
    volume = extrude.outputs["Mesh"]
    for layer, value in zip(obj.data.uv_layers, uvs, strict=True):
        volume = graph.store(volume, layer.name, "FLOAT2", "CORNER", value, extrude.outputs["Side"])
    result = graph.node("GeometryNodeJoinGeometry", "Closed Pixels")
    graph.feed(result.inputs["Geometry"], flip.outputs["Mesh"])
    graph.feed(result.inputs["Geometry"], volume)
    return result.outputs[0]


def prepare(transaction, root, obj, owner, *, relief, outline_proxy=False):
    outline._prove(transaction, root, obj, owner)
    if outline_proxy and not relief:
        raise ValueError("描边连续表面仅用于已准备的像素3D凹凸")
    if obj.data.attributes.get(OUTLINE_PROXY_ATTRIBUTE) is not None:
        raise ValueError("网格已有同名用户描边表面属性，未覆盖")
    contour = needs_contour(obj, transaction=transaction)
    _prepare(transaction, root, obj)
    graph = outline.Graph(transaction, owner, "geometry")
    geometry, source_material = _grid(graph, obj)
    geometry = _cutout(graph, obj, geometry, source_material)
    if obj.data.attributes.get(MATERIAL_ATTRIBUTE) is not None:
        raise ValueError("网格已有同名用户原材质索引属性，未覆盖")
    geometry = graph.store(geometry, MATERIAL_ATTRIBUTE, "INT", "FACE", source_material)
    if not relief:
        geometry = _connect_contour(graph, obj, geometry)
    cell_index = graph.node("GeometryNodeInputIndex", "Native Pixel Cell Index").outputs[0]
    geometry = graph.store(geometry, outline.CELL_ATTRIBUTE, "INT", "FACE", cell_index)
    proxy_source = geometry
    geometry = _volume(graph, obj, geometry, relief=relief)
    if not relief:
        geometry = _connect_contour(graph, obj, geometry)
    if relief and not contour:
        switch = graph.node("GeometryNodeSwitch", "Pixel Geometry Enabled")
        switch.input_type = "GEOMETRY"
        graph.feed(switch.inputs["Switch"], graph.math("MULTIPLY", graph.value("glint_geometry_enabled"), graph.value("glint_enabled")))
        graph.feed(switch.inputs["False"], graph.entry.outputs["Geometry"])
        graph.feed(switch.inputs["True"], geometry)
        geometry = switch.outputs[0]
    if outline_proxy:
        # Shared regional faces feed the actual reference Solidify instead of
        # its outlining each independent relief cube. Both branches remain
        # Mesh components, so later native armature/lattice deformation acts
        # on them together. The paired post-Solidify adapter always deletes
        # this input surface and the unused relief shells, including when the
        # native Solidify is disabled by its animated visibility channel.
        proxy = _connect_contour(graph, obj, proxy_source)
        proxy = _volume(graph, obj, proxy, relief=True, connected=True)
        proxy = _connect_contour(graph, obj, proxy)
        proxy = graph.store(proxy, OUTLINE_PROXY_ATTRIBUTE, "BOOLEAN", "FACE", True)
        geometry = graph.store(geometry, OUTLINE_PROXY_ATTRIBUTE, "BOOLEAN", "FACE", False)
        joined = graph.node("GeometryNodeJoinGeometry", "Relief And Connected Outline Input")
        graph.feed(joined.inputs["Geometry"], geometry)
        graph.feed(joined.inputs["Geometry"], proxy)
        geometry = joined.outputs["Geometry"]
        graph.tree[OUTLINE_PROXY_MARK] = 1
    tree = graph.finish(geometry, outline.GEOMETRY_MARK)
    outline.publish(transaction, obj, owner, "geometry", tree, first=True, relief=relief)


def remove_data(transaction, root, obj):
    record = _saved(obj)
    if not record:
        return
    validate_data(root, obj, transaction=transaction)
    if obj.data != record["mesh"] and (obj.data not in transaction.ids or _mesh_signature(obj.data, record["attributes"]) != record["hash"]):
        raise ValueError("像素输入被用户替换，未删除其属性")
    old = obj.data
    in_place = old.shape_keys is not None
    if in_place and old != record["mesh"] and old.shape_keys != record["mesh"].shape_keys:
        raise ValueError("原生形态键被私有复制，未删除实际绑定")
    fresh = old if in_place else transaction.track(old.copy())
    saved = _attribute_values(old, record["attributes"]) if in_place else []
    before = dict(record)
    def restore():
        if in_place:_restore_attributes(old, record["attributes"], saved)
        obj.data = old
        obj[DATA] = before
    transaction.on_rollback(restore)
    for name in record["attributes"]:
        fresh.attributes.remove(fresh.attributes[name])
    obj.data = fresh
    del obj[DATA]
