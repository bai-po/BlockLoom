"""Native display-tick smoke from the pinned brewing particle parameters.

Random births and initial conditions are allocated once. Geometry Nodes reads
the fixed cohort and integrates friction/gravity analytically, including the
fractional last tick. No Python executes on frame or render callbacks.
"""

import hashlib
import math
import random
from array import array
from bisect import bisect_left
from typing import Any, cast

import bpy
from bpy.types import Mesh
from mathutils import Vector

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import require_owned
from ...util.resources import resolve_view_layer
from . import asset_catalog as catalog
from . import asset_effects as effects
from . import asset_states
from .asset_geometry import Creation, mesh_object

PREFIX = "oxal_brewing_native_"
META = "oxal_mc_studio_brewing42_"
CARRIER = META+"carrier"
CLOCK = META+"clock"


def _smoke_signature(root):
    from .asset_torch_particles import clock_signature
    signature = clock_signature(root,"烟雾开关")
    if not signature:
        return ""
    animation = root.animation_data
    state = signature+str((animation.use_nla,animation.use_tweak_mode))
    return hashlib.sha256(state.encode()).hexdigest()


def _birth_gate(scene,root,settings,rows,samples):
    if settings["smoke_signature"]!=_smoke_signature(root):
        raise ValueError("烟雾开关动画在提交后改变，请重新提交原生粒子重建")
    births = {row[1] for row in rows}
    if not settings["smoke_signature"]:
        return dict.fromkeys(births,float(settings["smoke_enabled"]))
    previous = scene.frame_current,scene.frame_subframe
    result = {}
    local_frames = [local for _,local in samples]
    try:
        layer = resolve_view_layer(scene)
        for tick in sorted(births):
            local = settings["start"]+tick*settings["fps"]/settings["fps_base"]/20
            index = bisect_left(local_frames,local)
            if local<=settings["start"]:
                frame = local
            elif index>=len(samples):
                frame = settings["end"]+1
            elif index==0:
                frame = samples[0][0]
            else:
                a,b = samples[index-1],samples[index]
                frame = a[0]+(local-a[1])/(b[1]-a[1])*(b[0]-a[0])
            if not -1048574<=frame<=1048574:
                raise ValueError("烟雾出生采样超出Blender原生帧范围，请收窄显式时间窗")
            whole = math.floor(frame)
            scene.frame_set(whole,subframe=frame-whole)
            layer.update()
            result[tick] = float(bool(root.evaluated_get(layer.depsgraph).get("oxal_mc_studio_brewing_smoke",True)))
    finally:
        scene.frame_set(previous[0],subframe=previous[1])
    return result


def _rows(spec,seed,first_tick,last_tick):
    result = []
    for tick in range(first_tick,last_tick+1):
        # Each display tick is its own deterministic source stream. Keeping the
        # Bernoulli draw order also makes an explicit seed comparable to 196.
        rng = random.Random((seed<<32)^(tick&0xffffffff))
        count = 0
        for radius in spec["displayTickRadii"]:
            probability = 1/radius**3
            count += sum(rng.random()<probability for _ in range(spec["displayTickSamples"]))
        for _ in range(count):
            game = [start+rng.random()*span for start,span in zip(spec["spawnGameMin"],spec["spawnGameSpan"],strict=True)]
            spawn = (game[0]-.5,.5-game[2],game[1])
            velocity = Vector([(rng.random()*2-1)*spec["velocityRandomRange"] for _ in range(3)])
            velocity.normalize()
            velocity *= (rng.random()+rng.random()+1)*spec["velocityMagnitudeBase"]*spec["velocityMagnitudeScale"]
            velocity.y += spec["initialVerticalBias"]
            velocity *= spec["velocityScale"]
            velocity = (velocity.x,-velocity.z,velocity.y)
            size = spec["quadBase"]*(rng.random()*spec["quadRandomScale"]+.5)*2*spec["quadScale"]
            gray = rng.random()*spec["grayRandomScale"]
            life = max(1,int(spec["baseLifetimeTicks"]/(rng.random()*spec["lifetimeRandomScale"]+spec["lifetimeRandomOffset"])))
            result.append((spawn,tick,velocity,size,gray,life))
            if len(result)>262144:
                raise ValueError("原生烟雾潜在样本超过262144，请明确缩短缓存时间窗")
    return result


