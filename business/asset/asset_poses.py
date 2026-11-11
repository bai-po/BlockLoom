"""Explicit armor-stand presets from the packed immutable local.31 data."""

import logging
import math
from typing import Any, cast

import bpy
from mathutils import Euler, Matrix

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import REGISTRY, prune_owned, require_owned, seal_owned
from ...util.resources import live_ids
from . import asset_catalog as catalog

POSES = (
    ("RESET", "原始模型"), ("default", "默认"), ("no", "直立"),
    ("solemn", "庄严"), ("athena", "雅典娜"), ("brandish", "挥舞"),
    ("entertain", "娱乐"), ("hero", "英雄"), ("honor", "致敬"),
    ("riposte", "反击"), ("salute", "敬礼"), ("zombie", "僵尸"),
    ("cancan_a", "康康舞 A"), ("cancan_b", "康康舞 B"),
)
_PARTS = ("head", "body", "left_arm", "right_arm", "left_leg", "right_leg")
_OLD_NAMES = {"WAVE": "salute", "WALK": "zombie", "GUARD": "honor"}
logger = logging.getLogger(__name__)


def supports(identifier):
    return identifier.split(":")[-1] == "armor_stand"


requires_rig = supports


def build(transaction, scene, collection, root, identifier, options=None, material_for=None):
    if supports(identifier):
        ensure_mutation_allowed()
        parts = {obj.get("oxal_mc_studio_model_part") for obj in root.children_recursive if obj.type == "EMPTY"}
        if not set(_PARTS) <= parts:
            return False
        root["oxal_mc_studio_stand_pose_schema"] = 1
        if "盔甲架姿态" not in root:
            root["盔甲架姿态"] = "RESET"
        return True
    return False


def apply_pose(root, pose_name, scene=None, auto_key=None):
    """Change original manual bones; optional keys remain native on disable."""
    ensure_mutation_allowed()
    scene = scene or cast(Any, bpy.context.scene)
    require_owned(root, (root, root.data))
    if root.type != "ARMATURE" or root.get("资产ID") != "armor_stand":
        raise ValueError("请选择原生盔甲架骨架")
    selected = _OLD_NAMES.get(pose_name, pose_name)
    if selected not in {key for key, _label in POSES}:
        raise ValueError("未知盔甲架姿态")
    values = {} if selected == "RESET" else catalog.read(catalog.ROOT / "stand_poses.json")["poses"][selected]
    bones = [root.pose.bones.get("手动 · " + name) for name in _PARTS]
    if any(bone is None for bone in bones):
        raise ValueError("盔甲架缺少原始手动姿态骨")
    for bone in bones:
        if bone.rotation_mode not in {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}:
            raise ValueError("姿态骨已改用四元数或轴角，请保留该动画并使用原生姿态工具")
        if root.animation_data and any(curve.data_path == bone.path_from_id("rotation_euler") for curve in root.animation_data.drivers):
            raise ValueError("姿态骨已有自定义旋转驱动，未覆盖：" + bone.name)
    keyframes = scene.tool_settings.use_keyframe_insert_auto if auto_key is None else bool(auto_key)
    animation = root.animation_data
    if keyframes and animation and animation.use_tweak_mode:
        raise ValueError("请退出 NLA 调整模式后添加盔甲架姿态关键帧")
    previous_action = animation.action if animation else None
    previous_slot = getattr(animation, "action_slot", None) if animation else None
    previous_pose = [(bone, bone.rotation_euler.copy()) for bone in bones]
    previous_name = root.get("盔甲架姿态")
    registry = dict(root[REGISTRY].items())
    new_action = None
    try:
        # Work on an isolated Action when adding keys. Its full channels,
        # modifiers and slots survive, and the original is untouched on failure.
        if keyframes and previous_action:
            new_action = previous_action.copy()
            root.animation_data.action = new_action
            if previous_slot is not None:
                matches = [slot for slot in new_action.slots if slot.identifier == previous_slot.identifier]
                if len(matches) != 1:
                    raise ValueError("盔甲架动作槽不唯一，未添加姿态关键帧")
                root.animation_data.action_slot = matches[0]
        conversion = Matrix(((1, 0, 0), (0, 0, 1), (0, -1, 0)))
        for name, bone in zip(_PARTS, bones, strict=True):
            angles = tuple(math.radians(float(v)) for v in values.get(name.replace("_", ""), (0, 0, 0)))
            rotation = conversion @ Euler(angles, "XYZ").to_matrix() @ conversion.inverted()
            basis = bone.bone.matrix_local.to_3x3()
            bone.rotation_euler = (basis.inverted() @ rotation @ basis).to_euler(bone.rotation_mode)
            if keyframes and not bone.keyframe_insert("rotation_euler", frame=scene.frame_current, group=bone.name):
                raise ValueError("盔甲架姿态关键帧未写入")
        if keyframes:
            new_action = root.animation_data.action
            if new_action is not None and new_action != previous_action:
                seal_owned(root, (new_action,))
        root["盔甲架姿态"] = selected
        root.update_tag()
    except Exception:
        if root.animation_data:
            created = root.animation_data.action
            root.animation_data.action = previous_action
            if previous_slot is not None:
                root.animation_data.action_slot = previous_slot
            if created != previous_action:
                new_action = created
        for bone, rotation in previous_pose:
            bone.rotation_euler = rotation
        if previous_name is None:
            if "盔甲架姿态" in root:
                del root["盔甲架姿态"]
        else:
            root["盔甲架姿态"] = previous_name
        root[REGISTRY] = registry
        if new_action is not None and new_action != previous_action:
            bpy.data.batch_remove(ids=live_ids((new_action,)))
        raise
    try:
        prune_owned(root)
    except Exception:
        logger.exception("Unused pose-action cleanup was postponed")
    return selected
