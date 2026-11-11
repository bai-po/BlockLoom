"""Instance-local native simulation epochs and explicit packed-cache commands.

NEW_INSTANCE, scoped to one owned projectile. Geometry Nodes retain the event
state; Python runs only for explicit preparation, cache publication and checks.
Unbaked playback needs sequential evaluation. Random-frame delivery requires
the user's explicit packed bake, never a playback/render observer.
"""

import hashlib
import json
import math
from typing import cast

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.drivers import capture as capture_drivers
from ...util.drivers import restore as restore_drivers
from ...util.ownership import REGISTRY, explicit_scene_ids, require_owned
from .asset_geometry import Creation, driver
from .asset_lightning import _variables

CLOCK = "oxal_mc_studio_projectile_clock"
MARK = "oxal_mc_studio_projectile_clock_schema"
CONTRACT = "oxal_mc_studio_projectile_clock_contract"
BAKED = "oxal_mc_studio_projectile_clock_baked"
START = "oxal_mc_studio_projectile_clock_start"
END = "oxal_mc_studio_projectile_clock_end"
SIGNATURE = "oxal_mc_studio_projectile_clock_source"
EPOCH = "oxal_mc_studio_projectile_clock_manual_epoch"
ELAPSED = "oxal_projectile_elapsed"
ACTIVE = "oxal_projectile_active"
EVENT = "oxal_projectile_event"
INFO = "OXAL Projectile Clock"
STAGE_BONE = "阶段 · 0隐藏 1投掷 2特效 3关闭"
MAX_FRAMES = 20000


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _plain(value, root=None, scene=None):
    if isinstance(value, bpy.types.ID):
        if value == root:
            return "ROOT"
        if value == scene:
            return "SCENE"
        return [value.id_type, value.name_full]
    if hasattr(value, "items"):
        return {key: _plain(item, root, scene) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, set):
        return sorted(_plain(item, root, scene) for item in value)
    return [_plain(item, root, scene) for item in value]


def _scalars(owner, exclude=()):
    return [(prop.identifier, _plain(getattr(owner, prop.identifier))) for prop in owner.bl_rna.properties
            if not prop.is_readonly and prop.type in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}
            and prop.identifier not in {"name", "select", "active", "show_expanded", "lock", *exclude}]


def _curve(curve):
    return [_scalars(curve), [_scalars(point) for point in curve.keyframe_points],
            [tuple(point.co) for point in curve.sampled_points],
            [(_scalars(modifier), [_scalars(point) for point in getattr(modifier, "control_points", ())]) for modifier in curve.modifiers]]


def _source(root, scene):
    """Explicit-command fingerprint of Action/NLA and native driver inputs."""
    from .asset_rigs import driver_records
    animation = root.animation_data
    records, stage_keys, effective = [], False, False
    path = root.pose.bones[STAGE_BONE].path_from_id("location")

    def binding(owner, enabled=True):
        nonlocal stage_keys, effective
        curves = action_fcurves(owner.action, slot=getattr(owner, "action_slot", None))
        relevant = [curve for curve in curves if curve.data_path == path and curve.array_index == 0]
        stage_keys |= bool(relevant)
        effective |= enabled and any(not curve.mute for curve in relevant)
        changing = {curve.data_path.rsplit(".", 1)[-1] for curve in getattr(owner, "fcurves", ())}
        if isinstance(owner, bpy.types.NlaStrip):
            if not owner.use_animated_influence:changing.add("influence")
            if not owner.use_animated_time:changing.add("strip_time")
        return [_scalars(owner, changing), [_curve(curve) for curve in curves],
                [_curve(curve) for curve in getattr(owner, "fcurves", ())]]

    def strips(items, enabled):
        return [(binding(strip, enabled and not strip.mute),
                 strips(getattr(strip, "strips", ()), enabled and not strip.mute)) for strip in items]

    if animation:
        solo = any(track.is_solo for track in animation.nla_tracks)
        records = [binding(animation, animation.action_influence > 0),
                   [(_scalars(track), strips(track.strips, animation.use_nla and not track.mute and (not solo or track.is_solo)))
                    for track in animation.nla_tracks]]
    # Unkeyed pose is a real input. A keyed pose varies per frame, rather than
    # invalidating its own recorded cache on every frame.
    static = None if effective else tuple(root.pose.bones[STAGE_BONE].location)
    return _digest([scene.render.fps, scene.render.fps_base, records,
                    _plain(driver_records(root), root, scene), static]), effective, stage_keys and not effective