def request(scene,root,options):
    from .asset_torch_particles import clock_signature
    explicit = options.get("particles_window",{})
    if not isinstance(explicit,dict):
        raise TypeError("粒子时间窗快照无效")
    default_seed = int(str(root.get("oxal_mc_studio_instance_id","196"))[-8:],16)&0x7fffffff
    values = {"start":scene.frame_start,"end":scene.frame_end,"fps":scene.render.fps,
              "fps_base":scene.render.fps_base,"seed":root.get("粒子种子",default_seed),
              "smoke_enabled":float(bool(root.get("oxal_mc_studio_brewing_smoke",True))),
              "smoke_signature":_smoke_signature(root),"rate":root.get("粒子播放倍率",1.0),
              "rate_signature":clock_signature(root)}
    values.update(explicit)
    if set(values)!={"start","end","fps","fps_base","seed","smoke_enabled","smoke_signature","rate","rate_signature"}:
        raise ValueError("粒子时间窗含未知字段")
    if any(not isinstance(values[key],str) for key in ("smoke_signature","rate_signature")):
        raise TypeError("烟雾开关动画快照无效")
    if any(isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) for key,value in values.items() if not key.endswith("signature")):
        raise ValueError("粒子时间窗必须为有限数值")
    if any(not isinstance(values[key],int) for key in ("start","end","fps","seed")):
        raise TypeError("粒子帧范围/FPS/种子必须为整数")
    if (not -1048574<=values["start"]<=values["end"]<=1048574 or values["end"]-values["start"]>4096 or values["fps"]<=0 or values["fps_base"]<=0
            or not 0<=values["seed"]<=0x7fffffff or values["smoke_enabled"] not in (0,1) or not 0<=values["rate"]<=8):
        raise ValueError("粒子时间窗/FPS/种子超出有效范围")
    return values


def _fingerprint(mesh):
    if len(mesh.polygons) or len(mesh.edges):
        raise ValueError("烟雾点载体有用户新增边面，未覆盖")
    digest = hashlib.sha256()
    for attribute in sorted(mesh.attributes,key=lambda item:item.name):
        if attribute.name.startswith(".") or not len(attribute.data):
            continue
        if attribute.name!="position" and not attribute.name.startswith(PREFIX):
            raise ValueError("烟雾载体有用户新增属性，未覆盖")
        vector = attribute.data_type=="FLOAT_VECTOR"
        values = array("f",[0])*(len(attribute.data)*(3 if vector else 1))
        attribute.data.foreach_get("vector" if vector else "value",values)
        digest.update(attribute.name.encode("utf8")+values.tobytes())
    return digest.hexdigest()


