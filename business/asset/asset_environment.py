"""Source environment families compiled once into native Blender animation.

Light emission comes from the pinned complete block-state table. Particle
cohorts, source mob meshes and sunflower joints remain usable without Python.
"""

import json
import math
import random
from functools import lru_cache
from typing import Any, cast

import bpy
from mathutils import Euler, Matrix, Vector

from ...core.runtime import ensure_mutation_allowed
from . import asset_catalog as catalog
from . import asset_effects as effects
from . import asset_states
from .asset_geometry import driver, elements, mesh_object

SCHEMA = "oxal_mc_studio_environment_schema"
TARGET = "控制 · 朝向目标"
SUN_TARGET = "控制 · 花朵朝向目标"
SUN_NECK = "内部 · 花颈追踪"
SUN_FLOWER = "花朵朝向"
WIDGET = "oxal_mc_studio_environment_target_widget"
_BURSTS = {"sculk_sensor", "calibrated_sculk_sensor", "sculk_shrieker"}
_PARTICLES = _BURSTS | {"spore_blossom", "spawner", "trial_spawner", "brewing_stand"}


@lru_cache(maxsize=1)
def profiles():
    result = dict(catalog.read(catalog.ROOT / "light_profiles_full.json")["profiles"])
    extra = catalog.read(catalog.ROOT / "light_profiles.json")
    result.update(extra.get("profiles", extra))
    return result


def requires_rig(identifier):
    short = identifier.split(":")[-1]
    return short in profiles() or short in _PARTICLES or short == "sunflower"


def _control(root, key, public, initial, minimum: float=0, maximum: float=1, *, label=None, values=()):
    specs = json.loads(root.get("oxal_mc_studio_state_controls", "[]"))
    if any(spec["key"] == key for spec in specs):
        return key
    asset_states.register_control(root, key, public, label or public,
                                  minimum, maximum, float(initial), values)
    if isinstance(initial,bool):
        root[key] = root[public] = initial
    return key


def _state(root, identifier, key):
    """Return the exact enum channel; expose latent emission-only states too."""
    specs = json.loads(root.get("oxal_mc_studio_state_controls", "[]"))
    existing = next((spec for spec in specs if spec["property"] == "状态 · " + key), None)
    if existing:
        return existing["key"], existing["values"]
    combined = next((spec for spec in specs if spec["property"] == "状态 · 附着面组合"), None)
    directions = ("north","east","south","west","up","down")
    if combined and key in directions:
        channel = "oxal_mc_studio_environment_state_"+key
        if channel not in root:
            root[channel] = 0.0
            effects.native(root,'["'+channel+'"]',f"floor(mask/{2**directions.index(key)})%2",root,[("mask",combined["key"])])
        return channel,["false","true"]
    original = catalog.read(catalog.ROOT/"block_state_properties.json")["blocks"].get(identifier.split(":")[-1],{})
    values = catalog.schema(identifier).get(key, []) or [str(value) for value in original.get("values",{}).get(key,[])]
    if not values:
        profile = profiles().get(identifier.split(":")[-1],{})
        values = list(dict.fromkeys(str(record["state"][key]) for record in profile.get("states",[]) if key in record["state"]))
    if not values:
        return None, []
    current = json.loads(root.get("oxal_mc_studio_block_state", "{}"))
    initial = values.index(str(current.get(key, original.get("default",{}).get(key, "false" if "false" in values else values[0]))))
    channel = "oxal_mc_studio_environment_state_" + key
    if len(values) > 1:
        _control(root, channel, "状态 · " + key, initial, 0, len(values)-1,
                 label=asset_states._label(key), values=values)
    else:
        root[channel] = initial
    return channel, values


def _native_sum(root, public, terms):
    # Intermediate scalar channels keep every expression below Blender's byte
    # limit, including the full 64-state glow-lichen truth table.
    channels = []
    for index, (expression, variables) in enumerate(terms):
        key = f"oxal_mc_studio_light_case_{index}"
        root[key] = 0.0
        effects.native(root, '["'+key+'"]', expression, root, variables)
        channels.append(key)
    level = 0
    while len(channels) > 8:
        reduced = []
        for index in range(0, len(channels), 8):
            group = channels[index:index+8]
            key = f"oxal_mc_studio_light_sum_{level}_{index}"
            root[key] = 0.0
            variables = [(f"v{i}", item) for i, item in enumerate(group)]
            effects.native(root, '["'+key+'"]', "+".join(v[0] for v in variables), root, variables)
            reduced.append(key)
        channels, level = reduced, level+1
    root[public] = 0.0
    variables = [(f"v{i}", item) for i, item in enumerate(channels)]
    effects.native(root, '["'+public+'"]', "+".join(v[0] for v in variables) or "0", root, variables)


