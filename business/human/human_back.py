"""Native BaiPo back controls and explicit source-backed accessory contracts.

asset_id: oxal.human.back; asset_version: 1 (legacy) / 2 (2.4.2);
policy: NEW_INSTANCE; scope: one precisely owned human. Legacy instances retain
the packaged 2.4.0/local.31 source; 2.4.2 uses the minimal versioned closures in
human_back242. Playback uses native bones, constraints and drivers; structural
mode changes belong to human_parts.
"""

import hashlib
import json
import math
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import require_owned
from .human_state import key, path

MODE_OBJECT = {1: "New model", 2: "披风·单段", 3: "披风·五段"}
WING_BONES = ("鞘翅开合·轨道", "wing.L", "up.L", "down.L", "bottom.L",
              "wing.R", "up.R", "down.R", "bottom.R", "鞘翅展开")
CAPE_BONES = ("披风·根", "披风·单段", "披风·单段目标",
              "披风·柔性1", "披风·柔性2", "披风·柔性3", "披风·柔性4", "披风·柔性5", "披风·柔性6")
MODE_BONES = ("披风模式·轨道", "披风模式")
ALL_BONES = (*MODE_BONES, *CAPE_BONES, *WING_BONES)
POSE_MEMORY = "oxal_human_back_pose"
MATERIAL_MEMORY = "oxal_human_back_materials"
SCHEMA = "oxal_human_back_schema"
SCHEMA_VERSION = 3
RIG = "oxal_human_back_rig"
RIG_MARK = "oxal_mc_studio_human_back_rig"
CONTRACT = "oxal_human_back_contract"
WING_TRAVEL = 0.6481501253236033
MODE_TRAVEL = 0.30102702925280034


def bones(mode, root=None):
    """The selected immutable native closure; body/chest bones stay public."""
    if root is not None:
        from . import human_back242

        if human_back242.current(root):
            if mode == 0:
                return ()
            if mode == 1:
                return WING_BONES
            if mode == 2:
                return human_back242.CAPE_BONES
            raise ValueError("2.4.2背饰模式仅支持关闭、鞘翅和披风")
    if mode == 0:
        return ()
    if mode == 1:
        return (*MODE_BONES, *WING_BONES)
    if mode == 2:
        return (*MODE_BONES, *CAPE_BONES[:3])
    if mode == 3:
        return (*MODE_BONES, CAPE_BONES[0], *CAPE_BONES[3:])
    raise ValueError("背饰模式无效")


def all_bones(root):
    from . import human_back242

    return (*MODE_BONES, *human_back242.CAPE_BONES, *WING_BONES) if human_back242.current(root) else ALL_BONES


def rig_for(root, body_rig) -> Any:
    """Resolve the exact accessory reference; inherited tags confer no ownership."""
    rig = root.get(RIG)
    if rig is None:
        return None
    if not isinstance(rig, bpy.types.Object) or rig.type != "ARMATURE" or rig == body_rig or rig not in tuple(root.all_objects):
        raise ValueError("背饰骨架引用损坏，未接管其它骨架")
    require_owned(root, (rig, cast(Any, rig.data)))
    if rig.get(RIG_MARK) != SCHEMA_VERSION:
        raise ValueError("背饰骨架来源版本未知")
    return rig


def is_back_rig(obj):
    """Presentation hint only; destructive access always uses rig_for."""
    return obj.type == "ARMATURE" and obj.get(RIG_MARK) == SCHEMA_VERSION


def _selection_owner(rig, bone):
    """Use the host's actual native pose-selection RNA owner (4.5 / 5.x)."""
    pose = rig.pose.bones[bone.name]
    return pose if pose.bl_rna.properties.get("select") is not None else bone


