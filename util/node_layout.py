"""Explicit, reversible presentation of a caller-owned shader node tree.

This module never discovers materials or runs from registration/draw/timers.
It changes only node presentation and creates Frame nodes, never shader groups,
business node names, sockets, links, settings, animation or ID references.
Callers keep responsibility for ownership, native Undo and their shader seals.
"""

import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from math import isfinite
from typing import Any

import bpy

from ..core.runtime import ensure_mutation_allowed
from .animation import action_fcurves

FRAME_MARK = "oxal_mc_studio_shader_layout_frame"
FRAME_CATEGORY = "oxal_mc_studio_shader_layout_category"
META = "oxal_mc_studio_layout_original_ui"
_SCHEMA = 1
_FRAME_PREFIX = "OXAL · 布局 · "
_GROUPS = (
    ("coordinates", "坐标与动画", (.20, .28, .39)),
    ("textures", "贴图采样", (.29, .24, .17)),
    ("color", "颜色与材质", (.22, .32, .23)),
    ("alpha", "透明与遮罩", (.30, .23, .34)),
    ("enchantment", "附魔与描边", (.32, .20, .34)),
    ("output", "着色与输出", (.35, .23, .20)),
)
_VISUAL_PATH = re.compile(r'(?:^|\.)nodes\["(?:[^"\\]|\\.)*"\]\.(?:location|location_absolute|dimensions|width|height|parent|label|label_size|color|use_custom_color|shrink|hide)(?:$|\.|\[)')


def _animation_curves(tree):
    animation = tree.animation_data
    if animation is None:
        return ()
    result = list(animation.drivers)
    result.extend(action_fcurves(animation.action, slot=getattr(animation, "action_slot", None)))
    strips = [strip for track in animation.nla_tracks for strip in track.strips]
    while strips:
        strip = strips.pop()
        result.extend(action_fcurves(strip.action, slot=getattr(strip, "action_slot", None)))
        strips.extend(getattr(strip, "strips", ()))
    return result


def _animated_visuals(tree, *, prefix=None):
    for curve in _animation_curves(tree):
        if (prefix is None and _VISUAL_PATH.search(curve.data_path)
                or prefix is not None and curve.data_path.startswith(prefix + ".")):
            return True
        driver = getattr(curve, "driver", None)
        if driver is None:
            continue
        for variable in driver.variables:
            for target in variable.targets:
                owner = target.id
                references_tree = owner == tree or isinstance(owner, (bpy.types.Material, bpy.types.World, bpy.types.Light)) and owner.node_tree == tree
                if references_tree and (prefix is None and _VISUAL_PATH.search(target.data_path)
                                        or prefix is not None and (target.data_path.startswith(prefix + ".") or target.data_path.startswith("node_tree." + prefix + "."))):
                    return True
    return False


def is_layout_frame(node) -> bool:
    """Recognize only this helper's nonfunctional Frames for UI projection.

    Original/source Frames, Text references, custom business properties and
    Frame animation are not presentation-only. The owning explicit Tool also
    guards references from other IDs before moving any node in a shader tree.
    """
    if (node.type != "FRAME" or not node.name.startswith(_FRAME_PREFIX)
            or type(node.get(FRAME_MARK)) is not int or node.get(FRAME_MARK) != _SCHEMA
            or set(node.keys()) != {FRAME_MARK, FRAME_CATEGORY}
            or len(node.inputs) or len(node.outputs) or getattr(node, "text", None) is not None):
        return False
    category = node.get(FRAME_CATEGORY)
    expected = next((label for key, label, _color in _GROUPS if key == category), None)
    return (expected is not None and node.label == expected
            and node.name.startswith(_FRAME_PREFIX + category)
            and not _animated_visuals(node.id_data, prefix=node.path_from_id()))


@dataclass
class LayoutSnapshot:
    """Ephemeral exact-node journal, not a Bus payload or persistent registry."""

    tree: Any
    nodes: tuple
    active: Any = None
    original_ui: Any = None
    created_frames: list = field(default_factory=list)


