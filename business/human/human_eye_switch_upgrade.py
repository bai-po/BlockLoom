"""Migrate the original facial switches and their bound or stashed native animation."""

import bpy

from ...util.animation import action_fcurves
from ...util.animation_edit import _nla

OLD_MASTER = "眉纹切换"
LEGACY_INPUT = "bp_face_legacy_eye_input"
NEW_NAMES = {side + ".眉纹切换": side + ".眼睛表情切换" for side in ("L", "R")}


def _new_path(path):
    master = 'pose.bones["' + OLD_MASTER + '"]'
    if path.startswith(master):
        if path == master + ".location" or path.startswith(master + ".location["):
            return path.replace(master + ".location", '["' + LEGACY_INPUT + '"]', 1)
        raise ValueError("旧眉纹控制器存在自定义动画，请先重接：" + path)
    for old, new in NEW_NAMES.items():
        token = 'pose.bones["' + old + '"]'
        if path.startswith(token):
            return path.replace(token, 'pose.bones["' + new + '"]', 1)
    return path


def _assign(binding, action, slot_id):
    binding.action = action
    if slot_id:
        binding.action_slot = next(s for s in action.slots if s.identifier == slot_id)


def _paths(transaction, rig):
    anim = rig.animation_data
    bindings = [b for b in (anim, *_nla(anim)) if b and b.action]
    if anim and anim.use_tweak_mode:
        raise ValueError("请先退出 NLA 调整模式再更新眼控")
    by_action = {}
    for b in bindings:
        by_action.setdefault(b.action, []).append(b)
    identifiers = {
        b.action_slot.identifier for b in bindings if getattr(b, "action_slot", None)
    }
    identifiers.add("OB" + rig.name)
    if anim and getattr(anim, "last_slot_identifier", ""):
        identifiers.add(anim.last_slot_identifier)
    for action in tuple(bpy.data.actions):
        bound = by_action.get(action, [])
        slots = tuple(action.slots) or (None,)
        matching = [
            s
            for s in slots
            if (
                any(getattr(b, "action_slot", None) == s for b in bound)
                or (s and not s.users() and s.identifier in identifiers)
            )
        ]
        changes = [
            (s, fc.data_path, _new_path(fc.data_path), fc.array_index)
            for s in matching
            for fc in action_fcurves(action, slot=s)
            if fc.data_path.startswith("pose.bones[")
            and _new_path(fc.data_path) != fc.data_path
        ]
        if not changes:
            continue
        users = bpy.data.user_map(subset=(action,)).get(action, set())
        # Detached clips have no definitive owner. Keep their original for old
        # rigs, alongside a native revised copy that can be assigned explicitly.
        private = (
            not bound
            or bool(set(users) - {rig})
            or action.library
            or action.override_library
        )
        dest = action
        if private:
            dest = transaction.track(action.copy())
            dest.name = action.name + " · 新眼控"
            if not bound:
                dest.use_fake_user = True
            for b in bound:
                slot = getattr(b, "action_slot", None)
                ident = slot.identifier if slot else None
                transaction.on_rollback(lambda b=b, a=action, i=ident: _assign(b, a, i))
                _assign(b, dest, ident)
        for slot, old, new, index in changes:
            target_slot = (
                next(s for s in dest.slots if s.identifier == slot.identifier)
                if slot
                else None
            )
            (fc,) = [
                c
                for c in action_fcurves(dest, slot=target_slot)
                if c.data_path == old and c.array_index == index
            ]
            transaction.on_rollback(lambda c=fc, p=old: setattr(c, "data_path", p))
            fc.data_path = new
    if anim:
        for fc in anim.drivers:
            path = _new_path(fc.data_path)
            if path != fc.data_path:
                transaction.on_rollback(
                    lambda c=fc, p=fc.data_path: setattr(c, "data_path", p)
                )
                fc.data_path = path


def _master_channels(transaction, rig):
    old = rig.pose.bones[OLD_MASTER]
    if LEGACY_INPUT in rig:
        raise ValueError("旧眉纹动画属性名已占用：" + rig.name)
    rig[LEGACY_INPUT] = list(old.location)
    transaction.on_rollback(lambda: rig.__delitem__(LEGACY_INPUT))
    if old.children or old.bone.use_deform:
        raise ValueError("旧眉纹控制器有关联子骨或变形绑定：" + old.name)
    ids = bpy.data.user_map(subset=(rig,)).get(rig, set()) | {rig, rig.data}
    for owner in ids:
        if isinstance(owner, bpy.types.Object):
            constraints = list(owner.constraints)
            if owner.pose:
                constraints.extend(c for pb in owner.pose.bones for c in pb.constraints)
            if (
                owner.parent == rig
                and owner.parent_type == "BONE"
                and owner.parent_bone == OLD_MASTER
            ) or any(
                getattr(c, "target", None) == rig
                and getattr(c, "subtarget", "") == OLD_MASTER
                for c in constraints
            ):
                raise ValueError("请先重接引用旧眉纹的自定义骨骼绑定：" + owner.name)
        for owner_data in (owner, getattr(owner, "node_tree", None)):
            animation = getattr(owner_data, "animation_data", None)
            for fc in animation.drivers if animation else ():
                for var in fc.driver.variables:
                    target = var.targets[0]
                    if target.id != rig:
                        continue
                    if var.type == "SINGLE_PROP":
                        path = _new_path(target.data_path)
                        if path != target.data_path:
                            transaction.on_rollback(
                                lambda t=target, p=target.data_path: setattr(
                                    t, "data_path", p
                                )
                            )
                            target.data_path = path
                    elif any(
                        t.id == rig and t.bone_target == OLD_MASTER for t in var.targets
                    ):
                        if (
                            var.type,
                            target.transform_type,
                            target.transform_space,
                        ) != ("TRANSFORMS", "LOC_Z", "LOCAL_SPACE"):
                            raise ValueError(
                                "旧眉纹控制器有自定义变换驱动："
                                + owner.name
                                + " / "
                                + fc.data_path
                            )

                        def restore(v=var):
                            v.type = "TRANSFORMS"
                            t = v.targets[0]
                            t.id, t.bone_target = rig, OLD_MASTER
                            t.transform_type, t.transform_space = "LOC_Z", "LOCAL_SPACE"

                        transaction.on_rollback(restore)
                        var.type = "SINGLE_PROP"
                        target = var.targets[0]
                        target.id, target.data_path = rig, '["' + LEGACY_INPUT + '"][2]'


def rename(transaction, rig, brow_switch):
    mapping = {**NEW_NAMES, OLD_MASTER: brow_switch}
    for old, new in mapping.items():
        if old not in rig.pose.bones or new in rig.pose.bones:
            raise ValueError("待迁移的原生眼部控制骨重名或缺失：" + old + " → " + new)
    _master_channels(transaction, rig)
    _paths(transaction, rig)
    for old, new in mapping.items():
        # Edit-mode rebuilds Armature.bones, invalidating cached Bone pointers.
        transaction.on_rollback(
            lambda src=new, dst=old: setattr(rig.data.bones[src], "name", dst)
        )
        rig.data.bones[old].name = new
