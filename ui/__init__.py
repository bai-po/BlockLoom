"""Blender panels and operators."""

from ..util.lifecycle import register_modules, unregister_modules
from . import (
    asset_rail,
    assets,
    common,
    human,
    instances,
    inventory,
    preferences,
    sidebar,
    sky,
    statusbar,
    workbench,
)

MODULES = (common, preferences, instances, sky, inventory, human, asset_rail, assets, sidebar, statusbar, workbench)


def register():
    register_modules(MODULES)


def unregister():
    unregister_modules(MODULES)
