"""Independent inventory imports using the packed local.31 HUD asset."""

import ast
import json
import math
from collections.abc import Mapping
from pathlib import Path
from time import perf_counter
from typing import Any, cast
from uuid import uuid4

import bpy
from mathutils import Matrix, Vector

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import owns, release_owned, seal_owned
from ...util.resources import (
    ASSET_ID,
    INSTANCE,
    VERSION,
    AppendTransaction,
    ImportResult,
    resolve_scene,
    resolve_view_layer,
)

RESOURCE_ID = "oxal.inventory"
RESOURCE_VERSION = "V0.6.11-local.1"
ROOT_NAME = f"OXAL· 物品栏 {RESOURCE_VERSION}"
POLICY = "NEW_INSTANCE"
SCOPE = "instance"
_FRAME_LOCATIONS = (
    1.11346,
    0.838522,
    0.560514,
    0.281948,
    0.004498,
    -0.27284,
    -0.551034,
    -0.829228,
    -1.10835,
)


class InventoryTool:
    def __init__(self, asset_path: Path | None = None) -> None:
        self.asset_path = (
            asset_path
            or Path(__file__).resolve().parents[2]
            / "asset/inventory/oxal_inventory_standalone.blend"
        )

    def load(self, scene_uid: int, options: Mapping[str, object]) -> ImportResult:
        started = perf_counter()
        ensure_mutation_allowed()
        if set(options) - {"view_mode", "camera_uid", "cursor", "hands_enabled", "arm_mode", "skin_filepath", "creation_id"}:
            raise ValueError("物品栏导入不接受未知选项")
        mode = options.get("view_mode", "THIRD_PERSON")
        if mode not in {"FIRST_PERSON", "THIRD_PERSON"}:
            raise ValueError("请选择第一人称或第三人称")
        if bpy.context.mode != "OBJECT":
            raise ValueError("请在物体模式导入独立物品栏")
        scene = resolve_scene(scene_uid)
        identity = options.get("creation_id", uuid4().hex)
        if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("物品栏创建标识无效")
        if any(c.get(ASSET_ID) == RESOURCE_ID and c.get(INSTANCE) == identity for c in scene.collection.children_recursive):
            raise ValueError("物品栏创建标识已存在，未重复创建")
        layer = resolve_view_layer(scene, require_active=True)
        cursor = options.get("cursor", [scene.cursor.location[i] for i in range(3)])
        if not isinstance(cursor, (list, tuple)) or len(cursor) != 3 or any(not isinstance(v, (float, int)) or isinstance(v, bool) or not math.isfinite(v) for v in cursor):
            raise ValueError("物品栏位置必须为三个有限数值")
        camera_uid = str(options.get("camera_uid", ""))
        reference = None
        if mode == "FIRST_PERSON" and camera_uid:
            reference = next((o for o in scene.objects if str(o.session_uid) == camera_uid and o.type == "CAMERA"), None)
            if reference is None:
                raise ValueError("参考相机已删除或已切换场景")
            _validate_camera(reference)
        old_active = scene.get("oxal_mc_studio_active_inventory")
        try:
            with AppendTransaction(self.asset_path) as transaction:
                library_start = perf_counter()
                root = transaction.append_collection(ROOT_NAME)
                library_ms = (perf_counter() - library_start) * 1000
                rigs = [
                    o
                    for o in root.all_objects
                    if o.type == "ARMATURE"
                    and o.pose
                    and "物品栏控制器" in o.pose.bones
                ]
                if (
                    not root.get("oxal_inventory_asset")
                    or root.get("oxal_inventory_asset_version") != RESOURCE_VERSION
                    or len(rigs) != 1
                ):
                    raise ValueError("物品栏资源没有唯一的原生控制骨架")
                rig = rigs[0]
                prepare_start = perf_counter()
                root[ASSET_ID] = RESOURCE_ID
                root[VERSION] = RESOURCE_VERSION
                root[INSTANCE] = identity
                root.name = "OXAL · 独立物品栏"
                root["oxal_standalone_inventory_v1"] = True
                root["独立视角"] = mode
                for obsolete in ("oxal_inventory_asset", "oxal_inventory_control_schema"):
                    if obsolete in root:
                        del root[obsolete]
                rig.rotation_euler.z = -math.pi / 2
                fps = scene.render.fps / scene.render.fps_base
                rig["物品栏动画基准帧率"] = fps
                rig["物品栏动画基准起始帧"] = float(scene.frame_start)
                rig["物品栏动画计时源帧率"] = fps
                for obj in tuple(root.all_objects):
                    obj.visible_shadow = False
                if root.get("oxal_inventory_native_schema") != 2:
                    raise ValueError("请使用已清理的独立物品栏资源")
                rig["oxal_inventory_rig_id"] = identity + ":inventory-rig"
                from .inventory_hands import native_channels

                native_channels(rig)
                prepare_ms = (perf_counter() - prepare_start) * 1000
                scene.collection.children.link(root)
                scene["oxal_mc_studio_active_inventory"] = root
                for obj in tuple(root.all_objects):
                    if obj.type == "MESH":
                        cast(Any, obj.data).update()
                    obj.update_tag(refresh={"DATA"})
                rig.update_tag(refresh={"DATA"})
                layer.update()
                if mode == "FIRST_PERSON":
                    _camera_hud(root, rig, scene, reference, transaction)
                    from .inventory_hands import prepare

                    prepare(root, rig, transaction, enabled=bool(options.get("hands_enabled", True)), arm_mode=str(options.get("arm_mode", "SLIM")), skin_filepath=str(options.get("skin_filepath", "")))
                else:
                    for obj in tuple(root.all_objects):
                        if obj.parent is None:
                            obj.location += Vector(cursor)
                update_start = perf_counter()
                resolve_view_layer(scene).update()
                self._audit_native_drivers(transaction)
                update_ms = (perf_counter() - update_start) * 1000
                transaction.prune_unused()
                try:
                    seal_owned(root, transaction.owned)
                    transaction.commit()
                except Exception:
                    if owns(root, root):
                        release_owned(root)
                    raise
            return ImportResult(
                RESOURCE_ID,
                RESOURCE_VERSION,
                root[INSTANCE],
                root.name,
                rig.name,
                False,
                True,
                (perf_counter() - started) * 1000,
                library_ms,
                prepare_ms,
                update_ms,
            )
        except Exception:
            for key, value in (
                ("oxal_mc_studio_active_inventory", old_active),
            ):
                if value is None:
                    if key in scene:
                        del scene[key]
                else:
                    scene[key] = value
            raise

    @staticmethod
    def _prepare_frame_driver(rig, transaction: AppendTransaction) -> None:
        """Prepare the source's nine native frame positions without Python indexing."""
        ensure_mutation_allowed()
        if rig not in transaction.owned:
            raise ValueError("物品栏骨架不属于本次追加，未修改驱动")
        animation = rig.animation_data
        candidates = (
            [
                curve
                for curve in animation.drivers
                if curve.data_path == 'pose.bones["物品框"].location'
                and curve.array_index == 2
            ]
            if animation
            else []
        )
        if len(candidates) != 1:
            raise ValueError("物品栏资源缺少唯一的物品框位置驱动")
        driver = candidates[0].driver
        if driver.type != "SCRIPTED" or len(driver.variables) != 1:
            raise ValueError("物品栏位置驱动不符合资源契约")
        variable = driver.variables[0]
        target = variable.targets[0]
        if (
            variable.type != "TRANSFORMS"
            or target.id != rig
            or target.bone_target != "物品栏控制器"
            or target.transform_type != "LOC_Z"
        ):
            raise ValueError("物品栏位置驱动没有引用本次追加的控制骨骼")
        expression = str(_FRAME_LOCATIONS[-1])
        for index in reversed(range(len(_FRAME_LOCATIONS) - 1)):
            # Thresholds are exact binary fractions. At each half step, the
            # even index wins the tie; the outer branches also clamp the range.
            threshold = -(index + 0.5) * 2.25 / 8
            comparison = ">=" if index % 2 == 0 else ">"
            expression = (
                f"{_FRAME_LOCATIONS[index]} if v{comparison}{threshold} else "
                + expression
            )
        if len(expression) > 255:
            raise ValueError("物品栏原生位置驱动超出表达式长度限制")
        variable.name = "v"
        driver.expression = expression

    @staticmethod
    def _audit_native_drivers(transaction: AppendTransaction) -> None:
        """Check only source-proven IDs and their embedded node trees."""
        owners = {
            owner
            for item in transaction.owned
            for owner in (item, getattr(item, "node_tree", None))
            if owner is not None
        }
        for owner in owners:
            animation = getattr(owner, "animation_data", None)
            if animation is None:
                continue
            for curve in animation.drivers:
                driver = curve.driver
                if (
                    not curve.is_valid
                    or not driver.is_valid
                    or (driver.type == "SCRIPTED" and not driver.is_simple_expression)
                ):
                    raise ValueError(
                        f"物品栏资源含非原生或失效驱动：{owner.name} / {curve.data_path}"
                    )

    @staticmethod
    def _prepare_materials(root, rig) -> None:
        """Retain local.31's hardcore and half-armor import corrections."""
        materials = {
            slot.material
            for obj in root.all_objects
            for slot in obj.material_slots
            if slot.material
        }
        for material in materials:
            tree = cast(Any, material.node_tree)
            if tree is None:
                continue
            decoded = tree.nodes.get("控制值解码")
            integer = tree.nodes.get("控制值取整")
            if decoded and integer and decoded.type == "MATH" and integer.type == "MATH" and decoded.operation == "MULTIPLY" and integer.operation == "FLOOR" and integer.inputs[0].is_linked and integer.inputs[0].links[0].from_node == decoded:
                integer.operation = "ROUND"
            mode = any(
                node.type == "GROUP" and node.inputs.get("生存/极限")
                for node in tree.nodes
            )
            value = tree.nodes.get("值")
            if mode and value and value.outputs:
                curve = value.outputs[0].driver_add("default_value")
                driver = curve.driver
                driver.type = "SCRIPTED"
                driver.expression = "hardcore"
                for variable in tuple(driver.variables):
                    driver.variables.remove(variable)
                variable = driver.variables.new()
                variable.name = "hardcore"
                variable.type = "SINGLE_PROP"
                variable.targets[0].id = rig
                variable.targets[0].data_path = '["极限模式"]'
            ref = material.library_weak_reference
            if ref and ref.id_name == "MAarmorbar":
                threshold = tree.nodes.get("运算")
                if threshold and len(threshold.inputs) > 1:
                    threshold.inputs[1].default_value = 0.975
                animation = tree.animation_data
                if animation:
                    for curve in animation.drivers:
                        if curve.data_path == 'nodes["Math"].inputs[0].default_value':
                            curve.driver.expression = "20.0-location*20"