def _ui_root(tree):
    raw = tree.get(META)
    if raw is None and META not in tree:
        return None
    if (raw is None or not hasattr(raw, "items") or set(raw.keys()) != {"schema", "nodes"}
            or type(raw.get("schema")) is not int or raw.get("schema") != _SCHEMA
            or not hasattr(raw.get("nodes"), "items")):
        raise ValueError("节点排版原始UI记录格式无效，未覆盖")
    return raw


def _ui_row(key, value):
    if (not isinstance(key, str) or not key or not hasattr(value, "items")
            or set(value.keys()) != {"name", "bl_idname", "location_absolute"}
            or value.get("name") != key or not isinstance(value.get("bl_idname"), str)
            or not value.get("bl_idname")):
        raise ValueError("节点排版原始UI身份记录无效，未覆盖")
    location = value.get("location_absolute")
    if (not hasattr(location, "__len__") or len(location) != 2
            or any(isinstance(number, bool) or not isinstance(number, (int, float)) or not isfinite(number) for number in location)):
        raise ValueError("节点排版原始位置必须为两个有限数字")
    return {"name": key, "bl_idname": value["bl_idname"],
            "location_absolute": [float(number) for number in location]}


def _original_ui(tree):
    raw = _ui_root(tree)
    if raw is None:
        return None
    records = {key: _ui_row(key, value) for key, value in raw["nodes"].items()}
    return {"schema": _SCHEMA, "nodes": records}


def original_ui_value(node, identifier, current):
    """Project only an original absolute UI position for legacy shader seals.

    Names, node types and finite coordinates are checked; every business value
    and every other UI field remains the caller's actual value. This metadata
    lives on the tree, never in a business node's custom properties.
    """
    if identifier != "location_absolute" or node.id_data.bl_idname != "ShaderNodeTree":
        return current
    saved = _ui_root(node.id_data)
    row = saved["nodes"].get(node.name) if saved else None
    if row is None:
        return current
    row = _ui_row(node.name, row)
    if row["bl_idname"] != node.bl_idname:
        raise ValueError("原始UI记录与实际节点名称/类型不一致，未投影业务参数")
    return tuple(row["location_absolute"])


def _remember_original_ui(tree, nodes):
    saved = _original_ui(tree) or {"schema": _SCHEMA, "nodes": {}}
    for node in nodes:
        row = saved["nodes"].get(node.name)
        if row is not None:
            if row["name"] != node.name or row["bl_idname"] != node.bl_idname:
                raise ValueError("原始UI记录与实际节点身份变化，未覆盖既存记录")
            continue
        location = tuple(node.location_absolute)
        if len(location) != 2 or not all(isfinite(number) for number in location):
            raise ValueError("节点原始绝对位置不是两个有限数字")
        saved["nodes"][node.name] = {"name": node.name, "bl_idname": node.bl_idname,
                                     "location_absolute": list(location)}
    tree[META] = saved


def snapshot(tree) -> LayoutSnapshot:
    """Capture only visual fields; do not copy a node tree or its shader data."""
    _check_tree(tree)
    return LayoutSnapshot(tree, tuple(
        (node, node.parent, tuple(node.location), float(node.width), float(node.height),
         node.label, node.use_custom_color, tuple(node.color),
         getattr(node, "shrink", None), getattr(node, "label_size", None), node.select)
        for node in tree.nodes), active=tree.nodes.active, original_ui=_original_ui(tree))


