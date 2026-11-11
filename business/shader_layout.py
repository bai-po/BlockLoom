"""Explicit layout of the current file's precisely owned plugin shaders.

asset_id: oxal.shaders.layout; version: 1; policy: ACTIVATE_EXISTING;
scope: frozen plugin instances in this file; source: their actual native trees.
No source file is read, no material is replaced and no shader seal is re-signed.
"""

import bpy

from ..core.runtime import ensure_mutation_allowed
from ..util.ownership import owned_ids, require_owned
from ..util.resources import INSTANCE, resolve_scene
from ..util.shader_layout import arrange_owned_shaders


def request():
    """Freeze only small instance identities at the submitting Operator."""
    from .asset import assets
    from .human import human
    from .inventory import inventory_slots
    from .sky import sky

    targets = []
    for scene in bpy.data.scenes:
        groups = (("assets", assets.roots(scene)), ("human", human.roots(scene)),
                  ("inventory", inventory_slots.roots(scene)))
        for kind, roots in groups:
            targets.extend({"kind": kind, "scene_uid": int(scene.session_uid), "instance": str(root[INSTANCE])}
                           for root in roots)
        root = sky.root_for(scene)
        if root is not None:
            targets.append({"kind": "sky", "scene_uid": int(scene.session_uid), "instance": str(root[INSTANCE])})
    return targets


def _resolve(row):
    from .asset import assets
    from .human import human
    from .inventory import inventory_slots
    from .sky import sky

    if (not isinstance(row, dict) or set(row) != {"kind", "scene_uid", "instance"}
            or not isinstance(row["instance"], str) or not row["instance"]
            or isinstance(row["scene_uid"], bool) or not isinstance(row["scene_uid"], int)):
        raise ValueError("着色器排版目标快照无效，请重新提交")
    scene = resolve_scene(row["scene_uid"])
    if row["kind"] == "assets":
        root = assets.find_root(scene, row["instance"])
    elif row["kind"] == "human":
        root = human.resolve_human(scene, row["instance"])
    elif row["kind"] == "inventory":
        root = inventory_slots.resolve_inventory(scene, row["instance"])[0]
    elif row["kind"] == "sky":
        root = sky.root_for(scene)
        if root is None or str(root[INSTANCE]) != row["instance"]:
            raise ValueError("天空目标已改变，请重新提交着色器排版")
    else:
        raise ValueError("未知的插件着色器排版模块")
    require_owned(root, (root,))
    return root


def execute(options):
    ensure_mutation_allowed()
    targets = options.get("shader_targets")
    if not isinstance(targets, list) or len(targets) > 8192:
        raise ValueError("着色器排版缺少有限的实例快照")
    roots, seen = [], set()
    for row in targets:
        root = _resolve(row)
        if root in seen:
            raise ValueError("着色器排版目标重复或实例标识冲突，未改写")
        seen.add(root)
        roots.append(root)
    proven = set()
    for root in roots:
        items = owned_ids(root)
        require_owned(root, items)
        proven.update((root, *items))
    stats = arrange_owned_shaders(proven)
    protected = len(stats.get("protected", ()))
    message = f"已整理 {stats.get('arranged', 0)} 个插件着色器，保留原节点连线与动画"
    if protected:
        message += f"；{protected} 个含界面动画、驱动引用或只读数据，保持原位"
    return {"message": message, "shader_layout": stats}
