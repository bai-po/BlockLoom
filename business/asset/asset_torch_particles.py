"""Finite, explicitly rebuilt torch209 cohorts evaluated entirely by native GN."""
import ast
import hashlib
import json
import math
import random
from array import array
from functools import lru_cache
from itertools import pairwise
from typing import Any, cast

import bpy
from mathutils import Vector

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.ownership import owns, require_owned
from ...util.resources import resolve_view_layer
from . import asset_catalog as catalog
from . import asset_contracts, asset_states
from .asset_geometry import Creation, driver

TORCHES = frozenset({'torch','wall_torch','soul_torch','soul_wall_torch','copper_torch','copper_wall_torch'})
PREFIX = 'oxal_mc_studio_torch209_'
CARRIER = PREFIX+'carrier'
CLOCK = PREFIX+'clock'
MAX_EVENTS = 8
MAX_POINTS = 300000


def requires_rig(identifier):
    return identifier.split(':')[-1] in TORCHES


@lru_cache(maxsize=1)
def _spec():
    return catalog.read(catalog.ROOT/'native_effect/torch_provider209.json')


def _binding(root):
    obj=root.get(CARRIER)
    if not isinstance(obj,bpy.types.Object) or obj.type!='MESH' or obj not in root.children_recursive:
        raise ValueError('火把原生粒子载体缺失，未接管其它数据')
    obj=cast(Any,obj)
    require_owned(root,(root,obj,obj.data))
    if obj.data.get(PREFIX+'schema')!=1:
        raise ValueError('火把粒子表版本未知，未改写')
    return obj


def _rate_graph(root,source_property):
    """Prove generated aliases; an arbitrary user driver is not a rate curve."""
    state=next((spec for spec in json.loads(root.get('oxal_mc_studio_state_controls','[]'))
                if source_property in (spec.get('property'),spec.get('key'))),None)
    public_name=state['property'] if state else source_property
    public='['+json.dumps(public_name,ensure_ascii=False)+']'
    control=next((spec for spec in json.loads(root.get('骨骼控件','[]')) if public_name in spec.get('keys',())),None)
    location='pose.bones['+json.dumps(control['bone'] if control else '控制 · '+source_property,ensure_ascii=False)+'].location'
    expected={};boolean=False
    keys={public,location}
    if control:
        minimum,maximum=control['min'],control['max']
        boolean=isinstance(root.get(state['key'] if state else public_name),bool)
        expression='v>.175' if boolean else f'{minimum}+min(1,max(0,v/.35))*{maximum-minimum}'
        expected[public]=(expression,location+'[0]')
        if state:
            if (state['min'],state['max'])!=(minimum,maximum):
                raise ValueError('粒子控件声明与实际骨骼范围冲突，未采样或改写')
            private='['+json.dumps(state['key'],ensure_ascii=False)+']'
            keys.add(private);expected[private]=('v',public)
    animation=root.animation_data
    public_driven=False
    for curve in animation.drivers if animation else ():
        if curve.data_path not in keys or (curve.data_path==location and curve.array_index!=0):continue
        contract=expected.get(curve.data_path)
        driver=curve.driver
        valid=contract is not None and not curve.mute and driver.type=='SCRIPTED' and driver.is_simple_expression and not driver.use_self
        if valid:
            assert contract is not None
            expression,target_path=contract
            valid=ast.dump(ast.parse(driver.expression,mode='eval'))==ast.dump(ast.parse(expression,mode='eval')) and len(driver.variables)==1
            if valid:
                variable=driver.variables[0];target=variable.targets[0]
                valid=variable.name=='v' and variable.type=='SINGLE_PROP' and target.id==root and target.data_path==target_path
        if valid:
            from types import SimpleNamespace

            from .asset_rigs import driver_records
            # Reuse the native mapping/modifier validator on this exact curve.
            driver_records(SimpleNamespace(animation_data=SimpleNamespace(drivers=(curve,))))
            require_owned(root,(root,))
        else:
            raise ValueError('粒子播放倍率含未知用户驱动，未采样或改写：'+curve.data_path)
        public_driven |= curve.data_path==public
    mapping=None
    if public_driven:
        assert control is not None
        mapping=cast(Any,dict(control,boolean=boolean))
    return keys,location if public_driven else public,mapping