def _owners(transaction):
    return tuple({owner for item in transaction.owned for owner in (item, getattr(item, "node_tree", None)) if owner is not None})


def _driver(owner, path, expression, variables, index=None):
    curve = owner.driver_add(path) if index is None else owner.driver_add(path, index)
    driver = curve.driver
    driver.type = "SCRIPTED"
    for variable in tuple(driver.variables):
        driver.variables.remove(variable)
    used = {node.id for node in ast.walk(ast.parse(expression, mode="eval")) if isinstance(node, ast.Name)}
    for name, target_id, target_path in variables:
        if name not in used:
            continue
        variable = driver.variables.new()
        variable.name, variable.type = name, "SINGLE_PROP"
        variable.targets[0].id_type = target_id.id_type
        variable.targets[0].id = target_id
        variable.targets[0].data_path = target_path
    driver.expression = expression
    return curve


def _property(owner, key, value, low: float = 0.0, high: float = 100.0):
    owner[key] = value
    if not isinstance(value, bool):
        owner.id_properties_ui(key).update(min=low, max=high)


def _prepare_standalone(root, rig, tx):
    manual = {f'pose.bones[{json.dumps(n, ensure_ascii=False)}].location' for n in ("血条", "饱食度", "护甲值", "金血条", "氧气", "经验值")}
    if rig.animation_data:
        for curve in tuple(rig.animation_data.drivers):
            if curve.data_path in manual:
                rig.animation_data.drivers.remove(curve)
        rig.animation_data.action = None
        for track in tuple(rig.animation_data.nla_tracks):
            rig.animation_data.nla_tracks.remove(track)
    helpers = [o for o in root.all_objects if o.get("oxal_inventory_status_helper")]
    if len(helpers) != 1:
        raise ValueError("物品栏状态源契约不匹配")
    helper = helpers[0]
    dynamic = {f.data_path for f in helper.animation_data.drivers} if helper.animation_data else set()
    xp = {path for path in dynamic if path.startswith('["经验')}
    for path in xp:
        name = json.loads(path[1:-1])
        _property(rig.data, "独立_" + name, float(helper[name]), 0, 9999)
        source = next(f for f in helper.animation_data.drivers if f.data_path == path)
        variables = []
        for variable in source.driver.variables:
            if variable.type != "SINGLE_PROP" or len(variable.targets) != 1:
                raise ValueError("经验数字源通道不匹配")
            target = variable.targets[0]
            variables.append((variable.name, target.id, target.data_path))
        _driver(rig.data, '["独立_' + name + '"]', source.driver.expression, variables)
    for owner in _owners(tx):
        animation = getattr(owner, "animation_data", None)
        if animation is None or owner == helper:
            continue
        for curve in tuple(animation.drivers):
            replacements, discarded = {}, []
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id != helper:
                        continue
                    if variable.type != "SINGLE_PROP" or not target.data_path.startswith('["'):
                        raise ValueError("状态源有未知依赖，未删除")
                    if target.data_path in xp:
                        target.id_type = "ARMATURE"
                        target.id = rig.data
                        target.data_path = '["独立_' + json.loads(target.data_path[1:-1]) + '"]'
                    else:
                        replacements[variable.name] = 0.0 if target.data_path in dynamic else float(helper.path_resolve(target.data_path))
                        discarded.append(variable)
            if replacements:
                class Inline(ast.NodeTransformer):
                    def __init__(self, values):
                        self.values = values

                    def visit_Name(self, node):
                        if node.id in self.values:
                            return ast.copy_location(ast.Constant(self.values[node.id]), node)
                        return node
                parsed = Inline(replacements).visit(ast.parse(curve.driver.expression, mode="eval"))
                curve.driver.expression = ast.unparse(ast.fix_missing_locations(parsed))
                for variable in discarded:
                    curve.driver.variables.remove(variable)
    tx.remove((helper,))
    for owner in _owners(tx):
        animation = getattr(owner, "animation_data", None)
        if animation:
            for curve in tuple(animation.drivers):
                names = {n.id for n in ast.walk(ast.parse(curve.driver.expression or "0", mode="eval")) if isinstance(n, ast.Name)} if curve.driver.type == "SCRIPTED" else set()
                if "frame" in names:
                    if curve.data_path.startswith('["'):
                        owner[json.loads(curve.data_path[1:-1])] = 0.0
                    animation.drivers.remove(curve)
    referenced = {(target.id, target.data_path) for owner in _owners(tx) if getattr(owner, "animation_data", None) for curve in owner.animation_data.drivers for variable in curve.driver.variables for target in variable.targets}
    for name in tuple(rig.keys()):
        simulator = name.startswith(("自动", "启用受击", "启用回血", "生命回复", "低血量", "低饱食度", "饱食度常态", "受击闪烁", "回血闪烁")) or name in {"oxal_inventory_control_schema", "生命值", "盔甲值", "黄血值", "氧气值", "当前饱食度", "bp_inventory_frame_slot", "bp_oxal_first_person_view_distance_chunks", "bp_oxal_first_person_focal_length_mm", "发行类型", "来源候选", "第一人称模式", "望远镜倍率", "望远镜过渡帧", "望远镜缩放", "南瓜头遮罩", "攻击冷却", "攻击起始帧", "攻击冷却倍率", "经验进度", "oxal_combined_hand_rig_v1"}
        if simulator and (rig, '[' + json.dumps(name, ensure_ascii=False) + ']') not in referenced:
            del rig[name]
    for collection in rig.data.collections_all:
        collection.is_solo = False
    controls = rig.data.collections.get("状态栏控制器")
    if controls:
        controls.is_visible = True
        controls.is_solo = True
    slots = rig.data.collections.get("物品栏位置")
    if slots:
        slots.is_visible = False
    for name in ("血条", "饱食度", "护甲值", "金血条", "氧气", "经验值", "物品栏控制器", "物品框"):
        if name in rig.data.bones:
            rig.data.bones[name].hide = False
            rig.data.bones[name].hide_select = False
    rail = rig.data.bones.get("经验进度轨道")
    if rail:
        rail.hide_select = True
    rig.show_in_front = True
    _offhand(root, rig)
    _hotbar(root, rig, tx)
    root["独立物品栏版本"] = 1


