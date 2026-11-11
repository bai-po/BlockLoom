"""Independent native projectile visuals and controller-driven native particles.

Source profiles and frozen source mesh data are immutable. Root transforms,
stage and visual controls are authored by the user; no launch,
collision, inventory simulation or Python playback observer is installed.
"""

import json
import math
from functools import lru_cache
from typing import cast

import bpy
from mathutils import Matrix, Vector

from ...core.runtime import ensure_mutation_allowed
from . import asset_catalog as catalog
from . import asset_rigs
from .asset_geometry import ORIGINAL_PATH, TEXTURE_KEY, driver, mesh_object
from .asset_lightning import _property, _variables
from .asset_special import _entity_mesh
from .asset_states import register_control

_ARROWS = {"arrow", "tipped_arrow", "spectral_arrow"}
EFFECT_PHASE = "oxal_mc_studio_projectile_effect_phase"
STAGE_BONE = "阶段 · 0隐藏 1投掷 2特效 3关闭"


@lru_cache(maxsize=1)
def profiles():
    result = dict(catalog.read(catalog.ROOT / "native_effect/projectile_profiles214.json"))
    result.update(catalog.read(catalog.ROOT / "projectile_profiles.json"))
    return result


def requires_rig(identifier):
    return identifier.split(":")[-1] in profiles()


def _arrow_material(transaction, root, identifier, material_for):
    key = "minecraft:" + profiles()[identifier]["texture"]
    mat = material_for(key)
    if identifier == "spectral_arrow":
        return mat
    mat = material_for("minecraft:entity/projectiles/arrow_tipped")
    tree = mat.node_tree
    tint = tree.nodes["OXAL Tint"]
    normal = material_for("minecraft:entity/projectiles/arrow")
    image = tree.nodes.new("ShaderNodeTexImage")
    image.name = "OXAL Arrow Normal Texture"
    image.image = normal.node_tree.nodes["OXAL Source Texture"].image
    image.interpolation, image.extension = "Closest", "CLIP"
    image[TEXTURE_KEY] = "minecraft:entity/projectiles/arrow"
    image[ORIGINAL_PATH] = str(catalog.resource(image[TEXTURE_KEY],"textures",".png"))
    mix = tree.nodes.new("ShaderNodeMixRGB")
    mix.name = "OXAL Arrow Kind"
    driver(mix.inputs[0],"default_value","v>.5",root,"箭型")
    tree.links.new(image.outputs["Color"],mix.inputs[1])
    tree.links.new(tint.outputs[0],mix.inputs[2])
    shader = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    tree.links.new(mix.outputs[0],shader.inputs["Base Color"])
    return mat


