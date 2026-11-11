"""Native reference hulls borrowing an external object's evaluated geometry.

asset_id: oxal.enchantment.external_outline; asset_version: 1;
policy: NEW_INSTANCE on explicit preparation; scope: one exact external target
and its private effect controller. Source: asset/outline/outline_reference.blend.
The user's Object, Mesh, modifiers, Key and animation never join our ownership.
Only the new carrier, native groups and alpha-only hull materials are owned.
"""

import logging
from typing import Any

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util import drivers
from ...util.ownership import (
    DATA_UID,
    REGISTRY,
    explicit_scene_ids,
    owned_ids,
    prune_owned,
    require_owned,
)
from ..outline import outline_solidify
from . import enchantment
from .enchantment_outline import CONTRACT, contract, contract_matches

STATE = "outline_external_state"
TYPES = frozenset({"MESH", "CURVE", "SURFACE", "FONT", "META"})
logger = logging.getLogger(__name__)


def _clone(value) -> Any:
    if isinstance(value, bpy.types.ID):
        return value
    if hasattr(value, "items"):
        return {key: _clone(part) for key, part in value.items()}
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return [_clone(part) for part in value]


def _owned(transaction, owner, items):
    items = tuple(item for item in items if transaction is None or item not in transaction.ids)
    if items:
        require_owned(owner, items)


def prepared(owner):
    """Cheap UI metadata only; no evaluation, hashing or source IO."""
    state = owner.get(STATE)
    return bool(state and state.get("schema") == 1
                and isinstance(state.get("proxy"), bpy.types.Object))


def _proxy_contract(proxy, state, *, create=False):
    references = dict(state.get("references", {}).items())
    value = (
        proxy.data, tuple("SCENE_MASTER_COLLECTION" if collection.is_embedded_data else collection
                          for collection in proxy.users_collection), proxy.parent,
        tuple(proxy.location), tuple(proxy.rotation_euler), tuple(proxy.scale),
        proxy.hide_select,
        tuple((modifier.name, modifier.type, outline_solidify._settings(modifier))
              for modifier in proxy.modifiers),
        tuple((constraint.name, constraint.type, constraint.target, constraint.influence,
               constraint.owner_space, constraint.target_space, constraint.mix_mode)
              for constraint in proxy.constraints),
        drivers.capture(proxy),
    )
    result = outline_solidify._referenced_digest(value, references, create=create)
    if create:
        state["references"] = references
    return result


def validate(scene, target, owner, *, transaction=None):
    """Validate private resources only; a borrowed target is never adopted."""
    ensure_mutation_allowed()
    state = _clone(owner.get(STATE, {}))
    if not state:
        return None
    if (state.get("schema") != 1 or state.get("target") != target
            or owner.get("oxal_external_target") != target
            or scene.objects.get(target.name) != target):
        raise ValueError("外部描边目标或所属场景已改变，未接管")
    proxy, mesh = state.get("proxy"), state.get("mesh")
    groups = tuple(state.get("groups", {}).values())
    materials = tuple(state.get("materials", {}).values())
    if (not isinstance(proxy, bpy.types.Object) or proxy.type != "MESH"
            or proxy.data != mesh or not isinstance(mesh, bpy.types.Mesh)
            or mesh.vertices or mesh.edges or mesh.polygons or mesh.shape_keys
            or scene.objects.get(proxy.name) != proxy
            or proxy.animation_data is None or proxy.animation_data.action
            or proxy.animation_data.nla_tracks
            or len(proxy.modifiers) != 3 or len(proxy.constraints) != 1
            or tuple(proxy.users_scene) != (scene,)):
        raise ValueError("外部描边私有载体或动画已被修改，未覆盖")
    _owned(transaction, owner, (owner, proxy, mesh, *groups, *materials))
    for group in groups:
        if not isinstance(group, bpy.types.NodeTree) or not contract_matches(group):
            raise ValueError("外部描边原生几何节点已被修改，未覆盖")
    for material in materials:
        if (not isinstance(material, bpy.types.Material) or material.node_tree is None
                or not enchantment.material_contract_matches(material, material.get(CONTRACT))
                or not contract_matches(material.node_tree, reference_owner=material)):
            raise ValueError("外部描边专属材质已被修改，未覆盖")
    if state.get("proxy_contract") != _proxy_contract(proxy, state):
        raise ValueError("外部描边修改器、跟随或驱动已被修改，未覆盖")
    return proxy


