"""Authored book subrig and bounded native rune clocks, without callbacks."""

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.ownership import require_owned
from . import asset_catalog as catalog
from . import asset_contracts, asset_playback, asset_rigs
from .asset_geometry import Creation, driver

SCHEMA='oxal_mc_studio_native_book_schema'
RUNE='oxal_mc_studio_native_book_rune'
EMITTER='oxal_mc_studio_native_book_emitter'
TARGET='oxal_mc_studio_book_target'
TARGET_SCHEMA=TARGET+'_schema'
ROOT_DRIVERS=TARGET+'_root_drivers'
ANCHOR_DRIVERS=TARGET+'_anchor_drivers'
FOLLOW='玩家方向跟随'
DISTANCE='["内部玩家距离"]'
AUTO='玩家自动开合'


def build_entry(transaction,scene,collection,root,identifier,options,material_for):
    if identifier!='minecraft:enchanting_table':
        return False
    from .asset_native_rigs import load

    load(transaction,scene,collection,root,catalog.ROOT/'native_effect/book_native.blend','OXAL Native Book',pack_digest=options.get('pack_digest'))
    clock=asset_playback.create(transaction,scene,collection,root,options)
    runes=[obj for obj in root.children_recursive if obj.get(RUNE)]
    if len(runes)!=26 or len([obj for obj in root.children_recursive if obj.get(EMITTER)])!=16:
        raise ValueError('书本原生资源缺少独立符文或可动发射源')
    for obj in runes:
        for curve in obj.animation_data.drivers:
            asset_playback.retarget(curve,clock)
            if curve.data_path=='scale':
                expression=curve.driver.expression
                variable=curve.driver.variables.new();variable.name='window';variable.type='SINGLE_PROP'
                variable.targets[0].id=clock;variable.targets[0].data_path='["'+asset_playback.ACTIVE+'"]'
                curve.driver.expression='('+expression+')*window'
        if scene.camera:
            facing=obj.constraints.new('TRACK_TO');facing.name='符文朝向渲染相机'
            facing.target=scene.camera;facing.track_axis='TRACK_NEGATIVE_Y';facing.up_axis='UP_Z'
    root[SCHEMA]=1
    return True


def post_compile(root,scene,options):
    if root.get(SCHEMA)!=1:return
    # Scene membership is published before this explicit import hook. Driver
    # contracts distinguish the actual owning Scene from external Scene IDs.
    root[ROOT_DRIVERS]=asset_rigs.driver_contract(root)
    root[ANCHOR_DRIVERS]=asset_rigs.driver_contract(root['内部书本朝向'])


def _binding(scene,root):
    if root.get(SCHEMA)!=1 or root.get('oxal_mc_studio_library_identifier')!='minecraft:enchanting_table':
        raise ValueError('请选择完整的新原生附魔台')
    anchor,book=root.get('内部书本朝向'),root.get('内部书本骨架')
    if (not isinstance(anchor,bpy.types.Object) or not isinstance(book,bpy.types.Object)
            or book.type!='ARMATURE' or book.parent!=anchor or anchor not in root.children_recursive
            or scene not in root.users_scene):
        raise ValueError('书本独立骨架或朝向机构不完整，未接管其它数据')
    assert isinstance(book.data,bpy.types.Armature)
    require_owned(root,(root,anchor,book,book.data))
    return anchor


def _animated(root,path):
    animation=root.animation_data
    bindings=[animation] if animation else []
    stack=[strip for track in animation.nla_tracks for strip in track.strips] if animation else []
    while stack:
        strip=stack.pop();bindings.append(strip);stack.extend(getattr(strip,'strips',()))
    return any(curve.data_path==path for binding in bindings
               for curve in action_fcurves(binding.action,slot=getattr(binding,'action_slot',None)))


def target_request(scene,root,target=None,*,remove=False):
    """Freeze one explicit target choice; names alone never grant identity."""
    _binding(scene,root)
    result={'instance_id':root['oxal_mc_studio_instance_id'],'remove':bool(remove)}
    if remove:return result
    if not isinstance(target,bpy.types.Object) or scene not in target.users_scene:
        raise ValueError('请明确选择当前场景中的玩家目标物体')
    if target==root or target in root.children_recursive:
        raise ValueError('玩家目标不能是附魔台自身或其内部机构')
    instance=target.get('oxal_mc_studio_instance_id')
    if instance:
        require_owned(target,(target,))
        result['target']={'instance_id':instance}
    else:
        result['target']={'name':target.name_full,'session_uid':target.session_uid}
    return result