def _surface(transaction, root, material_for, texture, phase, *, potion=False, strength=1):
    source = material_for("minecraft:particle/"+texture)
    material = transaction.track(source.copy())
    material.name = "投射物粒子 · "+texture
    material.surface_render_method = "BLENDED"
    tree = material.node_tree
    texture_node = tree.nodes["OXAL Source Texture"]
    tint = tree.nodes["OXAL Tint"]
    for node in tuple(tree.nodes):
        if node not in {texture_node,tint}:
            tree.nodes.remove(node)
    alpha_source=texture_node.outputs["Alpha"]
    if potion:
        info=tree.nodes.new("ShaderNodeParticleInfo")
        age=tree.nodes.new("ShaderNodeMath");age.operation="DIVIDE"
        tree.links.new(info.outputs["Age"],age.inputs[0]);tree.links.new(info.outputs["Lifetime"],age.inputs[1])
        steps=tree.nodes.new("ShaderNodeMath");steps.operation="MULTIPLY"
        tree.links.new(age.outputs[0],steps.inputs[0]);steps.inputs[1].default_value=7
        integral=tree.nodes.new("ShaderNodeMath");integral.operation="FLOOR"
        tree.links.new(steps.outputs[0],integral.inputs[0])
        color=texture_node.outputs["Color"]
        for index in range(1,8):
            key="minecraft:particle/effect_"+str(7-index)
            source_material=material_for(key)
            sampler=tree.nodes.new("ShaderNodeTexImage")
            sampler.image=source_material.node_tree.nodes["OXAL Source Texture"].image
            sampler.interpolation,sampler.extension="Closest","CLIP"
            sampler[TEXTURE_KEY],sampler[ORIGINAL_PATH]=key,str(catalog.resource(key,"textures",".png"))
            choose=tree.nodes.new("ShaderNodeMath");choose.operation="GREATER_THAN"
            tree.links.new(integral.outputs[0],choose.inputs[0]);choose.inputs[1].default_value=index-.5
            for channel,previous in (("Color",color),("Alpha",alpha_source)):
                blend=tree.nodes.new("ShaderNodeMixRGB")
                tree.links.new(choose.outputs[0],blend.inputs[0]);tree.links.new(previous,blend.inputs[1]);tree.links.new(sampler.outputs[channel],blend.inputs[2])
                if channel=="Color":color=blend.outputs[0]
                else:alpha_source=blend.outputs[0]
        tree.links.new(color,tint.inputs[1])
    alpha = tree.nodes.new("ShaderNodeMath")
    alpha.operation = "MULTIPLY"
    tree.links.new(alpha_source,alpha.inputs[0])
    driver(alpha.inputs[1],"default_value",phase,root,"阶段")
    random=tree.nodes.new("ShaderNodeObjectInfo")
    density=tree.nodes.new("ShaderNodeMath");density.operation="LESS_THAN"
    tree.links.new(random.outputs["Random"],density.inputs[0])
    curve=driver(density.inputs[1],"default_value","strength*(kind>.5)" if potion else "strength")
    variables=[("strength",root,'["箭拖尾"]')]
    if potion:variables.append(("kind",root,'["箭型"]'))
    _variables(curve,variables)
    masked=tree.nodes.new("ShaderNodeMath");masked.operation="MULTIPLY"
    tree.links.new(alpha.outputs[0],masked.inputs[0]);tree.links.new(density.outputs[0],masked.inputs[1])
    shader=tree.nodes.new("ShaderNodeBsdfPrincipled")
    shader.inputs["Roughness"].default_value=1.
    tree.links.new(tint.outputs[0],shader.inputs["Base Color"])
    tree.links.new(masked.outputs[0],shader.inputs["Alpha"])
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(shader.outputs[0],output.inputs["Surface"])
    if potion:
        material["oxal_mc_studio_potion_color"] = True
    return material


def _particle_system(transaction, scene, collection, root, material_for, label, data, *, impact=False, potion=False):
    """Allocate a native emitter/settings directly, including its exact IDs."""
    texture = (data.get("textures") or [data.get("texture","effect_7")])[0]
    if not catalog.resource("minecraft:particle/"+texture,"textures",".png").is_file():
        raise ValueError("缺少原版投射物粒子纹理："+texture)
    phase = "v==2" if impact else "v==1"
    mat = _surface(transaction,root,material_for,texture,phase,potion=potion)
    prototype = mesh_object(transaction,label+" · 原生粒子原型",
        [(-.5,-.5,0),(.5,-.5,0),(.5,.5,0),(-.5,.5,0)],[(0,1,2,3)],
        [[(0,0),(1,0),(1,1),(0,1)]],[mat],[0])
    prototype["oxal_mc_studio_effect_geometry"] = True
    prototype["oxal_mc_studio_model_part"]=label+" · 原型"
    collection.objects.link(prototype)
    prototype.location=(0,0,-10000)
    prototype.hide_select=True
    from .asset_projectile_effects import billboard
    billboard(transaction,prototype,plane_rotation=0)
    emitter = mesh_object(transaction,label,
        [(-.025,-.025,0),(.025,-.025,0),(.025,.025,0),(-.025,.025,0)],[(0,1,2,3)],
        [[(0,0),(1,0),(1,1),(0,1)]],[],[0])
    collection.objects.link(emitter)
    prototype.parent = root
    emitter.parent = root
    emitter["oxal_mc_studio_effect_geometry"] = True
    emitter["oxal_mc_studio_model_part"]=label+" · 发射器"
    emitter.show_instancer_for_render = emitter.show_instancer_for_viewport = False
    # Blender's particle operator initializes native point-cache state that
    # adding a ParticleSystemModifier alone does not reproduce on timeline
    # rewind. Limit the temporary publication to this proven new emitter.
    from .asset_rebuild import object_mode
    temporary=emitter.name not in scene.objects
    if temporary:scene.collection.objects.link(emitter)
    try:
        with object_mode(),bpy.context.temp_override(object=emitter,active_object=emitter,selected_objects=[emitter],selected_editable_objects=[emitter]):
            if bpy.ops.object.particle_system_add()!={"FINISHED"}:
                raise RuntimeError("原生粒子系统初始化失败")
        particle=emitter.particle_systems[-1]
    finally:
        if temporary:scene.collection.objects.unlink(emitter)
    settings = particle.settings
    transaction.track(settings)
    settings.name = label+" · 原生设置"
    particle.seed = 0
    settings.type, settings.physics_type = "EMITTER","NEWTON"
    settings.render_type, settings.instance_object = "OBJECT",prototype
    settings.emit_from="FACE"
    settings.use_emit_random=True
    settings.frame_start=float(scene.frame_current)
    settings.frame_end=float(max(scene.frame_current+1,scene.frame_end))
    settings.count=max(1,round((settings.frame_end-settings.frame_start)*scene.render.fps_base/max(scene.render.fps,1)*data.get("rate",40)))
    settings.lifetime = max(1,float(data.get("lifetime",.6))*scene.render.fps/scene.render.fps_base)
    settings.lifetime_random = float(data.get("random_life",.5))
    settings.particle_size = float(data.get("size",.065))
    settings.size_random = .25
    settings.normal_factor = 0.
    settings.object_factor = 0.
    settings.factor_random = float(data.get("random_velocity",.12))
    cast(bpy.types.EffectorWeights,settings.effector_weights).gravity = float(data.get("gravity",0))
    settings.damping = min(1,float(data.get("damping",0)))
    settings.use_rotations=False
    settings.use_rotation_instance=True
    settings.use_scale_instance=True
    particle.point_cache.frame_start=round(settings.frame_start)
    particle.point_cache.frame_end=round(settings.frame_end)
    if impact:
        driver(settings,"frame_start","v",root,"命中帧")
        driver(settings,"frame_end","v+.01",root,"命中帧")
    root["oxal_mc_studio_native_particle_"+str(len(root.keys()))] = settings


