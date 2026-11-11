"""Explicit staged repair preserving instance and compatible native ID bindings."""

import json
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import bpy
from mathutils import Matrix, Vector

from ...util import drivers
from ...util.animation import action_fcurves
from ...util.ownership import (
    REGISTRY,
    explicit_scene_ids,
    owned_ids,
    owns,
    require_owned,
)
from ...util.resources import ASSET_ID, INSTANCE, VERSION, live_ids
from . import asset_contracts as contracts
from . import asset_rigs as rigs


@contextmanager
def object_mode(fallback_root=None):
    active = cast(Any, bpy.context.object)
    mode = bpy.context.mode
    selected = tuple(bpy.context.selected_objects)
    scene = cast(bpy.types.Scene, bpy.context.scene)
    bone = active.data.bones.active.name if mode == "POSE" and active.data.bones.active else None
    if mode == "POSE":
        bpy.ops.object.mode_set(mode="OBJECT")
    try:
        yield
    finally:
        for obj in tuple(bpy.context.selected_objects):
            obj.select_set(False)
        for item in live_ids(selected):
            obj = cast(bpy.types.Object, item)
            if scene.objects.get(obj.name) == obj and not obj.get("oxal_mc_studio_slot_internal_helper"):
                obj.select_set(True)
        alive = live_ids((active,)) if active is not None else ()
        active = cast(Any, alive[0]) if alive else None
        if active is not None and scene.objects.get(active.name) == active and not active.get("oxal_mc_studio_slot_internal_helper"):
            cast(bpy.types.ViewLayer,bpy.context.view_layer).objects.active = active
            if mode == "POSE":
                if bone and bone in active.data.bones:
                    active.data.bones.active = active.data.bones[bone]
                bpy.ops.object.mode_set(mode="POSE")
        elif fallback_root is not None:
            from ..inventory.inventory_helpers import visible_selection
            candidate = visible_selection(fallback_root)
            if candidate is not None:
                candidate.select_set(True)
                cast(bpy.types.ViewLayer, bpy.context.view_layer).objects.active = candidate


def _key(obj, *, textures=True):
    values = [obj.type,obj.parent_bone,obj.get("oxal_mc_studio_model_part",""),obj.get("oxal_mc_studio_structure_part",""),
              obj.get("oxal_mc_studio_bed_color",-1),obj.get("oxal_mc_studio_fishing_phase",""),
              obj.get("oxal_mc_studio_representation",""),bool(obj.get("oxal_mc_studio_effect_geometry")),obj.get("oxal_mc_studio_native_state",-1)]
    values.extend((obj.get("oxal_mc_studio_native_particle_kind",""),obj.get("oxal_mc_studio_source_variant","")))
    if textures and obj.type == "MESH":
        values.append(tuple(slot.material.get("oxal_mc_studio_texture_key","") if slot.material else "" for slot in obj.material_slots))
    if obj.get("oxal_mc_studio_effect_geometry"):
        values.append(tuple((p,i,e) for p,i,_,e,_ in rigs.driver_records(obj)))
    return tuple(values)


def _pairs(old,new):
    remaining_old,remaining_new = set(old),set(new)
    mapping = {}
    def geometry(obj):
        if obj.type != "MESH":return None
        return tuple(sorted(tuple(round(float(value),6) for value in vertex.co) for vertex in obj.data.vertices))
    for textures in (True,False):
        old_groups,new_groups = defaultdict(list),defaultdict(list)
        for obj in remaining_old:
            old_groups[_key(obj,textures=textures)].append(obj)
        for obj in remaining_new:
            new_groups[_key(obj,textures=textures)].append(obj)
        for key,previous in old_groups.items():
            following = new_groups.get(key,[])
            if len(previous) == len(following) == 1:
                mapping[following[0]] = previous[0]
                remaining_old.remove(previous[0])
                remaining_new.remove(following[0])
            elif previous and following:
                # Repeated state parts share the same role/texture key. Only
                # exact immutable vertex layouts may disambiguate them; no
                # names or arbitrary ordering establish a correspondence.
                left,right=defaultdict(list),defaultdict(list)
                for obj in previous:
                    token=geometry(obj)
                    if token is not None:left[token].append(obj)
                for obj in following:
                    token=geometry(obj)
                    if token is not None:right[token].append(obj)
                for token,items in left.items():
                    candidates=right.get(token,[])
                    if len(items)==len(candidates)==1:
                        mapping[candidates[0]]=items[0]
                        remaining_old.remove(items[0]);remaining_new.remove(candidates[0])
    # A newly introduced state controller still represents the same original
    # body. Match its initial variant only when that correspondence is unique.
    for old_obj in tuple(remaining_old):
        candidates = [obj for obj in remaining_new if obj.type == old_obj.type and (obj.parent_bone == old_obj.parent_bone or old_obj.parent_type != "BONE") and obj.get("oxal_mc_studio_native_state",-1) == 0 and not obj.get("oxal_mc_studio_effect_geometry")]
        if len(candidates) == 1:
            mapping[candidates[0]] = old_obj
            remaining_old.remove(old_obj)
            remaining_new.remove(candidates[0])
    return mapping,remaining_old,remaining_new


def _mapped_records(owner,mapping):
    records = rigs.driver_records(owner)
    for _,_,_,_,variables in records:
        for _,_,targets in variables:
            for target in targets:
                value = target.get("id")
                if value in mapping:
                    target["id"] = mapping[value]
    return records


def _animated_channels(owner):
    """Include every bound Action slot, even muted or nested NLA strips."""
    animation = owner.animation_data
    if animation is None:
        return set()

    def bindings(strips):
        for strip in strips:
            yield strip
            yield from bindings(getattr(strip,"strips",()))

    sources = [animation, *[strip for track in animation.nla_tracks for strip in bindings(track.strips)]]
    return {(curve.data_path,curve.array_index) for source in sources
            for curve in action_fcurves(source.action,slot=getattr(source,"action_slot",None))}


