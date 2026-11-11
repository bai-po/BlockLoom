"""Explicit construction hooks for independently owned native asset families."""

from functools import cache
from importlib import import_module

MODULES = ("asset_lightning","asset_potions","asset_particles","asset_environment","asset_poses","asset_banners","asset_projectiles","asset_book","asset_beacon","asset_crystal")


@cache
def _module(name):
    qualified = f"{__package__}.{name}"
    return import_module(qualified)


def prepare():
    """Load only fixed Python capabilities during an explicit catalog request."""
    for name in MODULES:
        _module(name)
    _module("asset_surfaces")
    _module("asset_arrow_bake")


def particles_module(root):
    identifier=root.get("oxal_mc_studio_library_identifier","")
    if identifier == "minecraft:brewing_stand":return _module("asset_brewing")
    if identifier in {"minecraft:"+name for name in ("torch","wall_torch","soul_torch","soul_wall_torch","copper_torch","copper_wall_torch")}:
        return _module("asset_torch_particles")
    if root.get("oxal_mc_studio_native_playback_clock"):
        return _module("asset_playback")
    return None


def requires_rig(identifier):
    return any(getattr(_module(name),"requires_rig",lambda _:False)(identifier) for name in MODULES)


def build_entry(transaction,scene,collection,root,identifier,options,material_for):
    for name in MODULES:
        module = _module(name)
        function = getattr(module,"build_entry",None)
        if function and function(transaction,scene,collection,root,identifier,options,material_for):
            return True
    return False


def build(transaction,scene,collection,root,identifier,options,material_for):
    for name in MODULES:
        function = getattr(_module(name),"build",None)
        if function:
            function(transaction,scene,collection,root,identifier,options,material_for)


def palette(transaction,scene,collection,root,identifier,options,material_for):
    function = getattr(_module("asset_potions"),"apply_palette",None)
    if function:
        function(transaction,scene,collection,root,identifier,options,material_for)


def post_compile(root,scene,options):
    for name in MODULES:
        function = getattr(_module(name),"post_compile",None)
        if function:
            function(root,scene,options)
