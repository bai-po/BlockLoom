"""Actual reference Solidify outlines with exact, journaled private slots.

asset_id: oxal.outline.solidify; version: 1; policy: NEW_INSTANCE;
scope: proven exclusive Mesh objects in one instance. Identical armor shader
variants can share generated Material IDs within the same explicit transaction.
Source: asset/outline/SOURCE.json, object 平面 / Solidify.
Mesh/Key/Action/NLA are never copied or replaced.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import bpy
from mathutils import Vector

from ...core.runtime import ensure_mutation_allowed
from ...util import drivers
from ...util.animation import action_fcurves
from ...util.ownership import (
    explicit_scene_ids,
    owned_ids,
    require_no_external_users,
    require_owned,
)
from ..enchantment import enchantment
from . import outline_alpha

MARK = "oxal_mc_studio_solidify_outline_schema"
RECORD = "oxal_mc_studio_solidify_outline"
SOURCE_SHA256 = "41065561ad769a75eb90954f7f18e6ba00b1525a0e3969074af40d07bc04baec"
RESOURCE = Path(__file__).resolve().parents[2] / "asset/outline/outline_reference.blend"
_MOD_KEYS = ("pre", "solidify", "filter", "adapter")


def invalidate(transaction, obj, *, materials=()):
    """Retire this explicit operation's receipts before known native writes."""
    if transaction is None:
        return
    cache = getattr(transaction, "solidify_validation", {})
    changed_materials = tuple(materials)
    for key in tuple(cache):
        if key[1] == obj or changed_materials:
            del cache[key]
    material_cache = getattr(transaction, "solidify_material_validation", {})
    for material in changed_materials:
        for key in tuple(material_cache):
            if key[1] == material:
                del material_cache[key]
    shared = getattr(transaction, "solidify_armor_materials", {})
    for key, material in tuple(shared.items()):
        if material in changed_materials or key[2] in changed_materials:
            del shared[key]


def _validation_key(obj, state):
    """Cheap exact ID/RNA boundary, without revisiting every shader socket."""
    from ..enchantment.enchantment_outline import CONTRACT

    materials = {material for row in state.get("roles", {}).values() for material in row["materials"].values()}
    return (state, obj.data, obj.data.shape_keys, obj.users, obj.data.users,
            tuple(obj.users_collection), tuple(obj.users_scene),
            tuple((slot.link, slot.material) for slot in obj.material_slots), tuple(obj.data.materials),
            tuple(poly.material_index for poly in obj.data.polygons),
            tuple((modifier.as_pointer(), modifier.name, modifier.type,
                   modifier.node_group if modifier.type == "NODES" else getattr(modifier, "object", None),
                   _settings(modifier)) for modifier in obj.modifiers),
            drivers.capture(obj),
            {material: (material.node_tree, material.get(MARK), material.get(CONTRACT),
                        material.node_tree.get(CONTRACT) if material.node_tree else None,
                        enchantment._material_digest(material)) for material in materials})


def _material_valid(root, material, transaction):
    from ..enchantment.enchantment_outline import CONTRACT, contract_matches

    tree = material.node_tree
    signature = (tree, material.get(MARK), material.get(CONTRACT), tree.get(CONTRACT) if tree else None,
                 enchantment._material_digest(material))
    fresh = transaction is not None and material in transaction.ids
    cache = getattr(transaction, "solidify_material_validation", {}) if transaction is not None else {}
    key = (root, material)
    if not fresh and cache.get(key) == signature:
        return True
    valid = (tree is not None and enchantment.material_contract_matches(material, material.get(CONTRACT))
             and contract_matches(tree, reference_owner=material))
    if valid and transaction is not None and not fresh:
        cache[key] = signature
        transaction.solidify_material_validation = cache
    return valid


def node_marker(value):
    """Identify only these adapters for native visibility-wiring exclusions."""
    tree = getattr(value, "node_group", value)
    return isinstance(tree, bpy.types.NodeTree) and tree.get(MARK) == 1