def _animation_preflight(root,staged,mapping):
    """An unchanged Action ID is insufficient if its native provider changes.

    A released Empty's property animation cannot drive the same-named property
    on a new helper rig. Existing rigs likewise cannot retain an Action channel
    underneath an incoming generated driver. Refuse those exact channels before
    publishing any replacement; unrelated root transform animation stays valid.
    """
    conflicts = set()
    for incoming,current in mapping.items():
        driven = {(path,axis) for path,axis,*_ in rigs.driver_records(incoming)}
        conflicts.update((current.name,path) for path,axis in _animated_channels(current)
                         if (path,axis) in driven)
        if current.type == incoming.type == "ARMATURE":
            previous_controls = {spec["key"]: spec for spec in json.loads(current.get("oxal_mc_studio_state_controls", "[]"))}
            for spec in json.loads(incoming.get("oxal_mc_studio_state_controls", "[]")):
                previous_control = previous_controls.get(spec["key"])
                if (previous_control is None or (previous_control["min"], previous_control["max"]) == (spec["min"], spec["max"])):
                    continue
                bone = previous_control.get("bone", "控制 · " + previous_control["label"])
                paths = {'pose.bones[' + json.dumps(bone, ensure_ascii=ascii_only) + '].location' for ascii_only in (False, True)}
                conflicts.update((current.name, path) for path, axis in _animated_channels(current) if path in paths and axis == 0)
    if root != mapping.get(staged,staged):
        migrated = {'['+json.dumps(key,ensure_ascii=False)+']' for key in staged.keys()}  # noqa: SIM118 -- Blender ID iteration is not key iteration.
        conflicts.update((root.name,path) for path,_ in _animated_channels(root) if path in migrated)
    legacy_profile=root.get("oxal_mc_studio_effect_profile","")
    if legacy_profile in {"smoke+flame","dust","smoke","campfire_cosy_smoke","end_rod"}:
        legacy={"effect_speed","effect_strength","effect_lifetime","effect_scale","smoke_enabled","particles_enabled","light_enabled"}
        paths={'['+json.dumps(key)+']':key for key in legacy if key in root}
        consumers=set();seen=set()
        def inspect(owner):
            if owner is None or owner in seen:return
            seen.add(owner)
            for curve in getattr(getattr(owner,"animation_data",None),"drivers",()):
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if target.id==staged:consumers.add(target.data_path)
            tree=getattr(owner,"node_tree",None)
            if tree is not None:inspect(tree)
            for node in getattr(owner,"nodes",()):
                if node.type=="GROUP" and node.node_tree is not None:inspect(node.node_tree)
        for item in (staged,*tuple(staged.children_recursive)):
            inspect(item);data=getattr(item,"data",None);inspect(data)
            for material in getattr(data,"materials",()):inspect(material)
            for modifier in item.modifiers:
                if modifier.type=="NODES":inspect(modifier.node_group)
        removed={path:key for path,key in paths.items() if path not in consumers}
        conflicts.update((root.name,path) for path,_ in _animated_channels(root) if path in removed)
        for spec in json.loads(root.get("oxal_mc_studio_state_controls","[]")):
            if spec.get("key") not in removed.values() and spec.get("property") not in removed.values():continue
            bone=spec.get("bone","控制 · "+spec["label"])
            prefix='pose.bones['+json.dumps(bone,ensure_ascii=False)+'].'
            conflicts.update((root.name,path) for path,_ in _animated_channels(root) if path.startswith(prefix))
    if conflicts:
        raise ValueError("新控件会覆盖或迁移已有Action/NLA动画通道，保留现状，未重建："+
                         "、".join(name+" "+path for name,path in sorted(conflicts)))


def _explicit_scene_objects(scene):
    """Separate user Scene links from native collection/view-layer membership."""
    return {item for item in explicit_scene_ids(scene) if isinstance(item, bpy.types.Object)}


def _slot_helper_reference(owner, helper, root, destination):
    """Read candidate consumers only, excluding the exact generated ID edges.

    Ownership does not imply that every use is generated: an owned material or
    child can also contain a user's object coordinates, modifier, or raw ID
    property. Never evaluate registered Python getters while checking them.
    """
    seen = set()

    def custom(value):
        if isinstance(value, bpy.types.ID):
            return value == helper
        if hasattr(value, "items"):
            return any(custom(part) for part in value.values())
        if isinstance(value, (tuple, list)):
            return any(custom(part) for part in value)
        return False

    def visit(value):
        if value is None or not hasattr(value, "bl_rna") or value.as_pointer() in seen:
            return False
        seen.add(value.as_pointer())
        if hasattr(value, "items"):
            try:
                properties = tuple(value.items())
            except TypeError:
                properties = ()
            for key, item in properties:
                if value == root and key in {REGISTRY, "oxal_mc_studio_contract_refs"}:
                    continue
                if custom(item):
                    return True
        for prop in value.bl_rna.properties:
            if prop.is_runtime or prop.identifier in {"rna_type", "id_data"}:
                continue
            if value == owner and isinstance(owner, bpy.types.Object) and prop.identifier == "parent" and owner.parent == helper:
                continue
            if value == destination and prop.identifier in {"objects", "all_objects"}:
                continue
            if prop.type not in {"POINTER", "COLLECTION"}:
                continue
            item = getattr(value, prop.identifier)
            for part in tuple(item) if prop.type == "COLLECTION" else (item,):
                if isinstance(part, bpy.types.ID):
                    if part == helper or (part.is_embedded_data and visit(part)):
                        return True
                elif visit(part):
                    return True
        return False

    return visit(owner)


