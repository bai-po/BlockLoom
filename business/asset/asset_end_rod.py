"""Eight authored end-rod sprites, with native age, drift and size animation."""

import math
from typing import Any, cast

from mathutils import Matrix, Vector

from . import asset_playback
from .asset_geometry import driver, mesh_object
from .asset_hinges import model_matrix

PARTICLE = "oxal_mc_studio_end_rod_particles"
SCHEMA = "oxal_mc_studio_end_rod_native"


def build(tx,scene,collection,root,options,material_for):
    from .asset_environment import _control, _sprite_material, _textures, _var
    _control(root,PARTICLE,"末地烛粒子",True)
    clock = asset_playback.create(tx,scene,collection,root,options)
    points = [model_matrix(obj,root)@vertex.co for obj in tuple(root.children_recursive)
              if obj.type=="MESH" and not obj.get("oxal_mc_studio_effect_geometry")
              for vertex in obj.data.vertices]
    if not points:
        raise ValueError("末地烛缺少原生本体几何")
    center = sum(points,Vector())/len(points)
    source = material_for(_textures("end_rod")[0])
    camera = scene.camera
    single_face = camera is not None
    previous = options.get("_particles_source_root")
    if previous is not None and previous.get(SCHEMA):
        # Rebuilding placement must not replace an existing no-camera sprite
        # contract merely because the user has since created a scene camera.
        from ...util.ownership import require_owned
        prior = [obj for obj in tuple(previous.children_recursive)
                 if obj.get("oxal_mc_studio_particle_kind")=="end_rod"]
        require_owned(previous,(previous,*prior))
        if len(prior)!=8 or len({len(obj.data.polygons) for obj in prior})!=1:
            raise ValueError("末地烛既有精灵几何合同不唯一，保留现状")
        single_face = len(prior[0].data.polygons)==1
        track = next((constraint for constraint in prior[0].constraints if constraint.type=="TRACK_TO"),None)
        camera = track.target if track is not None else None
    planes = [[(-.02,0,-.02),(.02,0,-.02),(.02,0,.02),(-.02,0,.02)]]
    if not single_face:
        planes += [[(0,-.02,-.02),(0,.02,-.02),(0,.02,.02),(0,-.02,.02)],
                   [(-.02,-.02,0),(.02,-.02,0),(.02,.02,0),(-.02,.02,0)]]
    for index in range(8):
        obj = mesh_object(tx,"末地烛 · 飘散粒子 "+str(index+1),[point for plane in planes for point in plane],
                          [tuple(range(offset,offset+4)) for offset in range(0,len(planes)*4,4)],
                          [[(0,0),(1,0),(1,1),(0,1)]]*len(planes),[source],[0]*len(planes))
        collection.objects.link(obj)
        obj.parent = root
        obj.hide_select = True
        obj["oxal_mc_studio_effect_geometry"] = True
        obj["oxal_mc_studio_particle_kind"] = "end_rod"
        obj["oxal_mc_studio_particle_index"] = index
        obj["oxal_mc_studio_model_part"] = "end_rod_sprite_"+str(index)
        obj["age"] = index/8
        age = cast(Any,driver(obj,'["age"]',f"fmod(fmod(local*base/fps/2.2+{index/8},1)+1,1)"))
        for name,owner,path in (("local",clock,'["'+asset_playback.TIME+'"]'),
                                ("base",scene,"render.fps_base"),("fps",scene,"render.fps")):
            _var(age,name,owner,path)
        material = _sprite_material(tx,root,obj,"end_rod",material_for)
        material.surface_render_method = source.surface_render_method
        tree = cast(Any,material.node_tree)
        # The authored image switches evaluate the clock expression directly.
        # Reading the float32 display-age property instead can round upward at
        # exact 1/8 boundaries (for example frame99), selecting a later PNG.
        for curve in tuple(tree.animation_data.drivers) if tree.animation_data else ():
            variables = tuple(curve.driver.variables)
            if not (curve.driver.expression.startswith("v>=") and len(variables)==1
                    and variables[0].targets[0].id==obj and variables[0].targets[0].data_path=='["age"]'):
                continue
            threshold = float(curve.driver.expression[3:])
            for variable in variables:
                curve.driver.variables.remove(variable)
            curve.driver.expression = "("+age.driver.expression+")>="+str(threshold)
            for name,owner,path in (("local",clock,'["'+asset_playback.TIME+'"]'),
                                    ("base",scene,"render.fps_base"),("fps",scene,"render.fps")):
                _var(curve,name,owner,path)
        shader = next(node for node in tree.nodes if node.type=="BSDF_PRINCIPLED")
        tree.links.new(shader.inputs["Base Color"].links[0].from_socket,shader.inputs["Emission Color"])
        shader.inputs["Emission Strength"].default_value = 1
        opacity = shader.inputs["Alpha"].links[0].from_node
        tree.links.new(opacity.inputs[0].links[0].from_socket,shader.inputs["Alpha"])
        tree.nodes.remove(opacity)
        cast(Any,obj.data).materials[0] = material
        motion = (.2*math.cos(index*2.4),.2*math.sin(index*2.4),.15)
        for axis in range(3):
            driver(obj,"location",f"{center[axis]}+v*{motion[axis]}",obj,"age",axis)
            size = driver(obj,"scale","enabled*window*(1-age)",index=axis)
            _var(size,"enabled",root,'["'+PARTICLE+'"]')
            _var(size,"window",clock,'["'+asset_playback.ACTIVE+'"]')
            _var(size,"age",obj,'["age"]')
        if camera is not None:
            track = cast(Any,obj.constraints.new("TRACK_TO"))
            track.target = camera
            track.track_axis,track.up_axis = "TRACK_NEGATIVE_Y","UP_Z"
    root[SCHEMA] = 1


def post_compile(root):
    """The original particles inherit the visible master, not the deform bone."""
    if not root.get(SCHEMA):
        return
    bone = root.data.bones["总控制"]
    inverse = (bone.matrix_local@Matrix.Translation((0,bone.length,0))).inverted()
    for obj in tuple(root.children_recursive):
        if obj.get("oxal_mc_studio_particle_kind")=="end_rod":
            obj.parent_bone = bone.name
            obj.matrix_parent_inverse = inverse