def clock_signature(root, source_property='粒子播放倍率'):
    paths,_,_=_rate_graph(root,source_property)
    animation=root.animation_data
    if animation is None:return ''
    strips=[strip for track in animation.nla_tracks for strip in track.strips]
    bindings=[animation]
    while strips:
        strip=strips.pop();bindings.append(strip);strips.extend(getattr(strip,'strips',()))
    records=[]
    for binding in bindings:
        curves=[curve for curve in action_fcurves(binding.action,slot=getattr(binding,'action_slot',None)) if curve.data_path in paths]
        if not curves:continue
        state={name:getattr(binding,name) for name in ('frame_start','frame_end','action_frame_start','action_frame_end','scale','repeat','blend_type','influence','mute','use_reverse','blend_in','blend_out','extrapolation','use_auto_blend','use_animated_influence','use_animated_time','use_animated_time_cyclic','action_blend_type','action_extrapolation','action_influence') if hasattr(binding,name)}
        if getattr(binding,'use_animated_influence',False):state.pop('influence',None)
        points=[(c.data_path,c.array_index,c.extrapolation,c.mute,[(tuple(cast(Any,p.co)),tuple(cast(Any,p.handle_left)),tuple(cast(Any,p.handle_right)),p.interpolation,p.easing,p.amplitude,p.back,p.period) for p in c.keyframe_points]) for c in curves]
        if any(c.modifiers for c in curves):raise ValueError('播放倍率含未知曲线修改器，未改写原动画')
        strip_curves=[(c.data_path,c.mute,c.extrapolation,[(tuple(cast(Any,p.co)),tuple(cast(Any,p.handle_left)),tuple(cast(Any,p.handle_right)),p.interpolation,p.easing,p.amplitude,p.back,p.period) for p in c.keyframe_points]) for c in getattr(binding,'fcurves',())]
        records.append((state,points,strip_curves))
    if records:
        records.append([(track.mute,track.is_solo) for track in animation.nla_tracks])
    payload={'integration':'adaptive-gauss4-v1','animation':records}
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest() if records else ''


def _rate_signature(root):
    return clock_signature(root)


def _gauss4(fn,left,right):
    midpoint=(left+right)*.5;radius=(right-left)*.5
    return radius*sum(weight*(fn(midpoint-radius*node)+fn(midpoint+radius*node)) for node,weight in (
        (.3399810435848563,.6521451548625461),(.8611363115940526,.3478548451374538)))


def _integrate_rate(fn,left,right):
    """Adaptive composite Gauss-Legendre, using interior samples at jumps."""
    result=0.;stack=[(left,right,_gauss4(fn,left,right),0)]
    while stack:
        lo,hi,coarse,depth=stack.pop();mid=(lo+hi)*.5
        a,b=_gauss4(fn,lo,mid),_gauss4(fn,mid,hi)
        if abs(a+b-coarse)<=1e-7*max(1.,hi-lo) or depth>=10:
            result+=a+b
        else:
            stack.extend(((mid,hi,b,depth+1),(lo,mid,a,depth+1)))
    return result


def _direct_rate(root,source_property):
    _,path,control=_rate_graph(root,source_property)
    animation=root.animation_data
    if not animation or not animation.action or animation.nla_tracks:
        return None
    if animation.action_blend_type!='REPLACE' or abs(animation.action_influence-1)>1e-7:
        return None
    curves=[curve for curve in action_fcurves(animation.action,slot=animation.action_slot) if curve.data_path==path and curve.array_index==0 and not curve.mute]
    if len(curves)!=1 or curves[0].modifiers:return None
    curve=curves[0]
    def rate(time):
        value=float(curve.evaluate(time))
        if control:
            value=float(value>.175) if control['boolean'] else control['min']+min(1,max(0,value/.35))*(control['max']-control['min'])
        return min(8,max(0,value))
    return rate,tuple(float(point.co.x) for point in curve.keyframe_points)