def restore(tree, saved: LayoutSnapshot) -> None:
    """Restore captured references; remove only Frames created in this journal.

    An unrelated node created after the snapshot is never removed or adopted.
    The caller must not delete/recreate captured business nodes before rollback.
    """
    if saved.tree != tree:
        raise ValueError("节点排版快照不属于此实际节点树")
    current = set(tree.nodes)
    original = {row[0] for row in saved.nodes}
    if not original <= current:
        raise ValueError("排版快照中的实际节点已被移除，未按名称接管替代节点")
    created = [node for node in saved.created_frames if node in current]
    if any(node.type != "FRAME" for node in created):
        raise ValueError("排版回滚创建记录不是Frame，未删除业务节点")
    # Detach before restoring parent-relative locations; Node parenting itself
    # may otherwise translate the coordinates during a rollback.
    for row in saved.nodes:
        row[0].parent = None
    for row in saved.nodes:
        node, parent, location, width, height, label, custom, color, shrink, label_size, selected = row
        node.parent = parent
        node.location = location
        node.width, node.height = width, height
        node.label, node.use_custom_color, node.color = label, custom, color
        node.select = selected
        if shrink is not None:
            node.shrink = shrink
        if label_size is not None:
            node.label_size = label_size
    for node in reversed(created):
        # A foreign node parented to our new Frame is retained at its actual
        # absolute position instead of being moved by Frame removal.
        for child in tuple(tree.nodes):
            if child.parent == node and child not in original:
                position = _absolute(child)
                child.parent = None
                child.location = position
        tree.nodes.remove(node)
    saved.created_frames.clear()
    tree.nodes.active = saved.active
    if saved.original_ui is not None:
        tree[META] = saved.original_ui
    elif META in tree:
        del tree[META]


def _check_tree(tree):
    if not isinstance(tree, bpy.types.NodeTree) or tree.bl_idname != "ShaderNodeTree":
        raise TypeError("仅支持明确传入的ShaderNodeTree")
    if tree.library is not None or tree.override_library is not None:
        raise ValueError("节点排版需要可编辑的私有节点树")


def _absolute(node):
    x, y = node.location
    parent, seen = node.parent, set()
    while parent is not None:
        if parent in seen:
            raise ValueError("节点Frame层级存在循环，未排版")
        seen.add(parent)
        x += parent.location.x
        y += parent.location.y
        parent = parent.parent
    return float(x), float(y)


def _size(node):
    """Conservative UI extent; numeric socket values never affect placement."""
    if node.type == "REROUTE":
        return 32., 32.
    width = max(110., float(node.width))
    if node.hide:
        return width, 42.
    inputs = [socket for socket in node.inputs if socket.enabled and not socket.hide]
    outputs = [socket for socket in node.outputs if socket.enabled and not socket.hide]
    socket_height = sum(72 if socket.type == "VECTOR" and not socket.is_linked and not socket.hide_value else 26
                        for socket in inputs)
    height = 75. + max(socket_height, len(outputs) * 26.)
    minimum = {
        "BSDF_PRINCIPLED": 980., "TEX_IMAGE": 370., "MAPPING": 380.,
        "VALTORGB": 280., "CURVE_RGB": 350., "CURVE_VEC": 350.,
        "RGB": 250., "TEX_NOISE": 430., "TEX_VORONOI": 510.,
        "TEX_WAVE": 430., "VECT_MATH": 250., "VECTOR_MATH": 250.,
        "ATTRIBUTE": 190., "MIX": 230., "MIX_RGB": 210., "MATH": 165.,
    }.get(node.type, 115.)
    # dimensions is often (0, 0) before an editor has drawn the tree. Using a
    # conservative type/socket estimate also keeps repeated offline runs stable.
    height = max(height, minimum)
    return width, height


def _graphs(tree, nodes):
    selected = set(nodes)
    before, after = {node: set() for node in nodes}, {node: set() for node in nodes}
    for link in tree.links:
        source, target = link.from_node, link.to_node
        if source in selected and target in selected and source != target:
            before[target].add(source)
            after[source].add(target)
    return before, after


def _well_placed(tree, nodes):
    if len(nodes) < 2:
        return True
    points = {node: _absolute(node) for node in nodes if node.type != "REROUTE"}
    if len(points) < 2:
        return False
    repeated = Counter((round(x, 2), round(y, 2)) for x, y in points.values())
    if max(repeated.values()) > 1:
        return False
    xs = [position[0] for position in points.values()]
    if max(xs) - min(xs) < max(500., min(len(points), 8) * 100.):
        return False
    links = [link for link in tree.links if link.from_node in points and link.to_node in points]
    return not links or sum(points[link.to_node][0] >= points[link.from_node][0] for link in links) >= len(links) * .7


