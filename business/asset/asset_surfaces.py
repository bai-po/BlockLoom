"""Declared coplanar front/back faces become one native two-sided surface."""
from typing import cast

import bpy
from mathutils import Vector

BACK_UV='OXAL Back UV'
LAYERED='oxal_mc_studio_layered_surface'
LAYER_KEYS='oxal_mc_studio_surface_layers'
LAYER_SOURCES='oxal_mc_studio_surface_layer_sources'
SUSPENDED='oxal_mc_studio_surface_layers_suspended'

def _coordinates(tree,source):
    """Replace implicit surface UV before any existing animation transform."""
    nodes=tuple(tree.nodes)
    for node in nodes:
        if node.type=='TEX_COORD':
            for link in tuple(node.outputs['UV'].links):tree.links.new(source,link.to_socket)
        elif node.type=='TEX_IMAGE' and not node.inputs['Vector'].is_linked:
            tree.links.new(source,node.inputs['Vector'])


def _selected_uv(tree):
    front=tree.nodes.new('ShaderNodeUVMap');front.uv_map='UVMap'
    back=tree.nodes.new('ShaderNodeUVMap');back.uv_map=BACK_UV
    geometry=tree.nodes.new('ShaderNodeNewGeometry')
    choose=tree.nodes.new('ShaderNodeMixRGB');choose.name='OXAL Native Front Back UV'
    tree.links.new(geometry.outputs['Backfacing'],choose.inputs[0]);tree.links.new(front.outputs[0],choose.inputs[1]);tree.links.new(back.outputs[0],choose.inputs[2])
    return choose.outputs[0]


def _opacity(tree, socket):
    """Coverage of the generated source shaders, without sampling images twice."""
    node=socket.node
    if node.type=='BSDF_PRINCIPLED':
        value=node.inputs['Alpha']
        return value.links[0].from_socket if value.is_linked else float(value.default_value)
    if node.type=='BSDF_TRANSPARENT':return 0.0
    if node.type=='GROUP' and 'Alpha' in node.outputs:
        return node.outputs['Alpha']
    if node.type=='MIX_SHADER':
        factor=node.inputs[0]
        weight=factor.links[0].from_socket if factor.is_linked else float(factor.default_value)
        values=[_opacity(tree,node.inputs[i].links[0].from_socket) for i in (1,2)]
        mix=tree.nodes.new('ShaderNodeMixRGB');mix.name='OXAL Surface Coverage'
        for index,value in enumerate((weight,*values)):
            if isinstance(value,float):
                mix.inputs[index].default_value=value if index==0 else (value,value,value,1)
            else:tree.links.new(value,mix.inputs[index])
        return mix.outputs[0]
    raise ValueError('叠层表面缺少可核验的原生透明度入口，未猜测混合')


def _opaque(tx,tree):
    """Private overlay closures: the outer Mix Shader applies alpha once."""
    for node in tree.nodes:
        if node.type=='BSDF_PRINCIPLED':
            for link in tuple(node.inputs['Alpha'].links):tree.links.remove(link)
            node.inputs['Alpha'].default_value=1
        elif node.type=='GROUP' and node.node_tree and any(s.type=='SHADER' for s in node.outputs):
            node.node_tree=tx.track(node.node_tree.copy());_opaque(tx,node.node_tree)


