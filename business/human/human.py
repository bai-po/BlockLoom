"""Independent BaiPo instances with native controls and explicit edit commands.

Identity oxal.human / 2.4.7; NEW_INSTANCE; instance ownership.
Versioned native templates in human_source, pinned by SOURCE.json.
No plugin driver functions, frame handlers, or playback-time reconstruction.
"""

import ast
import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from time import perf_counter
from typing import Any, cast
from uuid import uuid4

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.geometry_nodes import get_input, input_path, set_input
from ...util.ownership import (
    REGISTRY,
    owned_ids,
    owns,
    release_owned,
    remove_unused_owned,
    require_no_external_users,
    require_owned,
    seal_owned,
)
from ...util.resources import (
    ASSET_ID,
    INSTANCE,
    VERSION,
    AppendTransaction,
    ImportResult,
    resolve_scene,
    resolve_view_layer,
)
from .human_source import (
    CURRENT,
    HEAD_HD_MARKER,
    LEGACY,
    body_path,
    is_skin_material,
    model_version,
    root_name,
    skin_image,
    skin_materials,
    skin_nodes,
)
from .human_state import (
    ARMS,
    LEGS,
    OWNER,
    PARAMETERS,
    SOURCE_NAME,
    key,
    parameter_spec,
    path,
)

RESOURCE_ID = "oxal.human"
RESOURCE_VERSION = CURRENT
ROOT_NAME = root_name()
POLICY = "NEW_INSTANCE"
SCOPE = "instance"
BINDING = "OXAL MC Studio · 人模绑定"
MAIN_RIG = "oxal_mc_studio_human_main_rig"
_MATH = {"abs", "acos", "asin", "atan", "atan2", "ceil", "cos", "exp", "floor", "log", "max", "min", "pow", "round", "sin", "sqrt", "tan"}
HumanResult = str | dict[str, Any]


def source_name(item: Any) -> str:
    saved = item.get(SOURCE_NAME)
    if saved:
        return str(saved)
    ref = item.library_weak_reference
    return ref.id_name[2:] if ref else item.name


def _matches(item: Any, name: str) -> bool:
    actual = source_name(item)
    return actual == name or (actual.startswith(name + ".") and actual[len(name) + 1:].isdigit())


def roots(scene: Any) -> list[Any]:
    return [c for c in scene.collection.children_recursive if c.get(ASSET_ID) == RESOURCE_ID and c.get(INSTANCE)]


def resolve_human(scene: Any, instance_id: str = "") -> Any:
    candidates = roots(scene)
    if not instance_id:
        current = scene.get("oxal_mc_studio_active_human")
        if current in candidates:
            instance_id = str(current[INSTANCE])
        elif len(candidates) == 1:
            instance_id = str(candidates[0][INSTANCE])
        else:
            raise ValueError("请先选择一个新版人模实例")
    found = [c for c in candidates if c.get(INSTANCE) == instance_id]
    if len(found) != 1:
        raise ValueError("人模已删除或实例标识不唯一")
    root = found[0]
    if root.library or root.override_library:
        raise ValueError("链接人模不可修改")
    if sum(root in tuple(s.collection.children_recursive) for s in bpy.data.scenes) != 1:
        raise ValueError("人模被多个场景共享，请先建立独立实例")
    _require_instance(root)
    return root


def rig_of(root: Any) -> Any:
    saved = root.get(MAIN_RIG)
    if saved is not None:
        if (not isinstance(saved, bpy.types.Object) or saved.type != "ARMATURE" or not isinstance(saved.data, bpy.types.Armature)
                or saved not in tuple(root.all_objects)):
            raise ValueError("人模主骨架的实际引用已失效，未按名称接管")
        if REGISTRY in root:
            require_owned(root, (root, saved, saved.data))
        return saved
    found = [o for o in root.all_objects if o.type == "ARMATURE"]
    if len(found) != 1:
        raise ValueError("人模没有唯一主骨架")
    return found[0]


def parameter_owner(root: Any) -> Any:
    controller = root.get("oxal_human_controls")
    if controller is None or controller.get(OWNER) != root.get(INSTANCE) or (REGISTRY in root and not owns(root, controller)):
        raise ValueError("人模缺少独立参数控制器")
    return controller


def _require_instance(root: Any) -> None:
    """Copied UUIDs/tags never authorize edits to arbitrary instance members."""
    model_version(root)
    require_owned(root, (root, *root.children_recursive, *root.all_objects))
    require_owned(root, (parameter_owner(root),))


def library_path(kind: str) -> Path:
    if kind not in {"skins", "armor", "cape", "elytra"}:
        raise ValueError("未知人模资源库")
    from ...util.user_storage import personal_directory

    base = personal_directory("human", "OXAL_MC_STUDIO_HUMAN_LIBRARY_DIR")
    result = base / kind
    result.mkdir(parents=True, exist_ok=True)
    return result


def library_items(kind: str) -> list[Path]:
    items = sorted(library_path(kind).glob("*.png"), key=lambda p: p.name.casefold())
    if kind == "skins":
        items = [file for _, file in builtin_skins()] + items
    else:
        from .human_packs import pack_rows

        items.extend(pack_rows())
    return items


def builtin_skins() -> tuple[tuple[str, Path], ...]:
    """Immutable bundled defaults; no scan, decoding or user-library writes."""
    base = Path(__file__).parents[2] / "asset" / "library" / "assets" / "minecraft" / "textures" / "entity" / "player"
    return (("史蒂夫 · Steve", base / "wide" / "steve.png"),
            ("艾莉克斯 · Alex", base / "slim" / "alex.png"))


