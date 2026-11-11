"""Prebuilt standalone effects, animated entirely by native scalar drivers.

Particle texture profiles follow the pinned 26.4 client resources. In particular
end rods use glitter_7..0, smoke uses generic_7..0, and enchanting uses sga glyphs.
No mesh, particle or material is allocated by frame/render handlers.
"""

import json
import math
from typing import Any, cast

import bpy
from bpy.types import Mesh

from ...core.runtime import ensure_mutation_allowed
from .asset_geometry import driver, initialize_driver, mesh_object

CONTROL_LABELS = {
    "banner_motion": "旗帜动画",
    "dragon_motion": "龙首动画",
    "摆动幅度": "摆动幅度",
    "自动开合": "自动开合",
    "最大开盖角度": "最大开盖角度",
    "auto_paddles": "自动划桨",
    "划桨速度": "划桨速度",
    "左桨角度": "左桨角度",
    "右桨角度": "右桨角度",
    "bed_color": "床颜色",
    "body_visible": "本体显示",
    "line_cast": "鱼线与鱼钩",
    "rope_visible": "绳索显示",
    "fence_variant": "拴绳变体",
    "拴绳粗细": "拴绳粗细",
    "鱼线粗细": "鱼线粗细",
    "jaw_open": "龙首下颚",
    "particles_enabled": "粒子开关",
    "effect_scale": "粒子大小",
    "effect_lifetime": "粒子周期（秒）",
    "beam_enabled": "信标光束",
    "beam_height": "光束高度",
    "beam_radius": "光束宽度",
    "beam_brightness": "光束亮度",
    "book_open": "书本开合",
    "book_turn": "书本朝向",
    "book_speed": "书页速度",
    "book_hover": "书本悬浮",
    "runes_enabled": "附魔文字",
    "rune_speed": "文字速度",
    "crystal_spin": "水晶旋转",
    "crystal_bob": "水晶浮动",
    "crystal_speed": "水晶速度",
    "open": "开合",
    "progress": "活塞伸出",
}


def control(root, key, value, minimum=None, maximum=None):
    root[key] = value
    settings = {"description": CONTROL_LABELS.get(key, key)}
    if minimum is not None:
        settings["min"] = settings["soft_min"] = minimum
    if maximum is not None:
        settings["max"] = settings["soft_max"] = maximum
    root.id_properties_ui(key).update(**settings)


def native(owner, path, expression, root, variables, index=-1):
    timed = "frame" in expression
    if timed:
        expression = expression.replace("frame", "((frame-first)*base/max(fps,1))")
    if len(expression.encode("utf8")) > 255:
        raise ValueError("原生特效驱动表达式过长")
    curve = owner.driver_add(path) if index < 0 else owner.driver_add(path, index)
    initialize_driver(curve)
    curve.driver.type = "SCRIPTED"
    curve.driver.expression = expression
    for name, prop in variables:
        variable = curve.driver.variables.new()
        variable.name = name
        variable.type = "SINGLE_PROP"
        variable.targets[0].id = root
        variable.targets[0].data_path = f'["{prop}"]'
    if timed:
        scene = root.get("oxal_mc_studio_effect_scene") or bpy.context.scene
        for name, prop in (
            ("first", "frame_start"),
            ("fps", "render.fps"),
            ("base", "render.fps_base"),
        ):
            variable = curve.driver.variables.new()
            variable.name = name
            variable.type = "SINGLE_PROP"
            variable.targets[0].id_type = "SCENE"
            variable.targets[0].id = scene
            variable.targets[0].data_path = prop
    return curve


