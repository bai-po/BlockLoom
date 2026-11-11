"""Explicit armor lifecycle with source geometry and persistent material references."""

import hashlib
import json
from typing import Any, cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.geometry_nodes import get_input, set_input
from ...util.ownership import REGISTRY, owned_ids, require_owned, seal_owned
from ...util.resources import INSTANCE, AppendTransaction, live_ids
from .human_parts import (
    _capture_materials,
    _check_retirement,
    _forget,
    _restore_materials,
)
from .human_source import DATA, armor_declaration, armor_path
from .human_state import OWNER, PARTS, SOURCE_NAME, key, path

MEMORY = "oxal_human_armor_materials"
SCHEMA = "oxal_human_armor_schema"
ATTRIBUTE_SCHEMA = "oxal_human_armor_attribute_schema"
SHAPE_SCHEMA = "oxal_human_armor_shape_key_contract"
GLINT_CONTROLS = "oxal_human_armor_glint_controls"
PACK_BACKUP = "oxal_human_original_armor_"


def source_name(item):
    from .human import source_name as name
    return name(item)


def part_of(obj):
    from .human import _armor_part
    return _armor_part(obj)


def source_identity(obj):
    """Resolve a source key; this never grants ownership of an object."""
    # Object source keys are identical in both native model versions. Geometry,
    # templates and repair contracts below always use the instance's profile.
    metadata = json.loads((DATA / "legacy/ARMOR.json").read_text("utf-8"))
    names = {item["name"] for entries in metadata.values() for item in entries}
    name = source_name(obj)
    while name not in names:
        prefix, separator, suffix = name.rpartition(".")
        if not separator or not suffix.isdigit():
            raise ValueError("盔甲来源名称不属于已知资源：" + name)
        name = prefix
    return name


def dependency_identity(item):
    """Canonical native lattice role, after callers prove its actual ID.

    The two leg lattices are named by the immutable source contract. Blender
    adds numeric suffixes when another instance already uses those names.
    Other object/data names retain their exact existing contract.
    """
    name = source_name(item)
    if isinstance(item, bpy.types.Object) and item.type == "LATTICE":
        while name not in {"L腿晶格", "R腿晶格"}:
            prefix, separator, suffix = name.rpartition(".")
            if not separator or not suffix.isdigit():
                return source_name(item)
            name = prefix
    return name


def material_memory(records, obj):
    """Read one native part's saved slots, including old numeric-suffix keys."""
    name = source_identity(obj)
    matches = [value for role, value in records.items() if role == name or (
        role.startswith(name + ".") and all(part.isdigit() for part in role[len(name) + 1:].split(".")))]
    if len(matches) > 1:
        raise ValueError("盔甲材质记忆包含重复来源，未选择或覆盖")
    return matches[0] if matches else None


def objects(root, part):
    return tuple(o for o in root.all_objects if part_of(o) == part)


def current_variant(obj, official):
    name = source_name(obj)
    return part_of(obj) not in {"legs", "feet"} or not any(side in name for side in ("左", "右")) or ("-官方腿" in name) == bool(official)


def generated_native_modifiers(root, obj):
    """Project only exact, fully validated feature modifiers during retirement."""
    from ..enchantment.enchantment_outline import generated_modifiers
    from .human_armor_outline import generated_modifiers as armor_outline_modifiers

    return (*generated_modifiers(root, obj), *armor_outline_modifiers(root, obj))


def geometry_signature(obj, *, root=None):
    """Keep the released hash while excluding only proven generated modifiers."""
    generated = generated_native_modifiers(root, obj) if root is not None else ()
    content = ([tuple(v.co) for v in obj.data.vertices], [tuple(p.vertices) for p in obj.data.polygons], [(m.name, m.type) for m in obj.modifiers if m not in generated], [(layer.name, [tuple(loop.uv) for loop in layer.data]) for layer in obj.data.uv_layers], [group.name for group in obj.vertex_groups], [[(group.group, group.weight) for group in vertex.groups] for vertex in obj.data.vertices], [(c.name, c.type) for c in obj.constraints])
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _attribute_schema(obj, *, root=None):
    from ..enchantment.enchantment_geometry import generated_attributes

    generated = generated_attributes(root, obj) if root is not None else ()
    return sorted((attribute.name, attribute.data_type, attribute.domain)
                  for attribute in obj.data.attributes if attribute.name not in generated)