def _png(value: str, *, skin: bool = False) -> Path:
    p = Path(bpy.path.abspath(value)).resolve()
    if not p.is_file() or p.suffix.lower() != ".png":
        raise ValueError("请选择有效的 PNG 文件")
    with p.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG 文件头无效")
    width, height = int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    if skin and (width, height) not in {(64, 64), (128, 128)}:
        raise ValueError("皮肤必须为 64×64 或 128×128 PNG")
    if not 0 < width <= 8192 or not 0 < height <= 8192:
        raise ValueError("PNG 尺寸超出支持范围")
    image = None
    try:
        image = bpy.data.images.load(str(p), check_existing=False)
        # File-image decoding is lazy. Force the native decoder before a
        # library entry is accepted or an existing skin is changed.
        if tuple(image.size) != (width, height) or not image.has_data:
            raise ValueError("PNG 无法完整解码，未导入")
    except RuntimeError as exc:
        raise ValueError("PNG 无法完整解码，未导入") from exc
    finally:
        if image is not None:
            bpy.data.images.remove(image)
    return p


def _drive(owner: Any, data_path: str, root: Any, expression: str, variables: Mapping[str, str], index: int | None = None) -> None:
    try:
        if index is None:
            owner.driver_remove(data_path)
        else:
            owner.driver_remove(data_path, index)
    except (TypeError, RuntimeError):
        pass
    f = owner.driver_add(data_path) if index is None else owner.driver_add(data_path, index)
    d = f.driver
    d.type = "SCRIPTED"
    for v in tuple(d.variables):
        d.variables.remove(v)
    for name, data in variables.items():
        v = d.variables.new()
        v.name, v.type = name, "SINGLE_PROP"
        v.targets[0].id_type = "OBJECT"
        v.targets[0].id = parameter_owner(root)
        if "[" in data:
            parameter, axis = data.split("[", 1)
            v.targets[0].data_path = path(parameter) + "[" + axis
        else:
            v.targets[0].data_path = path(data)
    d.expression = expression


def _audit_drivers(items: Any) -> None:
    for item in items:
        for owner in (item, getattr(item, "node_tree", None)):
            animation = getattr(owner, "animation_data", None)
            if not animation:
                continue
            for f in animation.drivers:
                if f.driver.type != "SCRIPTED":
                    continue
                tree = ast.parse(f.driver.expression or "0", mode="eval")
                if any(isinstance(n, (ast.Attribute, ast.Subscript, ast.Lambda)) for n in ast.walk(tree)):
                    raise ValueError("人模资源含非原生驱动表达式")
                for n in ast.walk(tree):
                    if isinstance(n, ast.Call) and (not isinstance(n.func, ast.Name) or n.func.id not in _MATH):
                        raise ValueError("人模资源依赖非原生驱动函数")


def _private_materials(root: Any, transaction: AppendTransaction) -> None:
    materials: dict[Any, Any] = {}
    groups: dict[Any, Any] = {}

    def private_group(group: Any) -> Any:
        if group not in groups:
            copy = group.copy()
            copy.use_fake_user = False
            transaction.track(copy)
            groups[group] = copy
            for node in copy.nodes:
                if node.type == "GROUP" and node.node_tree:
                    node.node_tree = private_group(node.node_tree)
        return groups[group]

    for obj in tuple(root.all_objects):
        for slot in obj.material_slots:
            original = slot.material
            if original is None:
                continue
            if original not in materials:
                copy = original.copy()
                copy.use_fake_user = False
                transaction.track(copy)
                materials[original] = copy
                copy[SOURCE_NAME] = source_name(original)
                if copy.node_tree:
                    for node in copy.node_tree.nodes:
                        if node.type == "GROUP" and node.node_tree:
                            node.node_tree = private_group(node.node_tree)
            slot.material = materials[original]
    _audit_drivers([*materials.values(), *groups.values()])


def _constraint_animated(obj: Any, constraint: Any) -> bool:
    animation = obj.animation_data
    if animation is None:
        return False
    prefix = constraint.path_from_id()
    if any(f.data_path.startswith(prefix) for f in animation.drivers):
        return True
    bindings = [strip for track in animation.nla_tracks for strip in track.strips if strip.action]
    if animation.action:
        bindings.append(animation)
    for binding in bindings:
        action = binding.action
        slot = getattr(binding, "action_slot", None)
        if getattr(action, "slots", ()) and slot is None:
            continue
        if any(f.data_path.startswith(prefix) for f in action_fcurves(action, slot=slot)):
            return True
    return False


def _initialize_parameters(root: Any, options: Mapping[str, object]) -> None:
    specifications = {name: parameter_spec(name, root) for name in PARAMETERS}
    root = parameter_owner(root)
    for name, (label, default, minimum, maximum) in specifications.items():
        root[key(name)] = default
        ui = root.id_properties_ui(key(name))
        if isinstance(default, bool):
            ui.update(description=label)
        elif isinstance(default, tuple):
            ui.update(description=label, min=minimum, max=maximum, subtype="COLOR")
        else:
            ui.update(description=label, min=minimum, max=maximum)
    root[key("arm_mode")] = int(options.get("arm_mode", "SLIM") == "WIDE")
    root[key("leg_mode")] = int(options.get("leg_mode", "NORMAL") == "OFFICIAL")
    for name in ("name_enabled", "leg_fixed", "foot_fixed"):
        root[key(name)] = bool(options.get(name, False))


def _armor_part(obj: Any) -> str | None:
    name = source_name(obj)
    for prefixes, part in ((("盔甲头盔",), "head"), (("盔甲躯干", "盔甲左手", "腿甲右手"), "chest"), (("盔甲裤子", "盔甲左腿", "盔甲右腿"), "legs"), (("盔甲左脚", "盔甲右脚"), "feet")):
        if name.startswith(prefixes):
            return part
    return None


