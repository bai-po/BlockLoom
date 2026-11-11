"""Sky command submission and file selection; business work stays in tools."""

import json
from typing import TYPE_CHECKING, Any, cast

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Context, Operator, Scene, UILayout, WindowManager

from ..business.sky.sky import cloud_structure_snapshot
from ..business.sky.sky_state import VALUES
from ..core.runtime import ensure_mutation_allowed, mutation_allowed
from ..module import sky

if TYPE_CHECKING:
    from bpy.stub_internal.rna_enums import OperatorReturnItems


def _ready(context: Context):
    scene = cast(Scene | None, context.scene)
    return (
        mutation_allowed()
        and scene is not None
        and not scene.library
        and not scene.override_library
        and context.mode == "OBJECT"
        and not sky.busy(scene.session_uid)
    )


class OXALMCSTUDIO_OT_load_sky(Operator):
    bl_idname = "oxal_mc_studio.load_sky"
    bl_label = "一键加载并切换 OXAL天空"

    @classmethod
    def poll(cls, context):
        return _ready(context)

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            sky.endpoint.submit(cast(Scene, context.scene).session_uid, {})
        except (ValueError, RuntimeError, TypeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_sky_command(Operator):
    bl_idname = "oxal_mc_studio.sky_command"
    bl_label = "天空操作"

    action: StringProperty(options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return _ready(context)

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            scene = cast(Scene, context.scene)
            options: dict[str, Any] = {"action": self.action}
            if self.action == "cloud_apply":
                options["structure"] = cloud_structure_snapshot(getattr(scene, VALUES))
            if self.action in {"save_slot", "apply_slot", "clear_slot", "key_slot"}:
                options.update(sky.slot_request(scene, self.action))
            sky.commands.submit(scene.session_uid, options)
        except (ValueError, RuntimeError, TypeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


_CACHE_ITEMS = {}


def _cache_items(self, _context):
    # Blender retains enum string pointers; keep each host's actual choices live.
    choices = json.loads(self.mode_snapshot).get("cache_choices", []) if self.mode_snapshot else []
    key = tuple((identifier, label) for identifier, label in choices)
    if key not in _CACHE_ITEMS:
        _CACHE_ITEMS[key] = tuple((identifier, label, "至少 1 GB，且不低于当前缓存") for identifier, label in key)
    return _CACHE_ITEMS[key]


class OXALMCSTUDIO_OT_sky_cloud_mode(Operator):
    bl_idname = "oxal_mc_studio.sky_cloud_mode"
    bl_label = "云模式 / 修复已有 Beta 天空"

    mode: EnumProperty(items=(
        ("MASTER", "启用云", "点击立即应用总开关"),
        ("LIGHT", "普通云光照", "互斥模式"),
        ("ADVANCED", "Eevee 高级云光照", "互斥模式"),
        ("SHADOW", "云阴影", "互斥模式"),
        ("LIGHT_SHADOW_AUTO", "自动控制云光照阴影", "跟随高级模式"),
        ("LIGHT_SHADOW", "手动云光照阴影", "明确改变投射器结构"),
        ("REPAIR", "修复已有 Beta 天空", "保留实际动画、材质和绑定；冲突拒绝"),
    ), options={"SKIP_SAVE"})
    mode_snapshot: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    cache_size: EnumProperty(name="阴影缓存", items=_cache_items, options={"SKIP_SAVE"})
    cache_confirmed: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return _ready(context)

    def invoke(self, context: Context, event) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            plan = sky.mode_request(cast(Scene, context.scene), self.mode)
            self.mode_snapshot = json.dumps(plan)
            if plan["needs_confirmation"]:
                self.cache_size = plan["cache_choices"][0][0]
                # execute() is reached only after the user accepts this dialog.
                self.cache_confirmed = True
                return cast(WindowManager, context.window_manager).invoke_props_dialog(self, width=400)
            return self.execute(context)
        except (ValueError, RuntimeError, TypeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def draw(self, context):
        plan = json.loads(self.mode_snapshot)
        layout = cast(UILayout, self.layout)
        layout.label(text=f"当前 Eevee 阴影缓存：{plan['cache']} MB", icon="INFO")
        layout.label(text="启用前必须明确提高至至少 1 GB；取消保留全部状态。")
        layout.prop(self, "cache_size")

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            scene = cast(Scene, context.scene)
            plan = json.loads(self.mode_snapshot) if self.mode_snapshot else sky.mode_request(scene, self.mode)
            choice = self.cache_size if plan["needs_confirmation"] else plan["cache"]
            sky.commands.submit(scene.session_uid, {
                "action": "cloud_mode", "snapshot": plan, "cache_size": choice,
                "cache_confirmed": self.cache_confirmed,
            })
        except (ValueError, RuntimeError, TypeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_sky_shadow_quality(Operator):
    bl_idname = "oxal_mc_studio.sky_shadow_quality"
    bl_label = "明确应用天空灯阴影投影精度"
    bl_description = "数值更大时阴影更粗、占缓存更少；影响本天空灯照射的所有物体阴影，保留原值供恢复"

    quality: EnumProperty(items=(
        ("ORIGINAL", "恢复原值", "恢复首次明确调整前的各灯原值"),
        ("LARGE", "大范围 · 0.01", "将本天空三盏灯的投影精度原生数值设为 0.01"),
        ("WIDE", "更大范围 · 0.02", "将本天空三盏灯的投影精度原生数值设为 0.02"),
    ), options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return _ready(context)

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            scene = cast(Scene, context.scene)
            sky.commands.submit(scene.session_uid, {
                "action": "shadow_quality", "snapshot": sky.quality_request(scene, self.quality),
            })
        except (ValueError, RuntimeError, TypeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_sky_preset_file(Operator):
    bl_idname = "oxal_mc_studio.sky_preset_file"
    bl_label = "天空预设文件"

    action: EnumProperty(
        items=(("import_preset", "导入预设", ""), ("export_preset", "导出当前参数", ""))
    )
    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.oxalsky", options={"HIDDEN"})
    scene_uid: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    snapshot_json: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return _ready(context)

    def invoke(self, context: Context, event) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            scene = cast(Scene, context.scene)
            self.scene_uid = str(scene.session_uid)
            plan = sky.preset_context(scene)
            if self.action == "export_preset":
                plan = sky.preset_request(scene, self.action, plan)
            self.snapshot_json = json.dumps(plan)
            cast(WindowManager, context.window_manager).fileselect_add(self)
            return {"RUNNING_MODAL"}
        except (ValueError, RuntimeError, TypeError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            scene_uid = (
                int(self.scene_uid)
                if self.scene_uid
                else cast(Scene, context.scene).session_uid
            )
            scene = cast(Scene, context.scene)
            if scene_uid != scene.session_uid:
                raise ValueError("文件选择期间场景已切换，请回原场景重新操作")
            options = json.loads(self.snapshot_json) if self.snapshot_json else {}
            options.update({"action": self.action, "filepath": self.filepath})
            options = sky.preset_request(scene, self.action, options)
            sky.commands.submit(scene_uid, options)
        except (ValueError, RuntimeError, TypeError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OXALMCSTUDIO_OT_sky_library(Operator):
    bl_idname = "oxal_mc_studio.sky_library"
    bl_label = "天空预设库操作"

    action: EnumProperty(
        items=(
            ("refresh", "刷新", ""),
            ("apply_preset", "应用", ""),
            ("delete_preset", "删除", ""),
        )
    )

    @classmethod
    def poll(cls, context):
        return _ready(context)

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        try:
            ensure_mutation_allowed()
            if self.action == "refresh":
                options = {"action": "list_presets"}
            else:
                state = cast(Any, context.window_manager).oxal_mc_studio_sky_ui
                if not 0 <= state.index < len(state.items):
                    raise ValueError("请先刷新并选择天空预设")
                options = {
                    "action": self.action,
                    "filename": state.items[state.index].filename,
                }
            sky.commands.submit(
                cast(Scene, context.scene).session_uid,
                sky.preset_request(cast(Scene, context.scene), str(options["action"]), options),
            )
        except (ValueError, RuntimeError, TypeError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


CLASSES = (
    OXALMCSTUDIO_OT_load_sky,
    OXALMCSTUDIO_OT_sky_command,
    OXALMCSTUDIO_OT_sky_cloud_mode,
    OXALMCSTUDIO_OT_sky_shadow_quality,
    OXALMCSTUDIO_OT_sky_preset_file,
    OXALMCSTUDIO_OT_sky_library,
)


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
