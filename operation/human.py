"""Human request snapshots followed by explicit shared Undo dispatch."""

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator

from ..business.human.human import resolve_human, rig_of
from ..business.human.human_state import MATERIALS, PARTS, PATTERNS
from ..core.runtime import ensure_mutation_allowed, mutation_allowed
from ..module import human
from ..ui import instances
from ..util.resources import INSTANCE

if TYPE_CHECKING:
    from bpy.stub_internal.rna_enums import OperatorReturnItems


def _root(context: Any) -> Any:
    state = context.window_manager.oxal_mc_studio_human_ui
    selected = instances.selected_root(context, state, "oxal.human")
    return resolve_human(context.scene, str(selected[INSTANCE]))


def _legacy(context: Any) -> Any:
    state = context.window_manager.oxal_mc_studio_human_ui
    return instances.selected_legacy(context, state)


def _legacy_skin(context: Any, target: Any, filepath: str) -> dict[str, Any]:
    from ..business.human import human_compat

    state = context.window_manager.oxal_mc_studio_human_ui
    image = state.legacy_skin_image
    return {"action": "legacy_compat", "target": human_compat.capture(context.scene, target),
            "feature": "skin", "values": {}, "parts": [], "filepath": filepath,
            "image_uid": image.session_uid if image is not None else 0}


def _legacy_or_object_mode(context: Any) -> bool:
    if context.mode == "OBJECT":
        return True
    if context.mode == "POSE":
        try:
            return _legacy(context) is not None
        except ValueError:
            return False
    return False


