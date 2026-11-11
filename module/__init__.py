"""Business module registration."""

from .. import operation as operators
from .. import ui
from ..util.lifecycle import register_modules, unregister_modules
from . import assets, human, inventory, sky

MODULES = (sky, human, inventory, assets, operators, ui)


def register():
    register_modules(MODULES)


def unregister():
    unregister_modules(MODULES)
