"""Explicit native editing of recognized legacy humans, without adopting IDs.

The catalog is loaded at human-module registration, never by panel drawing.
Compatibility retains the original meshes, rig, Actions and native node graphs.
Native capabilities are exposed through explicit commands; no old callbacks run.
"""

import json
import math
import re
from dataclasses import dataclass
from typing import Any

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.animation_edit import require_keyframe_edit, require_property_edit
from ...util.geometry_nodes import get_input, set_input
from ...util.geometry_nodes import input_path as geometry_input_path
from ...util.resources import ASSET_ID, resolve_scene, resolve_view_layer
from .human_source import CURRENT, DATA, HEAD_HD_MARKER
from .human_state import ARMS, LEGS, OWNER, PARTS

_catalog: dict[str, Any] | None = None
_VERSION = re.compile(r"(?<!\d)(2\.\d+\.\d+)(?!\d)")
_CORE = ("根", "上身", "肚", "胸口", "肩膀", "脖子", "头")
_LAYER = {
    "outer": "对外厚度", "inner": "对内厚度", "random_enabled": "随机高低凸出",
    "random_factor": "随机系数", "height_ratio": "高低比例", "seed": "随机种子",
    "center_falloff": "中心高外围低", "center_range": "中心衰减范围",
}
_ARMOR = {"material": "材质编号", "pattern": "花纹编号", "leather_color": "皮革颜色", "trim_color": "花纹颜色"}
_MATERIALS = ("leather", "chainmail", "copper", "diamond", "gold", "iron", "netherite", "turtle_scute")
_HEAD_CHANNELS = (
    ("标准大眼睛", -.02, "0.0", "0.039998"),
    ("中下大眼睛", -.06, "0.03999", "0.079999"),
    ("中位大眼睛", -.10, "0.079999", "0.11999"),
    ("下半小眼睛", -.14, "0.11999", "0.15999"),
    ("中下小眼睛", -.18, "0.15999", "0.1999"),
    ("中位小眼睛", -.22, "0.19999", "0.23999"),
    ("中上小眼睛", -.26, "0.23999", "0.27999"),
    ("无脸", -.30, "0.27999", "0.5"),
)


@dataclass(frozen=True)
class LegacyHuman:
    rig: Any
    version: str
    members: tuple[Any, ...]
    containers: tuple[Any, ...]
    editable: bool
    reason: str


def prepare_catalog():
    global _catalog
    from . import human_compat_name, human_compat_parts
    document = json.loads((DATA / "LEGACY_MIGRATION.json").read_text(encoding="utf8"))
    if document.get("schema") != 1 or any(name not in document["anchors"] for name in _CORE):
        raise ValueError("旧人模结构声明不完整")
    document["roles"] = frozenset({item["name"] for profile in document["sources"] for item in profile["object_roles"]}
                                  | {"头部 · 八型单网格"})
    _catalog = document
    try:
        human_compat_parts.prepare_catalog()
        human_compat_name.prepare_catalog()
    except Exception:
        clear_catalog()
        raise


def clear_catalog():
    global _catalog
    from . import human_compat_name, human_compat_parts

    _catalog = None
    human_compat_parts.clear_catalog()
    human_compat_name.clear_catalog()


def _rigs(obj):
    if obj is None:
        return set()
    if obj.type == "ARMATURE":
        return {obj}
    found, seen, todo = set(), set(), [obj]
    while todo:
        item = todo.pop()
        if item in seen:
            continue
        seen.add(item)
        if item.type == "ARMATURE":
            found.add(item)
            continue
        if item.parent is not None:
            todo.append(item.parent)
        for modifier in item.modifiers:
            if modifier.type in {"ARMATURE", "LATTICE"} and modifier.object is not None:
                todo.append(modifier.object)
        if item.type == "FONT":
            for constraint in item.constraints:
                if (constraint.type == "CHILD_OF" and constraint.target is not None
                        and constraint.target.type == "ARMATURE" and constraint.subtarget == "头"):
                    todo.append(constraint.target)
    return found


def _named(name, choices):
    while name not in choices:
        prefix, separator, suffix = name.rpartition(".")
        if not separator or not suffix.isdigit():
            return None
        name = prefix
    return name


def _role(obj):
    if _catalog is None:
        return None
    return _named(str(obj.get("oxal_mc_studio_human_source", obj.name)), _catalog["roles"])


def _local(owner):
    return owner.library is None and owner.override_library is None


