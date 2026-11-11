"""Explicit native basic controls for a previously identified legacy human.

Reads do not create parameters or adopt the rig. Writes join the caller's
Creation transaction; shaders and constraints continue working without Python.
"""

import math

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.animation_edit import require_property_edit

RED_PROPERTY = "oxal_legacy_red"
RED_PATH = '["' + RED_PROPERTY + '"]'
_RED_OWNER = "oxal_legacy_red_rig"
_RED_SCHEMA = "oxal_legacy_red_schema"
_RED_SOURCE = "oxal_legacy_red_source"
LEGACY_RED_PROPERTY = "bp_model_red_temp140"
LEGACY_RED_PATH = '["' + LEGACY_RED_PROPERTY + '"]'


def _local(owner):
    return owner.library is None and owner.override_library is None and owner.is_editable


def _leg_pairs(target):
    pairs = []
    for side in ("L", "R"):
        bone = target.rig.pose.bones.get(side + ".脚控制器")
        if bone is None:
            return None
        constraints = tuple(item for item in bone.constraints if item.type == "CHILD_OF")
        down = [item for item in constraints if item.target == target.rig and item.subtarget == side.lower() + ".下盘"]
        root = [item for item in constraints if item.target == target.rig and item.subtarget == "根"]
        if len(constraints) != 2 or len(down) != 1 or len(root) != 1 or any(item.mute for item in constraints):
            return None
        if side.lower() + ".下盘" not in target.rig.data.bones:
            return None
        pairs.append((down[0], root[0]))
    return tuple(pairs)


def _leg_value(target):
    pairs = _leg_pairs(target)
    if pairs is None:
        return None
    if all(abs(down.influence - 1) < 1e-6 and abs(root.influence) < 1e-6 for down, root in pairs):
        return True
    if all(abs(down.influence) < 1e-6 and abs(root.influence - 1) < 1e-6 for down, root in pairs):
        return False
    return None


def _ankle_parents(target):
    parents = {}
    for side in ("L", "R"):
        bone = target.rig.data.bones.get(side + ".脚腕")
        if bone is None or bone.parent is None:
            return None
        parent = bone.parent.name
        if parent not in {side + ".脚控制器", side + ".小腿中间节"}:
            return None
        parents[bone.name] = parent
    return parents


def _foot_value(target):
    parents = _ankle_parents(target)
    if parents is None:
        return None
    values = [parent == side + ".脚控制器" for side, parent in (("L", parents["L.脚腕"]), ("R", parents["R.脚腕"]))]
    return values[0] if values[0] == values[1] else None


def _names(target):
    from .human_compat import _named, _rigs
    from .human_compat_name import RIG, SCHEMA
    from .human_state import SOURCE_NAME

    candidates = {obj for collection in target.containers for obj in collection.all_objects if obj.type == "FONT"}
    found = []
    for obj in candidates:
        if _rigs(obj) != {target.rig}:
            continue
        authored = _named(str(obj.get(SOURCE_NAME, obj.name)), {"名字"}) == "名字"
        if not authored and not (obj.get(SCHEMA) == 1 and obj.get(RIG) == target.rig):
            continue
        parented = obj.parent == target.rig and obj.parent_type == "BONE" and obj.parent_bone == "头"
        constraints = tuple(item for item in obj.constraints if item.type == "CHILD_OF")
        bound = (len(constraints) == 1 and constraints[0].target == target.rig
                 and constraints[0].subtarget == "头" and not constraints[0].mute)
        if parented or bound:
            found.append(obj)
    return tuple(found)


def _red_groups(target):
    from . import human_compat

    surfaces = {*human_compat._skin_objects(target),
                *(obj for obj in target.members if obj.type == "MESH" and human_compat._role(obj) == "嘴")}
    groups = {}
    for obj in surfaces:
        for index, slot in enumerate(obj.material_slots):
            material = slot.material
            if material is None or material.node_tree is None:
                continue
            nodes = tuple(node for node in material.node_tree.nodes if node.label == "红温控制器")
            if nodes:
                groups.setdefault(material, {"nodes": nodes, "slots": []})["slots"].append((obj, index))
    return groups, surfaces