def _tree_contract(clock, root, scene):
    from .asset_rigs import driver_records
    tree = clock.modifiers[0].node_group
    curves = driver_records(tree)
    driven = {record[0] for record in curves}
    nodes = []
    for node in tree.nodes:
        defaults = [(i, _plain(socket.default_value, root, scene))
                    for i, socket in enumerate((*tuple(node.inputs), *tuple(node.outputs)))
                    if hasattr(socket, "default_value") and socket.path_from_id("default_value") not in driven]
        state = [(item.name, item.socket_type) for item in getattr(node, "state_items", ())]
        nodes.append([node.name, node.bl_idname, _scalars(node), defaults, state, _plain(dict(node.items()), root, scene)])
    ignored = {CONTRACT, "oxal_mc_studio_generated_owner", "oxal_mc_studio_generated_instance", "oxal_mc_studio_owned_data_uid"}
    properties = {key: value for key, value in clock.items() if key not in {
        CONTRACT, "oxal_mc_studio_generated_owner", "oxal_mc_studio_generated_instance", "oxal_mc_studio_owned_data_uid"}}
    return _digest([_plain(properties, root, scene), _plain(clock.matrix_basis), _plain(clock.matrix_parent_inverse),
                    _plain({key: value for key, value in tree.items() if key not in ignored}, root, scene),
                    nodes, [(link.from_node.name, link.from_socket.identifier,
                             link.to_node.name, link.to_socket.identifier) for link in tree.links],
                    _plain(capture_drivers(tree), root, scene), _scalars(clock.modifiers[0], {"open_bake_panel", "open_bake_data_blocks_panel"}),
                    [_scalars(bake) for bake in clock.modifiers[0].bakes],
                    [list(vertex.co) for vertex in clock.data.vertices],
                    [list(edge.vertices) for edge in clock.data.edges], [list(poly.vertices) for poly in clock.data.polygons],
                    [(attr.name, attr.domain, attr.data_type, [_scalars(value) for value in attr.data]) for attr in clock.data.attributes
                     if attr.name not in {"position", ".edge_verts", ".corner_vert", ".corner_edge", "material_index"}
                     and not attr.name.startswith((".select_", ".hide_", ".uv_select_"))]])


def _validate(scene, root, clock):
    if not isinstance(clock, bpy.types.Object) or clock.type != "MESH":
        raise ValueError("投射物缺少原生时钟")
    mesh = cast(bpy.types.Mesh, clock.data)
    if (clock.get(MARK) != 1
            or clock.parent != root or clock.parent_type != "OBJECT" or clock.parent_bone
            or clock.animation_data or mesh.animation_data or mesh.shape_keys or clock.constraints
            or len(clock.modifiers) != 1 or clock.modifiers[0].type != "NODES"
            or clock.use_fake_user or mesh.use_fake_user or mesh.materials or clock.vertex_groups
            or clock.hide_viewport or clock.hide_render):
        raise ValueError("投射物原生时钟已被用户修改，未覆盖")
    group = cast(bpy.types.NodesModifier, clock.modifiers[0]).node_group
    if group is None or group.use_fake_user:
        raise ValueError("投射物时钟缺少原生节点")
    require_owned(root, (clock, mesh, group))
    if clock.get(CONTRACT) != _tree_contract(clock, root, scene):
        raise ValueError("投射物时钟网格或原生节点含用户修改，未覆盖")


