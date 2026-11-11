"""Recognize and explicitly upgrade owned Beta bindings without replacing assets."""

import ast
import json

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.animation import action_fcurves
from ...util.ownership import REGISTRY, owned_ids, require_owned, seal_owned
from ...util.resources import ASSET_ID, INSTANCE, VERSION
from . import sky_schema

ROLES = "oxal_mc_studio_sky_roles"


def _formula(expression):
    try:
        return ast.dump(ast.parse(expression, mode="eval"))
    except (SyntaxError, ValueError):
        return None


def curve(owner, path, index=None):
    animation = owner.animation_data
    found = [c for c in animation.drivers if c.data_path == path and (index is None or c.array_index == index)] if animation else []
    if len(found) > 1:
        raise ValueError("天空包含重复驱动，未覆盖用户绑定")
    return found[0] if found else None


def matches(owner, path, expression, variables, index=None):
    item = curve(owner, path, index)
    if item is None or item.driver.type != "SCRIPTED" or item.mute or item.driver.use_self:
        return False
    # Native driver_add and the immutable source use this identity generator.
    # Anything which changes the evaluated curve is a user conflict.
    if item.modifiers and not (len(item.modifiers) == 1 and all(
        modifier.type == "GENERATOR" and modifier.mode == "POLYNOMIAL"
        and modifier.poly_order == 1 and tuple(modifier.coefficients) == (0.0, 1.0)
        and not modifier.use_additive and not modifier.use_restricted_range
        and not modifier.mute and modifier.influence == 1
        for modifier in item.modifiers
    )):
        return False
    driver = item.driver
    expected = {name: (target, data_path) for name, target, data_path in variables}
    actual = {v.name: v for v in driver.variables}
    return (
        _formula(driver.expression) == _formula(expression)
        and set(actual) == set(expected)
        and all(actual[name].type == "SINGLE_PROP" and actual[name].targets[0].id == target
                and actual[name].targets[0].data_path == data_path
                for name, (target, data_path) in expected.items())
    )


def _no_animation(owner, paths):
    animation = owner.animation_data
    if animation is None:
        return
    bindings = [animation] if animation.action else []
    strips = [strip for track in animation.nla_tracks for strip in track.strips]
    while strips:
        strip = strips.pop()
        if strip.action:
            bindings.append(strip)
        strips.extend(getattr(strip, "strips", ()))
    for binding in bindings:
        for item in action_fcurves(binding.action, slot=getattr(binding, "action_slot", None)):
            if item.data_path in paths:
                raise ValueError("待修复的模式或可见性通道已有动画，未改写：" + item.data_path)


def initial_roles(root, controller):
    """Names are source-entry contracts here, before publication or user edits."""
    from . import sky

    objects = tuple(root.all_objects)
    def one(predicate, caption):
        found = [obj for obj in objects if predicate(obj)]
        if len(found) != 1:
            raise ValueError("天空资源缺少唯一角色：" + caption)
        return found[0]
    return {
        "controller": controller,
        "world": root[sky.WORLD_REFERENCE],
        "cloud_light": one(lambda o: o.type == "LIGHT" and o.get("oxal_cloud_light"), "云光照"),
        "sun_mesh": one(lambda o: o.type == "MESH" and o.name.split(".")[0] == "Sun", "太阳存档平面"),
        "moon_mesh": one(lambda o: o.type == "MESH" and o.name.split(".")[0] == "MC月亮", "月亮存档平面"),
    }


