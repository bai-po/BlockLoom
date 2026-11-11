"""Address Geometry Nodes modifier inputs through the available native RNA.

Current modifiers expose typed ``properties.inputs.<identifier>.value`` fields.
Earlier modifiers store these values as ID properties. Socket identifiers come
from the node group's interface, never from display names or version numbers.
"""

from typing import Any, cast

import bpy
from bpy.types import NodesModifier, NodeTreeInterfaceSocket

from ..core.runtime import ensure_mutation_allowed


def _value_socket(modifier: NodesModifier, identifier: str) -> Any:
    if modifier.type != "NODES" or modifier.node_group is None:
        raise ValueError("几何节点修改器缺少节点组")
    interface = modifier.node_group.interface
    if interface is None:
        raise ValueError("几何节点组缺少输入接口")
    interface_socket = next(
        (
            cast(NodeTreeInterfaceSocket, item)
            for item in interface.items_tree
            if item.item_type == "SOCKET"
            and cast(NodeTreeInterfaceSocket, item).in_out == "INPUT"
            and cast(NodeTreeInterfaceSocket, item).identifier == identifier
        ),
        None,
    )
    if interface_socket is None:
        raise ValueError(f"几何节点组不存在指定输入：{identifier}")
    if interface_socket.bl_rna.properties.get("default_value") is None:
        raise ValueError(f"几何节点输入不提供可读写值：{identifier}")
    properties = modifier.bl_rna.properties.get("properties")
    if properties is None:
        return None
    settings = cast(Any, modifier).properties
    inputs = getattr(settings, "inputs", None)
    socket = getattr(inputs, identifier, None)
    if socket is None or socket.bl_rna.properties.get("value") is None:
        raise ValueError(f"几何节点输入不提供可读写值：{identifier}")
    return socket


def get_input(modifier: NodesModifier, identifier: str) -> Any:
    """Read a scalar/ID value, or an independent tuple for an array input."""
    socket = _value_socket(modifier, identifier)
    if socket is not None:
        value = socket.value
        prop = socket.bl_rna.properties["value"]
        return tuple(value) if getattr(prop, "is_array", False) else value
    value = modifier[identifier]
    return tuple(value.to_list()) if hasattr(value, "to_list") else value


def set_input(modifier: NodesModifier, identifier: str, value: Any) -> None:
    """Set the input value without changing its constant/attribute field mode."""
    ensure_mutation_allowed()
    owner = modifier.id_data
    if not isinstance(owner, bpy.types.Object):
        raise TypeError("几何节点修改器缺少所属对象")
    socket = _value_socket(modifier, identifier)
    if socket is not None:
        socket.value = value
    else:
        modifier[identifier] = value
    # Direct legacy ID-property writes do not emit a modifier RNA update.
    # Invalidate the owning object's evaluated geometry after either native
    # storage form; evaluation remains with Blender/the explicit caller.
    owner.update_tag(refresh={"OBJECT", "DATA"})


def input_path(modifier: NodesModifier, identifier: str) -> str:
    """Return the complete Object-relative RNA path of the input's value."""
    socket = _value_socket(modifier, identifier)
    if socket is not None:
        return socket.path_from_id("value")
    escaped = bpy.utils.escape_identifier(identifier)
    return f'{modifier.path_from_id()}["{escaped}"]'
