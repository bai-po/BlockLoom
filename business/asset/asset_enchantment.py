"""Object-scoped material glint edits independent of generated geometry.

asset_id: oxal.library.enchantment; asset_version: 1; policy: NEW_INSTANCE;
scope: proven instance Objects' existing material slots. Sources are the user's
current surfaces; only new private Material/animation IDs join our registry.
Existing Meshes, their keys, custom slots and attached user Objects stay intact.
"""

from typing import Any

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import owned_ids, owns, require_owned
from . import asset_geometry


def _objects(root):
    from .assets import members

    return tuple(obj for obj in members(root)
                 if obj.type == "MESH" and owns(root, obj)
                 and not obj.get("oxal_mc_studio_effect_geometry"))


def _base_count(root, obj):
    from ..outline.outline_solidify import RECORD

    state = obj.get(RECORD)
    if state is None:
        return len(obj.material_slots)
    if (not hasattr(state, "get") or state.get("schema") != 1 or state.get("root") != root
            or type(state.get("base_count")) is not int
            or not 0 < state["base_count"] <= len(obj.material_slots)):
        raise ValueError("描边原表面范围无法确认，请先重新绑定描边：" + obj.name)
    # This restricts selection; it never grants ownership of the current Mesh.
    return state["base_count"]


def _surface_indices(root, obj):
    from ..outline.outline_solidify import RECORD

    count = _base_count(root, obj)
    state = obj.get(RECORD)
    indices = state.get("roles", {}).get("enchant", {}).get("source_indices") if state else None
    if (state and state.get("mesh") == obj.data and owns(root, obj.data)
            and indices is not None and all(type(index) is int and 0 <= index < count for index in indices)):
        # Explicit rebind records every potential surface in the validated
        # static chain. Preserve unrelated, unused user material slots.
        return sorted({*indices, *(p.material_index for p in obj.data.polygons)})
    return list(range(count))


def outline_needs_rebind(root) -> bool:
    """UI hint only: bounded ID/slot reads, no validation hash or evaluation."""
    from ..outline.outline_solidify import RECORD

    state = root.get(RECORD)
    if not state:
        return False
    if (not hasattr(state, "get") or state.get("schema") != 1
            or state.get("root") != root or state.get("mesh") != root.data):
        return True
    roles = state.get("roles", {})
    if not hasattr(roles, "values"):
        return True
    for row in roles.values():
        if not hasattr(row, "get"):
            return True
        start = row.get("slot_start", len(root.material_slots))
        materials = row.get("materials", {})
        if type(start) is not int or start < 0 or not hasattr(materials, "items"):
            return True
        for offset, material in materials.items():
            if not isinstance(offset, str) or not offset.isdecimal():
                return True
            index = start + int(offset)
            if index >= len(root.material_slots) or root.material_slots[index].material != material:
                return True
    return False


def request(scene, options) -> list[dict[str, Any]]:
    """Freeze actual slot identities in the submitting Operator, never draw."""
    from .assets import INSTANCE, selected_roots

    targets = []
    for root in selected_roots(scene, options.get("instances")):
        objects = _objects(root)
        if not objects:
            raise ValueError("实例中没有可独立处理的已登记物品表面：" + root.name)
        for obj in objects:
            targets.append({
                "instance": root[INSTANCE], "root_uid": root.session_uid,
                "object_uid": obj.session_uid, "mesh_uid": obj.data.session_uid,
                "base_count": _base_count(root, obj),
                "surface_indices": _surface_indices(root, obj),
                "slots": [{"link": slot.link,
                           "material_uid": slot.material.session_uid if slot.material else None,
                           "tree_uid": (slot.material.node_tree.session_uid
                                        if slot.material and slot.material.node_tree else None)}
                          for slot in obj.material_slots],
                "assigners": [{"modifier_uid": modifier.persistent_uid,
                               "group_uid": modifier.node_group.session_uid
                               if modifier.node_group else None}
                              for modifier in obj.modifiers if modifier.type == "NODES"],
            })
    return targets


def verify_targets(scene, options) -> None:
    frozen = options.get("material_targets")
    if not isinstance(frozen, list) or not frozen or frozen != request(scene, options):
        raise ValueError("确认后的物品网格或材质槽已改变，请重新执行操作")


