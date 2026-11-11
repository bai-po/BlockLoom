"""Explicit, asset-owned native bow/crossbow SLOT presentations.

Each phase is prepared once by the import transaction. The native property
chooses geometry; playback never dispatches an event or builds a model.
"""

import math

import bpy
from mathutils import Matrix

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import require_owned
from . import asset_catalog as catalog
from .asset_effects import native
from .asset_slot_geometry import apply

PROPERTY = "持物阶段"
SCHEMA = "oxal_mc_studio_slot_phase_schema"
LABELS = ("待机", "拉动一", "拉动二", "拉满", "装填箭", "装填烟花")
ITEMS = tuple((str(i), label, "", i) for i, label in enumerate(LABELS))


def supported(identifier):
    return identifier in {"minecraft:bow", "minecraft:crossbow"}


def _select(transaction, obj, root, phase):
    group = transaction.track(bpy.data.node_groups.new("持物阶段 · " + LABELS[phase], "GeometryNodeTree"))
    group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    source = group.nodes.new("NodeGroupInput")
    output = group.nodes.new("NodeGroupOutput")
    switch = group.nodes.new("GeometryNodeSwitch")
    switch.input_type = "GEOMETRY"
    group.links.new(source.outputs["Geometry"], switch.inputs["True"])
    group.links.new(switch.outputs["Output"], output.inputs["Geometry"])
    native(switch.inputs["Switch"], "default_value", f"floor(phase+.5)=={phase}", root, [("phase", PROPERTY)])
    modifier = obj.modifiers.new("原生持物阶段", "NODES")
    modifier.node_group = group
    obj["oxal_mc_studio_model_part"] = f"库存 · 持物阶段 {phase} · " + str(obj.get("oxal_mc_studio_model_part", "模型"))
    obj["oxal_mc_studio_slot_phase"] = phase


def build(transaction, scene, collection, root, pivot, identifier, context, options, representation):
    if not supported(identifier) or options.get("_slot_phase_variant"):
        return
    short = identifier.split(":")[-1]
    side = "LEFT" if context.endswith("lefthand") else "RIGHT"
    declarations = catalog.read(catalog.ROOT / "slot_use_states196.json")["states"][short][side]
    root[PROPERTY] = 0
    root.id_properties_ui(PROPERTY).update(min=0, max=len(declarations)-1, description="资产原生阶段，可右键插入关键帧；不运行战斗或发射事件")
    root[SCHEMA] = 1
    groups = [(pivot, 0)]
    for declaration in declarations[1:]:
        phase = int(declaration["index"])
        variant = representation(transaction, scene, collection, root, identifier, context,
                                 {**options, "_slot_phase_variant": True,
                                  "_descriptors": [{"type": "minecraft:model", "model": declaration["model"]}]}, "HELD")
        variant.parent = pivot
        variant.matrix_basis = Matrix.Identity(4)
        variant["oxal_mc_studio_model_part"] = "库存 · 持物形态 · " + LABELS[phase]
        apply(transaction, variant, Matrix(declaration["relative_rest_matrix"]))
        groups.append((variant, phase))
    for group, phase in groups:
        for obj in tuple(group.children):
            if obj.type == "MESH":
                _select(transaction, obj, root, phase)
        if group != pivot:
            # The relative phase was already baked into private vertices;
            # an identity scene Empty no longer serves a native transform.
            for obj in tuple(group.children):
                basis, inverse = obj.matrix_basis.copy(), obj.matrix_parent_inverse.copy()
                obj.parent = pivot
                obj.matrix_parent_inverse, obj.matrix_basis = inverse, basis
            transaction.forget(group)
            bpy.data.objects.remove(group, do_unlink=True)


def set_phase(scene, root, value):
    del scene
    ensure_mutation_allowed()
    from .asset_contracts import validate
    from .assets import IDENTIFIER
    if not supported(root.get(IDENTIFIER)) or root.get(SCHEMA) != 1:
        raise ValueError("当前实例没有原生物品栏持物阶段，请显式重建该槽位资产")
    require_owned(root, (root,))
    validate(root)
    maximum = 3 if root[IDENTIFIER] == "minecraft:bow" else 5
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value) or not 0 <= value <= maximum:
        raise ValueError("所选持物阶段不在当前资产的有效形态中")
    from ...util.animation_edit import require_property_edit
    require_property_edit(root, '["' + PROPERTY + '"]')
    root[PROPERTY] = int(value)
    root.update_tag()
    return {"message": "持物阶段：" + LABELS[int(value)]}


def upgrade_pairs(root, mapping, retired, fresh):
    """Pair only the unique legacy resting held mesh after contract validation."""
    if root.get(SCHEMA) == 1 or not supported(root.get("oxal_mc_studio_library_identifier")):
        return
    if root.get("oxal_mc_studio_presentation_context") != "slot":
        return
    if PROPERTY in root:
        raise ValueError("旧实例含用户同名持物阶段属性，未覆盖")
    pivot = root.get("oxal_inventory_held")
    if pivot is None:
        return
    previous = [obj for obj in retired if obj.type == "MESH" and obj in tuple(pivot.children_recursive)
                and not obj.get("oxal_mc_studio_effect_geometry")]
    following = [obj for obj in fresh if obj.type == "MESH" and obj.get("oxal_mc_studio_slot_phase") == 0]
    if len(previous) != 1 or len(following) != 1:
        raise ValueError("旧持物待机模型无法唯一对应，未覆盖")
    old, new = previous[0], following[0]
    require_owned(root, (pivot, old, old.data))
    mapping[new] = old
    retired.remove(old)
    fresh.remove(new)


def bridge_existing(transaction, old, new):
    """Add the new phase selector while retaining an existing object's modifiers."""
    if new.get("oxal_mc_studio_slot_phase") != 0 or "oxal_mc_studio_slot_phase" in old:
        return
    selectors = [modifier for modifier in new.modifiers if modifier.type == "NODES" and modifier.name == "原生持物阶段"]
    if len(selectors) != 1 or any(modifier.name == "原生持物阶段" for modifier in old.modifiers):
        raise ValueError("原生持物阶段修改器冲突，未覆盖")
    modifier = old.modifiers.new("原生持物阶段", "NODES")
    def rollback():
        old.modifiers.remove(modifier)
    transaction.on_rollback(rollback)
    modifier.node_group = selectors[0].node_group
