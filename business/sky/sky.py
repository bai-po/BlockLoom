"""Scene-scoped sky operations with provenance and native animation.

asset_id=oxal.sky; version=4; policy=ACTIVATE_EXISTING; scope=current scene.
Source: immutable asset/sky/oxal_sky.blend and SOURCE.json.
"""

import ast
import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any, cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.geometry_nodes import get_input, set_input
from ...util.geometry_nodes import input_path as geometry_input_path
from ...util.ownership import (
    REGISTRY,
    owned_ids,
    owns,
    prune_owned,
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
    instance_creation_id,
    resolve_scene,
    resolve_view_layer,
)
from . import sky_inputs, sky_state
from .sky_schema import CLOUD_STRUCTURE_KEYS, PARAMETERS, RANGES, snapshot, validated

RESOURCE_ID = "oxal.sky"
RESOURCE_VERSION = "4"
SUPPORTED_RESOURCE_VERSIONS = {"2", "3", RESOURCE_VERSION}
POLICY = "ACTIVATE_EXISTING"
SCOPE = "scene"
logger = logging.getLogger(__name__)
COLLECTION_NAME = "OXAL天空 · V0.18.71"
WORLD_NAME = "OXAL天空 · World V0.18.71"
WORLD_REFERENCE = "oxal_mc_studio_sky_world"
PREVIOUS_WORLD = "oxal_mc_studio_sky_previous_world"
PREVIOUS_WORLD_EMPTY = "oxal_mc_studio_sky_previous_was_empty"
TEMPLATE_MESHES = ("OXAL_CloudTemplate_Low", "OXAL_CloudTemplate_High")
CLOUD_WINDOW_KEYS = ("cloud_distance", "cloud_shadow_distance")
CLOUD_TEMPLATES = "oxal_mc_studio_cloud_templates"
SAFE_FUNCTIONS = {
    "abs",
    "min",
    "max",
    "sin",
    "cos",
    "tan",
    "asin",
    "acos",
    "atan",
    "atan2",
    "sqrt",
    "floor",
    "fmod",
    "ceil",
    "round",
    "pow",
    "exp",
    "log",
    "int",
    "float",
}


class _NativeModulo(ast.NodeTransformer):
    """Keep signed wrapping inside Blender's native expression evaluator."""

    def visit_BinOp(self, node):
        node = cast(ast.BinOp, self.generic_visit(node))
        if not isinstance(node.op, ast.Mod):
            return node
        # '%' requires the Python evaluator in Blender 4.5. The nested fmod
        # preserves Python's signed modulo for frames before the scene start.
        return ast.Call(
            func=ast.Name(id="fmod", ctx=ast.Load()),
            args=[
                ast.BinOp(
                    left=ast.Call(
                        func=ast.Name(id="fmod", ctx=ast.Load()),
                        args=[node.left, node.right],
                        keywords=[],
                    ),
                    op=ast.Add(),
                    right=node.right,
                ),
                node.right,
            ],
            keywords=[],
        )


@lru_cache(maxsize=1024)
def _native_expression(expression):
    if "%" not in expression:
        return expression
    tree = _NativeModulo().visit(ast.parse(expression, mode="eval"))
    return ast.unparse(tree)


@dataclass(frozen=True, slots=True)
class SkyLoadResult:
    scene_name: str
    collection_name: str
    world_name: str
    controller_name: str
    reused: bool
    elapsed_ms: float
    append_ms: float
    update_ms: float


def root_for(scene):
    roots = [
        c
        for c in scene.collection.children_recursive
        if c.get(ASSET_ID) == RESOURCE_ID and c.get("oxal_mc_studio_sky_root")
    ]
    if len(roots) > 1:
        raise ValueError("当前场景存在多个新版天空，请先明确保留的实例")
    if not roots:
        return None
    root = roots[0]
    if root.library or root.override_library or root.get(VERSION) not in SUPPORTED_RESOURCE_VERSIONS:
        raise ValueError("天空实例只读或版本不兼容")
    owners = [
        s for s in bpy.data.scenes if root in tuple(s.collection.children_recursive)
    ]
    if len(owners) != 1:
        raise ValueError("天空实例被多个场景共享，未修改共享数据")
    require_owned(root, (root,))
    return root


def controller_for(root):
    candidates = [o for o in root.all_objects if o.get("oxal_sky_controller")]
    if len(candidates) != 1:
        raise ValueError("天空资源缺少唯一控制器")
    if candidates[0].library or candidates[0].override_library:
        raise ValueError("天空控制器只读或已作库覆盖，未改写绑定")
    return candidates[0]


def input_activation_required(scene):
    """Read cached driver state; never repair an already-open file in draw.

    Enabling after file parsing can leave RNA input lookups invalid. Only an
    explicit load/activate action is allowed to revalidate this owned instance.
    """
    root = root_for(scene)
    if root is None:
        return False
    controller = controller_for(root)
    require_owned(root, (controller,))
    if scene.world != root.get(WORLD_REFERENCE):
        return True
    if sky_inputs.legacy_animation(scene):
        return True
    if root.get(VERSION) == RESOURCE_VERSION:
        from .sky_linking import plan
        from .sky_modes import actual_structure

        try:
            if any(before != after for _role, _light, before, after in plan(root, controller, actual_structure(controller))):
                return True
        except ValueError:
            return True
    animation = controller.animation_data
    if animation is None:
        return True
    prefixes = (sky_state.VALUES + ".", sky_state.SLOTS + "[")
    if any(target.id == scene and target.data_path.startswith(prefixes)
           for item in animation.drivers for variable in item.driver.variables for target in variable.targets):
        return True
    return any(
        curve.driver is not None
        and not curve.driver.is_valid
        and any(
            target.id == scene and target.data_path.startswith(prefixes)
            for variable in curve.driver.variables
            for target in variable.targets
        )
        for curve in animation.drivers
    )


def _scene_input_bindings(scene, controller):
    """Validate only this beta's exact generated Scene-input bindings."""
    animation = controller.animation_data
    if animation is None:
        raise ValueError("天空缺少参数驱动，未接管或重建数据")
    aliases = sky_inputs.aliases()
    expected = {
        '["oxal_mc_studio_selected_slot"]': {
            "s": f"{sky_state.VALUES}.preset_slot",
            "e": f"{sky_state.VALUES}.preset_keyframes",
            **{
                f"f{i}": f"{sky_state.SLOTS}[{i}].filled"
                for i in range(sky_state.SLOT_COUNT)
            },
        }
    }
    for key, (_, _, native) in PARAMETERS.items():
        if key in CLOUD_STRUCTURE_KEYS and not (key == "cloud_light" and controller.get(VERSION) == "2"):
            continue
        expected[f"[{json.dumps(native, ensure_ascii=False)}]"] = {
            "base": f"{sky_state.VALUES}.{key}",
            **{
                f"v{i}": f"{sky_state.SLOTS}[{i}].values.{key}"
                for i in range(sky_state.SLOT_COUNT)
            },
        }
    targets, drivers = [], []
    curves = {curve.data_path: curve for curve in animation.drivers}
    for path, inputs in expected.items():
        curve = curves.get(path)
        driver = curve.driver if curve else None
        if (
            driver is None
            or driver.type != "SCRIPTED"
            or not driver.is_simple_expression
        ):
            raise ValueError("天空参数驱动缺失或已被改写，未重建数据")
        variables = {variable.name: variable for variable in driver.variables}
        for name, input_path in inputs.items():
            variable = variables.get(name)
            target = variable.targets[0] if variable else None
            if (
                variable is None
                or variable.type != "SINGLE_PROP"
                or target is None
                or target.id != scene
                or target.data_path not in {input_path, aliases[input_path]}
            ):
                raise ValueError("天空参数目标已被改写，未接管其它绑定")
            targets.append((target, aliases[input_path]))
        drivers.append(driver)
    return targets, drivers


def _dependency_domain(root):
    """IDs reached from this sky, excluding unrelated copies with copied tags."""
    instance = root[INSTANCE]
    domain = {root, *owned_ids(root)}
    world = root.get(WORLD_REFERENCE)
    if isinstance(world, bpy.types.World):
        domain.add(world)
    visited = set()
    while pending := domain - visited:
        for item in pending:
            visited.add(item)
            tree = getattr(item, "node_tree", None)
            if tree is not None:
                domain.add(tree)
            for attribute in ("data", "shape_keys"):
                linked = getattr(item, attribute, None)
                if linked is not None and linked.get(INSTANCE) == instance:
                    domain.add(linked)
            for material in getattr(item, "materials", ()):
                if material is not None and material.get(INSTANCE) == instance:
                    domain.add(material)
            for node in getattr(item, "nodes", ()):
                linked = getattr(node, "node_tree", None)
                if linked is not None and linked.get(INSTANCE) == instance:
                    domain.add(linked)
    return domain


