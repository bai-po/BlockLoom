"""Persistent native inputs; edits only invalidate their owning Scene."""

from dataclasses import dataclass
from typing import Any

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
)
from bpy.types import PropertyGroup, Scene

from ...core.runtime import ensure_mutation_allowed, mutation_allowed
from . import sky_inputs
from .sky_schema import (
    CLOUD_STRUCTURE_KEYS,
    PARAMETERS,
    RANGES,
    SOFT_RANGES,
    SPEED_ITEMS,
    snapshot,
    validated,
)

STRUCTURE_KEYS = set(CLOUD_STRUCTURE_KEYS)

VALUES = "oxal_mc_studio_sky_values"
SLOTS = "oxal_mc_studio_sky_slots"
SLOT_COUNT = 10
VALUE_KEYS = (*PARAMETERS, "preset_slot", "preset_keyframes")


@dataclass(frozen=True, slots=True)
class StoredValues:
    present: bool
    fields: dict[str, bool | int | float | str]


@dataclass(frozen=True, slots=True)
class StoredSlot:
    filled: bool | None
    values: StoredValues


@dataclass(frozen=True, slots=True)
class StoredInputs:
    values: StoredValues
    slots_present: bool
    slots: tuple[StoredSlot, ...]
    native: dict[str, Any]
    native_ui: dict[str, dict[str, Any]]


def _stored_values(values) -> StoredValues:
    if values is None:
        return StoredValues(False, {})
    fields = {}
    for key in VALUE_KEYS:
        if values.is_property_set(key):
            value = getattr(values, key)
            if not isinstance(value, (bool, int, float, str)):
                raise TypeError("天空参数存储含非标量值，未改写数据")
            fields[key] = value
    return StoredValues(True, fields)


def effective_values(scene):
    """The same active source the native controller reads, also used by the UI."""
    values = getattr(scene, VALUES)
    slots = getattr(scene, SLOTS)
    if values.preset_keyframes and 1 <= values.preset_slot <= len(slots):
        selected = slots[values.preset_slot - 1]
        if selected.filled:
            return selected.values
    return values


def effective_snapshot(scene):
    """Portable/save values reflect active scalars and the applied global modes."""
    from .sky import controller_for, root_for
    from .sky_modes import actual_structure

    result = snapshot(effective_values(scene))
    root = root_for(scene)
    result.update(actual_structure(controller_for(root)) if root is not None else {
        key: getattr(getattr(scene, VALUES), key) for key in CLOUD_STRUCTURE_KEYS})
    return result


def _native_ui(scene: Scene, key: str) -> Any:
    manager = scene.id_properties_ui(key)
    if manager is None:
        raise ValueError("天空原生输入缺少属性元数据：" + key)
    return manager


def capture_inputs(scene: Scene) -> StoredInputs:
    """Capture presence as well as values, before RNA creates default groups."""
    values = getattr(scene, VALUES) if scene.is_property_set(VALUES) else None
    slots_present = scene.is_property_set(SLOTS)
    slots = []
    for slot in getattr(scene, SLOTS) if slots_present else ():
        filled = slot.filled if slot.is_property_set("filled") else None
        saved = slot.values if slot.is_property_set("values") else None
        slots.append(StoredSlot(filled, _stored_values(saved)))
    native = {key: scene[key] for key in sky_inputs.managed_keys() if key in scene}
    native_ui = {key: _native_ui(scene, key).as_dict() for key in native}
    return StoredInputs(_stored_values(values), slots_present, tuple(slots), native, native_ui)


def _restore_values(values, stored: StoredValues) -> None:
    for key in VALUE_KEYS:
        if key in stored.fields:
            setattr(values, key, stored.fields[key])
        else:
            values.property_unset(key)


def restore_inputs(scene: Scene, stored: StoredInputs) -> None:
    ensure_mutation_allowed()
    if stored.values.present:
        _restore_values(getattr(scene, VALUES), stored.values)
    else:
        scene.property_unset(VALUES)
    slots = getattr(scene, SLOTS)
    while len(slots) > len(stored.slots):
        slots.remove(len(slots) - 1)
    for slot, previous in zip(slots, stored.slots, strict=True):
        if previous.filled is None:
            slot.property_unset("filled")
        else:
            slot.filled = previous.filled
        if previous.values.present:
            _restore_values(slot.values, previous.values)
        else:
            slot.property_unset("values")
    if not stored.slots_present:
        scene.property_unset(SLOTS)
    for key in sky_inputs.managed_keys():
        if key in scene and key not in stored.native:
            del scene[key]
    for key,value in stored.native.items():
        scene[key] = value
        _native_ui(scene, key).update(**stored.native_ui[key])
    scene.update_tag()


def _tag_inputs(self, _context):
    # Python-defined RNA values need explicit dependency invalidation. No graph
    # evaluation, Bus work or structural mutation happens in this callback.
    self.id_data.update_tag()


def _auto_time_changed(self, context):
    # Scalar input synchronization only; never dispatch business from RNA updates.
    if self.auto_time and mutation_allowed() and not self.id_data.is_evaluated:
        self.cloud_light_auto = True
    _tag_inputs(self, context)


class OXALMCSTUDIO_PG_sky_values(PropertyGroup):
    """Scene-owned inputs read by native drivers during playback/render."""


