"""Native material remapping after a user's geometry-node evaluation.

asset_id: oxal.enchantment.external.assigners; asset_version: 1;
policy: NEW_INSTANCE; scope: one explicitly selected external Object.
Only the adapter NodeTree/Modifier is ours. The user's Mesh, geometry-node
groups, their animation and their material-input sockets are never rewritten.
"""

from typing import Any

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import require_owned
from . import enchantment_outline as contracts

RECORD = "oxal_external_material_assigners"
MARK = "oxal_external_material_adapter_schema"
TARGET = "oxal_external_target"


def _clone(value) -> Any:
    if isinstance(value, bpy.types.ID):
        return value
    if hasattr(value, "items"):
        return {key: _clone(part) for key, part in value.items()}
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return [_clone(part) for part in value]


def _journal(transaction, owner):
    before = _clone(owner.get(RECORD))

    def restore_record():
        if before is None:
            if RECORD in owner:
                del owner[RECORD]
        else:
            owner[RECORD] = before

    transaction.on_rollback(restore_record)


def _modifier_materials(modifier):
    """Read actual Material pointers in old and new native modifier storage."""
    from ...util.geometry_nodes import get_input

    result = []

    def visit(value):
        if isinstance(value, bpy.types.Material):
            result.append(value)
        elif hasattr(value, "items"):
            for part in value.values():
                visit(part)

    if modifier.bl_rna.properties.get("properties") is None:
        visit(dict(modifier.items()))
    elif modifier.node_group is not None:
        for socket in modifier.node_group.interface.items_tree:
            if (socket.item_type == "SOCKET" and socket.in_out == "INPUT"
                    and socket.bl_rna.properties.get("default_value") is not None):
                visit(get_input(modifier, socket.identifier))
    return result


def _tree_materials(tree, result, seen, active):
    if tree in active:
        raise ValueError("几何节点组递归，无法确认待附魔表面")
    if tree in seen:
        return
    seen.add(tree)
    for item in tree.interface.items_tree:
        material = getattr(item, "default_value", None)
        if isinstance(material, bpy.types.Material):
            result.append(material)
    for node in tree.nodes:
        for socket in (*tuple(node.inputs), *tuple(node.outputs)):
            value = getattr(socket, "default_value", None)
            if isinstance(value, bpy.types.Material):
                result.append(value)
            # Object/Collection Info can introduce surfaces without a
            # SetMaterial node. Their source IDs remain read-only leaves.
            elif isinstance(value, bpy.types.Object):
                result.extend(slot.material for slot in value.material_slots if slot.material)
            elif isinstance(value, bpy.types.Collection):
                result.extend(slot.material for obj in tuple(value.all_objects)
                              for slot in obj.material_slots if slot.material)
        group = getattr(node, "node_tree", None)
        if group is not None:
            _tree_materials(group, result, seen, {*active, tree})


def materials(obj):
    """Read potential GN surfaces, with our current native mapping applied.

    This discovery grants no ownership. apply() checks the exact persisted
    adapter references before modifying its own private groups.
    """
    result, seen = [], set()
    mappings = []
    for modifier in obj.modifiers:
        if modifier.type != "NODES" or modifier.node_group is None:
            continue
        group = modifier.node_group
        if group.get(MARK) == 1:
            mappings.extend((node.inputs[1].default_value, node.inputs[2].default_value)
                            for node in group.nodes
                            if node.bl_idname == "GeometryNodeReplaceMaterial")
            continue
        result.extend(_modifier_materials(modifier))
        _tree_materials(group, result, seen, set())
    for before, after in mappings:
        result = [after if material == before else material for material in result]
    return tuple(dict.fromkeys(material for material in result if material is not None))


def snapshot(obj):
    """Only immutable identity values travel with the submitting request."""
    result = []
    seen = set()

    def tree_snapshot(tree):
        if tree in seen:
            return []
        seen.add(tree)
        from .enchantment import _digest

        rows = [{"tree_uid": tree.session_uid, "digest": _digest(tree)}]
        for node in tree.nodes:
            group = getattr(node, "node_tree", None)
            if group is not None:
                rows.extend(tree_snapshot(group))
        return rows

    for modifier in obj.modifiers:
        if modifier.type != "NODES":
            continue
        group = modifier.node_group
        result.append({
            "modifier_uid": modifier.persistent_uid,
            "group_uid": group.session_uid if group else None,
            "trees": tree_snapshot(group) if group else [],
            "materials": [material.session_uid for material in _modifier_materials(modifier)],
        })
    return result


