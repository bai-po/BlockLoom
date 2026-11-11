"""Instance-owned enchantment hulls derived from the supplied reference.

asset_id: oxal.enchantment.outline; version: 3; policy: NEW_INSTANCE;
scope: one owned mesh; source: asset/outline/SOURCE.json. The small native
inverted hull follows the reference Solidify without CSG or attribute removal.
"""

import hashlib
import json
import math
from typing import Any

import bpy
from mathutils import Vector

from ...core.runtime import ensure_mutation_allowed
from ...util import drivers
from ...util.animation import action_fcurves
from ...util.node_layout import META as LAYOUT_META
from ...util.node_layout import is_layout_frame, original_ui_value
from ...util.ownership import require_owned
from . import enchantment

MARK = "oxal_mc_studio_enchant_outline_schema"
GEOMETRY_MARK = "oxal_mc_studio_enchant_geometry_schema"
RECORD = "oxal_mc_studio_enchant_modifiers"
OWNER = "oxal_mc_studio_enchant_controls"
ROLE = "oxal_mc_studio_enchant_role"
CONTRACT = "oxal_mc_studio_enchant_contract"
REFERENCES = "oxal_mc_studio_enchant_references"
MATERIAL_REFERENCES = "oxal_mc_studio_outline_material_references"
SOURCE_ATTRIBUTE = "oxal_enchantment_outline_source"
CELL_ATTRIBUTE = "oxal_enchantment_pixel_cell"


def plain(value):
    if isinstance(value, bpy.types.ID):
        return [value.id_type, value.name_full]
    if hasattr(value, "items"):
        return {str(key): plain(part) for key, part in value.items()}
    if isinstance(value, (str, int, bool, type(None))):
        return value
    if isinstance(value, float):
        return round(value, 8) if math.isfinite(value) else str(value)
    return [plain(part) for part in value]