def _driver(owner, path, expression, variables, index=None):
    expression = _native_expression(expression)
    if len(expression.encode("utf8")) > 255:
        raise ValueError("原生驱动表达式超出长度限制")
    try:
        if index is None:
            owner.driver_remove(path)
        else:
            owner.driver_remove(path, index)
    except (TypeError, RuntimeError):
        pass
    curve = owner.driver_add(path) if index is None else owner.driver_add(path, index)
    driver = curve.driver
    driver.type = "SCRIPTED"
    while driver.variables:
        driver.variables.remove(driver.variables[0])
    for name, target, data_path in variables:
        variable = driver.variables.new()
        variable.name = name
        variable.type = "SINGLE_PROP"
        variable.targets[0].id_type = target.id_type
        variable.targets[0].id = target
        variable.targets[0].data_path = data_path
    driver.expression = expression
    return curve


def _bind_parameters(scene, controller):
    controller["oxal_mc_studio_scene_uid"] = scene.session_uid
    controller["oxal_mc_studio_selected_slot"] = 0
    slot_variables = [
        ("s", scene, sky_inputs.path("preset_slot")),
        ("e", scene, sky_inputs.path("preset_keyframes")),
    ]
    slot_variables.extend(
        (f"f{i}", scene, sky_inputs.path("filled",i)) for i in range(10)
    )
    filled = " or ".join(f"(s=={i + 1} and f{i})" for i in range(10))
    _driver(
        controller,
        '["oxal_mc_studio_selected_slot"]',
        f"s if e and ({filled}) else 0",
        slot_variables,
    )
    for key, (_caption, default, native) in PARAMETERS.items():
        controller[native] = 0 if isinstance(default, str) else default
        if key in CLOUD_STRUCTURE_KEYS:
            controller[native] = getattr(getattr(scene, sky_state.VALUES), key)
            continue
        variables = [
            ("base", scene, sky_inputs.path(key)),
            ("s", controller, '["oxal_mc_studio_selected_slot"]'),
        ]
        expression = "base"
        for i in reversed(range(sky_state.SLOT_COUNT)):
            variables.extend(
                ((f"v{i}", scene, sky_inputs.path(key,i)),)
            )
            expression = f"v{i} if s=={i + 1} else {expression}"
        if key == "cloud_light_auto":
            variables.append(("automatic", controller, '["自动时间"]'))
            expression = f"automatic or ({expression})"
        _driver(
            controller,
            f"[{json.dumps(native, ensure_ascii=False)}]",
            expression,
            variables,
        )
    _set_cloud_window_ranges(controller)
    _driver(
        controller,
        '["云接力原始位移"]',
        "offset + moving*(frame-start)/max(rate,1)*speed",
        [
            ("offset", controller, '["云手动X位移"]'),
            ("moving", controller, '["云移动启用"]'),
            ("rate", controller, '["每小时帧数"]'),
            ("speed", controller, '["云移动速度"]'),
            ("start", scene, "frame_start"),
        ],
    )
    _driver(
        controller,
        '["计算时间"]',
        "(start+(frame-first)/max(rate,1))%24 if auto else time%24",
        [
            ("start", controller, '["起始时间"]'),
            ("first", scene, "frame_start"),
            ("rate", controller, '["每小时帧数"]'),
            ("auto", controller, '["自动时间"]'),
            ("time", controller, '["时间"]'),
        ],
    )
    controller["云光照阴影"] = False
    _driver(
        controller,
        '["云光照阴影"]',
        "advanced if auto else manual",
        [
            ("advanced", controller, '["Eevee高级云光照"]'),
            ("auto", controller, '["云光照阴影自动控制"]'),
            ("manual", controller, '["oxal_mc_studio_cloud_shadow_manual"]'),
        ],
    )


def _set_cloud_window_ranges(controller):
    for key in CLOUD_WINDOW_KEYS:
        caption, _default, native = PARAMETERS[key]
        minimum, maximum = RANGES[key]
        controller.id_properties_ui(native).update(
            min=int(minimum),
            max=int(maximum),
            soft_min=int(minimum),
            soft_max=int(maximum),
            description=caption,
        )


def cloud_structure_snapshot(values):
    """Small click-time snapshot; structural parameters never animate implicitly."""
    return {name: getattr(values, name) for name in CLOUD_WINDOW_KEYS}


def slot_request(scene, action):
    """Freeze the chosen slot, active values and key time without RNA writes."""
    values = getattr(scene, sky_state.VALUES)
    index = values.preset_slot
    if not 1 <= index <= sky_state.SLOT_COUNT:
        raise ValueError("天空保存值编号无效")
    result: dict[str, Any] = {"slot_index": index, "slot_keyframes": bool(values.preset_keyframes)}
    if action == "save_slot":
        result["parameters"] = validated(sky_state.effective_snapshot(scene))
    elif action == "apply_slot":
        slots = getattr(scene, sky_state.SLOTS)
        if len(slots) < index or not slots[index - 1].filled:
            raise ValueError("当前天空保存值为空")
        result["parameters"] = validated(snapshot(slots[index - 1].values))
    elif action == "key_slot":
        result["key_frame"] = float(scene.frame_current_final)
        result["slot_keyframes"] = True
    elif action != "clear_slot":
        raise ValueError("未知天空保存值操作")
    return result


def _require_cloud_templates(root):
    descriptions = root.get(CLOUD_TEMPLATES)
    if descriptions is None or set(descriptions) != {"low", "high"}:
        raise ValueError("天空云资源合同已损坏")
    members = []
    collections = set(root.children_recursive)
    for description in descriptions.values():
        mesh, collection = description.get("mesh"), description.get("collection")
        if not isinstance(mesh, bpy.types.Mesh) or collection not in collections:
            raise ValueError("云模板引用已变化，未接管其它数据")
        members.extend((mesh, collection))
        for modifier in description["modifiers"]:
            group = modifier.get("group")
            if not isinstance(group, bpy.types.NodeTree) or group.bl_idname != "GeometryNodeTree":
                raise ValueError("云模板节点合同已损坏")
            members.append(group)
            members.extend(value for value in modifier["inputs"].values() if isinstance(value, bpy.types.ID))
    require_owned(root, members)


def _cloud_window_updates(root, controller):
    # New instances use explicit physical cloud layouts, not a dormant pool.
    require_owned(root, (controller,))
    _require_cloud_templates(root)
    return []


def _cloud_shadow_material(transaction):
    """A camera-transparent native caster keeps visible clouds alpha blended."""
    material = bpy.data.materials.new("OXAL · 云阴影投射（不可见）")
    transaction.track(material)
    material.use_nodes = True
    material.surface_render_method = "DITHERED"
    tree = material.node_tree
    assert tree is not None
    tree.nodes.clear()
    path = tree.nodes.new("ShaderNodeLightPath")
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    solid = tree.nodes.new("ShaderNodeBsdfDiffuse")
    mix = tree.nodes.new("ShaderNodeMixShader")
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(path.outputs["Is Shadow Ray"], mix.inputs[0])
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(solid.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], output.inputs["Surface"])
    material["oxal_mc_studio_cloud_shadow_only"] = True
    from ...util.shader_layout import arrange_owned_shaders

    arrange_owned_shaders((material,))
    return material


def _prepare_cloud_sampling(group, transaction):
    """Sample the moving infinite texture continuously when finite tiles wrap."""
    if group not in transaction.owned:
        group = group.copy()
        transaction.track(group)
    socket_id = group.get("oxal_mc_studio_cloud_sample_socket")
    if socket_id:
        return group, socket_id
    textures = [node for node in group.nodes if node.type == "IMAGE_TEXTURE"]
    inputs = [node for node in group.nodes if node.type == "GROUP_INPUT"]
    if len(textures) != 1 or len(inputs) != 1:
        raise ValueError("云节点采样合同不明确，未替换模板")
    interface = group.interface
    if interface is None:
        raise ValueError("云模板缺少原生输入接口")
    socket = interface.new_socket(name="OXAL Cloud Sample Offset", in_out="INPUT", socket_type="NodeSocketVector")
    source = next(output for output in inputs[0].outputs if output.identifier == socket.identifier)
    vector = textures[0].inputs["Vector"]
    if vector.is_linked:
        position = vector.links[0].from_socket
    else:
        node = group.nodes.new("GeometryNodeInputPosition")
        node.name = "OXAL · 云采样坐标"
        position = node.outputs["Position"]
    offset = group.nodes.new("ShaderNodeVectorMath")
    offset.name, offset.operation = "OXAL · 云无缝接力采样", "ADD"
    group.links.new(position, offset.inputs[0])
    group.links.new(source, offset.inputs[1])
    group.links.new(offset.outputs["Vector"], vector)
    group["oxal_mc_studio_cloud_sample_socket"] = socket.identifier
    return group, socket.identifier


