"""Import a new, independently editable creature collection into the target scene."""

import json
import math
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path

import bpy
from mathutils import Vector

from ...core.runtime import ensure_mutation_allowed
from ...util.animation_edit import require_property_edit
from ...util.ownership import (
    owned_ids,
    owns,
    release_owned,
    remove_unused_owned,
    require_no_external_users,
    require_owned,
    seal_owned,
)
from ...util.resources import (
    ASSET_ID,
    INSTANCE,
    VERSION,
    AppendTransaction,
    instance_creation_id,
    resolve_view_layer,
)

ROOT = Path(__file__).resolve().parents[2] / "asset" / "creature"
LIBRARY = ROOT / "creatures_0_1_2.blend"
CREATURE_ID = "blockloom.creature"
CREATURE_VERSION = "0.1.2"
CREATURE_REVISION = 4
RIG = "blockloom_creature_rig"


@lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / "catalog.json").read_text(encoding="utf8"))


GROUPS = {"亡灵生物": "COLOR_01", "袭击生物": "COLOR_03", "村民": "COLOR_04",
          "猪灵": "COLOR_02", "末地生物": "COLOR_06", "动物": "COLOR_05", "幼年体": "COLOR_07"}


def categories(name):
    family = catalog()[name].removeprefix("幼年")
    species = name.split(" · ")[-1]
    if family in {"僵尸", "骷髅"} or species == "zombified_piglin":
        group = "亡灵生物"
    else:
        group = {"刌民": "袭击生物", "女巫": "袭击生物", "流浪商人": "村民",
                 "末影人": "末地生物"}.get(family, family)
    return (group, "幼年体") if name.startswith("幼年") else (group,)


def icon_path(name):
    species = name.split(" · ")[-1]
    return ROOT / "icons" / (("baby_" if name.startswith("幼年") else "") + species + ".png")


@lru_cache(maxsize=64)
def display_name(name):
    from .asset_catalog import read, resource

    family = catalog()[name]
    species = name.rsplit(" · ", 1)[-1]
    language = read(resource("minecraft:zh_cn", "lang"))
    label = "僵尸猪人" if species == "zombie_pigman" else language["entity.minecraft." + species]
    return ("幼年" if family.startswith("幼年") else "") + label


def needs_upgrade(root):
    return (root.get("blockloom_creature_version") != CREATURE_VERSION
            or root.get("blockloom_creature_asset_revision", 0) < CREATURE_REVISION)


def instances(scene):
    return tuple(root for root in scene.collection.children_recursive
                 if root.get(ASSET_ID) == CREATURE_ID and owns(root, root))


def selected_instance(scene, selection):
    selected = set(selection)
    if not selected:
        return None
    matches = [
        root for root in instances(scene)
        if any(owns(root, obj) for obj in selected.intersection(root.all_objects))
        and root.get(RIG) and owns(root, root[RIG])
    ]
    return matches[0] if len(matches) == 1 else None


def selected_rig(scene, obj):
    root = selected_instance(scene, (obj,)) if obj else None
    return root[RIG] if root else None


