"""Explicit native outlines for one precisely owned human body and back item.

asset_id: oxal.human.outline; asset_version: 1; policy: NEW_INSTANCE;
scope: one human instance; source: the supplied reference's actual Solidify
modifier and Material, with a native shell-normal adapter where required.
Armor retains its independent enchantment outline. Skin shaders, mesh data,
Actions and NLA are never replaced by this feature.
"""

import math
from typing import Any, cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import (
    explicit_scene_ids,
    owned_ids,
    require_no_external_users,
    require_owned,
)
from ...util.resources import live_ids
from .human_state import PARAMETERS as HUMAN_PARAMETERS
from .human_state import key

MARK = "oxal_mc_studio_human_outline"
ROLE = "human"
PARAMETERS = {name: HUMAN_PARAMETERS[name]
              for name in ("outline_enabled", "outline_color", "outline_width", "outline_quality_normals")}

# These exact primary-head roles include the 2.4.2 native eight-variant Mesh.
# Independent eyes, brows, mouth and bone widgets are never shell targets.
_HEAD_SOURCES = frozenset({
    "无脸", "下半小眼睛", "中上小眼睛", "中下大眼睛", "中下小眼睛",
    "中位大眼睛", "中位小眼睛", "标准大眼睛", "头部 · 八型单网格",
})

# These are immutable source roles, not display-name or ownership proofs.
# require_owned checks actual ID references before these roles are used.
_BODY_SOURCES = frozenset({
    "身体", "身体第二层", "头二层",
    "L.细胳膊", "R.细胳膊", "L.细胳膊二层", "R.细胳膊二层",
    "L粗隔壁", "R粗隔壁", "L粗胳膊二层", "R粗胳膊二层",
    "L腿", "R腿", "L腿二层", "R腿二层",
    "L腿-官方", "R腿-官方", "L腿-官方二层", "R腿-官方二层",
    "披风·单段", "披风·五段", "New model",
})
_BACK_SOURCES = frozenset({"披风·单段", "披风·五段", "New model"})


def _source_role(obj):
    """Resolve a role inside an already proven instance, never ownership.

    Native Object append has no weak source reference on supported hosts.
    Early imports consequently saved Blender's numeric collision suffix in
    the source role. Keep that persisted value and resolve only known roles;
    callers must first validate the actual root/object ID references.
    """
    from .human import source_name

    source = source_name(obj)
    while source not in (_BODY_SOURCES | _HEAD_SOURCES):
        prefix, separator, suffix = source.rpartition(".")
        if not separator or not suffix.isdigit():
            return None
        source = prefix
    return source


def _validate_suffixed_role(root, obj, role):
    """Cross-check old Object roles against the proven native Mesh source."""
    from .human import source_name

    if source_name(obj) == role:
        return
    require_owned(root, (obj.data,))
    if role == "头部 · 八型单网格":
        # The unified head's Mesh has its own versioned source label rather
        # than the Object role. Prove its authored exterior/facial branches.
        attribute = obj.data.attributes.get("V3_HeadVariant")
        if (attribute is None or attribute.domain != "FACE" or attribute.data_type != "INT"
                or obj.data.shape_keys is None
                or {item.value for item in attribute.data} != {-1, *range(8)}):
            raise ValueError("人模共用头部来源结构不匹配，未接管")
        require_owned(root, (obj.data.shape_keys,))
        return
    mesh_role = source_name(obj.data).partition(" · ")[0]
    while mesh_role != role:
        prefix, separator, suffix = mesh_role.rpartition(".")
        if not separator or not suffix.isdigit():
            raise ValueError("人模描边重名角色与原网格来源不匹配，未接管：" + role)
        mesh_role = prefix


