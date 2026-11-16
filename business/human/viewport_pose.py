"""Resolve viewport hits to native BaiPo control bones and edit one verified rig."""

from math import isfinite
from typing import Any, cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import DATA_UID, owns
from ...util.resources import ASSET_ID, INSTANCE, resolve_scene, resolve_view_layer
from ..asset import creatures
from . import human_compat
from . import viewport_character_state as state
from .human import RESOURCE_ID, rig_of, roots, source_name

CONTROLS = {
    "ROOT": "根",
    "HEAD": "头",
    "CHEST": "上身",
    "HIPS": "下半身",
    "HAND_L": "L.胳膊控制器",
    "HAND_R": "R.胳膊控制器",
    "FOOT_L": "L.脚控制器",
    "FOOT_R": "R.脚控制器",
}

_BODY_PICK = "blockloom_pick_character_body"
_SOLO_GATE = "blockloom_native_widgets_gate"
_PREVIOUS_SOLO = "blockloom_restore_solo"
_SHOW_DIAGRAM = "blockloom_restore_face_diagram"
_PANEL = "blockloom_character_panel"
_OFFSET = "blockloom_character_offset"


def panel(rig):
    return rig.get(_PANEL, "OPEN") if rig else "OPEN"


def offset(rig):
    return rig.get(_OFFSET, (0.0, 0.0)) if rig else (0.0, 0.0)


def body_enabled(rig):
    return bool(rig.get(_BODY_PICK, False))


def native_visible(rig):
    return not bool(rig.get(_SOLO_GATE, False))


def _diagrams(rig):
    from .human_face_preview import _MODIFIER

    return [obj for obj in rig.children if obj.type == "MESH" and len(obj.data.polygons) == 1
            and obj.active_material and "面部兼容图片" in obj.active_material.name
            and (obj.modifiers.get(_MODIFIER)
                 or any(mod.type == "ARMATURE" and mod.object == rig for mod in obj.modifiers))]


def _display_option(rig, command, enabled):
    if not isinstance(enabled, bool):
        raise TypeError("无效的角色显示设置")
    if command == "BODY":
        if enabled:
            rig[_BODY_PICK] = True
        elif _BODY_PICK in rig:
            del rig[_BODY_PICK]
        return
    if enabled == native_visible(rig):
        return
    data = rig.data
    if data.library or data.override_library or any(obj != rig and obj.data == data for obj in bpy.data.objects):
        raise ValueError("请先将此角色的骨架数据设为独立")
    diagrams = _diagrams(rig)
    if not enabled:
        from .human_armor import animated

        if any(animated(obj,"hide_viewport") for obj in diagrams):
            raise ValueError("面控示意图已有自定义视口显隐动画，请先检查该图示")
        if any(animated(data,c.path_from_id("is_solo")) for c in data.collections_all):
            raise ValueError("骨集合已有自定义独显动画，请先检查该骨架")
        # Solo one empty collection. Authored display flags, custom widgets,
        # skin visibility drivers and the user's viewport overlay stay intact.
        before = [c for c in data.collections_all if c.is_solo]
        gate = data.collections.new("原控制器 · 暂时隐藏")
        try:
            gate[_SOLO_GATE] = True
            gate.is_solo = True
            for coll in before:
                coll[_PREVIOUS_SOLO] = True
                coll.is_solo = False
            for obj in diagrams:
                if not obj.hide_viewport:
                    obj[_SHOW_DIAGRAM] = True
                    obj.hide_viewport = True
            rig[_SOLO_GATE] = True
        except Exception:
            for obj in diagrams:
                if obj.get(_SHOW_DIAGRAM):
                    obj.hide_viewport = False
                    del obj[_SHOW_DIAGRAM]
            for coll in before:
                coll.is_solo = True
                if _PREVIOUS_SOLO in coll:
                    del coll[_PREVIOUS_SOLO]
            data.collections.remove(gate)
            raise
    else:
        gates = [c for c in data.collections_all if c.get(_SOLO_GATE)]
        for obj in diagrams:
            if obj.get(_SHOW_DIAGRAM):
                obj.hide_viewport = False
                del obj[_SHOW_DIAGRAM]
        for coll in data.collections_all:
            if coll.get(_PREVIOUS_SOLO):
                coll.is_solo = True
                del coll[_PREVIOUS_SOLO]
        for gate in gates:
            gate.is_solo = False
            if gate.bones or gate.children:
                del gate[_SOLO_GATE]
            else:
                data.collections.remove(gate)
        del rig[_SOLO_GATE]


def character_of(scene, obj):
    if not obj or obj.type not in {"ARMATURE", "MESH", "FONT", "LATTICE"}:
        return None
    for root in (*roots(scene), *creatures.instances(scene)):
        if root.all_objects.get(obj.name) == obj and owns(root, obj):
            rig = (
                rig_of(root)
                if root.get(ASSET_ID) == RESOURCE_ID
                else root.get(creatures.RIG)
            )
            if rig and owns(root, rig):
                return root, rig
    found = human_compat._rigs(obj)
    if len(found) == 1:
        legacy = human_compat._identify(scene, found.pop())
        if legacy and legacy.editable and (obj == legacy.rig or obj in legacy.members):
            return None, legacy.rig
    return None