def _mesh(tx,settings,spec,scene,root):
    from .asset_torch_particles import sample_clock
    samples,_ = sample_clock(scene,root,settings["start"],settings["end"],settings["fps"],settings["fps_base"],constant=settings["rate"],expected_signature=settings["rate_signature"])
    history = math.ceil(spec["baseLifetimeTicks"]/spec["lifetimeRandomOffset"])
    ticks = math.ceil((samples[-1][1]-settings["start"])*spec["ticksPerSecond"]*settings["fps_base"]/settings["fps"])
    rows = _rows(spec,int(settings["seed"]),-history,ticks)
    gates = _birth_gate(scene,root,settings,rows,samples)
    mesh = tx.track(bpy.data.meshes.new("酿造台 · 原生显示tick载体"))
    mesh.from_pydata([row[0] for row in rows],[],[])
    for key,index,kind in (("birth",1,"FLOAT"),("velocity",2,"FLOAT_VECTOR"),("size",3,"FLOAT"),("gray",4,"FLOAT"),("life",5,"FLOAT")):
        record = mesh.attributes.new(PREFIX+key,kind,"POINT")
        record.data.foreach_set("vector" if kind=="FLOAT_VECTOR" else "value",
                                [component for row in rows for component in row[index]] if kind=="FLOAT_VECTOR" else [row[index] for row in rows])
    for key,value in settings.items():
        mesh[META+key] = value
        if key.endswith("signature"):
            continue
        record = mesh.attributes.new(PREFIX+key,"FLOAT","POINT")
        record.data.foreach_set("value",[value]*len(rows))
    mask = mesh.attributes.new(PREFIX+"birth_mask","FLOAT","POINT")
    mask.data.foreach_set("value",[gates[row[1]] for row in rows])
    mesh[META+"schema"] = 1
    mesh[META+"fingerprint"] = _fingerprint(mesh)
    clock = tx.track(bpy.data.meshes.new("酿造台 · 原生倍率积分时间表"))
    clock.from_pydata([(0,0,0)]*len(samples),[],[])
    record = clock.attributes.new(PREFIX+"clock_tick","FLOAT","POINT")
    record.data.foreach_set("value",[(local-settings["start"])*spec["ticksPerSecond"]*settings["fps_base"]/settings["fps"] for _,local in samples])
    clock[META+"fingerprint"] = _fingerprint(clock)
    return mesh,clock


def _carrier(root):
    carrier = root.get(CARRIER)
    if not isinstance(carrier,bpy.types.Object) or not isinstance(carrier.data,Mesh):
        raise TypeError("原生烟雾载体已删除，请显式修复实例")
    require_owned(root,(root,carrier,carrier.data))
    return carrier


def pending(scene,root):
    carrier = root.get(CARRIER)
    if not isinstance(carrier,bpy.types.Object) or not isinstance(carrier.data,Mesh):
        return True
    current = request(scene,root,{})
    return any(carrier.data.get(META+key)!=value for key,value in current.items()
               if (key!="smoke_enabled" or not current["smoke_signature"]) and (key!="rate" or not current["rate_signature"])) or not current["start"]<=scene.frame_current<=current["end"]


def rebuild(scene,root,options):
    ensure_mutation_allowed()
    from .asset_contracts import PROPERTY, stamp, validate
    validate(root)
    carrier = _carrier(root)
    clock = root.get(CLOCK)
    if not isinstance(clock,bpy.types.Object) or not isinstance(clock.data,Mesh):
        raise TypeError("原生烟雾时间表已删除，请显式修复实例")
    require_owned(root,(clock,clock.data))
    old = cast(Mesh,carrier.data)
    old_clock = clock.data
    if old_clock.get(META+"fingerprint")!=_fingerprint(old_clock):
        raise ValueError("烟雾时间表已由用户修改，未覆盖")
    if old_clock.shape_keys or old_clock.animation_data:
        raise ValueError("烟雾时间表含用户动画，未覆盖")
    if any(owner not in {root,clock} for owner in bpy.data.user_map(subset=(old_clock,)).get(old_clock,set())):
        raise ValueError("烟雾时间表被外部引用，未覆盖")
    if old.shape_keys is not None or (old.animation_data and (old.animation_data.action or old.animation_data.nla_tracks)):
        raise ValueError("烟雾载体含用户动画/形态键，未覆盖")
    if old.get(META+"fingerprint")!=_fingerprint(old):
        raise ValueError("烟雾载体几何已由用户修改，未覆盖")
    if any(owner not in {root,carrier} for owner in bpy.data.user_map(subset=(old,)).get(old,set())):
        raise ValueError("烟雾载体已被外部引用，未覆盖")
    settings = request(scene,root,options)
    old_contract = root.get(PROPERTY)
    old_refs = dict(root.get("oxal_mc_studio_contract_refs",{}).items())
    old_seed = root.get("粒子种子")
    with Creation() as tx:
        tx.owner = root
        following,next_clock = _mesh(tx,settings,catalog.read(catalog.ROOT/"native_effect/brewing_smoke42.json"),scene,root)
        def rollback():
            carrier.data = old
            clock.data = old_clock
            root["粒子种子"] = old_seed
            root[PROPERTY] = old_contract
            root["oxal_mc_studio_contract_refs"] = old_refs
        tx.on_rollback(rollback)
        carrier.data = following
        clock.data = next_clock
        root["粒子种子"] = int(settings["seed"])
        stamp(root)
        tx.commit()
    root.update_tag()
    return {"message":"原生酿造烟雾缓存已重建","instance_id":root.get("oxal_mc_studio_instance_id","")}