def _equivalent_attribute_schema(obj, actual, expected):
    """Compare render data across the native 4.5 -> 5.x UV-selection migration.

    4.5 stores two Boolean corner selections per UV map; 5.x replaces them
    with mesh-wide corner/face UI selections. Only these exact reserved fields
    are excluded, after the caller has verified geometry, UVs and weights.
    """
    selections = {
        (".uv_select_vert", "BOOLEAN", "CORNER"),
        (".uv_select_edge", "BOOLEAN", "CORNER"),
        (".uv_select_face", "BOOLEAN", "FACE"),
    }
    selections.update((prefix + uv.name, "BOOLEAN", "CORNER")
                      for uv in obj.data.uv_layers for prefix in (".vs.", ".es."))
    def rendering(schema):
        return sorted(tuple(row) for row in schema if tuple(row) not in selections)
    return rendering(actual) == rendering(expected)


def initialize_geometry(obj):
    """Stamp the untouched contract of one proven newly appended source part."""
    obj["oxal_human_armor_geometry"] = geometry_signature(obj)
    obj[ATTRIBUTE_SCHEMA] = json.dumps(_attribute_schema(obj), ensure_ascii=False)
    obj[SHAPE_SCHEMA] = _shape_key_contract(obj.data.shape_keys) or ""


def _shape_key_contract(keys, *, root=None):
    """Compare authored native keys, including their remapped driver targets."""
    from ...util.drivers import capture

    if keys is None:
        return None
    if root is not None:
        require_owned(root, (keys,))
    animation = keys.animation_data
    if animation and (animation.action or animation.nla_tracks):
        raise ValueError("盔甲形态键含独立 Action/NLA，未替换")
    snapshot = capture(keys)
    animated_paths = {curve["path"] for curve in snapshot["curves"]}
    def normalize(value):
        if isinstance(value, bpy.types.ID):
            if root is not None:
                require_owned(root, (value,))
            return (value.id_type, source_name(value))
        if isinstance(value, dict):
            return {name: normalize(part) for name, part in value.items()
                    if name not in {"select", "select_control_point", "select_left_handle", "select_right_handle", "hide"}}
        if isinstance(value, (list, tuple, set)):
            return [normalize(part) for part in value]
        if value is None or isinstance(value, (str, float, int, bool)):
            return value
        raise ValueError("盔甲形态键含未知驱动参数，未替换")
    content = (keys.use_relative,
               keys.eval_time if "eval_time" not in animated_paths else None,
               [(block.name, block.relative_key.name, block.interpolation, block.vertex_group,
                 block.mute, block.slider_min, block.slider_max,
                 block.value if block.path_from_id("value") not in animated_paths else None,
                 [tuple(point.co) for point in block.data]) for block in keys.key_blocks],
               normalize(snapshot))
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _validate_geometry(root, obj, signatures, reference=None, *, native_contract=None):
    require_owned(root, (obj, obj.data))
    signature = obj.get("oxal_human_armor_geometry", signatures.get(source_identity(obj)))
    if obj.data.animation_data or not signature or signature != geometry_signature(obj, root=root):
        raise ValueError("盔甲网格已有用户修改，未移除：" + obj.name)
    expected_keys = obj.get(SHAPE_SCHEMA)
    if expected_keys is None:
        if reference is None:
            raise ValueError("盔甲缺少原始形态键契约，未移除：" + obj.name)
        expected_keys = _shape_key_contract(reference.data.shape_keys) or ""
    if (_shape_key_contract(obj.data.shape_keys, root=root) or "") != expected_keys:
        raise ValueError("盔甲形态键含用户修改，未移除：" + obj.name)
    saved = obj.get(ATTRIBUTE_SCHEMA)
    if saved is None:
        if reference is None:
            raise ValueError("盔甲缺少原始属性契约，未移除：" + obj.name)
        expected = _attribute_schema(reference)
    else:
        expected = json.loads(saved)
    actual = _attribute_schema(obj, root=root)
    if not _equivalent_attribute_schema(obj, actual, expected):
        raise ValueError("盔甲网格含用户属性，未移除：" + obj.name)
    from .human_armor_contract import NativeArmorContract

    (native_contract or NativeArmorContract(root, (reference,) if reference is not None else ())).validate(obj, reference)


def validate_parts(root, members, references):
    """Verify old and decorated parts against their unchanged source contract."""
    metadata = json.loads(armor_declaration(root).read_text("utf-8"))
    signatures = {item["name"]: item["geometry_signature"] for entries in metadata.values() for item in entries}
    canonical = {}
    from .human_armor_contract import NativeArmorContract

    native_contract = NativeArmorContract(root, references.values())
    for reference in references.values():
        name = source_identity(reference)
        if name in canonical:
            raise ValueError("盔甲来源包含重复部位：" + name)
        canonical[name] = reference
    for obj in tuple(members):
        _validate_geometry(root, obj, signatures, canonical.get(source_identity(obj)), native_contract=native_contract)