def _red_socket(node):
    if (node.bl_idname not in {"ShaderNodeMix", "ShaderNodeMixShader"} or not node.inputs
            or not isinstance(getattr(node.inputs[0], "default_value", None), (int, float))
            or node.inputs[0].is_linked):
        raise ValueError("旧人模红温节点接口已有修改，未覆盖：" + node.name)
    return node.inputs[0]


def _socket_path(node):
    return 'nodes["' + bpy.utils.escape_identifier(node.name) + '"].inputs[0].default_value'


def _has_animation(owner, path):
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return False
    if any(curve.data_path == path for curve in action_fcurves(animation.action, slot=animation.action_slot)):
        return True
    strips = [strip for track in animation.nla_tracks for strip in track.strips]
    while strips:
        strip = strips.pop()
        if strip.type == "META":
            strips.extend(strip.strips)
        if any(curve.data_path == path for curve in action_fcurves(strip.action, slot=getattr(strip, "action_slot", None))):
            return True
    return False


def _curve(material, node):
    animation = material.node_tree.animation_data
    curves = tuple(curve for curve in animation.drivers if curve.data_path == _socket_path(node)) if animation else ()
    if len(curves) > 1:
        raise ValueError("旧人模红温节点有多个驱动，未覆盖")
    return curves[0] if curves else None


def _identity_modifiers(curve):
    modifiers = tuple(curve.modifiers)
    if not modifiers:
        return True
    if len(modifiers) != 1:
        return False
    modifier = modifiers[0]
    return (modifier.type == "GENERATOR" and not modifier.mute and not modifier.use_restricted_range
            and not modifier.use_influence and not modifier.use_additive and modifier.mode == "POLYNOMIAL"
            and modifier.poly_order == 1 and tuple(modifier.coefficients) == (0.0, 1.0))


def _driver_matches(curve, rig, source_path, *, legacy=False):
    if curve is None:
        return False
    variables = tuple(curve.driver.variables)
    if (curve.array_index != 0 or curve.mute or curve.lock or curve.keyframe_points or curve.sampled_points
            or curve.driver.type != "SCRIPTED" or curve.driver.use_self
            or curve.driver.expression != ("min(1,max(0,value))" if legacy else "value")
            or len(variables) != 1 or variables[0].name != "value" or variables[0].type != "SINGLE_PROP"
            or (not _identity_modifiers(curve) if legacy else bool(curve.modifiers))):
        return False
    source = variables[0].targets[0]
    return source.id == rig and source.data_path == source_path


def _native_driver(material, node, rig, source_key=RED_PROPERTY):
    if material.get(_RED_OWNER) != rig:
        return False
    schema = material.get(_RED_SCHEMA)
    if (schema == 1 and source_key != RED_PROPERTY
            or schema == 2 and material.get(_RED_SOURCE) != source_key
            or schema not in {1, 2}):
        return False
    _red_socket(node)
    path = _socket_path(node)
    if _has_animation(material.node_tree, path):
        return False
    return _driver_matches(_curve(material, node), rig, '["' + source_key + '"]',
                           legacy=schema == 2 and source_key == LEGACY_RED_PROPERTY)


