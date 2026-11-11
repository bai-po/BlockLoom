"""Asset request snapshots and native file selection dialogs."""

import json
from typing import TYPE_CHECKING, Any, cast

import bpy
from bpy.props import EnumProperty, IntProperty, StringProperty
from bpy.types import Context, Event, Operator, Scene, WindowManager

from ..business.asset.assets import root_of
from ..core.runtime import mutation_allowed
from ..module import assets
from ..ui.assets import STATE_PROPERTY
from ..util.resources import INSTANCE, resolve_scene

if TYPE_CHECKING:
    from bpy.stub_internal.rna_enums import OperatorReturnItems

    from ..ui.assets import OXALMCSTUDIO_PG_assets_ui


def _managed_asset(obj):
    """Copied tags or user-added children do not grant instance ownership."""
    from ..util.ownership import owns

    root = root_of(obj)
    return root is not None and owns(root, root) and owns(root, obj)


def _snapshot(context: Context, action, destination="") -> dict[str, Any]:
    scene = cast(Scene, context.scene)
    if action == "PACK_LIST":
        return {"action": action}
    if action == "SHADER_LAYOUT":
        from ..business.shader_layout import request

        return {"action": action, "shader_targets": request()}
    state = cast(
        "OXALMCSTUDIO_PG_assets_ui", getattr(context.window_manager, STATE_PROPERTY)
    )
    if action in {"CATALOG", "CATALOG_OPEN", "CATALOG_PAGE"}:
        return {
            "action": "CATALOG",
            "search": state.search,
            "category": state.category,
            "page": state.page,
            "selected_identifier": state.rows[state.index].identifier if 0 <= state.index < len(state.rows) else "",
        }
    requested_destination = destination or state.destination
    if action in {"IMPORT", "IMAGE"} and not requested_destination:
        raise ValueError("当前选择已不支持之前的导入位置，请重新选择导入位置")
    requested_destination = requested_destination or "ORIGIN"
    options = {
        "action": action,
        "destination": requested_destination,
        "position": [scene.cursor.location[axis] for axis in range(3)],
        "special": state.special,
        "shape": int(state.shape_choice or "0"),
        "complete_structure": state.complete_structure,
        "search": state.search,
        "page": state.page,
        "category": state.category,
        "display_target": state.display_target,
        "display_slot": int(state.display_slot or "1"),
        "rotation": state.display_rotation,
        "kind": state.image_kind,
        "depth": state.image_depth,
        "pbr_format": state.pbr_format,
        "quantity":state.quantity,
        "potion_key":state.potion_key,
        "spawn_entity":state.spawn_entity,
        "stand_pose":state.stand_pose,
        "slot_phase":int(state.slot_phase or "0"),
        "replace":state.replace_occupied,
        "context_mode":context.mode,
    }
    if 0 <= state.index < len(state.rows):
        options["identifier"] = state.rows[state.index].identifier
    options["state"] = {row.key: row.value for row in state.variants}
    selected = {
        root[INSTANCE]
        for obj in context.selected_objects
        if (root := root_of(obj)) is not None
    }
    options["instances"] = sorted(selected)
    active = root_of(context.object)
    if requested_destination in {"SLOT", "DISPLAY"} and action in {"IMPORT", "IMAGE"}:
        from ..business.asset.asset_targets import selected_targets
        targets = selected_targets(context)
        if requested_destination not in targets:
            raise ValueError("请先选择当前场景中实际拥有的物品栏或展示容器，再定向导入")
        if requested_destination == "SLOT":
            options["inventory_target"] = targets["SLOT"]
        else:
            options.update(targets["DISPLAY"])
        options["shape"] = 0
    if active is not None:
        options["instance"] = active[INSTANCE]
        if action in {"REBUILD","REPAIR","PARTICLES_REBUILD"}:
            from ..business.asset.asset_extensions import particles_module
            particle_tool=particles_module(active)
            if particle_tool is not None:
                options["particles_window"]=particle_tool.request(scene,active,options)
    if action == "ARROW_BAKE":
        options["frame_start"]=int(scene.frame_start)
        options["frame_end"]=int(scene.frame_end)
    if action in {"PROJECTILE_CACHE_BAKE", "PROJECTILE_CACHE_CLEAR"}:
        if active is None:
            raise ValueError("请选择投掷物实例")
        from ..business.asset.asset_projectile_clock import request
        options["projectile_cache"] = request(scene, active, {
            "current_frame": float(scene.frame_current_final),
            "frame_start": int(scene.frame_start), "frame_end": int(scene.frame_end),
        })
    if action in {"BOOK_TARGET", "BOOK_TARGET_CLEAR"}:
        if active is None:
            raise ValueError("请选择原生附魔台实例")
        from ..business.asset.asset_book import target_request
        options["book_target_snapshot"] = target_request(scene, active, state.book_target, remove=action == "BOOK_TARGET_CLEAR")
    if action == "LIGHTNING":
        if active is None or active.get("oxal_mc_studio_library_identifier") != "minecraft:trident":
            raise ValueError("请选择三叉戟资产")
        origin = active
        held = active.get("oxal_inventory_held")
        if isinstance(held,bpy.types.Object) and any(obj.type == "MESH" and not obj.hide_render for obj in held.children_recursive):
            origin = held
        evaluated = origin.evaluated_get(context.evaluated_depsgraph_get())
        options["position"] = [float(value) for value in evaluated.matrix_world.translation]
        options["trigger_frame"] = float(scene.frame_current_final)
    if 0 <= state.pack_index < len(state.packs):
        options["digest"] = state.packs[state.pack_index].identifier
    if action == "PACK_FUTURE" and not state.future_pack:
        options["digest"] = ""
    if action == "REPAIR":
        for key in ("state","shape","complete_structure"):
            options.pop(key,None)
    if action == "REBUILD" and active is not None and state.variant_instance != active[INSTANCE]:
        raise ValueError("请先读取当前实例变体，再应用本次编辑")
    if action in {"ENCHANT", "UNENCHANT", "ENCHANT_OUTLINE_REBIND"} and options.get("instances"):
        from ..business.asset.asset_enchantment import request

        options["material_targets"] = request(scene, options)
    return options