def _surface(
    transaction, source, name, root, strength=1.0, strength_prop="effect_strength"
):
    mat = transaction.track(source.copy())
    mat.name = name
    tree = cast(Any, mat.node_tree)
    image = tree.nodes.get("OXAL Source Texture").image
    tint_node = tree.nodes.get("OXAL Tint")
    tint = tuple(tint_node.inputs[2].default_value) if tint_node else (1, 1, 1, 1)
    tree.nodes.clear()
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.name = "OXAL Source Texture"
    texture.image = image
    texture.interpolation = "Closest"
    texture.extension = "REPEAT"
    multiply = tree.nodes.new("ShaderNodeMixRGB")
    multiply.name = "OXAL Tint"
    multiply.blend_type = "MULTIPLY"
    multiply.inputs[0].default_value = 1.0
    multiply.inputs[2].default_value = tint
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs[1].default_value = strength
    if strength_prop:
        driver(
            emission.inputs[1],
            "default_value",
            f"{strength}*max(v,0)",
            root,
            strength_prop,
        )
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    alpha = tree.nodes.new("ShaderNodeMath")
    alpha.name = "OXAL Effect Alpha"
    alpha.operation = "MULTIPLY"
    alpha.inputs[1].default_value = 1.0
    mix = tree.nodes.new("ShaderNodeMixShader")
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(texture.outputs["Color"], multiply.inputs[1])
    tree.links.new(multiply.outputs[0], emission.inputs[0])
    tree.links.new(texture.outputs["Alpha"], alpha.inputs[0])
    tree.links.new(alpha.outputs[0], mix.inputs[0])
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], output.inputs["Surface"])
    mat.surface_render_method = "DITHERED"
    return mat, alpha.inputs[1]


def _quad(transaction, collection, parent, name, material):
    obj = mesh_object(
        transaction,
        name,
        [
            (-0.5, 0, -0.5),
            (0.5, 0, -0.5),
            (0.5, 0, 0.5),
            (-0.5, 0, 0.5),
            (0, -0.5, -0.5),
            (0, 0.5, -0.5),
            (0, 0.5, 0.5),
            (0, -0.5, 0.5),
        ],
        [(0, 1, 2, 3), (4, 5, 6, 7)],
        [[(0, 0), (1, 0), (1, 1), (0, 1)]] * 2,
        [material],
        [0, 0],
    )
    collection.objects.link(obj)
    obj.parent = parent
    obj["oxal_mc_studio_effect_geometry"] = True
    return obj


def point_light(
    transaction, collection, root, *, lit=True, color=(1, 0.6, 0.25), height=0.8
):
    control(root, "light_enabled", bool(lit))
    control(root, "effect_strength", 1.0, 0.0, 100.0)
    data = transaction.track(bpy.data.lights.new("OXAL asset light", "POINT"))
    data.color = color
    data.shadow_soft_size = 0.2
    light = transaction.track(bpy.data.objects.new("OXAL asset light", data))
    collection.objects.link(light)
    light.parent = root
    light.location.z = height
    physical = [
        obj
        for obj in collection.objects
        if obj.type == "MESH" and not obj.get("oxal_mc_studio_effect_geometry")
    ]
    if physical:
        blockers = transaction.track(
            bpy.data.collections.new("OXAL emitter self-shadow exclusions")
        )
        for obj in physical:
            blockers.objects.link(obj)
            blockers.collection_objects[-1].light_linking.link_state = "EXCLUDE"
        light.light_linking.blocker_collection = blockers
    native(
        data,
        "energy",
        "40*max(power,0)*enabled",
        root,
        [("power", "effect_strength"), ("enabled", "light_enabled")],
    )


def body_emission(root, texture_keys):
    """Let luminous source surfaces glow, independently of their light object."""
    seen = set()
    for obj in root.children_recursive:
        if obj.type != "MESH" or obj.get("oxal_mc_studio_effect_geometry"):
            continue
        for material in obj.data.materials:
            if (
                material is None
                or material in seen
                or material.get("oxal_mc_studio_texture_key") not in texture_keys
            ):
                continue
            seen.add(material)
            tree = cast(Any, material.node_tree)
            if tree is None:
                continue
            shader = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if shader is None:
                continue
            color = shader.inputs["Base Color"]
            if color.is_linked:
                tree.links.new(
                    color.links[0].from_socket, shader.inputs["Emission Color"]
                )
            else:
                shader.inputs["Emission Color"].default_value = color.default_value
            native(
                shader.inputs["Emission Strength"],
                "default_value",
                "3*max(power,0)*enabled",
                root,
                [("power", "effect_strength"), ("enabled", "light_enabled")],
            )