def _identify(scene, rig, *, version_hint=None):
    if _catalog is None or rig.type != "ARMATURE" or scene.objects.get(rig.name) != rig:
        return None
    # A copied/broken Beta ownership marker is not a legacy escape hatch.
    if rig.get(OWNER) or any(c.get(ASSET_ID) == "oxal.human" for c in rig.users_collection):
        return None
    for name in _CORE:
        bone = rig.data.bones.get(name)
        if bone is None or (bone.parent.name if bone.parent else "") != _catalog["anchors"][name]["parent"]:
            return None
    containers = tuple(rig.users_collection)
    shapes = {bone.custom_shape for bone in rig.pose.bones if bone.custom_shape is not None}
    candidates = {obj for collection in containers for obj in collection.all_objects}
    members = tuple(obj for obj in candidates if obj not in shapes and scene.objects.get(obj.name) == obj
                    and _rigs(obj) == {rig})
    if not any(obj.type == "MESH" and _role(obj) == "身体" for obj in members):
        return None
    versions = {item["version"] for item in _catalog["sources"]} | {CURRENT}
    names = set(rig.data.bones.keys())
    # These authored coarse-arm aliases distinguish 2.3.5's stale labels.
    # Other known roles may be legitimate user attachments or later facial
    # components; they are not an entire-model version contract.
    coarse_roles = {"L粗隔壁", "R粗隔壁", "L粗胳膊", "R粗胳膊"}
    seen_roles = {_role(obj) for obj in members if obj.type == "MESH"} & coarse_roles
    matching_versions = {profile["version"] for profile in _catalog["sources"]
                         if set(profile["bone_names"]) <= names
                         and seen_roles <= {row["name"] for row in profile["object_roles"]}}
    # Explicit declarations override old names retained by later authored rigs.
    version = None
    private_containers = [c for c in containers if {o for o in c.all_objects if o.type == "ARMATURE"} == {rig}]
    owners = (rig, rig.data, *private_containers)
    for field in ("bp_model_version", "人模版本", "bp_asset_version"):
        declared = {str(owner[field]) for owner in owners if field in owner and str(owner[field]) in versions}
        if len(declared) > 1:
            return None
        if declared:
            version = declared.pop()
            break
    if version is None:
        hints = {match for owner in (*private_containers, rig, rig.data)
                 for match in _VERSION.findall(owner.name) if match in versions}
        if len(hints) > 1:
            return None
        authored_hints = {match for profile in _catalog["sources"] if profile["version"] == version_hint
                          for label in (profile["rig"], profile["collection"])
                          for match in _VERSION.findall(label)}
        keep_hint = (isinstance(version_hint, str) and version_hint in matching_versions
                     and (not hints or hints <= authored_hints))
        if hints and not hints & matching_versions and CURRENT not in hints:
            # Some originals kept a previous version's display labels even
            # after native roles changed. Do not restore that older template.
            hints = set()
        if keep_hint and isinstance(version_hint, str):
            version = version_hint
        elif len(hints) == 1:
            version = hints.pop()
        else:
            matches = sorted(matching_versions)
            if not matches:
                return None
            version = matches[0] if len(matches) == 1 else matches[0] + "–" + matches[-1]
    local = (_local(rig) and _local(rig.data) and len(rig.users_scene) == 1
             and all(len(obj.users_scene) == 1 for obj in members))
    return LegacyHuman(rig, version, members, containers, local,
                       "" if local else "关联库／跨场景共享人模仅识别显示；请先建立本地独立副本")


def selected(context):
    active = getattr(context, "object", None)
    if active is None:
        active = context.view_layer.objects.active
    candidates = _rigs(active)
    return _identify(context.scene, candidates.pop()) if len(candidates) == 1 else None


def capture(scene, target):
    if not target.editable:
        raise ValueError(target.reason)
    return {"scene_uid": scene.session_uid, "rig_uid": target.rig.session_uid,
            "data_uid": target.rig.data.session_uid, "version": target.version}


def _resolve(scene, token):
    if not isinstance(token, dict) or token.get("scene_uid") != scene.session_uid:
        raise ValueError("旧人模目标场景已改变，请重新提交")
    matches = [obj for obj in scene.objects if obj.session_uid == token.get("rig_uid") and obj.type == "ARMATURE"]
    target = _identify(scene, matches[0], version_hint=token.get("version")) if len(matches) == 1 else None
    if (target is None or target.rig.data.session_uid != token.get("data_uid")
            or target.version != token.get("version")):
        raise ValueError("旧人模骨架或版本已改变，请重新选择")
    if not target.editable:
        raise ValueError(target.reason)
    return target


