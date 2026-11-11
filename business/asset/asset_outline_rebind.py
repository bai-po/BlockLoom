"""Explicit item-outline rebind; user geometry stays outside provenance.

asset_id=oxal.library.outline_rebind; version=1; policy=NEW_INSTANCE;
scope=selected exact owned root Objects; source=the current user's Mesh and
asset/outline/outline_reference.blend. No geometry contract is restamped.
"""

from typing import Any

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import explicit_scene_ids, owned_ids, require_owned
from ..enchantment import enchantment
from ..outline import outline_solidify as solidify
from .asset_geometry import Creation

RECORD = "oxal_mc_studio_item_outline_rebind"


def _only_parent_reference(child, root):
    """Permit an unknown child only when its sole root reference is parent."""
    seen = set()

    def custom(value):
        if isinstance(value, bpy.types.ID):
            return value == root
        if hasattr(value, "items"):
            return any(custom(part) for _, part in value.items())
        return False

    def visit(owner):
        if owner is None or not hasattr(owner, "bl_rna"):
            return False
        pointer = owner.as_pointer()
        if pointer in seen:
            return False
        seen.add(pointer)
        if hasattr(owner, "items"):
            try:
                values = dict(owner.items())
            except TypeError:
                # Some native RNA structs expose items() without supporting
                # IDProperties; their actual RNA pointers are inspected below.
                values = {}
            if custom(values):
                return True
        system = getattr(owner, "bl_system_properties_get", None)
        if system is not None and custom(system()):
            return True
        for prop in owner.bl_rna.properties:
            if prop.is_runtime or prop.identifier in {"rna_type", "id_data", "original"}:
                continue
            if owner == child and prop.identifier in {
                "parent", "children", "children_recursive", "users_collection", "users_scene",
            }:
                continue
            if prop.type not in {"POINTER", "COLLECTION"}:
                continue
            value = getattr(owner, prop.identifier)
            for item in tuple(value) if prop.type == "COLLECTION" else (value,):
                if isinstance(item, bpy.types.ID):
                    if item == root:
                        return True
                elif visit(item):
                    return True
        return False

    return isinstance(child, bpy.types.Object) and child.parent == root and not visit(child)


def _preflight(scene, root):
    require_owned(root, (root,))
    if root.type != "MESH" or root.mode != "OBJECT":
        raise ValueError("重新绑定附魔描边只支持物体模式的独立物品Mesh根")
    if root.get("oxal_mc_studio_geometry_kind") != "ITEM":
        raise ValueError("重新绑定附魔描边仅支持物品实例，请使用对应方块或盔甲功能")
    if root.data.library or root.data.override_library:
        raise ValueError("当前物品Mesh为链接或覆盖数据，请先本地化")
    domain = {root, *owned_ids(root)}
    references = explicit_scene_ids(scene, root=root)
    for user in bpy.data.user_map(subset=(root,)).get(root, ()):
        if user in domain or (user == scene and root not in references):
            continue
        if not _only_parent_reference(user, root):
            raise ValueError("其它数据显式引用物品根，无法安全重新绑定：" + user.name)
    if solidify.RECORD in root:
        return solidify.retire_for_rebind(None, root, root)
    return solidify.validate(root, root, role="enchant")


def _used_surfaces(root):
    """Include current evaluated surfaces; never parse an unused user slot."""
    indices = {polygon.material_index for polygon in root.data.polygons}
    # Material-index animation or arbitrary GN material assignment could make a
    # currently unused slot visible later. Reject those uncertain chains.
    for modifier in root.modifiers:
        if modifier.type == "NODES":
            group = modifier.node_group
            from ..enchantment.enchantment_outline import (
                GEOMETRY_MARK,
                contract_matches,
            )

            if (group is None or group.get(GEOMETRY_MARK) != 1 or not contract_matches(group)
                    or any(node.bl_idname in {"GeometryNodeSetMaterialIndex", "GeometryNodeGroup"}
                           for node in group.nodes)):
                raise ValueError("保留的几何节点可能动态分配材质，请先应用或整理后重新绑定：" + modifier.name)
            for node in group.nodes:
                if node.bl_idname != "GeometryNodeSetMaterial":
                    continue
                material = node.inputs["Material"]
                if material.is_linked:
                    raise ValueError("保留的几何节点动态选择材质，未忽略其潜在表面：" + modifier.name)
                matches = {index for index, slot in enumerate(root.material_slots)
                           if slot.material == material.default_value}
                if not matches:
                    raise ValueError("保留的几何节点引用槽外材质，请先整理材质：" + modifier.name)
                indices.update(matches)
    evaluated = root.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        indices.update(polygon.material_index for polygon in mesh.polygons)
    finally:
        evaluated.to_mesh_clear()
    if not indices or any(index >= len(root.material_slots) for index in indices):
        raise ValueError("当前物品表面缺少有效材质槽")
    return tuple(sorted(indices))