def _offhand(root, rig):
    for obj in tuple(root.all_objects):
        if not obj.get("oxal_offhand_hud"):
            continue
        if obj.animation_data:
            for curve in tuple(obj.animation_data.drivers):
                if curve.data_path in {"hide_viewport", "hide_render"}:
                    obj.animation_data.drivers.remove(curve)
        obj.hide_viewport = False
        obj.hide_set(False)
        _driver(obj, "hide_render", "not enabled", [("enabled", rig, '["副手开关"]')])
        for axis in range(3):
            original = next((f for f in obj.animation_data.drivers if f.data_path == "scale" and f.array_index == axis), None)
            if original is not None:
                variable = original.driver.variables.new()
                variable.name, variable.type = "standalone_offhand", "SINGLE_PROP"
                variable.targets[0].id = rig
                variable.targets[0].data_path = '["副手开关"]'
                original.driver.expression = "(" + original.driver.expression + ")*standalone_offhand"
            else:
                _driver(obj, "scale", repr(float(obj.scale[axis])) + "*enabled", [("enabled", rig, '["副手开关"]')], axis)


def _hotbar(root, rig, tx):
    candidates = [o for o in root.all_objects if o.type == "MESH" and any(m and m.node_tree and any(n.type == "TEX_IMAGE" and n.image and str(n.image.get("oxal_minecraft_source26_4", "")).lower().endswith("/hotbar.png") for n in m.node_tree.nodes) for m in o.data.materials)]
    if len(candidates) != 1:
        raise ValueError("快捷栏贴图载体不唯一")
    bar = candidates[0]
    old, uv = bar.data, bar.data.uv_layers.active
    if uv is None or old.shape_keys:
        raise ValueError("快捷栏原生几何契约不匹配")
    front = []
    for face in old.polygons:
        coords = [uv.data[i].uv.copy() for i in face.loop_indices]
        area = sum(a.x*b.y-a.y*b.x for a,b in zip(coords, coords[1:] + coords[:1]))
        if abs(area) > 1e-6:
            front.append((face, coords))
    if len(front) != 1 or len(front[0][1]) != 4:
        raise ValueError("快捷栏源平面不唯一")
    face, coords = front[0]
    low = Vector((min(u.x for u in coords), min(u.y for u in coords)))
    high = Vector((max(u.x for u in coords), max(u.y for u in coords)))
    vertices = [old.vertices[old.loops[i].vertex_index] for i in face.loop_indices]
    def corner(x, y):
        value = low + Vector((x*(high.x-low.x), y*(high.y-low.y)))
        return vertices[min(range(4), key=lambda i: (coords[i]-value).length)].co.copy()
    origin, right, top = corner(0, 0), corner(1, 0), corner(0, 1)
    def point(u, v):
        return origin + (right-origin)*u + (top-origin)*v
    texture = Path(__file__).resolve().parents[2] / "asset/library/assets/minecraft/textures/gui/sprites/hud/hotbar.png"
    if not texture.is_file():
        raise FileNotFoundError("独立物品栏缺少原版快捷栏贴图")
    image = bpy.data.images.load(str(texture), check_existing=False)
    tx.track(image)
    image.pack()
    if tuple(image.size) != (182, 22):
        raise ValueError("原版快捷栏贴图尺寸不匹配")
    material = bpy.data.materials.new("独立物品栏 · 九格材质")
    tx.track(material)
    material.use_nodes = True
    material.surface_render_method = "BLENDED"
    tree = cast(Any, material.node_tree)
    tree.nodes.clear()
    tex = cast(Any, tree.nodes.new("ShaderNodeTexImage"))
    tex.image, tex.interpolation = image, "Closest"
    tex.extension = "CLIP"
    emission = tree.nodes.new("ShaderNodeEmission")
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    mix = tree.nodes.new("ShaderNodeMixShader")
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(tex.outputs["Color"], emission.inputs[0])
    tree.links.new(tex.outputs["Alpha"], mix.inputs[0])
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], output.inputs["Surface"])
    border, pitch = 1/182, 20/182
    bottom, upper = 1/22, 21/22
    rectangles = [(border+i*pitch, bottom, border+(i+1)*pitch, upper) for i in range(9)]
    rectangles += [(0,0,1,bottom), (0,upper,1,1), (0,bottom,border,upper), (1-border,bottom,1,upper)]
    points, faces, uvs = [], [], []
    for x0,y0,x1,y1 in rectangles:
        quad = ((x0,y0),(x1,y0),(x1,y1),(x0,y1))
        offset = len(points)
        points.extend(point(u,v) for u,v in quad)
        faces.append(tuple(range(offset,offset+4)))
        uvs.extend(quad)
    mesh = bpy.data.meshes.new("独立物品栏 · 原生九格")
    tx.track(mesh)
    mesh.from_pydata(points, [], faces)
    layer = mesh.uv_layers.new(name="UVMap")
    for item, value in zip(layer.data, uvs, strict=True):
        item.uv = value
    weights = [(g.group, g.weight) for g in vertices[0].groups]
    bar.data = mesh
    mesh.materials.append(material)
    for index, weight in weights:
        bar.vertex_groups[index].add(list(range(len(points))), weight, "REPLACE")
    bar["oxal_hotbar_cell_count1"] = 9
    root["oxal_hotbar_nine_slots1"] = 1
    _durability(root, rig, bar, point, weights, tx)


