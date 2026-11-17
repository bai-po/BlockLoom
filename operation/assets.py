"""Asset request snapshots and native file selection dialogs."""

import json
from typing import TYPE_CHECKING, Any, cast

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Context, Event, Operator, Scene, ViewLayer, WindowManager

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


def _snapshot(context: Context, action, destination="", *, part="", enabled=False, instance="") -> dict[str, Any]:
    scene = cast(Scene, context.scene)
    if action == "PACK_LIST":
        return {"action": action}
    if action == "SHADER_LAYOUT":
        from ..business.shader_layout import request

        return {"action": action, "shader_targets": request()}
    state = cast(
        "OXALMCSTUDIO_PG_assets_ui", getattr(context.window_manager, STATE_PROPERTY)
    )
    from ..business.outline import stylized

    if action in stylized.ACTIONS:
        from ..business.outline.compositor import exclusion_targets

        targets = []
        if action in {"COMP_EXCLUDE", "COMP_RESTORE", "COMP_SUBJECT_ADD", "COMP_SUBJECT_REMOVE"}:
            selected = context.selected_objects
            if context.mode == "POSE" and context.selected_pose_bones:
                selected = tuple({bone.id_data for bone in context.selected_pose_bones})
            targets = exclusion_targets(scene, selected)
        return {"action": action, "layer": cast(ViewLayer, context.view_layer).name, "targets": targets}
    if action in {"CREATURE_BIND", "CREATURE_UNBIND", "CREATURE_STYLE", "CREATURE_REMOVE", "CREATURE_UPGRADE", "CREATURE_FACE_REPAIR"}:
        from ..business.asset.creatures import RIG, selected_instance
        from ..business.human import viewport_character_state

        if action == "CREATURE_REMOVE" and instance:
            return {"action": action, "instance": instance}
        root = selected_instance(scene, context.selected_objects)
        if root is None:
            raise ValueError("请选中一个生物实例")
        if action in {"CREATURE_REMOVE", "CREATURE_FACE_REPAIR"}:
            return {"action": action, "instance": root[INSTANCE]}
        if action == "CREATURE_UPGRADE":
            from ..util.resources import prepare_creation_options
            return prepare_creation_options({"action": action, "instance": root[INSTANCE]})
        if action == "CREATURE_STYLE":
            if part:
                value = enabled
                if part == "arm_mode":
                    value = "SLIM" if enabled else "WIDE"
                elif part == "leg_mode":
                    value = "OFFICIAL" if enabled else "NORMAL"
                return {"action": action, "instance": root[INSTANCE], "field": part, "value": value}
            field = state.creature_field
            return {"action": action, "instance": root[INSTANCE], "field": field,
                    "value": getattr(state, "creature_" + field)}
        members = set(root.all_objects)
        return {"action": action, "instance": root[INSTANCE], "wrist": viewport_character_state.wrist(root[RIG]),
                "object_uids": [o.session_uid for o in context.selected_objects if o not in members]}
    if action == "CREATURE_IMPORT":
        creature = state.creature
        if state.creature_open and not state.creature_rows:
            raise ValueError("没有匹配的生物可导入")
        if state.creature_rows:
            if not 0 <= state.creature_index < len(state.creature_rows):
                raise ValueError("请从目录选择要导入的生物")
            creature = state.creature_rows[state.creature_index].identifier
        return {"action": action, "creature": creature,
                "position": [scene.cursor.location[i] for i in range(3)] if destination == "CURSOR" else [0, 0, 0]}
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
    if action in {"ENCHANT", "UNENCHANT", "ENCHANT_OUTLINE_REBIND", "ENCHANT_PIXELS_PREPARE"} and options.get("instances"):
        from ..business.asset.asset_enchantment import request

        options["material_targets"] = request(scene, options)
    return options