def red_channel(target):
    """Return only an exactly verified, instance-local native RNA source."""
    rig = target.rig
    groups, surfaces = _red_groups(target)
    if not groups:
        return None
    keys = {RED_PROPERTY if material.get(_RED_SCHEMA) == 1 else material.get(_RED_SOURCE)
            for material in groups}
    if len(keys) != 1:
        return None
    source_key = keys.pop()
    if source_key not in {RED_PROPERTY, LEGACY_RED_PROPERTY}:
        return None
    value = rig.get(source_key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        return None
    # Appended legacy styles use native DATA slots for the current skin.
    # This remains instance-local only when every actual Mesh consumer belongs
    # to this exact surface set; OBJECT overrides are not the only valid form.
    meshes = {obj.data for record in groups.values() for obj, index in record["slots"]
              if obj.material_slots[index].link == "DATA"}
    mesh_users = bpy.data.user_map(subset=tuple(meshes)) if meshes else {}
    if any(not _local(mesh) or not mesh_users.get(mesh)
           or any(not isinstance(owner, bpy.types.Object) or owner not in surfaces
                  or not _local(owner) or len(owner.users_scene) != 1
                  for owner in mesh_users[mesh]) for mesh in meshes):
        return None
    users = bpy.data.user_map(subset=tuple(groups))
    try:
        for material, record in groups.items():
            if not _local(material) or not all(_native_driver(material, node, rig, source_key) for node in record["nodes"]):
                return None
            for obj, index in record["slots"]:
                slot = obj.material_slots[index]
                if slot.link == "DATA" and (obj.data not in meshes or index >= len(obj.data.materials)
                                            or obj.data.materials[index] != material):
                    return None
                if slot.link not in {"OBJECT", "DATA"}:
                    return None
            if any(owner not in surfaces and owner not in meshes for owner in users.get(material, ())):
                return None
    except ValueError:
        return None
    return rig, '["' + source_key + '"]'


def settings(target):
    names = _names(target)
    name_value = (not names[0].hide_viewport and not names[0].hide_render and not names[0].hide_get()) if len(names) == 1 else None
    if not names:
        from .human_compat_name import available

        if available(target.version):
            name_value = False
    channel = red_channel(target)
    red = float(channel[0].path_resolve(channel[1])) if channel else None
    if channel is None:
        groups, _ = _red_groups(target)
        try:
            values = [float(_red_socket(node).default_value) for record in groups.values() for node in record["nodes"]]
            if values and all(math.isfinite(value) and 0 <= value <= 1 and abs(value - values[0]) < 1e-6 for value in values):
                red = values[0]
        except ValueError:
            pass
    return {"name_enabled": name_value, "leg_fixed": _leg_value(target),
            "foot_fixed": _foot_value(target), "red": red}


def can_edit(target, field):
    """Distinguish a known mixed value from an unavailable native control."""
    if not target.editable or not _local(target.rig) or not _local(target.rig.data):
        return False
    if field == "leg_fixed":
        return _leg_pairs(target) is not None
    if field == "foot_fixed":
        return _ankle_parents(target) is not None
    if field == "name_enabled":
        names = _names(target)
        if len(names) == 1:
            obj = names[0]
            return _local(obj) and _local(obj.data) and len(obj.users_scene) == 1
        if names:
            return False
        from .human_compat_name import available

        return available(target.version)
    return False


def _legs(transaction, target, enabled):
    from .human_compat import _set

    pairs = _leg_pairs(target)
    if pairs is None:
        raise ValueError("旧人模腿部固定约束不完整或已修改，未覆盖")
    if _leg_value(target) == enabled:
        return
    for down, root in pairs:
        for constraint, value in ((down, float(enabled)), (root, float(not enabled))):
            require_property_edit(target.rig, constraint.path_from_id("influence"))
            _set(transaction, constraint, "influence", value)


def _ankles(transaction, scene, target, enabled):
    from .human import _set_ankles
    from .human_compat import _journal_selection

    if _foot_value(target) == enabled:
        return
    rig = target.rig
    if any(owner != rig for owner in bpy.data.user_map(subset=(rig.data,)).get(rig.data, ())):
        raise ValueError("旧人模骨架数据被其它对象或数据共用，未修改脚腕初始绑定")
    parents = _ankle_parents(target)
    if parents is None:
        raise ValueError("旧人模脚腕父级不完整或已有未知修改，未修改绑定")
    if any(animation and (animation.action or animation.nla_tracks)
           for animation in (rig.animation_data, rig.data.animation_data)):
        raise ValueError("已有 Action/NLA，不能修改脚腕初始绑定；动画保持原样")
    if bpy.context.scene != scene or bpy.context.mode not in {"OBJECT", "POSE"}:
        raise ValueError("请在当前场景的物体或姿态模式修改脚腕绑定")
    layer = bpy.context.view_layer
    if layer is None:
        raise ValueError("当前视图层不可用")
    _journal_selection(transaction, layer)
    poses = {name: rig.pose.bones[name].matrix.copy() for name in parents}

    def change(*, restore=False):
        mode = bpy.context.mode
        if mode == "POSE":
            bpy.ops.object.mode_set(mode="OBJECT")
        try:
            _set_ankles(rig, enabled, restore_parents=parents if restore else None)
            if restore:
                for name, matrix in poses.items():
                    rig.pose.bones[name].matrix = matrix
        finally:
            if mode == "POSE" and bpy.context.mode == "OBJECT":
                bpy.ops.object.mode_set(mode="POSE")

    transaction.on_rollback(lambda: change(restore=True))
    change()


def _name(transaction, scene, target, enabled):
    from .human_compat import _set

    names = _names(target)
    if not names and enabled:
        from .human_compat_name import ensure

        names = (ensure(transaction, scene, target),)
    if not names:
        return
    if len(names) != 1:
        raise ValueError("旧人模存在多个头部名字对象，请先确认绑定")
    obj = names[0]
    if not _local(obj) or not _local(obj.data) or tuple(obj.users_scene) != (scene,):
        raise ValueError("旧人模名字来自关联库或跨场景共享，未修改")
    hidden = obj.hide_get()
    transaction.on_rollback(lambda: obj.hide_set(hidden))
    for field in ("hide_viewport", "hide_render"):
        if getattr(obj, field) != (not enabled):
            _set(transaction, obj, field, not enabled)
    obj.hide_set(not enabled)


def _prepare_source(target, groups):
    rig = target.rig
    source_key = None
    values = []
    for material, record in groups.items():
        if not _local(material) or not _local(material.node_tree):
            raise ValueError("旧人模红温材质来自只读关联库，未修改")
        for node in record["nodes"]:
            socket = _red_socket(node)
            if _has_animation(material.node_tree, _socket_path(node)):
                raise ValueError("旧人模红温节点已有动画，请通过原动画编辑")
            curve = _curve(material, node)
            if curve is not None:
                if not any(_driver_matches(curve, rig, path, legacy=True)
                           for path in (LEGACY_RED_PROPERTY, LEGACY_RED_PATH)):
                    raise ValueError("旧人模红温节点已有未知驱动，未覆盖：" + node.name)
                source_key = LEGACY_RED_PROPERTY
            else:
                values.append(float(socket.default_value))
    if source_key is None:
        if not values or any(not math.isfinite(value) or not 0 <= value <= 1
                             or abs(value - values[0]) > 1e-6 for value in values):
            raise ValueError("旧人模原生红温节点值不一致，未自动准备")
        return RED_PROPERTY, values[0]
    value = rig.get(source_key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("旧版红温驱动的原参数无效，未覆盖")
    if _has_animation(rig, LEGACY_RED_PROPERTY):
        raise ValueError("旧版红温仍使用旧 RNA 动画路径；原动画保留，请先明确迁移该动画")
    if any(abs(value - candidate) > 1e-6 for candidate in values):
        raise ValueError("旧版红温控制器与未驱动表面值不一致，未自动准备")
    return source_key, float(value)


def prepare(transaction, scene, target):
    """Explicitly prepare native live controls without replacing old animation."""
    from .human_compat import _private_material

    ensure_mutation_allowed()
    rig = target.rig
    if (not target.editable or not _local(rig) or not _local(rig.data)
            or scene.objects.get(rig.name) != rig or bpy.context.scene != scene):
        raise ValueError("旧人模红温目标已改变或不可编辑")
    native = red_channel(target)
    if native is not None:
        return native
    groups, surfaces = _red_groups(target)
    if not groups:
        raise ValueError("旧人模没有可确认的原生红温节点")
    if RED_PROPERTY in rig or any(material.get(_RED_OWNER) == rig for material in groups):
        raise ValueError("旧人模本地红温接线已改变或被外部共享，请先检查，未覆盖")
    source_key, initial = _prepare_source(target, groups)
    existed = source_key in rig
    before_ui = rig.id_properties_ui(source_key).as_dict() if existed else {}

    def restore():
        if not existed:
            if source_key in rig:
                del rig[source_key]
        elif source_key in rig:
            ui = rig.id_properties_ui(source_key)
            ui.clear()
            ui.update(**before_ui)

    transaction.on_rollback(restore)
    if not existed:
        rig[source_key] = initial
    rig.id_properties_ui(source_key).update(min=0.0, max=1.0, soft_min=0.0, soft_max=1.0,
                                            description="旧人模红温；原生材质驱动，停用插件后仍有效")
    path = '["' + source_key + '"]'
    cache = {}
    prepared = set()
    for record in groups.values():
        for obj, index in record["slots"]:
            if not _local(obj) or obj not in surfaces:
                raise ValueError("旧人模红温表面不可编辑，未修改")
            private = _private_material(transaction, obj, index, cache)
            if private in prepared:
                continue
            prepared.add(private)
            legacy = source_key == LEGACY_RED_PROPERTY
            private[_RED_SCHEMA], private[_RED_OWNER] = (2 if legacy else 1), rig
            if legacy:
                private[_RED_SOURCE] = source_key
            for original in record["nodes"]:
                node = private.node_tree.nodes[original.name]
                curve = _curve(private, node)
                if curve is not None:
                    # This curve was verified against the exact old 196 contract
                    # before copying. Preserve its clamp and identity mapping.
                    curve.driver.variables[0].targets[0].data_path = path
                    continue
                _red_socket(node)
                curve = private.node_tree.driver_add(_socket_path(node))
                curve.keyframe_points.clear()
                for modifier in tuple(curve.modifiers):
                    curve.modifiers.remove(modifier)
                curve.driver.type = "SCRIPTED"
                for variable in tuple(curve.driver.variables):
                    curve.driver.variables.remove(variable)
                variable = curve.driver.variables.new()
                variable.name, variable.type = "value", "SINGLE_PROP"
                variable.targets[0].id, variable.targets[0].data_path = rig, path
                curve.driver.expression = "min(1,max(0,value))" if legacy else "value"
    rig.update_tag()
    native = red_channel(target)
    if native is None:
        raise ValueError("旧人模红温原生接线准备校验失败，已撤回")
    return native


def _red(transaction, target, value):
    native = prepare(transaction, bpy.context.scene, target)
    rig, path = native
    before = float(rig.path_resolve(path))
    if abs(before - value) < 1e-6:
        return
    require_property_edit(rig, path)
    source_key = LEGACY_RED_PROPERTY if path == LEGACY_RED_PATH else RED_PROPERTY
    transaction.on_rollback(lambda: rig.__setitem__(source_key, before))
    rig[source_key] = float(value)
    rig.update_tag()


def apply(transaction, scene, target, values):
    """Apply only requested basic values, within the caller's transaction."""
    ensure_mutation_allowed()
    if not isinstance(values, dict) or set(values) - {"name_enabled", "leg_fixed", "foot_fixed", "red"}:
        raise ValueError("未知旧人模基础参数")
    if not target.editable or not _local(target.rig) or not _local(target.rig.data):
        raise ValueError("旧人模骨架来自只读关联库，未修改")
    if scene.objects.get(target.rig.name) != target.rig or bpy.context.scene != scene:
        raise ValueError("旧人模基础参数目标不属于当前场景，未修改")
    for name, value in values.items():
        if name == "red":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("红温必须为0到1之间的数值")
        elif type(value) is not bool:
            raise TypeError("旧人模开关必须为布尔值：" + name)
    # Structural binding runs before optional parameter/material preparation.
    if "foot_fixed" in values:
        _ankles(transaction, scene, target, values["foot_fixed"])
    if "leg_fixed" in values:
        _legs(transaction, target, values["leg_fixed"])
    if "name_enabled" in values:
        _name(transaction, scene, target, values["name_enabled"])
    if "red" in values:
        _red(transaction, target, values["red"])