def _durability(root, rig, bar, point, weights, tx):
    from .inventory_native import control_key
    for slot in (*range(1, 10), "offhand"):
        index = slot if isinstance(slot, int) else 1
        enabled = control_key(rig,f"oxal_durability_{slot}_enabled")
        percent = control_key(rig,f"oxal_durability_{slot}_percent")
        full = control_key(rig,f"oxal_durability_{slot}_show_full")
        _property(rig, enabled, False)
        _property(rig, percent, 100.0)
        _property(rig, full, False)
        x0, x1 = (4+(index-1)*20)/182, (17+(index-1)*20)/182
        coords = [point(x0,3/22),point(x1,3/22),point(x1,4/22),point(x0,4/22)]
        normal = (coords[1]-coords[0]).cross(coords[3]-coords[0]).normalized()
        coords = [p+normal*.0005 for p in coords]
        if slot == "offhand":
            matrix = bar.matrix_parent_inverse @ bar.matrix_basis
            shift = rig.data.bones["物品栏副手"].head_local - rig.data.bones["1号物品栏"].head_local
            delta = matrix.to_3x3().inverted() @ shift
            coords = [p+delta for p in coords]
        mesh = bpy.data.meshes.new(f"独立耐久格{slot}")
        tx.track(mesh)
        mesh.from_pydata(coords, [], [(0,1,2,3)])
        obj = bar.copy()
        tx.track(obj)
        obj.data = mesh
        obj.animation_data_clear()
        obj.name = f"独立耐久格{slot}"
        if "oxal_hotbar_cell_count1" in obj:
            del obj["oxal_hotbar_cell_count1"]
        root.objects.link(obj)
        for index, weight in weights:
            obj.vertex_groups[index].add(list(range(4)),weight,"REPLACE")
        obj.shape_key_add(name="Basis")
        empty = obj.shape_key_add(name="剩余耐久")
        tx.track(mesh.shape_keys)
        empty.data[1].co = coords[0]
        empty.data[2].co = coords[3]
        _driver(empty, "value", "1-max(0,min(100,remaining))/100", [("remaining",rig,'["'+percent+'"]')])
        visibility = [("enabled",rig,'["'+enabled+'"]'),("remaining",rig,'["'+percent+'"]'),("full",rig,'["'+full+'"]')]
        if slot == "offhand":
            visibility.append(("offhand", rig, '["副手开关"]'))
            obj["oxal_offhand_hud"] = True
        for field in ("hide_viewport", "hide_render"):
            _driver(obj,field,"not enabled or (remaining>=100 and not full)" + (" or not offhand" if slot == "offhand" else ""),visibility)
        material = bpy.data.materials.new(f"独立耐久材质{slot}")
        tx.track(material)
        material.use_nodes = True
        tree = cast(Any, material.node_tree)
        tree.nodes.clear()
        color = tree.nodes.new("ShaderNodeEmission")
        output = tree.nodes.new("ShaderNodeOutputMaterial")
        tree.links.new(color.outputs[0],output.inputs[0])
        for axis, expression in enumerate(("min(1,2-remaining/50)","min(1,remaining/50)","0")):
            _driver(tree,color.inputs[0].path_from_id("default_value"),expression,[("remaining",rig,'["'+percent+'"]')],axis)
        mesh.materials.append(material)


