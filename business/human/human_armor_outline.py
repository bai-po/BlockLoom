"""Independent native armor outlines, optionally following the human controls.

asset_id: oxal.human.armor_outline; asset_version: 1; policy: NEW_INSTANCE;
scope: one exactly owned human instance. The native reversed hull is shared
with outline_native; no enchantment shader, skin or source Mesh is replaced.
"""

import math

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import owned_ids, require_owned
from .human_state import PARAMETERS as HUMAN_PARAMETERS
from .human_state import PARTS, key

MARK = "oxal_mc_studio_human_armor_outline"
ROLE = "armor"
FIELDS = ("follow_human", "enabled", "color", "width", "quality_normals")
NAMES = tuple(part + "_outline_" + field for part in PARTS for field in FIELDS)


def prepared(root, parts=None):
    """Lightweight UI metadata, without source reads or graph evaluation."""
    record = root.get(MARK)
    valid = bool(record and hasattr(record, "get") and record.get("schema") == 1
                 and record.get("scope") == "armor"
                 and record.get("owner") == root.get("oxal_human_controls"))
    if not valid:
        return False
    recorded = record.get("parts", ())
    if isinstance(recorded, str) or not hasattr(recorded, "__iter__"):
        return False
    selected = set(recorded)
    return bool(selected) if parts is None else bool(parts) and set(parts) <= selected


def _parts(parts):
    selected = set(parts)
    if not selected or selected - set(PARTS):
        raise ValueError("盔甲描边部位无效")
    return tuple(part for part in PARTS if part in selected)


def _state(root, owner):
    record = root.get(MARK)
    if record is not None and (not hasattr(record, "get")
                               or record.get("schema") != 1
                               or record.get("owner") != owner
                               or record.get("scope") != "armor"
                               or "parts" not in record
                               or isinstance(record["parts"], str)
                               or set(record["parts"]) - set(PARTS)):
        raise ValueError("盔甲描边登记不兼容或归属不匹配，未覆盖")
    return set(record["parts"]) if record is not None else set()


def targets(root, members=None, *, parts=PARTS):
    """Resolve known armor roles only after proving the actual instance IDs."""
    from . import human_armor

    candidates = tuple(root.all_objects) if members is None else tuple(members)
    require_owned(root, (root, *candidates))
    current = set(root.all_objects)
    if any(obj not in current for obj in candidates):
        raise ValueError("描边目标已离开此人模，未修改")
    result, roles = [], set()
    for obj in candidates:
        if obj.type != "MESH" or human_armor.part_of(obj) not in parts:
            continue
        source = human_armor.source_identity(obj)
        if source in roles:
            raise ValueError("盔甲描边来源角色不唯一，未接管：" + source)
        roles.add(source)
        result.append(obj)
    return tuple(result)


def _context(root, members=None, *, transaction=None, parts=PARTS):
    from ..outline import outline_native
    from . import human_armor
    from .human import parameter_owner
    from .human_outline import _external_users

    ensure_mutation_allowed()
    if bpy.context.mode != "OBJECT":
        raise ValueError("请在物体模式准备或移除盔甲描边")
    owner = parameter_owner(root)
    require_owned(root, (owner,))
    registered = _state(root, owner)
    current = set(root.all_objects)
    meshes = targets(root, members, parts=parts)
    for item in owned_ids(root):
        if (isinstance(item, bpy.types.Object) and outline_native.prepared(item, role=ROLE)
                and (item not in current or item.type != "MESH" or not human_armor.part_of(item))):
            raise ValueError("已准备的盔甲描边对象离开人模或来源角色被修改，未丢弃其结构")
        if isinstance(item, bpy.types.Object) and outline_native.prepared(item, role=ROLE):
            human_armor.source_identity(item)
            if human_armor.part_of(item) not in registered:
                raise ValueError("盔甲描边缺少实例范围登记，未接管其结构")
    _external_users(root, (owner, *meshes), transaction)
    return owner, meshes