class OXALMCSTUDIO_OT_asset_action(Operator):
    bl_idname = "oxal_mc_studio.asset_action"
    bl_label = "OXAL 资产操作"
    action: StringProperty(options={"SKIP_SAVE"})
    page_step: IntProperty(default=0, options={"SKIP_SAVE"})
    destination: StringProperty(default="", options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context: Context) -> bool:
        return (
            context.scene is not None
            and context.mode in {"OBJECT","POSE"}
            and mutation_allowed()
            and not assets.endpoint.busy(cast(Scene, context.scene).session_uid)
        )

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        try:
            query = None
            from ..business.enchantment import enchantment_external as external

            if self.action in external.ACTIONS:
                if context.mode != "OBJECT":
                    raise ValueError("请在物体模式选择要附魔的对象或父物体")
                assets.submit(cast(Scene, context.scene).session_uid,
                              external.request(context.scene, context.selected_objects, self.action))
                return {"FINISHED"}
            if self.action in {"ENCHANT", "UNENCHANT", "ENCHANT_3D_PREPARE", "ENCHANT_OUTLINE_PREPARE", "ENCHANT_PIXELS_PREPARE"}:
                from ..business.human.human_selection import armor_snapshot
                from ..module import human

                armor = armor_snapshot(context.scene, context.selected_objects)
                selected_assets = any(_managed_asset(obj) for obj in context.selected_objects)
                if armor is not None:
                    from ..business.human.human import resolve_human

                    human_root = resolve_human(context.scene, armor["instance_id"])
                    if any(obj not in tuple(human_root.all_objects) for obj in context.selected_objects):
                        armor = None
                if armor is not None:
                    if context.mode != "OBJECT":
                        raise ValueError("请在物体模式选择盔甲网格或人模主骨架后附魔")
                    armor["action"] = {"ENCHANT": "armor_enchant", "UNENCHANT": "armor_unenchant",
                                       "ENCHANT_3D_PREPARE": "armor_enchant_3d",
                                       "ENCHANT_OUTLINE_PREPARE": "armor_enchant_outline",
                                       "ENCHANT_PIXELS_PREPARE": "armor_enchant_pixels"}[self.action]
                    human.commands.submit(cast(Scene, context.scene).session_uid, armor)
                    return {"FINISHED"}
                generic_action = {"ENCHANT": "ENCHANT_GENERIC", "UNENCHANT": "UNENCHANT_GENERIC",
                                  "ENCHANT_OUTLINE_PREPARE": "ENCHANT_GENERIC_OUTLINE",
                                  "ENCHANT_3D_PREPARE": "ENCHANT_GENERIC_3D",
                                  "ENCHANT_PIXELS_PREPARE": "ENCHANT_GENERIC_PIXELS"}.get(self.action)
                mixed = selected_assets and any(not _managed_asset(obj) for obj in context.selected_objects)
                if generic_action and mixed:
                    raise ValueError("请分别选择 OXAL 实例与外部模型，以保留各自的材质包、盔甲和重建控制")
                if generic_action and (not selected_assets or mixed):
                    if context.mode != "OBJECT":
                        raise ValueError("请在物体模式选择要附魔的对象或父物体")
                    assets.submit(cast(Scene, context.scene).session_uid,
                                  external.request(context.scene, context.selected_objects, generic_action))
                    return {"FINISHED"}
            options = _snapshot(context, self.action,self.destination)
            if self.action == "CATALOG_OPEN":
                state = getattr(context.window_manager, STATE_PROPERTY)
                loaded = json.dumps([state.search, state.category, state.page], ensure_ascii=False)
                if state.catalog_loaded == loaded:
                    return {"FINISHED"}
                options["page"] = 0
            if self.action == "CATALOG":
                options["page"] = 0
            elif self.action == "CATALOG_PAGE":
                options["action"] = "CATALOG"
                options["page"] = max(0, options["page"] + self.page_step)
            if options["action"] == "CATALOG":
                query = {
                    "scene_uid": cast(Scene, context.scene).session_uid,
                    "manager_uid": cast(WindowManager, context.window_manager).session_uid,
                    "search": options["search"], "category": options["category"], "page": options["page"],
                }
                options["catalog_query"] = query
            assets.submit(cast(Scene, context.scene).session_uid, options)
            if query is not None:
                getattr(context.window_manager, STATE_PROPERTY).catalog_requested = json.dumps(query, sort_keys=True)
        except (ValueError, TypeError, RuntimeError, KeyError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_asset_file(Operator):
    bl_idname = "oxal_mc_studio.asset_file"
    bl_label = "选择资产文件"
    filter_glob: StringProperty(default="*.png;*.zip", options={"HIDDEN"})
    filepath: StringProperty(subtype="FILE_PATH")
    source_scene_uid: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    options_json: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    action: EnumProperty(
        items=(("IMAGE", "图片资产", ""), ("PACK_ADD", "材质包入库", ""))
    )

    @classmethod
    def poll(cls, context: Context) -> bool:
        return context.scene is not None and mutation_allowed()

    def invoke(self, context: Context, event: Event) -> "set[OperatorReturnItems]":
        try:
            self.source_scene_uid = str(cast(Scene, context.scene).session_uid)
            self.options_json = json.dumps(
                _snapshot(context, self.action), ensure_ascii=False, allow_nan=False
            )
            cast(WindowManager, context.window_manager).fileselect_add(self)
        except (ValueError, TypeError, RuntimeError, KeyError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        try:
            if self.options_json or self.source_scene_uid:
                scene_uid = int(self.source_scene_uid)
                options = json.loads(self.options_json)
                if not isinstance(options, dict):
                    raise TypeError("资产文件参数快照格式错误")
            else:
                scene_uid = cast(Scene, context.scene).session_uid
                options = _snapshot(context, self.action)
            resolve_scene(scene_uid)
            options["path"] = self.filepath
            assets.submit(scene_uid, options)
        except (ValueError, TypeError, RuntimeError, KeyError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


CLASSES = (OXALMCSTUDIO_OT_asset_action, OXALMCSTUDIO_OT_asset_file)


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
