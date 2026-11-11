"""Native plant bounds and unobtrusive origins for ordinary world assets."""

import json
import math

from mathutils import Matrix

from ...util.animation import action_fcurves
from ...util.ownership import require_owned

_FIELDS = frozenset({"empty_display_type", "empty_display_size"})
FRAME = "oxal_mc_studio_world_block_centered_frame"
_PLANT = "oxal_mc_studio_native_plant_bounds_schema"
_SHIFT = Matrix.Translation((0, 0, -0.5))
_PLANT_TEMPLATES = frozenset({
    "minecraft:block/cross", "minecraft:block/tinted_cross", "minecraft:block/cross_emissive",
    "minecraft:block/crop", "minecraft:block/template_seagrass",
    *("minecraft:block/flowerbed_" + str(index) for index in range(1, 5)),
})


def configure(root):
    """Configure only a freshly created Empty, without adding helper objects."""
    if root.type == "EMPTY":
        root.empty_display_type = "CUBE"
        root.empty_display_size = 0.55


def _ordinary_world(root):
    return (root.type == "EMPTY" and root.get("oxal_mc_studio_geometry_kind") == "BLOCK"
            and root.get("oxal_mc_studio_presentation_context", "world") == "world")


def _base_marker(root):
    root.empty_display_type = "PLAIN_AXES"
    # Blender clamps a requested zero to this hard minimum. Use its actual
    # native point-sized marker instead of retaining a tiny cube or a big axis.
    root.empty_display_size = root.bl_rna.properties["empty_display_size"].hard_min


def _plain_item(root):
    """Recognize generated sprites without an authored controller hierarchy."""
    if (root.type != "EMPTY" or root.constraints
            or root.get("oxal_mc_studio_presentation_context", "world") != "world"
            or root.get("oxal_mc_studio_geometry_kind") not in {"ITEM", "BLOCK"}
            or root.get("oxal_mc_studio_control_rig") is not None):
        return False
    children = tuple(root.children_recursive)
    return bool(children) and all(obj.type == "MESH" and not obj.constraints
                                  and obj.get("oxal_mc_studio_pixel_topology")
                                  for obj in children)


def _plant_recipe(root):
    """Classify immutable model parent families, never translated display names."""
    from . import asset_catalog as catalog

    identifier = root.get("oxal_mc_studio_library_identifier", "")
    if identifier == "minecraft:cobweb":
        return False  # A non-plant deliberately shares the generic cross model.
    descriptors, _ = catalog.plan(identifier, state=json.loads(root.get("oxal_mc_studio_block_state", "{}")),
                                  shape=int(root.get("oxal_mc_studio_shape", 0)),
                                  special=bool(root.get("oxal_mc_studio_special", False)),
                                  complete=bool(root.get("oxal_mc_studio_complete_structure", True)))
    for descriptor in descriptors:
        model = descriptor.get("model")
        seen = set()
        while isinstance(model, str) and model not in seen and len(seen) < 64:
            if model in _PLANT_TEMPLATES:
                return True
            if model.startswith(("builtin/", "minecraft:builtin/")):
                break
            seen.add(model)
            model = catalog.read(catalog.resource(model, "models")).get("parent")
    return False


def prepare_base_frame(transaction, root):
    """Keep the source 0..1 frame and bottom origin; draw boxes on plants only."""
    if _plain_item(root):
        if root not in transaction.ids or any(obj not in transaction.ids for obj in root.children_recursive):
            raise ValueError("物品显示框缺少本次创建来源，未调整已有对象")
        previous = root.empty_display_type, root.empty_display_size

        def restore_item():
            root.empty_display_type, root.empty_display_size = previous

        transaction.on_rollback(restore_item)
        _base_marker(root)
        return True
    if (root.type != "EMPTY" or root.get("oxal_mc_studio_geometry_kind") != "BLOCK"
            or root.get("oxal_mc_studio_presentation_context", "world") != "world"):
        return False
    marker = root.get(FRAME)
    if marker == 2:
        return False
    if marker is not None:
        raise ValueError("方块坐标框版本未知，未重复调整")
    descendants = tuple(root.children_recursive)
    bodies = tuple(obj for obj in descendants if obj.type == "MESH"
                   and not obj.get("oxal_mc_studio_pixel_topology")
                   and not obj.get("oxal_mc_studio_effect_geometry")
                   and not obj.get("oxal_mc_studio_native_particle_kind")
                   and not obj.get("oxal_mc_studio_slot_internal_helper"))
    children = tuple(root.children)
    # Inventory icons and special sources with constraints or sub-rigs retain
    # their existing authored frame and controller display.
    if not bodies or any(obj.constraints or obj.type == "ARMATURE" for obj in descendants):
        return False
    if (root not in transaction.ids or any(child not in transaction.ids for child in children)
            or any(body not in transaction.ids for body in bodies)):
        raise ValueError("方块坐标框缺少本次创建来源，未调整已有对象")
    previous = root.empty_display_type, root.empty_display_size
    old_bounds = tuple((obj, obj.show_bounds, obj.display_bounds_type, obj.get(_PLANT)) for obj in bodies)

    def restore():
        root.empty_display_type, root.empty_display_size = previous
        for obj, enabled, kind, plant in old_bounds:
            obj.show_bounds, obj.display_bounds_type = enabled, kind
            if plant is None:
                if _PLANT in obj:
                    del obj[_PLANT]
            else:
                obj[_PLANT] = plant
        if FRAME in root:
            del root[FRAME]

    transaction.on_rollback(restore)
    _base_marker(root)
    if _plant_recipe(root):
        for obj in bodies:
            obj.show_bounds, obj.display_bounds_type = True, "BOX"
            obj[_PLANT] = 1
    root[FRAME] = 2
    return True