def _material(tx,root,material_for,keys):
    material = tx.track(material_for(keys[0]).copy())
    material.name = "酿造台 · 原版灰度烟雾"
    material.surface_render_method = "DITHERED"
    tree = cast(Any,material.node_tree)
    shader = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    texture = tree.nodes["OXAL Source Texture"]
    color,alpha = texture.outputs["Color"],texture.outputs["Alpha"]
    age = tree.nodes.new("ShaderNodeAttribute")
    age.attribute_name = PREFIX+"frame"
    for index,key in enumerate(keys[1:],1):
        texture = tree.nodes.new("ShaderNodeTexImage")
        texture.image = material_for(key).node_tree.nodes["OXAL Source Texture"].image
        texture.interpolation = "Closest"
        texture["oxal_mc_studio_texture_key"] = key
        threshold = tree.nodes.new("ShaderNodeMath")
        threshold.operation = "GREATER_THAN"
        threshold.inputs[1].default_value = index-.5
        tree.links.new(age.outputs["Fac"],threshold.inputs[0])
        combined = []
        for previous,current in ((color,texture.outputs["Color"]),(alpha,texture.outputs["Alpha"])):
            mix = tree.nodes.new("ShaderNodeMixRGB")
            tree.links.new(threshold.outputs[0],mix.inputs[0])
            tree.links.new(previous,mix.inputs[1])
            tree.links.new(current,mix.inputs[2])
            combined.append(mix.outputs[0])
        color,alpha = combined
    gray = tree.nodes.new("ShaderNodeAttribute")
    gray.attribute_name = PREFIX+"gray"
    tint = tree.nodes.new("ShaderNodeMixRGB")
    tint.name = "OXAL 原版烟雾灰度"
    tint.blend_type = "MULTIPLY"
    tint.inputs[0].default_value = 1
    tree.links.new(color,tint.inputs[1])
    tree.links.new(gray.outputs["Color"],tint.inputs[2])
    tree.links.new(tint.outputs[0],shader.inputs["Base Color"])
    tree.links.new(alpha,shader.inputs["Alpha"])
    return material


