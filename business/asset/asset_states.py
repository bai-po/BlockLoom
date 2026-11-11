"""Declarative native presentation states compiled once by explicit commands."""

import itertools
import json
import math
from functools import lru_cache

from . import asset_catalog as catalog


def contracts():
    return catalog.read(catalog.ROOT / "native_state_contracts.json")


def _label(key):
    return contracts()["LABELS"].get(key, key)


def _control(key, values, initial=0, *, label=None, maximum=None, selector=None):
    return {"property": "状态 · " + key, "label": label or _label(key),
            "values": list(values), "min": 0, "max": len(values)-1 if maximum is None else maximum,
            "initial": initial, "selector": selector or "floor(v+.5)"}


@lru_cache(maxsize=1)
def copper_families():
    identifiers = {entry.identifier for entry in catalog.entries()}
    found = {}
    for identifier in sorted(identifiers):
        short = identifier.split(":")[-1]
        if not short.startswith("exposed_"):
            continue
        stem = short.removeprefix("exposed_")
        base = "copper_block" if stem == "copper" else stem
        names = tuple("minecraft:" + n for n in (base, "exposed_"+stem, "weathered_"+stem, "oxidized_"+stem))
        if all(name in identifiers for name in names):
            for name in names:
                found[name] = names
                if "minecraft:waxed_" + name.split(":")[-1] in identifiers:
                    found["minecraft:waxed_" + name.split(":")[-1]] = names
    return found


def _ranges(node):
    if not isinstance(node, dict):
        return []
    if node.get("type", "").endswith(":range_dispatch") and node.get("entries"):
        return node["entries"]
    for value in node.values():
        for child in value if isinstance(value, list) else [value]:
            if isinstance(child, dict) and (result := _ranges(child)):
                return result
    return []


def layout(identifier, state, *, complete=True):
    """Return controls and resource recipes; never allocate Blender data."""
    short = identifier.split(":")[-1]
    if short == "painting":
        names = tuple(path.stem for path in sorted((catalog.ROOT/"assets/minecraft/textures/painting").glob("*.png")) if path.stem != "back")
        return [_control("画作",names,names.index("kebab"))], [{"case":(i,),"state":state,"descriptors":[{"_oxal_painting":name}]} for i,name in enumerate(names)]
    if short in {"bow", "crossbow"}:
        names = ["待机", "拉动一", "拉动二", "拉满"]
        models = ["item/"+short, *(f"item/{short}_pulling_{i}" for i in range(3))]
        if short == "crossbow":
            names += ["装填箭", "装填烟花"]
            models += ["item/crossbow_arrow", "item/crossbow_firework"]
        return [_control("使用形态", names)], [
            {"case": (i,), "state": state, "descriptors": [{"type":"minecraft:model", "model":"minecraft:"+model}]}
            for i, model in enumerate(models)]
    if short in {"clock", "compass", "recovery_compass"}:
        entries = _ranges(catalog.read(catalog.resource(identifier,"items"))["model"])
        if not entries:
            raise ValueError("缺少原版状态贴图："+identifier)
        count, clock = len(entries), short == "clock"
        control = _control("时间" if clock else "指向角度", range(count),
                           12 if clock else 0, label="时间 · 0–24时" if clock else "指南针朝向",
                           maximum=24 if clock else 360,
                           selector=f"min({count-1},floor(fmod(v/{24 if clock else 360}+{.75 if clock else 0},1)*{count}))")
        control["property"] = "时间" if clock else "指向角度"
        return [control], [{"case":(i,),"state":state,"descriptors":catalog.item_nodes(entry["model"])} for i,entry in enumerate(entries)]
    # Mechanical families have continuous native joints, not competing boolean
    # alternatives. Their placement/type variants still rebuild explicitly.
    if short.endswith(("_bed","chest","_boat","_raft","minecart","_door","_trapdoor","_fence_gate","shulker_box","_banner")) or short in {
        "piston","sticky_piston","armor_stand","bell","dragon_head","end_crystal","enchanting_table","beacon",
    }:
        return [], []
    schema = catalog.schema(identifier)
    placement = set(contracts()["PLACEMENT"])
    if complete:
        placement.update({"half","part"})
    options = {key:values for key,values in schema.items() if key not in placement and len(values)>1}
    if short == "brewing_stand":
        options = {"药水瓶组合":("无","左","中","左中","右","左右","中右","全部")}
    elif short == "sculk_vein":
        options = {"附着面组合":tuple(range(64))}
    elif short in {"fire","soul_fire"}:
        options = {"火焰附着组合":tuple(range(32))}
    if not options:
        return [], []
    if math.prod(len(values) for values in options.values()) > 2048:
        raise ValueError("原生形态组合超过2048安全预算："+identifier)
    controls = [_control(key,values,values.index(state.get(key,values[0]))) for key,values in options.items()]
    if short == "brewing_stand":
        controls[0]["initial"] = sum((state.get(f"has_bottle_{i}")=="true")*(1<<i) for i in range(3))
    variants = []
    for case in itertools.product(*(range(len(values)) for values in options.values())):
        selected = {**state, **{key:values[index] for (key,values),index in zip(options.items(),case,strict=True)}}
        if short == "brewing_stand":
            selected = {**state, **{f"has_bottle_{i}":str(bool(case[0]&(1<<i))).lower() for i in range(3)}}
        elif short in {"sculk_vein","fire","soul_fire"}:
            directions = ("north","east","south","west","up","down") if short == "sculk_vein" else ("north","east","south","west","up")
            selected = {**state, **{direction:str(bool(case[0]&(1<<i))).lower() for i,direction in enumerate(directions)}}
        descriptors,_ = catalog.plan(identifier,state=selected,special=True,complete=complete)
        variants.append({"case":case,"state":selected,"descriptors":descriptors})
    if len({json.dumps(v["descriptors"],sort_keys=True) for v in variants}) < 2:
        return [], []
    return controls, variants


