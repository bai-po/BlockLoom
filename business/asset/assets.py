"""Independent asset operations built from declarative sources and native data.

Policy: NEW_INSTANCE, instance scope. Source: asset/library/SOURCE.json.
Every generated ID is explicitly tracked; existing user data is never purged.
"""

import hashlib
import json
import math
import shutil
import zipfile
from contextlib import ExitStack
from pathlib import Path
from time import sleep
from typing import cast
from uuid import uuid4

import bpy
from mathutils import Matrix

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import owns, require_owned
from ...util.resources import (
    ASSET_ID,
    INSTANCE,
    VERSION,
    instance_creation_id,
    resolve_scene,
    resolve_view_layer,
)
from . import asset_authored, asset_extensions, asset_rigs, asset_special, asset_states
from . import asset_catalog as catalog
from . import asset_geometry as geometry

RESOURCE_ID = "oxal.library"
RESOURCE_VERSION = "2"
POLICY = "NEW_INSTANCE"
SCOPE = "instance"
IDENTIFIER = "oxal_mc_studio_library_identifier"
GLINT_PREFIX = "OXAL Glint "
OWNER_INSTANCE = "oxal_mc_studio_generated_instance"


class _AddedProperties:
    """Restore only properties this operation added to existing instances."""

    def __init__(self, transaction):
        self.added = []
        self.transaction = transaction
        transaction.on_rollback(self.restore)

    def glint_defaults(self, root):
        from ..enchantment.enchantment import control_defaults
        before = set(root.keys())
        try:
            control_defaults(root)
            from ..enchantment.enchantment_limits import refresh
            refresh(self.transaction, root)
        finally:
            self.added.extend((root, key) for key in root.keys() if key not in before)  # noqa: SIM118 -- Blender ID iteration is not key iteration.

    def restore(self):
        for root, key in reversed(self.added):
            if key in root:
                del root[key]


def _enchant_geometry_preflight(root):
    """Never bless unknown legacy/user mesh data while adding native effects."""
    from .asset_contracts import PROPERTY, _driver_order, record, validate
    if PROPERTY not in root or "mesh_data" not in json.loads(root[PROPERTY]):
        raise ValueError("请先修复 / 升级此Beta实例，再添加附魔几何")
    try:
        validate(root)
    except ValueError:
        from ..outline.outline_solidify import generated_driver_paths
        from .asset_outline_rebind import RECORD

        rebound = root.get(RECORD)
        if (not root.get("oxal_mc_studio_image_root_schema") or not rebound
                or rebound.get("schema") != 1 or rebound.get("private_mesh") != root.data):
            raise
        # An explicit rebind adds proven modifier drivers without certifying
        # the user's current topology as source geometry. Ignore only those
        # exact current generated paths, leaving every other saved field and
        # driver subject to the original asset contract comparison.
        generated = set(generated_driver_paths(root, root, role="enchant"))
        actual = json.loads(json.dumps(record(root)))
        saved = json.loads(root[PROPERTY])
        for value in (saved, actual):
            value["root_mesh_drivers"] = [curve for curve in value.get("root_mesh_drivers", ())
                                          if curve["path"] not in generated]
        if saved != actual and _driver_order(saved) != _driver_order(actual):
            raise


def _stamp_geometry(transaction, root):
    from .asset_contracts import PROPERTY, stamp
    before = root.get(PROPERTY)
    references = dict(root.get("oxal_mc_studio_contract_refs", {}).items())
    def restore():
        if before is None:
            if PROPERTY in root:
                del root[PROPERTY]
        else:
            root[PROPERTY] = before
        root["oxal_mc_studio_contract_refs"] = references
    transaction.on_rollback(restore)
    stamp(root, transaction=transaction)


def _stamp(transaction, root):
    for item in transaction.ids:
        item[OWNER_INSTANCE] = root[INSTANCE]
    from .asset_contracts import stamp
    stamp(root, transaction=transaction)


def user_library() -> Path:
    from ...util.user_storage import personal_directory

    return personal_directory("library", "OXAL_MC_STUDIO_LIBRARY_DIR")


def roots(scene):
    return [
        obj
        for obj in scene.objects
        if obj.get(ASSET_ID) == RESOURCE_ID and obj.get(INSTANCE)
    ]


def find_root(scene, instance: str):
    matches = [obj for obj in roots(scene) if obj.get(INSTANCE) == instance]
    if len(matches) != 1:
        raise ValueError("目标资产已删除或身份不唯一")
    root = matches[0]
    if root.library or root.override_library:
        raise ValueError("目标资产为只读数据")
    if any(root in tuple(other.objects) for other in bpy.data.scenes if other != scene):
        raise ValueError("目标资产被多个场景共享，请先创建独立实例")
    require_owned(root, [root])
    return root


def root_of(obj):
    while obj is not None:
        if obj.get(ASSET_ID) == RESOURCE_ID and obj.get(INSTANCE):
            return obj
        obj = obj.parent
    return None


def members(root):
    return [root, *(obj for obj in root.children_recursive if root_of(obj) == root)]


def selected_roots(scene, identifiers):
    if not isinstance(identifiers, list) or not identifiers:
        raise ValueError("请选择新版资产实例")
    return [find_root(scene, identifier) for identifier in dict.fromkeys(identifiers)]


def _require_members(root):
    require_owned(root, members(root))


def packs() -> list[dict]:
    directory = user_library() / "packs"
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("*/pack.json")):
        try:
            data = json.loads(path.read_text(encoding="utf8"))
            if isinstance(data, dict) and data.get("digest") == path.parent.name:
                found.append(data)
        except (OSError, ValueError):
            continue
    return found


def _pack_path(digest, key):
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("材质包标识无效")
    namespace, name = key.split(":", 1)
    directory = user_library() / "packs" / digest
    path = directory / "assets" / namespace / "textures" / (name + ".png")
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("材质包路径越界")
    return path


def texture_pack_path(digest: str, key: str) -> Path:
    """Canonical first, then the pinned historical texture-path aliases."""
    canonical = _pack_path(digest,key)
    if canonical.is_file():
        return canonical
    aliases = []
    if key == "minecraft:misc/enchanted_glint_armor":
        aliases.append("minecraft:misc/enchanted_glint_entity")
    elif key == "minecraft:misc/enchanted_glint_item":
        aliases.append("minecraft:misc/enchanted_item_glint")
    namespace,name = key.split(":",1)
    for folder,layer in (("entity/equipment/humanoid/",1),("entity/equipment/humanoid_leggings/",2)):
        if name.startswith(folder):
            material = name.removeprefix(folder)
            overlay = material.endswith("_overlay")
            material = material.removesuffix("_overlay")
            material = "turtle" if material == "turtle_scute" else material
            aliases.append(namespace+":models/armor/"+material+"_layer_"+str(layer)+("_overlay" if overlay else ""))
    return next((path for alias in aliases if (path := _pack_path(digest,alias)).is_file()),canonical)


def _pack_texture_candidate(digest, key, original):
    """Custom images keep their identity; filename fallback must be unique."""
    candidate = texture_pack_path(digest, key)
    if candidate.is_file() or not key.startswith("oxal_image:"):
        return candidate
    directory = user_library() / "packs" / digest
    metadata = json.loads((directory / "pack.json").read_text(encoding="utf8"))
    matches = []
    for relative in metadata.get("files", {}):
        if Path(relative).name != Path(original).name or "/textures/" not in relative:
            continue
        path = directory / relative
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("材质包图片映射越界")
        if path.is_file():
            matches.append(path)
    if len(matches) > 1:
        raise ValueError("图片名称在材质包中对应多个资源，无法唯一替换：" + Path(original).name)
    return matches[0] if matches else candidate


def _pbr_companions(digest, key, path, *, overlay_only=False):
    """PBR-only packs overlay data maps without requiring another color PNG."""
    from .asset_pbr import companions

    maps = {} if overlay_only else companions(path)
    if not digest:
        return maps
    for role, suffixes in (("normal", ("_n",)), ("pbr", ("_s", "_a"))):
        for suffix in suffixes:
            original = Path(path).with_name(Path(path).stem + suffix + ".png")
            candidate = _pack_texture_candidate(digest, key + suffix, original)
            if candidate.is_file():
                maps[role] = candidate
    return maps


def _texture(scene, key, *, pack_digest=None):
    original = catalog.resource(key, "textures", ".png")
    digest = (
        scene.get("oxal_mc_studio_future_pack", "") if pack_digest is None else pack_digest
    )
    if digest:
        candidate = texture_pack_path(digest, key)
        if candidate.is_file():
            return candidate, original
    return original, original


def _tint(descriptor, index):
    if index < 0:
        return (1, 1, 1, 1)
    tints = descriptor.get("tints", [])
    if tints and index >= len(tints):
        return (1, 1, 1, 1)
    if index < len(tints):
        tint = tints[index]
        value = tint.get("value", tint.get("default", 0xFFFFFF))
        if isinstance(value, int):
            return tuple(((value >> shift) & 255) / 255 for shift in (16, 8, 0)) + (1,)
    return (0.55, 0.75, 0.35, 1)


def _pbr_format(scene, options=None):
    value = (options or {}).get("pbr_format", scene.get("oxal_mc_studio_pbr_format", "lab_PBR_1.3"))
    if value not in {"lab_PBR_1.3", "old_PBR"}:
        raise ValueError("未知 PBR 解析格式")
    return value


def _material_factory(transaction, scene, *, pack_digest=None, pbr_format=None):
    cache = {}

    def material_for(key, tint_index=-1, descriptor=None):
        tint = _tint(descriptor or {}, tint_index)
        lookup = (key, tint)
        if lookup not in cache:
            path, original = _texture(scene, key, pack_digest=pack_digest)
            digest = scene.get("oxal_mc_studio_future_pack", "") if pack_digest is None else pack_digest
            mat = geometry.material(transaction, path, key=key, tint=tint, scene=scene,
                                    pbr_format=pbr_format or _pbr_format(scene),
                                    pbr_maps=_pbr_companions(digest, key, path))
            mat[geometry.ORIGINAL_PATH] = str(original)
            if path != original:
                baseline = transaction.track(
                    bpy.data.images.load(str(original), check_existing=False)
                )
                baseline.pack()
                mat[geometry.ORIGINAL_IMAGE] = baseline
                mat[geometry.ORIGINAL_ANIMATION] = json.dumps(
                    geometry._animation(original, baseline)
                )
            cache[lookup] = mat
        return cache[lookup]

    return material_for