def _validate(transaction, obj, owner):
    if owner.get(TARGET) != obj:
        raise ValueError("材质适配器的实际目标已变化，未接管")
    row = owner.get(RECORD)
    if row is None:
        if any(mod.type == "NODES" and mod.node_group and mod.node_group.get(MARK)
               for mod in obj.modifiers):
            raise ValueError("目标存在未登记的材质适配器，未按标记接管")
        return None, None
    if not hasattr(row, "get") or row.get("schema") != 1:
        raise ValueError("未知外部材质适配器版本")
    group = row.get("group")
    if not isinstance(group, bpy.types.NodeTree):
        raise TypeError("材质适配器节点树引用已损坏")
    if group not in transaction.ids:
        require_owned(owner, (group,))
    modifier = next((mod for mod in obj.modifiers
                     if mod.persistent_uid == row.get("modifier_uid")), None)
    if (modifier is None or modifier.type != "NODES" or modifier.node_group != group
            or group.get(MARK) != 1 or not contracts.contract_matches(group)
            or not modifier.show_viewport or not modifier.show_render
            or list(obj.modifiers).index(modifier) != len(obj.modifiers) - 1):
        raise ValueError("附魔材质适配器含用户修改或顺序变化，保留原链路")
    for index, pair in enumerate(row.get("mapping", {}).values()):
        node = group.nodes.get("Replace " + str(index))
        if (node is None or node.inputs[1].default_value != pair.get("source")
                or node.inputs[2].default_value != pair.get("current")):
            raise ValueError("附魔材质映射引用已变化，未覆盖")
    return modifier, row


def _new_group(transaction, pairs):
    group = transaction.track(bpy.data.node_groups.new("附魔 · 评估表面材质", "GeometryNodeTree"))
    group[MARK] = 1
    group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    source = group.nodes.new("NodeGroupInput")
    output = group.nodes.new("NodeGroupOutput")
    realize = group.nodes.new("GeometryNodeRealizeInstances")
    group.links.new(source.outputs["Geometry"], realize.inputs["Geometry"])
    geometry = realize.outputs["Geometry"]
    for index, (before, after) in enumerate(pairs):
        node = group.nodes.new("GeometryNodeReplaceMaterial")
        node.name = "Replace " + str(index)
        node.inputs[1].default_value = before
        node.inputs[2].default_value = after
        group.links.new(geometry, node.inputs[0])
        geometry = node.outputs[0]
    group.links.new(geometry, output.inputs["Geometry"])
    group[contracts.CONTRACT] = contracts.contract(group, create=True)
    return group


def apply(transaction, obj, replacements, owner):
    """Map evaluated material IDs without copying or editing user GN groups.

    A repeat maps the current generated material to its next private material.
    Cancellation may map to an edited, unwrapped private base; that exact base
    remains in the native adapter rather than losing edits to restore a name.
    """
    ensure_mutation_allowed()
    modifier, row = _validate(transaction, obj, owner)
    if modifier is None and not any(part.type == "NODES" for part in obj.modifiers):
        # Object-linked slots already cover an ordinary surface. A native
        # remapper is needed only when geometry nodes introduce materials.
        return False
    mapping = [(pair.get("source"), pair.get("current")) for pair in row["mapping"].values()] if row else []
    applicable = set(materials(obj)) | {current for _, current in mapping}
    # None is an explicit caller-proven evaluated surface, never inferred
    # from an unrelated empty socket in a user's GN graph.
    if None in replacements:
        applicable.add(None)
    relevant = {old: new for old, new in replacements.items() if old in applicable and old != new}
    if not relevant:
        return False
    mapping = [(source, relevant.get(current, current)) for source, current in mapping]
    covered = {pair.get("current") for pair in row["mapping"].values()} if row else set()
    mapping.extend((old, new) for old, new in relevant.items() if old not in covered)
    mapping = [(old, new) for old, new in mapping if old != new]
    group = _new_group(transaction, mapping)
    _journal(transaction, owner)
    if modifier is None:
        modifier = obj.modifiers.new("附魔 · 原生材质适配", "NODES")
        created_uid = modifier.persistent_uid

        def undo_created():
            current = next((mod for mod in obj.modifiers if mod.persistent_uid == created_uid), None)
            if current is not None and current.type == "NODES" and current.node_group == group:
                obj.modifiers.remove(current)

        transaction.on_rollback(undo_created)
    else:
        previous = modifier.node_group
        transaction.on_rollback(lambda: setattr(modifier, "node_group", previous))
    modifier.node_group = group
    owner[RECORD] = {
        "schema": 1, "group": group, "modifier_uid": modifier.persistent_uid,
        "mapping": {str(index): {**({"source": source} if source is not None else {"source_missing": True}),
                                 **({"current": current} if current is not None else {"current_missing": True})}
                    for index, (source, current) in enumerate(mapping)},
    }
    obj.update_tag()
    return True


def restore(transaction, obj, owner):
    """Clear native mapping while retaining the exact modifier for rollback.

    The resulting passthrough group has no material substitutions. Retaining
    this tiny adapter avoids deleting/recreating a user's native modifier UID
    during a transaction and never discards edited private base materials.
    """
    ensure_mutation_allowed()
    modifier, row = _validate(transaction, obj, owner)
    if modifier is None or row is None:
        return False
    return apply(transaction, obj, {pair.get("current"): pair.get("source")
                                   for pair in row["mapping"].values()}, owner)