def _material_sources(target):
    """Inspect the actual current surface once, inside the explicit Tool."""
    if target.type not in TYPES:
        raise ValueError("外部描边需要网格或可求值为表面的曲线／文本对象")
    from . import enchantment_external_assigners as assigners

    candidates = tuple(dict.fromkeys((*(slot.material for slot in target.material_slots),
                                      *assigners.materials(target))))
    # The chosen source can retain procedural geometry; only unknowable native
    # material selection is refused. Nested groups with fixed materials work.
    def check(tree, seen):
        if tree in seen:
            return
        seen.add(tree)
        for node in tree.nodes:
            if (node.bl_idname == "GeometryNodeSetMaterial"
                    and node.inputs["Material"].is_linked
                    and not any(material is not None for material in candidates)):
                raise ValueError("几何节点动态选择材质，无法确认描边透明度：" + target.name)
            child = getattr(node, "node_tree", None)
            if child is not None:
                check(child, seen)

    for modifier in target.modifiers:
        if modifier.type == "NODES" and modifier.node_group:
            check(modifier.node_group, set())
    evaluated = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh(preserve_all_data_layers=True,
                             depsgraph=bpy.context.evaluated_depsgraph_get())
    try:
        if mesh is None or not mesh.polygons:
            raise ValueError("所选对象没有可描边表面；曲线需要填充或倒角：" + target.name)
        materials = []
        for polygon in mesh.polygons:
            material = mesh.materials[polygon.material_index] if polygon.material_index < len(mesh.materials) else None
            if material is not None:
                # to_mesh() may expose evaluated Material wrappers. A native
                # Object Info/Replace Material graph must reference their
                # persistent original IDs, never a temporary evaluated copy.
                material = material.original
            if material not in materials:
                materials.append(material)
        return tuple(dict.fromkeys((*materials, *(material.original if material is not None else None
                                                  for material in candidates))))
    finally:
        evaluated.to_mesh_clear()


def _tree(transaction, name):
    tree = transaction.track(bpy.data.node_groups.new(name, "GeometryNodeTree"))
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    return tree, tree.nodes.new("NodeGroupOutput")


def _input_group(transaction, target, attribute):
    tree, output = _tree(transaction, "外部描边 · 原生表面引用")
    source = tree.nodes.new("GeometryNodeObjectInfo")
    source.transform_space = "RELATIVE"
    source.inputs["Object"].default_value = target
    source.inputs["As Instance"].default_value = False
    realize = tree.nodes.new("GeometryNodeRealizeInstances")
    tree.links.new(source.outputs["Geometry"], realize.inputs["Geometry"])
    geometry = realize.outputs["Geometry"]
    overrides = []
    for index, slot in enumerate(target.material_slots):
        original = target.data.materials[index] if index < len(target.data.materials) else None
        if slot.link == "OBJECT" and slot.material != original:
            overrides.append((index, original, slot.material))
    if overrides:
        # Object Info can expose the Data material table even when rendering
        # the source Object uses private Object-linked overrides. Remap only
        # the exact original slot AND original material identity. A user's GN
        # surfaces with another material are left intact.
        index_attribute = attribute + "_material_index"
        store_index = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        store_index.data_type, store_index.domain = "INT", "FACE"
        store_index.inputs["Name"].default_value = index_attribute
        material_index = tree.nodes.new("GeometryNodeInputMaterialIndex")
        tree.links.new(material_index.outputs["Material Index"], store_index.inputs["Value"])
        tree.links.new(geometry, store_index.inputs["Geometry"])
        geometry = store_index.outputs["Geometry"]
        original_index = tree.nodes.new("GeometryNodeInputNamedAttribute")
        original_index.data_type = "INT"
        original_index.inputs["Name"].default_value = index_attribute
        for index, before, after in overrides:
            equal = tree.nodes.new("ShaderNodeMath")
            equal.operation = "COMPARE"
            equal.inputs[1].default_value = index
            equal.inputs[2].default_value = .1
            tree.links.new(original_index.outputs["Attribute"], equal.inputs[0])
            material = tree.nodes.new("GeometryNodeMaterialSelection")
            material.inputs["Material"].default_value = before
            selection = tree.nodes.new("FunctionNodeBooleanMath")
            selection.operation = "AND"
            tree.links.new(equal.outputs[0], selection.inputs[0])
            tree.links.new(material.outputs["Selection"], selection.inputs[1])
            assign = tree.nodes.new("GeometryNodeSetMaterial")
            assign.inputs["Material"].default_value = after
            tree.links.new(geometry, assign.inputs["Geometry"])
            tree.links.new(selection.outputs["Boolean"], assign.inputs["Selection"])
            geometry = assign.outputs["Geometry"]
    store = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store.data_type, store.domain = "FLOAT_VECTOR", "POINT"
    store.inputs["Name"].default_value = attribute
    position = tree.nodes.new("GeometryNodeInputPosition")
    tree.links.new(position.outputs["Position"], store.inputs["Value"])
    tree.links.new(geometry, store.inputs["Geometry"])
    tree.links.new(store.outputs["Geometry"], output.inputs["Geometry"])
    tree[CONTRACT] = contract(tree, create=True)
    return tree


