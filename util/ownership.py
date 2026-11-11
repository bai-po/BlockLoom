"""Persistent exact ID provenance, including protection against copied tags.

User duplicates inherit ID properties, so an ownership UUID alone is never a
deletion proof. A registry also retains the actual ID reference saved by Blender.
"""

from collections.abc import Iterable, Mapping
from contextlib import suppress
from uuid import uuid4

import bpy
from bpy.types import ID, Collection, Object, Scene

from ..core.runtime import ensure_mutation_allowed

REGISTRY = "oxal_mc_studio_owned_data"
DATA_UID = "oxal_mc_studio_owned_data_uid"
ROOT_UID = "oxal_mc_studio_owned_root_uid"
SCENE_ROOTS = "oxal_mc_studio_owned_roots"


def _contains(scene: Scene, root: ID) -> bool:
    if isinstance(root, Object):
        return scene.objects.get(root.name) == root
    if isinstance(root, Collection):
        return root in tuple(scene.collection.children_recursive)
    return False


def _scope(root: ID) -> Scene | None:
    key = root.get(ROOT_UID)
    if not isinstance(key, str):
        return None
    scenes = []
    for scene in bpy.data.scenes:
        roots = scene.get(SCENE_ROOTS)
        if roots is not None and hasattr(roots, "get") and roots.get(key) == root:
            scenes.append(scene)
    if len(scenes) != 1 or not _contains(scenes[0], root):
        return None
    if any(_contains(scene, root) for scene in bpy.data.scenes if scene != scenes[0]):
        return None
    return scenes[0]


def owned_ids(root: ID) -> tuple[ID, ...]:
    if _scope(root) is None:
        return ()
    registry = root.get(REGISTRY)
    if registry is None:
        return ()
    if not hasattr(registry, "values"):
        raise ValueError("实例来源登记已损坏，未清理数据")
    return tuple(item for item in registry.values() if isinstance(item, ID))


def seal_owned(root: ID, items: Iterable[ID], *, scene: Scene | None = None) -> None:
    """Publish only IDs proven newly created by a scoped import transaction.

    Never call this on a scene difference, a name match or arbitrary children.
    Calling again extends the registry for a successfully prepared replacement.
    """
    ensure_mutation_allowed()
    items = tuple(items)
    registered_scope = _scope(root)
    new_root = root in items and registered_scope is None
    if registered_scope is None and not new_root:
        raise ValueError("实例根归属无法核验，未接管已有数据")
    if scene is None:
        scopes = [s for s in bpy.data.scenes if _contains(s, root)]
        if len(scopes) != 1:
            raise ValueError("实例必须先发布到唯一的目标场景")
        scene = scopes[0]
    if not _contains(scene, root) or (registered_scope is not None and registered_scope != scene):
        raise ValueError("实例来源场景已变化，未接管数据")
    registry = root.get(REGISTRY)
    if registry is not None and not hasattr(registry, "items"):
        raise ValueError("实例来源登记已损坏，未接管数据")
    table = dict(registry.items()) if registry is not None and not new_root else {}
    known = set(owned_ids(root))
    for item in items:
        if item == root or item in known:
            continue
        key = uuid4().hex
        item[DATA_UID] = key
        table[key] = item
        known.add(item)
    if new_root:
        roots = scene.get(SCENE_ROOTS)
        if roots is not None and not hasattr(roots, "items"):
            raise ValueError("场景实例登记已损坏，未接管数据")
        root_key = uuid4().hex
        scene_table = {
            key: value for key, value in roots.items() if value is not None
        } if roots is not None else {}
        scene_table[root_key] = root
        root[ROOT_UID] = root_key
        root[REGISTRY] = table
        scene[SCENE_ROOTS] = scene_table
    else:
        root[REGISTRY] = table


def owns(root: ID, item: ID) -> bool:
    registry = root.get(REGISTRY)
    if _scope(root) is None or registry is None or not hasattr(registry, "get"):
        return False
    if item == root:
        return True
    try:
        key = item.get(DATA_UID)
        return isinstance(key, str) and registry.get(key) == item
    except ReferenceError:
        return False


def require_owned(root: ID, items: Iterable[ID]) -> None:
    """Refuse unknown/copy-inherited members before any destructive change."""
    if REGISTRY not in root or _scope(root) is None:
        raise ValueError("实例缺少可核验的来源登记，未删除或重建数据")
    for item in tuple(items):
        if not owns(root, item):
            raise ValueError("实例中含用户复制或添加的数据，请先移出后再操作")


