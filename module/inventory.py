"""Register the independent inventory import capability."""

from typing import cast

import bpy
from bpy.types import Scene

from ..business.inventory.inventory import InventoryTool
from ..business.inventory.inventory_edit import InventoryCommands
from ..core.commands import CommandEndpoint
from ..core.importing import ImportEndpoint
from ..core.runtime import ensure_mutation_allowed


class InventoryImportEndpoint(ImportEndpoint):
    def after_apply(self, result) -> None:
        ensure_mutation_allowed()
        from ..business.inventory.inventory_slots import resolve_inventory
        from ..ui.instances import select_target

        scene = cast(Scene, bpy.context.scene)
        root, _rig = resolve_inventory(scene, result.instance_id)
        for manager in bpy.data.window_managers:
            state = getattr(manager, "oxal_mc_studio_inventory_ui", None)
            if state is not None:
                select_target(state, scene, root)

class InventoryCommandEndpoint(CommandEndpoint):
    def after_apply(self, result) -> None:
        if result != "已移除独立物品栏":
            return
        for manager in bpy.data.window_managers:
            state = getattr(manager, "oxal_mc_studio_inventory_ui", None)
            if state is not None and state.target is None:
                state.target_instance, state.target_scene_uid = "", ""


tool = InventoryTool()
endpoint = InventoryImportEndpoint(
    "inventory.import", "oxal_mc_studio.apply_resource_import", tool.load, new_instance=True
)
commands = InventoryCommandEndpoint("inventory.edit", InventoryCommands().edit)


def register() -> None:
    endpoint.register()
    try:
        commands.register()
    except Exception:
        endpoint.unregister()
        raise


def unregister() -> None:
    commands.unregister()
    endpoint.unregister()
