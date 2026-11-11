"""Source cuboid resources and independent, native animated asset controls."""

import json
import math
from typing import Any, cast

import bpy
from mathutils import Euler, Matrix, Vector

from ...core.runtime import ensure_mutation_allowed
from . import asset_catalog as catalog
from . import asset_effects
from . import asset_geometry as geometry
from .asset_geometry import driver, mesh_object

# CommonColors.TEXTURE_TINT_COLORS from the pinned official 26.4 client.
DYE_COLORS = dict(
    zip(
        (
            "white",
            "orange",
            "magenta",
            "light_blue",
            "yellow",
            "lime",
            "pink",
            "gray",
            "light_gray",
            "cyan",
            "purple",
            "blue",
            "brown",
            "green",
            "red",
            "black",
        ),
        (
            0xF9FFFE,
            0xF9801D,
            0xC74EBD,
            0x3AB3DA,
            0xFED83D,
            0x80C71F,
            0xF38BAA,
            0x474F52,
            0x9D9D97,
            0x169C9C,
            0x8932B8,
            0x3C44AA,
            0x835432,
            0x5E7C16,
            0xB02E26,
            0x1D1D21,
        ),
        strict=True,
    )
)


def _entity_mesh(transaction, name, cubes, atlas, mat, upright):
    vertices, faces, coordinates, cuboids = [], [], [], []
    width, height = atlas
    side_indices = {
        "down": (1, 0, 4, 5),
        "up": (3, 2, 6, 7),
        "north": (0, 1, 2, 3),
        "south": (5, 4, 7, 6),
        "west": (4, 0, 3, 7),
        "east": (1, 5, 6, 2),
    }
    for cube in cubes:
        cube_start = len(vertices)
        start = cube["from"]
        size = cube["size"]
        grow = float(cube.get("grow", 0))
        x, y, z = [value - grow for value in start]
        dx, dy, dz = [value + 2 * grow for value in size]
        points = [
            (x, y, z),
            (x + dx, y, z),
            (x + dx, y + dy, z),
            (x, y + dy, z),
            (x, y, z + dz),
            (x + dx, y, z + dz),
            (x + dx, y + dy, z + dz),
            (x, y + dy, z + dz),
        ]
        u, v = cube.get("uv", (0, 0))
        sx, sy, sz = size
        rects = {
            "west": (u, v + sz, u + sz, v + sz + sy),
            "north": (u + sz, v + sz, u + sz + sx, v + sz + sy),
            "east": (u + sz + sx, v + sz, u + 2 * sz + sx, v + sz + sy),
            "south": (u + 2 * sz + sx, v + sz, u + 2 * sz + 2 * sx, v + sz + sy),
            "down": (u + sz, v, u + sz + sx, v + sz),
            "up": (u + sz + sx, v, u + sz + 2 * sx, v + sz),
        }
        for side, indices in side_indices.items():
            if cube.get("faces") is not None and side.upper() not in cube["faces"]:
                continue
            if any(value == 0 for value in size) and side != ("north" if sz == 0 else "west" if sx == 0 else "down"):
                continue
            offset = len(vertices)
            selected = [points[index] for index in indices]
            if cube.get("mirror"):
                selected = [(2*x+dx-point[0],point[1],point[2]) for point in reversed(selected)]
            vertices.extend(
                (
                    point[0] / 16,
                    -upright * point[2] / 16,
                    upright * point[1] / 16,
                )
                for point in selected
            )
            faces.append(tuple(range(offset, offset + 4)))
            a, b, c, d = rects[side]
            uv = [
                (c / width, 1 - b / height),
                (a / width, 1 - b / height),
                (a / width, 1 - d / height),
                (c / width, 1 - d / height),
            ]
            if cube.get("mirror"):
                uv.reverse()
            coordinates.append(uv)
        cuboids.append((cube_start,len(vertices)))
    obj = mesh_object(
        transaction, name, vertices, faces, coordinates, [mat], [0] * len(faces)
    )
    cast(bpy.types.Mesh,obj.data)["oxal_mc_studio_cuboids"] = json.dumps(cuboids)
    return obj