def _shell_group(transaction, attribute, sources, materials):
    tree, output = _tree(transaction, "外部描边 · 仅保留原生外壳")
    source = tree.nodes.new("NodeGroupInput")
    position = tree.nodes.new("GeometryNodeInputPosition")
    stored = tree.nodes.new("GeometryNodeInputNamedAttribute")
    stored.data_type = "FLOAT_VECTOR"
    stored.inputs["Name"].default_value = attribute
    distance = tree.nodes.new("ShaderNodeVectorMath")
    distance.operation = "DISTANCE"
    tree.links.new(position.outputs["Position"], distance.inputs[0])
    tree.links.new(stored.outputs["Attribute"], distance.inputs[1])
    original = tree.nodes.new("ShaderNodeMath")
    original.operation = "LESS_THAN"
    original.inputs[1].default_value = 1e-8
    tree.links.new(distance.outputs["Value"], original.inputs[0])
    delete = tree.nodes.new("GeometryNodeDeleteGeometry")
    delete.domain, delete.mode = "FACE", "ALL"
    tree.links.new(source.outputs["Geometry"], delete.inputs["Geometry"])
    tree.links.new(original.outputs[0], delete.inputs["Selection"])
    geometry = delete.outputs["Geometry"]
    for base, material in zip(sources, materials, strict=True):
        replace = tree.nodes.new("GeometryNodeReplaceMaterial")
        replace.inputs["Old"].default_value = base
        replace.inputs["New"].default_value = material
        tree.links.new(geometry, replace.inputs["Geometry"])
        geometry = replace.outputs["Geometry"]
    for name in (attribute, attribute + "_material_index"):
        remove = tree.nodes.new("GeometryNodeRemoveAttribute")
        remove.inputs["Name"].default_value = name
        tree.links.new(geometry, remove.inputs["Geometry"])
        geometry = remove.outputs["Geometry"]
    tree.links.new(geometry, output.inputs["Geometry"])
    tree[CONTRACT] = contract(tree, create=True)
    return tree


def _driver(owner, subject, path, expression, variables):
    curve = subject.driver_add(path)
    curve.keyframe_points.clear()
    for modifier in tuple(curve.modifiers):
        curve.modifiers.remove(modifier)
    curve.driver.type, curve.driver.expression = "SCRIPTED", expression
    for name, target, property_path in variables:
        variable = curve.driver.variables.new()
        variable.name, variable.type = name, "SINGLE_PROP"
        variable.targets[0].id_type = target.id_type
        variable.targets[0].id = target
        variable.targets[0].data_path = property_path
    return curve


def remove(transaction, scene, target, owner):
    """Stage retirement; ``cleanup(transaction)`` runs only after commit."""
    ensure_mutation_allowed()
    proxy = validate(scene, target, owner, transaction=transaction)
    if proxy is None:
        return
    if not isinstance(proxy.data, bpy.types.Mesh):
        raise TypeError("外部描边载体缺少私有网格")
    _retire_check(scene, owner, proxy)
    state, collections = _clone(owner[STATE]), tuple(proxy.users_collection)

    def restore():
        owner[STATE] = state
        for collection in collections:
            if collection.objects.get(proxy.name) != proxy:
                collection.objects.link(proxy)

    transaction.on_rollback(restore)
    del owner[STATE]
    for collection in collections:
        collection.objects.unlink(proxy)
    retired = getattr(transaction, "external_outline_retired", [])
    retired.append((owner, proxy, proxy.data))
    transaction.external_outline_retired = retired


def _retire_check(scene, owner, proxy):
    """Allow the validated carrier's borrowed collection membership only.

    The proxy is deliberately linked beside its source so view-layer exclusion
    remains native. Those exact, contract-checked memberships are not an import
    of the user's Collections into our ownership domain. Every other external
    pointer, including explicit Scene properties, still prevents retirement.
    """
    require_owned(owner, (proxy, proxy.data))
    domain = {owner, *owned_ids(owner)}
    domain.update(value for item in tuple(domain) for field in ("node_tree", "shape_keys")
                  if isinstance(value := getattr(item, field, None), bpy.types.ID))
    collections = tuple(proxy.users_collection)
    explicit = explicit_scene_ids(scene, root=owner)
    for item, users in bpy.data.user_map(subset=(proxy, proxy.data)).items():
        for user in users:
            if user in domain:
                continue
            if item == proxy and user in collections:
                continue
            if user == scene and item not in explicit:
                continue
            raise ValueError("外部数据显式引用描边载体，未移除：" + proxy.name)


