"""Conservative pose upgrades for precisely owned, generated inventory assets."""

import math

import bpy
from mathutils import Matrix

from ...util.ownership import owns, require_owned
from ...util.resources import INSTANCE
from ..inventory import inventory_slots
from . import asset_display, asset_item_geometry

GUI_DISPLAY = "库存 · GUI显示变换"
POSE_SCHEMA = "oxal_mc_studio_slot_pose_schema"
ENTITY_FRAME = "oxal_mc_studio_slot_entity_frame"


def _near(a, b):
    return max(abs(float(x) - float(y)) for row_a, row_b in zip(a, b, strict=True)
               for x, y in zip(row_a, row_b, strict=True)) < 2e-5


def _trs(matrix):
    return Matrix.LocRotScale(*matrix.decompose())


def _plain_transform(obj):
    animation = obj.animation_data
    if animation and (animation.action or animation.nla_tracks):
        raise ValueError("旧格子表示存在用户 Action/NLA，未覆盖姿态：" + obj.name)
    paths = {"location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale",
             "delta_location", "delta_rotation_euler", "delta_rotation_quaternion", "delta_scale"}
    if animation and any(curve.data_path in paths for curve in animation.drivers):
        raise ValueError("旧格子表示的变换已有驱动，未覆盖姿态：" + obj.name)
    if (any(abs(v) > 1e-7 for v in (*obj.delta_location, *obj.delta_rotation_euler))
            or any(abs(v - 1) > 1e-7 for v in obj.delta_scale)
            or any(abs(v - expected) > 1e-7 for v, expected in zip(obj.delta_rotation_quaternion, (1, 0, 0, 0), strict=True))):
        raise ValueError("旧格子表示存在用户增量变换，未覆盖：" + obj.name)


def _frame(root, pivot, target, *, gui):
    frame = pivot.parent
    if (frame is None or frame.type != "EMPTY" or frame.parent != root or frame.parent_type != "OBJECT"
            or pivot.parent_type != "OBJECT" or pivot.parent_bone
            or not _near(pivot.matrix_parent_inverse, Matrix.Identity(4))
            or not _near(frame.matrix_parent_inverse, Matrix.Identity(4))
            or not _near(frame.matrix_basis, Matrix.Identity(4))
            or len(frame.constraints) != 1 or pivot.constraints):
        raise ValueError("旧格子锚点已不符合生成契约，未覆盖")
    require_owned(root, (pivot, frame))
    _plain_transform(pivot)
    _plain_transform(frame)
    constraint = frame.constraints[0]
    bone = target.gui_bone if gui else target.hand_bone
    if (constraint.type != "COPY_TRANSFORMS" or constraint.name != "OXAL · 原生物品栏绑定"
            or constraint.target != target.rig or constraint.subtarget != bone
            or constraint.owner_space != "WORLD" or constraint.target_space != "WORLD"
            or constraint.mute or abs(constraint.influence - 1) > 1e-7
            or constraint.mix_mode != "REPLACE" or constraint.remove_target_shear
            or abs(constraint.head_tail) > 1e-7):
        raise ValueError("旧格子原生约束已有修改，未覆盖")


def legacy_canvas(target):
    """The previous released canvas, before separating its display rotation."""
    from ..inventory.inventory_gui import legacy_canvas as previous_canvas

    return previous_canvas(target)


def _display(root, pivot, display):
    require_owned(root, (display,))
    _plain_transform(display)
    if (display.type != "EMPTY" or display.parent != pivot or display.parent_type != "OBJECT"
            or display.parent_bone or display.constraints
            or not _near(display.matrix_parent_inverse, Matrix.Identity(4))):
        raise ValueError("旧格子显示变换已不符合生成契约，未覆盖")


def _generated(obj):
    from ..inventory.inventory_gui import GUI_MATRIX

    values = obj.get(GUI_MATRIX)
    if values is None or len(values) != 16:
        raise ValueError("格子 GUI 缺少已知生成矩阵，未覆盖")
    if not _near(obj.matrix_basis, Matrix(tuple(tuple(values[i:i + 4]) for i in range(0, 16, 4)))):
        raise ValueError("格子 GUI 变换已有用户修改，未覆盖")