def selected_character(context):
    characters = {
        found
        for obj in context.selected_objects
        if (found := character_of(context.scene, obj))
    }
    return characters.pop() if len(characters) == 1 else None


def body_part(obj):
    role = obj.get("oxal_humanoid_role")
    if role:
        return {
            "head": "HEAD",
            "body": "CHEST",
            "left_arm": "HAND_L",
            "right_arm": "HAND_R",
            "left_leg": "FOOT_L",
            "right_leg": "FOOT_R",
            "folded": "FOLDED",
        }.get(role)
    name = source_name(obj)
    side = "L" if name.startswith("L") else "R" if name.startswith("R") else ""
    if side:
        if any(token in name for token in ("胳膊", "隔壁", "臂", "手")):
            return "HAND_" + side
        if any(token in name for token in ("腿", "脚")):
            return "FOOT_" + side
    if any(token in name for token in ("头", "脸", "眼", "眉", "嘴")):
        return "HEAD"
    if name.startswith(("身体", "身子", "躯干", "本体二层")):
        return "CHEST"
    return None


def control(rig, part, rotate=False):
    name = CONTROLS.get(part)
    if part == "FOLDED":
        name = "交叠手"
    if rotate and part in {"HAND_L", "HAND_R"}:
        name = part[-1] + ".大臂"
    if not name or name not in rig.pose.bones:
        raise ValueError("当前角色没有此部位的原控制骨")
    return name, (
        "MOVE"
        if part == "ROOT" or (not rotate and part.startswith(("HAND_", "FOOT_")))
        else "ROTATE"
    )


def target(root, rig):
    return {
        "instance": str(root.get(INSTANCE, "")) if root else "",
        "rig_uid": rig.session_uid,
        "rig_source": rig.get(DATA_UID),
        "data_source": rig.data.get(DATA_UID),
        "data_uid": rig.data.session_uid,
    }


def selected_names(rig):
    return [
        p.name
        for p in rig.pose.bones
        if (p.select if hasattr(p, "select") else p.bone.select)
    ]


def _resolve(scene, token):
    if not isinstance(token, dict):
        raise TypeError("缺少角色记录")
    if token.get("instance"):
        matches = [
            r
            for r in (*roots(scene), *creatures.instances(scene))
            if r.get(INSTANCE) == token["instance"]
        ]
        if len(matches) != 1:
            raise ValueError("原角色已删除或实例标识重复")
        root = matches[0]
        rig = (
            rig_of(root)
            if root.get(ASSET_ID) == RESOURCE_ID
            else root.get(creatures.RIG)
        )
        if (
            not rig
            or not owns(root, rig)
            or not owns(root, rig.data)
            or rig.session_uid != token.get("rig_uid")
            or rig.data.session_uid != token.get("data_uid")
            or rig.get(DATA_UID) != token.get("rig_source")
            or rig.data.get(DATA_UID) != token.get("data_source")
        ):
            raise ValueError("角色骨架已替换")
    else:
        rig = next(
            (o for o in scene.objects if o.session_uid == token.get("rig_uid")), None
        )
        if (
            not rig
            or rig.type != "ARMATURE"
            or rig.data.session_uid != token.get("data_uid")
        ):
            raise ValueError("原骨架已删除或替换")
        found = character_of(scene, rig)
        if not found or found[0] is not None:
            raise ValueError("旧人模来源已变化")
    if rig.library or rig.override_library or tuple(rig.users_scene) != (scene,):
        raise ValueError("请先将角色设为当前场景的独立可编辑实例")
    return rig


def rotation_path(bone):
    return {
        "QUATERNION": "rotation_quaternion",
        "AXIS_ANGLE": "rotation_axis_angle",
    }.get(bone.rotation_mode, "rotation_euler")


def _bones(rig, names):
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) for name in names)
    ):
        raise ValueError("请先选择原控制骨")
    result = [rig.pose.bones.get(n) for n in dict.fromkeys(names)]
    if any(p is None for p in result):
        raise ValueError("所选控制骨已删除或改名")
    return result


def _select(scene, rig, bones):
    if any(obj != rig and obj.data == rig.data for obj in bpy.data.objects):
        raise ValueError("请先将此角色的骨架数据设为独立")
    layer = resolve_view_layer(scene, require_active=True)
    if not rig.visible_get(view_layer=layer) or rig.hide_select:
        raise ValueError("当前视图层的角色骨架不可选择")
    ops = cast(Any, bpy.ops)
    if bpy.context.mode == "POSE" and (
        layer.objects.active != rig or len(bpy.context.objects_in_mode or ()) != 1
    ):
        ops.object.mode_set(mode="OBJECT")
    if bpy.context.mode != "POSE":
        for obj in tuple(bpy.context.selected_objects or ()):
            obj.select_set(False, view_layer=layer)
        rig.select_set(True, view_layer=layer)
        layer.objects.active = rig
        if "FINISHED" not in ops.object.mode_set(mode="POSE"):
            raise ValueError("无法进入该角色的姿态模式")
    chosen = set(bones)
    for p in rig.pose.bones:
        if hasattr(p, "select"):
            p.select = p in chosen
        else:
            p.bone.select = p in chosen
    rig.data.bones.active = bones[-1].bone


