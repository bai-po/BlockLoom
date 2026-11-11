"""Explicit effects for borrowed render objects, without importing their ownership.

asset_id: oxal.enchantment.external; asset_version: 1; policy: NEW_INSTANCE;
scope: one exact selected Object in one Scene. Sources are existing material
surfaces; newly created controllers, shaders and outline proxies are private.
Original Object/Mesh/Key/Action IDs never join the generated-data registry.
"""

from uuid import uuid4

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import SCENE_ROOTS, owns, require_owned
from ...util.resources import resolve_scene
from ..asset.asset_geometry import Creation
from . import enchantment as shader

REFERENCE = "oxal_external_enchantment"
TARGET = "oxal_external_target"
SCHEMA = "oxal_external_enchantment_schema"
NORMALIZED = "oxal_external_normalized_materials"
SURFACES = "oxal_external_enchantment_surfaces"
TYPES = frozenset({"MESH", "CURVE", "SURFACE", "FONT", "META"})
ACTIONS = frozenset({"ENCHANT_GENERIC", "UNENCHANT_GENERIC", "ENCHANT_GENERIC_PIXELS",
                     "ENCHANT_GENERIC_OUTLINE", "ENCHANT_GENERIC_REMOVE_OUTLINE",
                     "ENCHANT_GENERIC_3D", "ENCHANT_GENERIC_REMOVE_3D"})


def controller(obj):
    """Cheap exact-reference UI hint; no hashing, evaluation or adoption."""
    if obj is None:
        return None
    owner = obj.get(REFERENCE)
    if (not isinstance(owner, bpy.types.Object) or owner.get(SCHEMA) != 1
            or owner.get(TARGET) != obj or not owns(owner, owner)):
        return None
    return owner


def targets(selected):
    """Containers select their renderable descendants; direct meshes stay local."""
    result = {}
    for selected_obj in tuple(selected):
        obj = selected_obj
        helper_target = _helper_target(obj)
        if helper_target is not None:
            obj = helper_target
        candidates = (obj,) if obj.type in TYPES else tuple(obj.children_recursive)
        for candidate in candidates:
            if candidate.type in TYPES and _helper_target(candidate) is None:
                result[candidate.session_uid] = candidate
    return tuple(result[key] for key in sorted(result))


def _helper_target(obj):
    """A copied helper tag is not permission to edit its referenced target."""
    from .enchantment_external_outline import STATE

    target = obj.get("oxal_external_outline_target")
    if isinstance(target, bpy.types.Object):
        owner = controller(target)
        if owner is not None and owner.get(STATE, {}).get("proxy") == obj and owns(owner, obj):
            return target
    target = obj.get(TARGET)
    if isinstance(target, bpy.types.Object) and controller(target) == obj:
        return target
    return None


def _check(scene, obj):
    if (obj.type not in TYPES or scene.objects.get(obj.name) != obj
            or not obj.is_editable or obj.library or obj.override_library
            or obj.data is None):
        raise ValueError("目标不是当前场景中的可编辑表面对象：" + obj.name)
    if tuple(obj.users_scene) != (scene,):
        raise ValueError("该对象被多个场景共用，请先建立单用户对象以分别调节：" + obj.name)


def _record(obj):
    from . import enchantment_external_assigners as assigners

    owner = controller(obj)
    uv_layers = tuple(getattr(obj.data, "uv_layers", ()))
    return {"object_uid": obj.session_uid, "data_uid": obj.data.session_uid,
            "owner_uid": owner.session_uid if owner is not None else None,
            "relief_subdivision": owner.get("glint_geometry_subdivision") if owner is not None else None,
            "uv_names": [layer.name for layer in uv_layers],
            "render_uv": next((layer.name for layer in uv_layers if layer.active_render), None),
            "slots": [{"link": slot.link, "material_uid": slot.material.session_uid if slot.material else None,
                       "tree_uid": slot.material.node_tree.session_uid
                       if slot.material and slot.material.node_tree else None}
                      for slot in obj.material_slots],
            "assigners": assigners.snapshot(obj)}


