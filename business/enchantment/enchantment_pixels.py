"""Fixed source-texel cells with native, smoothly changing light intensity.

asset_id: oxal.enchantment.pixels; version: 1; policy: NEW_INSTANCE;
scope: one caller-owned private enchantment material and its private groups.
Only explicit tools build or refresh this graph. No image/frame callbacks.
"""

import json

from ...core.runtime import ensure_mutation_allowed

REVISION = 1
MARKER = "oxal_mc_studio_glint_pixel_revision"
ROWS = "oxal_mc_studio_glint_pixel_samplers"
ARMOR_PREPARATION = "oxal_mc_studio_glint_armor_pixels_prepared"
FALLBACK = "oxal_mc_studio_glint_pixel_fallback_schema"
FALLBACK_ROWS = "oxal_mc_studio_glint_pixel_fallback_rows"
_CHANNELS = ("glint_pixel_enabled", "glint_pixel_size", "glint_pixel_speed", "glint_pixel_brightness_multiplier")


def armor_preparation(material):
    """None is the released legacy schema; 0/1 seals explicit preparation."""
    if ARMOR_PREPARATION not in material:
        return None
    value = material[ARMOR_PREPARATION]
    if type(value) is not int or value not in {0, 1} or bool(value) != bool(_revision(material)):
        raise ValueError("盔甲像素流光准备登记与实际图不一致，未覆盖")
    return value


def requested(material, owner):
    """Keep explicit preparation and every native pixel channel's animation.

    Callers inspect the actual source material before using this predicate.
    No graph mutation, evaluation, cross-operation cache or default reset.
    """
    if material is not None and armor_preparation(material) == 1 or bool(owner.get("glint_pixel_enabled")):
        return True
    from .enchantment import _curves

    paths = {'["' + key + '"]' for key in _CHANNELS}
    animation = owner.animation_data
    return any(curve.data_path in paths for curve in (*_curves(owner), *(animation.drivers if animation else ())))


def _revision(material):
    revision = material.get(MARKER, 0)
    if type(revision) is not int or revision not in {0, REVISION}:
        raise ValueError("未知像素附魔节点版本，未覆盖")
    return revision


def _tree(material, path):
    tree = material.node_tree
    for name in path:
        node = tree.nodes.get(name)
        if node is None or node.type != "GROUP" or node.node_tree is None:
            raise ValueError("像素附魔原贴图节点组路径已变化")
        tree = node.node_tree
    return tree


def _rows(material):
    try:
        rows = json.loads(material.get(ROWS, "null"))
    except (TypeError, ValueError) as error:
        raise ValueError("像素附魔贴图记录损坏") from error
    if (not isinstance(rows, list) or not rows and FALLBACK not in material or len(rows) > 256
            or any(not isinstance(row, dict) or set(row) != {"path", "sampler", "grid", "uv"}
                   or not isinstance(row["path"], list) or len(row["path"]) > 32
                   or any(not isinstance(name, str) for name in row["path"])
                   or any(not isinstance(row[key], str) for key in ("sampler", "grid", "uv")) for row in rows)):
        raise ValueError("像素附魔贴图记录损坏")
    return rows


def fallback_metadata(material):
    """Separate generic-grid schema; legacy image rows keep identical meaning."""
    if FALLBACK not in material and FALLBACK_ROWS not in material:
        return None
    if type(material.get(FALLBACK)) is not int or material[FALLBACK] != 1:
        raise ValueError("未知通用像素流光网格版本，未覆盖")
    raw = material.get(FALLBACK_ROWS)
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("通用像素流光网格记录损坏") from error
    if (not isinstance(rows, list) or not rows or len(rows) > 256
            or any(not isinstance(row, dict) or set(row) != {"path", "grid", "uv", "map"}
                   or not isinstance(row["path"], list) or len(row["path"]) > 32
                   or any(not isinstance(name, str) for name in row["path"])
                   or any(not isinstance(row[key], str) for key in ("grid", "uv", "map")) for row in rows)):
        raise ValueError("通用像素流光网格记录损坏")
    return [1, raw]


def _fallback_rows(material):
    metadata = fallback_metadata(material)
    return json.loads(metadata[1]) if metadata is not None else ()