def build(tx,scene,collection,root,options,material_for):
    if "粒子播放倍率" not in root:
        asset_states.register_control(root,META+"rate_control","粒子播放倍率","粒子播放倍率",0,8,1)
    spec = catalog.read(catalog.ROOT/"native_effect/brewing_smoke42.json")
    settings = request(scene,root,options)
    source = options.get("_particles_source_root",root)
    if source != root:
        if not isinstance(source,bpy.types.Object):
            raise TypeError("粒子动画采样目标必须为实际实例")
        require_owned(source,(source,))
        if tuple(source.users_scene)!=(scene,) or source.get("oxal_mc_studio_library_identifier")!="minecraft:brewing_stand":
            raise ValueError("粒子动画采样目标归属不符")
    root["粒子种子"] = int(settings["seed"])
    root.id_properties_ui("粒子种子").update(min=0,max=0x7fffffff)
    root["oxal_mc_studio_brewing_native_schema"] = 1
    mesh,clock_mesh = _mesh(tx,settings,spec,scene,source)
    obj = tx.track(bpy.data.objects.new(mesh.name,mesh))
    collection.objects.link(obj)
    obj.parent = root
    obj["oxal_mc_studio_effect_geometry"] = True
    obj["oxal_mc_studio_brewing_smoke"] = True
    root[CARRIER] = obj
    clock_obj = tx.track(bpy.data.objects.new(clock_mesh.name,clock_mesh))
    collection.objects.link(clock_obj)
    clock_obj.parent = root
    clock_obj.hide_render = clock_obj.hide_viewport = clock_obj.hide_select = True
    clock_obj["oxal_mc_studio_effect_geometry"] = True
    clock_obj["oxal_mc_studio_model_part"] = "brewing_smoke_clock"
    root[CLOCK] = clock_obj
    group = tx.track(bpy.data.node_groups.new("酿造台 · 原生逐tick烟雾","GeometryNodeTree"))
    group.interface.new_socket(name="Geometry",in_out="INPUT",socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry",in_out="OUTPUT",socket_type="NodeSocketGeometry")
    obj.modifiers.new("原版烟雾 · 无回调", "NODES").node_group = group
    nodes,links = group.nodes,group.links
    inp,out = nodes.new("NodeGroupInput"),nodes.new("NodeGroupOutput")
    def attribute(key,kind="FLOAT"):
        node = nodes.new("GeometryNodeInputNamedAttribute")
        node.data_type = kind
        node.inputs["Name"].default_value = PREFIX+key
        return node.outputs["Attribute"]
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
    clock = nodes.new("GeometryNodeInputSceneTime")
    clock_info = nodes.new("GeometryNodeObjectInfo")
    clock_info.transform_space = "ORIGINAL"
    clock_info.inputs["Object"].default_value = clock_obj
    index = math_node("MINIMUM",math_node("MAXIMUM",math_node("SUBTRACT",clock.outputs["Frame"],attribute("start")),0),math_node("SUBTRACT",attribute("end"),attribute("start")))
    def sample(where):
        node = nodes.new("GeometryNodeSampleIndex")
        node.data_type,node.domain = "FLOAT","POINT"
        links.new(clock_info.outputs["Geometry"],node.inputs["Geometry"])
        links.new(attribute("clock_tick"),node.inputs["Value"])
        links.new(where,node.inputs["Index"])
        return node.outputs["Value"]
    lower = sample(math_node("FLOOR",index))
    upper = sample(math_node("MINIMUM",math_node("ADD",math_node("FLOOR",index),1),math_node("SUBTRACT",attribute("end"),attribute("start"))))
    tick = math_node("ADD",lower,math_node("MULTIPLY",math_node("FRACT",index),math_node("SUBTRACT",upper,lower)))
    raw_age = math_node("SUBTRACT",tick,attribute("birth"))
    age = math_node("MINIMUM",math_node("MAXIMUM",raw_age,0),attribute("life"))
    whole = math_node("FLOOR",age)
    fraction = math_node("FRACT",age)
    friction = spec["friction"]
    power = math_node("POWER",friction,whole)
    geometric = math_node("DIVIDE",math_node("SUBTRACT",1,power),1-friction)
    travel = math_node("ADD",geometric,math_node("MULTIPLY",fraction,power))
    vector = nodes.new("ShaderNodeVectorMath")
    vector.operation = "SCALE"
    links.new(attribute("velocity","FLOAT_VECTOR"),vector.inputs[0])
    links.new(travel,vector.inputs["Scale"])
    # v <- v + gravity; p <- p + v; v <- v*friction, followed by
    # interpolation of the fractional last tick, exactly as the source data.
    gravity_whole = math_node("DIVIDE",math_node("SUBTRACT",whole,math_node("MULTIPLY",friction,geometric)),1-friction)
    gravity_fraction = math_node("MULTIPLY",fraction,math_node("ADD",1,math_node("MULTIPLY",friction,geometric)))
    vertical = math_node("MULTIPLY",math_node("ADD",gravity_whole,gravity_fraction),-spec["gravityStep"]*spec["gravity"])
    gravity = nodes.new("ShaderNodeCombineXYZ")
    links.new(vertical,gravity.inputs["Z"])
    offset = nodes.new("ShaderNodeVectorMath")
    offset.operation = "ADD"
    links.new(vector.outputs["Vector"],offset.inputs[0])
    links.new(gravity.outputs["Vector"],offset.inputs[1])
    position = nodes.new("GeometryNodeSetPosition")
    links.new(inp.outputs["Geometry"],position.inputs["Geometry"])
    links.new(offset.outputs["Vector"],position.inputs["Offset"])
    keys = ["minecraft:particle/"+key.split(":")[-1] for key in catalog.read(catalog.resource("minecraft:"+spec["particle"],"particles"))["textures"]]
    sprite_frame = math_node("MINIMUM",math_node("FLOOR",math_node("MULTIPLY",math_node("DIVIDE",age,attribute("life")),len(keys)-1)),len(keys)-1)
    stored = nodes.new("GeometryNodeStoreNamedAttribute")
    stored.domain,stored.data_type = "POINT","FLOAT"
    stored.inputs["Name"].default_value = PREFIX+"frame"
    links.new(position.outputs["Geometry"],stored.inputs["Geometry"])
    links.new(sprite_frame,stored.inputs["Value"])
    material = _material(tx,root,material_for,keys)
    uv_node = cast(Any,material.node_tree).nodes.new("ShaderNodeUVMap")
    uv_node.uv_map = "UVMap"
    for texture in cast(Any,material.node_tree).nodes:
        if texture.type == "TEX_IMAGE":
            cast(Any,material.node_tree).links.new(uv_node.outputs["UV"],texture.inputs["Vector"])
    prototype = mesh_object(tx,"酿造台 · 原生烟雾精灵",[(-1,-1,0),(1,-1,0),(1,1,0),(-1,1,0)],
                            [(0,1,2,3)],[[(0,0),(1,0),(1,1),(0,1)]],[material],[0])
    collection.objects.link(prototype)
    prototype.parent = root
    prototype.hide_render = True
    prototype.hide_viewport = True
    prototype.hide_select = True
    prototype["oxal_mc_studio_effect_geometry"] = True
    prototype["oxal_mc_studio_model_part"] = "brewing_smoke_sprite"
    obj["oxal_mc_studio_model_part"] = "brewing_smoke_emitter"
    sprite = nodes.new("GeometryNodeObjectInfo")
    sprite.transform_space = "ORIGINAL"
    sprite.inputs["Object"].default_value = prototype
    camera = nodes.new("GeometryNodeInputActiveCamera")
    camera_info = nodes.new("GeometryNodeObjectInfo")
    camera_info.transform_space = "RELATIVE"
    links.new(camera.outputs["Active Camera"],camera_info.inputs["Object"])
    enabled = nodes.new("ShaderNodeValue")
    effects.native(enabled.outputs[0],"default_value","v",root,[("v","oxal_mc_studio_brewing_smoke")])
    live = math_node("MULTIPLY",math_node("LESS_THAN",raw_age,attribute("life")),math_node("GREATER_THAN",raw_age,-.000001))
    in_window = math_node("MULTIPLY",math_node("GREATER_THAN",clock.outputs["Frame"],math_node("SUBTRACT",attribute("start"),.0001)),math_node("LESS_THAN",clock.outputs["Frame"],math_node("ADD",attribute("end"),.0001)))
    live = math_node("MULTIPLY",math_node("MULTIPLY",live,in_window),enabled.outputs[0])
    live = math_node("MULTIPLY",live,attribute("birth_mask"))
    scale = math_node("MULTIPLY",attribute("size"),math_node("MINIMUM",1,math_node("MULTIPLY",math_node("DIVIDE",age,attribute("life")),spec["growthFactor"])))
    instances = nodes.new("GeometryNodeInstanceOnPoints")
    links.new(stored.outputs["Geometry"],instances.inputs["Points"])
    links.new(live,instances.inputs["Selection"])
    links.new(sprite.outputs["Geometry"],instances.inputs["Instance"])
    links.new(camera_info.outputs["Rotation"],instances.inputs["Rotation"])
    links.new(scale,instances.inputs["Scale"])
    realized = nodes.new("GeometryNodeRealizeInstances")
    links.new(instances.outputs["Instances"],realized.inputs["Geometry"])
    links.new(realized.outputs["Geometry"],out.inputs["Geometry"])
