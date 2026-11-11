"""Explicit celestial cloud receivers and blockers, matching the source modes."""

import bpy

from ...util.ownership import require_owned
from .sky_repair import roles_for


def _state(light, group):
    return (light.light_linking.blocker_collection,
            tuple(entry.light_linking.link_state for entry in group.collection_children))


def plan(root, controller, settings):
    from . import sky

    roles = roles_for(root, controller)
    lights = tuple(obj for obj in root.all_objects if obj.type == "LIGHT")
    sun, cloud = roles["sun_mesh"].parent, roles["cloud_light"]
    remaining = set(lights) - {sun, cloud}
    if len(lights) != 3 or sun not in lights or cloud not in lights or sun == cloud or len(remaining) != 1:
        raise ValueError("天空灯角色无法确认，未重接用户光照")
    moon = remaining.pop()
    layers = {description["collection"] for description in root[sky.CLOUD_TEMPLATES].values()}
    result = []
    groups = set()
    for key, light in (("sun", sun), ("moon", moon)):
        group = light.light_linking.receiver_collection
        if (not isinstance(group, bpy.types.Collection) or set(group.children) != layers or group.objects
                or len(group.collection_children) != len(layers)):
            raise ValueError("太阳／月光云层链接集合已自定义，未重接其它光照集合")
        require_owned(root, (light, group, *layers))
        if light.library or light.override_library or light.light_linking.is_property_readonly("blocker_collection"):
            raise ValueError("天空灯阴影链接只读，未改写")
        if group.library or group.override_library or any(
            entry.light_linking.is_property_readonly("link_state") for entry in group.collection_children
        ):
            raise ValueError("天空云层链接状态只读，未改写")
        before = _state(light, group)
        if before[0] not in {None, group}:
            raise ValueError("太阳／月光投影集合已自定义，未覆盖用户绑定")
        # Previous Betas always excluded receivers and removed the blocker
        # pointer in shadow mode. Recognize that exact generated layout only.
        version = root.get(sky.VERSION)
        current_shadow = bool(controller[sky.PARAMETERS["cloud_enabled"][2]] and controller["云阴影"])
        expected = "EXCLUDE" if version in {"2", "3"} or not current_shadow else "INCLUDE"
        if any(state != expected for state in before[1]):
            raise ValueError("太阳／月光云层链接状态已自定义，未覆盖用户受光设置")
        # local.31 shadow mode lights the cloud bodies with Sun/Moon directly.
        # Both native link pointers stay on the same authored cloud collection.
        state = "INCLUDE" if settings["cloud_enabled"] and settings["cloud_shadow"] else "EXCLUDE"
        after = (group, (state,) * len(before[1]))
        result.append((key, light, before, after))
        groups.add(group)
    if len(groups) != 1 or cloud.light_linking.receiver_collection in groups or cloud.light_linking.blocker_collection in groups:
        raise ValueError("日月与专属云灯的链接边界已改变，未修改共享云层状态")
    domain = sky._dependency_domain(root)
    scenes = tuple(controller.users_scene)
    if len(scenes) != 1 or root not in tuple(scenes[0].collection.children_recursive):
        raise ValueError("天空控制器不再属于唯一场景，未改变共享链接")
    # Blender includes the owning Scene in link-collection user_map results.
    domain.add(scenes[0])
    users = bpy.data.user_map(subset=tuple(groups))
    if any(user not in domain for references in users.values() for user in references):
        raise ValueError("云层链接集合被其它数据引用，未改变其它灯的受光范围")
    return result


def snapshot(root, controller, settings):
    return {key: {"before": {"blocker": before[0] is not None, "layers": before[1]},
                  "after": {"blocker": after[0] is not None, "layers": after[1]}}
            for key, _light, before, after in plan(root, controller, settings)}


def apply(links, *, restore=False):
    for _key, light, before, after in links:
        blocker, states = before if restore else after
        light.light_linking.blocker_collection = blocker
        group = light.light_linking.receiver_collection
        for entry, state in zip(group.collection_children, states, strict=True):
            entry.light_linking.link_state = state
        group.update_tag()
        light.update_tag()