def _validate_camera(camera):
    if camera.type != "CAMERA" or camera.data.type != "PERSP":
        raise ValueError("第一人称独立物品栏需要透视相机")
    _position, _rotation, scale = camera.matrix_world.decompose()
    if min(scale) <= 0 or max(scale)-min(scale) > 1e-5:
        raise ValueError("参考相机需要正向均匀缩放")
    basis = camera.matrix_world.to_3x3().normalized()
    if any(abs(basis.col[i].dot(basis.col[j])) > 1e-5 for i in range(3) for j in range(i)):
        raise ValueError("参考相机包含剪切，未修改")


def _camera_hud(root, rig, scene, reference, tx):
    data = reference.data.copy() if reference is not None else bpy.data.cameras.new("独立物品栏相机数据")
    tx.track(data)
    data.animation_data_clear()
    camera = bpy.data.objects.new("独立物品栏专用相机",data)
    tx.track(camera)
    root.objects.link(camera)
    if reference is not None:
        camera.matrix_world = reference.matrix_world.copy()
        constraint = camera.constraints.new("COPY_TRANSFORMS")
        constraint.name = "OXAL · 参考相机原生跟随"
        cast(Any, constraint).target = reference
        normalized = camera.constraints.new("LIMIT_SCALE")
        normalized.name = "OXAL · 相机尺度归一"
        normalized.owner_space = "WORLD"
        for axis in ("x", "y", "z"):
            setattr(normalized, "use_min_" + axis, True)
            setattr(normalized, "use_max_" + axis, True)
            setattr(normalized, "min_" + axis, 1.0)
            setattr(normalized, "max_" + axis, 1.0)
        for field in ("lens", "sensor_width", "sensor_height", "shift_x", "shift_y", "clip_start", "clip_end"):
            _driver(data, field, "value", [("value", reference.data, field)])
        root["独立物品栏参考相机"] = reference
    else:
        camera.location = (0, -6, 2)
        camera.rotation_euler = (math.pi/2, 0, 0)
        data.sensor_fit = "VERTICAL"
        data.sensor_height = 24
        data.lens = 24 / (2 * math.tan(math.radians(70) / 2))
        data.clip_start = .01
    _validate_camera(camera)
    # Measure the authored front plane in rig-local space after native binding.
    layer = resolve_view_layer(scene, require_active=True)
    layer.update()
    bar = next(o for o in root.all_objects if o.get("oxal_hotbar_cell_count1") == 9)
    graph = bpy.context.evaluated_depsgraph_get()
    evaluated = bar.evaluated_get(graph)
    points = [rig.matrix_world.inverted() @ evaluated.matrix_world @ v.co for v in evaluated.data.vertices]
    normal = (points[1]-points[0]).cross(points[3]-points[0]).normalized()
    # Authored hotbar horizontal direction follows its first UV edge.
    horizontal = (points[1]-points[0]).normalized()
    vertical = normal.cross(horizontal).normalized()
    local = Matrix((horizontal,vertical,normal)).transposed().to_4x4()
    conversion = local.inverted()
    projected = [conversion @ p for p in points]
    width = max(p.x for p in projected)-min(p.x for p in projected)
    center = (max(p.x for p in projected)+min(p.x for p in projected))/2
    bottom = min(p.y for p in projected)
    depth = sum(p.z for p in projected)/len(projected)
    if width < 1e-8:
        raise ValueError("快捷栏标定宽度无效")
    rig.parent = camera
    rig.matrix_parent_inverse = Matrix.Identity(4)
    rig.rotation_mode = "QUATERNION"
    rig.rotation_quaternion = conversion.to_quaternion()
    state = rig.data
    for name,value in (("HUD深度",1.0),("HUD宽度占比",.2854),("HUD底边距",.001472),("HUD有效深度",1.0),("投影宽度",1.0),("投影高度",1.0),("投影比例",1.0)):
        _property(state,name,value,.001,10000)
    _driver(state, '["HUD有效深度"]', "max(reference,near*2)", [("reference",state,'["HUD深度"]'),("near",data,"clip_start")])
    variables = [("depth",state,'["HUD有效深度"]'),("lens",data,"lens"),("sw",data,"sensor_width"),("sh",data,"sensor_height"),("rx",scene,"render.resolution_x"),("ry",scene,"render.resolution_y"),("px",scene,"render.pixel_aspect_x"),("py",scene,"render.pixel_aspect_y")]
    horizontal_fit = "1" if data.sensor_fit == "HORIZONTAL" else "0" if data.sensor_fit == "VERTICAL" else "rx*px>=ry*py"
    _driver(state,'["投影宽度"]',"depth/max(lens,.001)*(sw if "+horizontal_fit+" else sh*rx*px/max(ry*py,1))",variables)
    _driver(state,'["投影高度"]',"w*ry*py/max(rx*px,1)",[("w",state,'["投影宽度"]'),*variables])
    _driver(state,'["投影比例"]',"w*fraction/"+repr(width),[("w",state,'["投影宽度"]'),("fraction",state,'["HUD宽度占比"]')])
    placement=[("s",state,'["投影比例"]'),("w",state,'["投影宽度"]'),("h",state,'["投影高度"]'),("margin",state,'["HUD底边距"]'),("depth",state,'["HUD有效深度"]'),("sx",data,"shift_x"),("sy",data,"shift_y")]
    for axis in range(3):
        _driver(rig,"scale","s",placement,axis)
    _driver(rig,"location","-s*"+repr(center)+"+sx*w",placement,0)
    _driver(rig,"location","h*(margin-.5)-s*"+repr(bottom)+"+sy*h",placement,1)
    _driver(rig,"location","-depth-s*"+repr(depth),placement,2)
    root["独立物品栏相机"] = camera
    camera["oxal_standalone_inventory_camera"] = True
    layer.update()