def _resolve(scene,root,snapshot):
    if snapshot.get('instance_id')!=root.get('oxal_mc_studio_instance_id') or not isinstance(snapshot.get('remove'),bool):
        raise ValueError('书本目标请求与实际实例不一致')
    if snapshot['remove']:return None
    identity=snapshot.get('target',{})
    if identity.get('instance_id'):
        from .assets import find_root
        target=find_root(scene,identity['instance_id'])
        if target:require_owned(target,(target,))
    else:
        target=scene.objects.get(identity.get('name',''))
        if target is not None and target.session_uid!=identity.get('session_uid'):target=None
    if target is None or target==root or target in root.children_recursive:
        raise ValueError('明确选择的玩家目标已删除、替换或形成依赖循环')
    return target


def _preflight(scene,root,anchor):
    asset_contracts.validate(root)
    if (asset_rigs.driver_contract(root)!=root.get(ROOT_DRIVERS)
            or asset_rigs.driver_contract(anchor)!=root.get(ANCHOR_DRIVERS)):
        raise ValueError('书本原生驱动已由用户修改，未覆盖')
    if _animated(root,DISTANCE):
        raise ValueError('玩家距离含用户Action/NLA，未覆盖原动画')
    previous=root.get(TARGET)
    generated=root.get(TARGET_SCHEMA)==1
    constraint=anchor.constraints.get(FOLLOW)
    curve=next((curve for curve in root.animation_data.drivers if curve.data_path==DISTANCE),None)
    if not generated and (constraint is not None or curve is not None):
        raise ValueError('玩家跟随位置已有未知约束或驱动，未覆盖')
    if generated:
        if previous is not None and not isinstance(previous,bpy.types.Object):raise ValueError('玩家目标引用不完整')
        expected='distance/max(abs(size),.000001)' if previous else '0'
        if curve is None or curve.driver.expression!=expected:
            raise ValueError('玩家距离驱动不是已知生成合同')
        variables={variable.name:variable for variable in curve.driver.variables}
        if previous:
            if set(variables)!={'distance','size'}:raise ValueError('玩家距离变量已修改')
            distance,size=variables['distance'],variables['size']
            if (distance.type!='LOC_DIFF' or distance.targets[0].id!=root or distance.targets[1].id!=previous
                    or any(target.bone_target or target.transform_space!='WORLD_SPACE' for target in distance.targets)
                    or size.type!='TRANSFORMS' or size.targets[0].id!=root or size.targets[0].bone_target
                    or size.targets[0].transform_type!='SCALE_X' or size.targets[0].transform_space!='WORLD_SPACE'):
                raise ValueError('玩家距离目标已由用户修改')
            if (constraint is None or constraint.type!='LOCKED_TRACK' or constraint.target!=previous
                    or constraint.track_axis!='TRACK_NEGATIVE_Y' or constraint.lock_axis!='LOCK_Z'
                    or constraint.mute or constraint.subtarget or constraint.head_tail!=0):
                raise ValueError('玩家方向约束已由用户修改')
            influence=next((item for item in anchor.animation_data.drivers if item.data_path==constraint.path_from_id('influence')),None)
            if influence is None or influence.driver.expression!='v' or len(influence.driver.variables)!=1:
                raise ValueError('玩家方向影响驱动已由用户修改')
            variable=influence.driver.variables[0]
            if (variable.name!='v' or variable.type!='SINGLE_PROP' or variable.targets[0].id!=root
                    or variable.targets[0].data_path!='["'+AUTO+'"]'):
                raise ValueError('玩家方向影响目标已由用户修改')
        elif variables or constraint is not None:
            raise ValueError('未绑定玩家的机构与生成合同冲突')
    for owner in (root,anchor):
        for item in owner.animation_data.drivers:
            if item.mute:raise ValueError('书本生成驱动已被用户停用，未覆盖')
            for variable in item.driver.variables:
                for target in variable.targets:
                    allowed={root,scene,previous} if owner==root and item.data_path==DISTANCE else {root,scene}
                    if target.id is not None and target.id not in allowed:
                        raise ValueError('书本驱动引用未知用户数据，未接管')
                    if item.data_path=='["'+AUTO+'"]':
                        raise ValueError('玩家自动开合已有用户驱动，未覆盖')