def _category(node, reaches):
    name = (node.name + " " + node.label).casefold()
    if node.type in {"OUTPUT_MATERIAL", "OUTPUT_WORLD", "OUTPUT_LIGHT", "GROUP_OUTPUT", "GROUP_INPUT"}:
        return "output" if node.type != "GROUP_INPUT" else "coordinates"
    if any(word in name for word in ("oxal glint", "outline", "附魔", "描边")):
        return "enchantment"
    if (node.type.startswith("BSDF_") or node.type in {"MIX_SHADER", "ADD_SHADER", "EMISSION", "BACKGROUND"}
            or node.type == "GROUP" and any(socket.type == "SHADER" for socket in node.outputs)):
        return "output"
    if node.type == "TEX_IMAGE" or node.type.startswith("TEX_") and node.type != "TEX_COORD":
        return "textures"
    if any(word in name for word in ("alpha", "opacity", "透明", "遮罩", "cutout")):
        return "alpha"
    if node.type in {"TEX_COORD", "UVMAP", "MAPPING", "NEW_GEOMETRY", "ATTRIBUTE", "VECT_TRANSFORM", "VECTOR_TRANSFORM"}:
        return "coordinates"
    if "vector" in reaches.get(node, ()):
        return "coordinates"
    if reaches.get(node) == {"alpha"}:
        return "alpha"
    return "color"


def _reachability(tree, nodes):
    """Small semantic hints from actual socket wiring, never socket numbers."""
    before, after = _graphs(tree, nodes)
    reach = {node: set() for node in nodes}
    queue = deque()
    for link in tree.links:
        if link.from_node not in reach:
            continue
        name = link.to_socket.name.casefold()
        tag = "alpha" if name in {"alpha", "opacity"} else "vector" if link.to_node.type == "TEX_IMAGE" and name == "vector" else None
        if tag:
            reach[link.from_node].add(tag)
            queue.append(link.from_node)
    while queue:
        node = queue.popleft()
        for parent in before[node]:
            previous = len(reach[parent])
            reach[parent].update(reach[node])
            if len(reach[parent]) != previous:
                queue.append(parent)
    return reach, before, after


def _layers(nodes, before, after):
    selected = set(nodes)
    degrees = {node: len(before[node] & selected) for node in nodes}
    queue = deque(sorted((node for node in nodes if not degrees[node]), key=lambda node: node.name))
    levels = {node: 0 for node in nodes}
    order = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for target in sorted(after[node] & selected, key=lambda part: part.name):
            levels[target] = max(levels[target], levels[node] + 1)
            degrees[target] -= 1
            if not degrees[target]:
                queue.append(target)
    cyclic = sorted(selected - set(order), key=lambda node: node.name)
    # Invalid shader cycles remain untouched as links. They only receive a
    # finite presentation fallback rather than causing an unbounded traversal.
    for node in cyclic:
        levels[node] = 0
    return levels, len(cyclic)


