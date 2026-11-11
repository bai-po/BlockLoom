"""Read-only live resource lists; no draw-time scene/state synchronization."""

from typing import TYPE_CHECKING, Any, cast

import bpy
from bpy.props import IntProperty, PointerProperty, StringProperty
from bpy.types import Collection, Context, Object, Scene, UIList

from ..core.importing import ImportEndpoint
from ..util.ownership import require_owned
from ..util.resources import ASSET_ID, INSTANCE
from . import common

if TYPE_CHECKING:
    from bpy.types import bpy_prop_array


def roots(context: Context, asset_id: str):
    scene = cast(Scene, context.scene)
    return [
        root
        for root in scene.collection.children_recursive
        if root.get(ASSET_ID) == asset_id
    ]


def select_target(state: Any, scene: Scene, root: Collection) -> None:
    """Remember a concrete UI choice, independently of collection ordering."""
    if root not in tuple(scene.collection.children_recursive) or not root.get(INSTANCE):
        raise ValueError("实例不属于当前场景")
    state.target = root
    state.target_instance = str(root[INSTANCE])
    state.target_scene_uid = str(scene.session_uid)
    if hasattr(state, "legacy_target_uid"):
        state.legacy_target = None
        state.legacy_target_uid = ""
        state.legacy_target_data_uid = ""
        state.legacy_target_version = ""
        state.human_selection_anchor_uid = _active_uid(bpy.context)


def human_entries(context: Context) -> dict[Any, tuple[Any, str]]:
    """Read live rigs for the human list without adopting legacy resources."""
    from ..business.human import human_compat
    from ..business.human.human import rig_of
    from ..business.human.human_source import model_version

    found = {}
    for root in roots(context, "oxal.human"):
        try:
            found[rig_of(root)] = root, model_version(root)
        except ValueError:
            continue
    scene = cast(Scene, context.scene)
    for obj in scene.objects:
        if obj.type == "ARMATURE" and obj not in found:
            target = human_compat._identify(scene, obj)
            if target is not None:
                found[obj] = None, target.version
    return found


def has_human_choice(state: Any) -> bool:
    """An explicit invalid list choice must not fall back to the active rig."""
    return bool(state.target_instance or state.target is not None
                or state.legacy_target_uid or state.legacy_target is not None)


def _remembered_legacy(context: Context, state: Any):
    """Validate the concrete legacy list choice without considering activity."""
    from ..business.human import human_compat

    pointer, identity = state.legacy_target, state.legacy_target_uid
    if pointer is None and not identity:
        return None
    scene = cast(Scene, context.scene)
    if state.target_scene_uid != str(scene.session_uid):
        raise ValueError("旧人模列表目标属于另一场景，请重新选择")
    matches = [obj for obj in scene.objects if str(obj.session_uid) == identity and obj.type == "ARMATURE"]
    if len(matches) != 1 or pointer is not None and pointer != matches[0]:
        raise ValueError("所选旧人模已删除或身份改变，请重新选择")
    rig = matches[0]
    target = human_compat._identify(scene, rig, version_hint=state.legacy_target_version)
    if (target is None or rig.data is None or str(rig.data.session_uid) != state.legacy_target_data_uid
            or target.version != state.legacy_target_version):
        raise ValueError("所选旧人模骨架或版本已改变，请重新选择")
    return target


def _active_object(context: Context):
    active = getattr(context, "object", None)
    view_layer = getattr(context, "view_layer", None)
    if active is None and view_layer is not None:
        active = view_layer.objects.active
    scene = cast(Scene | None, context.scene)
    return active if active is not None and scene is not None and scene.objects.get(active.name) == active else None


def _active_uid(context: Context) -> str:
    active = _active_object(context)
    return str(active.session_uid) if active is not None else "0"


def _viewport_human(context: Context, state: Any):
    from ..business.human import human_compat
    from ..business.human.human import resolve_human

    scene = cast(Scene, context.scene)
    active = _active_object(context)
    if active is None:
        return None
    candidates = [root for root in roots(context, "oxal.human") if active in tuple(root.all_objects)]
    if candidates:
        if len(candidates) != 1:
            return None
        try:
            root = resolve_human(scene, str(candidates[0].get(INSTANCE, "")))
        except ValueError:
            return None
        return root, None
    rigs = human_compat._rigs(active)
    if len(rigs) != 1:
        return None
    rig = rigs.pop()
    hint = (state.legacy_target_version if state.target_scene_uid == str(scene.session_uid)
            and state.legacy_target_uid == str(rig.session_uid)
            and rig.data is not None and state.legacy_target_data_uid == str(rig.data.session_uid) else None)
    target = human_compat._identify(scene, rig, version_hint=hint)
    return (None, target) if target is not None else None


