"""Human texture-pack mapping on private shader trees, using the shared pack store."""

import hashlib
import json
from pathlib import Path
from typing import Any

import bpy

from .human_state import OWNER

_ARMOR = {"leather", "leather_overlay", "chainmail", "copper", "diamond", "gold", "iron", "netherite", "turtle_scute"}
_TRIMS = {"bolt", "coast", "dune", "eye", "flow", "host", "raiser", "rib", "sentry", "shaper", "silence", "snout", "spire", "tide", "vex", "ward", "wayfinder", "wild"}


def digest_from_file(filename: str) -> str:
    from ..asset import assets

    p = Path(filename).resolve()
    if p.suffix.lower() == ".zip":
        assets.add_pack(str(p))
        return hashlib.sha256(p.read_bytes()).hexdigest()
    if p.name == "pack.json":
        data = json.loads(p.read_text(encoding="utf8"))
        digest = str(data.get("digest", ""))
        expected = assets.user_library() / "packs" / digest / "pack.json"
        if expected.resolve() == p and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            return digest
    raise ValueError("请选择 ZIP 材质包或库中材质包")


def pack_rows() -> list[Path]:
    from ..asset import assets

    return [assets.user_library() / "packs" / row["digest"] / "pack.json" for row in assets.packs()]


def apply_armor(root, bindings, filepath):
    """Publish armor textures and refresh their native features atomically."""
    from ...core.runtime import ensure_mutation_allowed
    from ...util.ownership import require_owned
    from ...util.resources import INSTANCE
    from ..asset.asset_geometry import Creation
    from ..enchantment import enchantment, enchantment_pixels
    from ..outline.outline_native import base_slots
    from .human import _png
    from .human_armor import (
        PACK_BACKUP,
        prepare_controls,
        refresh_features,
        suspend_features,
    )

    ensure_mutation_allowed()
    requested = {(obj, index) for obj, index, _slot in bindings}
    members = {obj for obj, _index in requested}
    bindings = tuple((obj, index, slot) for obj in members for index, slot in base_slots(root, obj)
                     if (obj, index) in requested and slot.material is not None and slot.material.node_tree is not None)
    require_owned(root, (obj for obj, _, _ in bindings))
    require_owned(root, (slot.material for _, _, slot in bindings))
    require_owned(root, (backup for obj, index, _ in bindings
                         if (backup := obj.get(PACK_BACKUP + str(index))) is not None))
    originals = {slot.material for _, _, slot in bindings}
    if not originals:
        raise ValueError("所选盔甲没有可替换材质")
    changed = []
    def restore():
        for obj, index, saved, material, previous in reversed(changed):
            obj.material_slots[index].material = material
            if previous is None:
                if saved in obj:
                    del obj[saved]
            else:
                obj[saved] = previous

    with Creation() as transaction:
        transaction.owner = root
        transaction.on_rollback(restore)
        suspend_features(transaction, root, members)
        bindings = tuple((obj, index, obj.material_slots[index]) for obj, index, _slot in bindings)
        owners = {info["owner"] for material in originals
                  if (info := enchantment.inspect(material)) is not None}
        prepare_controls(transaction, root, owners)
        if Path(filepath).suffix.lower() in {".zip", ".json"}:
            copies, created = prepare(originals, "armor", digest_from_file(filepath), root[INSTANCE])
        else:
            copies, created = prepare_png(originals, _png(filepath), root[INSTANCE], root=root, transaction=transaction)
        for item in created:
            transaction.track(item)
        for original, replacement in copies.items():
            if (replacement != original and enchantment.inspect(replacement) is not None
                    and enchantment_pixels.prepared(replacement) and enchantment_pixels.refresh(replacement)):
                enchantment.stamp(replacement)
        for obj, index, slot in bindings:
            saved = PACK_BACKUP + str(index)
            changed.append((obj, index, saved, slot.material, obj.get(saved)))
            obj[saved] = obj.get(saved, slot.material)
            slot.material = copies[slot.material]
        refresh_features(transaction, root, {obj for obj, _, _ in bindings})
        transaction.commit(root)