def _copy_action_binding(transaction, binding):
    """Keep this owner's slot when privately copying its Action, including NLA."""
    if binding.action is None:
        return
    original_slot = getattr(binding, "action_slot", None)
    copied = transaction.track(binding.action.copy())
    copied_slot = None
    if original_slot is not None:
        matches = [
            slot
            for slot in getattr(copied, "slots", ())
            if slot.identifier == original_slot.identifier
            and slot.target_id_type == original_slot.target_id_type
        ]
        if len(matches) != 1:
            raise ValueError("材质动画槽无法唯一对应，未替换原动画")
        copied_slot = matches[0]
    binding.action = copied
    if hasattr(binding, "action_slot"):
        binding.action_slot = copied_slot


def _private_material(transaction, old):
    new = transaction.track(old.copy())
    for owner in (new,new.node_tree):
        animation = owner.animation_data if owner else None
        if animation:
            for curve in animation.drivers:
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if target.id == old:
                            target.id = new
                        elif target.id == old.node_tree:
                            target.id = new.node_tree
    return new


def _private_shader_trees(transaction, old):
    result = _private_material(transaction, old)
    copies = {old.node_tree: result.node_tree}

    def clone(tree):
        for node in tree.nodes:
            child = getattr(node, "node_tree", None)
            if child is not None:
                if child not in copies:
                    copies[child] = transaction.track(child.copy())
                    clone(copies[child])
                node.node_tree = copies[child]

    clone(result.node_tree)
    for tree in copies.values():
        animation = tree.animation_data
        for curve in animation.drivers if animation else ():
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id in copies:
                        target.id = copies[target.id]
    alpha_groups = result.get("oxal_mc_studio_glint_alpha_groups")
    if alpha_groups:
        for entry in alpha_groups.values():
            if entry.get("private") in copies:
                entry["private"] = copies[entry["private"]]
    return result, copies


def _assign_materials(transaction, changes, *, commit=True):
    changes = [change for change in changes if change[2] != change[3]]
    mesh_copies, applied = [], []
    try:
        for obj in dict.fromkeys(change[0] for change in changes):
            root = root_of(obj)
            transaction.owner = root
            registry_user = int(root is not None and owns(root, obj.data))
            from ..enchantment.enchantment_geometry import DATA, generated_attributes
            pixel_data = obj.get(DATA)
            if pixel_data is not None:
                generated_attributes(root, obj, transaction=transaction)
                if pixel_data.get("mesh") == obj.data:
                    registry_user += 1
            if obj.data.users > 1 + registry_user or (
                root and not owns(root, obj.data)
            ):
                previous = obj.data
                if previous.shape_keys:
                    raise ValueError("共享网格含实际形态键，无法在保留其身份的同时替换材质：" + obj.name)
                private = transaction.track(previous.copy())
                if private.shape_keys and private.shape_keys != previous.shape_keys:
                    transaction.track(private.shape_keys)
                root = root_of(obj)
                if root:
                    private[OWNER_INSTANCE] = root[INSTANCE]
                    if private.shape_keys and private.shape_keys != previous.shape_keys:
                        private.shape_keys[OWNER_INSTANCE] = root[INSTANCE]
                obj.data = private
                mesh_copies.append((obj, previous))
        for obj, index, old, new in changes:
            root = root_of(obj)
            if root:
                new[OWNER_INSTANCE] = root[INSTANCE]
            obj.material_slots[index].material = new
            applied.append((obj, index, old))
        def rollback():
            for obj,index,old in reversed(applied):
                obj.material_slots[index].material = old
            for obj,previous in reversed(mesh_copies):
                obj.data = previous
        transaction.on_rollback(rollback)
        if commit:
            transaction.commit()
    except Exception:
        for obj, index, old in reversed(applied):
            obj.material_slots[index].material = old
        for obj, previous in reversed(mesh_copies):
            obj.data = previous
        raise


def _new_root(transaction, identifier, label, *, special=True, creation_id=None, object_root=None):
    if object_root is not None and (object_root not in transaction.ids or object_root.type != "MESH" or object_root.data not in transaction.ids):
        raise ValueError("图片资产根必须是本次明确创建的Mesh")
    collection = transaction.track(bpy.data.collections.new(label))
    if object_root is None:
        data = transaction.track(bpy.data.armatures.new(label + " · 骨架")) if special and asset_rigs.requires_rig(identifier) else None
        root = transaction.track(bpy.data.objects.new(label, data))
    else:
        root = object_root
    from .asset_bounds import configure as configure_origin_marker

    configure_origin_marker(root)
    root[ASSET_ID], root[VERSION], root[INSTANCE] = (
        RESOURCE_ID,
        RESOURCE_VERSION,
        instance_creation_id({"creation_id":creation_id}) if creation_id is not None else uuid4().hex,
    )
    root[IDENTIFIER] = identifier
    root["oxal_mc_studio_source_version"] = catalog.VERSION
    collection[ASSET_ID], collection[INSTANCE] = RESOURCE_ID, root[INSTANCE]
    collection.objects.link(root)
    return collection, root


def _activate(scene, root, *, transaction=None):
    layer = resolve_view_layer(scene)
    layer.update()
    if transaction is not None:
        previous = layer.objects.active
        original_selection = tuple(obj for obj in layer.objects if obj.select_get(view_layer=layer))

        def restore_selection():
            # Creation invokes callbacks before removing its new IDs. Restore
            # the actual original objects, never objects found by display name.
            present = set(layer.objects)
            for obj in tuple(layer.objects):
                if obj.select_get(view_layer=layer):
                    obj.select_set(False, view_layer=layer)
            for obj in original_selection:
                if obj in present:
                    obj.select_set(True, view_layer=layer)
            layer.objects.active = previous if previous in present else None

        transaction.on_rollback(restore_selection)
    selected = tuple(bpy.context.selected_objects) if bpy.context.scene == scene else tuple(layer.objects)
    for obj in selected:
        if obj.select_get(view_layer=layer):
            obj.select_set(False, view_layer=layer)
    layer.objects.active = root
    root.select_set(True, view_layer=layer)


def _validate_position(options):
    destination = options.get("destination", "ORIGIN")
    if destination not in {"ORIGIN", "CURSOR", "DISPLAY","SLOT"}:
        raise ValueError("未知资产导入目标")
    position = options.get("position", [0, 0, 0])
    if (
        not isinstance(position, list)
        or len(position) != 3
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in position
        )
    ):
        raise ValueError("位置必须是三个有限数值")
    return destination, position


def _build(transaction, scene, collection, root, identifier, options):
    root["oxal_mc_studio_effect_scene"] = scene
    root["oxal_mc_studio_presentation_context"] = options.get("context","world")
    root["oxal_mc_studio_geometry_kind"] = "BLOCK" if catalog.resource(identifier,"blockstates").is_file() and identifier.split(":")[-1] not in asset_rigs.ITEMS else "ITEM"
    context = options.get("context","world")
    root["oxal_mc_studio_build_handheld"] = (bool(options.get("special",False)) or context.startswith("firstperson")) and identifier.split(":")[-1] in asset_rigs.ITEMS
    pack_digest = options.get("pack_digest",scene.get("oxal_mc_studio_future_pack",""))
    material_for = _material_factory(transaction,scene,pack_digest=pack_digest,pbr_format=_pbr_format(scene,options))
    handled = context == "world" and bool(options.get("special",False)) and not options.get("_native_variant") and asset_extensions.build_entry(transaction,scene,collection,root,identifier,options,material_for)
    if handled:
        descriptors,state = [],{}
    else:
        descriptors,state = catalog.plan(identifier,state=options.get("state"),shape=int(options.get("shape",0)),
                                         special=bool(options.get("special",False)),complete=bool(options.get("complete_structure",True)),context=context)
        descriptors = options.get("_descriptors",descriptors)
        if identifier == "minecraft:painting" and options.get("special") and "_descriptors" not in options:
            descriptors = [{"_oxal_painting":"kebab"}]
        if not descriptors:
            raise ValueError("该资源不包含可见模型")
        if options.get("special",False) and asset_authored.world_model(transaction,collection,root,identifier,material_for):
            descriptors = []
    for number, descriptor in enumerate(descriptors):
        if "_oxal_painting" in descriptor:
            asset_states.painting_geometry(transaction,collection,root,descriptor["_oxal_painting"],material_for)
            continue
        if descriptor.get("type", "").endswith(":special"):
            before = set(root.children)
            asset_special.build(
                transaction, collection, root, identifier, {**descriptor,"_oxal_state":state,"_oxal_rig_enabled":bool(options.get("special",False))}, material_for
            )
            if context != "world":
                matrix = geometry.descriptor_matrix({**descriptor,"_oxal_centered":True})
                for part in set(root.children)-before:
                    if identifier.split(":")[-1] not in {"shield","trident"}:
                        part.location.z -= .5
                    part.matrix_basis = matrix @ part.matrix_basis
            continue
        data = catalog.model(descriptor["model"])
        if options.get("_slot_presentation") and data.get("elements"):
            # The 196 item renderer's element frame intentionally excludes
            # the placed-block rescale operation; keep that distinction local
            # to SLOT data without mutating the cached/world model recipe.
            data = {**data, "elements": [
                {**element, "rotation": {**element["rotation"], "rescale": False}}
                if element.get("rotation", {}).get("rescale") else element
                for element in data["elements"]
            ]}
        if data.get("builtin") == "generated":
            layers = [
                (key, value)
                for key, value in data.get("textures", {}).items()
                if key.startswith("layer")
            ]
            if not layers:
                raise ValueError("生成式物品没有 layer 贴图")
            for key, value in sorted(layers):
                mat = material_for(
                    catalog.texture(value, data["textures"]),
                    int(key[5:]) if descriptor.get("tints") else -1,
                    descriptor,
                )
                # Successive source layers sit outside both faces. Translating
                # a layer toward the front buries its back inside the base.
                obj = geometry.sprite(transaction, root.name + " " + key, mat,
                                      depth=0.0625 + 0.0004 * int(key[5:]),
                                      allow_empty=len(layers) > 1 and int(key[5:]) > 0)
                if obj is None:
                    # Minecraft generated overlays may intentionally contain
                    # no opaque pixels. The required base layer stays strict.
                    continue
                obj.matrix_local = (
                    geometry.descriptor_matrix({**descriptor,"_oxal_centered":True}) @ obj.matrix_basis
                )
                obj["oxal_mc_studio_structure_part"] = descriptor.get(
                    "_oxal_structure_part", ""
                )
                collection.objects.link(obj)
                obj.parent = root
        elif data.get("elements"):
            obj = geometry.elements(
                transaction,
                f"{root.name} {number}",
                data,
                lambda key, tint, desc=descriptor: material_for(key, tint, desc),
                descriptor,
            )
            collection.objects.link(obj)
            obj.parent = root
            if context != "world":
                cast(bpy.types.Mesh,obj.data).transform(Matrix.Translation((0,0,-.5)))
            obj["oxal_mc_studio_structure_part"] = descriptor.get("_oxal_structure_part", "")
        else:
            item_definition = catalog.read(catalog.resource(identifier, "items"))[
                "model"
            ]
            specials = [
                item
                for item in catalog.item_nodes(item_definition, state=state)
                if item.get("type", "").endswith(":special")
            ]
            if not specials:
                raise ValueError("源资源没有可见几何或特殊模型：" + identifier)
            for special_descriptor in specials:
                asset_special.build(
                    transaction,
                    collection,
                    root,
                    identifier,
                    special_descriptor,
                    material_for,
                )
    root["oxal_mc_studio_block_state"] = json.dumps(state, ensure_ascii=False)
    root["oxal_mc_studio_shape"] = int(options.get("shape", 0))
    root["oxal_mc_studio_special"] = bool(options.get("special", False))
    root["oxal_mc_studio_complete_structure"] = bool(options.get("complete_structure", True))
    root["oxal_mc_studio_pack_digest"] = pack_digest
    asset_extensions.palette(transaction,scene,collection,root,identifier,options,material_for)
    if options.get("_native_variant"):
        return
    short = identifier.split(":")[-1]
    if short in {"item_frame", "glow_item_frame"} or short.endswith("_shelf"):
        root["oxal_mc_studio_display_slots"] = 3 if short.endswith("_shelf") else 1
    if options.get("special", False):
        asset_states.compile_states(transaction, scene, collection, root, identifier, options,
                                    lambda variant: _build(transaction,scene,collection,root,identifier,variant))
        asset_special.effects(transaction, collection, root, identifier, material_for)
        if not handled:
            asset_extensions.build(transaction,scene,collection,root,identifier,options,material_for)
        asset_authored.prepare_grip(root, identifier)
        if short == "fishing_rod":
            asset_authored.rod_phases(transaction, collection, root, material_for)
        if short.endswith("_bed"):
            asset_authored.bed_variants(transaction, collection, root, identifier, state, material_for)
        asset_states.compile_copper(transaction,root,identifier,material_for)
    _stamp(transaction, root)