def roles_for(root, controller):
    from . import sky

    saved = root.get(ROLES)
    if saved is not None:
        roles = dict(saved.items())
        if set(roles) != {"controller", "world", "cloud_light", "sun_mesh", "moon_mesh"}:
            raise ValueError("天空角色登记已损坏，未按显示名接管")
    else:
        # Beta.2 did not record object roles. Identify only its exact generated
        # visibility bindings and native parent relationships, even after rename.
        candidates = [obj for obj in root.all_objects if obj.type == "MESH" and all(
            matches(obj, flag, "not enabled", [("enabled", controller, '["使用3D日月贴图"]')])
            for flag in ("hide_render", "hide_viewport"))]
        suns = [obj for obj in candidates if obj.parent and obj.parent.type == "LIGHT"]
        moons = [obj for obj in candidates if obj.parent and obj.parent.type == "EMPTY"]
        lights = [obj for obj in root.all_objects if obj.type == "LIGHT" and obj.get("oxal_cloud_light")]
        if len(suns) != 1 or len(moons) != 1 or len(lights) != 1:
            raise ValueError("已有 Beta 日月或云灯角色不唯一，未覆盖用户数据")
        roles = {"controller": controller, "world": root.get(sky.WORLD_REFERENCE),
                 "cloud_light": lights[0], "sun_mesh": suns[0], "moon_mesh": moons[0]}
    if roles["controller"] != controller or roles["world"] != root.get(sky.WORLD_REFERENCE):
        raise ValueError("天空角色引用已被替换，未接管其它实例")
    require_owned(root, roles.values())
    require_owned(root, (roles["cloud_light"].data,))
    return roles


def world_contract(world, controller):
    """The original 3D option selects packed World images, not distant meshes."""
    tree = world.node_tree
    if tree is None:
        raise ValueError("天空 World 节点已移除，未重建用户材质")
    switches = [node for node in tree.nodes if node.type == "VALUE" and matches(
        tree, node.outputs[0].path_from_id("default_value"), "use3d",
        [("use3d", controller, '["使用3D日月贴图"]')])]
    if len(switches) != 1:
        raise ValueError("World 的 3D 日月选择绑定已修改，未覆盖用户材质")
    switch = switches[0]
    mixes = [link.to_node for link in switch.outputs[0].links if link.to_socket.identifier == "Fac"]
    if len(mixes) != 4 or any(node.type != "MIX_RGB" for node in mixes):
        raise ValueError("World 日月颜色／透明度选择结构已修改")
    images = set()
    for node in mixes:
        for name in ("Color1", "Color2"):
            links = tuple(node.inputs[name].links)
            if len(links) != 1 or links[0].from_node.type != "TEX_IMAGE":
                raise ValueError("World 日月贴图连接已修改，未覆盖用户绑定")
            image = links[0].from_node.image
            if image is None or image.size[0] < 1 or image.size[1] < 1:
                raise ValueError("World 日月贴图缺失或无法解码")
            images.add(image)
    outputs = [node for node in tree.nodes if node.type == "OUTPUT_WORLD" and node.is_active_output]
    if len(outputs) != 1:
        raise ValueError("天空 World 缺少唯一活动输出")
    pending, reachable = [outputs[0]], set()
    while pending:
        node = pending.pop()
        if node in reachable:
            continue
        reachable.add(node)
        pending.extend(link.from_node for socket in node.inputs for link in socket.links)
    if switch not in reachable or not set(mixes) <= reachable:
        raise ValueError("日月贴图未连接至当前 World 输出，未重接用户图")
    return switch, tuple(images)


def _legacy_contracts(controller, light):
    def variables(*pairs):
        return [(name, controller, f'[{json.dumps(prop, ensure_ascii=False)}]') for name, prop in pairs]
    return [
        (controller, '["oxal_mc_studio_cloud_day"]', None,
         "min(1,max(0,(t-r)*u/max(.001,e-r)),max(0,(n-t)*v/max(.001,n-d)))",
         variables(("t", "计算时间"), ("r", "日出开始时间"), ("e", "日出完成时间"),
                   ("d", "日落开始时间"), ("n", "夜晚完成过渡时间"),
                   ("u", "云光照日出过渡倍率"), ("v", "云光照日落过渡倍率"))),
        (light.data, "energy", None,
         "(day*w+night*(1-w))*(o or a) if automatic else day*(o or a)",
         variables(("day", "云光照日间强度"), ("night", "云光照夜间强度"),
                   ("w", "oxal_mc_studio_cloud_day"), ("o", "云光照启用"),
                   ("a", "Eevee高级云光照"), ("automatic", "云光照自动过渡"))),
        (light, "rotation_euler", 1,
         "(day*w+night*(1-w))*pi/180 if automatic else day*pi/180",
         variables(("day", "云光照日间角度"), ("night", "云光照夜间角度"),
                   ("w", "oxal_mc_studio_cloud_day"), ("automatic", "云光照自动过渡"))),
    ]


