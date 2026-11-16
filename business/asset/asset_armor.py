"""Independent wearable items from the same source meshes and shaders as BaiPo armor."""

from typing import cast

import bpy
from bpy.types import ID, ArmatureModifier, Mesh, ViewLayer
from mathutils import Vector

from ...util.geometry_nodes import input_path
from ...util.resources import AppendTransaction
from ..human.human_armor import current_variant, source_identity
from ..human.human_source import armor_path
from ..human.human_state import MATERIALS, PARAMETERS, PATTERNS
from .asset_rigs import _edit

PART = "blockloom_library_armor_part"
PREFIX = "blockloom_library_armor_"
_PARTS = {"helmet": "head", "chestplate": "chest", "leggings": "legs", "boots": "feet"}
_MATERIALS = ("leather", "chainmail", "copper", "diamond", "golden", "iron", "netherite")


def _item(identifier):
    namespace, _, item = identifier.partition(":")
    if namespace != "minecraft":
        return None
    if item == "turtle_helmet":
        return "head", 7
    material, _, piece = item.partition("_")
    if material in _MATERIALS and piece in _PARTS:
        return _PARTS[piece], _MATERIALS.index(material)
    return None


def _properties(root, part, material):
    for field in ("material", "pattern", "leather_color", "trim_color", "3d", "thickness"):
        label, default, lo, hi = PARAMETERS[part + "_" + field]
        name = PREFIX + field
        root[name] = material if field == "material" else default
        ui = root.id_properties_ui(name)
        if field == "3d":
            ui.update(description=label, default=default)
        elif field in {"pattern", "material"}:
            labels = PATTERNS if field == "pattern" else MATERIALS
            ui.update(description=label, min=lo, max=hi, default=root[name],
                      items=[("ID_" + str(i), text, "", i) for i, text in enumerate(labels)])
        elif field.endswith("_color"):
            ui.update(description=label, min=lo, max=hi, subtype="COLOR", default=default)
        else:
            ui.update(description=label, min=lo, max=hi, default=default)


def _bind(owner, path, root, field, axis=None):
    curve = owner.driver_add(path) if axis is None else owner.driver_add(path, axis)
    driver = curve.driver
    driver.type = "AVERAGE"
    variable = driver.variables.new()
    variable.type = "SINGLE_PROP"
    variable.targets[0].id = root
    source = '["' + PREFIX + field + '"]'
    variable.targets[0].data_path = source if axis is None else source + f"[{axis}]"


def _surface_controls(root, objects, appended):
    materials = set()
    for obj in objects:
        for modifier in obj.modifiers:
            if modifier.type != "NODES":
                continue
            depth = next((s for s in modifier.node_group.interface.items_tree
                          if getattr(s, "in_out", "") == "INPUT" and s.name == "厚度"), None)
            if depth and not modifier.node_group.get("bp_armor_envelope214"):
                for attr in ("show_viewport", "show_render"):
                    _bind(obj, modifier.path_from_id(attr), root, "3d")
                _bind(obj, input_path(modifier, depth.identifier), root, "thickness")
        materials.update(slot.material for slot in obj.material_slots if slot.material)
    for mat in materials:
        if mat not in appended:
            raise ValueError("盔甲材质不是本次独立追加的数据")
        selector, = (n for n in mat.node_tree.nodes if n.type == "GROUP" and n.inputs.get("材质编号"))
        for socket, field in (("材质编号", "material"), ("花纹编号", "pattern"),
                              ("皮革颜色", "leather_color"), ("花纹颜色", "trim_color")):
            path = selector.inputs[socket].path_from_id("default_value")
            if field.endswith("_color"):
                for axis in range(4):
                    _bind(mat.node_tree, path, root, field, axis)
            else:
                _bind(mat.node_tree, path, root, field)


