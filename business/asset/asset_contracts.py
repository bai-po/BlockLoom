"""Passive generated-geometry contracts for explicit, conservative repair."""

import hashlib
import json
import re

import bpy

from . import asset_catalog as catalog
from . import asset_rigs

PROPERTY = "oxal_mc_studio_geometry_contract"


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def mesh_hash(mesh):
    payload = {
        "vertices":[[round(float(v),6) for v in vertex.co] for vertex in mesh.vertices],
        "polygons":[(tuple(p.vertices),p.material_index) for p in mesh.polygons],
        "uv":{layer.name:[[round(float(v),6) for v in loop.uv] for loop in layer.data] for layer in mesh.uv_layers},
    }
    # The released Beta's independent data capture uses ASCII JSON escapes.
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def mesh_additions(obj):
    """Data replaced with a Mesh but absent from the released geometry hash."""
    mesh = obj.data
    attributes = {}
    topology = {"position", ".edge_verts", ".corner_vert", ".corner_edge", "material_index"}
    for attribute in mesh.attributes:
        name = attribute.name
        if (name in topology or name in mesh.uv_layers or name.startswith((".select_", ".hide_", ".uv_select_"))
                or not len(attribute.data)):
            continue
        values = []
        for item in attribute.data:
            fields = {}
            for prop in item.bl_rna.properties:
                if prop.identifier == "rna_type" or prop.type not in {"BOOLEAN", "INT", "FLOAT", "STRING"}:
                    continue
                value = getattr(item, prop.identifier)
                fields[prop.identifier] = tuple(value) if prop.is_array else value
            values.append(fields)
        attributes[name] = [attribute.data_type, attribute.domain, digest(values)]
    weights = [[vertex.index, [(obj.vertex_groups[group.group].name, group.weight) for group in vertex.groups]]
               for vertex in mesh.vertices if vertex.groups]
    normals = [tuple(normal.vector) for normal in mesh.corner_normals] if mesh.has_custom_normals else []
    return {"attributes": attributes, "weights": digest(weights) if weights else "",
            "normals": digest(normals) if normals else ""}


def legacy_mesh_preflight(root, mapping, retired):
    """Check old unrecorded data against only the freshly proven counterpart.

    New generated attributes may legitimately be added by a SLOT upgrade. An
    old attribute/weight must already match its source counterpart, rather than
    being trusted just because it uses a generated-looking name.
    """
    saved = json.loads(root[PROPERTY]) if PROPERTY in root else {}
    if "mesh_data" in saved:
        return
    pairs = {old: new for new, old in mapping.items() if old.type == new.type == "MESH"}
    for old in {*pairs, *(obj for obj in retired if obj.type == "MESH")}:
        actual = mesh_additions(old)
        source = mesh_additions(pairs[old]) if old in pairs else {"attributes": {}, "weights": "", "normals": ""}
        if (any(source["attributes"].get(name) != values for name, values in actual["attributes"].items())
                or any(actual[key] and actual[key] != source[key] for key in ("weights", "normals"))):
            raise ValueError("旧资产含未登记的用户网格属性、权重或自定义法线，保留现状，未重建：" + old.name)


def bones(rig):
    return [(b.name,b.parent.name if b.parent else None,[round(float(v),6) for row in b.matrix_local for v in row]) for b in rig.data.bones] if rig and rig.type == "ARMATURE" else []


def control_rig(root):
    return root if root.type == "ARMATURE" else root.get("oxal_mc_studio_control_rig")