def prepare_png(originals, source, identity, *, root=None, transaction=None):
    """A private explicit PNG surface, retaining any validated native glint."""
    from ...util.resources import live_ids
    from ..enchantment import enchantment
    from .human_armor import apply_glint, glint_images

    originals = tuple(originals)
    inspections = {original: (enchantment.inspect_source(transaction, original) if transaction is not None
                              else enchantment.inspect(original)) for original in originals}
    if transaction is None and any(inspections.values()):
        raise ValueError("附魔盔甲PNG替换需要外层资源事务，未创建或修改数据")
    created, copies = [], {}
    class Tracker:
        def __getattr__(self, name):
            if transaction is None:
                raise AttributeError(name)
            return getattr(transaction, name)

        def track(self, item):
            item.use_fake_user = False
            item[OWNER] = identity
            created.append(item)
            return transaction.track(item) if transaction is not None else item
    tracker = Tracker()
    try:
        image = tracker.track(bpy.data.images.load(str(source), check_existing=False))
        if image.size[0] <= 0 or not image.has_data:
            raise ValueError("盔甲材质 PNG 无法解码")
        image.pack()
        for original in originals:
            info = inspections[original]
            if info:
                if root is None:
                    raise ValueError("附魔盔甲PNG替换需要明确的人模实例")
                glint_images(root, original, info["owner"])
            base = enchantment.remove(tracker, info["owner"], original) if info else original
            material = enchantment._copy_material(tracker, base)
            tree = material.node_tree
            texture = tree.nodes.new("ShaderNodeTexImage")
            texture.name, texture.image, texture.interpolation = "OXAL MC Studio · 自定义材质", image, "Closest"
            surface = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None) or tree.nodes.new("ShaderNodeBsdfPrincipled")
            output = next((n for n in tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None) or tree.nodes.new("ShaderNodeOutputMaterial")
            tree.links.new(texture.outputs["Color"], surface.inputs["Base Color"])
            tree.links.new(texture.outputs["Alpha"], surface.inputs["Alpha"])
            tree.links.new(surface.outputs["BSDF"], output.inputs["Surface"])
            if info:
                material = apply_glint(tracker, root, info["owner"], material,
                                       images_from=original)
            copies[original] = material
        # The outer Creation already owns these IDs and the Scene clock
        # rollback/exclusion journal. Do not track or free them a second time.
        return copies, [] if transaction is not None else created
    except Exception:
        if transaction is None:
            bpy.data.batch_remove(ids=live_ids(created))
        raise


def prepare(originals: set[Any], kind: str, digest: str, identity: str) -> tuple[dict[Any, Any], list[Any]]:
    from ..asset import assets

    if kind == "armor":
        created = []
        class Tracker:
            def track(self, item):
                item.use_fake_user = False
                item[OWNER] = identity
                created.append(item)
                return item
        try:
            result = {original: assets.replace_material_textures(Tracker(), original, original, digest, False, bpy.context.scene) for original in originals}
            if all(result[original] == original for original in originals):
                raise ValueError("材质包没有与所选盔甲匹配的原版贴图")
            return result, created
        except Exception:
            from ...util.resources import live_ids
            bpy.data.batch_remove(ids=live_ids(created))
            raise

    created: list[Any] = []
    materials: dict[Any, Any] = {}
    groups: dict[Any, Any] = {}
    images: dict[str, Any] = {}
    replacements = 0

    def track(item: Any) -> Any:
        item.use_fake_user = False
        item[OWNER] = identity
        created.append(item)
        return item

    def replacement(key: str) -> Any:
        nonlocal replacements
        source = assets.texture_pack_path(digest, key)
        if source is None or not source.is_file():
            return None
        if key not in images:
            image = track(bpy.data.images.load(str(source), check_existing=False))
            if image.size[0] <= 0 or not image.has_data:
                raise ValueError("材质包贴图无法解码：" + key)
            image.pack()
            images[key] = image
        replacements += 1
        return images[key]

    def visit(tree: Any, context: str) -> None:
        if "Leggings" in tree.name:
            context = "humanoid_leggings"
        elif "Humanoid" in tree.name:
            context = "humanoid"
        for node in tree.nodes:
            if node.type == "GROUP" and node.node_tree:
                original = node.node_tree
                cache_key = (original, context)
                if cache_key not in groups:
                    groups[cache_key] = track(original.copy())
                    visit(groups[cache_key], context)
                node.node_tree = groups[cache_key]
            elif node.type == "TEX_IMAGE" and node.image:
                stem = node.image.name.split(".png", 1)[0]
                if "_layer_" in stem:
                    stem, layer = stem.rsplit("_layer_", 1)
                    context = "humanoid_leggings" if layer == "2" else "humanoid"
                key = ""
                if kind == "armor" and stem in _ARMOR:
                    key = f"minecraft:entity/equipment/{context}/{stem}"
                elif kind == "armor" and stem in _TRIMS:
                    key = f"minecraft:trims/entity/{context}/{stem}"
                if key:
                    image = replacement(key)
                    if image:
                        node.image = image

    try:
        for original in originals:
            material = track(original.copy())
            materials[original] = material
            if kind == "armor":
                visit(material.node_tree, "humanoid")
            else:
                candidates = ("minecraft:entity/equipment/wings/elytra", "minecraft:entity/elytra") if kind == "elytra" else ("minecraft:entity/cape",)
                image = next((candidate for key in candidates if (candidate := replacement(key)) is not None), None)
                if image:
                    tree = material.node_tree
                    tex = tree.nodes.new("ShaderNodeTexImage")
                    tex.name, tex.image, tex.interpolation = "OXAL MC Studio · 材质包", image, "Closest"
                    principled = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None) or tree.nodes.new("ShaderNodeBsdfPrincipled")
                    output = next((n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"), None) or tree.nodes.new("ShaderNodeOutputMaterial")
                    tree.links.new(tex.outputs["Color"], principled.inputs["Base Color"])
                    tree.links.new(tex.outputs["Alpha"], principled.inputs["Alpha"])
                    tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
        if not replacements:
            raise ValueError("材质包没有与所选人模部位匹配的原版贴图")
        return materials, created
    except Exception:
        bpy.data.batch_remove(ids=list(reversed(created)))
        raise
