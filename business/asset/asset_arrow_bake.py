"""Explicit native-PS sampling to private, engine-independent GN playback."""

import hashlib
import json
import math
import random
from array import array
from typing import Any, cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.ownership import owns, require_owned
from ...util.resources import INSTANCE, live_ids
from .asset_geometry import Creation, driver

OBJECT="oxal_mc_studio_arrow_bake_object"
ENABLED="oxal_mc_studio_arrow_bake_enabled"
SIGNATURE="oxal_mc_studio_arrow_bake_signature"
BUNDLE="oxal_mc_studio_arrow_bake_ids"
CONTRACT="oxal_mc_studio_arrow_bake_contract"
MARK="oxal_mc_studio_arrow_bake"
MASK="OXAL Arrow Native Visibility"
MAX_RECORDS=2_000_000


def _plain(value):
    if isinstance(value,bpy.types.ID):return [cast(Any,value.bl_rna).identifier,value.get(INSTANCE,value.name)]
    if hasattr(value,"items"):return {key:_plain(item) for key,item in value.items()}
    if isinstance(value,(str,int,bool,type(None))):return value
    if isinstance(value,float):return round(value,7)
    return [_plain(item) for item in value]


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def _bindings(owner):
    animation=getattr(owner,"animation_data",None)
    if animation is None:return []
    def nested(strips):
        for strip in strips:
            yield strip
            yield from nested(getattr(strip,"strips",()))
    return [animation,*[strip for track in animation.nla_tracks for strip in nested(track.strips)]]


def _scalars(owner,exclude=()):
    return [(prop.identifier,_plain(getattr(owner,prop.identifier))) for prop in owner.bl_rna.properties
            if not prop.is_readonly and prop.type in {"BOOLEAN","INT","FLOAT","ENUM","STRING"} and prop.identifier not in {"name","select","active","show_expanded","lock",*exclude}]


def _animation(owner):
    result=[];paths=set()
    for binding in _bindings(owner):
        curves=(*action_fcurves(binding.action,slot=getattr(binding,"action_slot",None)),*tuple(getattr(binding,"fcurves",())))
        result.append([(curve.data_path,curve.array_index,curve.extrapolation,
                        [(tuple(point.co[:]),tuple(point.handle_left[:]),tuple(point.handle_right[:]),point.interpolation,point.handle_left_type,point.handle_right_type,point.easing) for point in curve.keyframe_points],
                        [_scalars(modifier) for modifier in curve.modifiers],[_plain(point.co) for point in curve.sampled_points]) for curve in curves])
        result.append(_scalars(binding,{curve.data_path.rsplit(".",1)[-1] for curve in getattr(binding,"fcurves",())}))
        paths.update(curve.data_path for curve in curves)
    animation=getattr(owner,"animation_data",None)
    if animation:
        result.append([_scalars(track) for track in animation.nla_tracks])
        for curve in animation.drivers:
            paths.add(curve.data_path)
            result.append((curve.data_path,curve.array_index,curve.driver.expression,curve.driver.use_self,[_scalars(modifier) for modifier in curve.modifiers],
                           [(variable.name,variable.type,[(_plain(target.id),target.data_path,target.bone_target,target.transform_type,target.transform_space) for target in variable.targets]) for variable in curve.driver.variables]))
    return result,paths


def _tree_record(tree):
    from ...util.node_layout import is_layout_frame

    _,animated=_animation(tree)
    nodes=[]
    for node in tree.nodes:
        if is_layout_frame(node):
            continue
        values=[]
        for index,socket in enumerate((*tuple(node.inputs),*tuple(node.outputs))):
            if hasattr(socket,"default_value") and socket.path_from_id("default_value") not in animated:
                values.append((index,_plain(socket.default_value)))
        options=[(key,_plain(getattr(node,key))) for key in ("operation","blend_type","data_type","domain","attribute_name","transform_space","rotation_type","axis","pivot_axis","invert","interpolation","extension") if hasattr(node,key)]
        image=getattr(node,"image",None)
        if image is not None:options.append(("image",[_plain(image),image.filepath_raw,image.source,tuple(image.size)]))
        nodes.append((node.name,node.bl_idname,values,options))
    return [nodes,[(link.from_node.name,link.from_socket.identifier,link.to_node.name,link.to_socket.identifier) for link in tree.links],_animation(tree)[0]]


