"""Precise, read-only inventory target resolution and native asset attachments."""

from dataclasses import dataclass
from typing import Any, cast

import bpy
from mathutils import Matrix

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import require_owned
from ...util.resources import ASSET_ID, INSTANCE
from .inventory_native import control_path

HOST = "oxal_inventory_host"
SLOT = "oxal_inventory_slot"
RIG = "oxal_inventory_binding_rig"


def quantity_path(slot):
    return '["副手物品数量"]' if str(slot) == "OFFHAND" else '["物品栏格' + str(slot) + '数量"]'


def roots(scene):
    return tuple(c for c in scene.collection.children_recursive if c.get(ASSET_ID) == "oxal.inventory" and c.get(INSTANCE))


def resolve_inventory(scene, instance_id):
    found = [root for root in roots(scene) if root.get(INSTANCE) == instance_id]
    if len(found) != 1:
        raise ValueError("物品栏已删除或实例身份不唯一")
    root = found[0]
    rigs = [obj for obj in root.all_objects if obj.type == "ARMATURE" and "物品栏控制器" in obj.pose.bones]
    if len(rigs) != 1:
        raise ValueError("物品栏缺少唯一原生骨架")
    rig = rigs[0]
    require_owned(root, (root, rig, rig.data))
    return root, rig


@dataclass(frozen=True)
class SlotTarget:
    root: Any
    rig: Any
    slot: str
    bone_name: str
    gui_bone: str
    hand_bone: str
    handedness: str
    view_mode: str
    gui_unit: float
    hand_unit: float


def snapshot_target(context, slot_ui=None):
    state = slot_ui or getattr(context.window_manager, "oxal_mc_studio_inventory_ui", None)
    candidates = roots(context.scene)
    explicit = getattr(state, "target", None) if state else None
    identity = str(getattr(state, "target_instance", "")) if state else ""
    if identity or explicit is not None:
        from ...ui.instances import selected_root
        root = selected_root(context, state, "oxal.inventory")
    else:
        active = context.object
        found = [root for root in candidates if active is not None and active in tuple(root.all_objects)]
        if len(found) != 1:
            raise ValueError("请先明确选择目标独立物品栏")
        root = found[0]
    root, rig = resolve_inventory(context.scene, str(root[INSTANCE]))
    slot = str(getattr(state, "slot", "CURRENT")) if state else "CURRENT"
    active_bone = context.active_pose_bone if context.mode == "POSE" and context.object == rig else None
    if active_bone:
        names = {str(i) + "号物品栏": str(i) for i in range(1, 10)}
        names["物品栏副手"] = "OFFHAND"
        if active_bone.name in names:
            slot = names[active_bone.name]
    if slot == "CURRENT":
        slot = str(min(9, max(1, round(-rig.pose.bones["物品栏控制器"].location.z * 8 / 2.25) + 1)))
    payload = {"inventory_instance_id": str(root[INSTANCE]), "rig_uid": str(rig.get("oxal_inventory_rig_id", "")), "rig_session_uid": str(rig.session_uid), "slot": slot, "context_mode": context.mode}
    resolve_target(context.scene, payload)
    return payload


def resolve_target(scene, snapshot):
    if not hasattr(snapshot, "get"):
        raise ValueError("物品栏目标快照无效")
    root, rig = resolve_inventory(scene, str(snapshot.get("inventory_instance_id", "")))
    # Runtime IDs change when native history replays an import. The concrete
    # Scene/root ownership registry and unique authored rig contract authorize
    # this binding, never a stale runtime UID or a copied UUID alone.
    slot = str(snapshot.get("slot", ""))
    if slot not in {*(str(i) for i in range(1, 10)), "OFFHAND"}:
        raise ValueError("物品栏目标格子无效")
    bone = "物品栏副手" if slot == "OFFHAND" else slot + "号物品栏"
    required = {*(str(i) + "号物品栏" for i in range(1, 10)), "物品栏副手", "物品栏控制器", "物品栏左手", "物品栏右手"}
    if required - set(rig.data.bones.keys()):
        raise ValueError("物品栏原生格子骨骼不完整")
    if quantity_path(slot)[2:-2] not in rig:
        raise ValueError("物品栏原生数量通道缺失")
    pitch = (rig.data.bones["2号物品栏"].head_local - rig.data.bones["1号物品栏"].head_local).length
    if not .1 < pitch < 1:
        raise ValueError("物品栏源格子间距不符合契约")
    return SlotTarget(root, rig, slot, bone, bone, "物品栏左手" if slot == "OFFHAND" else "物品栏右手", "LEFT" if slot == "OFFHAND" else "RIGHT", str(root.get("独立视角", "THIRD_PERSON")), pitch * 16 / 20, .05 / 1.4)