def build_entry(transaction, scene, collection, root, identifier, options, material_for):
    short = identifier.split(":")[-1]
    if short not in profiles() or short == "trident":
        return False
    ensure_mutation_allowed()
    spec = profiles()[short]
    root["oxal_mc_studio_master_pivot"]=(0.,0.,0.)
    impact=spec.get("impact",{})
    if "lifetime_min" in impact:
        seconds=(impact["lifetime_min"]+impact["lifetime_random"]-1)/20
    elif impact.get("kind") not in {None,"none"}:
        seconds=impact.get("lifetime_numerator",4)/impact.get("lifetime_minimum",.1)/20
    else:
        seconds=2.0
    if impact.get("kind")=="gust":
        seconds += impact["emitter_lifetime"]/20
    if "cloud" in spec:
        cloud=spec["cloud"]
        seconds=(cloud["duration_ticks"]+cloud["wait_ticks"]+cloud.get("lifetime_numerator",8)/cloud.get("lifetime_minimum",.2))/20
    initial_size=.25 if short=="wind_charge" else 1.0
    radius=.75 if short=="wind_charge" else spec.get("cloud",{}).get("radius",3.0)
    controls=(("phase","阶段","阶段 · 0隐藏 1投掷 2特效 3关闭",0,3,1.0),
              ("duration","粒子时长","粒子时长",0,max(1.,seconds*4),seconds),
              ("radius","范围半径","范围半径",0,max(1.,radius*4),radius),
              ("size","粒子大小","粒子大小",0,max(1.,initial_size*4),initial_size))
    if short in _ARROWS:
        _property(root,"阶段",1.0,0,3)
    else:
        for key,prop,label,minimum,maximum,value in controls:
            register_control(root,"oxal_mc_studio_projectile_"+key,prop,label,minimum,maximum,value)
    _property(root,"命中帧",float(scene.frame_current),-1048574,1048574)
    _property(root,"随机种子",0,0,100000)
    fields=["阶段","随机种子"] if short in _ARROWS else ["随机种子"]
    if short=="firework_rocket":
        register_control(root,"oxal_mc_studio_firework_shape","烟花形状","烟花形状",0,4,0.0,("小球","大球","星形","苦力怕","爆裂"))
        for key,value in (("烟花颜色",(1.,1.,1.)),("烟花渐变色",(1.,1.,1.)),("烟花使用渐变",False),("烟花闪烁",False),("烟花拖尾",False)):
            root[key]=value
            if isinstance(value,tuple):root.id_properties_ui(key).update(subtype="COLOR",min=0.,max=1.)
            fields.append(key)
    elif short in {"splash_potion","lingering_potion"}:
        root["瞬时药水"]=False
    root["oxal_mc_studio_effect_fields"]=json.dumps(fields,ensure_ascii=False)
    root["oxal_mc_studio_projectile_schema"]=1 if short in _ARROWS else 3
    if short not in _ARROWS:
        # The original four-detent rail enters a native Simulation event at 2.
        # Keep the legacy epoch aliases driven, so explicit upgrades refuse an
        # existing Action/NLA on those formerly editable channels.
        root[EFFECT_PHASE]=1.0
        driver(root,'["'+EFFECT_PHASE+'"]',"floor(v+.5)",root,"阶段")
        root["特效时间"]=0.0
        driver(root,'["特效时间"]',"0")
        driver(root,'["命中帧"]',"0")
        root["oxal_mc_studio_projectile_batch_gate"]=2.0
        driver(root,'["oxal_mc_studio_projectile_batch_gate"]',"2")
        from .asset_projectile_clock import CLOCK
        from .asset_projectile_clock import build as build_clock
        root[CLOCK]=build_clock(transaction,scene,collection,root)
    if short in _ARROWS:
        if short != "spectral_arrow":
            register_control(root,"oxal_mc_studio_arrow_kind","箭型","箭型 · 普通 / 药水",0,1,1.0 if short=="tipped_arrow" else 0.0)
        register_control(root,"oxal_mc_studio_arrow_trail","箭拖尾","拖尾强度",0,1,1.0)
        data = catalog.read(catalog.ROOT / "native_effect/arrow_mesh.json")
        obj = mesh_object(transaction,"箭实体 · "+short,data["vertices"],data["faces"],
            [data["uv"][i:i+4]for i in range(0,len(data["uv"]),4)],[_arrow_material(transaction,root,short,material_for)],[0]*len(data["faces"]))
        collection.objects.link(obj)
        obj.parent = root
        for prop in ("hide_render","hide_viewport"):
            driver(obj,prop,"not(1<=v<=2)",root,"阶段")
        trail = catalog.read(catalog.ROOT / "native_effect/arrow_particles219.json")["profiles"]
        _particle_system(transaction,scene,collection,root,material_for,"箭 · 暴击拖尾",trail["trail"])
        if short in {"arrow","tipped_arrow"}:
            _particle_system(transaction,scene,collection,root,material_for,"箭 · 药水粒子",trail["potion"],potion=True)
    elif spec["model"] == "wind":
        material = material_for("minecraft:"+spec["texture"])
        tree = material.node_tree
        mapping = tree.nodes.new("ShaderNodeMapping")
        uv = tree.nodes.new("ShaderNodeTexCoord")
        tree.links.new(uv.outputs["UV"],mapping.inputs["Vector"])
        tree.links.new(mapping.outputs[0],tree.nodes["OXAL Source Texture"].inputs["Vector"])
        tree.nodes["OXAL Source Texture"].extension="REPEAT"
        curve=driver(mapping.inputs["Location"],"default_value",_wind_uv_expression(spec),index=0)
        _variables(curve,[("fps",scene,"render.fps"),("base",scene,"render.fps_base")])
        for box in spec["boxes"]:
            part = transaction.track(bpy.data.objects.new("风弹 · "+box["part"],None))
            collection.objects.link(part)
            part.parent = root
            obj = _entity_mesh(transaction,"风弹实体",[{"from":box["origin"],"size":box["size"],"uv":box["uv"]}],spec["atlas"],material,-1)
            collection.objects.link(obj)
            obj.parent = part
            for visibility in ("hide_render","hide_viewport"):
                driver(obj,visibility,"v!=1",root,EFFECT_PHASE)
            curve=driver(part,"rotation_euler",str(-math.radians(spec["rotation_degrees_per_tick"][box["part"]])*20)+"*frame*base/max(fps,.000001)",index=2)
            _variables(curve,[("fps",scene,"render.fps"),("base",scene,"render.fps_base")])
    else:
        from .assets import _representation
        pivot = _representation(transaction,scene,collection,root,identifier,spec.get("display_context","gui"),options,"PROJECTILE")
        pivot.matrix_basis=Matrix.Identity(4)
        for obj in tuple(pivot.children_recursive):
            if obj.type != "MESH":
                continue
            for prop in ("hide_render","hide_viewport"):
                driver(obj,prop,"v!=1",root,EFFECT_PHASE)
            if spec.get("billboard"):
                from .asset_projectile_effects import billboard
                billboard(transaction,obj)
    if impact.get("kind") not in {None,"none"}:
        from .asset_projectile_effects import build as native_effects
        native_effects(transaction,scene,collection,root,identifier,options)
    root.update_tag()
    return True


