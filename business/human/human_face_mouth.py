"""Keep the two native mouth pads independent while retaining both animation channels."""

from typing import cast

import bpy

from .human import _matches

SELECTOR = "嘴巴线性切换"
PADS = ("嘴巴控制器-控制", "嘴巴控制器-线性控制")
# The original rig swaps the two visible pad bones at this local Z position.
DISCRETE = "-0.03 < var < 0.003"
LIMIT = "嘴型 · 停用控制归零"


def _selector_input(driver, rig, name):
    variable = driver.variables.new()
    variable.name, variable.type = name, "TRANSFORMS"
    target = variable.targets[0]
    target.id, target.bone_target = rig, SELECTOR
    target.transform_type, target.transform_space = "LOC_Z", "LOCAL_SPACE"
    return variable


def _original_driver(owner, path, rig, bone):
    animation = owner.animation_data
    curve = animation.drivers.find(path) if animation else None
    if curve and curve.driver.type == "SCRIPTED" and len(curve.driver.variables) == 1:
        variable = curve.driver.variables[0]
        target = variable.targets[0]
        if (variable.type == "TRANSFORMS" and variable.name == "var" and
                target.id == rig and target.bone_target == bone):
            return curve.driver
    raise ValueError("嘴型控制驱动缺失或已自定义：" + owner.name + " · " + path)


def install(transaction, rig, objects):
    mouths = [obj for obj in objects if obj.type == "MESH" and _matches(obj, "嘴")]
    if not mouths:
        # Creature source meshes use their own painted mouths.
        return
    mouth, = mouths
    keys = mouth.data.shape_keys
    if not keys or not {"X", "Z", "X-线性", "Z-线性"}.issubset(keys.key_blocks.keys()):
        raise ValueError("嘴部缺少原生离散或线性形态键：" + mouth.name)
    pads = [rig.pose.bones[name] for name in PADS]
    if any(p.constraints.get(LIMIT) for p in pads):
        raise ValueError("嘴型控制器已包含同名约束：" + LIMIT)
    # This one authored key used the raw pose channel, bypassing constraints.
    # When the pad is active keep its original (pre-constraint) input intact.
    raw_x = _original_driver(keys, keys.key_blocks["X"].path_from_id("value"), rig, PADS[0])

    selectors = []
    materials = set()
    for slot in mouth.material_slots:
        mat = slot.material
        if not mat or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "VALUE" and node.label == "嘴巴控制器":
                shader = _original_driver(mat.node_tree, node.outputs[0].path_from_id("default_value"), rig, SELECTOR)
                if shader.expression != "1 if ((var*0.410683101639804) > -0.03 and (var*0.410683101639804) < 0.003) else 3":
                    raise ValueError("嘴部材质的嘴型选择已自定义：" + mat.name)
                selectors.append(shader)
                materials.add(mat)
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned

        root = transaction.owner
        require_owned(root, (rig, mouth, mouth.data, *materials))
        require_no_external_users(cast(bpy.types.Scene, bpy.context.scene), root, (keys, mouth.data, *materials))

    for pad, disabled in zip(pads, ("not (" + DISCRETE + ")", DISCRETE), strict=True):
        constraint = pad.constraints.new("LIMIT_LOCATION")
        def restore(p=pad, c=constraint):
            c.driver_remove("influence")
            p.constraints.remove(c)
        transaction.on_rollback(restore)
        constraint.name = LIMIT
        constraint.owner_space = "LOCAL"
        # The hidden pad's evaluated position becomes neutral. The F-curves
        # and the pad's actual stored location are untouched when switching.
        constraint.use_transform_limit = False
        for axis in "xyz":
            setattr(constraint, "use_min_" + axis, True)
            setattr(constraint, "use_max_" + axis, True)
        driver = constraint.driver_add("influence").driver
        _selector_input(driver, rig, "var")
        driver.expression = disabled

    original = raw_x.expression
    variable = _selector_input(raw_x, rig, "mode")
    def restore_raw_key():
        raw_x.expression = original
        raw_x.variables.remove(variable)
    transaction.on_rollback(restore_raw_key)
    raw_x.expression = f"({original}) if {DISCRETE.replace('var', 'mode')} else 0"

    for shader in selectors:
        transaction.on_rollback(lambda d=shader, old=shader.expression: setattr(d, "expression", old))
        shader.expression = "1 if " + DISCRETE + " else 3"