def _groups(target, choices):
    found = {}
    for obj in target.members:
        for collection in obj.users_collection:
            role = _named(collection.name, choices)
            if role is not None:
                if role in found and found[role] != collection:
                    raise ValueError("旧人模样式合集不唯一：" + role)
                found[role] = collection
    return found


def _visible(obj):
    return not obj.hide_render and obj.visible_get(view_layer=bpy.context.view_layer)


def _style(target, choices):
    groups = _groups(target, set(choices.values()))
    active = [name for name, label in choices.items() if label in groups
              and any(_visible(obj) for obj in groups[label].all_objects if obj.type == "MESH")]
    return active[0] if len(active) == 1 else "MIXED"


def _part(obj):
    role = _role(obj)
    if role == "腿甲右手":
        return "chest"
    if role is None or not role.startswith("盔甲"):
        return None
    return "head" if "头" in role else "feet" if "脚" in role else "legs" if "腿" in role or "裤" in role else "chest"


def _armors(target, parts):
    return tuple(obj for obj in target.members if obj.type == "MESH" and _part(obj) in parts)


def _layers(target):
    return tuple(obj for obj in target.members if obj.type == "MESH"
                 and ("二层" in str(_role(obj)) or _role(obj) == "身体第二层"))


def _socket(modifier, name):
    return next((socket for socket in modifier.node_group.interface.items_tree
                 if getattr(socket, "in_out", "") == "INPUT" and socket.name == name
                 and getattr(socket, "item_type", "SOCKET") == "SOCKET"), None)


def _layer_mods(target):
    return tuple((obj, modifier) for obj in _layers(target) for modifier in obj.modifiers
                 if modifier.type == "NODES" and modifier.node_group
                 and modifier.node_group.get("bp_second_layer_geometry"))


def _armor_mods(obj):
    return tuple(modifier for modifier in obj.modifiers if modifier.type == "NODES" and modifier.node_group
                 and not modifier.node_group.get("bp_armor_envelope214")
                 and _socket(modifier, "厚度") is not None and _socket(modifier, "Image") is not None)


def _selector(material):
    if not material or not material.node_tree:
        return None
    found = [node for node in material.node_tree.nodes if node.type == "GROUP" and node.node_tree
             and all(name in node.inputs for name in _ARMOR.values())]
    if len(found) > 1:
        raise ValueError("旧盔甲材质选择器不唯一")
    return found[0] if found else None


def _skin_objects(target):
    from .human_outline import _BACK_SOURCES, _BODY_SOURCES, _HEAD_SOURCES

    roles = (_BODY_SOURCES | _HEAD_SOURCES | {"第二层", "L粗胳膊", "R粗胳膊"}) - _BACK_SOURCES
    return tuple(obj for obj in target.members if obj.type == "MESH" and _role(obj) in roles)


def _skin_images(target):
    return tuple({node.image for obj in _skin_objects(target) for slot in obj.material_slots
                  if slot.material and slot.material.node_tree for node in slot.material.node_tree.nodes
                  if node.type == "TEX_IMAGE" and node.image and node.image.source in {"FILE", "GENERATED"}})


def _head_choices(target):
    if target.rig.pose.bones.get("面部兼容") is None:
        return ()
    choices = []
    for role, position, lower, upper in _HEAD_CHANNELS:
        objects = [obj for obj in target.members if obj.type == "MESH" and _role(obj) == role]
        if len(objects) != 1:
            continue
        animation = objects[0].animation_data
        curves = tuple(animation.drivers) if animation is not None else ()
        expected = f"0if{lower}<=-var<={upper}else1"
        valid = True
        for field in ("hide_viewport", "hide_render"):
            matching = [curve for curve in curves if curve.data_path == field]
            if len(matching) != 1:
                valid = False
                break
            driver = matching[0].driver
            variables = tuple(driver.variables)
            if (driver.expression.replace(" ", "") != expected or len(variables) != 1
                    or variables[0].name != "var" or variables[0].type != "TRANSFORMS"):
                valid = False
                break
            native = variables[0].targets[0]
            if (native.id != target.rig or native.bone_target != "面部兼容"
                    or native.transform_type != "LOC_Z" or native.transform_space != "LOCAL_SPACE"):
                valid = False
                break
        if valid:
            choices.append((role, position))
    return tuple(choices)