def _source_angle_matches(controller, light):
    """Recognize the immutable X binding by target semantics, not symbols."""
    from . import sky

    item = curve(light, "rotation_euler", 0)
    if item is None:
        return False
    expression, variables = sky.cloud_angle_contract(controller, automatic=False)
    expected = {path: name for name, _owner, path in variables}
    actual = {}
    for variable in item.driver.variables:
        target = variable.targets[0]
        if variable.type != "SINGLE_PROP" or target.id != controller or target.data_path not in expected:
            return False
        if target.data_path in actual:
            return False
        actual[target.data_path] = variable.name
    if set(actual) != set(expected):
        return False
    aliases = {expected[path]: actual[path] for path in expected}
    tree = ast.parse(expression, mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in aliases:
            node.id = aliases[node.id]
    return matches(light, "rotation_euler", ast.unparse(tree),
                   [(actual[path], controller, path) for path in expected], 0)


def preflight(scene, root, controller):
    from . import sky, sky_celestial, sky_inputs, sky_modes, sky_state

    roles = roles_for(root, controller)
    input_targets,_ = sky._scene_input_bindings(scene,controller)
    sky_inputs.migration_plan(scene,input_targets)
    if any(item.library or item.override_library for item in (root, *owned_ids(root)) if VERSION in item):
        raise ValueError("天空包含链接或库覆盖数据，未改写版本及原生绑定")
    world_contract(roles["world"], controller)
    sky_celestial.plan(roles["world"], controller, root=root, scene=scene)
    light = roles["cloud_light"]
    _no_animation(light, {"rotation_euler"})
    _no_animation(light.data, {"energy", "use_shadow"})
    domain = sky._dependency_domain(root)
    if any(user not in domain for user in bpy.data.user_map(subset=(light.data,)).get(light.data, ())):
        raise ValueError("云灯数据被外部共享，未修改其它实例")
    sky._require_cloud_templates(root)
    _no_animation(controller, {f'[{json.dumps(sky_schema.PARAMETERS[name][2], ensure_ascii=False)}]'
                               for name in sky_schema.CLOUD_STRUCTURE_KEYS})
    if root[VERSION] == "2":
        from .sky_contracts import validate

        validate(root, controller, roles)
        for owner, path, index, expression, variables in _legacy_contracts(controller, light):
            if not matches(owner, path, expression, variables, index):
                raise ValueError("已有 Beta 云光照驱动被自定义，未覆盖：" + path)
        mode_paths = {f'{sky_state.VALUES}.{name}' for name in sky_schema.CLOUD_STRUCTURE_KEYS}
        mode_paths.update(f'{sky_state.SLOTS}[{index}].values.cloud_light' for index in range(sky_state.SLOT_COUNT))
        mode_paths.update(sky_inputs.aliases()[path] for path in tuple(mode_paths))
        _no_animation(scene, mode_paths)
        expression = "base"
        variables = [("base", scene, f"{sky_state.VALUES}.cloud_light"),
                     ("s", controller, '["oxal_mc_studio_selected_slot"]')]
        for index in reversed(range(sky_state.SLOT_COUNT)):
            variables.append((f"v{index}", scene, f"{sky_state.SLOTS}[{index}].values.cloud_light"))
            expression = f"v{index} if s=={index + 1} else {expression}"
        native_variables = [(name,owner,sky_inputs.aliases().get(path,path)) for name,owner,path in variables]
        if not matches(controller, '["云光照启用"]', expression, variables) and not matches(controller, '["云光照启用"]', expression, native_variables):
            raise ValueError("已有 Beta 普通云模式绑定被修改，未移除用户驱动")
    else:
        if curve(controller, '["云光照启用"]') is not None:
            raise ValueError("当前普通云模式已有用户驱动，未覆盖")
        for owner, path, index, expression, variables in sky.cloud_lighting_contracts(
            controller, light, angle_axis=1 if root[VERSION] == "3" else 0
        ):
            if not matches(owner, path, expression, variables, index):
                raise ValueError("当前云光照驱动被修改，未覆盖：" + path)
    if root[VERSION] in {"2", "3"} and not _source_angle_matches(controller, light):
        raise ValueError("已有 Beta 云灯原始 X 角度绑定已修改，未覆盖用户受光设置")
    before = sky_modes.actual_structure(controller)
    after = sky_modes.normalize_modes(before)
    from .sky_linking import plan

    plan(root, controller, after)
    def casters(settings):
        return settings["cloud_enabled"] and (settings["cloud_shadow"] or (
            settings["cloud_advanced"] if settings["cloud_shadow_auto"] else settings["cloud_light_shadow"]))
    if casters(before) != casters(after):
        raise ValueError("已有云模式冲突会改变投射器结构，未移除带材质或绑定的对象")
    return roles


def _capture(owner, path, index=None):
    item = curve(owner, path, index)
    if item is None:
        return None
    driver = item.driver
    return {
        "index": item.array_index, "expression": driver.expression, "type": driver.type,
        "mute": item.mute, "use_self": driver.use_self,
        "variables": [(v.name, v.type, [(t.id_type, t.id, t.data_path, t.bone_target,
                                       t.transform_type, t.transform_space) for t in v.targets])
                      for v in driver.variables],
    }


def _restore(owner, path, index, saved):
    item = curve(owner, path, index)
    if item is not None:
        owner.animation_data.drivers.remove(item)
    if saved is None:
        return
    item = owner.driver_add(path) if index is None else owner.driver_add(path, saved["index"])
    item.mute, item.driver.type, item.driver.expression = saved["mute"], saved["type"], saved["expression"]
    item.driver.use_self = saved["use_self"]
    for name, kind, targets in saved["variables"]:
        variable = item.driver.variables.new()
        variable.name, variable.type = name, kind
        for target, values in zip(variable.targets, targets, strict=True):
            id_type, identity, data_path, bone, transform, space = values
            if kind == "SINGLE_PROP":
                target.id_type = id_type
            target.id, target.data_path = identity, data_path
            if kind == "TRANSFORMS":
                target.bone_target, target.transform_type, target.transform_space = bone, transform, space


def apply(scene, root, controller, settings):
    from . import sky, sky_celestial, sky_inputs, sky_linking, sky_state

    ensure_mutation_allowed()
    roles = preflight(scene, root, controller)
    inputs,_ = sky._scene_input_bindings(scene,controller)
    if root[VERSION] == "2":
        removed = {sky_inputs.path("cloud_light",index) for index in (None,*range(sky_state.SLOT_COUNT))}
        inputs = [(target,path) for target,path in inputs if path not in removed]
    migration = sky_inputs.migration_plan(scene,sky._scene_input_bindings(scene,controller)[0])
    previous_inputs = [(target,target.data_path) for target,_ in inputs]
    stored_inputs = sky_state.capture_inputs(scene)
    light = roles["cloud_light"]
    links = sky_linking.plan(root, controller, settings)
    paths = [(owner, path, index) for owner, path, index, _, _ in sky.cloud_lighting_contracts(controller, light)]
    previous_beta = root[VERSION] in {"2", "3"}
    if previous_beta:
        paths.append((light, "rotation_euler", 1))
    previous_y_angle = light.rotation_euler[1]
    paths.append((controller, '["云光照启用"]', None))
    if root[VERSION] == "2":
        paths.extend((obj, flag, None) for obj in (roles["sun_mesh"], roles["moon_mesh"])
                     for flag in ("hide_render", "hide_viewport"))
    snapshots = [(owner, path, index, _capture(owner, path, index)) for owner, path, index in paths]
    properties = {name: (name in controller, controller.get(name)) for name in (
        "oxal_mc_studio_cloud_day", "oxal_mc_studio_cloud_night",
        *(sky_schema.PARAMETERS[key][2] for key in sky_schema.CLOUD_STRUCTURE_KEYS))}
    hidden = [(obj, obj.hide_render, obj.hide_viewport) for obj in (roles["sun_mesh"], roles["moon_mesh"])]
    versions = [(item, item.get(VERSION)) for item in (root, *owned_ids(root)) if VERSION in item]
    old_roles = dict(root[ROLES].items()) if ROLES in root else None
    old_structure = root.get("oxal_mc_studio_cloud_structure")
    registry = dict(root[REGISTRY].items())
    celestial_images = None
    try:
        sky_state.materialize_inputs(scene)
        sky_inputs.apply_migration(migration)
        for target,path in inputs:
            target.data_path = path
        if root[VERSION] == "2":
            regular = curve(controller, '["云光照启用"]')
            if regular is not None:
                controller.animation_data.drivers.remove(regular)
        for name, value in settings.items():
            controller[sky_schema.PARAMETERS[name][2]] = value
        if root[VERSION] == "2":
            for obj, _, _ in hidden:
                for flag in ("hide_render", "hide_viewport"):
                    generated = curve(obj, flag)
                    if generated is not None:
                        obj.animation_data.drivers.remove(generated)
                obj.hide_render = obj.hide_viewport = True
        if previous_beta:
            generated = curve(light, "rotation_euler", 1)
            if generated is not None:
                light.animation_data.drivers.remove(generated)
            light.rotation_euler[1] = 0.0
        sky.install_cloud_lighting(controller, light)
        sky_linking.apply(links)
        celestial_images = sky_celestial.Replacement(
            sky_celestial.plan(roles["world"], controller, root=root, scene=scene)
        ).apply()
        for image in celestial_images.images:
            image[ASSET_ID], image[VERSION], image[INSTANCE] = sky.RESOURCE_ID, sky.RESOURCE_VERSION, root[INSTANCE]
        if celestial_images.images:
            seal_owned(root, celestial_images.images)
        root[ROLES] = roles
        root["oxal_mc_studio_cloud_structure"] = json.dumps(settings)
        for item, _ in versions:
            item[VERSION] = sky.RESOURCE_VERSION
        controller.update_tag()
        sky.resolve_view_layer(scene).update()
    except Exception:
        root[REGISTRY] = registry
        if celestial_images is not None:
            celestial_images.rollback()
        sky_inputs.apply_migration(migration,restore=True)
        for target,path in previous_inputs:
            target.data_path = path
        sky_state.restore_inputs(scene,stored_inputs)
        sky_linking.apply(links, restore=True)
        for owner, path, index, saved in reversed(snapshots):
            _restore(owner, path, index, saved)
        light.rotation_euler[1] = previous_y_angle
        for name, (present, value) in properties.items():
            if present:
                controller[name] = value
            elif name in controller:
                del controller[name]
        for obj, render, viewport in hidden:
            obj.hide_render, obj.hide_viewport = render, viewport
        for item, version in versions:
            item[VERSION] = version
        if old_roles is None:
            if ROLES in root:
                del root[ROLES]
        else:
            root[ROLES] = old_roles
        if old_structure is None:
            if "oxal_mc_studio_cloud_structure" in root:
                del root["oxal_mc_studio_cloud_structure"]
        else:
            root["oxal_mc_studio_cloud_structure"] = old_structure
        raise
    return "已修复已有 Beta 天空并恢复 193 原始 3D 日月图；动画、材质与云对象保持"