def request(scene, selected, action):
    if action not in ACTIONS:
        raise ValueError("未知通用附魔操作")
    objects = targets(selected)
    if not objects:
        raise ValueError("请选择网格、曲线、文字等有表面的对象，或包含它们的骨架／父物体")
    for obj in objects:
        _check(scene, obj)
    return {"action": action, "external_targets": [_record(obj) for obj in objects],
            "external_creation_ids": [uuid4().hex for _ in objects]}


def _journal_property(transaction, obj, key, value):
    def clone(item):
        if isinstance(item, bpy.types.ID):
            return item
        if hasattr(item, "items"):
            return {key: clone(value) for key, value in item.items()}
        if isinstance(item, (str, int, float, bool, type(None))):
            return item
        return [clone(value) for value in item]

    existed, previous = key in obj, clone(obj.get(key))

    def restore():
        if existed:
            obj[key] = previous
        elif key in obj:
            del obj[key]

    transaction.on_rollback(restore)
    obj[key] = value
    obj.update_tag()


def _owner(transaction, scene, obj, creation_id):
    owner = controller(obj)
    if owner is not None:
        if owner.get("oxal_external_creation_id") == creation_id:
            raise ValueError("本次通用附魔创建标识重复，未重复创建")
        require_owned(owner, (owner,))
        transaction.owner = owner
        return owner
    if REFERENCE in obj:
        previous = obj[REFERENCE]
        if isinstance(previous, bpy.types.Object) and previous.get(TARGET) == obj:
            raise ValueError("原附魔控制器归属已失效，未按标签接管")
    transaction.owner = None
    owner = transaction.track(bpy.data.objects.new("附魔参数 · " + obj.name, None))
    transaction.owner = owner
    transaction.domains.setdefault(owner, []).append(owner)
    roots_before = dict(scene.get(SCENE_ROOTS, {}).items()) if SCENE_ROOTS in scene else None

    def restore_roots():
        if roots_before is None:
            if SCENE_ROOTS in scene:
                del scene[SCENE_ROOTS]
        else:
            scene[SCENE_ROOTS] = roots_before

    transaction.on_rollback(restore_roots)
    owner[SCHEMA], owner[TARGET] = 1, obj
    owner["oxal_external_creation_id"] = creation_id
    owner.empty_display_type = "IMAGE"
    owner.show_empty_image_orthographic = False
    owner.show_empty_image_perspective = False
    owner.hide_select = True
    from .enchantment_collections import link as link_controls

    link_controls(transaction, scene, owner)
    _journal_property(transaction, obj, REFERENCE, owner)
    shader.control_defaults(owner)
    return owner


def _defaults(transaction, owner):
    before = set(owner.keys())

    def restore():
        for key in set(owner.keys()) - before:
            del owner[key]

    transaction.on_rollback(restore)
    shader.control_defaults(owner)