def _head_value(transaction, scene, target, position, *, keyframe, frame):
    bone = target.rig.pose.bones["面部兼容"]
    path = bone.path_from_id("location")
    before = tuple(bone.location)
    if not keyframe:
        require_property_edit(target.rig, path)
        _set(transaction, bone, "location", (before[0], before[1], position))
        return
    frame = _number(frame, -1048574, 1048574, "头型关键帧")
    if abs(frame - scene.frame_current_final) > 1e-5:
        raise ValueError("当前帧已改变，请重新提交头型关键帧")
    require_keyframe_edit(target.rig, path)
    animation = target.rig.animation_data
    original_action = animation.action if animation else None
    original_slot = animation.action_slot if animation else None
    fields = ("co", "handle_left_type", "handle_right_type", "handle_left", "handle_right",
              "interpolation", "type", "easing", "amplitude", "back", "period",
              "select_control_point", "select_left_handle", "select_right_handle")
    curves = [curve for curve in action_fcurves(original_action, slot=original_slot)
              if curve.data_path == path and curve.array_index == 2]
    if len(curves) > 1 or any(curve.modifiers or curve.sampled_points or curve.lock for curve in curves):
        raise ValueError("头型动画通道包含多层、烘焙或修改器，请使用原动画编辑")
    saved = [{field: tuple(value) if hasattr(value, "to_tuple") else value
              for field in fields if (value := getattr(point, field)) is not None}
             for point in curves[0].keyframe_points] if curves else []

    def restore():
        if original_action is None:
            if animation is None:
                target.rig.animation_data_clear()
            else:
                animation.action = None
        elif curves:
            points = curves[0].keyframe_points
            points.clear()
            points.add(len(saved))
            for point, values in zip(points, saved, strict=True):
                for field in fields:
                    setattr(point, field, values[field])
            curves[0].update()
        else:
            for curve in action_fcurves(original_action, slot=original_slot):
                if curve.data_path != path or curve.array_index != 2:
                    continue
                if original_action.layers:
                    for layer in original_action.layers:
                        for strip in layer.strips:
                            for bag in getattr(strip, "channelbags", ()):
                                if curve in tuple(bag.fcurves):
                                    bag.fcurves.remove(curve)
                else:
                    original_action.fcurves.remove(curve)
        bone.location = before

    transaction.on_rollback(restore)
    if original_action is None:
        data = target.rig.animation_data_create()
        data.action = transaction.track(bpy.data.actions.new("旧人模 · 头型"))
    bone.location.z = position
    if not target.rig.keyframe_insert(data_path=path, index=2, frame=frame):
        raise ValueError("头型关键帧未能写入，已撤回")


def summary(target):
    from . import human_compat_controls, human_compat_parts

    images = _skin_images(target)
    armor = _armors(target, PARTS)
    heads = _head_choices(target)
    controller = target.rig.pose.bones.get("面部兼容")
    return {**human_compat_controls.settings(target),
            "arm_mode": _style(target, ARMS), "leg_mode": _style(target, LEGS),
            "head_modes": tuple((role, role) for role, _ in heads),
            "head_mode": next((role for role, position in heads
                               if controller is not None and abs(controller.location.z - position) < .02), None),
            "arm_modes": tuple(key for key, label in ARMS.items() if label in _groups(target, set(ARMS.values()))
                               or human_compat_parts.available(target.version, "arm_mode", key)),
            "leg_modes": tuple(key for key, label in LEGS.items() if label in _groups(target, set(LEGS.values()))
                               or human_compat_parts.available(target.version, "leg_mode", key)),
            "armor_parts": tuple(part for part in PARTS if any(_part(obj) == part for obj in armor)),
            "armor_available": tuple(part for part in PARTS if any(_part(obj) == part for obj in armor)
                                     or human_compat_parts.available(target.version, "armor", (part,))),
            "layer_count": len(_layers(target)), "layer_3d": bool(_layer_mods(target)),
            "armor_selector": any(_selector(slot.material) is not None for obj in armor for slot in obj.material_slots),
            "skin_images": images, "skin_label": " / ".join(sorted(image.name for image in images)) or "未识别主皮肤"}