def build(transaction, scene, collection, root, *, start=None, end=None, manual_epoch=None, baked=False):
    mesh = transaction.track(bpy.data.meshes.new("爆炸时钟 · 单点状态"))
    mesh.from_pydata([(0, 0, 0)], [], [])
    clock = transaction.track(bpy.data.objects.new("投射物 · 原生爆炸时钟", mesh))
    collection.objects.link(clock)
    clock.parent = root
    clock.hide_select = True
    clock[MARK] = 1
    clock["oxal_mc_studio_native_clock"] = True
    clock["oxal_mc_studio_model_part"] = "投射物 · 原生爆炸时钟"
    clock["oxal_mc_studio_effect_geometry"] = True
    clock[BAKED] = baked
    clock[START], clock[END] = int(scene.frame_start if start is None else start), int(scene.frame_end if end is None else end)
    if manual_epoch is not None:
        clock[EPOCH] = float(manual_epoch)
    tree = transaction.track(bpy.data.node_groups.new("投射物 · 原生阶段事件", "GeometryNodeTree"))
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")

    def node(kind, name):
        value = tree.nodes.new(kind)
        value.name = name
        return value

    def math_node(operation, a, b, name):
        value = node("ShaderNodeMath", name)
        value.operation = operation
        for index, part in enumerate((a, b)):
            if isinstance(part, (int, float, bool)):
                value.inputs[index].default_value = part
            else:
                tree.links.new(part, value.inputs[index])
        return value.outputs[0]

    entry, output = node("NodeGroupInput", "Input"), node("NodeGroupOutput", "Output")
    source = node("GeometryNodeSimulationInput", "Event Input")
    state = node("GeometryNodeSimulationOutput", "Event Output")
    source.pair_with_output(state)
    state.state_items.new("BOOLEAN", "Previous Active")
    state.state_items.new("FLOAT", "Event Frame")
    source.inputs["Previous Active"].default_value = manual_epoch is not None
    source.inputs["Event Frame"].default_value = float(manual_epoch or 0)
    # Simulation's implicit state name is translated when new-data
    # translation is enabled. It is the one geometry state in this freshly
    # constructed zone; resolve its RNA type instead of its display name.
    def geometry_socket(sockets):
        candidates = [socket for socket in sockets if socket.type == "GEOMETRY"]
        if len(candidates) != 1:
            raise ValueError("投射物模拟区缺少唯一原生几何状态")
        return candidates[0]
    tree.links.new(entry.outputs["Geometry"], geometry_socket(source.inputs))
    tree.links.new(geometry_socket(source.outputs), geometry_socket(state.inputs))
    phase = node("ShaderNodeValue", "Stage")
    driver(phase.outputs[0], "default_value", "v", root, "阶段")
    active = math_node("COMPARE", phase.outputs[0], 2, "Stage Two")
    active.node.inputs[2].default_value = .01
    previous = node("FunctionNodeBooleanMath", "Keep Event")
    previous.operation = "AND"
    tree.links.new(active, previous.inputs[0])
    tree.links.new(source.outputs["Previous Active"], previous.inputs[1])
    time = node("GeometryNodeInputSceneTime", "Native Frame")
    epoch = node("GeometryNodeSwitch", "Event Epoch")
    epoch.input_type = "FLOAT"
    tree.links.new(previous.outputs[0], epoch.inputs["Switch"])
    tree.links.new(time.outputs["Frame"], epoch.inputs["False"])
    tree.links.new(source.outputs["Event Frame"], epoch.inputs["True"])
    tree.links.new(epoch.outputs[0], state.inputs["Event Frame"])
    tree.links.new(active, state.inputs["Previous Active"])
    fps = node("ShaderNodeValue", "FPS")
    curve = driver(fps.outputs[0], "default_value", "fps/max(base,.000001)")
    _variables(curve, [("fps", scene, "render.fps"), ("base", scene, "render.fps_base")])
    elapsed = math_node("DIVIDE", math_node("SUBTRACT", time.outputs["Frame"], state.outputs["Event Frame"], "Elapsed Frames"), fps.outputs[0], "Elapsed Seconds")
    enabled = state.outputs["Previous Active"]
    if baked:
        low = math_node("GREATER_THAN", time.outputs["Frame"], clock[START] - .001, "In Start")
        high = math_node("LESS_THAN", time.outputs["Frame"], clock[END] + .001, "In End")
        enabled = math_node("MULTIPLY", enabled, math_node("MULTIPLY", low, high, "In Cache"), "Cached Active")
    geometry = geometry_socket(state.outputs)
    for name, socket, datatype in ((ELAPSED, elapsed, "FLOAT"), (ACTIVE, enabled, "BOOLEAN"), (EVENT, state.outputs["Event Frame"], "FLOAT")):
        store = node("GeometryNodeStoreNamedAttribute", name)
        store.data_type, store.domain = datatype, "POINT"
        store.inputs["Name"].default_value = name
        tree.links.new(geometry, store.inputs["Geometry"])
        tree.links.new(socket, store.inputs["Value"])
        geometry = store.outputs["Geometry"]
    tree.links.new(geometry, output.inputs["Geometry"])
    modifier = clock.modifiers.new("原生阶段事件", "NODES")
    modifier.node_group = tree
    modifier.bake_target = "PACKED"
    for bake in modifier.bakes:
        bake.use_custom_simulation_frame_range = baked
        bake.frame_start, bake.frame_end = clock[START], clock[END]
    clock[CONTRACT] = _tree_contract(clock, root, scene)
    return clock