@contextmanager
def object_mode(root, body_rig, snapshot=None, *, select_back=False):
    """Temporarily leave this instance's single pose rig inside an Undo Tool.

    Optional replay data contains only mode/body_or_back/active_bone strings.
    Native selection references remain local to this explicit transaction.
    """
    from ...util.resources import live_ids, resolve_view_layer
    from .human import rig_of

    ensure_mutation_allowed()
    require_owned(root, (body_rig, body_rig.data))
    if rig_of(root) != body_rig:
        raise ValueError("背饰操作目标不是此实例的主骨架")
    layer: Any = resolve_view_layer(cast(Any, bpy.context.scene), require_active=True)
    active: Any = layer.objects.active
    original_mode = bpy.context.mode
    accessory = rig_for(root, body_rig)
    if original_mode not in {"OBJECT", "POSE"}:
        raise ValueError("请在物体或姿态模式操作背饰")
    if original_mode == "POSE":
        editing = tuple(bpy.context.objects_in_mode)
        if len(editing) != 1 or editing[0] != active or active not in {body_rig, accessory}:
            raise ValueError("背饰操作只支持当前实例的单骨架姿态模式")
    role = "BACK" if active == accessory and accessory is not None else "BODY"
    active_bone = active.data.bones.active if original_mode == "POSE" else None
    bone_name = active_bone.name if active_bone else ""
    if snapshot is not None:
        if (not isinstance(snapshot, dict) or set(snapshot) != {"mode", "body_or_back", "active_bone"}
                or snapshot["mode"] not in {"OBJECT", "POSE"}
                or snapshot["body_or_back"] not in {"BODY", "BACK"}
                or not isinstance(snapshot["active_bone"], str)):
            raise ValueError("背饰姿态操作快照无效")
        if original_mode == "POSE" and (snapshot["mode"] != "POSE" or snapshot["body_or_back"] != role):
            raise ValueError("当前姿态骨架与背饰操作快照不一致")
        desired_mode, desired_role, desired_bone = snapshot["mode"], snapshot["body_or_back"], snapshot["active_bone"]
    else:
        desired_mode, desired_role, desired_bone = original_mode, role, bone_name
    selected = tuple(bpy.context.selected_objects)
    bone_selection = {bone.name: _selection_owner(active, bone).select for bone in active.data.bones} if original_mode == "POSE" else {}
    failed = True
    if original_mode == "POSE":
        bpy.ops.object.mode_set(mode="OBJECT")
    try:
        yield
        if select_back and not any(control(root, body_rig, kind) is not None for kind in ("elytra", "cape")):
            raise ValueError("请先启用背饰后定位控制骨")
        failed = False
    finally:
        # Ordinary Object-mode Tools own their successful selection.
        if failed or desired_mode == "POSE" or select_back:
            # Structural replacement restores its exact prior IDs on failure. On
            # success resolve the new accessory from its owned root reference.
            restore_mode = original_mode if failed else "POSE" if select_back else desired_mode
            target: Any = active if failed else ((rig_for(root, body_rig) or body_rig) if select_back or desired_role == "BACK" else body_rig)
            chosen_by_tool = target.data.bones.active if not failed and select_back else None
            name = bone_name if failed else chosen_by_tool.name if chosen_by_tool else desired_bone
            if bpy.context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            for obj in tuple(bpy.context.selected_objects):
                obj.select_set(False, view_layer=layer)
            for obj in live_ids(selected):
                if layer.objects.get(obj.name) == obj:
                    cast(Any, obj).select_set(True, view_layer=layer)
            if restore_mode == "POSE":
                if target is None or layer.objects.get(target.name) != target:
                    raise ValueError("背饰操作后无法恢复原姿态骨架")
                # Prevent accidental multi-armature Pose entry when restoring an
                # old selection containing another selected, non-editing armature.
                for obj in tuple(bpy.context.selected_objects):
                    if obj.type == "ARMATURE" and obj != target:
                        obj.select_set(False, view_layer=layer)
                target.select_set(True, view_layer=layer)
                layer.objects.active = target
                bpy.ops.object.mode_set(mode="POSE")
                for bone in target.data.bones:
                    _selection_owner(target, bone).select = bool(bone_selection.get(bone.name, False))
                chosen = target.data.bones.get(name)
                if chosen is None:
                    mode = int(root["oxal_human_controls"].get(key("back_mode"), 0))
                    chosen = target.data.bones.get("鞘翅展开" if mode == 1 else "披风·根")
                if chosen is not None:
                    _selection_owner(target, chosen).select = True
                target.data.bones.active = chosen
            else:
                original = live_ids((active,)) if active is not None else ()
                layer.objects.active = original[0] if original else (target if active is not None and target in tuple(layer.objects) else None)