def _capture_cloud_templates(root, templates, transaction):
    """Retain data and modifier contracts, never an unused hidden cloud Object."""
    existing = tuple(o for o in root.all_objects if o.get("oxal_cloud_tile"))
    descriptions = {}
    prepared_groups = {}
    for layer, mesh_name in (("low", TEMPLATE_MESHES[0]), ("high", TEMPLATE_MESHES[1])):
        source = next((o for o in existing if o.get("oxal_cloud_layer") == layer), None)
        if source is None:
            raise ValueError("天空资源缺少云层模板")
        modifiers = []
        for modifier in source.modifiers:
            if modifier.type != "NODES" or modifier.node_group is None:
                raise ValueError("云模板含有未声明的修改器，未导入")
            original = modifier.node_group
            if original not in prepared_groups:
                prepared_groups[original] = _prepare_cloud_sampling(original, transaction)
            group, sampling = prepared_groups[original]
            inputs = {sampling: (0.0, 0.0, 0.0)}
            for socket in original.interface.items_tree:
                if (socket.item_type == "SOCKET" and socket.in_out == "INPUT"
                        and socket.bl_rna.properties.get("default_value") is not None
                        and socket.identifier != sampling):
                    inputs[socket.identifier] = get_input(modifier, socket.identifier)
            modifiers.append({"group": group, "inputs": inputs, "sampling": sampling})
        descriptions[layer] = {
            "mesh": templates[mesh_name], "collection": source.users_collection[0],
            "location": list(source.location), "scale": list(source.scale),
            "modifiers": modifiers,
        }
    root[CLOUD_TEMPLATES] = descriptions
    return existing


def _cloud_mesh(transaction, template, layer, ix, iy, material=None):
    x, y, e = -1 + ix / 4, -1 + iy / 4, 0.0000007
    mesh = bpy.data.meshes.new(f"OXAL 云 {layer} {ix}-{iy}")
    transaction.track(mesh)
    mesh.from_pydata([
        (x, y, 0), (x + .25, y, 0), (x + .25, y + .25, 0), (x, y + .25, 0),
        (x, y, -e), (x + .25, y, -e), (x + .25, y + .25, e), (x, y + .25, e),
    ], [], [(0, 1, 2, 3), (7, 6, 5, 4)])
    for source_material in template.materials:
        mesh.materials.append(material or source_material)
    uv = mesh.uv_layers.new(name="UVMap")
    for face in mesh.polygons:
        for loop in face.loop_indices:
            co = mesh.vertices[mesh.loops[loop].vertex_index].co
            uv.data[loop].uv = ((co.x + 1) / 2, (co.y + 1) / 2)
    return mesh


def _cloud_domains(settings):
    if not settings["cloud_enabled"]:
        return {}
    result = {"view": int(settings["cloud_distance"])}
    if settings["cloud_shadow"] or (settings["cloud_advanced"] if settings["cloud_shadow_auto"] else settings["cloud_light_shadow"]):
        result["shadow"] = int(settings["cloud_shadow_distance"])
    return result


def _periodic_cloud_sampling(description, saved, size):
    """Whole REPEAT periods need no animated geometry input.

    Relay displacement is an integer multiple of the window width. At N=4/8
    the authored scale makes every resulting UV offset a whole image period;
    retaining its time-dependent driver only invalidates identical GN output.
    Other sizes and edited sampling graphs keep their continuous sampler.
    """
    scale = float(description["scale"][0])
    periods = size * 800 / scale if scale else 0
    if periods < 1 or abs(periods - round(periods)) > 1e-9:
        return False
    group = saved["group"]
    textures = [node for node in group.nodes if node.type == "IMAGE_TEXTURE"]
    if len(textures) != 1 or textures[0].extension != "REPEAT":
        return False
    links = tuple(textures[0].inputs["Vector"].links)
    if len(links) != 1:
        return False
    offset = links[0].from_node
    if offset.type != "VECT_MATH" or offset.operation != "ADD" or len(links[0].from_socket.links) != 1:
        return False
    inputs = tuple(offset.inputs[1].links)
    return (len(inputs) == 1 and inputs[0].from_node.type == "GROUP_INPUT"
            and inputs[0].from_socket.identifier == saved["sampling"]
            and len(inputs[0].from_socket.links) == 1)


def _periodic_sampling_updates(root, controller, *, excluded=()):
    """Read-only recognition of the exact previous Beta's redundant drivers."""
    from .sky_repair import _no_animation, curve, matches

    if root.get(VERSION) != RESOURCE_VERSION:
        return []
    settings = json.loads(root["oxal_mc_studio_cloud_structure"])
    domains = _cloud_domains(settings)
    result = []
    for obj in tuple(root.all_objects):
        if not obj.get("oxal_mc_studio_cloud_tile") or obj in excluded:
            continue
        layer, domain = obj["oxal_cloud_layer"], obj["oxal_mc_studio_cloud_domain"]
        description = root[CLOUD_TEMPLATES][layer]
        size = domains.get(domain)
        if size is None:
            raise ValueError("现有云的采样域已改变，未优化用户数据")
        cx = (obj["oxal_mc_studio_cloud_ix"] - 3.5) * 800
        rate = ("(1.5 if mode==1 else .75 if mode==2 else 1)" if layer == "high"
                else "(.75 if mode==1 else 1.5 if mode==2 else 1)")
        saved_modifiers = description["modifiers"]
        if len(obj.modifiers) != len(saved_modifiers):
            continue
        for modifier, saved in zip(obj.modifiers, saved_modifiers, strict=True):
            if modifier.type != "NODES" or modifier.node_group != saved["group"] or not _periodic_cloud_sampling(description, saved, size):
                continue
            path = geometry_input_path(cast(bpy.types.NodesModifier, modifier), saved["sampling"])
            item = curve(obj, path, 0)
            if item is None:
                continue
            expression = f"(x-({cx})-phase*{rate})/{description['scale'][0]}"
            variables = [("x", obj, '["oxal_mc_studio_cloud_x"]'),
                         ("phase", controller, '["云接力原始位移"]'),
                         ("mode", controller, '["云移动速度模式"]')]
            _no_animation(obj, {path})
            mapping = tuple(item.keyframe_points)
            identity = not mapping or (len(mapping) == 2 and item.extrapolation == "LINEAR" and all(
                point.interpolation == "BEZIER" and point.easing == "AUTO" and point.type == "KEYFRAME"
                and point.handle_left_type == point.handle_right_type == "AUTO_CLAMPED"
                and max(abs(float(value) - expected) for vector, expected in (
                    (point.co, float(index)), (point.handle_left, index - 1/3),
                    (point.handle_right, index + 1/3)) for value in vector) < 1e-6
                for index, point in enumerate(mapping)))
            if (not matches(obj, path, expression, variables, 0) or not identity or item.sampled_points
                    or any(target.use_fallback_value for variable in item.driver.variables for target in variable.targets)):
                raise ValueError("云接力采样驱动含用户修改，未覆盖：" + obj.name)
            require_owned(root, (obj, modifier.node_group))
            result.append((obj, modifier, saved["sampling"], path))
    if result:
        targets = {obj for obj, _modifier, _socket, _path in result}
        users = bpy.data.user_map(subset=tuple(targets))
        for obj, references in users.items():
            for user in {*references, obj}:
                for owner in (user, getattr(user, "node_tree", None)):
                    animation = getattr(owner, "animation_data", None)
                    for driven in animation.drivers if animation else ():
                        if any(target.id == obj and target.data_path.startswith("modifiers[")
                               for variable in driven.driver.variables for target in variable.targets):
                            raise ValueError("其它驱动仍读取云修改器数值，保留原采样链，未优化：" + obj.name)
    return result