def bridge(transaction, scene, root, tree):
    """Give a freshly appended original FX graph the shared native event age."""
    if tree not in transaction.ids or tree.bl_idname != "GeometryNodeTree":
        raise ValueError("只能接入本次追加的私有投射物节点")
    clock = root[CLOCK]
    times = [node for node in tree.nodes if node.bl_idname == "GeometryNodeInputSceneTime"]
    if len(times) != 1:
        raise ValueError("原生投射物缺少唯一的特效年龄入口")
    info = tree.nodes.new("GeometryNodeObjectInfo")
    info.name = INFO
    info.inputs["Object"].default_value = clock
    info.transform_space = "ORIGINAL"

    def sample(name, datatype):
        attribute = tree.nodes.new("GeometryNodeInputNamedAttribute")
        attribute.data_type = datatype
        attribute.inputs["Name"].default_value = name
        node = tree.nodes.new("GeometryNodeSampleIndex")
        node.data_type, node.domain = datatype, "POINT"
        tree.links.new(info.outputs["Geometry"], node.inputs["Geometry"])
        tree.links.new(attribute.outputs["Attribute"], node.inputs["Value"])
        return node.outputs["Value"]

    elapsed = sample(ELAPSED, "FLOAT")
    active = sample(ACTIVE, "BOOLEAN")
    phase = tree.nodes.new("ShaderNodeMath")
    phase.operation = "MULTIPLY"
    phase.inputs[1].default_value = 2
    tree.links.new(active, phase.inputs[0])
    fps = tree.nodes.new("ShaderNodeValue")
    curve = driver(fps.outputs[0], "default_value", "fps/max(base,.000001)")
    _variables(curve, [("fps", scene, "render.fps"), ("base", scene, "render.fps_base")])
    frame = tree.nodes.new("ShaderNodeMath")
    frame.operation = "MULTIPLY"
    tree.links.new(elapsed, frame.inputs[0])
    tree.links.new(fps.outputs[0], frame.inputs[1])
    for link in tuple(times[0].outputs["Frame"].links):
        tree.links.new(frame.outputs[0], link.to_socket)
    for curve in tree.animation_data.drivers if tree.animation_data else ():
        paths = {target.data_path for variable in curve.driver.variables for target in variable.targets if target.id == root}
        if paths & {'["阶段"]', '["oxal_mc_studio_projectile_effect_phase"]', '["命中帧"]'}:
            socket = tree.path_resolve(curve.data_path.rsplit(".", 1)[0])
            if not isinstance(socket, bpy.types.NodeSocketFloat):
                raise ValueError("投射物阶段/年龄原生输入类型已改变")
            for link in tuple(socket.links or ()):
                if '["命中帧"]' in paths:
                    zero = tree.nodes.new("ShaderNodeValue")
                    zero.outputs[0].default_value = 0
                    tree.links.new(zero.outputs[0], link.to_socket)
                else:
                    tree.links.new(phase.outputs[0], link.to_socket)


def _value(clock, name):
    evaluated = clock.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        attribute = mesh.attributes.get(name)
        if attribute is None or len(attribute.data) != 1:
            raise ValueError("原生事件时钟缺少唯一的求值状态")
        return attribute.data[0].value
    finally:
        evaluated.to_mesh_clear()


def _scope(scene, root):
    from .asset_contracts import validate
    if bpy.context.scene != scene or tuple(root.users_scene) != (scene,) or root.get("oxal_mc_studio_projectile_schema") != 3:
        raise ValueError("请选择当前场景中已修复的原生阶段投射物")
    validate(root)
    clock = root.get(CLOCK)
    _validate(scene, root, clock)
    return clock


