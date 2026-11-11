"""Explicit legacy BaiPo migration; old IDs are archived, never owned or deleted.

asset_id=oxal.human.legacy_migration; version=1; policy=NEW_INSTANCE;
scope=one confirmed local legacy collection; source=LEGACY_MIGRATION.json.
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import owned_ids, require_owned
from ...util.resources import (
    ASSET_ID,
    INSTANCE,
    VERSION,
    resolve_scene,
    resolve_view_layer,
)
from . import human_migrate_animation as animation
from .human_source import CURRENT, skin_materials, skin_nodes

MANIFEST = Path(__file__).resolve().parents[2] / 'asset/human/LEGACY_MIGRATION.json'
REPLACEMENT = 'oxal_mc_studio_legacy_replacement'
_BETA_SOURCES = frozenset({'2.4.0', '2.4.2', '2.4.3', '2.4.4', '2.4.5', '2.4.6'})


def _manifest():
    data = json.loads(MANIFEST.read_text(encoding='utf8'))
    if data.get('schema') != 1:
        raise ValueError('旧人模识别资源版本无效')
    return data


def _local(owner):
    if owner.library or owner.override_library:
        raise ValueError('请先将旧人模完整本地化，再执行迁移')


def _beta_source(scene, root, rig):
    """Prove an old Beta through actual registry/main-rig references, not names."""
    from .human import resolve_human, rig_of
    from .human_source import model_version

    version = model_version(root)
    if version not in _BETA_SOURCES or version == CURRENT:
        raise ValueError('仅支持已知旧版 Beta 人模迁移，当前或未知版本不可自迁移')
    if resolve_human(scene, str(root.get(INSTANCE, ''))) != root or rig_of(root) != rig:
        raise ValueError('旧 Beta 实例或主骨架的实际归属不匹配')
    require_owned(root, (root, *owned_ids(root), *root.children_recursive,
                         *root.all_objects, rig.data))
    return version


def _source(context):
    obj = context.object
    if obj is None:
        raise ValueError('请先选中旧版白珀人模的骨架或身体网格')
    candidates = {obj} if obj.type == 'ARMATURE' else set()
    parent = obj.parent
    while parent is not None:
        if parent.type == 'ARMATURE':
            candidates.add(parent)
        parent = parent.parent
    candidates.update(mod.object for mod in obj.modifiers if mod.type == 'ARMATURE' and mod.object)
    if len(candidates) != 1:
        raise ValueError('无法唯一识别旧人模主骨架，请直接选中主骨架')
    rig = candidates.pop()
    if rig not in tuple(context.scene.objects):
        raise ValueError('旧人模不在当前场景')
    roots = [c for c in rig.users_collection if c in tuple(context.scene.collection.children_recursive)
             and len([o for o in c.all_objects if o.type == 'ARMATURE']) == 1
             and any(o.type == 'MESH' for o in c.all_objects)]
    if not roots:
        raise ValueError('旧人模需处于独立合集，合集内不能包含其它人模骨架')
    roots.sort(key=lambda c:len(c.all_objects))
    root = roots[0]
    if root.get(ASSET_ID) == 'oxal.human':
        profiles = [{'version': _beta_source(context.scene, root, rig), 'owned_beta': True}]
    else:
        data = _manifest()
        declared_version = root.get('bp_model_version')
        if declared_version == CURRENT:
            raise ValueError('所选完整人模已是当前版本，无需迁移')
        for name, expected in data['anchors'].items():
            bone = rig.data.bones.get(name)
            if (bone is None or (bone.parent.name if bone.parent else '') != expected['parent']
                    or max(abs(a-b) for row, other in zip(bone.matrix_local, expected['matrix_local'], strict=True)
                           for a,b in zip(row,other,strict=True)) > data['tolerance']
                    or max(abs(a-b) for a,b in zip(bone.tail_local,expected['tail'],strict=True)) > data['tolerance']):
                raise ValueError('所选骨架不符合已核对的白珀人模 2.3.1+ 结构：' + name)
        names = set(rig.data.bones.keys())
        profiles = [p for p in data['sources'] if set(p['bone_names']) <= names]
        if declared_version is not None:
            profiles = [p for p in profiles
                        if p.get('version_properties', {}).get('bp_model_version') == declared_version]
        if not profiles:
            raise ValueError('旧人模骨骼缺失或版本未知，未猜测迁移')
    _local(root); _local(rig); _local(rig.data)
    objects = tuple(root.all_objects)
    if any(o.type in {'LIGHT','CAMERA'} for o in objects):
        raise ValueError('旧人模合集含灯光或相机，请先整理到独立人模合集')
    if sum(root in tuple(s.collection.children_recursive) for s in bpy.data.scenes) != 1:
        raise ValueError('旧人模合集被多个场景共享，请先建立独立副本')
    collections = {root, *root.children_recursive}
    shapes = {p.custom_shape for p in rig.pose.bones if p.custom_shape is not None}
    for item in objects:
        _local(item)
        if item not in shapes and any(c not in collections for c in item.users_collection):
            raise ValueError('旧人模对象同时属于外部合集，未隐藏共享对象：' + item.name)
    replacement = root.get(REPLACEMENT)
    if replacement is not None and any(c.get(INSTANCE) == replacement for c in context.scene.collection.children_recursive):
        raise ValueError('此旧人模已经迁移；原版保留隐藏，未重复创建')
    animation.validate(rig)
    return root, rig, profiles


def _signature(root, rig, scene, layer):
    from array import array

    image = _skin(root)
    pixels = array('f', [0.0]) * len(image.pixels)
    image.pixels.foreach_get(pixels)
    layers = []
    def visit(view, current, path=()):
        current_path = (*path, current.collection.session_uid)
        if current.collection == root or root.session_uid in path:
            layers.append((view.name, current_path, current.exclude, current.hide_viewport,
                           current.holdout, current.indirect_only))
        for child in current.children:
            visit(view, child, current_path)
    for view in scene.view_layers:
        visit(view, view.layer_collection)
    objects = tuple(root.all_objects)
    payload = [root.session_uid, rig.session_uid, rig.data.session_uid,
               root.get(ASSET_ID), root.get(INSTANCE), root.get(VERSION),
               sorted((o.session_uid, o.name, o.type, getattr(o.data, 'session_uid', 0),
                       o.hide_viewport, o.hide_render,
                       o.hide_get(view_layer=layer) if o in tuple(layer.objects) else None) for o in objects),
               [(b.name, b.parent.name if b.parent else '', [list(row) for row in b.matrix_local])
                for b in rig.data.bones],
               image.session_uid, list(image.size), image.colorspace_settings.name, image.alpha_mode,
               hashlib.sha256(pixels.tobytes()).hexdigest(),
               [(c.session_uid, c.hide_viewport, c.hide_render) for c in (root, *root.children_recursive)],
               scene.frame_current, scene.frame_subframe, layer.name, layers,
               [list(row) for row in rig.matrix_world], animation.fingerprint(rig)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _name_matches(actual, authored):
    return actual == authored or (actual.startswith(authored + '.') and actual[len(authored)+1:].isdigit())


def _legacy_objects(root, rig, profiles):
    """Validate the entire old collection before moving it, without owning it."""
    if root.get(ASSET_ID) == 'oxal.human':
        _beta_source(bpy.context.scene, root, rig)
        return tuple(root.all_objects)
    roles = [role for profile in profiles for role in profile['object_roles']]
    shapes = {bone.custom_shape for bone in rig.pose.bones if bone.custom_shape is not None}
    objects = tuple(root.all_objects)
    for obj in objects:
        if obj == rig or obj in shapes:
            continue
        matches = [role for role in roles if _name_matches(obj.name, role['name']) and obj.type == role['type']]
        if not matches:
            raise ValueError('旧人模合集含未识别对象，请先移出再迁移：' + obj.name)
        if obj.type == 'MESH' and not any(role.get('mesh_counts') == [len(obj.data.vertices), len(obj.data.edges), len(obj.data.polygons)] for role in matches):
            raise ValueError('旧人模网格已改变，无法确认归档范围：' + obj.name)
    return objects


def _skin(root):
    bodies = [o for o in root.all_objects if o.type == 'MESH' and o.name.split('.')[0] == '身体']
    images = set()
    for obj in bodies:
        for slot in obj.material_slots:
            material = slot.material
            if material and material.node_tree:
                images.update(n.image for n in material.node_tree.nodes if n.type == 'TEX_IMAGE'
                              and n.image is not None and tuple(n.image.size) in {(64,64),(128,128)})
    if len(images) != 1:
        raise ValueError('旧人模身体皮肤不是唯一64/128图像，请先整理主皮肤')
    return images.pop()


def snapshot(context):
    """Read-only confirmation snapshot. Never creates data or hides the source."""
    if context.mode != 'OBJECT':
        raise ValueError('请在物体模式选择旧人模后迁移')
    root, rig, profiles = _source(context)
    _skin(root)
    from .human_migrate_archive import plan

    objects = _legacy_objects(root, rig, profiles)
    plan(context.scene, root, rig, objects)
    return {'root_uid':root.session_uid,'rig_uid':rig.session_uid,'source_name':rig.name,
            'collection_name':root.name,'signature':_signature(root,rig,context.scene,context.view_layer),
            'versions':sorted({p['version'] for p in profiles}), 'target_version':CURRENT,
            'archived_objects':len(objects),**animation.estimate(rig)}


def _style(root, rig, context, choices):
    def visible(collection_name):
        found = [c for c in root.children_recursive if c.name.split('.')[0] == collection_name]
        return any(o.visible_get(view_layer=context.view_layer) for c in found for o in c.all_objects if o.type=='MESH')
    values = {}
    for field, first, second, a, b in [('arm_mode','手臂-三格','手臂-四格','SLIM','WIDE'),
                                       ('leg_mode','腿-常规弯曲','腿-官方动画','NORMAL','OFFICIAL')]:
        value=choices.get(field,'AUTO')
        if value=='AUTO':
            pair=visible(first),visible(second)
            if pair[0]==pair[1]:
                raise ValueError('旧人模同时显示或隐藏两种样式，请在确认窗口明确选择胳膊和腿样式')
            value=a if pair[0] else b
        if value not in {a,b}:
            raise ValueError('迁移样式无效')
        values[field]=value
    ankles=[rig.data.bones[n].parent.name for n in ('L.脚腕','R.脚腕')]
    if ankles not in [['L.脚控制器','R.脚控制器'],['L.小腿中间节','R.小腿中间节']]:
        raise ValueError('旧人模脚腕绑定已自定义，未猜测初始固定方式')
    values['foot_fixed']=ankles==['L.脚控制器','R.脚控制器']
    return values


def migrate(tool, scene_uid, options):
    ensure_mutation_allowed()
    scene=resolve_scene(scene_uid);layer=resolve_view_layer(scene,require_active=True)
    expected=options.get('legacy')
    if not isinstance(expected,dict) or options.get('confirmed') is not True:
        raise ValueError('请先通过旧人模迁移确认窗口确认操作')
    if expected.get('target_version', CURRENT) != CURRENT:
        raise ValueError('确认后的目标人模版本已变化，请重新确认迁移')
    # Resolve the frozen object identity, never the current selection.
    matching=[o for o in scene.objects if o.session_uid==expected.get('rig_uid')]
    if len(matching)!=1 or bpy.context.mode!='OBJECT':
        raise ValueError('旧人模或模式已变化，请重新选择并确认')
    source=matching[0]
    context=SimpleNamespace(object=source,view_layer=layer,mode='OBJECT',scene=scene)
    root,source,profiles=_source(context)
    if root.session_uid!=expected.get('root_uid') or _signature(root,source,scene,layer)!=expected.get('signature'):
        raise ValueError('确认后的旧人模或动画已变化，请重新确认')
    image=_skin(root)
    from .human_migrate_archive import plan

    archive = plan(scene, root, source, _legacy_objects(root, source, profiles))
    choices=_style(root,source,context,options.get('style',{}))
    choices['creation_id']=options['creation_id']
    marker=root.get(REPLACEMENT)
    counts={}
    def prepare(tx,new_root,new_rig):
        nonlocal counts
        # Relocate all top-level native helpers with the main rig, keeping the
        # old rig's parent and transform animation basis on the new rig.
        delta=source.matrix_world @ new_rig.matrix_world.inverted()
        top=tuple(o for o in new_root.all_objects if o.parent is None)
        for obj in top:
            obj.matrix_world=delta @ obj.matrix_world
        new_rig.parent=source.parent
        new_rig.parent_type=source.parent_type
        new_rig.parent_bone=source.parent_bone
        new_rig.matrix_parent_inverse=source.matrix_parent_inverse.copy()
        new_rig.rotation_mode=source.rotation_mode
        new_rig.matrix_basis=source.matrix_basis.copy()
        copied=image.copy();tx.track(copied)
        if copied.packed_file is None:copied.pack()
        from .human_state import OWNER
        copied[OWNER]=new_root[INSTANCE]
        for material in skin_materials(new_root):
            for node in skin_nodes(material):node.image=copied
        from .human_layer_resolution import prepare as prepare_resolution
        prepare_resolution(tx,new_root,tuple(new_root.all_objects),copied)
        new_root['oxal_human_current_skin']=copied
        new_root['oxal_human_skin_pixels']=int(copied.size[0])
        new_root['oxal_human_skin_label']=image.name
        counts=animation.copy_animation(tx,source,new_rig)
        new_root['oxal_human_migration_source']=source
        new_root['oxal_human_migration_collection']=root
        root[REPLACEMENT] = new_root[INSTANCE]
        archive.apply(new_root)

    def rollback():
        archive.rollback()
        if marker is None:
            if REPLACEMENT in root:
                del root[REPLACEMENT]
        else:
            root[REPLACEMENT] = marker

    try:
        result = tool._create(scene_uid, choices, prepare=prepare, rollback=rollback)
    except Exception:
        rollback()
        raise
    return {'migrated_instance_id':result.instance_id,'source':source.name,'target_version':CURRENT,
            'counts':counts,'message':f'已迁移到 {CURRENT}：复制 {counts["copied_curves"]} 条动画曲线，跳过 {counts["skipped_curves"]} 条不匹配通道；旧版未删除，已放入“旧人模”合集并从所有视图层排除'}