def _simplify_periodic_sampling(updates):
    """Apply a preflighted native reduction, returning its local undo journal."""
    from ...util.drivers import capture, restore

    records = {obj: capture(obj) for obj, _modifier, _socket, _path in updates}
    values = [(modifier, socket, tuple(get_input(modifier, socket))) for _obj, modifier, socket, _path in updates]
    def rollback():
        for modifier, socket, value in values:
            set_input(modifier, socket, value)
        for obj, record in records.items():
            restore(obj, record)
            obj.update_tag()
    try:
        for obj, modifier, socket, path in updates:
            obj.driver_remove(path, 0)
            set_input(modifier, socket, (0.0, 0.0, 0.0))
            obj.update_tag()
    except Exception:
        rollback()
        raise
    return rollback


def _create_cloud_tiles(root, controller, settings, transaction, changed_domains=None):
    descriptions = root.get(CLOUD_TEMPLATES)
    if descriptions is None:
        raise ValueError("天空云资源合同已损坏")
    if not settings["cloud_enabled"]:
        return []
    needed = _cloud_domains(settings)
    if changed_domains is not None:
        needed = {domain: size for domain, size in needed.items() if domain in changed_domains}
    shadow_material = _cloud_shadow_material(transaction) if "shadow" in needed else None
    created = []
    domains: list[tuple[str, int, Any]] = [(domain, size, shadow_material if domain == "shadow" else None)
                                        for domain, size in needed.items()]
    for layer in ("low", "high"):
        description = descriptions[layer]
        template = description["mesh"]
        for domain, size, material in domains:
            first = (8 - size) // 2
            low = -size * 400
            width = size * 800
            for ix in range(first, first + size):
                for iy in range(first, first + size):
                    mesh = _cloud_mesh(transaction, template, layer, ix, iy, material)
                    obj = bpy.data.objects.new(f"OXAL 云 {layer} {domain} {ix + 1}-{iy + 1}", mesh)
                    transaction.track(obj)
                    obj.location = description["location"]
                    obj.scale = description["scale"]
                    obj["oxal_mc_studio_cloud_tile"] = True
                    obj["oxal_cloud_layer"] = layer
                    obj["oxal_mc_studio_cloud_domain"] = domain
                    obj["oxal_mc_studio_cloud_ix"], obj["oxal_mc_studio_cloud_iy"] = ix, iy
                    obj.visible_camera = domain == "view"
                    obj.visible_shadow = domain == "shadow"
                    obj.visible_diffuse = obj.visible_glossy = domain == "view"
                    obj.visible_transmission = obj.visible_volume_scatter = domain == "view"
                    for number, saved in enumerate(description["modifiers"]):
                        modifier = cast(bpy.types.NodesModifier, obj.modifiers.new(f"OXAL Cloud Geometry {number + 1}", "NODES"))
                        modifier.node_group = saved["group"]
                        for identifier, value in saved["inputs"].items():
                            set_input(modifier, identifier, value)
                    cx = (ix - 3.5) * 800
                    rate = ("(1.5 if mode==1 else .75 if mode==2 else 1)" if layer == "high"
                            else "(.75 if mode==1 else 1.5 if mode==2 else 1)")
                    obj["oxal_mc_studio_cloud_x"] = cx
                    _driver(obj, '["oxal_mc_studio_cloud_x"]',
                            f"(({cx}+phase*{rate}-({low}))%{width}+({low}))",
                            [("phase", controller, '["云接力原始位移"]'),
                             ("mode", controller, '["云移动速度模式"]')])
                    # A wrapped tile enters a different region of the source
                    # texture. Offset its sampling by whole relay widths; using
                    # local coordinates alone creates seams for N != 4 or 8.
                    for modifier, saved in zip(obj.modifiers, description["modifiers"], strict=True):
                        if _periodic_cloud_sampling(description, saved, size):
                            continue
                        _driver(obj, geometry_input_path(cast(bpy.types.NodesModifier, modifier), saved["sampling"]),
                                f"(x-({cx})-phase*{rate})/{description['scale'][0]}",
                                [("x", obj, '["oxal_mc_studio_cloud_x"]'),
                                 ("phase", controller, '["云接力原始位移"]'),
                                 ("mode", controller, '["云移动速度模式"]')], 0)
                    delta = f"(x-({cx}))"
                    variables = [("x", obj, '["oxal_mc_studio_cloud_x"]'),
                                 ("angle", controller, '["云旋转角度"]')]
                    bx, by = description["location"][:2]
                    _driver(obj, "location", f"{bx}+{delta}*cos(angle*pi/180)", variables, 0)
                    _driver(obj, "location", f"{by}+{delta}*sin(angle*pi/180)", variables, 1)
                    _driver(obj, "location", f"height+{106 if layer == 'low' else 300}",
                            [("height", controller, '["云高度"]')], 2)
                    _driver(obj, "rotation_euler", "angle*pi/180", variables[1:], 2)
                    created.append((obj, description["collection"]))
    return created


def _cloud_pool(root, controller, templates, transaction):
    """Initial explicit import only creates enabled view and shadow tiles."""
    existing = _capture_cloud_templates(root, templates, transaction)
    scene = resolve_scene(int(controller["oxal_mc_studio_scene_uid"]))
    settings = {name: getattr(getattr(scene, sky_state.VALUES), name) for name in CLOUD_STRUCTURE_KEYS}
    created = _create_cloud_tiles(root, controller, settings, transaction)
    for obj, collection in created:
        collection.objects.link(obj)
    transaction.remove(existing)
    root["oxal_mc_studio_cloud_structure"] = json.dumps(settings)


class _CloudCreation:
    """Track only IDs explicitly created by one structure command."""
    def __init__(self):
        self.owned = set()

    def track(self, item):
        self.owned.add(item)

    def rollback(self):
        if self.owned:
            bpy.data.batch_remove(ids=list(self.owned))
            self.owned.clear()


def _apply_cloud_structure(scene, options):
    from .sky_modes import actual_structure

    root = root_for(scene)
    if root is None:
        raise ValueError("请先加载天空")
    if root.get(VERSION) != RESOURCE_VERSION:
        raise ValueError("请先修复已有 Beta 天空，再应用云范围")
    submitted = options.get("structure")
    if not isinstance(submitted, dict) or set(submitted) != set(CLOUD_WINDOW_KEYS):
        raise ValueError("云范围请求只接受可见范围和阴影范围")
    settings = actual_structure(controller_for(root))
    settings.update(submitted)
    return replace_cloud_structure(scene, settings)


def cloud_structure_plan(scene, root, controller, settings):
    """Read-only domain/ownership validation, also used before a cache dialog."""
    if root.get(VERSION) != RESOURCE_VERSION:
        raise ValueError("请先修复已有 Beta 天空，再修改云结构")
    _require_cloud_templates(root)
    complete = snapshot(sky_state.effective_values(scene))
    complete.update(settings)
    settings = {name: value for name, value in validated(complete).items() if name in CLOUD_STRUCTURE_KEYS}
    from .sky_linking import plan as lighting_plan
    from .sky_modes import normalize_modes
    from .sky_repair import _no_animation, curve

    settings = normalize_modes(settings)
    lighting_plan(root, controller, settings)
    _no_animation(controller, {f'[{json.dumps(PARAMETERS[name][2], ensure_ascii=False)}]' for name in CLOUD_STRUCTURE_KEYS})
    for name in CLOUD_STRUCTURE_KEYS:
        native = PARAMETERS[name][2]
        if curve(controller, f'[{json.dumps(native, ensure_ascii=False)}]') is not None:
            raise ValueError("云结构输入已有用户驱动，未覆盖：" + native)
    previous_settings = json.loads(root["oxal_mc_studio_cloud_structure"])
    before, after = _cloud_domains(previous_settings), _cloud_domains(settings)
    changed_domains = {domain for domain in {*before, *after} if before.get(domain) != after.get(domain)}
    members = tuple(o for o in root.all_objects if o.get("oxal_mc_studio_cloud_tile"))
    require_owned(root, (controller, *members))
    if any(item.library or item.override_library for item in members):
        raise ValueError("现有云含链接或覆盖对象，未替换用户数据")
    for domain in ("view", "shadow"):
        if len([obj for obj in members if obj.get("oxal_mc_studio_cloud_domain") == domain]) != 2 * before.get(domain, 0) ** 2:
            raise ValueError("现有云对象数量与已应用结构不符，未重建用户数据")
    # Lighting mode changes keep unchanged visible/caster objects, preserving
    # user Actions, materials and constraints on those existing domains.
    old = tuple(o for o in members if o.get("oxal_mc_studio_cloud_domain") in changed_domains)
    if old:
        require_no_external_users(scene, root, old)
    _periodic_sampling_updates(root, controller, excluded=old)
    return settings, old, changed_domains