def entity(transaction, collection, root, name, mat, *, upright=-1):
    ensure_mutation_allowed()
    definition = catalog.read(catalog.ROOT / "entity_geometry.json").get(name)
    if not definition:
        raise ValueError("缺少实体几何资源：" + name)
    parts = {}
    conversion = Matrix(((1, 0, 0), (0, 0, -upright), (0, upright, 0)))

    def visit(label, data, parent):
        pivot = Vector(data.get("pivot", (0, 0, 0)))
        part = transaction.track(bpy.data.objects.new(label, None))
        part["oxal_mc_studio_model_part"] = (
            name + "_" + ("root" if parent == root else label)
            if name in {"banner", "wall_banner", "flag"} else label
        )
        collection.objects.link(part)
        part.parent = parent
        part.location = conversion @ pivot / 16
        rotation = Euler(data.get("rotation", (0, 0, 0)), "XYZ").to_matrix()
        part.rotation_euler = (conversion @ rotation @ conversion.inverted()).to_euler()
        scale = data.get("scale", (1, 1, 1))
        part.scale = tuple(scale) if len(scale) == 3 else (scale[0],) * 3
        parts[label] = part
        if data.get("cubes"):
            obj = _entity_mesh(
                transaction,
                label + " geometry",
                data["cubes"],
                definition["atlas"],
                mat,
                upright,
            )
            collection.objects.link(obj)
            obj.parent = part
        for child_label, child in data.get("children", {}).items():
            visit(child_label, child, part)

    visit(name, definition["root"], root)
    return parts


