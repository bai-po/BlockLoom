"""Explicit, transactional model-part replacement; never evaluated during playback."""

import json
from contextlib import ExitStack, contextmanager
from typing import Any, cast

import bpy
from mathutils import Matrix

from ...core.runtime import ensure_mutation_allowed
from ...util import drivers
from ...util.geometry_nodes import get_input, input_path, set_input
from ...util.ownership import (
    REGISTRY,
    explicit_scene_ids,
    owned_ids,
    require_no_external_users,
    require_owned,
    seal_owned,
)
from ...util.resources import INSTANCE, AppendTransaction, live_ids, resolve_view_layer
from . import human_back as back
from .human_state import ARMS, LEGS, OWNER, SOURCE_NAME, key, path, refresh_limits

WING_BONES = ("鞘翅开合·轨道", "wing.L", "up.L", "down.L", "bottom.L", "wing.R", "up.R", "down.R", "bottom.R", "鞘翅展开")
POSE_MEMORY = "oxal_human_elytra_pose"
MATERIAL_MEMORY = "oxal_human_elytra_materials"
PACK_BACKUP = "oxal_human_original_elytra_"


def _capture_materials(obj, *, slots=None):
    """Retain actual material IDs, never a removed Object/Slot RNA wrapper."""
    slots = tuple(enumerate(obj.material_slots)) if slots is None else tuple(slots)
    return {
        "count": len(slots),
        "materials": {str(i): slot.material for i, slot in slots if slot.material is not None},
        "links": {str(i): slot.link for i, slot in slots},
        "backups": {name: value for name, value in obj.items() if name.startswith((PACK_BACKUP, "oxal_human_original_cape_")) and isinstance(value, bpy.types.Material)},
        "faces": [polygon.material_index for polygon in obj.data.polygons],
    }


def _read_material_memory(root):
    state = root.get(MATERIAL_MEMORY)
    if state is None:
        return None
    if not hasattr(state, "get") or set(state.keys()) != {"count", "materials", "links", "backups", "faces"}:
        raise ValueError("鞘翅材质保留记录损坏，未修改部件")
    record = {"count": int(state["count"]), "faces": list(state["faces"])}
    for name in ("materials", "links", "backups"):
        if not hasattr(state[name], "items"):
            raise ValueError("鞘翅材质保留记录损坏，未修改部件")
        record[name] = dict(state[name].items())
    if record["count"] < 0 or any(not isinstance(material, bpy.types.Material) for name in ("materials", "backups") for material in record[name].values()):
        raise ValueError("鞘翅保留材质引用无效，未修改部件")
    return record


def _restore_materials(obj, record):
    while len(obj.data.materials) > record["count"]:
        obj.data.materials.pop(index=len(obj.data.materials)-1)
    while len(obj.data.materials) < record["count"]:
        obj.data.materials.append(None)
    for index, slot in enumerate(obj.material_slots):
        slot.link = record["links"][str(index)]
        slot.material = record["materials"].get(str(index))
    for name, material in record["backups"].items():
        obj[name] = material
    if len(record["faces"]) != len(obj.data.polygons):
        raise ValueError("鞘翅材质面映射与源网格不匹配，未替换现有部件")
    for polygon, index in zip(obj.data.polygons, record["faces"], strict=True):
        polygon.material_index = index


def _name(item):
    saved = item.get(SOURCE_NAME)
    ref = item.library_weak_reference
    return str(saved) if saved else ref.id_name[2:] if ref else item.name


def _collection(root, name):
    found = [c for c in root.children_recursive if _name(c) == name or (_name(c).startswith(name+".") and _name(c)[len(name)+1:].isdigit())]
    if len(found) != 1:
        raise ValueError("人模部件合集不唯一：" + name)
    return found[0]


@contextmanager
def _edit_rig(rig):
    ensure_mutation_allowed()
    if bpy.context.mode != "OBJECT":
        raise ValueError("请在物体模式切换人模部件")
    layer = resolve_view_layer(cast(Any, bpy.context.scene), require_active=True)
    active, selected = layer.objects.active, tuple(bpy.context.selected_objects)
    hidden = rig.hide_get(view_layer=layer)
    try:
        for obj in selected:
            obj.select_set(False, view_layer=layer)
        rig.hide_set(False, view_layer=layer)
        rig.select_set(True, view_layer=layer)
        layer.objects.active = rig
        bpy.ops.object.mode_set(mode="EDIT")
        yield rig.data.edit_bones
    finally:
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        rig.select_set(False, view_layer=layer)
        rig.hide_set(hidden, view_layer=layer)
        for obj in selected:
            if layer.objects.get(obj.name) == obj:
                obj.select_set(True, view_layer=layer)
        layer.objects.active = active


def _bone_path(path, names=WING_BONES):
    return any(path.startswith(prefix + '[' + json.dumps(name, ensure_ascii=False) + ']')
               for name in names for prefix in ("pose.bones", "bones"))


def _capture_wings(rig, names=WING_BONES):
    from .human_armor import _plain

    missing = set(names) - set(rig.data.bones.keys())
    if missing:
        raise ValueError("鞘翅源骨架不完整")
    bones = []
    for name in names:
        bone, pose = rig.data.bones[name], rig.pose.bones[name]
        constraints = []
        for constraint in pose.constraints:
            fields = {}
            for prop in constraint.bl_rna.properties:
                if prop.identifier in {"rna_type", "type"} or prop.is_readonly:
                    continue
                value = getattr(constraint, prop.identifier)
                if prop.type in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}:
                    fields[prop.identifier] = list(value) if getattr(prop, "is_array", False) else value
                elif prop.type == "POINTER" and value is not None:
                    fields[prop.identifier] = value
            constraints.append((constraint.type, fields))
        bones.append({"name": name, "matrix": bone.matrix_local.copy(), "length": bone.length,
                      "parent": bone.parent.name if bone.parent else "", "deform": bone.use_deform,
                      "connect": bone.use_connect, "collections": [c.name for c in bone.collections],
                      "flags": {name: getattr(bone,name) for name in ("hide", "hide_select", "use_inherit_rotation", "inherit_scale", "use_local_location", "use_relative_parent")},
                      "rotation_mode": pose.rotation_mode, "basis": pose.matrix_basis.copy(),
                      "shape": pose.custom_shape, "shape_scale": tuple(pose.custom_shape_scale_xyz),
                      "shape_translation": tuple(pose.custom_shape_translation),
                      "shape_rotation": tuple(pose.custom_shape_rotation_euler),
                      "shape_options": {field: getattr(pose, field) for field in ("use_custom_shape_bone_size", "custom_shape_wire_width")
                                        if pose.bl_rna.properties.get(field) is not None},
                      "shape_transform": pose.custom_shape_transform.name if pose.custom_shape_transform else "",
                      "constraints": constraints, "locks": (tuple(pose.lock_location), tuple(pose.lock_rotation), tuple(pose.lock_scale))})
        bones[-1]["custom"] = (_plain(dict(bone.items())), _plain(dict(pose.items())))
        bones[-1]["custom_ui"] = tuple({field: owner.id_properties_ui(field).as_dict()
                                        for field, value in owner.items() if isinstance(value, (str, int, float, bool))}
                                       for owner in (bone, pose))
    drivers = []
    for driver_owner in (rig, rig.data):
        if driver_owner.animation_data is None:
            continue
        for curve in driver_owner.animation_data.drivers:
            if not _bone_path(curve.data_path, names):
                continue
            variables = []
            for variable in curve.driver.variables:
                targets = []
                for target in variable.targets:
                    targets.append({"id": target.id, "self_data": target.id == rig.data, "id_type": target.id_type, "data_path": target.data_path,
                                    "bone_target": target.bone_target, "transform_type": target.transform_type,
                                    "transform_space": target.transform_space})
                variables.append((variable.name, variable.type, targets))
            drivers.append((driver_owner == rig.data, curve.data_path, curve.array_index, curve.driver.type, curve.driver.expression, variables))
    return bones, drivers


