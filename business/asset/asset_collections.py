"""Scene-local presentation parent for newly created library instances.

asset_id: oxal.library.collection; asset_version: 1;
policy: ACTIVATE_EXISTING; scope: one exact Scene-owned organizational parent.
Source: a newly authored Collection, never a collection found by display name.
The parent owns no instances and never enters an individual asset registry.
"""

from collections.abc import Iterable

import bpy

from ...core.runtime import ensure_mutation_allowed

REFERENCE = "oxal_mc_studio_asset_library_collection"
MARK = "oxal_mc_studio_asset_library_collection_schema"
SCENE = "oxal_mc_studio_asset_library_scene"


def current(scene):
    """Resolve only an exact scene reference, not a copied marker or name."""
    collection = scene.get(REFERENCE)
    if collection is None:
        return None
    if (not isinstance(collection, bpy.types.Collection) or collection.get(MARK) != 1
            or collection.get(SCENE) != scene or collection.library or collection.override_library
            or scene.collection.children.get(collection.name) != collection):
        raise ValueError("资产库组织合集引用或位置已变化，未接管其它合集")
    if any(collection in tuple(other.collection.children_recursive)
           for other in bpy.data.scenes if other != scene):
        raise ValueError("资产库组织合集被其它场景共享，未修改")
    parents = [parent for parent in bpy.data.collections
               if parent.children.get(collection.name) == collection]
    if parents:
        raise ValueError("资产库组织合集额外链接到其它合集，未修改")
    return collection


def publish(transaction, scene, collection):
    """Link this creation's instance below the verified shared parent."""
    ensure_mutation_allowed()
    if collection not in transaction.ids:
        raise ValueError("资产库归类仅适用于本次明确创建的实例合集")
    parent = current(scene)
    if parent is None:
        parent = bpy.data.collections.new("资产库")
        parent[MARK], parent[SCENE] = 1, scene
        existed, previous = REFERENCE in scene, scene.get(REFERENCE)

        def restore_created_parent():
            # Only this actual new ID is eligible for rollback deletion. It
            # never belongs to the asset root and survives normal removals.
            if scene.get(REFERENCE) == parent:
                if existed and previous is not None:
                    scene[REFERENCE] = previous
                else:
                    del scene[REFERENCE]
            bpy.data.collections.remove(parent)

        transaction.on_rollback(restore_created_parent)
        scene.collection.children.link(parent)
        scene[REFERENCE] = parent
    parent.children.link(collection)
    transaction.on_rollback(lambda: parent.children.unlink(collection)
                            if parent.children.get(collection.name) == collection else None)
    return parent


def _references(value, target):
    if isinstance(value, bpy.types.ID):
        return value == target
    if hasattr(value, "items"):
        return any(_references(part, target) for part in value.values())
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return any(_references(part, target) for part in value)
    return False


def is_native_parent(scene, parent, child):
    """A narrow deletion exemption for this exact native membership edge.

    Callers must first prove the child is their own instance Collection. A
    custom property referencing it is not native membership and is refused.
    """
    if (not isinstance(child, bpy.types.Collection) or parent != current(scene)
            or parent.children.get(child.name) != child):
        return False
    return not any(_references(value, child) for value in parent.values())
