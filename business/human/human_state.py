"""Persistent instance parameters; no UI or scene mutation on import."""

PREFIX = "oxal_human_"
OWNER = "oxal_mc_studio_human_owner"
SOURCE_NAME = "oxal_mc_studio_human_source"
PARTS = ("head", "chest", "legs", "feet")
ARMS = {"SLIM": "手臂-三格", "WIDE": "手臂-四格"}
LEGS = {"NORMAL": "腿-常规弯曲", "OFFICIAL": "腿-官方动画"}
MATERIALS = ("皮革", "锁链甲", "铜甲", "钻石甲", "金甲", "铁甲", "下界合金甲", "海龟壳甲")
PATTERNS = ("无", "螺栓", "海岸", "沙丘", "眼睛", "流动", "寄主", "塑造者", "肋骨", "哨兵", "塑形", "幽静", "猪鼻", "尖塔", "潮汐", "恼鬼", "守卫", "寻路者", "荒野")
PARAMETERS = {
    "arm_mode": ("胳膊（0 三格 / 1 四格）", 0, 0, 1),
    "leg_mode": ("腿（0 常规 / 1 官方）", 0, 0, 1),
    "name_enabled": ("名字", False, 0, 1),
    "leg_fixed": ("腿部固定", False, 0, 1),
    "foot_fixed": ("脚腕固定", False, 0, 1),
    "red": ("红温", 0.0, 0.0, 1.0),
    "layer_enabled": ("3D二层", False, 0, 1),
    "outer": ("对外厚度", 0.0, 0.0, 1.0),
    "inner": ("对内厚度", 0.062, 0.0, 1.0),
    "random_enabled": ("随机高低凸出", False, 0, 1),
    "random_factor": ("随机系数", 0.5, 0.0, 1.0),
    "height_ratio": ("高低比例", 1.25, 0.0, 5.0),
    "seed": ("随机种子", 0, 0, 2147483647),
    "center_falloff": ("中心高外围低", 0.0, 0.0, 1.0),
    "center_range": ("中心衰减范围", 1.0, 0.0, 5.0),
    "back_mode": ("背饰（0 关闭 / 1 鞘翅 / 2 单段 / 3 五段）", 0, 0, 3),
    "cape_index": ("内置披风", 0, 0, 53),
    "outline_enabled": ("人模描边", False, 0, 1),
    "outline_color": ("描边颜色", (1.0, 1.0, 1.0, 1.0), 0.0, 1.0),
    "outline_width": ("描边宽度", 0.02, 0.0, 0.2),
    "outline_quality_normals": ("高质量法线", True, 0, 1),
}
for _part in PARTS:
    PARAMETERS.update({
        _part + "_enabled": ("启用", False, 0, 1),
        _part + "_material": ("材质编号", 5, 0, len(MATERIALS) - 1),
        _part + "_pattern": ("花纹编号", 0, 0, len(PATTERNS) - 1),
        _part + "_3d": ("3D", False, 0, 1),
        _part + "_thickness": ("厚度", 0.025, 0.0, 0.2),
        _part + "_leather_color": ("皮革颜色", (0.5, 0.25, 0.1, 1.0), 0.0, 1.0),
        _part + "_trim_color": ("纹饰颜色", (1.0, 0.65, 0.1, 1.0), 0.0, 1.0),
        _part + "_outline_follow_human": ("跟随人模描边", True, 0, 1),
        _part + "_outline_enabled": ("独立描边", False, 0, 1),
        _part + "_outline_color": ("描边颜色", (1.0, 1.0, 1.0, 1.0), 0.0, 1.0),
        _part + "_outline_width": ("描边粗细", 0.02, 0.0, 0.2),
        _part + "_outline_quality_normals": ("高质量法线", True, 0, 1),
    })


def key(name: str) -> str:
    return PREFIX + name


def path(name: str) -> str:
    return '["' + key(name) + '"]'


def parameter_spec(name, root):
    """Version-local bounds keep legacy four-mode instances editable."""
    from .human_source import model_version

    if name == "back_mode" and model_version(root) in {"2.4.2", "2.4.3", "2.4.4", "2.4.5", "2.4.6", "2.4.7"}:
        return ("背饰（0 关闭 / 1 鞘翅 / 2 披风）", 0, 0, 2)
    return PARAMETERS[name]


def refresh_limits(transaction, owner, root=None):
    """Refresh known instance bounds during an explicit, journaled repair."""
    from ...util.property_limits import update

    limits = {}
    root = root if root is not None else getattr(transaction, "owner", None)
    for name, specification in PARAMETERS.items():
        label, default, minimum, maximum = parameter_spec(name, root) if root is not None else specification
        options = {"description": label}
        if not isinstance(default, bool):
            options.update(min=minimum, max=maximum, soft_min=minimum, soft_max=maximum)
        if isinstance(default, tuple):
            options["subtype"] = "COLOR"
        limits[key(name)] = options
    update(transaction, owner, limits)