def _prepare_controls(root: Any, rig: Any, *, objects: Any = None, material_ids: Any = None) -> None:
    # Visibility/driver edits can invalidate Blender's live collection iterator.
    # Freeze object identities before any change to dependency or visibility.
    full = objects is None
    material_ids = set() if material_ids is None else material_ids
    objects = tuple(root.all_objects) if full else tuple(objects)
    variant: dict[Any, tuple[str, int]] = {}
    for feature, choices in (("arm_mode", ARMS), ("leg_mode", LEGS)):
        for value, name in enumerate(choices.values()):
            coll = next((c for c in root.children_recursive if _matches(c, name)), None)
            if coll is None:
                raise ValueError(f"人模资源缺少样式：{name}")
            variant.update({o: (feature, value) for o in coll.all_objects})
    shapes = {p.custom_shape for p in rig.pose.bones if p.custom_shape}
    for obj in objects:
        if obj in shapes:
            obj.hide_set(True)
            obj.hide_render = True
        if obj in variant:
            feature, value = variant[obj]
            for attr in ("hide_viewport", "hide_render"):
                _drive(obj, attr, root, f"value!={value}", {"value": feature})
        if _matches(obj, "名字"):
            for attr in ("hide_viewport", "hide_render"):
                _drive(obj, attr, root, "not enabled", {"enabled": "name_enabled"})
        for name, mode in (("New model", 1), ("披风·单段", 2), ("披风·五段", 3)) if model_version(root) == LEGACY else ():
            if _matches(obj, name):
                for attr in ("hide_viewport", "hide_render"):
                    _drive(obj, attr, root, f"mode!={mode}", {"mode": "back_mode"})
        part = _armor_part(obj)
        if part:
            variables = {"enabled": part + "_enabled"}
            expr = "not enabled"
            if part in {"legs", "feet"} and any(s in source_name(obj) for s in ("左", "右")):
                variables["official"] = "leg_mode"
                expr += f" or official!={int('-官方腿' in source_name(obj))}"
            for attr in ("hide_viewport", "hide_render"):
                _drive(obj, attr, root, expr, variables)
            for modifier in obj.modifiers:
                if (modifier.type == "NODES" and modifier.node_group
                        and not any(modifier.node_group.get(marker) for marker in (
                            "bp_armor_envelope214", "oxal_mc_studio_enchant_geometry_schema",
                            "oxal_mc_studio_enchant_outline_schema", "oxal_mc_studio_solidify_outline_schema"))):
                    for attr in ("show_viewport", "show_render"):
                        _drive(obj, modifier.path_from_id(attr), root, "enabled", {"enabled": part + "_3d"})
                    for socket in modifier.node_group.interface.items_tree:
                        if getattr(socket, "in_out", "") == "INPUT" and socket.name == "厚度":
                            _drive(obj, input_path(modifier, socket.identifier), root, "value", {"value": part + "_thickness"})
            for material in (s.material for s in obj.material_slots):
                if not material or not material.node_tree or (not full and material not in material_ids):
                    continue
                for node in material.node_tree.nodes:
                    if node.type != "GROUP":
                        continue
                    for socket, suffix in (("材质编号", "material"), ("花纹编号", "pattern"), ("皮革颜色", "leather_color"), ("花纹颜色", "trim_color")):
                        if socket not in node.inputs:
                            continue
                        target_path = node.inputs[socket].path_from_id("default_value")
                        if suffix.endswith("color"):
                            for axis in range(4):
                                _drive(material.node_tree, target_path, root, "value", {"value": f"{part}_{suffix}[{axis}]"}, axis)
                        else:
                            _drive(material.node_tree, target_path, root, "value", {"value": part + "_" + suffix})
        for modifier in obj.modifiers:
            if modifier.type != "NODES" or not modifier.node_group or not modifier.node_group.get("bp_second_layer_geometry"):
                continue
            if obj in variant:
                feature, value = variant[obj]
                expr, variables = f"enabled and mode=={value}", {"enabled": "layer_enabled", "mode": feature}
            else:
                expr, variables = "enabled", {"enabled": "layer_enabled"}
            for attr in ("show_viewport", "show_render"):
                _drive(obj, modifier.path_from_id(attr), root, expr, variables)
            mapping = {"对外厚度": "outer", "对内厚度": "inner", "随机高低凸出": "random_enabled", "随机系数": "random_factor", "高低比例": "height_ratio", "随机种子": "seed", "中心高外围低": "center_falloff", "中心衰减范围": "center_range"}
            for socket in modifier.node_group.interface.items_tree:
                if getattr(socket, "in_out", "") == "INPUT" and socket.name in mapping:
                    _drive(obj, input_path(modifier, socket.identifier), root, "value", {"value": mapping[socket.name]})
    if full:
        if model_version(root) == LEGACY:
            _drive(rig, '["披风模式"]', root, "value", {"value": "back_mode"})
            mode_bone = rig.pose.bones.get("披风模式")
            if mode_bone:
                _drive(rig, mode_bone.path_from_id("location"), root, "-mode*0.10034234308426679", {"mode": "back_mode"}, 2)
        for side in ("L", "R"):
            control = rig.pose.bones.get(side + ".脚控制器")
            if control:
                for c in control.constraints:
                    if c.type == "CHILD_OF" and c.name in {"子级", "子级.001"}:
                        _drive(rig, c.path_from_id("influence"), root, "fixed" if c.name == "子级" else "1-fixed", {"fixed": "leg_fixed"})
    materials = {s.material for obj in objects for s in obj.material_slots if s.material and s.material.node_tree and (full or s.material in material_ids)}
    for material in materials:
        for node in material.node_tree.nodes:
            if node.label == "红温控制器" and node.inputs:
                _drive(material.node_tree, node.inputs[0].path_from_id("default_value"), root, "value", {"value": "red"})
            if node.type == "GROUP" and node.node_tree and node.node_tree.get("bp_cape_count"):
                _drive(material.node_tree, node.inputs["披风编号"].path_from_id("default_value"), root, "value", {"value": "cape_index"})


def _require_rig_layer(rig: Any, layer: Any) -> None:
    if layer.objects.get(rig.name) != rig:
        raise ValueError("人模骨架已从当前视图层排除，请先恢复该合集后再操作")


