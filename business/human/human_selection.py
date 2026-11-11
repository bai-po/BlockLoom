"""Read-only armor selection snapshots shared by native UI and Operators."""

from ...util.ownership import require_owned
from ...util.resources import INSTANCE


def armor_snapshot(scene, selected_objects, *, strict=False):
    """A mesh selects its part; the actual main rig selects added armor parts."""
    from .human import resolve_human, rig_of, roots
    from .human_armor import objects, part_of
    from .human_state import PARTS

    try:
        selected = tuple(selected_objects)
        candidates = [root for root in roots(scene) if any(obj in tuple(root.all_objects) for obj in selected)]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ValueError("请一次选择一个人模的盔甲，避免修改其它实例")
        from ..asset.assets import root_of

        if any(root_of(obj) is not None for obj in selected):
            raise ValueError("请分开选择物品方块与人模盔甲，再执行一键附魔")
        root = resolve_human(scene, str(candidates[0][INSTANCE]))
        selected_members = tuple(obj for obj in selected if obj in tuple(root.all_objects))
        require_owned(root, selected_members)
        if rig_of(root) in selected_members:
            parts = tuple(part for part in PARTS if objects(root, part))
        else:
            selected_parts = {part_of(obj) for obj in selected_members if obj.type == "MESH"}
            parts = tuple(part for part in PARTS if part in selected_parts and objects(root, part))
        if not parts:
            raise ValueError("请选择已添加的盔甲网格或人模主骨架；皮肤和背饰不参与盔甲附魔")
        return {"instance_id": str(root[INSTANCE]), "parts": list(parts)}
    except ValueError:
        if strict:
            raise
        return None