def _light(tx, col, root, identifier):
    short = identifier.split(":")[-1]
    profile = profiles().get(short)
    if profile is None:
        return
    terms = []
    for state in profile["states"]:
        if not state["level"]:
            continue
        predicates, variables = [], []
        for key, value in state["state"].items():
            channel, values = _state(root, identifier, key)
            if channel is None or str(value) not in values:
                raise ValueError("原版发光状态缺少精确枚举："+identifier+"/"+key)
            variable = "v"+str(len(variables))
            variables.append((variable, channel))
            predicates.append(f"(floor({variable}+.5)=={values.index(str(value))})")
        terms.append((str(state["level"])+("*"+"*".join(predicates) if predicates else ""), variables))
    _native_sum(root, "原版发光等级", terms)
    maximum = max(profile["max"], 1)
    lit = short not in {"brewing_stand","end_rod"}
    if short=="end_rod":
        # This optional native lamp was absent from the original four-bone
        # particle rig. Keep it off and avoid adding effect-control bones.
        root["light_enabled"] = False
        root["oxal_mc_studio_light_power"] = 60*(maximum/15)**2
        root.id_properties_ui("oxal_mc_studio_light_power").update(min=0,max=10000)
    else:
        _control(root, "light_enabled", "照明开关", lit)
        _control(root, "oxal_mc_studio_light_power", "照明功率", 60*(maximum/15)**2, 0, 10000)
    lights = [obj for obj in tuple(tx.ids) if isinstance(obj, bpy.types.Object)
              and obj.type == "LIGHT" and obj.parent == root]
    def local_matrix(obj):
        if obj == root:
            return Matrix.Identity(4)
        return local_matrix(obj.parent) @ obj.matrix_parent_inverse @ obj.matrix_basis
    points = [local_matrix(obj) @ v.co for obj in tuple(root.children_recursive)
              if obj.type == "MESH" and not obj.get("oxal_mc_studio_effect_geometry")
              for v in obj.data.vertices]
    height = max((p.z for p in points), default=.5)+.05
    mean = sum(points,Vector())/len(points) if points else Vector()
    color = (.25,.8,1) if "soul" in short else (.3,1,.65) if "copper_torch" in short else (1,.72,.38) if any(n in short for n in ("torch","lantern","fire","furnace","candle","magma")) else (.85,1,.9)
    keep_strength = "effect_strength" in root
    if not lights:
        effects.point_light(tx, col, root, lit=lit, color=color, height=height)
        lights = [obj for obj in tuple(tx.ids) if isinstance(obj, bpy.types.Object)
                  and obj.type == "LIGHT" and obj.parent == root]
    for light in lights:
        data = cast(Any, light.data)
        data.color = color
        light.location = (mean.x,mean.y,height)
        data.driver_remove("energy")
        data.shadow_soft_size = .25
        data.use_shadow = False
        expression = f"enabled*max(power,0)*(level/{maximum})**2"
        variables = [("enabled","light_enabled"),("power","oxal_mc_studio_light_power"),("level","原版发光等级")]
        if short in {"sculk_sensor", "calibrated_sculk_sensor"}:
            channel, values = _state(root, identifier, "sculk_sensor_phase")
            if channel and "active" in values:
                expression += f"*(floor(phase+.5)=={values.index('active')})"
                variables.append(("phase",channel))
        effects.native(data, "energy", expression, root, variables)
    if short=="end_rod" and not keep_strength and "effect_strength" in root:
        # The generated generic lamp strength was replaced by the explicit W
        # channel above; do not expose an unused extra particle control.
        del root["effect_strength"]


def _empty(tx, col, root, name, location):
    part = tx.track(bpy.data.objects.new(name, None))
    col.objects.link(part)
    part.parent = root
    part.location = location
    part["oxal_mc_studio_model_part"] = name
    return part


def _tracking_widget(tx,root):
    # Original cross-and-diamond glyph in XY and YZ, not a one-axis slider.
    glyph = [(-.2,0),(.2,0),(0,-.2),(0,.2),(-.14,-.14),(.14,-.14),(.14,.14),(-.14,.14)]
    vertices = [(x,y,0) for x,y in glyph]+[(0,x,y) for x,y in glyph]
    edges = [(i+a,i+b) for i in (0,8) for a,b in ((0,1),(2,3),(4,5),(5,6),(6,7),(7,4))]
    mesh = tx.track(bpy.data.meshes.new("WGT · OXAL资产 · 双面朝向目标"))
    mesh.from_pydata(vertices,edges,[])
    widget = tx.track(bpy.data.objects.new(mesh.name,mesh))
    widget.hide_render = True
    root[WIDGET] = widget


def _textures(kind):
    data = catalog.read(catalog.resource("minecraft:"+kind, "particles"))
    return ["minecraft:particle/"+key.split(":")[-1] for key in data["textures"]]