def build(transaction, collection, root, identifier, descriptor, material_for):
    ensure_mutation_allowed()
    animated = descriptor.get("_oxal_rig_enabled",True)
    spec = (
        descriptor.get("model", {}) if isinstance(descriptor.get("model"), dict) else {}
    )
    kind = spec.get("type", "").removeprefix("minecraft:")
    short = identifier.split(":")[-1]
    if not kind:
        if "chest" in short and "chestplate" not in short:
            kind = "chest"
            spec = {
                "texture": "minecraft:"
                + (
                    "ender"
                    if short == "ender_chest"
                    else "trapped"
                    if short == "trapped_chest"
                    else "normal"
                )
            }
        elif "shulker_box" in short:
            kind = "shulker_box"
            color = short.removesuffix("_shulker_box")
            spec = {
                "texture": "minecraft:shulker"
                + ("_" + color if color != "shulker_box" else "")
            }
        elif short.endswith("banner"):
            kind = "banner"
        elif short == "decorated_pot":
            kind = "decorated_pot"
        elif short == "conduit":
            kind = "conduit"
    parts = {}
    if kind == "chest":
        texture = spec.get("texture", "minecraft:normal").split(":")[-1]
        state = descriptor.get("_oxal_state",{})
        chest_type = state.get("type","single") if short != "ender_chest" else "single"
        if chest_type not in {"single","left","right"}:
            raise ValueError("箱子类型无效")
        suffix = "" if chest_type == "single" else "_"+chest_type
        texture += suffix
        mat = material_for("minecraft:entity/chest/" + texture)
        parts = entity(transaction, collection, root, "chest"+suffix, mat, upright=1)
        angle = {"north":math.pi,"south":0,"east":math.pi/2,"west":-math.pi/2}.get(state.get("facing","north"),0)
        body = parts["chest"+suffix]
        body["oxal_mc_studio_model_part"] = "chest"
        body.location = Matrix.Rotation(angle,3,"Z") @ Vector((-.5,.5,0))
        body.rotation_euler.z = angle
        from .asset_hinges import chest_motion
        if animated:
            chest_motion(root,parts,collection)
    elif kind == "shulker_box":
        texture = spec.get("texture", "minecraft:shulker").split(":")[-1]
        mat = material_for("minecraft:entity/shulker/" + texture)
        parts = entity(transaction, collection, root, "shulker", mat)
        if animated:
            root["open"] = 0.0
            base = parts["lid"].location.z
            driver(parts["lid"], "location", f"{base}+v*.5", root, "open", 2)
            driver(parts["lid"], "rotation_euler", "-v*4.71238898038469", root, "open", 2)
        from .asset_authored import normalize_world
        normalize_world(root)
        state = descriptor.get("_oxal_state",{})
        rotation = {"up":(0,0,0),"down":(math.pi,0,0),"north":(-math.pi/2,0,0),"south":(math.pi/2,0,0),"east":(0,math.pi/2,0),"west":(0,-math.pi/2,0)}[state.get("facing","up")]
        change = Matrix.Translation((0,0,.5)) @ Euler(rotation).to_matrix().to_4x4() @ Matrix.Translation((0,0,-.5))
        for top in tuple(root.children):
            top.matrix_basis = change @ top.matrix_basis
    elif kind in {"head", "player_head"}:
        head = spec.get("kind", "player")
        if head == "dragon":
            from .asset_entity_geometry import build as entity_geometry

            root["oxal_mc_studio_special_kind"] = "dragon_head"
            return entity_geometry(
                transaction, collection, root, "dragon_head", material_for, {**spec,"_oxal_rig_enabled":animated}
            )
        textures = {
            "skeleton": "entity/skeleton/skeleton",
            "wither_skeleton": "entity/skeleton/wither_skeleton",
            "zombie": "entity/zombie/zombie",
            "creeper": "entity/creeper/creeper",
            "piglin": "entity/piglin/piglin",
            "dragon": "entity/enderdragon/dragon",
        }
        if head == "player":
            texture = "entity/player/wide/steve"
            if not catalog.resource(
                "minecraft:" + texture, "textures", ".png"
            ).is_file():
                raise ValueError("原版资源中没有默认玩家皮肤")
        else:
            texture = textures.get(head)
            if texture is None:
                raise ValueError("尚未适配的头颅类型：" + head)
        parts = entity(
            transaction,
            collection,
            root,
            "piglin_head"
            if head == "piglin"
            else "humanoid_head"
            if head in {"zombie", "player"}
            else "skull",
            material_for("minecraft:" + texture),
        )
        from .asset_authored import normalize_world
        normalize_world(root)
    elif kind == "banner":
        mat = material_for("minecraft:entity/banner/banner_base")
        parts = entity(transaction, collection, root, "banner", mat)
        color = spec.get("color", short.removesuffix("_banner"))
        if color not in DYE_COLORS:
            raise ValueError("未知旗帜染色：" + str(color))
        cloth = material_for(
            "minecraft:entity/banner/base",
            0,
            {"tints": [{"type": "minecraft:constant", "value": DYE_COLORS[color]}]},
        )
        # The palette is sRGB, while Blender shader color sockets are scene-linear.
        # Keep the wooden support on its independent untinted material.
        rgb = [((DYE_COLORS[color] >> shift) & 255) / 255 for shift in (16, 8, 0)]
        linear = tuple(
            v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb
        ) + (1.0,)
        cast(Any, cloth.node_tree).nodes["OXAL Tint"].inputs[2].default_value = linear
        cloth.diffuse_color = linear
        flag = entity(transaction, collection, root, "flag", cloth)
        root["oxal_mc_studio_banner_color"] = color
        parts.update(flag)
        root["banner_motion"], root["摆动幅度"] = False, .1
        root["banner_wall"] = descriptor.get("_oxal_state",{}).get("attachment","floor") == "wall"
        if animated and "pole" in parts:
            for axis in range(3):
                driver(parts["pole"],"scale","1-v",root,"banner_wall",axis)
        for obj in flag.values():
            if animated and obj.name.startswith("flag"):
                asset_effects.native(
                    obj, "rotation_euler", "enabled*amount*sin(frame*2)", root,
                    [("enabled", "banner_motion"), ("amount", "摆动幅度")], index=0
                )
        from .asset_authored import normalize_world
        normalize_world(root, 2 / 3)
    elif kind == "decorated_pot":
        parts = entity(
            transaction,
            collection,
            root,
            "pot_base",
            material_for("minecraft:entity/decorated_pot/decorated_pot_base"),
        )
        parts.update(
            entity(
                transaction,
                collection,
                root,
                "pot_sides",
                material_for("minecraft:entity/decorated_pot/decorated_pot_side"),
            )
        )
    elif kind == "conduit":
        parts = entity(
            transaction,
            collection,
            root,
            "conduit_shell",
            material_for("minecraft:entity/conduit/base"),
        )
        for part in parts.values():
            if animated and part.name.startswith("conduit_shell"):
                asset_effects.native(
                    part, "rotation_euler", "frame*.4", root, [], index=2
                )
    elif kind == "copper_golem_statue":
        from .asset_entity_geometry import build as entity_geometry

        parts = entity_geometry(transaction, collection, root, kind, material_for, spec)
    elif kind in {"shield", "trident"}:
        from .asset_item_geometry import build as item_geometry

        parts = item_geometry(transaction, collection, root, kind, material_for)
    else:
        raise ValueError("尚未适配的特殊模型：" + (kind or short))
    root["oxal_mc_studio_special_kind"] = kind
    return parts


