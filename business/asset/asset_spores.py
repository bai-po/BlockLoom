"""Native ambient spores with the authored sprite shape and volume physics."""

import math
import random
from typing import Any, cast

import bpy
from mathutils import Vector

from . import asset_catalog as catalog
from . import asset_effects as effects
from . import asset_playback
from .asset_geometry import mesh_object


def build(tx,scene,col,root,material_for,playback):
    rng = random.Random(196)
    mesh = tx.track(bpy.data.meshes.new("孢子花 · 原生点载体"))
    mesh.from_pydata([(rng.uniform(-10,10),rng.uniform(-10,10),rng.uniform(-9,0)) for _ in range(384)],[],[])
    for name,values in (
        ("phase",[rng.random() for _ in range(384)]),
        ("life",[rng.uniform(25,50) for _ in range(384)]),
        ("size",[rng.uniform(.07,.1) for _ in range(384)]),
        ("variant",[rng.randrange(48) for _ in range(384)]),
    ):
        record = mesh.attributes.new("oxal_spore_"+name,"FLOAT","POINT")
        record.data.foreach_set("value",values)
    obj = tx.track(bpy.data.objects.new(mesh.name,mesh))
    col.objects.link(obj)
    obj.parent = root
    obj["oxal_mc_studio_effect_geometry"] = True
    obj["oxal_mc_studio_model_part"] = "spore_volume_carrier"
    key = catalog.read(catalog.resource("minecraft:spore_blossom_air","particles"))["textures"][0]
    source = material_for("minecraft:particle/"+key.split(":")[-1])
    material = tx.track(source.copy())
    material.name = "孢子花 · 原版单面孢子"
    material.surface_render_method = "BLENDED"
    shader_tree = cast(Any,material.node_tree)
    shader = next(node for node in shader_tree.nodes if node.type=="BSDF_PRINCIPLED")
    texture = shader_tree.nodes["OXAL Source Texture"]
    tint = shader_tree.nodes.get("OXAL Tint") or shader_tree.nodes.new("ShaderNodeMixRGB")
    tint.name = "OXAL Tint"
    tint.blend_type = "MULTIPLY"
    tint.inputs[0].default_value = 1
    tint.inputs[2].default_value = (.32,.5,.22,1)
    shader_tree.links.new(texture.outputs["Color"],tint.inputs[1])
    for key in ("Base Color","Emission Color"):
        shader_tree.links.new(tint.outputs[0],shader.inputs[key])
    shader.inputs["Emission Strength"].default_value = .25
    vertices,faces,uvs = [],[],[]
    for flip_u,flip_v in ((False,False),(True,False),(False,True),(True,True)):
        for index in range(12):
            y = 1-2*(index+.5)/12
            radius = math.sqrt(1-y*y)
            theta = index*math.pi*(3-math.sqrt(5))
            normal = Vector((radius*math.cos(theta),y,radius*math.sin(theta)))
            rotation = Vector((0,1,0)).rotation_difference(normal)
            first = len(vertices)
            vertices.extend(tuple(float((rotation@Vector(point))[axis]) for axis in range(3)) for point in ((-1,0,-1),(1,0,-1),(1,0,1),(-1,0,1)))
            faces.append(tuple(range(first,first+4)))
            uvs.append([(1-u if flip_u else u,1-v if flip_v else v) for u,v in ((0,0),(1,0),(1,1),(0,1))])
    prototype = mesh_object(tx,"孢子花 · 4UV × 12朝向原生精灵",vertices,faces,uvs,[material],[0]*48)
    col.objects.link(prototype)
    prototype.parent = root
    prototype.hide_render = prototype.hide_select = True
    prototype.hide_viewport = True
    prototype["oxal_mc_studio_effect_geometry"] = True
    prototype["oxal_mc_studio_model_part"] = "spore_sprite_variants"
    group = tx.track(bpy.data.node_groups.new("孢子花 · 原生体积重力精灵","GeometryNodeTree"))
    group.interface.new_socket(name="Geometry",in_out="INPUT",socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry",in_out="OUTPUT",socket_type="NodeSocketGeometry")
    obj.modifiers.new("原版孢子 · 无回调","NODES").node_group = group
    nodes,links = group.nodes,group.links
    inp,out = nodes.new("NodeGroupInput"),nodes.new("NodeGroupOutput")
    def attribute(name):
        node = nodes.new("GeometryNodeInputNamedAttribute")
        node.data_type = "FLOAT"
        node.inputs["Name"].default_value = "oxal_spore_"+name
        return node.outputs["Attribute"]
    def math_node(operation,a=None,b=None):
        node = nodes.new("ShaderNodeMath")
        node.operation = operation
        for index,value in enumerate((a,b)):
            if isinstance(value,(int,float)):
                node.inputs[index].default_value = value
            elif value is not None:
                links.new(value,node.inputs[index])
        return node.outputs[0]
    clock = nodes.new("ShaderNodeValue")
    curve = effects.native(clock.outputs[0],"default_value","frame",root,[])
    asset_playback.retarget(curve,playback)
    normalized = math_node("FRACT",math_node("ADD",attribute("phase"),math_node("DIVIDE",clock.outputs[0],attribute("life"))))
    seconds = math_node("MULTIPLY",normalized,attribute("life"))
    gravity = nodes.new("ShaderNodeCombineXYZ")
    for axis in range(3):
        curve = gravity.inputs[axis].driver_add("default_value")
        from .asset_geometry import initialize_driver
        initialize_driver(curve)
        curve.driver.expression = "g*enabled"
        for name,path in (("g","gravity["+str(axis)+"]"),("enabled","use_gravity")):
            variable = curve.driver.variables.new()
            variable.name,variable.type = name,"SINGLE_PROP"
            variable.targets[0].id_type = "SCENE"
            variable.targets[0].id = scene
            variable.targets[0].data_path = path
    # Particle collection sprites have world-space normals and size. Cancel
    # the emitter's linear transform for the card and for world gravity; its
    # actual translation continues to position the birth volume natively.
    self_object = nodes.new("GeometryNodeSelfObject")
    self_info = nodes.new("GeometryNodeObjectInfo")
    self_info.transform_space = "ORIGINAL"
    links.new(self_object.outputs[0],self_info.inputs["Object"])
    inverse = nodes.new("FunctionNodeInvertMatrix")
    links.new(self_info.outputs["Transform"],inverse.inputs["Matrix"])
    local_gravity = nodes.new("FunctionNodeTransformDirection")
    links.new(gravity.outputs[0],local_gravity.inputs["Direction"])
    links.new(inverse.outputs[0],local_gravity.inputs["Transform"])
    vector = nodes.new("ShaderNodeVectorMath")
    vector.operation = "SCALE"
    links.new(local_gravity.outputs[0],vector.inputs[0])
    links.new(math_node("MULTIPLY",math_node("MULTIPLY",seconds,seconds),.0015),vector.inputs["Scale"])
    position = nodes.new("GeometryNodeSetPosition")
    links.new(inp.outputs["Geometry"],position.inputs["Geometry"])
    links.new(vector.outputs[0],position.inputs["Offset"])
    info = nodes.new("GeometryNodeObjectInfo")
    info.transform_space = "ORIGINAL"
    info.inputs["Object"].default_value = prototype
    card = nodes.new("GeometryNodeSetPosition")
    point = nodes.new("GeometryNodeInputPosition")
    direction = nodes.new("FunctionNodeTransformDirection")
    links.new(point.outputs[0],direction.inputs["Direction"])
    links.new(inverse.outputs[0],direction.inputs["Transform"])
    links.new(info.outputs["Geometry"],card.inputs["Geometry"])
    links.new(direction.outputs[0],card.inputs["Position"])
    index = nodes.new("GeometryNodeInputIndex")
    split = nodes.new("GeometryNodeSplitToInstances")
    split.domain = "FACE"
    links.new(card.outputs["Geometry"],split.inputs["Geometry"])
    links.new(index.outputs[0],split.inputs["Group ID"])
    density = nodes.new("ShaderNodeValue")
    curve = effects.native(density.outputs[0],"default_value","window*enabled*(96 if density<.75 else 192 if density<1.5 else 384)",root,[("density","oxal_mc_studio_spore_density"),("enabled","particles_enabled")])
    variable = curve.driver.variables.new()
    variable.name,variable.type = "window","SINGLE_PROP"
    variable.targets[0].id = playback
    variable.targets[0].data_path = '["'+asset_playback.ACTIVE+'"]'
    instances = nodes.new("GeometryNodeInstanceOnPoints")
    instances.inputs["Pick Instance"].default_value = True
    links.new(position.outputs["Geometry"],instances.inputs["Points"])
    links.new(math_node("LESS_THAN",index.outputs[0],density.outputs[0]),instances.inputs["Selection"])
    links.new(split.outputs["Instances"],instances.inputs["Instance"])
    links.new(attribute("variant"),instances.inputs["Instance Index"])
    links.new(attribute("size"),instances.inputs["Scale"])
    links.new(instances.outputs["Instances"],out.inputs["Geometry"])