def _particle_profile(
    transaction,
    collection,
    root,
    material_for,
    keys,
    *,
    count,
    flag,
    origin=(0, 0, 0.8),
    spread=0.1,
    rise=1.0,
    size=0.12,
    emission=1.0,
    tint_color=None,
):
    materials = [
        material_for("minecraft:particle/" + name)
        if tint_color is None
        else material_for(
            "minecraft:particle/" + name,
            0,
            {"tints": [{"type": "minecraft:constant", "value": tint_color}]},
        )
        for name in keys
    ]
    for i in range(count):
        obj = _quad(
            transaction,
            collection,
            root,
            "OXAL particle",
            materials[i % len(materials)],
        )
        phase = i / count
        obj["age"] = phase
        clock = f"frame*speed/max(life,.05)+{phase}"
        native(
            obj,
            '["age"]',
            f"({clock})-floor({clock})",
            root,
            [("speed", "effect_speed"), ("life", "effect_lifetime")],
        )
        mat, alpha = _surface(
            transaction,
            materials[i % len(materials)],
            "OXAL particle sprite",
            root,
            emission,
        )
        cast(Mesh, obj.data).materials[0] = mat
        native(
            alpha, "default_value", "enabled*max(0,1-age)", root, [("enabled", flag)]
        )
        age_variable = alpha.id_data.animation_data.drivers[-1].driver.variables.new()
        age_variable.name = "age"
        age_variable.type = "SINGLE_PROP"
        age_variable.targets[0].id = obj
        age_variable.targets[0].data_path = '["age"]'
        angle = i * 2.39996322973
        driver(obj, "location", f"{origin[0]}+{spread}*sin({angle}+v*3)", obj, "age", 0)
        driver(obj, "location", f"{origin[1]}+{spread}*cos({angle}+v*2)", obj, "age", 1)
        driver(obj, "location", f"{origin[2]}+{rise}*v", obj, "age", 2)
        for axis in range(3):
            native(
                obj, "scale", f"{size}*scale", root, [("scale", "effect_scale")], axis
            )


def _beacon(transaction, collection, root, material_for):
    control(root, "beam_enabled", True)
    control(root, "beam_height", 32.0, 0.1, 1024.0)
    control(root, "beam_radius", 0.2, 0.01, 4.0)
    control(root, "beam_brightness", 3.0, 0.0, 100.0)
    control(root, "effect_speed", 1.0, 0.0, 100.0)
    source = material_for("minecraft:entity/beacon/beacon_beam")
    for layer, width, opacity in (("core", 1.0, 0.9), ("halo", 1.6, 0.2)):
        mat, alpha = _surface(
            transaction, source, "OXAL beacon " + layer, root, 1.0, "beam_brightness"
        )
        driver(alpha, "default_value", f"{opacity}*v", root, "beam_enabled")
        a = 0.5 * width
        vertices = [
            (-a, -a, 0),
            (a, -a, 0),
            (a, a, 0),
            (-a, a, 0),
            (-a, -a, 1),
            (a, -a, 1),
            (a, a, 1),
            (-a, a, 1),
        ]
        obj = mesh_object(
            transaction,
            "OXAL beacon " + layer,
            vertices,
            [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)],
            [[(0, 0), (1, 0), (1, 8), (0, 8)]] * 4,
            [mat],
            [0] * 4,
        )
        collection.objects.link(obj)
        obj.parent = root
        obj["oxal_mc_studio_effect_geometry"] = True
        obj.location.z = 1.0
        for axis in (0, 1):
            driver(obj, "scale", "v", root, "beam_radius", axis)
        driver(obj, "scale", "v", root, "beam_height", 2)
        native(
            obj,
            "rotation_euler",
            "frame*speed*.3",
            root,
            [("speed", "effect_speed")],
            2,
        )
        tree = cast(Any, mat.node_tree)
        uv = tree.nodes.new("ShaderNodeTexCoord")
        offset = tree.nodes.new("ShaderNodeVectorMath")
        offset.operation = "ADD"
        tree.links.new(uv.outputs["UV"], offset.inputs[0])
        native(
            offset.inputs[1],
            "default_value",
            "frame*speed*.2",
            root,
            [("speed", "effect_speed")],
            1,
        )
        tree.links.new(
            offset.outputs[0], tree.nodes["OXAL Source Texture"].inputs["Vector"]
        )
    point_light(transaction, collection, root, color=(0.7, 0.9, 1.0))
    body_emission(root, {"minecraft:block/beacon"})