def _source_bounds(identifier, state):
    descriptors, _ = catalog.plan(identifier, state=state)
    points = []
    for descriptor in descriptors:
        data = catalog.model(descriptor["model"])
        for element in data.get("elements", ()):
            a, b = element["from"], element["to"]
            for x in (a[0], b[0]):
                for y in (a[1], b[1]):
                    for z in (a[2], b[2]):
                        points.append(
                            geometry.model_point(
                                (x, y, z), element.get("rotation"), descriptor
                            )
                        )
    if not points:
        raise ValueError("无法从源方块模型确定关节位置")
    return (
        Vector(tuple(min(p[i] for p in points) for i in range(3))),
        Vector(tuple(max(p[i] for p in points) for i in range(3))),
    )


def _pivot(transaction, collection, root, name, point):
    obj = transaction.track(bpy.data.objects.new(name, None))
    obj["oxal_mc_studio_model_part"] = name
    collection.objects.link(obj)
    obj.parent = root
    obj.location = point
    return obj


def _attach(obj, pivot):
    # All affected IDs are still in this transaction's unpublished collection.
    # Retain the source rest transform without evaluating a dependency graph.
    obj.parent = pivot
    obj.matrix_parent_inverse = Matrix.Translation(-pivot.location)


def _hinge(transaction, collection, root, identifier, state):
    closed = _source_bounds(identifier, {**state, "open": "false"})
    opened = _source_bounds(identifier, {**state, "open": "true"})
    overlap_low = Vector(tuple(max(closed[0][i], opened[0][i]) for i in range(3)))
    overlap_high = Vector(tuple(min(closed[1][i], opened[1][i]) for i in range(3)))
    pivot_point = (overlap_low + overlap_high) / 2
    axis = (
        max(range(3), key=lambda i: overlap_high[i] - overlap_low[i])
        if "trapdoor" in identifier
        else 2
    )
    if axis == 2:
        pivot_point.z = 0
    corners = [
        Vector((x, y, z))
        for x in (closed[0].x, closed[1].x)
        for y in (closed[0].y, closed[1].y)
        for z in (closed[0].z, closed[1].z)
    ]

    def mismatch(angle):
        rotation = Matrix.Rotation(angle, 3, "XYZ"[axis])
        points = [pivot_point + rotation @ (p - pivot_point) for p in corners]
        low = [min(p[i] for p in points) for i in range(3)]
        high = [max(p[i] for p in points) for i in range(3)]
        return sum(
            abs(low[i] - opened[0][i]) + abs(high[i] - opened[1][i]) for i in range(3)
        )

    angle = min((-math.pi / 2, math.pi / 2), key=mismatch)
    initial = float(state.get("open") == "true")
    asset_effects.control(root, "open", initial, 0.0, 1.0)
    parts = tuple(root.children)
    pivot = _pivot(transaction, collection, root, "OXAL hinge", pivot_point)
    for obj in parts:
        _attach(obj, pivot)
    driver(pivot, "rotation_euler", f"{angle}*(v-{initial})", root, "open", axis)
    root["oxal_mc_studio_native_control"] = "hinge"


def _replace_owned_geometry(root):
    for obj in tuple(root.children):
        if obj.type == "MESH":
            obj.hide_render = obj.hide_viewport = True
            obj["oxal_mc_studio_replaced_geometry"] = True


def _fence_gate(transaction, collection, root, identifier, state, material_for):
    descriptors, _ = catalog.plan(identifier, state={**state, "open": "false"})
    _replace_owned_geometry(root)
    asset_effects.control(root, "open", float(state.get("open") == "true"), 0.0, 1.0)
    for descriptor in descriptors:
        data = catalog.model(descriptor["model"])
        left = _pivot(
            transaction,
            collection,
            root,
            "OXAL gate left",
            geometry.model_point((1, 0, 8), variant=descriptor),
        )
        right = _pivot(
            transaction,
            collection,
            root,
            "OXAL gate right",
            geometry.model_point((15, 0, 8), variant=descriptor),
        )
        driver(left, "rotation_euler", "-pi*v/2", root, "open", 2)
        driver(right, "rotation_euler", "pi*v/2", root, "open", 2)
        for index, element in enumerate(data.get("elements", ())):
            obj = geometry.elements(
                transaction,
                f"OXAL gate part {index}",
                {**data, "elements": [element]},
                material_for,
                descriptor,
            )
            collection.objects.link(obj)
            obj.parent = root
            center = (element["from"][0] + element["to"][0]) / 2
            if 2 < center < 14:
                _attach(obj, left if center < 8 else right)
    root["oxal_mc_studio_native_control"] = "fence_gate"