def import_asset(scene, options):
    ensure_mutation_allowed()
    destination, position = _validate_position(options)
    creation_id = instance_creation_id(options)
    if any(root.get(INSTANCE) == creation_id for root in roots(scene)):
        raise ValueError("创建身份与现有资产冲突，未创建重复实例")
    options = {**options,"creation_id":creation_id}
    identifier = options.get("identifier", "")
    entry = next(
        (entry for entry in catalog.entries() if entry.identifier == identifier), None
    )
    if entry is None:
        raise ValueError("资产不在随包目录中")
    if identifier.startswith("oxal:"):
        if destination not in {"ORIGIN","CURSOR"}:
            raise ValueError("独立视觉效果只支持世界原点或3D游标")
        if not options.get("special",False):
            raise ValueError("独立视觉效果需要开启特殊模型")
    if destination == "SLOT":
        return _import_slot(scene,options,identifier,entry.label)
    if destination == "DISPLAY":
        _,count,_ = _display_target(scene, options)
        options = {**options,"special":False,"context":"on_shelf" if count == 3 else "fixed","complete_structure":False}
    with geometry.Creation() as transaction:
        collection, root = _new_root(transaction, identifier, entry.label, special=bool(options.get("special",False)),creation_id=creation_id)
        _build(transaction, scene, collection, root, identifier, options)
        root.location = position if destination == "CURSOR" else (0, 0, 0)
        if destination == "DISPLAY":
            bind_display(scene, root, options,transaction=transaction)
        from .asset_collections import publish as publish_collection

        publish_collection(transaction, scene, collection)
        if options.get("special",False):
            asset_rigs.build_subrigs(transaction, scene, collection, root, identifier)
        asset_rigs.build(transaction, scene, collection, root, identifier)
        if options.get("special", False):
            asset_authored.attachments(transaction, scene, collection, root, identifier,
                                       _material_factory(transaction, scene, pbr_format=_pbr_format(scene,options)))
        asset_extensions.post_compile(root,scene,options)
        from .asset_bounds import prepare_base_frame

        prepare_base_frame(transaction, root)
        root.update_tag()
        _stamp(transaction, root)
        if bpy.context.mode == "OBJECT":
            _activate(scene, root, transaction=transaction)
        transaction.commit(root)
    return {"message": "已导入：" + entry.label, "instance_id": root[INSTANCE]}


def _representation(transaction,scene,collection,root,identifier,context,options,role):
    from .asset_display import slot_transform
    pivot = transaction.track(bpy.data.objects.new("资产表示 · "+role,None))
    collection.objects.link(pivot)
    pivot.parent = root
    pivot["oxal_mc_studio_representation"] = role
    _build(transaction,scene,collection,pivot,identifier,{**options,"special":False,"context":context,"complete_structure":False,"_native_variant":True,"_slot_presentation":True})
    if identifier in {"minecraft:shield", "minecraft:trident"}:
        from . import asset_item_geometry
        kind = identifier.split(":")[-1]
        definitions = asset_item_geometry.SHIELD if kind == "shield" else asset_item_geometry.TRIDENT
        floor = max(cube.origin[1]+cube.size[1] for cube in definitions) / 16 if role == "GUI" else 0
        for child in tuple(pivot.children):
            if child.type == "MESH" and child.get("oxal_mc_studio_model_source") == asset_item_geometry.MODEL_CLASSES[kind]:
                # This builder already converted Java Y-down to its authored
                # item frame. Reapplying the item definition's renderer flip
                # reverses the fork/plate a second time in slot presentations.
                child.matrix_basis = Matrix.Identity(4)
                if floor:
                    child.data.transform(Matrix.Translation((0,0,-floor)))
                from .asset_slot_geometry import entity_uv
                entity_uv(transaction,child,kind)
                child["oxal_mc_studio_slot_entity_frame"] = 1
        pivot["oxal_mc_studio_slot_entity_frame"] = 1
    suffix = identifier.rsplit("_",1)[-1].split(":")[-1]
    weapon = suffix in {"sword","axe","pickaxe","shovel","hoe","spear","bow","crossbow","shield","trident","mace"}
    sprite = any(obj.get("oxal_mc_studio_pixel_topology") for obj in pivot.children_recursive)
    visual_profile = "WEAPON" if weapon else "ITEM" if sprite or not catalog.resource(identifier,"blockstates").is_file() else "BLOCK"
    pivot["oxal_mc_studio_slot_visual_profile"] = visual_profile
    from .asset_slot_geometry import prepare
    prepare(transaction,pivot,identifier,context,options,role,visual_profile)
    display_matrix = slot_transform(identifier,context,visual_profile=visual_profile)
    pivot["oxal_mc_studio_slot_pose_schema"] = 1
    if role == "GUI":
        # A flattened canvas S followed by a rotated item D is generally a
        # shear. Keep them on separate native Objects: matrix_basis would
        # decompose S@D into TRS and silently change the icon's silhouette.
        display = transaction.track(bpy.data.objects.new("GUI · 原版显示变换",None))
        collection.objects.link(display)
        display.parent = pivot
        display["oxal_mc_studio_model_part"] = "库存 · GUI显示变换"
        display.matrix_basis = display_matrix
        for child in tuple(pivot.children):
            if child != display:
                child.parent = display
    else:
        pivot.matrix_basis = display_matrix
        from .asset_slot_states import build as build_slot_states
        build_slot_states(transaction,scene,collection,root,pivot,identifier,context,options,_representation)
    for child in tuple(pivot.children_recursive):
        child["oxal_mc_studio_representation"] = role
    return pivot


def _import_slot(scene,options,identifier,label):
    from ..inventory import inventory_slots
    target = inventory_slots.resolve_target(scene,options.get("inventory_target",{}))
    with geometry.Creation() as transaction:
        collection,root = _new_root(transaction,identifier,label,special=False,creation_id=options["creation_id"])
        gui = _representation(transaction,scene,collection,root,identifier,"gui",options,"GUI")
        held = None
        if target.view_mode == "FIRST_PERSON":
            context = "firstperson_lefthand" if target.handedness == "LEFT" else "firstperson_righthand"
            held = _representation(transaction,scene,collection,root,identifier,context,options,"HELD")
        root["oxal_mc_studio_block_state"] = json.dumps(catalog.default_state(identifier),ensure_ascii=False)
        root["oxal_mc_studio_shape"] = int(options.get("shape",0))
        root["oxal_mc_studio_special"],root["oxal_mc_studio_complete_structure"] = False,False
        root["oxal_mc_studio_pack_digest"] = options.get("pack_digest",scene.get("oxal_mc_studio_future_pack",""))
        root["oxal_mc_studio_presentation_context"] = "slot"
        root["oxal_mc_studio_geometry_kind"] = "BLOCK" if catalog.resource(identifier,"blockstates").is_file() else "ITEM"
        from .asset_collections import publish as publish_collection

        publish_collection(transaction, scene, collection)
        change = inventory_slots.bind_source(transaction,root,options["inventory_target"],quantity=int(options.get("quantity",1)),replace=bool(options.get("replace",False)),gui_representation=gui,held_representation=held)
        from .asset_removal import plan as removal_plan
        displaced = [removal_plan(scene,old) for old in change.previous]
        _stamp(transaction,root)
        transaction.commit(root)
        change.finalize()
        from .asset_removal import remove as remove_displaced
        for old in displaced:
            remove_displaced(scene,old)
    return {"message":"已导入所选物品栏格子："+label,"instance_id":root[INSTANCE]}