def request(scene, root, options):
    clock = _scope(scene, root)
    current = float(options.get("current_frame", scene.frame_current_final))
    start, end = options.get("frame_start", scene.frame_start), options.get("frame_end", scene.frame_end)
    if (not math.isfinite(current) or isinstance(start, bool) or isinstance(end, bool)
            or int(start) != start or int(end) != end or start > end or end - start + 1 > MAX_FRAMES):
        raise ValueError("原生缓存帧范围无效或超过20000帧")
    signature, animated, ambiguous = _source(root, scene)
    epoch = None
    phase = root.evaluated_get(bpy.context.evaluated_depsgraph_get()).get("阶段", 0)
    manual_active = not animated and abs(float(phase) - 2) < .01
    if manual_active:
        epoch = float(_value(clock, EVENT)) if bool(_value(clock, ACTIVE)) else current
    return {"current_frame": current, "frame_start": int(start), "frame_end": int(end),
            "source_signature": signature, "manual_epoch": epoch, "manual_active": manual_active, "ambiguous_stage": ambiguous}


def cache_status(root):
    """Constant metadata only: no draw-time hashing, evaluation or IO."""
    clock = root.get(CLOCK)
    if not isinstance(clock, bpy.types.Object) or clock.get(MARK) != 1:
        return {"is_baked": False, "frame_start": None, "frame_end": None, "label": "缺少原生时钟，请显式修复此Beta实例"}
    baked = bool(clock.get(BAKED))
    return {"is_baked": baked, "frame_start": clock.get(START) if clock else None,
            "frame_end": clock.get(END) if clock else None,
            "label": (f"已烘焙 {clock[START]}–{clock[END]}；改阶段动画后请重新烘焙" if baked
                      else "未烘焙：顺播预览；跳帧、冷开渲染前请烘焙")}


def preserve_existing(scene, root, old, new):
    if not old.get(MARK) and not new.get(MARK):
        return False
    if old != root.get(CLOCK) or old.get(MARK) != new.get(MARK):
        raise ValueError("原生投射物时钟无法唯一对应，未重建")
    _validate(scene, root, old)
    return True


def _unknown_reference(owner, target, root, clock, destination, sockets):
    seen = set()
    def custom(value):
        if isinstance(value, bpy.types.ID):return value == target
        if hasattr(value, "items"):return any(custom(part) for part in value.values())
        if isinstance(value, (tuple, list)):return any(custom(part) for part in value)
        return False
    def visit(value):
        if value is None or not hasattr(value, "bl_rna") or value.as_pointer() in seen:return False
        seen.add(value.as_pointer())
        if hasattr(value, "items"):
            try:properties = tuple(value.items())
            except TypeError:properties = ()
            for key, part in properties:
                if value == root and key in {REGISTRY, "oxal_mc_studio_contract_refs", CLOCK}:continue
                if custom(part):return True
        for prop in value.bl_rna.properties:
            if prop.is_runtime or prop.identifier in {"rna_type", "id_data", "original"}:continue
            if value == destination and prop.identifier in {"objects", "all_objects"}:continue
            if value == clock and prop.identifier == "data" and target == clock.data:continue
            if value == clock.modifiers[0] and prop.identifier == "node_group" and target == clock.modifiers[0].node_group:continue
            if value in sockets and prop.identifier == "default_value" and target == clock:continue
            if prop.type not in {"POINTER", "COLLECTION"}:continue
            part = getattr(value, prop.identifier)
            for item in tuple(part) if prop.type == "COLLECTION" else (part,):
                if isinstance(item, bpy.types.ID):
                    if item == target or (item.is_embedded_data and visit(item)):return True
                elif visit(item):return True
        return False
    return visit(owner)