def _controls(transaction, owner, parts=PARTS):
    from ...util.property_limits import update

    names = tuple(part + "_outline_" + field for part in parts for field in FIELDS)
    parameters = {name: HUMAN_PARAMETERS[name] for name in (*names, "outline_enabled",
                                                           "outline_color", "outline_width", "outline_quality_normals")}
    missing = tuple(key(name) for name in parameters if key(name) not in owner)

    def restore():
        for name in missing:
            if name in owner:
                del owner[name]

    transaction.on_rollback(restore)
    limits = {}
    for name, (label, default, minimum, maximum) in parameters.items():
        channel = key(name)
        if channel not in owner:
            owner[channel] = default
        value = owner[channel]
        if isinstance(default, bool):
            valid = isinstance(value, (bool, int)) and value in (0, 1)
        elif isinstance(default, tuple):
            valid = (hasattr(value, "__len__") and len(value) == 4
                     and all(isinstance(component, (int, float)) and math.isfinite(component)
                             and minimum <= component <= maximum for component in value))
        else:
            valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value) and minimum <= value <= maximum)
        if not valid:
            raise ValueError("盔甲描边参数无效，未覆盖：" + name)
        limits[channel] = {"description": label}
        if not isinstance(default, bool):
            limits[channel].update(min=minimum, max=maximum, soft_min=minimum, soft_max=maximum)
        if isinstance(default, tuple):
            limits[channel]["subtype"] = "COLOR"
    update(transaction, owner, limits)


def _snapshot(transaction, root, owner, *, independent=None, parts=PARTS):
    previous = ({**dict(root[MARK].items()), "parts": list(root[MARK]["parts"])}
                if MARK in root else None)
    values = {key(name): owner.get(key(name))
              for part in parts for name in (part + "_outline_follow_human", part + "_outline_enabled")}

    def restore():
        if previous is None:
            if MARK in root:
                del root[MARK]
        else:
            root[MARK] = previous
        for name, value in values.items():
            if value is not None:
                owner[name] = value
        owner.update_tag()

    transaction.on_rollback(restore)
    if independent is not None:
        for part in parts:
            owner[key(part + "_outline_follow_human")] = False
            owner[key(part + "_outline_enabled")] = independent


def _publish(root, owner, parts):
    registered = _state(root, owner) | set(parts)
    root[MARK] = {"schema": 1, "owner": owner, "scope": "armor",
                  "parts": [part for part in PARTS if part in registered]}


def generated_modifiers(root, obj, *, transaction=None):
    """Exclude only this exact validated shell from immutable part contracts."""
    from ..outline import outline_native

    return outline_native.validate(root, obj, transaction=transaction, role=ROLE)


def suspend(transaction, root, members):
    """Temporarily retire exact hulls before changing the preceding glint chain.

    The root preparation record remains, so refresh recreates the same native
    control relationship. Rollback restores the old modifiers and exact IDs.
    """
    from ..outline import outline_native
    from .human import parameter_owner

    meshes = targets(root, members)
    registered = _state(root, parameter_owner(root))
    from .human_armor import part_of
    if any(outline_native.prepared(obj, role=ROLE) and part_of(obj) not in registered for obj in meshes):
        raise ValueError("盔甲描边缺少实例范围登记，未删除其结构")
    for obj in meshes:
        outline_native.remove(transaction, root, obj, role=ROLE)


def _apply(transaction, root, owner, meshes, *, rebuild=False):
    from ..outline import outline_native
    from .human_armor import part_of

    for obj in meshes:
        part = part_of(obj)
        if part is None:
            raise ValueError("盔甲描边部位来源已改变，未绑定不明控制器")
        outline_native.apply(transaction, root, obj, owner,
                             enabled=key(part + "_outline_enabled"),
                             width=key(part + "_outline_width"),
                             color=key(part + "_outline_color"), role=ROLE, rebuild=rebuild,
                             quality_normals=key(part + "_outline_quality_normals"),
                             quality_follow_key=key("outline_quality_normals"),
                             disable_when=(owner, key(part + "_3d")),
                             follow=(key(part + "_outline_follow_human"), key("outline_enabled"),
                                     key("outline_width"), key("outline_color")))


