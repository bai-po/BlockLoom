"""Replace only the selected creature's actual, independently appended components."""

from array import array
from contextlib import ExitStack
from hashlib import blake2b
from pathlib import Path

import bpy

from ...util.animation_edit import require_property_edit
from ...util.ownership import (
    REGISTRY,
    prune_owned,
    require_no_external_users,
    require_owned,
    seal_owned,
)
from ...util.resources import AppendTransaction

LIBRARY = (
    Path(__file__).resolve().parents[2] / "asset" / "creature" / "creatures_0_1_2.blend"
)
COMPONENT = "blockloom_component"
SOCKET = "blockloom_socket_id"
ARMOR = {"HEAD", "CHEST", "LEGS", "FEET"}
RIG_FIELDS = {
    "arm_mode": "blockloom_slim",
    "leg_mode": "blockloom_official",
    "player_arms": "oxal_humanoid_player_arms",
    "name": "blockloom_name",
}


def use_double_sided_materials(transaction):
    # Blender 5.x can restore light-probe culling while converting a 4.5 material.
    # Touch only IDs actually created by this library append.
    for material in transaction.owned:
        if isinstance(material, bpy.types.Material):
            material.use_backface_culling = False
            material.use_backface_culling_shadow = False
            material.use_backface_culling_lightprobe_volume = False


def _components(root):
    pairs = [(c[COMPONENT], c) for c in root.children if COMPONENT in c]
    if len(dict(pairs)) != len(pairs):
        raise ValueError("生物实例有重复的部件合集")
    return dict(pairs)


def option_value(root, rig, field):
    parts = _components(root)
    if field == "arm_mode":
        return (
            "SLIM"
            if "SLIM" in parts or ("FOLDED" in parts and rig.get("blockloom_slim"))
            else "WIDE"
        )
    if field == "leg_mode":
        return "OFFICIAL" if "OFFICIAL" in parts else "NORMAL"
    if field == "player_arms":
        return "FOLDED" not in parts
    if field == "name":
        return "NAME" in parts
    if field.removeprefix("armor_") in ARMOR and field.startswith("armor_"):
        return field[6:] in parts
    raise ValueError("未知生物部件")


def _desired(root, rig, parts, field, value):
    desired = set(parts)
    if field in {"arm_mode", "player_arms"}:
        if field == "player_arms":
            if type(value) is not bool or not rig.get("oxal_humanoid_can_fold"):
                raise ValueError("该生物不支持当前手臂姿势")
            target = option_value(root, rig, "arm_mode") if value else "FOLDED"
        else:
            if value not in ("WIDE", "SLIM"):
                raise ValueError("未知生物手臂宽度")
            target = value
        desired.difference_update(("WIDE", "SLIM", "FOLDED"))
        desired.add(target)
    elif field == "leg_mode":
        if value not in ("NORMAL", "OFFICIAL"):
            raise ValueError("未知生物腿型")
        desired.difference_update(("NORMAL", "OFFICIAL"))
        desired.add(value)
    elif field == "armor_ALL":
        if type(value) is not bool or not rig.get("oxal_humanoid_armor_supported"):
            raise ValueError("当前生物不支持盔甲")
        if value:
            desired.update(ARMOR)
        else:
            desired.difference_update(ARMOR)
    else:
        token = "NAME" if field == "name" else field.removeprefix("armor_")
        if type(value) is not bool or (
            token != "NAME"
            and (
                field != "armor_" + token
                or token not in ARMOR
                or not rig.get("oxal_humanoid_armor_supported")
            )
        ):
            raise ValueError("不支持给该生物添加所选部件")
        if value:
            desired.add(token)
        else:
            desired.discard(token)
    desired.difference_update(("CHEST_FOLDED", "CHEST_PLAYER"))
    if "CHEST" in desired and rig.get("oxal_humanoid_can_fold"):
        desired.add("CHEST_FOLDED" if "FOLDED" in desired else "CHEST_PLAYER")
    return desired