def _consumers(scene, root, clock):
    """Permit the generated ObjectInfo input, never arbitrary owned references."""
    group = clock.modifiers[0].node_group
    bundle = {clock, clock.data, group}
    require_owned(root, bundle)
    consumers = []
    for obj in root.children_recursive:
        if obj == clock or not obj.get("oxal_mc_studio_native_particle_kind"):
            continue
        for modifier in obj.modifiers:
            if modifier.type != "NODES":
                continue
            tree = modifier.node_group
            info = tree.nodes.get(INFO)
            if info is None or info.bl_idname != "GeometryNodeObjectInfo" or info.inputs["Object"].default_value != clock:
                raise ValueError("原生投射物时钟消费者已改变，未覆盖")
            require_owned(root, (tree,))
            if any(node != info and any(getattr(socket, "default_value", None) == clock for socket in node.inputs
                                      if socket.type == "OBJECT") for node in tree.nodes):
                raise ValueError("特效节点含额外用户时钟引用，未覆盖")
            consumers.append(info.inputs["Object"])
    trees = {socket.id_data for socket in consumers}
    if not trees:
        raise ValueError("投射物时钟没有已验证的特效消费者")
    references = explicit_scene_ids(scene)
    destination = clock.users_collection[0] if len(clock.users_collection) == 1 else None
    for item, users in bpy.data.user_map(subset=tuple(bundle)).items():
        for user in users:
            if user == scene and item not in references:
                continue
            allowed = (user == root or (user == destination and item == clock)
                       or (user == clock and item in {clock.data, group}) or (user in trees and item == clock))
            if not allowed or _unknown_reference(user, item, root, clock, destination, set(consumers)):
                raise ValueError("原生时钟被额外用户或场景引用，未覆盖")
    return consumers


