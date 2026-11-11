"""Stock SLOT geometry frames, prepared only inside the creation transaction.

The legacy renderer distinguishes a unit block canvas, generated sprite canvas,
and bounded entity/item models. These declarations restore that distinction
without changing world models, user armatures, materials or runtime callbacks.
"""

from math import pi

from mathutils import Matrix, Vector

from . import asset_catalog as catalog
from .asset_geometry import descriptor_matrix


def matrices(pivot):
    result = {pivot: Matrix.Identity(4)}
    def local(obj):
        if obj not in result:
            result[obj] = local(obj.parent) @ obj.matrix_parent_inverse @ obj.matrix_basis
        return result[obj]
    return {obj: local(obj) for obj in tuple(pivot.children_recursive) if obj.type == "MESH"}


def points(pivot):
    return [matrix @ vertex.co for obj, matrix in matrices(pivot).items() for vertex in obj.data.vertices]


def apply(transaction, pivot, correction):
    """Bake a renderer frame into only freshly proven private Mesh data."""
    for obj, matrix in matrices(pivot).items():
        if obj not in transaction.ids or obj.data not in transaction.ids or obj.data.users != 1:
            raise ValueError("物品栏几何不是本次创建的独立数据，未改写")
        local = matrix.inverted() @ correction @ matrix
        obj.data.transform(local)
        if obj.get("oxal_mc_studio_pixel_topology"):
            saved = obj.get("oxal_mc_studio_sprite_transform")
            previous = Matrix([saved[i:i+4] for i in range(0,16,4)]) if saved else Matrix.Identity(4)
            obj["oxal_mc_studio_sprite_transform"] = [float(value) for row in local @ previous for value in row]
        obj.data.update()


def entity_uv(transaction, obj, kind):
    """Restore observed SLOT cuboid face/UV pairing on fresh private data."""
    if obj not in transaction.ids or obj.data not in transaction.ids or obj.data.users != 1:
        raise ValueError("物品栏实体UV没有本次私有数据归属")
    declarations = catalog.read(catalog.ROOT / "slot_entity_uv196.json")["items"][kind]
    rows = declarations[obj["oxal_mc_studio_model_part"]]
    mesh = obj.data
    if len(rows) != len(mesh.polygons) or any(len(row) != len(poly.vertices) for row, poly in zip(rows, mesh.polygons, strict=True)):
        raise ValueError("物品栏实体面布局与已知原生UV契约不一致")
    coordinates = [{vertex: uv for vertex, uv in zip(poly.vertices, row, strict=True)} for poly, row in zip(mesh.polygons, rows, strict=True)]
    mesh.flip_normals()
    layer = mesh.uv_layers.get("UVMap")
    if layer is None:
        raise ValueError("物品栏实体缺少原生UVMap")
    for poly, values in zip(mesh.polygons, coordinates, strict=True):
        for loop in poly.loop_indices:
            layer.data[loop].uv = values[mesh.loops[loop].vertex_index]
    obj["oxal_mc_studio_slot_entity_uv"] = 1
    mesh.update()


def refresh_pack_bounds(transaction, root):
    """Update only the owned eight-point carrier after explicit contour edits."""
    from ...util.ownership import require_owned
    held = root.get("oxal_inventory_held")
    if held is None:
        return
    candidates = [obj for obj in tuple(held.children_recursive) if obj.get("oxal_inventory_depth_bounds")]
    if len(candidates) != 1:
        raise ValueError("手持轮廓缺少唯一的原生深度边界")
    carrier = candidates[0]
    require_owned(root,(held,carrier,carrier.data))
    old = carrier.data
    if len(old.vertices) != 8 or old.polygons or old.shape_keys or old.animation_data:
        raise ValueError("手持深度边界含用户拓扑或动画，未覆盖")
    vertices = [matrix @ vertex.co for obj,matrix in matrices(held).items() if obj != carrier for vertex in obj.data.vertices]
    if not vertices:
        raise ValueError("手持物没有可用的包轮廓")
    low = tuple(min(v[i] for v in vertices) for i in range(3))
    high = tuple(max(v[i] for v in vertices) for i in range(3))
    mesh = transaction.track(old.copy())
    for vertex, point in zip(mesh.vertices,((x,y,z) for x in (low[0],high[0]) for y in (low[1],high[1]) for z in (low[2],high[2])),strict=True):
        vertex.co = point
    mesh.update()
    transaction.on_rollback(lambda: setattr(carrier,"data",old))
    carrier.data = mesh


