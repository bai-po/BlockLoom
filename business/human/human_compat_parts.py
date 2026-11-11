"""Explicit recovery of missing, version-matched legacy native parts.

oxal.human.legacy_parts / 1; NEW_INSTANCE for missing parts only; selected
native rig scope. COMPAT_PARTS.json records the offline immutable source,
native bindings and rest matrices. No model adoption or old callbacks.
"""

import hashlib
import json
from typing import Any

import bpy
from mathutils import Matrix

from ...core.runtime import ensure_mutation_allowed
from ...util.geometry_nodes import set_input
from ...util.resources import AppendTransaction, live_ids
from .human_source import DATA, SKIN_INPUT
from .human_state import ARMS, LEGS, SOURCE_NAME

_catalog: dict[str, Any] | None = None


def prepare_catalog():
    global _catalog
    document = json.loads((DATA / "COMPAT_PARTS.json").read_text(encoding="utf8"))
    if document.get("schema") != 1 or document.get("resource") != "oxal_compat_parts.blend":
        raise ValueError("旧人模部件来源声明无效")
    _catalog = document


def clear_catalog():
    global _catalog
    _catalog = None


def available(version, kind, value):
    """Read-only capability query; registration owns all metadata IO."""
    if _catalog is None or version not in _catalog["versions"]:
        return False
    groups = _catalog["versions"][version]["groups"]
    if kind == "arm_mode":
        return value in ARMS and "ARM_" + value in groups
    if kind == "leg_mode":
        return value in LEGS and "LEG_" + value in groups
    return (kind == "armor" and isinstance(value, (tuple, list)) and bool(value)
            and set(value) <= {"head", "chest", "legs", "feet"}
            and all(all("ARMOR_" + part.upper() + suffix in groups for suffix in
                        ("_NORMAL", "_OFFICIAL") if part in {"legs", "feet"})
                    and (part in {"legs", "feet"} or "ARMOR_" + part.upper() in groups)
                    for part in value))


def _local(item):
    return item.library is None and item.override_library is None


def _role(item, choices):
    name = str(item.get(SOURCE_NAME, item.name))
    while name not in choices:
        prefix, dot, number = name.rpartition(".")
        if not dot or not number.isdigit():
            return None
        name = prefix
    return name


def _close(first, second):
    return (len(first) == len(second)
            and all(len(a) == len(b) and all(abs(float(x) - float(y)) <= 2e-5 for x, y in zip(a, b, strict=True))
                    for a, b in zip(first, second, strict=True)))


def _rest(rig, profile, entries):
    names = {name for record in entries.values() for name in record["bone_names"]}
    names.update(record["parent_bone"] for record in entries.values() if record["parent_bone"])
    todo = list(names)
    while todo:
        name = todo.pop()
        record = profile["rig_rest"].get(name)
        if record is None:
            raise ValueError("旧部件含未知骨骼角色，未补齐")
        if record["parent"] and record["parent"] not in names:
            names.add(record["parent"])
            todo.append(record["parent"])
    for name in names:
        bone, expected = rig.data.bones.get(name), profile["rig_rest"][name]
        if (bone is None or (bone.parent.name if bone.parent else "") != expected["parent"]
                or not _close(bone.matrix_local, expected["matrix"])):
            raise ValueError("旧人模休止骨架与该版本部件不匹配，未补齐：" + name)


def _dependency(obj, record, rig, members):
    if (obj.type != record["type"] or not _local(obj) or not _local(obj.data)
            or len(obj.users_scene) != 1 or obj not in members):
        raise ValueError("旧部件依赖被外部共享或来源不明确，未补齐：" + obj.name)
    if record["parent"] == "@RIG" and obj.parent != rig:
        raise ValueError("旧部件依赖未绑定当前实际骨架，未补齐：" + obj.name)
    if obj.parent_type != record["parent_type"] or obj.parent_bone != record["parent_bone"]:
        raise ValueError("旧部件依赖的原生父级绑定已改变，未补齐：" + obj.name)
    expected = record.get("lattice")
    if expected and ([obj.data.points_u, obj.data.points_v, obj.data.points_w] != expected["dimensions"]
                     or not _close([point.co_deform for point in obj.data.points], expected["points"])):
        raise ValueError("旧人模晶格休止数据已有用户修改，未补齐：" + obj.name)
    for pointer in record["pointers"]:
        if pointer["role"] != "@RIG":
            continue
        entries = obj.modifiers if pointer["category"] == "MODIFIER" else obj.constraints
        item = entries.get(pointer["name"])
        if item is None or getattr(item, pointer["field"]) != rig:
            raise ValueError("旧部件依赖的原生修改器绑定已改变，未补齐：" + obj.name)