def sources(root):
    if root.get("oxal_mc_studio_library_identifier","").split(":")[-1] not in {"arrow","tipped_arrow","spectral_arrow"}:
        raise ValueError("请选择世界中的原生箭资产")
    result=[]
    for obj in tuple(root.children_recursive):
        for system in obj.particle_systems:
            prototype=system.settings.instance_object
            if prototype is None:raise ValueError("箭粒子原型已被移除")
            require_owned(root,(obj,prototype,prototype.data,system.settings))
            mesh=cast(bpy.types.Mesh,prototype.data)
            if len(mesh.vertices)!=4 or len(mesh.polygons)!=1 or len(mesh.polygons[0].vertices)!=4:
                raise ValueError("粒子原型已改变拓扑，保留现状，未替换为方片")
            if system.settings.use_rotations or not system.settings.use_rotation_instance or not system.settings.use_scale_instance:
                raise ValueError("粒子实例旋转模式已改变，请保留原生缓存或先恢复原型约定")
            if any(slot.link!="DATA" for slot in prototype.material_slots):
                raise ValueError("粒子原型使用独立对象材质绑定，保留现状")
            if any(user not in {root,prototype} for user in bpy.data.user_map(subset=(mesh,)).get(mesh,set())):
                raise ValueError("粒子原型网格由外部对象共享，未改写材质绑定")
            if not mesh.materials or mesh.materials[0] is None:raise ValueError("粒子原型缺少材质")
            result.append((obj,system,prototype))
    if not result:raise ValueError("所选箭没有可采样的原生粒子系统")
    return result


def source_signature(root,scene):
    """Pure RNA inspection for UI; no resource decoding or scene mutation."""
    result=[scene.render.fps,scene.render.fps_base,tuple(scene.gravity)]
    for owner in (root,*[obj for obj in root.children_recursive if obj.particle_systems]):
        animation,paths=_animation(owner)
        transforms=[]
        for path in ("location","rotation_euler","rotation_quaternion","scale"):
            if path not in paths:transforms.append((path,tuple(getattr(owner,path))))
        result.append((animation,transforms,owner.parent_type,owner.parent_bone,_plain(owner.parent),_plain(owner.matrix_parent_inverse),
                       [tuple(vertex.co[:]) for vertex in owner.data.vertices] if owner.type=="MESH" else [],
                       [(_scalars(constraint),_plain(getattr(constraint,"target",None))) for constraint in owner.constraints]))
    if root.type=="ARMATURE" and "总控制" in root.pose.bones:
        pose=root.pose.bones["总控制"];_,paths=_animation(root)
        result.append([(path,tuple(getattr(pose,path))) for path in ("location","rotation_euler","rotation_quaternion","scale") if pose.path_from_id(path) not in paths])
        result.append([(_scalars(constraint),_plain(getattr(constraint,"target",None))) for constraint in pose.constraints])
    for obj in root.children_recursive:
        for system in obj.particle_systems:
            settings=system.settings;animation,paths=_animation(settings)
            values=[(prop.identifier,_plain(getattr(settings,prop.identifier))) for prop in settings.bl_rna.properties
                    if not prop.is_readonly and prop.type in {"BOOLEAN","INT","FLOAT","ENUM"} and prop.identifier not in paths]
            prototype=settings.instance_object
            result.append((system.seed,values,animation,tuple(prototype.scale),tuple(v.co[:] for v in prototype.data.vertices),
                           [_tree_record(mat.node_tree) for mat in prototype.data.materials if mat and mat.use_nodes]))
    return _digest(result)


def status(root,scene):
    if not root.get(OBJECT):return "未烘焙：Eevee逐粒贴图需完成轨迹后显式重建"
    try:
        changed=root.get(SIGNATURE)!=source_signature(root,scene)
    except (ReferenceError,AttributeError,ValueError,TypeError):
        return "源粒子数据已改变，请检查后显式重建"
    return "动画或粒子参数已改变，请重建飞行粒子" if changed else "飞行粒子已烘焙，可停用插件原生回放"


def preserved_objects(root):
    obj=root.get(OBJECT)
    if obj is None:return ()
    if not isinstance(obj,bpy.types.Object) or not owns(root,obj) or obj.parent!=root or not obj.get(MARK):
        raise ValueError("箭粒子烘焙引用无法确认归属，未重建")
    return (obj,)


