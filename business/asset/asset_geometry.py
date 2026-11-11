"""Owned Blender geometry/material construction from source model data."""

import json
import logging
import math
from array import array
from pathlib import Path
from typing import Literal, cast, overload
from uuid import uuid4

import bpy
from bpy.types import (
    NodeSocketColor,
    NodeSocketFloat,
    ShaderNodeBsdfPrincipled,
    ShaderNodeMixRGB,
    ShaderNodeTexImage,
)
from mathutils import Matrix, Quaternion, Vector

from ...util.ownership import REGISTRY, prune_owned, seal_owned
from ...util.packed_images import ImagePaths
from ...util.resources import live_ids
from . import asset_catalog as catalog

OWNER = "oxal_mc_studio_generated_owner"
TEXTURE_KEY = "oxal_mc_studio_texture_key"
ORIGINAL_PATH = "oxal_mc_studio_original_texture"
ORIGINAL_IMAGE = "oxal_mc_studio_original_image"
ORIGINAL_ANIMATION = "oxal_mc_studio_original_animation"
logger = logging.getLogger(__name__)


class Creation:
    """Track only IDs explicitly created here, never a global scene difference."""

    def __init__(self):
        self.token = uuid4().hex
        self.ids = []
        self.committed = False
        self._commit_prepared = False
        self.owner = None
        self.domains = {}
        self.excluded = set()
        self.registry_before = None
        self.rollback_callbacks = []
        self.surface_materials = {}
        self.image_paths = ImagePaths()

    def __enter__(self):
        return self

    def on_rollback(self, callback):
        """Restore external binding changes before created native IDs are freed."""
        self.rollback_callbacks.append(callback)

    def track(self, item):
        item[OWNER] = self.token
        self.ids.append(item)
        if self.owner is not None:
            self.domains.setdefault(self.owner, []).append(item)
        return item

    def forget(self, item):
        """Remove an explicitly discarded temporary ID from the proof set."""
        self.ids.remove(item)
        for items in self.domains.values():
            if item in items:
                items.remove(item)

    def seal(self, root=None):
        if self.registry_before is not None:
            return
        groups = {root: self.ids} if root is not None else self.domains
        saved = {
            owner: dict(owner[REGISTRY].items()) if REGISTRY in owner else None
            for owner in groups
        }
        try:
            for owner, items in groups.items():
                seal_owned(owner, [item for item in items if item not in self.excluded])
        except Exception:
            for owner, table in saved.items():
                if table is None:
                    if REGISTRY in owner:
                        del owner[REGISTRY]
                else:
                    owner[REGISTRY] = table
            raise
        self.registry_before = saved

    def prepare_commit(self, root=None):
        """Complete layout, image IO and seals without releasing rollback."""
        from ...util.shader_layout import arrange_owned_shaders

        arrange_owned_shaders(live_ids(self.ids), on_rollback=self.on_rollback)
        for image in live_ids(self.ids):
            if isinstance(image, bpy.types.Image):
                self.image_paths.prepare(image)
        self.seal(root)
        self._commit_prepared = True

    def finish_commit(self):
        """Pure Python final flag; no Blender access, validation or cleanup."""
        self.committed = True

    def cleanup(self):
        """Committed-ID cleanup cannot reopen an already retired transaction."""
        for owner in self.registry_before or {}:
            try:
                prune_owned(owner)
            except Exception:
                # Preparation/publication is committed. Cleanup failure must not
                # roll back references to IDs that earlier pruning reclaimed.
                logger.exception("Unused owned asset cleanup was postponed")

    def commit(self, root=None):
        self.prepare_commit(root)
        self.finish_commit()
        self.cleanup()

    def __exit__(self, kind, value, traceback):
        if not self.committed:
            for callback in reversed(self.rollback_callbacks):
                try:
                    callback()
                except Exception:
                    logger.exception("Asset rollback callback failed; continuing owned cleanup")
            for owner, table in (self.registry_before or {}).items():
                if table is None:
                    if REGISTRY in owner:
                        del owner[REGISTRY]
                else:
                    owner[REGISTRY] = table
            # Native removal of a Mesh may already have freed its embedded
            # Key ID. Validate only this transaction's proven ID references.
            try:
                bpy.data.batch_remove(ids=live_ids(self.ids))
            finally:
                self.image_paths.rollback()