def _retired_slot_helper(root, destination, helper, users, old_objects, scene, scene_references):
    """Validate only the display/phase Empties removed by SLOT compaction."""
    role = str(helper.get("oxal_mc_studio_model_part", ""))
    if (root.get("oxal_mc_studio_presentation_context") != "slot" or helper.type != "EMPTY"
            or not (role == "库存 · GUI显示变换" or role.startswith("库存 · 持物形态 · "))):
        return False
    for user in users:
        if user == scene and helper not in scene_references:
            continue
        generated = (user in (root, destination)
                     or (isinstance(user, bpy.types.Object) and user in old_objects and user.parent == helper))
        if not generated or _slot_helper_reference(user, helper, root, destination):
            raise ValueError("待移除格子辅助对象存在用户引用，未重建：" + helper.name + " ← " + user.name)
    return True


def _pose_snapshot(rig):
    if rig is None:
        return {}
    return {pose.name:{"matrix":pose.matrix_basis.copy(),"shape":pose.custom_shape,"properties":_value(dict(pose.items())),"bone_properties":_value(dict(pose.bone.items())),
                       "rotation_mode":pose.rotation_mode,"locks":(tuple(pose.lock_location),tuple(pose.lock_rotation),tuple(pose.lock_scale)),
                       "shape_scale":tuple(pose.custom_shape_scale_xyz),"shape_rotation":tuple(pose.custom_shape_rotation_euler),
                       "shape_translation":tuple(pose.custom_shape_translation)} for pose in rig.pose.bones}


def _value(value):
    if isinstance(value,bpy.types.ID):
        return value
    if hasattr(value,"items"):
        return {key:_value(item) for key,item in value.items()}
    if hasattr(value,"to_list"):
        return value.to_list()
    return value


def _constraint(source,pose,mapping):
    target = pose.constraints.get(source.name)
    if target is not None and target.type != source.type:
        raise ValueError("骨骼约束与新资源冲突，保留用户结构")
    target = target or pose.constraints.new(source.type)
    target.name = source.name
    for prop in source.bl_rna.properties:
        if prop.is_readonly or prop.identifier in {"rna_type","type","name"} or prop.type == "COLLECTION":
            continue
        value = getattr(source,prop.identifier)
        if prop.type == "POINTER":
            value = mapping.get(value,value)
        elif getattr(prop,"is_array",False):
            value = tuple(value)
        setattr(target,prop.identifier,value)


def _rig_publish(target,source,mapping,previous):
    literal_controls={spec.get("bone") for spec in json.loads(source.get("oxal_mc_studio_state_controls","[]"))}
    missing = set(previous)-set(source.pose.bones.keys())
    if missing and target.animation_data:
        animation = target.animation_data
        def nested(strips):
            for strip in strips:
                yield strip
                yield from nested(getattr(strip,"strips",()))
        bindings = [animation,*[strip for track in animation.nla_tracks for strip in nested(track.strips)]]
        curves = [curve for binding in bindings for curve in action_fcurves(binding.action,slot=getattr(binding,"action_slot",None))]
        keyed_missing = [name for name in missing if any(curve.data_path.startswith('pose.bones['+json.dumps(name,ensure_ascii=False)+']') for curve in curves)]
        if keyed_missing:
            raise ValueError("新形态会移除现有动画骨骼，未替换："+"、".join(sorted(keyed_missing)))
    if any(target.pose.bones[name].constraints for name in missing):
        raise ValueError("将移除的骨骼含用户约束，保留现状")
    for staged_pose in source.pose.bones:
        current = target.pose.bones.get(staged_pose.name)
        if current is None:
            continue
        for constraint in tuple(staged_pose.constraints):
            existing = current.constraints.get(constraint.name)
            if existing is None:
                # Default native constraint names depend on Blender's locale.
                # The old rig already passed its exact saved contract; only a
                # unique equivalent generated constraint may bridge the name.
                candidates = [item for item in current.constraints if _same_constraint(constraint, item, mapping)]
                if len(candidates) == 1:
                    existing = candidates[0]
                    prior_path = constraint.path_from_id()
                    constraint.name = existing.name
                    following_path = constraint.path_from_id()
                    animation = source.animation_data
                    for curve in animation.drivers if animation else ():
                        if curve.data_path.startswith(prior_path):
                            curve.data_path = following_path + curve.data_path[len(prior_path):]
                        for variable in curve.driver.variables:
                            for field in variable.targets:
                                if field.id == source and field.data_path.startswith(prior_path):
                                    field.data_path = following_path + field.data_path[len(prior_path):]
            if existing is None or existing.type != constraint.type:
                raise ValueError("现有控件约束已改变，未覆盖")
            fields = ("owner_space","target_space","influence","use_transform_limit","subtarget","use_offset",
                      *(prefix+axis for prefix in ("use_min_","use_max_","min_","max_","use_","invert_") for axis in "xyz"))
            for field in fields:
                expected = getattr(constraint,field,None)
                actual = getattr(existing,field,None)
                if isinstance(expected,(int,float)) and isinstance(actual,(int,float)):
                    equal = abs(expected-actual)<1e-6
                else:
                    equal = expected == actual
                if not equal:
                    raise ValueError("现有控件的限制/绑定含用户修改，未覆盖")
            expected_target = getattr(constraint,"target",None)
            actual_target = getattr(existing,"target",None)
            if expected_target is not None and mapping.get(expected_target,expected_target) != actual_target:
                raise ValueError("现有骨骼约束实际目标已改变，未覆盖")
    target.data = source.data
    for pose in target.pose.bones:
        staged = source.pose.bones[pose.name]
        pose.rotation_mode = staged.rotation_mode
        pose.matrix_basis = staged.matrix_basis
        pose.custom_shape = staged.custom_shape
        pose.custom_shape_scale_xyz = staged.custom_shape_scale_xyz
        pose.custom_shape_rotation_euler = staged.custom_shape_rotation_euler
        pose.custom_shape_translation = staged.custom_shape_translation
        pose.lock_location,pose.lock_rotation,pose.lock_scale = staged.lock_location,staged.lock_rotation,staged.lock_scale
        for key,value in staged.items():
            pose[key] = value
        if pose.name in previous:
            for key,value in previous[pose.name]["properties"].items():
                pose[key] = value
            for key,value in previous[pose.name]["bone_properties"].items():
                pose.bone[key] = value
        # Generated mechanisms take their new rest pose. User-facing controls
        # retain the exact local pose and therefore their existing Action/NLA.
        if pose.name in previous and (pose.name in {"总控制","根控制","绳索显示"} or pose.name in literal_controls or pose.name.startswith(("控制 · ","手动 · ","鱼线","拴绳"))):
            prior = previous[pose.name]
            pose.rotation_mode = prior["rotation_mode"]
            pose.matrix_basis = prior["matrix"]
            from .asset_projectiles import restore_control_range
            restore_control_range(target, source, pose, prior)
            pose.lock_location,pose.lock_rotation,pose.lock_scale = prior["locks"]
            # The first Beta generated a flat master for banners. Replace only
            # that exact owned template; keep actual user custom shapes.
            legacy_banner_shape = False
            shape = prior["shape"]
            if pose.name == "总控制" and target.get("oxal_mc_studio_library_identifier","").endswith("_banner") and shape is not None and owns(target,shape):
                points = [(-.2,0,0),(.2,0,0),(0,-.2,0),(0,.2,0),(-.14,-.14,0),(.14,-.14,0),(.14,.14,0),(-.14,.14,0)]
                edges = [(0,1),(2,3),(4,5),(5,6),(6,7),(7,4)]
                legacy_banner_shape = (shape.type == "MESH" and len(shape.data.vertices) == len(points)
                                       and [tuple(edge.vertices) for edge in shape.data.edges] == edges
                                       and all((vertex.co-Vector(point)).length<1e-6 for vertex,point in zip(shape.data.vertices,points,strict=True)))
            if prior["shape"] is not None and not legacy_banner_shape:
                pose.custom_shape = prior["shape"]
                pose.custom_shape_scale_xyz = prior["shape_scale"]
                pose.custom_shape_rotation_euler = prior["shape_rotation"]
                pose.custom_shape_translation = prior["shape_translation"]
        for constraint in staged.constraints:
            _constraint(constraint,pose,mapping)
    rigs.install_drivers(target,_mapped_records(source,mapping))