def current_human(context: Context, state: Any):
    """Resolve viewport activity and explicit list choice without writing state.

    The list's selection Operator activates its rig through human.edit and the
    success projection records that activity. A later different proven viewport
    human takes precedence; unrelated activity retains the last explicit choice.
    """
    explicit = has_human_choice(state)
    if not explicit or _active_uid(context) != state.human_selection_anchor_uid:
        viewport = _viewport_human(context, state)
        if viewport is not None:
            return viewport
    if explicit:
        legacy = _remembered_legacy(context, state)
        if legacy is not None:
            return None, legacy
        return _remembered_target(state, cast(Scene, context.scene), "oxal.human"), None
    return None, None


def selected_legacy(context: Context, state: Any):
    """Use the same pure target decision as the human list and Beta commands."""
    return current_human(context, state)[1]


def _remembered_target(state: Any, scene: Scene, asset_id: str) -> Collection:
    if state.target_scene_uid != str(scene.session_uid):
        raise ValueError("目标实例属于另一场景，请重新选择")
    candidates = [root for root in scene.collection.children_recursive
                  if root.get(ASSET_ID) == asset_id
                  and root.get(INSTANCE) == state.target_instance]
    if len(candidates) != 1:
        raise ValueError("所选实例已删除或身份不唯一，请重新选择")
    target = candidates[0]
    if state.target is not None and state.target != target:
        raise ValueError("所选实例身份已变化，请重新选择")
    # Native memory-file Redo can restore the root while leaving a transient
    # WindowManager pointer null. Re-resolve only this explicit identity, then
    # prove the actual restored ID through the scene's ownership registry.
    require_owned(target, (target,))
    return target


def selection_properties(asset_id: str) -> dict[str, Any]:
    """Native list index is derived from its bound ID, never used as identity.

    Getter/setter callbacks only read data and update transient UI selection.
    They do not activate objects, invoke tools or repair ownership registries.
    """
    def get_index(state):
        if asset_id == "oxal.human":
            scene = cast(Scene | None, bpy.context.scene)
            if scene is None:
                return -1
            try:
                root, legacy = current_human(bpy.context, state)
                if legacy is not None:
                    target = legacy.rig
                elif root is not None:
                    from ..business.human.human import rig_of

                    target = rig_of(root)
                else:
                    return -1
            except ValueError:
                return -1
            position = bpy.data.objects.find(target.name)
            return position if position >= 0 and bpy.data.objects[position] == target else -1
        target = state.target
        if state.target_instance:
            scene = cast(Scene | None, bpy.context.scene)
            if scene is None:
                return -1
            try:
                target = _remembered_target(state, scene, asset_id)
            except ValueError:
                return -1
        elif target is None:
            return -1
        position = bpy.data.collections.find(target.name)
        return position if position >= 0 and bpy.data.collections[position] == target else -1

    def set_index(state, value):
        scene = cast(Scene | None, bpy.context.scene)
        if asset_id == "oxal.human":
            if scene is None or not 0 <= value < len(bpy.data.objects):
                return
            target = bpy.data.objects[value]
            entry = human_entries(bpy.context).get(target)
            if entry is None:
                return
            root, version = entry
            if root is not None:
                select_target(state, scene, root)
            else:
                state.target = None
                state.target_instance = ""
                state.target_scene_uid = str(scene.session_uid)
                state.legacy_target = target
                state.legacy_target_uid = str(target.session_uid)
                state.legacy_target_data_uid = str(target.data.session_uid)
                state.legacy_target_version = version
                state.human_selection_anchor_uid = _active_uid(bpy.context)
            return
        if scene is None or not 0 <= value < len(bpy.data.collections):
            return
        target = bpy.data.collections[value]
        if target.get(ASSET_ID) == asset_id and target in tuple(scene.collection.children_recursive):
            select_target(state, scene, target)

    result = {
        "target": PointerProperty(type=Collection, options={"SKIP_SAVE"}),
        "target_instance": StringProperty(options={"SKIP_SAVE"}),
        "target_scene_uid": StringProperty(options={"SKIP_SAVE"}),
        "index": IntProperty(min=-1, get=get_index, set=set_index, options={"SKIP_SAVE"}),
    }
    if asset_id == "oxal.human":
        result.update({
            "legacy_target": PointerProperty(type=Object, options={"SKIP_SAVE"}),
            "legacy_target_uid": StringProperty(options={"SKIP_SAVE"}),
            "legacy_target_data_uid": StringProperty(options={"SKIP_SAVE"}),
            "legacy_target_version": StringProperty(options={"SKIP_SAVE"}),
            "human_selection_anchor_uid": StringProperty(options={"SKIP_SAVE"}),
        })
    return result