def _part_rig(transaction, scene, root, source, objects):
    """Retain the actual weighted rest bones without importing the character controls."""
    data = transaction.track(bpy.data.armatures.new(root.name + " · 部件骨架"))
    rig = transaction.track(bpy.data.objects.new(data.name, data))
    rig.parent = root
    rig.matrix_basis = source.matrix_basis
    names = {obj.vertex_groups[g.group].name for obj in objects
             for v in obj.data.vertices for g in v.groups if g.weight > 0}
    bones = sorted((b for b in source.data.bones if b.name in names), key=lambda b: len(b.parent_recursive))
    if not bones:
        raise ValueError("白珀盔甲缺少原生变形骨")
    scene.collection.objects.link(rig)
    try:
        with _edit(rig, scene) as editable:
            for bone in bones:
                copy = editable.new(bone.name)
                copy.head, copy.tail = bone.head_local, bone.tail_local
                copy.align_roll(bone.matrix_local.to_3x3().col[2])
                copy.use_deform = bone.use_deform
                parent = next((p for p in bone.parent_recursive if p.name in editable), None)
                if parent:
                    copy.parent = editable[parent.name]
    finally:
        scene.collection.objects.unlink(rig)
    rig.hide_render = True
    rig.hide_select = True
    rig.data.display_type = "STICK"
    return rig


def build_entry(transaction, scene, collection, root, identifier, options, material_for):
    entry = _item(identifier)
    if entry is None:
        return False
    part, material = entry
    with AppendTransaction(armor_path(None)) as tx:
        branch = tx.append_collection("OXAL_ARMOR_" + part.upper())
        # The rigid official-leg variant uses native Armature weights without a live player lattice.
        surfaces = tuple(obj for obj in branch.objects if current_variant(obj, True))
        source, = {obj.parent for obj in surfaces}
        if source is None or source.type != "ARMATURE" or source not in tx.owned:
            raise ValueError("白珀盔甲来源骨架无效")
        rig = _part_rig(transaction, scene, root, source, surfaces)
        collection.objects.link(rig)
        _properties(root, part, material)
        _surface_controls(root, surfaces, tx.owned)
        for obj in surfaces:
            inverse = obj.matrix_parent_inverse.copy()
            # Some native pieces ship an empty Basis-only key. Keep the actual
            # mesh positions and omit that inert animation container on items.
            keys = cast(Mesh, obj.data).shape_keys
            animation = keys.animation_data if keys else None
            if (keys and len(keys.key_blocks) == 1 and keys in tx.owned
                    and not (animation and (animation.action or animation.drivers or animation.nla_tracks))):
                tx.owned.remove(keys)
                obj.shape_key_clear()
            for mod in obj.modifiers:
                if isinstance(mod, ArmatureModifier) and mod.object == source:
                    mod.object = rig
            obj.parent = rig
            obj.matrix_parent_inverse = inverse
            obj["oxal_mc_studio_model_part"] = source_identity(obj)
            branch.objects.unlink(obj)
            collection.objects.link(obj)
            obj.hide_render = obj.hide_viewport = False
        # Keep only proven dependencies of the requested geometry. Source controller widgets,
        # alternative leg style and the original full character skeleton stay in the library.
        keep: set[ID] = set(surfaces)
        users = bpy.data.user_map(subset=tuple(tx.owned))
        while additions := {item for item, refs in users.items() if item not in keep and refs & keep}:
            keep.update(additions)
        tx.remove(tx.owned - keep)
        tx.prepare_commit(image_paths=transaction.image_paths)
        for item in tx.owned:
            transaction.track(item)
        tx.finish_commit()
    root[PART] = part
    root["oxal_mc_studio_geometry_kind"] = "ARMOR"
    return True


def post_compile(root, scene, options):
    if PART not in root:
        return
    # The wearable keeps its source scale. Place its lower face at the requested
    # origin/cursor instead of leaving a loose helmet suspended at head height.
    cast(ViewLayer, bpy.context.view_layer).update()
    rig, = (obj for obj in root.children if obj.type == "ARMATURE")
    inv = root.matrix_world.inverted()
    points = [inv @ obj.matrix_world @ Vector(corner) for obj in rig.children for corner in obj.bound_box]
    rig.location -= Vector(((max(p.x for p in points) + min(p.x for p in points)) / 2,
                            (max(p.y for p in points) + min(p.y for p in points)) / 2,
                            min(p.z for p in points)))
    rig.hide_set(True)