def _xp_layout(root, rig):
    """Calibrate the native XP font against the authored bar's GUI-pixel plane."""
    layer = resolve_view_layer(cast(Any, bpy.context.scene), require_active=True)
    def reads(obj, value):
        animation = obj.animation_data
        return bool(animation and any(target.id == rig and target.data_path == '[' + json.dumps(value, ensure_ascii=False) + ']' for f in animation.drivers for v in f.driver.variables for target in v.targets))
    xp = [o for o in root.all_objects if o.type == "MESH" and reads(o, "经验等级")]
    counts = [o for o in root.all_objects if o.type == "MESH" and reads(o, "物品栏格1数量")]
    if len(xp) != 1 or len(counts) != 1:
        raise ValueError("独立物品栏数字通道不唯一")
    bar = next(o for o in root.all_objects if o.get("oxal_hotbar_cell_count1") == 9)
    graph = bpy.context.evaluated_depsgraph_get()
    evaluated = bar.evaluated_get(graph)
    coordinates = [evaluated.matrix_world @ v.co for v in evaluated.data.vertices]
    up = (coordinates[3]-coordinates[0]).normalized()
    unit = (coordinates[3]-coordinates[0]).length/20
    def bounds(obj):
        view = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        values = [(view.matrix_world @ v.co - coordinates[0]).dot(up) for v in view.data.vertices]
        return min(values), max(values)
    xp_low, xp_high = bounds(xp[0])
    count_low, count_high = bounds(counts[0])
    if min(xp_high-xp_low, count_high-count_low, unit) <= 1e-8:
        raise ValueError("独立物品栏数字几何为空")
    ratio = (count_high-count_low)/(xp_high-xp_low)
    rig["经验数字缩放"] *= ratio
    rig["经验数字间距"] *= ratio
    rig.update_tag(refresh={"DATA"})
    layer.update()
    coefficients = [0.0, 0.0, 0.0]
    for curve in xp[0].animation_data.drivers:
        if curve.data_path != "location":
            continue
        for variable in curve.driver.variables:
            if variable.name == "zfactor" and variable.type == "SINGLE_PROP":
                target = variable.targets[0]
                coefficients[curve.array_index] = float(target.id.path_resolve(target.data_path))
    view = xp[0].evaluated_get(bpy.context.evaluated_depsgraph_get())
    parent = view.matrix_world @ view.matrix_basis.inverted()
    sensitivity = (parent.to_3x3() @ Vector(coefficients)).dot(up)
    if abs(sensitivity) < 1e-8:
        raise ValueError("经验数字纵向控制无效")
    _, xp_top = bounds(xp[0])
    # Vanilla text top is 35 GUI pixels above the hotbar's outside bottom.
    # The first reconstructed cell starts one pixel above that outside bottom.
    rig["经验数字纵向偏移"] += (34*unit-xp_top)/sensitivity
    rig.update_tag(refresh={"DATA"})
    layer.update()
    root["oxal_xp_native_layout1"] = True