def _side_group(tx,material,uv_name,*,opaque=False):
    original=material.node_tree
    if original.animation_data and (original.animation_data.action or original.animation_data.nla_tracks):
        raise ValueError('新建正反材质的源模板含未知Action，未猜测迁移')
    group=tx.track(bpy.data.node_groups.new('OXAL surface '+uv_name,'ShaderNodeTree'))
    mapping={}
    for node in original.nodes:
        copied=group.nodes.new(node.bl_idname);copied.name=node.name;mapping[node]=copied
        for prop in node.bl_rna.properties:
            if prop.is_readonly or prop.identifier in {'rna_type','name','parent','location'} or prop.type not in {'BOOLEAN','INT','FLOAT','ENUM','STRING','POINTER'}:continue
            setattr(copied,prop.identifier,getattr(node,prop.identifier))
        for key,value in node.items():copied[key]=value
        for before,after in zip(node.inputs,copied.inputs,strict=True):
            if hasattr(before,'default_value'):after.default_value=before.default_value
        for before,after in zip(node.outputs,copied.outputs,strict=True):
            if hasattr(before,'default_value'):after.default_value=before.default_value
    for link in original.links:
        group.links.new(mapping[link.from_node].outputs[list(link.from_node.outputs).index(link.from_socket)],mapping[link.to_node].inputs[list(link.to_node.inputs).index(link.to_socket)])
    from .asset_rigs import driver_records, install_drivers
    install_drivers(group,driver_records(original),remap=original)
    for node in group.nodes:
        if node.type=='TEX_IMAGE' and node.name=='OXAL Source Texture':
            for key in ('oxal_mc_studio_texture_key','oxal_mc_studio_original_texture','oxal_mc_studio_original_image','oxal_mc_studio_original_animation'):
                if key in material:node[key]=material[key]
    # The material output is replaced with an explicit shader group interface.
    output=next(n for n in group.nodes if n.type=='OUTPUT_MATERIAL' and n.is_active_output)
    if len(output.inputs['Surface'].links)!=1:raise ValueError('源正反材质缺少唯一表面输出')
    source=output.inputs['Surface'].links[0].from_socket
    opacity=_opacity(group,source)
    group.interface.new_socket(name='Surface',in_out='OUTPUT',socket_type='NodeSocketShader')
    group.interface.new_socket(name='Alpha',in_out='OUTPUT',socket_type='NodeSocketFloat')
    group.interface.new_socket(name='Layer Alpha',in_out='OUTPUT',socket_type='NodeSocketFloat')
    target=group.nodes.new('NodeGroupOutput');group.links.new(source,target.inputs[0])
    for name in ('Alpha','Layer Alpha'):
        if isinstance(opacity,float):target.inputs[name].default_value=opacity
        else:group.links.new(opacity,target.inputs[name])
    if opaque:
        _opaque(tx,group)
        for link in tuple(target.inputs['Alpha'].links):group.links.remove(link)
        target.inputs['Alpha'].default_value=1
    for node in tuple(group.nodes):
        if node.type=='OUTPUT_MATERIAL':group.nodes.remove(node)
    uv=group.nodes.new('ShaderNodeUVMap');uv.uv_map=uv_name;_coordinates(group,uv.outputs[0])
    if group.animation_data:
        for curve in group.animation_data.drivers:
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id==material.node_tree:target.id=group
    return group


def _material(tx,front,back):
    if (front,back) in tx.surface_materials:return tx.surface_materials[front,back]
    from .assets import _private_material
    result=_private_material(tx,front);result.use_backface_culling=False
    result['oxal_mc_studio_two_sided_surface']='SAME' if front==back else 'SPLIT'
    result['oxal_mc_studio_surface_back_texture']=back.get('oxal_mc_studio_texture_key','')
    tx.surface_materials[front,back]=result
    tree=result.node_tree
    if front==back:
        _coordinates(tree,_selected_uv(tree))
        return result
    # Retain each source shader and its canonical samplers independently.
    tree.animation_data_clear();tree.nodes.clear()
    geometry=tree.nodes.new('ShaderNodeNewGeometry');mix=tree.nodes.new('ShaderNodeMixShader');out=tree.nodes.new('ShaderNodeOutputMaterial')
    tree.links.new(geometry.outputs['Backfacing'],mix.inputs[0])
    for index,(material,layer) in enumerate(((front,'UVMap'),(back,BACK_UV)),1):
        node=tree.nodes.new('ShaderNodeGroup');node.node_tree=_side_group(tx,material,layer);tree.links.new(node.outputs[0],mix.inputs[index])
    tree.links.new(mix.outputs[0],out.inputs['Surface'])
    return result