def _wind_uv_expression(spec):
    return "fmod("+str(spec["uv_scroll_per_tick"]*20)+"*frame*base/max(fps,.000001),1)"


def bridge_material(transaction,root,material,staged):
    """Upgrade only the known prior wind UV rate; retain the actual material."""
    if root.get("oxal_mc_studio_library_identifier")!="minecraft:wind_charge" or material.node_tree is None:
        return material
    if staged.get(TEXTURE_KEY)!="minecraft:entity/projectiles/wind_charge":
        return material
    from ...util.drivers import capture, restore
    from ...util.ownership import owned_ids, require_owned
    from ..enchantment import enchantment
    from .asset_rebuild import _animated_channels, _value
    from .asset_rigs import _default_mapping
    tree=material.node_tree
    source=tree.nodes.get("OXAL Source Texture")
    if source is None or len(source.inputs["Vector"].links)!=1 or source.inputs["Vector"].links[0].from_node.type!="MAPPING":
        raise ValueError("风弹材质UV输入含用户修改，未覆盖")
    mapping=source.inputs["Vector"].links[0].from_node
    path=mapping.inputs["Location"].path_from_id("default_value")
    curve=tree.animation_data.drivers.find(path,index=0) if tree.animation_data else None
    spec=profiles()["wind_charge"]
    previous=str(spec["uv_scroll_per_tick"]*20)+"*frame"
    wrapped=str(spec["uv_scroll_per_tick"]*20)+"*((frame-first)*base/max(fps,1))"
    following=_wind_uv_expression(spec)
    if curve is None or curve.driver.type!="SCRIPTED" or curve.driver.expression not in {previous,wrapped,following}:
        raise ValueError("风弹贴图滚动驱动已改变，未覆盖")
    if curve.driver.use_self or curve.mute or not _default_mapping(curve) or curve.sampled_points:
        raise ValueError("风弹贴图驱动含用户映射，未覆盖")
    for modifier in curve.modifiers:
        if not (modifier.type=="GENERATOR" and modifier.mode=="POLYNOMIAL" and modifier.poly_order==1
                and tuple(modifier.coefficients)==(0.,1.) and not modifier.use_additive and not modifier.mute
                and not modifier.use_restricted_range and (not modifier.use_influence or modifier.influence==1)
                and modifier.blend_in==modifier.blend_out==0):
            raise ValueError("风弹贴图驱动含用户修改器，未覆盖")
    scene=root.users_scene[0]
    variables={v.name:(v.type,[(t.id,t.data_path)for t in v.targets])for v in curve.driver.variables}
    expected={"fps":("SINGLE_PROP",[(scene,"render.fps")]),"base":("SINGLE_PROP",[(scene,"render.fps_base")])}
    if curve.driver.expression==following:
        if variables!=expected:
            raise ValueError("风弹贴图FPS绑定已改变，未覆盖")
        return material
    if ((curve.driver.expression==previous and variables)
            or (curve.driver.expression==wrapped and variables!={**expected,"first":("SINGLE_PROP",[(scene,"frame_start")])})
            or (path,0) in _animated_channels(tree)):
        raise ValueError("风弹贴图滚动含用户输入或Action/NLA，未覆盖")
    if material not in transaction.ids:require_owned(root,(material,))
    domain={root,*owned_ids(root),*transaction.ids}
    if any(user not in domain for user in bpy.data.user_map(subset=(material,)).get(material,set())):
        raise ValueError("风弹材质仍被其它实例使用，未覆盖")
    snapshot,properties=capture(tree),_value(dict(material.items()))
    glint=enchantment.inspect(material)
    def rollback():
        restore(tree,snapshot)
        for key in tuple(material.keys()):
            if key not in properties:del material[key]
        for key,value in properties.items():material[key]=value
    transaction.on_rollback(rollback)
    curve.driver.expression=following
    for variable in tuple(curve.driver.variables):curve.driver.variables.remove(variable)
    _variables(curve,[("fps",scene,"render.fps"),("base",scene,"render.fps_base")])
    tree.update_tag()
    if glint:enchantment.stamp(material)
    return material