def arrange_shader(tree, *, nodes=None, rollback: LayoutSnapshot | None = None,
                   force=False, preserve_authored_frames=True, origin=(0., 0.),
                   horizontal_gap=90., vertical_gap=55., frame_gap=150.,
                   padding=55., column_height=1700.) -> dict:
    """Arrange explicitly supplied shader nodes, with stable semantic Frames.

    ``rollback=snapshot(tree)`` lets an outer Tool register ``restore`` with its
    transaction. The returned dictionary is safe for a plain-data status reply.
    Existing authored Frame hierarchies stay intact. Without ``force``, an
    already spread manual shader (including the supplied mouth) is preserved.
    """
    ensure_mutation_allowed()
    _check_tree(tree)
    for value in (*origin, horizontal_gap, vertical_gap, frame_gap, padding, column_height):
        if not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError("节点排版间距必须为有限数字")
    if min(horizontal_gap, vertical_gap, frame_gap, padding) < 0 or column_height < 300:
        raise ValueError("节点排版间距或列高度无效")
    all_nodes = tuple(tree.nodes)
    selected = set(all_nodes if nodes is None else tuple(nodes))
    if not selected <= set(all_nodes):
        raise ValueError("排版目标包含其它节点树或失效节点")
    saved = rollback or snapshot(tree)
    if saved.tree != tree:
        raise ValueError("节点排版快照不属于此实际节点树")
    if _animated_visuals(tree):
        raise ValueError("节点位置或Frame界面字段参与原生动画/驱动，未移动业务参数")
    generated = {node for node in all_nodes if is_layout_frame(node)}
    protected = set()
    if preserve_authored_frames:
        for node in all_nodes:
            parent = node.parent
            while parent is not None:
                if parent.type == "FRAME" and parent not in generated:
                    protected.add(node)
                    break
                parent = parent.parent
    targets = tuple(node for node in all_nodes if node in selected and node.type != "FRAME" and node not in protected)
    stats = {"nodes": len(targets), "moved": 0, "frames_created": 0, "frames_reused": 0,
             "preserved": len(protected), "already_arranged": False, "cyclic_nodes": 0}
    if not targets or not force and not generated and _well_placed(tree, targets):
        stats["already_arranged"] = True
        return stats
    reach, before, after = _reachability(tree, targets)
    levels, stats["cyclic_nodes"] = _layers(targets, before, after)
    groups = defaultdict(list)
    for node in targets:
        groups[_category(node, reach)].append(node)
    occupied = [node for node in all_nodes if node.type != "FRAME" and node not in targets and node not in generated]
    x, top = map(float, origin)
    if occupied:
        x = max(x, max(_absolute(node)[0] + _size(node)[0] for node in occupied) + frame_gap)
    try:
        active = tree.nodes.active
        _remember_original_ui(tree, targets)
        for category, label, color in _GROUPS:
            members = sorted(groups.get(category, ()), key=lambda node: node.name)
            if not members:
                continue
            available = [frame for frame in generated if frame.get(FRAME_CATEGORY) == category
                         and all(child in targets for child in all_nodes if child.parent == frame)]
            if available:
                frame = min(available, key=lambda node: node.name)
                stats["frames_reused"] += 1
            else:
                frame = tree.nodes.new("NodeFrame")
                saved.created_frames.append(frame)
                frame.name = _FRAME_PREFIX + category
                frame[FRAME_MARK], frame[FRAME_CATEGORY] = _SCHEMA, category
                frame.select = False
                stats["frames_created"] += 1
            columns = defaultdict(list)
            for node in members:
                columns[levels[node]].append(node)
            placements, right, bottom = {}, x + padding, top - padding - 35.
            for level in sorted(columns):
                column = columns[level]
                cursor_x, cursor_y, width = right, top - padding - 35., 0.
                for node in column:
                    node_width, node_height = _size(node)
                    if cursor_y < top - padding - 35. and top - padding - 35. - cursor_y + node_height > column_height:
                        cursor_x += width + horizontal_gap
                        cursor_y, width = top - padding - 35., 0.
                    placements[node] = (cursor_x, cursor_y)
                    width = max(width, node_width)
                    bottom = min(bottom, cursor_y - node_height)
                    cursor_y -= node_height + vertical_gap
                right = cursor_x + width + horizontal_gap
            frame.parent = None
            frame.shrink = False
            frame.location = (x, top)
            frame.label, frame.label_size = label, 24
            frame.use_custom_color, frame.color = True, color
            frame.width, frame.height = right - x - horizontal_gap + padding, top - bottom + padding
            for node, position in placements.items():
                previous = _absolute(node)
                node.parent = frame
                node.location = (position[0] - x, position[1] - top)
                if abs(previous[0] - position[0]) > .01 or abs(previous[1] - position[1]) > .01:
                    stats["moved"] += 1
            x += frame.width + frame_gap
        tree.nodes.active = active
        return stats
    except Exception:
        restore(tree, saved)
        raise