def settings(target, kind, parts=()):
    if kind == "layer":
        mods = _layer_mods(target)
        if not mods:
            raise ValueError("此版本没有原生二层3D参数")
        modifier = mods[0][1]
        values = {}
        for key, name in _LAYER.items():
            socket = _socket(modifier, name)
            if socket is None:
                raise ValueError("旧二层参数缺失：" + name)
            values[key] = get_input(modifier, socket.identifier)
        return values
    objects = _armors(target, parts)
    if not objects:
        raise ValueError("旧人模没有所选盔甲部位")
    selectors = [node for obj in objects for slot in obj.material_slots if (node := _selector(slot.material)) is not None]
    result = {"material": 5, "pattern": 0, "leather_color": (.5, .25, .1, 1.),
              "trim_color": (1., .65, .1, 1.), "selector": bool(selectors),
              "three_d": False, "thickness": .03, "three_d_available": False}
    if selectors:
        result.update({key: tuple(value) if hasattr(value, "__len__") else value
                       for key, name in _ARMOR.items() if (value := selectors[0].inputs[name].default_value) is not None})
    for obj in objects:
        mods = _armor_mods(obj)
        if mods:
            socket = _socket(mods[0], "厚度")
            if socket is None:
                raise ValueError("旧盔甲厚度接口缺失")
            result.update(three_d_available=True, three_d=mods[0].show_viewport and mods[0].show_render,
                          thickness=get_input(mods[0], socket.identifier))
            break
    return result


def _set(transaction, owner, field, value):
    actual_owner = getattr(owner, "id_data", owner)
    if isinstance(actual_owner, bpy.types.ID) and not _local(actual_owner):
        raise ValueError("旧人模包含只读关联数据，未修改")
    if isinstance(actual_owner, bpy.types.ID):
        prop = owner.bl_rna.properties.get(field)
        if prop is not None and prop.is_animatable:
            require_property_edit(actual_owner, owner.path_from_id(field))
    before = getattr(owner, field)
    if hasattr(before, "to_tuple"):
        before = tuple(before)
    transaction.on_rollback(lambda: setattr(owner, field, before))
    setattr(owner, field, value)


def _input(transaction, modifier, socket, value):
    if socket is None:
        raise ValueError("旧版原生参数接口缺失")
    if not _local(modifier.id_data):
        raise ValueError("旧人模修改器来自关联库，未修改")
    require_property_edit(modifier.id_data, geometry_input_path(modifier, socket.identifier))
    previous = get_input(modifier, socket.identifier)
    transaction.on_rollback(lambda: set_input(modifier, socket.identifier, previous))
    set_input(modifier, socket.identifier, value)


def _layer_nodes(view, collection):
    todo = [view.layer_collection]
    while todo:
        item = todo.pop()
        if item.collection == collection:
            yield item
        todo.extend(item.children)


def _enable_collection(transaction, scene, target, collection, enabled):
    if any(obj.type in {"MESH", "LATTICE", "ARMATURE"} and obj not in target.members
           for obj in collection.all_objects):
        raise ValueError("样式合集包含其它人模，未改变共享合集")
    _set(transaction, collection, "hide_render", not enabled)
    _set(transaction, collection, "hide_viewport", not enabled)
    for view in scene.view_layers:
        for node in _layer_nodes(view, collection):
            _set(transaction, node, "exclude", not enabled)
            _set(transaction, node, "hide_viewport", False)
        view.update()


def _visibility(transaction, scene, objects, enabled):
    for obj in objects:
        _set(transaction, obj, "hide_render", not enabled)
        _set(transaction, obj, "hide_viewport", not enabled)
        for view in scene.view_layers:
            if view.objects.get(obj.name) == obj:
                previous = obj.hide_get(view_layer=view)
                transaction.on_rollback(lambda obj=obj, view=view, previous=previous: obj.hide_set(previous, view_layer=view))
                obj.hide_set(not enabled, view_layer=view)


def _journal_selection(transaction, layer):
    """Restore Bases lost when a native collection is excluded then rolled back."""
    active = layer.objects.active
    selected = tuple(obj for obj in layer.objects if obj.select_get(view_layer=layer))
    mode = bpy.context.mode

    def restore():
        # Registered first, restored last: visibility and collection membership
        # must be back before Blender can recreate the original selection.
        layer.update()
        if bpy.context.mode != mode and bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        present = set(layer.objects)
        for obj in tuple(layer.objects):
            obj.select_set(obj in selected, view_layer=layer)
        layer.objects.active = active if active in present else None
        if mode == "POSE" and bpy.context.mode != "POSE" and active in present:
            bpy.ops.object.mode_set(mode="POSE")

    transaction.on_rollback(restore)