def image_asset(scene, options):
    ensure_mutation_allowed()
    destination, position = _validate_position(options)
    creation_id = instance_creation_id(options)
    if any(root.get(INSTANCE) == creation_id for root in roots(scene)):
        raise ValueError("创建身份与现有资产冲突，未创建重复实例")
    path = Path(str(options.get("path", ""))).resolve()
    if path.suffix.lower() != ".png" or not path.is_file():
        raise ValueError("请选择有效 PNG 文件")
    kind = options.get("kind", "ITEM")
    if kind not in {"ITEM", "BLOCK"}:
        raise ValueError("图片资产类型无效")
    depth = options.get("depth", 0.03)
    if (
        isinstance(depth, bool)
        or not isinstance(depth, (int, float))
        or not math.isfinite(depth)
        or not 0 < depth <= 1
    ):
        raise ValueError("物品厚度必须大于0且不超过1")
    if destination == "DISPLAY":
        _display_target(scene, options)
    from ..inventory import inventory_slots
    slot_target = None
    if destination == "SLOT":
        from ..inventory import inventory_slots
        slot_target = inventory_slots.resolve_target(scene,options.get("inventory_target",{}))
    with geometry.Creation() as transaction:
        # A user image is not a vanilla item merely because its filename matches.
        key = "oxal_image:" + hashlib.sha256(str(path).encode("utf8")).hexdigest()[:16] + "/" + path.stem
        selected_path = path
        future = scene.get("oxal_mc_studio_future_pack", "")
        if future:
            candidate = _pack_texture_candidate(future, key, path)
            if candidate.is_file():
                selected_path = candidate
        mat = geometry.material(transaction, selected_path, key=key, scene=scene,
                                pbr_format=_pbr_format(scene,options),
                                pbr_maps=_pbr_companions(future,key,selected_path))
        if selected_path != path:
            baseline = transaction.track(bpy.data.images.load(str(path), check_existing=False))
            baseline.pack()
            mat[geometry.ORIGINAL_PATH] = str(path)
            mat[geometry.ORIGINAL_IMAGE] = baseline
            mat[geometry.ORIGINAL_ANIMATION] = json.dumps(geometry._animation(path, baseline))
        obj = (
            geometry.sprite(transaction, path.stem, mat, depth)
            if kind == "ITEM"
            else geometry.box(transaction, path.stem, (0, 0, 0), (16, 16, 16), mat)
        )
        collection, root = _new_root(transaction, "image:" + path.name, path.stem, special=False,
                                     creation_id=creation_id, object_root=obj if destination != "SLOT" else None)
        if obj != root:
            collection.objects.link(obj)
            obj.parent = root
        else:
            root["oxal_mc_studio_image_root_schema"] = 1
        if kind == "BLOCK" and destination in {"DISPLAY","SLOT"}:
            cast(bpy.types.Mesh,obj.data).transform(Matrix.Translation((0,0,-.5)))
        root["oxal_mc_studio_image_path"] = str(path)
        root["oxal_mc_studio_image_kind"] = kind
        root["oxal_mc_studio_image_depth"] = depth
        root["oxal_mc_studio_pack_digest"] = future
        root["oxal_mc_studio_geometry_kind"] = "BLOCK" if kind == "BLOCK" else "ITEM"
        root["oxal_mc_studio_presentation_context"] = "slot" if destination == "SLOT" else "world"
        root["oxal_mc_studio_special"] = False
        root.location = position if destination == "CURSOR" else (0, 0, 0)
        if destination == "DISPLAY":
            bind_display(scene, root, options, transaction=transaction)
        from .asset_collections import publish as publish_collection

        publish_collection(transaction, scene, collection)
        change = None
        if slot_target is not None:
            from .asset_display import slot_transform
            proxy = "minecraft:stone" if kind == "BLOCK" else "minecraft:apple"
            gui = transaction.track(bpy.data.objects.new("图片资产表示 · GUI",None))
            collection.objects.link(gui)
            gui.parent = root
            gui["oxal_mc_studio_representation"] = "GUI"
            gui["oxal_mc_studio_slot_visual_profile"] = kind
            gui["oxal_mc_studio_slot_canvas_kind"] = "BLOCK" if kind == "BLOCK" else "SPRITE"
            gui["oxal_mc_studio_slot_pose_schema"] = 1
            gui["oxal_mc_studio_slot_geometry_frame"] = 1
            display = transaction.track(bpy.data.objects.new("GUI · 原版显示变换",None))
            collection.objects.link(display)
            display.parent = gui
            display["oxal_mc_studio_model_part"] = "库存 · GUI显示变换"
            display["oxal_mc_studio_representation"] = "GUI"
            display.matrix_basis = slot_transform(proxy,"gui",visual_profile=kind)
            obj.parent = display
            obj["oxal_mc_studio_representation"] = "GUI"
            held = None
            if slot_target.view_mode == "FIRST_PERSON":
                held = transaction.track(bpy.data.objects.new("图片资产表示 · HELD",None))
                collection.objects.link(held)
                held.parent = root
                held["oxal_mc_studio_representation"] = "HELD"
                held["oxal_mc_studio_slot_visual_profile"] = kind
                held["oxal_mc_studio_slot_canvas_kind"] = "BLOCK" if kind == "BLOCK" else "SPRITE"
                held["oxal_mc_studio_slot_pose_schema"] = 1
                held["oxal_mc_studio_slot_geometry_frame"] = 1
                body = transaction.track(obj.copy())
                body.data = transaction.track(cast(bpy.types.Mesh,obj.data).copy())
                collection.objects.link(body)
                body.parent = held
                body["oxal_mc_studio_representation"] = "HELD"
                held.matrix_basis = slot_transform(proxy,"firstperson_lefthand" if slot_target.handedness == "LEFT" else "firstperson_righthand",visual_profile=kind)
            change = inventory_slots.bind_source(transaction,root,options["inventory_target"],quantity=int(options.get("quantity",1)),replace=bool(options.get("replace",False)),gui_representation=gui,held_representation=held)
        from .asset_removal import plan as removal_plan
        displaced = [removal_plan(scene,old) for old in change.previous] if change else []
        _stamp(transaction, root)
        if bpy.context.mode == "OBJECT" and destination != "SLOT":
            _activate(scene, root, transaction=transaction)
        transaction.commit(root)
        if change:
            change.finalize()
            from .asset_removal import remove as remove_displaced
            for old in displaced:
                remove_displaced(scene,old)
    return {"message": "已导入图片资产：" + path.name, "instance_id": root[INSTANCE]}


def _display_target(scene, options):
    target = find_root(scene, str(options.get("display_target", "")))
    slots = int(target.get("oxal_mc_studio_display_slots", 0))
    slot = int(options.get("display_slot", 1))
    if not 1 <= slot <= slots:
        raise ValueError("目标不是展示框/木展示架，或展示槽无效")
    return target, slots, slot


def bind_display(scene, root, options, *, transaction=None):
    from .asset_display import transform
    ensure_mutation_allowed()
    target,slots,slot = _display_target(scene,options)
    if root == target or target in tuple(root.children_recursive):
        raise ValueError("不能建立循环展示绑定")
    occupied = [obj for obj in roots(scene) if obj != root and obj.get("oxal_mc_studio_display_target") == target.get(INSTANCE) and obj.get("oxal_mc_studio_display_slot") == slot and obj.get("oxal_mc_studio_display_binding_active",True)]
    if occupied and not options.get("replace",False):
        raise ValueError("展示槽已占用，请明确选择替换")
    if transaction is None:
        with geometry.Creation() as local:
            bind_display(scene, root, options, transaction=local)
            local.commit()
        return
    for obj in occupied:
        require_owned(obj,(obj,))
        from ...util.animation_edit import require_property_edit
        require_property_edit(obj, '["oxal_mc_studio_display_binding_active"]')
        for mesh in members(obj):
            if mesh.type == "MESH":
                require_property_edit(mesh, "hide_render")
    if root.parent is not None and not root.get("oxal_mc_studio_display_target"):
        raise ValueError("资产已有用户父级，未覆盖")
    if any(constraint.type == "CHILD_OF" and constraint.name != "OXAL · 原生展示绑定" for constraint in root.constraints):
        raise ValueError("资产含用户父级约束，未覆盖")
    context = "on_shelf" if slots == 3 else "fixed"
    display = transform(root[IDENTIFIER],context) if root[IDENTIFIER].startswith("minecraft:") else transform("minecraft:stone" if root.get("oxal_mc_studio_image_kind") == "BLOCK" else "minecraft:apple",context)
    display = Matrix.Rotation(math.radians(45*int(options.get("rotation",0))),4,"Y") @ display
    bone_name = "展示 · "+(("左格","中格","右格")[slot-1] if slots == 3 else "展示物品")
    if target.type != "ARMATURE" or bone_name not in target.pose.bones:
        raise ValueError("目标缺少原生展示槽；请先显式修复或以特殊模型导入目标")
    constraint = root.constraints.get("OXAL · 原生展示绑定")
    if constraint is not None and constraint.type != "CHILD_OF":
        raise ValueError("展示绑定约束类型已改变，未覆盖")
    from ...util.animation_edit import require_property_edit
    for path in ("location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale"):
        require_property_edit(root, path)
    for key in _DISPLAY_PROPERTIES:
        require_property_edit(root, '["' + key + '"]')
    if constraint is not None:
        _require_display_paths(root, (constraint.path_from_id(),))
    _display_snapshot(transaction, root, constraint)
    if constraint is None:
        constraint = root.constraints.new("CHILD_OF")
        constraint.name = "OXAL · 原生展示绑定"
    constraint.target,constraint.subtarget = target,bone_name
    constraint.inverse_matrix = Matrix.Identity(4)
    root.parent = None
    root.matrix_parent_inverse = Matrix.Identity(4)
    root.matrix_basis = display
    root["oxal_mc_studio_display_host"] = target
    root["oxal_mc_studio_display_target"] = target[INSTANCE]
    root["oxal_mc_studio_display_slot"] = slot
    root["oxal_mc_studio_display_context"] = context
    root["oxal_mc_studio_display_binding_active"] = True
    if occupied:
        old = [(obj, "oxal_mc_studio_display_binding_active" in obj,
                obj.get("oxal_mc_studio_display_binding_active", True)) for obj in occupied]
        def restore_occupied():
            for obj, existed, active in old:
                if existed:
                    obj["oxal_mc_studio_display_binding_active"] = active
                elif "oxal_mc_studio_display_binding_active" in obj:
                    del obj["oxal_mc_studio_display_binding_active"]
        transaction.on_rollback(restore_occupied)
        for obj in occupied:
            obj["oxal_mc_studio_display_binding_active"] = False
            for mesh in members(obj):
                if mesh.type == "MESH":
                    previous = mesh.hide_render
                    transaction.on_rollback(lambda mesh=mesh,previous=previous:setattr(mesh,"hide_render",previous))
                    mesh.hide_render = True


