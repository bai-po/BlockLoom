"""Screen-space drawing and hit areas for the BlockLoom workbench."""

from functools import lru_cache
from math import cos, pi, sin
from typing import cast

import blf
import gpu
from bpy.types import Gizmo
from gpu_extras.batch import batch_for_shader

PAPER = (0.969, 0.973, 0.969, 1.0)
WHITE = (1.0, 1.0, 1.0, 1.0)
INK = (0.078, 0.173, 0.141, 1.0)
MUTED = (0.250, 0.367, 0.314, 1.0)
TEAL = (0.043, 0.478, 0.373, 1.0)
ACCENT = (0.090, 0.725, 0.557, 1.0)
MINT = (0.902, 0.961, 0.937, 1.0)
EDGE = (0.811, 0.864, 0.836, 1.0)

# Positive IDs are also Blender's gizmo operator part indices.
BACKGROUND, COLLAPSE, CLOSE, CHARACTER, ENCHANT, TOOLS = range(6)
ADD, PIXELS, REMOVE, IMAGE, GLOW, OUTLINE, EXCLUDE, CLEAR = range(6, 14)
STYLE, IMPORT, BIND, UNBIND, CREATURE_BIND, CREATURE_UNBIND = range(14, 20)
TITLE = 20
ARM, LEG, NAME, LEG_FIXED, FOOT_FIXED, WRIST = range(21, 27)
AUTO, KEY, IK, ROTATE, BODY, BONES = range(27, 33)
RESTORE = 33
_TABS = ((CHARACTER, "CHARACTER", "角色"), (ENCHANT, "ENCHANT", "附魔"), (TOOLS, "TOOLS", "其他"))


@lru_cache(maxsize=1)
def _shader():
    return gpu.shader.from_builtin("UNIFORM_COLOR")


@lru_cache(maxsize=128)
def _rounded(width, height, radius):
    points = [(width * 0.5, height * 0.5)]
    for x, y, start in (
        (width - radius, height - radius, 0),
        (radius, height - radius, 1),
        (radius, radius, 2),
        (width - radius, radius, 3),
    ):
        for step in range(6):
            angle = (start + step / 5) * pi * 0.5
            points.append((x + radius * cos(angle), y + radius * sin(angle)))
    points.append(points[1])
    return batch_for_shader(_shader(), "TRI_FAN", {"pos": points})


def _fill(x, y, width, height, radius, color):
    shader = _shader()
    shader.bind()
    shader.uniform_float("color", color)
    gpu.matrix.push()
    try:
        gpu.matrix.translate((x, y, 0))
        _rounded(width, height, min(radius, height * 0.5, width * 0.5)).draw(shader)
    finally:
        gpu.matrix.pop()


def _text(x, baseline, text, size, limit, color):
    blf.size(0, max(8, round(size)))
    if blf.dimensions(0, text)[0] > limit:
        while text and blf.dimensions(0, text + "…")[0] > limit:
            text = text[:-1]
        text += "…"
    blf.color(0, *color)
    blf.position(0, x, baseline, 0)
    blf.draw(0, text)


def hit_part(cells, location):
    px, py = location
    for part, (x, y, w, h), _style, _label in reversed(cells):
        if part >= 0 and x <= px < x + w and y <= py < y + h:
            return part
    return -1


class OXALMCSTUDIO_GT_workbench_card(Gizmo):
    bl_idname = "OXALMCSTUDIO_GT_workbench_card"
    bl_label = "BlockLoom 悬浮工作台"
    __slots__ = ("cells", "hovered", "scale")

    def setup(self):
        self.cells = []
        self.hovered = -1
        self.scale = 1.0

    def test_select(self, context, location):
        self.hovered = hit_part(self.cells, location)
        return self.hovered

    def draw(self, context):
        scale = self.scale
        hovered = self.hovered if self.is_highlight else -1
        blend = cast(str, gpu.state.blend_get())
        try:
            gpu.state.blend_set("ALPHA")
            for part, (x, y, w, h), style, label in self.cells:
                if style == "root":
                    _fill(x, y, w, h, w / 2, WHITE)
                    _fill(x + 2 * scale, y + 2 * scale, w - 4 * scale, h - 4 * scale, w / 2, TEAL)
                    continue
                if style == "handle":
                    continue
                if style == "card":
                    _fill(x, y - 2 * scale, w, h, 12 * scale, (0.04, 0.10, 0.08, 0.25))
                    _fill(x, y, w, h, 12 * scale, EDGE)
                    _fill(x + scale, y + scale, w - 2 * scale, h - 2 * scale, 11 * scale, PAPER)
                    continue
                if style in {"title", "section"}:
                    color = INK if style == "title" else MUTED
                    if style == "title":
                        _fill(x, y + 3 * scale, 3 * scale, 16 * scale, scale, ACCENT)
                        x += 9 * scale
                        w -= 9 * scale
                    _text(x, y + 4 * scale, label, (16 if style == "title" else 12) * scale, w, color)
                    continue
                current = style == "active"
                fill = TEAL if current else MINT if part == hovered or style == "tab" else WHITE
                border = TEAL if current or part == hovered else EDGE
                _fill(x, y, w, h, 6 * scale, border)
                _fill(x + scale, y + scale, w - 2 * scale, h - 2 * scale, 5 * scale, fill)
                size = (14 if style == "action" else 13) * scale
                blf.size(0, max(8, round(size)))
                inset = 12 * scale if style == "action" else max(3 * scale, (w - blf.dimensions(0, label)[0]) * 0.5)
                _text(x + inset, y + (h - size) * 0.5 + scale, label, size,
                      w - inset - 3 * scale, WHITE if current else INK)
        finally:
            gpu.state.blend_set(blend)