def _armor_visibility(transaction, scene, target, parts, enabled):
    objects = _armors(target, parts)
    if not objects:
        raise ValueError("旧人模没有所选盔甲部位")
    groups = _groups(target, {"盔甲"})
    if enabled and "盔甲" in groups:
        other_parts = tuple(obj for obj in _armors(target, PARTS) if obj not in objects)
        previous = {obj: _visible(obj) for obj in other_parts}
        _enable_collection(transaction, scene, target, groups["盔甲"], True)
        for obj, visible in previous.items():
            _visibility(transaction, scene, (obj,), visible)
    leg = _style(target, LEGS)
    if enabled and leg == "MIXED" and set(parts) & {"legs", "feet"}:
        raise ValueError("请先选择常规或官方腿型，再开启下肢盔甲")
    for obj in objects:
        role = _role(obj)
        if role is None:
            raise ValueError("旧盔甲部位来源已改变")
        official = "-官方腿" in role
        sided = any(side in role for side in ("左", "右"))
        visible = enabled and (_part(obj) not in {"legs", "feet"} or not sided or official == (leg == "OFFICIAL"))
        _visibility(transaction, scene, (obj,), visible)


def _number(value, minimum, maximum, name):
    if not isinstance(value, (float, int)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("旧人模参数超出范围：" + name)
    return value


def _private_material(transaction, obj, index, cache):
    if not _local(obj):
        raise ValueError("旧人模对象来自关联库，未替换其材质")
    slot = obj.material_slots[index]
    material = slot.material
    if material is None:
        raise ValueError("旧人模材质槽为空")
    if material not in cache:
        cache[material] = transaction.track(material.copy())
    before = slot.link, slot.material
    before_mesh = obj.data

    def restore():
        if obj.data != before_mesh or index >= len(obj.material_slots):
            raise ValueError("旧人模材质绑定已被外部修改，未恢复到其它网格")
        obj.material_slots[index].link = before[0]
        obj.material_slots[index].material = before[1]

    transaction.on_rollback(restore)
    slot.link = "OBJECT"
    slot.material = cache[material]
    return slot.material


def _image(transaction, path, skin=False):
    from .human import _png

    file = _png(str(path), skin=skin)
    image = transaction.track(bpy.data.images.load(str(file), check_existing=False))
    if image.size[0] <= 0 or not image.has_data:
        raise ValueError("贴图无法解码")
    image.pack()
    return image


def _apply_skin(transaction, target, filepath, image_uid):
    from .human_skin_history import recent_paths, remember

    is_default = not filepath
    if not is_default:
        recent_paths(target.rig)
    if not filepath:
        from .human import builtin_skins

        filepath = builtin_skins()[0][1]
    objects = _skin_objects(target)
    candidates = _skin_images(target)
    if image_uid:
        matches = [image for image in candidates if image.session_uid == image_uid]
    else:
        from .human_source import SKIN_ROLE, skin_nodes

        matches = list({node.image for obj in objects for slot in obj.material_slots
                        if slot.material and slot.material.node_tree for node in slot.material.node_tree.nodes
                        if node.type == "TEX_IMAGE" and node.image in candidates
                        and (node.name == "图像纹理" or slot.material.get(SKIN_ROLE) == "skin"
                             and node in skin_nodes(slot.material))})
    if len(matches) != 1:
        raise ValueError("主皮肤通道不唯一，请先选择原图通道")
    original = matches[0]
    image = _image(transaction, filepath, skin=True)
    cache = {}
    for obj in objects:
        for index, slot in enumerate(obj.material_slots):
            material = slot.material
            if not material or not material.node_tree or not any(n.type == "TEX_IMAGE" and n.image == original for n in material.node_tree.nodes):
                continue
            private = _private_material(transaction, obj, index, cache)
            for node in private.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image == original:
                    node.image = image
    for obj in objects:
        for modifier in obj.modifiers:
            if (modifier.type != "NODES" or not modifier.node_group
                    or not (modifier.node_group.get("bp_second_layer_geometry") or modifier.node_group.get(HEAD_HD_MARKER))):
                continue
            socket = _socket(modifier, "Image")
            if socket is not None and get_input(modifier, socket.identifier) == original:
                _input(transaction, modifier, socket, image)
    if not is_default:
        remember(transaction, target.rig, image.filepath_raw)


def _armor_settings(transaction, target, parts, values):
    objects = _armors(target, parts)
    if not objects:
        raise ValueError("旧人模没有所选盔甲部位")
    if set(values) - {*_ARMOR, "three_d", "thickness"}:
        raise ValueError("未知旧盔甲设置")
    if type(values.get("material", 5)) is not int:
        raise TypeError("旧盔甲材质编号必须为整数")
    material_id = int(_number(values.get("material", 5), 0, 7, "材质"))
    cache, images = {}, {}
    for obj in objects:
        for index, slot in enumerate(obj.material_slots):
            if slot.material is None or slot.material.node_tree is None:
                continue
            private = _private_material(transaction, obj, index, cache.setdefault(_part(obj), {}))
            selector = _selector(private)
            if selector is not None:
                for key, name in _ARMOR.items():
                    if key not in values:
                        continue
                    value = values[key]
                    if key in {"material", "pattern"}:
                        if type(value) is not int:
                            raise TypeError("旧盔甲编号必须为整数")
                        value = int(_number(value, 0, 7 if key == "material" else 18, key))
                    elif (not isinstance(value, (list, tuple)) or len(value) != 4
                          or any(not isinstance(c, (float, int)) or not math.isfinite(c) or not 0 <= c <= 1 for c in value)):
                        raise ValueError("旧盔甲颜色无效")
                    destination = selector.inputs[name]
                    require_property_edit(private.node_tree, destination.path_from_id("default_value"))
                    destination.default_value = value
            else:
                base_nodes = [node for node in private.node_tree.nodes if node.type == "TEX_IMAGE"
                              and node.name in {"Texture.002", "图像纹理.001"} and node.image]
                if len(base_nodes) != 1:
                    raise ValueError("旧盔甲底图通道无法确认，未覆盖自定义材质")
                folder = "humanoid_leggings" if _part(obj) == "legs" else "humanoid"
                variant = folder, material_id
                if variant not in images:
                    path = DATA.parent / "library/assets/minecraft/textures/entity/equipment" / folder / (_MATERIALS[material_id] + ".png")
                    if not path.is_file():
                        raise ValueError("此部位没有所选原版盔甲贴图")
                    images[variant] = _image(transaction, path)
                base_nodes[0].image = images[variant]
                for modifier in _armor_mods(obj):
                    socket = _socket(modifier, "Image")
                    if socket is None:
                        raise ValueError("旧盔甲原生图像接口缺失")
                    # The authored legacy sampler and geometry mask can use
                    # different vanilla armor images; both must switch parts.
                    _input(transaction, modifier, socket, images[variant])
        for modifier in _armor_mods(obj):
            if "three_d" in values:
                if not isinstance(values["three_d"], bool):
                    raise ValueError("原生3D开关应为布尔值")
                _set(transaction, modifier, "show_viewport", values["three_d"])
                _set(transaction, modifier, "show_render", values["three_d"])
            if "thickness" in values:
                _input(transaction, modifier, _socket(modifier, "厚度"), _number(values["thickness"], -10000, 10000, "厚度"))


def execute(scene_uid, options):
    ensure_mutation_allowed()
    if bpy.context.mode not in {"OBJECT", "POSE"}:
        raise ValueError("旧人模兼容功能仅在物体或姿态模式可用")
    scene = resolve_scene(scene_uid)
    layer = resolve_view_layer(scene, require_active=True)
    target = _resolve(scene, options.get("target"))
    feature, values = options.get("feature"), options.get("values", {})
    if not isinstance(values, dict):
        raise TypeError("旧人模参数格式无效")
    parts = options.get("parts", ())
    if not isinstance(parts, (list, tuple)) or set(parts) - set(PARTS):
        raise ValueError("旧人模盔甲部位无效")
    from ..asset.asset_geometry import Creation

    with Creation() as transaction:
        _journal_selection(transaction, layer)
        if feature == "select":
            if target.rig not in tuple(layer.objects) or not target.rig.visible_get(view_layer=layer):
                raise ValueError("旧人模当前被隐藏或归档，请先在视图层中明确启用对应合集")
            if bpy.context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            for obj in tuple(bpy.context.selected_objects):
                obj.select_set(False)
            target.rig.select_set(True)
            layer.objects.active = target.rig
        elif feature == "prepare_controls":
            from .human_compat_controls import prepare

            if values:
                raise ValueError("原生控制初始化不接受覆盖参数")
            prepare(transaction, scene, target)
        elif feature == "head":
            if set(values) - {"role", "keyframe"} or "role" not in values or type(values.get("keyframe", False)) is not bool:
                raise ValueError("旧人模头型参数无效")
            choices = dict(_head_choices(target))
            if values["role"] not in choices:
                raise ValueError("此旧头型的原生控制已改变，未覆盖")
            _head_value(transaction, scene, target, choices[values["role"]],
                        keyframe=values.get("keyframe", False), frame=options.get("frame", scene.frame_current_final))
        elif feature in {"style", "styles", "basic_settings"}:
            fields = {"arm_mode", "leg_mode"}
            basic_fields = {"name_enabled", "leg_fixed", "foot_fixed", "red"}
            if (feature == "basic_settings" and (not values or set(values) - (fields | basic_fields))
                    or feature != "basic_settings" and (not set(values) <= fields
                        or len(values) != (1 if feature == "style" else 2))):
                raise ValueError("旧人模样式参数无效")
            for field, value in values.items():
                if field not in fields:
                    continue
                choices = ARMS if field == "arm_mode" else LEGS
                if value not in choices:
                    raise ValueError("旧人模样式无效")
                if feature == "basic_settings" and _style(target, choices) == value:
                    continue
                layers_enabled = any(_visible(obj) for obj in _layers(target))
                from .human_compat_parts import available, ensure

                if available(target.version, field, value):
                    ensure(transaction, scene, target, field, value)
                    target = _resolve(scene, options["target"])
                groups = _groups(target, set(choices.values()))
                if choices[value] not in groups:
                    raise ValueError("旧人模没有此原生样式，未生成其它版本替代网格")
                previous_armor = {part: any(_visible(obj) for obj in _armors(target, (part,)))
                                  for part in ("legs", "feet")} if field == "leg_mode" else {}
                for label, collection in groups.items():
                    enabled = label == choices[value]
                    _enable_collection(transaction, scene, target, collection, enabled)
                    geometry = tuple(obj for obj in collection.all_objects if obj in target.members and obj.type == "MESH")
                    _visibility(transaction, scene, geometry, enabled)
                    if enabled and not layers_enabled:
                        _visibility(transaction, scene, tuple(obj for obj in geometry if obj in _layers(target)), False)
                restored_parts = tuple(part for part, enabled in previous_armor.items() if enabled)
                if restored_parts and available(target.version, "armor", restored_parts):
                    ensure(transaction, scene, target, "armor", restored_parts)
                    target = _resolve(scene, options["target"])
                for part, enabled in previous_armor.items():
                    if _armors(target, (part,)):
                        _armor_visibility(transaction, scene, target, (part,), enabled)
            if feature == "basic_settings":
                from .human_compat_controls import apply

                apply(transaction, scene, target, {name: value for name, value in values.items()
                                                  if name in basic_fields})
        elif feature == "armor_visibility":
            if set(values) != {"enabled"} or not isinstance(values["enabled"], bool):
                raise ValueError("旧盔甲显隐参数无效")
            if values["enabled"]:
                from .human_compat_parts import available, ensure

                if available(target.version, "armor", parts):
                    ensure(transaction, scene, target, "armor", parts)
                    target = _resolve(scene, options["target"])
            if _armors(target, parts):
                _armor_visibility(transaction, scene, target, parts, values["enabled"])
        elif feature == "layer_visibility":
            if set(values) != {"enabled"} or not isinstance(values["enabled"], bool):
                raise ValueError("旧二层显隐参数无效")
            _visibility(transaction, scene, _layers(target), values["enabled"])
        elif feature == "layer_settings":
            limits = {"outer": (-10000, 10000), "inner": (-10000, 10000), "random_factor": (0, 1),
                      "height_ratio": (1, 20), "seed": (0, 2147483647), "center_falloff": (0, 1), "center_range": (.05, 5)}
            mods = _layer_mods(target)
            if not mods or set(values) != set(_LAYER):
                raise ValueError("此旧人模没有完整的原生二层3D参数")
            for key, value in values.items():
                if key == "random_enabled":
                    if not isinstance(value, bool):
                        raise ValueError("随机开关应为布尔值")
                else:
                    if key == "seed" and type(value) is not int:
                        raise TypeError("随机种子必须为整数")
                    minimum, maximum = limits[key]
                    _number(value, minimum, maximum, key)
            for _obj, modifier in mods:
                for key, value in values.items():
                    _input(transaction, modifier, _socket(modifier, _LAYER[key]), value)
        elif feature == "armor_settings":
            _armor_settings(transaction, target, parts, values)
        elif feature == "skin":
            _apply_skin(transaction, target, options.get("filepath", ""), options.get("image_uid", 0))
        else:
            raise ValueError("未知旧人模兼容功能")
        target.rig.update_tag()
        resolve_view_layer(scene).update()
        transaction.commit()
    result = {"message": "旧人模兼容操作已完成；原骨架、网格与动画保留", "version": target.version}
    if feature == "select":
        result["selected_human_rig_uid"] = target.rig.session_uid
    if feature == "prepare_controls":
        result.update(red_prepared_rig_uid=target.rig.session_uid, scene_uid=scene.session_uid,
                      red_popup=options.get("red_popup") is True)
    return result