_DISPLAY_PROPERTIES = ("oxal_mc_studio_display_host", "oxal_mc_studio_display_target",
                       "oxal_mc_studio_display_slot", "oxal_mc_studio_display_context",
                       "oxal_mc_studio_display_binding_active")


def _display_snapshot(transaction, root, constraint):
    parent, parent_type, parent_bone = root.parent, root.parent_type, root.parent_bone
    inverse, basis = root.matrix_parent_inverse.copy(), root.matrix_basis.copy()
    properties = {key: root[key] for key in _DISPLAY_PROPERTIES if key in root}
    values = ((constraint.target, constraint.subtarget, constraint.inverse_matrix.copy())
              if constraint is not None else None)

    def restore():
        current = root.constraints.get("OXAL · 原生展示绑定")
        if constraint is None:
            if current is not None:
                root.constraints.remove(current)
        else:
            assert values is not None
            constraint.target, constraint.subtarget, constraint.inverse_matrix = values
        root.parent, root.parent_type, root.parent_bone = parent, parent_type, parent_bone
        root.matrix_parent_inverse, root.matrix_basis = inverse, basis
        for key in _DISPLAY_PROPERTIES:
            if key in properties:
                root[key] = properties[key]
            elif key in root:
                del root[key]

    transaction.on_rollback(restore)


def _require_display_paths(root, prefixes):
    """A removed constraint/property must not strand animation or driver reads."""
    from ...util.animation import action_fcurves

    animation = root.animation_data
    bindings = [animation] if animation else []
    strips = [strip for track in animation.nla_tracks for strip in track.strips] if animation else []
    while strips:
        strip = strips.pop()
        bindings.append(strip)
        strips.extend(getattr(strip, "strips", ()))
    for binding in bindings:
        if any(curve.data_path.startswith(prefixes) for curve in action_fcurves(
                binding.action, slot=getattr(binding, "action_slot", None))):
            raise ValueError("展示绑定含 Action／NLA 动画，未覆盖或移除")
    if animation and any(curve.data_path.startswith(prefixes) for curve in animation.drivers):
        raise ValueError("展示绑定含用户驱动，未覆盖或移除")
    owners = set(bpy.data.user_map())
    owners.update(tree for owner in tuple(owners) if (tree := getattr(owner, "node_tree", None)) is not None)
    for owner in owners:
        data = getattr(owner, "animation_data", None)
        for curve in data.drivers if data else ():
            if any(target.id == root and target.data_path.startswith(prefixes)
                   for variable in curve.driver.variables for target in variable.targets):
                raise ValueError("其它驱动正在读取展示绑定，未覆盖或移除")


def unbind_display(root):
    ensure_mutation_allowed()
    if not root.get("oxal_mc_studio_display_target"):
        raise ValueError("所选资产没有新版展示绑定")
    matrix = root.matrix_world.copy()
    constraint = root.constraints.get("OXAL · 原生展示绑定")
    if constraint is None or constraint.type != "CHILD_OF":
        raise ValueError("展示绑定约束缺失或类型已改变，未移除")
    _require_display_paths(root, (constraint.path_from_id(), *('["' + key + '"]' for key in _DISPLAY_PROPERTIES)))
    from ...util.animation_edit import require_property_edit
    for path in ("location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale"):
        require_property_edit(root, path)
    with geometry.Creation() as transaction:
        _display_snapshot(transaction, root, constraint)
        root.parent = None
        root.parent_type, root.parent_bone = "OBJECT", ""
        root.matrix_parent_inverse = Matrix.Identity(4)
        root.matrix_basis = matrix
        for key in _DISPLAY_PROPERTIES:
            if key in root:
                del root[key]
        transaction.prepare_commit()
        root.constraints.remove(constraint)
        transaction.finish_commit()


def _publish_pack_directory(temporary, directory):
    """Publish this owned staging directory without replacing concurrent data.

    Windows indexers can briefly retain a handle after the last file closes.
    Retry only its native access/sharing errors, for at most 350 ms. Other
    failures and an existing destination are never retried or overwritten.
    """
    if temporary.resolve().parent != directory.resolve().parent or temporary.is_symlink():
        raise ValueError("材质包暂存目录归属无法核验")
    delays = (0.0, 0.025, 0.075, 0.25)
    for attempt, delay in enumerate(delays):
        if delay:
            sleep(delay)
        if directory.exists():
            raise FileExistsError("材质包目标已存在，未覆盖同时入库的数据")
        try:
            temporary.rename(directory)
            return
        except PermissionError as error:
            if (getattr(error, "winerror", None) not in {5, 32}
                    or attempt == len(delays) - 1 or directory.exists()):
                raise


def add_pack(path_value):
    ensure_mutation_allowed()
    from .asset_pack_layout import plan

    path = Path(path_value).resolve()
    if not path.is_dir() and (not path.is_file() or path.suffix.lower() != ".zip"):
        raise ValueError("请选择ZIP或资源包目录")
    directory_files = []
    if path.is_dir():
        fingerprint = hashlib.sha256()
        total = 0
        for file in sorted(path.rglob("*")):
            if not file.is_file():
                continue
            if not file.resolve().is_relative_to(path):
                raise ValueError("资源包目录含越界链接")
            relative = file.relative_to(path).as_posix()
            if file.name != "pack.mcmeta" and ("/textures/" not in relative or not relative.endswith((".png",".png.mcmeta"))):
                continue
            data = file.read_bytes()
            total += len(data)
            if total>2_000_000_000 or len(directory_files)>=100000:
                raise ValueError("资源包超过安全读取预算")
            fingerprint.update(relative.encode())
            fingerprint.update(data)
            directory_files.append((file,relative))
        digest = fingerprint.hexdigest()
    else:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with ExitStack() as sources:
        if path.is_dir():
            files = {relative: file for file,relative in directory_files}
            names = tuple(files)
            def read_directory(name):
                file = files[name]
                if file.name == "pack.mcmeta" and file.stat().st_size > 1_048_576:
                    raise ValueError("材质包pack.mcmeta超过读取预算")
                return file.read_bytes()
            read = read_directory
        else:
            archive = sources.enter_context(zipfile.ZipFile(path))
            entries = archive.infolist()
            if len(entries)>100000 or sum(entry.file_size for entry in entries)>2_000_000_000:
                raise ValueError("材质包超过文件数或解压大小限制")
            names = tuple(entry.filename for entry in entries if not entry.is_dir())
            def read_archive(name):
                if Path(name).name == "pack.mcmeta" and archive.getinfo(name).file_size > 1_048_576:
                    raise ValueError("材质包pack.mcmeta超过读取预算")
                return archive.read(name)
            read = read_archive
        layout = plan(names, read, catalog.RESOURCE_PACK_FORMAT)
        directory = user_library()/"packs"/digest
        if directory.is_dir():
            saved = json.loads((directory/"pack.json").read_text("utf8"))
            if saved.get("import_layout") == "mcmeta-overlays-v1":
                compatible = saved.get("resource_format") == list(catalog.RESOURCE_PACK_FORMAT)
            else:
                compatible = (set(saved.get("files", {})) == {relative for _,relative,_ in layout.entries}
                              and not any(layer for _,_,layer in layout.entries))
            if not compatible:
                raise ValueError("已入库材质包采用旧版目录规则，请先恢复其使用实例、移除后重新入库")
            return {"message":"材质包已在库中","packs":packs()}
        temporary = directory.parent/(".tmp-"+uuid4().hex[:12])
        directory.parent.mkdir(parents=True, exist_ok=True)
        temporary.mkdir()  # Exclusive creation proves this transaction owns cleanup.
        hashes = {}
        try:
            for source,relative,_layer in layout.entries:
                target = temporary/relative
                if not target.resolve().is_relative_to(temporary.resolve()):
                    raise ValueError("材质包路径越界")
                data = read(source)
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(data)
                hashes[relative] = hashlib.sha256(data).hexdigest()
            count = sum(name.endswith(".png") for name in hashes)
            if not count:
                raise ValueError("材质包没有assets/<namespace>/textures/*.png")
            metadata = {"digest":digest,"label":path.stem,"textures":count,"files":hashes,
                        "import_layout":"mcmeta-overlays-v1","resource_format":list(catalog.RESOURCE_PACK_FORMAT),
                        "source_root":layout.root,"overlays":list(layout.overlays)}
            (temporary/"pack.json").write_text(json.dumps(metadata,ensure_ascii=False),encoding="utf8")
            _publish_pack_directory(temporary, directory)
        except Exception:
            if temporary.is_dir() and temporary.resolve().parent == directory.resolve().parent:
                shutil.rmtree(temporary)
            raise
    return {"message":f"已入库：{path.stem}（{count}张纹理）","packs":packs()}