def mesh_signature(mesh):
    """Fingerprint authored base geometry so replacing a limb cannot discard modelling edits."""
    digest = blake2b(digest_size=16)

    def capture(domain, field, width, kind):
        values = array(kind, [0]) * (len(domain) * width)
        domain.foreach_get(field, values)
        digest.update(values)

    capture(mesh.vertices, "co", 3, "f")
    capture(mesh.loops, "vertex_index", 1, "i")
    capture(mesh.polygons, "loop_start", 1, "i")
    for layer in mesh.uv_layers:
        capture(layer.uv, "vector", 2, "f")
    if mesh.shape_keys:
        for block in mesh.shape_keys.key_blocks:
            digest.update(block.name.encode("utf8"))
            capture(block.data, "co", 3, "f")
    return digest.hexdigest()


def _preflight(scene, root, outgoing):
    objects = {o for group in outgoing for o in group.objects}
    if any(
        len(group.objects) != group.get("blockloom_part_count") or group.children
        for group in outgoing
    ):
        raise ValueError("待替换的生物部件合集已修改，请先检查合集成员")
    require_owned(root, (*outgoing, *objects, *(obj.data for obj in objects)))
    require_no_external_users(scene, root, (*outgoing, *objects))
    users = bpy.data.user_map(subset=tuple(objects))
    for obj in objects:
        if obj.type == "MESH" and obj.get("blockloom_mesh_signature") != mesh_signature(
            obj.data
        ):
            raise ValueError("待替换部件的网格或 UV 已编辑：" + obj.name)
        if set(obj.users_collection) - outgoing or users[obj] - {
            root,
            scene,
            *outgoing,
            *objects,
        }:
            raise ValueError("其它对象或合集仍引用生物部件：" + obj.name)
        for owner in (obj, obj.data, getattr(obj.data, "shape_keys", None)):
            anim = getattr(owner, "animation_data", None)
            if anim and (anim.action or anim.nla_tracks):
                raise ValueError("待替换的部件有独立动画：" + obj.name)
    return objects


def name_component(group):
    labels = {"WIDE": "四格手臂", "SLIM": "三格手臂", "FOLDED": "交叉手臂",
              "NORMAL": "常规腿型", "OFFICIAL": "官方动画腿型", "HEAD": "头盔",
              "CHEST": "胸甲", "LEGS": "护腿", "FEET": "靴子", "NAME": "名字",
              "CHEST_PLAYER": "展开手臂胸甲", "CHEST_FOLDED": "交叉手臂胸甲"}
    if label := labels.get(group.get(COMPONENT)):
        group.name = label


def _append(transaction, root, key, token):
    group = transaction.append_collection("BL." + key + "." + token)
    use_double_sided_materials(transaction)
    if (
        group.get(COMPONENT) != token
        or not group.objects
        or len(group.objects) != group.get("blockloom_part_count")
    ):
        raise ValueError("生物部件资源缺失或不完整")
    members = set(group.objects)
    sockets = {obj[SOCKET]: obj for obj in root.objects if SOCKET in obj}
    # Blender 4.5 does not attach library_weak_reference to Object IDs. A local Geometry
    # Nodes Object Info socket is also a native source reference to its deformation cage.
    # Walk that specific authored edge while the append still proves which IDs are new.
    for obj in members:
        for mod in obj.modifiers:
            if mod.type != "NODES" or mod.node_group is None:
                continue
            for node in mod.node_group.nodes:
                for socket in node.inputs:
                    ref = getattr(socket, "default_value", None)
                    if not isinstance(ref, bpy.types.Object):
                        continue
                    target = sockets.get(ref.get(SOCKET))
                    if target is None:
                        raise ValueError("几何节点缺少实例中的变形参照")
                    if ref == target:
                        continue
                    if mod.node_group not in transaction.owned:
                        raise ValueError("待绑定的变形节点组正在由其它数据共用")
                    for dependency in (
                        ref,
                        *(getattr(m, "object", None) for m in ref.modifiers),
                    ):
                        if (
                            dependency is not None
                            and dependency not in transaction.owned
                            and dependency not in sockets.values()
                        ):
                            transaction.track(dependency)
                    socket.default_value = target
    dependencies = {
        obj
        for obj in transaction.owned
        if isinstance(obj, bpy.types.Object) and obj not in members
    }
    for obj in dependencies:
        if SOCKET in obj:
            target = sockets.get(obj[SOCKET])
            if target is None or target.type != obj.type:
                raise ValueError("生物实例缺少骨架或变形晶格")
            require_owned(root, (target,))
            obj.user_remap(target)
    # Custom bone shapes belong to the discarded template rig. Their existing instance counterparts
    # stay intact; only this append's proven native IDs can be retired.
    transaction.remove(dependencies)
    name_component(group)
    return group