def capture_materials(root, obj):
    """Retain real shader IDs and feature readiness before retiring a part."""
    from ..enchantment.enchantment_outline import generated_modifiers, outline_prepared
    from ..outline.outline_native import base_slots
    from .human_armor_outline import generated_modifiers as armor_outline_modifiers

    generated_modifiers(root, obj)
    armor_outline_modifiers(root, obj)
    slots = base_slots(root, obj)
    if tuple(index for index, _slot in slots) != tuple(range(len(slots))):
        raise ValueError("盔甲基材槽不是原始连续范围，未保留不明槽位")
    record = _capture_materials(obj, slots=slots)
    if any(index >= len(slots) for index in record["faces"]):
        raise ValueError("盔甲原始面使用了生成描边材质，未覆盖")
    record["backups"] = {name: value for name, value in obj.items()
                         if name.startswith(PACK_BACKUP) and isinstance(value, bpy.types.Material)}
    # The native per-part 3D controls remain on the human owner. The obsolete
    # additional pixel extrusion must not be resurrected by a later leg swap.
    record["enchant_geometry_prepared"] = False
    record["enchant_outline_prepared"] = outline_prepared(obj)
    return record


def glint_owner(root, obj, *, transaction=None):
    """Resolve a single exact part controller from validated shader layers."""
    from ..enchantment import enchantment
    from ..outline.outline_native import base_slots

    inspect_material = (lambda material: enchantment.inspect_source(transaction, material)) if transaction is not None else enchantment.inspect
    owners = {info["owner"] for _index, slot in base_slots(root, obj, transaction=transaction)
              if slot.material and (info := inspect_material(slot.material)) is not None}
    if not owners:
        return None
    if len(owners) != 1:
        raise ValueError("盔甲材质包含不同附魔控制器，未覆盖：" + obj.name)
    owner = next(iter(owners))
    require_owned(root, (owner,))
    if root.get(GLINT_CONTROLS, {}).get(part_of(obj)) != owner:
        raise ValueError("盔甲附魔控制器不属于此部位，未覆盖：" + obj.name)
    return owner


def glint_images(root, material, owner, *, transaction=None):
    """Capture actual owned glint images before preparing any replacement."""
    from ..enchantment import enchantment

    info = (enchantment.inspect_source(transaction, material) if transaction is not None
            else enchantment.inspect(material))
    if info is None:
        return None
    if info["owner"] != owner:
        raise ValueError("盔甲附魔控制归属已改变，未覆盖")
    texture = material.node_tree.nodes[enchantment.PREFIX + "Texture"].image
    baseline = material.get(enchantment.ORIGINAL_IMAGE)
    if not isinstance(texture, bpy.types.Image) or not isinstance(baseline, bpy.types.Image):
        raise TypeError("盔甲附魔纹理来源缺失，未覆盖")
    require_owned(root, (material, owner, texture, baseline))
    return texture, baseline


def apply_glint(transaction, root, owner, material, *, images_from=None, prepare_pixels=False):
    """Upgrade a private armor layer while retaining the actual chosen images.

    A texture restore may wrap a different backup base. Its current slot, not
    that backup, supplies the user's selected glint and restore baseline.
    Existing material IDs are never edited just to publish those references.
    """
    from ..enchantment import enchantment, enchantment_pixels

    if owner not in getattr(transaction, "ids", ()):
        require_owned(root, (owner,))
    images = glint_images(root, material if images_from is None else images_from, owner, transaction=transaction)
    pixel_source = material if images_from is None else images_from
    keep_pixels = prepare_pixels or enchantment_pixels.requested(pixel_source, owner)
    replacement = enchantment.apply(transaction, owner, material,
                                    geometry_kind="ARMOR", scene=bpy.context.scene,
                                    prepare_pixels=keep_pixels)
    if images is not None:
        current = replacement.node_tree.nodes[enchantment.PREFIX + "Texture"].image
        baseline = replacement.get(enchantment.ORIGINAL_IMAGE)
        if (current, baseline) != images:
            if replacement == material:
                replacement = enchantment._copy_material(transaction, replacement)
            replacement.node_tree.nodes[enchantment.PREFIX + "Texture"].image = images[0]
            replacement[enchantment.ORIGINAL_IMAGE] = images[1]
    if replacement != material:
        if enchantment_pixels.prepared(replacement):
            enchantment_pixels.refresh(replacement)
        enchantment.stamp(replacement)
    return replacement