class OXALMCSTUDIO_OT_human_migrate(Operator):
    bl_idname = "oxal_mc_studio.human_migrate"
    bl_label = "确认迁移旧版人模"
    bl_description = "复制同名骨骼动画到新版；旧人模仅归档并排除，不删除"

    snapshot_json: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    confirmed: BoolProperty(name="我已了解：旧人模仅归档并排除，不删除", default=False, options={"SKIP_SAVE"})
    arm_mode: EnumProperty(name="新版胳膊", items=(("AUTO", "沿用旧版", ""), ("SLIM", "3格", ""), ("WIDE", "4格", "")), default="AUTO")
    leg_mode: EnumProperty(name="新版腿", items=(("AUTO", "沿用旧版", ""), ("NORMAL", "常规弯曲", ""), ("OFFICIAL", "官方动画", "")), default="AUTO")

    @classmethod
    def poll(cls, context: Any) -> bool:
        return (context.scene is not None and context.mode == "OBJECT"
                and mutation_allowed() and not human.commands.busy(context.scene.session_uid)
                and not human.endpoint.busy(context.scene.session_uid))

    def invoke(self, context: Any, event: Any) -> "set[OperatorReturnItems]":
        try:
            from ..business.human.human_migrate import snapshot
            from ..util.resources import prepare_creation_options

            state = context.window_manager.oxal_mc_studio_human_ui
            legacy = instances.selected_legacy(context, state)
            target_context = context
            if legacy is not None:
                target_context = SimpleNamespace(object=legacy.rig, scene=context.scene,
                                                 view_layer=context.view_layer, mode=context.mode)
            elif instances.has_human_choice(state):
                target_context = SimpleNamespace(object=rig_of(_root(context)),
                                                 scene=context.scene, view_layer=context.view_layer, mode=context.mode)
            captured = prepare_creation_options({"legacy": snapshot(target_context), "scene_uid": context.scene.session_uid})
            self.snapshot_json = json.dumps(captured, ensure_ascii=False)
            self.confirmed = False
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context: Any) -> None:
        from ..business.human.human_source import CURRENT

        if not self.snapshot_json:
            return
        source = json.loads(self.snapshot_json)["legacy"]
        layout = self.layout
        if layout is None:
            return
        layout.label(text="旧人模：" + source["source_name"], icon="ARMATURE_DATA")
        layout.label(text="目标人模：白珀-人模V" + source.get("target_version", CURRENT), icon="IMPORT")
        layout.label(text=f'动画：{source["actions"]} 个 Action / {source["curves"]} 条曲线')
        layout.label(text="导入新版并复制同名骨骼的姿态、关键帧和普通 NLA。")
        layout.label(text=f'旧人模 {source["archived_objects"]} 个对象将放入“旧人模”合集并从视图层排除。', icon="INFO")
        layout.label(text="不匹配通道会跳过并提示；不重绑外部物品或约束。")
        layout.prop(self, "arm_mode")
        layout.prop(self, "leg_mode")
        layout.prop(self, "confirmed")

    def execute(self, context: Any) -> "set[OperatorReturnItems]":
        try:
            if not self.confirmed or not self.snapshot_json:
                raise ValueError("请先阅读并勾选迁移确认；旧人模只归档，不删除")
            options = json.loads(self.snapshot_json)
            scene_uid = options.pop("scene_uid")
            if scene_uid != context.scene.session_uid:
                raise ValueError("确认后的场景已切换，请重新确认")
            options.update(action="migrate_legacy", confirmed=True,
                           style={"arm_mode": self.arm_mode, "leg_mode": self.leg_mode})
            human.commands.submit(scene_uid, options)
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_human_pick(Operator):
    """A deliberate list-row click selects its rig through the human endpoint."""

    bl_idname = "oxal_mc_studio.human_pick"
    bl_label = "选择人模"
    bl_description = "选择此人模并显示它的设置"

    scene_uid: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    rig_uid: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context: Any) -> bool:
        return (context.scene is not None and context.mode in {"OBJECT", "POSE"}
                and mutation_allowed() and not human.commands.busy(context.scene.session_uid)
                and not human.endpoint.busy(context.scene.session_uid))

    def execute(self, context: Any) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            if self.scene_uid != str(context.scene.session_uid):
                raise ValueError("列表人模属于另一场景，请重新选择")
            matches = [(rig, root, version) for rig, (root, version) in instances.human_entries(context).items()
                       if str(rig.session_uid) == self.rig_uid]
            if len(matches) != 1:
                raise ValueError("列表人模已删除或身份改变，请重新选择")
            rig, root, version = matches[0]
            if root is None:
                from ..business.human import human_compat

                target = human_compat._identify(context.scene, rig, version_hint=version)
                if target is None:
                    raise ValueError("旧人模结构或版本已改变，请重新选择")
                options = {"action": "legacy_compat", "target": human_compat.capture(context.scene, target),
                           "feature": "select", "values": {}, "parts": []}
            else:
                options = {"action": "select", "instance_id": str(root[INSTANCE]), "selection_ui": True}
            human.commands.submit(context.scene.session_uid, options)
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_human_command(Operator):
    bl_idname = "oxal_mc_studio.human_command"
    bl_label = "人模操作"

    action: StringProperty()
    kind: StringProperty(default="skins")
    filepath: StringProperty(subtype="FILE_PATH")
    values_json: StringProperty(default="{}")
    value: StringProperty(default="")

    @classmethod
    def poll(cls, context: Any) -> bool:
        return (context.scene is not None and mutation_allowed() and _legacy_or_object_mode(context)
                and not human.commands.busy(context.scene.session_uid))

    def execute(self, context: Any) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            state = context.window_manager.oxal_mc_studio_human_ui
            action, filepath, kind = self.action, self.filepath, self.kind
            if action == "legacy_functions":
                from ..business.human import human_compat

                target = _legacy(context)
                if target is None:
                    raise ValueError("请先选中旧人模")
                state.functions_open = not state.functions_open
                if state.functions_open:
                    human.commands.submit(context.scene.session_uid, {
                        "action": "legacy_compat", "target": human_compat.capture(context.scene, target),
                        "feature": "prepare_controls", "values": {}, "parts": [],
                    })
                return {"FINISHED"}
            if action == "apply_style":
                from ..business.human import human_compat

                legacy = _legacy(context)
                if legacy is not None:
                    values: dict[str, Any] = {name: value for name in ("arm_mode", "leg_mode", "name_enabled", "leg_fixed", "foot_fixed")
                                             if (value := getattr(state, "edit_" + name)) != "MIXED"}
                    for name in ("name_enabled", "leg_fixed", "foot_fixed"):
                        if name in values:
                            values[name] = values[name] == "ON"
                    owner = f"{context.scene.session_uid}:{legacy.rig.session_uid}"
                    if state.style_draft_owner == owner and "red" in json.loads(state.style_draft_values):
                        values["red"] = state.edit_legacy_red
                    options = {"action": "legacy_compat", "target": human_compat.capture(context.scene, legacy),
                               "feature": "basic_settings", "values": values, "parts": []}
                else:
                    if any(getattr(state, "edit_" + name) == "MIXED"
                           for name in ("arm_mode", "leg_mode", "name_enabled", "leg_fixed", "foot_fixed")):
                        raise ValueError("请为所选人模选择明确的样式与固定状态")
                    options = {"action": "parameters", "instance_id": _root(context)[INSTANCE],
                               "values": {"arm_mode": int(state.edit_arm_mode == "WIDE"),
                                          "leg_mode": int(state.edit_leg_mode == "OFFICIAL"),
                                          "name_enabled": state.edit_name_enabled == "ON",
                                          "leg_fixed": state.edit_leg_fixed == "ON",
                                          "foot_fixed": state.edit_foot_fixed == "ON"}}
                state.style_draft_owner = ""
                human.commands.submit(context.scene.session_uid, options)
                return {"FINISHED"}
            if action == "skin_library_view":
                state.skin_source = "LIBRARY"
                action, kind = "library_list", "skins"
            if action == "skin_recent_list":
                from ..business.human import human_compat

                state.skin_source = "RECENT"
                legacy = _legacy(context)
                if legacy is not None:
                    options = {"action": action, "target": human_compat.capture(context.scene, legacy)}
                else:
                    options = {"action": action, "instance_id": _root(context)[INSTANCE]}
                human.commands.submit(context.scene.session_uid, options)
                return {"FINISHED"}
            if action == "library_refresh":
                action = "library_list"
            if action in {"library_skin", "library_pack"}:
                if action == "library_skin":
                    filepath = state.skin_library_filepath
                    if state.library_kind != "skins" or not filepath or not any(row.filepath == filepath for row in state.library_rows):
                        raise ValueError("请先刷新并选择皮肤库条目")
                else:
                    if not 0 <= state.library_index < len(state.library_rows):
                        raise ValueError("请先刷新并选择资源库条目")
                    filepath = state.library_rows[state.library_index].filepath
                action = "skin" if action == "library_skin" else "pack"
            if action == "recent_skin":
                from ..business.human.human_skin_history import recent_paths

                legacy = _legacy(context)
                owner = legacy.rig if legacy is not None else _root(context)
                if (state.recent_scene_uid != str(context.scene.session_uid)
                        or state.recent_owner_uid != str(owner.session_uid)):
                    raise ValueError("最近使用的选择属于另一人模或场景，请重新选择")
                filepath = state.recent_filepath
                if not filepath or filepath not in recent_paths(owner):
                    raise ValueError("最近使用的皮肤选择已过期，请重新选择")
                action = "skin"
            if action == "skin" and (legacy := _legacy(context)) is not None:
                human.commands.submit(context.scene.session_uid, _legacy_skin(context, legacy, filepath))
                return {"FINISHED"}
            options: dict[str, Any] = {"action": action, "kind": kind, "filepath": filepath}
            if action == "back_control":
                options["value"] = float(self.value)
            if action not in {"library_add", "library_list"}:
                options["instance_id"] = _root(context)[INSTANCE]
            if action in {"bind", "unbind"}:
                root = _root(context)
                members = set(root.all_objects)
                selected = [o for o in context.selected_objects if o not in members]
                options.update(objects=[o.name for o in selected], object_uids=[o.session_uid for o in selected], wrist=state.wrist)
            if action == "parameters":
                options["values"] = json.loads(self.values_json)
            if action == "apply_initial":
                options["action"] = "parameters"
                options["values"] = {
                    "arm_mode": int(state.arm_mode == "WIDE"), "leg_mode": int(state.leg_mode == "OFFICIAL"),
                    "name_enabled": state.name_enabled == "ON", "leg_fixed": state.leg_fixed == "ON", "foot_fixed": state.foot_fixed == "ON",
                }
            if action in {"armor_enable", "armor_disable", "armor_all_enable", "armor_all_disable"}:
                parts = PARTS if "all" in action or state.armor_target == "ALL" else (state.armor_target.lower(),)
                options["action"] = "armor_set"
                options["parts"] = list(parts)
                options["enabled"] = "disable" not in action
            if action == "armor_apply":
                parts = PARTS if state.armor_target == "ALL" else (state.armor_target.lower(),)
                options["action"] = "parameters"
                options["values"] = {p + "_" + suffix: value for p in parts for suffix, value in (
                    ("material", int(state.armor_material)), ("pattern", int(state.armor_pattern)),
                    ("leather_color", list(state.leather_color)), ("trim_color", list(state.trim_color)),
                )}
            if action in {"armor_enchant", "armor_unenchant", "armor_enchant_3d", "armor_enchant_outline", "armor_enchant_pixels",
                          "armor_enchant_outline_enable", "armor_enchant_pixels_enable",
                          "armor_outline_prepare", "armor_outline_remove", "armor_outline_repair"}:
                parts = (state.armor_target.lower(),) if state.armor_target != "ALL" else PARTS
                options["parts"] = list(parts)
            human.commands.submit(context.scene.session_uid, options)
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_human_back_command(Operator):
    bl_idname = "oxal_mc_studio.human_back_command"
    bl_label = "人模背饰操作"

    action: StringProperty()
    kind: StringProperty(default="")
    values_json: StringProperty(default="{}")
    value: StringProperty(default="")

    @classmethod
    def poll(cls, context: Any) -> bool:
        return (context.scene is not None and context.mode in {"OBJECT", "POSE"}
                and mutation_allowed() and not human.commands.busy(context.scene.session_uid))

    def execute(self, context: Any) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            from ..business.human.human import rig_of
            from ..business.human.human_back import rig_for

            root = _root(context)
            body = rig_of(root)
            accessory = rig_for(root, body)
            active = context.view_layer.objects.active
            if context.mode == "POSE" and (active not in {body, accessory}
                    or tuple(context.objects_in_mode) != (active,)):
                raise ValueError("请仅对当前人模的一套骨架进入姿态模式")
            role = "BACK" if accessory is not None and active == accessory else "BODY"
            bone = active.data.bones.active if active in {body, accessory} and active is not None else None
            options: dict[str, Any] = {
                "action": self.action, "instance_id": root[INSTANCE],
                "back_context": {"mode": context.mode, "body_or_back": role,
                                 "active_bone": bone.name if bone else ""},
            }
            if self.action == "parameters":
                values = json.loads(self.values_json)
                if not isinstance(values, dict) or set(values) != {"back_mode"}:
                    raise ValueError("背饰操作只接受背饰模式")
                options["values"] = values
            elif self.action == "back_control":
                options.update(kind=self.kind, value=float(self.value))
            elif self.action == "back_control_select":
                options["kind"] = self.kind
            else:
                raise ValueError("未知背饰操作")
            human.commands.submit(context.scene.session_uid, options)
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_human_file(Operator):
    bl_idname = "oxal_mc_studio.human_file"
    bl_label = "选择人模 PNG 资源"

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.png", options={"HIDDEN"})
    action: StringProperty(default="skin")
    kind: StringProperty(default="skins")
    instance_id: StringProperty()
    scene_uid: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    legacy_json: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})

    def _capture_target(self, context: Any) -> None:
        self.scene_uid = str(context.scene.session_uid)
        if self.action == "library_add":
            return
        if self.action == "skin" and not self.instance_id and (legacy := _legacy(context)) is not None:
            self.legacy_json = json.dumps(_legacy_skin(context, legacy, ""), ensure_ascii=False)
        elif not self.instance_id:
            self.instance_id = _root(context)[INSTANCE]

    @classmethod
    def poll(cls, context: Any) -> bool:
        return context.scene is not None and mutation_allowed() and _legacy_or_object_mode(context)

    def invoke(self, context: Any, event: Any) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            self.filter_glob = "*.png" if self.kind == "skins" else "*.png;*.zip"
            self._capture_target(context)
            context.window_manager.fileselect_add(self)
        except (ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def execute(self, context: Any) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            target_uid = int(self.scene_uid) if self.scene_uid else context.scene.session_uid
            if target_uid != context.scene.session_uid:
                raise ValueError("已切换场景，请重新选择人模并提交")
            if (not self.scene_uid or self.action != "library_add"
                    and not self.instance_id and not self.legacy_json):
                self._capture_target(context)
            if self.legacy_json:
                options = json.loads(self.legacy_json)
                options["filepath"] = self.filepath
            else:
                options = {"action": self.action, "kind": self.kind, "filepath": self.filepath, "instance_id": self.instance_id}
            human.commands.submit(target_uid, options)
        except (ValueError, TypeError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_human_compat(Operator):
    """Capture one legacy target; existing human.edit owns execution and Undo."""

    bl_idname = "oxal_mc_studio.human_compat"
    bl_label = "旧人模兼容设置"
    bl_description = "使用所选旧人模现有的原生功能，保留原网格和动画"

    feature: StringProperty()
    values_json: StringProperty(default="{}", options={"HIDDEN", "SKIP_SAVE"})
    parts_json: StringProperty(default="[]", options={"HIDDEN", "SKIP_SAVE"})
    target_json: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    scene_uid: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    image_uid: StringProperty(default="0", options={"HIDDEN", "SKIP_SAVE"})
    frame: FloatProperty(options={"HIDDEN", "SKIP_SAVE"})
    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.png", options={"HIDDEN"})
    armor_selector: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})
    red_popup: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})
    armor_three_d_available: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})
    material: EnumProperty(name="材质", items=tuple((str(i), label, "") for i, label in enumerate(MATERIALS)), default="5")
    pattern: EnumProperty(name="纹饰", items=tuple((str(i), label, "") for i, label in enumerate(PATTERNS)), default="0")
    leather_color: FloatVectorProperty(name="皮革颜色", size=4, subtype="COLOR", min=0, max=1, default=(0.5, 0.25, 0.1, 1))
    trim_color: FloatVectorProperty(name="纹饰颜色", size=4, subtype="COLOR", min=0, max=1, default=(1, 0.65, 0.1, 1))
    three_d: BoolProperty(name="原生3D", default=True)
    thickness: FloatProperty(name="厚度", min=-10000, max=10000, default=0.03)
    outer: FloatProperty(name="对外厚度", min=-10000, max=10000, default=0)
    inner: FloatProperty(name="对内厚度", min=-10000, max=10000, default=0)
    random_enabled: BoolProperty(name="随机高低凸出", default=False)
    random_factor: FloatProperty(name="随机系数", min=0, max=1, default=0.5)
    height_ratio: FloatProperty(name="高低比例", min=1, max=20, default=1)
    seed: IntProperty(name="随机种子", min=0, max=2147483647, default=0)
    center_falloff: FloatProperty(name="中心高外围低", min=0, max=1, default=0)
    center_range: FloatProperty(name="中心衰减范围", min=0.05, max=5, default=1)

    @classmethod
    def poll(cls, context: Any) -> bool:
        return (context.scene is not None and context.mode in {"OBJECT", "POSE"}
                and mutation_allowed() and not human.commands.busy(context.scene.session_uid))

    def _capture(self, context: Any) -> Any:
        from ..business.human import human_compat

        state = context.window_manager.oxal_mc_studio_human_ui
        target = _legacy(context)
        if target is None:
            raise ValueError("请先选中已识别的旧版人模")
        if not target.editable:
            raise ValueError(target.reason or "所选旧人模不可编辑")
        self.target_json = json.dumps(human_compat.capture(context.scene, target), ensure_ascii=False)
        self.scene_uid = str(context.scene.session_uid)
        image = state.legacy_skin_image
        self.image_uid = str(image.session_uid if image is not None else 0)
        if self.feature == "head":
            self.frame = context.scene.frame_current_final
        if self.feature in {"armor_visibility", "armor_settings"}:
            parts = json.loads(self.parts_json)
            if not parts:
                parts = list(PARTS) if state.armor_target == "ALL" else [state.armor_target.lower()]
            if not isinstance(parts, list) or not parts or set(parts) - set(PARTS):
                raise ValueError("旧人模盔甲部位参数无效")
            self.parts_json = json.dumps(parts)
        return target

    def _prefill(self, target: Any, *, keep_explicit: bool = False) -> None:
        from ..business.human import human_compat

        if self.feature == "layer_settings":
            values = human_compat.settings(target, "layer")
        else:
            values = human_compat.settings(target, "armor", tuple(json.loads(self.parts_json)))
            self.armor_selector = bool(values.pop("selector"))
            self.armor_three_d_available = bool(values.pop("three_d_available"))
            values["material"], values["pattern"] = str(values["material"]), str(values["pattern"])
        for name, value in values.items():
            if not keep_explicit or not self.properties.is_property_set(name):
                setattr(self, name, value)

    def invoke(self, context: Any, event: Any) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            target = self._capture(context)
            if self.feature == "skin" and not self.filepath:
                context.window_manager.fileselect_add(self)
                return {"RUNNING_MODAL"}
            if self.feature in {"layer_settings", "armor_settings"}:
                self._prefill(target, keep_explicit=True)
                return context.window_manager.invoke_props_dialog(self, width=440)
            return self.execute(context)
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def draw(self, context: Any) -> None:
        layout = self.layout
        if layout is None:
            return
        if self.feature == "layer_settings":
            layout.label(text="参数取首个现有二层，确定后应用全部现有二层。", icon="INFO")
            for name in ("outer", "inner", "random_enabled", "random_factor", "height_ratio",
                         "seed", "center_falloff", "center_range"):
                layout.prop(self, name)
        elif self.feature == "armor_settings":
            layout.label(text="确定后应用到提交时选定的盔甲部位。", icon="INFO")
            layout.prop(self, "material")
            if self.armor_selector:
                layout.prop(self, "pattern")
                if self.material == "0":
                    layout.prop(self, "leather_color")
                if self.pattern != "0":
                    layout.prop(self, "trim_color")
            else:
                layout.label(text="沿用旧版纹饰图，不加入新版选择器。", icon="INFO")
            if self.armor_three_d_available:
                layout.prop(self, "three_d")
                if self.three_d:
                    layout.prop(self, "thickness")

    def execute(self, context: Any) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            if not self.target_json:
                target = self._capture(context)
                if self.feature in {"layer_settings", "armor_settings"}:
                    # EXEC_DEFAULT has no dialog prefill. Preserve unspecified
                    # native values instead of submitting Operator defaults.
                    self._prefill(target, keep_explicit=True)
            target_uid = int(self.scene_uid)
            if target_uid != context.scene.session_uid:
                raise ValueError("已切换场景，请重新选择旧人模并提交")
            if self.feature == "layer_settings":
                values = {name: getattr(self, name) for name in (
                    "outer", "inner", "random_enabled", "random_factor", "height_ratio",
                    "seed", "center_falloff", "center_range")}
            elif self.feature == "armor_settings":
                values: dict[str, Any] = {"material": int(self.material)}
                if self.armor_selector:
                    values.update(pattern=int(self.pattern), leather_color=list(self.leather_color),
                                  trim_color=list(self.trim_color))
                if self.armor_three_d_available:
                    values.update(three_d=self.three_d, thickness=self.thickness)
            else:
                values = json.loads(self.values_json)
            if not isinstance(values, dict):
                raise TypeError("旧人模兼容参数必须为对象")
            options = {
                "action": "legacy_compat", "target": json.loads(self.target_json),
                "feature": self.feature, "values": values, "parts": json.loads(self.parts_json),
                "filepath": self.filepath, "image_uid": int(self.image_uid),
                "red_popup": self.red_popup,
            }
            if self.feature == "head":
                options["frame"] = self.frame
            human.commands.submit(target_uid, options)
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_legacy_red_popup(Operator):
    bl_idname = "oxal_mc_studio.legacy_red_popup"
    bl_label = "红温"
    bl_options = {"INTERNAL"}  # noqa: RUF012 -- Blender RNA metadata.

    scene_uid: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    rig_uid: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def _channel(self, context):
        from ..business.human.human_compat_controls import red_channel

        if self.scene_uid != str(context.scene.session_uid):
            raise ValueError("红温目标场景已改变")
        target = _legacy(context)
        if target is None or self.rig_uid != str(target.rig.session_uid):
            raise ValueError("红温目标人模已改变")
        channel = red_channel(target)
        if channel is None:
            raise ValueError("当前人模原生红温接线尚未准备完成")
        return channel

    def invoke(self, context: Any, event: Any) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            self._channel(context)
        except (ValueError, RuntimeError) as error:
            self.report({"INFO"}, str(error))
            return {"CANCELLED"}
        return context.window_manager.invoke_popup(self, width=280)

    def draw(self, context: Any) -> None:
        layout = self.layout
        if layout is None:
            return
        try:
            channel = self._channel(context)
        except (ValueError, RuntimeError) as error:
            layout.label(text=str(error), icon="INFO")
            return
        row = layout.row()
        row.enabled = mutation_allowed()
        row.prop(channel[0], channel[1], text="红温", slider=True)

    def execute(self, context: Any) -> "set[OperatorReturnItems]":
        return {"FINISHED"}


CLASSES = (OXALMCSTUDIO_OT_human_migrate, OXALMCSTUDIO_OT_human_pick, OXALMCSTUDIO_OT_human_command, OXALMCSTUDIO_OT_human_back_command,
           OXALMCSTUDIO_OT_legacy_red_popup,
           OXALMCSTUDIO_OT_human_file, OXALMCSTUDIO_OT_human_compat)


def register() -> None:
    registered = []
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
    except Exception:
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        raise


def unregister() -> None:
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
