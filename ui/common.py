"""Native presentation helpers adapted from local.31 (74ab71a).

All state belongs to WindowManager. Buttons are disabled native placeholders.
This module does not inspect resources, invoke business operators, or run timers.
"""

import logging
from typing import Any, cast

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Context, Preferences, PropertyGroup, Region, UILayout, UIList

logger = logging.getLogger(__name__)
_registered_states: dict[str, Any] = {}


class OXALMCSTUDIO_PG_preview_item(PropertyGroup):
    label: StringProperty(options={"SKIP_SAVE"})


class OXALMCSTUDIO_UL_preview(UIList):
    def draw_item(
        self,
        context,
        layout,
        data,
        item,
        icon,
        active_data,
        active_property,
        index=0,
        flt_flag=0,
    ):
        layout.label(text=getattr(item, "label", ""))


def annotations(fields, enums=None, colors=None):
    """Create presentation properties; deliberately no update/get/set functions."""
    result: dict[str, Any] = {}
    for name, (label, default) in fields.items():
        kw = {"name": label, "default": default, "options": {"SKIP_SAVE"}}
        if isinstance(default, bool):
            result[name] = BoolProperty(**kw)
        elif isinstance(default, int):
            result[name] = IntProperty(**kw)
        elif isinstance(default, float):
            result[name] = FloatProperty(**kw, precision=3)
        else:
            result[name] = StringProperty(**kw)
    for name, (label, items, default) in (enums or {}).items():
        result[name] = EnumProperty(
            name=label, items=items, default=default, options={"SKIP_SAVE"}
        )
    for name, (label, default) in (colors or {}).items():
        result[name] = FloatVectorProperty(
            name=label,
            default=default,
            size=4,
            subtype="COLOR",
            min=0,
            max=1,
            options={"SKIP_SAVE"},
        )
    result["items"] = CollectionProperty(
        type=OXALMCSTUDIO_PG_preview_item, options={"SKIP_SAVE"}
    )
    result["index"] = IntProperty(default=0, options={"SKIP_SAVE"})
    return result


def register_state(cls, name):
    if name in _registered_states or hasattr(bpy.types.WindowManager, name):
        raise RuntimeError("UI 状态属性已登记：" + name)
    bpy.utils.register_class(cls)
    _registered_states[name] = cls
    try:
        setattr(
            bpy.types.WindowManager, name, PointerProperty(type=cls, options={"SKIP_SAVE"})
        )
    except Exception as error:
        try:
            unregister_state(cls, name)
        except Exception as cleanup_error:
            logger.exception("UI state registration rollback failed: %s", name)
            error.add_note(f"UI cleanup failure: {type(cleanup_error).__name__}: {cleanup_error}")
        raise


def unregister_state(cls, name):
    if _registered_states.get(name) is not cls:
        return
    definition = bpy.types.WindowManager.bl_rna.properties.get(name)
    if definition is not None and getattr(definition, "fixed_type", None) == cls.bl_rna:
        delattr(bpy.types.WindowManager, name)
    bpy.utils.unregister_class(cls)
    del _registered_states[name]