def digest(value):
    return hashlib.sha256(json.dumps(plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _properties(owner, *, exclude=()):
    return [(prop.identifier, plain(original_ui_value(owner, prop.identifier, getattr(owner, prop.identifier))
                                   if isinstance(owner, bpy.types.Node) else getattr(owner, prop.identifier))) for prop in owner.bl_rna.properties
            if not prop.is_readonly and prop.type in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}
            and prop.identifier not in {"name", "label", "location", "width", "height", "select", "hide", "color", "use_custom_color",
                                        "is_active", "show_expanded", "active_index", "show_group_selector", *exclude}
            and not prop.identifier.startswith("bl_")]


def contract(tree, *, create=False, embedded=None, native=False, reference_owner=None, legacy_driver_paths=False):
    storage, reference_key = tree, REFERENCES
    owners = dict(embedded or {})
    if reference_owner is not None:
        if not isinstance(reference_owner, bpy.types.Material) or reference_owner.node_tree != tree:
            raise ValueError("描边引用表缺少精确Material拥有者，未按名称寻找或改写")
        owners[tree] = reference_owner
        if MATERIAL_REFERENCES in reference_owner and REFERENCES in tree:
            raise ValueError("描边材质存在两套引用表，未猜测或覆盖")
        if create and REFERENCES in tree:
            raise ValueError("旧描边材质引用表需要显式重建，未重签现有节点")
        if create or MATERIAL_REFERENCES in reference_owner:
            storage, reference_key = reference_owner, MATERIAL_REFERENCES
        # Historical embedded-tree records remain read-only digest inputs.
        # Only explicit rebuilding creates a new Material-owned reference table.
    references = {key: dict(value.items()) for key, value in storage.get(reference_key, {}).items()}
    def target(row):
        value = row["target"]
        return getattr(value, row["path"]) if row.get("path") else value
    def normalize(value):
        if isinstance(value, bpy.types.ID):
            for key, member in references.items():
                if target(member) == value:
                    return ["ID", key]
            if not create:
                return ["UNKNOWN_ID"]
            key = str(len(references))
            if value.is_embedded_data:
                owner = owners.get(value)
                if owner is None or owner.node_tree != value:
                    raise ValueError("嵌入节点引用缺少精确材质拥有者，未按名称接管")
                references[key] = {"target": owner, "path": "node_tree"}
            else:
                references[key] = {"target": value, "path": ""}
            return ["ID", key]
        if hasattr(value, "items"):
            return {str(key): normalize(part) for key, part in value.items()}
        if isinstance(value, (str, int, bool, float, type(None))):
            return plain(value)
        return [normalize(part) for part in value]
    curves = drivers.capture(tree)
    animated = {record["path"] for record in curves["curves"]}
    animation = tree.animation_data
    if animation and (animation.action is not None or animation.nla_tracks):
        channels = list(action_fcurves(animation.action, slot=getattr(animation, "action_slot", None)))
        action_ids = [animation.action] if animation.action else []
        strips = [strip for track in animation.nla_tracks for strip in track.strips]
        structure = []
        while strips:
            strip = strips.pop()
            channels.extend(action_fcurves(strip.action, slot=getattr(strip, "action_slot", None)))
            if strip.action:
                action_ids.append(strip.action)
            structure.append((_properties(strip), normalize(strip.action)))
            strips.extend(getattr(strip, "strips", ()))
        animated.update(curve.data_path for curve in channels)
        curves["actions"] = normalize((action_ids, structure,
            [(curve.data_path, curve.array_index, _properties(curve),
              [_properties(point) for point in curve.keyframe_points],
              [plain(point.co) for point in curve.sampled_points]) for curve in channels]))
    nodes = []
    from ..outline import outline_compat
    for node in tree.nodes:
        if is_layout_frame(node):
            continue
        values = [(index, normalize(socket.default_value)) for index, socket in enumerate((*tuple(node.inputs), *tuple(node.outputs)))
                  if hasattr(socket, "default_value") and socket.path_from_id("default_value") not in animated]
        scalar = _properties(node)
        if not native:
            scalar, values = outline_compat.node_record(node, scalar, values, animated)
            if node.bl_idname == "ShaderNodeLightPath":
                values = [(index, value) for index, value in values
                          if index < len(node.inputs) or not enchantment._legacy_light_path_output(
                              node, node.outputs[index - len(node.inputs)], index - len(node.inputs), animated, strict=False)]
        ramp = getattr(node, "color_ramp", None)
        structured = ((ramp.color_mode, ramp.hue_interpolation, ramp.interpolation,
                       [(part.position, tuple(part.color)) for part in ramp.elements]) if ramp is not None else None)
        nodes.append((node.name, node.bl_idname, scalar, values,
                      normalize(getattr(node, "image", None)), normalize(dict(node.items())), structured))
    custom = {key: value for key, value in tree.items() if key not in {CONTRACT, REFERENCES, LAYOUT_META, "oxal_mc_studio_generated_owner",
                                                                   "oxal_mc_studio_generated_instance", "oxal_mc_studio_owned_data_uid"}}
    interface = [(item.name, item.item_type, getattr(item, "identifier", ""),
                  _properties(item) if native else outline_compat.properties(item, _properties(item))) for item in tree.interface.items_tree]
    def identifier(node, socket, output):
        return socket.identifier if native else outline_compat.socket_identifier(node, socket.identifier, output=output)
    curve_record = outline_compat.legacy_driver_paths(tree, curves) if legacy_driver_paths else curves
    result = digest((nodes, [(link.from_node.name, identifier(link.from_node, link.from_socket, True),
                             link.to_node.name, identifier(link.to_node, link.to_socket, False)) for link in tree.links],
                     normalize(curve_record), normalize(custom), interface))
    if create:
        storage[reference_key] = references
    return result


def contract_matches(tree, *, reference_owner=None):
    """Verify stored bytes against canonical or exact historical native RNA."""
    expected = tree.get(CONTRACT)
    return (expected == contract(tree, reference_owner=reference_owner)
            or expected == contract(tree, native=True, reference_owner=reference_owner)
            or expected == contract(tree, reference_owner=reference_owner, legacy_driver_paths=True))


def _record(obj) -> dict[str, dict[str, Any]]:
    def clone(value) -> Any:
        if isinstance(value, bpy.types.ID):
            return value
        if hasattr(value, "items"):
            return {key: clone(part) for key, part in value.items()}
        if isinstance(value, (str, int, float, bool, type(None))):
            return value
        return [clone(part) for part in value]
    values = obj.get(RECORD)
    return clone(values) if values else {}


def _owned(root, items, transaction=None):
    require_owned(root, (item for item in items if transaction is None or item not in transaction.ids))


def _modifier_contract(modifier, *, native=False):
    if modifier.bl_rna.properties.get("properties") is None:
        storage = dict(modifier.items())
    else:
        storage = [_properties(modifier.properties), _properties(modifier.properties.inputs)]
    scalar = _properties(modifier)
    if not native:
        from ..outline.outline_compat import modifier_record
        scalar, storage = modifier_record(modifier, scalar, storage)
    return digest((scalar, storage))


def modifier_contract_matches(modifier, expected):
    return expected == _modifier_contract(modifier) or expected == _modifier_contract(modifier, native=True)


def _outline_position(obj, modifier=None):
    others = [value for value in obj.modifiers if value != modifier]
    deformation = {"ARMATURE", "LATTICE"}
    for index, value in enumerate(others):
        if value.type == "ARMATURE" and all(part.type in deformation for part in others[index:]):
            return index
    return len(others)


def _deformation_contract(obj, modifier=None):
    others = [value for value in obj.modifiers if value != modifier]
    tail = others[_outline_position(obj, modifier):]
    animation = obj.animation_data
    curves = list(animation.drivers) if animation else []
    if animation:
        curves.extend(action_fcurves(animation.action, slot=getattr(animation, "action_slot", None)))
        strips = [strip for track in animation.nla_tracks for strip in track.strips]
        while strips:
            strip = strips.pop()
            curves.extend(action_fcurves(strip.action, slot=getattr(strip, "action_slot", None)))
            strips.extend(getattr(strip, "strips", ()))
    animated = {curve.data_path for curve in curves}
    records, references = [], {}
    for index, part in enumerate(tail):
        values = [(key, ["animated"] if part.path_from_id(key) in animated else value) for key, value in _properties(part)]
        pointers = []
        for prop in part.bl_rna.properties:
            if prop.type != "POINTER" or prop.identifier == "rna_type":
                continue
            value = getattr(part, prop.identifier)
            if isinstance(value, bpy.types.ID):
                key = str(index) + ":" + prop.identifier
                references[key] = value
                pointers.append(key)
                data = getattr(value, "data", None)
                if isinstance(data, bpy.types.ID):
                    references[key + ":data"] = data
                    pointers.append(key + ":data")
        records.append((part.type, values, pointers))
    return digest(records), references


def _legacy_modifiers(root, obj, *, transaction=None):
    """Return only exact owned modifiers with intact native graph contracts."""
    table = _record(obj)
    result = []
    for role, row in table.items():
        modifier = obj.modifiers.get(row.get("name", ""))
        tree = row.get("group")
        if (modifier is None or modifier.type != "NODES" or modifier.node_group != tree
                or tree is None or tree.get(ROLE) != role or tree.get(OWNER) != row.get("owner")):
            raise ValueError("附魔几何修改器归属已改变，未覆盖：" + obj.name)
        _owned(root, (obj, tree, row["owner"]), transaction)
        if not contract_matches(tree):
            raise ValueError("附魔几何节点含用户改动，未覆盖：" + obj.name)
        if role == "outline" and tree.get(MARK) == 3:
            from ..outline.outline_native import validate_material
            validate_material(root, tree, transaction=transaction)
        if (modifier.show_viewport != row.get("show_viewport", True)
                or modifier.show_render != row.get("show_render", True)):
            raise ValueError("附魔修改器可见性被用户改变，未覆盖：" + obj.name)
        if not modifier_contract_matches(modifier, row.get("settings")):
            raise ValueError("附魔修改器参数含用户修改，未覆盖：" + obj.name)
        position = list(obj.modifiers).index(modifier)
        expected_outline = (_outline_position(obj, modifier) if row.get("placement") == "before_deformation"
                            else len(obj.modifiers) - 1)
        if position != row.get("position") or (role == "outline" and position != expected_outline) or (role == "geometry" and position != 0):
            raise ValueError("附魔修改器顺序被用户改变，未覆盖：" + obj.name)
        if role == "outline" and row.get("placement") == "before_deformation":
            signature, references = _deformation_contract(obj, modifier)
            saved = row.get("deform_references", {})
            if (signature != row.get("deform_contract") or set(references) != set(saved)
                    or any(saved[key] != value for key, value in references.items())):
                raise ValueError("附魔后置原生变形链含用户修改，未重新排列：" + obj.name)
            _owned(root, references.values(), transaction)
        if role == "geometry":
            from .enchantment_geometry import validate_data
            validate_data(root, obj, transaction=transaction)
        animation = tree.animation_data
        if animation and (animation.action is not None or animation.nla_tracks):
            raise ValueError("附魔生成节点包含独立用户动画，请在实例参数上控制")
        animation = obj.animation_data
        if animation:
            curves = list(animation.drivers)
            curves.extend(action_fcurves(animation.action, slot=getattr(animation, "action_slot", None)))
            strips = [strip for track in animation.nla_tracks for strip in track.strips]
            while strips:
                strip = strips.pop()
                curves.extend(action_fcurves(strip.action, slot=getattr(strip, "action_slot", None)))
                strips.extend(getattr(strip, "strips", ()))
            if any(curve.data_path.startswith(modifier.path_from_id() + ".") for curve in curves):
                raise ValueError("附魔修改器包含独立用户动画，未删除其通道")
        result.append(modifier)
    if any(modifier.type == "NODES" and modifier.node_group is not None
           and (modifier.node_group.get(MARK) or modifier.node_group.get(GEOMETRY_MARK))
           and modifier not in result for modifier in obj.modifiers):
        raise ValueError("发现未登记的附魔修改器，未接管：" + obj.name)
    return tuple(result)


def _snapshot(transaction, obj):
    existed, before = RECORD in obj, _record(obj)
    def restore():
        if existed:
            obj[RECORD] = before
        elif RECORD in obj:
            del obj[RECORD]
    transaction.on_rollback(restore)
    return {key: dict(row) for key, row in before.items()}


def _prove(transaction, root, obj, owner):
    for item in (obj, owner):
        if item not in transaction.ids:
            require_owned(root, (item,))


class Graph:
    def __init__(self, transaction, owner, role):
        self.owner = owner
        self.embedded_owners = {}
        self.tree = transaction.track(bpy.data.node_groups.new("附魔 · " + role, "GeometryNodeTree"))
        self.tree[ROLE], self.tree[OWNER] = role, owner
        self.tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        self.tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        self.entry = self.node("NodeGroupInput", "Input")
        self.output = self.node("NodeGroupOutput", "Output")

    def node(self, kind, name):
        value = self.tree.nodes.new(kind)
        value.name = name
        return value

    def feed(self, socket, value):
        if isinstance(value, (int, float, bool, tuple, list, Vector)):
            socket.default_value = value
        else:
            self.tree.links.new(value, socket)

    def math(self, operation, a, b: Any = 0, name="Math"):
        node = self.node("ShaderNodeMath", name)
        node.operation = operation
        self.feed(node.inputs[0], a)
        self.feed(node.inputs[1], b)
        return node.outputs[0]

    def vector(self, operation, a, b=None, name="Vector"):
        node = self.node("ShaderNodeVectorMath", name)
        node.operation = operation
        self.feed(node.inputs[0], a)
        if b is not None:
            self.feed(node.inputs["Scale"] if operation == "SCALE" else node.inputs[1], b)
        return node.outputs["Value"] if operation in {"DOT_PRODUCT", "DISTANCE", "LENGTH"} else node.outputs[0]

    def value(self, key, expression="value"):
        node = self.node("ShaderNodeValue", key)
        enchantment._driver(node.outputs[0], self.owner, key, expression=expression)
        return node.outputs[0]

    def store(self, geometry, name, data_type, domain, value, selection=True):
        node = self.node("GeometryNodeStoreNamedAttribute", name)
        node.data_type, node.domain = data_type, domain
        node.inputs["Name"].default_value = name
        self.feed(node.inputs["Geometry"], geometry)
        self.feed(node.inputs["Value"], value)
        self.feed(node.inputs["Selection"], selection)
        return node.outputs["Geometry"]

    def attribute(self, name, datatype):
        node = self.node("GeometryNodeInputNamedAttribute", name)
        node.data_type = datatype
        node.inputs["Name"].default_value = name
        return node.outputs["Attribute"]

    def finish(self, geometry, marker):
        self.tree.links.new(geometry, self.output.inputs["Geometry"])
        self.tree[marker] = 2 if marker == MARK else 1
        self.tree[CONTRACT] = contract(self.tree, create=True, embedded=self.embedded_owners)
        return self.tree




def publish(transaction, obj, owner, role, tree, *, first=False, relief=False):
    table = _snapshot(transaction, obj)
    previous = table.get(role)
    modifier = obj.modifiers.get(previous["name"]) if previous else None
    if modifier is None:
        modifier = obj.modifiers.new("附魔 · " + role, "NODES")
        transaction.on_rollback(lambda obj=obj, modifier=modifier: obj.modifiers.remove(modifier))
    else:
        before = modifier.node_group
        transaction.on_rollback(lambda modifier=modifier, before=before: setattr(modifier, "node_group", before))
    modifier.node_group = tree
    index = list(obj.modifiers).index(modifier)
    target = 0 if first else len(obj.modifiers) - 1
    if index != target:
        obj.modifiers.move(index, target)
        transaction.on_rollback(lambda obj=obj, modifier=modifier, index=index: obj.modifiers.move(list(obj.modifiers).index(modifier), index))
    table[role] = {"name": modifier.name, "group": tree, "owner": owner, "show_viewport": True, "show_render": True,
                   "relief": relief, "settings": _modifier_contract(modifier), "position": target,
                   "placement": "after_deformation" if role == "outline" else "first"}
    if role == "outline" and tree.get(MARK) != 3:
        signature, references = _deformation_contract(obj, modifier)
        table[role]["deform_contract"] = signature
        table[role]["deform_references"] = references
    obj[RECORD] = table


def _outline_animated(owner):
    animation = owner.animation_data
    if animation is None:
        return False
    path = '["glint_outline_enabled"]'
    curves = list(animation.drivers)
    curves.extend(action_fcurves(animation.action, slot=getattr(animation, "action_slot", None)))
    strips = [strip for track in animation.nla_tracks for strip in track.strips]
    while strips:
        strip = strips.pop()
        curves.extend(action_fcurves(strip.action, slot=getattr(strip, "action_slot", None)))
        strips.extend(getattr(strip, "strips", ()))
    return any(curve.data_path == path for curve in curves)


def apply(transaction, root, obj, owner, *, prepare_geometry=False, prepare_outline=False):
    ensure_mutation_allowed()
    if obj.type != "MESH":
        return
    from ..outline import outline_native
    from . import enchantment_geometry
    from .enchantment_targets import weapon

    _prove(transaction, root, obj, owner)
    # Preparation is explicit. Fresh disabled glint does not need a private
    # shell or an alpha copy of the complete armor shader. Existing shells and
    # animated enable channels retain their ready native geometry and drivers.
    wants_outline = (prepare_outline or outline_prepared(obj)
                     or obj in getattr(transaction, "enchantment_outline_ready", ())
                     or bool(owner.get("glint_outline_enabled")) or _outline_animated(owner))
    # Native apply validates its complete existing state before reuse/build.
    # Keep an earlier validation only for legacy/geometry mutation or a no-op
    # path that does not enter native apply at all.
    if not wants_outline or _record(obj) or prepare_geometry:
        generated_modifiers(root, obj, transaction=transaction)
    profiles = {slot.material.get(enchantment._PROFILE)
                for _, slot in outline_native.base_slots(root, obj, transaction=transaction)
                if slot.material is not None}
    is_armor = "ARMOR" in profiles
    if prepare_geometry and is_armor:
        raise ValueError("盔甲请使用原版3D和厚度设置")
    suspended = getattr(transaction, "enchantment_relief_ready", {})
    had_relief = (bool(_record(obj).get("geometry", {}).get("relief"))
                  or suspended.get(obj) == (root, owner))
    if _record(obj) or prepare_geometry:
        # Retiring a legacy/relief modifier changes the following native
        # modifier positions. Only this real structural change needs a hull
        # suspension; an already current native-only shell stays untouched.
        outline_native.remove(transaction, root, obj, role="enchant")
    _remove_legacy(transaction, root, obj)
    relief = not is_armor and (prepare_geometry or had_relief)
    contour = wants_outline and not is_armor and (enchantment_geometry.needs_weapon_contour(obj, transaction=transaction) if weapon(root)
                               else enchantment_geometry.needs_contour(obj, transaction=transaction))
    if relief or contour:
        enchantment_geometry.prepare(transaction, root, obj, owner, relief=relief,
                                     outline_proxy=relief and wants_outline)
    if not wants_outline:
        obj.update_tag()
        return
    suppression = disable_when = None
    if is_armor:
        from ..human.human import parameter_owner
        from ..human.human_armor import part_of
        from ..human.human_armor_outline import prepared as armor_prepared
        from ..human.human_state import key

        part = part_of(obj)
        if part:
            controller = parameter_owner(root)
            disable_when = (controller, key(part + "_3d"))
            if armor_prepared(root, (part,)):
                suppression = (controller, key(part + "_outline_follow_human"),
                               key("outline_enabled"), key(part + "_outline_enabled"))
    outline_native.apply(transaction, root, obj, owner,
                         enabled="glint_outline_enabled", width="glint_outline_width",
                         color="glint_outline_color", brightness="glint_outline_brightness",
                         quality_normals="glint_outline_quality_normals", disable_when=disable_when,
                         gate="glint_enabled", role="enchant",
                         suppression=suppression,
                         source_attribute=None)
    obj.update_tag()


def strip_relief(transaction, root, obj, owner):
    """Explicitly retire only the recorded Beta pixel relief, never native 3D.

    Callers select ITEM targets. Native armor/Block thickness and unrelated
    modifiers do not use this exact record and are left in their original chain.
    Alpha contour preparation may be recreated without relief after retirement.
    """
    ensure_mutation_allowed()
    generated_modifiers(root, obj, transaction=transaction)
    if not _record(obj).get("geometry", {}).get("relief"):
        return False
    remove(transaction, root, obj)
    apply(transaction, root, obj, owner, prepare_geometry=False)
    return True


def refresh(transaction, root, obj, owner):
    from ..outline.outline_native import prepared as native_prepared

    if obj.get(RECORD) or native_prepared(obj, role="enchant"):
        apply(transaction, root, obj, owner)


def _remove_legacy(transaction, root, obj):
    ensure_mutation_allowed()
    modifiers = _legacy_modifiers(root, obj, transaction=transaction)
    if not modifiers:
        return
    from .enchantment_geometry import remove_data
    table = _snapshot(transaction, obj)
    remove_data(transaction, root, obj)
    for modifier in modifiers:
        index, group, name = list(obj.modifiers).index(modifier), modifier.node_group, modifier.name
        values = {prop.identifier: getattr(modifier, prop.identifier) for prop in modifier.bl_rna.properties
                  if not prop.is_readonly and prop.type in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}
                  and prop.identifier not in {"name", "type"}}
        def restore(obj=obj, index=index, group=group, name=name, values=values):
            restored = obj.modifiers.new(name, "NODES")
            restored.node_group = group
            for key, value in values.items():setattr(restored, key, value)
            obj.modifiers.move(len(obj.modifiers) - 1, min(index, len(obj.modifiers) - 1))
        transaction.on_rollback(restore)
        obj.modifiers.remove(modifier)
    for role in table:
        _owned(root, (table[role]["group"],), transaction)
    del obj[RECORD]
    obj.update_tag()