def _packed_bake(transaction, scene, root, fresh, collection):
    """Evaluate only a private Scene and a read-only animation proxy.

    Advancing the user's Scene would also advance existing unbaked simulations.
    The proxy keeps the bound Action/NLA, but only the proven stage driver and
    local pose input participate. No existing cache or animation is modified.
    """
    window = cast(bpy.types.Window | None, bpy.context.window)
    if window is None:
        raise ValueError("原生缓存需要当前Blender窗口上下文")
    previous_scene, previous_layer = window.scene, window.view_layer
    temporary = transaction.track(bpy.data.scenes.new("爆炸缓存 · 私有求值"))
    temporary.render.fps, temporary.render.fps_base = scene.render.fps, scene.render.fps_base
    temporary.frame_start, temporary.frame_end = fresh[START], fresh[END]
    proxy = transaction.track(root.copy())
    data = transaction.track(root.data.copy())
    proxy.data = data
    proxy.name = "爆炸缓存 · 阶段动画代理"
    proxy.parent = None
    proxy.constraints.clear()
    proxy.modifiers.clear()
    data.animation_data_clear()
    for owner in (proxy, data, *tuple(data.bones), *tuple(proxy.pose.bones)):
        for key in tuple(owner.keys()):
            del owner[key]
    # The copied Object's ID properties are not an identity or ownership proof.
    # Only the stage destination is required by the private animation graph.
    proxy["阶段"] = 1.0
    for pose in proxy.pose.bones:
        for constraint in tuple(pose.constraints):pose.constraints.remove(constraint)
        pose.custom_shape = None
        pose.custom_shape_transform = None
    record = capture_drivers(root)
    phase = [item for item in record["curves"] if item["path"] == '["阶段"]']
    expected = root.pose.bones[STAGE_BONE].path_from_id("location") + "[0]"
    if len(phase) != 1:
        raise ValueError("原生阶段驱动不唯一，未复制缓存输入")
    for variable in phase[0]["variables"]:
        if variable["properties"]["type"] != "SINGLE_PROP":
            raise ValueError("阶段驱动已不是原生局部位置输入")
        for target in variable["targets"]:
            if target.get("id") != root or target.get("data_path") != expected:
                raise ValueError("阶段驱动含额外场景输入，未复制缓存输入")
            target["id"] = proxy
    restore_drivers(proxy, {"animation": True, "curves": phase})
    private_actions = {}
    history_start = fresh[START]
    stage_path = root.pose.bones[STAGE_BONE].path_from_id("location")
    def action(binding, *, global_time=False, enabled=True):
        nonlocal history_start
        source_action = binding.action
        if source_action is None:return
        source_slot = getattr(binding, "action_slot", None)
        timeline = ({name: getattr(binding, name) for name in ("frame_start", "frame_end", "action_frame_start", "action_frame_end", "repeat", "scale")}
                    if isinstance(binding, bpy.types.NlaStrip) else {})
        if global_time and enabled:
            for curve in action_fcurves(source_action, slot=source_slot):
                if curve.data_path == stage_path and curve.array_index == 0 and curve.keyframe_points:
                    history_start = min(history_start, math.floor(min(point.co.x for point in curve.keyframe_points)) - 1)
        if source_action not in private_actions:
            copied = transaction.track(source_action.copy())
            private_actions[source_action] = copied
            original_range = tuple(source_action.frame_range)
            for layer in copied.layers:
                for strip in layer.strips:
                    for bag in strip.channelbags:
                        for curve in tuple(bag.fcurves):
                            if curve.data_path != stage_path or curve.array_index != 0:
                                bag.fcurves.remove(curve)
                            elif curve.keyframe_points:
                                first = min(curve.keyframe_points, key=lambda point: point.co.x)
                                # A lone first key at detent 2 holds backwards
                                # under native Action extrapolation. Source196
                                # treats that key as the event, not frame 1.
                                # Prepare a private pre-event key; never edit
                                # the user's curve or its original Action ID.
                                if round(float(first.co.y) / .35 * 3) == 2:
                                    at, right = float(first.co.x), tuple(first.handle_right)
                                    prior = curve.keyframe_points.insert(at - 1, .35 / 3)
                                    prior.interpolation = "CONSTANT"
                                    following = next(point for point in curve.keyframe_points if abs(point.co.x - at) < 1e-6)
                                    following.handle_right_type = "FREE"
                                    following.handle_right = right
            copied.use_frame_range = True
            copied.frame_start, copied.frame_end = original_range
        binding.action = private_actions[source_action]
        if source_slot is not None:
            binding.action_slot = next(slot for slot in binding.action.slots if slot.identifier == source_slot.identifier)
        if timeline:
            binding.use_sync_length = False
            for name in ("action_frame_start", "action_frame_end", "repeat", "scale", "frame_start", "frame_end"):
                setattr(binding, name, timeline[name])
    def strips(items, enabled):
        nonlocal history_start
        for strip in items:
            active_strip = enabled and not strip.mute
            if active_strip:history_start = min(history_start, math.floor(strip.frame_start) - 1)
            action(strip, enabled=active_strip)
            strips(getattr(strip, "strips", ()), active_strip)
    animation = proxy.animation_data
    action(animation, global_time=True, enabled=animation.action_influence > 0)
    solo = any(track.is_solo for track in animation.nla_tracks)
    for track in animation.nla_tracks:strips(track.strips, animation.use_nla and not track.mute and (not solo or track.is_solo))
    if _source(root, scene)[1]:proxy.pose.bones[STAGE_BONE].location.x = .35 / 3
    if fresh[END] - history_start + 1 > MAX_FRAMES:
        raise ValueError("阶段动画历史超过20000帧，请缩短动画历史后再烘焙")
    fresh["oxal_mc_studio_projectile_clock_history_start"] = history_start
    temporary.frame_start = history_start
    for bake in fresh.modifiers[0].bakes:bake.frame_start = history_start
    temporary.collection.objects.link(proxy)
    collection.objects.unlink(fresh)
    temporary.collection.objects.link(fresh)
    tree = fresh.modifiers[0].node_group
    for curve in tree.animation_data.drivers:
        for variable in curve.driver.variables:
            for target in variable.targets:
                if target.id == root:target.id = proxy
    tree.update_tag()
    try:
        window.scene = temporary
        proxy.select_set(False)
        fresh.hide_select = False
        fresh.select_set(True)
        window.view_layer.objects.active = fresh
        result = bpy.ops.object.simulation_nodes_cache_bake("EXEC_DEFAULT", False, selected=True)
        if result != {"FINISHED"}:
            raise RuntimeError("原生缓存未同步完成，未发布新时钟")
        points = sorted({fresh[START], (fresh[START] + fresh[END]) // 2, fresh[END]})
        def samples():
            values = []
            for frame in points:
                temporary.frame_set(frame)
                values.append(tuple(_value(fresh, name) for name in (ELAPSED, ACTIVE, EVENT)))
            return values
        frozen = samples()
        # A real packed simulation must ignore a changed live stage input.
        # This edits only the private proxy, never the user's Action or pose.
        proxy.animation_data_clear()
        for phase_value in (2.0, 0.0):
            proxy["阶段"] = phase_value
            proxy.update_tag()
            if samples() != frozen:
                raise RuntimeError("原生缓存仍依赖活动输入，未确认同步packed完成")
        for curve in tree.animation_data.drivers:
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id == proxy:target.id = root
        tree.update_tag()
        if samples() != frozen or any(bake.data_blocks for bake in fresh.modifiers[0].bakes):
            raise RuntimeError("原生缓存仍含来源对象依赖，未发布")
    finally:
        fresh.hide_select = True
        window.scene = previous_scene
        window.view_layer = previous_layer
        if temporary.objects.get(fresh.name) == fresh:
            temporary.collection.objects.unlink(fresh)
            collection.objects.link(fresh)
    for item in (proxy, data, temporary, *private_actions.values()):
        transaction.forget(item)
    bpy.data.objects.remove(proxy, do_unlink=True)
    bpy.data.armatures.remove(data)
    bpy.data.scenes.remove(temporary)
    for copied in private_actions.values():bpy.data.actions.remove(copied)


def cache(scene, root, options, *, clear=False):
    """Copy-on-write native packed bake; old cache survives every failure."""
    ensure_mutation_allowed()
    clock = _scope(scene, root)
    payload = options.get("projectile_cache")
    if not isinstance(payload, dict):
        raise TypeError("投射物缓存请求缺少冻结快照")
    current = float(payload.get("current_frame", math.nan))
    start, end = payload.get("frame_start"), payload.get("frame_end")
    if (not math.isfinite(current) or not isinstance(start, int) or not isinstance(end, int)
            or isinstance(start, bool) or isinstance(end, bool) or start > end or end - start + 1 > MAX_FRAMES):
        raise ValueError("缓存快照帧范围无效")
    signature, animated, ambiguous = _source(root, scene)
    if signature != payload.get("source_signature"):
        raise ValueError("阶段动画在提交后改变，请重新提交缓存请求")
    if ambiguous and not clear:
        raise ValueError("阶段曲线仅存在于静音或禁用动画中；请启用或移除这些阶段曲线后再烘焙")
    epoch = payload.get("manual_epoch")
    if epoch is not None and (animated or not isinstance(epoch, (float, int)) or not math.isfinite(epoch)):
        raise ValueError("手动触发起点快照无效")
    consumers = _consumers(scene, root, clock)
    from . import asset_contracts
    from .asset_rebuild import _value as plain_properties
    from .asset_rebuild import object_mode
    before = plain_properties(dict(root.items()))
    original_frame, original_subframe = scene.frame_current, scene.frame_subframe
    with object_mode(), Creation() as transaction:
        transaction.owner = root
        collection = transaction.track(bpy.data.collections.new("爆炸缓存 · 暂存"))
        scene.collection.children.link(collection)
        fresh = build(transaction, scene, collection, root, start=start, end=end,
                      manual_epoch=(current if payload.get("manual_active") else None) if clear else epoch,
                      baked=not clear)
        fresh.parent = None
        fresh[SIGNATURE] = signature
        try:
            if not clear:
                if not bpy.data.filepath:
                    raise ValueError("请先保存工程，再烘焙原生播放缓存")
                _packed_bake(transaction, scene, root, fresh, collection)
            fresh[CONTRACT] = _tree_contract(fresh, root, scene)
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
        destination = clock.users_collection[0]
        def rollback():
            for socket in consumers:socket.default_value = clock
            for key in tuple(root.keys()):
                if key not in before:del root[key]
            for key, value in before.items():root[key] = value
            if clock.parent != root:clock.parent = root
        transaction.on_rollback(rollback)
        destination.objects.link(fresh)
        collection.objects.unlink(fresh)
        fresh.parent = root
        for socket in consumers:socket.default_value = fresh
        root[CLOCK] = fresh
        clock.parent = None
        root.update_tag()
        asset_contracts.stamp(root)
        transaction.seal()
        # All fallible native evaluation and source validation completed. The
        # old private cache is retired only after the replacement is sealed.
        bpy.data.objects.remove(clock, do_unlink=True)
        transaction.forget(collection)
        bpy.data.collections.remove(collection)
        transaction.commit()
    return {"message": "已清除缓存；可重新编辑阶段并顺播预览" if clear else "已烘焙原生阶段播放缓存；缓存范围内支持跳帧与冷开"}