def guard_transform(rig, bone, transform, autokey):
    if (
        rig.library
        or rig.override_library
        or tuple(rig.users_scene) != (bpy.context.scene,)
    ):
        raise ValueError("请先将角色设为当前场景的独立可编辑实例")
    if any(obj != rig and obj.data == rig.data for obj in bpy.data.objects):
        raise ValueError("请先将此角色的骨架数据设为独立")
    path = bone.path_from_id("location" if transform == "MOVE" else rotation_path(bone))
    if autokey:
        state.preflight_keys(rig, [bone], transform)
    elif rig.animation_data and any(
        curve.data_path == path for curve in rig.animation_data.drivers
    ):
        raise ValueError("此控制骨通道由驱动器控制")
    locks = bone.lock_location if transform == "MOVE" else bone.lock_rotation
    if all(locks):
        raise ValueError("该控制骨的变换通道已锁定")


def select(scene_uid, options):
    if options.get("command") != "SELECT":
        raise ValueError("选择请求只接受原控制骨目标")
    return edit(scene_uid, options)


def edit(scene_uid, options):
    # The Bus runs outside an editor event. Blender's native pose/keying operators
    # require an actual 3D-view context in the active scene and window.
    context = cast(Any, bpy.context)
    window = context.window
    if window is None or window.scene.session_uid != scene_uid:
        raise ValueError("请切回提交姿态操作的场景")
    area = next(
        (
            a
            for a in window.screen.areas
            if a.type == "VIEW_3D" and str(a.as_pointer()) == options.get("area")
        ),
        None,
    )
    if area is None:
        area = next((a for a in window.screen.areas if a.type == "VIEW_3D"), None)
    region = (
        next((r for r in area.regions if r.type == "WINDOW"), None) if area else None
    )
    if area is None or region is None:
        raise ValueError("此窗口没有可用的三维视图")
    with context.temp_override(window=window, area=area, region=region):
        return _apply(scene_uid, options)


def _apply(scene_uid, options):
    ensure_mutation_allowed()
    scene = resolve_scene(scene_uid)
    resolve_view_layer(scene, require_active=True)

    command = options["command"]
    rig = _resolve(scene, options["target"])
    if command == "PANEL":
        value = options.get("value")
        if value not in {"OPEN","COLLAPSED","CLOSED"}:
            raise ValueError("无效的角色面板状态")
        rig[_PANEL] = value
        return {"message": "已更新角色面板", "viewport_pose": True}
    if command == "OFFSET":
        value = options.get("value")
        if not isinstance(value,(list,tuple)) or len(value)!=2 or not all(
                isinstance(n,(int,float)) and isfinite(n) for n in value):
            raise ValueError("无效的角色面板位置")
        rig[_OFFSET] = list(value)
        return {"message": "已更新角色面板位置", "viewport_pose": True}
    if command in {"BODY", "BONES"}:
        _display_option(rig, command, options["enabled"])
        return {"message": "已更新所选角色的视口设置", "viewport_pose": True}
    if command in {"AUTO", "ROTATE", "WRIST"}:

        state.set_option(rig, command, options.get("value", options.get("enabled")))
        return {"message": "已更新所选角色的操作设置", "viewport_pose": True}
    bones = _bones(rig, options["bones"])
    if command == "KEY":
        if scene.frame_current_final != options.get("frame"):
            raise ValueError("当前帧已改变，请再次插帧")
        state.insert_keys(rig, bones, options["frame"], options.get("transform"), channels=options.get("channels"))
    elif command in {"SELECT", "TRANSFORM"} and len(bones) == 1:
        bone = bones[0]
        transform = options["transform"]
        if transform not in {"MOVE", "ROTATE"}:
            raise ValueError("未知姿态变换")
        guard_transform(rig, bone, transform, state.auto(rig))
        vector = options.get("vector", [])
        if command == "TRANSFORM":
            if scene.frame_current_final != options.get("frame"):
                raise ValueError("拖动期间当前帧已改变")
            if (
                not isinstance(vector, list)
                or len(vector) != 3
                or not all(isinstance(v, (int, float)) and isfinite(v) for v in vector)
            ):
                raise ValueError("无效的视口变换")
        _select(scene, rig, bones)
        if command == "TRANSFORM":
            state.apply_transform(rig, bone, transform, vector, options["frame"], record=state.auto(rig))
    else:
        raise ValueError("未知视口姿态操作")
    return {"message": "已更新所选原控制骨", "viewport_pose": True}