def targets(root, members=None):
    """Return proven body/layer/back meshes, excluding armor and face details."""
    from . import human_back242

    candidates = tuple(root.all_objects) if members is None else tuple(members)
    require_owned(root, (root, *candidates))
    in_root = set(root.all_objects)
    if any(obj not in in_root for obj in candidates):
        raise ValueError("描边目标已离开此人模，未修改")
    back_members = set(human_back242.members(root)) if human_back242.current(root) else None
    result, roles = [], set()
    for obj in candidates:
        if obj.type != "MESH":
            continue
        source = _source_role(obj)
        if source is None:
            continue
        if source in _BACK_SOURCES and back_members is not None and obj not in back_members:
            # Saved early imports can retain an inactive native wing template.
            # Only the explicitly published back closure is an outline target.
            continue
        _validate_suffixed_role(root, obj, source)
        if source in roles:
            raise ValueError("人模描边来源角色不唯一，未接管：" + source)
        roles.add(source)
        result.append(obj)
    return tuple(result)


def prepared(root):
    """Lightweight UI state; no source IO, graph hashing or scene mutation."""
    record = root.get(MARK)
    return bool(record and hasattr(record, "get") and record.get("schema") == 1
                and record.get("scope") == "body_and_back"
                and record.get("owner") == root.get("oxal_human_controls"))


def _state(root, owner):
    record = root.get(MARK)
    if record is not None and (not hasattr(record, "get")
                               or record.get("schema") != 1
                               or record.get("owner") != owner
                               or record.get("scope") != "body_and_back"):
        raise ValueError("人模描边登记不兼容或归属不匹配，未覆盖")
    return record is not None


def _controls(transaction, owner):
    """Add missing channels without replacing existing animation or values."""
    from ...util.property_limits import update

    missing = tuple(key(name) for name in PARAMETERS if key(name) not in owner)

    def restore():
        for name in missing:
            if name in owner:
                del owner[name]

    transaction.on_rollback(restore)
    for name, (_label, default, _minimum, _maximum) in PARAMETERS.items():
        if key(name) not in owner:
            owner[key(name)] = default
    limits = {}
    for name, (label, default, minimum, maximum) in PARAMETERS.items():
        value = owner[key(name)]
        if isinstance(default, bool):
            valid = isinstance(value, (bool, int)) and value in (0, 1)
        elif isinstance(default, tuple):
            valid = (hasattr(value, "__len__") and len(value) == 4
                     and all(isinstance(channel, (int, float)) and math.isfinite(channel)
                             and minimum <= channel <= maximum for channel in value))
        else:
            valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value) and minimum <= value <= maximum)
        if not valid:
            raise ValueError("人模描边参数无效，未覆盖：" + name)
        options = {"description": label}
        if not isinstance(default, bool):
            options.update(min=minimum, max=maximum, soft_min=minimum, soft_max=maximum)
        if isinstance(default, tuple):
            options["subtype"] = "COLOR"
        limits[key(name)] = options
    update(transaction, owner, limits)


def _snapshot(transaction, root, owner, *, enabled=None):
    previous = dict(root[MARK].items()) if MARK in root else None
    before = owner.get(key("outline_enabled"))

    def restore():
        if previous is None:
            if MARK in root:
                del root[MARK]
        else:
            root[MARK] = previous
        if before is not None:
            owner[key("outline_enabled")] = before
        owner.update_tag()

    transaction.on_rollback(restore)
    if enabled is not None:
        owner[key("outline_enabled")] = enabled


def _external_users(root, members, transaction=None):
    """Allow only this caller's newly tracked, not yet sealed consumers."""
    scene = cast(Any, bpy.context.scene)
    fresh = tuple(live_ids(transaction.ids)) if transaction is not None else ()
    if not fresh:
        require_no_external_users(scene, root, members)
        return
    proven = owned_ids(root)
    require_owned(root, (root, *proven, *members))
    domain = {root, *proven, *fresh}
    domain.update(value for item in tuple(domain) for field in ("node_tree", "shape_keys")
                  if isinstance(value := getattr(item, field, None), bpy.types.ID))
    explicit = explicit_scene_ids(scene, root=root)
    for item, users in bpy.data.user_map(subset=members).items():
        if any(user not in domain and not (user == scene and item not in explicit) for user in users):
            raise ValueError("其它数据或场景显式引用仍使用人模描边目标，未修改")


