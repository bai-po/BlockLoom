"""Blender add-on entry point, with native user add-on installation metadata."""

bl_info = {
    "name": "BlockLoom",
    "author": "白珀、空树林",
    "version": (0, 1, 40),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > BlockLoom",
    "description": "Minecraft 创作工具：人模、天空、物品栏、资产库与材质包",
    "doc_url": "https://oxal.baipo.fun/",
    "category": "3D View",
}

import logging
from typing import cast

from . import module as modules
from .core import runtime

logger = logging.getLogger(__name__)


def _cleanup(*, modules_ready):
    actions = [("runtime.stop", runtime.stop)]
    if modules_ready:
        actions.append(("modules.unregister", modules.unregister))
    actions.append(("runtime.unregister", runtime.unregister))
    errors = []
    for label, cleanup in actions:
        try:
            cleanup()
        except Exception as error:
            logger.exception("OXAL cleanup failed: %s", label)
            error.add_note("OXAL cleanup stage: " + label)
            errors.append(error)
    return errors


def register():
    import bpy

    from .util.identity import EXTENSION_ID
    from .util.user_storage import reset_cache

    preferences = cast(bpy.types.Preferences, bpy.context.preferences)
    conflicts = [addon.module for addon in preferences.addons
                 if addon.module != __package__ and (addon.module == EXTENSION_ID
                 or (addon.module.startswith("bl_ext.") and addon.module.rsplit(".", 1)[-1] == EXTENSION_ID))]
    if conflicts:
        raise RuntimeError("已有 BlockLoom 正在启用。请先停用并卸载旧条目，再启用此版本，避免重复登记。")
    runtime.register()
    modules_ready = False
    try:
        reset_cache()
        modules.register()
        modules_ready = True
        runtime.start()
    except Exception as error:
        for cleanup_error in _cleanup(modules_ready=modules_ready):
            error.add_note(f"Additional cleanup failure: {type(cleanup_error).__name__}: {cleanup_error}")
        raise


def unregister():
    if not runtime.mutation_allowed():
        runtime.request_shutdown(_unregister_now)
        return
    _unregister_now()


def _unregister_now():
    errors = _cleanup(modules_ready=True)
    if errors:
        raise ExceptionGroup("OXAL unregister failed", errors)