def prepare(transaction, pivot, identifier, context, options, role, profile):
    descriptors, _ = catalog.plan(identifier, context=context, special=False, complete=False, state=options.get("state"), shape=int(options.get("shape", 0)))
    special = [part for part in descriptors if part.get("type", "").endswith(":special")]
    short = identifier.split(":")[-1]
    sprite = any(obj.get("oxal_mc_studio_pixel_topology") for obj in pivot.children_recursive)
    # Stock entity builders already implement their renderer geometry basis.
    # An item-definition transform is for raw Java coordinates, not the
    # normalized Blender mesh. Keep intrinsic component TRS untouched; bake
    # the frame correction so compatible existing object animation survives.
    if len(special) == len(descriptors) == 1 and short not in {"shield", "trident"}:
        renderer = descriptor_matrix({**special[0], "_oxal_centered": True})
        apply(transaction, pivot, renderer.inverted())
        vertices = points(pivot)
        low = Vector(tuple(min(point[i] for point in vertices) for i in range(3)))
        high = Vector(tuple(max(point[i] for point in vertices) for i in range(3)))
        if special[0].get("model", {}).get("type") != "minecraft:chest":
            shift = Vector((-(low.x + high.x) / 2, -(low.y + high.y) / 2, -low.z - .5))
            apply(transaction, pivot, Matrix.Translation(shift))
    element_canvas = profile == "BLOCK" and short != "dragon_head"
    pivot["oxal_mc_studio_slot_canvas_kind"] = "BLOCK" if element_canvas else "SPRITE" if sprite else "BOUNDS"
    pivot["oxal_mc_studio_slot_geometry_frame"] = 1
    if role != "HELD" or short in {"shield", "trident", "fishing_rod", "crossbow"} or short.endswith("_spear") or element_canvas:
        return
    vertices = points(pivot)
    if not vertices:
        return
    low = Vector(tuple(min(point[i] for point in vertices) for i in range(3)))
    high = Vector(tuple(max(point[i] for point in vertices) for i in range(3)))
    extent, center = high - low, (low + high) / 2
    camera_basis = Matrix.Rotation(-pi / 2, 4, "X")
    if profile == "BLOCK" or sprite:
        orient = camera_basis
    else:
        thin_axis = min(range(3), key=lambda axis: extent[axis])
        orient = (Matrix(((0, 1, 0, 0), (0, 0, 1, 0), (1, 0, 0, 0), (0, 0, 0, 1))) if thin_axis == 0
                  else camera_basis if thin_axis == 1 else Matrix.Identity(4))
    transformed = [orient @ point for point in vertices]
    width = max(point.x for point in transformed) - min(point.x for point in transformed)
    height = max(point.y for point in transformed) - min(point.y for point in transformed)
    diagonal = profile == "WEAPON" and height > width * 1.4
    if diagonal:
        orient = Matrix.Rotation(-pi / 4, 4, "Z") @ orient
    textures = {material.get("oxal_mc_studio_texture_key") for obj in pivot.children_recursive if obj.type == "MESH" for material in obj.data.materials if material}
    canvas = sprite and len(textures) == 1 and not diagonal
    correction = camera_basis.inverted() @ orient
    if not canvas:
        correction = correction @ Matrix.Scale(1 / max(extent.x, extent.y, extent.z, 1e-8), 4) @ Matrix.Translation(-center)
    apply(transaction, pivot, correction)