def _sprite_material(tx, root, sprite, kind, material_for):
    """Native age selects all immutable sprite frames, including resource packs."""
    keys = _textures(kind)
    source = material_for(keys[0])
    material = tx.track(source.copy())
    material.name = "原版粒子 · "+kind
    material.surface_render_method = "BLENDED"
    tree = cast(Any, material.node_tree)
    shader = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    first = tree.nodes["OXAL Source Texture"]
    color, alpha = first.outputs["Color"], first.outputs["Alpha"]
    for index, key in enumerate(keys[1:], 1):
        source_tree = material_for(key).node_tree
        texture = tree.nodes.new("ShaderNodeTexImage")
        texture.image = source_tree.nodes["OXAL Source Texture"].image
        texture.interpolation = "Closest"
        texture["oxal_mc_studio_texture_key"] = key
        for channel, previous in (("Color", color), ("Alpha", alpha)):
            mix = tree.nodes.new("ShaderNodeMixRGB")
            tree.links.new(previous, mix.inputs[1])
            tree.links.new(texture.outputs[channel], mix.inputs[2])
            driver(mix.inputs[0], "default_value", f"v>={index/len(keys)}", sprite, "age")
            if channel == "Color":
                color = mix.outputs[0]
            else:
                alpha = mix.outputs[0]
    tree.links.new(color,shader.inputs["Base Color"])
    opacity = tree.nodes.new("ShaderNodeMath")
    opacity.operation = "MULTIPLY"
    opacity.inputs[1].default_value = 1
    tree.links.new(alpha,opacity.inputs[0])
    tree.links.new(opacity.outputs[0],shader.inputs["Alpha"])
    if kind == "shriek" or "smoke" in kind:
        driver(opacity.inputs[1],"default_value","max(0,1-v)",sprite,"age")
    if kind in {"flame","soul_fire_flame","copper_fire_flame","vibration","shriek","dust"}:
        tree.links.new(color,shader.inputs["Emission Color"])
        shader.inputs["Emission Strength"].default_value = 1
    return material


def _var(curve, name, owner, path):
    variable = curve.driver.variables.new()
    variable.name, variable.type = name, "SINGLE_PROP"
    if isinstance(owner,bpy.types.Scene):
        variable.targets[0].id_type = "SCENE"
    variable.targets[0].id = owner
    variable.targets[0].data_path = path


