"""Actual source Solidify entry points and strict old Beta graph retirement.

New outlines always use the bundled reference modifier and Material through
outline_solidify. The previous procedural hull is only validated and removed
for explicit Beta migration; no replacement hull generator remains here.
"""

from typing import Any

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.ownership import require_owned
from ..enchantment import enchantment

MARK = "oxal_mc_studio_native_outline_schema"
RECORD = "oxal_mc_studio_native_outline"
MATERIAL = "oxal_mc_studio_native_outline_material"
MATERIALS = "oxal_mc_studio_native_outline_materials"
ALPHA_GROUPS = "oxal_mc_studio_native_outline_alpha_groups"
SOURCE_SHA256 = "41065561ad769a75eb90954f7f18e6ba00b1525a0e3969074af40d07bc04baec"




def _record(obj) -> dict[str, dict[str, Any]]:
    from ..enchantment.enchantment_outline import plain

    # ID references must stay actual IDs, not names or copied ownership UUIDs.
    def clone(value) -> Any:
        if isinstance(value, bpy.types.ID):
            return value
        if hasattr(value, "items"):
            return {key: clone(part) for key, part in value.items()}
        return plain(value)

    return clone(obj.get(RECORD, {}))


def _owned(root, items, transaction=None):
    existing = tuple(item for item in items if transaction is None or item not in transaction.ids)
    if existing:
        require_owned(root, existing)


def _journal(transaction, obj):
    existed, previous = RECORD in obj, _record(obj)

    def restore():
        if existed:
            obj[RECORD] = previous
        elif RECORD in obj:
            del obj[RECORD]

    transaction.on_rollback(restore)
    # Publication/removal edits its working table. The rollback snapshot must
    # remain an independent container holding the same exact Blender IDs.
    return _record(obj)






def validate_material(root, tree, *, transaction=None):
    from ..enchantment.enchantment_outline import CONTRACT, contract_matches

    materials = tree.get(MATERIALS, {})
    if not materials:
        raise ValueError("描边专属材质引用已损坏，未覆盖")
    for material in materials.values():
        if not isinstance(material, bpy.types.Material):
            raise TypeError("描边专属材质引用已损坏，未覆盖")
        groups = tuple(material.get(ALPHA_GROUPS, {}).values())
        _owned(root, (material, *groups), transaction)
        if (material.get(MARK) != 1 or material.node_tree is None
                or not material.use_backface_culling
                or not enchantment.material_contract_matches(material, material.get(CONTRACT))
                or not contract_matches(material.node_tree, reference_owner=material)
                or any(not contract_matches(group) for group in groups)):
            raise ValueError("描边专属材质含用户修改，未覆盖")


def _validate_legacy(root, obj, *, transaction=None, role="human"):
    """Return exact owned modifiers only; copied tags never grant ownership."""
    from ..enchantment.enchantment_outline import (
        OWNER,
        ROLE,
        contract_matches,
        modifier_contract_matches,
    )

    row = _record(obj).get(role)
    if not row:
        if any(mod.type == "NODES" and mod.node_group and mod.node_group.get(MARK)
               and mod.node_group.get(ROLE) == role for mod in obj.modifiers):
            raise ValueError("描边修改器没有精确登记，未接管：" + obj.name)
        return ()
    tree, owner, material = row["group"], row["owner"], row["material"]
    modifier = obj.modifiers.get(row["name"])
    _owned(root, (obj, owner, tree, material), transaction)
    validate_material(root, tree, transaction=transaction)
    if (modifier is None or modifier.type != "NODES" or modifier.node_group != tree
            or tree.get(MARK) != 1 or tree.get(ROLE) != role or tree.get(OWNER) != owner
            or tree.get(MATERIAL) != material or not material.use_backface_culling
            or list(obj.modifiers).index(modifier) != row["position"]
            or row["position"] != len(obj.modifiers) - 1
            or not modifier_contract_matches(modifier, row["settings"])
            or not contract_matches(tree)
            or not contract_matches(material.node_tree, reference_owner=material)):
        raise ValueError("描边生成结构或参数含用户修改，未覆盖：" + obj.name)
    for target in (tree,):
        animation = target.animation_data
        if animation and (animation.action is not None or animation.nla_tracks):
            raise ValueError("描边节点存在独立用户动画，未覆盖")
    animation = obj.animation_data
    if animation:
        curves = list(animation.drivers)
        curves.extend(action_fcurves(animation.action, slot=getattr(animation, "action_slot", None)))
        strips = [strip for track in animation.nla_tracks for strip in track.strips]
        while strips:
            strip = strips.pop()
            curves.extend(action_fcurves(strip.action, slot=getattr(strip, "action_slot", None)))
            strips.extend(getattr(strip, "strips", ()))
        if any(curve.data_path.startswith(modifier.path_from_id() + ".") for curve in curves):
            raise ValueError("描边修改器存在用户动画，未覆盖")
    return (modifier,)