class UI:
    def __init__(
        self,
        context: Context,
        state: Any,
        fields: dict,
        short: dict | None = None,
        full: set | None = None,
        captions: dict | None = None,
    ):
        self.context = context
        self.state = state
        self.schema = fields
        self.short = short or {}
        self.full = full or set()
        self.captions = captions or {}

    def units(self) -> float:
        region = cast(Region | None, self.context.region)
        if region is None or region.type != "UI":
            return 14.0
        view = region.view2d
        width = abs(
            view.region_to_view(region.width, 0)[0] - view.region_to_view(0, 0)[0]
        )
        preferences = cast(Preferences, self.context.preferences)
        return width / (20 * max(preferences.system.ui_scale, 0.01))

    def wide(self, minimum=12):
        return self.units() >= minimum

    @staticmethod
    def width(text):
        return sum(0.72 if ord(c) > 255 else 0.38 for c in str(text))

    def note(self, layout: UILayout, text: str, icon: Any = "NONE"):
        budget = max(4.0, self.units() - 4.0)
        line = ""
        used = 0.0
        first = True
        for char in text:
            weight = self.width(char)
            if char == "\n" or (line and used + weight > budget):
                layout.label(text=line, icon=icon if first else "NONE")
                line, used, first = "", 0.0, False
                if char == "\n":
                    continue
            line += char
            used += weight
        if line or first:
            layout.label(text=line, icon=icon if first else "NONE")

    def section(
        self, layout: UILayout, name: str, icon: Any = "NONE"
    ) -> UILayout | None:
        box = layout.box()
        box.prop(self.state, name, text=self.schema[name][0], icon=icon, emboss=False)
        return box if getattr(self.state, name) else None

    def prop(
        self,
        layout: UILayout,
        name: str,
        text: str | None = None,
        slider: bool | None = None,
        **kw,
    ):
        label = self.schema[name][0] if text is None else text
        label = self.captions.get(label, label)
        value = getattr(self.state, name)
        if slider is None:
            slider = isinstance(value, (int, float)) and not isinstance(value, bool)
        if kw.get("expand") and text == "":
            # Expanded enums need their native option captions, not a blank override.
            layout.prop(self.state, name, slider=slider, **kw)
            return
        layout.prop(self.state, name, text=label, slider=slider, **kw)

    def fields(self, layout: UILayout, *names: str):
        # Layout is a function of the available width, never of a live value.
        # Reflowing while a numeric button is dragged destroys the button's
        # position and can transfer the remainder of the drag to another row.
        units = self.units()
        numeric_reserve = 5.0

        def field(cell, name, short=False, available=None):
            label = (
                self.short.get(name, self.schema[name][0])
                if short
                else self.schema[name][0]
            )
            if self.width(label) + numeric_reserve > (available or units):
                cell.label(text=label)
                label = ""
            self.prop(cell, name, text=label)

        offset = 0
        while offset < len(names):
            name = names[offset]
            pair = False
            if offset + 1 < len(names) and name not in self.full:
                other = names[offset + 1]
                pair = other not in self.full and units >= 12.0
            if pair:
                row = layout.row(align=True)
                field(row.column(align=True), name, True, units / 2)
                field(row.column(align=True), names[offset + 1], True, units / 2)
                offset += 2
            else:
                field(layout, name)
                offset += 1

    def paired(self, layout: UILayout, *items: tuple[str, str]):
        parent = layout.row(align=True) if self.wide() else layout.column(align=True)
        for name, label in items:
            cell = parent.column(align=True)
            cell.label(text=label)
            self.prop(cell, name, text="", slider=True)

    @staticmethod
    def button(layout: UILayout, text: str, icon: Any = "NONE", depress=False):
        row = layout.row(align=True)
        row.enabled = False
        row.operator("wm.context_set_boolean", text=text, icon=icon, depress=depress)

    @staticmethod
    def listing(layout: UILayout, state: Any, name: str, rows=4):
        layout.template_list(
            "OXALMCSTUDIO_UL_preview",
            name,
            state,
            "items",
            state,
            "index",
            rows=rows,
            maxrows=rows,
        )

    def list_actions(self, layout, name, actions, rows=4):
        if self.wide():
            split = layout.split(factor=0.65)
            listing, buttons = split.column(), split.column(align=True)
        else:
            listing, buttons = layout, None
        self.listing(listing, self.state, name, rows)
        if buttons is None:
            buttons = (
                layout.row(align=True) if self.wide(9) else layout.column(align=True)
            )
        for text, icon in actions:
            self.button(buttons, text, icon)


def register():
    bpy.utils.register_class(OXALMCSTUDIO_PG_preview_item)
    try:
        bpy.utils.register_class(OXALMCSTUDIO_UL_preview)
    except Exception:
        bpy.utils.unregister_class(OXALMCSTUDIO_PG_preview_item)
        raise


def unregister():
    bpy.utils.unregister_class(OXALMCSTUDIO_UL_preview)
    bpy.utils.unregister_class(OXALMCSTUDIO_PG_preview_item)