def _skin(target):
    bodies = [obj for obj in target.members if obj.type == "MESH" and _role(obj, {"身体"}) == "身体"]
    materials = {slot.material for obj in bodies for slot in obj.material_slots if slot.material
                 and slot.material.node_tree and any(node.type == "TEX_IMAGE" and node.image
                                                     and (node.name == "图像纹理" or node.get(SKIN_INPUT) == 1)
                                                     for node in slot.material.node_tree.nodes)}
    if len(materials) != 1:
        raise ValueError("当前人模主皮肤材质不唯一，未用模板皮肤覆盖")
    return materials.pop()


def _skin_image(material):
    nodes = [node for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image]
    marked = [node for node in nodes if node.get(SKIN_INPUT) == 1 or node.name == "图像纹理"]
    images = {node.image for node in marked}
    if len(images) != 1:
        raise ValueError("当前主皮肤图像通道不唯一，未替换原生二层图像")
    return images.pop()


def _container(target):
    candidates = [collection for collection in target.containers if _local(collection)
                  and {obj for obj in collection.all_objects if obj.type == "ARMATURE"} == {target.rig}]
    if len(candidates) != 1:
        raise ValueError("旧人模没有唯一私有实例合集，未补齐")
    return candidates[0]


def _destination(transaction, container, name, target, fresh):
    existing = [collection for collection in container.children_recursive if _role(collection, {name}) == name]
    if len(existing) > 1:
        raise ValueError("旧人模目标合集角色不唯一，未补齐：" + name)
    if existing:
        collection = existing[0]
        permitted = {*target.members, *transaction.ids, *fresh}
        if not _local(collection) or any(obj not in permitted for obj in collection.all_objects):
            raise ValueError("旧人模目标合集含未知外部对象，未补齐：" + name)
        return collection
    collection = transaction.track(bpy.data.collections.new(name))
    container.children.link(collection)
    return collection


def _bind(obj, record, mapping):
    obj.parent = mapping.get(record["parent"])
    obj.parent_type, obj.parent_bone = record["parent_type"], record["parent_bone"]
    obj.matrix_parent_inverse = Matrix(record["parent_inverse"])
    obj.matrix_basis = Matrix(record["basis"])
    for pointer in record["pointers"]:
        items = obj.modifiers if pointer["category"] == "MODIFIER" else obj.constraints
        item = items.get(pointer["name"])
        if item is None:
            raise ValueError("内置旧部件修改器契约损坏")
        setattr(item, pointer["field"], mapping[pointer["role"]])
    for driver in record["drivers"]:
        owner = obj if driver["owner"] == "OBJECT" else obj.data if driver["owner"] == "DATA" else obj.data.shape_keys
        animation = owner.animation_data if owner is not None else None
        curves = [curve for curve in animation.drivers if curve.data_path == driver["path"]
                  and curve.array_index == driver["index"]] if animation else []
        if len(curves) != 1:
            raise ValueError("内置旧部件驱动契约损坏")
        curves[0].driver.variables[driver["variable"]].targets[driver["target"]].id = mapping[driver["role"]]


def _keys(target, kind, value):
    if kind == "arm_mode":
        return ("ARM_" + value,)
    if kind == "leg_mode":
        return ("LEG_" + value,)
    from .human_compat import _style

    mode = _style(target, LEGS)
    if set(value) & {"legs", "feet"} and mode not in LEGS:
        raise ValueError("请先选择常规或官方腿型，再补齐下肢盔甲")
    return tuple("ARMOR_" + part.upper() + ("_" + mode if part in {"legs", "feet"} else "") for part in value)