def initialize_driver(curve):
    """Discard Blender's version-specific default mapping on a new driver."""
    curve.keyframe_points.clear()
    return curve


def driver(owner, path, expression, root=None, prop="", index=-1):
    curve = owner.driver_add(path) if index < 0 else owner.driver_add(path, index)
    initialize_driver(curve)
    curve.driver.type = "SCRIPTED"
    curve.driver.expression = expression
    if root is not None:
        variable = curve.driver.variables.new()
        variable.name = "v"
        variable.type = "SINGLE_PROP"
        variable.targets[0].id = root
        variable.targets[0].data_path = (
            prop if prop.startswith("[") else '["' + prop + '"]'
        )
    return curve


def _animation(path: Path, image):
    meta = Path(str(path) + ".mcmeta")
    if not meta.is_file():
        return None
    if meta.stat().st_size > 1_048_576:
        raise ValueError("贴图mcmeta超过1MiB读取预算")
    document = json.loads(meta.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise TypeError("贴图mcmeta必须为对象")
    if "animation" not in document:
        return None
    spec = document["animation"]
    if not isinstance(spec, dict):
        raise TypeError("动态贴图animation必须为对象")
    width, height = image.size
    # A single explicit dimension retains the image's other dimension. Only
    # an entirely implicit frame is square (the shorter image dimension).
    explicit = "width" in spec or "height" in spec
    tile_w = spec.get("width", width if explicit else min(width, height))
    tile_h = spec.get("height", height if explicit else min(width, height))
    if (any(isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (tile_w, tile_h)) or width % tile_w or height % tile_h):
        raise ValueError("动态贴图帧尺寸无效")
    columns, rows = width // tile_w, height // tile_h
    frame_time = spec.get("frametime", 1)
    if (
        isinstance(frame_time, bool)
        or not isinstance(frame_time, int)
        or not 1 <= frame_time <= 1_000_000
    ):
        raise ValueError("动态贴图 frametime 必须为正整数")
    frames = spec.get("frames", [])
    if not isinstance(frames, list) or len(frames) > 4096:
        raise ValueError("动态贴图帧序列超过 4096 帧或格式无效")
    if not frames:
        if columns * rows > 4096:
            raise ValueError("动态贴图帧序列超过 4096 帧")
        frames = list(range(columns * rows))
    sequence = []
    for frame in frames:
        if isinstance(frame, int) and not isinstance(frame, bool):
            index, duration = frame, frame_time
        elif isinstance(frame, dict):
            index, duration = frame.get("index"), frame.get("time", frame_time)
        else:
            raise TypeError("动态贴图帧必须为整数或index/time对象")
        if (
            isinstance(index, bool) or not isinstance(index, int)
            or not 0 <= index < columns * rows
            or isinstance(duration, bool) or not isinstance(duration, int)
            or not 1 <= duration <= 1_000_000
        ):
            raise ValueError("动态贴图帧索引或时长无效")
        sequence.append((index, duration))
    interpolate = spec.get("interpolate", False)
    if not isinstance(interpolate, bool):
        raise TypeError("动态贴图interpolate必须为布尔值")
    return columns, rows, sequence, interpolate


def material(
    transaction: Creation,
    path: Path,
    *,
    key="",
    tint=(1, 1, 1, 1),
    emission=0.0,
    scene=None,
    pbr_format="lab_PBR_1.3",
    pbr_maps=None,
):
    if not path.is_file():
        raise FileNotFoundError("缺少贴图：" + str(path))
    image = transaction.track(bpy.data.images.load(str(path), check_existing=False))
    if image.size[0] < 1 or image.size[1] < 1:
        raise ValueError("无法解码贴图：" + path.name)
    image.pack()
    mat = transaction.track(bpy.data.materials.new(path.stem))
    mat.use_nodes = True
    mat.diffuse_color = tint
    mat[TEXTURE_KEY] = key
    mat[ORIGINAL_PATH] = str(path)
    mat[ORIGINAL_IMAGE] = image
    mat[ORIGINAL_ANIMATION] = json.dumps(_animation(path, image))
    mat.surface_render_method = "DITHERED"
    tree = mat.node_tree
    assert tree is not None
    tree.nodes.clear()
    out = tree.nodes.new("ShaderNodeOutputMaterial")
    shader = cast(ShaderNodeBsdfPrincipled, tree.nodes.new("ShaderNodeBsdfPrincipled"))
    assert shader.inputs is not None and shader.outputs is not None
    cast(NodeSocketFloat, shader.inputs["Roughness"]).default_value = .5
    cast(NodeSocketFloat, shader.inputs["Specular IOR Level"]).default_value = .5
    image_node = cast(ShaderNodeTexImage, tree.nodes.new("ShaderNodeTexImage"))
    assert image_node.outputs is not None
    image_node.name = "OXAL Source Texture"
    image_node.image = image
    image_node.interpolation = "Closest"
    image_node.extension = "CLIP"
    multiply = cast(ShaderNodeMixRGB, tree.nodes.new("ShaderNodeMixRGB"))
    assert multiply.inputs is not None and multiply.outputs is not None
    multiply.blend_type = "MULTIPLY"
    cast(NodeSocketFloat, multiply.inputs[0]).default_value = 1
    cast(NodeSocketColor, multiply.inputs[2]).default_value = tint
    multiply.name = "OXAL Tint"
    tree.links.new(image_node.outputs["Color"], multiply.inputs[1])
    tree.links.new(multiply.outputs[0], shader.inputs["Base Color"])
    tree.links.new(image_node.outputs["Alpha"], shader.inputs["Alpha"])
    if emission:
        tree.links.new(multiply.outputs[0], shader.inputs["Emission Color"])
        cast(
            NodeSocketFloat, shader.inputs["Emission Strength"]
        ).default_value = emission
    tree.links.new(shader.outputs[0], out.inputs["Surface"])
    configure_animation(mat, path, scene=scene)
    from .asset_pbr import install
    install(transaction, mat, path, key=key, scene=scene, format=pbr_format, maps=pbr_maps)
    from .asset_surfaces import flame_shadow
    flame_shadow(mat,key)
    return mat


def configure_animation(
    mat, path, *, animation_override=None, restore=False, scene=None, image_node=None, tree=None, prefix="OXAL Animation "
):
    tree = tree or mat.node_tree
    image_node = image_node or tree.nodes["OXAL Source Texture"]
    if image_node.name.startswith(prefix):
        raise ValueError("贴图采样器与动画辅助节点前缀冲突，未清理节点")
    metadata = mat if tree == mat.node_tree and image_node.name == "OXAL Source Texture" else image_node
    source_uv = None
    mapping = tree.nodes.get(prefix+"Current Mapping")
    candidates = mapping.inputs[0].links if mapping is not None else image_node.inputs["Vector"].links
    if candidates and not candidates[0].from_node.name.startswith(prefix):
        source_uv = candidates[0].from_socket
    animation_data = tree.animation_data
    if animation_data:
        for curve in tuple(animation_data.drivers):
            if curve.data_path.startswith('nodes["'+prefix):
                animation_data.drivers.remove(curve)
    image = image_node.image
    for label, output in (("Color", "Color"), ("Alpha", "Alpha")):
        previous = tree.nodes.get(prefix + "Mix " + label)
        if previous is not None:
            for link in tuple(previous.outputs[0].links):
                tree.links.new(image_node.outputs[output], link.to_socket)
    for existing in tuple(tree.nodes):
        if existing.name.startswith(prefix):
            tree.nodes.remove(existing)
    animation = animation_override if restore else _animation(Path(path), image)
    metadata["oxal_mc_studio_texture_animated"] = bool(animation)
    metadata["oxal_mc_studio_texture_animation"] = json.dumps(animation)
    if not animation:
        if source_uv is not None:tree.links.new(source_uv,image_node.inputs["Vector"])
        return
    columns, rows, sequence, interpolate = animation
    total = sum(duration for _, duration in sequence)
    scene = scene or bpy.context.scene

    def new(kind, label):
        node = tree.nodes.new(kind)
        node.name = prefix + label
        return node

    def value(label, number, expression=None):
        node = new("ShaderNodeValue", label)
        node.outputs[0].default_value = number
        if expression is not None:
            expression = expression.replace(
                "(frame-1)", "((frame-1)*20*fps_base/max(fps,1))"
            )
            curve = driver(node.outputs[0], "default_value", expression)
            for name, data_path in (
                ("fps", "render.fps"),
                ("fps_base", "render.fps_base"),
            ):
                variable = curve.driver.variables.new()
                variable.name = name
                variable.type = "SINGLE_PROP"
                variable.targets[0].id_type = "SCENE"
                variable.targets[0].id = scene
                variable.targets[0].data_path = data_path
        return node.outputs[0]

    def math_node(label, operation, a, b):
        node = new("ShaderNodeMath", label)
        node.operation = operation
        for index, operand in enumerate((a, b)):
            if isinstance(operand, (int, float)):
                node.inputs[index].default_value = operand
            else:
                tree.links.new(operand, node.inputs[index])
        return node.outputs[0]

    phase = value("Phase", 0, f"fmod(fmod((frame-1),{total})+{total},{total})")

    def lookup(label, values):
        # Uniform sequences use a compact expression. Arbitrary sequences use
        # native Math lookup nodes, avoiding Blender's 255-character RNA limit.
        durations = [duration for _, duration in sequence]
        delta = values[1] - values[0] if len(values) > 1 else 0
        if len(set(durations)) == 1 and all(
            abs(number - (values[0] + i * delta)) < 1e-9
            for i, number in enumerate(values)
        ):
            return value(
                label,
                values[0],
                f"{values[0]}+{delta}*fmod(fmod(floor((frame-1)/{durations[0]}),{len(values)})+{len(values)},{len(values)})",
            )
        result = value(label + " Base", values[-1])
        elapsed = 0
        for index in range(len(values) - 1):
            elapsed += durations[index]
            difference = values[index] - values[index + 1]
            if not difference:
                continue
            flag = math_node(f"{label} Before {index}", "LESS_THAN", phase, elapsed)
            jump = math_node(f"{label} Jump {index}", "MULTIPLY", flag, difference)
            result = math_node(f"{label} Sum {index}", "ADD", result, jump)
        return result

    uv = new("ShaderNodeTexCoord", "UV")
    uv_output = source_uv if source_uv is not None else uv.outputs["UV"]

    def mapping_for(label, indices):
        index = lookup(label + " Index", indices)
        column = math_node(label + " Column", "MODULO", index, columns)
        x = math_node(label + " U", "DIVIDE", column, columns)
        row_div = math_node(label + " Row Division", "DIVIDE", index, columns)
        row = math_node(label + " Row", "FLOOR", row_div, 0)
        row_next = math_node(label + " Row Next", "ADD", row, 1)
        y_div = math_node(label + " V Division", "DIVIDE", row_next, rows)
        y = math_node(label + " V", "SUBTRACT", 1, y_div)
        offset = new("ShaderNodeCombineXYZ", label + " Offset")
        tree.links.new(x, offset.inputs["X"])
        tree.links.new(y, offset.inputs["Y"])
        mapping = tree.nodes.new("ShaderNodeVectorMath")
        mapping.name = prefix + label + " Mapping"
        mapping.operation = "MULTIPLY_ADD"
        mapping.inputs[1].default_value = (1 / columns, 1 / rows, 1)
        tree.links.new(uv_output, mapping.inputs[0])
        tree.links.new(offset.outputs[0], mapping.inputs[2])
        return mapping.outputs[0]

    indices = [index for index, _ in sequence]
    current_map = mapping_for("Current", indices)
    tree.links.new(current_map, image_node.inputs["Vector"])
    if interpolate:
        color_links = tuple(image_node.outputs["Color"].links)
        alpha_links = tuple(image_node.outputs["Alpha"].links)
        next_image = new("ShaderNodeTexImage", "Next Texture")
        next_image.image = image
        next_image.interpolation = "Closest"
        next_image.extension = "REPEAT"
        next_map = mapping_for("Next", [*indices[1:], indices[0]])
        tree.links.new(next_map, next_image.inputs["Vector"])
        starts, elapsed = [], 0
        for _, duration in sequence:
            starts.append(elapsed)
            elapsed += duration
        start = lookup("Frame Start", starts)
        duration = lookup("Frame Duration", [duration for _, duration in sequence])
        within = math_node("Within Frame", "SUBTRACT", phase, start)
        factor = math_node("Blend Factor", "DIVIDE", within, duration)
        color = new("ShaderNodeMixRGB", "Mix Color")
        tree.links.new(factor, color.inputs[0])
        tree.links.new(image_node.outputs["Color"], color.inputs[1])
        tree.links.new(next_image.outputs["Color"], color.inputs[2])
        alpha = new("ShaderNodeMix", "Mix Alpha")
        alpha.data_type = "FLOAT"
        tree.links.new(factor, alpha.inputs[0])
        tree.links.new(image_node.outputs["Alpha"], alpha.inputs[2])
        tree.links.new(next_image.outputs["Alpha"], alpha.inputs[3])
        for link in color_links:
            tree.links.new(color.outputs[0], link.to_socket)
        for link in alpha_links:
            tree.links.new(alpha.outputs[0], link.to_socket)
    metadata["oxal_mc_studio_texture_interpolate"] = interpolate


def mesh_object(
    transaction: Creation, name, vertices, faces, uvs, materials, material_indices
):
    mesh = transaction.track(bpy.data.meshes.new(name))
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    for mat in materials:
        mesh.materials.append(mat)
    layer = mesh.uv_layers.new(name="UVMap")
    for polygon, coordinates, material_index in zip(
        mesh.polygons, uvs, material_indices
    ):
        polygon.material_index = material_index
        for loop, uv in zip(polygon.loop_indices, coordinates):
            layer.data[loop].uv = uv
    return transaction.track(bpy.data.objects.new(name, mesh))


FACE_VERTICES = {
    "down": (0, 1, 5, 4),
    "up": (7, 6, 2, 3),
    "north": (1, 0, 3, 2),
    "south": (4, 5, 6, 7),
    "west": (0, 4, 7, 3),
    "east": (5, 1, 2, 6),
}


def _locked_uv(corners, side, variant, coordinates):
    """Keep block-face texture orientation fixed under quarter-turn block states."""
    transform = Matrix.Identity(3)
    for axis in ("x", "y", "z"):
        if variant.get(axis):
            transform = (
                Matrix.Rotation(-math.radians(variant[axis]), 3, axis.upper())
                @ transform
            )
    normals = {
        "down": (0, -1, 0),
        "up": (0, 1, 0),
        "north": (0, 0, -1),
        "south": (0, 0, 1),
        "west": (-1, 0, 0),
        "east": (1, 0, 0),
    }
    normal = transform @ Vector(normals[side])
    new_side = max(normals, key=lambda key: normal.dot(Vector(normals[key])))
    center = Vector((8, 8, 8))
    rotated = [center + transform @ (Vector(point) - center) for point in corners]
    low = [min(point[axis] for point in rotated) for axis in range(3)]
    high = [max(point[axis] for point in rotated) for axis in range(3)]
    x1, y1, z1 = low
    x2, y2, z2 = high
    canonical_corners = [
        (x1, y1, z1),
        (x2, y1, z1),
        (x2, y2, z1),
        (x1, y2, z1),
        (x1, y1, z2),
        (x2, y1, z2),
        (x2, y2, z2),
        (x1, y2, z2),
    ]
    canonical = [Vector(canonical_corners[index]) for index in FACE_VERTICES[new_side]]
    return [
        coordinates[
            min(range(4), key=lambda i: (canonical[i] - rotated[index]).length_squared)
        ]
        for index in FACE_VERTICES[side]
    ]


def _uv_default(face, start, end):
    x1, y1, z1 = start
    x2, y2, z2 = end
    return {
        "down": (x1, 16 - z2, x2, 16 - z1),
        "up": (x1, z1, x2, z2),
        "north": (16 - x2, 16 - y2, 16 - x1, 16 - y1),
        "south": (x1, 16 - y2, x2, 16 - y1),
        "west": (z1, 16 - y2, z2, 16 - y1),
        "east": (16 - z2, 16 - y2, 16 - z1, 16 - y1),
    }[face]


def _point(point, rotation=None, variant=None):
    point = Vector(point)
    if rotation:
        origin = Vector(rotation.get("origin", (8, 8, 8)))
        axis = rotation.get("axis", "y").upper()
        angle = math.radians(rotation.get("angle", 0))
        offset = point - origin
        if rotation.get("rescale") and angle:
            factor = 1 / math.cos(angle)
            for index, name in enumerate("XYZ"):
                if name != axis:
                    offset[index] *= factor
        point = origin + Matrix.Rotation(angle, 3, axis) @ offset
    for axis in ("x", "y", "z"):
        if variant and variant.get(axis):
            point = Vector((8, 8, 8)) + Matrix.Rotation(
                -math.radians(variant[axis]), 3, axis.upper()
            ) @ (point - Vector((8, 8, 8)))
    return ((point.x - 8) / 16, -(point.z - 8) / 16, point.y / 16)


def _quaternion(value):
    if value is None:
        return Matrix.Identity(4)
    if isinstance(value, dict):
        axis = Vector(value.get("axis", (0, 1, 0)))
        return (
            Quaternion(axis, math.radians(float(value.get("angle", 0))))
            .to_matrix()
            .to_4x4()
        )
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("模型旋转必须是四元数或轴角")
    x, y, z, w = value
    return Quaternion((w, x, y, z)).normalized().to_matrix().to_4x4()


def source_transform(spec):
    """Minecraft/JOML affine TRS, before conversion from model coordinates."""
    if spec is None:
        return Matrix.Identity(4)
    if isinstance(spec, list):
        if len(spec) == 16:
            return Matrix(
                tuple(
                    tuple(spec[column * 4 + row] for column in range(4))
                    for row in range(4)
                )
            )
        if len(spec) == 4 and all(
            isinstance(row, list) and len(row) == 4 for row in spec
        ):
            return Matrix(spec)
        raise ValueError("模型矩阵必须有 16 个元素")
    if not isinstance(spec, dict):
        raise TypeError("模型变换格式无效")
    translation = Vector(spec.get("translation", (0, 0, 0)))
    scale = spec.get("scale", (1, 1, 1))
    if isinstance(scale, (int, float)):
        scale = (scale, scale, scale)
    scaling = Matrix.Diagonal((*scale, 1))
    return (
        Matrix.Translation(translation)
        @ _quaternion(spec.get("left_rotation"))
        @ scaling
        @ _quaternion(spec.get("right_rotation"))
    )


def descriptor_matrix(descriptor):
    source = Matrix.Identity(4)
    for spec in descriptor.get("_oxal_transform_chain", []):
        source = source @ source_transform(spec)
    source = source @ source_transform(descriptor.get("transformation"))
    basis = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
    center = Matrix.Identity(4) if descriptor.get("_oxal_centered") else Matrix.Translation((0, 0, 0.5))
    result = center @ basis @ source @ basis.inverted() @ center.inverted()
    offset = descriptor.get("_oxal_offset", (0, 0, 0))
    return Matrix.Translation((offset[0], -offset[2], offset[1])) @ result


def model_point(point, rotation=None, variant=None):
    """A source 0..16 point in the same centered space as generated meshes."""
    return descriptor_matrix(variant or {}) @ Vector(_point(point, rotation, variant))


def elements(transaction: Creation, name, data: dict, material_for, variant=None):
    vertices, faces, uvs, indices, materials = [], [], [], [], []
    textures = data.get("textures", {})
    extra = descriptor_matrix(variant or {})
    for element in data.get("elements", []):
        a, b = element["from"], element["to"]
        corners = [
            (a[0], a[1], a[2]),
            (b[0], a[1], a[2]),
            (b[0], b[1], a[2]),
            (a[0], b[1], a[2]),
            (a[0], a[1], b[2]),
            (b[0], a[1], b[2]),
            (b[0], b[1], b[2]),
            (a[0], b[1], b[2]),
        ]
        for side, face in element.get("faces", {}).items():
            if side not in FACE_VERTICES:
                raise ValueError("模型面方向无效")
            key = catalog.texture(face["texture"], textures)
            mat = material_for(key, face.get("tintindex", -1))
            if mat not in materials:
                materials.append(mat)
            offset = len(vertices)
            vertices.extend(
                extra @ Vector(_point(corners[index], element.get("rotation"), variant))
                for index in FACE_VERTICES[side]
            )
            faces.append((offset, offset + 1, offset + 2, offset + 3))
            x1, y1, x2, y2 = face.get("uv", _uv_default(side, a, b))
            coordinates = [
                (x1 / 16, 1 - y2 / 16),
                (x2 / 16, 1 - y2 / 16),
                (x2 / 16, 1 - y1 / 16),
                (x1 / 16, 1 - y1 / 16),
            ]
            turns = int(face.get("rotation", 0)) // 90
            coordinates = coordinates[turns:] + coordinates[:turns]
            if variant and variant.get("uvlock"):
                coordinates = _locked_uv(corners, side, variant, coordinates)
            uvs.append(coordinates)
            indices.append(materials.index(mat))
    if not faces:
        raise ValueError("源模型不包含可见几何：" + name)
    from .asset_surfaces import build
    return build(transaction,name,vertices,faces,uvs,materials,indices,mesh_object)


@overload
def sprite(transaction: Creation, name, mat, depth=0.0625, *, allow_empty: Literal[False] = False) -> bpy.types.Object: ...


@overload
def sprite(transaction: Creation, name, mat, depth=0.0625, *, allow_empty: bool) -> bpy.types.Object | None: ...


def sprite(transaction: Creation, name, mat, depth=0.0625, *, allow_empty=False):
    """Pixel faces and extruded silhouette, welded within four-connected islands.

    The union of animation-frame silhouettes is prepared once. Shader alpha
    retains the current frame; diagonal-only pixels never become nonmanifold.
    """
    image = mat.node_tree.nodes["OXAL Source Texture"].image
    image_width, image_height = image.size
    animation = json.loads(mat.get("oxal_mc_studio_texture_animation", "null"))
    columns, rows = (animation[0], animation[1]) if animation else (1, 1)
    width, height = image_width // columns, image_height // rows
    if width * height > 262144:
        raise ValueError("图片几何超过 262144 像素，请先缩小源图片")
    # Animated atlases can contain many frames. Native bulk reads avoid one
    # Python float object per channel while retaining all referenced frames.
    pixels = array("f", [0.0]) * len(image.pixels)
    image.pixels.foreach_get(pixels)
    filled = set()
    frame_indices = {index for index, _ in animation[2]} if animation else {0}
    for frame_index in frame_indices:
        x_offset = (frame_index % columns) * width
        y_offset = image_height - (frame_index // columns + 1) * height
        for y in range(height):
            for x in range(width):
                if pixels[((y + y_offset) * image_width + x + x_offset) * 4 + 3] > .1:
                    filled.add((x, y))
        if len(filled) == width * height:
            break
    if not filled:
        if allow_empty:
            return None
        raise ValueError("图片没有可见像素：" + name)
    split_corners = set()
    for x in range(1,width):
        for y in range(1,height):
            a,b,c,d = ((x-1,y-1) in filled,(x,y-1) in filled,(x-1,y) in filled,(x,y) in filled)
            if (a and d and not b and not c) or (b and c and not a and not d):
                split_corners.add((x,y))
    islands, remaining = {}, set(filled)
    island = 0
    while remaining:
        stack = [remaining.pop()]
        while stack:
            x, y = stack.pop()
            islands[x, y] = island
            for point in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                if point in remaining:
                    remaining.remove(point)
                    stack.append(point)
        island += 1
    vertices, faces, uvs, lookup = [], [], [], {}
    def face(corners, coords, group, owner):
        indices = []
        for x, side, y in corners:
            key = (group,x,side,y,owner if (x,y) in split_corners else None)
            if key not in lookup:
                lookup[key] = len(vertices)
                vertices.append((x / width - .5, side * depth / 2, (y-height/2) / width))
            indices.append(lookup[key])
        faces.append(tuple(reversed(indices)))
        uvs.append(list(reversed(coords)))
    for x, y in sorted(filled):
        group = islands[x, y]
        u0,u1,v0,v1 = x/width,(x+1)/width,y/height,(y+1)/height
        face([(x,-1,y),(x,-1,y+1),(x+1,-1,y+1),(x+1,-1,y)],
             [(u0,v0),(u0,v1),(u1,v1),(u1,v0)],group,(x,y))
        face([(x+1,1,y),(x+1,1,y+1),(x,1,y+1),(x,1,y)],
             [(u1,v0),(u1,v1),(u0,v1),(u0,v0)],group,(x,y))
        # Sample inside the texel while retaining a genuine two-dimensional UV
        # footprint across the thickness. A point UV loses pack shading detail.
        lo,hi = (x+.1)/width,(x+.9)/width
        bottom,top = (y+.1)/height,(y+.9)/height
        for neighbor,corners,coords in (
            ((x-1,y),[(x,-1,y),(x,1,y),(x,1,y+1),(x,-1,y+1)],[(lo,bottom),(hi,bottom),(hi,top),(lo,top)]),
            ((x+1,y),[(x+1,1,y),(x+1,-1,y),(x+1,-1,y+1),(x+1,1,y+1)],[(hi,bottom),(lo,bottom),(lo,top),(hi,top)]),
            ((x,y-1),[(x,1,y),(x,-1,y),(x+1,-1,y),(x+1,1,y)],[(lo,top),(lo,bottom),(hi,bottom),(hi,top)]),
            ((x,y+1),[(x,-1,y+1),(x,1,y+1),(x+1,1,y+1),(x+1,-1,y+1)],[(lo,bottom),(lo,top),(hi,top),(hi,bottom)]),
        ):
            if neighbor not in filled:
                face(corners,coords,group,(x,y))
    obj = mesh_object(transaction,name,vertices,faces,uvs,[mat],[0]*len(faces))
    obj["oxal_mc_studio_sprite_depth"] = depth
    obj["oxal_mc_studio_pixel_topology"] = 2
    atlas = catalog.read(catalog.ROOT/"glint_atlas179.json").get("sprites",{})
    key = mat.get(TEXTURE_KEY,"").split(":",1)[-1]
    u,v,du,dv = atlas.get(key,(0,0,16/2048,16/2048))
    mesh = cast(bpy.types.Mesh, obj.data)
    glint = mesh.uv_layers.new(name="OXAL Glint UV")
    for loop in mesh.loops:
        point = mesh.vertices[loop.vertex_index].co
        glint.data[loop.index].uv = (u+(point.x+.5)*du,v+(.5-point.z)*dv)
    mesh.uv_layers.active_index = 0
    mat["oxal_mc_studio_glint_uv_name"] = glint.name
    return obj


def box(transaction, name, start, end, mat, uv=(0, 0, 16, 16)):
    data = {
        "textures": {"all": "minecraft:temporary"},
        "elements": [
            {
                "from": start,
                "to": end,
                "faces": {
                    side: {"texture": "#all", "uv": uv} for side in FACE_VERTICES
                },
            }
        ],
    }
    return elements(transaction, name, data, lambda key, tint: mat)