def sample_clock(scene,root,start,end,fps,fps_base,source_property='粒子播放倍率',*,constant=None,expected_signature=None):
    """Explicit, read-only native-rate sampling; output source local frames.

    This function is never registered as a playback or render callback. The
    caller owns the bounded cache and records the returned signature. Linear
    interpolation between per-frame integral samples is the native cache's
    declared subframe representation, leaving the user's Action/NLA intact.
    """
    ensure_mutation_allowed()
    if end<start or end-start>4096 or fps<=0 or fps_base<=0:raise ValueError('粒子时钟采样范围无效')
    signature=clock_signature(root,source_property)
    if expected_signature is not None and signature!=expected_signature:
        raise ValueError('确认期间粒子倍率Action/NLA已改变，请重新重建')
    if not root.users_scene or not signature:
        constant_rate=float(root.get(source_property,1) if constant is None else constant)
        if not math.isfinite(constant_rate) or not 0<=constant_rate<=8:raise ValueError('粒子播放倍率无效')
        return [(frame,start+(frame-start)*constant_rate) for frame in range(start,end+1)],signature
    original=scene.frame_current,scene.frame_subframe
    result=[(start,float(start))];local=float(start)
    try:
        layer=resolve_view_layer(scene)
        direct=_direct_rate(root,source_property)
        cuts=direct[1] if direct else ()
        def rate(time):
            if direct:return direct[0](time)
            integer=math.floor(time);scene.frame_set(integer,subframe=time-integer);layer.update()
            return min(8,max(0,float(root.evaluated_get(layer.depsgraph).get(source_property,1))))
        for frame in range(start+1,end+1):
            boundaries=[frame-1,*sorted({time for time in cuts if frame-1<time<frame}),frame]
            local+=sum(_integrate_rate(rate,a,b) for a,b in pairwise(boundaries));result.append((frame,local))
    finally:scene.frame_set(original[0],subframe=original[1])
    return result,signature