def _layer_material(tx,base,overlay):
    if base==overlay:return base
    key=('LAYER',base,overlay)
    if key in tx.surface_materials:return tx.surface_materials[key]
    from .assets import _private_material
    result=_private_material(tx,base);tree=result.node_tree
    tree.animation_data_clear();tree.nodes.clear()
    bottom=tree.nodes.new('ShaderNodeGroup');bottom.name='OXAL Coplanar Base';bottom.node_tree=_side_group(tx,base,'UVMap')
    top=tree.nodes.new('ShaderNodeGroup');top.name='OXAL Coplanar Overlay';top.node_tree=_side_group(tx,overlay,'UVMap',opaque=True)
    mix=tree.nodes.new('ShaderNodeMixShader');mix.name='OXAL Coplanar Alpha Over'
    tree.links.new(top.outputs['Layer Alpha'],mix.inputs[0])
    tree.links.new(bottom.outputs['Surface'],mix.inputs[1]);tree.links.new(top.outputs['Surface'],mix.inputs[2])
    out=tree.nodes.new('ShaderNodeOutputMaterial');tree.links.new(mix.outputs[0],out.inputs['Surface'])
    result[LAYERED]=1
    result[LAYER_KEYS]=[*_layer_keys(base),*_layer_keys(overlay)]
    sources=([base[LAYER_SOURCES][str(i)] for i in range(len(base[LAYER_SOURCES]))] if base.get(LAYERED) else [base])
    if overlay.get(LAYERED):
        sources.extend(overlay[LAYER_SOURCES][str(i)] for i in range(len(overlay[LAYER_SOURCES])))
    else:sources.append(overlay)
    result[LAYER_SOURCES]={str(i):material for i,material in enumerate(sources)}
    result.use_backface_culling=False
    tx.surface_materials[key]=result
    return result


def _layer_keys(material):
    if material.get(LAYERED):
        return [dict(row.items()) for row in material[LAYER_KEYS]]
    return [{'texture':material.get('oxal_mc_studio_texture_key',''),
             'back':material.get('oxal_mc_studio_surface_back_texture',''),
             'sides':material.get('oxal_mc_studio_two_sided_surface','')}]


def variant_surface(tx,root,previous,staged):
    """Suspend/resume generated layers, retaining their actual private graphs."""
    if not (previous.get(LAYERED) or previous.get(SUSPENDED)):
        return previous,False
    if bool(previous.get(LAYERED))==bool(staged.get(LAYERED)):
        return previous,False
    from ...util.ownership import require_owned
    from ..enchantment import enchantment
    from .assets import _private_shader_trees
    if previous not in tx.ids:require_owned(root,(previous,))
    enchanted=enchantment.inspect(previous) is not None
    baseline=enchantment.remove(tx,root,previous) if enchanted else previous
    result,_=_private_shader_trees(tx,baseline);tree=result.node_tree
    base=tree.nodes.get('OXAL Coplanar Base');overlay=tree.nodes.get('OXAL Coplanar Overlay');mix=tree.nodes.get('OXAL Coplanar Alpha Over')
    output=next((n for n in tree.nodes if n.type=='OUTPUT_MATERIAL' and n.is_active_output),None)
    if (base is None or overlay is None or mix is None or output is None
            or base.type!='GROUP' or overlay.type!='GROUP' or mix.type!='MIX_SHADER'
            or len(output.inputs['Surface'].links)!=1
            or any(len(mix.inputs[i].links)!=1 for i in range(3))
            or mix.inputs[0].links[0].from_socket!=overlay.outputs['Layer Alpha']
            or mix.inputs[1].links[0].from_socket!=base.outputs['Surface']
            or mix.inputs[2].links[0].from_socket!=overlay.outputs['Surface']):
        raise ValueError('叠层材质结构已改变，未覆盖用户表面')
    original=mix.outputs[0] if previous.get(LAYERED) else base.outputs['Surface']
    if output.inputs['Surface'].links[0].from_socket!=original:
        raise ValueError('叠层材质表面已由用户更改，未覆盖')
    enable=bool(staged.get(LAYERED))
    if enable and _layer_keys(staged)!=[dict(row.items()) for row in result[LAYER_KEYS]]:
        raise ValueError('休眠叠层与新变体的贴图身份不同，未猜测恢复')
    tree.links.new(mix.outputs[0] if enable else base.outputs['Surface'],output.inputs['Surface'])
    result[LAYERED]=enable;result[SUSPENDED]=not enable
    return result,enchanted