def _restore_wings(rig, snapshot, source_rig, *, collection_bindings=None):
    bones, drivers = snapshot
    with _edit_rig(rig) as editable:
        for spec in bones:
            bone = editable.get(spec["name"]) or editable.new(spec["name"])
            # A newly created EditBone is zero length. Establish its length
            # before the authored matrix, otherwise native RNA discards the
            # matrix rotation and the later length extends along default +Z.
            bone.length = spec["length"]
            bone.matrix = spec["matrix"]
            bone.use_deform = spec["deform"]
        for spec in bones:
            bone = editable[spec["name"]]
            bone.parent = editable.get(spec["parent"])
            bone.use_connect = spec["connect"]
    for spec in bones:
        bone, pose = rig.data.bones[spec["name"]], rig.pose.bones[spec["name"]]
        for owner, values, ui in zip((bone, pose), spec.get("custom", ({}, {})), spec.get("custom_ui", ({}, {})), strict=True):
            for field, value in values.items():
                owner[field] = value
            for field, options in ui.items():
                owner.id_properties_ui(field).update(**options)
        for name, value in spec["flags"].items():
            setattr(bone, name, value)
        for label in spec["collections"]:
            collection = (collection_bindings.get(label) if collection_bindings is not None
                          else rig.data.collections_all.get(label))
            if collection:
                collection.assign(bone)
        pose.rotation_mode = spec["rotation_mode"]
        pose.matrix_basis = spec["basis"]
        pose.custom_shape = spec["shape"]
        pose.custom_shape_scale_xyz = spec["shape_scale"]
        pose.custom_shape_translation = spec["shape_translation"]
        pose.custom_shape_rotation_euler = spec["shape_rotation"]
        for field, value in spec.get("shape_options", {}).items():
            setattr(pose, field, value)
        if "shape_transform" in spec:
            name = spec["shape_transform"]
            if name and rig.pose.bones.get(name) is None:
                raise ValueError("背饰控件变换骨不在实际主骨架中，未绑定其它骨架")
            pose.custom_shape_transform = rig.pose.bones.get(name) if name else None
        pose.lock_location, pose.lock_rotation, pose.lock_scale = spec["locks"]
        for constraint in tuple(pose.constraints):
            pose.constraints.remove(constraint)
        for kind, fields in spec["constraints"]:
            constraint = pose.constraints.new(kind)
            for name, value in fields.items():
                setattr(constraint, name, rig if value == source_rig else value)
    for driver_owner in (rig, rig.data):
        if driver_owner.animation_data is None:
            continue
        for curve in tuple(driver_owner.animation_data.drivers):
            if _bone_path(curve.data_path, (spec["name"] for spec in bones)):
                driver_owner.animation_data.drivers.remove(curve)
    for is_data, curve_path, index, kind, expression, variables in drivers:
        driver_owner = rig.data if is_data else rig
        value = driver_owner.path_resolve(curve_path)
        curve = (driver_owner.driver_add(curve_path, index)
                 if hasattr(value, "__len__") and not isinstance(value, str)
                 else driver_owner.driver_add(curve_path))
        curve.driver.type = kind
        curve.driver.expression = expression
        for name, variable_type, targets in variables:
            variable = curve.driver.variables.new()
            variable.name, variable.type = name, variable_type
            for target, data in zip(variable.targets, targets, strict=True):
                if variable_type == "SINGLE_PROP":
                    target.id_type = data["id_type"]
                target.id = rig.data if data.get("self_data") else rig if data["id"] == source_rig else data["id"]
                target.data_path = data["data_path"]
                if variable_type == "TRANSFORMS":
                    target.bone_target = data["bone_target"]
                    target.transform_type = data["transform_type"]
                    target.transform_space = data["transform_space"]


def _drop_wings(rig, names=WING_BONES):
    for driver_owner in (rig, rig.data):
        if driver_owner.animation_data is None:
            continue
        for curve in tuple(driver_owner.animation_data.drivers):
            if _bone_path(curve.data_path, names):
                driver_owner.animation_data.drivers.remove(curve)
    with _edit_rig(rig) as editable:
        for name in reversed(names):
            bone = editable.get(name)
            if bone:
                editable.remove(bone)


def _back_widgets(rig, names):
    """Only native shapes exclusively consumed by the retiring bone closure."""
    selected = {pose.custom_shape for pose in rig.pose.bones if pose.name in names and pose.custom_shape}
    retained = {pose.custom_shape for pose in rig.pose.bones if pose.name not in names and pose.custom_shape}
    return selected - retained


def _forget(root, items):
    registry = root.get(REGISTRY)
    if registry is not None:
        for token, item in tuple(registry.items()):
            if item in items:
                del registry[token]


def _check_retirement(root, objects):
    require_owned(root, objects)
    require_no_external_users(cast(Any, bpy.context.scene), root, objects)
    domain = {root, *root.children_recursive, *root.all_objects, *owned_ids(root)}
    domain.update(getattr(item, "node_tree", None) for item in tuple(domain))
    users = bpy.data.user_map(subset=objects)
    for obj in objects:
        animation = obj.animation_data
        if animation and (animation.action or animation.nla_tracks):
            raise ValueError("待替换部件含独立对象动画，未丢弃：" + obj.name)
        scene = bpy.context.scene
        if any(c not in domain for c in obj.users_collection) or any(user not in domain and user != scene for user in users.get(obj, ())):
            raise ValueError("其它工程数据正在引用待替换部件：" + obj.name + ":" + str([(type(u).__name__, u.name) for u in users.get(obj, ()) if u not in domain]))


def _contains_id(value, target):
    if isinstance(value, bpy.types.ID):
        return value == target
    if hasattr(value, "items"):
        return any(_contains_id(part, target) for part in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_id(part, target) for part in value)
    return False