def load(scene, options, *, source_rig=None, bake_parent=False):
    name = options["creature"]
    identity = instance_creation_id(options)
    if name not in catalog():
        raise ValueError("未知生物资产")
    position = options["position"]
    if (
        not isinstance(position, (tuple, list))
        or len(position) != 3
        or any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in position
        )
    ):
        raise ValueError("生物导入位置必须包含三个有限坐标")
    if any(c.get(INSTANCE) == identity for c in scene.collection.children_recursive):
        raise ValueError("该生物实例已存在")
    with AppendTransaction(LIBRARY) as txn, ExitStack() as links:
        root = txn.append_collection(name)
        from .creature_parts import use_double_sided_materials
        use_double_sided_materials(txn)
        rigs = [
            obj
            for obj in root.all_objects
            if obj.type == "ARMATURE" and obj.get("oxal_humanoid_mob")
        ]
        if len(rigs) != 1 or root.get("blockloom_creature_version") != CREATURE_VERSION:
            raise ValueError("生物资源版本或骨架不匹配")
        rig = rigs[0]
        for key in (
            "oxal_humanoid_player_arms",
            "oxal_humanoid_armor_HEAD",
            "oxal_humanoid_armor_CHEST",
            "oxal_humanoid_armor_LEGS",
            "oxal_humanoid_armor_FEET",
        ):
            if key in rig and type(rig[key]) in (int, float):
                metadata = rig.id_properties_ui(key)
                if metadata is not None:
                    metadata.update(min=0, max=1, soft_min=0, soft_max=1)
        root[ASSET_ID], root[VERSION], root[INSTANCE] = (
            CREATURE_ID,
            CREATURE_VERSION,
            identity,
        )
        root[RIG] = rig
        root["blockloom_creature_asset_revision"] = CREATURE_REVISION
        # Compute the authored origin from local transforms even before the dependency graph sees the new collection.
        matrix = rig.matrix_basis.copy()
        child = rig
        while child.parent:
            matrix = child.parent.matrix_basis @ child.matrix_parent_inverse @ matrix
            child = child.parent
        offset = Vector(position) - matrix.translation
        for obj in root.all_objects:
            if obj.parent is None:
                obj.location += offset
        copied = {}
        if source_rig is not None:
            from ..human.human_migrate_animation import copy_animation
            rig.rotation_mode = source_rig.rotation_mode
            if bake_parent:
                rig.matrix_world = source_rig.matrix_world.copy()
            else:
                rig.parent = source_rig.parent
                rig.parent_type = source_rig.parent_type
                rig.parent_bone = source_rig.parent_bone
                rig.matrix_parent_inverse = source_rig.matrix_parent_inverse.copy()
                rig.matrix_basis = source_rig.matrix_basis.copy()
            skipped_paths = set()
            copied = {**copy_animation(txn, source_rig, rig, skipped_paths=skipped_paths),
                      "skipped_paths": sorted(skipped_paths)}
        from .asset_collections import library_parent, subgroup
        library = library_parent(scene, links.callback)
        group = subgroup(library, "生物", "COLOR_05", links.callback)
        kind = categories(name)[-1]
        parent = subgroup(group, kind, GROUPS[kind], links.callback)
        from .creature_parts import name_component
        for child in root.children:
            name_component(child)
        root.name = display_name(name)
        root.color_tag = GROUPS[kind]
        parent.children.link(root)
        layer = resolve_view_layer(scene)
        layer.update()
        if not any(obj.type == "MESH" and not obj.hide_render and obj.visible_get(view_layer=layer)
                   for obj in root.all_objects):
            raise ValueError("生物身体网格在当前视图层不可见，已撤回导入")
        select = bpy.context.scene == scene and bpy.context.mode == "OBJECT"
        previous = layer.objects.active
        selected = tuple(bpy.context.selected_objects) if select else ()
        try:
            if select:
                for obj in selected:
                    obj.select_set(False, view_layer=layer)
                rig.select_set(True, view_layer=layer)
                layer.objects.active = rig
                if not rig.select_get(view_layer=layer):
                    raise ValueError("生物骨架在当前视图层无法选中，已撤回导入")
            seal_owned(root, txn.owned, scene=scene)
            txn.commit()
            links.pop_all()
        except Exception:
            if select:
                rig.select_set(False, view_layer=layer)
                layer.objects.active = previous
                for obj in selected:
                    obj.select_set(True, view_layer=layer)
            if owns(root, root):
                release_owned(root)
            raise
    return {"message": "已导入 " + display_name(name), **copied}


