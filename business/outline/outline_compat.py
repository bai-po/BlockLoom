"""Exact RNA projections for the tested Blender 4.5 -> 5.x transitions.

Projections are digest candidates, never ownership proof or permission to
re-sign an old graph. Every candidate must match the saved historical hash.
"""

import bpy

HOSTS = {(5, 0), (5, 1), (5, 2)}


def _static(node, socket, expected, animated):
    return (not socket.is_linked and socket.path_from_id("default_value") not in animated
            and socket.default_value == expected)


def _principled_insert(node, animated):
    return (node.bl_idname == "ShaderNodeBsdfPrincipled" and len(node.inputs) == 32
            and node.inputs[4].identifier == "Alpha" and node.inputs[5].identifier == "Thin Wall"
            and node.inputs[6].identifier == "Normal" and node.inputs[-1].identifier == "Thin Film IOR"
            and _static(node, node.inputs[5], False, animated))


def legacy_driver_paths(tree, captured):
    """One historical digest candidate for native Principled socket insertion.

    Blender updates numeric animation paths when opening a 4.5 material in
    5.x. Project only that exact known insertion, without touching real curves
    or accepting a different target, expression, node or reference table.
    """
    if bpy.app.version[:2] not in HOSTS:
        return captured
    animated = {record["path"] for record in captured["curves"]}
    prefixes = {node.path_from_id() + ".inputs[": len(node.inputs)
                for node in tree.nodes if _principled_insert(node, animated)}
    curves = []
    for record in captured["curves"]:
        path = record["path"]
        for prefix, count in prefixes.items():
            if not path.startswith(prefix):
                continue
            index, closing, suffix = path[len(prefix):].partition("]")
            if closing and index.isdecimal() and 5 < int(index) < count:
                path = prefix + str(int(index) - 1) + "]" + suffix
            break
        curves.append({**record, "path": path})
    return {**captured, "curves": curves}


def properties(owner, values):
    """New UI/native defaults may be absent from a 4.5 record."""
    if bpy.app.version[:2] not in HOSTS:
        return values
    defaults = {"optional_label": False, "show_manage_panel": True}
    if getattr(owner, "bl_idname", "") == "GeometryNodeRealizeInstances":
        defaults["realize_to_point_domain"] = True
    return [(key, value) for key, value in values if key not in defaults or value != defaults[key]]


def random_float(node):
    return (bpy.app.version[:2] == (5, 2) and node.bl_idname == "FunctionNodeRandomValue"
            and node.data_type == "FLOAT"
            and tuple((s.identifier, s.bl_idname) for s in node.inputs) == (
                ("Min", "NodeSocketFloat"), ("Max", "NodeSocketFloat"),
                ("ID", "NodeSocketInt"), ("Seed", "NodeSocketInt"))
            and tuple((s.identifier, s.bl_idname) for s in node.outputs) == (("Value", "NodeSocketFloat"),))


def node_record(node, scalar, values, animated):
    """Reconstruct the exact old scalar/socket layout without changing RNA."""
    if bpy.app.version[:2] not in HOSTS:
        return scalar, values
    scalar = properties(node, scalar)
    kind = node.bl_idname
    if _principled_insert(node, animated):
        scalar = [(key, "RANDOM_WALK" if value == "RANDOM_WALK_LEGACY" else
                   "RANDOM_WALK_5X" if value == "RANDOM_WALK" else value)
                  if key == "subsurface_method" else (key, value) for key, value in scalar]
        values = [(index - (index > 5), value) for index, value in values if index != 5]
    elif (kind == "GeometryNodeRemoveAttribute" and tuple(s.identifier for s in node.inputs) == ("Geometry", "Pattern Mode", "Name")
            and _static(node, node.inputs[1], "Exact", animated)):
        scalar = [*scalar, ("pattern_mode", "EXACT")]
        values = [(index - (index > 1), value) for index, value in values if index != 1]
    elif (kind == "GeometryNodeMergeByDistance"
          and tuple(s.identifier for s in node.inputs) == ("Geometry", "Selection", "Mode", "Distance")
          and _static(node, node.inputs[2], "All", animated)):
        scalar = [*scalar, ("mode", "ALL")]
        values = [(index - (index > 2), value) for index, value in values if index != 2]
    elif (bpy.app.version[:2] == (5, 2) and kind == "GeometryNodeCaptureAttribute"
          and len(node.inputs) >= 3 and len(node.outputs) >= 3
          and node.inputs[1].identifier == node.outputs[1].identifier == "Selection"
          and _static(node, node.inputs[1], True, animated)
          and _static(node, node.outputs[1], False, animated)):
        split = len(node.inputs)
        values = [(index - (1 if index < split else 2), value) for index, value in values
                  if index not in {1, split + 1}]
    elif random_float(node):
        # Blender 5.2 drops inactive Random Value sockets. Only the released
        # generator's known defaults are reconstructable. A formerly edited
        # inactive value cannot match the saved hash and remains a refusal.
        old = {0: (0.0, 0.0, 0.0), 1: (1.0, 1.0, 1.0), 4: 0, 5: 100,
               6: 0.5, 9: (0.0, 0.0, 0.0), 11: 0, 12: False}
        indices = {0: 2, 1: 3, 2: 7, 3: 8, 4: 10}
        old.update((indices[index], value) for index, value in values)
        values = sorted(old.items())
    return scalar, values


def socket_identifier(node, identifier, *, output):
    if random_float(node):
        return ({"Value": "Value_001"} if output else {"Min": "Min_001", "Max": "Max_001"}).get(identifier, identifier)
    return identifier


def modifier_record(modifier, scalar, storage):
    if bpy.app.version[:2] not in HOSTS:
        return scalar, storage
    scalar = properties(modifier, scalar)
    if bpy.app.version[:2] == (5, 2) and storage == [[], []]:
        storage = {}
    return scalar, storage