def _validate_fallback(material):
    from .enchantment import PREFIX

    for row in _fallback_rows(material):
        tree = _tree(material, row["path"])
        grid, uv = tree.nodes.get(row["grid"]), tree.nodes.get(row["uv"])
        if (grid is None or grid.type != "VECT_MATH" or grid.operation != "MULTIPLY"
                or not grid.name.startswith(PREFIX + "Pixel Fallback Grid ")
                or tuple(grid.inputs[1].default_value) != (16.0, 16.0, 16.0)
                or uv is None or not uv.name.startswith(PREFIX + "Pixel Fallback UV ")
                or (uv.type != "UVMAP" or uv.uv_map != row["map"] if row["map"] else uv.type != "TEX_COORD")):
            raise ValueError("通用像素流光坐标或网格节点已变化")
        output = uv.outputs[0] if row["map"] else uv.outputs["Generated"]
        if not grid.inputs[0].is_linked or grid.inputs[0].links[0].from_socket != output:
            raise ValueError("通用像素流光坐标连接已变化")


def prepared(material):
    """Read only: require actual compiled nodes, not just owner control keys."""
    if _revision(material) == 0:
        return False
    from .enchantment import PREFIX
    tree = material.node_tree
    switch = tree.nodes.get(PREFIX + "Pixel Mode") if tree else None
    if switch is None or switch.type != "VALUE":
        raise ValueError("像素附魔模式节点缺失")
    for row in _rows(material):
        inner = _tree(material, row["path"])
        sampler, grid, uv = (inner.nodes.get(row[key]) for key in ("sampler", "grid", "uv"))
        if (sampler is None or sampler.type != "TEX_IMAGE" or sampler.image is None
                or grid is None or grid.type != "VECT_MATH" or grid.operation != "MULTIPLY"
                or not grid.name.startswith(PREFIX + "Pixel Grid ")
                or uv is None or uv.type != "TEX_COORD" or not uv.name.startswith(PREFIX + "Pixel UV ")):
            raise ValueError("像素附魔原贴图或网格节点已变化")
    _validate_fallback(material)
    return True


def display_prepared(material):
    """Read a compiled mode's presentation marker without visiting samplers.

    Tools still use ``prepared`` and the material contract before editing. A
    redraw only needs to decide which existing native controls to present.
    """
    from .enchantment import PREFIX

    tree = material.node_tree
    mode = tree.nodes.get(PREFIX + "Pixel Mode") if tree else None
    return material.get(MARKER) == REVISION and mode is not None and mode.type == "VALUE"


def _sampler_uv(material, tree, sampler, fallback):
    image = sampler.image
    if image is None or len(image.size) != 2 or min(image.size) <= 0:
        raise ValueError("像素附魔原贴图无法读取尺寸")
    width, height = map(int, image.size)
    metadata = material if tree == material.node_tree and sampler.name == "OXAL Source Texture" else sampler
    raw = metadata.get("oxal_mc_studio_texture_animation", "null")
    try:
        animation = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("像素附魔原贴图动画元数据损坏") from error
    vector = sampler.inputs["Vector"].links[0].from_socket if sampler.inputs["Vector"].is_linked else fallback.outputs["UV"]
    if animation is not None:
        if (not isinstance(animation, list) or len(animation) != 4
                or any(type(number) is not int or number <= 0 for number in animation[:2])
                or width % animation[0] or height % animation[1]):
            raise ValueError("像素附魔原贴图动画网格无效")
        columns, rows = animation[:2]
        mapping = vector.node
        if (mapping.type != "VECT_MATH" or mapping.operation != "MULTIPLY_ADD"
                or not mapping.inputs[0].is_linked
                or any(abs(a - b) > 1e-7 for a, b in zip(mapping.inputs[1].default_value, (1 / columns, 1 / rows, 1)))):
            raise ValueError("像素附魔原贴图动画坐标链已变化")
        vector = mapping.inputs[0].links[0].from_socket
        width, height = width // columns, height // rows
    return vector, (width, height, 0.0)


def refresh(material):
    """Refresh compiled source dimensions/UV only; caller inspects and stamps.

    Must run on a private material inside the caller's publication transaction.
    Legacy graphs return False and are never marked prepared by this function.
    """
    ensure_mutation_allowed()
    if not prepared(material):
        return False
    updates = []
    for row in _rows(material):
        tree = _tree(material, row["path"])
        sampler, grid, uv = (tree.nodes[row[key]] for key in ("sampler", "grid", "uv"))
        vector, dimensions = _sampler_uv(material, tree, sampler, uv)
        updates.append((tree, grid, vector, dimensions))
    changed = False
    # Validate every row before the first mutation.
    for tree, grid, vector, dimensions in updates:
        if tuple(grid.inputs[1].default_value) != dimensions:
            grid.inputs[1].default_value = dimensions
            changed = True
        if not grid.inputs[0].is_linked or grid.inputs[0].links[0].from_socket != vector:
            tree.links.new(vector, grid.inputs[0])
            changed = True
    return changed


