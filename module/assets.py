"""Asset capability registration and explicit presentation publication."""

import json
from typing import Any, cast

import bpy
import bpy.utils.previews
from bpy.types import Scene

from ..business.asset import assets as tool
from ..core.commands import CommandEndpoint
from ..core.runtime import ensure_mutation_allowed
from ..util.resources import prepare_creation_options, resolve_scene

_previews = None


def execute(scene_uid, options):
    """Prepare finite named choices at the explicit Tool boundary, never draw."""
    ensure_mutation_allowed()
    from ..business.asset import asset_catalog as catalog
    default_shape = 0
    identifier = options.get("identifier", "")
    if options.get("action") == "REBUILD":
        root = tool.find_root(resolve_scene(scene_uid), options.get("instance", ""))
        identifier = root[tool.IDENTIFIER]
        default_shape = root.get("oxal_mc_studio_shape", 0)
    if options.get("action") in {"IMPORT", "REBUILD"} and identifier and not identifier.startswith("image:"):
        shape = options.get("shape", default_shape)
        values = options.get("state") or catalog.default_state(identifier)
        choices = catalog.shape_items(catalog.shape_schema(identifier), values)
        if isinstance(shape, bool) or not isinstance(shape, int) or str(shape) not in {choice[0] for choice in choices}:
            raise ValueError("造型不是当前方块状态的有效选项，请重新选择")
    result: Any = tool.execute(scene_uid, options)
    if isinstance(result, dict) and "catalog" in result:
        for row in result["catalog"]:
            row["shape_schema"] = catalog.shape_schema(row["identifier"])
        if "catalog_query" in options:
            result["catalog_query"] = options["catalog_query"]
            result["selected_identifier"] = options.get("selected_identifier", "")
    if isinstance(result, dict) and "variants" in result:
        root = tool.find_root(resolve_scene(scene_uid), options.get("instance", ""))
        result["shape_schema"] = catalog.shape_schema(root[tool.IDENTIFIER])
    return result


class AssetEndpoint(CommandEndpoint):
    def submit(self,scene_uid,options):
        ensure_mutation_allowed()
        if options.get("action") in {"IMPORT","IMAGE","LIGHTNING"}:
            options = prepare_creation_options(options)
        if options.get("action") == "IMPORT" and "particles_window" not in options and options.get("identifier") in {
            "minecraft:"+name for name in ("torch","wall_torch","soul_torch","soul_wall_torch","copper_torch","copper_wall_torch","brewing_stand","spore_blossom","spawner","trial_spawner","sculk_shrieker","sculk_sensor","calibrated_sculk_sensor","redstone_torch","redstone_wall_torch","end_rod","enchanting_table")
        }:
            scene=resolve_scene(scene_uid)
            options={**options,"particles_window":{"start":int(scene.frame_start),"end":int(scene.frame_end),"fps":int(scene.render.fps),"fps_base":float(scene.render.fps_base),
                                                    "seed":195,"rate":1.0,"rate_signature":"","smoke_enabled":1.0,"smoke_signature":""}}
        return super().submit(scene_uid,options)

    def after_apply(self,result):
        ensure_mutation_allowed()
        if not isinstance(result,dict):
            return
        for manager in bpy.data.window_managers:
            state = getattr(manager,"oxal_mc_studio_assets_ui",None)
            if state is None:
                continue
            if "variants" in result:
                from ..ui.assets import populate_variants, publish_shapes
                populate_variants(state,result["variants"],result.get("state",{}))
                state.shape = result.get("shape",0)
                publish_shapes(state, result.get("shape_schema", {}), result.get("shape", 0))
                state.complete_structure = result.get("complete_structure",True)
                state.variant_instance = result.get("variant_instance","")
                if "spawn_entity" in result:
                    state.spawn_entity = result["spawn_entity"]
                if "potion_key" in result:
                    state.potion_key = result["potion_key"]
            elif "instance_id" in result:
                state.variant_instance = ""
            if "removed" in result:
                state.variant_instance = ""
    def after_execute(self, result):
        ensure_mutation_allowed()
        if not isinstance(result, dict):
            return
        for manager in bpy.data.window_managers:
            state = getattr(manager, "oxal_mc_studio_assets_ui", None)
            if state is None:
                continue
            if "catalog" in result:
                query = result.get("catalog_query")
                if query is not None:
                    scene = cast(Scene | None, bpy.context.scene)
                    if (scene is None or scene.session_uid != query["scene_uid"]
                            or manager.session_uid != query["manager_uid"]
                            or state.catalog_requested != json.dumps(query, sort_keys=True)
                            or state.search != query["search"] or state.category != query["category"]):
                        continue
                from ..ui.assets import publish_potion_items, publish_spawner_items
                publish_potion_items(result.get("potion_presets",()))
                publish_spawner_items(result.get("spawner_entities",()))
                selected = result.get("selected_identifier", "")
                if not selected and 0 <= state.index < len(state.rows):
                    selected = state.rows[state.index].identifier
                state.rows.clear()
                for item in result["catalog"]:
                    row = state.rows.add()
                    row.identifier = item["identifier"]
                    row.label = item["label"]
                    row.schema_json = json.dumps(item.get("schema", {}))
                    row.defaults_json = json.dumps(item.get("defaults", {}))
                    row.shape_schema_json = json.dumps(item.get("shape_schema", {}))
                state.index = next((index for index, row in enumerate(state.rows) if row.identifier == selected), 0)
                state.page = int(result.get("page", 0))
                state.pages = int(result.get("pages", 1))
                state.total = int(result.get("total", len(state.rows)))
                state.catalog_loaded = json.dumps([state.search, state.category, state.page], ensure_ascii=False)
                from ..ui.assets import selected_changed

                selected_changed(state, None)
            previews = result.get("previews", [])
            if "preview" in result:
                previews = [*previews, result["preview"]]
            if _previews is not None:
                rows = {row.identifier: row for row in state.rows}
                for item in previews:
                    key, path = item["identifier"], item["path"]
                    try:
                        if key not in _previews:
                            _previews.load(key, path, "IMAGE")
                        if key in rows:
                            rows[key].icon_id = _previews[key].icon_id
                    except (OSError, RuntimeError, KeyError):
                        if key in rows:
                            rows[key].icon_id = 0
            if "variants" in result:
                from ..ui.assets import populate_variants, publish_shapes

                populate_variants(state, result["variants"], result.get("state", {}))
                state.shape = result.get("shape", 0)
                publish_shapes(state, result.get("shape_schema", {}), result.get("shape", 0))
                state.complete_structure = result.get("complete_structure", True)
                state.variant_instance = result.get("variant_instance","")
            if "packs" in result:
                state.packs.clear()
                for item in result["packs"]:
                    row = state.packs.add()
                    row.identifier = item["digest"]
                    row.label = item["label"]
                state.pack_index = min(state.pack_index,max(0,len(state.packs)-1))


endpoint = AssetEndpoint("assets.execute", execute, allowed_modes=("OBJECT","POSE"))


def submit(scene_uid, options):
    ensure_mutation_allowed()
    return endpoint.submit(scene_uid, options)


def register():
    global _previews
    _previews = bpy.utils.previews.new()
    try:
        endpoint.register()
    except Exception:
        bpy.utils.previews.remove(_previews)
        _previews = None
        raise


def unregister():
    global _previews
    endpoint.unregister()
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