def _unused_leg_lattices(root, retiring, *, proof=()):
    """Retire only source leg lattices whose last actual consumers retire here."""
    candidates = {target for obj in retiring for target in
                  (obj.parent, *(getattr(modifier, "object", None) for modifier in obj.modifiers))
                  if target is not None and target.type == "LATTICE" and _name(target) in {"R腿晶格", "L腿晶格"}}
    if not candidates:
        return set()
    existing = candidates - set(proof)
    if existing:
        require_owned(root, existing)
    containers = {root, *root.children_recursive}
    scene = cast(Any, bpy.context.scene)
    explicit = explicit_scene_ids(scene, root=root)
    users = bpy.data.user_map(subset=tuple(candidates))
    return {target for target in candidates
            if target not in explicit
            and not any(_contains_id(value, target) for collection in containers for name, value in collection.items() if name != REGISTRY)
            and all(user in retiring or user in containers or user == scene for user in users.get(target, ()))}


def _check_leg_users(root, members, outgoing):
    """Surviving owned users are also external to this part's retirement."""
    containers = {root, *root.children_recursive}
    scene = cast(Any, bpy.context.scene)
    explicit = explicit_scene_ids(scene, root=root)
    users = bpy.data.user_map(subset=members)
    for obj in members:
        if (obj in explicit
                or any(_contains_id(value, obj) for collection in containers for name, value in collection.items() if name != REGISTRY)
                or any(user not in outgoing and user not in containers and user != scene for user in users.get(obj, ()))):
            raise ValueError("其它数据仍引用待退役腿部，未替换：" + obj.name)


def _legacy_leg_dependencies(root, outgoing):
    """Known source objects retained unlinked by earlier armor appends."""
    names = {"R腿二层", "L腿二层", "R腿-官方二层", "L腿-官方二层", "R腿晶格", "L腿晶格"}
    attached = set(root.all_objects)
    candidates = {item for item in owned_ids(root) if isinstance(item, bpy.types.Object)
                  and item not in attached and item.type in {"MESH", "LATTICE"} and _name(item) in names}
    containers = {root, *root.children_recursive}
    scene = cast(Any, bpy.context.scene)
    explicit = explicit_scene_ids(scene, root=root)
    users = bpy.data.user_map(subset=tuple(candidates))
    while candidates:
        retained = {item for item in candidates if item not in explicit
                    and not any(_contains_id(value, item) for collection in containers for name, value in collection.items() if name != REGISTRY)
                    and all(user in outgoing or user in candidates or user in containers or user == scene for user in users.get(item, ()))}
        if retained == candidates:
            break
        candidates = retained
    return candidates


def _leg_graph(tree, seen=frozenset()):
    from .human_layer_resolution import _tree_contract

    if tree.animation_data:
        raise ValueError("腿部几何节点含独立动画或驱动，未替换")
    if tree in seen:
        raise ValueError("腿部几何节点含循环引用，未替换")
    return (_tree_contract(tree), [(node.name, _leg_graph(node.node_tree, seen | {tree})) for node in tree.nodes
                                   if node.type == "GROUP" and node.node_tree is not None])