def _same_constraint(source, target, mapping):
    def flat(values):
        for value in values:
            if hasattr(value, "__len__") and not isinstance(value, str):
                yield from flat(value)
            else:
                yield float(value)

    if source.type != target.type:
        return False
    for prop in source.bl_rna.properties:
        if prop.is_readonly or prop.identifier in {"rna_type", "type", "name", "active", "show_expanded"} or prop.type == "COLLECTION":
            continue
        expected, actual = getattr(source, prop.identifier), getattr(target, prop.identifier)
        if prop.type == "POINTER":
            expected = mapping.get(expected, expected)
        if getattr(prop, "is_array", False):
            if any(abs(a - b) > 1e-6 for a, b in zip(flat(expected), flat(actual), strict=True)):
                return False
        elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            if abs(expected - actual) > 1e-6:
                return False
        elif expected != actual:
            return False
    return True


def _materials(transaction,root,old_obj,new_obj,cache,enchanted):
    from ..enchantment import enchantment
    from . import assets
    old_materials = [slot.material for slot in old_obj.material_slots] if old_obj and old_obj.type == "MESH" else []
    for i,staged in enumerate(tuple(new_obj.data.materials)):
        if staged is None:
            continue
        key = staged.get("oxal_mc_studio_texture_key","")
        matched = [mat for mat in old_materials if mat and mat.get("oxal_mc_studio_texture_key","") == key]
        previous = matched[0] if len(matched) == 1 else old_materials[i] if i<len(old_materials) else None
        if previous is None:
            if enchanted and not new_obj.get("oxal_mc_studio_effect_geometry"):
                new_obj.data.materials[i] = assets._set_glint(transaction,root,staged)
            continue
        lookup = (previous,key)
        if lookup not in cache:
            from .asset_surfaces import variant_sampler, variant_surface
            result,variant_glint = variant_surface(transaction,root,previous,staged)
            uv_name = staged.get("oxal_mc_studio_glint_uv_name")
            if uv_name and previous.get("oxal_mc_studio_glint_uv_name") != uv_name:
                result = assets._private_material(transaction,previous)
                result["oxal_mc_studio_glint_uv_name"] = uv_name
            previous_key = previous.get("oxal_mc_studio_texture_key","")
            if previous_key and previous_key != key:
                _old_tree,old_source = variant_sampler(previous)
                new_tree,new_source = variant_sampler(staged)
                if old_source is None or new_source is None:
                    raise ValueError("用户材质没有可唯一更新的原版采样器，未覆盖变体")
                if result == previous:
                    result = assets._private_material(transaction,previous)
                result_tree,result_source = variant_sampler(result)
                if result_source is None:
                    raise ValueError("私有变体材质缺少实际采样器，未覆盖")
                result_source.image = new_source.image
                for prop in ("oxal_mc_studio_texture_key","oxal_mc_studio_original_texture","oxal_mc_studio_original_image","oxal_mc_studio_original_animation"):
                    if prop in staged:
                        result[prop] = staged[prop]
                    elif prop in result:
                        del result[prop]
                    if result_tree!=result.node_tree:
                        value=new_source.get(prop,staged.get(prop))
                        if value is None:
                            if prop in result_source:del result_source[prop]
                        else:result_source[prop]=value
                path = staged.get("oxal_mc_studio_original_texture")
                if path:
                    assets.geometry.configure_animation(result,path,scene=root.users_scene[0],tree=result_tree,image_node=result_source)
                    from . import asset_pbr
                    if asset_pbr.compatible(result,tree=result_tree):
                        from ...util.packed_images import semantic_filepath
                        maps={node.get(asset_pbr.ROLE):Path(bpy.path.abspath(semantic_filepath(node.image)))
                              for node in new_tree.nodes if node.type=='TEX_IMAGE' and node.image is not None
                              and node.get(asset_pbr.ROLE) in {'normal','pbr'}}
                        asset_pbr.refresh(transaction,result,Path(path),key=key,scene=root.users_scene[0],tree=result_tree,maps=maps)
                if enchantment.inspect(previous):
                    enchantment.stamp(result)
            from .asset_surfaces import bridge_existing, bridge_layers, bridge_shadow
            result = bridge_layers(transaction,root,result,staged,old_materials)
            result = bridge_existing(transaction,root,result,staged)
            result = bridge_shadow(transaction,root,result,staged)
            from ..inventory.inventory_gui import bridge_existing as bridge_gui

            result = bridge_gui(transaction, root, result, staged)
            from .asset_projectiles import bridge_material
            result = bridge_material(transaction, root, result, staged)
            if enchantment.inspect(result) or variant_glint or enchantment.inspect(previous):
                result = assets._set_glint(transaction,root,result)
            cache[lookup] = result
        new_obj.data.materials[i] = cache[lookup]