def _same_matrix(a, b):
    return all(abs(x - y) <= 1e-6 for row, other in zip(a, b, strict=True)
               for x, y in zip(row, other, strict=True))


def validate_legacy_frame(root, staged, mapping, animated_channels):
    """Accept only known old base/center frames, retaining transform curves."""
    if staged.get(FRAME) != 2 or root.get(FRAME) == 2:
        return
    if root.get(FRAME) not in (None, 1):
        raise ValueError("旧方块坐标框版本未知，未自动居中")
    for new in tuple(staged.children):
        old = mapping.get(new)
        if old is None:
            continue
        if old.parent != root or old.constraints:
            raise ValueError("旧方块子物体已有自定义父绑定或约束，未自动居中：" + old.name)
        expected_inverse = _SHIFT @ new.matrix_parent_inverse if root.get(FRAME) == 1 else new.matrix_parent_inverse
        if not _same_matrix(old.matrix_parent_inverse, expected_inverse):
            raise ValueError("旧方块子物体父逆矩阵已调整，未重复居中：" + old.name)
        if _same_matrix(old.matrix_basis, new.matrix_basis):
            continue
        animation = old.animation_data
        channels = animated_channels(old) | ({(curve.data_path, curve.array_index) for curve in animation.drivers}
                                             if animation is not None else set())
        rotation = ("rotation_quaternion" if old.rotation_mode == "QUATERNION" else
                    "rotation_axis_angle" if old.rotation_mode == "AXIS_ANGLE" else "rotation_euler")
        for field in ("location", "scale", rotation, "delta_location", "delta_scale",
                      "delta_rotation_euler", "delta_rotation_quaternion"):
            before, expected = getattr(old, field), getattr(new, field)
            for index, (actual, source) in enumerate(zip(before, expected, strict=True)):
                if abs(actual - source) > 1e-6 and (field, index) not in channels:
                    raise ValueError("旧方块子物体已有静态变换，可能已手工居中；请保留现状：" + old.name)


def _animated_display(root, fields=_FIELDS):
    animation = root.animation_data
    if animation is None:
        return False

    def strips(items):
        for strip in items:
            yield strip
            yield from strips(getattr(strip, "strips", ()))

    bindings = [animation, *(strip for track in animation.nla_tracks for strip in strips(track.strips))]
    return (any(curve.data_path in fields for curve in animation.drivers)
            or any(curve.data_path in fields for binding in bindings
                   for curve in action_fcurves(binding.action, slot=getattr(binding, "action_slot", None))))


def repair_legacy(transaction, root):
    """Hide known ordinary-asset root glyphs, preserving custom/animated UI."""
    require_owned(root, (root,))
    known = ((root.empty_display_type == "PLAIN_AXES"
              and math.isclose(root.empty_display_size, 0.15, rel_tol=0, abs_tol=1e-6))
             or (root.empty_display_type == "CUBE"
                 and math.isclose(root.empty_display_size, 0.55, rel_tol=0, abs_tol=1e-6)))
    if (not (_ordinary_world(root) or _plain_item(root)) or not known
            or _animated_display(root)):
        return False
    previous = root.empty_display_type, root.empty_display_size

    def restore():
        root.empty_display_type, root.empty_display_size = previous

    transaction.on_rollback(restore)
    _base_marker(root)
    return True


def publish_plant_bounds(transaction, root, staged, mapping):
    """Publish a new native plant box only onto untouched old display defaults."""
    if staged.get(FRAME) != 2:
        return
    for new, old in mapping.items():
        if (new.get(_PLANT) != 1 or old.type != "MESH" or _PLANT in old
                or old.show_bounds or old.display_bounds_type != "BOX"
                or _animated_display(old, frozenset({"show_bounds", "display_bounds_type"}))):
            continue
        require_owned(root, (old,))
        before = old.show_bounds, old.display_bounds_type

        def restore(obj=old, values=before):
            obj.show_bounds, obj.display_bounds_type = values

        transaction.on_rollback(restore)
        old.show_bounds, old.display_bounds_type = True, "BOX"
