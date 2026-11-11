"""Original item display contexts and geometry-derived native attachment slots."""

import json
import math

from mathutils import Matrix, Vector

from . import asset_catalog as catalog


def transform(identifier,context):
    definition = catalog.read(catalog.resource(catalog.item_identifier(identifier),"items"))["model"]
    descriptors = catalog.item_nodes(definition,context=context)
    if not descriptors:
        raise ValueError("该物品在目标上下文没有显示模型")
    descriptor = descriptors[0]
    model = descriptor.get("base") if descriptor.get("type","").endswith(":special") else descriptor.get("model")
    data = catalog.model(model) if isinstance(model,str) else {}
    displays = data.get("display",{})
    left = context in {"firstperson_lefthand","thirdperson_lefthand"}
    spec = displays.get(context,displays.get(context.replace("lefthand","righthand"),{}) if left else {})
    angles = [float(value) for value in spec.get("rotation",(0,0,0))]
    offset = [float(value) for value in spec.get("translation",(0,0,0))]
    if left:
        angles[1],angles[2],offset[0] = -angles[1],-angles[2],-offset[0]
    basis = Matrix(((1,0,0),(0,0,-1),(0,1,0)))
    source_rotation = (Matrix.Rotation(math.radians(angles[0]),3,"X")
                       @ Matrix.Rotation(math.radians(angles[1]),3,"Y")
                       @ Matrix.Rotation(math.radians(angles[2]),3,"Z"))
    rotation = basis @ source_rotation @ basis.inverted()
    translation = basis @ Vector(offset) / 16
    scale = spec.get("scale",(1,1,1))
    scaling = basis.to_4x4() @ Matrix.Diagonal((*scale,1)) @ basis.inverted().to_4x4()
    return Matrix.Translation(translation) @ rotation.to_4x4() @ scaling


def slot_transform(identifier, context, *, visual_profile=None):
    """SLOT presentation retains the observed local.31 hand pose contract."""
    result = transform(identifier, context)
    short = identifier.split(":")[-1]
    if context.startswith("firstperson") and short not in {"shield", "trident", "fishing_rod", "crossbow"} and not short.endswith("_spear"):
        # The 196 inventory renderer has profile-specific resting transforms;
        # it does not use a missing/custom item JSON transform as identity.
        # Model/context selection still uses the declared item definition.
        side = -1 if context.endswith("lefthand") else 1
        profile = visual_profile or "ITEM"
        if profile == "BLOCK":
            yaw, roll, offset, size = (135 if side < 0 else 45), 0, (0, 0, 0), .4
        else:
            weapon = profile == "WEAPON"
            yaw, roll = (-90, 25) if weapon else (-90 * side, 25 * side)
            offset, size = (side * 1.13, 3.2, 1.13), .68
        basis = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
        source = (Matrix.Translation(Vector(offset) / 16)
                  @ Matrix.Rotation(math.radians(yaw), 4, "Y")
                  @ Matrix.Rotation(math.radians(roll), 4, "Z")
                  @ Matrix.Scale(size, 4))
        result = basis @ source @ basis.inverted()
    if identifier == "minecraft:shield" and context == "firstperson_righthand":
        # The real 196 inventory shield pose places the right hand at 2px;
        # this client's resource JSON uses 1.75px. Keep that observed quarter
        # pixel offset specific to inventory presentation, without changing
        # world/display contexts or the user's wrist bone.
        result.translation.z += .25 / 16
    return result


def slot_layout(root,identifier,points,objects,model_matrix):
    if not points:
        raise ValueError("展示架没有可用原生几何")
    low = Vector(tuple(min(point[i] for point in points) for i in range(3)))
    high = Vector(tuple(max(point[i] for point in points) for i in range(3)))
    center = (low+high)/2
    state = json.loads(root.get("oxal_mc_studio_block_state","{}"))
    if identifier.endswith("_shelf"):
        horizontal = max((0,1),key=lambda i:high[i]-low[i])
        normal = Vector({"north":(0,1,0),"south":(0,-1,0),"east":(1,0,0),"west":(-1,0,0)}.get(state.get("facing","north"),(0,1,0)))
        result = []
        for index in range(3):
            point = center.copy()
            point[horizontal] = low[horizontal]+(high[horizontal]-low[horizontal])*(index+.5)/3
            result.append((point,normal))
        return result
    thin = min(range(3),key=lambda i:high[i]-low[i])
    axis = Vector(tuple(1 if i==thin else 0 for i in range(3)))
    area = {1:0.,-1:0.}
    for obj in objects:
        matrix = model_matrix(obj)
        for polygon in obj.data.polygons:
            normal = (matrix.to_3x3()@polygon.normal).normalized()
            alignment = normal.dot(axis)
            if abs(alignment)>.95:
                area[1 if alignment>0 else -1] += polygon.area*abs(alignment)
    return [(center,axis*(1 if area[1]>=area[-1] else -1))]