def _remap_created(transaction,mapping):
    for item in live_ids(transaction.ids):
        for key,value in tuple(item.items()):
            if isinstance(value,bpy.types.ID) and value in mapping:
                item[key] = mapping[value]
        for owner in (item,getattr(item,"node_tree",None)):
            animation = getattr(owner,"animation_data",None)
            if animation:
                for curve in animation.drivers:
                    for variable in curve.driver.variables:
                        for target in variable.targets:
                            if target.id in mapping:
                                target.id = mapping[target.id]
            nodes = getattr(owner,"nodes",None)
            if nodes:
                for node in nodes:
                    for socket in node.inputs:
                        value = getattr(socket,"default_value",None)
                        if isinstance(value,bpy.types.ID) and value in mapping:
                            socket.default_value = mapping[value]
        if isinstance(item,bpy.types.Object):
            if item.parent in mapping:
                item.parent = mapping[item.parent]
            for constraint in item.constraints:
                target = getattr(constraint,"target",None)
                if target in mapping:
                    cast(Any,constraint).target = mapping[target]
            for modifier in item.modifiers:
                for prop in ("object","target"):
                    target = getattr(modifier,prop,None)
                    if target in mapping:
                        setattr(modifier,prop,mapping[target])


def _image_stage(transaction,scene,collection,staged,root,objects):
    from . import assets
    from .asset_display import slot_transform
    templates = [obj for obj in objects if obj.type == "MESH" and not obj.get("oxal_mc_studio_effect_geometry") and obj.data.materials]
    if not templates:
        raise ValueError("图片资产缺少实际纹理材质，未重建")
    template = next((obj for obj in templates if obj.get("oxal_mc_studio_representation") == "GUI"), templates[0])
    slot = root.get("oxal_mc_studio_presentation_context") == "slot"
    roles = ("GUI","HELD") if slot and root.get("oxal_inventory_held") is not None else ("GUI",) if slot else ("",)
    kind = root.get("oxal_mc_studio_image_kind","ITEM")
    proxy = "minecraft:stone" if kind == "BLOCK" else "minecraft:apple"
    for key in ("oxal_mc_studio_image_kind", "oxal_mc_studio_image_depth", "oxal_mc_studio_image_path"):
        if key in root:
            staged[key] = root[key]
    target = None
    gui = None
    for role in roles:
        role_template = next((obj for obj in templates if obj.get("oxal_mc_studio_representation") == role), template)
        material = assets._private_material(transaction,role_template.data.materials[0])
        parent = staged
        if role:
            parent = transaction.track(bpy.data.objects.new("图片资产表示 · "+role,None))
            collection.objects.link(parent)
            parent.parent = staged
            parent["oxal_mc_studio_representation"] = role
            parent["oxal_mc_studio_slot_visual_profile"] = kind
            parent["oxal_mc_studio_slot_pose_schema"] = 1
            if role == "GUI":
                gui = parent
                display = transaction.track(bpy.data.objects.new("GUI · 原版显示变换", None))
                collection.objects.link(display)
                display.parent = parent
                display["oxal_mc_studio_model_part"] = "库存 · GUI显示变换"
                display["oxal_mc_studio_representation"] = role
                display.matrix_basis = slot_transform(proxy, "gui", visual_profile=kind)
                parent = display
        if kind == "ITEM":
            obj = assets.geometry.sprite(transaction,root.name,material,root.get("oxal_mc_studio_image_depth",.0625))
        else:
            obj = assets.geometry.box(transaction,root.name,(0,0,0),(16,16,16),material)
            if slot or root.get("oxal_mc_studio_display_context"):
                cast(bpy.types.Mesh,obj.data).transform(Matrix.Translation((0,0,-.5)))
        collection.objects.link(obj)
        obj.parent = parent
        if role:
            obj["oxal_mc_studio_representation"] = role
        if role == "GUI":
            from ..inventory.inventory_gui import stage_existing as stage_gui

            target = stage_gui(transaction, scene, staged, root, gui)
        elif role == "HELD":
            if target is None:
                raise ValueError("图片资产缺少精确的原生格子目标，未重建")
            context = "firstperson_lefthand" if target.handedness == "LEFT" else "firstperson_righthand"
            parent["oxal_mc_studio_presentation_context"] = context
            parent.matrix_basis = slot_transform(proxy, context, visual_profile=kind)
            from ..inventory.inventory_projection import stage_existing as stage_held

            stage_held(transaction, scene, staged, root, parent)


