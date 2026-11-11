"""Register sky types/capabilities; cached inputs activate only by explicit action."""

from typing import cast

import bpy
from bpy.types import Scene

from ..business.sky import sky_state
from ..business.sky.sky import SkyTool, input_activation_required
from ..core.commands import CommandEndpoint
from ..core.runtime import ensure_mutation_allowed
from ..util.resources import prepare_creation_options

tool = SkyTool()


def _load(scene_uid, options):
    return tool.load(scene_uid, options)


endpoint = CommandEndpoint("sky.load", _load)
endpoint.new_instance = True  # Freeze creation identity; reuse policy remains scene activation.


class SkyCommandEndpoint(CommandEndpoint):
    def submit(self, scene_uid, options):
        if options.get("action") == "load":
            options = prepare_creation_options(options)
        return super().submit(scene_uid, options)

    def after_execute(self, result):
        ensure_mutation_allowed()
        if not isinstance(result, dict) or "presets" not in result:
            return
        for manager in bpy.data.window_managers:
            state = getattr(manager, "oxal_mc_studio_sky_ui", None)
            if state is None:
                continue
            state.items.clear()
            for filename, label in result["presets"]:
                item = state.items.add()
                item.label, item.filename = label, filename
            state.index = min(state.index, max(0, len(state.items) - 1))


commands = SkyCommandEndpoint("sky.command", tool.command)


def busy(scene_uid=None):
    if scene_uid is None:
        scene_uid = cast(Scene, bpy.context.scene).session_uid
    return endpoint.busy(scene_uid) or commands.busy(scene_uid)


def needs_activation(scene):
    return input_activation_required(scene)


def mode_request(scene, intent):
    """Read-only preflight for a submission Operator's confirmation dialog."""
    from ..business.sky.sky_modes import request

    return request(scene, intent)


def quality_request(scene, quality):
    from ..business.sky.sky_quality import request

    return request(scene, quality)


def slot_request(scene, action):
    from ..business.sky.sky import slot_request as snapshot

    return snapshot(scene, action)


def preset_context(scene):
    from ..business.sky.sky_presets import context_snapshot

    return context_snapshot(scene)


def preset_request(scene, action, options):
    from ..business.sky.sky_presets import request

    return request(scene, action, options)


def register():
    sky_state.register()
    registered = []
    try:
        for item in (endpoint, commands):
            item.register()
            registered.append(item)
    except Exception:
        for item in reversed(registered):
            item.unregister()
        sky_state.unregister()
        raise


def unregister():
    try:
        commands.unregister()
        endpoint.unregister()
    finally:
        sky_state.unregister()