def record(root, *, create=False, transaction=None):
    from .assets import members
    rig = control_rig(root)
    references = {key:value for key,value in root.get("oxal_mc_studio_contract_refs",{}).items() if value is not None}
    used_references = set()
    def identity(value):
        if value is None:
            return None
        if value == root:
            return "SELF"
        if value in root.users_scene:
            return "SCENE"
        for key,reference in references.items():
            if value == reference:
                used_references.add(key)
                return key
        if not create:
            return "UNKNOWN"
        key = str(max((-1,*(int(key) for key in references)))+1)
        references[key] = value
        used_references.add(key)
        return key
    mesh_root = (root.type == "MESH" and root.get("oxal_mc_studio_image_root_schema") == 1)
    child_drivers = []
    hierarchy = []
    for obj in members(root):
        hierarchy.append((identity(obj),identity(obj.parent),obj.parent_type,obj.parent_bone,
                          [round(float(v),6) for row in obj.matrix_parent_inverse for v in row]))
        owners = (obj,obj.data) if obj.data is not None else (obj,)
        for owner in owners:
            if owner == root:
                continue
            curves = asset_rigs.driver_records(owner)
            for _,_,_,_,variables in curves:
                for _,_,targets in variables:
                    for target in targets:
                        target["id"] = identity(target.get("id"))
            if curves:
                child_drivers.append((identity(owner),curves))
    constraints = []
    if rig and rig.pose:
        for pose in rig.pose.bones:
            for constraint in pose.constraints:
                values = {}
                for prop in constraint.bl_rna.properties:
                    if prop.is_readonly or prop.identifier in {"rna_type","type","name"} or prop.type == "COLLECTION":
                        continue
                    value = getattr(constraint,prop.identifier)
                    if isinstance(value,bpy.types.ID):
                        value = identity(value)
                    elif getattr(prop,"is_array",False):
                        value = tuple(value)
                    values[prop.identifier] = value
                constraints.append((pose.name,constraint.type,constraint.name,values))
    mesh_drivers = []
    root_modifiers = []
    if mesh_root:
        from ...util import drivers
        from ..enchantment.enchantment_outline import generated_modifiers

        generated = generated_modifiers(root, root, transaction=transaction)
        root_modifiers = [(modifier.type, modifier.name) for modifier in root.modifiers if modifier not in generated]

        def normalize(value):
            if isinstance(value, bpy.types.ID):
                return identity(value)
            if isinstance(value, dict):
                return {key: normalize(part) for key, part in value.items() if key != "select"}
            if isinstance(value, (tuple, list, set)):
                return [normalize(part) for part in value]
            return value

        # The root's public transform/control animation stays user-editable.
        # Only its modifier channels are generated geometry; do not validate or
        # rewrite unrelated root drivers while recording those exact channels.
        mesh_drivers = [normalize(curve) for curve in drivers.capture(root)["curves"] if curve["path"].startswith("modifiers[")]
    if create:
        root["oxal_mc_studio_contract_refs"] = {key:references[key] for key in used_references}
    result = {"meshes":sorted(mesh_hash(o.data) for o in members(root) if o.type == "MESH"),
            "mesh_data":sorted((identity(o), mesh_additions(o)) for o in members(root) if o.type == "MESH"),
            "edges":sorted(digest([tuple(edge.vertices) for edge in o.data.edges]) for o in members(root) if o.type == "MESH"),
            "bones":digest(bones(rig)),"drivers":asset_rigs.driver_contract(rig) if rig else "",
            "child_drivers":sorted(json.dumps(value,sort_keys=True) for value in child_drivers),"hierarchy":sorted(hierarchy),"constraints":constraints,"objects":len(members(root))-1}
    if mesh_root:
        result["root_mesh_modifiers"] = root_modifiers
        result["root_mesh_drivers"] = mesh_drivers
    return result


def stamp(root, *, transaction=None):
    root[PROPERTY] = json.dumps(record(root,create=True,transaction=transaction),sort_keys=True,separators=(",",":"))


def _driver_order(record):
    """Compare FCurve contents independently of native collection insertion.

    A rollback can recreate only removed curves at the collection's end.
    Preserve every channel, setting and actual-reference token; duplicate
    channels remain invalid rather than being deduplicated or re-signed.
    """
    result = dict(record)
    if "child_drivers" not in result:
        return result
    normalized = []
    for encoded in result["child_drivers"]:
        owner, curves = json.loads(encoded)
        channels = [(curve[0], curve[1]) for curve in curves]
        if len(channels) != len(set(channels)):
            raise ValueError("资产生成合同含重复驱动通道，未接管")
        normalized.append(json.dumps((owner, sorted(curves, key=lambda curve: (curve[0], curve[1]))), sort_keys=True))
    result["child_drivers"] = sorted(normalized)
    return result


