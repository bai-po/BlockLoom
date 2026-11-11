"""Read-only import targets authorized by the current concrete selection."""

from types import SimpleNamespace

from ...util.ownership import require_owned
from ...util.resources import INSTANCE


def selected_targets(context):
    """Never use a remembered sidebar index as evidence of a selected host."""
    scene, active = getattr(context, "scene", None), getattr(context, "object", None)
    if scene is None or active is None or active not in getattr(context, "selected_objects", ()):
        return {}
    if scene.objects.get(active.name) != active:
        return {}
    result = {}
    from ..inventory import inventory_slots
    inventories = [root for root in inventory_slots.roots(scene) if active in tuple(root.all_objects)]
    if len(inventories) == 1:
        try:
            root, rig = inventory_slots.resolve_inventory(scene, str(inventories[0][INSTANCE]))
            require_owned(root, (active, rig, rig.data))
            state = getattr(context.window_manager, "oxal_mc_studio_inventory_ui", None)
            # Slot choice is a value; the current owned selection supplies its
            # container identity, irrespective of the remembered UI target.
            slot_ui = SimpleNamespace(slot=getattr(state, "slot", "CURRENT"), target=None, target_instance="")
            result["SLOT"] = inventory_slots.snapshot_target(context, slot_ui)
        except ValueError:
            pass
    from .assets import _display_target, find_root, root_of
    root = root_of(active)
    if root is not None and root.get("oxal_mc_studio_display_slots"):
        try:
            if find_root(scene, str(root[INSTANCE])) != root:
                return result
            require_owned(root, (active,))
            state = getattr(context.window_manager, "oxal_mc_studio_assets_ui", None)
            slot = int(getattr(state, "display_slot", "1") or "1")
            bone = context.active_pose_bone if context.mode == "POSE" and context.object == root else None
            if bone is not None:
                names = {"展示 · 展示物品": 1, "展示 · 左格": 1, "展示 · 中格": 2, "展示 · 右格": 3}
                slot = names.get(bone.name, slot)
            payload = {"display_target": str(root[INSTANCE]), "display_slot": slot}
            _display_target(scene, payload)
            result["DISPLAY"] = payload
        except (ValueError, TypeError):
            pass
    return result