def variant_sampler(material):
    tree=material.node_tree
    if material.get(LAYERED) or material.get(SUSPENDED):
        base=tree.nodes.get('OXAL Coplanar Base')
        if base is None or base.type!='GROUP' or base.node_tree is None:
            raise ValueError('叠层材质缺少实际底层，未猜测采样器')
        tree=base.node_tree
    source=tree.nodes.get('OXAL Source Texture') if tree else None
    return tree,source


def bridge_layers(tx,root,previous,staged,old_materials):
    """Explicit rebuild: retain each actual old surface, never default-pack it."""
    if not staged.get(LAYERED):
        if previous.get(LAYERED):
            raise ValueError('变体会移除已有叠层底材，未猜测迁移用户表面')
        return previous
    expected=_layer_keys(staged)
    if previous.get(LAYERED):
        if _layer_keys(previous)!=expected:
            raise ValueError('叠层材质身份已改变，未覆盖用户表面')
        return previous
    from ..enchantment import enchantment
    sources=[]
    for index,identity in enumerate(expected):
        if not identity['texture']:raise ValueError('叠层材质缺少完整贴图身份，未猜测替换')
        found={material for material in (previous,*old_materials) if material and _layer_keys(material)==[identity]}
        # A newly introduced layer has no old user material to preserve. Only
        # use the exact source Material created by this staging transaction.
        if not found and index>0:
            candidate=staged.get(LAYER_SOURCES,{}).get(str(index))
            if candidate in tx.ids and _layer_keys(candidate)==[identity]:found={candidate}
        if len(found)!=1:
            raise ValueError('旧叠层材质缺失或身份不唯一，已保留原实例；请重新导入此资产')
        material=found.pop();state=enchantment.inspect(material)
        if state:
            material=enchantment.remove(tx,root,material)
        sources.append(material)
    result=sources[0]
    for upper in sources[1:]:result=_layer_material(tx,result,upper)
    return result


def pair_records(vertices,faces,uvs,indices):
    """Read-only source topology recipe, shared with offline preview planning."""
    found={};records=[];pairs=0
    for face,uv,index in zip(faces,uvs,indices,strict=True):
        points=[Vector(vertices[i]) for i in face]
        normal=cast(Vector,(points[1]-points[0]).cross(points[2]-points[0]));normal.normalize()
        key=tuple(sorted(tuple(round(float(c),7) for c in point) for point in points))
        match=next((record for record in found.get(key,()) if record['back'] is None and normal.dot(record['normal'])<-.9999),None)
        if match is not None:
            aligned=[uv[min(range(len(points)),key=lambda i:(points[i]-Vector(vertices[vertex])).length_squared)] for vertex in match['face']]
            match['back']=(index,aligned);pairs+=1
        else:
            record={'face':face,'uv':uv,'index':index,'normal':normal,'back':None};records.append(record);found.setdefault(key,[]).append(record)
    return records,pairs