def _remove_legacy(transaction, root, obj, *, role="human"):
    ensure_mutation_allowed()
    modifiers = _validate_legacy(root, obj, transaction=transaction, role=role)
    if not modifiers:
        return
    table = _journal(transaction, obj)
    modifier = modifiers[0]
    name, group, position = modifier.name, modifier.node_group, list(obj.modifiers).index(modifier)

    def restore():
        restored = obj.modifiers.new(name, "NODES")
        restored.node_group = group
        obj.modifiers.move(len(obj.modifiers) - 1, min(position, len(obj.modifiers) - 1))

    transaction.on_rollback(restore)
    obj.modifiers.remove(modifier)
    del table[role]
    if table:
        obj[RECORD] = table
    else:
        del obj[RECORD]
    obj.update_tag()


def _prepared_legacy(obj, *, role="human"):
    """Read-only UI metadata: no source IO, graph evaluation or mutation."""
    return bool(_record(obj).get(role))


def validate(root, obj, *, transaction=None, role="human"):
    from . import outline_solidify

    legacy = _validate_legacy(root, obj, transaction=transaction, role=role)
    current = outline_solidify.validate(root, obj, transaction=transaction, role=role)
    if legacy and current:
        raise ValueError("同一描边角色存在两套生成链，未按显示名接管")
    return current or legacy


def invalidate(transaction, obj, *, materials=()):
    """Invalidate only this Creation's exact native outline validation receipts."""
    from . import outline_solidify

    outline_solidify.invalidate(transaction, obj, materials=materials)


def apply(transaction, root, obj, owner, *, enabled="outline_enabled", width="outline_width",
          color="outline_color", brightness=None, gate=None, role="human", rebuild=False,
          follow=None, suppression=None, source_attribute=None,
          quality_normals=None, quality_follow_key=None, disable_when=None, pixel_surface=False):
    from . import outline_solidify

    ensure_mutation_allowed()
    if _prepared_legacy(obj, role=role):
        invalidate(transaction, obj)
        _remove_legacy(transaction, root, obj, role=role)
    outline_solidify.apply(transaction, root, obj, owner, enabled=enabled, width=width,
                           color=color, brightness=brightness, gate=gate, role=role,
                           rebuild=rebuild, follow=follow, suppression=suppression,
                           source_attribute=source_attribute, quality_normals=quality_normals,
                           quality_follow_key=quality_follow_key, disable_when=disable_when,
                           pixel_surface=pixel_surface)


def remove(transaction, root, obj, *, role="human"):
    from . import outline_solidify

    ensure_mutation_allowed()
    if _prepared_legacy(obj, role=role):
        invalidate(transaction, obj)
        _remove_legacy(transaction, root, obj, role=role)
    outline_solidify.remove(transaction, root, obj, role=role)


def prepared(obj, *, role="human"):
    from . import outline_solidify

    return _prepared_legacy(obj, role=role) or outline_solidify.prepared(obj, role=role)


def base_slots(root, obj, *, transaction=None):
    from . import outline_solidify

    return outline_solidify.base_slots(root, obj, transaction=transaction)


def generated_slots(root, obj, *, transaction=None):
    from . import outline_solidify

    return outline_solidify.generated_slots(root, obj, transaction=transaction)


def generated_driver_paths(root, obj, *, transaction=None):
    from . import outline_solidify

    return outline_solidify.generated_driver_paths(root, obj, transaction=transaction)