def rebuild(scene,root,options,*,repair=False):
    from . import assets
    contracts.validate(root,beta=repair)
    destination,old_objects = assets._rebuild_target(scene,root)
    image_root = (root.type == "MESH" and root.get("oxal_mc_studio_image_root_schema") == 1
                  and root[assets.IDENTIFIER].startswith("image:"))
    old_rig = contracts.control_rig(root)
    old_pose = _pose_snapshot(old_rig)
    parameters = {"state":options.get("state",json.loads(root.get("oxal_mc_studio_block_state","{}"))),
                  "shape":options.get("shape",root.get("oxal_mc_studio_shape",0)),"special":root.get("oxal_mc_studio_special",False),
                  "complete_structure":options.get("complete_structure",root.get("oxal_mc_studio_complete_structure",True)),
                  "spawn_entity":options.get("spawn_entity",root.get("oxal_mc_studio_spawn_entity","pig")),
                  "pack_digest":root.get("oxal_mc_studio_pack_digest","")}
    particle_tool=assets.asset_extensions.particles_module(root)
    if particle_tool is not None:
        parameters["particles_window"]=(options["particles_window"] if "particles_window" in options else particle_tool.request(scene,root,options))
        # Native animated birth/rate sampling reads the validated original
        # root; this transient RNA never enters the queued JSON snapshot.
        parameters["_particles_source_root"]=root
    with object_mode(root),assets.geometry.Creation() as transaction:
        transaction.owner = root
        from ..enchantment import enchantment_outline as outline_features

        relief_before = {obj: outline_features.prepared(obj) for obj in old_objects if obj.type == "MESH"}
        for obj in old_objects:
            if obj.type == "MESH":
                outline_features.remove(transaction, root, obj)
        existing_images = tuple(item for item in owned_ids(root) if isinstance(item, bpy.types.Image))
        require_owned(root, existing_images)
        for image in existing_images:
            if image.is_dirty:
                raise ValueError("资产图片包含未保存的像素编辑，先保存这些编辑再修复路径")
            transaction.image_paths.prepare(image)
        collection,staged = assets._new_root(transaction,root[assets.IDENTIFIER],root.name,special=parameters["special"])
        transaction.excluded.update((collection,staged))
        if root[assets.IDENTIFIER].startswith("image:"):
            _image_stage(transaction,scene,collection,staged,root,old_objects)
        elif root.get("oxal_mc_studio_presentation_context") == "slot":
            gui = assets._representation(transaction,scene,collection,staged,root[assets.IDENTIFIER],"gui",parameters,"GUI")
            from ..inventory.inventory_gui import stage_existing as stage_gui

            stage_gui(transaction, scene, staged, root, gui)
            old_held = root.get("oxal_inventory_held")
            if old_held is not None:
                context = old_held.get("oxal_mc_studio_presentation_context","firstperson_righthand")
                held=assets._representation(transaction,scene,collection,staged,root[assets.IDENTIFIER],context,parameters,"HELD")
                from ..inventory.inventory_projection import stage_existing
                stage_existing(transaction,scene,staged,root,held)
        else:
            context = root.get("oxal_mc_studio_display_context","world")
            assets._build(transaction,scene,collection,staged,root[assets.IDENTIFIER],{**parameters,"context":context})
        scene.collection.children.link(collection)
        if staged.type == "ARMATURE":
            rigs.build_subrigs(transaction,scene,collection,staged,root[assets.IDENTIFIER])
            rigs.build(transaction,scene,collection,staged,root[assets.IDENTIFIER])
            if parameters["special"]:
                assets.asset_authored.attachments(transaction,scene,collection,staged,root[assets.IDENTIFIER],assets._material_factory(transaction,scene,pack_digest=parameters["pack_digest"]))
        if root.get("oxal_mc_studio_native_book_schema"):
            from .asset_book import stage_target
            stage_target(transaction, scene, staged, root)
        assets.asset_extensions.post_compile(staged,scene,parameters)
        from .asset_bounds import (
            FRAME,
            prepare_base_frame,
            publish_plant_bounds,
            validate_legacy_frame,
        )

        prepare_base_frame(transaction, staged)
        following = tuple(staged.children_recursive)
        mapping,retired,fresh = _pairs(old_objects,following)
        from . import asset_slot_states

        asset_slot_states.upgrade_pairs(root, mapping, retired, fresh)
        from .asset_slot_upgrade import updates as slot_pose_updates
        slot_matrices = slot_pose_updates(scene, root, mapping, old_objects)
        # Explicit trajectory caches remain valid native data until rebaked.
        from .asset_arrow_bake import preserved_objects
        for cached in preserved_objects(root):
            retired.discard(cached)
        helper = root.type != staged.type and not image_root
        if image_root and not any(old == root and new.type == "MESH" for new, old in mapping.items()):
            raise ValueError("图片根网格无法对应本次源几何，未覆盖")
        if helper and root.type == "EMPTY" and staged.type == "ARMATURE" and old_rig is not None:
            mapping[staged] = old_rig
            retired.discard(old_rig)
            helper = False
        if helper:
            if root.type != "EMPTY" or staged.type != "ARMATURE":
                raise ValueError("此骨架类型变更无法保留根身份与动画，未覆盖")
            transaction.excluded.discard(staged)
            staged.parent = root
            staged.name = root.name+" · 原生形态控制器"
            for key in (ASSET_ID,INSTANCE):
                del staged[key]
        elif staged not in mapping:
            mapping[staged] = root
        _animation_preflight(root,staged,mapping)
        validate_legacy_frame(root, staged, mapping, _animated_channels)
        from . import asset_rope
        rope_plan = asset_rope.prepare_repair(transaction, root, mapping)
        contracts.legacy_mesh_preflight(root, mapping, retired)
        scene_references = _explicit_scene_objects(scene) if retired else set()
        for obj in retired:
            if obj.type == "MESH" and not obj.get("oxal_mc_studio_effect_geometry"):
                raise ValueError("现有实体部件无法唯一对应；为保留实际对象、材质和动画，未重建")
            if obj.type == "MESH" and any(_animated_channels(owner) for slot in obj.material_slots if (material := slot.material) for owner in (material,material.node_tree) if owner is not None):
                raise ValueError("待替换特效材质含实际动画，未丢弃其绑定")
            # Existing native inventory attachment frames remain the binding
            # boundary; their independent GUI/HELD pivots were matched above.
            if obj.type == "EMPTY" and obj.constraints and any(c.name == "OXAL · 原生物品栏绑定" for c in obj.constraints):
                continue
            if obj.animation_data and (obj.animation_data.action or obj.animation_data.nla_tracks):
                raise ValueError("新形态无法对应已有子对象动画，未覆盖："+obj.name)
            data_animation = obj.data.animation_data if obj.data is not None else None
            if data_animation and (data_animation.action or data_animation.nla_tracks):
                raise ValueError("新形态无法对应已有部件数据动画，未覆盖："+obj.name)
            users = bpy.data.user_map(subset=(obj,)).get(obj,set())
            if _retired_slot_helper(root, destination, obj, users, old_objects, scene, scene_references):
                continue
            allowed = {root,destination,*old_objects}
            if tuple(root.users_scene) == (scene,) and scene.objects.get(obj.name) == obj and obj not in scene_references:
                allowed.add(scene)
            if any(user not in allowed and not owns(root,user) for user in users):
                raise ValueError("待移除部件有外部用户引用，未覆盖")
        from ..enchantment import enchantment
        enchanted = any(enchantment.inspect(slot.material) for obj in old_objects if obj.type == "MESH" and not obj.get("oxal_mc_studio_effect_geometry") for slot in obj.material_slots if slot.material)
        cache = {}
        initial_bodies = [obj for obj in old_objects if obj.type == "MESH" and not obj.get("oxal_mc_studio_effect_geometry") and "oxal_mc_studio_native_state" not in obj]
        for new in following:
            if new.type == "MESH":
                previous = mapping.get(new)
                if image_root and previous == root:
                    # This root is also the body. Repair its proven geometry
                    # while retaining actual user Material IDs and animation.
                    new.data.materials.clear()
                    for slot in root.material_slots:
                        new.data.materials.append(slot.material)
                    continue
                if previous is None and "oxal_mc_studio_native_state" in new and len(initial_bodies) == 1:
                    previous = initial_bodies[0]
                _materials(transaction,root,previous,new,cache,enchanted)
        old_props = _value(dict(root.items()))
        data_snapshots = {obj:obj.data for obj in (root,*old_objects)}
        slot_snapshots = {obj: tuple((slot.link, slot.material) for slot in obj.material_slots)
                          for obj in data_snapshots if obj.type == "MESH"}
        driver_snapshots = {obj:rigs.driver_records(obj) for obj in (root,*old_objects) if not (image_root and obj == root)}
        root_drivers = None
        if image_root:
            root_drivers = drivers.capture(root)
        parent_snapshots = {obj:(obj.parent,obj.parent_type,obj.parent_bone,obj.matrix_parent_inverse.copy(),obj.matrix_basis.copy()) for obj in old_objects}
        object_properties = {obj: _value(dict(obj.items())) for obj in old_objects}
        def rollback():
            for obj,data in data_snapshots.items():
                obj.data = data
                if obj in slot_snapshots:
                    for slot, (link, material) in zip(obj.material_slots, slot_snapshots[obj], strict=True):
                        slot.link = link
                        if link == "OBJECT":
                            slot.material = material
                if image_root and obj == root:
                    drivers.restore(root, root_drivers)
                else:
                    rigs.install_drivers(obj,driver_snapshots[obj])
            if old_rig is not None:
                for name,values in old_pose.items():
                    if name in old_rig.pose.bones:
                        pose = old_rig.pose.bones[name]
                        pose.rotation_mode = values["rotation_mode"]
                        pose.matrix_basis = values["matrix"]
                        pose.lock_location,pose.lock_rotation,pose.lock_scale = values["locks"]
                        pose.custom_shape = values["shape"]
                        pose.custom_shape_scale_xyz = values["shape_scale"]
                        pose.custom_shape_rotation_euler = values["shape_rotation"]
                        pose.custom_shape_translation = values["shape_translation"]
                        for key in tuple(pose.keys()):
                            if key not in values["properties"]:
                                del pose[key]
                        for key,value in values["properties"].items():
                            pose[key] = value
            for obj,(parent,kind,bone,inverse,basis) in parent_snapshots.items():
                obj.parent,obj.parent_type,obj.parent_bone = parent,kind,bone
                obj.matrix_parent_inverse,obj.matrix_basis = inverse,basis
            for obj, properties in object_properties.items():
                for key in tuple(obj.keys()):
                    if key not in properties:
                        del obj[key]
                for key, value in properties.items():
                    obj[key] = value
            for key in tuple(root.keys()):
                if key not in old_props:
                    del root[key]
            for key,value in old_props.items():
                root[key] = value
        transaction.on_rollback(rollback)
        publish_plant_bounds(transaction, root, staged, mapping)
        for key,value in staged.items():
            if key not in root and key not in {ASSET_ID,INSTANCE,"oxal_mc_studio_owned_data","oxal_mc_studio_owned_root_uid"}:
                root[key] = mapping.get(value,value) if isinstance(value,bpy.types.ID) else _value(value)
        if staged.get(asset_slot_states.SCHEMA) == 1 and asset_slot_states.PROPERTY not in old_props:
            phase_ui = cast(Any, staged.id_properties_ui(asset_slot_states.PROPERTY)).as_dict()
            root.id_properties_ui(asset_slot_states.PROPERTY).update(**phase_ui)
        if helper:
            root["oxal_mc_studio_control_rig"] = staged
        elif staged.type == "ARMATURE":
            _rig_publish(mapping[staged],staged,mapping,old_pose)
        for new,old in tuple(mapping.items()):
            if new == staged:
                continue
            if root.get("oxal_mc_studio_projectile_schema", 0) >= 3 and new.type == "MESH":
                from .asset_projectile_clock import preserve_existing
                if preserve_existing(scene, root, old, new):
                    continue
            asset_slot_states.bridge_existing(transaction, old, new)
            if old.get("oxal_mc_studio_native_clock") or new.get("oxal_mc_studio_native_clock"):
                from .asset_playback import publish_staged
                publish_staged(transaction,root,old,new)
            if old.type == "ARMATURE":
                _rig_publish(old,new,mapping,_pose_snapshot(old))
            elif old.type not in {"EMPTY","LIGHT","CURVE"}:
                old.data = new.data
                if old.type == "MESH":
                    # Object-linked overrides survive a Mesh replacement. Keep
                    # their link mode, but publish the prepared actual surface.
                    for slot, material in zip(old.material_slots, tuple(new.data.materials), strict=True):
                        if slot.link == "OBJECT":
                            slot.material = material
                if old != root and root.get("oxal_mc_studio_presentation_context") != "slot":
                    rigs.install_drivers(old,_mapped_records(new,mapping))
            else:
                rigs.install_drivers(old,_mapped_records(new,mapping))
            if old != root and not (new.get("oxal_mc_studio_representation") and old.type == "EMPTY"):
                old.parent = mapping.get(new.parent,new.parent)
                old.parent_type,old.parent_bone = new.parent_type,new.parent_bone
                old.matrix_parent_inverse = new.matrix_parent_inverse
            for key,value in new.items():
                if (key.startswith("oxal_mc_studio_") and "owned_" not in key
                        and key not in {assets.geometry.OWNER, assets.OWNER_INSTANCE}):
                    old[key] = value
        for new in fresh:
            destination.objects.link(new)
            collection.objects.unlink(new)
            if new.parent in mapping:
                new.parent = mapping[new.parent]
        if helper:
            destination.objects.link(staged)
            collection.objects.unlink(staged)
        for obj, basis in slot_matrices.items():
            obj.matrix_basis = basis
        for key,value in staged.items():
            if (key not in old_props or key in {FRAME,"oxal_mc_studio_block_state","oxal_mc_studio_shape","oxal_mc_studio_complete_structure","oxal_mc_studio_state_controls","oxal_mc_studio_spawn_entity","oxal_mc_studio_projectile_schema","资产部件","骨骼控件","内部书本骨架"}) and key not in {ASSET_ID,INSTANCE,"oxal_mc_studio_owned_data","oxal_mc_studio_owned_root_uid"}:
                root[key] = mapping.get(value,value) if isinstance(value,bpy.types.ID) else value
        root[VERSION] = assets.RESOURCE_VERSION
        _remap_created(transaction,mapping)
        asset_rope.publish_repair(rope_plan, mapping)
        for temporary in mapping:
            if temporary != staged:
                temporary.parent = None
                target = mapping[temporary]
                if temporary.type == "MESH" and target.type == "MESH" and temporary.data == target.data:
                    # The fresh staging object is about to be discarded. Keep
                    # it alive for rollback while making the published mesh
                    # exclusive before actual Solidify appends private slots.
                    temporary.data = transaction.track(bpy.data.meshes.new("OXAL · 暂存空网格"))
        removed = [obj for obj in retired if not (obj.type == "EMPTY" and obj.constraints and any(c.name == "OXAL · 原生物品栏绑定" for c in obj.constraints))]
        for obj in removed:
            obj.parent = None
        from ..enchantment import enchantment, enchantment_outline
        for obj in assets.members(root):
            if obj.type == "MESH" and not obj.get("oxal_mc_studio_effect_geometry"):
                if enchantment_outline.RECORD in obj:
                    enchantment_outline.refresh(transaction, root, obj, root)
                elif any(enchantment.inspect(slot.material) for slot in obj.material_slots if slot.material):
                    enchantment_outline.apply(transaction, root, obj, root, prepare_geometry=relief_before.get(obj, False))
        from ..inventory.inventory_helpers import configure as configure_slot_helpers
        configure_slot_helpers(transaction, scene, root)
        root.update_tag()
        if contracts.control_rig(root) is not None:
            contracts.control_rig(root).update_tag()
        contracts.stamp(root, transaction=transaction)
        if root.type == "ARMATURE":
            root["oxal_mc_studio_rig_driver_contract"] = rigs.driver_contract(root)
        # Complete fallible image/cache preparation before releasing old IDs.
        for item in live_ids(transaction.ids):
            if isinstance(item, bpy.types.Image):
                transaction.image_paths.prepare(item)
        transaction.seal()
        if removed:
            require_owned(root,removed)
            bpy.data.batch_remove(ids=live_ids(removed))
        discarded = [new for new in mapping if new != staged]
        if not helper:
            discarded.append(staged)
        for item in discarded:
            transaction.forget(item)
        bpy.data.batch_remove(ids=live_ids(discarded))
        for item in live_ids(transaction.ids):
            if isinstance(item,bpy.types.Object) and not item.users_collection and item.users == 1 and owns(root,item):
                transaction.forget(item)
                bpy.data.objects.remove(item,do_unlink=True)
        transaction.forget(collection)
        bpy.data.collections.remove(collection)
        transaction.commit()
    return {"message":"已修复旧Beta，保留实例身份、动画、材质与绑定" if repair else "已应用变体，保留实例动画、材质与绑定"}