def contract(root, rig):
    """Native structural proof, excluding editable pose transforms and UI state."""
    from ...util import drivers
    from ...util.ownership import DATA_UID
    from . import human_back242
    from .human_parts import _capture_wings

    if human_back242.current(root) and not is_back_rig(rig):
        return human_back242.contract(root, rig)

    snapshot = _capture_wings(rig, tuple(rig.data.bones.keys()))[0]
    for spec in snapshot:
        spec.pop("basis")
        spec.pop("rotation_mode")
        spec.pop("shape_options", None)
        spec.pop("shape_transform", None)
        # Driver-evaluated hide flags are not independent authored structure.
        spec["flags"].pop("hide")

    def plain(value):
        if isinstance(value, bpy.types.ID):
            require_owned(root, (value,))
            return (value.id_type, value.get(DATA_UID))
        if isinstance(value, dict):
            return {name: plain(part) for name, part in value.items() if name not in {"select", "select_control_point", "select_left_handle", "select_right_handle"}}
        if isinstance(value, (str, int, bool, type(None))):
            return value
        if isinstance(value, float):
            return round(value, 6)
        return [plain(part) for part in value]

    data = [snapshot, drivers.capture(rig), drivers.capture(rig.data),
            [(c.name, c.parent.name if c.parent else None) for c in rig.data.collections_all]]
    from . import human_back242

    if human_back242.current(root):
        data.append(human_back242.contract_data(root, rig))
    return hashlib.sha256(json.dumps(plain(data), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_retirement(root, rig, names):
    """Reject user animation/structural edits before discarding a native closure."""
    from ...util.animation import action_fcurves
    from . import human_back242
    from .human_parts import _bone_path

    if human_back242.current(root):
        human_back242.validate_retirement(root)
        if any(bone.parent and bone.parent.name in names and bone.name not in names for bone in rig.data.bones):
            raise ValueError("用户骨骼仍连接背饰骨骼，未切换背饰")
        if not is_back_rig(rig):
            expected = root.get(CONTRACT)
            if ((expected is None and (names or human_back242.members(root)))
                    or expected is not None and expected != contract(root, rig)):
                raise ValueError("主骨架背饰含用户结构／驱动修改，未替换")

    for owner in (rig, rig.data):
        animation = owner.animation_data
        if animation is None:
            continue
        bindings = [animation]
        strips = [strip for track in animation.nla_tracks for strip in track.strips]
        while strips:
            strip = strips.pop()
            bindings.append(strip)
            strips.extend(getattr(strip, "strips", ()))
        if any(_bone_path(curve.data_path, names)
               for binding in bindings for curve in action_fcurves(binding.action, slot=getattr(binding, "action_slot", None))):
            raise ValueError("背饰骨骼已有Action／NLA动画，未删除；原动画保持不变")
    if rig.get(RIG_MARK) == SCHEMA_VERSION and rig.get(CONTRACT) != contract(root, rig):
        raise ValueError("背饰骨架含用户结构／驱动修改，未替换")


def validate_legacy(root, rig, source, names, transaction):
    """Project only the released merged-bone formats onto the source contract."""
    from .human_parts import _capture_wings, _drop_wings, _edit_rig, _name

    if any(bone.parent and bone.parent.name in names and bone.name not in names for bone in rig.data.bones):
        raise ValueError("用户骨骼仍连接旧背饰骨骼，未迁移")
    actual, expected = _capture_wings(rig, names), _capture_wings(source, names)
    historical = {}
    if root.get(SCHEMA, 1) == 1:
        # Reproduce the one released native-RNA assignment error on a private
        # scratch copy. This admits only its exact result, never arbitrary rest
        # edits, and does not alter the public rig or the immutable template.
        scratch = source.copy()
        transaction.track(scratch)
        scratch.data = source.data.copy()
        transaction.track(scratch.data)
        scratch.animation_data_clear()
        scratch.data.animation_data_clear()
        for pose in scratch.pose.bones:
            for constraint in tuple(pose.constraints):
                pose.constraints.remove(constraint)
        root.objects.link(scratch)
        _drop_wings(scratch, names)
        with _edit_rig(scratch) as editable:
            for spec in expected[0]:
                bone = editable.new(spec["name"])
                bone.matrix = spec["matrix"]
                bone.length = spec["length"]
            for spec in expected[0]:
                editable[spec["name"]].parent = editable.get(spec["parent"])
                editable[spec["name"]].use_connect = spec["connect"]
        historical = {name: scratch.data.bones[name].matrix_local.copy() for name in names}
        transaction.remove((scratch, scratch.data))

    def plain(value):
        if isinstance(value, bpy.types.ID):
            if value in {rig, source}:
                return "SELF"
            if value in {rig.data, source.data}:
                return "SELF_DATA"
            name = _name(value)
            while True:
                prefix, separator, suffix = name.rpartition(".")
                if not separator or not suffix.isdigit():
                    return (value.id_type, name)
                name = prefix
        if isinstance(value, dict):
            return {name: plain(part) for name, part in value.items()}
        if isinstance(value, (str, int, bool, type(None))):
            return value
        if isinstance(value, float):
            return value
        return [plain(part) for part in value]

    def equal(first, second):
        if isinstance(first, (float, int)) and isinstance(second, (float, int)):
            return abs(first - second) < 0.00001
        if isinstance(first, dict) and isinstance(second, dict):
            return first.keys() == second.keys() and all(equal(first[name], second[name]) for name in first)
        if isinstance(first, (tuple, list)) and isinstance(second, (tuple, list)):
            return len(first) == len(second) and all(equal(a, b) for a, b in zip(first, second, strict=True))
        return first == second

    for first, second in zip(actual[0], expected[0], strict=True):
        for spec in (first, second):
            spec.pop("basis")
            spec.pop("rotation_mode")
            spec.pop("shape_options", None)
            spec.pop("shape_transform", None)
            spec["flags"].pop("hide")
        old_matrix, source_matrix = first.pop("matrix"), second.pop("matrix")
        compatible = equal(plain(old_matrix), plain(source_matrix))
        if first["name"] in historical:
            compatible |= equal(plain(old_matrix), plain(historical[first["name"]]))
        if not compatible or not equal(plain(first), plain(second)):
            raise ValueError("旧背饰骨骼含未知结构编辑，未迁移：" + first["name"])
    # Known generated mode Y/Z drivers were individually proven by preflight.
    def native(curves):
        return [curve for curve in curves if not (curve[1] == 'pose.bones["披风模式"].location' and curve[2] in {1, 2})]
    if not equal(plain(native(actual[1])), plain(native(expected[1]))):
        raise ValueError("旧背饰骨骼含未知原生驱动，未迁移")


def control(root, rig, kind):
    """Read-only UI accessor to existing native controls, never an initializer."""
    from .human import parameter_owner

    names = {"elytra": "鞘翅展开", "cape": "披风·根", "cape_target": "披风·单段目标", "cape_start": "披风·单段", "cape_middle": "披风·单段中点"}
    names.update({"cape_segment_" + str(index): "披风·柔性" + str(index) for index in range(1, 7)})
    name = names.get(kind)
    if name is None:
        raise ValueError("背饰控制器无效")
    owner = parameter_owner(root)
    mode = int(owner.get(key("back_mode"), 0))
    if name not in bones(mode, root):
        return None
    accessory = rig_for(root, rig)
    return (accessory or rig).pose.bones.get(name)


def set_control(root, rig, kind, value):
    """Set the original native wing slider without editing its Action/NLA."""
    ensure_mutation_allowed()
    if kind != "elytra" or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("鞘翅开合必须为0到1的有限数值")
    require_owned(root, (root, rig, rig.data))
    pose = control(root, rig, kind)
    if pose is None:
        raise ValueError("请先启用鞘翅")
    pose.location.y = -WING_TRAVEL * float(value)
    pose.id_data.update_tag()


def validate_png(filepath):
    """Both 196 back images use the source model's 2:1 UV image layout."""
    source = Path(bpy.path.abspath(str(filepath))).resolve()
    if not source.is_file() or source.suffix.lower() != ".png":
        raise ValueError("请选择有效的披风／鞘翅PNG")
    with source.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("披风／鞘翅PNG文件头无效")
    width, height = int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    if width < 32 or width > 8192 or width != height * 2:
        raise ValueError("披风／鞘翅PNG须为2:1，宽度至少32像素")
    return source


def validate_source(root, obj, reference):
    """Keep material edits, but refuse to discard a remodeled back mesh."""
    from .human_layer_resolution import _attribute, _plain
    from .human_outline import generated_modifiers

    require_owned(root, (obj, obj.data))
    if reference is None or obj.data.shape_keys or obj.data.animation_data:
        raise ValueError("背饰网格来源未知或含用户形态／动画，未替换")

    def geometry(value):
        mesh = value.data
        attributes = {attribute.name: _plain(_attribute(attribute)) for attribute in mesh.attributes
                      if attribute.name not in {"position", ".edge_verts", ".corner_vert", ".corner_edge", "material_index"}
                      and attribute.name not in mesh.uv_layers
                      and not attribute.name.startswith((".select_", ".hide_", ".uv_select_"))}
        return ([tuple(vertex.co) for vertex in mesh.vertices],
                [tuple(edge.vertices) for edge in mesh.edges],
                [(tuple(face.vertices), face.use_smooth) for face in mesh.polygons],
                {layer.name: [tuple(item.uv) for item in layer.data] for layer in mesh.uv_layers},
                [group.name for group in value.vertex_groups],
                [[(group.group, group.weight) for group in vertex.groups] for vertex in mesh.vertices],
                attributes, [tuple(normal.vector) for normal in mesh.corner_normals] if mesh.has_custom_normals else [])

    modifiers = tuple(modifier for modifier in obj.modifiers if modifier not in generated_modifiers(root, obj))
    actual, expected = geometry(obj), geometry(reference)
    changed = [name for name, first, second in zip(("顶点", "边", "面", "UV", "顶点组", "权重", "属性", "法线"), actual, expected, strict=True) if first != second]
    expected_modifiers = tuple(reference.modifiers)
    compatible = [modifier.type for modifier in modifiers] == [modifier.type for modifier in expected_modifiers]
    if compatible:
        fields = ("object", "use_vertex_groups", "use_bone_envelopes", "use_deform_preserve_volume", "vertex_group", "invert_vertex_group", "use_multi_modifier")
        compatible = all(all(getattr(modifier, field) == getattr(template, field) for field in fields)
                         for modifier, template in zip(modifiers, expected_modifiers, strict=True) if modifier.type == "ARMATURE")
    if changed or not compatible:
        raise ValueError("背饰来源结构不匹配，未替换：" + obj.name + "（" + "、".join(changed or ["修改器"]) + "）")


def validate_mode_control(root, rig):
    """Preflight the original mode control before retiring any native bones."""
    from ...util.animation import action_fcurves
    from .human import parameter_owner

    ensure_mutation_allowed()
    require_owned(root, (root, rig, rig.data))
    owner = parameter_owner(root)
    from . import human_back242

    if human_back242.current(root):
        if any(name in rig.data.bones for name in MODE_BONES) or "披风模式" in rig:
            raise ValueError("2.4.2实例含未知背饰模式控制骨，未接管")
        return
    mode = rig.pose.bones.get("披风模式")
    mode_path = mode.path_from_id("location") if mode is not None else ""
    animation = rig.animation_data
    if mode_path and animation:
        channels = list(action_fcurves(animation.action, slot=getattr(animation, "action_slot", None)))
        strips = [strip for track in animation.nla_tracks for strip in track.strips]
        while strips:
            strip = strips.pop()
            channels.extend(action_fcurves(strip.action, slot=getattr(strip, "action_slot", None)))
            strips.extend(getattr(strip, "strips", ()))
        if any(curve.data_path == mode_path and curve.array_index in {1, 2} for curve in channels):
            raise ValueError("背饰模式骨柄已有Action／NLA动画，未覆盖")
        for curve in animation.drivers:
            if curve.data_path != mode_path or curve.array_index not in {1, 2}:
                continue
            variables = tuple(curve.driver.variables)
            known = (curve.driver.type == "SCRIPTED" and curve.driver.expression == "-mode*0.10034234308426679"
                     and len(variables) == 1 and variables[0].name == "mode" and variables[0].type == "SINGLE_PROP"
                     and len(variables[0].targets) == 1 and variables[0].targets[0].id == owner
                     and variables[0].targets[0].data_path == path("back_mode"))
            if not known:
                raise ValueError("背饰模式骨柄含未知Y／Z驱动，未覆盖")


def prepare(transaction, root, rig, objects=None, *, journal=True):
    """Restore known control metadata and state on an explicit owned edit."""
    from ...util import drivers
    from .human import _drive, parameter_owner

    accessory = rig_for(root, rig)
    rig = accessory or rig
    validate_mode_control(root, rig)
    owner = parameter_owner(root)
    from . import human_back242

    if human_back242.current(root):
        root[SCHEMA] = human_back242.MAIN_SCHEMA if accessory is None else SCHEMA_VERSION
        human_back242.finalize_visibility(root, rig)
        return
    mode = rig.pose.bones.get("披风模式")
    mode_path = mode.path_from_id("location") if mode is not None else ""
    previous_schema = root.get(SCHEMA)
    snapshot = drivers.capture(rig)
    properties = []

    def restore():
        drivers.restore(rig, snapshot)
        if previous_schema is None:
            if SCHEMA in root:
                del root[SCHEMA]
        else:
            root[SCHEMA] = previous_schema
        for pose, values in reversed(properties):
            for name, existed, value in values:
                if existed:
                    pose[name] = value
                elif name in pose:
                    del pose[name]

    if journal:
        transaction.on_rollback(restore)
    for name, travel, description in (
        ("鞘翅展开", WING_TRAVEL, "向下展开，向上收拢；轨道可选中带动整套鞘翅。"),
        ("披风模式", MODE_TRAVEL, "从上到下：全部关闭 / 鞘翅 / 单段披风 / 五段披风"),
    ):
        pose = rig.pose.bones.get(name)
        if pose is None:
            continue
        values = [(field, field in pose, pose.get(field)) for field in ("滑动行程", "说明")]
        properties.append((pose, values))
        if "滑动行程" not in pose:
            pose["滑动行程"] = travel
        if "说明" not in pose:
            pose["说明"] = description
    if mode is not None:
        if rig.animation_data:
            old = next((curve for curve in rig.animation_data.drivers if curve.data_path == mode_path and curve.array_index == 1), None)
            if old is not None:
                rig.driver_remove(mode_path, 1)
        _drive(rig, mode.path_from_id("location"), root, "-mode*0.10034234308426679", {"mode": "back_mode"}, 2)
    # Existing schema-1 controls may sit on bones made with the historical
    # zero-length matrix assignment bug. Metadata repair alone must not claim
    # that their authored rest matrices have already been rebuilt.
    if accessory is not None or mode is None or not journal or previous_schema == SCHEMA_VERSION:
        root[SCHEMA] = SCHEMA_VERSION
    owner.update_tag()
    rig.update_tag()


def template(transaction, root, body_rig, source_rig, mode, mesh):
    """Keep the authored Armature, collection visibility and pose control graph."""
    from . import human_back242
    from .human_armor import rebind_incoming
    from .human_parts import _bone_path, _drop_wings

    if human_back242.current(root):
        return human_back242.template(transaction, root, body_rig, source_rig, mode, mesh)

    rig = source_rig.copy()
    transaction.track(rig)
    rig.data = source_rig.data.copy()
    transaction.track(rig.data)
    rig[RIG_MARK] = SCHEMA_VERSION
    # All replacements apply only to this explicit copy and selected new mesh.
    rebind_incoming((rig, rig.data, mesh), {source_rig: rig, source_rig.data: rig.data,
                                         body_rig: rig, body_rig.data: rig.data})
    root.objects.link(rig)
    rig.parent = None
    rig.matrix_world = body_rig.matrix_world.copy()
    wanted = {"胸口", *bones(mode)}
    removed = set(rig.data.bones.keys()) - wanted
    for owner in (rig, rig.data):
        if not owner.animation_data:
            continue
        for curve in tuple(owner.animation_data.drivers):
            targets = [target for variable in curve.driver.variables for target in variable.targets]
            if (_bone_path(curve.data_path, removed)
                    or any(target.id == rig and (target.bone_target in removed or _bone_path(target.data_path, removed)) for target in targets)):
                owner.animation_data.drivers.remove(curve)
    _drop_wings(rig, tuple(removed))
    chest = rig.pose.bones["胸口"]
    chest.custom_shape = None
    constraint = chest.constraints.new("COPY_TRANSFORMS")
    constraint.name = "OXAL Body Chest"
    constraint.target, constraint.subtarget = body_rig, "胸口"
    constraint.owner_space = constraint.target_space = "WORLD"
    chest.bone.hide = True
    return rig