def _copy_mesh(transaction, root):
    previous = root.data
    slots = tuple((slot.link, slot.material) for slot in root.material_slots)
    private = transaction.track(previous.copy())
    if previous.shape_keys is not None and private.shape_keys == previous.shape_keys:
        raise ValueError("原生Mesh复制未生成独立Key，未接管用户形态键")
    if private.shape_keys is not None:
        transaction.track(private.shape_keys)
    for owner, source in ((private, previous), (private.shape_keys, previous.shape_keys)):
        animation = owner.animation_data if owner is not None else None
        for curve in animation.drivers if animation else ():
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id == source:
                        target.id = owner

    def restore():
        root.data = previous
        for index, (link, material) in enumerate(slots):
            root.material_slots[index].link = link
            root.material_slots[index].material = material

    transaction.on_rollback(restore)
    root.data = private
    for index, (link, material) in enumerate(slots):
        root.material_slots[index].link = link
        root.material_slots[index].material = material
    return previous, private


def _trim_retired_tail(root, state, used_indices):
    """Discard only an exact unused generated suffix on this new Mesh copy.

    Rebind deliberately preserves arbitrary user slots. Keeping a proven old
    Solidify suffix as new base slots, however, doubles slots on every click.
    Any user-assigned face, changed binding or appended slot prevents trimming.
    """
    row = state.get("roles", {}).get("enchant", {})
    start, count = row.get("slot_start"), row.get("slot_count")
    if (type(start) is not int or type(count) is not int or count <= 0
            or start + count != len(root.material_slots)
            or any(index >= start for index in used_indices)):
        return
    materials = row.get("materials", {})
    if any(root.material_slots[start + index].material != materials.get(str(index))
           for index in range(count)):
        return
    for index in range(start + count - 1, start - 1, -1):
        root.data.materials.pop(index=index)


def rebind(scene: Any, options: dict[str, Any]) -> str:
    """Explicit EMT command; only root identity is taken from selection IDs."""
    from .asset_enchantment import verify_targets
    from .assets import selected_roots

    ensure_mutation_allowed()
    if bpy.context.scene != scene:
        raise ValueError("请切回提交重新绑定时的场景")
    verify_targets(scene, options)
    roots = selected_roots(scene, options.get("instances"))
    from ..enchantment.enchantment_outline import (
        _legacy_modifiers,
        _outline_animated,
        _record,
    )

    for root in roots:
        _preflight(scene, root)
        if _outline_animated(root):
            raise ValueError("附魔描边开关已有关键帧、NLA或驱动，请在原通道启用，未覆盖其动画")
        if _record(root).get("geometry", {}).get("relief"):
            # Rebinding can borrow edited item surfaces, but rebuilding a
            # generated relief branch still requires its complete old proof.
            _legacy_modifiers(root, root)
    with Creation() as transaction:
        for root in roots:
            transaction.owner = root
            before_record = solidify._clone(root.get(RECORD))
            before_keys = set(root.keys())
            previous_enabled = root.get("glint_outline_enabled")

            def restore(root=root, before_record=before_record, before_keys=before_keys,
                        previous_enabled=previous_enabled):
                for key in tuple(root.keys()):
                    if key not in before_keys:
                        del root[key]
                if before_record is not None:
                    root[RECORD] = before_record
                if previous_enabled is not None:
                    root["glint_outline_enabled"] = previous_enabled

            transaction.on_rollback(restore)
            retired_state = solidify._state(root)
            previous, private = _copy_mesh(transaction, root)
            if solidify.RECORD in root:
                solidify.retire_for_rebind(transaction, root, root)
            indices = _used_surfaces(root)
            _trim_retired_tail(root, retired_state, indices)
            enchantment.control_defaults(root)
            root["glint_outline_enabled"] = True
            if _record(root).get("geometry", {}).get("relief"):
                from ..enchantment import enchantment_geometry

                enchantment_geometry.prepare(transaction, root, root, root,
                                             relief=True, outline_proxy=True)
                private = root.data
            receipts = getattr(transaction, "solidify_rebind_targets", {})
            receipts[root] = private
            transaction.solidify_rebind_targets = receipts  # pyright: ignore[reportAttributeAccessIssue]
            solidify.apply(transaction, root, root, root, role="enchant",
                           enabled="glint_outline_enabled", width="glint_outline_width",
                           color="glint_outline_color", brightness="glint_outline_brightness",
                           quality_normals="glint_outline_quality_normals", gate=None,
                           source_indices=indices)
            root[RECORD] = {"schema": 1, "source_mesh": previous, "private_mesh": private,
                            "source_indices": list(indices),
                            **({"previous": before_record} if before_record is not None else {})}
            root.update_tag()
        transaction.prepare_commit()
        transaction.finish_commit()
        # Retired IDs and the original user Mesh are deliberately retained;
        # this operation never invokes broad instance-owned-data pruning.
    return f"已重新绑定描边：{len(roots)} 个物品；保留自定义材质槽，描边与材质附魔独立"