def restore_control_range(target,source,pose,prior):
    """Preserve an unkeyed custom duration when correcting the old default."""
    if target.get("oxal_mc_studio_library_identifier")!="minecraft:wind_charge" or pose.name!="粒子时长":
        return
    previous=next((spec for spec in json.loads(target.get("oxal_mc_studio_state_controls","[]")) if spec["property"]=="粒子时长"),None)
    following=next((spec for spec in json.loads(source.get("oxal_mc_studio_state_controls","[]")) if spec["property"]=="粒子时长"),None)
    if not previous or not following or (previous["min"],previous["max"])==(following["min"],following["max"]):
        return
    x=float(prior["matrix"].translation.x)
    value=previous["min"]+min(1,max(0,x/.35))*(previous["max"]-previous["min"])
    if abs(value-previous["initial"])>1e-6:
        pose.location.x=.35*(value-following["min"])/(following["max"]-following["min"])


def post_compile(root, scene, options):
    if not root.get("oxal_mc_studio_projectile_schema") or root.type != "ARMATURE":
        return
    specs = json.loads(root.get("oxal_mc_studio_state_controls","[]"))
    renamed={}
    for spec in specs:
        if not spec["key"].startswith(("oxal_mc_studio_arrow_","oxal_mc_studio_projectile_","oxal_mc_studio_firework_")):
            continue
        old = "控制 · "+spec["label"]
        root.data.bones[old].name = spec["label"]
        renamed[old]=spec["label"]
        spec["bone"] = spec["label"]
    for curve in root.animation_data.drivers if root.animation_data else ():
        for variable in curve.driver.variables:
            for target in variable.targets:
                if target.id!=root:continue
                if target.bone_target in renamed:target.bone_target=renamed[target.bone_target]
                for old,new in renamed.items():
                    for ascii_only in (False,True):
                        previous='pose.bones['+json.dumps(old,ensure_ascii=ascii_only)+']'
                        replacement='pose.bones['+json.dumps(new,ensure_ascii=ascii_only)+']'
                        target.data_path=target.data_path.replace(previous,replacement)
    root["oxal_mc_studio_state_controls"] = json.dumps(specs,ensure_ascii=False)
    for field in (("烟花形状",) if root.get("oxal_mc_studio_projectile_schema")==2 else ("阶段","烟花形状")):
        curve=root.animation_data.drivers.find('["'+field+'"]') if root.animation_data else None
        if curve is not None:
            curve.driver.expression="floor(("+curve.driver.expression+")+.5)"
    heads={"箭型 · 普通 / 药水":(.5,0,.25),"拖尾强度":(.5,0,0)}
    offset=1.1 if root.get("oxal_mc_studio_library_identifier","").split(":")[-1] in _ARROWS else .5
    heads.update({"阶段 · 0隐藏 1投掷 2特效 3关闭":(offset,0,.7),"粒子时长":(offset,0,.2),"范围半径":(offset,0,-.05),"粒子大小":(offset,0,-.3)})
    with asset_rigs._edit(root,scene) as bones:
        for name,position in heads.items():
            if name not in bones:continue
            for label in (name,"标尺 · "+name):
                bone=bones[label];bone.head=Vector(position);bone.tail=bone.head+Vector((0,0,.15));bone.parent=bones["总控制"]
    root.pose.bones["总控制"].custom_shape_scale_xyz=(.3,.3,.3)
    for name in heads:
        if name not in root.pose.bones:continue
        for label in (name,"标尺 · "+name):
            root.pose.bones[label].custom_shape_rotation_euler.x=math.pi/2
    root["oxal_mc_studio_rig_driver_contract"] = asset_rigs.driver_contract(root)
    root.update_tag()


def trigger(scene,root):
    """Select the start of the native burst without authoring a frame epoch."""
    ensure_mutation_allowed()
    if root.get("oxal_mc_studio_projectile_schema")!=3:
        raise ValueError("请先显式修复为原生阶段进度投射物")
    bone=root.pose.bones.get(STAGE_BONE) if root.type=="ARMATURE" else None
    if bone is None:raise ValueError("投射物缺少已验证的阶段控件")
    from ...util.animation_edit import require_keyframe_edit, require_property_edit
    path = bone.path_from_id("location")
    if scene.tool_settings.use_keyframe_insert_auto:
        require_keyframe_edit(root, path)
    else:
        require_property_edit(root, path)
    bone.location.x=.35*2/3
    if scene.tool_settings.use_keyframe_insert_auto:
        bone.keyframe_insert("location",index=0,frame=scene.frame_current_final)
    root.update_tag()
    return {"message":"已切至特效阶段；保持阶段2并播放时间轴即可自动播放"}
