"""Source-derived dragon head and four distinct copper-statue meshes.

The pinned vanilla mesh constructors supply cuboids, atlas offsets and poses;
only declarative data is reused. Minecraft's renderer-wide orientation and item
display transforms are normalized once to Blender world-up with a ground foot.
No game renderer or legacy addon behaviour is executed.
"""

from typing import Any

import bpy
from mathutils import Euler, Matrix, Vector

from ...core.runtime import ensure_mutation_allowed
from . import asset_catalog as catalog
from .asset_geometry import mesh_object
from .asset_item_geometry import Cuboid, _surface

CLIENT_SHA1 = "dabe2b9759859995565e7d01c4a96d05043718ee"
MODEL_CLASSES = {
    "dragon_head": "net/minecraft/client/model/object/skull/DragonHeadModel.class",
    "copper_golem_statue": "net/minecraft/client/model/animal/golem/CopperGolemModel.class",
}
POSES = ("standing", "sitting", "running", "star")
_BASIS = Matrix(((1, 0, 0), (0, 0, 1), (0, -1, 0)))


def _cuboids(transaction: Any, name: str, cubes: list[dict], atlas: tuple, material: Any) -> Any:
    vertices, faces, coordinates = [], [], []
    for definition in cubes:
        box = Cuboid(name, tuple(definition["from"]), tuple(definition["size"]), tuple(definition["uv"]), bool(definition.get("mirror", False)))
        points, surfaces, uv = _surface(box, atlas, 0, grow=float(definition.get("grow", 0)))
        start = len(vertices)
        vertices.extend(points)
        faces.extend(tuple(start + index for index in surface) for surface in surfaces)
        coordinates.extend(uv)
    return mesh_object(transaction, name, vertices, faces, coordinates, [material], [0] * len(faces))


def build(transaction: Any, collection: Any, root: Any, kind: str, material_for: Any, spec: dict | None = None) -> dict[str, Any]:
    ensure_mutation_allowed()
    spec = spec or {}
    if kind == "dragon_head":
        resource = "dragon_head"
        texture = "minecraft:entity/enderdragon/dragon"
    elif kind == "copper_golem_statue":
        pose = str(spec.get("pose", "standing"))
        if pose not in POSES:
            raise ValueError("未知铜傀儡雕像姿态：" + pose)
        resource = "copper_golem_" + pose
        texture = str(spec.get("texture", "minecraft:textures/entity/copper_golem/copper_golem.png"))
        texture = texture.replace(":textures/", ":").removesuffix(".png")
    else:
        raise ValueError("未知实体几何：" + kind)
    definition = catalog.read(catalog.ROOT / "entity_item_geometry.json").get(resource)
    if not definition:
        raise ValueError("随包实体几何缺失：" + resource)
    material = material_for(texture)
    parts, bound_points = {}, []
    top = None
    jaw_angle = float(root.get("jaw_open", 0.2))

    def visit(label: str, data: dict, parent: Any, parent_matrix: Matrix) -> None:
        nonlocal top
        part = transaction.track(bpy.data.objects.new(label, None))
        collection.objects.link(part)
        part.parent = parent
        translation = _BASIS @ Vector(data.get("pivot", (0, 0, 0))) / 16
        angles = list(data.get("rotation", (0, 0, 0)))
        if kind == "dragon_head" and label == "jaw":
            angles[0] = jaw_angle
        rotation = _BASIS @ Euler(angles, "XYZ").to_matrix() @ _BASIS.transposed()
        raw_scale = data.get("scale", (1, 1, 1))
        scale = Vector((raw_scale[0], raw_scale[2], raw_scale[1]))
        part.location = translation
        part.rotation_euler = rotation.to_euler()
        part.scale = scale
        part["oxal_mc_studio_model_part"] = label
        part["oxal_mc_studio_model_source"] = MODEL_CLASSES[kind]
        parts[label] = part
        matrix = parent_matrix @ Matrix.LocRotScale(translation, rotation.to_quaternion(), scale)
        if top is None:
            top = part
        if data.get("cubes"):
            mesh = _cuboids(transaction, label + " geometry", data["cubes"], tuple(definition["atlas"]), material)
            collection.objects.link(mesh)
            mesh.parent = part
            bound_points.extend(matrix @ vertex.co for vertex in mesh.data.vertices)
        for child_label, child in data.get("children", {}).items():
            visit(child_label, child, part, matrix)

    visit(resource, definition["root"], root, Matrix.Identity(4))
    if top is None or not bound_points:
        raise ValueError("实体定义不包含可见 cuboid")
    top.location.z -= min(point.z for point in bound_points)
    root["oxal_mc_studio_entity_geometry_basis"] = "WORLD_UP"
    root["oxal_mc_studio_entity_geometry_source"] = resource
    if kind == "dragon_head" and spec.get("_oxal_rig_enabled",True):
        if "jaw_open" not in root:
            root["jaw_open"] = 0.0
        root["dragon_motion"] = False
        root.id_properties_ui("jaw_open").update(min=0, max=1.0, description="龙首张嘴进度；保留原版 0.2 弧度静止姿态")
        from .asset_effects import native
        native(parts["jaw"], "rotation_euler", ".2+.7*jaw+automatic*(sin(frame*4)+1)*.15", root,
               [("jaw", "jaw_open"), ("automatic", "dragon_motion")], 0)
    elif kind != "dragon_head":
        root["oxal_mc_studio_statue_pose"] = str(spec.get("pose", "standing"))
    return parts
