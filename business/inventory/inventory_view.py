"""Explicit viewport preview of an exactly owned first-person inventory camera."""

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import require_owned
from ...util.resources import INSTANCE
from .inventory_slots import resolve_inventory


def preview(context, root):
    ensure_mutation_allowed()
    root, _rig = resolve_inventory(context.scene, str(root[INSTANCE]))
    if root.get("独立视角") != "FIRST_PERSON":
        raise ValueError("请选择第一人称独立物品栏")
    camera = root.get("独立物品栏相机")
    if camera is None or camera.type != "CAMERA":
        raise ValueError("实例缺少第一人称专用相机")
    require_owned(root, (camera, camera.data))
    area = context.area
    if area is None or area.type != "VIEW_3D":
        raise ValueError("请从需要预览的三维视图打开物品栏面板")
    space = area.spaces.active
    region = space.region_3d
    if region is None:
        raise ValueError("当前三维视图无法预览相机")
    space.use_local_camera = True
    space.camera = camera
    region.view_perspective = "CAMERA"
    area.tag_redraw()