def build(tx,name,vertices,faces,uvs,materials,indices,mesh_object):
    """Pair exact opposite polygons by source-space corner identity and winding."""
    records,pairs=pair_records(vertices,faces,uvs,indices)
    merged=[];by_points={};layers=0
    for record in records:
        key=tuple(sorted(tuple(round(float(c),7) for c in vertices[i]) for i in record['face']))
        match=None
        for previous in by_points.get(key,()):
            if previous['normal'].dot(record['normal'])<.9999:continue
            # Different UV canvases are intentional distinct surfaces. Only
            # merge source layers whose front/back samples align per corner.
            aligned=[record['face'].index(vertex) for vertex in previous['face']] if set(record['face'])==set(previous['face']) else [
                min(range(len(record['face'])),key=lambda i:(Vector(vertices[record['face'][i]])-Vector(vertices[vertex])).length_squared)
                for vertex in previous['face']]
            if any(max(abs(a-b)for a,b in zip(uv,record['uv'][i],strict=True))>1e-7 for uv,i in zip(previous['uv'],aligned,strict=True)):continue
            if bool(previous['back'])!=bool(record['back']):continue
            if record['back'] and any(max(abs(a-b)for a,b in zip(uv,record['back'][1][i],strict=True))>1e-7 for uv,i in zip(previous['back'][1],aligned,strict=True)):continue
            match=previous;break
        if match is None:
            record['layers']=[];merged.append(record);by_points.setdefault(key,[]).append(record)
        else:
            match['layers'].append(record);layers+=1
    records=merged
    if not pairs and not layers:return mesh_object(tx,name,vertices,faces,uvs,materials,indices)
    following=[];front_uv=[];back_uv=[];slots=[];palette=[]
    for record in records:
        index=record['index'];back=record['back'];following.append(record['face']);front_uv.append(record['uv']);back_uv.append(back[1] if back else record['uv'])
        material=_material(tx,materials[index],materials[back[0]]) if back else materials[index]
        for upper in record['layers']:
            top=materials[upper['index']]
            if upper['back']:top=_material(tx,top,materials[upper['back'][0]])
            material=_layer_material(tx,material,top)
        if material not in palette:palette.append(material)
        slots.append(palette.index(material))
    obj=mesh_object(tx,name,vertices,following,front_uv,palette,slots)
    layer=cast(bpy.types.Mesh,obj.data).uv_layers.new(name=BACK_UV)
    for polygon,coordinates in zip(obj.data.polygons,back_uv,strict=True):
        for loop,uv in zip(polygon.loop_indices,coordinates,strict=True):layer.data[loop].uv=uv
    obj['oxal_mc_studio_coplanar_pairs']=pairs
    obj['oxal_mc_studio_coplanar_layers']=layers
    return obj


def bridge_existing(tx,root,previous,staged):
    """Add a verified native UV selector without replacing an authored Material."""
    if bool(staged.get(LAYERED))!=bool(previous.get(LAYERED)):
        raise ValueError('旧叠层材质需要重新导入此资产；已保留用户底材和原实例，未丢弃透明叠层')
    marker='oxal_mc_studio_two_sided_surface'
    if not staged.get(marker) or previous.get(marker):return previous
    if staged[marker]!='SAME':
        raise ValueError('旧正反面使用不同材质，无法在保留实际材质绑定时合并；保留旧实例')
    from ...util.ownership import owns, require_owned
    from ..enchantment import enchantment
    from .asset_rebuild import _value
    if previous not in tx.ids:require_owned(root,(previous,))
    if any(user!=root and user not in tx.ids and not owns(root,user) for user in bpy.data.user_map(subset=(previous,)).get(previous,set())):
        raise ValueError('正反UV升级材质由外部数据使用，未修改')
    tree=previous.node_tree;source=tree.nodes.get('OXAL Source Texture') if tree else None
    if source is None or source.type!='TEX_IMAGE' or not previous.get('oxal_mc_studio_texture_key'):
        raise ValueError('用户材质没有已知原版UV入口，未覆盖')
    inputs=[]
    mapping=tree.nodes.get('OXAL Animation Current Mapping')
    if mapping is not None:
        coord=tree.nodes.get('OXAL Animation UV')
        if coord is None or coord.type!='TEX_COORD' or len(mapping.inputs[0].links)!=1 or mapping.inputs[0].links[0].from_node!=coord:
            raise ValueError('用户动画UV链已改变，未覆盖')
        links=tuple(coord.outputs['UV'].links)
        if any(not link.to_node.name.startswith('OXAL Animation ') for link in links):raise ValueError('用户UV链无法确认，未覆盖')
        inputs.extend(link.to_socket for link in links)
    else:
        if source.inputs['Vector'].is_linked:raise ValueError('用户UV链已改变，未覆盖')
        inputs.append(source.inputs['Vector'])
    for node in tree.nodes:
        if node.type=='TEX_IMAGE' and node.name.startswith('OXAL Copper Texture '):
            if node.inputs['Vector'].is_linked:raise ValueError('铜材质UV链无法确认，未覆盖')
            inputs.append(node.inputs['Vector'])
    glint=enchantment.inspect(previous)
    original_names=set(tree.nodes.keys());properties=_value(dict(previous.items()))
    links=[(socket,socket.links[0].from_socket if socket.is_linked else None) for socket in inputs]
    def rollback():
        for socket,origin in links:
            for link in tuple(socket.links):tree.links.remove(link)
            if origin is not None:tree.links.new(origin,socket)
        for name in tuple(tree.nodes.keys()):
            if name not in original_names:tree.nodes.remove(tree.nodes[name])
        for key in tuple(previous.keys()):
            if key not in properties:del previous[key]
        for key,value in properties.items():previous[key]=value
        previous.update_tag()
    tx.on_rollback(rollback)
    uv=_selected_uv(tree)
    for socket in inputs:tree.links.new(uv,socket)
    previous[marker]='SAME'
    if glint:enchantment.stamp(previous)
    from ...util.shader_layout import arrange_owned_shaders
    arrange_owned_shaders((previous,), on_rollback=tx.on_rollback)
    previous.update_tag()
    return previous


