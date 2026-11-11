"""2.4.2 native accessory closures, selected explicitly by the plugin.

asset_id: oxal.human.back; asset_version: 2; policy: NEW_INSTANCE;
scope: one precisely owned human; source: oxal_back_parts_2_4_2.blend.
Only the requested collection is appended. Playback has no Python dependency.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import bpy

from ...util import drivers
from ...util.ownership import require_no_external_users, require_owned
from .human_state import SOURCE_NAME

CAPE_BONES = ("披风·根", "披风·单段", "披风·单段中点", "披风·单段目标", "MCH·披风·单段中点·首尾跟随")
HELPERS = ("披风·单段 · 原生平滑路径", "披风 · 首尾跟随计算（内部）")
MEMBERS = "oxal_human_back_members"
MAIN_SCHEMA = 5
NATIVE_COLLECTIONS = "oxal_human_back_bone_collections"
COLLECTION_UID = "oxal_human_back_collection_uid"
SOLO_STATE = "oxal_human_back_solo_state"
SOLO_UID = "oxal_human_back_solo_uid"
SOURCE = Path(__file__).resolve().parents[2] / "asset/human/oxal_back_parts_2_4_2.blend"
COLLECTIONS = {1: "OXAL_BACK_2_4_2_ELYTRA", 2: "OXAL_BACK_2_4_2_CAPE"}


def current(root):
    from .human_source import model_version

    return model_version(root) in {"2.4.2", "2.4.3", "2.4.4", "2.4.5", "2.4.6", "2.4.7"}


def members(root):
    """Actual IDs bound at publication, never a scan of user data by name."""
    record = root.get(MEMBERS, {})
    if not hasattr(record, "values"):
        raise ValueError("背饰依赖登记损坏")
    objects = tuple(record.values())
    if any(not isinstance(obj, bpy.types.Object) or obj not in tuple(root.all_objects) for obj in objects):
        raise ValueError("背饰依赖引用损坏，未接管其它数据")
    require_owned(root, objects)
    return objects


def prune_initial(root, rig, transaction):
    """Retire source-proven back dependencies before the body is published."""
    from . import human_back as back
    from .human_parts import _back_widgets, _bone_path, _drop_wings, _name

    schema = json.loads(rig.get("Human241_CapeControls", "{}"))
    if schema.get("schema") != 3 or not schema.get("single_only"):
        raise ValueError("2.4.2披风原生来源契约不匹配")
    names = (*back.MODE_BONES, *CAPE_BONES, *back.WING_BONES)
    unwanted = {obj for obj in tuple(root.all_objects) if _name(obj) in {*back.MODE_OBJECT.values(), *HELPERS}}
    unwanted.update(_back_widgets(rig, names))
    if not unwanted <= transaction.owned:
        raise ValueError("背饰初始清理缺少追加来源证明")
    for owner in (rig, rig.data):
        if owner.animation_data:
            for curve in tuple(owner.animation_data.drivers):
                if (_bone_path(curve.data_path, names) or curve.data_path == '["披风模式"]'
                        or any(target.id == rig and (_bone_path(target.data_path, names) or target.bone_target in names)
                               for variable in curve.driver.variables for target in variable.targets)):
                    owner.animation_data.drivers.remove(curve)
        for field in ("Human241_CapeControls", "披风模式", "披风绑定说明", "机制说明", "参考工程"):
            if field in owner:
                del owner[field]
    transaction.remove(unwanted)
    _drop_wings(rig, names)
    for collection in tuple(rig.data.collections_all):
        if not collection.bones and ("披风" in collection.name or "鞘翅" in collection.name or "背饰" in collection.name):
            rig.data.collections.remove(collection)
    root[MEMBERS] = {}


def template(transaction, root, body_rig, rig, mode, mesh):
    """Validate the immutable closure before copying into the actual main rig."""
    from . import human_back as back

    if set(rig.data.bones.keys()) != {"胸口", *back.bones(mode, root)}:
        raise ValueError("2.4.2背饰来源骨骼闭包不匹配")
    if not {rig, rig.data, mesh, mesh.data} <= transaction.owned:
        raise ValueError("2.4.2背饰缺少独立追加来源证明")
    if "胸口" not in body_rig.data.bones:
        raise ValueError("人模主骨架缺少胸口，未添加背饰")
    return rig


def collection_bindings(root, rig):
    """Resolve only this exact Armature's recorded native collection members.

    BoneCollection is an embedded RNA struct, not an ID-property ID pointer.
    Keep its enclosing actual Armature ID and a unique local token, then bind
    the resolved RNA references directly during this operation. Names never
    select a collection or prove that a pre-existing collection is ours.
    """
    record = root.get(NATIVE_COLLECTIONS)
    if record is None:
        return {}
    if (not hasattr(record, "get") or record.get("schema") != 1
            or record.get("armature") != rig.data or not hasattr(record.get("groups"), "items")):
        raise ValueError("背饰骨骼集合的实际主Armature登记已变化，未接管")
    from .human_back import all_bones

    groups = {}
    candidates = tuple(rig.data.collections_all)
    for token, entry in record["groups"].items():
        if (not isinstance(token, str) or len(token) != 32 or not hasattr(entry, "get")
                or not isinstance(entry.get("source"), str) or entry["source"] in groups):
            raise ValueError("背饰骨骼集合登记损坏，未按名称恢复")
        matching = [collection for collection in candidates if collection.get(COLLECTION_UID) == token]
        if len(matching) != 1:
            raise ValueError("背饰骨骼集合被删除或复制，未按名称接管")
        collection = matching[0]
        if collection.parent is not None or any(bone.name not in all_bones(root) for bone in collection.bones):
            raise ValueError("背饰专属骨骼集合含用户骨骼或父组，未覆盖")
        groups[entry["source"]] = collection
    return groups


def install(transaction, root, rig, snapshot):
    """Install into exclusive groups, never a user's similarly named group."""
    from .human_armor import _plain
    from .human_parts import _restore_wings

    bindings = collection_bindings(root, rig)
    existed = NATIVE_COLLECTIONS in root
    previous = _plain(root.get(NATIVE_COLLECTIONS, {}))
    entries = _plain(root.get(NATIVE_COLLECTIONS, {}).get("groups", {}))
    created = []

    def restore():
        for collection in reversed(created):
            if collection in tuple(rig.data.collections_all):
                if collection.bones:
                    raise ValueError("回滚背饰集合仍有骨骼引用，未删除")
                rig.data.collections.remove(collection)
        if existed:
            root[NATIVE_COLLECTIONS] = previous
        elif NATIVE_COLLECTIONS in root:
            del root[NATIVE_COLLECTIONS]

    transaction.on_rollback(restore)
    for spec in snapshot[0]:
        for name in spec["collections"]:
            if name not in bindings:
                collection = rig.data.collections.new("背饰 · " + name)
                created.append(collection)
                token = uuid4().hex
                collection[COLLECTION_UID] = token
                collection.is_visible = True
                collection.is_solo = False
                bindings[name] = collection
                entries[token] = {"source": name}
    _restore_wings(rig, snapshot, rig, collection_bindings=bindings)
    root[NATIVE_COLLECTIONS] = {"schema": 1, "armature": rig.data, "groups": entries}