def replace_cloud_structure(scene, settings):
    ensure_mutation_allowed()
    from . import sky_linking
    from .sky_modes import require_cache

    root = root_for(scene)
    if root is None:
        raise ValueError("请先加载天空")
    controller = controller_for(root)
    settings, old, changed_domains = cloud_structure_plan(scene, root, controller, settings)
    links = sky_linking.plan(root, controller, settings)
    require_cache(scene, settings)
    registry = dict(root[REGISTRY].items())
    previous = {name: controller[PARAMETERS[name][2]] for name in CLOUD_STRUCTURE_KEYS}
    previous_structure = root["oxal_mc_studio_cloud_structure"]
    next_structure = json.dumps(settings)
    sampling_updates = _periodic_sampling_updates(root, controller, excluded=old)
    sampling_rollback = None
    transaction = _CloudCreation()
    try:
        sampling_rollback = _simplify_periodic_sampling(sampling_updates)
        created = _create_cloud_tiles(root, controller, settings, transaction, changed_domains)
        _audit_drivers(transaction.owned)
        for name, value in settings.items():
            controller[PARAMETERS[name][2]] = value
        sky_linking.apply(links)
        for obj, collection in created:
            collection.objects.link(obj)
        for item in transaction.owned:
            item[ASSET_ID], item[VERSION], item[INSTANCE] = RESOURCE_ID, RESOURCE_VERSION, root[INSTANCE]
        seal_owned(root, transaction.owned)
        controller.update_tag()
        resolve_view_layer(scene).update()
        root["oxal_mc_studio_cloud_structure"] = next_structure
    except Exception:
        if sampling_rollback is not None:
            sampling_rollback()
        sky_linking.apply(links, restore=True)
        root[REGISTRY] = registry
        for name, value in previous.items():
            controller[PARAMETERS[name][2]] = value
        root["oxal_mc_studio_cloud_structure"] = previous_structure
        transaction.rollback()
        raise
    # All fallible preparation and graph validation precedes destructive commit.
    bpy.data.batch_remove(ids=list(old))
    try:
        prune_owned(root)
    except Exception:
        # Old cloud Objects have already been retired. Keep the committed
        # structure/inputs and leave unused registered IDs for the next explicit
        # cleanup, rather than making callers restore unrecoverable old values.
        logger.exception("Unused owned sky cleanup was postponed after cloud structure replacement")
    return f"已应用云结构：可见 {settings['cloud_distance']}×{settings['cloud_distance']}；关闭及范围外对象已移除"


def cloud_angle_contract(controller, *, automatic=True):
    expression = _native_expression(
        "(day-(day-night)*((t-r)/max(.001,d-r) if t>=r and t<d else ((t-d)%24)/max(.001,24-d+r)))*pi/180"
    )
    pairs = [("day", "云光照日间角度"), ("night", "云光照夜间角度"),
             ("t", "计算时间"), ("r", "日落开始时间"), ("d", "夜晚完成过渡时间")]
    if automatic:
        expression += " if automatic else day*pi/180"
        pairs.append(("automatic", "云光照自动过渡"))
    return expression, [(name, controller, f'[{json.dumps(prop, ensure_ascii=False)}]') for name, prop in pairs]


def cloud_lighting_contracts(controller, light, *, angle_axis=0):
    """Native scalar curves matching local.31's day/night transition contract."""
    def variables(*pairs):
        return [(name, controller, f'[{json.dumps(prop, ensure_ascii=False)}]') for name, prop in pairs]
    transition = variables(("t", "计算时间"), ("r", "日出开始时间"),
                           ("e", "日出完成时间"), ("d", "日落开始时间"),
                           ("n", "夜晚完成过渡时间"), ("u", "云光照日出过渡倍率"),
                           ("v", "云光照日落过渡倍率"))
    angle, angle_variables = cloud_angle_contract(controller)
    return [
        (controller, '["oxal_mc_studio_cloud_day"]', None,
         "min(1,max(0,(t-r)*u/max(.001,e-r)))*min(1,max(0,(d-t)*v/max(.001,n-d)))", transition),
        (controller, '["oxal_mc_studio_cloud_night"]', None,
         "max(min(1,max(0,(t-d)*v/max(.001,n-d))),min(1,max(0,(r-t)*u/max(.001,e-r))))", transition),
        (light.data, "energy", None,
         "enabled*(day*w+night*z)*(o or a) if automatic else enabled*day*(o or a)",
         variables(("enabled", "oxal_mc_studio_cloud_enabled"), ("day", "云光照日间强度"),
                   ("night", "云光照夜间强度"), ("w", "oxal_mc_studio_cloud_day"),
                   ("z", "oxal_mc_studio_cloud_night"), ("o", "云光照启用"),
                   ("a", "Eevee高级云光照"), ("automatic", "云光照自动过渡"))),
        (light, "rotation_euler", angle_axis, angle, angle_variables),
        (light.data, "use_shadow", None, "shadow", variables(("shadow", "云光照阴影"))),
        (controller, '["云光照阴影"]', None, "advanced if auto else manual",
         variables(("advanced", "Eevee高级云光照"), ("auto", "云光照阴影自动控制"),
                   ("manual", "oxal_mc_studio_cloud_shadow_manual"))),
    ]


def install_cloud_lighting(controller, light):
    for prop in ("oxal_mc_studio_cloud_day", "oxal_mc_studio_cloud_night"):
        if prop not in controller:
            controller[prop] = 0.0
    for owner, path, index, expression, variables in cloud_lighting_contracts(controller, light):
        _driver(owner, path, expression, variables, index)


def _native_bindings(root, controller):
    for obj in tuple(root.all_objects):
        if obj.name.split(".")[0] in {"Sun", "MC月亮"}:
            obj.hide_render = obj.hide_viewport = True
        if obj.type == "LIGHT" and not obj.get("oxal_cloud_light"):
            group = obj.light_linking.receiver_collection
            obj.light_linking.blocker_collection = group
            if group is not None:
                state = "INCLUDE" if controller[PARAMETERS["cloud_enabled"][2]] and controller["云阴影"] else "EXCLUDE"
                for entry in group.collection_children:
                    entry.light_linking.link_state = state
            visibility = (
                "太阳当前可见度"
                if obj.name.split(".")[0] == "Sun_Light"
                else "月亮当前可见度"
            )
            _driver(
                obj.data,
                "use_shadow",
                "visibility>0.0001",
                [("visibility", controller, f'["{visibility}"]')],
            )
        if obj.name.split(".")[0] == "Sky_Fog" and obj.type == "MESH":
            for axis, native, divisor in (
                (0, "oxal_mc_studio_fog_range", 1024),
                (1, "oxal_mc_studio_fog_range", 1024),
                (2, "oxal_mc_studio_fog_height", 500),
            ):
                factor = obj.scale[axis] / divisor
                _driver(
                    obj,
                    "scale",
                    f"size*{factor!r}",
                    [
                        (
                            "size",
                            controller,
                            f"[{json.dumps(native, ensure_ascii=False)}]",
                        )
                    ],
                    axis,
                )
        if obj.type == "LIGHT" and obj.get("oxal_cloud_light"):
            install_cloud_lighting(controller, obj)


def _audit_drivers(owned):
    for item in owned:
        for owner in (item, getattr(item, "node_tree", None)):
            if owner is None:
                continue
            animation = getattr(owner, "animation_data", None)
            if not animation:
                continue
            for curve in animation.drivers:
                driver = curve.driver
                if driver.type != "SCRIPTED":
                    continue
                expression = _native_expression(driver.expression)
                if len(expression.encode("utf8")) > 255:
                    raise ValueError("天空原生驱动表达式超出长度限制")
                driver.expression = expression
                tree = ast.parse(driver.expression, mode="eval")
                variables = {v.name for v in driver.variables}
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and (
                        not isinstance(node.func, ast.Name)
                        or node.func.id not in SAFE_FUNCTIONS
                    ):
                        raise ValueError(
                            f"天空资源依赖不支持的 Python 驱动函数：{owner.name}"
                        )
                    if isinstance(
                        node, (ast.Attribute, ast.Subscript, ast.Lambda, ast.ListComp)
                    ):
                        raise TypeError(f"天空资源含有非标量驱动：{owner.name}")
                    if isinstance(
                        node, ast.Name
                    ) and node.id not in variables | SAFE_FUNCTIONS | {
                        "pi",
                        "frame",
                        "True",
                        "False",
                    }:
                        raise ValueError(f"天空驱动含有外部依赖：{node.id}")
                if not driver.is_simple_expression:
                    raise ValueError(f"天空驱动无法使用原生表达式求值：{owner.name}")