@lru_cache(maxsize=4096)
def has_states(identifier):
    short = identifier.split(":")[-1]
    if short in {"bow","crossbow","clock","compass","recovery_compass","painting"} or identifier in copper_families():
        return True
    controls,_ = layout(identifier,catalog.default_state(identifier),complete=True)
    return bool(controls)


def aliases(root):
    return [(item["key"],(item["property"],item["min"],item["max"],item["label"]))
            for item in json.loads(root.get("oxal_mc_studio_state_controls","[]"))]


def register_control(root,key,property_name,label,minimum,maximum,initial,values=()):
    """Declare one native rail before rig compilation, without UI callbacks."""
    if minimum>=maximum:
        raise ValueError("原生控件范围无效")
    specs = json.loads(root.get("oxal_mc_studio_state_controls","[]"))
    if key in {spec["key"] for spec in specs} or any(spec["label"] == label for spec in specs):
        raise ValueError("原生控件标识或骨名重复："+label)
    if isinstance(initial,bool):
        initial = float(initial)
    root[key],root[property_name] = initial,initial
    root.id_properties_ui(property_name).update(min=minimum,max=maximum)
    specs.append({"key":key,"property":property_name,"label":label,"min":minimum,"max":maximum,"initial":initial,"values":list(values)})
    root["oxal_mc_studio_state_controls"] = json.dumps(specs,ensure_ascii=False)


def painting_geometry(transaction, collection, root, name, material_for):
    from .asset_geometry import mesh_object
    front = material_for("minecraft:painting/"+name)
    width,height = front.node_tree.nodes["OXAL Source Texture"].image.size
    w,h = width/16,height/16
    vertices = [(-w/2,-1/32,0),(w/2,-1/32,0),(w/2,-1/32,h),(-w/2,-1/32,h),
                (-w/2,1/32,0),(w/2,1/32,0),(w/2,1/32,h),(-w/2,1/32,h)]
    faces = [(0,1,2,3),(5,4,7,6),(4,0,3,7),(1,5,6,2),(3,2,6,7),(4,5,1,0)]
    obj = mesh_object(transaction,"画作 · "+name,vertices,faces,[[(0,0),(1,0),(1,1),(0,1)]]*6,
                      [front,material_for("minecraft:painting/back")],[0,1,1,1,1,1])
    collection.objects.link(obj)
    obj.parent = root
    return obj


def compile_copper(transaction, root, identifier, material_for):
    """Four source texture stages, preserving geometry, alpha and native UVs."""
    from .asset_effects import native
    family = copper_families().get(identifier)
    if family is None:
        return
    initial = family.index(identifier.replace("minecraft:waxed_","minecraft:"))
    specs = json.loads(root.get("oxal_mc_studio_state_controls","[]"))
    key = "oxal_mc_studio_copper_stage"
    root[key],root["氧化程度"] = float(initial),float(initial)
    specs.append({"key":key,"property":"氧化程度","label":"铜氧化程度","min":0,"max":3,"initial":initial,"values":["未氧化","斑驳","锈蚀","氧化"]})
    root["oxal_mc_studio_state_controls"] = json.dumps(specs,ensure_ascii=False)
    seen = set()
    for obj in tuple(root.children_recursive):
        if obj.type != "MESH" or obj.get("oxal_mc_studio_effect_geometry"):
            continue
        for mat in obj.data.materials:
            if mat is None or mat in seen or not mat.use_nodes:
                continue
            seen.add(mat)
            texture_key = mat.get("oxal_mc_studio_texture_key","")
            if not texture_key.startswith("minecraft:"):
                continue
            tree = mat.node_tree
            original = tree.nodes.get("OXAL Source Texture")
            if original is None:
                continue
            links = [(link.to_socket,link.from_socket.name) for link in tuple(tree.links) if link.from_node == original]
            folder,stem = texture_key.rsplit("/",1)
            for prefix in ("exposed_","weathered_","oxidized_"):
                stem = stem.removeprefix(prefix)
            samplers = []
            for stage,prefix in enumerate(("","exposed_","weathered_","oxidized_")):
                candidate = folder+"/"+prefix+("copper" if prefix and stem == "copper_block" else stem)
                if folder == "minecraft:entity/chest":
                    suffix = "_left" if stem.endswith("_left") else "_right" if stem.endswith("_right") else ""
                    definition = catalog.read(catalog.resource(family[stage],"items"))["model"]
                    descriptor = catalog.item_nodes(definition)[0]
                    candidate = "minecraft:entity/chest/"+descriptor["model"]["texture"].split(":")[-1]+suffix
                if not catalog.resource(candidate,"textures",".png").is_file():
                    candidate = texture_key
                source = material_for(candidate)
                node = tree.nodes.new("ShaderNodeTexImage")
                node.name = "OXAL Copper Texture "+str(stage)
                node["oxal_mc_studio_texture_key"] = candidate
                node["oxal_mc_studio_original_texture"] = str(catalog.resource(candidate,"textures",".png"))
                node.image = source.node_tree.nodes["OXAL Source Texture"].image
                node.interpolation,node.extension = "Closest",original.extension
                if original.inputs["Vector"].is_linked:
                    tree.links.new(original.inputs["Vector"].links[0].from_socket,node.inputs["Vector"])
                samplers.append(node)
            for output_name in ("Color","Alpha"):
                previous = samplers[0].outputs[output_name]
                for stage in range(1,4):
                    mix = tree.nodes.new("ShaderNodeMixRGB")
                    native(mix.inputs[0],"default_value",f"floor(v+.5)=={stage}",root,[("v","氧化程度")])
                    tree.links.new(previous,mix.inputs[1])
                    tree.links.new(samplers[stage].outputs[output_name],mix.inputs[2])
                    previous = mix.outputs[0]
                for destination,source_name in links:
                    if source_name == output_name:
                        tree.links.new(previous,destination)