def selected_root(context: Context, state: Any, asset_id: str) -> Collection:
    """Resolve the displayed target; an invalid explicit choice stays invalid."""
    scene = cast(Scene, context.scene)
    if asset_id == "oxal.human":
        root, legacy = current_human(context, state)
        if legacy is not None:
            raise ValueError("当前选中旧人模，请使用兼容设置")
        if root is None:
            raise ValueError("请在视口或实例列表中明确选择一个人模")
        return root
    candidates = roots(context, asset_id)
    target = state.target
    identity = state.target_instance
    if identity or target is not None:
        return _remembered_target(state, scene, asset_id)
    active = context.object
    found = [root for root in candidates if active is not None and active in tuple(root.all_objects)]
    if len(found) != 1:
        raise ValueError("请在视口或实例列表中明确选择一个实例")
    return found[0]


class _ResourceList(UIList):
    asset_id = ""

    def filter_items(self, context, data, property):
        allowed = set(roots(context, self.asset_id))
        flags = [
            self.bitflag_filter_item if item in allowed else 0
            for item in getattr(data, property)
        ]
        # Blender accepts Python lists; the generated stub uses RNA array types.
        return cast("tuple[bpy_prop_array[int], bpy_prop_array[int]]", (flags, []))

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
        if item is not None:
            layout.label(text=item.name, icon="OUTLINER_COLLECTION")


class OXALMCSTUDIO_UL_human_instances(_ResourceList):
    asset_id = "oxal.human"

    def filter_items(self, context, data, property):
        allowed = human_entries(context)
        flags = [self.bitflag_filter_item if item in allowed else 0 for item in getattr(data, property)]
        return cast("tuple[bpy_prop_array[int], bpy_prop_array[int]]", (flags, []))

    def draw_item(self, context, layout, data, item, icon, active_data, active_property,
                  index: int | None = 0, flt_flag: int | None = 0):
        from ..business.human import human_compat
        from ..business.human.human import rig_of
        from ..business.human.human_source import model_version

        if item is None or item.type != "ARMATURE":
            return
        legacy = human_compat._identify(context.scene, item)
        if legacy is not None:
            operator = layout.operator("oxal_mc_studio.human_pick", text=item.name + " · V" + legacy.version,
                                       icon="ARMATURE_DATA", emboss=False)
            operator.scene_uid = str(context.scene.session_uid)
            operator.rig_uid = str(item.session_uid)
            return
        for root in roots(context, self.asset_id):
            try:
                if rig_of(root) == item:
                    operator = layout.operator("oxal_mc_studio.human_pick", text=root.name + " · V" + model_version(root),
                                               icon="OUTLINER_COLLECTION", emboss=False)
                    operator.scene_uid = str(context.scene.session_uid)
                    operator.rig_uid = str(item.session_uid)
                    return
            except ValueError:
                continue


class OXALMCSTUDIO_UL_inventory_instances(_ResourceList):
    asset_id = "oxal.inventory"


def draw(ui: common.UI, layout, feature: str, actions, *, caption: str = "", description: str = ""):
    if ui.wide():
        split = layout.split(factor=0.65)
        listing, buttons = split.column(), split.column(align=True)
    else:
        listing, buttons = layout, None
    listing.template_list(
        f"OXALMCSTUDIO_UL_{feature}_instances",
        feature,
        bpy.data,
        "objects" if feature == "human" else "collections",
        ui.state,
        "index",
        rows=5,
        maxrows=5,
    )
    if buttons is None:
        buttons = layout.row(align=True) if ui.wide(9) else layout.column(align=True)
    for text, icon, operator, at_cursor in actions:
        if operator:
            op = buttons.operator(operator, text=text, icon=icon)
            if at_cursor is not None:
                op.at_cursor = at_cursor
        else:
            ui.button(buttons, text, icon)
    if caption:
        buttons.separator()
        buttons.label(text=caption)
    if description:
        buttons.label(text=description)


def status(ui: common.UI, layout, endpoint: ImportEndpoint, asset_id: str, label: str):
    present = roots(ui.context, asset_id)
    if asset_id == "oxal.human":
        ui.note(layout, f"{label}：{len(human_entries(ui.context))} 个实例", "INFO")
    else:
        ui.note(layout, f"{label}：{len(present)} 个独立实例", "INFO")
    scene = cast(Scene, ui.context.scene)
    state = endpoint.state(scene.session_uid)
    if endpoint.busy(scene.session_uid):
        ui.note(layout, "正在导入…", "TIME")
    elif state.message and (
        not state.succeeded
        or state.result is not None
        and any(root.get(INSTANCE) == state.result.instance_id for root in present)
    ):
        ui.note(layout, state.message, "CHECKMARK" if state.succeeded else "ERROR")


CLASSES = (OXALMCSTUDIO_UL_human_instances, OXALMCSTUDIO_UL_inventory_instances)


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