def _context(root, members=None, *, transaction=None):
    from ..outline import outline_native
    from .human import parameter_owner

    ensure_mutation_allowed()
    if bpy.context.mode != "OBJECT":
        raise ValueError("请在物体模式准备或移除人模描边")
    owner = parameter_owner(root)
    require_owned(root, (owner,))
    _state(root, owner)
    current = set(root.all_objects)
    for item in owned_ids(root):
        if (isinstance(item, bpy.types.Object) and outline_native.prepared(item, role=ROLE)
                and (item not in current or _source_role(item) is None)):
            raise ValueError("已准备的描边对象离开人模或来源角色被修改，未丢弃其结构")
    meshes = targets(root, members)
    _external_users(root, (owner, *meshes), transaction)
    return owner, meshes


def generated_modifiers(root, obj, *, transaction=None):
    """Validate the exact generated modifier before source-part comparison."""
    from ..outline import outline_native

    return outline_native.validate(root, obj, transaction=transaction, role=ROLE)


def suspend(transaction, root, members):
    """Retire exact generated slots before publishing a material replacement."""
    from ..outline import outline_native

    for obj in targets(root, members):
        outline_native.remove(transaction, root, obj, role=ROLE)


def _apply(transaction, root, owner, meshes, *, rebuild=False):
    from ..outline import outline_native

    for obj in meshes:
        source = _source_role(obj)
        outline_native.apply(transaction, root, obj, owner,
                             enabled=key("outline_enabled"), width=key("outline_width"),
                             color=key("outline_color"), quality_normals=key("outline_quality_normals"),
                             role=ROLE, rebuild=rebuild,
                             pixel_surface=source in (_HEAD_SOURCES | {"头二层"}),
                             source_attribute=("V3_HeadVariant"
                                               if source == "头部 · 八型单网格" else None))


def prepare(root, *, enable=True, rebuild=False):
    """Prepare all current body/back shells in one reversible explicit edit."""
    from ..asset.asset_geometry import Creation

    if not isinstance(enable, bool):
        raise TypeError("人模描边开关必须为布尔值")
    owner, meshes = _context(root)
    if not meshes:
        raise ValueError("人模没有可描边的皮肤、二层或背饰")
    with Creation() as transaction:
        transaction.owner = root
        _controls(transaction, owner)
        _snapshot(transaction, root, owner, enabled=enable)
        _apply(transaction, root, owner, meshes, rebuild=rebuild)
        root[MARK] = {"schema": 1, "owner": owner, "scope": "body_and_back"}
        from .human_armor_outline import refresh as refresh_armor

        refresh_armor(transaction, root, tuple(root.all_objects), rebuild=rebuild)
        owner.update_tag()
        transaction.commit(root)


def remove(root):
    """Remove this feature's exact modifiers while retaining native channels."""
    from ..asset.asset_geometry import Creation
    from ..outline import outline_native

    owner, meshes = _context(root)
    with Creation() as transaction:
        transaction.owner = root
        _controls(transaction, owner)
        _snapshot(transaction, root, owner, enabled=False)
        for obj in meshes:
            outline_native.remove(transaction, root, obj, role=ROLE)
        if MARK in root:
            del root[MARK]
        owner.update_tag()
        transaction.commit(root)


def repair(root):
    """Explicitly rebuild a prepared feature without changing its channels."""
    owner, _meshes = _context(root)
    if not _state(root, owner):
        return
    prepare(root, enable=bool(owner.get(key("outline_enabled"), False)), rebuild=True)


def refresh(transaction, root, incoming, *, rebuild=False):
    """Part replacement hook, after incoming IDs are sealed to the same root.

    The outer import/part transaction owns rollback and commit. No outgoing
    object references are retained, so arm/leg/elytra retirement stays native.
    """
    if MARK not in root:
        return
    owner, meshes = _context(root, incoming, transaction=transaction)
    _controls(transaction, owner)
    _apply(transaction, root, owner, meshes, rebuild=rebuild)
    from .human_armor_outline import refresh as refresh_armor

    refresh_armor(transaction, root, incoming, rebuild=rebuild)
    owner.update_tag()
