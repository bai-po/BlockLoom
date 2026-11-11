"""Scene presentation container for newly created native glint controls.

asset_id: oxal.enchantment.controls; asset_version: 1; policy: ACTIVATE_EXISTING;
scope: one exact Scene reference. The organizational Collection is not owned by
an actor: removing one actor must never remove another actor's parameters.
"""

import bpy

from ...core.runtime import ensure_mutation_allowed

REFERENCE = "oxal_mc_studio_enchantment_controls_collection"
MARKER = "oxal_mc_studio_enchantment_controls_collection_schema"


def _container(transaction, scene):
    if REFERENCE in scene:
        collection = scene[REFERENCE]
        if (not isinstance(collection, bpy.types.Collection) or collection.get(MARKER) != 1
                or collection.library or collection.override_library
                or scene.collection.children.get(collection.name) != collection
                or any(collection in tuple(other.collection.children_recursive)
                       for other in bpy.data.scenes if other != scene)):
            raise ValueError("附魔参数合集的实际场景引用已变化，未按名称接管")
        return collection
    collection = bpy.data.collections.new("附魔参数")
    collection[MARKER] = 1

    def restore():
        if scene.get(REFERENCE) == collection:
            del scene[REFERENCE]
        # Only this call created the container; unknown new members are kept.
        if not collection.objects and not collection.children and not collection.use_fake_user:
            bpy.data.collections.remove(collection)

    transaction.on_rollback(restore)
    scene.collection.children.link(collection)
    scene[REFERENCE] = collection
    return collection


def link(transaction, scene, obj):
    """Link only a newly tracked controller; never move arbitrary scene data."""
    ensure_mutation_allowed()
    if obj not in transaction.ids or obj.users_collection:
        raise ValueError("附魔参数归类只接收本次新建、尚未链接的控制对象")
    collection = _container(transaction, scene)

    def restore():
        if collection.objects.get(obj.name) == obj:
            collection.objects.unlink(obj)

    transaction.on_rollback(restore)
    collection.objects.link(obj)