def _reference(transaction):
    """Copy the actual bundled Material/RNA through proven append ownership."""
    from ...util.resources import AppendTransaction

    cached = getattr(transaction, "solidify_source_reference", None)
    if cached is not None:
        return cached
    with AppendTransaction(RESOURCE) as library:
        collection = library.append_collection("OXAL Outline Reference")
        objects = tuple(collection.objects)
        if len(objects) != 1 or objects[0].type != "MESH":
            raise ValueError("随包真实描边carrier入口已损坏")
        carrier = objects[0]
        mesh = carrier.data
        if not isinstance(mesh, bpy.types.Mesh):
            raise TypeError("真实描边carrier缺少Mesh")
        if len(carrier.modifiers) != 1 or carrier.modifiers[0].type != "SOLIDIFY" or len(mesh.materials) != 2:
            raise ValueError("真实描边资源缺少原Solidify/第二材质")
        source = mesh.materials[1]
        if source is None or source.node_tree is None:
            raise ValueError("真实描边资源缺少Material节点树")
        shader = source.node_tree.nodes.get("Principled BSDF")
        output = source.node_tree.nodes.get("Material Output")
        if (shader is None or shader.type != "BSDF_PRINCIPLED" or output is None
                or len(source.node_tree.nodes) != 2 or len(source.node_tree.links) != 1
                or output.inputs["Surface"].links[0].from_socket != shader.outputs["BSDF"]
                or abs(shader.inputs["Emission Strength"].default_value - 3.9) > 1e-5
                or not source.use_backface_culling):
            raise ValueError("随包真实Material的Principled/Emission3.9合同已改变")
        template = transaction.track(source.copy())
        template["oxal_mc_studio_human_source"] = "oxal.outline.reference.template." + transaction.token
        template["oxal_mc_studio_outline_generated_role"] = "reference_template"
        values = {prop.identifier: getattr(carrier.modifiers[0], prop.identifier)
                  for prop in carrier.modifiers[0].bl_rna.properties if not prop.is_readonly
                  and prop.type in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"} and prop.identifier != "name"}
        transaction.solidify_source_reference = template, values
        # Imported carriers stay unpublished. The append transaction retires
        # exactly its proven template IDs after the independent Material copy.
    return template, values


def _clone(value) -> Any:
    if isinstance(value, bpy.types.ID):
        return value
    if hasattr(value, "items"):
        return {key: _clone(part) for key, part in value.items()}
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return [_clone(part) for part in value]


def reference_settings(transaction):
    """Return the bundled Solidify recipe for another proven private carrier."""
    ensure_mutation_allowed()
    return dict(_reference(transaction)[1])


def reference_material(transaction, owner, source):
    """Build an alpha-preserving private hull material without adopting source."""
    ensure_mutation_allowed()
    legacy = source is not None and not source.use_nodes
    material = _material(transaction, None, owner, None if legacy else source,
                         "glint_outline_color", "glint_outline_brightness", None)
    if legacy:
        from ..enchantment.enchantment_outline import CONTRACT, contract

        tree = material.node_tree
        shader = tree.nodes["Principled BSDF"]
        opacity = shader.inputs["Alpha"].links[0].from_node.inputs[1]
        curve = opacity.driver_add("default_value")
        _variables(curve, [("value", source, "diffuse_color[3]")])
        curve.driver.expression = "min(max(value,0),1)"
        material["oxal_mc_studio_outline_alpha_source"] = source
        tree[CONTRACT] = contract(tree, create=True, reference_owner=material)
        material[CONTRACT] = enchantment._material_digest(material)
    return material


def _state(obj) -> dict[str, Any]:
    return _clone(obj.get(RECORD, {}))


def _owned(root, items, transaction=None):
    existing = tuple(item for item in items if item is not None and (transaction is None or item not in transaction.ids))
    if existing:
        require_owned(root, existing)


def _source(root, obj, transaction=None):
    state = _state(obj)
    if not state:
        return state, len(obj.material_slots)
    _owned(root, (obj, obj.data), transaction)
    if (state.get("schema") != 1 or state.get("root") != root or state.get("mesh") != obj.data
            or state.get("key") != obj.data.shape_keys):
        raise ValueError("Solidify描边源Mesh/Key归属已改变，未覆盖：" + obj.name)
    count = state["base_count"]
    if not 0 < count <= len(obj.material_slots):
        raise ValueError("Solidify原始材质槽登记已损坏")
    return state, count


def base_slots(root, obj, transaction=None):
    """Read-only original indices/slots; no graph hashing, evaluation or IO."""
    _, count = _source(root, obj, transaction)
    return tuple((index, obj.material_slots[index]) for index in range(count))


original_slots = base_slots


def generated_slots(root, obj, transaction=None):
    state, count = _source(root, obj, transaction)
    return tuple(range(count, len(obj.material_slots))) if state else ()


def generated_driver_paths(root, obj, transaction=None, *, role=None):
    # A source contract may ignore only our complete, still-valid structure.
    # Reading copied/custom metadata alone must never hide user drivers.
    validate(root, obj, transaction=transaction, role=role or "human")
    state, _ = _source(root, obj, transaction)
    return tuple(path for key, row in state.get("roles", {}).items() if role is None or key == role
                 for path in row["driver_paths"])


def _exclusive(root, obj, transaction=None):
    if obj.type != "MESH" or obj.library or obj.override_library or obj.data.library or obj.data.override_library:
        raise ValueError("描边需要可编辑且来源明确的Mesh对象")
    _owned(root, (obj, obj.data), transaction)
    users = bpy.data.user_map(subset=(obj.data,)).get(obj.data, set())
    owners = tuple(item for item in users if isinstance(item, bpy.types.Object) and item.data == obj.data)
    if owners != (obj,):
        raise ValueError("Mesh被其它对象共享，未复制Mesh/Key或追加共享材质槽：" + obj.name)
    # Only the explicit rebind tool may issue this operation-local receipt. It
    # proves the existing Object and its external parent-only users separately;
    # the Mesh here must still be this transaction's newly created private ID.
    if (transaction is not None and obj.data in transaction.ids
            and getattr(transaction, "solidify_rebind_targets", {}).get(obj) == obj.data):
        return
    existing = [item for item in (obj, obj.data) if transaction is None or item not in transaction.ids]
    if existing:
        scenes = tuple(obj.users_scene)
        if len(scenes) != 1:
            raise ValueError("描边目标必须属于唯一场景")
        if transaction is None or not transaction.ids:
            require_no_external_users(scenes[0], root, existing)
        else:
            domain = {root, *owned_ids(root), *transaction.ids}
            domain.update(value for item in tuple(domain) for field in ("node_tree", "shape_keys")
                          if isinstance(value := getattr(item, field, None), bpy.types.ID))
            references = explicit_scene_ids(scenes[0], root=root)
            for item, users in bpy.data.user_map(subset=existing).items():
                if any(user not in domain and not (user == scenes[0] and item not in references) for user in users):
                    raise ValueError("外部数据仍引用描边目标，未追加或移除材质槽")


def _journal(transaction, obj):
    existed, before = RECORD in obj, _state(obj)
    mesh = obj.data
    data_materials = tuple(mesh.materials)
    slots = tuple((slot.link, slot.material) for slot in obj.material_slots)
    indices = tuple(poly.material_index for poly in mesh.polygons)
    active = obj.active_material_index
    animation = drivers.capture(obj)

    def restore():
        invalidate(transaction, obj)
        if obj.data != mesh:
            raise ValueError("回滚时Mesh引用被外部修改，未写入其它数据")
        while len(mesh.materials) > len(data_materials):
            mesh.materials.pop(index=len(mesh.materials) - 1)
        while len(mesh.materials) < len(data_materials):
            mesh.materials.append(None)
        for index, material in enumerate(data_materials):
            mesh.materials[index] = material
        for index, (link, material) in enumerate(slots):
            obj.material_slots[index].link = link
            obj.material_slots[index].material = material
        for polygon, index in zip(mesh.polygons, indices, strict=True):
            polygon.material_index = index
        obj.active_material_index = active
        drivers.restore(obj, animation)
        if existed:
            restored = _clone(before)
            proven = getattr(transaction, "solidify_rollback_modifiers", {})
            for row in restored.get("roles", {}).values():
                for key in _MOD_KEYS:
                    if key not in row:
                        continue
                    spec = row[key]
                    modifier = proven.get((obj, spec["name"]))
                    if modifier is not None and modifier == obj.modifiers.get(spec["name"]):
                        # A removed native modifier cannot retain its UID when
                        # recreated. Only this rollback's explicit new wrapper
                        # may replace that exact identity binding.
                        spec["uid"] = getattr(modifier, "persistent_uid", -1)
            obj[RECORD] = restored
        elif RECORD in obj:
            del obj[RECORD]

    transaction.on_rollback(restore)
    return _state(obj)


def _referenced_digest(value, references, *, create=False):
    from ..enchantment.enchantment_outline import digest

    def normalize(part):
        if isinstance(part, bpy.types.ID):
            for key, item in references.items():
                if item == part:
                    return ["ID", key]
            if not create:
                return ["UNKNOWN_ID"]
            key = str(len(references))
            references[key] = part
            return ["ID", key]
        if hasattr(part, "items"):
            return {key: normalize(item) for key, item in part.items()}
        if isinstance(part, (str, int, float, bool, type(None))):
            return part
        return [normalize(item) for item in part]

    return digest(normalize(value))


def _driver_signature(obj, row, *, create=False):
    records = [record for record in drivers.capture(obj)["curves"] if record["path"] in row["driver_paths"]]
    references = dict(row.get("driver_refs", {}).items())
    value = _referenced_digest(records, references, create=create)
    if create:
        row["driver_refs"] = references
    return value


def _settings(modifier, *, native=False):
    from ..enchantment.enchantment_outline import _properties, digest
    from .outline_compat import modifier_record

    exclude = ("thickness", "show_viewport", "show_render") if modifier.type == "SOLIDIFY" else ()
    if modifier.type == "SOLIDIFY":
        animation = modifier.id_data.animation_data
        quality_path = modifier.path_from_id("use_quality_normals")
        if animation and any(curve.data_path == quality_path for curve in animation.drivers):
            # Animated values are covered by the exact driver contract. Old
            # source chains without this driver retain their saved RNA hash.
            exclude += ("use_quality_normals",)
    scalar = _properties(modifier, exclude=exclude)
    storage = {}
    if modifier.type == "NODES":
        if modifier.bl_rna.properties.get("properties") is None:
            storage = dict(modifier.items())
        else:
            storage = [_properties(modifier.properties), _properties(modifier.properties.inputs)]
    if not native:
        scalar, storage = modifier_record(modifier, scalar, storage)
    return digest((scalar, storage))


def _action_paths(obj):
    animation = obj.animation_data
    if animation is None:
        return set()
    curves = list(action_fcurves(animation.action, slot=getattr(animation, "action_slot", None)))
    strips = [strip for track in animation.nla_tracks for strip in track.strips]
    while strips:
        strip = strips.pop()
        curves.extend(action_fcurves(strip.action, slot=getattr(strip, "action_slot", None)))
        strips.extend(getattr(strip, "strips", ()))
    return {curve.data_path for curve in curves}


def _modifier(obj, row, key):
    spec = row[key]
    modifier = obj.modifiers.get(spec["name"])
    if (modifier is None or modifier.type != spec["type"]
            or getattr(modifier, "persistent_uid", -1) != spec["uid"]
            or list(obj.modifiers).index(modifier) != spec["position"]
            or (spec["settings"] != _settings(modifier) and spec["settings"] != _settings(modifier, native=True))):
        raise ValueError("Solidify描边修改器身份/参数/顺序含用户修改：" + obj.name)
    if key != "solidify" and modifier.node_group != row[key + "_group"]:
        raise ValueError("Solidify法线适配器引用已改变")
    return modifier


def validate(root, obj, *, transaction=None, role="human"):
    from ..enchantment.enchantment_outline import contract_matches

    state, count = _source(root, obj, transaction)
    if not state:
        return ()
    cache = getattr(transaction, "solidify_validation", {}) if transaction is not None else {}
    boundary_signature = _validation_key(obj, state) if transaction is not None else None
    receipt = cache.get((root, obj))
    if receipt is not None and receipt[0] == boundary_signature:
        return receipt[1].get(role, ())
    _exclusive(root, obj, transaction)
    if any(poly.material_index >= count for poly in obj.data.polygons):
        raise ValueError("原网格面已使用描边专属槽，未移除用户赋值")
    if len(obj.material_slots) != count + sum(row["slot_count"] for row in state["roles"].values()):
        raise ValueError("Solidify描边尾部材质槽数量已改变")
    for index in range(count):
        slot = obj.material_slots[index]
        if (slot.link != state["base_links"][index] or slot.material != state["base_materials"].get(str(index))
                or obj.data.materials[index] != state["mesh_materials"].get(str(index))):
            raise ValueError("原材质槽或链接已改变，请显式刷新描边")
    result, generated, roles = (), [], {}
    action_paths = _action_paths(obj)
    driver_paths = {curve.data_path for curve in obj.animation_data.drivers} if obj.animation_data else set()
    for key, row in sorted(state["roles"].items(), key=lambda item: item[1]["slot_start"]):
        if row.get("generation", 1) not in {1, 2, 3, 4, 5, 6}:
            raise ValueError("Solidify描边生成版本不兼容，未覆盖")
        _owned(root, (row["owner"], *row["materials"].values(), *row.get("driver_refs", {}).values()), transaction)
        values = []
        for part in _MOD_KEYS:
            if part not in row:
                continue
            modifier = _modifier(obj, row, part)
            prefix = modifier.path_from_id() + "."
            if any(path.startswith(prefix) for path in action_paths):
                raise ValueError("描边修改器存在用户Action/NLA通道，未删除")
            if any(path.startswith(prefix) and path not in row["driver_paths"] for path in driver_paths):
                raise ValueError("描边修改器存在额外用户驱动，未覆盖")
            values.append(modifier)
        if set(row["driver_paths"]) - driver_paths or _driver_signature(obj, row) != row["driver_contract"]:
            raise ValueError("Solidify描边原生驱动含用户修改")
        for offset in range(row["slot_count"]):
            slot = obj.material_slots[row["slot_start"] + offset]
            material = row["materials"][str(offset)]
            if (slot.link != "DATA" or slot.material != material or material.get(MARK) != 1
                    or not _material_valid(root, material, transaction)):
                raise ValueError("Solidify描边专属材质/槽位含用户修改")
        for part in ("pre", "filter", "adapter"):
            if part + "_group" in row:
                _owned(root, (row[part + "_group"],), transaction)
                if not contract_matches(row[part + "_group"]):
                    raise ValueError("Solidify输入/法线/侧壁适配节点含用户修改")
        if "native3d" in row:
            native = obj.modifiers.get(row["native3d"]["name"])
            if (native is None or native.type != "NODES" or native.node_group != row["native3d"]["group"]
                    or getattr(native, "persistent_uid", -1) != row["native3d"]["uid"]
                    or not row["pre"]["position"] < list(obj.modifiers).index(native) < row["solidify"]["position"]):
                raise ValueError("原生盔甲3D的精确输入链已改变，未在不同空间比较法线")
            from ..enchantment.enchantment_outline import _deformation_contract
            deform_signature, references = _deformation_contract(obj)
            if (deform_signature != row["deform_contract"] or references != row["deform_refs"]):
                raise ValueError("描边后的原生变形链已改变，未重排用户修改器")
            _owned(root, (row["native3d"]["group"], *references.values()), transaction)
        generated.extend(values)
        roles[key] = tuple(values)
        if key == role:
            result = tuple(values)
    if generated:
        last = max(list(obj.modifiers).index(part) for part in generated)
        if any(part.type not in {"ARMATURE", "LATTICE"} for part in tuple(obj.modifiers)[last + 1:]):
            raise ValueError("描边之后存在未知修改器，未改写用户链路")
    if transaction is not None and not any(material in transaction.ids
                                            for row in state["roles"].values() for material in row["materials"].values()):
        cache[(root, obj)] = (boundary_signature, roles)
        transaction.solidify_validation = cache
    return result


def _variables(curve, declarations):
    curve.driver.type = "SCRIPTED"
    for name, owner, path in declarations:
        variable = curve.driver.variables.new()
        variable.name, variable.type = name, "SINGLE_PROP"
        target = variable.targets[0]
        target.id_type, target.id, target.data_path = owner.id_type, owner, path


def _property(key, index=None):
    return '["' + key + '"]' + ("[" + str(index) + "]" if index is not None else "")


def _activation(owner, enabled, gate, follow, suppression, disable_when):
    declarations = [("enabled", owner, _property(enabled))]
    active = "enabled>0.5"
    if follow:
        flag, inherited_enabled, _, _ = follow
        declarations.extend((("follow", owner, _property(flag)), ("inherited_enabled", owner, _property(inherited_enabled))))
        active = "(inherited_enabled>0.5 if follow>0.5 else enabled>0.5)"
    if gate:
        declarations.append(("gate", owner, _property(gate)))
        active = "(" + active + ") and gate>0.5"
    if suppression:
        controller, flag, human_enabled, armor_enabled = suppression
        declarations.extend((("suppress_follow", controller, _property(flag)),
                             ("suppress_human", controller, _property(human_enabled)),
                             ("suppress_armor", controller, _property(armor_enabled))))
        active = "(" + active + ") and not (suppress_human>0.5 if suppress_follow>0.5 else suppress_armor>0.5)"
    if disable_when:
        declarations.append(("disabled", disable_when[0], _property(disable_when[1])))
        active = "(" + active + ") and disabled<0.5"
    return active, declarations


def _color(socket, owner, color, follow, multiplier):
    for axis in range(4):
        curve = socket.driver_add("default_value", axis)
        declarations = [("local", owner, _property(color, axis))]
        expression = "local"
        if follow:
            flag, _, _, inherited_color = follow
            declarations.extend((("follow", owner, _property(flag)), ("inherited", owner, _property(inherited_color, axis))))
            expression = "inherited if follow>0.5 else local"
        _variables(curve, declarations)
        curve.driver.expression = "(" + expression + ")*" + repr(multiplier[axis])


def _material(transaction, obj, owner, source, color, brightness, follow, *,
              sprite_normal=None, inward=False, shared_variant=None, unused=False):
    from ..enchantment.enchantment_outline import CONTRACT, contract

    # The shader contains no object binding: UVs, face normals and geometry
    # attributes are evaluated on each actual consumer. Armor meshes with the
    # exact same source/control/normal recipe can share this generated ID.
    cache = getattr(transaction, "solidify_armor_materials", {})
    if shared_variant is not None and shared_variant in cache:
        return cache[shared_variant]
    template, _ = _reference(transaction)
    material = transaction.track(template.copy())
    material.name = "描边 · 原工程Material"
    material[MARK] = 1
    material["oxal_mc_studio_human_source"] = "oxal.outline.generated.solidify." + transaction.token
    material["oxal_mc_studio_outline_generated_role"] = "material"
    tree = material.node_tree
    opacity = 0.0 if unused else outline_alpha.build(transaction, material, source)
    shader = tree.nodes["Principled BSDF"]
    _color(shader.inputs["Base Color"], owner, color, follow, tuple(shader.inputs["Base Color"].default_value))
    _color(shader.inputs["Emission Color"], owner, color, follow, tuple(shader.inputs["Emission Color"].default_value))
    if brightness:
        baseline = shader.inputs["Emission Strength"].default_value
        curve = shader.inputs["Emission Strength"].driver_add("default_value")
        _variables(curve, [("value", owner, _property(brightness))])
        curve.driver.expression = repr(baseline) + "*max(value,0)"
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    facing = geometry.outputs["Backfacing"]
    if sprite_normal:
        # A wide shell can fold a concave pixel quad and reverse its geometric
        # winding. Preserve the source surface's side instead of exposing that
        # near shell through the opaque item. No faces are deleted or moved.
        material.use_backface_culling = False
        attribute = tree.nodes.new("ShaderNodeAttribute")
        attribute.attribute_name = sprite_normal
        transform = tree.nodes.new("ShaderNodeVectorTransform")
        transform.vector_type = "NORMAL"
        transform.convert_from, transform.convert_to = "OBJECT", "WORLD"
        tree.links.new(attribute.outputs["Vector"], transform.inputs["Vector"])
        dot = tree.nodes.new("ShaderNodeVectorMath")
        dot.operation = "DOT_PRODUCT"
        tree.links.new(transform.outputs["Vector"], dot.inputs[0])
        tree.links.new(geometry.outputs["Incoming"], dot.inputs[1])
        near = tree.nodes.new("ShaderNodeMath")
        near.operation = "LESS_THAN" if inward else "GREATER_THAN"
        near.inputs[1].default_value = 0
        tree.links.new(dot.outputs["Value"], near.inputs[0])
        facing = near.outputs[0]
    light = tree.nodes.new("ShaderNodeLightPath")
    hidden = tree.nodes.new("ShaderNodeMath")
    hidden.operation = "MAXIMUM"
    tree.links.new(facing, hidden.inputs[0])
    tree.links.new(light.outputs["Is Shadow Ray"], hidden.inputs[1])
    visible = tree.nodes.new("ShaderNodeMath")
    visible.operation = "SUBTRACT"
    visible.inputs[0].default_value = 1
    tree.links.new(hidden.outputs[0], visible.inputs[1])
    alpha = tree.nodes.new("ShaderNodeMath")
    alpha.operation = "MULTIPLY"
    tree.links.new(visible.outputs[0], alpha.inputs[0])
    if isinstance(opacity, (int, float)):
        alpha.inputs[1].default_value = opacity
    else:
        tree.links.new(opacity, alpha.inputs[1])
    # Keep the actual Principled -> actual Material Output connection intact.
    # Principled Alpha itself supplies necessary Cycles backface transparency.
    tree.links.new(alpha.outputs[0], shader.inputs["Alpha"])
    tree[CONTRACT] = contract(tree, create=True, reference_owner=material)
    material[CONTRACT] = enchantment._material_digest(material)
    if shared_variant is not None:
        cache[shared_variant] = material
        transaction.solidify_armor_materials = cache
    return material


def _inward(mesh):
    if not mesh.vertices or not mesh.polygons:
        raise ValueError("描边源网格没有表面")
    low = Vector(tuple(min(vertex.co[axis] for vertex in mesh.vertices) for axis in range(3)))
    high = Vector(tuple(max(vertex.co[axis] for vertex in mesh.vertices) for axis in range(3)))
    center = (low + high) * .5
    return sum(poly.area * (poly.center - center).dot(poly.normal) for poly in mesh.polygons) < -1e-10


def _publish_group(transaction, obj, group, name, position):
    modifier = obj.modifiers.new(name, "NODES")
    modifier.node_group = group
    transaction.on_rollback(lambda: _remove_created(transaction, obj, name, "NODES", modifier, group))
    obj.modifiers.move(len(obj.modifiers) - 1, position)
    return modifier


def pixel_sprite(obj, *, role="enchant"):
    """Read-only routing/UI predicate; ownership is proved by apply/validate."""
    return role == "enchant" and obj.type == "MESH" and bool(obj.get("oxal_mc_studio_pixel_topology"))


def prepared_sprite(obj, *, role="enchant"):
    """Read-only generated-state hint, with no source IO or graph evaluation."""
    record = obj.get(RECORD)
    row = record.get("roles", {}).get(role, {}) if record else {}
    return row.get("generation") in {3, 5} and bool(row.get("sprite_normal"))


def _sprite_normal_group(transaction, attribute):
    from ..enchantment.enchantment_outline import CONTRACT, contract

    tree = transaction.track(bpy.data.node_groups.new("Solidify · 像素原面法线", "GeometryNodeTree"))
    tree[MARK] = 1
    tree["oxal_mc_studio_human_source"] = "oxal.outline.generated.sprite_normal." + transaction.token
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    source = tree.nodes.new("NodeGroupInput")
    output = tree.nodes.new("NodeGroupOutput")
    normal = tree.nodes.new("GeometryNodeInputNormal")
    store = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store.data_type, store.domain = "FLOAT_VECTOR", "FACE"
    store.inputs["Name"].default_value = attribute
    tree.links.new(source.outputs["Geometry"], store.inputs["Geometry"])
    tree.links.new(normal.outputs["Normal"], store.inputs["Value"])
    tree.links.new(store.outputs["Geometry"], output.inputs["Geometry"])
    tree[CONTRACT] = contract(tree, create=True)
    return tree


def _positions(state, obj):
    for row in state.get("roles", {}).values():
        for key in _MOD_KEYS:
            if key in row:
                row[key]["position"] = list(obj.modifiers).index(obj.modifiers[row[key]["name"]])


def _adapter(transaction, materials, *, slot_start=None, flip_normals=True,
             source_material_attribute=None, proxy_attribute=None):
    from ..enchantment.enchantment_outline import CONTRACT, contract

    tree = transaction.track(bpy.data.node_groups.new("Solidify · 仅翻转描边面", "GeometryNodeTree"))
    tree[MARK] = 1
    tree["oxal_mc_studio_human_source"] = "oxal.outline.generated.adapter." + transaction.token
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    source = tree.nodes.new("NodeGroupInput")
    output = tree.nodes.new("NodeGroupOutput")
    geometry = source.outputs["Geometry"]
    if slot_start is not None:
        # UV-pixel reconstruction can output only its used source materials.
        # Solidify still offsets face indices into the Object's private tail,
        # leaving those new indices unbound in that evaluated material table.
        # Capture its actual post-Solidify indices before assigning any face;
        # each exact shell slot then receives its actual private Material ID.
        attribute_name = "oxal_outline_material_index_" + transaction.token
        material_index = tree.nodes.new("GeometryNodeInputMaterialIndex")
        store = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        store.data_type, store.domain = "INT", "FACE"
        store.inputs["Name"].default_value = attribute_name
        tree.links.new(geometry, store.inputs["Geometry"])
        tree.links.new(material_index.outputs[0], store.inputs["Value"])
        geometry = store.outputs["Geometry"]
        captured = tree.nodes.new("GeometryNodeInputNamedAttribute")
        captured.data_type = "INT"
        captured.inputs["Name"].default_value = attribute_name
        lower = tree.nodes.new("ShaderNodeMath")
        lower.name, lower.operation = "Shell Slot Start", "GREATER_THAN"
        lower.inputs[1].default_value = slot_start - .5
        tree.links.new(captured.outputs["Attribute"], lower.inputs[0])
        upper = tree.nodes.new("ShaderNodeMath")
        upper.name, upper.operation = "Shell Slot End", "LESS_THAN"
        upper.inputs[1].default_value = slot_start + len(materials) - .5
        tree.links.new(captured.outputs["Attribute"], upper.inputs[0])
        shell = tree.nodes.new("FunctionNodeBooleanMath")
        shell.operation = "AND"
        tree.links.new(lower.outputs[0], shell.inputs[0])
        tree.links.new(upper.outputs[0], shell.inputs[1])
        relative = tree.nodes.new("ShaderNodeMath")
        relative.name, relative.operation = "Shell Source Slot", "SUBTRACT"
        relative.inputs[1].default_value = slot_start
        tree.links.new(captured.outputs["Attribute"], relative.inputs[0])
        source_slot = relative.outputs[0]
        if source_material_attribute:
            original_slot = tree.nodes.new("GeometryNodeInputNamedAttribute")
            original_slot.data_type = "INT"
            original_slot.inputs["Name"].default_value = source_material_attribute
            source_slot = original_slot.outputs["Attribute"]
        for offset, material in materials.items():
            match = tree.nodes.new("ShaderNodeMath")
            match.operation = "COMPARE"
            match.inputs[1].default_value, match.inputs[2].default_value = int(offset), .1
            tree.links.new(source_slot, match.inputs[0])
            selected = tree.nodes.new("FunctionNodeBooleanMath")
            selected.operation = "AND"
            tree.links.new(shell.outputs[0], selected.inputs[0])
            tree.links.new(match.outputs[0], selected.inputs[1])
            assign = tree.nodes.new("GeometryNodeSetMaterial")
            assign.inputs["Material"].default_value = material
            tree.links.new(geometry, assign.inputs["Geometry"])
            tree.links.new(selected.outputs[0], assign.inputs["Selection"])
            geometry = assign.outputs["Geometry"]
        if proxy_attribute:
            # Keep the untouched relief and only the connected proxy's shell.
            # With Solidify off there are no shell slots, so this same native
            # rule removes every proxy face without leaking a duplicate core.
            proxy = tree.nodes.new("GeometryNodeInputNamedAttribute")
            proxy.data_type = "BOOLEAN"
            proxy.inputs["Name"].default_value = proxy_attribute
            discard = tree.nodes.new("FunctionNodeBooleanMath")
            discard.operation = "XOR"
            tree.links.new(proxy.outputs["Attribute"], discard.inputs[0])
            tree.links.new(shell.outputs[0], discard.inputs[1])
            delete = tree.nodes.new("GeometryNodeDeleteGeometry")
            delete.domain, delete.mode = "FACE", "ALL"
            tree.links.new(geometry, delete.inputs["Geometry"])
            tree.links.new(discard.outputs[0], delete.inputs["Selection"])
            geometry = delete.outputs["Geometry"]
            remove_proxy = tree.nodes.new("GeometryNodeRemoveAttribute")
            remove_proxy.inputs["Name"].default_value = proxy_attribute
            tree.links.new(geometry, remove_proxy.inputs["Geometry"])
            geometry = remove_proxy.outputs["Geometry"]
        remove = tree.nodes.new("GeometryNodeRemoveAttribute")
        remove.inputs["Name"].default_value = attribute_name
        tree.links.new(geometry, remove.inputs["Geometry"])
        geometry = remove.outputs["Geometry"]
        if source_material_attribute:
            remove_source = tree.nodes.new("GeometryNodeRemoveAttribute")
            remove_source.inputs["Name"].default_value = source_material_attribute
            tree.links.new(geometry, remove_source.inputs["Geometry"])
            geometry = remove_source.outputs["Geometry"]
    selection = None
    for material in materials.values():
        selector = tree.nodes.new("GeometryNodeMaterialSelection")
        selector.inputs["Material"].default_value = material
        if selection is None:
            selection = selector.outputs["Selection"]
        else:
            combine = tree.nodes.new("FunctionNodeBooleanMath"); combine.operation = "OR"
            tree.links.new(selection, combine.inputs[0]); tree.links.new(selector.outputs["Selection"], combine.inputs[1])
            selection = combine.outputs[0]
    if flip_normals:
        flip = tree.nodes.new("GeometryNodeFlipFaces")
        tree.links.new(geometry, flip.inputs["Mesh"])
        tree.links.new(selection, flip.inputs["Selection"])
        geometry = flip.outputs["Mesh"]
    tree.links.new(geometry, output.inputs["Geometry"])
    tree[CONTRACT] = contract(tree, create=True)
    return tree


def _facial_filter(transaction, materials, attribute):
    """Retain the source surface and shell only its shared exterior faces."""
    from ..enchantment.enchantment_outline import CONTRACT, contract

    tree = transaction.track(bpy.data.node_groups.new("Solidify · 保留原生眼眉", "GeometryNodeTree"))
    tree[MARK] = 1
    tree["oxal_mc_studio_human_source"] = "oxal.outline.generated.face_filter." + transaction.token
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    source = tree.nodes.new("NodeGroupInput")
    output = tree.nodes.new("NodeGroupOutput")
    variant = tree.nodes.new("GeometryNodeInputNamedAttribute")
    variant.data_type = "INT"
    variant.inputs["Name"].default_value = attribute
    facial = tree.nodes.new("ShaderNodeMath")
    facial.operation = "GREATER_THAN"
    # The source values are exact integers: -1 exterior, 0..7 facial branch.
    facial.inputs[1].default_value = -.5
    tree.links.new(variant.outputs["Attribute"], facial.inputs[0])
    selected = None
    for material in materials.values():
        selector = tree.nodes.new("GeometryNodeMaterialSelection")
        selector.inputs["Material"].default_value = material
        if selected is None:
            selected = selector.outputs["Selection"]
        else:
            combine = tree.nodes.new("FunctionNodeBooleanMath")
            combine.operation = "OR"
            tree.links.new(selected, combine.inputs[0])
            tree.links.new(selector.outputs["Selection"], combine.inputs[1])
            selected = combine.outputs[0]
    selection = tree.nodes.new("FunctionNodeBooleanMath")
    selection.operation = "AND"
    tree.links.new(facial.outputs[0], selection.inputs[0])
    tree.links.new(selected, selection.inputs[1])
    delete = tree.nodes.new("GeometryNodeDeleteGeometry")
    delete.domain, delete.mode = "FACE", "ALL"
    tree.links.new(source.outputs["Geometry"], delete.inputs["Geometry"])
    tree.links.new(selection.outputs[0], delete.inputs["Selection"])
    tree.links.new(delete.outputs["Geometry"], output.inputs["Geometry"])
    tree[CONTRACT] = contract(tree, create=True)
    return tree


def _spec(modifier):
    return {"name": modifier.name, "type": modifier.type, "uid": getattr(modifier, "persistent_uid", -1),
            "position": list(modifier.id_data.modifiers).index(modifier), "settings": _settings(modifier)}


def _remove_created(transaction, obj, name, kind, created, group=None):
    modifier = obj.modifiers.get(name)
    if modifier is None:
        return
    try:
        exact = modifier == created
    except ReferenceError:
        exact = False
    if not exact:
        exact = modifier == getattr(transaction, "solidify_rollback_modifiers", {}).get((obj, name))
    if not exact or modifier.type != kind or (group is not None and modifier.node_group != group):
        raise ValueError("回滚修改器引用不匹配，未按名称删除")
    prefix = modifier.path_from_id() + "."
    if obj.animation_data:
        for curve in tuple(obj.animation_data.drivers):
            if curve.data_path.startswith(prefix):
                obj.driver_remove(curve.data_path, curve.array_index)
    obj.modifiers.remove(modifier)


def apply(transaction, root, obj, owner, *, enabled="outline_enabled", width="outline_width", color="outline_color",
          brightness=None, gate=None, role="human", rebuild=False, follow=None, suppression=None, source_attribute=None,
          quality_normals=None, quality_follow_key=None, disable_when=None, pixel_surface=False,
          source_indices=None):
    ensure_mutation_allowed()
    if obj.type != "MESH":
        return
    _owned(root, (owner, suppression[0] if suppression else None, disable_when[0] if disable_when else None), transaction)
    keys = {enabled, width, color, *(key for key in (brightness, gate, quality_normals, quality_follow_key) if key),
            *(follow or ())}
    if keys - set(owner.keys()):
        raise ValueError("请先初始化Solidify描边原生控制参数")
    if suppression and set(suppression[1:]) - set(suppression[0].keys()):
        raise ValueError("描边互斥控制参数缺失")
    if disable_when and disable_when[1] not in disable_when[0]:
        raise ValueError("盔甲原生3D互斥控制参数缺失")
    if quality_follow_key and (not follow or not quality_normals):
        raise ValueError("高质量法线跟随需要完整的本地与继承参数")
    if not isinstance(pixel_surface, bool):
        raise TypeError("像素表面适配开关必须为布尔值")
    if source_attribute is not None:
        attribute = obj.data.attributes.get(source_attribute)
        if attribute is None or attribute.domain != "FACE" or attribute.data_type != "INT":
            raise ValueError("头部外壳／原生面部分支属性缺失，未描边眼眉")
    controls = [enabled, width, color, brightness or "", gate or "", list(follow or ()),
                quality_normals or "", quality_follow_key or ""]
    suppression_record = {"owner": suppression[0], "keys": list(suppression[1:])} if suppression else {}
    disable_record = {"owner": disable_when[0], "key": disable_when[1]} if disable_when else {}
    current = validate(root, obj, transaction=transaction, role=role)
    from ..enchantment.enchantment_outline import _legacy_modifiers, _record

    pixel_material_table = role == "enchant" and bool(_record(obj).get("geometry"))
    proxy_attribute = None
    if pixel_material_table:
        _legacy_modifiers(root, obj, transaction=transaction)
        from ..enchantment.enchantment_geometry import (
            OUTLINE_PROXY_ATTRIBUTE,
            OUTLINE_PROXY_MARK,
        )

        pixel_record = _record(obj)["geometry"]
        pixel_tree = pixel_record["group"]
        # The exact owned tree and its complete contract were proved above.
        # This feature marker is part of that contract, never a name-based
        # permission to adopt an unrelated user's attribute or node group.
        if pixel_record.get("relief") and pixel_tree.get(OUTLINE_PROXY_MARK) == 1:
            proxy_attribute = OUTLINE_PROXY_ATTRIBUTE
    if not obj.get(RECORD):
        _exclusive(root, obj, transaction)
    fixed_head = pixel_surface and source_attribute is not None
    generation = 6 if fixed_head else 5 if proxy_attribute else 4 if pixel_surface else 3 if pixel_sprite(obj, role=role) else 2
    if current:
        row = _state(obj)["roles"][role]
        if (not rebuild and row.get("generation") == generation
                and row["owner"] == owner and row["controls"] == controls
                and row.get("source_attribute") == source_attribute
                and bool(row.get("pixel_material_table")) == pixel_material_table
                and row.get("outline_proxy_attribute") == proxy_attribute
                and row["suppression"] == suppression_record and row.get("disable_when") == disable_record
                and row.get("source_indices") == (list(source_indices) if source_indices is not None else None)):
            return
        invalidate(transaction, obj, materials=tuple(row["materials"].values()))
        remove(transaction, root, obj, role=role)
    invalidate(transaction, obj)
    state = _journal(transaction, obj)
    if not state:
        count = len(obj.material_slots)
        if count < 1 or any(poly.material_index >= count for poly in obj.data.polygons):
            raise ValueError("描边源需要有效原材质槽，未添加会改变原表面的槽")
        state = {"schema": 1, "root": root, "mesh": obj.data, "base_count": count,
                 "base_links": [slot.link for slot in obj.material_slots],
                 "base_materials": {str(index): slot.material for index, slot in enumerate(obj.material_slots) if slot.material},
                 "mesh_materials": {str(index): material for index, material in enumerate(obj.data.materials) if material},
                 "animation_existed": obj.animation_data is not None, "roles": {}}
        if obj.data.shape_keys:
            state["key"] = obj.data.shape_keys
    count = state["base_count"]
    if source_indices is not None:
        source_indices = tuple(source_indices)
        if (not source_indices or any(type(index) is not int or not 0 <= index < count for index in source_indices)
                or any(poly.material_index not in source_indices for poly in obj.data.polygons)):
            raise ValueError("显式描边源表面索引无效，未忽略实际使用的材质")
    start = len(obj.material_slots)
    if start + count > 32767:
        raise ValueError("Solidify描边材质槽超出原生偏移范围")
    inward = _inward(obj.data)
    sprite_normal = normal_group = normal_pre = None
    if pixel_sprite(obj, role=role):
        sprite_normal = "oxal_outline_sprite_normal_" + transaction.token
        if obj.data.attributes.get(sprite_normal) is not None:
            raise ValueError("像素描边原面法线属性与现有数据冲突，未覆盖")
        normal_group = _sprite_normal_group(transaction, sprite_normal)
        normal_pre = _publish_group(transaction, obj, normal_group,
                                    "描边 · 像素原面法线 · " + transaction.token[:8], len(obj.modifiers))
    recipe = tuple(tuple(value) if isinstance(value, list) else value for value in controls)
    materials = {}
    for index in range(count):
        source_material = obj.material_slots[index].material
        variant = ((root, owner, source_material, recipe, sprite_normal, inward, SOURCE_SHA256)
                   if role == "armor" else None)
        materials[str(index)] = _material(transaction, obj, owner, source_material, color, brightness, follow,
                                           sprite_normal=sprite_normal, inward=inward, shared_variant=variant,
                                           unused=source_indices is not None and index not in source_indices)
    for material in materials.values():
        obj.data.materials.append(material)
    name = "描边 · Solidify · " + role + " · " + transaction.token[:8]
    modifier = obj.modifiers.new(name, "SOLIDIFY")
    transaction.on_rollback(lambda: _remove_created(transaction, obj, name, "SOLIDIFY", modifier))
    _, source_rna = _reference(transaction)
    for attribute, value in source_rna.items():
        setattr(modifier, attribute, value)
    if pixel_surface:
        # Native 3D skin layers contain collapsed inner walls when outward
        # thickness is zero. Simple Solidify averages opposing normals there,
        # folding the reversed hull into visible triangles. Its native complex
        # constraints mode resolves those surfaces without editing the source
        # Mesh/Key or generating another hull. Open rim walls would copy the
        # skin into this offset shell, so only its reversed surface is retained.
        modifier.solidify_mode = "NON_MANIFOLD"
        modifier.nonmanifold_merge_threshold = 0.
        modifier.use_rim = False
        if fixed_head:
            # The unified head also contains animated eyelid/brow branches.
            # Their near-coplanar faces make the constraint-thickness solver
            # ill-conditioned in migrated poses, even though the post-filter
            # removes those shell faces. Fixed displacement bounds the hull
            # without changing the source Mesh/Key or the native facial rig.
            modifier.nonmanifold_thickness_mode = "FIXED"
    if proxy_attribute:
        # Only the reversed proxy shell survives the post adapter. Solidify's
        # open-edge bridges use the base material offset, so they are neither
        # source faces nor shell faces and must not be generated on the relief
        # branch. The proxy already supplies its own closed regional volume.
        modifier.use_rim = False
    modifier.material_offset, modifier.material_offset_rim = start, 0
    modifier.use_flip_normals = not inward
    active, declarations = _activation(owner, enabled, gate, follow, suppression, disable_when)
    declarations = [*declarations, ("width", owner, _property(width))]
    effective_width = "width"
    if follow:
        declarations.append(("inherited_width", owner, _property(follow[2])))
        effective_width = "(inherited_width if follow>0.5 else width)"
    paths = []
    for attribute in ("thickness", "show_viewport", "show_render"):
        curve = modifier.driver_add(attribute)
        _variables(curve, declarations)
        curve.driver.expression = (("-" if inward else "") + "max(" + effective_width + ",0) if (" + active + ") else 0"
                                   if attribute == "thickness" else "(" + active + ") and " + effective_width + ">0")
        paths.append(curve.data_path)
    modifier.use_quality_normals = True
    if quality_normals and not sprite_normal:
        curve = modifier.driver_add("use_quality_normals")
        quality_variables = [("local_quality", owner, _property(quality_normals))]
        quality_expression = "local_quality>0.5"
        if quality_follow_key:
            assert follow is not None
            quality_variables.extend((("follow", owner, _property(follow[0])),
                                      ("inherited_quality", owner, _property(quality_follow_key))))
            quality_expression = "inherited_quality>0.5 if follow>0.5 else local_quality>0.5"
        _variables(curve, quality_variables)
        curve.driver.expression = quality_expression
        paths.append(curve.data_path)
    row = {"generation": generation, "owner": owner, "slot_start": start, "slot_count": count, "materials": materials,
           "solidify": _spec(modifier), "driver_paths": paths,
           "controls": controls, "suppression": suppression_record, "disable_when": disable_record,
           "inward": inward, "source_sha256": SOURCE_SHA256}
    if source_indices is not None:
        row["source_indices"] = list(source_indices)
    if source_attribute:
        group = _facial_filter(transaction, materials, source_attribute)
        face_filter = _publish_group(transaction, obj, group,
                                     "描边 · 保留原生眼眉 · " + transaction.token[:8], len(obj.modifiers))
        row["filter"], row["filter_group"], row["source_attribute"] = _spec(face_filter), group, source_attribute
    if sprite_normal:
        assert normal_pre is not None and normal_group is not None
        row["pre"], row["pre_group"], row["sprite_normal"] = _spec(normal_pre), normal_group, sprite_normal
    if inward or pixel_material_table:
        source_material_attribute = None
        if pixel_material_table:
            from ..enchantment.enchantment_geometry import MATERIAL_ATTRIBUTE

            pixel_tree = _record(obj)["geometry"]["group"]
            # Only consume the named field when this exact, already validated
            # generated pixel tree actually writes it. Old saved trees retain
            # their historical post-index mapping until explicitly rebuilt.
            if any(node.bl_idname == "GeometryNodeStoreNamedAttribute"
                   and node.domain == "FACE" and node.data_type == "INT"
                   and not node.inputs["Name"].is_linked
                   and node.inputs["Name"].default_value == MATERIAL_ATTRIBUTE
                   for node in pixel_tree.nodes):
                source_material_attribute = MATERIAL_ATTRIBUTE
        group = _adapter(transaction, materials, slot_start=start if pixel_material_table else None,
                         flip_normals=inward, source_material_attribute=source_material_attribute,
                         proxy_attribute=proxy_attribute)
        adapter_name = "描边 · 仅法线适配 · " + role + " · " + transaction.token[:8]
        adapter = _publish_group(transaction, obj, group, adapter_name, len(obj.modifiers))
        row["adapter"], row["adapter_group"] = _spec(adapter), group
        if pixel_material_table:
            row["pixel_material_table"] = True
        if proxy_attribute:
            row["outline_proxy_attribute"] = proxy_attribute
    row["driver_contract"] = _driver_signature(obj, row, create=True)
    state["roles"][role] = row
    _positions(state, obj)
    obj[RECORD] = state
    owner.update_tag(); obj.update_tag()


def retire_for_rebind(transaction, root, obj):
    """Retire exact generated modifiers only; current Mesh/slots stay untouched.

    This explicit path does not certify changed geometry or material slots and
    never weakens validate(). Recorded materials are retained, even if edited.
    """
    from ..enchantment.enchantment_outline import _properties, contract_matches

    ensure_mutation_allowed()
    state = _state(obj)
    if (state.get("schema") != 1 or state.get("root") != root
            or set(state.get("roles", {})) != {"enchant"}):
        raise ValueError("重新绑定需要此实例的唯一附魔Solidify精确登记")
    if (not isinstance(state.get("mesh"), bpy.types.Mesh)
            or state.get("key") != state["mesh"].shape_keys):
        raise ValueError("旧描边记录的Mesh/Key实际来源不完整，未重新绑定")
    _owned(root, (root, obj, state.get("mesh"), state.get("key")), transaction)
    row = state["roles"]["enchant"]
    if (row.get("owner") != root or row.get("generation") not in {1, 2, 3, 4, 5}
            or row.get("source_sha256") != SOURCE_SHA256 or "native3d" in row):
        raise ValueError("旧描边控制来源或生成版本无法确认，未重新绑定")
    _owned(root, (*row["materials"].values(), *row.get("driver_refs", {}).values()), transaction)
    action_paths = _action_paths(obj)
    current_paths = {curve.data_path for curve in obj.animation_data.drivers} if obj.animation_data else set()
    values = []
    for key in _MOD_KEYS:
        if key not in row:
            continue
        modifier = _modifier(obj, row, key)
        prefix = modifier.path_from_id() + "."
        if any(path.startswith(prefix) for path in action_paths):
            raise ValueError("旧描边修改器存在用户Action/NLA，未重新绑定：" + modifier.name)
        if any(path.startswith(prefix) and path not in row["driver_paths"] for path in current_paths):
            raise ValueError("旧描边修改器存在额外用户驱动，未重新绑定：" + modifier.name)
        if key != "solidify":
            group = row[key + "_group"]
            _owned(root, (group,), transaction)
            if not contract_matches(group):
                raise ValueError("旧描边生成节点含用户修改，未重新绑定：" + modifier.name)
        values.append(modifier)
    if set(row["driver_paths"]) - current_paths or _driver_signature(obj, row) != row["driver_contract"]:
        raise ValueError("旧描边原生驱动含用户修改，未重新绑定")
    solid = next(modifier for modifier in values if modifier.type == "SOLIDIFY")
    allowed_paths = {solid.path_from_id(name) for name in
                     ("thickness", "show_viewport", "show_render", "use_quality_normals")}
    if set(row["driver_paths"]) - allowed_paths:
        raise ValueError("旧描边驱动登记包含非生成字段，未删除")
    if transaction is None:
        return tuple(values)
    invalidate(transaction, obj)
    _journal(transaction, obj)
    snapshots = [(part.name, part.type, list(obj.modifiers).index(part), _properties(part),
                  part.node_group if part.type == "NODES" else None) for part in values]

    def restore():
        for name, kind, position, properties, group in snapshots:
            if obj.modifiers.get(name) is not None:
                raise ValueError("回滚旧描边时出现同名用户修改器，未覆盖")
            modifier = obj.modifiers.new(name, kind)
            for key, value in properties:
                setattr(modifier, key, value)
            if group is not None:
                modifier.node_group = group
            obj.modifiers.move(len(obj.modifiers) - 1, min(position, len(obj.modifiers) - 1))
            proven = getattr(transaction, "solidify_rollback_modifiers", {})
            proven[(obj, name)] = modifier
            transaction.solidify_rollback_modifiers = proven

    transaction.on_rollback(restore)
    for path in row["driver_paths"]:
        curve = next(curve for curve in obj.animation_data.drivers if curve.data_path == path)
        obj.driver_remove(path, curve.array_index)
    for modifier in reversed(values):
        obj.modifiers.remove(modifier)
    del obj[RECORD]
    obj.update_tag()
    return tuple(part[0] for part in snapshots)


def prepare_material_bindings(transaction, root, obj) -> Callable[[], None]:
    """Validate first, then return publication for a known alpha-neutral swap.

    Only actual base-slot bindings change. Roles, geometry, modifier identity,
    native drivers and generated material/node contracts keep their exact state.
    """
    validate(root, obj, transaction=transaction, role="enchant")
    before = _state(obj)
    if not before:
        return lambda: None

    def restore():
        obj[RECORD] = before
        invalidate(transaction, obj)

    transaction.on_rollback(restore)

    def publish():
        if obj.data != before["mesh"] or _state(obj) != before:
            raise ValueError("材质更新期间描边源或记录已变更，请重新绑定描边")
        count = before["base_count"]
        if len(obj.material_slots) != count + sum(row["slot_count"] for row in before["roles"].values()):
            raise ValueError("材质更新期间描边槽数量已改变，请重新绑定描边")
        state = _clone(before)
        state["base_links"] = [obj.material_slots[index].link for index in range(count)]
        state["base_materials"] = {str(index): obj.material_slots[index].material for index in range(count)
                                   if obj.material_slots[index].material is not None}
        state["mesh_materials"] = {str(index): obj.data.materials[index] for index in range(count)
                                   if obj.data.materials[index] is not None}
        obj[RECORD] = state
        invalidate(transaction, obj)

    return publish


def remove(transaction, root, obj, *, role="human"):
    ensure_mutation_allowed()
    values = validate(root, obj, transaction=transaction, role=role)
    if not values:
        return
    from ..enchantment.enchantment_outline import CONTRACT, _properties, contract

    invalidate(transaction, obj)
    state = _journal(transaction, obj)
    row = state["roles"][role]
    snapshots = [(part.name, part.type, list(obj.modifiers).index(part), _properties(part),
                  part.node_group if part.type == "NODES" else None) for part in values]

    def restore():
        for name, kind, position, properties, group in snapshots:
            modifier = obj.modifiers.new(name, kind)
            for key, value in properties:
                setattr(modifier, key, value)
            if group:
                modifier.node_group = group
            obj.modifiers.move(len(obj.modifiers) - 1, min(position, len(obj.modifiers) - 1))
            proven = getattr(transaction, "solidify_rollback_modifiers", {})
            proven[(obj, name)] = modifier
            transaction.solidify_rollback_modifiers = proven

    transaction.on_rollback(restore)
    for path in row["driver_paths"]:
        curve = next(curve for curve in obj.animation_data.drivers if curve.data_path == path)
        obj.driver_remove(path, curve.array_index)
    for part in reversed(values):
        obj.modifiers.remove(part)
    for _ in range(row["slot_count"]):
        obj.data.materials.pop(index=row["slot_start"])
    for key, later in state["roles"].items():
        if key == role or later["slot_start"] < row["slot_start"]:
            continue
        later["slot_start"] -= row["slot_count"]
        modifier = obj.modifiers[later["solidify"]["name"]]
        old_offset = modifier.material_offset
        def restore_offset(modifier=modifier, name=modifier.name, value=old_offset):
            current = obj.modifiers.get(name)
            if current is None:
                raise ValueError("回滚时后继描边修改器缺失")
            try:
                exact = current == modifier
            except ReferenceError:
                exact = False
            if not exact:
                exact = current == getattr(transaction, "solidify_rollback_modifiers", {}).get((obj, name))
            if not exact:
                raise ValueError("回滚时后继修改器身份改变，未按名称改写")
            current.material_offset = value
        transaction.on_rollback(restore_offset)
        modifier.material_offset = later["slot_start"]
        later["solidify"] = _spec(modifier)
        if "adapter" in later:
            tree = later["adapter_group"]
            # Material Selection binds actual private Material IDs, so slot
            # reindexing needs no node/contract rewrite. The early QA adapter
            # used explicit bounds; keep that exact known form reversible.
            if tree.nodes.get("Shell Slot Start") is not None:
                lower, upper = tree.nodes["Shell Slot Start"].inputs[1], tree.nodes["Shell Slot End"].inputs[1]
                relative = tree.nodes.get("Shell Source Slot")
                previous = (lower.default_value, upper.default_value, tree.get(CONTRACT),
                            relative.inputs[1].default_value if relative is not None else None)
                def restore_adapter(tree=tree, previous=previous):
                    tree.nodes["Shell Slot Start"].inputs[1].default_value = previous[0]
                    tree.nodes["Shell Slot End"].inputs[1].default_value = previous[1]
                    if previous[3] is not None:
                        tree.nodes["Shell Source Slot"].inputs[1].default_value = previous[3]
                    tree[CONTRACT] = previous[2]
                transaction.on_rollback(restore_adapter)
                lower.default_value = later["slot_start"] - .5
                upper.default_value = later["slot_start"] + later["slot_count"] - .5
                if relative is not None:
                    relative.inputs[1].default_value = later["slot_start"]
                tree[CONTRACT] = contract(tree, create=True)
            later["adapter"] = _spec(obj.modifiers[later["adapter"]["name"]])
    del state["roles"][role]
    _positions(state, obj)
    if state["roles"]:
        obj[RECORD] = state
    else:
        del obj[RECORD]
        if (not state["animation_existed"] and obj.animation_data and obj.animation_data.action is None
                and not obj.animation_data.nla_tracks and not obj.animation_data.drivers):
            obj.animation_data_clear()
    obj.update_tag()


def prepared(obj, *, role="human"):
    """Read-only feature state, with no material/source evaluation."""
    return role in _state(obj).get("roles", {})