def arrange(context, *, page, collapsed, sections, follow=None, title=None, origin=None, rotate=False,
            offset=(0,0)):
    """Return one geometry list for drawing and picking, inside the unobscured viewport."""
    region = context.region
    left, right = 0, region.width
    for other in context.area.regions:
        if other.width <= 1 or other.y + other.height <= region.y or other.y >= region.y + region.height:
            continue
        if other.type == "UI" and other.x < region.x + region.width:
            right = min(right, other.x - region.x)
        elif other.type == "TOOLS" and other.x + other.width > region.x:
            left = max(left, other.x + other.width - region.x)
    height = 46 if collapsed else 96 + sum(28 + len(rows) * 34 for _, rows in sections)
    ui_scale = context.preferences.system.ui_scale
    # Blender draws view navigation gizmos and tool headers above custom gizmos.
    if context.space_data.show_gizmo_navigate:
        right -= min(95 * ui_scale, (right - left) / 3)
    available = right - left
    if available < 135 or region.height < 70:
        return 1.0, []
    scale = min(ui_scale, available / 265, region.height / (height + 76))
    width = min(245 if collapsed else 310 if title else 350, available / scale - 20)
    rightmost = right - (width + 10) * scale
    highest = region.height - 62 * scale
    wm = context.window_manager
    if follow is not None:
        lo, hi, head_y = follow
        gap = 21 * scale
        base_x = hi + gap
        if base_x > rightmost and lo - left > right - hi:
            base_x = lo - gap - width * scale
        base_y = head_y + 30 * scale
        if origin is not None:
            origin.extend((base_x, base_y))
        dx, dy = offset
        x = min(rightmost, max(left + 10 * scale, base_x + dx))
        top = min(highest, max((height + 10) * scale, base_y + dy))
    elif wm.is_property_set("blockloom_workbench_position"):
        anchor_x, anchor_top = wm.blockloom_workbench_position
        x = min(rightmost, max(left + 10 * scale, anchor_x))
        top = min(highest, max((height + 10) * scale, anchor_top))
    else:
        x, top = rightmost, highest
    cells = []

    def cell(part, offset, down, w, h, style, text=""):
        cells.append((part, (x + offset * scale, top - (down + h) * scale, w * scale, h * scale), style, text))

    cell(BACKGROUND, 0, 0, width, height, "card")
    if collapsed:
        tab_width = (width - 61) / 3
        for i, (part, key, tab) in enumerate(_TABS):
            cell(part, 9 + i * (tab_width + 4), 8, tab_width, 30, "active" if page == key else "tab", tab)
        cell(CLOSE, width - 35, 10, 25, 25, "button", "×")
        return scale, cells
    title = title or ("BlockLoom · 悬浮工作台" if width >= 295 else "悬浮工作台")
    cell(TITLE, 0, 0, width - 72, 44, "handle")
    cell(-1, 14, 12, width - 90, 23, "title", title)
    cell(COLLAPSE, width - 69, 10, 25, 25, "button", "−")
    cell(CLOSE, width - 39, 10, 25, 25, "button", "×")
    tab_width = (width - 36) / 3
    for i, (part, key, tab) in enumerate(_TABS):
        cell(part, 14 + i * (tab_width + 4), 46, tab_width, 30, "active" if page == key else "tab", tab)
    down = 84
    for label, rows in sections:
        cell(-1, 15, down, width - 30, 20, "section", label)
        down += 26
        for row in rows:
            item_width = (width - 28 - (len(row) - 1) * 6) / len(row)
            for index, (part, caption) in enumerate(row):
                cell(part, 14 + index * (item_width + 6), down, item_width, 29, "active" if part == (ROTATE if rotate else IK) else "action", caption)
            down += 34
        down += 2
    return scale, cells


def clear_graphics():
    _rounded.cache_clear()
    _shader.cache_clear()
