"""Explicit batch presentation for shader IDs already proven by the caller.

No ownership is discovered or granted here. Fresh import transactions and an
explicit existing-instance Tool supply their own exact ID domain. Shader groups
are arranged only when supplied directly, never by following a Group node.
"""

import re

import bpy

from ..core.runtime import ensure_mutation_allowed
from . import node_layout

_OWNERS = (bpy.types.Material, bpy.types.World, bpy.types.Light)
_VISUAL_PATH = re.compile(
    r"(?:^|\.)nodes\[(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\d+)\]\."
    r"(?:location|location_absolute|dimensions|width|height|parent|label|label_size|color|use_custom_color|shrink|hide)(?:$|\.|\[)"
)


def shader_trees(proven_ids):
    """Map actual ShaderNodeTrees to their proven native ID owners.

    A lone embedded tree has no reliable native reverse-owner lookup. Its
    Material/World/Light must be supplied too; a name or a global scan cannot
    establish that relation. Direct standalone ShaderNodeTrees own themselves.
    """
    result = {}
    for owner in tuple(proven_ids):
        if isinstance(owner, _OWNERS):
            tree = owner.node_tree
            if tree is not None and tree.is_embedded_data and tree.bl_idname == "ShaderNodeTree":
                result.setdefault(tree, set()).add(owner)
        elif isinstance(owner, bpy.types.NodeTree) and not owner.is_embedded_data and owner.bl_idname == "ShaderNodeTree":
            result.setdefault(owner, set()).add(owner)
    return result


def _readonly(owner):
    return owner.library is not None or owner.override_library is not None or not getattr(owner, "is_editable", True)


def _containers(owner):
    yield owner
    tree = getattr(owner, "node_tree", None)
    if tree is not None and tree.is_embedded_data:
        yield tree


def _protected(trees):
    reasons = {tree: set() for tree in trees}
    by_owner = {}
    for tree, owners in trees.items():
        if _readonly(tree) or any(_readonly(owner) for owner in owners):
            reasons[tree].add("只读、链接或库覆盖")
        if node_layout._animated_visuals(tree):
            reasons[tree].add("节点界面参与原生动画或驱动")
        for owner in owners:
            by_owner.setdefault(owner, set()).add(tree)
    if not by_owner:
        return reasons
    # Native reverse references cover drivers on any consuming ID, including
    # an ID's embedded compositor/shader tree. Only this bounded domain is
    # inspected; unrelated materials, objects and drivers are never enumerated.
    users = bpy.data.user_map(subset=tuple(by_owner))
    candidates = {*by_owner, *(user for consumers in users.values() for user in consumers)}
    containers = {container for owner in candidates for container in _containers(owner)}
    for container in containers:
        animation = getattr(container, "animation_data", None)
        if animation is None:
            continue
        # UI Action paths may be bound through the owning Material rather than
        # its embedded tree. Include those native writes, including nested NLA.
        own_trees = by_owner.get(container, set()) | ({container} if container in trees else set())
        if own_trees and any(_VISUAL_PATH.search(curve.data_path) for curve in node_layout._animation_curves(container)):
            for tree in own_trees:
                reasons[tree].add("节点界面参与原生动画或驱动")
        for curve in animation.drivers:
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id not in by_owner or not _VISUAL_PATH.search(target.data_path):
                        continue
                    for tree in by_owner[target.id]:
                        reasons[tree].add("其它数据驱动读取节点界面")
    return reasons


def _metadata(tree, saved=None):
    records = saved.original_ui if saved is not None else node_layout._original_ui(tree)
    if records is None:
        return
    for name, record in records["nodes"].items():
        node = tree.nodes.get(name)
        if node is not None and node.bl_idname != record["bl_idname"]:
            raise ValueError("节点排版原始UI记录与实际节点类型不一致，未覆盖")


def arrange_owned_shaders(proven_ids, *, on_rollback=None, strict=False):
    """Arrange a proven batch atomically and register exact visual journals.

    Automatic callers skip protected trees and receive their actual names.
    An explicit strict caller rejects the entire batch before any UI changes.
    Metadata errors and other unknown failures always propagate. Outer native
    transactions may replay registered restores after this helper returns.
    """
    ensure_mutation_allowed()
    if on_rollback is not None and not callable(on_rollback):
        raise TypeError("节点排版回滚登记器必须可调用")
    trees = shader_trees(proven_ids)
    ordered = sorted(trees, key=lambda tree: (tree.name_full, tuple(sorted((owner.id_type, owner.name_full) for owner in trees[tree]))))
    reasons = _protected(trees)
    snapshots = {}
    # Preflight every candidate before arranging the first tree. Read-only IDs
    # cannot use the mutating helper's snapshot check, but malformed metadata
    # is still an error rather than a reason to silently skip unknown state.
    for tree in ordered:
        if _readonly(tree) or any(_readonly(owner) for owner in trees[tree]):
            _metadata(tree)
        else:
            snapshots[tree] = node_layout.snapshot(tree)
            _metadata(tree, snapshots[tree])
    protected = [tree.name_full for tree in ordered if reasons[tree]]
    if strict and protected:
        raise ValueError("节点排版目标含只读或界面动画保护树，未修改整批：" + "、".join(protected))
    stats = {"trees": len(trees), "processed": 0, "arranged": 0, "nodes": 0, "moved": 0,
             "frames_created": 0, "frames_reused": 0, "preserved": 0, "already_arranged": 0,
             "protected": protected,
             "protected_details": [{"tree": tree.name_full, "owners": sorted(owner.name_full for owner in trees[tree]),
                                    "reasons": sorted(reasons[tree])} for tree in ordered if reasons[tree]]}
    journals = []
    try:
        for tree in ordered:
            if reasons[tree]:
                continue
            saved = snapshots[tree]
            if on_rollback is not None:
                on_rollback(lambda tree=tree, saved=saved: node_layout.restore(tree, saved))
            journals.append((tree, saved))
            result = node_layout.arrange_shader(tree, rollback=saved)
            stats["processed"] += 1
            stats["arranged"] += int(bool(result["moved"] or result["frames_created"]))
            for key in ("nodes", "moved", "frames_created", "frames_reused", "preserved", "already_arranged"):
                stats[key] += int(result[key])
    except Exception as error:
        failures = []
        for tree, saved in reversed(journals):
            try:
                node_layout.restore(tree, saved)
            except Exception as failure:  # noqa: BLE001 -- Restore every journal, then propagate all failures.
                failures.append(failure)
        if failures:
            raise ExceptionGroup("着色器排版失败，且部分视觉回滚无法完成", [error, *failures]) from error
        raise
    return stats