def _validate_body_legs(root, members, references, graphs, prepare, rig, skin, lattice_references=None, raw_members=()):
    """Compare retiring native legs with this transaction's immutable source."""
    from . import human_layer_resolution as layer
    lattice_references = lattice_references or {}
    raw_members = set(raw_members)
    aliases = {}
    for obj in members:
        source = _name(obj)
        while source not in references:
            prefix, separator, suffix = source.rpartition(".")
            if not separator or not suffix.isdigit():
                raise ValueError("腿部网格来源不明，未替换：" + obj.name)
            source = prefix
        # This is only a candidate correspondence. The exact owned Mesh,
        # topology, weights, native drivers and bindings are checked below.
        aliases[obj] = references[source]
    aliases.update(lattice_references)

    def close(first, second):
        return all(abs(a - b) < .000001 for row, other in zip(first, second, strict=True)
                   for a, b in zip(row, other, strict=True))

    def curves(obj, excluded="") -> list[dict[str, Any]]:
        from ..outline.outline_native import generated_driver_paths

        outlines = generated_driver_paths(root, obj)
        snapshot = drivers.capture(obj)
        result = [curve for curve in snapshot["curves"]
                  if (not excluded or not curve["path"].startswith(excluded)) and curve["path"] not in outlines]
        def clean(value: Any) -> Any:
            if isinstance(value, bpy.types.ID):
                return aliases.get(value, value)
            if isinstance(value, dict):
                return {key: clean(part) for key, part in value.items()
                        if key not in {"select", "select_control_point", "select_left_handle", "select_right_handle", "hide"}}
            if isinstance(value, (list, tuple)):
                return [clean(part) for part in value]
            return value
        return clean(result)

    def settings(modifier, animated):
        result = {}
        for prop in modifier.bl_rna.properties:
            name = prop.identifier
            if (prop.is_readonly or name in {"name", "is_active", "show_expanded", "node_group", "properties"}
                    or modifier.path_from_id(name) in animated
                    or prop.type not in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING", "POINTER"}):
                continue
            value = getattr(modifier, name)
            result[name] = (tuple(value) if getattr(prop, "is_array", False)
                            else aliases.get(value, value) if isinstance(value, bpy.types.ID) else value)
        if modifier.type == "NODES":
            result["inputs"] = {socket.identifier: get_input(modifier, socket.identifier)
                                for socket in modifier.node_group.interface.items_tree
                                if getattr(socket, "in_out", "") == "INPUT"
                                and socket.bl_rna.properties.get("default_value") is not None
                                and input_path(modifier, socket.identifier) not in animated}
        return result

    for obj in members:
        reference = aliases.get(obj)
        if reference is None or obj.type != "MESH" or reference.type != "MESH":
            raise ValueError("腿部网格来源不明，未替换：" + obj.name)
        require_owned(root, (obj, obj.data))
        record = layer._record(obj)
        source = layer._source(obj)
        generated = None
        if source is not None:
            row = layer.declarations(root).get(_name(reference))
            if row is None:
                raise ValueError("腿部二层来源未知，未替换")
            generated = layer._validate(root, obj, source, row, set())
        if (obj.data.shape_keys or obj.data.animation_data
                or layer.fingerprint(obj, exclude=record.get("attributes", {}) if record else ()) != layer.fingerprint(reference)):
            raise ValueError("腿部网格、UV或权重含用户修改，未替换：" + obj.name)
        from ..outline.outline_native import base_slots

        slots = tuple(slot for _, slot in base_slots(root, obj))
        require_owned(root, (slot.material for slot in slots if slot.material is not None))
        if (len(slots) != len(reference.material_slots)
                or any(slot.link != template.link or slot.material != template.material
                       for slot, template in zip(slots, reference.material_slots, strict=True))):
            raise ValueError("腿部材质绑定含用户修改，未覆盖：" + obj.name)
        from .human_outline import generated_modifiers as outline_modifiers

        outlines = outline_modifiers(root, obj)
        actual_modifiers = tuple(modifier for modifier in obj.modifiers if modifier != generated and modifier not in outlines)
        expected_modifiers = tuple(reference.modifiers)
        if [(m.name, m.type) for m in actual_modifiers] != [(m.name, m.type) for m in expected_modifiers]:
            raise ValueError("腿部修改器含用户修改，未替换：" + obj.name)
        for modifier, expected in zip(actual_modifiers, expected_modifiers, strict=True):
            if modifier.type == "NODES" and _leg_graph(modifier.node_group) != graphs[reference][expected.name]:
                raise ValueError("腿部几何节点含用户修改，未替换：" + obj.name)
        destination = (next(collection for collection in obj.users_collection if collection in root.children_recursive)
                       if obj not in raw_members else None)
        if destination is not None:
            destination.objects.link(reference)
        try:
            if obj not in raw_members:
                prepare(root, rig, objects=(reference,))
            reference_source = layer._source(reference)
            if reference_source is not None and obj not in raw_members:
                for socket in reference_source.node_group.interface.items_tree:
                    if getattr(socket, "in_out", "") == "INPUT" and socket.name == "Image":
                        set_input(reference_source, socket.identifier, skin)
            actual = curves(obj, generated.path_from_id() + "." if generated else "")
            expected = curves(reference)
            if actual != expected:
                raise ValueError("腿部包含未知驱动，未替换：" + obj.name)
            animated = {curve["path"] for curve in actual}
            for modifier, template in zip(actual_modifiers, expected_modifiers, strict=True):
                if settings(modifier, animated) != settings(template, animated):
                    raise ValueError("腿部修改器参数含用户修改，未替换：" + obj.name)
            if (aliases.get(obj.parent, obj.parent) != aliases.get(reference.parent, reference.parent) or obj.parent_type != reference.parent_type
                    or obj.parent_bone != reference.parent_bone or obj.constraints
                    or not close(obj.matrix_basis, reference.matrix_basis)
                    or not close(obj.matrix_parent_inverse, reference.matrix_parent_inverse)):
                raise ValueError("腿部绑定或变换含用户修改，未替换：" + obj.name)
        finally:
            if destination is not None:
                destination.objects.unlink(reference)
    for obj, reference in lattice_references.items():
        from .human_armor import _shape_key_contract

        require_owned(root, (obj, obj.data))
        def lattice(value):
            return (value.points_u, value.points_v, value.points_w, value.interpolation_type_u,
                    value.interpolation_type_v, value.interpolation_type_w, value.use_outside,
                    [tuple(point.co_deform) for point in value.points])
        if (lattice(obj.data) != lattice(reference.data)
                or _shape_key_contract(obj.data.shape_keys, root=root) != _shape_key_contract(reference.data.shape_keys)
                or obj.data.animation_data
                or curves(obj) != curves(reference)
                or [(m.name, m.type) for m in obj.modifiers] != [(m.name, m.type) for m in reference.modifiers]
                or obj.parent != reference.parent or obj.parent_type != reference.parent_type or obj.parent_bone != reference.parent_bone
                or obj.constraints or not close(obj.matrix_basis, reference.matrix_basis)
                or not close(obj.matrix_parent_inverse, reference.matrix_parent_inverse)):
            raise ValueError("腿部晶格含用户修改，未退役：" + obj.name)
        animated = {curve["path"] for curve in curves(obj)}
        if any(settings(modifier, animated) != settings(template, animated)
               for modifier, template in zip(obj.modifiers, reference.modifiers, strict=True)):
            raise ValueError("腿部晶格修改器含用户修改，未退役：" + obj.name)


def prune_initial(root, rig, transaction, arm_mode, leg_mode):
    """Remove disabled source geometry before the new instance is committed."""
    from .human_armor import part_of

    unwanted = set(_collection(root, ARMS["WIDE" if arm_mode == "SLIM" else "SLIM"]).all_objects)
    unwanted.update(_collection(root, LEGS["OFFICIAL" if leg_mode == "NORMAL" else "NORMAL"]).all_objects)
    # Authored body templates contain legacy armor in hidden branches. A new
    # instance has no added armor parts: discard those proven new meshes before
    # publication; armor_set appends only the explicitly requested part later.
    unwanted.update(obj for obj in tuple(root.all_objects) if obj.type == "MESH" and part_of(obj))
    from . import human_back242

    if human_back242.current(root):
        human_back242.prune_initial(root, rig, transaction)
    else:
        unwanted.update(o for o in root.all_objects if _name(o) in back.MODE_OBJECT.values())
        unwanted.update(obj for obj in _back_widgets(rig, back.ALL_BONES) if obj in transaction.owned)
    unwanted.update(_unused_leg_lattices(root, unwanted, proof=transaction.owned))
    transaction.remove(unwanted)
    transaction.prune_unused()
    if not human_back242.current(root):
        _drop_wings(rig, back.ALL_BONES)
    root[back.SCHEMA] = human_back242.MAIN_SCHEMA if human_back242.current(root) else back.SCHEMA_VERSION
    root["oxal_human_parts_schema"] = 1


def replace(root, rig, values, asset_path, prepare):
    """Prepare source-backed replacement, then retire only confirmed old objects."""
    ensure_mutation_allowed()
    from . import human_back242
    from .human_source import armor_path, root_name

    is242 = human_back242.current(root)
    all_back_bones = back.all_bones(root)
    controller = root["oxal_human_controls"]
    previous = {name: controller.get(key(name)) for name in values}
    arms_changed = "arm_mode" in values and int(previous["arm_mode"]) != values["arm_mode"]
    old_back_mode = int(controller.get(key("back_mode"), 0))
    back_mode = int(values.get("back_mode", old_back_mode))
    wanted_bones = back.bones(back_mode, root)
    back_objects = tuple(obj for obj in root.all_objects if _name(obj) in back.MODE_OBJECT.values())
    accessory = back.rig_for(root, rig)
    back_rig = accessory or rig
    old_back_bones = tuple(name for name in all_back_bones if back_rig.data.bones.get(name))
    wanted_back_objects = {back.MODE_OBJECT[back_mode]} if back_mode else set()
    wings_changed = "back_mode" in values and (old_back_mode != back_mode
        or {_name(obj) for obj in back_objects} != wanted_back_objects
        or set(old_back_bones) != set(wanted_bones)
        or (bool(accessory) if is242 else bool(accessory) != bool(back_mode))
        or root.get(back.SCHEMA) != (human_back242.MAIN_SCHEMA if is242 else back.SCHEMA_VERSION))
    from ..asset.asset_geometry import Creation
    from .human_armor import (
        _plain,
        animated,
        capture_materials,
        current_variant,
        initialize_geometry,
        part_of,
        restore_features,
        source_identity,
        validate_parts,
    )

    armor_legs = tuple(o for o in root.all_objects if part_of(o) in {"legs", "feet"})
    leg_target = int(values.get("leg_mode", previous.get("leg_mode", controller[key("leg_mode")])))
    leg_residue = bool(_collection(root, tuple(LEGS.values())[1 - leg_target]).all_objects) if "leg_mode" in values else False
    legs_changed = "leg_mode" in values and (int(previous["leg_mode"]) != leg_target or leg_residue)
    if legs_changed and animated(controller, path("leg_mode")):
        raise ValueError("腿类型已有Action、NLA或驱动，未进行结构替换")
    if wings_changed:
        back.validate_mode_control(root, back_rig)
        back.validate_retirement(root, back_rig, old_back_bones)
        if is242 and accessory is None:
            human_back242.collection_bindings(root, rig)
        if animated(controller, path("back_mode")):
            raise ValueError("背饰模式已有Action、NLA或驱动，未进行结构替换")
    body_legs = tuple(obj for label in LEGS.values() for obj in _collection(root, label).all_objects) if legs_changed else ()
    if not arms_changed and not wings_changed and not legs_changed:
        if is242 and "back_mode" in values:
            with Creation() as features:
                features.owner = root
                human_back242.repair_display(features, root, rig, back_mode)
                human_back242.focus_controls(features, root, rig, back_mode)
                features.commit(root)
        return False
    from .human_layer_resolution import image_for
    skin = image_for(root) if arms_changed or legs_changed else None
    require_owned(root, (rig, rig.data, controller))
    # Mesh style replacement leaves the public Armature data untouched. Bone
    # edits allow proven in-instance readers, while rejecting external users.
    if wings_changed and accessory is None and (old_back_bones or is242):
        require_no_external_users(cast(Any, bpy.context.scene), root, (rig.data,))
    elif wings_changed and accessory is not None:
        require_no_external_users(cast(Any, bpy.context.scene), root, (accessory, accessory.data))
    if bpy.context.mode != "OBJECT":
        raise ValueError("请在物体模式切换人模部件")
    outgoing = set()
    if arms_changed:
        old_label = tuple(ARMS.values())[int(previous["arm_mode"])]
        outgoing.update(_collection(root, old_label).all_objects)
    if wings_changed:
        outgoing.update(back_objects)
        if is242:
            outgoing.update(human_back242.members(root))
        outgoing.update(_back_widgets(back_rig, old_back_bones))
        # A shape shared with the public body rig must remain public.
        if accessory:
            outgoing.difference_update(pose.custom_shape for pose in rig.pose.bones if pose.custom_shape)
            outgoing.add(accessory)
    if legs_changed:
        outgoing.update(armor_legs)
        outgoing.update(body_legs)
    legacy_legs = _legacy_leg_dependencies(root, outgoing) if legs_changed else set()
    outgoing.update(legacy_legs)
    leg_lattices = _unused_leg_lattices(root, outgoing) if legs_changed else set()
    leg_lattices.update(obj for obj in legacy_legs if obj.type == "LATTICE")
    outgoing.update(leg_lattices)
    raw_legs = tuple(obj for obj in legacy_legs if obj.type == "MESH")
    if legs_changed:
        _check_leg_users(root, {*body_legs, *raw_legs, *(obj.data for obj in (*body_legs, *raw_legs)), *leg_lattices}, outgoing)
    armor_materials = {source_identity(obj).replace("-官方腿", ""): capture_materials(root, obj) for obj in armor_legs} if legs_changed else {}
    _check_retirement(root, outgoing)
    if wings_changed and old_back_bones:
        retiring_dependencies = {obj.data for obj in outgoing if obj.data is not None}
        require_owned(root, retiring_dependencies)
        for user in bpy.data.user_map(subset=(back_rig,)).get(back_rig, ()):
            if user in outgoing or user in retiring_dependencies:
                continue
            if isinstance(user, bpy.types.Object):
                if user.parent == back_rig and user.parent_type == "BONE" and user.parent_bone in old_back_bones:
                    raise ValueError("外部对象仍绑定背饰骨骼，未切换背饰")
                for constraint in user.constraints:
                    if getattr(constraint, "target", None) == back_rig and getattr(constraint, "subtarget", "") in old_back_bones:
                        raise ValueError("外部约束仍引用背饰骨骼，未切换背饰")
                if any(mod.type == "ARMATURE" and getattr(mod, "object", None) == back_rig for mod in user.modifiers) and any(group.name in old_back_bones for group in user.vertex_groups):
                    raise ValueError("外部网格仍使用背饰变形，未切换背饰")
            animation = getattr(user, "animation_data", None)
            if animation:
                for curve in animation.drivers:
                    if user in {back_rig, back_rig.data} and _bone_path(curve.data_path, old_back_bones):
                        continue
                    for variable in curve.driver.variables:
                        if any(target.id == back_rig and (target.bone_target in old_back_bones or _bone_path(target.data_path, old_back_bones)) for target in variable.targets):
                            raise ValueError("外部驱动仍引用背饰骨骼，未切换背饰")
    old_registry = dict(root[REGISTRY].items())
    old_links = {obj: tuple(obj.users_collection) for obj in outgoing}
    retiring_bone_parents = {obj: (obj.parent_type, obj.parent_bone, obj.matrix_basis.copy(), obj.matrix_parent_inverse.copy())
                            for obj in outgoing if is242 and obj.parent == rig
                            and obj.parent_type == "BONE" and obj.parent_bone in old_back_bones}
    memory_existed, old_memory = back.POSE_MEMORY in root, root.get(back.POSE_MEMORY)
    back_schema_existed, old_back_schema = back.SCHEMA in root, root.get(back.SCHEMA)
    from .human import MAIN_RIG

    back_reference_existed, old_back_reference = back.RIG in root, root.get(back.RIG)
    old_back_members = _plain(root.get(human_back242.MEMBERS, {}))
    contract_existed, old_contract = back.CONTRACT in root, root.get(back.CONTRACT)
    original_bone_collections = {collection.name: collection for collection in rig.data.collections_all}
    main_reference_existed, old_main_reference = MAIN_RIG in root, root.get(MAIN_RIG)
    material_memory_existed = back.MATERIAL_MEMORY in root
    old_material_memory = _plain(root.get(back.MATERIAL_MEMORY, {}))
    back_material_memory = _plain(root.get(back.MATERIAL_MEMORY, {}))
    if "New model" not in back_material_memory and MATERIAL_MEMORY in root:
        back_material_memory["New model"] = _read_material_memory(root)
    from ..outline.outline_native import base_slots

    for obj in back_objects if wings_changed else ():
        back_material_memory[_name(obj)] = _capture_materials(obj, slots=base_slots(root, obj))
    active_back_name = back.MODE_OBJECT.get(old_back_mode)
    if active_back_name in back_material_memory and old_back_mode in {2, 3}:
        back_material_memory["cape"] = back_material_memory[active_back_name]
    wing_backup = _capture_wings(back_rig, old_back_bones) if wings_changed and old_back_bones else None
    with ExitStack() as stack:
        transaction = stack.enter_context(AppendTransaction(asset_path))
        staged: Any = transaction.append_collection(root_name(root)) if arms_changed or legs_changed or not is242 else None
        source_rig = next(o for o in staged.all_objects if o.type == "ARMATURE" and not back.is_back_rig(o)) if staged else rig
        back_transaction = None
        back_staged: Any = staged
        back_source_rig = source_rig
        if is242 and wings_changed and back_mode:
            back_transaction = stack.enter_context(AppendTransaction(human_back242.SOURCE))
            back_staged = back_transaction.append_collection(human_back242.COLLECTIONS[back_mode])
            back_source_rig = next(obj for obj in back_staged.all_objects if obj.type == "ARMATURE")
            for item in tuple(back_transaction.owned):
                transaction.track(item)
        # The 2.4.2 source Armature is temporary. Only its selected bone closure
        # is installed into the existing body Armature at transaction commit.
        back_source_objects = set(back_staged.all_objects) if back_transaction is not None else set()
        back_source_ids = set(back_transaction.owned) if back_transaction is not None else set()
        if wings_changed and not is242:
            for obj in staged.all_objects:
                name = _name(obj)
                while name not in back.MODE_OBJECT.values():
                    prefix, separator, suffix = name.rpartition(".")
                    if not separator or not suffix.isdigit():
                        break
                    name = prefix
                if name in back.MODE_OBJECT.values():
                    obj[SOURCE_NAME] = name
        leg_references = {}
        if legs_changed:
            names = {"R腿", "L腿", "R腿二层", "L腿二层", "R腿-官方", "L腿-官方", "R腿-官方二层", "L腿-官方二层"}
            for label in LEGS.values():
                for obj in _collection(staged, label).all_objects:
                    name = _name(obj)
                    while name not in names:
                        prefix, separator, suffix = name.rpartition(".")
                        if not separator or not suffix.isdigit():
                            raise ValueError("腿部源资源包含未知对象：" + name)
                        name = prefix
                    obj[SOURCE_NAME] = name
                    leg_references[name] = obj
        leg_graphs = {obj: {modifier.name: _leg_graph(modifier.node_group) for modifier in obj.modifiers
                            if modifier.type == "NODES" and modifier.node_group} for obj in leg_references.values()}
        source_lattices = {}
        if legs_changed:
            for obj in tuple(staged.all_objects):
                if obj.type != "LATTICE":
                    continue
                name = _name(obj)
                while name not in {"R腿晶格", "L腿晶格"}:
                    prefix, separator, suffix = name.rpartition(".")
                    if not separator or not suffix.isdigit():
                        break
                    name = prefix
                if name in {"R腿晶格", "L腿晶格"}:
                    obj[SOURCE_NAME] = name
                    source_lattices[name] = obj
        armor_transaction, armor_staged = None, None
        if legs_changed:
            wanted_parts = {part_of(obj) for obj in armor_legs}
            armor_staged = staged
            if not wanted_parts <= {part_of(obj) for obj in staged.all_objects}:
                armor_source = armor_path(root)
                armor_transaction = stack.enter_context(AppendTransaction(armor_source))
                armor_staged = armor_transaction.append_collection("OXAL_ARMOR_ALL")
                for item in tuple(armor_transaction.owned):
                    transaction.track(item)
        body_sources = {obj: name for name, obj in leg_references.items()}
        if legs_changed:
            from .human_layer_resolution import fingerprint

            for obj in tuple(transaction.owned):
                if not isinstance(obj, bpy.types.Object) or obj.type != "MESH" or obj in body_sources:
                    continue
                name = _name(obj)
                while name not in leg_references:
                    prefix, separator, suffix = name.rpartition(".")
                    if not separator or not suffix.isdigit():
                        break
                    name = prefix
                if name in leg_references:
                    if fingerprint(obj) != fingerprint(leg_references[name]):
                        raise ValueError("追加盔甲引用的身体腿资源不匹配，未替换")
                    obj[SOURCE_NAME] = name
                    body_sources[obj] = name
        incoming = set()
        armor_records = {}
        destinations = {}
        if arms_changed:
            label = tuple(ARMS.values())[values["arm_mode"]]
            destination = _collection(root, label)
            for obj in _collection(staged, label).all_objects:
                incoming.add(obj)
                destinations[obj] = destination
        if legs_changed:
            label = tuple(LEGS.values())[values["leg_mode"]]
            destination = _collection(root, label)
            for obj in _collection(staged, label).all_objects:
                incoming.add(obj)
                destinations[obj] = destination
        if legs_changed and armor_staged is not None:
            wanted = {part_of(obj) for obj in armor_legs}
            for obj in armor_staged.all_objects:
                if part_of(obj) in wanted and current_variant(obj, values["leg_mode"]):
                    incoming.add(obj)
                    destinations[obj] = root
        wing_snapshot = _capture_wings(back_source_rig, wanted_bones) if wings_changed and back_mode else None
        new_back_rig = None
        if wing_snapshot:
            assert back_staged is not None
            for obj in back_staged.all_objects:
                if (is242 and obj != back_source_rig) or (not is242 and _name(obj) == back.MODE_OBJECT[back_mode]):
                    incoming.add(obj)
                    destinations[obj] = root
            if not any(_name(o) == back.MODE_OBJECT[back_mode] for o in incoming):
                raise ValueError("人模源资源缺少背饰网格")
        from ..outline.outline_native import MARK as NATIVE_OUTLINE_MARK

        # A private shell may copy an authored shader's alpha network, but it
        # never replaces that shader when newly appended body parts are bound.
        current = {(type(i), _name(i)): i for i in owned_ids(root)
                   if i not in outgoing and not i.get(NATIVE_OUTLINE_MARK)}
        def identity_name(name):
            prefix, separator, suffix = name.rpartition(".")
            return prefix if separator and suffix.isdigit() else name
        def replacement(item):
            if is242 and item in back_source_ids:
                return None
            if item == source_rig:
                return rig
            if item in incoming or isinstance(item, (bpy.types.Mesh, bpy.types.Armature, bpy.types.Collection)):
                return None
            if is242 and isinstance(item, bpy.types.Material):
                from .human_source import is_skin_material

                if is_skin_material(item):
                    matches = [value for value in current.values() if isinstance(value, bpy.types.Material) and is_skin_material(value)]
                    if len(matches) != 1:
                        raise ValueError("2.4.2实例的共享皮肤材质不唯一，未替换部件")
                    return matches[0]
            exact = current.get((type(item), _name(item)))
            if exact is not None:
                return exact
            matches = [value for (kind,name),value in current.items() if kind == type(item) and identity_name(name) == identity_name(_name(item))]
            return matches[0] if len(matches) == 1 else None
        mapping = {item: other for item in tuple(transaction.owned) if (other := replacement(item)) is not None and other != item}
        # The shared incoming-only rebind retains authored parent inverses.
        # Assigning a new parent pointer otherwise clears Blender's bind frame.
        from .human_armor import rebind_incoming

        if is242 and back_transaction is not None:
            # Keep source rest bones available for the local snapshot; assigning
            # its data pointer would replace that source with the public body.
            rebind_incoming((item for item in transaction.owned if item not in {back_source_rig, back_source_rig.data}), mapping)
        else:
            rebind_incoming(transaction.owned, mapping)
        if wings_changed and not is242:
            back_references = {_name(obj): obj for obj in staged.all_objects if _name(obj) in back.MODE_OBJECT.values()}
            if accessory is None and old_back_bones:
                back.validate_legacy(root, rig, source_rig, old_back_bones, transaction)
            for obj in back_objects:
                # Compare binding to this instance's actual previous rig.
                reference = back_references.get(_name(obj))
                if reference:
                    for modifier in reference.modifiers:
                        if modifier.type == "ARMATURE":
                            cast(Any, modifier).object = back_rig
                back.validate_source(root, obj, back_references.get(_name(obj)))
        if legs_changed:
            _validate_body_legs(root, (*raw_legs, *body_legs), leg_references, leg_graphs, prepare, rig, skin,
                                {obj: source_lattices[_name(obj)] for obj in leg_lattices}, raw_members=raw_legs)
            helpers = {target for obj in tuple(incoming) for target in
                       (obj.parent, *(getattr(modifier, "object", None) for modifier in obj.modifiers))
                       if target is not None and target.type == "LATTICE" and target in transaction.owned}
            for obj in helpers:
                incoming.add(obj)
                destinations[obj] = root
        if legs_changed and armor_staged is not None:
            validate_parts(root, armor_legs, {_name(obj): obj for obj in armor_staged.all_objects if part_of(obj)})
        if legs_changed:
            active = {_name(obj).replace("-官方", ""): obj for obj in incoming if obj in leg_references.values()}
            # Authored armor envelopes read the body layer through Object
            # sockets. Point those proven source consumers at the selected
            # body, rather than keeping an unlinked inactive leg as a target.
            body_mapping = {obj: active[name.replace("-官方", "")] for obj, name in body_sources.items()
                            if obj not in incoming}
            rebind_incoming(transaction.owned, body_mapping)
            if values["leg_mode"] == 0:
                selected_lattices = {_name(target): target for obj in tuple(incoming) if obj in leg_references.values() for target in
                                     (obj.parent, *(getattr(modifier, "object", None) for modifier in obj.modifiers))
                                     if target is not None and target.type == "LATTICE" and _name(target) in source_lattices}
                lattice_mapping = {}
                for obj in tuple(transaction.owned):
                    if not isinstance(obj, bpy.types.Object) or obj.type != "LATTICE" or obj in selected_lattices.values():
                        continue
                    name = _name(obj)
                    while name not in selected_lattices:
                        prefix, separator, suffix = name.rpartition(".")
                        if not separator or not suffix.isdigit():
                            break
                        name = prefix
                    if name in selected_lattices:
                        lattice_mapping[obj] = selected_lattices[name]
                rebind_incoming(transaction.owned, lattice_mapping)
                for obj in lattice_mapping:
                    incoming.discard(obj)
                    destinations.pop(obj, None)
        if legs_changed:
            for obj in incoming:
                if part_of(obj) in {"legs", "feet"}:
                    obj[SOURCE_NAME] = source_identity(obj)
                    record = armor_materials.get(_name(obj).replace("-官方腿", ""))
                    if record is not None:
                        _restore_materials(obj, record)
                    armor_records[obj] = record
                    initialize_geometry(obj)
        if skin:
            for obj in incoming:
                for modifier in obj.modifiers:
                    if modifier.type == "NODES" and modifier.node_group and modifier.node_group.get("bp_second_layer_geometry"):
                        for socket in modifier.node_group.interface.items_tree:
                            if getattr(socket, "in_out", "") == "INPUT" and socket.name == "Image":
                                set_input(modifier, socket.identifier, skin)
        if wing_snapshot:
            selected_mesh = next(obj for obj in incoming if _name(obj) == back.MODE_OBJECT[back_mode])
            new_back_rig = back.template(transaction, root, rig, back_source_rig, back_mode, selected_mesh)
            if is242:
                wing_snapshot = human_back242.capture(back_source_rig, rig, wanted_bones)
                new_back_rig = rig
            else:
                incoming.add(new_back_rig)
                destinations[new_back_rig] = root
                wing_snapshot = _capture_wings(new_back_rig, wanted_bones)
            for spec in wing_snapshot[0]:
                if spec["shape"] is not None and spec["shape"] in transaction.owned:
                    incoming.add(spec["shape"])
                    destinations[spec["shape"]] = root
        if wing_snapshot:
            for obj in incoming:
                if _name(obj) == back.MODE_OBJECT[back_mode]:
                    if obj.data not in transaction.owned:
                        raise ValueError("背饰源网格不是本次独立追加，未修改材质槽")
                    material_state = (back_material_memory.get("cape") if back_mode in {2, 3} else None) or back_material_memory.get(_name(obj))
                    if material_state is not None:
                        require_owned(root, (*material_state["materials"].values(), *material_state["backups"].values()))
                        _restore_materials(obj, material_state)
        kept = set(incoming)
        references = bpy.data.user_map(subset=tuple(transaction.owned))
        while True:
            linked = {item for item, users in references.items() if any(user in kept for user in users)}
            if linked <= kept:
                break
            kept.update(linked)
        transaction.remove(transaction.owned - kept)
        features = stack.enter_context(Creation())
        features.owner = root
        refresh_limits(features, controller, root)
        added_bones = False
        try:
            if is242 and wings_changed:
                added_bones = True
                # The retiring consumers remain linked until outline validation
                # completes, but no longer evaluate a just-removed bone parent.
                for obj in retiring_bone_parents:
                    obj.parent_type = "OBJECT"
                if accessory is None and old_back_bones:
                    _drop_wings(rig, old_back_bones)
                if wing_snapshot:
                    human_back242.install(features, root, rig, wing_snapshot)
                    # Native driver RNA can cache an unresolved bone path.
                    # Bind after the new main-rig paths actually exist, then
                    # retire the temporary source before publishing the parts.
                    rebind_incoming(incoming | {obj.data for obj in incoming if obj.data is not None},
                                    {back_source_rig: rig, back_source_rig.data: rig.data})
                    transaction.remove((back_source_rig, back_source_rig.data))
            for obj in incoming:
                for coll in tuple(obj.users_collection):
                    coll.objects.unlink(obj)
                destinations[obj].objects.link(obj)
                obj[OWNER], obj[SOURCE_NAME] = root[INSTANCE], _name(obj)
                obj.hide_set(False)
                obj.hide_viewport = False
                obj.hide_render = False
            for name, value in values.items():
                controller[key(name)] = value
            if wings_changed:
                added_bones = is242 or accessory is None and bool(old_back_bones)
                if not is242 and accessory is None and old_back_bones:
                    _drop_wings(rig, old_back_bones)
                memory = json.loads(root.get(back.POSE_MEMORY, root.get(POSE_MEMORY, "{}")))
                if wing_backup:
                    memory.update({spec["name"]: {"rotation_mode": spec["rotation_mode"], "matrix": [list(row) for row in spec["basis"]]}
                                   for spec in wing_backup[0]})
                for name, transform in memory.items():
                    pose = cast(Any, new_back_rig).pose.bones.get(name) if new_back_rig else None
                    if pose:
                        pose.rotation_mode = transform["rotation_mode"]
                        pose.matrix_basis = Matrix(transform["matrix"])
                root[back.POSE_MEMORY] = json.dumps(memory)
                root[back.MATERIAL_MEMORY] = back_material_memory
                root[MAIN_RIG] = rig
                if new_back_rig is not None and not is242:
                    root[back.RIG] = new_back_rig
                elif back.RIG in root:
                    del root[back.RIG]
                if is242:
                    root[human_back242.MEMBERS] = {str(index): obj for index, obj in enumerate(incoming)
                                                  if obj in back_source_objects}
            prepare(root, rig, objects=incoming, material_ids={item for item in transaction.owned if isinstance(item, bpy.types.Material)})
            if wing_snapshot:
                # Only the selected, freshly appended back closure supplies
                # widgets. The public body may reference arbitrary user shapes.
                shapes = {spec["shape"] for spec in wing_snapshot[0] if spec["shape"] is not None}
                # Legacy sources can intentionally reuse a proven body widget.
                # Keep that exact binding without changing its visibility.
                require_owned(root, shapes - transaction.owned)
                for shape in shapes & transaction.owned:
                    hidden, rendered = shape.hide_get(), shape.hide_render
                    features.on_rollback(lambda shape=shape, hidden=hidden, rendered=rendered:
                                         (shape.hide_set(hidden), setattr(shape, "hide_render", rendered)))
                    shape.hide_set(True)
                    shape.hide_render = True
            from .human_layer_resolution import prepare as prepare_layer_resolution
            if skin is not None:
                for obj in incoming:
                    for modifier in obj.modifiers:
                        if modifier.type != "NODES" or not modifier.node_group or not modifier.node_group.get("bp_second_layer_geometry"):
                            continue
                        for socket in modifier.node_group.interface.items_tree:
                            if getattr(socket, "in_out", "") == "INPUT" and socket.name == "Image":
                                set_input(modifier, socket.identifier, skin)
                prepare_layer_resolution(features, root, incoming, skin, fresh_ids=transaction.owned)
            seal_owned(root, transaction.owned)
            if wings_changed:
                back.prepare(features, root, rig, journal=False)
                if new_back_rig is not None and not is242:
                    new_back_rig[back.CONTRACT] = back.contract(root, new_back_rig)
            from .human_outline import refresh as refresh_outline

            refresh_outline(features, root, incoming)
            for obj, record in armor_records.items():
                restore_features(features, root, obj, record)
            features.seal(root)
            if wings_changed and is242:
                root[back.CONTRACT] = back.contract(root, rig)
                human_back242.focus_controls(features, root, rig, back_mode)
            assert features.registry_before is not None
            features.registry_before[root] = old_registry
            # Every fallible layout, image handoff and final seal runs with
            # the actual outgoing IDs still alive. Nothing is committed early.
            sources = tuple(item for item in (transaction, armor_transaction, back_transaction) if item is not None)
            for source in sources:
                source.prepare_commit(image_paths=features.image_paths)
            features.prepare_commit()
            if not features._commit_prepared or any(not source._commit_prepared for source in sources):
                raise RuntimeError("部件事务未完成准备，未退役旧部件")
            finishers = tuple(source.finish_commit for source in (*sources, features))
            retired = live_ids(outgoing)
            if len(retired) != len(outgoing):
                raise ValueError("待退役部件引用在准备期间失效，未继续删除")
            for obj in outgoing:
                for coll in tuple(obj.users_collection):
                    coll.objects.unlink(obj)
            _forget(root, outgoing)
            bpy.data.batch_remove(ids=retired)
            # The irreversible native retirement is followed only by Python
            # flags. No RNA access, validation, layout or IO occurs here.
            for finish in finishers:
                finish()
        except Exception:
            for name, value in previous.items():
                controller[key(name)] = value
            if added_bones:
                _drop_wings(rig, tuple(name for name in all_back_bones if rig.data.bones.get(name)))
            if wing_backup and added_bones and accessory is None:
                _restore_wings(rig, wing_backup, rig, collection_bindings=original_bone_collections)
            for obj, (parent_type, parent_bone, basis, inverse) in retiring_bone_parents.items():
                obj.parent_type, obj.parent_bone = parent_type, parent_bone
                obj.matrix_basis, obj.matrix_parent_inverse = basis, inverse
            if memory_existed:
                root[back.POSE_MEMORY] = old_memory
            elif back.POSE_MEMORY in root:
                del root[back.POSE_MEMORY]
            if material_memory_existed:
                root[back.MATERIAL_MEMORY] = old_material_memory
            elif back.MATERIAL_MEMORY in root:
                del root[back.MATERIAL_MEMORY]
            if back_schema_existed:
                root[back.SCHEMA] = old_back_schema
            elif back.SCHEMA in root:
                del root[back.SCHEMA]
            for name, existed, value in ((back.RIG, back_reference_existed, old_back_reference),
                                         (MAIN_RIG, main_reference_existed, old_main_reference)):
                if existed:
                    root[name] = value
                elif name in root:
                    del root[name]
            if is242:
                root[human_back242.MEMBERS] = old_back_members
                if contract_existed:
                    root[back.CONTRACT] = old_contract
                elif back.CONTRACT in root:
                    del root[back.CONTRACT]
            root[REGISTRY] = old_registry
            for obj, collections in old_links.items():
                for collection in collections:
                    if collection.objects.get(obj.name) != obj:
                        collection.objects.link(obj)
            controller.update_tag()
            rig.data.update_tag()
            rig.update_tag()
            raise
        features.cleanup()
    rig.update_tag()
    return True