def updates(scene, root, mapping, old_objects):
    """Return matrix replacements only; publication uses the rebuild journal."""
    if root.get("oxal_mc_studio_presentation_context") != "slot":
        return {}
    host, rig, slot = (root.get(key) for key in (inventory_slots.HOST, inventory_slots.RIG, inventory_slots.SLOT))
    if host is None or rig is None:
        raise ValueError("格子资产缺少精确的原生宿主绑定，未重建")
    target = inventory_slots.resolve_target(scene, {"inventory_instance_id": host[INSTANCE], "slot": slot})
    if target.root != host or target.rig != rig:
        raise ValueError("格子宿主实例不一致，未重建")
    identifier = str(root.get("oxal_mc_studio_library_identifier", ""))
    display_identifier = identifier
    if identifier.startswith("image:"):
        display_identifier = "minecraft:stone" if root.get("oxal_mc_studio_image_kind") == "BLOCK" else "minecraft:apple"
    result = {}
    gui, held = root.get("oxal_inventory_gui"), root.get("oxal_inventory_held")
    fresh_gui = next((new for new, old in mapping.items() if old == gui), None)
    if gui is not None and fresh_gui is not None:
        from ..inventory.inventory_gui import DISPLAY_BAKED, GUI_SCHEMA, canvas_matrix

        previous_display = [obj for obj in gui.children if obj.get("oxal_mc_studio_model_part") == GUI_DISPLAY]
        next_display = [obj for obj in fresh_gui.children if obj.get("oxal_mc_studio_model_part") == GUI_DISPLAY]
        if fresh_gui.get(GUI_SCHEMA) == 2:
            baked = fresh_gui.get(DISPLAY_BAKED) == 1
            if len(next_display) != (0 if baked else 1):
                raise ValueError("新格子 GUI 显示变换不唯一，未重建")
            _frame(root, gui, target, gui=True)
            if gui.get(GUI_SCHEMA) == 2:
                if gui.get(DISPLAY_BAKED) == 1:
                    if previous_display:
                        raise ValueError("已烘焙图标仍有未知显示中间对象，未覆盖")
                    _generated(gui)
                elif len(previous_display) != 1:
                    raise ValueError("现有格子 GUI 显示变换不唯一，未覆盖")
                else:
                    display = previous_display[0]
                    _display(root, gui, display)
                    _generated(gui)
                    _generated(display)
                    if not baked:
                        result[display] = next_display[0].matrix_basis.copy()
            elif not previous_display:
                expected = _trs(legacy_canvas(target) @ asset_display.transform(display_identifier, "gui"))
                if not _near(gui.matrix_basis, expected):
                    raise ValueError("旧格子 GUI 变换已有用户修改，未覆盖")
            elif len(previous_display) == 1 and gui.get(POSE_SCHEMA) == 1:
                # The intermediate, two-object canvas used the JSON display
                # unchanged. It is accepted only with that exact contract.
                display = previous_display[0]
                _display(root, gui, display)
                if (not _near(gui.matrix_basis, _trs(legacy_canvas(target)))
                        or not _near(display.matrix_basis, asset_display.transform(display_identifier, "gui"))):
                    raise ValueError("旧格子 GUI 变换已有用户修改，未覆盖")
                if not baked:
                    result[display] = next_display[0].matrix_basis.copy()
            else:
                raise ValueError("现有格子 GUI 生成形式未知，未覆盖")
            # Keep the binding canvas separate from the display baked into
            # private geometry; never assign a sheared S@D to one Object.
            result[gui] = canvas_matrix(target, fresh_gui)
            if baked and gui.get(DISPLAY_BAKED) != 1:
                for new, old in mapping.items():
                    if old.type != "MESH" or old not in tuple(gui.children_recursive):
                        continue
                    _plain_transform(old)
                    planned_entity = (identifier in {"minecraft:shield", "minecraft:trident"}
                                      and new.get(ENTITY_FRAME) == 1 and old.get(ENTITY_FRAME) != 1)
                    expected = Matrix.Rotation(math.pi, 4, "X") if planned_entity else new.matrix_basis
                    if not _near(old.matrix_basis, expected) or old.constraints:
                        raise ValueError("旧图标部件变换已有用户修改，未移除其中间变换")
    for old in old_objects:
        if old.type == "EMPTY" and str(old.get("oxal_mc_studio_model_part", "")).startswith("库存 · 持物形态 · "):
            _plain_transform(old)
            if old.constraints or not _near(old.matrix_basis, Matrix.Identity(4)):
                raise ValueError("旧持物形态中间对象已有用户变换，未移除")
    if identifier in {"minecraft:shield", "minecraft:trident"}:
        kind = identifier.split(":")[-1]
        for new, old in mapping.items():
            if (old.type != "MESH" or new.get(ENTITY_FRAME) != 1 or old.get(ENTITY_FRAME) == 1
                    or old.get("oxal_mc_studio_model_source") != asset_item_geometry.MODEL_CLASSES[kind]):
                continue
            require_owned(root, (old, old.data))
            _plain_transform(old)
            if old.constraints or not _near(old.matrix_basis, Matrix.Rotation(math.pi, 4, "X")):
                raise ValueError("旧盾牌或三叉戟部件变换已修改，未覆盖：" + old.name)
            result[old] = new.matrix_basis.copy()
    fresh_held = next((new for new, old in mapping.items() if old == held), None)
    if (held is not None and fresh_held is not None and fresh_held.get(POSE_SCHEMA) == 1
            and held.get(POSE_SCHEMA) != 1):
        from ..inventory.inventory_projection import held_matrix

        _frame(root, held, target, gui=False)
        context = "firstperson_lefthand" if target.handedness == "LEFT" else "firstperson_righthand"
        expected = _trs(held_matrix(root, held, asset_display.transform(display_identifier, context), target))
        if not _near(held.matrix_basis, expected):
            raise ValueError("旧物品栏持物变换已修改，未覆盖")
        result[held] = held_matrix(root, fresh_held, fresh_held.matrix_basis.copy(), target)
    allowed = {root, *old_objects, *root.users_collection}
    users = bpy.data.user_map(subset=tuple(result)) if result else {}
    explicit_scene_links = set()
    if result:
        from .asset_rebuild import _explicit_scene_objects

        explicit_scene_links = _explicit_scene_objects(scene)
    for obj in result:
        permitted = allowed | ({scene} if obj not in explicit_scene_links else set())
        if any(user not in permitted and not owns(root, user) for user in users.get(obj, ())):
            raise ValueError("待修复格子姿态存在外部引用，未覆盖：" + obj.name)
    return result