def capture(source, rig, names) -> Any:
    """Detach the temporary source rig from every copied native reference."""
    from .human_parts import _capture_wings

    def remap(value):
        if isinstance(value, bpy.types.ID):
            return rig if value == source else rig.data if value == source.data else value
        if isinstance(value, dict):
            return {key: remap(part) for key, part in value.items()}
        if isinstance(value, (list, tuple)):
            return [remap(part) for part in value]
        return value

    return remap(_capture_wings(source, names))


def contract(root, rig):
    """Proof of the accessory subgraph, independent of body Action/NLA edits."""
    from ...util.ownership import DATA_UID
    from . import human_back as back
    from .human_parts import _bone_path, _capture_wings

    names = tuple(name for name in back.all_bones(root) if name in rig.data.bones)
    snapshot = _capture_wings(rig, names)[0]
    for spec in snapshot:
        spec.pop("basis")
        spec.pop("rotation_mode")
        # These display fields were omitted by released Betas. Keep their old
        # structural seals readable; repair verifies them against the template.
        spec.pop("shape_options", None)
        spec.pop("shape_transform", None)
        spec["flags"].pop("hide")

    def selected(owner):
        capture = drivers.capture(owner)
        capture["curves"] = [record for record in capture["curves"] if _bone_path(record["path"], names)]
        # A user's body Action can create AnimData without changing back bones.
        capture.pop("animation")
        return capture

    def plain(value):
        if isinstance(value, bpy.types.ID):
            require_owned(root, (value,))
            return (value.id_type, value.get(DATA_UID))
        if isinstance(value, dict):
            return {name: plain(part) for name, part in value.items()
                    if name not in {"select", "select_control_point", "select_left_handle", "select_right_handle"}}
        if isinstance(value, (str, int, bool, type(None))):
            return value
        if isinstance(value, float):
            return round(value, 6)
        return [plain(part) for part in value]

    data = [snapshot, selected(rig), selected(rig.data), contract_data(root, rig)]
    if NATIVE_COLLECTIONS in root:
        # Legacy .18 seals retain their exact bytes until an explicit rebuild.
        data.append(["native_collections", [(name, collection.get(COLLECTION_UID), collection.name)
                                            for name, collection in sorted(collection_bindings(root, rig).items())]])
    return hashlib.sha256(json.dumps(plain(data), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_retirement(root):
    """Native data and Keys can carry user Actions independently of Objects."""
    owners = set(members(root))
    owners.update(obj.data for obj in tuple(owners) if obj.data is not None)
    owners.update(key for item in tuple(owners) if (key := getattr(item, "shape_keys", None)) is not None)
    require_owned(root, owners)
    require_no_external_users(cast(Any, bpy.context.scene), root, owners)
    for owner in owners:
        animation = owner.animation_data
        if animation and (animation.action or animation.nla_tracks):
            raise ValueError("背饰部件或其原生数据已有Action／NLA动画，未删除：" + owner.name)


def finalize_visibility(root, rig):
    """Authored functional controls remain visible without any mode driver."""
    from . import human_back as back
    from .human_parts import _name

    controls = {"鞘翅开合·轨道", "鞘翅展开", "披风·根", "披风·单段", "披风·单段中点", "披风·单段目标"}
    for name in back.all_bones(root):
        bone = rig.data.bones.get(name)
        if bone is not None:
            bone.hide = name not in controls

    for obj in members(root):
        if obj == rig:
            continue
        helper = _name(obj) in HELPERS
        widget = any(pose.custom_shape == obj for pose in rig.pose.bones)
        obj.hide_viewport = False
        obj.hide_render = helper or widget
        obj.hide_set(helper or widget)


def repair_display(transaction, root, rig, mode):
    """Repair the released display omissions without replacing animated bones."""
    from ...util.resources import AppendTransaction
    from . import human_back as back
    from .human_armor import animated
    from .human_parts import _capture_wings

    if not mode:
        return
    require_owned(root, (rig, rig.data))
    require_no_external_users(cast(Any, bpy.context.scene), root, (rig.data,))
    if root.get(back.CONTRACT) != contract(root, rig):
        raise ValueError("背饰结构或驱动已变化，未覆盖控制器显示")
    names = back.bones(mode, root)
    with AppendTransaction(SOURCE) as source:
        collection = source.append_collection(COLLECTIONS[mode])
        candidates = [obj for obj in collection.all_objects if obj.type == "ARMATURE"]
        if len(candidates) != 1 or set(cast(Any, candidates[0].data).bones.keys()) != {"胸口", *names}:
            raise ValueError("背饰控件来源骨架不唯一，未修复")
        expected = _capture_wings(candidates[0], names)[0]
        changes = []
        for spec in expected:
            pose = rig.pose.bones.get(spec["name"])
            if pose is None:
                raise ValueError("背饰控制骨缺失，请明确修复背饰结构")
            transform = pose.custom_shape_transform.name if pose.custom_shape_transform else ""
            if transform != spec["shape_transform"]:
                raise ValueError("背饰控件的变换骨已自定义，未覆盖")
            for field, value in spec["shape_options"].items():
                actual = getattr(pose, field)
                if actual == value:
                    continue
                historical = True if field == "use_custom_shape_bone_size" else 1.0
                data_path = f"pose.bones[{json.dumps(pose.name, ensure_ascii=False)}].{field}"
                if actual != historical or animated(rig, data_path):
                    raise ValueError("背饰控件显示含未知自定义值，未覆盖：" + spec["name"])
                changes.append((pose, field, actual, value))
        transaction.on_rollback(lambda: [setattr(pose, field, old) for pose, field, old, _new in changes])
        for pose, field, _old, value in changes:
            setattr(pose, field, value)


def focus_controls(transaction, root, rig, mode):
    """Expose accessory controls alongside the existing body control domain.

    Tokens on other collections only identify saved UI flags; they never prove
    resource ownership. A user's subsequent solo changes take precedence when
    leaving our focus, instead of being reset to an earlier snapshot.
    """
    from .human_armor import _plain

    require_owned(root, (rig, rig.data))
    require_no_external_users(cast(Any, bpy.context.scene), root, (rig.data,))
    collections = tuple(rig.data.collections_all)
    bindings = collection_bindings(root, rig)
    public = {collection for collection in bindings.values()
              if any(not bone.hide and rig.pose.bones[bone.name].custom_shape is not None
                     for bone in collection.bones)} if mode else set()
    if mode and not public:
        raise ValueError("背饰没有可确认的公开控制器集合，未孤立其它骨骼")
    previous = _plain(root.get(SOLO_STATE))
    if previous is not None and (not isinstance(previous, dict) or previous.get("schema") not in {1, 2}
                                 or previous.get("armature") != rig.data
                                 or any(not isinstance(previous.get(field), (list, tuple))
                                        or any(not isinstance(token, str) or len(token) != 32 for token in previous[field])
                                        for field in ("previous", "active"))):
        raise ValueError("背饰孤立状态引用已变化，未改写用户集合")
    flags = [(collection, collection.is_solo, collection.is_visible) for collection in collections]
    tokens = []

    def restore():
        current = tuple(rig.data.collections_all)
        for collection, solo, visible in flags:
            if collection in current:
                collection.is_solo, collection.is_visible = solo, visible
        for collection in tokens:
            if collection in current and SOLO_UID in collection:
                del collection[SOLO_UID]
        if previous is None:
            if SOLO_STATE in root:
                del root[SOLO_STATE]
        else:
            root[SOLO_STATE] = previous

    transaction.on_rollback(restore)

    def token(collection):
        value = collection.get(SOLO_UID)
        if value is None:
            value = uuid4().hex
            collection[SOLO_UID] = value
            tokens.append(collection)
        if (not isinstance(value, str) or len(value) != 32
                or sum(other.get(SOLO_UID) == value for other in collections) != 1):
            raise ValueError("骨骼集合孤立标识有冲突，未按名称接管")
        return value

    def resolve(values):
        if any(sum(collection.get(SOLO_UID) == value for collection in collections) > 1 for value in values):
            raise ValueError("保存的孤立集合标识有冲突，未改写用户集合")
        return {collection for collection in collections if collection.get(SOLO_UID) in values}

    before = {collection for collection in collections if collection.is_solo}
    active = resolve(previous.get("active", ())) if previous else set()
    unchanged = previous is not None and before == active
    if not mode:
        for collection in active:
            collection.is_solo = False
        if unchanged and previous is not None:
            for collection in resolve(previous.get("previous", ())):
                collection.is_solo = True
        if SOLO_STATE in root:
            del root[SOLO_STATE]
        return
    baseline = list(previous["previous"]) if unchanged and previous is not None else [token(collection) for collection in before - active]
    retained = resolve(baseline)
    if not retained:
        # With no original solo selection, preserve ordinary visible body
        # collections. Ignore solo here: schema 1 itself hid these collections
        # by soloing only the accessory, so effective visibility would perpetuate
        # that released mistake. Respect raw visibility and hidden ancestors.
        def visible(collection):
            while collection is not None:
                if not collection.is_visible:
                    return False
                collection = collection.parent
            return True

        retained = {collection for collection in collections
                    if collection not in bindings.values() and visible(collection)}
    focused = public | retained
    selected = [token(collection) for collection in focused]
    for collection in collections:
        collection.is_solo = collection in focused
    for collection in bindings.values():
        collection.is_visible = collection in public
    root[SOLO_STATE] = {"schema": 2, "armature": rig.data, "previous": baseline, "active": selected}


def _modifiers(root, obj, *, legacy=False):
    from .human_outline import generated_modifiers

    generated = set(generated_modifiers(root, obj))
    records = []
    native = tuple(modifier for modifier in obj.modifiers if modifier not in generated)
    for modifier in native:
        fields = {}
        for prop in modifier.bl_rna.properties:
            if prop.identifier in {"rna_type", "name", "type"} or prop.is_readonly or prop.identifier == "is_active" and not legacy:
                continue
            value = getattr(modifier, prop.identifier)
            if prop.identifier == "is_active" and generated:
                # Released private contracts included this UI-only flag. A
                # generated outline becomes active when appended; project the
                # authored last native modifier to its released active state.
                value = modifier == native[-1]
            if prop.type in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}:
                fields[prop.identifier] = list(value) if getattr(prop, "is_array", False) else value
            elif prop.type == "POINTER" and isinstance(value, bpy.types.ID):
                fields[prop.identifier] = value
        records.append((modifier.type, fields))
    return records


def contract_data(root, rig):
    """Include every private native driver and immutable mesh in retirement proof."""
    from ..outline.outline_native import generated_driver_paths
    from .human_back import is_back_rig
    from .human_layer_resolution import _attribute
    from .human_parts import _name

    records = []
    for obj in sorted(members(root), key=lambda item: str(item.get(SOURCE_NAME, item.name))):
        if obj == rig:
            continue
        data = obj.data
        # Only a fully validated generated outline can be projected out of the
        # native source contract. Its drivers are appended to the same Object.
        generated = generated_driver_paths(root, obj)
        object_drivers = drivers.capture(obj)
        object_drivers["curves"] = [curve for curve in object_drivers["curves"] if curve["path"] not in generated]
        record = {"object": obj, "data": data, "parent": obj.parent, "parent_type": obj.parent_type,
                  "parent_bone": obj.parent_bone, "inverse": [list(row) for row in obj.matrix_parent_inverse],
                  "drivers": object_drivers, "data_drivers": drivers.capture(data) if data else [],
                  "modifiers": _modifiers(root, obj, legacy=is_back_rig(rig))}
        if obj.type == "MESH":
            record["geometry"] = {
                "vertices": [tuple(vertex.co) for vertex in data.vertices],
                "edges": [tuple(edge.vertices) for edge in data.edges],
                "faces": [(tuple(poly.vertices), poly.use_smooth) for poly in data.polygons],
                "uv": {layer.name: [tuple(item.uv) for item in layer.data] for layer in data.uv_layers},
                "groups": [group.name for group in obj.vertex_groups],
                "weights": [[(group.group, group.weight) for group in vertex.groups] for vertex in data.vertices],
                "attributes": {attr.name: _attribute(attr) for attr in data.attributes
                               if attr.name not in {"position", ".edge_verts", ".corner_vert", ".corner_edge", "material_index", *data.uv_layers.keys()}
                               and not attr.name.startswith((".select_", ".hide_", ".uv_select_"))},
                "normals": [tuple(normal.vector) for normal in data.corner_normals] if data.has_custom_normals else [],
            }
        elif obj.type == "CURVE":
            # Evaluated coordinates/handles/tilt are outputs of native drivers.
            record["splines"] = [(spline.type, len(spline.bezier_points), spline.use_cyclic_u, spline.resolution_u)
                                 for spline in data.splines]
        elif _name(obj) == HELPERS[1]:
            record["helper_fields"] = sorted(obj.keys())
        records.append(record)
    return records
