"""Explicit deletion bounded to the persistent exact ID provenance registry."""

from dataclasses import dataclass

import bpy

from ...util.animation import action_fcurves
from ...util.ownership import (
    explicit_scene_ids,
    owned_ids,
    release_owned,
    remove_unused_owned,
    require_owned,
)
from ...util.resources import INSTANCE, live_ids


@dataclass
class Removal:
    root: object
    objects: set
    collections: set
    binding: object = None


def _require_no_external_users(scene, root, proven, attached):
    """Preserve external-use checks while allowing the exact library parent."""
    from .asset_collections import is_native_parent

    domain = {root, *proven, *attached}
    references = explicit_scene_ids(scene, root=root)
    for item, users in bpy.data.user_map(subset=tuple(domain)).items():
        for user in users:
            if user in domain or user == scene and item not in references:
                continue
            if is_native_parent(scene, user, item):
                continue
            raise ValueError("其它数据或场景显式引用仍使用该资产，未删除")


def plan(scene,root):
    from . import assets
    assets._require_members(root)
    proven = set(owned_ids(root))
    objects = {root,*(item for item in proven if isinstance(item,bpy.types.Object))}
    collections = {item for item in proven if isinstance(item,bpy.types.Collection)}
    require_owned(root,(*objects,*collections))
    if any(any(collection not in collections for collection in obj.users_collection) for obj in objects):
        raise ValueError("资产对象被用户合集共享，未移除")
    for collection in collections:
        if any(obj not in objects for obj in collection.objects) or any(child not in collections for child in collection.children):
            raise ValueError("资产合集中含未知用户数据，未移除")
    attached = {item for owner in proven for name in ("node_tree","shape_keys") if (item := getattr(owner,name,None)) is not None}
    _require_no_external_users(scene, root, proven, attached)
    binding = None
    host = root.get("oxal_inventory_host")
    if host is not None:
        from ..inventory.inventory_slots import quantity_path, resolve_inventory
        actual_host,rig = resolve_inventory(scene,str(host[INSTANCE]))
        slot = str(root.get("oxal_inventory_slot",""))
        if actual_host != host or rig != root.get("oxal_inventory_binding_rig") or slot not in {*(str(i) for i in range(1,10)),"OFFHAND"}:
            raise ValueError("物品栏绑定归属已改变，未移除")
        binding = (host,rig,slot,quantity_path(slot)[2:-2],"oxal_inventory_bound_"+slot)
    return Removal(root,objects,collections,binding)


def remove(scene,prepared):
    from . import assets
    root = prepared.root
    # A replacement may have just published another independent source. Clear
    # the native slot only when this removed source was the last active item.
    if prepared.binding is not None:
        host,rig,slot,quantity,bound = prepared.binding
        others = [obj for obj in assets.roots(scene) if obj != root and obj.get("oxal_inventory_host") == host and str(obj.get("oxal_inventory_slot")) == slot and obj.get("oxal_inventory_binding_active",True)]
        if not others:
            animation = rig.animation_data
            bindings = [animation,*[strip for track in animation.nla_tracks for strip in track.strips]] if animation else []
            quantity_animated = any(curve.data_path == '["'+quantity+'"]' for binding in bindings for curve in action_fcurves(binding.action,slot=getattr(binding,"action_slot",None)))
            if not quantity_animated:
                rig[quantity] = 0
            rig[bound] = False
            rig.update_tag()
    if bpy.context.mode == "POSE" and bpy.context.object in prepared.objects:
        bpy.ops.object.mode_set(mode="OBJECT")
    captured = release_owned(root)
    bpy.data.batch_remove(ids=live_ids(prepared.objects|prepared.collections))
    remove_unused_owned(captured)


def selected(scene,identifiers):
    from . import assets
    targets = assets.selected_roots(scene,identifiers)
    prepared = [plan(scene,root) for root in targets]
    for item in prepared:
        remove(scene,item)
    return {"message":f"已移除{len(prepared)}个资产实例","removed":list(identifiers)}
