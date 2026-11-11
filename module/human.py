"""Register the independent human import capability."""

from typing import Any, cast

import bpy
from bpy.types import Scene, ViewLayer

from ..business.human.human import HumanResult, HumanTool, resolve_human
from ..core.commands import CommandEndpoint
from ..core.importing import ImportEndpoint
from ..core.runtime import ensure_mutation_allowed


def _recent_rows(state, scene, owner, paths=None):
    from pathlib import Path

    from ..business.human.human_skin_history import recent_paths

    state.recent_rows.clear()
    state.recent_scene_uid = str(scene.session_uid)
    state.recent_owner_uid = str(owner.session_uid)
    state.recent_error = ""
    try:
        values = recent_paths(owner) if paths is None else tuple(paths)
    except (ValueError, TypeError) as error:
        state.recent_error = str(error)
        state.recent_filepath = ""
        return
    for path in values:
        item = state.recent_rows.add()
        item.label, item.filepath = Path(path).name, path
    if state.recent_filepath not in values:
        state.recent_filepath = values[0] if values else ""


def _current_history(state, scene):
    from ..ui.instances import current_human

    try:
        root, legacy = current_human(bpy.context, state)
    except ValueError:
        root, legacy = None, None
    owner = legacy.rig if legacy is not None else root
    if owner is not None:
        _recent_rows(state, scene, owner)
    else:
        state.recent_rows.clear()
        state.recent_owner_uid = state.recent_filepath = ""


class HumanImportEndpoint(ImportEndpoint):
    def after_apply(self, result) -> None:
        ensure_mutation_allowed()
        from ..ui.instances import select_target

        scene = cast(Scene, bpy.context.scene)
        root = resolve_human(scene, result.instance_id)
        for manager in bpy.data.window_managers:
            state = getattr(manager, "oxal_mc_studio_human_ui", None)
            if state is not None:
                select_target(state, scene, root)
                _recent_rows(state, scene, root)


class HumanCommandEndpoint(CommandEndpoint[HumanResult]):
    def after_apply(self, result) -> None:
        if isinstance(result, dict) and "selected_human_rig_uid" in result:
            active = cast(ViewLayer, bpy.context.view_layer).objects.active
            if active is not None and active.session_uid == result["selected_human_rig_uid"]:
                for manager in bpy.data.window_managers:
                    state = getattr(manager, "oxal_mc_studio_human_ui", None)
                    if state is not None:
                        state.index = bpy.data.objects.find(active.name)
        if isinstance(result, dict) and "migrated_instance_id" in result:
            from ..ui.instances import select_target

            scene = cast(Scene, bpy.context.scene)
            root = resolve_human(scene, result["migrated_instance_id"])
            for manager in bpy.data.window_managers:
                state = getattr(manager, "oxal_mc_studio_human_ui", None)
                if state is not None:
                    select_target(state, scene, root)
                    _recent_rows(state, scene, root)
            return
        scene = cast(Scene, bpy.context.scene)
        for manager in bpy.data.window_managers:
            state = getattr(manager, "oxal_mc_studio_human_ui", None)
            if state is not None:
                _current_history(state, scene)
        if result != "人模操作完成：remove":
            return
        for manager in bpy.data.window_managers:
            state = getattr(manager, "oxal_mc_studio_human_ui", None)
            if state is not None and state.target is None:
                state.target_instance, state.target_scene_uid = "", ""

    def after_execute(self, result: HumanResult) -> None:
        ensure_mutation_allowed()
        if isinstance(result, dict) and result.get("red_popup") is True:
            from ..ui.instances import selected_legacy

            scene = cast(Scene, bpy.context.scene)
            if result.get("scene_uid") != scene.session_uid:
                return
            state = getattr(bpy.context.window_manager, "oxal_mc_studio_human_ui", None)
            if state is None:
                return
            try:
                target = selected_legacy(bpy.context, state)
            except ValueError:
                return
            if target is not None and target.rig.session_uid == result.get("red_prepared_rig_uid"):
                namespace = cast(Any, bpy.ops).oxal_mc_studio
                namespace.legacy_red_popup("INVOKE_DEFAULT", scene_uid=str(scene.session_uid),
                                           rig_uid=str(target.rig.session_uid))
            return
        if isinstance(result, dict) and "recent" in result:
            from ..ui.instances import current_human

            scene = cast(Scene, bpy.context.scene)
            if result.get("scene_uid") != scene.session_uid:
                return
            for manager in bpy.data.window_managers:
                state = getattr(manager, "oxal_mc_studio_human_ui", None)
                if state is None:
                    continue
                try:
                    root, legacy = current_human(bpy.context, state)
                except ValueError:
                    continue
                owner = legacy.rig if legacy is not None else root
                if owner is not None and owner.session_uid == result.get("owner_uid"):
                    _recent_rows(state, scene, owner, (row["filepath"] for row in result["recent"]))
            return
        if not isinstance(result, dict) or "library" not in result:
            return
        for manager in bpy.data.window_managers:
            state = getattr(manager, "oxal_mc_studio_human_ui", None)
            if state is None:
                continue
            state.library_kind = result["kind"]
            state.library_rows.clear()
            for item in result["library"]:
                row = state.library_rows.add()
                row.label, row.filepath = item["label"], item["filepath"]
            state.library_index = min(state.library_index, max(0, len(state.library_rows) - 1))
            if result["kind"] == "skins":
                paths = [row.filepath for row in state.library_rows]
                if state.skin_library_filepath not in paths:
                    state.skin_library_filepath = paths[0] if paths else ""

tool = HumanTool()
endpoint = HumanImportEndpoint("human.import", "oxal_mc_studio.apply_resource_import", tool.load, new_instance=True)
commands = HumanCommandEndpoint("human.edit", tool.edit, allowed_modes=("OBJECT", "POSE"))


def register() -> None:
    from ..business.human.human_compat import clear_catalog, prepare_catalog

    prepare_catalog()
    try:
        endpoint.register()
        try:
            commands.register()
        except Exception:
            endpoint.unregister()
            raise
    except Exception:
        clear_catalog()
        raise


def unregister() -> None:
    from ..business.human.human_compat import clear_catalog

    commands.unregister()
    endpoint.unregister()
    clear_catalog()
