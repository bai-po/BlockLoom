"""Read-only retirement proof against this operation's native armor template.

asset_id: oxal.human.armor_contract; version: 1; policy: NEW_INSTANCE;
scope: one precisely owned human. Source: the instance's versioned armor blend.
No current-state signature is published or accepted as an immutable baseline.
"""

import bpy

from ...util import drivers
from ...util.geometry_nodes import _value_socket, get_input, input_path
from ...util.ownership import owned_ids, require_owned

_MODIFIER_UI = {"is_active", "show_expanded", "show_group_selector", "open_output_attributes_panel",
                "open_manage_panel", "open_bake_panel", "open_named_attributes_panel",
                "open_bake_data_blocks_panel", "open_warnings_panel"}
_NODE_UI = {"label", "location", "location_absolute", "width", "height", "dimensions",
            "select", "hide", "parent", "color", "use_custom_color"}
_CURVE_UI = {"select", "select_control_point", "select_left_handle", "select_right_handle", "hide"}
_CONTAINERS = {"targets", "capture_items", "enum_items", "index_switch_items", "repeat_items",
               "simulation_items", "bake_items", "curves", "points", "elements",
               "image_user", "texture_mapping", "color_mapping", "curve_mapping"}


class NativeArmorContract:
    """Temporary exact comparison; actual ID references never become names alone."""

    def __init__(self, root, references=()):
        from .human import parameter_owner, rig_of

        self.root = root
        self.rig = rig_of(root)
        self.controls = parameter_owner(root)
        # Reference objects come from the caller's unpublished, versioned
        # AppendTransaction. Their authored rig is older than the current
        # body's rig in some released profiles; map this actual source role,
        # never an arbitrary same-named Armature in the user's file.
        self.reference_rigs = {value for obj in references for value in (
            obj.parent, *(getattr(modifier, "object", None) for modifier in obj.modifiers))
            if isinstance(value, bpy.types.Object) and value.type == "ARMATURE"}
        self.trees = {}
        self.checked = {root, self.rig, self.rig.data, self.controls}
        self.object_roles = {}
        from .human_armor import dependency_identity

        for item in owned_ids(root):
            if isinstance(item, bpy.types.Object):
                self.object_roles.setdefault((item.type, dependency_identity(item)), []).append(item)
        require_owned(root, (root, self.rig, self.rig.data, self.controls))

    def _owned(self, value):
        if value not in self.checked:
            require_owned(self.root, (value,))
            self.checked.add(value)

    def _id(self, value, actual):
        from .human import source_name
        from .human_armor import dependency_identity

        if actual:
            self._owned(value)
        if value == self.controls:
            return ("CONTROLS",)
        if isinstance(value, bpy.types.Object) and value.type == "ARMATURE":
            if actual and value != self.rig:
                raise ValueError("盔甲绑定不是此实例的实际主骨架，未移除")
            if not actual and value not in self.reference_rigs and value != self.rig:
                raise ValueError("盔甲源骨架角色不匹配，未移除")
            return ("MAIN_RIG",)
        if isinstance(value, bpy.types.Armature):
            if actual and value != self.rig.data:
                raise ValueError("盔甲驱动不是此实例的实际骨架数据，未移除")
            if not actual and value not in {rig.data for rig in self.reference_rigs} and value != self.rig.data:
                raise ValueError("盔甲源骨架数据角色不匹配，未移除")
            return ("MAIN_ARMATURE",)
        if (actual and isinstance(value, bpy.types.Object)
                and self.object_roles.get((value.type, dependency_identity(value))) != [value]):
            raise ValueError("盔甲原生依赖的实际来源引用不唯一，未按名称接管")
        return (value.id_type, dependency_identity(value) if isinstance(value, bpy.types.Object) else source_name(value))

    def _value(self, value, actual):
        if isinstance(value, bpy.types.ID):
            return self._id(value, actual)
        if hasattr(value, "items"):
            return {key: self._value(part, actual) for key, part in value.items() if key not in _CURVE_UI}
        if isinstance(value, (str, bool, int, type(None))):
            return value
        if isinstance(value, float):
            return round(value, 7)
        return tuple(self._value(part, actual) for part in value)

    def _rna(self, owner, actual, *, exclude=(), driven=()):
        result = {}
        for prop in owner.bl_rna.properties:
            name = prop.identifier
            if ((prop.is_readonly and name not in _CONTAINERS) or name in {"rna_type", *exclude}
                    or (hasattr(owner, "path_from_id") and owner.path_from_id(name) in driven)):
                continue
            value = getattr(owner, name)
            if prop.type in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}:
                result[name] = self._value(value, actual)
            elif prop.type == "POINTER":
                if value is None or isinstance(value, bpy.types.ID):
                    result[name] = self._value(value, actual)
                elif isinstance(value, bpy.types.bpy_struct):
                    result[name] = self._rna(value, actual)
            elif prop.type == "COLLECTION":
                result[name] = tuple(self._rna(item, actual) for item in value)
        return result

    def _tree(self, tree, actual, seen=frozenset()):
        if tree in seen:
            raise ValueError("盔甲原生几何节点循环引用，未移除")
        cache = (tree, actual)
        if cache in self.trees:
            return self.trees[cache]
        if actual:
            self._owned(tree)
        animation = tree.animation_data
        if animation and (animation.action or animation.nla_tracks):
            raise ValueError("盔甲原生几何节点含独立Action/NLA，未移除")
        captured = drivers.capture(tree)
        driven = {curve["path"] for curve in captured["curves"]}
        nodes = []
        for node in tree.nodes:
            values = []
            for direction, sockets in (("IN", node.inputs), ("OUT", node.outputs)):
                for socket in sockets:
                    if hasattr(socket, "default_value"):
                        value = ("DRIVEN",) if socket.path_from_id("default_value") in driven else self._value(socket.default_value, actual)
                        values.append((direction, socket.identifier, value))
            child = getattr(node, "node_tree", None)
            nodes.append((node.name, node.bl_idname,
                          self._rna(node, actual, exclude=(*_NODE_UI, "node_tree"), driven=driven), values,
                          self._tree(child, actual, seen | {tree}) if child is not None else None))
        interface = [(item.item_type, self._rna(item, actual, exclude=("parent", "description")))
                     for item in tree.interface.items_tree]
        result = (nodes, interface, [(link.from_node.name, link.from_socket.identifier,
                           link.to_node.name, link.to_socket.identifier) for link in tree.links],
                  self._value(captured["curves"], actual))
        self.trees[cache] = result
        return result

    def _inputs(self, modifier, actual, driven):
        result = []
        for socket in modifier.node_group.interface.items_tree:
            if (getattr(socket, "in_out", "") != "INPUT"
                    or socket.bl_rna.properties.get("default_value") is None):
                continue
            field = _value_socket(modifier, socket.identifier)
            mode = (self._rna(field, actual, exclude=("value",)) if field is not None else
                    (modifier.get(socket.identifier + "_use_attribute"), modifier.get(socket.identifier + "_attribute_name")))
            value = get_input(modifier, socket.identifier)
            if input_path(modifier, socket.identifier) in driven:
                value = ("DRIVEN",)
            else:
                value = self._value(value, actual)
            result.append((socket.identifier, value, mode))
        return result

    def _snapshot(self, obj, actual):
        from .human_armor import generated_native_modifiers

        generated = generated_native_modifiers(self.root, obj) if actual else ()
        captured = drivers.capture(obj)
        if actual:
            from ..outline.outline_native import generated_driver_paths

            paths = generated_driver_paths(self.root, obj)
            captured["curves"] = [curve for curve in captured["curves"] if curve["path"] not in paths]
        driven = {curve["path"] for curve in captured["curves"]}
        modifiers = []
        for modifier in obj.modifiers:
            if modifier in generated:
                continue
            scalar = self._rna(modifier, actual, exclude=(*_MODIFIER_UI, "node_group", "properties"), driven=driven)
            modifiers.append((modifier.name, modifier.type, scalar,
                              self._inputs(modifier, actual, driven) if modifier.type == "NODES" else None,
                              self._tree(modifier.node_group, actual) if modifier.type == "NODES" and modifier.node_group else None))
        return {
            "modifiers": modifiers,
            "constraints": [(constraint.name, constraint.type, self._rna(constraint, actual, exclude=("show_expanded",), driven=driven)) for constraint in obj.constraints],
            "drivers": self._value(captured["curves"], actual),
            # Native parenting decomposes Euler matrices on publication. Its
            # sub-microunit roundoff is not a new authored transform.
            "binding": (self._value(obj.parent, actual), obj.parent_type, obj.parent_bone,
                        tuple(tuple(round(value, 5) for value in row) for row in obj.matrix_parent_inverse),
                        tuple(tuple(round(value, 5) for value in row) for row in obj.matrix_basis)),
            "key_drivers": self._value(drivers.capture(obj.data.shape_keys)["curves"], actual) if obj.data.shape_keys else (),
        }

    def validate(self, obj, reference):
        from .human import _prepare_controls

        if reference is None:
            raise ValueError("盔甲缺少同版本不可变原生模板，未移除：" + obj.name)
        # Only this operation's unpublished library reference is prepared. The
        # actual object is never rewritten or signed to make a mismatch pass.
        _prepare_controls(self.root, self.rig, objects=(reference,))
        expected, actual = self._snapshot(reference, False), self._snapshot(obj, True)
        changed = [name for name in expected if expected[name] != actual[name]]
        if changed:
            raise ValueError("盔甲原生修改器／约束／绑定／驱动含用户修改，未移除：" + obj.name + "（" + "、".join(changed) + "）")