annotations: dict[str, Any] = {}
for key, (caption, default, _native) in PARAMETERS.items():
    options: dict[str, Any] = {
        "name": caption,
        "default": default,
        "update": _auto_time_changed if key == "auto_time" else _tag_inputs,
        "get": sky_inputs.getter(key, next(item[3] for item in SPEED_ITEMS if item[0] == default) if isinstance(default,str) else default),
        "set": sky_inputs.setter(key),
    }
    if key in STRUCTURE_KEYS:
        options["options"] = set()  # Explicit structure changes cannot be keyed.
    if isinstance(default, bool):
        prop = BoolProperty(**options)
    elif isinstance(default, str):
        prop = EnumProperty(items=SPEED_ITEMS, **options)
    else:
        low, high = RANGES[key]
        soft_low, soft_high = SOFT_RANGES.get(key, (low, high))
        if isinstance(default, int):
            prop = IntProperty(min=int(low), max=int(high), soft_min=int(soft_low), soft_max=int(soft_high), **options)
        else:
            prop = FloatProperty(min=low, max=high, soft_min=soft_low, soft_max=soft_high, precision=4, **options)
    annotations[key] = prop
annotations["preset_slot"] = IntProperty(
    name="保存值", default=1, min=1, max=SLOT_COUNT, update=_tag_inputs,
    get=sky_inputs.getter("preset_slot",1),set=sky_inputs.setter("preset_slot")
)
annotations["preset_keyframes"] = BoolProperty(
    name="关键帧自动切换", default=False, update=_tag_inputs,
    get=sky_inputs.getter("preset_keyframes",False),set=sky_inputs.setter("preset_keyframes")
)
OXALMCSTUDIO_PG_sky_values.__annotations__ = annotations


class OXALMCSTUDIO_PG_sky_slot(PropertyGroup):
    filled: BoolProperty(name="已保存", default=False, update=_tag_inputs,
                         get=sky_inputs.getter("filled",False),set=sky_inputs.setter("filled"))
    values: PointerProperty(type=OXALMCSTUDIO_PG_sky_values)


CLASSES = (OXALMCSTUDIO_PG_sky_values, OXALMCSTUDIO_PG_sky_slot)
_registered_properties: dict[str, Any] = {}
_registered_classes: list[Any] = []


def initialize_slots(scene: Scene) -> None:
    slots = getattr(scene, SLOTS)
    while len(slots) < SLOT_COUNT:
        slots.add()


def materialize_inputs(scene: Scene) -> None:
    """Persist RNA defaults only within an explicit import/activation operation."""
    ensure_mutation_allowed()
    initialize_slots(scene)
    values = getattr(scene, VALUES)
    slots = getattr(scene, SLOTS)
    if len(slots) > SLOT_COUNT:
        raise ValueError("天空保存值数量异常，未改写绑定")
    groups = (values, *(slot.values for slot in slots))
    prepared = []
    for group in groups:
        parameters = validated(snapshot(group))
        selected, keyframes = group.preset_slot, group.preset_keyframes
        if not 1 <= selected <= SLOT_COUNT:
            raise ValueError("天空保存值编号无效，未改写绑定")
        prepared.append((group, parameters, selected, keyframes))
    for group, parameters, selected, keyframes in prepared:
        for key, value in parameters.items():
            setattr(group, key, value)
        group.preset_slot = selected
        group.preset_keyframes = keyframes
    for slot in slots:
        slot.filled = slot.filled
    for index,group in ((None,values),*((i,slot.values) for i,slot in enumerate(slots))):
        for name in VALUE_KEYS:
            value = getattr(group,name)
            if isinstance(value,str):
                value = next(item[3] for item in SPEED_ITEMS if item[0] == value)
            sky_inputs.configure(scene,name,value,index)
    for index,slot in enumerate(slots):
        sky_inputs.configure(scene,"filled",slot.filled,index)


def register() -> None:
    if any(hasattr(Scene, name) for name in (VALUES, SLOTS)):
        raise ValueError("天空输入 RNA 属性已存在，未覆盖其它登记")
    registered = []
    created = []

    def install(name, definition):
        if hasattr(Scene, name):
            raise ValueError("天空输入 RNA 属性已存在，未覆盖其它登记：" + name)
        setattr(Scene, name, definition)
        created.append((name, Scene.bl_rna.properties[name]))

    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
        install(VALUES, PointerProperty(type=OXALMCSTUDIO_PG_sky_values))
        install(SLOTS, CollectionProperty(type=OXALMCSTUDIO_PG_sky_slot))
    except Exception:
        for name, prop in reversed(created):
            if Scene.bl_rna.properties.get(name) == prop:
                delattr(Scene, name)
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        raise
    _registered_properties.update(created)
    _registered_classes.extend(registered)


def unregister() -> None:
    # Keep ID-property payloads in scenes across a development reload.
    for name, prop in reversed(tuple(_registered_properties.items())):
        if Scene.bl_rna.properties.get(name) == prop:
            delattr(Scene, name)
        del _registered_properties[name]
    for cls in reversed(tuple(_registered_classes)):
        bpy.utils.unregister_class(cls)
        _registered_classes.remove(cls)