def prepare_controls(transaction, root, owners):
    """Add new native channels to old part controllers inside the edit journal."""
    from ..enchantment import enchantment

    prepared = getattr(transaction, "armor_glint_defaults", set())
    transaction.armor_glint_defaults = prepared
    for owner in set(owners) - prepared:
        if owner not in transaction.ids:
            require_owned(root, (owner,))
        before = _plain(dict(owner.items()))
        def restore(owner=owner, before=before):
            for name in tuple(owner.keys()):
                if name not in before:
                    del owner[name]
            for name, value in before.items():
                owner[name] = value
        transaction.on_rollback(restore)
        enchantment.control_defaults(owner)
        from ..enchantment.enchantment_limits import refresh
        refresh(transaction, owner)
        prepared.add(owner)


def suspend_features(transaction, root, members):
    """Retire exact generated hulls before touching their base material slots."""
    from ..enchantment import enchantment_outline
    from . import human_armor_outline

    members = tuple(members)
    human_armor_outline.suspend(transaction, root, members)
    for obj in members:
        enchantment_outline.remove(transaction, root, obj)


def restore_features(transaction, root, obj, record=None):
    """Recreate native feature graphs on a newly appended armor part."""
    from ..enchantment import enchantment_outline
    from . import human_armor_outline

    suspend_features(transaction, root, (obj,))
    owner = glint_owner(root, obj, transaction=transaction)
    if owner is not None:
        prepare_controls(transaction, root, (owner,))
        enchantment_outline.apply(transaction, root, obj, owner,
                                  prepare_outline=bool(record and record.get("enchant_outline_prepared")))
    human_armor_outline.refresh(transaction, root, (obj,), glint_current=True)


def refresh_features(transaction, root, members, *, upgrade_materials=False):
    """Refresh known feature graphs after an explicit material publication."""
    from ..enchantment import enchantment, enchantment_outline
    from . import human_armor_outline

    members = tuple(members)
    suspend_features(transaction, root, members)
    changed, replacements = [], {}
    if upgrade_materials:
        def restore():
            for obj, index, material in reversed(changed):
                obj.material_slots[index].material = material
        transaction.on_rollback(restore)

    for obj in tuple(members):
        enchantment_outline.generated_modifiers(root, obj)
        owner = glint_owner(root, obj, transaction=transaction)
        if owner is not None:
            prepare_controls(transaction, root, (owner,))
            if upgrade_materials:
                for index, slot in enumerate(obj.material_slots):
                    material = slot.material
                    if material is None:
                        continue
                    require_owned(root, (material,))
                    pair = (owner, material)
                    if pair not in replacements:
                        info = enchantment.inspect(material)
                        if info is None or info["owner"] != owner:
                            raise ValueError("盔甲附魔材质归属已改变，未修复：" + obj.name)
                        replacement = apply_glint(transaction, root, owner, material)
                        replacements[pair] = replacement
                    replacement = replacements[pair]
                    if replacement != material:
                        changed.append((obj, index, material))
                        slot.material = replacement
            enchantment_outline.apply(transaction, root, obj, owner)
        else:
            enchantment_outline.remove(transaction, root, obj)
    human_armor_outline.refresh(transaction, root, members, rebuild=True, glint_current=True)


def animated(owner, data_path):
    animation = owner.animation_data
    if animation is None:
        return False
    if any(f.data_path == data_path for f in animation.drivers):
        return True
    bindings = []
    pending = [s for track in animation.nla_tracks for s in track.strips]
    while pending:
        binding = pending.pop()
        if getattr(binding, "action", None):
            bindings.append(binding)
        pending.extend(getattr(binding, "strips", ()))
    if animation.action:
        bindings.append(animation)
    return any(f.data_path == data_path for binding in bindings for f in action_fcurves(binding.action, slot=getattr(binding, "action_slot", None)))