def _check_object(scene, obj):
    if (obj.library or obj.override_library or obj.data.library or obj.data.override_library
            or tuple(obj.users_scene) != (scene,)):
        raise ValueError("材质操作需要当前场景中可编辑的独立物品对象：" + obj.name)


def _source_uv(root, obj, material):
    """Recover an old sampling identity, without editing or adopting the Mesh."""
    from ..enchantment.enchantment import SOURCE_UV

    name = material.get(SOURCE_UV)
    if name:
        if not isinstance(name, str) or obj.data.uv_layers.get(name) is None:
            raise ValueError("材质声明的附魔UV不存在，请先确认当前UV贴图：" + obj.name)
        return name
    identity = material.get(asset_geometry.TEXTURE_KEY)
    texture = material.node_tree.nodes.get("OXAL Source Texture") if material.node_tree else None
    image = texture.image if texture is not None and texture.type == "TEX_IMAGE" else None
    if not identity and image is None:
        return None

    def same_source(source):
        if identity:
            return source.get(asset_geometry.TEXTURE_KEY) == identity
        node = source.node_tree.nodes.get("OXAL Source Texture") if source.node_tree else None
        return node is not None and node.type == "TEX_IMAGE" and node.image == image

    candidates = {source.get(SOURCE_UV) for source in owned_ids(root)
                  if owns(root, source) and isinstance(source, bpy.types.Material)
                  and same_source(source) and source.get(SOURCE_UV)}
    candidates = {name for name in candidates if isinstance(name, str) and obj.data.uv_layers.get(name) is not None}
    if len(candidates) > 1:
        raise ValueError("同一物品贴图对应多种附魔UV，请先明确材质的UV身份：" + obj.name)
    return next(iter(candidates), None)


def _bind(transaction, obj, index, material):
    """Use an Object override; do not copy, adopt or write the user's Mesh."""
    slot = obj.material_slots[index]
    before_link, before_visible = slot.link, slot.material
    before_override = [before_visible]

    def restore():
        current = obj.material_slots[index]
        current.link = "OBJECT"
        current.material = before_override[0]
        current.link = before_link

    transaction.on_rollback(restore)
    slot.link = "OBJECT"
    before_override[0] = slot.material
    slot.material = material


def _remap_assigners(transaction, root, obj, replacements):
    """Private-copy proven native material assigners, never user GN graphs."""
    from ..enchantment import enchantment_outline as features

    for modifier in obj.modifiers:
        if modifier.type != "NODES" or modifier.node_group is None:
            continue
        group = modifier.node_group
        if any(node.bl_idname == "GeometryNodeGroup"
               or (node.bl_idname == "GeometryNodeSetMaterial" and node.inputs["Material"].is_linked)
               for node in group.nodes):
            raise ValueError("几何节点含嵌套或动态材质分配，无法确认当前表面，请先整理：" + modifier.name)
        assigners = [node for node in group.nodes if node.bl_idname == "GeometryNodeSetMaterial"]
        if not assigners:
            continue
        table = features._record(obj)
        row = table.get("geometry", {})
        if (row.get("name") != modifier.name or row.get("group") != group
                or row.get("owner") != root or group.get(features.ROLE) != "geometry"
                or group.get(features.OWNER) != root
                or not features.modifier_contract_matches(modifier, row.get("settings"))
                or not features.contract_matches(group)):
            raise ValueError("几何节点固定引用此材质，但其来源或节点已改变：" + modifier.name)
        require_owned(root, (obj, group))
        affected = [node for node in assigners if node.inputs["Material"].default_value in replacements]
        if not affected:
            continue
        if group.animation_data and (group.animation_data.action or group.animation_data.nla_tracks):
            raise ValueError("材质分配节点包含用户动画，未改写：" + modifier.name)
        if any(node.inputs["Material"].is_linked for node in affected):
            raise ValueError("几何节点动态选择材质，未覆盖：" + modifier.name)
        previous_group, previous_record = group, features._record(obj)

        def restore(modifier=modifier, group=previous_group, obj=obj, record=previous_record):
            modifier.node_group = group
            obj[features.RECORD] = record

        transaction.on_rollback(restore)
        private = transaction.track(group.copy())
        animation = private.animation_data
        for curve in animation.drivers if animation else ():
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id == group:
                        target.id = private
        for node in affected:
            private.nodes[node.name].inputs["Material"].default_value = replacements[node.inputs["Material"].default_value]
        private[features.CONTRACT] = features.contract(private, create=True)
        modifier.node_group = private
        table["geometry"]["group"] = private
        table["geometry"]["settings"] = features._modifier_contract(modifier)
        obj[features.RECORD] = table