def _normal_material(transaction, obj, material, owner):
    if material is not None and material.use_nodes and material.node_tree:
        return material
    original = material
    for row in owner.get(NORMALIZED, {}).values():
        if row.get("original") == original:
            private = row.get("private")
            if isinstance(private, bpy.types.Material) and owns(owner, private) and private.node_tree:
                return private
    material = transaction.track(material.copy() if material else bpy.data.materials.new("表面 · " + obj.name))
    material.use_nodes = True
    tree = material.node_tree
    principled = next((node for node in tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
    if principled is None:
        principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
    output = next((node for node in tree.nodes if node.type == "OUTPUT_MATERIAL" and node.is_active_output), None)
    if output is None:
        output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(principled.outputs[0], output.inputs["Surface"])
    if original:
        principled.inputs["Base Color"].default_value = original.diffuse_color
        principled.inputs["Metallic"].default_value = original.metallic
        principled.inputs["Roughness"].default_value = original.roughness
        principled.inputs["Alpha"].default_value = original.diffuse_color[3]
        for socket, field, component in ((principled.inputs["Base Color"], "diffuse_color", None),
                                         (principled.inputs["Alpha"], "diffuse_color", 3),
                                         (principled.inputs["Roughness"], "roughness", None),
                                         (principled.inputs["Metallic"], "metallic", None)):
            native = tree.nodes.new("ShaderNodeRGB" if socket.type == "RGBA" else "ShaderNodeValue")
            native.label = "原非节点材质 · " + socket.name
            tree.links.new(native.outputs[0], socket)
            axes = range(4) if socket.type == "RGBA" else (None,)
            for axis in axes:
                value = native.outputs[0]
                curve = value.driver_add("default_value", axis) if axis is not None else value.driver_add("default_value")
                curve.driver.type, curve.driver.expression = "SCRIPTED", "value"
                variable = curve.driver.variables.new()
                variable.name, variable.type = "value", "SINGLE_PROP"
                variable.targets[0].id_type = "MATERIAL"
                variable.targets[0].id = original
                index = axis if axis is not None else component
                variable.targets[0].data_path = field + ("[" + str(index) + "]" if index is not None else "")
    table = {key: dict(row.items()) for key, row in owner.get(NORMALIZED, {}).items()}
    row = {"private": material}
    if original:
        row["original"] = original
    table[str(len(table))] = row
    _journal_property(transaction, owner, NORMALIZED, table)
    return material


def _empty_slots(transaction, obj, owner):
    """Only objects without any slots need a local data copy, never conversion."""
    if obj.material_slots:
        return
    previous = obj.data
    private = transaction.track(previous.copy())
    keys = getattr(private, "shape_keys", None)
    if keys is not None:
        transaction.track(keys)
    for item in (private, keys):
        animation = item.animation_data if item else None
        for curve in animation.drivers if animation else ():
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id == previous:
                        target.id = private
                    elif target.id == getattr(previous, "shape_keys", None):
                        target.id = keys
    transaction.on_rollback(lambda: setattr(obj, "data", previous))
    obj.data = private
    # Add a slot without giving an arbitrary existing material ownership.
    placeholder = transaction.track(bpy.data.materials.new("空白表面 · " + obj.name))
    placeholder.use_nodes = True
    private.materials.append(placeholder)
    table = {key: dict(row.items()) for key, row in owner.get(NORMALIZED, {}).items()}
    table[str(len(table))] = {"private": placeholder}
    _journal_property(transaction, owner, NORMALIZED, table)


def _bind(transaction, obj, index, material):
    slot = obj.material_slots[index]
    link, visible = slot.link, slot.material
    previous_override = [visible]

    def restore():
        current = obj.material_slots[index]
        current.link = "OBJECT"
        current.material = previous_override[0]
        current.link = link

    transaction.on_rollback(restore)
    slot.link = "OBJECT"
    previous_override[0] = slot.material
    slot.material = material


def materials(obj):
    from . import enchantment_external_assigners as assigners

    return tuple(dict.fromkeys([*(slot.material for slot in obj.material_slots if slot.material),
                                *assigners.materials(obj)]))


def _evaluated_materials(obj):
    if not any(modifier.type == "NODES" for modifier in obj.modifiers):
        return ()
    graph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(graph)
    mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
    try:
        if mesh is None:
            return ()
        indices = {face.material_index for face in mesh.polygons}
        return tuple(dict.fromkeys(mesh.materials[index].original if index < len(mesh.materials) and mesh.materials[index]
                                   else None for index in indices))
    finally:
        evaluated.to_mesh_clear()


def _remove_material(transaction, owner, material):
    info = shader.inspect_source(transaction, material)
    if info is None or info["owner"] != owner:
        return material
    result = shader.remove(transaction, owner, material)
    for row in owner.get(NORMALIZED, {}).values():
        if row.get("private") == result:
            return row.get("original")
    return result


def execute(scene_uid, options):
    ensure_mutation_allowed()
    from . import enchantment_external_assigners as assigners
    from . import enchantment_external_outline as outline
    from . import enchantment_external_relief as relief

    scene = resolve_scene(scene_uid)
    rows = options.get("external_targets")
    tokens = options.get("external_creation_ids")
    if (not isinstance(rows, list) or not rows or not isinstance(tokens, list) or len(rows) != len(tokens)
            or any(not isinstance(token, str) or len(token) != 32 for token in tokens)):
        raise ValueError("通用附魔缺少提交时的明确目标快照")
    objects = {obj.session_uid: obj for obj in scene.objects}
    selected = []
    for row in rows:
        obj = objects.get(row.get("object_uid"))
        if obj is None:
            raise ValueError("提交的附魔对象已移除，请重新选择")
        _check(scene, obj)
        if row != _record(obj):
            raise ValueError("附魔目标、材质或修改器在排队期间变化，请重新执行：" + obj.name)
        selected.append(obj)
    action = options["action"]
    if action not in ACTIONS:
        raise ValueError("未知通用附魔操作")
    remove = action == "UNENCHANT_GENERIC"
    outline_only = action in {"ENCHANT_GENERIC_OUTLINE", "ENCHANT_GENERIC_REMOVE_OUTLINE"}
    changed = 0
    with Creation() as transaction:
        for obj, token in zip(selected, tokens, strict=True):
            owner = controller(obj)
            if owner is None and (remove or action in {"ENCHANT_GENERIC_REMOVE_OUTLINE", "ENCHANT_GENERIC_REMOVE_3D"}):
                continue
            owner = _owner(transaction, scene, obj, token)
            _defaults(transaction, owner)
            if action in {"ENCHANT_GENERIC_3D", "ENCHANT_GENERIC_REMOVE_3D"}:
                if action == "ENCHANT_GENERIC_3D":
                    relief.apply(transaction, scene, obj, owner)
                    _journal_property(transaction, owner, "glint_geometry_enabled", True)
                else:
                    relief.remove(transaction, scene, obj, owner)
                    _journal_property(transaction, owner, "glint_geometry_enabled", False)
                obj.update_tag()
                if outline.prepared(owner):
                    outline.apply(transaction, scene, obj, owner)
                changed += 1
                continue
            if outline_only:
                if action == "ENCHANT_GENERIC_REMOVE_OUTLINE":
                    outline.remove(transaction, scene, obj, owner)
                    _journal_property(transaction, owner, "glint_outline_enabled", False)
                else:
                    outline.apply(transaction, scene, obj, owner)
                    _journal_property(transaction, owner, "glint_outline_enabled", True)
                changed += 1
                continue
            if not remove:
                _empty_slots(transaction, obj, owner)
            originals = tuple(dict.fromkeys((*materials(obj), *_evaluated_materials(obj))))
            # Empty existing slots are actual surfaces too.
            if any(slot.material is None for slot in obj.material_slots):
                originals = (*originals, None)
            replacements = {}
            uv = getattr(obj.data, "uv_layers", None)
            uv_name = uv.active_render.name if uv and getattr(uv, "active_render", None) else None
            if uv and uv_name is None:
                uv_name = next((layer.name for layer in uv if layer.active_render), None)
            for original in originals:
                if remove:
                    result = _remove_material(transaction, owner, original) if original else None
                else:
                    source = _normal_material(transaction, obj, original, owner)
                    result = shader.apply(transaction, owner, source, geometry_kind="GENERIC", scene=scene, uv_name=uv_name)
                replacements[original] = result
            for index, slot in enumerate(obj.material_slots):
                if slot.material in replacements and replacements[slot.material] != slot.material:
                    _bind(transaction, obj, index, replacements[slot.material])
                    changed += 1
            assigners.apply(transaction, obj, replacements, owner)
            _journal_property(transaction, owner, SURFACES,
                              {str(index): material for index, material in enumerate(dict.fromkeys(replacements.values()))
                               if material is not None and material.get(shader._OWNER) == owner})
            if not remove:
                _journal_property(transaction, owner, "glint_enabled", True)
            if action == "ENCHANT_GENERIC_PIXELS":
                _journal_property(transaction, owner, "glint_pixel_enabled", True)
            if relief.prepared(owner):
                relief.apply(transaction, scene, obj, owner)
            # The proxy samples current material identities, so explicitly
            # refresh it in the same transaction after any surface rebind.
            if outline.prepared(owner):
                outline.apply(transaction, scene, obj, owner)
        transaction.commit()
        outline.cleanup(transaction)
    return {"message": ("已取消所选对象附魔；独立描边保留" if remove else
                        "已移除所选对象描边" if action == "ENCHANT_GENERIC_REMOVE_OUTLINE" else
                        "已添加所选对象3D凹凸，保留原模型与材质" if action == "ENCHANT_GENERIC_3D" else
                        "已移除所选对象3D凹凸" if action == "ENCHANT_GENERIC_REMOVE_3D" else
                        "已为所选对象添加描边" if outline_only else
                        "已为所选对象添加像素流光" if action == "ENCHANT_GENERIC_PIXELS" else
                        "已为所选对象添加附魔；描边默认关闭"),
            "external_objects": len(selected), "material_slots_changed": changed}