def rebind_incoming(items, mapping):
    """Rebind only appended RNA, never a global user_remap."""
    visited = set()
    def properties(group):
        if group is None:
            return False
        changed = False
        for name, value in tuple(group.items()):
            if isinstance(value, bpy.types.ID):
                if value in mapping:
                    group[name] = mapping[value]
                    changed = True
            elif hasattr(value, "items"):
                changed = properties(value) or changed
        return changed

    def visit(owner: Any):
        pointer = owner.as_pointer()
        if pointer in visited:
            return
        visited.add(pointer)
        parent_state = None
        for prop in owner.bl_rna.properties:
            if prop.type == "POINTER" and not prop.is_readonly:
                value = getattr(owner, prop.identifier)
                if isinstance(value, bpy.types.ID) and value in mapping:
                    if isinstance(owner, bpy.types.Object) and prop.identifier == "parent":
                        # Blender resets the parent inverse when replacing an
                        # Object's parent pointer. Retain the authored binding
                        # in the incoming data, rather than applying rig scale
                        # and rest translation for a second time.
                        parent_state = (owner.matrix_basis.copy(), owner.matrix_parent_inverse.copy(),
                                        owner.parent_type, owner.parent_bone, tuple(owner.parent_vertices))
                    setattr(owner, prop.identifier, mapping[value])
        if parent_state is not None:
            basis, inverse, parent_type, parent_bone, parent_vertices = parent_state
            owner.parent_type, owner.parent_bone, owner.parent_vertices = parent_type, parent_bone, parent_vertices
            owner.matrix_basis, owner.matrix_parent_inverse = basis, inverse
        if isinstance(owner, (bpy.types.ID, bpy.types.PoseBone)):
            for name in tuple(owner.keys()):
                value = owner.get(name)
                if isinstance(value, bpy.types.ID) and value in mapping:
                    owner[name] = mapping[value]
            system = getattr(owner, "bl_system_properties_get", None)
            if system is not None and properties(system()):
                # Registered source PointerProperties use a separate native
                # ID storage on newer hosts. Rebinding only owner[name]
                # otherwise leaves a second, invisible body reference alive.
                data = owner if isinstance(owner, bpy.types.ID) else owner.id_data
                if data is None:
                    raise ValueError("追加系统属性缺少原生拥有者，未发布")
                data.update_tag()
        if isinstance(owner, bpy.types.NodesModifier) and owner.node_group:
            for socket in cast(Any, owner.node_group.interface).items_tree:
                if getattr(socket, "in_out", "") == "INPUT" and socket.bl_rna.properties.get("default_value"):
                    value = get_input(owner, socket.identifier)
                    if isinstance(value, bpy.types.ID) and value in mapping:
                        set_input(owner, socket.identifier, mapping[value])
        for field in ("nodes", "inputs", "constraints", "modifiers"):
            for child in tuple(getattr(owner, field, ())):
                visit(child)
        pose = getattr(owner, "pose", None)
        if pose:
            for bone in tuple(pose.bones):
                visit(bone)
        tree = getattr(owner, "node_tree", None)
        if tree:
            visit(tree)
        animation = getattr(owner, "animation_data", None)
        if animation:
            for curve in animation.drivers:
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if target.id in mapping:
                            target.id = mapping[target.id]
    for item in tuple(items):
        visit(item)


def _plain(value: Any) -> Any:
    if isinstance(value, bpy.types.ID):
        return value
    if hasattr(value, "items"):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "to_list"):
        return _plain(value.to_list())
    return value


