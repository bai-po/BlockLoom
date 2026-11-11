"""One source-sized, stationary redstone dust sprite and native pulse clock."""

import random
from typing import Any, cast

import bpy

from . import asset_playback
from .asset_geometry import driver, mesh_object
from .asset_hinges import model_matrix
from .asset_projectile_effects import billboard

IDENTIFIERS = {"redstone_torch", "redstone_wall_torch"}


def _variable(curve, name, owner, path):
    variable = curve.driver.variables.new()
    variable.name, variable.type = name, "SINGLE_PROP"
    variable.targets[0].id_type = "SCENE" if isinstance(owner, bpy.types.Scene) else "OBJECT"
    variable.targets[0].id = owner
    variable.targets[0].data_path = path


def build(tx, scene, collection, root, identifier, options, material_for):
    from .asset_environment import _control, _sprite_material, _state, _textures

    short = identifier.split(":")[-1]
    if short not in IDENTIFIERS:
        return False
    _control(root, "particles_enabled", "粒子开关", True)
    _control(root, "oxal_mc_studio_dust_period", "粒子周期", 1.6, .5, 4.0)
    clock = asset_playback.create(tx, scene, collection, root, options)
    origin = [0.0, 0.0, .7]
    if short == "redstone_wall_torch":
        points = [model_matrix(obj, root) @ vertex.co for obj in root.children_recursive
                  if obj.type == "MESH" and not obj.get("oxal_mc_studio_effect_geometry")
                  for vertex in obj.data.vertices]
        if not points:
            raise ValueError("墙面红石火把缺少可核验的原生几何")
        top = max(point.z for point in points)
        caps = [point for point in points if point.z > top-.06]
        origin = [sum(point[axis] for point in caps)/len(caps) for axis in range(3)]
        origin[2] += .12
    rng = random.Random(195)
    position = tuple(value+rng.uniform(-.1, .1) for value in origin)
    source = material_for(_textures("dust")[0])
    sprite = mesh_object(tx, "红石火把 · 原版粉尘", [(-.1,0,-.1),(.1,0,-.1),(.1,0,.1),(-.1,0,.1)],
                         [(0,1,2,3)], [[(0,0),(1,0),(1,1),(0,1)]], [source], [0])
    collection.objects.link(sprite)
    sprite.parent, sprite.location = root, position
    sprite["oxal_mc_studio_effect_geometry"] = True
    sprite["oxal_mc_studio_particle_kind"] = "dust"
    sprite["oxal_mc_studio_model_part"] = "红石火把 · 原版粉尘"
    sprite["age"] = 0.0
    curve = driver(sprite, '["age"]', "fmod(fmod(local*base/fps,p)+p,p)/.45")
    for name, owner, path in (("local", clock, '["'+asset_playback.TIME+'"]'),
                              ("base", scene, "render.fps_base"), ("fps", scene, "render.fps"),
                              ("p", root, '["oxal_mc_studio_dust_period"]')):
        _variable(curve, name, owner, path)
    material = _sprite_material(tx, root, sprite, "dust", material_for)
    cast(bpy.types.Mesh, sprite.data).materials[0] = material
    tree = cast(Any, material.node_tree)
    shader = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    tint = tree.nodes.new("ShaderNodeMixRGB")
    tint.blend_type, tint.label = "MULTIPLY", "红石粉尘原版染色"
    tint.inputs[0].default_value = 1.0
    tint.inputs[2].default_value = (1.0, .025, .015, 1.0)
    tree.links.new(shader.inputs["Base Color"].links[0].from_socket, tint.inputs[1])
    tree.links.new(tint.outputs[0], shader.inputs["Base Color"])
    tree.links.new(tint.outputs[0], shader.inputs["Emission Color"])
    lit, values = _state(root, identifier, "lit")
    for axis in range(3):
        expression = "enabled and active and age>=0 and age<1"
        curve = cast(Any, driver(sprite, "scale", expression, index=axis))
        _variable(curve, "enabled", root, '["particles_enabled"]')
        _variable(curve, "active", clock, '["'+asset_playback.ACTIVE+'"]')
        _variable(curve, "age", sprite, '["age"]')
        if lit is not None and "true" in values:
            curve.driver.expression += " and floor(lit+.5)=="+str(values.index("true"))
            _variable(curve, "lit", root, '["'+lit+'"]')
    billboard(tx, sprite, point_at_camera=True)
    root["oxal_mc_studio_redstone_native"] = True
    return True