def _particles(tx, scene, col, root, short, options, material_for):
    from . import asset_playback
    clock = asset_playback.create(tx,scene,col,root,options)
    _control(root, "particles_enabled", "粒子开关", True)
    target = None
    if short in _BURSTS:
        _control(root, "oxal_mc_studio_particle_loop", "粒子循环", short == "sculk_shrieker", label="循环播放")
        _control(root, "oxal_mc_studio_particle_trigger", "触发进度", 0.0)
        _control(root, "oxal_mc_studio_particle_period", "循环周期", 5.0 if short == "sculk_shrieker" else 3.0,
                 4 if short == "sculk_shrieker" else 1, 30)
        target = _empty(tx, col, root, TARGET, (0,0,3.5) if short == "sculk_shrieker" else (0,-2,.5))
        root["oxal_mc_studio_environment_target"] = True
        for axis in range(3):
            key = "内部粒子目标"+str(axis)
            root[key] = float(target.location[axis])
            curve = driver(root,'["'+key+'"]',f"{target.location[axis]}+v")
            _var(curve,"v",target,f"location[{axis}]")
        if short != "sculk_shrieker":
            _control(root, "oxal_mc_studio_vibration_count", "粒子数量", 2.0, 1, 8, label="振动粒子数量")
            root["振动飞行时长"] = .1
            curve = driver(root, '["振动飞行时长"]', "max(.05,sqrt(x*x+y*y+z*z)*.05)")
            for axis, name in enumerate("xyz"):
                _var(curve,name,root,'["内部粒子目标'+str(axis)+'"]')
            curve.driver.expression = "max(.05,sqrt(x*x+y*y+(z-.5)**2)*.05)"
        target["native_target"] = True
    if short == "sculk_shrieker":
        recipes = [("shriek",10,1.5)]
    elif short in _BURSTS:
        recipes = [("vibration",8,.1)]
    elif short == "spawner":
        recipes = [("smoke",12,1.2),("flame",12,.8)]
    elif short == "trial_spawner":
        # The original trial cage distinguishes ominous soul-fire from normal
        # flame. Selection is native; no state-dependent allocation at frames.
        recipes = [("smoke",12,1.2),("flame",12,.8),("soul_fire_flame",12,.8)]
    else:
        raise ValueError("该资产不使用原生精灵粒子族："+short)
    rng = random.Random(195)
    for kind, count, lifetime in recipes:
        for index in range(count):
            material = material_for(_textures(kind)[0])
            if kind == "shriek":
                obj = effects._quad(tx,col,root,"原版粒子 · "+kind,material)
            else:
                obj = mesh_object(tx,"原版粒子 · "+kind,[(-.5,0,-.5),(.5,0,-.5),(.5,0,.5),(-.5,0,.5)],
                                  [(0,1,2,3)],[[(0,0),(1,0),(1,1),(0,1)]],[material],[0])
                col.objects.link(obj)
                obj.parent = root
                obj["oxal_mc_studio_effect_geometry"] = True
            obj["age"] = index/count
            obj["oxal_mc_studio_particle_kind"] = kind
            cast(Any, obj.data).materials[0] = _sprite_material(tx, root, obj, kind, material_for)
            if short in _BURSTS:
                expression = f"(((frame%max(period,.05)) if loop else progress*period)-{index*.25 if kind == 'shriek' else 0})/{lifetime}"
                variables = [("period","oxal_mc_studio_particle_period"),("loop","oxal_mc_studio_particle_loop"),("progress","oxal_mc_studio_particle_trigger")]
                if kind == "vibration":
                    expression = "((frame%max(period,.05)) if loop else progress*duration)/max(duration,.05)"
                    variables += [("duration","振动飞行时长")]
            else:
                expression = f"(frame/{lifetime}+{index/count})%1"
                variables = []
            curve = cast(Any,effects.native(obj, '["age"]', expression, root, variables))
            if short in _PARTICLES:
                # The authored burst clock is absolute scene seconds; unlike
                # display-tick smoke it never subtracts Scene.frame_start.
                curve.driver.expression = curve.driver.expression.replace("frame-first","frame")
                unused = curve.driver.variables.get("first")
                if unused is not None:
                    curve.driver.variables.remove(unused)
            asset_playback.retarget(curve,clock)
            age_vars = [("enabled","particles_enabled")]
            factor = "enabled*window*(age>=0 and age<1)"
            if short in _BURSTS:
                factor += "*(loop or progress>0)"
                age_vars += [("loop","oxal_mc_studio_particle_loop"),("progress","oxal_mc_studio_particle_trigger")]
            if kind == "vibration":
                factor += f"*(floor(quantity+.5)>{index})"
                factor += f"*(age+{index}/max(quantity,1)<1)"
                age_vars.append(("quantity","oxal_mc_studio_vibration_count"))
            if kind == "shriek":
                factor += "*min(1,max(0,age*.75))"
            if short == "trial_spawner":
                state_key, values = _state(root,"minecraft:trial_spawner","trial_spawner_state")
                active = [str(values.index(value)) for value in ("active","waiting_for_reward_ejection","ejecting_reward") if value in values]
                if state_key:
                    age_vars.append(("state",state_key))
                    factor += "*("+" or ".join("floor(state+.5)=="+value for value in active)+")"
                ominous, values = _state(root,"minecraft:trial_spawner","ominous")
                if ominous and kind in {"flame","soul_fire_flame"}:
                    age_vars.append(("ominous",ominous))
                    factor += f"*(floor(ominous+.5){'==' if kind=='soul_fire_flame' else '!='}{values.index('true')})"
            scale = .85 if kind == "shriek" else .24 if kind in {"vibration","smoke"} else .07
            if "smoke" in kind:
                factor += "*(.5+age)"
            for axis in range(3):
                curve = effects.native(obj, "scale", f"{scale}*({factor})", root, age_vars, axis)
                _var(curve, "age", obj, '["age"]')
                _var(curve,"window",clock,'["'+asset_playback.ACTIVE+'"]')
            cage_rng = random.Random(1950+index)
            origin = (cage_rng.uniform(-.5,.5),cage_rng.uniform(-.5,.5),cage_rng.uniform(0,1)) if short in {"spawner","trial_spawner"} else (0,0,.5)
            for axis in range(3):
                if kind == "vibration":
                    curve = driver(obj,"location",f"{origin[axis]}+(target-{origin[axis]})*(age+{index}/max(quantity,1))",index=axis)
                    _var(curve,"target",root,'["内部粒子目标'+str(axis)+'"]')
                    _var(curve,"age",obj,'["age"]')
                    _var(curve,"quantity",root,'["oxal_mc_studio_vibration_count"]')
                elif short in _BURSTS:
                    curve = driver(obj,"location",f"{origin[axis]}+(target-{origin[axis]})*min(1,max(0,age))", index=axis)
                    _var(curve,"target",root,'["内部粒子目标'+str(axis)+'"]')
                    _var(curve,"age",obj,'["age"]')
                else:
                    source_drift = (rng.uniform(-.15,.15),rng.uniform(-.15,.15),.18 if kind == "flame" or kind.endswith("_flame") else .3)
                    drift = source_drift[axis]
                    driver(obj,"location",f"{origin[axis]}+v*{drift}",obj,"age",axis)
            if kind == "shriek":
                # Two slanted source rings; their normal follows the exposed
                # target with a native pose constraint after compilation.
                vertices = [(x,y*math.cos(angle),y*math.sin(angle))
                            for angle in (-math.pi/6,math.pi/6)
                            for x,y in ((-1,-1),(1,-1),(1,1),(-1,1))]
                cast(Any,obj.data).vertices.foreach_set("co",[component for point in vertices for component in point])
            elif scene.camera:
                constraint = cast(Any, obj.constraints.new("TRACK_TO"))
                constraint.target = scene.camera
                constraint.track_axis, constraint.up_axis = "TRACK_NEGATIVE_Y", "UP_Z"