def set_parts(root, rig, parts, enabled, *, repair=False):
    ensure_mutation_allowed()
    from ..asset.asset_geometry import Creation
    from .human import _prepare_controls, _private_materials, parameter_owner
    if bpy.context.mode != "OBJECT":
        raise ValueError("请在物体模式添加或移除盔甲")
    if not isinstance(enabled, bool) or (not parts and not repair) or set(parts) - set(PARTS) or (repair and enabled):
        raise ValueError("盔甲部位操作无效")
    controller = parameter_owner(root)
    require_owned(root, (rig, rig.data, controller))
    old_values = {part: bool(controller[key(part + "_enabled")]) for part in parts}
    outgoing = {o for part in parts for o in objects(root, part)} if not enabled else set()
    missing = [part for part in parts if not objects(root, part)] if enabled else []
    from ..enchantment.enchantment_outline import RECORD
    from ..outline.outline_native import prepared as outline_prepared
    from . import human_armor_outline, human_outline

    feature_members = (tuple(obj for obj in root.all_objects
                             if part_of(obj) and obj not in outgoing and (
                                 obj.get(RECORD) or outline_prepared(obj, role=human_armor_outline.ROLE)
                                 or human_armor_outline.prepared(root) or human_outline.prepared(root)))
                       if repair else ())
    _check_retirement(root, outgoing)
    signatures = {}
    if outgoing or feature_members:
        metadata = json.loads(armor_declaration(root).read_text("utf-8"))
        signatures = {item["name"]: item["geometry_signature"] for entries in metadata.values() for item in entries}
    previous_registry = dict(root[REGISTRY].items())
    old_memory = _plain(root.get(MEMORY, {}))
    memory_existed = MEMORY in root
    memory = _plain(root.get(MEMORY, {}))
    links = {obj: tuple(obj.users_collection) for obj in outgoing}
    schema_existed, old_schema = SCHEMA in root, root.get(SCHEMA)
    def restore():
        for part, value in old_values.items():
            controller[key(part + "_enabled")] = value
        if memory_existed:
            root[MEMORY] = old_memory
        elif MEMORY in root:
            del root[MEMORY]
        if schema_existed:
            root[SCHEMA] = old_schema
        elif SCHEMA in root:
            del root[SCHEMA]
        root[REGISTRY] = previous_registry
        for obj, collections in links.items():
            for coll in collections:
                if coll.objects.get(obj.name) != obj:
                    coll.objects.link(obj)

    source = armor_path(root)
    with AppendTransaction(source) as tx:
        incoming, states = set(), {}
        needs_reference = bool(outgoing or feature_members)
        staged = tx.append_collection("OXAL_ARMOR_ALL") if missing or needs_reference else None
        references = {source_identity(obj): obj for obj in staged.all_objects if part_of(obj)} if staged else {}
        from .human_armor_contract import NativeArmorContract

        native_contract = NativeArmorContract(root, references.values())
        for obj in (*outgoing, *feature_members):
            _validate_geometry(root, obj, signatures, references.get(source_identity(obj)), native_contract=native_contract)
        if missing and staged is not None:
            for part in missing:
                branch = next(c for c in staged.children if source_name(c) == "OXAL_ARMOR_" + part.upper())
                _private_materials(branch, tx)
                incoming.update(o for o in branch.all_objects if current_variant(o, controller[key("leg_mode")]))
            current = {}
            for item in owned_ids(root):
                current.setdefault((type(item), dependency_identity(item)), []).append(item)
            mapping = {}
            for item in tuple(tx.owned):
                if isinstance(item, bpy.types.Object) and item not in incoming:
                    matches = current.get((type(item), dependency_identity(item)), [])
                    if item.type == "ARMATURE":
                        mapping[item] = rig
                    elif len(matches) == 1:
                        mapping[item] = matches[0]
            rebind_incoming(tx.owned, mapping)
            for obj in incoming:
                part = part_of(obj)
                state = material_memory(memory.get(part, {}), obj)
                if state:
                    _restore_materials(obj, state)
                states[obj] = state
                initialize_geometry(obj)
            kept = set(incoming)
            users = bpy.data.user_map(subset=tuple(tx.owned))
            while True:
                dependencies = {item for item, references in users.items() if any(user in kept for user in references)}
                if dependencies <= kept:
                    break
                kept.update(dependencies)
            tx.remove(tx.owned - kept)
        elif staged is not None:
            tx.remove(tuple(tx.owned))
        with Creation() as features:
            features.owner = root
            features.on_rollback(restore)
            if repair:
                from .human_state import refresh_limits

                refresh_limits(features, controller)
            for obj in incoming:
                for coll in tuple(obj.users_collection):
                    coll.objects.unlink(obj)
                root.objects.link(obj)
                obj[OWNER], obj[SOURCE_NAME] = root[INSTANCE], source_name(obj)
                obj.hide_viewport = False
                obj.hide_render = False
                cast(Any, obj).hide_set(False)
            for part in parts:
                controller[key(part + "_enabled")] = enabled
            if incoming:
                _prepare_controls(root, rig, objects=incoming, material_ids={item for item in tx.owned if isinstance(item, bpy.types.Material)})
            # The appended IDs are proven before the feature service receives
            # them. Its own new IDs remain in the nested rollback journal.
            seal_owned(root, tx.owned)
            if feature_members:
                refresh_features(features, root, feature_members, upgrade_materials=True)
            for obj in incoming:
                restore_features(features, root, obj, states[obj])
            for part in parts:
                if enabled:
                    memory.pop(part, None)
                else:
                    members = objects(root, part)
                    if members:
                        memory[part] = {source_identity(obj): capture_materials(root, obj) for obj in members}
            root[MEMORY] = memory
            features.seal(root)
            assert features.registry_before is not None
            features.registry_before[root] = previous_registry
            root[SCHEMA] = 1
            # Complete every fallible layout/image/seal step before any old
            # Object or embedded Key can be irreversibly retired.
            tx.prepare_commit(image_paths=features.image_paths)
            features.prepare_commit()
            if not tx._commit_prepared or not features._commit_prepared:
                raise RuntimeError("盔甲事务未完成提交准备，未退役原部位")
            finishers = (tx.finish_commit, features.finish_commit)
            retired = live_ids(outgoing)
            if len(retired) != len(outgoing):
                raise ValueError("原盔甲引用在提交准备期间失效，未继续删除")
            for obj in outgoing:
                for coll in tuple(obj.users_collection):
                    coll.objects.unlink(obj)
            _forget(root, outgoing)
            bpy.data.batch_remove(ids=retired)
            for finish in finishers:
                finish()
        features.cleanup()
    controller.update_tag()
    rig.update_tag()