def _piston(transaction, collection, root, identifier, state, material_for):
    descriptors, _ = catalog.plan(identifier, state={**state, "extended": "true"})
    if len(descriptors) != 1:
        raise ValueError("活塞源方块没有唯一基座模型")
    descriptor = descriptors[0]
    _replace_owned_geometry(root)
    asset_effects.control(
        root, "progress", float(state.get("extended") == "true"), 0.0, 1.0
    )
    data = catalog.model(descriptor["model"])
    body = geometry.elements(
        transaction, "OXAL piston base", data, material_for, descriptor
    )
    collection.objects.link(body)
    body.parent = root
    head_name = (
        "minecraft:block/piston_head_sticky"
        if identifier.endswith("sticky_piston")
        else "minecraft:block/piston_head"
    )
    head = catalog.model(head_name)
    elements = head.get("elements", ())
    if len(elements) != 2:
        raise ValueError("活塞头源模型缺少面板或连杆")
    origin = geometry.model_point((8, 8, 8), variant=descriptor)
    direction = geometry.model_point((8, 8, -8), variant=descriptor) - origin
    direction.normalize()
    cap_center = geometry.model_point(tuple((elements[0]["from"][i] + elements[0]["to"][i]) / 2 for i in range(3)), variant=descriptor)
    cap_pivot = _pivot(transaction, collection, root, "OXAL piston head", cap_center)
    rod_point = geometry.model_point((8, 8, 4), variant=descriptor)
    rod_pivot = _pivot(transaction, collection, root, "OXAL piston rod", rod_point)
    for axis in range(3):
        driver(cap_pivot, "location", f"{cap_center[axis]}+{direction[axis]}*v", root, "progress", axis)
        driver(
            rod_pivot,
            "location",
            f"{rod_point[axis]}+{direction[axis]}*v",
            root,
            "progress",
            axis,
        )
    moving_axis = max(range(3), key=lambda i: abs(direction[i]))
    driver(rod_pivot, "scale", "v", root, "progress", moving_axis)
    for index, element in enumerate(elements):
        obj = geometry.elements(
            transaction,
            "OXAL piston cap" if index == 0 else "OXAL piston shaft",
            {**head, "elements": [element]},
            material_for,
            descriptor,
        )
        collection.objects.link(obj)
        obj.parent = root
        if index == 0:
            _attach(obj, cap_pivot)
        else:
            _attach(obj, rod_pivot)
    root["oxal_mc_studio_native_control"] = "piston"


def prepare_block_controls(transaction, collection, root, identifier, material_for):
    """Prepare movable owned parts before publication; native playback only."""
    state = json.loads(root.get("oxal_mc_studio_block_state", "{}"))
    short = identifier.split(":")[-1]
    if short.endswith(("_trapdoor", "_door")):
        _hinge(transaction, collection, root, identifier, state)
    elif short.endswith("_fence_gate"):
        _fence_gate(transaction, collection, root, identifier, state, material_for)
    elif short in {"piston", "sticky_piston"}:
        _piston(transaction, collection, root, identifier, state, material_for)


def effects(transaction, collection, root, identifier, material_for):
    """Build owned effect data once; native drivers handle all later frames."""
    ensure_mutation_allowed()
    prepare_block_controls(transaction, collection, root, identifier, material_for)
    if asset_effects.build(transaction, collection, root, identifier, material_for):
        return
    short = identifier.split(":")[-1]
    if any(
        key in short for key in ("lantern", "glowstone", "sea_lantern", "end_crystal")
    ):
        color = (0.2, 0.7, 1.0) if "soul" in short else (1.0, 0.58, 0.22)
        asset_effects.point_light(transaction, collection, root, color=color)
        asset_effects.body_emission(root, {"minecraft:block/" + short})