def _set_ankles(rig: Any, fixed: bool, *, restore_parents: Mapping[str, str] | None = None) -> None:
    ensure_mutation_allowed()
    if bpy.context.mode != "OBJECT":
        raise ValueError("请在物体模式修改脚腕初始绑定")
    if any(animation and (animation.action or animation.nla_tracks) for animation in (rig.animation_data, rig.data.animation_data)):
        raise ValueError("已有 Action/NLA，不能修改脚腕初始绑定；动画保持原样")
    desired = dict(restore_parents) if restore_parents is not None else {side + ".脚腕": side + (".脚控制器" if fixed else ".小腿中间节") for side in ("L", "R")}
    if any(name not in rig.data.bones or (target and target not in rig.data.bones) for name, target in desired.items()):
        raise ValueError("人模缺少脚腕绑定骨骼")
    scene = cast(Any, bpy.context.scene)
    if scene is None:
        raise ValueError("当前场景不存在")
    layer = resolve_view_layer(scene, require_active=True)
    _require_rig_layer(rig, layer)
    previous, selected, hidden = layer.objects.active, tuple(bpy.context.selected_objects), rig.hide_get(view_layer=layer)
    matrices = {b.name: b.matrix_local.copy() for b in rig.data.bones}
    parents = {name: rig.data.bones[name].parent.name if rig.data.bones[name].parent else "" for name in desired}
    poses = {name: rig.pose.bones[name].matrix.copy() for name in desired}
    order = sorted(matrices, key=lambda n: len(rig.data.bones[n].parent_recursive))
    try:
        for obj in selected:
            obj.select_set(False, view_layer=layer)
        layer.objects.active = rig
        rig.hide_set(False, view_layer=layer)
        rig.select_set(True, view_layer=layer)
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            for name, target in desired.items():
                rig.data.edit_bones[name].parent = rig.data.edit_bones.get(target) if target else None
            for name in order:
                rig.data.edit_bones[name].matrix = matrices[name]
        except Exception:
            for name, parent in parents.items():
                rig.data.edit_bones[name].parent = rig.data.edit_bones.get(parent) if parent else None
            for name in order:
                rig.data.edit_bones[name].matrix = matrices[name]
            raise
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
            for name, matrix in poses.items():
                rig.pose.bones[name].matrix = matrix
    finally:
        rig.select_set(False, view_layer=layer)
        rig.hide_set(hidden, view_layer=layer)
        layer.objects.active = previous
        for obj in selected:
            obj.select_set(True, view_layer=layer)