def _binding_repairs(root, rig, excluded=()):
    """Recognize only the released identity-inverse parent reassignment bug."""
    targets = [obj for obj in root.all_objects if part_of(obj) and part_of(obj) not in excluded]
    if not targets:
        return []
    require_owned(root, (root, rig, *targets))
    source = armor_path(root)
    result = []
    def close(first, second):
        return all(abs(float(a) - float(b)) < 1e-5 for row, other in zip(first, second, strict=True) for a, b in zip(row, other, strict=True))
    with AppendTransaction(source) as transaction:
        templates = transaction.append_collection("OXAL_ARMOR_ALL")
        references = {source_identity(obj): obj for obj in templates.all_objects if part_of(obj)}
        for obj in targets:
            reference = references.get(source_identity(obj))
            if reference is None or reference.parent is None or reference.parent.type != "ARMATURE":
                raise ValueError("盔甲来源绑定未知，未改写现有部件：" + obj.name)
            inverse = reference.matrix_parent_inverse.copy()
            if close(obj.matrix_parent_inverse, inverse):
                continue
            identity = inverse.copy();identity.identity()
            known = (obj.parent == rig and source_name(reference.parent) == source_name(rig)
                     and obj.parent_type == reference.parent_type and obj.parent_bone == reference.parent_bone
                     and close(obj.matrix_parent_inverse, identity) and close(obj.matrix_basis, reference.matrix_basis)
                     and geometry_signature(obj, root=root) == geometry_signature(reference)
                     and _equivalent_attribute_schema(obj, _attribute_schema(obj, root=root), _attribute_schema(reference))
                     and _shape_key_contract(obj.data.shape_keys, root=root) == _shape_key_contract(cast(bpy.types.Mesh, reference.data).shape_keys))
            transforms = ("location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale", "matrix_basis", "matrix_parent_inverse")
            if not known or any(animated(obj, field) for field in transforms):
                raise ValueError("盔甲父级或变换含用户改动，未覆盖：" + obj.name)
            result.append((obj, inverse))
    return result


def repair(root, rig):
    """Explicit Beta migration: never retire armor controlled by existing animation."""
    ensure_mutation_allowed()
    from .human import parameter_owner
    controller = parameter_owner(root)
    removable = [part for part in PARTS if not controller[key(part + "_enabled")] and not animated(controller, path(part + "_enabled"))]
    replacements = _binding_repairs(root, rig, excluded=removable)
    before = [(obj, obj.matrix_parent_inverse.copy()) for obj, _ in replacements]
    try:
        for obj, inverse in replacements:
            obj.matrix_parent_inverse = inverse
        set_parts(root, rig, removable, False, repair=True)
    except Exception:
        for obj, inverse in before:
            obj.matrix_parent_inverse = inverse
        raise
    root[SCHEMA] = 1