def request(scene,root,options):
    source=options.get('particles_window',options)
    result={'start':source.get('start',scene.frame_start),'end':source.get('end',scene.frame_end),
            'fps':source.get('fps',scene.render.fps),'fps_base':source.get('fps_base',scene.render.fps_base),
            'seed':source.get('seed',root.get('粒子种子',195)),
            'rate':source.get('rate',root.get('粒子播放倍率',1.0)),
            'rate_signature':source.get('rate_signature',_rate_signature(root))}
    for key in ('start','end','fps','seed'):
        value=result[key]
        if isinstance(value,bool) or not isinstance(value,int):raise TypeError('火把采样范围/种子必须为整数')
    if not -1048574<=result['start']<=result['end']<=1048574 or result['end']-result['start']>4096:
        raise ValueError('火把显式采样范围须为至多4096帧')
    if not 1<=result['fps']<=1000000 or not 0<=result['seed']<=2147483647:
        raise ValueError('火把FPS或种子超出范围')
    for key in ('fps_base','rate'):
        value=result[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError('火把采样时钟无效')
    if result['fps_base']<=0 or not 0<=result['rate']<=8:raise ValueError('火把播放倍率须在0至8之间')
    if not isinstance(result['rate_signature'],str):raise TypeError('火把播放倍率快照无效')
    return result


def pending(scene,root):
    obj=root.get(CARRIER)
    if not isinstance(obj,bpy.types.Object) or obj.type!='MESH':return True
    obj=cast(Any,obj)
    current=request(scene,root,{})
    return any(obj.data.get(PREFIX+key)!=value for key,value in current.items() if key!='rate' or not current['rate_signature'])


def _origin(short,state):
    spec=_spec()
    if 'wall_torch' in short:
        facing=state.get('facing','north')
        if facing not in spec['wall_origins']:raise ValueError('墙火把朝向未知，未猜测粒子出生点')
        return Vector(spec['wall_origins'][facing])
    return Vector(spec['upright_origin'])


def _initial(seed,birth,radius,trial,smoke):
    """Read the immutable source distribution into a newly allocated cohort."""
    spec=_spec();rng=random.Random((seed*1000003+birth*91771+radius*4099+trial*131+int(smoke))&0xffffffffffffffff)
    rng.random()
    velocity=Vector([(2*rng.random()-1)*spec['velocity_random_range'] for _ in range(3)])
    magnitude=(rng.random()+rng.random()+1)*spec['velocity_magnitude_base']*spec['velocity_magnitude_scale']
    if velocity.length:velocity.normalize();velocity*=magnitude
    velocity.y+=spec['vertical_bias']
    size=spec['quad_base']*(rng.random()*.5+.5)*2
    kind=spec['smoke' if smoke else 'flame'];velocity*=kind['velocity_scale']
    jitter=Vector((0,0,0));gray=1.
    if smoke:
        gray=rng.random()*kind['gray_scale'];size*=kind['size_scale']
    else:
        jitter=Vector([(rng.random()-rng.random())*kind['jitter'] for _ in range(3)])
    life=max(1,int(kind['life_base']/(rng.random()*kind['life_random_scale']+kind['life_random_offset'])))+kind.get('life_add',0)
    choice=rng.random()
    return (jitter.x,-jitter.z,jitter.y),(velocity.x,-velocity.z,velocity.y),size,gray,life,choice


def _rows(window,origin,ticks):
    spec=_spec();rows=[]
    low=math.floor(min(value for _,value in ticks))-spec['history_ticks']
    high=math.floor(max(value for _,value in ticks))
    for birth in range(low,high+1):
        for radius in spec['display_radii']:
            rng=random.Random((window['seed']*1000003+birth*91771+radius*37)&0xffffffffffffffff)
            denominator=math.log1p(-1/radius**3);trial_min=-1;logs=[]
            while True:
                logs.append(math.log1p(-rng.random()))
                trial_min+=1+math.floor(logs[-1]/denominator)
                if trial_min>=spec['display_samples']:break
                if len(logs)>MAX_EVENTS:raise ValueError('火把样本超过原生事件容量，旧表保持')
                packed=tuple(logs+[0.]*(MAX_EVENTS-len(logs)))
                for trial in range(trial_min,spec['display_samples']):
                    for smoke in (True,False):
                        jitter,velocity,size,gray,life,choice=_initial(window['seed'],birth,radius,trial,smoke)
                        rows.append((tuple(origin+Vector(jitter)),birth,radius,trial,len(logs)-1,int(smoke),velocity,size,gray,life,choice,packed))
                        if len(rows)>MAX_POINTS:raise ValueError('火把采样超过30万潜在点，请缩短显式时间范围')
    return rows


def _mesh(tx,window,origin,ticks):
    rows=_rows(window,origin,ticks)
    mesh=tx.track(bpy.data.meshes.new('火把209 · 潜在事件表'))
    count=len(ticks)
    mesh.from_pydata([(0,0,0)]*count+[row[0] for row in rows],[],[])
    marker=mesh.attributes.new(PREFIX+'particle','FLOAT','POINT');marker.data.foreach_set('value',[0.]*count+[1.]*len(rows))
    clock=mesh.attributes.new(PREFIX+'tick','FLOAT','POINT');clock.data.foreach_set('value',[value for _,value in ticks]+[0.]*len(rows))
    samples=mesh.attributes.new(PREFIX+'samples','FLOAT','POINT');samples.data.foreach_set('value',[count]*(count+len(rows)))
    fields=[('birth',1,'FLOAT'),('radius',2,'FLOAT'),('trial',3,'FLOAT'),('cohort',4,'FLOAT'),('smoke',5,'FLOAT'),
            ('velocity',6,'FLOAT_VECTOR'),('size',7,'FLOAT'),('gray',8,'FLOAT'),('life',9,'FLOAT'),('choice',10,'FLOAT')]
    for key,index,kind in fields:
        attr=mesh.attributes.new(PREFIX+key,kind,'POINT')
        values=[0.]*(count*3)+[component for row in rows for component in row[index]] if kind=='FLOAT_VECTOR' else [16. if key=='radius' else 0.]*count+[row[index] for row in rows]
        attr.data.foreach_set('vector' if kind=='FLOAT_VECTOR' else 'value',values)
    for index in range(MAX_EVENTS):
        attr=mesh.attributes.new(PREFIX+'log'+str(index),'FLOAT','POINT');attr.data.foreach_set('value',[0.]*count+[row[11][index] for row in rows])
    for key,value in window.items():
        mesh[PREFIX+key]=value
        if isinstance(value,(int,float)):
            attr=mesh.attributes.new(PREFIX+key,'FLOAT','POINT');attr.data.foreach_set('value',[value]*(count+len(rows)))
    mesh[PREFIX+'schema']=1
    mesh[PREFIX+'fingerprint']=_table_hash(mesh)
    return mesh


def _clock_samples(scene,root,window):
    first,last=window['start'],window['end'];factor=20*window['fps_base']/window['fps']
    samples,_=sample_clock(scene,root,first,last,window['fps'],window['fps_base'],constant=window['rate'],expected_signature=window.get('rate_signature',''))
    return [(frame,(local-first)*factor) for frame,local in samples]


def _table_hash(mesh):
    digest=hashlib.sha256()
    for attr in sorted(mesh.attributes,key=lambda item:item.name):
        if attr.name.startswith('.') or len(attr.data)==0:continue
        if attr.data_type not in {'FLOAT','FLOAT_VECTOR'}:raise ValueError('火把粒子表含未知用户属性，未覆盖')
        digest.update((attr.name+attr.data_type+attr.domain).encode())
        values=array('f',[0.])*(len(attr.data)*(3 if attr.data_type=='FLOAT_VECTOR' else 1))
        attr.data.foreach_get('vector' if attr.data_type=='FLOAT_VECTOR' else 'value',values)
        digest.update(values.tobytes())
    return digest.hexdigest()


class _Nodes:
    def __init__(self,tree):self.tree=tree;self.nodes=tree.nodes;self.links=tree.links
    def node(self,kind):return self.nodes.new(kind)
    def wire(self,value,socket):
        if isinstance(value,(int,float,tuple,list)):socket.default_value=value
        else:self.links.new(value,socket)
    def math(self,operation,a,b=None):
        n=self.node('ShaderNodeMath');n.operation=operation;self.wire(a,n.inputs[0])
        if b is not None:self.wire(b,n.inputs[1])
        return n.outputs[0]
    def attr(self,name,kind='FLOAT'):
        n=self.node('GeometryNodeInputNamedAttribute');n.data_type=kind;n.inputs['Name'].default_value=PREFIX+name;return n.outputs['Attribute']
    def vector(self,operation,a,b):
        n=self.node('ShaderNodeVectorMath');n.operation=operation;self.wire(a,n.inputs[0]);self.wire(b,n.inputs['Scale'] if operation=='SCALE' else n.inputs[1]);return n.outputs[0]
    def store(self,geometry,name,value):
        n=self.node('GeometryNodeStoreNamedAttribute');n.domain='POINT';n.data_type='FLOAT';n.inputs['Name'].default_value=PREFIX+name;self.wire(geometry,n.inputs['Geometry']);self.wire(value,n.inputs['Value']);return n.outputs['Geometry']


def _textures(kind):
    return ['minecraft:particle/'+key.split(':')[-1] for key in catalog.read(catalog.resource('minecraft:'+kind,'particles'))['textures']]


def _material(tx,material_for,kind):
    keys=_textures(kind);mat=tx.track(material_for(keys[0]).copy());mat.name='火把209 · '+kind
    mat.surface_render_method='BLENDED';tree=cast(Any,mat.node_tree)
    shader=next(n for n in tree.nodes if n.type=='BSDF_PRINCIPLED');texture=tree.nodes['OXAL Source Texture']
    uv=tree.nodes.new('ShaderNodeUVMap');uv.uv_map='UVMap'
    for n in tuple(tree.nodes):
        if n.type=='TEX_IMAGE':tree.links.new(uv.outputs['UV'],n.inputs['Vector'])
    color,alpha=texture.outputs['Color'],texture.outputs['Alpha']
    frame=tree.nodes.new('ShaderNodeAttribute');frame.attribute_name=PREFIX+'sprite'
    for index,key in enumerate(keys[1:],1):
        tex=tree.nodes.new('ShaderNodeTexImage');tex.image=material_for(key).node_tree.nodes['OXAL Source Texture'].image;tex.interpolation='Closest';tex[PREFIX+'texture_key']=key
        tree.links.new(uv.outputs['UV'],tex.inputs['Vector'])
        threshold=tree.nodes.new('ShaderNodeMath');threshold.operation='GREATER_THAN';threshold.inputs[1].default_value=index-.5;tree.links.new(frame.outputs['Fac'],threshold.inputs[0])
        channels=[]
        for prior,current in ((color,tex.outputs['Color']),(alpha,tex.outputs['Alpha'])):
            mix=tree.nodes.new('ShaderNodeMixRGB');tree.links.new(threshold.outputs[0],mix.inputs[0]);tree.links.new(prior,mix.inputs[1]);tree.links.new(current,mix.inputs[2]);channels.append(mix.outputs[0])
        color,alpha=channels
    if kind=='smoke':
        gray=tree.nodes.new('ShaderNodeAttribute');gray.attribute_name=PREFIX+'gray';mix=tree.nodes.new('ShaderNodeMixRGB');mix.blend_type='MULTIPLY';mix.inputs[0].default_value=1;tree.links.new(color,mix.inputs[1]);tree.links.new(gray.outputs['Color'],mix.inputs[2]);color=mix.outputs[0]
    tree.links.new(color,shader.inputs['Base Color']);tree.links.new(alpha,shader.inputs['Alpha'])
    if kind!='smoke':tree.links.new(color,shader.inputs['Emission Color']);shader.inputs['Emission Strength'].default_value=1
    else:shader.inputs['Emission Strength'].default_value=0
    return mat

def _graph(tx,root,obj,material_for,flame):
    group=tx.track(bpy.data.node_groups.new('火把209 · 原生稀疏显示采样','GeometryNodeTree'))
    group.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry');group.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
    obj.modifiers.new('原版火把209 · 无播放回调','NODES').node_group=group
    g=_Nodes(group);inp=g.node('NodeGroupInput');out=g.node('NodeGroupOutput');geometry=inp.outputs[0]
    scene_time=g.node('GeometryNodeInputSceneTime');frame=scene_time.outputs['Frame']
    index=g.math('MAXIMUM',g.math('SUBTRACT',frame,g.attr('start')),0)
    index=g.math('MINIMUM',index,g.math('SUBTRACT',g.attr('samples'),1))
    whole=g.math('FLOOR',index);fraction=g.math('FRACT',index)
    def sample(where):
        n=g.node('GeometryNodeSampleIndex');n.data_type='FLOAT';n.domain='POINT';g.wire(geometry,n.inputs['Geometry']);g.wire(g.attr('tick'),n.inputs['Value']);g.wire(where,n.inputs['Index']);return n.outputs['Value']
    lower=sample(whole);upper=sample(g.math('MINIMUM',g.math('ADD',whole,1),g.math('SUBTRACT',g.attr('samples'),1)))
    clock=g.math('ADD',lower,g.math('MULTIPLY',fraction,g.math('SUBTRACT',upper,lower)))
    clock.node.label='209 clock'
    age=g.math('SUBTRACT',clock,g.attr('birth'))
    camera=g.node('GeometryNodeInputActiveCamera');relative=g.node('GeometryNodeObjectInfo');relative.transform_space='RELATIVE';g.wire(camera.outputs[0],relative.inputs['Object'])
    original=g.node('GeometryNodeObjectInfo');original.transform_space='ORIGINAL';g.wire(camera.outputs[0],original.inputs['Object'])
    length=g.node('ShaderNodeVectorMath');length.operation='LENGTH';g.wire(original.outputs['Scale'],length.inputs[0]);has_camera=g.math('GREATER_THAN',length.outputs['Value'],0)
    axes=g.node('ShaderNodeSeparateXYZ');g.wire(relative.outputs['Location'],axes.inputs[0])
    cells=(g.math('FLOOR',g.math('ADD',axes.outputs['X'],.5)),g.math('FLOOR',axes.outputs['Z']),g.math('FLOOR',g.math('ADD',g.math('MULTIPLY',axes.outputs['Y'],-1),.5)))
    radius=g.attr('radius');denominator=g.math('MULTIPLY',radius,radius);probability=1.
    for coordinate in cells:
        coordinate=g.math('MULTIPLY',coordinate,has_camera)
        factor=g.math('DIVIDE',g.math('MAXIMUM',g.math('SUBTRACT',radius,g.math('ABSOLUTE',coordinate)),0),denominator)
        probability=g.math('MULTIPLY',probability,factor)
    cast(Any,probability).node.label='209 probability'
    # Stable log1p(-p): direct log(1-p) loses the sparse probability in float32.
    power=probability;series=probability
    for exponent in range(2,5):
        power=g.math('MULTIPLY',power,probability);series=g.math('ADD',series,g.math('DIVIDE',power,exponent))
    logarithm=g.math('MULTIPLY',g.math('MAXIMUM',series,1e-20),-1)
    trial=-1.
    for event in range(MAX_EVENTS):
        skip=g.math('ADD',1,g.math('FLOOR',g.math('DIVIDE',g.attr('log'+str(event)),logarithm)))
        active=g.math('GREATER_THAN',g.attr('cohort'),event-.5)
        trial=g.math('ADD',trial,g.math('MULTIPLY',active,skip))
    cast(Any,trial).node.label='209 trial'
    success=g.math('LESS_THAN',g.math('ABSOLUTE',g.math('SUBTRACT',trial,g.attr('trial'))),.1)
    success=g.math('MULTIPLY',success,g.math('GREATER_THAN',probability,0))
    whole_age=g.math('FLOOR',age);fraction_age=g.math('FRACT',age);friction=_spec()['friction']
    decay=g.math('POWER',friction,whole_age);geometric=g.math('DIVIDE',g.math('SUBTRACT',1,decay),1-friction)
    travel=g.math('ADD',geometric,g.math('MULTIPLY',fraction_age,decay))
    offset=g.vector('SCALE',g.attr('velocity','FLOAT_VECTOR'),travel)
    gravity_whole=g.math('DIVIDE',g.math('SUBTRACT',whole_age,g.math('MULTIPLY',friction,geometric)),1-friction)
    gravity_fraction=g.math('MULTIPLY',fraction_age,g.math('ADD',1,g.math('MULTIPLY',friction,geometric)))
    vertical=g.math('MULTIPLY',g.math('ADD',gravity_whole,gravity_fraction),g.math('MULTIPLY',g.attr('smoke'),.004))
    gravity=g.node('ShaderNodeCombineXYZ');g.wire(vertical,gravity.inputs['Z']);offset=g.vector('ADD',offset,gravity.outputs[0])
    position=g.node('GeometryNodeSetPosition');g.wire(geometry,position.inputs['Geometry']);g.wire(offset,position.inputs['Offset']);geometry=position.outputs['Geometry']
    smoke_keys=_textures('smoke');flame_keys=_textures(flame)
    smoke_frame=g.math('MINIMUM',len(smoke_keys)-1,g.math('FLOOR',g.math('DIVIDE',g.math('MULTIPLY',age,len(smoke_keys)),g.math('ADD',g.attr('life'),1))))
    flame_frame=g.math('MINIMUM',len(flame_keys)-1,g.math('FLOOR',g.math('MULTIPLY',g.attr('choice'),len(flame_keys))))
    sprite_frame=g.math('ADD',g.math('MULTIPLY',smoke_frame,g.attr('smoke')),g.math('MULTIPLY',flame_frame,g.math('SUBTRACT',1,g.attr('smoke'))))
    geometry=g.store(geometry,'sprite',sprite_frame)
    # Named attributes survive realization and retain the provider's own tint.
    age_ratio=g.math('DIVIDE',age,g.math('MAXIMUM',g.attr('life'),1))
    growth=g.math('MINIMUM',1,g.math('MULTIPLY',age_ratio,32))
    shrink=g.math('SUBTRACT',1,g.math('MULTIPLY',.5,g.math('MULTIPLY',age_ratio,age_ratio)))
    scale=g.math('MULTIPLY',g.attr('size'),g.math('ADD',g.math('MULTIPLY',growth,g.attr('smoke')),g.math('MULTIPLY',shrink,g.math('SUBTRACT',1,g.attr('smoke')))))
    enabled=g.node('ShaderNodeValue');driver(enabled.outputs[0],'default_value','v',root,'particles_enabled')
    valid=g.math('MULTIPLY',success,g.attr('particle'))
    for condition in (g.math('SUBTRACT',1,g.math('GREATER_THAN',0,age)),g.math('LESS_THAN',age,g.attr('life')),g.math('LESS_THAN',age,44),g.math('SUBTRACT',1,g.math('GREATER_THAN',g.attr('start'),frame)),g.math('SUBTRACT',1,g.math('GREATER_THAN',frame,g.attr('end'))),enabled.outputs[0]):
        valid=g.math('MULTIPLY',valid,condition)
    valid.node.label='209 valid'
    geometry.node.label='209 positioned points'
    grid=g.node('GeometryNodeMeshGrid');grid.inputs['Size X'].default_value=grid.inputs['Size Y'].default_value=2;grid.inputs['Vertices X'].default_value=grid.inputs['Vertices Y'].default_value=2
    uv=g.node('GeometryNodeStoreNamedAttribute');uv.domain='CORNER';uv.data_type='FLOAT_VECTOR';uv.inputs['Name'].default_value='UVMap';g.wire(grid.outputs['Mesh'],uv.inputs['Geometry']);g.wire(grid.outputs['UV Map'],uv.inputs['Value'])
    inverse_scale=g.vector('DIVIDE',(1,1,1),original.outputs['Scale'])
    remove_scale=g.node('FunctionNodeCombineTransform');g.wire(inverse_scale,remove_scale.inputs['Scale'])
    remove_location=g.node('FunctionNodeCombineTransform');g.wire(g.vector('SCALE',relative.outputs['Location'],-1),remove_location.inputs['Translation'])
    multiply=g.node('FunctionNodeMatrixMultiply');g.wire(relative.outputs['Transform'],multiply.inputs[0]);g.wire(remove_scale.outputs[0],multiply.inputs[1])
    rotate=g.node('FunctionNodeMatrixMultiply');g.wire(remove_location.outputs[0],rotate.inputs[0]);g.wire(multiply.outputs[0],rotate.inputs[1])
    billboard=g.node('GeometryNodeTransform')
    if billboard.bl_rna.properties.get('mode') is not None:
        billboard.mode='MATRIX'
    elif billboard.inputs.get('Mode') is not None:
        billboard.inputs['Mode'].default_value='Matrix'
    else:
        raise ValueError('当前运行时缺少原生矩阵几何变换能力')
    g.wire(uv.outputs['Geometry'],billboard.inputs['Geometry']);g.wire(rotate.outputs[0],billboard.inputs['Transform'])
    fallback=g.node('GeometryNodeTransform');fallback.inputs['Rotation'].default_value=(math.pi/2,0,0);g.wire(uv.outputs['Geometry'],fallback.inputs['Geometry'])
    switch=g.node('GeometryNodeSwitch');switch.input_type='GEOMETRY';g.wire(has_camera,switch.inputs['Switch']);g.wire(fallback.outputs[0],switch.inputs['False']);g.wire(billboard.outputs[0],switch.inputs['True'])
    joined=g.node('GeometryNodeJoinGeometry')
    for smoke,kind in ((True,'smoke'),(False,flame)):
        material=_material(tx,material_for,kind);set_material=g.node('GeometryNodeSetMaterial');g.wire(switch.outputs[0],set_material.inputs['Geometry']);set_material.inputs['Material'].default_value=material
        selection=g.math('MULTIPLY',valid,g.attr('smoke') if smoke else g.math('SUBTRACT',1,g.attr('smoke')))
        instances=g.node('GeometryNodeInstanceOnPoints');g.wire(geometry,instances.inputs['Points']);g.wire(selection,instances.inputs['Selection']);g.wire(set_material.outputs[0],instances.inputs['Instance']);g.wire(scale,instances.inputs['Scale'])
        realized=g.node('GeometryNodeRealizeInstances');g.wire(instances.outputs[0],realized.inputs[0]);g.wire(realized.outputs[0],joined.inputs['Geometry'])
    g.wire(joined.outputs[0],out.inputs[0])
    return group


def build(tx,scene,collection,root,identifier,options,material_for):
    short=identifier.split(':')[-1]
    if short not in TORCHES:return
    ensure_mutation_allowed()
    if 'particles_enabled' not in root:
        asset_states.register_control(root,'particles_enabled','粒子开关','粒子开关',0,1,1)
        root['particles_enabled']=root['粒子开关']=True
    if '粒子播放倍率' not in root:
        asset_states.register_control(root,PREFIX+'rate_control','粒子播放倍率','粒子播放倍率',0,8,1)
    root['粒子种子']=int(root.get('粒子种子',195));root.id_properties_ui('粒子种子').update(min=0,max=2147483647,description='有限原生采样表的种子；修改后显式重建')
    window=request(scene,root,options)
    source=options.get('_particles_source_root') or (tx.owner if isinstance(tx.owner,bpy.types.Object) and tx.owner!=root else root)
    ticks=_clock_samples(scene,source,window)
    state=options.get('state',json.loads(root.get('oxal_mc_studio_block_state','{}')))
    mesh=_mesh(tx,window,_origin(short,state),ticks)
    obj=tx.track(bpy.data.objects.new('火把209 · 原生烟与焰',mesh));collection.objects.link(obj);obj.parent=root
    obj['oxal_mc_studio_effect_geometry']=True;obj['oxal_mc_studio_model_part']='torch209_cohorts'
    flame='soul_fire_flame' if short.startswith('soul_') else 'copper_fire_flame' if short.startswith('copper_') else 'flame'
    _graph(tx,root,obj,material_for,flame)
    root[CARRIER]=obj
    root['火把粒子说明']='209成对烟焰、相机邻域抽样、原生重力/摩擦与材质；时间范围、种子或倍率动画改变后显式重建，无播放回调'


def rebuild(scene,root,options):
    ensure_mutation_allowed();obj=_binding(root);asset_contracts.validate(root)
    if _table_hash(obj.data)!=obj.data.get(PREFIX+'fingerprint'):
        raise ValueError('火把粒子表已有用户编辑，保留旧表，未覆盖')
    window=request(scene,root,options)
    if any(user not in {root,obj} and not owns(root,user) for user in bpy.data.user_map(subset=(obj.data,)).get(obj.data,set())):
        raise ValueError('火把粒子表有外部共享，未替换')
    state=json.loads(root.get('oxal_mc_studio_block_state','{}'));short=root['oxal_mc_studio_library_identifier'].split(':')[-1]
    ticks=_clock_samples(scene,root,window)
    old=obj.data;old_contract=root.get(asset_contracts.PROPERTY);old_seed=root.get('粒子种子',195)
    with Creation() as tx:
        tx.owner=root;mesh=_mesh(tx,window,_origin(short,state),ticks)
        def rollback():
            obj.data=old;root['粒子种子']=old_seed
            if old_contract is not None:root[asset_contracts.PROPERTY]=old_contract
        tx.on_rollback(rollback)
        obj.data=mesh;root['粒子种子']=window['seed'];obj.update_tag();root.update_tag();asset_contracts.stamp(root);tx.commit()
    return {'message':'已显式重建火把209时间轴粒子，保留根、Action/NLA、材质与绑定'}
