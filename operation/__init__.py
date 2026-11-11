"""Blender entry points; business work belongs to bus handlers."""

from ..core import commands
from ..util.lifecycle import register_modules, unregister_modules
from . import assets, human, resources, sky

MODULES = (commands, sky, resources, human, assets)


def register() -> None:
    register_modules(MODULES)


def unregister() -> None:
    unregister_modules(MODULES)