@dataclass
class BindingChange:
    rig: Any
    quantity: str
    old_quantity: Any
    bound: str
    old_bound: Any
    previous: tuple
    old_offhand: Any = None

    def rollback(self):
        self.rig[self.quantity] = self.old_quantity
        self.rig[self.bound] = self.old_bound
        if self.old_offhand is not None:
            self.rig["副手开关"] = self.old_offhand
        self.rig.update_tag()

    def finalize(self):
        # The displaced source remains an independently owned, editable asset.
        # Only its exact inventory relationship is retired after publication.
        for root in self.previous:
            root["oxal_inventory_binding_active"] = False
            if root.get(HOST) is not None:
                del root[HOST]
            if SLOT in root:
                del root[SLOT]
            root.update_tag()


def bind_source(transaction, source_root, snapshot, quantity=1, replace=False, gui_representation=None, held_representation=None):
    ensure_mutation_allowed()
    from .inventory import _driver
    target = resolve_target(bpy.context.scene, snapshot)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 64:
        raise ValueError("格子物品数量必须为1到64的整数")
    if source_root not in transaction.ids:
        raise ValueError("只可绑定本次创建且归属明确的资产")
    if gui_representation is None or gui_representation not in transaction.ids:
        raise ValueError("格子物品缺少本次创建的GUI表示")
    previous = tuple(obj for obj in cast(Any, bpy.context.scene).objects if obj.get(ASSET_ID) == "oxal.library" and obj.get(HOST) == target.root and obj.get(SLOT) == target.slot and obj != source_root)
    if previous and not replace:
        raise ValueError("目标格子已被占用，请明确选择替换")
    for old in previous:
        require_owned(old, (old,))
    if "oxal_inventory_bound_" + target.slot not in target.rig:
        raise ValueError("旧版物品栏尚未准备格子绑定，请先显式修复实例")
    qname = quantity_path(target.slot)[2:-2]
    bname = "oxal_inventory_bound_" + target.slot
    change = BindingChange(target.rig, qname, target.rig[qname], bname, target.rig[bname], previous)
    if target.slot == "OFFHAND":
        change.old_offhand = target.rig["副手开关"]
    transaction.on_rollback(change.rollback)
    source_root[HOST], source_root[SLOT], source_root[RIG] = target.root, target.slot, target.rig
    source_root["oxal_inventory_binding_schema"] = 1
    source_root["oxal_inventory_binding_active"] = True
    source_root["oxal_inventory_gui"] = gui_representation
    if held_representation:
        source_root["oxal_inventory_held"] = held_representation
    def anchor(pivot, bone, gui):
        frame = transaction.track(bpy.data.objects.new("格子GUI锚点" if gui else "第一人称持物锚点", None))
        collection = source_root.users_collection[0]
        collection.objects.link(frame)
        frame.parent = source_root
        frame.matrix_parent_inverse = Matrix.Identity(4)
        constraint = frame.constraints.new("COPY_TRANSFORMS")
        constraint.name = "OXAL · 原生物品栏绑定"
        constraint.target, constraint.subtarget = target.rig, bone
        constraint.owner_space = constraint.target_space = "WORLD"
        original = pivot.matrix_basis.copy()
        pivot.parent = frame
        pivot.matrix_parent_inverse = Matrix.Identity(4)
        if gui:
            from .inventory_gui import canvas_matrix, prepare
            prepare(transaction, bpy.context.scene, source_root, pivot, target)
            pivot.matrix_basis = canvas_matrix(target, pivot)
        else:
            from .inventory_projection import held_matrix
            pivot.matrix_basis = held_matrix(source_root,pivot,original,target)
        meshes = tuple(obj for obj in (pivot, *pivot.children_recursive) if obj.type == "MESH")
        for obj in meshes:
            variables = [("count", target.rig, quantity_path(target.slot)), ("active", source_root, '["oxal_inventory_binding_active"]')]
            expression = "not active or count<=0"
            if target.slot == "OFFHAND":
                variables.append(("offhand", target.rig, '["副手开关"]'))
                expression += " or not offhand"
            if not gui:
                variables += [("slot", target.rig, '["oxal_inventory_selected_slot"]'), ("enabled", target.rig, control_path(target.rig,"oxal_inventory_hands_enabled"))]
                expression += " or not enabled" + (" or slot!=" + target.slot if target.slot != "OFFHAND" else "")
            for path in ("hide_viewport", "hide_render"):
                _driver(obj, path, expression, variables)
    anchor(gui_representation, target.gui_bone, True)
    if held_representation is not None and target.view_mode == "FIRST_PERSON":
        anchor(held_representation, target.hand_bone, False)
        from .inventory_projection import radial_depth
        radial_depth(transaction,source_root,held_representation,target)
    target.rig[qname] = quantity
    target.rig[bname] = True
    if target.slot == "OFFHAND":
        target.rig["副手开关"] = True
    source_root.update_tag()
    target.rig.update_tag()
    from .inventory_helpers import configure
    configure(transaction, bpy.context.scene, source_root)
    return change


attach_asset = bind_source