def _style(scene, root, rig, field, value):
    from ..human.human import _set_ankles
    from ..human.human_compat import LegacyHuman
    from ..human.human_compat_controls import _ankle_parents, _foot_value, apply
    from .asset_geometry import Creation
    from .creature_parts import apply_option

    if field in {"arm_mode", "leg_mode", "name", "player_arms",
                 "armor_ALL", "armor_HEAD", "armor_CHEST", "armor_LEGS", "armor_FEET"} and root.get("blockloom_parts_version"):
        apply_option(scene, root, rig, field, value)
        return

    if field in {"arm_mode", "leg_mode"}:
        key, modes = (
            ("blockloom_slim", ("WIDE", "SLIM"))
            if field == "arm_mode"
            else ("blockloom_official", ("NORMAL", "OFFICIAL"))
        )
        if value not in modes or key not in rig:
            raise ValueError("该生物资源不支持所选样式")
        require_property_edit(rig, f'["{key}"]')
        rig[key] = value == modes[1]
        rig.update_tag()
        return
    if field not in {"leg_fixed", "foot_fixed"} or type(value) is not bool:
        raise ValueError("未知的生物基础设置")
    target = LegacyHuman(rig, "", tuple(root.all_objects), (root,), True, "")
    if field == "foot_fixed":
        if _foot_value(target) == value:
            return
        if _ankle_parents(target) is None:
            raise ValueError("脚腕骨骼已修改，无法切换固定状态")
        # The provenance registry also references the Armature. Only another actual rig shares its bones.
        if any(o != rig and o.data == rig.data for o in bpy.data.objects):
            raise ValueError("脚腕骨架已由多个物体共用")
        _set_ankles(rig, value)
        return
    with Creation() as transaction:
        apply(transaction, scene, target, {field: value})
        transaction.finish_commit()


def edit(scene, options):
    from ..human.human import bind_rig_items

    ensure_mutation_allowed()
    roots = [
        c
        for c in scene.collection.children_recursive
        if c.get(INSTANCE) == options["instance"] and c.get(ASSET_ID) == CREATURE_ID
    ]
    if len(roots) != 1 or not owns(roots[0], roots[0]):
        raise ValueError("生物实例已移除或标识重复")
    root = roots[0]
    rig = root.get(RIG)
    if (
        not rig
        or rig.type != "ARMATURE"
        or tuple(rig.users_scene) != (scene,)
        or rig not in set(root.all_objects)
    ):
        raise ValueError("生物骨架不再属于此场景中的该实例")
    require_owned(root, (rig, rig.data))
    action = options["action"]
    if action == "CREATURE_FACE_REPAIR":
        if needs_upgrade(root):
            raise ValueError("请在生物资产库点击「更新所选生物资源」，以原生面控替换旧版附加表情")
        return {"message": "所选生物已使用原生面控"}
    if action == "CREATURE_UPGRADE":
        from .creature_upgrade import upgrade
        return upgrade(scene, root, rig, options)
    if action == "CREATURE_REMOVE":
        require_owned(root, (*root.children_recursive, *root.all_objects))
        proven = owned_ids(root)
        objects = {item for item in proven if isinstance(item, bpy.types.Object)}
        collections = {root, *(item for item in proven if isinstance(item, bpy.types.Collection))}
        if any(any(c not in collections for c in obj.users_collection) for obj in objects):
            raise ValueError("该生物有对象被其它合集共用")
        # Shared mesh/material data can survive independently. Only objects and
        # collections are deleted unconditionally after the registry is released.
        from .asset_collections import creature_parent
        require_no_external_users(scene, root, objects | collections,
                                  collection_parent=creature_parent(scene, root))
        captured = release_owned(root)
        bpy.data.batch_remove(ids=list(objects | collections))
        remove_unused_owned(captured)
        return {"message": "已移除生物实例"}
    if action in {"CREATURE_BIND", "CREATURE_UNBIND"}:
        bind_rig_items(scene, root, rig, options, unbind=action == "CREATURE_UNBIND")
    elif action == "CREATURE_STYLE":
        _style(scene, root, rig, options["field"], options["value"])
    else:
        raise ValueError("未知生物操作")
    return {"message": "已更新生物实例"}