class OXALMCSTUDIO_OT_asset_action(Operator):
    bl_idname = "oxal_mc_studio.asset_action"
    bl_label = "OXAL 资产操作"
    action: StringProperty(options={"SKIP_SAVE"})
    page_step: IntProperty(default=0, options={"SKIP_SAVE"})
    destination: StringProperty(default="", options={"SKIP_SAVE"})
    creature_part: StringProperty(options={"SKIP_SAVE"})
    creature_enabled: BoolProperty(options={"SKIP_SAVE"})
    creature_instance: StringProperty(options={"SKIP_SAVE"})

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
            options = _snapshot(context, self.action, self.destination, part=self.creature_part,
                                enabled=self.creature_enabled, instance=self.creature_instance)
            if self.action == "ENCHANT_3D_PREPARE":
                options["relief_cell_size"] = getattr(context.window_manager, STATE_PROPERTY).relief_cell_size
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
    depth: FloatProperty(name="物品厚度", default=0.03, min=0.0001, max=1, precision=4)
    image_item: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})
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
            options = _snapshot(context, self.action)
            self.image_item = self.action == "IMAGE" and options["kind"] == "ITEM"
            if self.image_item:
                self.depth = options["depth"]
            self.filter_glob = "*.png" if self.action == "IMAGE" else "*.zip"
            self.options_json = json.dumps(options, ensure_ascii=False, allow_nan=False)
            cast(WindowManager, context.window_manager).fileselect_add(self)
        except (ValueError, TypeError, RuntimeError, KeyError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def draw(self, context):
        if self.image_item and self.layout is not None:
            self.layout.prop(self, "depth")

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
            if options.get("action") == "IMAGE" and options.get("kind") == "ITEM" and self.properties.is_property_set("depth"):
                options["depth"] = self.depth
            options["path"] = self.filepath
            assets.submit(scene_uid, options)
        except (ValueError, TypeError, RuntimeError, KeyError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_quick(Operator):
    bl_idname = "oxal_mc_studio.quick_tools"
    bl_label = "悬浮工作台"

    intent: EnumProperty(
        items=(
            ("AUTO", "悬浮工作台", ""), ("CHARACTER", "角色功能", ""), ("ENCHANT", "附魔", ""),
            ("TOOLS", "其他功能", ""), ("COLLAPSE", "收起或展开", ""), ("CLOSE", "关闭", ""),
            ("KEEP", "工作台", ""),
        ), options={"SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context: Any):
        return context.area is not None and context.area.type == "VIEW_3D"

    def execute(self, context: Any) -> "set[OperatorReturnItems]":
        from ..business.human import viewport_pose
        from ..module import human
        from ..ui.workbench import selected_character

        wm = context.window_manager
        region = context.region
        if region.type != "WINDOW":
            region = next(r for r in context.area.regions if r.type == "WINDOW")
        identity = str(region.as_pointer())
        target = selected_character(context)
        uid = str(target[1].session_uid) if target else ""
        open_here = wm.blockloom_show_tools and wm.blockloom_workbench_region == identity and wm.blockloom_workbench_target == uid
        role = bool(target and (not open_here or wm.blockloom_workbench_page == "CHARACTER"))
        panel = viewport_pose.panel(target[1]) if role and target else "OPEN"
        automatic = bool(role and not open_here and context.space_data.show_gizmo and panel == "OPEN")
        collapsed = panel == "COLLAPSED" if role else wm.blockloom_workbench_collapsed
        def set_panel(value):
            if target and viewport_pose.panel(target[1]) != value:
                try:
                    human.commands.submit(context.scene.session_uid, {"action":"viewport_pose","command":"PANEL",
                        "target":viewport_pose.target(*target),"area":str(context.area.as_pointer()),"value":value})
                except (ValueError,RuntimeError) as exc:
                    self.report({"WARNING"},str(exc))
                    return False
            return True
        if self.intent == "KEEP":
            return {"FINISHED"}
        if self.intent == "CLOSE" or (self.intent == "AUTO" and (automatic or (open_here and not collapsed and panel != "CLOSED"))):
            if role and not set_panel("CLOSED"):
                return {"CANCELLED"}
            wm.blockloom_show_tools = False
        elif self.intent == "COLLAPSE":
            if role:
                if not set_panel("OPEN" if collapsed else "COLLAPSED"):
                    return {"CANCELLED"}
            else:
                wm.blockloom_workbench_collapsed = not collapsed
        else:
            page = self.intent if self.intent != "AUTO" else ("CHARACTER" if target else "ENCHANT" if context.selected_objects else "TOOLS")
            if not context.space_data.show_gizmo:
                name = {"TOOLS": "quick_tools", "ENCHANT": "quick_enchant", "CHARACTER": "quick_character"}[page]
                bpy.ops.wm.call_panel("INVOKE_DEFAULT", name="OXALMCSTUDIO_PT_" + name, keep_open=True)
                return {"FINISHED"}
            if page == "CHARACTER" and not set_panel("OPEN"):
                return {"CANCELLED"}
            wm.blockloom_workbench_region = identity
            wm.blockloom_workbench_target = uid
            wm.blockloom_show_tools = True
            wm.blockloom_workbench_page = page
            wm.blockloom_workbench_collapsed = False
        context.area.tag_redraw()
        return {"FINISHED"}


class OXALMCSTUDIO_OT_quick_move(Operator):
    bl_idname = "oxal_mc_studio.quick_move"
    bl_label = "移动悬浮工作台"
    bl_description = "按住标题栏并拖动悬浮工作台，松开后固定位置"
    bl_options = {"INTERNAL"}  # noqa: RUF012

    def invoke(self, context: Any, event: Event) -> "set[OperatorReturnItems]":
        from ..business.human.viewport_pose import offset
        from ..business.human.viewport_pose import target as pose_target
        from ..ui.workbench import card_layout, explicitly_open, selected_character
        from ..ui.workbench_card import BACKGROUND, TITLE, hit_part

        origin = []
        _, cells = card_layout(context, origin=origin)
        if hit_part(cells, (event.mouse_region_x, event.mouse_region_y)) != TITLE:
            return {"PASS_THROUGH"}
        wm = context.window_manager
        target = selected_character(context)
        role = target and origin and (not explicitly_open(context, target) or wm.blockloom_workbench_page == "CHARACTER")
        self._role_target = pose_target(*target) if role and target else None
        self._scene_uid = context.scene.session_uid
        self.prop = ("blockloom_character_preview_offset" if role else
                     "blockloom_character_offset" if origin else "blockloom_workbench_position")
        self.previous = tuple(getattr(wm, self.prop)) if wm.is_property_set(self.prop) else None
        if role and target:
            wm.blockloom_character_preview_offset = tuple(offset(target[1]))
            wm.blockloom_character_drag = str(target[1].session_uid)
        x, y, _, height = next(rect for part, rect, _style, _label in cells if part == BACKGROUND)
        base_x, base_y = origin or (0, 0)
        self.delta = (x - event.mouse_x - base_x, y + height - event.mouse_y - base_y)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context: Any, event: Event) -> "set[OperatorReturnItems]":
        wm = context.window_manager
        if event.type in {"ESC", "RIGHTMOUSE", "WINDOW_DEACTIVATE"} or context.scene.session_uid != self._scene_uid:
            if self._role_target:
                wm.blockloom_character_drag = ""
            if self.previous is None:
                wm.property_unset(self.prop)
            else:
                setattr(wm, self.prop, self.previous)
            context.area.tag_redraw()
            return {"CANCELLED"}
        released = event.type == "LEFTMOUSE" and event.value == "RELEASE"
        if event.type == "MOUSEMOVE" or released:
            setattr(wm, self.prop, (self.delta[0] + event.mouse_x, self.delta[1] + event.mouse_y))
            context.area.tag_redraw()
        if released and self._role_target:
            from ..module import human
            wm.blockloom_character_drag = ""
            try:
                human.commands.submit(self._scene_uid, {"action":"viewport_pose","command":"OFFSET",
                    "target":self._role_target,"area":str(context.area.as_pointer()),"value":list(getattr(wm,self.prop))})
            except (RuntimeError,ValueError) as exc:
                self.report({"WARNING"},str(exc))
                return {"CANCELLED"}
        # Finish with the initiating release consumed, even if a button is under the mouse.
        return {"FINISHED"} if released else {"RUNNING_MODAL"}


class OXALMCSTUDIO_OT_quick_character_action(Operator):
    bl_idname = "oxal_mc_studio.quick_character_action"
    bl_label = "角色控制"
    action: StringProperty(options={"SKIP_SAVE"})

    def execute(self, context: Any) -> "set[OperatorReturnItems]":
        from ..business.human import human_compat, viewport_pose
        from ..business.human import viewport_character_state as state
        from ..business.human.human import resolve_human
        from ..module import human
        from ..ui import workbench_character as controls
        from ..ui.workbench import selected_character

        target = selected_character(context)
        if not target or not mutation_allowed():
            return {"CANCELLED"}
        root, rig = target
        scene = context.scene
        name = self.action
        try:
            if name in {"ik", "rotate", "body", "bones", "wrist"}:
                if name == "wrist":
                    wrists = tuple(controls.WRISTS)
                    command, value = "WRIST", wrists[(wrists.index(state.wrist(rig)) + 1) % len(wrists)]
                elif name == "bones":
                    command, value = "BONES", not viewport_pose.native_visible(rig)
                elif name == "body":
                    command, value = "BODY", not viewport_pose.body_enabled(rig)
                else:
                    command, value = "ROTATE", name == "rotate"
                human.commands.submit(scene.session_uid, {"action": "viewport_pose", "command": command,
                    "target": viewport_pose.target(root,rig), "area": str(context.area.as_pointer()), "enabled": value})
            elif name in {"bind", "unbind"} and root is not None:
                from ..business.human.viewport_pose import character_of

                members = set(root.all_objects)
                selected = [ob for ob in context.selected_objects if ob not in members]
                if any(character_of(scene, ob) is not None for ob in selected):
                    raise ValueError("请只选择一个角色及要绑定的物品")
                if controls.is_creature(root):
                    return cast(Any, bpy.ops).oxal_mc_studio.asset_action(action="CREATURE_" + name.upper())
                root = resolve_human(scene, root[INSTANCE])
                human.commands.submit(scene.session_uid, {"action": name, "instance_id": root[INSTANCE],
                    "object_uids": [ob.session_uid for ob in selected],
                    "wrist": state.wrist(rig)})
            else:
                current = controls.values(scene, root, rig).get(name)
                if current is None:
                    raise ValueError("当前角色不支持此操作")
                enabled = not current
                if controls.is_creature(root):
                    part = "name" if name == "name_enabled" else name
                    return cast(Any, bpy.ops).oxal_mc_studio.asset_action(action="CREATURE_STYLE", creature_part=part,
                        creature_enabled=not enabled if name == "arm_mode" else enabled)
                if root is None:
                    legacy = human_compat._identify(scene, rig)
                    if legacy is None or not legacy.editable:
                        raise ValueError("旧人模不可编辑")
                    value = {"arm_mode": ("SLIM", "WIDE"), "leg_mode": ("NORMAL", "OFFICIAL")}.get(name, (False, True))[int(enabled)]
                    query = {"action": "legacy_compat", "target": human_compat.capture(scene, legacy),
                             "feature": "basic_settings", "values": {name: value}, "parts": []}
                else:
                    root = resolve_human(scene, root[INSTANCE])
                    query = {"action": "parameters", "instance_id": root[INSTANCE],
                             "values": {name: int(enabled) if name in {"arm_mode", "leg_mode"} else enabled}}
                human.commands.submit(scene.session_uid, query)
            context.area.tag_redraw()
        except (KeyError, ValueError, RuntimeError, TypeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_quick_dismiss(Operator):
    bl_idname = "oxal_mc_studio.quick_dismiss"
    bl_label = "收起悬浮工作台"

    @classmethod
    def poll(cls, context: Any):
        from ..business.human.viewport_pose import panel
        from ..ui.workbench import selected_character

        wm = context.window_manager
        target = selected_character(context)
        return bool(context.area and context.area.type == "VIEW_3D" and context.region and
                    ((target and panel(target[1]) != "CLOSED" and context.space_data.show_gizmo) or (wm.blockloom_show_tools and
                     wm.blockloom_workbench_region == str(context.region.as_pointer()))))

    def execute(self, context: Any) -> "set[OperatorReturnItems]":
        from ..business.human.viewport_pose import panel
        from ..ui.workbench import explicitly_open, selected_character

        target = selected_character(context)
        opened = explicitly_open(context, target)
        wm = context.window_manager
        collapsed = (panel(target[1]) == "COLLAPSED"
                     if target and (not opened or wm.blockloom_workbench_page == "CHARACTER")
                     else opened and wm.blockloom_workbench_collapsed)
        return cast(Any, bpy.ops).oxal_mc_studio.quick_tools(intent="CLOSE" if collapsed else "COLLAPSE")


CLASSES = (OXALMCSTUDIO_OT_asset_action, OXALMCSTUDIO_OT_asset_file, OXALMCSTUDIO_OT_quick,
           OXALMCSTUDIO_OT_quick_move, OXALMCSTUDIO_OT_quick_character_action, OXALMCSTUDIO_OT_quick_dismiss)


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