def _attributes_hash(mesh):
    result=[]
    for attribute in mesh.attributes:
        if not attribute.name.startswith("oxal_arrow_"):continue
        prop="vector" if attribute.data_type=="FLOAT_VECTOR" else "value"
        values=array('f',[0.])* (len(attribute.data)*(3 if prop=="vector" else 1))
        attribute.data.foreach_get(prop,values)
        result.append((attribute.name,attribute.data_type,hashlib.sha256(values.tobytes()).hexdigest()))
    for layer in mesh.uv_layers:
        values=array('f',[0.])*(len(layer.data)*2);layer.data.foreach_get("uv",values)
        result.append(("UV",layer.name,hashlib.sha256(values.tobytes()).hexdigest()))
    result.append(("ATTRIBUTES",[(a.name,a.data_type,a.domain) for a in mesh.attributes]))
    return _digest([[(tuple(v.co)) for v in mesh.vertices],[(tuple(p.vertices),p.material_index) for p in mesh.polygons],result])


def _contract(obj):
    return _digest([_attributes_hash(obj.data),_plain(obj.matrix_basis),_plain(obj.matrix_parent_inverse),obj.parent_type,obj.parent_bone,
                    _plain({key:value for key,value in obj.items() if key not in {"oxal_mc_studio_generated_owner","oxal_mc_studio_generated_instance","oxal_mc_studio_owned_data_uid"}}),_animation(obj)[0],_animation(obj.data)[0],_plain(obj.data.shape_keys),
                    [(group.name,group.index) for group in obj.vertex_groups],[slot.link for slot in obj.material_slots],
                    [(_scalars(constraint),_plain(getattr(constraint,"target",None))) for constraint in obj.constraints],
                    [(_scalars(modifier),_tree_record(modifier.node_group) if modifier.type=="NODES" else None) for modifier in obj.modifiers]])


def _old_cache(root,scene):
    objects=preserved_objects(root)
    if not objects:return None
    obj=objects[0]
    if root.get(CONTRACT)!=_contract(obj):raise ValueError("旧烘焙网格、属性表或节点有用户修改，未覆盖")
    bundle=set(root.get(BUNDLE,{}).values());require_owned(root,bundle)
    if obj not in bundle:raise ValueError("旧烘焙登记不完整")
    attached={item.node_tree for item in bundle if isinstance(item,bpy.types.Material) and item.node_tree}
    allowed={root,scene,*bundle,*attached,*obj.users_collection}
    if any(user not in allowed for users in bpy.data.user_map(subset=tuple(bundle|attached)).values() for user in users):
        raise ValueError("旧烘焙被用户对象或材质引用，未覆盖")
    from .asset_rebuild import _explicit_scene_objects
    if obj in _explicit_scene_objects(scene):raise ValueError("场景显式引用旧烘焙，未覆盖")
    return obj


def _clone_groups(transaction,tree,memo):
    for node in tree.nodes:
        if node.type!="GROUP" or node.node_tree is None:continue
        old=node.node_tree
        if old not in memo:
            memo[old]=transaction.track(old.copy());_clone_groups(transaction,memo[old],memo)
        node.node_tree=memo[old]


def _material(transaction,root,source):
    from .assets import _private_material
    result=_private_material(transaction,source);memo={source.node_tree:result.node_tree};_clone_groups(transaction,result.node_tree,memo)
    for tree in memo.values():
        for curve in getattr(tree.animation_data,"drivers",()):
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id in memo:target.id=memo[target.id]
        gate=tree.nodes.get(MASK)
        if gate is not None:
            if gate.type!="MIX_SHADER":raise ValueError("原生粒子显示节点已被用户改动")
            gate.inputs[0].driver_remove("default_value");gate.inputs[0].default_value=0
        for node in tuple(tree.nodes):
            if node.type=="PARTICLE_INFO":
                for socket in node.outputs:
                    links=tuple(socket.links)
                    if not links:continue
                    attribute=tree.nodes.new("ShaderNodeAttribute");attribute.attribute_name="oxal_arrow_"+socket.name.lower().replace(' ','_')
                    output=attribute.outputs["Vector"] if socket.type=="VECTOR" else attribute.outputs["Fac"]
                    for link in links:tree.links.new(output,link.to_socket)
            elif node.type=="OBJECT_INFO" and node.outputs["Random"].is_linked:
                attribute=tree.nodes.new("ShaderNodeAttribute");attribute.attribute_name="oxal_arrow_random"
                for link in tuple(node.outputs["Random"].links):tree.links.new(attribute.outputs["Fac"],link.to_socket)
    return result