def apply_option(scene, root, rig, field: str, value):
    if bpy.context.mode != "OBJECT":
        raise ValueError("请先进入物体模式再替换生物部件")
    require_owned(root, (root, rig, rig.data))
    current = _components(root)
    desired = _desired(root, rig, current, field, value)
    if desired == set(current):
        return
    prop = RIG_FIELDS[field] if field in RIG_FIELDS else "oxal_humanoid_" + field
    changed = {"oxal_humanoid_armor_" + part: value for part in sorted(ARMOR)} if field == "armor_ALL" else {prop: value}
    if field == "arm_mode":
        changed[prop] = value == "SLIM"
        if rig.get("oxal_humanoid_can_fold"):
            changed[RIG_FIELDS["player_arms"]] = True
    elif field == "leg_mode":
        changed[prop] = value == "OFFICIAL"
    for name in changed:
        require_property_edit(rig, '["' + name + '"]')
    outgoing = {group for label, group in current.items() if label not in desired}
    retiring = outgoing | _preflight(scene, root, outgoing)
    with ExitStack() as stack:
        staged = []
        for token in sorted(desired - current.keys()):
            txn = stack.enter_context(AppendTransaction(LIBRARY))
            group = _append(txn, root, root["blockloom_creature_key"], token)
            staged.append((txn, group))
        old_parts = {
            obj.get("oxal_humanoid_part"): obj
            for group in outgoing
            for obj in group.objects
            if obj.get("oxal_humanoid_part")
        }
        for _, group in staged:
            for obj in group.objects:
                old = old_parts.get(obj.get("oxal_humanoid_part"))
                if old is not None and old.type == "MESH" and obj.type == "MESH":
                    # The native 3/4-grid and two leg templates use the same material-slot contract.
                    if len(old.material_slots) != len(obj.material_slots):
                        raise ValueError("原部件材质槽已修改：" + old.name)
                    for new_slot, old_slot in zip(
                        obj.material_slots, old.material_slots
                    ):
                        new_slot.material = old_slot.material
        previous = {key: rig.get(key) for key in changed}
        registry = dict(root[REGISTRY].items())
        try:
            for txn, group in staged:
                root.children.link(group)
                txn.prune_unused()
                txn.prepare_commit()
                seal_owned(root, txn.owned, scene=scene)
            for key, state in changed.items():
                rig[key] = state
            rig.update_tag()
            table = root[REGISTRY]
            for key, member in tuple(table.items()):
                if member in retiring:
                    del table[key]
            if retiring:
                bpy.data.batch_remove(ids=tuple(retiring))
            for txn, _ in staged:
                txn.finish_commit()
        except Exception:
            root[REGISTRY] = registry
            for key, state in previous.items():
                if state is None:
                    rig.pop(key, None)
                else:
                    rig[key] = state
            rig.update_tag()
            raise
    prune_owned(root)
