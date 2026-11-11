"""Native camera-depth pass for independently owned held representations."""

import math
from typing import Any, cast

import bpy
from mathutils import Matrix, Vector

from ..asset.asset_geometry import initialize_driver
from .inventory import _driver


def local_points(pivot):
    """Immutable source geometry in its display pivot's local coordinates."""
    def matrix(obj):
        if obj == pivot:
            return Matrix.Identity(4)
        return matrix(obj.parent) @ obj.matrix_parent_inverse @ obj.matrix_basis
    return [matrix(obj) @ vertex.co for obj in tuple(pivot.children_recursive)
            if obj.type == "MESH" and not obj.get("oxal_inventory_depth_bounds")
            for vertex in obj.data.vertices]


def held_matrix(source_root, pivot, original, target):
    """Adapt retained two-unit wrists to the original resting arm/display."""
    basis = Matrix.Rotation(-math.pi/2,4,"X")
    side = -1 if target.handedness == "LEFT" else 1
    offset = Matrix.Translation(Vector((side*.07,-.44,-.04))*target.hand_unit)
    renderer = Matrix.Identity(4)
    identifier = source_root.get("oxal_mc_studio_library_identifier", "").split(":")[-1]
    if identifier in {"shield","trident"}:
        center = Vector((-.5,-.5,-.5))
        if identifier == "shield":
            # local.31 defines the shield's fitted center at -1.5/16 depth.
            # Account for this resource backend's native, uncentered special
            # geometry rather than applying that fitted center twice.
            points = [basis @ point for point in local_points(pivot)]
            actual = Vector(tuple((min(p[i] for p in points)+max(p[i] for p in points))/2 for i in range(3)))
            center += Vector((0,0,-1.5/16))-actual
        renderer = basis.inverted() @ Matrix.Translation(center) @ basis
    return offset @ basis @ Matrix.Scale(2*target.hand_unit,4) @ original @ renderer


def radial_depth(transaction, source_root, pivot, target):
    """Keep held vertices behind HUD while preserving their exact projection.

    One eight-point carrier bounds the complete representation. Every rendered
    component uses the same camera-radial factor, computed natively from the
    current bone, camera and HUD depth; no frame-time Python or cached matrix.
    """
    points = local_points(pivot)
    if not points:
        return
    collection = source_root.users_collection[0]
    mesh = transaction.track(bpy.data.meshes.new("手持深度 · 原生边界点"))
    low = [min(p[i] for p in points) for i in range(3)]
    high = [max(p[i] for p in points) for i in range(3)]
    mesh.from_pydata([(x,y,z) for x in (low[0],high[0]) for y in (low[1],high[1]) for z in (low[2],high[2])],[],[])
    bounds = transaction.track(bpy.data.objects.new(mesh.name,mesh))
    collection.objects.link(bounds)
    bounds.parent = pivot
    bounds.hide_render = True
    bounds.hide_select = True
    bounds.display_type = "WIRE"
    bounds["oxal_inventory_depth_bounds"] = True
    bounds["oxal_mc_studio_representation"] = "HELD"
    bounds["oxal_mc_studio_model_part"] = "库存 · 手持深度边界"
    bounds["oxal_mc_studio_effect_geometry"] = True
    group = transaction.track(bpy.data.node_groups.new("手持 · 原生HUD深度分层","GeometryNodeTree"))
    group.interface.new_socket(name="Geometry",in_out="INPUT",socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry",in_out="OUTPUT",socket_type="NodeSocketGeometry")
    nodes, links = group.nodes, group.links
    inp,out = nodes.new("NodeGroupInput"),nodes.new("NodeGroupOutput")
    camera = nodes.new("GeometryNodeObjectInfo")
    camera.transform_space = "RELATIVE"
    camera.inputs["Object"].default_value = target.root["独立物品栏相机"]
    invert = nodes.new("FunctionNodeInvertMatrix")
    links.new(camera.outputs["Transform"],invert.inputs["Matrix"])
    position = nodes.new("GeometryNodeInputPosition")
    to_camera = nodes.new("FunctionNodeTransformPoint")
    links.new(position.outputs["Position"],to_camera.inputs["Vector"])
    links.new(invert.outputs["Matrix"],to_camera.inputs["Transform"])
    carrier = nodes.new("GeometryNodeObjectInfo")
    carrier.transform_space = "RELATIVE"
    carrier.inputs["Object"].default_value = bounds
    separate = nodes.new("ShaderNodeSeparateXYZ")
    links.new(to_camera.outputs["Vector"],separate.inputs[0])
    depth = nodes.new("ShaderNodeMath")
    depth.operation = "MULTIPLY"
    depth.inputs[1].default_value = -1
    links.new(separate.outputs["Z"],depth.inputs[0])
    statistic = nodes.new("GeometryNodeAttributeStatistic")
    statistic.domain = "POINT"
    links.new(carrier.outputs["Geometry"],statistic.inputs["Geometry"])
    links.new(depth.outputs[0],statistic.inputs["Attribute"])
    def math_node(operation,a=None,b=None):
        node = nodes.new("ShaderNodeMath")
        node.operation = operation
        for index,value in enumerate((a,b)):
            if value is not None:
                if isinstance(value,(int,float)):
                    node.inputs[index].default_value = value
                else:
                    links.new(value,node.inputs[index])
        return node.outputs[0]
    hud = nodes.new("ShaderNodeValue")
    curve = _driver(hud.outputs[0],"default_value","depth+.002",[("depth",target.rig.data,'["HUD有效深度"]')])
    initialize_driver(curve)
    positive = math_node("GREATER_THAN",statistic.outputs["Min"],.00001)
    safe_depth = math_node("MAXIMUM",statistic.outputs["Min"],.00001)
    factor = math_node("MAXIMUM",math_node("DIVIDE",hud.outputs[0],safe_depth),1)
    factor = math_node("ADD",math_node("MULTIPLY",factor,positive),math_node("SUBTRACT",1,positive))
    scaled = nodes.new("ShaderNodeVectorMath")
    scaled.operation = "SCALE"
    links.new(to_camera.outputs["Vector"],scaled.inputs[0])
    links.new(factor,scaled.inputs["Scale"])
    to_object = nodes.new("FunctionNodeTransformPoint")
    links.new(scaled.outputs["Vector"],to_object.inputs["Vector"])
    links.new(camera.outputs["Transform"],to_object.inputs["Transform"])
    set_position = nodes.new("GeometryNodeSetPosition")
    links.new(inp.outputs["Geometry"],set_position.inputs["Geometry"])
    links.new(to_object.outputs["Vector"],set_position.inputs["Position"])
    links.new(set_position.outputs["Geometry"],out.inputs["Geometry"])
    for obj in tuple(pivot.children_recursive):
        if obj.type != "MESH" or obj == bounds:
            continue
        modifier = cast(Any,obj.modifiers.new("原生HUD前后层级","NODES"))
        modifier.node_group = group
    source_root["oxal_inventory_held_projection"] = 2


def stage_existing(transaction,scene,staged,source,held):
    """Rebuild generated depth geometry without touching the live slot state."""
    from ...util.resources import INSTANCE
    from .inventory_slots import HOST, RIG, SLOT, resolve_target
    host = source.get(HOST)
    if host is None:
        raise ValueError("手持深度重建缺少实际物品栏归属")
    target = resolve_target(scene,{"inventory_instance_id":str(host.get(INSTANCE,"")),"slot":source.get(SLOT,"")})
    if target.root != host or source.get(RIG) != target.rig:
        raise ValueError("手持深度重建的原生目标归属冲突")
    radial_depth(transaction,staged,held,target)