def _mask(transaction,root,source):
    from .assets import _private_material
    gate=source.node_tree.nodes.get(MASK)
    if gate is not None:
        if gate.type!="MIX_SHADER":raise ValueError("原生粒子显示节点已被用户改动")
        path=gate.inputs[0].path_from_id("default_value")
        curves=[curve for curve in getattr(source.node_tree.animation_data,"drivers",()) if curve.data_path==path]
        if len(curves)!=1:raise ValueError("原生粒子显隐驱动已被用户改动")
        curve=curves[0];variables=tuple(curve.driver.variables)
        from .asset_rigs import _default_mapping
        identity=all(modifier.type=="GENERATOR" and modifier.mode=="POLYNOMIAL" and modifier.poly_order==1 and tuple(modifier.coefficients)==(0.,1.)
                     and not modifier.use_additive and not modifier.mute and not modifier.use_restricted_range
                     and (not modifier.use_influence or abs(modifier.influence-1)<1e-7) and modifier.blend_in==0 and modifier.blend_out==0 for modifier in curve.modifiers)
        if (curve.driver.type!="SCRIPTED" or curve.driver.expression!="v" or curve.driver.use_self or not identity or not _default_mapping(curve) or curve.sampled_points or len(variables)!=1
                or variables[0].name!="v" or variables[0].type!="SINGLE_PROP" or variables[0].targets[0].id!=root or variables[0].targets[0].data_path!='["'+ENABLED+'"]'):
            raise ValueError("原生粒子显隐驱动已被用户改动")
        return source
    result=_private_material(transaction,source);tree=result.node_tree
    output=next((node for node in tree.nodes if node.type=="OUTPUT_MATERIAL" and node.is_active_output),None)
    if output is None or len(output.inputs['Surface'].links)!=1:raise ValueError("粒子材质输出无法安全包裹")
    shader=output.inputs['Surface'].links[0].from_socket
    gate=tree.nodes.new("ShaderNodeMixShader");gate.name=MASK
    transparent=tree.nodes.new("ShaderNodeBsdfTransparent")
    tree.links.new(shader,gate.inputs[1]);tree.links.new(transparent.outputs[0],gate.inputs[2]);tree.links.new(gate.outputs[0],output.inputs['Surface'])
    driver(gate.inputs[0],"default_value","v",root,ENABLED)
    return result


def _samples(transaction,scene,entries,start,end):
    from .asset_rebuild import object_mode
    temporary=transaction.track(bpy.data.collections.new("箭采样 · 临时原生求值"));scene.collection.children.link(temporary)
    copies=[]
    for emitter,system,prototype in entries:
        copy=transaction.track(emitter.copy());temporary.objects.link(copy)
        with object_mode(),bpy.context.temp_override(object=copy,active_object=copy,selected_objects=[copy],selected_editable_objects=[copy]):
            while copy.particle_systems:
                copy.particle_systems.active_index=len(copy.particle_systems)-1
                bpy.ops.object.particle_system_remove()
            bpy.ops.object.particle_system_add()
        sampled=copy.particle_systems[-1];default=sampled.settings;transaction.track(default)
        settings=transaction.track(system.settings.copy());sampled.settings=settings;sampled.seed=system.seed
        sampled.point_cache.frame_start=math.floor(settings.frame_start);sampled.point_cache.frame_end=max(end,math.ceil(settings.frame_end))
        copies.append((copy,prototype))
    rows=[]
    first=min(start,min(math.floor(system.settings.frame_start) for _,system,_ in entries))
    if end-first>5000:raise ValueError("粒子采样超过5000帧，请缩短时间轴范围")
    for frame in range(first,end+1):
        ensure_mutation_allowed();scene.frame_set(frame);cast(bpy.types.ViewLayer,bpy.context.view_layer).update()
        if frame<start:continue
        graph=bpy.context.evaluated_depsgraph_get()
        for mode,(obj,prototype) in enumerate(copies):
            scale=prototype.evaluated_get(graph).matrix_world.to_scale()
            for particle_index,particle in enumerate(obj.evaluated_get(graph).particle_systems[0].particles):
                if particle.alive_state!='ALIVE':continue
                rows.append((frame,mode,particle_index,tuple(particle.location),float(frame-particle.birth_time),float(particle.lifetime),float(particle.size),tuple(scale),tuple(particle.velocity),tuple(particle.angular_velocity)))
                if len(rows)>MAX_RECORDS:raise ValueError("粒子采样超过200万条，请缩短帧范围")
    retired=[*(obj for obj,_ in copies),temporary]
    for item in retired:transaction.forget(item)
    bpy.data.batch_remove(ids=live_ids(retired))
    # Settings copied solely for CPU sampling are not part of the published cache.
    for item in tuple(live_ids(transaction.ids)):
        if isinstance(item,bpy.types.ParticleSettings) and item.users==0:
            transaction.forget(item);bpy.data.particles.remove(item)
    return rows