def _book(transaction, collection, root, material_for):
    from .asset_special import entity

    control(root, "book_open", 1.0, 0.0, 1.0)
    control(root, "book_turn", 0.0, -math.pi, math.pi)
    control(root, "book_speed", 1.0, 0.0, 100.0)
    control(root, "book_hover", 0.05, 0.0, 2.0)
    control(root, "runes_enabled", True)
    control(root, "rune_speed", 1.0, 0.0, 100.0)
    control(root, "effect_strength", 1.0, 0.0, 100.0)
    orbit = transaction.track(bpy.data.objects.new("OXAL enchanting book", None))
    collection.objects.link(orbit)
    orbit.parent = root
    native(
        orbit,
        "location",
        "1.18+amplitude*sin(frame*speed*.8)",
        root,
        [("amplitude", "book_hover"), ("speed", "book_speed")],
        2,
    )
    driver(orbit, "rotation_euler", "v", root, "book_turn", 2)
    parts = entity(
        transaction,
        collection,
        orbit,
        "book",
        material_for("minecraft:entity/enchantment/enchanting_table_book"),
        upright=1,
    )
    parts["book"].rotation_euler.x = math.radians(75)
    for label, expression in (
        ("left_lid", "(1-v)*pi/2"),
        ("right_lid", "-(1-v)*pi/2"),
        ("left_pages", "pi+(1-v)*pi/2"),
        ("right_pages", "-(1-v)*pi/2"),
    ):
        driver(parts[label], "rotation_euler", expression, root, "book_open", 2)
    for label, phase in (("flip_page1", 0), ("flip_page2", 1.1)):
        native(
            parts[label],
            "rotation_euler",
            f"open*pi*(.5+.5*sin(frame*speed*1.2+{phase}))",
            root,
            [("open", "book_open"), ("speed", "book_speed")],
            2,
        )
    for i in range(16):
        key = "minecraft:particle/sga_" + chr(ord("a") + i)
        source = material_for(key)
        obj = _quad(transaction, collection, root, "OXAL enchanting rune", source)
        obj["age"] = i / 16
        clock = f"frame*speed/4+{i / 16}"
        native(
            obj,
            '["age"]',
            f"({clock})-floor({clock})",
            root,
            [("speed", "rune_speed")],
        )
        angle = i * 2.39996323
        driver(obj, "location", f"2*cos({angle})*(1-v)", obj, "age", 0)
        driver(obj, "location", f"2*sin({angle})*(1-v)", obj, "age", 1)
        driver(obj, "location", ".3+v", obj, "age", 2)
        obj.scale = (0.12, 0.12, 0.12)
        mat, alpha = _surface(transaction, source, "OXAL rune sprite", root, 2.0)
        cast(Mesh, obj.data).materials[0] = mat
        driver(alpha, "default_value", "v", root, "runes_enabled")


def _crystal(transaction, collection, root, material_for):
    from .asset_special import entity

    for obj in tuple(root.children):
        if obj.type == "MESH":
            obj.hide_render = obj.hide_viewport = True
            obj["oxal_mc_studio_replaced_geometry"] = True
    control(root, "crystal_spin", True)
    control(root, "crystal_bob", 0.2, 0.0, 2.0)
    control(root, "crystal_speed", 1.0, 0.0, 100.0)
    parts = entity(
        transaction,
        collection,
        root,
        "crystal",
        material_for("minecraft:entity/end_crystal/end_crystal"),
        upright=1,
    )
    outer = parts["outer_glass"]
    outer.rotation_euler.x = math.pi / 4
    native(
        outer,
        "rotation_euler",
        "frame*speed*.7*enabled",
        root,
        [("speed", "crystal_speed"), ("enabled", "crystal_spin")],
        2,
    )
    native(
        outer,
        "location",
        "1.5+amplitude*sin(frame*speed)",
        root,
        [("speed", "crystal_speed"), ("amplitude", "crystal_bob")],
        2,
    )
    for name in ("inner_glass", "cube"):
        parts[name].rotation_euler.x = math.pi / 4
        native(
            parts[name],
            "rotation_euler",
            "-frame*speed*enabled",
            root,
            [("speed", "crystal_speed"), ("enabled", "crystal_spin")],
            2,
        )
    point_light(transaction, collection, root, color=(0.8, 0.2, 1.0), height=1.5)


