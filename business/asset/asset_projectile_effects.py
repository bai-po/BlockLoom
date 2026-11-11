"""Append sanitized immutable projectile GN batches and bind native inputs."""
import json
import math
from typing import cast

import bpy

from ...util.packed_images import rebase_native_image
from ...util.resources import AppendTransaction, live_ids
from . import asset_catalog as catalog
from .asset_geometry import ORIGINAL_IMAGE, ORIGINAL_PATH, TEXTURE_KEY, driver


def billboard(transaction,obj, *, point_at_camera=False, plane_rotation=math.pi/2):
    """Resolve the active render camera in native object-relative coordinates."""
    tree=transaction.track(bpy.data.node_groups.new("投射物 · 原生镜头朝向","GeometryNodeTree"))
    tree.interface.new_socket(name="Geometry",in_out="INPUT",socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry",in_out="OUTPUT",socket_type="NodeSocketGeometry")
    entry=tree.nodes.new("NodeGroupInput");output=tree.nodes.new("NodeGroupOutput")
    position=tree.nodes.new("GeometryNodeInputPosition")
    active=tree.nodes.new("GeometryNodeInputActiveCamera")
    camera=tree.nodes.new("GeometryNodeObjectInfo");camera.transform_space="RELATIVE"
    tree.links.new(active.outputs[0],camera.inputs["Object"])
    orient=tree.nodes.new("ShaderNodeVectorRotate");orient.rotation_type="EULER_XYZ"
    if point_at_camera:
        direction=tree.nodes.new("ShaderNodeVectorMath");direction.operation="SCALE"
        tree.links.new(camera.outputs["Location"],direction.inputs[0]);direction.inputs[3].default_value=-1
        aim=tree.nodes.new("FunctionNodeAlignEulerToVector");aim.axis="Y"
        tree.links.new(direction.outputs[0],aim.inputs["Vector"])
        world=tree.nodes.new("GeometryNodeObjectInfo");world.transform_space="ORIGINAL"
        tree.links.new(active.outputs[0],world.inputs["Object"])
        inverse=tree.nodes.new("ShaderNodeVectorRotate");inverse.rotation_type="EULER_XYZ";inverse.invert=True
        inverse.inputs["Vector"].default_value=(0,0,1)
        tree.links.new(world.outputs["Rotation"],inverse.inputs["Rotation"])
        up=tree.nodes.new("ShaderNodeVectorRotate");up.rotation_type="EULER_XYZ"
        tree.links.new(inverse.outputs[0],up.inputs["Vector"]);tree.links.new(camera.outputs["Rotation"],up.inputs["Rotation"])
        roll=tree.nodes.new("FunctionNodeAlignEulerToVector");roll.axis="Z";roll.pivot_axis="Y"
        tree.links.new(aim.outputs[0],roll.inputs["Rotation"]);tree.links.new(up.outputs[0],roll.inputs["Vector"])
        tree.links.new(position.outputs[0],orient.inputs["Vector"]);tree.links.new(roll.outputs[0],orient.inputs["Rotation"])
    else:
        plane=tree.nodes.new("ShaderNodeVectorRotate");plane.rotation_type="EULER_XYZ"
        plane.inputs["Rotation"].default_value=(plane_rotation,0,0)
        tree.links.new(position.outputs[0],plane.inputs["Vector"])
        tree.links.new(plane.outputs[0],orient.inputs["Vector"])
        tree.links.new(camera.outputs["Rotation"],orient.inputs["Rotation"])
    move=tree.nodes.new("GeometryNodeSetPosition")
    tree.links.new(entry.outputs["Geometry"],move.inputs["Geometry"])
    tree.links.new(orient.outputs[0],move.inputs["Position"])
    tree.links.new(move.outputs[0],output.inputs["Geometry"])
    cast(bpy.types.NodesModifier,obj.modifiers.new("原生镜头朝向","NODES")).node_group=tree


def build(transaction,scene,collection,root,identifier,options):
    from .assets import replace_material_textures
    short=identifier.split(":")[-1]
    path=catalog.ROOT/"native_effect/projectile_native.blend"
    with AppendTransaction(path) as append:
        source=append.append_collection("FX."+short)
        appended=tuple(append.owned)
        objects=tuple(source.all_objects)
        if not set(objects)<=set(appended):
            raise ValueError("投射物原生资源来源登记不完整")
        if any(isinstance(item,(bpy.types.Scene,bpy.types.Text)) for item in appended):
            raise ValueError("投射物资源包含禁止的场景或脚本文本依赖")
        for item in appended:
            transaction.track(item)
        append.commit(image_paths=transaction.image_paths)
    controls={obj for obj in objects if obj.get("oxal_mc_studio_effect_template_controller")}
    for item in appended:
        for owner in (item,getattr(item,"node_tree",None)):
            animation=getattr(owner,"animation_data",None)
            for curve in animation.drivers if animation else ():
                if not curve.driver.is_simple_expression or curve.driver.use_self:
                    raise ValueError("投射物原生资源包含非简单驱动")
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if isinstance(target.id,bpy.types.Scene):
                            raise TypeError("投射物资源保留了来源场景")
                        if target.id not in controls:
                            continue
                        if target.data_path in {'["oxal_mc_studio_template_fps"]','["oxal_mc_studio_template_fps_base"]'}:
                            target.id_type="SCENE";target.id=scene
                            target.data_path="render.fps" if target.data_path.endswith('fps"]') else "render.fps_base"
                        else:
                            end=target.data_path.find("]")
                            key=json.loads(target.data_path[1:end])
                            if key not in root:
                                value=target.id[key]
                                root[key]=list(value) if hasattr(value,"to_list") else value
                            target.id=root
                            if key=="阶段" and root.get("oxal_mc_studio_projectile_schema")==2:
                                from .asset_projectiles import EFFECT_PHASE
                                target.data_path='["'+EFFECT_PHASE+'"]'
    if root.get("oxal_mc_studio_projectile_schema")==3:
        from .asset_projectile_clock import bridge
        for item in appended:
            if isinstance(item,bpy.types.GeometryNodeTree):
                bridge(transaction,scene,root,item)
    materials={mat for obj in objects if obj.type=="MESH" for mat in cast(bpy.types.Mesh,obj.data).materials if mat}
    replacements={}
    for material in materials:
        if material.get("oxal_mc_studio_potion_color"):
            # The original impact GN shader multiplies the registry's raw RGB
            # components; item overlays and arrow particles use scene-linear.
            material["oxal_mc_studio_native_particle_palette"]=True
        for node in cast(bpy.types.ShaderNodeTree,material.node_tree).nodes:
            if node.type=="TEX_IMAGE" and node.get(TEXTURE_KEY):
                original=catalog.resource(node[TEXTURE_KEY],"textures",".png")
                if not original.is_file():raise ValueError("投射物原图缺失："+node[TEXTURE_KEY])
                node[ORIGINAL_PATH]=str(original)
                image=cast(bpy.types.ShaderNodeTexImage,node).image
                node[ORIGINAL_IMAGE]=image
                if image is None:raise ValueError("投射物原图未打包")
                rebase_native_image(image, str(node[TEXTURE_KEY]))
        digest=options.get("pack_digest",scene.get("oxal_mc_studio_future_pack",""))
        replacements[material]=replace_material_textures(transaction,root,material,digest,False,scene) if digest else material
    for obj in objects:
        if obj in controls:continue
        variant=json.loads(obj.get("oxal_mc_studio_source_variant","{}"))
        collection.objects.link(obj)
        for previous in tuple(obj.users_collection):
            if previous!=collection:previous.objects.unlink(obj)
        obj.parent=root
        obj["oxal_mc_studio_effect_geometry"]=True
        mesh=cast(bpy.types.Mesh,obj.data)
        for index,material in enumerate(tuple(mesh.materials)):
            if material:mesh.materials[index]=replacements[material]
        terms=["phase!=2"]
        from .asset_projectiles import EFFECT_PHASE
        phase_key=("oxal_mc_studio_projectile_batch_gate" if root.get("oxal_mc_studio_projectile_schema")==3
                   else EFFECT_PHASE if root.get("oxal_mc_studio_projectile_schema")==2 else "阶段")
        variables=[("phase",root,'["'+phase_key+'"]')]
        if "shape" in variant:
            shapes=("small_ball","large_ball","star","creeper","burst")
            terms.extend((f"floor(shape+.5)!={shapes.index(variant['shape'])}",f"(twinkle>.5)!={int(variant['twinkle'])}"))
            variables.extend((("shape",root,'["烟花形状"]'),("twinkle",root,'["烟花闪烁"]')))
            if obj.get("oxal_mc_studio_native_particle_kind")=="spark_trail":
                terms.append("not trail");variables.append(("trail",root,'["烟花拖尾"]'))
        if "instant" in variant:
            terms.append(f"(instant>.5)!={int(variant['instant'])}")
            variables.append(("instant",root,'["瞬时药水"]'))
        from .asset_lightning import _variables
        for visibility in ("hide_render","hide_viewport"):
            curve=driver(obj,visibility," or ".join(terms))
            _variables(curve,variables)
    retired=[*controls,*[item for item in appended if isinstance(item,bpy.types.Collection)]]
    for item in retired:transaction.forget(item)
    bpy.data.batch_remove(ids=live_ids(retired))
    root["oxal_mc_studio_projectile_native_resource"]=1