def _configure_glow_node(glare):
    """Adapt native Glare configuration by socket capabilities, not version."""
    if glare.inputs.get("Type") is not None:
        glare.inputs["Type"].default_value = "Fog Glow"
        glare.inputs["Quality"].default_value = "Medium"
    else:
        glare.glare_type, glare.quality = "FOG_GLOW", "MEDIUM"
    glare.inputs["Size"].default_value = .25


def _glare_upstream(socket):
    """Find a live Glare on the linked output path, including nested groups."""
    frontier = list(socket.links)
    visited = set()
    while frontier:
        edge = frontier.pop(0)
        node = edge.from_node
        token = (node.as_pointer(), edge.from_socket.identifier)
        if token in visited:
            continue
        visited.add(token)
        if node.type == "GLARE" and not node.mute:
            return node
        if node.type == "GROUP" and not node.mute and node.node_tree is not None:
            for output in node.node_tree.nodes:
                if output.type != "GROUP_OUTPUT" or not output.is_active_output:
                    continue
                nested = next((item for item in output.inputs
                               if item.identifier == edge.from_socket.identifier), None)
                if nested is not None:
                    frontier.extend(nested.links)
        frontier.extend(link for item in node.inputs for link in item.links)
    return None


def _known_014_glow(glare):
    """Only the exact, unanimated released generator profile may be repaired."""
    if (glare.name != "OXAL · 自动辉光" or glare.label or glare.mute
            or set(glare.keys()) != {"oxal_mc_studio_glow"}
            or glare.get("oxal_mc_studio_glow") is not True):
        return False
    expected = {
        "Image": (1., 1., 1., 1.), "Threshold": 1., "Smoothness": .1,
        "Clamp": False, "Maximum": 10., "Strength": 1., "Saturation": 1.,
        "Tint": (1., 1., 1., 1.), "Size": .5, "Streaks": 4,
        "Streaks Angle": 0., "Iterations": 3, "Fade": .9,
        "Color Modulation": .25, "Diagonal": True,
    }
    optional = {"Sun Position": (.5, .5), "Jitter": 0.,
                "Kernel Data Type": "Float", "Float Kernel": 0., "Color Kernel": (.8, .8, .8, 1.)}
    if glare.inputs.get("Type") is not None:
        expected.update({"Type": "Fog Glow", "Quality": "High"})
    elif glare.glare_type != "FOG_GLOW" or glare.quality != "HIGH":
        return False
    if not set(expected) <= {item.name for item in glare.inputs}:
        return False
    for item in glare.inputs:
        value = expected.get(item.name, optional.get(item.identifier))
        if value is None or not hasattr(item, "default_value") or item.is_linked != (item.name == "Image"):
            return False
        actual = item.default_value
        if isinstance(value, (tuple, list)):
            if (not hasattr(actual, "__len__") or len(actual) != len(value)
                    or any(not math.isclose(a, b, abs_tol=1e-6) for a, b in zip(actual, value, strict=True))):
                return False
        elif isinstance(value, str | bool):
            if actual != value:
                return False
        elif not math.isclose(actual, value, abs_tol=1e-6):
            return False
    animation = glare.id_data.animation_data
    if animation is not None:
        bindings, strips = [animation], [strip for track in animation.nla_tracks for strip in track.strips]
        while strips:
            strip = strips.pop()
            bindings.append(strip)
            strips.extend(getattr(strip, "strips", ()))
        curves = list(animation.drivers)
        try:
            for binding in bindings:
                curves.extend(action_fcurves(binding.action, slot=getattr(binding, "action_slot", None)))
        except (ValueError, ReferenceError):
            return False
        prefix = glare.path_from_id()
        if any(curve.data_path.startswith((prefix + ".", prefix + "[")) for curve in curves):
            return False
    return True


def _enable_glow_preview(scene, shading):
    current_screen = cast(Any, bpy.context).screen if bpy.context.scene == scene else None
    for screen in ((current_screen,) if current_screen is not None else ()):
        for area in screen.areas:
            if area.type == "VIEW_3D":
                view = area.spaces.active.shading
                if view.bl_rna.properties.get("use_compositor") is not None:
                    shading.append((view, view.use_compositor))
                    view.use_compositor = "ALWAYS"
    scene.render.use_compositing = True
    scene.render.compositor_device = "GPU"