def remove_pack(digest):
    ensure_mutation_allowed()
    _pack_path(digest,"minecraft:validation")
    base = (user_library()/"packs").resolve()
    directory = base/digest
    if directory.resolve().parent != base or directory.is_symlink():
        raise ValueError("材质包路径不能安全核验")
    metadata = json.loads((directory/"pack.json").read_text(encoding="utf8"))
    if metadata.get("digest") != digest:
        raise ValueError("材质包身份不匹配")
    for scene in bpy.data.scenes:
        if scene.get("oxal_mc_studio_future_pack") == digest or any(root.get("oxal_mc_studio_pack_digest") == digest for root in roots(scene)):
            raise ValueError("材质包仍用于场景实例或后续导入，请先恢复原版并取消后续应用")
    files,folders = [],[]
    for item in directory.rglob("*"):
        attributes = getattr(item.lstat(),"st_file_attributes",0)
        if item.is_symlink() or attributes & 0x400 or not item.resolve().is_relative_to(directory.resolve()):
            raise ValueError("材质包内含链接或越界数据，未移除")
        if item.is_dir():
            folders.append(item)
        else:
            relative = item.relative_to(directory).as_posix()
            if relative != "pack.json" and not (relative.startswith("assets/") and "/textures/" in relative and relative.endswith((".png",".png.mcmeta"))):
                raise ValueError("材质包目录含未知用户文件，未移除")
            files.append(item)
    if sum(item.suffix == ".png" for item in files) != metadata.get("textures"):
        raise ValueError("材质包文件清单已改变，未移除")
    expected = metadata.get("files")
    if expected is not None and {item.relative_to(directory).as_posix() for item in files if item.name != "pack.json"} != set(expected):
        raise ValueError("材质包文件范围已改变，未移除")
    prefix = str(directory).replace("\\","/").casefold()+"/"
    if any(not image.packed_file and str(bpy.path.abspath(image.filepath)).replace("\\","/").casefold().startswith(prefix) for image in bpy.data.images):
        raise ValueError("其它未打包图像仍引用这个材质包，未移除")
    for item in files:
        item.unlink()
    for item in sorted(folders,key=lambda path:len(path.parts),reverse=True):
        item.rmdir()
    directory.rmdir()
    return {"message":"已移除未使用的材质包","packs":packs()}


def _logical_texture(node, material, *, top=False):
    key = node.get(geometry.TEXTURE_KEY, "")
    original = node.get(geometry.ORIGINAL_PATH, "")
    if top and node.name == "OXAL Source Texture":
        key = key or material.get(geometry.TEXTURE_KEY, "")
        original = original or material.get(geometry.ORIGINAL_PATH, "")
    image = node.image
    if image is not None:
        key = key or image.get(geometry.TEXTURE_KEY, "") or image.get("oxal_resource_path164", "")
        source = original or image.filepath_raw
        normalized = str(source).replace("\\", "/")
        marker = "/assets/"
        if not key and marker in normalized and "/textures/" in normalized:
            tail = normalized.split(marker,1)[1]
            namespace,relative = tail.split("/textures/",1)
            key = namespace+":"+relative.removesuffix(".png")
    if str(key).startswith("assets/") and "/textures/" in str(key):
        namespace,relative = str(key)[7:].split("/textures/",1)
        key = namespace+":"+relative.removesuffix(".png")
    if key and ":" in key:
        original = original or str(catalog.resource(key,"textures",".png"))
    return str(key),str(original)


def replace_material_textures(transaction, owner, material, digest, restore, scene, *, pbr_format=None):
    """Prepare a private texture replacement, including native nested samplers.

    Public adapter for precisely selected human equipment and library assets.
    Publication and old data retirement remain the caller's transaction.
    """
    from ..enchantment import enchantment as service
    if material is None or not material.use_nodes:
        return material
    glint = service.inspect(material)
    choices = {}
    restore_sources = {}
    seen = set()
    def discover(tree,top=False):
        if tree in seen:
            return
        seen.add(tree)
        for node in tree.nodes:
            if node.type == "TEX_IMAGE" and not node.name.startswith(("OXAL Animation ","OXAL Sampler ","OXAL PBR ")):
                key,original = _logical_texture(node,material,top=top)
                if node.name == GLINT_PREFIX+"Texture":
                    key = "minecraft:misc/enchanted_glint_armor" if glint and glint["profile"] == "ARMOR" else "minecraft:misc/enchanted_glint_item"
                    original = str(catalog.resource(key,"textures",".png"))
                if not key or not original:
                    continue
                path = Path(original) if restore else _pack_texture_candidate(digest,key,original)
                if not path.is_file() and node.name == "OXAL Source Texture" and not restore and _pbr_companions(digest,key,original,overlay_only=True):
                    from ...util.packed_images import semantic_filepath

                    path = Path(bpy.path.abspath(semantic_filepath(node.image)))
                baseline = node.get(geometry.ORIGINAL_IMAGE)
                animation = node.get(geometry.ORIGINAL_ANIMATION, "null")
                if top and node.name == "OXAL Source Texture":
                    baseline = baseline or material.get(geometry.ORIGINAL_IMAGE)
                    animation = material.get(geometry.ORIGINAL_ANIMATION, "null")
                if restore and isinstance(baseline, bpy.types.Image):
                    restore_sources[tree, node.name] = baseline, json.loads(animation)
                if path.is_file() or (tree, node.name) in restore_sources:
                    choices[tree,node.name] = (key,path)
            if node.type == "GROUP" and node.node_tree is not None:
                discover(node.node_tree)
    discover(material.node_tree,True)
    if not choices:
        return material
    result, copies = _private_shader_trees(transaction, material)
    for (old_tree,name),(key,path) in choices.items():
        tree = copies[old_tree]
        node = tree.nodes[name]
        baseline = restore_sources.get((old_tree, name))
        image = transaction.track(baseline[0].copy() if baseline else bpy.data.images.load(str(path),check_existing=False))
        if image.size[0]<1 or image.size[1]<1:
            raise ValueError("材质包纹理无法解码："+str(path))
        if node.image is not None:
            image.colorspace_settings.name = node.image.colorspace_settings.name
        if image.packed_file is None:
            image.pack()
        node.image = image
        if name != GLINT_PREFIX+"Texture":
            prefix = "OXAL Animation " if name == "OXAL Source Texture" else "OXAL Sampler "+hashlib.sha256(name.encode()).hexdigest()[:8]+" "
            geometry.configure_animation(result,path,scene=scene,image_node=node,tree=tree,prefix=prefix,
                                         restore=baseline is not None,animation_override=baseline[1] if baseline else None)
        if name == "OXAL Source Texture":
            from . import asset_pbr

            if asset_pbr.compatible(result, tree=tree):
                asset_pbr.refresh(transaction, result, path, key=key, scene=scene,
                                  format=pbr_format or material.get(asset_pbr.PROPERTY, _pbr_format(scene)), tree=tree,
                                  maps=None if restore else _pbr_companions(digest,key,path))
    if glint:
        from ..enchantment.enchantment_pixels import refresh

        # The source sampler was replaced in this private transaction. Keep
        # compiled pixel cells aligned with its new texture/tile dimensions;
        # an old glint graph remains old until explicitly reapplied.
        refresh(result)
        service.stamp(result)
    return result


def publish_material_replacements(transaction, scene, targets, replacements, *, pack_digest=None):
    """Prepare native silhouette/outline publication in an existing transaction."""
    ensure_mutation_allowed()
    from ..enchantment import enchantment_outline as features
    from ..enchantment.enchantment_targets import asset_meshes
    from ..outline import outline_native

    if not replacements:
        raise ValueError("所选资产没有可替换的贴图映射")
    meshes = []
    for root in targets:
        transaction.owner = root
        cache = {old: new for obj, _index, old, new in replacements if root_of(obj) == root}
        glint_meshes = set(asset_meshes(root))
        for obj in members(root):
            if obj.type != "MESH":
                continue
            if obj in glint_meshes:
                features.suspend(transaction, root, obj)
            # Query the body slot after suspending generated geometry materials.
            slots = outline_native.base_slots(root, obj, transaction=transaction)
            first = next((slot.material for index, slot in slots if index == 0), None)
            if not obj.get("oxal_mc_studio_pixel_topology") or first not in cache or cache[first] == first:
                continue
            if obj.data.shape_keys or (obj.data.animation_data and (obj.data.animation_data.action or obj.data.animation_data.nla_tracks)):
                raise ValueError("像素物品含用户拓扑动画，未覆盖轮廓")
            temporary = geometry.sprite(transaction,obj.name,cache[first],obj.get("oxal_mc_studio_sprite_depth",.0625))
            transform = obj.get("oxal_mc_studio_sprite_transform")
            if transform:
                cast(bpy.types.Mesh,temporary.data).transform(Matrix([transform[i:i+4] for i in range(0,16,4)]))
            from .asset_contracts import legacy_mesh_preflight
            legacy_mesh_preflight(root, {temporary: obj}, ())
            prepared = temporary.data
            transaction.forget(temporary)
            bpy.data.objects.remove(temporary)
            from ..inventory.inventory_gui import replay_projection
            replay_projection(obj,prepared,transaction=transaction)
            meshes.append((obj,obj.data,prepared))
    _assign_materials(transaction,replacements,commit=False)
    for obj,old,new in meshes:
        transaction.on_rollback(lambda obj=obj,old=old:setattr(obj,"data",old))
        obj.data = new
    for root in targets:
        transaction.owner = root
        if root.get("oxal_mc_studio_presentation_context") == "slot":
            from .asset_slot_geometry import refresh_pack_bounds
            refresh_pack_bounds(transaction,root)
        from ..enchantment import enchantment as shader_service
        for obj in asset_meshes(root):
            if any(shader_service.inspect(slot.material) for slot in obj.material_slots if slot.material):
                features.apply(transaction, root, obj, root)
        if pack_digest is not None:
            previous = root.get("oxal_mc_studio_pack_digest", "")
            transaction.on_rollback(lambda root=root, previous=previous: root.__setitem__("oxal_mc_studio_pack_digest", previous))
            root["oxal_mc_studio_pack_digest"] = pack_digest
        _stamp_geometry(transaction, root)


