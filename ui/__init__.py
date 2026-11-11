"""Blender panels and operators."""

from ..util.lifecycle import register_modules, unregister_modules
from . import (
    assets,
    common,
    human,
    instances,
    inventory,
    preferences,
    sidebar,
    sky,
    statusbar,
)

MODULES = (common, preferences, instances, sky, inventory, human, assets, sidebar, statusbar)


def register():
    register_modules(MODULES)


def unregister():
    unregister_modules(MODULES)