def _table(transaction,root,collection,entries,rows,previous=None):
    mesh=transaction.track(bpy.data.meshes.new("箭飞行粒子 · 世界位置逐龄表"))
    corners=[(-.5,-.5,0),(.5,-.5,0),(.5,.5,0),(-.5,.5,0)]
    mesh.from_pydata(corners*len(rows),[],[(4*i,4*i+1,4*i+2,4*i+3) for i in range(len(rows))])
    if previous is not None:
        if len(previous.data.materials)!=len(entries):raise ValueError("旧烘焙材质槽结构已改变，保留现状")
        for material in previous.data.materials:
            require_owned(root,(material,));mesh.materials.append(material)
    else:
        for _,_,prototype in entries:mesh.materials.append(_material(transaction,root,prototype.data.materials[0]))
    uv=mesh.uv_layers.new(name="UVMap")
    for index in range(len(rows)):
        for corner,value in enumerate(((0,0),(1,0),(1,1),(0,1))):uv.data[index*4+corner].uv=value
    for polygon,row in zip(mesh.polygons,rows,strict=True):polygon.material_index=row[1]
    fields={"frame":[r[0] for r in rows],"index":[r[2] for r in rows],"location":[r[3] for r in rows],"age":[r[4] for r in rows],"lifetime":[r[5] for r in rows],"size":[r[6] for r in rows],"scale":[r[7] for r in rows],"velocity":[r[8] for r in rows],"angular_velocity":[r[9] for r in rows],"random":[random.Random((r[1]<<24)+r[2]).random() for r in rows]}
    for name,values in fields.items():
        vector=name in {'location','scale','velocity','angular_velocity'}
        attribute=mesh.attributes.new("oxal_arrow_"+name,"FLOAT_VECTOR" if vector else "FLOAT","POINT")
        flat=[component for value in values for _ in range(4) for component in (value if vector else (value,))]
        cast(Any,attribute).data.foreach_set("vector" if vector else "value",flat)
    obj=transaction.track(bpy.data.objects.new(mesh.name,mesh));collection.objects.link(obj);obj.parent=root
    obj[MARK]=True;obj['oxal_mc_studio_effect_geometry']=True;obj['oxal_mc_studio_model_part']='箭 · 已烘焙世界粒子'
    group=transaction.track(bpy.data.node_groups.new("箭飞行粒子 · 原生逐帧逐龄","GeometryNodeTree"))
    group.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry');group.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
    nodes,links=group.nodes,group.links;inp=nodes.new('NodeGroupInput');out=nodes.new('NodeGroupOutput')
    def attribute(name,vector=False):
        node=nodes.new('GeometryNodeInputNamedAttribute');node.data_type='FLOAT_VECTOR' if vector else 'FLOAT';node.inputs['Name'].default_value='oxal_arrow_'+name;return node.outputs['Attribute']
    time=nodes.new('GeometryNodeInputSceneTime');floor=nodes.new('ShaderNodeMath');floor.operation='FLOOR';links.new(time.outputs['Frame'],floor.inputs[0])
    diff=nodes.new('ShaderNodeMath');diff.operation='COMPARE';links.new(attribute('frame'),diff.inputs[0]);links.new(floor.outputs[0],diff.inputs[1]);diff.inputs[2].default_value=.1
    negate=nodes.new('FunctionNodeBooleanMath');negate.operation='NOT';links.new(diff.outputs[0],negate.inputs[0])
    delete=nodes.new('GeometryNodeDeleteGeometry');delete.domain='FACE';links.new(inp.outputs['Geometry'],delete.inputs['Geometry']);links.new(negate.outputs[0],delete.inputs['Selection'])
    position=nodes.new('GeometryNodeInputPosition');scaled=nodes.new('ShaderNodeVectorMath');scaled.operation='SCALE';links.new(position.outputs[0],scaled.inputs[0]);links.new(attribute('size'),scaled.inputs[3])
    shape=nodes.new('ShaderNodeVectorMath');shape.operation='MULTIPLY';links.new(scaled.outputs[0],shape.inputs[0]);links.new(attribute('scale',True),shape.inputs[1])
    camera=nodes.new('GeometryNodeObjectInfo');camera.transform_space='ORIGINAL';active=nodes.new('GeometryNodeInputActiveCamera');links.new(active.outputs[0],camera.inputs['Object'])
    rotate=nodes.new('ShaderNodeVectorRotate');rotate.rotation_type='EULER_XYZ';links.new(shape.outputs[0],rotate.inputs['Vector']);links.new(camera.outputs['Rotation'],rotate.inputs['Rotation'])
    world=nodes.new('ShaderNodeVectorMath');world.operation='ADD';links.new(rotate.outputs[0],world.inputs[0]);links.new(attribute('location',True),world.inputs[1])
    owner=nodes.new('GeometryNodeObjectInfo');owner.transform_space='ORIGINAL';owner.inputs['Object'].default_value=root
    inverse=nodes.new('FunctionNodeInvertMatrix');links.new(owner.outputs['Transform'],inverse.inputs['Matrix'])
    local=nodes.new('FunctionNodeTransformPoint');links.new(world.outputs[0],local.inputs['Vector']);links.new(inverse.outputs['Matrix'],local.inputs['Transform'])
    move=nodes.new('GeometryNodeSetPosition');links.new(delete.outputs['Geometry'],move.inputs['Geometry']);links.new(local.outputs['Vector'],move.inputs['Position']);links.new(move.outputs[0],out.inputs['Geometry'])
    cast(bpy.types.NodesModifier,obj.modifiers.new('已烘焙粒子原生回放','NODES')).node_group=group
    for path in ('hide_render','hide_viewport'):driver(obj,path,'not v',root,ENABLED)
    return obj


