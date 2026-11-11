"""Append sanitized authored native rigs into a fresh library instance.

Policy NEW_INSTANCE, instance-private dependencies. Sources are the explicitly
listed collections in asset/library/native_effects; provenance is in SOURCE.
No source Python code, scene or runtime callback is loaded.
"""

import json

import bpy

from ...util.packed_images import rebase_native_image
from ...util.resources import AppendTransaction, live_ids
from . import asset_catalog as catalog
from . import asset_geometry as geometry
from . import asset_rigs

PREFAB = "oxal_mc_studio_native_prefab"
TEMPLATE = "oxal_mc_studio_native_template_root"
FPS = "oxal_mc_studio_native_scene_clock"


def _rna(source, target, *, exclude=()):
    for prop in source.bl_rna.properties:
        key = prop.identifier
        if key == "rna_type" or key in exclude or prop.is_readonly or prop.type == "COLLECTION":
            continue
        setattr(target, key, getattr(source, key))


def _props(source, target):
    for key in source.keys():  # noqa: SIM118 -- Blender RNA iteration is not IDProperty iteration.
        if key == "_RNA_UI":
            continue
        target[key] = source[key]
        info = source.id_properties_ui(key).as_dict()
        if info:
            target.id_properties_ui(key).update(**info)


def _constraints(source, target):
    for constraint in source.constraints:
        copied = target.constraints.new(constraint.type)
        _rna(constraint, copied, exclude={"is_override_data"})


def _drivers(items):
    for item in items:
        for owner in (item, getattr(item, "node_tree", None)):
            animation = getattr(owner, "animation_data", None)
            yield from animation.drivers if animation else ()


def load(transaction, scene, collection, root, path, collection_name, *, pack_digest=None):
    """Load one source-proven native collection, retaining the new root ID."""
    with AppendTransaction(path) as append:
        source_collection = append.append_collection(collection_name)
        objects = tuple(source_collection.all_objects)
        templates = [obj for obj in objects if obj.get(TEMPLATE)]
        proxies = [obj for obj in objects if obj.get(FPS)]
        if len(templates) != 1 or len(proxies) != 1:
            raise ValueError("原生资产模板根或场景时钟声明不唯一")
        source, proxy = templates[0], proxies[0]
        if source.type != "ARMATURE" or root.type != "ARMATURE":
            raise ValueError("原生资产模板需要独立骨架根")
        proven = tuple(append.owned)
        if any(isinstance(item, (bpy.types.Scene, bpy.types.Text)) for item in proven):
            raise ValueError("原生资产模板包含运行脚本或源场景")
        for item in proven:
            if not isinstance(item, bpy.types.Material) or item.node_tree is None:
                continue
            keys = []
            for node in item.node_tree.nodes:
                if not isinstance(node, bpy.types.ShaderNodeTexImage) or node.image is None:
                    continue
                key = node.get(geometry.TEXTURE_KEY, "") or node.image.get(geometry.TEXTURE_KEY, "")
                if not key:
                    # Compatibility for the known previous native template,
                    # before images carried their own declared logical key.
                    normalized = node.image.filepath.replace("\\", "/")
                    if "/textures/" not in normalized or not normalized.endswith(".png"):
                        raise ValueError("原生资产模板纹理缺少规范逻辑身份")
                    before, relative = normalized.rsplit("/textures/", 1)
                    namespace = before.rsplit("/", 1)[-1]
                    key = namespace + ":" + relative.removesuffix(".png")
                key = str(key)
                original = catalog.resource(key, "textures", ".png")
                if not original.is_file():
                    raise ValueError("原生资产模板纹理未随包提供：" + key)
                node[geometry.TEXTURE_KEY] = key
                node[geometry.ORIGINAL_PATH] = str(original)
                node[geometry.ORIGINAL_IMAGE] = node.image
                node[geometry.ORIGINAL_ANIMATION] = "{}"
                rebase_native_image(node.image, key)
                keys.append(key)
            if len(keys) == 1:
                item[geometry.TEXTURE_KEY] = keys[0]
                item[geometry.ORIGINAL_PATH] = str(catalog.resource(keys[0], "textures", ".png"))
        curves = tuple(_drivers(proven))
        for curve in curves:
            if not curve.driver.is_simple_expression or curve.driver.use_self:
                raise ValueError("原生资产模板包含非原生简单驱动")
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id == proxy:
                        key = target.data_path
                        if key not in {'["fps"]', '["fps_base"]'}:
                            raise ValueError("原生资产模板时钟字段未知")
                        target.id_type = "SCENE"
                        target.id = scene
                        target.data_path = "render." + ("fps" if key == '["fps"]' else "fps_base")
                    elif target.id is not None and target.id not in append.owned:
                        raise ValueError("原生资产模板驱动引用未知外部数据")
        records = asset_rigs.driver_records(source)
        old_data = root.data
        root.data = source.data
        assert source.pose is not None and root.pose is not None
        root.show_in_front = source.show_in_front
        root.rotation_mode = source.rotation_mode
        _props(source, root)
        for bone in source.pose.bones:
            copied = root.pose.bones[bone.name]
            _rna(bone, copied, exclude={"name", "parent", "matrix", "matrix_channel", "matrix_basis", "constraints", "bone", "id_data", "custom_shape_transform"})
            copied.matrix_basis = bone.matrix_basis.copy()
            _props(bone, copied)
            _constraints(bone, copied)
        for bone in source.pose.bones:
            root.pose.bones[bone.name].custom_shape_transform = (
                root.pose.bones[bone.custom_shape_transform.name] if bone.custom_shape_transform else None
            )
        _constraints(source, root)
        # Remap only this newly appended source root's users. No existing scene
        # ID could have referenced this just-appended object beforehand.
        source.user_remap(root)
        asset_rigs.install_drivers(root, records, remap=source)
        for obj in objects:
            if obj in {source, proxy}:
                continue
            if obj.parent == source:
                obj.parent = root
            if obj.name not in collection.objects:
                collection.objects.link(obj)
        root[PREFAB] = 1
        root["oxal_mc_studio_native_rig_source"] = path.name
        if TEMPLATE in root:
            del root[TEMPLATE]
        # Materials, widgets and nested native groups have append provenance.
        # Creation owns their cleanup from this point onward.
        append.remove((source, proxy, source_collection))
        for item in live_ids(append.owned):
            transaction.track(item)
        append.commit(image_paths=transaction.image_paths)
        if old_data is not None and old_data.users == 0:
            transaction.forget(old_data)
            bpy.data.batch_remove(ids=(old_data,))
    # Source material samplers carry canonical texture metadata. Pack services
    # operate on their actual private material IDs after the native import.
    digest = scene.get("oxal_mc_studio_future_pack", "") if pack_digest is None else pack_digest
    if digest:
        from .assets import replace_material_textures
        replacements = {}
        for obj in tuple(root.children_recursive):
            if obj.type != "MESH":
                continue
            for slot in obj.material_slots:
                old = slot.material
                if old not in replacements:
                    replacements[old] = replace_material_textures(transaction, root, old, digest, False, scene)
                slot.material = replacements[old]
    root.update_tag()
    return root


def editable_fields(root):
    """Pure RNA description: driven public aliases edit their actual slider."""
    sliders = {}
    for spec in json.loads(root.get("骨骼控件", "[]")):
        for key in spec.get("keys", ()):
            sliders[key] = spec["bone"]
    return sliders