def build(transaction, collection, root, identifier, material_for) -> bool:
    ensure_mutation_allowed()
    if root.get("oxal_mc_studio_native_prefab"):
        return True
    short = identifier.split(":")[-1]
    state = json.loads(root.get("oxal_mc_studio_block_state", "{}"))
    if short == "beacon":
        _beacon(transaction, collection, root, material_for)
        return True
    if short == "enchanting_table":
        _book(transaction, collection, root, material_for)
        return True
    if short == "end_crystal" and root.get("oxal_mc_studio_special"):
        _crystal(transaction, collection, root, material_for)
        return True
    if short in {"torch","wall_torch","soul_torch","soul_wall_torch","copper_torch","copper_wall_torch","redstone_torch","redstone_wall_torch","fire","soul_fire","campfire","soul_campfire","furnace","blast_furnace","smoker","end_rod"}:
        # The environment extension owns the complete pinned native profile.
        return True
    torch = "torch" in short
    redstone = torch and "redstone" in short
    campfire = "campfire" in short
    furnace = short in {"furnace", "blast_furnace", "smoker"}
    fire = short in {"fire", "soul_fire"}
    rod = short == "end_rod"
    if not any((torch, campfire, furnace, fire, rod)):
        return False
    lit = str(state.get("lit", "false" if furnace else "true")).lower() == "true"
    color = (
        (0.2, 0.7, 1.0)
        if "soul" in short
        else (1.0, 0.2, 0.12)
        if "redstone" in short
        else (1.0, 0.6, 0.25)
    )
    if rod:
        color = (1.0, 0.92, 0.85)
    point_light(transaction, collection, root, lit=lit, color=color)
    if rod:
        body_emission(root, {"minecraft:block/end_rod"})
    control(root, "particles_enabled", bool(lit))
    control(root, "smoke_enabled", bool(lit and not rod and not redstone))
    control(root, "effect_speed", 1.0, 0.0, 100.0)
    control(root, "effect_scale", 1.0, 0.01, 20.0)
    control(
        root,
        "effect_lifetime",
        5.0 if campfire else 0.5 if redstone else 2.0,
        0.05,
        1000.0,
    )
    if rod:
        _particle_profile(
            transaction,
            collection,
            root,
            material_for,
            [f"glitter_{i}" for i in reversed(range(8))],
            count=12,
            flag="particles_enabled",
            origin=(0, 0, 0.25),
            spread=0.3,
            rise=1,
            size=0.06,
            emission=3,
        )
    elif redstone:
        _particle_profile(
            transaction,
            collection,
            root,
            material_for,
            [f"generic_{i}" for i in reversed(range(8))],
            count=6,
            flag="particles_enabled",
            origin=(0, 0, 0.7),
            spread=0.05,
            rise=0.2,
            size=0.05,
            emission=1,
            tint_color=0xFF0000,
        )
    else:
        profile = (
            [f"big_smoke_{i}" for i in range(12)]
            if campfire
            else [f"generic_{i}" for i in reversed(range(8))]
        )
        _particle_profile(
            transaction,
            collection,
            root,
            material_for,
            profile,
            count=12 if campfire else 6,
            flag="smoke_enabled",
            origin=(0, 0, 0.7),
            spread=0.2 if campfire else 0.05,
            rise=(6 if str(state.get("signal_fire")) == "true" else 3)
            if campfire
            else 0.6,
            size=0.4 if campfire else 0.08,
            emission=0.4,
        )
        if torch and "redstone" not in short:
            key = (
                "soul_fire_flame"
                if "soul" in short
                else "copper_fire_flame"
                if "copper" in short
                else "flame"
            )
            _particle_profile(
                transaction,
                collection,
                root,
                material_for,
                [key],
                count=3,
                flag="particles_enabled",
                origin=(0, 0, 0.7),
                spread=0.025,
                rise=0.15,
                size=0.15,
                emission=4,
            )
    root["oxal_mc_studio_effect_profile"] = (
        "end_rod"
        if rod
        else "dust"
        if redstone
        else "campfire_cosy_smoke"
        if campfire
        else "smoke+flame"
        if torch
        else "smoke"
    )
    return True