def bake(scene,root,options):
    from . import assets
    ensure_mutation_allowed();assets._require_members(root)
    entries=sources(root);old=_old_cache(root,scene)
    start=int(options.get('frame_start',scene.frame_start));end=int(options.get('frame_end',scene.frame_end))
    if end<start or end-start>5000:raise ValueError('请选择不超过5000帧的有效烘焙范围')
    collections=tuple(root.users_collection)
    if len(collections)!=1:raise ValueError('箭资产合集归属不唯一')
    collection=collections[0];require_owned(root,(collection,))
    original_frame,subframe=scene.frame_current,scene.frame_subframe
    # Preserve actual ID references in existing root metadata.
    from .asset_rebuild import _value
    props=_value(dict(root.items()))
    slots=[];old_link=None
    with Creation() as transaction:
        transaction.owner=root
        def rollback():
            for mesh,index,material in slots:mesh.materials[index]=material
            if old is not None and old_link is not None:
                collection.objects.link(old);old.parent=root;old.matrix_parent_inverse,old.matrix_basis=old_link
            for key in tuple(root.keys()):
                if key not in props:del root[key]
            for key,value in props.items():root[key]=value
            root.update_tag()
        transaction.on_rollback(rollback)
        try:
            rows=_samples(transaction,scene,entries,start,end)
        finally:
            scene.frame_set(original_frame,subframe=subframe);cast(bpy.types.ViewLayer,bpy.context.view_layer).update()
        obj=_table(transaction,root,collection,entries,rows,old)
        bundle=tuple(item for item in transaction.ids if isinstance(item,(bpy.types.Object,bpy.types.Mesh,bpy.types.GeometryNodeTree)))
        masks={}
        for _,_,prototype in entries:
            mesh=cast(bpy.types.Mesh,prototype.data);source=mesh.materials[0]
            if source not in masks:masks[source]=_mask(transaction,root,source)
            slots.append((mesh,0,source));mesh.materials[0]=masks[source]
        root[OBJECT]=obj;root[ENABLED]=True;root[BUNDLE]={str(i):item for i,item in enumerate(bundle)}
        root['oxal_mc_studio_arrow_bake_range']=[start,end];root['oxal_mc_studio_arrow_bake_records']=len(rows)
        if old is not None:
            old_link=(old.matrix_parent_inverse.copy(),old.matrix_basis.copy());old.parent=None;collection.objects.unlink(old)
        root.update_tag();cast(bpy.types.ViewLayer,bpy.context.view_layer).update()
        root[SIGNATURE]=source_signature(root,scene);root[CONTRACT]=_contract(obj)
        assets._stamp(transaction,root);transaction.seal()
        if old is not None:bpy.data.objects.remove(old,do_unlink=True)
        transaction.commit()
    return {'message':f'已烘焙箭飞行粒子：{start}–{end}帧，{len(rows)}条逐龄采样'}
