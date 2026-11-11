"""Product information in native preferences; no scene or business state."""

import logging
from pathlib import Path
from typing import Any, cast

import bpy
import bpy.utils.previews
from bpy.types import AddonPreferences, UILayout

from ..util.identity import DISPLAY_VERSION, PRODUCT_NAME

WEBSITE = "https://oxal.baipo.fun/"
_LOGO = Path(__file__).resolve().parents[1] / "asset/brand/oxal-symbol.webp"
_previews = None
_registered = False
logger = logging.getLogger(__name__)


class OXALMCSTUDIO_AP_preferences(AddonPreferences):
    bl_idname = cast(str, __package__).rsplit(".", 1)[0]

    def draw(self, _context):
        layout = cast(UILayout, self.layout)
        header = layout.row()
        logo = _previews.get("logo") if _previews is not None else None
        if logo is not None and logo.icon_id:
            header.template_icon(icon_value=logo.icon_id, scale=4.0)
        else:
            header.label(text="", icon="ASSET_MANAGER")
        title = header.column(align=True)
        title.label(text=PRODUCT_NAME, translate=False)
        title.label(text=f"版本：{DISPLAY_VERSION}", translate=False)
        title.label(text="作者：白珀、空树林")
        title.label(text="兼容 Blender 4.5 / 5.0 / 5.1 / 5.2")
        box = layout.box()
        box.label(text="面向 Blender 的 Minecraft 创作工具。")
        for label, icon in (
            ("白珀人模：皮肤、骨骼控制、盔甲与背饰", "ARMATURE_DATA"),
            ("OXAL 天空：昼夜、日月、云与环境", "WORLD"),
            ("独立物品栏：第一／第三人称与原生槽位控制", "VIEW_CAMERA"),
            ("资产库：方块、物品、图片资产与材质包", "ASSET_MANAGER"),
        ):
            box.label(text=label, icon=cast(Any, icon))
        layout.operator("wm.url_open", text="访问 OXAL 官网", icon="URL").url = WEBSITE


def register():
    global _previews, _registered
    bpy.utils.register_class(OXALMCSTUDIO_AP_preferences)
    _registered = True
    try:
        try:
            if not _LOGO.is_file():
                raise FileNotFoundError(_LOGO)
            _previews = bpy.utils.previews.new()
            _previews.load("logo", str(_LOGO), "IMAGE")
        except (OSError, RuntimeError) as error:
            # Product information remains usable if an optional UI image is absent.
            logger.warning("OXAL preference logo unavailable; using a native icon: %s", error)
            if _previews is not None:
                bpy.utils.previews.remove(_previews)
                _previews = None
    except Exception:
        unregister()
        raise


def unregister():
    global _previews, _registered
    try:
        if _previews is not None:
            bpy.utils.previews.remove(_previews)
            _previews = None
    finally:
        if _registered:
            bpy.utils.unregister_class(OXALMCSTUDIO_AP_preferences)
            _registered = False