class _Builder:
    def __init__(self, transaction, material, owner, scene, copies, *, generic=False, uv_name=None):
        self.transaction, self.material, self.owner, self.scene, self.copies = transaction, material, owner, scene, copies
        self.rows, self.cache, self.pending = [], {}, set()
        self.generic, self.uv_name = generic, uv_name
        self.fallback_rows, self.fallback_grids = [], {}
        self.uv_nodes = {}
        self.serial = 0

    def graph(self, tree):
        from .enchantment import _Graph
        return _Graph(tree)

    def blend(self, graph, left, right, factor, role):
        if left is None:
            return right
        if right is None or left == right:
            return left
        # One native vector interpolation is the same unbounded affine mix as
        # a*(1-factor)+b*factor. Keep color/UV selection and animation intact
        # without constructing four separate arithmetic nodes per branch.
        node = graph.node("ShaderNodeMix", role)
        node.data_type, node.factor_mode, node.clamp_factor = "VECTOR", "UNIFORM", False
        graph.feed(node.inputs[0], factor)
        graph.feed(node.inputs[4], left)
        graph.feed(node.inputs[5], right)
        return node.outputs[1]

    def input(self, socket):
        from .enchantment import _input
        return _input(socket, self.graph(socket.id_data))

    def fallback_position(self, tree, path):
        """Reuse one rest-space grid per native tree; never bake user geometry."""
        if tree not in self.fallback_grids:
            graph = self.graph(tree)
            self.serial += 1
            role = str(self.serial)
            uv = graph.node("ShaderNodeUVMap" if self.uv_name else "ShaderNodeTexCoord", "Pixel Fallback UV " + role)
            if self.uv_name:
                uv.uv_map = self.uv_name
            grid = graph.node("ShaderNodeVectorMath", "Pixel Fallback Grid " + role)
            grid.operation = "MULTIPLY"
            grid.inputs[1].default_value = (16.0, 16.0, 16.0)
            tree.links.new(uv.outputs[0] if self.uv_name else uv.outputs["Generated"], grid.inputs[0])
            self.fallback_grids[tree] = grid.outputs[0]
            self.fallback_rows.append({"path": path, "grid": grid.name, "uv": uv.name, "map": self.uv_name or ""})
        return self.fallback_grids[tree]

    def group(self, node):
        from .enchantment import _GROUPS, _track
        current = node.node_tree
        groups = self.material.get(_GROUPS)
        if groups and any(entry["private"] == current for entry in groups.values()):
            return current
        private = self.copies.get(current)
        if private is None:
            private = _track(self.transaction, current.copy())
            self.copies[current] = private
            records = dict(groups.items()) if groups else {}
            records[str(len(records))] = {"original": current, "private": private}
            self.material[_GROUPS] = records
            if private.animation_data:
                for curve in private.animation_data.drivers:
                    for variable in curve.driver.variables:
                        for target in variable.targets:
                            if target.id == current:
                                target.id = private
        node.node_tree = private
        return private

    def group_output(self, socket, path, *, shader):
        from .enchantment import PREFIX
        node = socket.node
        index = list(node.outputs).index(socket)
        private = self.group(node)
        outputs = [item for item in private.nodes if item.type == "GROUP_OUTPUT" and item.is_active_output]
        if len(outputs) != 1:
            raise ValueError("像素附魔底材节点组没有唯一输出")
        original = outputs[0].inputs[index]
        value = self.shader(original.links[0].from_socket, [*path, node.name]) if shader and original.is_linked else self.position(original, [*path, node.name])
        if value is None:
            return None
        self.serial += 1
        role = PREFIX + "Pixel " + ("Color " if shader else "Coordinates ") + str(self.serial)
        private.interface.new_socket(name=role, in_out="OUTPUT", socket_type="NodeSocketColor" if shader else "NodeSocketVector")
        private.links.new(value, outputs[0].inputs[role])
        return node.outputs[role]

    def position(self, socket, path):
        if not socket.is_output:
            if not socket.is_linked:
                return None
            socket = socket.links[0].from_socket
        # Color and Alpha of the same image have exactly the same texel grid.
        # Share only that node's coordinates, retaining distinct image samplers
        # so a later explicit texture-pack refresh can change their dimensions.
        source_key = socket.node.as_pointer() if socket.node.type == "TEX_IMAGE" else socket.as_pointer()
        key = (socket.id_data.as_pointer(), source_key, tuple(path))
        if key in self.cache:
            return self.cache[key]
        if key in self.pending or len(self.pending) > 128:
            raise ValueError("像素附魔底材颜色链循环或过深")
        self.pending.add(key)
        node, tree = socket.node, socket.id_data
        graph = self.graph(tree)
        result = None
        if node.type == "TEX_IMAGE":
            if (node.projection != "FLAT"
                    or self.generic and not self.uv_name and not node.inputs["Vector"].is_linked):
                self.pending.remove(key)
                self.cache[key] = None
                return None
            self.serial += 1
            # Every fallback uses this tree's identical native UV output.
            # Keep per-sampler grids/dimensions independent for texture-pack
            # refresh, while sharing the invariant coordinate source.
            uv = self.uv_nodes.get(tree)
            if uv is None:
                uv = graph.node("ShaderNodeTexCoord", "Pixel UV " + str(self.serial))
                self.uv_nodes[tree] = uv
            vector, dimensions = _sampler_uv(self.material, tree, node, uv)
            grid = graph.node("ShaderNodeVectorMath", "Pixel Grid " + str(self.serial))
            grid.operation = "MULTIPLY"
            grid.inputs[1].default_value = dimensions
            tree.links.new(vector, grid.inputs[0])
            self.rows.append({"path": path, "sampler": node.name, "grid": grid.name, "uv": uv.name})
            result = grid.outputs[0]
        elif node.type == "GROUP":
            if (node.get("oxal_mc_studio_pbr_adapter") == 1
                    and socket.name in {"Base Color", "Alpha"} and socket.name in node.inputs):
                # Roughness/normal/AO atlases describe surface response, not
                # the color texel grid. Follow the adapter's public color port.
                result = self.position(node.inputs[socket.name], path)
            else:
                result = self.group_output(socket, path, shader=False)
        elif node.type == "GROUP_INPUT":
            if path:
                parent_tree = _tree(self.material, path[:-1])
                parent = parent_tree.nodes[path[-1]]
                external = next((item for item in parent.inputs if item.identifier == socket.identifier), None)
                if external is None:
                    raise ValueError("像素附魔底材节点组输入接口不匹配")
                position = self.position(external, path[:-1])
                if position is not None:
                    from .enchantment import PREFIX
                    self.serial += 1
                    name = PREFIX + "Pixel Input " + str(self.serial)
                    tree.interface.new_socket(name=name, in_out="INPUT", socket_type="NodeSocketVector")
                    parent_tree.links.new(position, parent.inputs[name])
                    result = node.outputs[name]
        elif node.type in {"MIX_RGB", "MIX"}:
            if node.type == "MIX_RGB":
                left, right, factor = node.inputs[1], node.inputs[2], node.inputs[0]
            elif node.data_type == "RGBA":
                left, right, factor = node.inputs[6], node.inputs[7], node.inputs[0]
            elif node.data_type == "FLOAT":
                left, right, factor = node.inputs[2], node.inputs[3], node.inputs[0]
            else:
                if self.generic:
                    self.pending.remove(key)
                    self.cache[key] = None
                    return None
                raise ValueError("像素附魔不支持此颜色混合坐标类型")
            a, b = self.position(left, path), self.position(right, path)
            self.serial += 1
            result = self.blend(graph, a, b, self.input(factor), "Pixel Selection " + str(self.serial))
        else:
            children = [value for item in node.inputs if item.is_linked and item.type in {"RGBA", "VECTOR", "VALUE", "INT"}
                        and (value := self.position(item, path)) is not None]
            if children:
                result = children[0]
                if any(child != result for child in children[1:]):
                    if self.generic:
                        result = None
                    else:
                        raise ValueError("像素附魔颜色包含不明确的多贴图坐标混合，未猜测原纹理")
        self.pending.remove(key)
        self.cache[key] = result
        return result

    def shader(self, socket, path):
        from . import enchantment_time as native_time
        from .enchantment import _driver
        node, tree = socket.node, socket.id_data
        graph = self.graph(tree)
        if node.type in {"BSDF_TRANSPARENT", "HOLDOUT"}:
            return None
        if node.type == "GROUP":
            return self.group_output(socket, path, shader=True)
        if node.type == "REROUTE" and node.inputs[0].is_linked:
            return self.shader(node.inputs[0].links[0].from_socket, path)
        if node.type in {"MIX_SHADER", "ADD_SHADER"}:
            start = 1 if node.type == "MIX_SHADER" else 0
            values = [self.shader(item.links[0].from_socket, path) if item.is_linked else None for item in node.inputs[start:start + 2]]
            self.serial += 1
            role = "Pixel Surface " + str(self.serial)
            if not start:
                if None in values:
                    return values[1] if values[0] is None else values[0]
                return graph.vector(role, "ADD", values[0], values[1])
            if values == [None, None]:
                return None
            if None in values:
                black = graph.node("ShaderNodeRGB", role + " Empty")
                black.outputs[0].default_value = (0, 0, 0, 1)
                values = [black.outputs[0] if value is None else value for value in values]
            return self.blend(graph, values[0], values[1], self.input(node.inputs[0]), role)
        color = node.inputs.get("Base Color") if node.type == "BSDF_PRINCIPLED" else node.inputs.get("Color")
        if color is None and not self.generic:
            return None
        position = self.position(color, path) if color is not None else None
        if position is None and node.type == "BSDF_PRINCIPLED":
            # Armor trims use an independently selected solid palette color;
            # the same surface's texture-backed alpha defines their texels.
            position = self.position(node.inputs["Alpha"], path)
        generic_fallback = position is None and self.generic
        if position is None:
            if not generic_fallback:
                return None
            position = self.fallback_position(tree, path)
        self.serial += 1
        role = "Pixel Cells " + str(self.serial)
        size = graph.node("ShaderNodeValue", role + " Size")
        _driver(size.outputs[0], self.owner, "glint_pixel_size", expression="min(max(floor(value+.5),1),16)")
        divided = graph.vector(role + " Divide", "SCALE", position, graph.math(role + " Reciprocal", "DIVIDE", 1, size.outputs[0]))
        fixed = graph.vector(role + " Floor", "FLOOR", divided)
        center = graph.vector(role + " Center", "ADD", fixed, (.5, .5, 0.0))
        noise = graph.node("ShaderNodeTexNoise", role + " Noise")
        noise.noise_dimensions = "4D"
        noise.inputs["Scale"].default_value = .73
        noise.inputs["Detail"].default_value = 0
        tree.links.new(center, noise.inputs["Vector"])
        speed = graph.node("ShaderNodeValue", role + " Speed")
        _driver(speed.outputs[0], self.owner, "glint_pixel_speed", expression="min(max(value,0),10)*.65")
        seconds = native_time.output(graph, "seconds", role + " Native Seconds")
        tree.links.new(graph.math(role + " Time", "MULTIPLY", seconds, speed.outputs[0]), noise.inputs["W"])
        strength = graph.math(role + " Contrast", "MULTIPLY", graph.math(role + " Low", "SUBTRACT", noise.outputs["Fac"], .25), 2)
        bounded = graph.math(role + " Intensity", "MINIMUM", 1, graph.math(role + " Positive", "MAXIMUM", 0, strength))
        # A pure-black or procedural base still receives the selected glint
        # color. This does not replace or resample its original surface.
        source = (1.0, 1.0, 1.0, 1.0) if generic_fallback else self.input(color)
        if isinstance(source, tuple) or hasattr(source, "__len__") and not hasattr(source, "node"):
            constant = graph.node("ShaderNodeRGB", role + " Base")
            constant.outputs[0].default_value = source
            source = constant.outputs[0]
        return graph.vector(role + " Color", "SCALE", source, bounded)


def build(transaction, material, owner, source, scene, copies, *, generic=False, uv_name=None):
    """Compile only inside fresh/private materials after legacy inspection."""
    ensure_mutation_allowed()
    builder = _Builder(transaction, material, owner, scene, copies, generic=generic, uv_name=uv_name)
    color = builder.shader(source, [])
    if color is None or not (builder.rows or builder.fallback_rows):
        return None
    material[MARKER] = REVISION
    material[ROWS] = json.dumps(builder.rows, ensure_ascii=False)
    if builder.fallback_rows:
        material[FALLBACK] = 1
        material[FALLBACK_ROWS] = json.dumps(builder.fallback_rows, ensure_ascii=False)
    return color