_FLAME_KEYS=frozenset('minecraft:block/'+name for name in ('fire_0','fire_1','soul_fire_0','soul_fire_1','campfire_fire','soul_campfire_fire'))
SHADOW='oxal_mc_studio_flame_shadowless'


def flame_shadow(material,key):
    """New private flame surfaces preserve RGB/alpha and transmit shadow rays."""
    if key not in _FLAME_KEYS or material.get(SHADOW):return
    tree=material.node_tree;output=next(n for n in tree.nodes if n.type=='OUTPUT_MATERIAL' and n.is_active_output)
    if len(output.inputs['Surface'].links)!=1:raise ValueError('火焰材质没有唯一Surface，未更改')
    surface=output.inputs['Surface'].links[0].from_socket
    ray=tree.nodes.new('ShaderNodeLightPath');ray.name='OXAL Flame Shadow Ray'
    transparent=tree.nodes.new('ShaderNodeBsdfTransparent');transparent.name='OXAL Flame Shadow Transparent'
    selector=tree.nodes.new('ShaderNodeMixShader');selector.name='OXAL Flame Shadow Selector'
    tree.links.new(ray.outputs['Is Shadow Ray'],selector.inputs[0]);tree.links.new(surface,selector.inputs[1]);tree.links.new(transparent.outputs[0],selector.inputs[2]);tree.links.new(selector.outputs[0],output.inputs['Surface'])
    material[SHADOW]=True
    if hasattr(material,'use_transparent_shadow'):material.use_transparent_shadow=True


def bridge_shadow(tx,root,previous,staged):
    if not staged.get(SHADOW) or previous.get(SHADOW):return previous
    from ...util.ownership import owns, require_owned
    from ..enchantment import enchantment
    from .asset_rebuild import _value
    if previous not in tx.ids:require_owned(root,(previous,))
    if any(user!=root and user not in tx.ids and not owns(root,user) for user in bpy.data.user_map(subset=(previous,)).get(previous,set())):
        raise ValueError('火焰材质由外部数据使用，未修改其投影')
    glint=enchantment.inspect(previous)
    tree=previous.node_tree;output=next(n for n in tree.nodes if n.type=='OUTPUT_MATERIAL' and n.is_active_output)
    if len(output.inputs['Surface'].links)!=1:raise ValueError('用户火焰表面无法安全包裹，未覆盖')
    original=output.inputs['Surface'].links[0].from_socket;names=set(tree.nodes.keys());props=_value(dict(previous.items()));shadow=previous.use_transparent_shadow
    def rollback():
        tree.links.new(original,output.inputs['Surface'])
        for name in tuple(tree.nodes.keys()):
            if name not in names:tree.nodes.remove(tree.nodes[name])
        for key in tuple(previous.keys()):
            if key not in props:del previous[key]
        for key,value in props.items():previous[key]=value
        previous.use_transparent_shadow=shadow;previous.update_tag()
    tx.on_rollback(rollback)
    flame_shadow(previous,staged.get('oxal_mc_studio_texture_key',''))
    if glint:enchantment.stamp(previous)
    from ...util.shader_layout import arrange_owned_shaders
    arrange_owned_shaders((previous,), on_rollback=tx.on_rollback)
    previous.update_tag()
    return previous