def apply_pack(scene, options, restore=False):
    ensure_mutation_allowed()
    from ..outline import outline_native

    targets = selected_roots(scene, options.get("instances"))
    digest = options.get("digest", "")
    replacements = []
    with geometry.Creation() as transaction:
        for root in targets:
            _require_members(root)
            from .asset_contracts import PROPERTY, validate
            if PROPERTY in root:
                validate(root)
            transaction.owner = root
            cache = {}
            for obj in members(root):
                if obj.type != "MESH":
                    continue
                for index, slot in outline_native.base_slots(root, obj, transaction=transaction):
                    old = slot.material
                    if old not in cache:
                        cache[old] = replace_material_textures(transaction, root, old, digest, restore, scene,
                                                               pbr_format=_pbr_format(scene, options))
                    if old != cache[old]:
                        replacements.append((obj, index, old, cache[old]))
        publish_material_replacements(transaction, scene, targets, replacements, pack_digest="" if restore else digest)
        transaction.commit()
    return {"message":"已恢复所选资产原版纹理" if restore else "已替换所选资产全部原生形态与轮廓"}


def upgrade_pbr(scene, options):
    """Explicit private conversion; never migrate materials from draw/load."""
    from ...util.packed_images import semantic_filepath
    from ..enchantment import enchantment as shader_service
    from ..outline import outline_native
    from . import asset_pbr
    from .asset_contracts import validate

    targets = selected_roots(scene, options.get("instances"))
    changes = []
    with geometry.Creation() as transaction:
        for root in targets:
            _require_members(root)
            validate(root)
            transaction.owner = root
            cache = {}
            for obj in members(root):
                if obj.type != "MESH":
                    continue
                for index, slot in outline_native.base_slots(root, obj, transaction=transaction):
                    old = slot.material
                    if old is None or not old.use_nodes:
                        continue
                    if old not in cache:
                        shader_service.inspect(old)
                        result, copies = _private_shader_trees(transaction, old)
                        converted = 0
                        for tree in copies.values():
                            source = tree.nodes.get("OXAL Source Texture")
                            if source is None or source.type != "TEX_IMAGE" or source.image is None:
                                continue
                            key, original = _logical_texture(source, result, top=tree == result.node_tree)
                            if not key and root[IDENTIFIER].startswith("image:"):
                                origin = str(root.get("oxal_mc_studio_image_path", original))
                                key = "oxal_image:" + hashlib.sha256(origin.encode("utf8")).hexdigest()[:16] + "/" + Path(origin).stem
                                result[geometry.TEXTURE_KEY] = key
                            path = Path(bpy.path.abspath(semantic_filepath(source.image)))
                            asset_pbr.install(transaction, result, path, key=key, scene=scene,
                                              format=_pbr_format(scene, options), tree=tree)
                            converted += 1
                        if not converted:
                            raise ValueError("材质没有可核验的 OXAL 图片表面，未改写用户着色器：" + old.name)
                        if shader_service.inspect(old):
                            shader_service.stamp(result)
                        cache[old] = result
                    changes.append((obj, index, old, cache[old]))
        if not changes:
            raise ValueError("所选资产没有可升级的材质")
        _assign_materials(transaction, changes)
    return {"message": "已升级所选资产的 PBR 材质，保留几何、绑定及附魔"}


def _set_glint(transaction, root, old, *, copy=True, uv_name=None):
    from ..enchantment import enchantment as service
    _AddedProperties(transaction).glint_defaults(root)
    kind = root.get("oxal_mc_studio_geometry_kind", "BLOCK" if catalog.resource(root.get(IDENTIFIER,""),"blockstates").is_file() else "ITEM")
    key = "minecraft:misc/enchanted_glint_armor" if kind == "ARMOR" else "minecraft:misc/enchanted_glint_item"
    scene = root.users_scene[0]
    selected,original = _texture(scene,key,pack_digest=root.get("oxal_mc_studio_pack_digest",""))
    current = service.inspect(old)
    images = None
    if current and current["version"] == service.VERSION and current["owner"] == root:
        texture = old.node_tree.nodes[service.PREFIX + "Texture"].image
        baseline = old.get(service.ORIGINAL_IMAGE)
        if isinstance(texture, bpy.types.Image) and isinstance(baseline, bpy.types.Image):
            require_owned(root, (texture, baseline))
            images = texture, baseline
    result = service.apply(transaction,root,old,geometry_kind=kind,scene=scene,
                           texture_path=selected,original_path=original,copy=copy,uv_name=uv_name)
    if images is not None and result != old:
        # A shader-schema upgrade changes the generated layer, not the
        # instance's actual packed/custom glint texture or restore baseline.
        result.node_tree.nodes[service.PREFIX + "Texture"].image = images[0]
        result[service.ORIGINAL_IMAGE] = images[1]
        service.stamp(result)
    return result


def enchant(scene, options, remove=False):
    ensure_mutation_allowed()
    if remove or options.get("action", "ENCHANT") == "ENCHANT":
        from . import asset_enchantment

        # Preserve direct business callers. UI submissions always carry the
        # Operator's earlier frozen snapshot; never replace a supplied one.
        if "material_targets" not in options:
            options = {**options, "material_targets": asset_enchantment.request(scene, options)}
        return asset_enchantment.execute(scene, options, remove=remove)
    targets = selected_roots(scene, options.get("instances"))
    changes = []
    from ..enchantment import enchantment as service
    from ..enchantment import enchantment_outline as features
    from ..enchantment.enchantment_targets import asset_meshes
    prepare_geometry = options.get("action") == "ENCHANT_3D_PREPARE"
    prepare_outline = options.get("action") == "ENCHANT_OUTLINE_PREPARE"
    if prepare_outline:
        for root in targets:
            if features._outline_animated(root):
                raise ValueError("附魔描边开关已有关键帧、NLA或驱动，请在原通道启用，未覆盖其动画")
    with geometry.Creation() as transaction:
        added = _AddedProperties(transaction)
        prepared_roots = []
        for root in targets:
            _require_members(root)
            meshes = asset_meshes(root)
            from ..outline import outline_native

            structural = not remove or any(features.RECORD in obj or outline_native.prepared(obj, role="enchant") for obj in meshes)
            if structural:
                _enchant_geometry_preflight(root)
            transaction.owner = root
            for obj in meshes:
                if remove:
                    features.remove(transaction, root, obj)
                else:
                    features.suspend(transaction, root, obj)
            if not remove:
                added.glint_defaults(root)
            cache = {}
            for obj in meshes:
                for index, slot in outline_native.base_slots(root, obj, transaction=transaction):
                    old = slot.material
                    if old is None or not old.use_nodes:
                        continue
                    if old not in cache:
                        if remove:
                            new = service.remove(transaction,root,old)
                        else:
                            new = _set_glint(transaction, root, old)
                        cache[old] = new
                    changes.append((obj, index, old, cache[old]))
            prepared_roots.append((root, meshes, structural))
        _assign_materials(transaction, changes, commit=False)
        for root, meshes, structural in prepared_roots:
            transaction.owner = root
            for obj in meshes:
                if remove:
                    features.remove(transaction, root, obj)
                elif any(service.inspect(slot.material) for slot in obj.material_slots if slot.material):
                    features.apply(transaction, root, obj, root, prepare_geometry=prepare_geometry,
                                   prepare_outline=prepare_outline)
            if prepare_geometry:
                before = root["glint_geometry_enabled"]
                transaction.on_rollback(lambda root=root, before=before: root.__setitem__("glint_geometry_enabled", before))
                root["glint_geometry_enabled"] = True
            if prepare_outline:
                before = root["glint_outline_enabled"]
                transaction.on_rollback(lambda root=root, before=before: root.__setitem__("glint_outline_enabled", before))
                root["glint_outline_enabled"] = True
                root.update_tag()
            if structural:
                _stamp_geometry(transaction, root)
        transaction.commit()
    return {
        "message": ("已取消附魔" if remove else "已准备像素3D凹凸，厚度与高低参数可插帧" if prepare_geometry
                    else "已准备附魔描边，可启用并插入关键帧" if prepare_outline
                    else "已应用附魔（参数可在实例上插入关键帧）")
    }


def _rebuild_target(scene, root):
    collections = tuple(root.users_collection)
    if len(collections) != 1 or collections[0].get(INSTANCE) != root[INSTANCE]:
        raise ValueError("资产根链接到其它合集，未重建")
    collection = collections[0]
    require_owned(root,(root,collection))
    if any(collection in tuple(other.collection.children_recursive) for other in bpy.data.scenes if other != scene):
        raise ValueError("资产合集由多个场景共享，未重建")
    image_root = (root.type == "MESH" and root.get(IDENTIFIER, "").startswith("image:")
                  and root.get("oxal_mc_studio_image_root_schema") == 1)
    children = members(root) if image_root else members(root)[1:]
    require_owned(root,children)
    require_owned(root,[obj.data for obj in children if obj.data is not None])
    # Independent displayed/slotted assets keep their own provenance and IDs.
    for obj in tuple(root.children_recursive):
        if obj not in children and obj.get(ASSET_ID) == RESOURCE_ID:
            require_owned(obj,(obj,))
            if obj.get("oxal_mc_studio_display_host") != root and not (obj.parent == root and obj.get("oxal_mc_studio_display_target") == root[INSTANCE]):
                raise ValueError("独立子资产绑定关系无法核验，未重建")
    for obj in children:
        if any(not owns(root,owner) for owner in obj.users_collection):
            raise ValueError("资产部件链接到用户合集，未覆盖")
        if obj.type == "MESH" and (obj.data.shape_keys or (obj.data.animation_data and (obj.data.animation_data.action or obj.data.animation_data.nla_tracks))):
            raise ValueError("资产网格含用户拓扑动画，保留现状，未重建")
        if obj.type == "MESH":
            from ..enchantment.enchantment_geometry import generated_attributes
            from ..enchantment.enchantment_outline import generated_modifiers
            generated_modifiers(root, obj)
            generated_attributes(root, obj)
    if image_root and any(obj != root and obj.data == root.data for obj in bpy.data.objects):
        raise ValueError("图片根网格被其它对象共享，保留现状，未重建")
    if root.type == "ARMATURE":
        require_owned(root,(root.data,))
        if any(obj != root and obj.data == root.data for obj in bpy.data.objects):
            raise ValueError("骨架数据由其它对象使用，保留现状，未重建")
        if root.data.animation_data and (root.data.animation_data.action or root.data.animation_data.nla_tracks):
            raise ValueError("骨架数据含用户动画，无法安全替换，保留现状")
    return collection,children