def apply(transaction, scene, target, owner):
    """Prepare a private native hull without rewriting the borrowed target."""
    ensure_mutation_allowed()
    if owner.get("oxal_external_target") != target or scene.objects.get(target.name) != target:
        raise ValueError("外部附魔控制器没有引用当前实际对象")
    controls = {"glint_outline_enabled", "glint_outline_width", "glint_outline_color",
                "glint_outline_brightness", "glint_outline_quality_normals"}
    if controls - set(owner.keys()):
        raise ValueError("请先初始化外部附魔描边参数")
    sources = _material_sources(target)
    remove(transaction, scene, target, owner)
    mesh = transaction.track(bpy.data.meshes.new("外部描边 · 私有载体"))
    proxy = transaction.track(bpy.data.objects.new("描边 · " + target.name, mesh))
    proxy["oxal_external_outline_proxy"] = 1
    proxy["oxal_external_outline_target"] = target
    proxy.hide_select = True
    constraint = proxy.constraints.new("COPY_TRANSFORMS")
    constraint.target = target
    for collection in target.users_collection:
        collection.objects.link(proxy)
    attribute = "oxal_external_outline_source_" + transaction.token
    before = _input_group(transaction, target, attribute)
    materials = tuple(outline_solidify.reference_material(transaction, owner, source) for source in sources)
    after = _shell_group(transaction, attribute, sources, materials)
    modifier = proxy.modifiers.new("外部描边 · 评估表面", "NODES")
    modifier.node_group = before
    solidify = proxy.modifiers.new("外部描边 · 原工程Solidify", "SOLIDIFY")
    for key, value in outline_solidify.reference_settings(transaction).items():
        setattr(solidify, key, value)
    # Keep the original side stationary. The post-adapter discards exactly it,
    # retaining the reference Solidify displacement and reversed hull normals.
    solidify.offset, solidify.material_offset, solidify.material_offset_rim = 1., 0, 0
    solidify.use_rim = False
    solidify.use_quality_normals = True
    declarations = [("enabled", owner, '["glint_outline_enabled"]'),
                    ("width", owner, '["glint_outline_width"]')]
    _driver(owner, solidify, "thickness", "max(width,0) if enabled>0.5 else 0", declarations)
    _driver(owner, solidify, "use_quality_normals", "value>0.5",
            [("value", owner, '["glint_outline_quality_normals"]')])
    modifier = proxy.modifiers.new("外部描边 · 仅外壳", "NODES")
    modifier.node_group = after
    for field in ("hide_viewport", "hide_render"):
        _driver(owner, proxy, field, "value>0.5", [("value", target, field)])
    state = {"schema": 1, "target": target, "proxy": proxy, "mesh": mesh,
             "groups": {"source": before, "shell": after},
             "materials": {str(index): material for index, material in enumerate(materials)}}
    state["proxy_contract"] = _proxy_contract(proxy, state, create=True)
    existed, previous = STATE in owner, _clone(owner.get(STATE, {}))

    def restore():
        if existed:
            owner[STATE] = previous
        elif STATE in owner:
            del owner[STATE]

    transaction.on_rollback(restore)
    owner[STATE] = state
    proxy.update_tag()
    owner.update_tag()
    return proxy


def cleanup(transaction):
    """Retire only staged exact private carriers after successful publication."""
    if not transaction.committed:
        raise ValueError("外部描边只能在事务提交后回收旧载体")
    for owner, proxy, mesh in getattr(transaction, "external_outline_retired", ()):
        try:
            require_owned(owner, (proxy, mesh))
            # The owner's registry is one user. Additional references/fake
            # users intentionally preserve the unlinked retired carrier.
            if proxy.users_collection or proxy.users != 1 or proxy.use_fake_user:
                continue
            key = proxy.get(DATA_UID)
            if owner[REGISTRY].get(key) != proxy:
                raise ValueError("退休外部描边载体的精确登记已变化")
            del owner[REGISTRY][key]
            bpy.data.objects.remove(proxy)
            prune_owned(owner)
        except Exception:
            logger.exception("Retired external outline cleanup postponed")