def prepared(root, owner=None):
    """Read-only UI metadata; no graph hash, evaluation or source IO."""
    if owner is None:
        return bool(_record(root).get("geometry", {}).get("relief"))
    objects = tuple(root.all_objects) if isinstance(root, bpy.types.Collection) else (root, *tuple(root.children_recursive))
    records = [_record(obj) for obj in objects if obj.type == "MESH"]
    records = [table for table in records if any(row.get("owner") == owner for row in table.values())]
    return bool(records) and all(table.get("geometry", {}).get("relief") for table in records)


def outline_prepared(obj):
    """UI readiness only; no shader hash, dependency evaluation or source IO."""
    from ..outline.outline_native import prepared as native_prepared

    return native_prepared(obj, role="enchant") or bool(_record(obj).get("outline"))


def generated_modifiers(root, obj, *, transaction=None):
    from ..outline.outline_native import validate

    return (*_legacy_modifiers(root, obj, transaction=transaction),
            *validate(root, obj, transaction=transaction, role="enchant"))


def suspend(transaction, root, obj):
    """Retain proven native readiness during one explicit material operation.

    The actual Object/root/controller references scope this temporary snapshot.
    Enable values and their Action/NLA bindings remain on the existing owner;
    restoring readiness does not enable a currently disabled relief channel.
    """
    generated_modifiers(root, obj, transaction=transaction)
    row = _record(obj).get("geometry", {})
    if row.get("relief"):
        ready = getattr(transaction, "enchantment_relief_ready", {})
        transaction.enchantment_relief_ready = ready
        ready[obj] = (root, row["owner"])
    remove(transaction, root, obj)


def remove(transaction, root, obj):
    from ..outline.outline_native import remove as native_remove

    # Material/part tools suspend generated features before replacing inputs.
    # Preserve readiness only in this explicit transaction, including a shell
    # whose native enable channel is currently False. An un-enchant operation
    # never calls apply afterward, so it still removes the shell completely.
    if outline_prepared(obj):
        ready = getattr(transaction, "enchantment_outline_ready", set())
        transaction.enchantment_outline_ready = ready
        ready.add(obj)
    native_remove(transaction, root, obj, role="enchant")
    _remove_legacy(transaction, root, obj)