def rebuild(scene, options):
    from .asset_bounds import repair_legacy
    from .asset_rebuild import rebuild as staged_rebuild
    ensure_mutation_allowed()
    root = find_root(scene,str(options.get("instance","")))
    repair = options.get("action") == "REPAIR"
    with geometry.Creation() as transaction:
        if repair:
            repair_legacy(transaction, root)
        result = staged_rebuild(scene, root, options, repair=repair)
        # The staged geometry transaction already committed its owned IDs.
        # This outer journal owns only the existing root's display fields.
        transaction.finish_commit()
    return result


def execute(scene_uid: int, options):
    ensure_mutation_allowed()
    scene = resolve_scene(scene_uid)
    action = options.get("action")
    from ..enchantment import enchantment_external

    if action in enchantment_external.ACTIONS:
        return enchantment_external.execute(scene_uid, options)
    if action == "ENCHANT_OUTLINE_REBIND":
        from .asset_outline_rebind import rebind

        return {"message": rebind(scene, options)}
    if action == "SHADER_LAYOUT":
        from ..shader_layout import execute as arrange_shaders

        return arrange_shaders(options)
    if action == "CATALOG":
        asset_extensions.prepare()
        from .asset_potions import definitions
        spawner_entities = catalog.read(catalog.ROOT/"spawner_catalog.json")["entries"]
        entries = catalog.filtered(
            str(options.get("search", "")), str(options.get("category", "ALL"))
        )
        page_size = 24
        pages = max(1, math.ceil(len(entries) / page_size))
        page = max(0, min(pages - 1, int(options.get("page", 0))))
        visible = entries[page * page_size:(page + 1) * page_size]
        return {
            "message": f"已加载 {len(entries)} 项资产",
            "page": page,
            "pages": pages,
            "total": len(entries),
            "catalog": [
                {
                    "identifier": entry.identifier,
                    "label": entry.label,
                    "schema": catalog.schema(entry.identifier),
                    "defaults": catalog.default_state(entry.identifier),
                }
                for entry in visible
            ],
            "previews": [
                {"identifier": entry.identifier, "path": str(path)}
                for entry in visible
                if (path := catalog.preview_path(entry.identifier)) is not None
            ],
            "packs": packs(),
            "potion_presets":definitions(),
            "spawner_entities":spawner_entities,
        }
    if action == "PREVIEW":
        identifier = str(options.get("identifier", ""))
        path = catalog.preview_path(identifier)
        if path is None:
            raise ValueError("该资产暂不提供平面贴图预览")
        return {
            "message": "已加载所选资产预览",
            "preview": {"identifier": identifier, "path": str(path)},
        }
    if action == "READ_VARIANT":
        root = find_root(scene, str(options.get("instance", "")))
        potion_rows = json.loads(root.get("oxal_mc_studio_potion_definitions","[]"))
        potion_index = max(0,min(len(potion_rows)-1,round(float(root.get("药水种类",0)))))
        return {
            "message": "已读取所选资产变体",
            "variants": catalog.schema(root[IDENTIFIER]),
            "state": json.loads(root.get("oxal_mc_studio_block_state", "{}")),
            "shape": int(root.get("oxal_mc_studio_shape", 0)),
            "complete_structure": bool(root.get("oxal_mc_studio_complete_structure", True)),
            "variant_instance":root[INSTANCE],
            "spawn_entity":root.get("oxal_mc_studio_spawn_entity","pig"),
            "potion_key":potion_rows[potion_index]["key"] if potion_rows else "poison",
        }
    if action == "IMPORT":
        return import_asset(scene, options)
    if action == "IMAGE":
        return image_asset(scene, options)
    if action == "PACK_ADD":
        return add_pack(str(options.get("path", "")))
    if action == "PACK_LIST":
        return {"message": "资源包列表已刷新", "packs": packs()}
    if action == "PACK_REMOVE":
        return remove_pack(str(options.get("digest","")))
    if action == "PACK_APPLY":
        return apply_pack(scene, options)
    if action == "PACK_RESTORE":
        return apply_pack(scene, options, True)
    if action == "PACK_FUTURE":
        digest = options.get("digest", "")
        if digest and digest not in {pack["digest"] for pack in packs()}:
            raise ValueError("材质包未在库中")
        scene["oxal_mc_studio_future_pack"] = digest
        scene["oxal_mc_studio_pbr_format"] = _pbr_format(scene, options)
        return {"message": "后续导入：" + ("所选材质包" if digest else "原版贴图")}
    if action == "PBR_UPGRADE":
        return upgrade_pbr(scene, options)
    if action in {"ENCHANT", "UNENCHANT", "ENCHANT_3D_PREPARE", "ENCHANT_OUTLINE_PREPARE"}:
        return enchant(scene, options, action == "UNENCHANT")
    if action == "POTION":
        from .asset_potions import set_preset
        root = find_root(scene,str(options.get("instance","")))
        _require_members(root)
        return set_preset(scene,root,str(options.get("potion_key","poison")))
    if action == "SLOT_PHASE":
        from .asset_slot_states import set_phase
        root = find_root(scene,str(options.get("instance","")))
        _require_members(root)
        return set_phase(scene,root,options.get("slot_phase",0))
    if action == "ARROW_BAKE":
        from .asset_arrow_bake import bake
        root=find_root(scene,str(options.get("instance","")))
        return bake(scene,root,options)
    if action == "PARTICLES_REBUILD":
        root=find_root(scene,str(options.get("instance","")))
        _require_members(root)
        particle_tool=asset_extensions.particles_module(root)
        if particle_tool is None:raise ValueError("当前资产没有有限时间轴粒子")
        return particle_tool.rebuild(scene,root,options)
    if action == "PROJECTILE_HIT":
        from .asset_projectiles import trigger
        root=find_root(scene,str(options.get("instance","")))
        _require_members(root)
        return trigger(scene,root)
    if action in {"PROJECTILE_CACHE_BAKE", "PROJECTILE_CACHE_CLEAR"}:
        from .asset_projectile_clock import cache
        root = find_root(scene, str(options.get("instance", "")))
        _require_members(root)
        return cache(scene, root, options, clear=action == "PROJECTILE_CACHE_CLEAR")
    if action == "ARMOR_STAND_POSE":
        from .asset_poses import apply_pose
        root = find_root(scene,str(options.get("instance","")))
        _require_members(root)
        return apply_pose(root,str(options.get("stand_pose","RESET")),scene=scene)
    if action in {"BOOK_TARGET", "BOOK_TARGET_CLEAR"}:
        from .asset_book import bind_target
        root = find_root(scene, str(options.get("instance", "")))
        _require_members(root)
        snapshot = options.get("book_target_snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("remove") != (action == "BOOK_TARGET_CLEAR"):
            raise ValueError("书本玩家目标快照不完整，请重新提交")
        return bind_target(scene, root, snapshot)
    if action == "LIGHTNING":
        from .asset_lightning import emit
        root = find_root(scene,str(options.get("instance","")))
        _require_members(root)
        return emit(scene,root,options)
    if action == "REMOVE":
        from .asset_removal import selected
        return selected(scene,options.get("instances"))
    if action == "POSE_CONTROLS":
        from .asset_contracts import control_rig
        if bpy.context.mode not in {"OBJECT", "POSE"}:
            raise ValueError("请先退出编辑模式，再进入资产原生控件")
        root = find_root(scene,str(options.get("instance","")))
        rig = control_rig(root)
        if rig is None:
            raise ValueError("当前资产没有原生骨骼控件")
        require_owned(root,(rig,rig.data))
        layer = resolve_view_layer(scene)
        book = root.get("内部书本骨架")
        if book is not None:
            if not isinstance(book, bpy.types.Object) or book.type != "ARMATURE":
                raise ValueError("书本独立骨架类型已改变，未选择")
            if book not in root.children_recursive:
                raise ValueError("书本独立骨架不属于当前资产，未选择")
            assert isinstance(book.data, bpy.types.Armature)
            require_owned(root, (book, book.data))
        for obj in (rig, book) if book is not None else (rig,):
            if layer.objects.get(obj.name) != obj or not obj.visible_get(view_layer=layer) or obj.hide_select:
                raise ValueError("原生控件在当前视图不可选择，未改变选择")
        previous, mode = layer.objects.active, bpy.context.mode
        selection = tuple(obj for obj in layer.objects if obj.select_get(view_layer=layer))

        def restore_pose_context():
            if bpy.context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            for obj in tuple(layer.objects):
                obj.select_set(obj in selection, view_layer=layer)
            layer.objects.active = previous
            if mode == "POSE":
                bpy.ops.object.mode_set(mode="POSE")

        with geometry.Creation() as transaction:
            transaction.on_rollback(restore_pose_context)
            if bpy.context.mode == "POSE":
                bpy.ops.object.mode_set(mode="OBJECT")
            _activate(scene, rig)
            if book is not None:
                book.select_set(True, view_layer=layer)
            bpy.ops.object.mode_set(mode="POSE")
            transaction.commit()
        return {"message":"已进入当前资产原生控件"}
    if action in {"BIND", "UNBIND"}:
        selected = selected_roots(scene, options.get("instances"))
        if len(selected) != 1:
            raise ValueError("请选择一个待绑定资产")
        if action == "BIND":
            bind_display(scene, selected[0], options)
        else:
            unbind_display(selected[0])
        return {"message": "已绑定展示槽" if action == "BIND" else "已解除展示绑定"}
    if action in {"REBUILD","REPAIR"}:
        return rebuild(scene, options)
    raise ValueError("未知资产操作")
