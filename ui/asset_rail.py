"""Bounded sidebar editing for the native X rails on library armatures."""

import json
from math import floor
from typing import TYPE_CHECKING, Any, cast

import bpy
from bpy.props import BoolProperty, FloatProperty, StringProperty
from bpy.types import Operator, PoseBone

from ..core.runtime import mutation_allowed
from ..util.animation_edit import require_keyframe_edit
from ..util.ownership import owns

if TYPE_CHECKING:
    from bpy.stub_internal.rna_enums import OperatorReturnItems

PROPERTY = "blockloom_rail_factor"


def limits(bone):
    from ..business.asset.assets import root_of

    rig = bone.id_data
    root = root_of(rig)
    if root is None or not owns(root, rig):
        return None
    track = next((c for c in bone.constraints if c.type == "LIMIT_LOCATION"
                  and c.owner_space == "LOCAL" and c.use_min_x and c.use_max_x
                  and not c.mute and c.max_x > c.min_x), None)
    if track is None:
        return None
    spec = next((s for s in json.loads(rig.get("骨骼控件", "[]")) if s["bone"] == bone.name), None)
    if not spec or spec["max"] <= spec["min"]:
        return None
    keys = spec.get("keys", ())
    state = next((s for s in json.loads(rig.get("oxal_mc_studio_state_controls", "[]"))
                  if s["property"] in keys), {})
    discrete = (bool(state.get("values")) and state.get("selector", "floor(v+.5)") == "floor(v+.5)"
                or "状态 · 床颜色" in keys or any(isinstance(rig.get(key), bool) for key in keys))
    return track.min_x, track.max_x, spec["min"], spec["max"], discrete


def _fraction(bone, bounds):
    lo, hi = bounds[:2]
    return min(1.0, max(0.0, (bone.location.x - lo) / (hi - lo)))


def _get(bone):
    bounds = limits(bone)
    return _fraction(bone, bounds) if bounds else 0.0


def _set(bone, factor):
    bounds = limits(bone)
    if bounds is None or not mutation_allowed():
        return
    left, right, low, high, discrete = bounds
    factor = min(1.0, max(0.0, factor))
    if discrete:
        factor = (floor(low + factor * (high - low) + 0.5) - low) / (high - low)
    rig = bone.id_data
    path = bone.path_from_id("location")
    if rig.library or rig.override_library or len(rig.users_scene) != 1 or bone.lock_location[0]:
        return
    if rig.animation_data and rig.animation_data.drivers.find(path, index=0):
        return
    auto = cast(bpy.types.Scene, bpy.context.scene).tool_settings.use_keyframe_insert_auto
    if auto:
        require_keyframe_edit(rig, path)
    bone.location.x = left + factor * (right - left)
    if auto:
        bone.keyframe_insert(data_path="location", index=0, group=bone.name)


class OXALMCSTUDIO_OT_edit_asset_rail(Operator):
    bl_idname = "oxal_mc_studio.edit_asset_rail"
    bl_label = "骨骼控制值"
    bl_options = {"UNDO", "INTERNAL"}  # noqa: RUF012 -- Blender RNA class metadata.
    target: StringProperty(options={"SKIP_SAVE"})
    bone_name: StringProperty(options={"SKIP_SAVE"})
    keyframe: BoolProperty(options={"SKIP_SAVE"})
    value: FloatProperty(name="数值", options={"SKIP_SAVE"})

    def _bone(self, context):
        from ..business.asset.assets import root_of

        root = root_of(context.object)
        rig = next((o for o in context.scene.objects if str(o.session_uid) == self.target), None)
        if not root or not rig or not owns(root, rig) or not rig.pose:
            raise ValueError("当前选择与原骨架不一致")
        bone = rig.pose.bones.get(self.bone_name)
        if not bone:
            raise ValueError("此骨骼的原生轨道已改变")
        bounds = limits(bone)
        if bounds is None:
            raise ValueError("此骨骼的原生轨道已改变")
        return bone, bounds

    def invoke(self, context: Any, event) -> "set[OperatorReturnItems]":
        if self.keyframe:
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self, width=220)

    def draw(self, context):
        if self.layout:
            self.layout.prop(self, "value")

    def execute(self, context) -> "set[OperatorReturnItems]":
        try:
            if not mutation_allowed():
                raise ValueError("当前作业锁定了场景")
            bone, bounds = self._bone(context)
            if self.keyframe:
                require_keyframe_edit(bone.id_data, bone.path_from_id("location"))
                bone.keyframe_insert(data_path="location", index=0, group=bone.name)
            else:
                low, high = bounds[2:4]
                _set(bone, (self.value - low) / (high - low))
        except (ValueError, RuntimeError) as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


def draw(layout, bone, label):
    bounds = limits(bone)
    if bounds is None:
        layout.label(text=label + " · 缺少有效轨道", icon="INFO")
        return
    low, high, discrete = bounds[2:]
    value = low + _fraction(bone, bounds) * (high - low)
    row = layout.row(align=True)
    split = row.split(factor=.80, align=True)
    split.prop(bone, PROPERTY, text=label, slider=True)
    caption = str(floor(value + 0.5)) if discrete else f"{value:.3f}".rstrip("0").rstrip(".")
    op = split.operator(OXALMCSTUDIO_OT_edit_asset_rail.bl_idname, text=caption)
    op.target, op.bone_name, op.value = str(bone.id_data.session_uid), bone.name, value
    key = row.operator(OXALMCSTUDIO_OT_edit_asset_rail.bl_idname, text="", icon="KEY_HLT")
    key.target, key.bone_name, key.keyframe = op.target, op.bone_name, True


def register():
    bpy.utils.register_class(OXALMCSTUDIO_OT_edit_asset_rail)
    try:
        setattr(PoseBone, PROPERTY, FloatProperty(name="轨道行程", min=0, max=1, subtype="FACTOR",
                options={"SKIP_SAVE"}, get=_get, set=_set))
    except Exception:
        bpy.utils.unregister_class(OXALMCSTUDIO_OT_edit_asset_rail)
        raise


def unregister():
    delattr(PoseBone, PROPERTY)
    bpy.utils.unregister_class(OXALMCSTUDIO_OT_edit_asset_rail)