class HumanTool:
    def __init__(self, asset_path: Path | None = None) -> None:
        self.asset_path = asset_path or body_path()

    def load(self, scene_uid: int, options: Mapping[str, object]) -> ImportResult:
        return self._create(scene_uid, options)

    def _create(self, scene_uid, options, *, prepare=None, rollback=None) -> ImportResult:
        """Create atomically; migration prepares only proven new IDs here."""
        started = perf_counter()
        ensure_mutation_allowed()
        allowed = {"arm_mode", "leg_mode", "name_enabled", "at_cursor", "position", "leg_fixed", "foot_fixed", "creation_id"}
        if set(options) - allowed or options.get("arm_mode", "SLIM") not in ARMS or options.get("leg_mode", "NORMAL") not in LEGS:
            raise ValueError("人模初始样式无效")
        for name in ("name_enabled", "at_cursor", "leg_fixed", "foot_fixed"):
            if name in options and not isinstance(options[name], bool):
                raise TypeError(f"{name} 必须是布尔值")
        position = options.get("position", [0.0, 0.0, 0.0])
        if not isinstance(position, (tuple, list)) or len(position) != 3 or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in position):
            raise ValueError("人模位置必须为三个有限数值")
        scene = resolve_scene(scene_uid)
        identity = options.get("creation_id", uuid4().hex)
        if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("人模创建标识无效")
        if any(root.get(INSTANCE) == identity for root in roots(scene)):
            raise ValueError("人模创建标识已存在，未重复创建")
        layer = resolve_view_layer(scene, require_active=True)
        if bpy.context.mode != "OBJECT":
            raise ValueError("请在物体模式导入人模")
        previous, selected = layer.objects.active, tuple(bpy.context.selected_objects)
        previous_root = scene.get("oxal_mc_studio_active_human")
        try:
            with AppendTransaction(self.asset_path) as tx:
                begin = perf_counter()
                root = tx.append_collection(ROOT_NAME)
                library_ms = (perf_counter() - begin) * 1000
                root.name = "白珀人模"
                root[ASSET_ID], root[VERSION], root[INSTANCE] = RESOURCE_ID, RESOURCE_VERSION, identity
                for item in tuple(tx.owned):
                    item[OWNER], item[SOURCE_NAME] = identity, source_name(item)
                rig = rig_of(root)
                root[MAIN_RIG] = rig
                _audit_drivers(tx.owned)
                begin = perf_counter()
                _private_materials(root, tx)
                for item in tuple(tx.owned):
                    item[OWNER] = identity
                controller = bpy.data.objects.new("人模参数", None)
                tx.track(controller)
                controller[OWNER], controller[SOURCE_NAME] = identity, "人模参数"
                controller.hide_select = True
                controller.empty_display_type = "IMAGE"
                controller.show_empty_image_orthographic = False
                controller.show_empty_image_perspective = False
                root.objects.link(controller)
                root["oxal_human_controls"] = controller
                _initialize_parameters(root, options)
                scene.collection.children.link(root)
                _set_ankles(rig, bool(options.get("foot_fixed", False)))
                _prepare_controls(root, rig)
                from .human_parts import prune_initial

                prune_initial(root, rig, tx, str(options.get("arm_mode", "SLIM")), str(options.get("leg_mode", "NORMAL")))
                skin = skin_image(root)
                if skin:
                    root["oxal_human_default_skin"] = skin
                    root["oxal_human_current_skin"] = skin
                    root["oxal_human_skin_pixels"] = int(skin.size[0])
                    from .human_layer_resolution import (
                        prepare as prepare_layer_resolution,
                    )

                    prepare_layer_resolution(tx, root, tuple(root.all_objects), skin)
                root["oxal_human_skin_label"] = "默认皮肤"
                if options.get("at_cursor"):
                    for obj in tuple(root.all_objects):
                        if obj.parent is None:
                            for axis in range(3):
                                obj.location[axis] += position[axis]
                for obj in selected:
                    obj.select_set(False)
                layer.objects.active = rig
                rig.select_set(True)
                scene["oxal_mc_studio_active_human"] = root
                if prepare is not None:
                    prepare(tx, root, rig)
                tx.prune_unused()
                prepare_ms = (perf_counter() - begin) * 1000
                begin = perf_counter()
                rig.update_tag()
                layer.update()
                for view in scene.view_layers:
                    controller.hide_set(True, view_layer=view)
                update_ms = (perf_counter() - begin) * 1000
                try:
                    seal_owned(root, tx.owned)
                    tx.commit()
                except Exception:
                    if owns(root, root):
                        release_owned(root)
                    raise
            return ImportResult(RESOURCE_ID, RESOURCE_VERSION, identity, root.name, rig.name, False, True,
                                (perf_counter() - started) * 1000, library_ms, prepare_ms, update_ms)
        except Exception:
            if rollback is not None:
                rollback()
            layer.objects.active = previous
            if previous_root is not None:
                scene["oxal_mc_studio_active_human"] = previous_root
            elif "oxal_mc_studio_active_human" in scene:
                del scene["oxal_mc_studio_active_human"]
            for obj in selected:
                obj.select_set(True)
            raise

    def edit(self, scene_uid: int, options: Mapping[str, object]) -> HumanResult:
        ensure_mutation_allowed()
        scene = resolve_scene(scene_uid)
        layer = resolve_view_layer(scene, require_active=True)
        action = str(options.get("action", ""))
        values = options.get("values")
        if action == "legacy_compat":
            from .human_compat import execute

            return execute(scene_uid, options)
        if action == "skin_recent_list":
            from .human_skin_history import recent_paths

            if "target" in options:
                from .human_compat import _resolve

                owner = _resolve(scene, options["target"]).rig
            else:
                owner = resolve_human(scene, str(options.get("instance_id", "")))
            return {"recent": [{"label": Path(path).name, "filepath": path} for path in recent_paths(owner)],
                    "owner_uid": owner.session_uid, "scene_uid": scene.session_uid,
                    "message": "已读取当前人模的最近皮肤"}
        back_action = (action in {"back_control", "back_control_select"}
                       or action == "parameters" and isinstance(values, dict) and set(values) == {"back_mode"})
        selection_ui = action == "select" and options.get("selection_ui") is True
        if bpy.context.mode == "POSE" and not back_action and not selection_ui and action not in {"library_list", "library_add"}:
            raise ValueError("姿态模式仅支持背饰模式和控制；其它人模操作请切到物体模式")
        if "back_context" in options and not back_action:
            raise ValueError("背饰上下文不能用于其它人模操作")
        if action == "migrate_legacy":
            from .human_migrate import migrate

            return migrate(self, scene_uid, options)
        if action == "library_list":
            kind = str(options.get("kind", "skins"))
            defaults = {file: label for label, file in builtin_skins()} if kind == "skins" else {}
            rows = []
            for item in library_items(kind):
                label = defaults.get(item, item.name)
                if item.name == "pack.json":
                    metadata = json.loads(item.read_text(encoding="utf8"))
                    if not isinstance(metadata, dict):
                        raise ValueError("人模资源库元数据格式无效")
                    label = str(metadata.get("label", item.parent.name))
                rows.append({"label": label, "filepath": str(item)})
            return {"library": rows, "kind": kind, "message": f"资源库已刷新：{len(rows)} 项"}
        if action == "library_add":
            kind = str(options.get("kind", "skins"))
            filename = str(options.get("filepath", ""))
            library_path(kind)
            if kind != "skins" and Path(filename).suffix.lower() == ".zip":
                from .human_packs import digest_from_file

                digest_from_file(filename)
                return "人模材质包已入库"
            source = _png(filename, skin=kind == "skins")
            content = source.read_bytes()
            target = library_path(kind) / (hashlib.sha256(content).hexdigest()[:12] + "-" + source.name)
            if target.exists():
                if not target.is_file() or target.read_bytes() != content:
                    raise ValueError("资源库已有同名但内容不同的文件，未覆盖")
            else:
                temporary = target.with_suffix(".tmp-" + uuid4().hex)
                try:
                    temporary.write_bytes(content)
                    temporary.replace(target)
                finally:
                    if temporary.is_file():
                        temporary.unlink()
            return "已入库：" + source.name
        root = resolve_human(scene, str(options.get("instance_id", "")))
        _require_instance(root)
        rig = rig_of(root)
        if back_action and (bpy.context.mode == "POSE" or "back_context" in options):
            from .human_back import object_mode

            submitted = dict(options)
            context_snapshot = submitted.pop("back_context", None)
            with object_mode(root, rig, context_snapshot, select_back=action == "back_control_select"):
                return self.edit(scene_uid, submitted)
        if action == "select":
            from ..asset.asset_geometry import Creation
            from .human_compat import _journal_selection

            _require_rig_layer(rig, layer)
            with Creation() as transaction:
                _journal_selection(transaction, layer)
                previous = scene.get("oxal_mc_studio_active_human")
                def restore_active():
                    if previous is None:
                        scene.pop("oxal_mc_studio_active_human", None)
                    else:
                        scene["oxal_mc_studio_active_human"] = previous
                transaction.on_rollback(restore_active)
                hidden = rig.hide_get(view_layer=layer)
                transaction.on_rollback(lambda: rig.hide_set(hidden, view_layer=layer))
                if bpy.context.mode == "POSE":
                    bpy.ops.object.mode_set(mode="OBJECT")
                for obj in tuple(bpy.context.selected_objects):
                    obj.select_set(False)
                rig.hide_set(False)
                rig.select_set(True)
                layer.objects.active = rig
                scene["oxal_mc_studio_active_human"] = root
                transaction.commit()
            if selection_ui:
                return {"message": "已选择人模：" + root.name, "selected_human_rig_uid": rig.session_uid}
        elif action == "remove":
            self._remove(scene, root)
        elif action == "parameters":
            self._parameters(root, rig, options.get("values", {}))
        elif action == "armor_set":
            from .human_armor import set_parts

            set_parts(root, rig, options.get("parts", ()), options.get("enabled"))
        elif action == "head_eye_repair":
            from .human_eye_depth import prepare

            result = prepare(root, rig)
            return "已修复头部闭眼盖片叠面，保留原生眼睛和动画" if result["changed"] else "头部已包含闭眼叠面修订"
        elif action == "repair":
            from .human_armor import repair

            repair(root, rig)
        elif action in {"outline_prepare", "outline_remove", "outline_repair"}:
            from . import human_outline

            {"outline_prepare": human_outline.prepare,
             "outline_remove": human_outline.remove,
             "outline_repair": human_outline.repair}[action](root)
        elif action in {"armor_outline_prepare", "armor_outline_remove", "armor_outline_repair"}:
            from . import human_armor_outline

            {"armor_outline_prepare": human_armor_outline.prepare,
             "armor_outline_remove": human_armor_outline.remove,
             "armor_outline_repair": human_armor_outline.repair}[action](root, parts=options.get("parts", ()))
        elif action == "back_control":
            from .human_back import set_control

            set_control(root, rig, str(options.get("kind", "")), options.get("value"))
        elif action == "back_control_select":
            from .human_back import _selection_owner, control, rig_for

            bone = control(root, rig, str(options.get("kind", "")))
            back_rig = rig_for(root, rig) or rig
            if bone is None:
                raise ValueError("请先启用对应背饰")
            _require_rig_layer(back_rig, layer)
            for obj in tuple(bpy.context.selected_objects):
                obj.select_set(False)
            back_rig.select_set(True)
            layer.objects.active = back_rig
            back_rig.data.bones.active = bone.bone
            for candidate in back_rig.data.bones:
                _selection_owner(back_rig, candidate).select = candidate == bone.bone
        elif action in {"armor_enchant", "armor_unenchant", "armor_enchant_3d", "armor_enchant_outline", "armor_enchant_pixels",
                        "armor_enchant_outline_enable", "armor_enchant_pixels_enable"}:
            from .human_armor import enchant

            enchant(root, options.get("parts", ()), remove=action == "armor_unenchant",
                    prepare_geometry=action == "armor_enchant_3d",
                    prepare_outline=action in {"armor_enchant_outline", "armor_enchant_outline_enable"},
                    prepare_pixels=action in {"armor_enchant_pixels", "armor_enchant_pixels_enable"},
                    activate=action in {"armor_enchant_outline_enable", "armor_enchant_pixels_enable"})
        elif action == "skin":
            self._skin(root, str(options.get("filepath", "")))
        elif action in {"pack", "pack_restore"}:
            self._pack(root, str(options.get("kind", "armor")), str(options.get("filepath", "")), restore=action == "pack_restore")
        elif action == "red_key":
            from ...util.animation_edit import require_keyframe_edit

            controller = parameter_owner(root)
            require_keyframe_edit(controller, path("red"))
            previous_action = controller.animation_data.action if controller.animation_data else None
            controller.keyframe_insert(data_path=path("red"), frame=scene.frame_current, group="OXAL MC Studio · 红温")
            if previous_action is None and controller.animation_data and controller.animation_data.action:
                seal_owned(root, (controller.animation_data.action,))
        elif action in {"bind", "unbind"}:
            self._bind(scene, root, rig, options, unbind=action == "unbind")
        else:
            raise ValueError("未知人模操作")
        if action == "armor_enchant":
            return "已为所选盔甲添加附魔"
        if action == "armor_unenchant":
            return "已移除所选盔甲附魔"
        labels = {
            "outline_prepare": "已开启人模描边",
            "outline_remove": "已移除人模描边",
            "outline_repair": "已按源工程重建人模描边",
            "armor_outline_prepare": "已开启所选部位独立描边",
            "armor_outline_remove": "已移除所选部位独立描边",
            "armor_outline_repair": "已重建所选部位描边",
            "armor_enchant_outline": "已准备所选部位附魔描边，可启用并插入关键帧",
            "armor_enchant_pixels": "已准备所选部位像素格流光，可启用并插入关键帧",
            "armor_enchant_outline_enable": "已为所选部位添加并启用附魔描边",
            "armor_enchant_pixels_enable": "已为所选部位添加并启用像素格流光",
            "back_control": "已调整原生鞘翅开合",
            "back_control_select": "已定位背饰控制骨，可在姿态模式操作",
        }
        if action in labels:
            return labels[action]
        return "人模操作完成：" + action

    @staticmethod
    def _parameters(root: Any, rig: Any, values: object) -> None:
        ensure_mutation_allowed()
        _require_instance(root)
        require_owned(root, (rig, rig.data))
        if not isinstance(values, dict) or set(values) - set(PARAMETERS):
            raise ValueError("人模参数无效")
        normalized = {}
        for name, value in values.items():
            _, default, minimum, maximum = parameter_spec(name, root)
            if isinstance(default, tuple):
                if not isinstance(value, (tuple, list)) or len(value) != len(default):
                    raise ValueError("颜色参数格式错误")
                checked = [float(v) for v in value]
                if any(not math.isfinite(v) or not minimum <= v <= maximum for v in checked):
                    raise ValueError("颜色参数超出范围")
                normalized[name] = checked
            else:
                if not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
                    raise ValueError(f"参数超出范围：{name}")
                normalized[name] = bool(value) if isinstance(default, bool) else int(value) if isinstance(default, int) else float(value)
        controller = parameter_owner(root)
        from .human_parts import replace

        foot = normalized.get("foot_fixed")
        old_foot = bool(controller.get(key("foot_fixed")))
        foot_changed = foot is not None and old_foot != foot
        ankle_parents = {name: rig.data.bones[name].parent.name if rig.data.bones[name].parent else "" for name in ("L.脚腕", "R.脚腕")} if foot_changed else {}
        if foot_changed:
            # Preflight and apply this reversible change before part retirement;
            # an Action/NLA refusal must leave every other requested field intact.
            _set_ankles(rig, bool(foot))
        try:
            replace(root, rig, normalized, body_path(root), _prepare_controls)
        except Exception:
            if foot_changed:
                _set_ankles(rig, old_foot, restore_parents=ankle_parents)
            raise
        for name, value in normalized.items():
            controller[key(name)] = value
        controller.update_tag()
        rig.update_tag()

    @staticmethod
    def _skin(root: Any, filepath: str) -> None:
        ensure_mutation_allowed()
        _require_instance(root)
        source = _png(filepath, skin=True) if filepath else None
        materials = skin_materials(root)
        slots = [slot for obj in root.all_objects for slot in obj.material_slots if slot.material in materials]
        if not slots:
            raise ValueError("人模缺少主皮肤材质，未执行皮肤替换")
        from ...util.ownership import require_no_external_users

        require_owned(root, materials)
        require_no_external_users(cast(Any, bpy.context.scene), root, materials)
        require_owned(root, (modifier.node_group for obj in root.all_objects for modifier in obj.modifiers
                             if modifier.type == "NODES" and modifier.node_group
                             and (modifier.node_group.get("bp_second_layer_geometry")
                                  or modifier.node_group.get(HEAD_HD_MARKER))))
        from .human_skin_history import recent_paths, remember

        if source:
            recent_paths(root)
        from ..asset.asset_geometry import Creation
        from .human_layer_resolution import prepare as prepare_layer_resolution
        changed: list[tuple[Any, Any]] = []
        remembered = {name: root.get(name) for name in (
            "oxal_human_skin_label", "oxal_human_current_skin", "oxal_human_skin_pixels")}
        with Creation() as transaction:
            transaction.owner = root
            def restore():
                for target, previous in reversed(changed):
                    if isinstance(target, tuple):
                        set_input(target[0], target[1], previous)
                    else:
                        target.image = previous
                for name, value in remembered.items():
                    if value is None:
                        if name in root:
                            del root[name]
                    else:
                        root[name] = value
            transaction.on_rollback(restore)
            image = transaction.track(bpy.data.images.load(str(source), check_existing=False)) if source else root.get("oxal_human_default_skin")
            if not source and image is not None:
                require_owned(root, (image,))
            if not image or image.size[0] <= 0 or not image.has_data:
                raise ValueError("皮肤无法解码")
            for obj in tuple(root.all_objects):
                for slot in obj.material_slots:
                    material = slot.material
                    if material and material.node_tree and is_skin_material(material):
                        for node in skin_nodes(material):
                            changed.append((node, node.image))
                            node.image = image
                for modifier in obj.modifiers:
                    if (modifier.type == "NODES" and modifier.node_group
                            and (modifier.node_group.get("bp_second_layer_geometry")
                                 or modifier.node_group.get(HEAD_HD_MARKER))):
                        for socket in modifier.node_group.interface.items_tree:
                            if getattr(socket, "in_out", "") == "INPUT" and socket.name == "Image":
                                changed.append(((modifier, socket.identifier), get_input(modifier, socket.identifier)))
                                set_input(modifier, socket.identifier, image)
            if source:
                image[OWNER] = root[INSTANCE]
                image.pack()
                remember(transaction, root, source)
            prepare_layer_resolution(transaction, root, tuple(root.all_objects), image)
            root["oxal_human_current_skin"] = image
            root["oxal_human_skin_pixels"] = int(image.size[0])
            root["oxal_human_skin_label"] = source.name if source else "默认皮肤"
            from .human_outline import refresh as refresh_outline

            refresh_outline(transaction, root, tuple(root.all_objects), rebuild=True)
            transaction.commit(root)

    @staticmethod
    def _pack(root: Any, kind: str, filepath: str, *, restore: bool) -> None:
        ensure_mutation_allowed()
        _require_instance(root)
        if kind not in {"armor", "cape", "elytra"}:
            raise ValueError("未知材质包目标")
        targets = [o for o in root.all_objects if _armor_part(o)] if kind == "armor" else [o for o in root.all_objects if (kind == "elytra" and _matches(o, "New model")) or (kind == "cape" and (_matches(o, "披风·单段") or _matches(o, "披风·五段")))]
        if not targets:
            raise ValueError("请先启用对应背饰，再应用或恢复材质包")
        saved_prefix = "oxal_human_original_" + kind + "_"
        from ..outline.outline_native import base_slots

        bindings = [(obj, index, slot) for obj in targets for index, slot in base_slots(root, obj)
                    if slot.material and slot.material.node_tree]
        require_owned(root, (slot.material for _, _, slot in bindings))
        require_owned(root, (backup for obj, index, _ in bindings if (backup := obj.get(saved_prefix + str(index))) is not None))
        if kind != "armor":
            from ..asset.asset_geometry import Creation
            from .human_outline import refresh as refresh_outline
            from .human_packs import digest_from_file, prepare, prepare_png

            require_owned(root, (obj for obj in targets))
            changed = []
            def restore_bindings():
                for obj, index, previous, had_backup, backup in reversed(changed):
                    obj.material_slots[index].material = previous
                    saved = saved_prefix + str(index)
                    if had_backup:
                        obj[saved] = backup
                    elif saved in obj:
                        del obj[saved]
            with Creation() as transaction:
                transaction.owner = root
                transaction.on_rollback(restore_bindings)
                from .human_outline import suspend as suspend_outline

                suspend_outline(transaction, root, targets)
                if restore:
                    for obj, index, slot in bindings:
                        saved = saved_prefix + str(index)
                        backup = obj.get(saved)
                        if backup is not None:
                            changed.append((obj, index, slot.material, True, backup))
                            slot.material = backup
                            del obj[saved]
                else:
                    packed = Path(filepath).suffix.lower() in {".zip", ".json"}
                    if not packed:
                        from .human_back import validate_png

                        validate_png(filepath)
                    originals = ({slot.material for _, _, slot in bindings} if packed else
                                 {obj.get(saved_prefix + str(index), slot.material) for obj, index, slot in bindings})
                    if packed:
                        copies, created = prepare(originals, kind, digest_from_file(filepath), root[INSTANCE])
                    else:
                        copies, created = prepare_png(originals, _png(filepath), root[INSTANCE])
                    for item in created:
                        transaction.track(item)
                        if isinstance(item, bpy.types.Image) and (item.size[0] < 32 or item.size[0] != item.size[1] * 2):
                            raise ValueError("披风／鞘翅贴图须为2:1，宽度至少32像素")
                    for obj, index, slot in bindings:
                        saved = saved_prefix + str(index)
                        previous = slot.material
                        had_backup, backup = saved in obj, obj.get(saved)
                        changed.append((obj, index, previous, had_backup, backup))
                        original = obj.get(saved, previous)
                        obj[saved] = original
                        slot.material = copies[previous if packed else original]
                refresh_outline(transaction, root, targets, rebuild=True)
                transaction.commit(root)
            return
        if restore:
            from .human_armor import restore_pack

            restore_pack(root, bindings, saved_prefix)
            return
        from .human_packs import apply_armor

        apply_armor(root, bindings, filepath)

    @staticmethod
    def _bind(scene: Any, root: Any, rig: Any, options: Mapping[str, object], *, unbind: bool) -> None:
        ensure_mutation_allowed()
        _require_instance(root)
        require_owned(root, (rig, rig.data))
        names = options.get("objects", [])
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError("绑定对象快照无效")
        targets: list[Any] = [bpy.data.objects.get(n) for n in names]
        identifiers = options.get("object_uids")
        if identifiers is not None:
            if not isinstance(identifiers, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in identifiers):
                raise ValueError("绑定对象标识无效")
            objects = {o.session_uid: o for o in bpy.data.objects}
            targets = [objects.get(i) for i in identifiers]
        members = set(root.all_objects)
        scene_members = set(scene.objects)
        if not targets or any(o is None or o not in scene_members or o in members or o.library or o.override_library for o in targets):
            raise ValueError("请选择人模以外的可编辑物品")
        if len(set(targets)) != len(targets):
            raise ValueError("绑定请求含重复对象")
        bone = {"L": "L.手腕", "R": "R.手腕", "H": "头"}.get(str(options.get("wrist", "L")))
        if bone not in rig.pose.bones:
            raise ValueError("绑定目标骨骼不存在")
        ancestors = set()
        parent = rig.parent
        while parent is not None and parent not in ancestors:
            ancestors.add(parent)
            parent = parent.parent
        for obj in targets:
            if not unbind and (obj == rig or obj in ancestors or obj.constraints.get(BINDING)):
                raise ValueError("绑定可能形成循环或物品已有绑定，请先解除")
        if unbind:
            plan = []
            attrs = ("name", "target", "subtarget", "influence", "mute", "owner_space", "target_space", "use_location_x", "use_location_y", "use_location_z", "use_rotation_x", "use_rotation_y", "use_rotation_z", "use_scale_x", "use_scale_y", "use_scale_z")
            for obj in targets:
                for c in tuple(obj.constraints):
                    if c.name == BINDING and c.type == "CHILD_OF" and c.target == rig:
                        if _constraint_animated(obj, c):
                            raise ValueError("绑定约束含驱动或 Action/NLA 动画，请先处理该约束动画；未修改用户动画")
                        saved = {attr: getattr(c, attr) for attr in attrs}
                        saved["inverse_matrix"] = c.inverse_matrix.copy()
                        plan.append((obj, c, obj.matrix_world.copy(), saved))
            removed = []
            try:
                for obj, c, matrix, saved in plan:
                    obj.constraints.remove(c)
                    removed.append((obj, matrix, saved))
                    obj.matrix_world = matrix
            except Exception:
                for obj, matrix, saved in reversed(removed):
                    c = obj.constraints.new("CHILD_OF")
                    for attr, value in saved.items():
                        setattr(c, attr, value)
                    obj.matrix_world = matrix
                raise
            return
        created = []
        try:
            bone_world = rig.matrix_world @ rig.pose.bones[bone].matrix
            for obj in targets:
                constraint = obj.constraints.new("CHILD_OF")
                created.append((obj, constraint))
                constraint.name, constraint.target, constraint.subtarget = BINDING, rig, bone
                constraint.inverse_matrix = bone_world.inverted_safe()
        except Exception:
            for obj, constraint in reversed(created):
                obj.constraints.remove(constraint)
            raise

    @staticmethod
    def _remove(scene: Any, root: Any) -> None:
        ensure_mutation_allowed()
        _require_instance(root)
        proven = owned_ids(root)
        objects = {item for item in proven if isinstance(item, bpy.types.Object)}
        collections = {root, *(item for item in proven if isinstance(item, bpy.types.Collection))}
        require_owned(root, (*objects, *collections))
        if any(any(c not in collections for c in o.users_collection) for o in objects):
            raise ValueError("人模含外部共享对象，未删除数据")
        if any(child in collections for parent in bpy.data.collections if parent not in collections for child in parent.children if child != root):
            raise ValueError("人模子合集被外部共享，未删除数据")
        require_no_external_users(scene, root, proven,
                                  scene_properties={"oxal_mc_studio_active_human": root})
        rig = rig_of(root)
        if any(c.type == "CHILD_OF" and getattr(c, "target", None) == rig for o in bpy.data.objects if o not in objects for c in o.constraints):
            raise ValueError("请先解除人模上的外部物品绑定")
        was_active = scene.get("oxal_mc_studio_active_human") == root
        captured = release_owned(root)
        bpy.data.batch_remove(ids=list(objects | collections))
        if was_active and "oxal_mc_studio_active_human" in scene:
            del scene["oxal_mc_studio_active_human"]
        remove_unused_owned(captured)