def _refresh_glint(transaction, root, meshes):
    from ..enchantment import enchantment_outline
    from . import human_armor

    for obj in meshes:
        owner = human_armor.glint_owner(root, obj, transaction=transaction)
        if owner is not None:
            human_armor.prepare_controls(transaction, root, (owner,))
            enchantment_outline.apply(transaction, root, obj, owner)


def _suspend_rebuilds(transaction, root, meshes, rebuild):
    """Only explicit rebuilds and old geometry-chain migration retire hulls."""
    from ..enchantment.enchantment_outline import RECORD

    retiring = tuple(obj for obj in meshes if rebuild or obj.get(RECORD))
    if retiring:
        suspend(transaction, root, retiring)


def refresh(transaction, root, incoming, *, rebuild=False, glint_current=False):
    """Refresh exact armor hulls in the caller's existing Creation journal."""
    from . import human_armor, human_outline

    if MARK not in root and not human_outline.prepared(root):
        return
    registered = set(root.get(MARK, {}).get("parts", ()))
    eligible = PARTS if human_outline.prepared(root) else registered
    owner, meshes = _context(root, incoming, transaction=transaction, parts=eligible)
    parts = tuple(part for part in PARTS if part in {human_armor.part_of(obj) for obj in meshes})
    _controls(transaction, owner, parts)
    _snapshot(transaction, root, owner, parts=parts)
    _publish(root, owner, parts)
    _suspend_rebuilds(transaction, root, meshes, rebuild)
    if not glint_current:
        _refresh_glint(transaction, root, meshes)
    _apply(transaction, root, owner, meshes, rebuild=rebuild)
    owner.update_tag()


def prepare(root, parts=PARTS, *, enable=True, rebuild=False):
    """Prepare armor without enchantment and select its independent channels."""
    from ..asset.asset_geometry import Creation

    if not isinstance(enable, bool):
        raise TypeError("盔甲描边开关必须为布尔值")
    parts = _parts(parts)
    owner, meshes = _context(root, parts=parts)
    with Creation() as transaction:
        transaction.owner = root
        _controls(transaction, owner, parts)
        _snapshot(transaction, root, owner, independent=enable, parts=parts)
        _publish(root, owner, parts)
        _suspend_rebuilds(transaction, root, meshes, rebuild)
        _refresh_glint(transaction, root, meshes)
        _apply(transaction, root, owner, meshes, rebuild=rebuild)
        owner.update_tag()
        transaction.commit(root)


def remove(root, parts=PARTS):
    """Explicitly remove only these hulls, preserving all user channels."""
    from ..asset.asset_geometry import Creation

    parts = _parts(parts)
    owner, meshes = _context(root, parts=parts)
    with Creation() as transaction:
        transaction.owner = root
        _controls(transaction, owner, parts)
        _snapshot(transaction, root, owner, independent=False, parts=parts)
        suspend(transaction, root, meshes)
        remaining = _state(root, owner) - set(parts)
        if remaining:
            root[MARK] = {"schema": 1, "owner": owner, "scope": "armor",
                          "parts": [part for part in PARTS if part in remaining]}
        elif MARK in root:
            del root[MARK]
        owner.update_tag()
        transaction.commit(root)


def repair(root, parts=PARTS):
    """Rebuild known prepared hulls without overwriting settings or animation."""
    from ..asset.asset_geometry import Creation

    if MARK not in root:
        return
    parts = _parts(parts)
    _owner, meshes = _context(root, parts=parts)
    with Creation() as transaction:
        transaction.owner = root
        refresh(transaction, root, meshes, rebuild=True)
        transaction.commit(root)