def execute(scene, options, *, remove=False):
    """Add/remove a private material layer without requiring a pristine rig."""
    ensure_mutation_allowed()
    from ..enchantment import enchantment as service
    from ..outline import outline_solidify
    from .assets import _AddedProperties, _set_glint, members, selected_roots

    verify_targets(scene, options)
    roots = selected_roots(scene, options.get("instances"))
    prepared = []
    for root in roots:
        for obj in _objects(root):
            _check_object(scene, obj)
            require_owned(root, (obj,))
            count = _base_count(root, obj)
            if any(polygon.material_index >= count for polygon in obj.data.polygons):
                raise ValueError("当前网格的实际表面使用了旧描边尾槽，请先重新绑定描边：" + obj.name)
            for index in _surface_indices(root, obj):
                material = obj.material_slots[index].material
                if material is None or not material.use_nodes:
                    continue
                try:
                    info = service.inspect(material)
                except (ValueError, TypeError) as error:
                    raise ValueError(f"{obj.name}，材质槽 {index + 1}（{material.name}）：{error}") from error
                if info and info["version"] == service.VERSION and info["owner"] != root:
                    raise ValueError(f"{obj.name}，材质槽 {index + 1}：附魔控制器属于其它实例，未接管")
                if not remove or info is not None:
                    prepared.append((root, obj, index, material))
    if not prepared:
        if remove:
            return {"message": "所选物品没有材质附魔，无需取消；描边独立保留"}
        raise ValueError("所选物品没有可附魔的节点材质")

    warnings = []
    with asset_geometry.Creation() as transaction:
        # Validate the complete old shell before any material-slot edits. An
        # invalid shell stays untouched; it cannot authorize updates to receipts.
        publishers = []
        for root, obj in dict.fromkeys((root, obj) for root, obj, _, _ in prepared):
            if outline_solidify.RECORD in obj:
                try:
                    publish = outline_solidify.prepare_material_bindings(transaction, root, obj)
                except (ValueError, TypeError) as error:
                    reason = "当前网格已独立编辑" if not owns(root, obj.data) else str(error)
                    warnings.append(obj.name + "：" + reason)
                else:
                    publishers.append(publish)
        cache = {}
        changed = 0
        changes = []
        for root, obj, index, material in prepared:
            transaction.owner = root
            uv_name = None if remove else _source_uv(root, obj, material)
            cache_key = (root, material, uv_name)
            if cache_key not in cache:
                if remove:
                    cache[cache_key] = service.remove(transaction, root, material)
                else:
                    _AddedProperties(transaction).glint_defaults(root)
                    cache[cache_key] = _set_glint(transaction, root, material, uv_name=uv_name)
            private = cache[cache_key]
            if private != material:
                changes.append((root, obj, index, material, private))
        for root, obj in dict.fromkeys((root, obj) for root, obj, _, _, _ in changes):
            transaction.owner = root
            mapping = {old: new for item_root, item_obj, _, old, new in changes
                       if item_root == root and item_obj == obj}
            _remap_assigners(transaction, root, obj, mapping)
        for root, obj, index, _old, private in changes:
            transaction.owner = root
            _bind(transaction, obj, index, private)
            changed += 1
        for publish in publishers:
            publish()
        transaction.commit()

    ignored = sum(not owns(root, obj) for root in roots for obj in members(root))
    message = ("已取消材质附魔，保留模型、底材和独立描边" if remove
               else "已应用材质附魔，保留模型和附属物")
    if ignored:
        message += f"；未改动 {ignored} 个用户附属对象"
    if warnings:
        message += "；描边需显式重新绑定：" + "；".join(warnings)
    return {"message": message, "material_slots_changed": changed,
            "outline_rebind_required": bool(warnings)}