def enchant(root, parts, *, remove=False, prepare_geometry=False, prepare_outline=False, prepare_pixels=False, activate=False):
    """Use the shared shader service on precise imported armor parts only."""
    ensure_mutation_allowed()
    from ..asset.asset_geometry import Creation
    from ..enchantment import enchantment, enchantment_outline
    from ..outline import outline_native
    from . import human_armor_outline
    if prepare_geometry:
        raise ValueError("盔甲使用原版部位3D与厚度，不能叠加附魔像素凹凸；请使用盔甲的3D开关")
    if not parts or set(parts) - set(PARTS):
        raise ValueError("附魔盔甲部位无效")
    targets = {part: objects(root, part) for part in parts}
    targets = {part: members for part, members in targets.items() if members}
    if not targets:
        raise ValueError("所选部位尚未添加盔甲")
    if not isinstance(activate, bool) or (activate and (remove or not (prepare_outline or prepare_pixels))):
        raise ValueError("盔甲附魔启用操作无效")
    require_owned(root, (obj for members in targets.values() for obj in members))
    owner_key = GLINT_CONTROLS
    existed = owner_key in root
    previous = _plain(root.get(owner_key, {}))
    controls = _plain(root.get(owner_key, {}))
    enable_keys = ("glint_enabled", *(('glint_outline_enabled',) if prepare_outline else ()),
                   *(('glint_pixel_enabled',) if prepare_pixels else ())) if activate else ()
    for part in targets:
        control = controls.get(part)
        if control is not None and any(animated(control, '["' + name + '"]') for name in enable_keys):
            raise ValueError("所选盔甲效果开关已有关键帧、NLA或驱动，请在原动画通道启用；未修改其它部位")
    from ..outline.outline_native import base_slots

    saved = []
    require_owned(root, controls.values())
    control_values = {control: _plain(dict(control.items())) for control in controls.values()}
    def restore():
        for obj, index, material in saved:
            obj.material_slots[index].material = material
        if existed:
            root[owner_key] = previous
        elif owner_key in root:
            del root[owner_key]
        for control, values in control_values.items():
            for name in tuple(control.keys()):
                if name not in values:
                    del control[name]
            for name, value in values.items():
                control[name] = value

    with Creation() as tx:
        tx.owner = root
        tx.on_rollback(restore)
        saved.extend((obj, index, slot.material) for members in targets.values() for obj in members
                     for index, slot in base_slots(root, obj, transaction=tx))
        replacements = {}
        for part, members in targets.items():
            control = controls.get(part)
            if control is None and not remove:
                control = tx.track(bpy.data.objects.new("盔甲附魔 · " + part, None))
                control[OWNER] = root[INSTANCE]
                root.objects.link(control)
                controls[part] = control
            elif control is not None:
                require_owned(root, (control,))
            if control is not None:
                prepare_controls(tx, root, (control,))
                for name in enable_keys:
                    control[name] = True
                if enable_keys:
                    control.update_tag()
            for obj in members:
                if outline_native.RECORD in obj:
                    # Old GN roles do not share the new Solidify all-role
                    # validation, so keep their explicit migration checks.
                    human_armor_outline.generated_modifiers(root, obj, transaction=tx)
                    enchantment_outline.generated_modifiers(root, obj, transaction=tx)
                for _index, slot in base_slots(root, obj, transaction=tx):
                    material = slot.material
                    if material is None:
                        continue
                    require_owned(root, (material,))
                    pair = (control, material)
                    if pair not in replacements:
                        if remove:
                            info = enchantment.inspect_source(tx, material)
                            if info is not None and info["owner"] != control:
                                raise ValueError("盔甲材质附魔归属不匹配，未覆盖")
                            replacements[pair] = enchantment.remove(tx, control, material)
                        else:
                            replacements[pair] = apply_glint(tx, root, control, material, prepare_pixels=prepare_pixels)
        # Validate and prepare every private replacement while the original
        # native hulls and base slots are still intact. Repeated preparation
        # must not retire/rebuild an already correct, disabled shell.
        changed = tuple({obj for obj, _index, material in saved
                         if material is not None and replacements[(controls.get(part_of(obj)), material)] != material})
        if changed:
            suspend_features(tx, root, changed)
        for obj, index, material in saved:
            if material is not None:
                replacement = replacements[(controls.get(part_of(obj)), material)]
                if replacement != material:
                    obj.material_slots[index].material = replacement
                    outline_native.invalidate(tx, obj)
        for part, members in targets.items():
            control = controls.get(part)
            for obj in members:
                if remove:
                    enchantment_outline.remove(tx, root, obj)
                elif control is not None:
                    enchantment_outline.apply(tx, root, obj, control, prepare_outline=prepare_outline)
        root[owner_key] = controls
        if changed:
            human_armor_outline.refresh(tx, root, changed, rebuild=True, glint_current=True)
        tx.commit(root)


def restore_pack(root, bindings, prefix):
    """Restore the original texture material while retaining current glint."""
    ensure_mutation_allowed()
    from ..asset.asset_geometry import Creation
    from ..enchantment import enchantment
    from ..outline.outline_native import base_slots

    requested = {(obj, index) for obj, index, _slot in bindings}
    members = {obj for obj, _index in requested}
    bindings = tuple((obj, index, slot) for obj in members for index, slot in base_slots(root, obj)
                     if (obj, index) in requested and slot.material is not None)
    changed = []
    def restore():
        for obj, index, saved, previous, backup in reversed(changed):
            obj.material_slots[index].material = previous
            obj[saved] = backup

    with Creation() as tx:
        tx.owner = root
        tx.on_rollback(restore)
        suspend_features(tx, root, members)
        bindings = tuple((obj, index, obj.material_slots[index]) for obj, index, _slot in bindings)
        replacements = {}
        for obj, index, slot in bindings:
            saved = prefix + str(index)
            backup = obj.get(saved)
            if backup is None:
                continue
            require_owned(root, (obj, backup, slot.material))
            info = enchantment.inspect(slot.material)
            pair = (slot.material, backup)
            if pair not in replacements:
                if info:
                    prepare_controls(tx, root, (info["owner"],))
                    replacement = apply_glint(tx, root, info["owner"], backup,
                                              images_from=slot.material)
                elif (prior := enchantment.inspect(backup)) is not None:
                    replacement = enchantment.remove(tx, prior["owner"], backup)
                else:
                    replacement = backup
                replacements[pair] = replacement
            changed.append((obj, index, saved, slot.material, backup))
            slot.material = replacements[pair]
            del obj[saved]
        # Every temporarily suspended hull is restored even when the selected
        # source material had no saved texture backup to publish.
        refresh_features(tx, root, members)
        tx.commit(root)
