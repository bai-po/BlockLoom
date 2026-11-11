"""Native collapsible sidebar panels for the four feature modules."""

from typing import ClassVar

import bpy
from bpy.types import Context, Panel

from ..core.runtime import mutation_allowed
from ..util.identity import DISPLAY_VERSION, PRODUCT_NAME
from . import assets, human, inventory, sky


class OXALMCSTUDIO_PT_sidebar(Panel):
    bl_idname = "OXALMCSTUDIO_PT_sidebar"
    bl_label = PRODUCT_NAME
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = PRODUCT_NAME
    bl_order = 0
    bl_options = {"HIDE_HEADER"}  # noqa: RUF012 -- Blender RNA class metadata.

    def draw(self, context: Context):
        if self.layout is not None:
            self.layout.label(text=f"{PRODUCT_NAME} v{DISPLAY_VERSION}")


class _SidebarPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = PRODUCT_NAME
    bl_options: ClassVar = {"DEFAULT_CLOSED"}

    def draw(self, context: Context):
        pass


class OXALMCSTUDIO_PT_sky(_SidebarPanel, Panel):
    bl_idname = "OXALMCSTUDIO_PT_sky"
    bl_label = "OXAL天空"
    bl_order = 20

    def draw(self, context: Context):
        if self.layout is not None:
            self.layout.enabled = mutation_allowed()
            sky.draw(self.layout, context)


class OXALMCSTUDIO_PT_inventory(_SidebarPanel, Panel):
    bl_idname = "OXALMCSTUDIO_PT_inventory"
    bl_label = "OXAL物品栏"
    bl_order = 30

    def draw(self, context: Context):
        if self.layout is not None:
            self.layout.enabled = mutation_allowed()
            inventory.draw(self.layout, context)


class OXALMCSTUDIO_PT_human(_SidebarPanel, Panel):
    bl_idname = "OXALMCSTUDIO_PT_human"
    bl_label = "白珀人模"
    bl_order = 10

    def draw(self, context: Context):
        if self.layout is not None:
            self.layout.enabled = mutation_allowed()
            human.draw(self.layout, context)


class OXALMCSTUDIO_PT_assets(_SidebarPanel, Panel):
    bl_idname = "OXALMCSTUDIO_PT_assets"
    bl_label = "OXAL资产库"
    bl_order = 40

    def draw(self, context: Context):
        if self.layout is not None:
            self.layout.enabled = mutation_allowed()
            assets.draw(self.layout, context)


CLASSES = (
    OXALMCSTUDIO_PT_sidebar,
    OXALMCSTUDIO_PT_sky,
    OXALMCSTUDIO_PT_inventory,
    OXALMCSTUDIO_PT_human,
    OXALMCSTUDIO_PT_assets,
)


def register():
    registered = []
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
    except Exception:
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        raise


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