def compile_states(transaction, scene, collection, root, identifier, options, build_variant):
    """Prepare unique alternatives and bounded native predicate trees."""
    from .asset_effects import native
    state = json.loads(root["oxal_mc_studio_block_state"])
    controls,variants = layout(identifier,state,complete=options.get("complete_structure",True))
    if not controls:
        return
    specs = []
    for i,control in enumerate(controls):
        spec = {**control,"key":f"oxal_mc_studio_state_{i}"}
        root[spec["key"]] = float(spec["initial"])
        root[spec["property"]] = float(spec["initial"])
        root["选项 · "+spec["property"].removeprefix("状态 · ")] = " / ".join(str(v) for v in spec["values"])
        specs.append(spec)
    root["oxal_mc_studio_state_controls"] = json.dumps(specs,ensure_ascii=False)
    original_metadata = {key:root.get(key) for key in ("oxal_mc_studio_block_state","oxal_mc_studio_shape")}
    original_objects = set(root.children_recursive)
    current,_ = catalog.plan(identifier,state=state,special=True,complete=options.get("complete_structure",True))
    current_key = json.dumps(current,sort_keys=True)
    groups = {}
    for variant in variants:
        signature = json.dumps(variant["descriptors"],sort_keys=True)
        group = groups.setdefault(signature,{"recipe":variant,"cases":[]})
        group["cases"].append(variant["case"])
    used_original = False
    for number,(signature,group) in enumerate(groups.items()):
        if signature == current_key and not used_original:
            objects = original_objects
            used_original = True
        else:
            before = set(root.children_recursive)
            recipe = group["recipe"]
            build_variant({**options,"state":recipe["state"],"_descriptors":recipe["descriptors"],"_native_variant":True})
            objects = set(root.children_recursive)-before
        terms = []
        for case_index,case in enumerate(group["cases"]):
            key = f"oxal_mc_studio_state_case_{number}_{case_index}"
            expression = " and ".join(f"({spec['selector'].replace('v','v'+str(i))})=={value}" for i,(spec,value) in enumerate(zip(specs,case,strict=True)))
            root[key] = 0.
            native(root,'["'+key+'"]',expression,root,[("v"+str(i),spec["property"]) for i,spec in enumerate(specs)])
            terms.append(key)
        while len(terms)>1:
            merged = []
            for start in range(0,len(terms),8):
                keys = terms[start:start+8]
                key = f"oxal_mc_studio_state_union_{number}_{len(terms)}_{start}"
                root[key] = 0.
                native(root,'["'+key+'"]',"max("+",".join("x"+str(i) for i in range(len(keys)))+")" if len(keys)>1 else "x0",root,[("x"+str(i),key) for i,key in enumerate(keys)])
                merged.append(key)
            terms = merged
        for obj in objects:
            if obj.type != "MESH":
                continue
            obj["oxal_mc_studio_native_state"] = number
            obj["oxal_mc_studio_state_recipe"] = json.dumps(group["recipe"],ensure_ascii=False)
            for path in ("hide_render","hide_viewport"):
                native(obj,path,"not visible",root,[("visible",terms[0])])
    if not used_original:
        import bpy
        for obj in tuple(original_objects):
            data = obj.data
            transaction.forget(obj)
            bpy.data.objects.remove(obj,do_unlink=True)
            if data is not None and data.users == 0 and data in transaction.ids:
                transaction.forget(data)
                bpy.data.batch_remove(ids=(data,))
    for key,value in original_metadata.items():
        root[key] = value
    root["形态骨骼"] = True