def validate(root, *, beta=False):
    from ...util.resources import VERSION
    from .assets import IDENTIFIER, members
    for obj in members(root):
        for owner in (obj, obj.data):
            animation = getattr(owner, "animation_data", None)
            if animation and any(curve.mute for curve in animation.drivers):
                raise ValueError("资产生成驱动已被用户静音，保留现状，未重建：" + obj.name)
    current = record(root)
    if PROPERTY in root:
        saved = json.loads(root[PROPERTY])
        if "mesh_data" not in saved:
            current.pop("mesh_data")
        actual = json.loads(json.dumps(current))
        if saved != actual and _driver_order(saved) != _driver_order(actual):
            raise ValueError("资产生成几何、骨架或驱动含用户改动，保留现状，未重建")
        return
    if not beta or str(root.get(VERSION,"")) != "1":
        raise ValueError("资产缺少可核验的生成合同，请使用已支持版本的显式修复")
    state = json.loads(root.get("oxal_mc_studio_block_state","{}"))
    actual = asset_rigs.driver_contract(root) if root.type == "ARMATURE" else ""
    if root.type == "ARMATURE" and root.get("oxal_mc_studio_rig_driver_contract") != actual:
        raise ValueError("旧Beta驱动已修改，保留用户绑定，未修复")
    candidates = [r for r in catalog.read(catalog.ROOT/"beta1_contracts.json")["records"]
                  if r["identifier"] == root.get(IDENTIFIER) and r["state"] == state and r["root_type"] == root.type]
    matching = [r for r in candidates if r["driver_hash"] == hashlib.sha256(actual.encode()).hexdigest()
                and r["bone_hash"] == digest(bones(root)) and r["meshes"] == current["meshes"]]
    if len(matching) != 1:
        raise ValueError("旧Beta形态未匹配已验证合同，或已有用户拓扑/骨架改动，未覆盖")
    _beta_objects(root,matching[0])
    for obj in members(root):
        if obj.type == "MESH" and obj.data.shape_keys:
            raise ValueError("旧Beta含未登记的用户形态键，未覆盖")


def _beta_objects(root,baseline):
    """Match known data contracts only after the caller's exact ID provenance.

    Names help disambiguate equal generated shapes; they never grant ownership.
    User transforms and Actions are retained, while a changed generated parent
    inverse or driver binding is a conflict rather than an implicit repair.
    """
    from .assets import members
    objects = members(root)
    expected = list(baseline.get("native_objects",()))
    if len(objects) != len(expected):
        raise ValueError("旧Beta部件数量与生成合同不同，未修复")
    roles = {root:"SELF"}
    remaining = [item for item in expected if item["role"] != "SELF"]
    def label(name):
        return re.sub(r"\.\d{3,}$","",name)
    for obj in objects[1:]:
        mesh = mesh_hash(obj.data) if obj.type == "MESH" else None
        candidates = [item for item in remaining if item["type"] == obj.type and item["parent_bone"] == obj.parent_bone and (mesh is None or item.get("mesh_hash") == mesh)]
        # Equal particle quads differ by their generated age/position drivers.
        # Compare that contract before any name fallback: suffixes shift when
        # importing a second instance and never establish ownership.
        actual=[(curve.data_path,curve.array_index,curve.driver.type,curve.driver.expression)
                for curve in obj.animation_data.drivers] if obj.animation_data else []
        driven=[item for item in candidates if actual==[(row["path"],row["index"],row["type"],row["expression"])
                for row in baseline.get("object_drivers",{}).get(item["role"],[])]]
        if driven:
            candidates=driven
        if len(candidates)>1:
            exact=[item for item in candidates if item["name"]==obj.name]
            named=exact or [item for item in candidates if label(item["name"])==label(obj.name)]
            if len(named)==1:candidates=named
        if len(candidates) != 1:
            raise ValueError("旧Beta相同形态部件无法唯一对应，未修复")
        chosen = candidates[0]
        roles[obj] = chosen["role"]
        remaining.remove(chosen)
    by_role = {item["role"]:item for item in expected}
    for obj,role in roles.items():
        if obj == root:
            continue
        item = by_role[role]
        inverse = [round(float(value),6) for row in obj.matrix_parent_inverse for value in row]
        if obj.parent_type != item["parent_type"] or roles.get(obj.parent) != item["parent"] or inverse != item["matrix_parent_inverse"]:
            raise ValueError("旧Beta部件的用户父级/绑定已变化，保留现状")
    def driver_record(owner,owner_role):
        animation = owner.animation_data
        result = []
        for curve in animation.drivers if animation else ():
            variables = []
            for variable in curve.driver.variables:
                targets = []
                for target in variable.targets:
                    if target.id == root:
                        identity = "SELF"
                    elif target.id in root.users_scene:
                        identity = "SCENE"
                    elif target.id == owner:
                        identity = owner_role
                    else:
                        identity = roles.get(target.id,"EXTERNAL") if target.id is not None else None
                    targets.append({"id":identity,"data_path":target.data_path,"bone_target":target.bone_target,
                                    "transform_type":target.transform_type,"transform_space":target.transform_space,"rotation_mode":target.rotation_mode})
                variables.append({"name":variable.name,"type":variable.type,"targets":targets})
            result.append({"path":curve.data_path,"index":curve.array_index,"type":curve.driver.type,"expression":curve.driver.expression,
                           "use_self":curve.driver.use_self,"variables":variables})
        return result
    drivers = baseline.get("object_drivers",{})
    for obj,role in roles.items():
        if driver_record(obj,role) != drivers.get(role,[]):
            raise ValueError("旧Beta部件驱动或实际目标已被修改，保留用户编辑")