def _spores(tx,scene,col,root,options,material_for):
    from . import asset_playback
    from .asset_spores import build as spores
    _control(root,"particles_enabled","粒子开关",True)
    _control(root,"oxal_mc_studio_spore_density","孢子花密度",1.0,.5,2)
    clock = asset_playback.create(tx,scene,col,root,options)
    spores(tx,scene,col,root,material_for,clock)


def _mob_geometry(tx, model, material, name):
    vertices, faces, uv = [], [], []
    conversion = Matrix(((1,0,0,0),(0,0,1,0),(0,-1,0,0),(0,0,0,1)))
    def visit(part,parent):
        if not part.get("visible",True):
            return
        matrix = parent @ Matrix.Translation(Vector(part["pivot"])) @ Euler(part["rotation"],"XYZ").to_matrix().to_4x4() @ Matrix.Diagonal((*part.get("scale",[1,1,1]),1))
        if part.get("visible",True):
            for polygon in part["polygons"]:
                start = len(vertices)
                vertices.extend(tuple((conversion @ matrix @ Vector(point[:3]))/16) for point in polygon)
                faces.append(tuple(range(start,len(vertices))))
                uv.append([(point[3],1-point[4]) for point in polygon])
        for child in part.get("children",{}).values():
            visit(child,matrix)
    visit(model["definition"]["root"],Matrix.Identity(4))
    return mesh_object(tx,name,vertices,faces,uv,[material],[0]*len(faces))


def _mob_material(tx,data,material_for):
    source = material_for("minecraft:"+data["texture"])
    if not (data.get("overlays") or data.get("emissive") or data.get("blended")):
        return source
    material = tx.track(source.copy())
    material.name = "刷怪笼 · "+data["texture"]
    tree = cast(Any,material.node_tree)
    shader = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    for overlay in data.get("overlays",[]):
        key = "minecraft:"+overlay["texture"]
        texture = tree.nodes.new("ShaderNodeTexImage")
        texture.image = material_for(key).node_tree.nodes["OXAL Source Texture"].image
        texture.interpolation = "Closest"
        texture["oxal_mc_studio_texture_key"] = key
        if overlay.get("emissive"):
            tree.links.new(texture.outputs["Color"],shader.inputs["Emission Color"])
            tree.links.new(texture.outputs["Alpha"],shader.inputs["Emission Strength"])
        else:
            previous = shader.inputs["Base Color"].links[0].from_socket
            mix = tree.nodes.new("ShaderNodeMixRGB")
            tree.links.new(texture.outputs["Alpha"],mix.inputs[0])
            tree.links.new(previous,mix.inputs[1])
            tree.links.new(texture.outputs["Color"],mix.inputs[2])
            tree.links.new(mix.outputs[0],shader.inputs["Base Color"])
    if data.get("blended"):
        material.surface_render_method = "BLENDED"
    if data.get("emissive"):
        emission = tree.nodes.new("ShaderNodeEmission")
        tree.links.new(shader.inputs["Base Color"].links[0].from_socket,emission.inputs["Color"])
        transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
        add = tree.nodes.new("ShaderNodeAddShader")
        tree.links.new(transparent.outputs[0],add.inputs[0])
        tree.links.new(emission.outputs[0],add.inputs[1])
        output = next(node for node in tree.nodes if node.type == "OUTPUT_MATERIAL")
        tree.links.new(add.outputs[0],output.inputs["Surface"])
    return material