class SkyTool:
    def __init__(self, asset_path: Path | None = None) -> None:
        self.asset_path = (
            asset_path
            or Path(__file__).resolve().parents[2] / "asset/sky/oxal_sky.blend"
        )

    def load(self, session_uid: int, options: Mapping[str, object] | None = None, *, hours: float | None = None) -> SkyLoadResult:
        ensure_mutation_allowed()
        scene = resolve_scene(session_uid)
        started = perf_counter()
        root = root_for(scene)
        if root is not None:
            controller = controller_for(root)
            world = root.get(WORLD_REFERENCE)
            if not isinstance(world, bpy.types.World):
                raise ValueError("天空 World 已删除；请先移除不完整实例再加载")
            self._activate_existing(scene, root, controller, world)
            return SkyLoadResult(
                scene.name,
                root.name,
                world.name,
                controller.name,
                True,
                (perf_counter() - started) * 1000,
                0.0,
                0.0,
            )
        if hours is not None and (
            isinstance(hours, bool)
            or not isinstance(hours, (int, float))
            or not math.isfinite(hours)
            or not 0 <= hours <= 24
        ):
            raise ValueError("天空时间必须为 0 到 24 的有限数值")
        identifier = instance_creation_id(options or {})
        if any(item.get(INSTANCE) == identifier and item.get("oxal_mc_studio_sky_root") for item in bpy.data.collections):
            raise ValueError("天空创建标识已存在，未重复创建")
        from .sky_modes import normalize_modes, require_cache

        initial_settings = normalize_modes(snapshot(getattr(scene, sky_state.VALUES)))
        require_cache(scene, initial_settings)
        stored_inputs = sky_state.capture_inputs(scene)
        input_migration = sky_inputs.migration_plan(scene)
        previous = scene.world
        previous_settings = {
            k: scene.get(k) for k in (PREVIOUS_WORLD, PREVIOUS_WORLD_EMPTY)
        }
        celestial_images = None
        try:
            with AppendTransaction(self.asset_path) as transaction:
                append_started = perf_counter()
                root, worlds, templates = transaction.append_bundle(
                    COLLECTION_NAME, worlds=(WORLD_NAME,), meshes=TEMPLATE_MESHES
                )
                append_ms = (perf_counter() - append_started) * 1000
                controller = controller_for(root)
                world = worlds[WORLD_NAME]
                root[WORLD_REFERENCE] = world
                values = getattr(scene, sky_state.VALUES)
                for name in CLOUD_STRUCTURE_KEYS:
                    setattr(values, name, initial_settings[name])
                if hours is not None:
                    values.time = hours
                sky_state.materialize_inputs(scene)
                sky_inputs.apply_migration(input_migration)
                _bind_parameters(scene, controller)
                _cloud_pool(root, controller, templates, transaction)
                _native_bindings(root, controller)
                from .sky_repair import ROLES, initial_roles, world_contract

                root[ROLES] = initial_roles(root, controller)
                world_contract(world, controller)
                from . import sky_celestial

                celestial_images = sky_celestial.Replacement(sky_celestial.plan(world, controller)).apply()
                for image in celestial_images.images:
                    transaction.track(image)
                for key in tuple(controller.keys()):
                    if key.startswith("oxal_cloud_") or key in {
                        "更新日志",
                        "OXAL天空发行类型",
                        "OXAL天空来源候选",
                    }:
                        del controller[key]
                for item in transaction.owned:
                    item[ASSET_ID], item[VERSION], item[INSTANCE] = (
                        RESOURCE_ID,
                        RESOURCE_VERSION,
                        identifier,
                    )
                root[WORLD_REFERENCE] = world
                root["oxal_mc_studio_sky_root"] = True
                _audit_drivers(transaction.owned)
                scene.collection.children.link(root)
                self._activate(scene, world)
                try:
                    # Finish provenance writes and unused-source retirement
                    # before evaluating. Otherwise the Operator's post-update
                    # invalidates and computes the same cloud geometry again.
                    seal_owned(root, transaction.owned)
                    prune_owned(root)
                    # Pruning can free unused IDs; keep rollback's exact set live.
                    transaction.owned = {root, *owned_ids(root)}
                    update_start = perf_counter()
                    controller.update_tag()
                    resolve_view_layer(scene).update()
                    update_ms = (perf_counter() - update_start) * 1000
                    transaction.commit()
                except Exception:
                    if owns(root, root):
                        transaction.owned = {root, *release_owned(root)}
                    raise
                return SkyLoadResult(
                    scene.name,
                    root.name,
                    world.name,
                    controller.name,
                    False,
                    (perf_counter() - started) * 1000,
                    append_ms,
                    update_ms,
                )
        except Exception:
            if celestial_images is not None:
                celestial_images.rollback()
            sky_inputs.apply_migration(input_migration,restore=True)
            scene.world = previous
            sky_state.restore_inputs(scene, stored_inputs)
            for key, value in previous_settings.items():
                if value is None:
                    if key in scene:
                        del scene[key]
                else:
                    scene[key] = value
            raise

    def _activate_existing(self, scene, root, controller, world):
        """Upgrade only an exact-owned previous beta in this explicit action."""
        ensure_mutation_allowed()
        require_owned(root, (root, controller, world))
        targets, drivers = _scene_input_bindings(scene, controller)
        input_migration = sky_inputs.migration_plan(scene,targets)
        window_updates = _cloud_window_updates(root, controller)
        sampling_updates = _periodic_sampling_updates(root, controller)
        sampling_rollback = None
        from . import sky_linking
        from .sky_modes import actual_structure

        links = sky_linking.plan(root, controller, actual_structure(controller)) if root.get(VERSION) == RESOURCE_VERSION else []
        stored_inputs = sky_state.capture_inputs(scene)
        previous_paths = [(target, target.data_path) for target, _ in targets]
        previous_expressions = [(driver, driver.expression) for driver in drivers]
        previous_ranges = {
            PARAMETERS[key][2]: controller.id_properties_ui(PARAMETERS[key][2]).as_dict()
            for key in CLOUD_WINDOW_KEYS
        }
        previous_range_values = {native: controller[native] for native in previous_ranges}
        previous = scene.world
        previous_settings = {
            k: scene.get(k) for k in (PREVIOUS_WORLD, PREVIOUS_WORLD_EMPTY)
        }
        try:
            sampling_rollback = _simplify_periodic_sampling(sampling_updates)
            sky_state.materialize_inputs(scene)
            sky_inputs.apply_migration(input_migration)
            _set_cloud_window_ranges(controller)
            for driver, _previous, expression in window_updates:
                driver.expression = expression
            for target, path in targets:
                target.data_path = path
            for driver in drivers:
                driver.expression = driver.expression
            self._activate(scene, world)
            sky_linking.apply(links)
            controller.update_tag()
            resolve_view_layer(scene).update()
        except Exception:
            if sampling_rollback is not None:
                sampling_rollback()
            sky_inputs.apply_migration(input_migration,restore=True)
            sky_linking.apply(links, restore=True)
            for driver, expression, _current in window_updates:
                driver.expression = expression
            for native, settings in previous_ranges.items():
                controller.id_properties_ui(native).update(**settings)
                controller[native] = previous_range_values[native]
            for target, path in previous_paths:
                target.data_path = path
            for driver, expression in previous_expressions:
                driver.expression = expression
            sky_state.restore_inputs(scene, stored_inputs)
            scene.world = previous
            for key, value in previous_settings.items():
                if value is None:
                    if key in scene:
                        del scene[key]
                else:
                    scene[key] = value
            raise

    @staticmethod
    def _activate(scene, world):
        if world.library or world.override_library:
            raise ValueError("天空 World 不可编辑")
        if scene.world is not world:
            if scene.world is not None and scene.world.get(ASSET_ID) != RESOURCE_ID:
                scene[PREVIOUS_WORLD] = scene.world
                scene[PREVIOUS_WORLD_EMPTY] = False
            elif scene.world is None:
                if PREVIOUS_WORLD in scene:
                    del scene[PREVIOUS_WORLD]
                scene[PREVIOUS_WORLD_EMPTY] = True
            scene.world = world

    def command(self, scene_uid: int, options: Mapping[str, object]) -> object:
        ensure_mutation_allowed()
        scene = resolve_scene(scene_uid)
        root_for(scene)
        action = options.get("action")
        if action == "cloud_mode":
            from .sky_modes import execute

            return execute(scene, options)
        if action == "shadow_quality":
            from .sky_quality import apply

            return apply(scene, options)
        if action == "cloud_apply":
            return _apply_cloud_structure(scene, options)
        if action == "load":
            return self.load(scene_uid, options)
        if action == "remove":
            return self.remove(scene)
        if action == "reset":
            saved = sky_state.capture_inputs(scene)
            try:
                values = getattr(scene, sky_state.VALUES)
                for key, (_, default, _) in PARAMETERS.items():
                    setattr(values, key, default)
                values.preset_keyframes = False
                if root_for(scene) is not None:
                    replace_cloud_structure(scene, {name: getattr(values, name) for name in CLOUD_STRUCTURE_KEYS})
            except Exception:
                sky_state.restore_inputs(scene, saved)
                raise
            return "已恢复天空默认参数与云结构"
        if action == "glow":
            return self.glow(scene)
        if action in {"save_slot", "apply_slot", "clear_slot", "key_slot"}:
            return self.slot(scene, str(action), options)
        from .sky_presets import operate

        if action in {
            "list_presets",
            "import_preset",
            "export_preset",
            "apply_preset",
            "delete_preset",
        }:
            saved = sky_state.capture_inputs(scene)
            try:
                result = operate(scene, str(action), options)
                if action == "apply_preset" and root_for(scene) is not None:
                    values = getattr(scene, sky_state.VALUES)
                    replace_cloud_structure(scene, {name: getattr(values, name) for name in CLOUD_STRUCTURE_KEYS})
                return result
            except Exception:
                sky_state.restore_inputs(scene, saved)
                raise
        raise ValueError("未知天空操作")

    @staticmethod
    def remove(scene):
        ensure_mutation_allowed()
        root = root_for(scene)
        if root is None:
            raise ValueError("当前场景没有新版天空")
        instance = root[INSTANCE]
        collections = {root, *root.children_recursive}
        objects = set(root.all_objects)
        require_owned(root, collections | objects)
        if any(item.get(INSTANCE) != instance for item in collections | objects):
            raise ValueError("天空合集中含有用户添加的数据，请先移出再删除天空")
        require_no_external_users(scene, root, objects | collections)
        if scene.world == root.get(WORLD_REFERENCE):
            previous = scene.get(PREVIOUS_WORLD)
            scene.world = previous if isinstance(previous, bpy.types.World) else None
        for key in (PREVIOUS_WORLD, PREVIOUS_WORLD_EMPTY):
            if key in scene:
                del scene[key]
        exclusive = {
            o for o in objects if all(c in collections for c in o.users_collection)
        }
        captured = release_owned(root)
        bpy.data.batch_remove(ids=list(exclusive | collections))
        remove_unused_owned(captured)
        return "已移除天空并恢复原 World"

    @staticmethod
    def glow(scene):
        """Connect native final/preview compositing without changing Cycles."""
        ensure_mutation_allowed()
        if scene.library or scene.override_library:
            raise ValueError("目标场景不可编辑")
        root_for(scene)
        scene = cast(Any, scene)
        grouped = scene.bl_rna.properties.get("compositing_node_group") is not None
        old_use_nodes = None if grouped else scene.use_nodes
        old_tree = getattr(scene, "compositing_node_group", None) if grouped else scene.node_tree
        old_compositing = scene.render.use_compositing
        device_property = scene.render.bl_rna.properties.get("compositor_device")
        if device_property is None or "GPU" not in {item.identifier for item in device_property.enum_items}:
            raise ValueError("当前 Blender 不支持 GPU 合成器，未修改合成图")
        old_device = scene.render.compositor_device
        tree: Any = old_tree
        private_tree = False
        nodes_created, replaced, shading, sockets_created, parameters_changed = [], [], [], [], []
        previous_active = None
        outcome = "已连接196默认辉光、合成预览和 GPU 合成器"
        try:
            if grouped:
                if tree is None:
                    tree = bpy.data.node_groups.new("OXAL · 场景合成", "CompositorNodeTree")
                    private_tree = True
                elif tree.library is not None or any(
                        owner != scene for owner in bpy.data.user_map(subset=(tree,)).get(tree, ())):
                    tree = tree.copy()
                    private_tree = True
                scene.compositing_node_group = tree
            else:
                scene.use_nodes = True
                tree = scene.node_tree
            if tree is None:
                raise ValueError("当前场景无法创建合成节点")
            # Embedded 4.5 and grouped 5.x graphs expose different interfaces.
            tree = cast(Any, tree)
            if not grouped and old_tree is None:
                nodes_created.extend(tree.nodes)
                # The native default active node is new too; never restore its
                # RNA reference after rollback has removed the default nodes.
            else:
                previous_active = tree.nodes.active
            output_type = "GROUP_OUTPUT" if grouped else "COMPOSITE"
            output = next((n for n in tree.nodes if n.type == output_type and getattr(n, "is_active_output", True)), None)
            if output is None:
                if grouped and not any(socket.item_type == "SOCKET" and socket.in_out == "OUTPUT"
                                       and socket.socket_type == "NodeSocketColor" for socket in tree.interface.items_tree):
                    sockets_created.append(tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor"))
                output = tree.nodes.new("NodeGroupOutput" if grouped else "CompositorNodeComposite")
                nodes_created.append(output)
            target = output.inputs.get("Image")
            if target is None:
                target = next((socket for socket in output.inputs if socket.type == "RGBA"), None)
            if target is None:
                raise ValueError("合成输出缺少图像接口，未修改用户图")
            if target.is_linked:
                previous = target.links[0].from_socket
            else:
                source = tree.nodes.new("CompositorNodeRLayers")
                nodes_created.append(source)
                previous = source.outputs["Image"]
            existing = _glare_upstream(target)
            if existing is not None:
                glare = existing
                outcome = "已复用最终输出中的辉光，保留已有参数与动画，并连接合成预览"
                if glare.id_data == tree and _known_014_glow(glare):
                    upstream = glare.inputs["Image"]
                    duplicate = _glare_upstream(upstream)
                    consumers = tuple(glare.outputs["Image"].links)
                    private_duplicate = (target.links[0].from_node == glare and duplicate is not None
                        and all(link.to_socket == target or (link.to_node.type == "VIEWER"
                                and link.to_node.get("oxal_mc_studio_glow_preview")) for link in consumers)
                        and not any(socket.is_linked for socket in glare.outputs if socket.name != "Image"))
                    if private_duplicate:
                        replaced.append((target, tuple(link.from_socket for link in target.links)))
                        tree.links.new(upstream.links[0].from_socket, target)
                        outcome = "已绕过上一版重复生成的辉光，保留原辉光参数与动画"
                    else:
                        attributes = ({"glare_type": glare.glare_type, "quality": glare.quality}
                                      if glare.inputs.get("Type") is None else {})
                        values = [(socket, tuple(socket.default_value) if not isinstance(socket.default_value, (str, bool, int, float))
                                   else socket.default_value) for socket in glare.inputs if hasattr(socket, "default_value")]
                        parameters_changed.append((glare, attributes, values))
                        _configure_glow_node(glare)
                        outcome = "已将未经修改的0.1.4辉光恢复为196默认，保留节点与全部绑定"
                preview_source = target.links[0].from_socket
            else:
                glare = tree.nodes.new("CompositorNodeGlare")
                nodes_created.append(glare)
                glare.name = "OXAL · 自动辉光"
                glare["oxal_mc_studio_glow"] = True
                _configure_glow_node(glare)
                glare.location = (output.location.x - 220, output.location.y)
                tree.links.new(previous, glare.inputs[0])
                replaced.append((target, tuple(link.from_socket for link in target.links)))
                tree.links.new(glare.outputs[0], target)
                preview_source = glare.outputs[0]
            viewers = [n for n in tree.nodes if n.type == "VIEWER" and (
                n.get("oxal_mc_studio_glow_preview") or any(link.from_socket == previous for link in n.inputs[0].links))]
            if not viewers:
                viewer = tree.nodes.new("CompositorNodeViewer")
                nodes_created.append(viewer)
                viewer["oxal_mc_studio_glow_preview"] = True
                viewer.location = (output.location.x, output.location.y - 180)
                viewers = [viewer]
            for viewer in viewers:
                replaced.append((viewer.inputs[0], tuple(link.from_socket for link in viewer.inputs[0].links)))
                tree.links.new(preview_source, viewer.inputs[0])
            tree.nodes.active = viewers[0]
            _enable_glow_preview(scene, shading)
        except Exception:
            try:
                if tree is not None:
                    for glare, attributes, values in reversed(parameters_changed):
                        for name, value in attributes.items():
                            setattr(glare, name, value)
                        for socket, value in values:
                            socket.default_value = value
                    for target, originals in reversed(replaced):
                        for link in tuple(target.links):
                            tree.links.remove(link)
                        for source in originals:
                            tree.links.new(source, target)
                    for node in reversed(nodes_created):
                        tree.nodes.remove(node)
                    for socket in reversed(sockets_created):
                        tree.interface.remove(socket)
                    tree.nodes.active = previous_active
            finally:
                # Restore execution/preferences even if native graph cleanup
                # itself fails; never leave GPU or viewport settings half set.
                try:
                    for view, previous_mode in shading:
                        view.use_compositor = previous_mode
                    scene.render.compositor_device = old_device
                    scene.render.use_compositing = old_compositing
                finally:
                    if grouped:
                        try:
                            scene.compositing_node_group = old_tree
                        finally:
                            if private_tree and tree is not None:
                                bpy.data.node_groups.remove(tree)
                    else:
                        scene.use_nodes = old_use_nodes
            raise
        return outcome

    @staticmethod
    def slot(scene, action, options):
        ensure_mutation_allowed()
        root_for(scene)
        index, keyframes = options.get("slot_index"), options.get("slot_keyframes")
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= sky_state.SLOT_COUNT:
            raise ValueError("缺少点击时的保存值编号，请重新操作")
        if not isinstance(keyframes, bool):
            raise TypeError("缺少点击时的关键帧模式")
        params = {}
        if action in {"save_slot", "apply_slot"}:
            submitted = options.get("parameters")
            if not isinstance(submitted, dict):
                raise TypeError("缺少点击时的天空参数")
            params = validated(submitted)
        if action == "apply_slot":
            slots = getattr(scene, sky_state.SLOTS)
            if len(slots) < index or not slots[index - 1].filled:
                raise ValueError("目标保存值已清除，未改写其它槽位")
        frame = options.get("key_frame")
        if action == "key_slot":
            if isinstance(frame, bool) or not isinstance(frame, (int, float)) or not math.isfinite(frame):
                raise ValueError("缺少点击时的关键帧位置")
            if sky_inputs.legacy_animation(scene):
                raise ValueError("旧天空动画仍使用RNA路径，请先加载/激活或修复天空，再插入保存值关键帧")
            animation = scene.animation_data
            if animation and animation.action and (animation.action.library or animation.action.override_library or animation.action.users - int(animation.action.use_fake_user) > 1):
                raise ValueError("场景 Action 只读或被共享，未插入天空关键帧")
        saved = sky_state.capture_inputs(scene)
        values = getattr(scene, sky_state.VALUES)
        from .sky_modes import normalize_modes
        try:
            sky_state.initialize_slots(scene)
            slot = getattr(scene, sky_state.SLOTS)[index - 1]
            if action == "clear_slot":
                slot.filled = False
                return "已清除点击时选择的天空保存值"
            if action == "save_slot":
                for key, value in params.items():
                    setattr(slot.values, key, value)
                slot.filled = True
                return "已保存点击时的有效天空参数"
            values.preset_slot, values.preset_keyframes = index, keyframes
            if action == "key_slot":
                sky_state.materialize_inputs(scene)
                path = sky_inputs.path("preset_slot")
                if not scene.keyframe_insert(path, frame=frame):
                    raise ValueError("天空选择关键帧未插入")
                animation = scene.animation_data
                if animation and animation.action:
                    for curve in action_fcurves(animation.action, slot=animation.action_slot):
                        if curve.data_path == path:
                            for point in curve.keyframe_points:
                                if abs(point.co.x - frame) < .001:
                                    point.interpolation = "CONSTANT"
                return "已在点击时的帧插入指定保存值关键帧"
            if keyframes:
                # The active native source is this slot. Replaying the captured
                # read must not silently use values edited after Undo.
                for key, value in normalize_modes(params).items():
                    setattr(slot.values, key, value)
            for key, value in normalize_modes(params).items():
                setattr(values, key, value)
            if root_for(scene) is not None:
                replace_cloud_structure(scene, {name: getattr(values, name) for name in CLOUD_STRUCTURE_KEYS})
        except Exception:
            sky_state.restore_inputs(scene, saved)
            raise
        return "已读取点击时的天空保存值参数"