def _write_target(root,anchor,target,*,distance_driver=True):
    constraint=anchor.constraints.get(FOLLOW)
    if constraint:
        constraint.driver_remove('influence');anchor.constraints.remove(constraint)
    root.driver_remove(DISTANCE)
    if not distance_driver:return
    curve=driver(root,DISTANCE,'0' if target is None else 'distance/max(abs(size),.000001)')
    if target is None:return
    distance=curve.driver.variables.new();distance.name='distance';distance.type='LOC_DIFF'
    distance.targets[0].id=root;distance.targets[1].id=target
    for item in distance.targets:item.transform_space='WORLD_SPACE'
    size=curve.driver.variables.new();size.name='size';size.type='TRANSFORMS'
    size.targets[0].id=root;size.targets[0].transform_type='SCALE_X';size.targets[0].transform_space='WORLD_SPACE'
    constraint=anchor.constraints.new('LOCKED_TRACK');constraint.name=FOLLOW
    constraint.target=target;constraint.track_axis='TRACK_NEGATIVE_Y';constraint.lock_axis='LOCK_Z'
    driver(constraint,'influence','v',root,AUTO)


def bind_target(scene,root,snapshot):
    """Explicit undoable binding, preserving unrelated IDs and user actions."""
    ensure_mutation_allowed()
    anchor=_binding(scene,root);target=_resolve(scene,root,snapshot);_preflight(scene,root,anchor)
    sentinel=object();keys=(TARGET,TARGET_SCHEMA,AUTO,'内部玩家距离',ROOT_DRIVERS,ANCHOR_DRIVERS,
                            asset_contracts.PROPERTY,'oxal_mc_studio_contract_refs')
    before={key:root.get(key,sentinel) for key in keys}
    if before['oxal_mc_studio_contract_refs'] is not sentinel:
        before['oxal_mc_studio_contract_refs']=dict(before['oxal_mc_studio_contract_refs'].items())
    had_distance=any(curve.data_path==DISTANCE for curve in root.animation_data.drivers)
    previous=root.get(TARGET)
    def restore():
        _write_target(root,anchor,previous,distance_driver=had_distance)
        for key,value in before.items():
            if value is sentinel:
                if key in root:del root[key]
            else:root[key]=value
        root.update_tag();anchor.update_tag()
    with Creation() as transaction:
        transaction.on_rollback(restore)
        _write_target(root,anchor,target)
        if target is None:
            if TARGET in root:del root[TARGET]
        else:root[TARGET]=target
        root[TARGET_SCHEMA]=1;root[AUTO]=target is not None
        root[ROOT_DRIVERS]=asset_rigs.driver_contract(root)
        root[ANCHOR_DRIVERS]=asset_rigs.driver_contract(anchor)
        asset_contracts.stamp(root)
        root.update_tag();anchor.update_tag()
        transaction.commit()
    return {'message':'已解除书本玩家跟随' if target is None else '已绑定书本玩家跟随'}


def stage_target(transaction,scene,staged,actual_root):
    """Rebuild only freshly tracked providers for an already proven binding."""
    if actual_root.get(SCHEMA)!=1:return
    actual_anchor=_binding(scene,actual_root)
    _preflight(scene,actual_root,actual_anchor)
    if actual_root.get(TARGET_SCHEMA)!=1:return
    anchor,book=staged.get('内部书本朝向'),staged.get('内部书本骨架')
    if (staged.get(SCHEMA)!=1 or not isinstance(anchor,bpy.types.Object)
            or not isinstance(book,bpy.types.Object) or book.parent!=anchor
            or any(item not in transaction.ids for item in (staged,anchor,book,book.data))):
        raise ValueError('书本重建目标不是本次明确创建的原生机构')
    target=actual_root.get(TARGET)
    _write_target(staged,anchor,target)
    if target is not None:staged[TARGET]=target
    staged[TARGET_SCHEMA]=1
    staged[AUTO]=actual_root[AUTO]