def _mob_attachments(tx,data,material_for):
    """Pinned renderer attachment matrices; ordinary block resource backend."""
    def part_matrix(part, name, parent):
        local = parent @ Matrix.Translation(Vector(part["pivot"])/16) @ Euler(part["rotation"],"XYZ").to_matrix().to_4x4() @ Matrix.Diagonal((*part.get("scale",[1,1,1]),1))
        if name == "head":
            return local
        for key, child in part.get("children",{}).items():
            found = part_matrix(child,key,local)
            if found is not None:
                return found
        return None
    result = []
    conversion = Matrix(((1,0,0,0),(0,0,1,0),(0,-1,0,0),(0,0,0,1)))
    block_basis = Matrix(((1,0,0,.5),(0,0,1,0),(0,-1,0,.5),(0,0,0,1)))
    head = part_matrix(data["definition"]["root"],"root",Matrix.Identity(4))
    for attachment in data.get("block_layers",[]):
        if head is None:
            raise ValueError("刷怪笼原版头部附属物缺少head部件")
        if attachment == "snow_golem_pumpkin":
            block = "carved_pumpkin"
            transforms = [head @ Matrix.Translation((0,-.34375,0)) @ Matrix.Rotation(math.pi,4,"Y")
                          @ Matrix.Diagonal((.625,-.625,-.625,1)) @ Matrix.Translation((-.5,-.5,-.5))]
        elif attachment == "mooshroom_mushrooms":
            block = "red_mushroom"
            mirrored = Matrix.Diagonal((-1,-1,1,1)) @ Matrix.Translation((-.5,-.5,-.5))
            body = Matrix.Translation((.2,-.35,.5))
            transforms = [body @ Matrix.Rotation(math.radians(-48),4,"Y") @ mirrored,
                          body @ Matrix.Rotation(math.radians(42),4,"Y") @ Matrix.Translation((.1,0,-.6)) @ Matrix.Rotation(math.radians(-48),4,"Y") @ mirrored,
                          head @ Matrix.Translation((0,-.7,-.2)) @ Matrix.Rotation(math.radians(-78),4,"Y") @ mirrored]
        else:
            raise ValueError("未知刷怪笼原版附属物："+attachment)
        descriptors,_ = catalog.plan("minecraft:"+block)
        for transform in transforms:
            for descriptor in descriptors:
                obj = elements(tx,"刷怪笼附属物 · "+block,catalog.model(descriptor["model"]),material_for,descriptor)
                cast(Any,obj.data).transform(conversion @ transform @ block_basis)
                result.append(obj)
    return result


def _spawner(tx,col,root,options,material_for):
    selected = options.get("spawn_entity", options.get("state",{}).get("spawn_entity","pig"))
    selected = selected.split(":")[-1]
    models = catalog.read(catalog.ROOT/"spawner_models.json")["models"]
    root["oxal_mc_studio_spawn_entity"] = selected
    if selected == "none":
        return
    if selected not in models:
        raise ValueError("刷怪笼生物未包含于随包原版资源："+selected)
    model = models[selected]
    if model.get("baby"):
        _control(root,"oxal_mc_studio_spawn_baby","幼年形态",False,label="成年 / 幼年")
    pivot = _empty(tx,col,root,"内部 · 刷怪笼生物",(0,0,.5))
    spin = cast(Any,effects.native(pivot,"rotation_euler","frame*.872664625997*(enabled>.5)",root,[("enabled","particles_enabled")],2))
    spin.driver.expression = spin.driver.expression.replace("frame-first","frame")
    spin.driver.variables.remove(spin.driver.variables["first"])
    from . import asset_playback
    clock = root.get(asset_playback.CLOCK)
    if clock is not None:
        asset_playback.retarget(spin,clock)
    appearances = [(False,model)]
    if model.get("baby"):
        appearances.append((True,model["baby"]))
    factor = None
    for is_baby, data in appearances:
        objects = [_mob_geometry(tx,entry,_mob_material(tx,entry,material_for),"刷怪笼 · "+selected+(" · 幼年" if is_baby else " · 成年"))
                   for entry in [data,*data.get("layers",[])]]
        objects.extend(_mob_attachments(tx,data,material_for))
        points = [v.co for obj in objects for v in cast(Any,obj.data).vertices]
        extent = max((max(p[i] for p in points)-min(p[i] for p in points) for i in range(3)), default=1)
        if factor is None:
            factor = .53125/max(1,extent)
        center = Vector(tuple((max(p[i] for p in points)+min(p[i] for p in points))/2 for i in range(3)))
        for obj in objects:
            col.objects.link(obj)
            obj.parent = pivot
            obj["oxal_mc_studio_effect_geometry"] = True
            cast(Any,obj.data).transform(Matrix.Translation(-center))
            # Both ages share adult scale; actual juvenile proportions remain.
            for axis in range(3):
                expression = f"{factor}*(enabled>.5)"+(f"*(baby{'>=.5' if is_baby else '<.5'})" if model.get("baby") else "")
                variables = [("enabled","particles_enabled")]+([("baby","oxal_mc_studio_spawn_baby")] if model.get("baby") else [])
                effects.native(obj,"scale",expression,root,variables,axis)
            obj["原版生物"],obj["生物年龄"],obj["参考缩放系数"] = selected,"baby" if is_baby else "adult",factor


