"""Button labels and current values for the character under the viewport selection."""

from ..business.asset.creature_parts import option_value
from ..business.human import human_compat, viewport_pose
from ..business.human import viewport_character_state as state
from ..business.human.human import parameter_owner
from ..business.human.human_compat_controls import _foot_value, _leg_value, settings
from ..business.human.human_state import ARMS, LEGS, key
from ..util.resources import ASSET_ID
from . import workbench_card as card

FIELDS = {
    card.ARM: ("arm_mode", "胳膊", ("3格", "4格")),
    card.LEG: ("leg_mode", "腿", ("常规弯曲", "官方动画")),
    card.NAME: ("name_enabled", "名字", ("关", "开")),
    card.LEG_FIXED: ("leg_fixed", "腿部固定", ("关", "开")),
    card.FOOT_FIXED: ("foot_fixed", "脚腕固定", ("关", "开")),
}
WRISTS = {"L": "左手", "R": "右手", "H": "头部"}


def is_creature(root):
    return root is not None and root.get(ASSET_ID) == "blockloom.creature"


def values(scene, root, rig):
    if root is None:
        target = human_compat._identify(scene, rig)
        if target is None or not target.editable:
            return {}
        arm = human_compat._style(target, ARMS)
        leg = human_compat._style(target, LEGS)
        return {**settings(target), "arm_mode": None if arm not in ARMS else arm == "WIDE",
                "leg_mode": None if leg not in LEGS else leg == "OFFICIAL"}
    if is_creature(root):
        if not root.get("blockloom_parts_version"):
            return {}
        target = human_compat.LegacyHuman(rig, "", tuple(root.all_objects), (root,), True, "")
        return {
            "arm_mode": option_value(root, rig, "arm_mode") == "WIDE",
            "leg_mode": option_value(root, rig, "leg_mode") == "OFFICIAL",
            "name_enabled": option_value(root, rig, "name"),
            "leg_fixed": _leg_value(target), "foot_fixed": _foot_value(target),
        }
    owner = parameter_owner(root)
    return {name: bool(owner.get(key(name))) for name, _, _labels in FIELDS.values()}


def sections(context, target):
    root, rig = target
    current = values(context.scene, root, rig)
    options = []
    for part, (field, name, labels) in FIELDS.items():
        value = current.get(field)
        if value is not None:
            options.append((part, f"{name} · {labels[int(bool(value))]}"))
    controls = tuple(tuple(options[i:i + 2]) for i in range(0, len(options), 2))
    sections: list[tuple[str, tuple[tuple[tuple[int, str], ...], ...]]] = [
        ("姿态 · " + rig.name, (
            ((card.AUTO, "自动记录 · " + ("开" if state.auto(rig) else "关")),
             (card.KEY, "◇ 记 K 帧")),
            ((card.IK, "移动 IK"), (card.ROTATE, "旋转部位")),
            ((card.BODY, "点身体 · " + ("开" if viewport_pose.body_enabled(rig) else "关")),
             (card.BONES, "原控制器 · " + ("显" if viewport_pose.native_visible(rig) else "隐"))),
        )),
    ]
    if controls:
        sections.append(("生物样式 · 测试中" if is_creature(root) else "人模样式", controls))
    if root is not None:
        sections.append(("物品绑定", (((card.WRIST, "位置 · " + WRISTS[state.wrist(rig)]),
                                     (card.BIND, "绑定物品"), (card.UNBIND, "解除绑定")),)))
    return tuple(sections)
