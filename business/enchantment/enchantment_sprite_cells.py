"""Coalesce the flat texel faces of an authored sprite within each relief cell."""

from array import array
from collections import defaultdict
from math import ceil, hypot


def candidate_size(mesh, uvs, sizes, excluded, budget):
    """Decode source faces once, then count the exact grouping for candidate sizes."""
    dimensions = array("d")
    groups = {}
    eligible = array("q")  # group, x, y
    for face in mesh.polygons:
        width, height = sizes[face.material_index]
        uv = uvs[face.material_index]
        coords = [uv.data[i].uv for i in face.loop_indices]
        a, b = coords[1] - coords[0], coords[3] - coords[0]
        dimensions.extend((hypot(a.x * width, a.y * height), hypot(b.x * width, b.y * height)))
        if abs(face.normal.y) < .99999:
            continue
        corners = [(round(p.x * width, 4), round(p.y * height, 4)) for p in coords]
        x, y = int(min(p[0] for p in corners)), int(min(p[1] for p in corners))
        if set(corners) != {(x, y), (x + 1, y), (x + 1, y + 1), (x, y + 1)}:
            continue
        orientation = tuple((int(u) - x, int(v) - y) for u, v in corners)
        key = (face.material_index, round(mesh.vertices[face.vertices[0]].co.y, 6), orientation)
        group = groups.setdefault(key, len(groups))
        eligible.extend((group, x, y))
    # Every non-mergeable face and at least one face per group must survive.
    if len(mesh.polygons) - len(eligible) // 3 + len(groups) > budget:
        return None
    for size in range(16, 0, -1):
        if size == excluded:
            continue
        if size == 1 and len(mesh.polygons) > 65536:
            continue
        total = sum(max(1, ceil(dimensions[i] / size - 1e-5)) *
                    max(1, ceil(dimensions[i + 1] / size - 1e-5))
                    for i in range(0, len(dimensions), 2))
        if size > 1:
            tiles = defaultdict(set)
            for i in range(0, len(eligible), 3):
                group, x, y = eligible[i:i + 3]
                tiles[group, x // size, y // size].add((x, y))
            for cells in tiles.values():
                for x, y in sorted(cells, key=lambda p: (p[1], p[0])):
                    if (x, y) not in cells:
                        continue
                    width = 1
                    while (x + width, y) in cells:
                        width += 1
                    height = 1
                    while all((x + i, y + height) in cells for i in range(width)):
                        height += 1
                    total -= width * height - 1
                    for dy in range(height):
                        for dx in range(width):
                            cells.remove((x + dx, y + dy))
        if total <= budget:
            return size
    return None


def rectangles(mesh, uvs, sizes, cell_size):
    """Map each surviving face to its outer source loops; preserve alpha holes and side walls."""
    # Corner loop indices supply the original position, weights and *all* UV sets.
    # The source Mesh is still the unmodified fallback of the geometry switch.
    loops = {index: array("i") for index in range(4)}
    for polygon in mesh.polygons:
        for index, loop in enumerate(polygon.loop_indices):
            loops[index].append(loop)
    retained = bytearray([1]) * len(mesh.polygons)
    tiles = defaultdict(dict)
    for face in mesh.polygons:
        if abs(face.normal.y) < .99999:
            continue
        width, height = sizes[face.material_index]
        uv = uvs[face.material_index]
        corners = [(round(uv.data[i].uv.x * width, 4), round(uv.data[i].uv.y * height, 4))
                   for i in face.loop_indices]
        x, y = int(min(p[0] for p in corners)), int(min(p[1] for p in corners))
        if set(corners) != {(x,y),(x+1,y),(x+1,y+1),(x,y+1)}:
            continue
        # Only adjacent cells in the same orientation, rest plane and material combine.
        orientation = tuple((int(u)-x,int(v)-y) for u,v in corners)
        layer = round(mesh.vertices[face.vertices[0]].co.y, 6)
        tile = tiles[face.material_index,layer,orientation,x//cell_size,y//cell_size]
        tile[x,y] = face.index
    for (_,_,orientation,_,_), cells in tiles.items():
        for x,y in sorted(cells, key=lambda p:(p[1],p[0])):
            first = cells.get((x,y))
            if first is None:
                continue
            width = 1
            while (x+width,y) in cells:
                width += 1
            height = 1
            while all((x+i,y+height) in cells for i in range(width)):
                height += 1
            for index,(right,top) in enumerate(orientation):
                outer = cells[x+right*(width-1), y+top*(height-1)]
                loops[index][first] = loops[index][outer]
            for dy in range(height):
                for dx in range(width):
                    face = cells.pop((x+dx,y+dy))
                    if face != first:
                        retained[face] = 0
    return retained, loops