def explicit_scene_ids(
    scene: Scene, *, root: ID | None = None,
    scene_properties: Mapping[str, ID] | None = None,
) -> set[ID]:
    """Read explicit Scene references without traversing native membership.

    Raw ID properties include stored add-on PropertyGroups without invoking
    their RNA getters. Only the exact registered root and explicitly declared
    presentation pointers are exempt; other Scene references remain external.
    """
    references, seen = set(), set()
    permitted = scene_properties or {}

    def custom(value):
        if isinstance(value, ID):
            references.add(value)
        elif hasattr(value, "items"):
            for part in value.values():
                custom(part)
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            for part in value:
                custom(part)

    def visit(owner):
        if owner is None or not hasattr(owner, "bl_rna") or owner.as_pointer() in seen:
            return
        seen.add(owner.as_pointer())
        storage = []
        if hasattr(owner, "items"):
            with suppress(TypeError):
                storage.append(tuple(owner.items()))
        # Newer Blender stores registered RNA payload separately from custom
        # ID properties. Read the raw native group, never a foreign getter;
        # an explicit PointerProperty must not look like Scene membership.
        get_system = getattr(owner, "bl_system_properties_get", None)
        if get_system is not None:
            system = get_system()
            if system is not None:
                storage.append(tuple(system.items()))
        for properties in storage:
            for key, value in properties:
                if owner == scene:
                    if key in permitted and value == permitted[key]:
                        continue
                    if key == SCENE_ROOTS and hasattr(value, "items"):
                        for token, member in value.items():
                            if root is not None and token == root.get(ROOT_UID) and member == root:
                                continue
                            custom(member)
                        continue
                custom(value)
        for prop in owner.bl_rna.properties:
            # Registered getters may initialize payload or execute Python.
            if prop.is_runtime or prop.identifier in {"rna_type", "id_data"}:
                continue
            # ParticleEdit.object exposes a native read-only editing cache,
            # which can retain an old pose object after Undo. It is not a
            # saved explicit Scene reference. Raw custom/system payloads and
            # every other native pointer still participate in this check.
            if (isinstance(owner, bpy.types.ParticleEdit) and prop.identifier == "object"
                    and prop.is_readonly):
                continue
            if owner == scene and prop.identifier in {"objects", "collection"}:
                continue
            if isinstance(owner, bpy.types.ViewLayer) and prop.identifier in {"objects", "layer_collection", "active_layer_collection", "depsgraph"}:
                continue
            if prop.type not in {"POINTER", "COLLECTION"}:
                continue
            value = getattr(owner, prop.identifier)
            for item in tuple(value) if prop.type == "COLLECTION" else (value,):
                if isinstance(item, ID):
                    references.add(item)
                    if item.is_embedded_data:
                        visit(item)
                else:
                    visit(item)

    visit(scene)
    return references


def require_no_external_users(
    scene: Scene, root: ID, items: Iterable[ID], *,
    scene_properties: Mapping[str, ID] | None = None,
) -> None:
    """Preflight exact owned IDs, including Collections and explicit Scene use."""
    proven = set(owned_ids(root))
    require_owned(root, (root, *proven))
    attached = {value for owner in (root, *proven) for field in ("node_tree", "shape_keys")
                if isinstance(value := getattr(owner, field, None), ID)}
    domain = {root, *proven, *attached}
    checked = tuple(items)
    if any(item not in domain for item in checked):
        raise ValueError("待修改数据不属于该实例，未删除或重建")
    references = explicit_scene_ids(scene, root=root, scene_properties=scene_properties)
    for item, users in bpy.data.user_map(subset=checked).items():
        if any(user not in domain and not (user == scene and item not in references) for user in users):
            raise ValueError("其它数据或场景显式引用仍使用该实例，未删除或重建")


def prune_owned(root: ID) -> None:
    """Reclaim only registered unused data; the registry itself is one user.

    Objects/collections may be intentionally unlinked helpers and are kept until
    explicit removal. Additional native references/fake users always keep data.
    """
    ensure_mutation_allowed()
    require_owned(root, (root,))
    for _ in range(12):
        registry = root.get(REGISTRY)
        if registry is None:
            return
        stale, unused = [], []
        for key, item in tuple(registry.items()):
            if item is None:
                stale.append(key)
                continue
            try:
                if (
                    not isinstance(item, (Object, Collection))
                    and item.users == 1
                    and not item.use_fake_user
                ):
                    stale.append(key)
                    unused.append(item)
            except ReferenceError:
                stale.append(key)
        if not stale:
            break
        for key in stale:
            del registry[key]
        remove_unused_owned(unused)


def release_owned(root: ID) -> tuple[ID, ...]:
    """Capture proven IDs and release registry references before root removal."""
    ensure_mutation_allowed()
    scope = _scope(root)
    if scope is None:
        raise ValueError("实例根归属无法核验，未删除数据")
    items = owned_ids(root)
    registry = scope[SCENE_ROOTS]
    del registry[root[ROOT_UID]]
    if not registry:
        del scope[SCENE_ROOTS]
    if REGISTRY in root:
        del root[REGISTRY]
    return items


def remove_unused_owned(items: Iterable[ID]) -> None:
    """For captured proven IDs only; never scan the scene or delete by tags."""
    ensure_mutation_allowed()
    remaining = tuple(items)
    for _ in range(12):
        unused, live = [], []
        for item in remaining:
            try:
                if item.users == 0 and not item.use_fake_user:
                    unused.append(item)
                else:
                    live.append(item)
            except ReferenceError:
                pass  # Native removal may also free embedded shape-key IDs.
        if not unused:
            break
        bpy.data.batch_remove(ids=unused)
        remaining = tuple(live)