def ensure(transaction, scene, target, kind, value):
    """Append only missing native roles and hand exact fresh IDs to Creation.

    No existing object is retired, adopted or rebound. The outer explicit
    command owns final commit and removal of every fresh ID on failure.
    """
    ensure_mutation_allowed()
    if transaction.owner is not None or not available(target.version, kind, value):
        raise ValueError("该精确旧版本没有可用的部件恢复资源")
    if not target.editable or scene.objects.get(target.rig.name) != target.rig:
        raise ValueError("旧人模目标已改变或不可编辑")
    assert _catalog is not None
    profile = _catalog["versions"][target.version]
    groups = [profile["groups"][key] for key in _keys(target, kind, value)]
    roles = {role for group in profile["groups"].values() for role in group["entries"]}
    current: dict[str, list[Any]] = {}
    for obj in target.members:
        if (role := _role(obj, roles)) is not None:
            current.setdefault(role, []).append(obj)
    requested = {role for group in groups for role in group["objects"]}
    if any(len(current.get(role, ())) > 1 for role in requested):
        raise ValueError("旧人模部件角色重复，未选择或覆盖用户对象")
    missing = requested - current.keys()
    if not missing:
        return ()
    source = DATA / _catalog["resource"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != _catalog["resource_sha256"]:
        raise ValueError("旧部件资源哈希与声明不符，未补齐")
    container = _container(target)
    skin = _skin(target) if kind != "armor" else None
    all_new = []
    for group in groups:
        wanted = set(group["objects"]) & missing
        if not wanted:
            continue
        needed = set(group["dependencies"])
        records = {role: group["entries"][role] for role in wanted | needed}
        _rest(target.rig, profile, records)
        mapping = {"@RIG": target.rig, "@RIG_DATA": target.rig.data}
        for role in needed:
            candidates = current.get(role, ())
            if len(candidates) > 1:
                raise ValueError("旧人模依赖角色重复，未按名称重绑：" + role)
            if candidates:
                _dependency(candidates[0], records[role], target.rig, (*target.members, *all_new))
                mapping[role] = candidates[0]
        with AppendTransaction(source) as appended:
            staging = appended.append_collection(group["collection"])
            originals = {str(obj.get(SOURCE_NAME, "")): obj for obj in staging.all_objects}
            if set(originals) != set(group["entries"]):
                raise ValueError("旧部件源合集的实际对象角色不匹配")
            fresh_roles = wanted | (needed - mapping.keys())
            fresh = tuple(originals[role] for role in sorted(fresh_roles))
            mapping.update({role: originals[role] for role in fresh_roles})
            for role in fresh_roles:
                obj = originals[role]
                if obj not in appended.owned or obj.type != records[role]["type"]:
                    raise ValueError("旧部件追加来源不完整")
                _bind(obj, records[role], mapping)
                for index in records[role]["skin_slots"]:
                    if skin is None or index >= len(obj.material_slots):
                        raise ValueError("旧部件皮肤槽契约损坏")
                    obj.material_slots[index].material = skin
                for image_input in records[role].get("skin_inputs", ()):
                    if skin is None:
                        raise ValueError("旧二层图像通道缺少当前主皮肤")
                    modifier = obj.modifiers.get(image_input["modifier"])
                    if modifier is None or modifier.type != "NODES":
                        raise ValueError("旧二层原生图像修改器契约损坏")
                    set_input(modifier, image_input["socket"], _skin_image(skin))
                obj.name = role
                for collection in tuple(obj.users_collection):
                    collection.objects.unlink(obj)
                destination = _destination(transaction, container, group["destination"] if role in wanted else "兼容原生依赖", target, fresh)
                destination.objects.link(obj)
            retained: set[Any] = set(fresh)
            users = bpy.data.user_map(subset=tuple(appended.owned))
            while True:
                dependencies = {item for item, consumers in users.items() if any(consumer in retained for consumer in consumers)}
                if dependencies <= retained:
                    break
                retained.update(dependencies)
            appended.remove(set(appended.owned) - retained)
            appended.prepare_commit(image_paths=transaction.image_paths)
            for item in live_ids(appended.owned):
                transaction.track(item)
            appended.finish_commit()
            all_new.extend(fresh)
            for role in fresh_roles:
                current[role] = [originals[role]]
    return tuple(all_new)