def _sunflower(tx,col,root):
    candidates = []
    stem_points = []
    for obj in tuple(root.children_recursive):
        if obj.type != "MESH" or obj.get("oxal_mc_studio_effect_geometry"):
            continue
        front_indices, head_faces = [], []
        for polygon in obj.data.polygons:
            material = obj.data.materials[polygon.material_index]
            key = material.get("oxal_mc_studio_texture_key","") if material else ""
            back = material.get("oxal_mc_studio_surface_back_texture","") if material else ""
            if any(value.endswith(("sunflower_front","sunflower_back")) for value in (key,back)):
                head_faces.append(polygon.index)
                if key.endswith("sunflower_front"):
                    front_indices.append((polygon.index,1))
                elif back.endswith("sunflower_front"):
                    front_indices.append((polygon.index,-1))
            elif any(value.endswith("sunflower_top") for value in (key,back)):
                stem_points.extend(obj.matrix_local@obj.data.vertices[index].co for index in polygon.vertices)
        if head_faces:
            candidates.append((obj,head_faces,front_indices))
    if not candidates or not stem_points:
        return
    tip = max(p.z for p in stem_points)
    top = [p for p in stem_points if abs(p.z-tip)<1e-5]
    pivot = sum(top,Vector())/len(top)
    source, _, fronts = candidates[0]
    index,sign = fronts[0]
    polygon = source.data.polygons[index]
    normal = (source.matrix_local.to_3x3().inverted().transposed()@(polygon.normal*sign)).normalized()
    flower = _empty(tx,col,root,SUN_FLOWER,pivot)
    _empty(tx,col,root,SUN_NECK,pivot)
    _empty(tx,col,root,SUN_TARGET,pivot+normal*.8)
    for obj, head_faces, _ in candidates:
        # Keep source vertex/UV/material values exactly; split only the flower
        # card away from the upper cross stem's immutable geometry.
        mesh = obj.data
        all_faces = list(mesh.polygons)
        def subset(polygons,name,mesh=mesh):
            indices = sorted({index for face in polygons for index in face.vertices})
            lookup = {old:new for new,old in enumerate(indices)}
            first = mesh.uv_layers.get("UVMap") or mesh.uv_layers.active
            result = mesh_object(tx,name,[tuple(mesh.vertices[i].co) for i in indices],
                               [tuple(lookup[i] for i in face.vertices) for face in polygons],
                               [[tuple(first.data[i].uv) for i in face.loop_indices] for face in polygons],
                               list(mesh.materials),[face.material_index for face in polygons])
            data = cast(Any,result.data)
            data.uv_layers[0].name = first.name
            for layer in mesh.uv_layers:
                target = data.uv_layers.get(layer.name) or data.uv_layers.new(name=layer.name)
                for new_face,old_face in zip(data.polygons,polygons,strict=True):
                    for new_loop,old_loop in zip(new_face.loop_indices,old_face.loop_indices,strict=True):
                        target.data[new_loop].uv = layer.data[old_loop].uv
                target.active_render = layer.active_render
            data.uv_layers.active_index = mesh.uv_layers.active_index
            return result
        head = subset([face for face in all_faces if face.index in head_faces],"向日葵 · 花盘")
        col.objects.link(head)
        head.parent = flower
        head.matrix_parent_inverse = Matrix.Translation(-pivot)
        head.matrix_basis = obj.matrix_basis.copy()
        remaining = [face for face in all_faces if face.index not in head_faces]
        if remaining:
            body = subset(remaining,"向日葵 · 上茎")
            col.objects.link(body)
            body.parent = root
            body.matrix_local = obj.matrix_local.copy()
        tx.forget(obj)
        bpy.data.objects.remove(obj,do_unlink=True)
    root["向日葵花朵初始法线"] = json.dumps(list(normal))
    root["向日葵花颈轴心"] = [pivot[i] for i in range(3)]
    root["向日葵花朵朝向版本"] = 6
    root["向日葵茎干追踪版本"] = 3


def build(transaction,scene,collection,root,identifier,options,material_for):
    short = identifier.split(":")[-1]
    if not requires_rig(identifier):
        return
    ensure_mutation_allowed()
    _light(transaction,collection,root,identifier)
    from .asset_torch_particles import TORCHES
    from .asset_torch_particles import build as torch_particles
    if short in TORCHES:
        torch_particles(transaction,scene,collection,root,identifier,options,material_for)
    elif short=="end_rod":
        from .asset_end_rod import build as end_rod_particles
        end_rod_particles(transaction,scene,collection,root,options,material_for)
    elif short in {"redstone_torch","redstone_wall_torch"}:
        from .asset_redstone import build as redstone_particles
        redstone_particles(transaction,scene,collection,root,identifier,options,material_for)
    elif short == "spore_blossom":
        _spores(transaction,scene,collection,root,options,material_for)
    elif short == "brewing_stand":
        from .asset_brewing import build as brewing_smoke
        _control(root,"oxal_mc_studio_brewing_smoke","烟雾开关",True)
        brewing_smoke(transaction,scene,collection,root,options,material_for)
    elif short in _PARTICLES:
        _particles(transaction,scene,collection,root,short,options,material_for)
    if short in {"spawner","trial_spawner"}:
        _spawner(transaction,collection,root,options,material_for)
    if short == "sunflower" and options.get("context","world") == "world":
        _sunflower(transaction,collection,root)
    if short in _BURSTS or root.get("向日葵花朵朝向版本") == 6:
        _tracking_widget(transaction,root)
    root[SCHEMA] = 1


