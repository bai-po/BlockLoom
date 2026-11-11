"""Shield/trident cuboids and atlas surfaces from pinned vanilla model data.

Source: Java 26.4 Snapshot 5 official client, SHA-1 below; inspected bytecode
constants in ShieldModel.createLayer and TridentModel.createLayer. Atlas face
rectangles follow the model's six declared cuboid surfaces. This is a new
Blender mesh builder; no legacy addon implementation or runtime is imported.
"""

from dataclasses import dataclass
from typing import Any

from ...core.runtime import ensure_mutation_allowed
from .asset_geometry import mesh_object

CLIENT_SHA1 = "dabe2b9759859995565e7d01c4a96d05043718ee"
SOURCE_URL = "https://piston-data.mojang.com/v1/objects/" + CLIENT_SHA1 + "/client.jar"
MODEL_CLASSES = {
    "shield": "net/minecraft/client/model/object/equipment/ShieldModel.class",
    "trident": "net/minecraft/client/model/object/projectile/TridentModel.class",
}


@dataclass(frozen=True, slots=True)
class Cuboid:
    name: str
    origin: tuple[float, float, float]
    size: tuple[float, float, float]
    uv: tuple[int, int]
    mirror: bool = False


SHIELD = (
    Cuboid("plate", (-6, -11, -2), (12, 22, 1), (0, 0)),
    Cuboid("handle", (-1, -3, -1), (2, 6, 6), (26, 0)),
)
TRIDENT = (
    Cuboid("pole", (-0.5, 2, -0.5), (1, 25, 1), (0, 6)),
    Cuboid("base", (-1.5, 0, -0.5), (3, 2, 1), (4, 0)),
    Cuboid("left_spike", (-2.5, -3, -0.5), (1, 4, 1), (4, 3)),
    Cuboid("middle_spike", (-0.5, -4, -0.5), (1, 4, 1), (0, 0)),
    Cuboid("right_spike", (1.5, -3, -0.5), (1, 4, 1), (4, 3), True),
)
_FACES = (
    ("down", (5, 4, 0, 1)),
    ("up", (2, 3, 7, 6)),
    ("west", (0, 4, 7, 3)),
    ("north", (1, 0, 3, 2)),
    ("east", (5, 1, 2, 6)),
    ("south", (4, 5, 6, 7)),
)


def _surface(cube: Cuboid, atlas: tuple[int, int], floor_y: float, *, grow: float = 0) -> tuple[list, list, list]:
    x0, y0, z0 = cube.origin
    dx, dy, dz = cube.size
    x1, y1, z1 = x0 + dx, y0 + dy, z0 + dz
    x0, y0, z0 = x0 - grow, y0 - grow, z0 - grow
    x1, y1, z1 = x1 + grow, y1 + grow, z1 + grow
    if cube.mirror:
        x0, x1 = x1, x0
    corners = (
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    )
    u, v = cube.uv
    # Pixel rectangles of the six faces in Minecraft's unfolded cuboid atlas.
    rectangles = {
        "down": (u + dz, v, u + dz + dx, v + dz),
        "up": (u + dz + dx, v + dz, u + dz + 2 * dx, v),
        "west": (u, v + dz, u + dz, v + dz + dy),
        "north": (u + dz, v + dz, u + dz + dx, v + dz + dy),
        "east": (u + dz + dx, v + dz, u + 2 * dz + dx, v + dz + dy),
        "south": (u + 2 * dz + dx, v + dz, u + 2 * dz + 2 * dx, v + dz + dy),
    }
    width, height = atlas
    vertices, faces, coordinates = [], [], []
    for side, indices in _FACES:
        a, b, c, d = rectangles[side]
        points = [corners[index] for index in indices]
        uv = [(c / width, 1 - b / height), (a / width, 1 - b / height), (a / width, 1 - d / height), (c / width, 1 - d / height)]
        if cube.mirror:
            points.reverse()
            uv.reverse()
        offset = len(vertices)
        # Java model Y points down; stand the item upright with its foot at Z=0.
        vertices.extend((x / 16, z / 16, (floor_y - y) / 16) for x, y, z in points)
        faces.append(tuple(range(offset, offset + 4)))
        coordinates.append(uv)
    return vertices, faces, coordinates


def build(transaction: Any, collection: Any, root: Any, kind: str, material_for: Any) -> dict[str, Any]:
    ensure_mutation_allowed()
    if kind == "shield":
        definitions, atlas, texture = SHIELD, (64, 64), "minecraft:entity/shield/shield_base_nopattern"
    elif kind == "trident":
        definitions, atlas, texture = TRIDENT, (32, 32), "minecraft:entity/trident/trident"
    else:
        raise ValueError("未知的原版手持模型：" + kind)
    material = material_for(texture)
    # Handheld assets retain their original item frame and authored palm pivot;
    # shifting the shaft/handle to the ground destroys that binding contract.
    floor_y = 0 if root.get("oxal_mc_studio_build_handheld") else max(cube.origin[1] + cube.size[1] for cube in definitions)
    parts = {}
    for cube in definitions:
        vertices, faces, uv = _surface(cube, atlas, floor_y)
        obj = mesh_object(transaction, kind + " " + cube.name, vertices, faces, uv, [material], [0] * len(faces))
        collection.objects.link(obj)
        obj.parent = root
        obj["oxal_mc_studio_model_part"] = cube.name
        obj["oxal_mc_studio_model_source"] = MODEL_CLASSES[kind]
        parts[cube.name] = obj
    return parts