def post_compile(root,scene,options):
    if root.type != "ARMATURE" or not root.get(SCHEMA):
        return
    from .asset_end_rod import post_compile as end_rod_post
    end_rod_post(root)
    ensure_mutation_allowed()
    from .asset_rigs import _edit
    widget = root.get(WIDGET)
    if widget is not None:
        name = SUN_TARGET if root.get("向日葵花朵朝向版本") == 6 else TARGET
        pose = root.pose.bones[name]
        pose.custom_shape = widget
        pose.use_custom_shape_bone_size = False
        pose.custom_shape_rotation_euler = (0,0,math.pi/2)
        pose.custom_shape_wire_width = 2
        for palette in (pose.color,pose.bone.color):
            palette.palette = "CUSTOM"
            palette.custom.normal = (.45,.7,1) if name == SUN_TARGET else (.3,.55,.95)
            palette.custom.select, palette.custom.active = (.5,1,.88),(1,.73,.25)
        del root[WIDGET]
    if root.get("oxal_mc_studio_environment_target"):
        with _edit(root,scene) as bones:
            target = bones[TARGET]
            target.tail = target.head+Vector((0,0,.2))
            target.roll = 0
            target.parent = bones["总控制"]
            target.use_deform = False
        pose = root.pose.bones[TARGET]
        pose.lock_location = (False,False,False)
        pose.lock_rotation = pose.lock_scale = (True,True,True)
        position = root.data.bones[TARGET].head_local
        curves = {curve.data_path:curve for curve in root.animation_data.drivers}
        for axis,channel,sign in ((0,0,1),(1,2,-1),(2,1,1)):
            curve = curves['["内部粒子目标'+str(axis)+'"]']
            curve.driver.expression = f"{position[axis]}+v*{sign}"
            curve.driver.variables[0].targets[0].data_path = pose.path_from_id("location")+f"[{channel}]"
        for obj in tuple(root.children_recursive):
            if obj.get("oxal_mc_studio_particle_kind") == "shriek":
                constraint = cast(Any,obj.constraints.new("DAMPED_TRACK"))
                constraint.target, constraint.subtarget = root,TARGET
                constraint.track_axis = "TRACK_Z"
    if root.get("向日葵花朵朝向版本") != 6:
        root.update_tag()
        return
    normal = Vector(json.loads(root["向日葵花朵初始法线"]))
    pivot = Vector(root["向日葵花颈轴心"])
    basis = normal.to_track_quat("Y","Z").to_matrix().to_4x4()
    basis.translation = pivot
    # Preserve exact initial mesh transforms when replacing the compiler's
    # translation-only rest basis with the original flower normal/rest basis.
    affected = [(obj,obj.matrix_parent_inverse.copy()) for obj in tuple(root.children_recursive)
                if obj.parent == root and obj.parent_type == "BONE" and obj.parent_bone == SUN_FLOWER]
    before = root.data.bones[SUN_FLOWER].matrix_local.copy()
    before_length = root.data.bones[SUN_FLOWER].length
    with _edit(root,scene) as bones:
        neck, flower, target = bones[SUN_NECK],bones[SUN_FLOWER],bones[SUN_TARGET]
        neck.matrix = flower.matrix = basis
        neck.length = flower.length = .18
        neck.parent = bones["根控制"]
        flower.parent = neck
        target.head, target.tail = pivot+normal*.8,pivot+normal*.8+Vector((0,0,.18))
        target.parent = bones["总控制"]
        for bone in (neck,flower,target):
            bone.use_deform = False
    after = root.data.bones[SUN_FLOWER].matrix_local
    correction = Matrix.Translation((0,.18,0)).inverted() @ after.inverted() @ before @ Matrix.Translation((0,before_length,0))
    for obj, inverse in affected:
        obj.matrix_parent_inverse = correction @ inverse
    pose = root.pose.bones[SUN_TARGET]
    pose.lock_location = (False,False,False)
    pose.lock_rotation = pose.lock_scale = (True,True,True)
    neck = root.pose.bones[SUN_NECK]
    constraint = neck.constraints.new("DAMPED_TRACK")
    constraint.name = "花颈追踪目标"
    constraint.target, constraint.subtarget = root,SUN_TARGET
    constraint.track_axis = "TRACK_Y"
    constraint.owner_space = constraint.target_space = "POSE"
    root.update_tag()
