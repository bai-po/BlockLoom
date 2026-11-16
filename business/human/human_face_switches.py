"""Paired inward-facing horizontal expression switches above the face preview."""

from contextlib import contextmanager
from typing import Any, cast

import bpy
from mathutils import Vector

from .human_face_controls import SWITCH, TRACK, _keep, _place, bounds


@contextmanager
def _edit(rig):
    context = cast(Any, bpy.context)
    if context.mode != "OBJECT":
        raise ValueError("请先回到物体模式更新面控")
    layer = context.view_layer
    previous = layer.objects.active
    selected = tuple(context.selected_objects)
    linked = rig.name not in layer.objects or not rig.visible_get()
    directly_linked = rig in tuple(context.scene.collection.objects)
    if linked and not directly_linked:
        context.scene.collection.objects.link(rig)
    hidden = rig.hide_get()
    try:
        for obj in selected:
            obj.select_set(False)
        rig.hide_set(False)
        rig.select_set(True)
        layer.objects.active = rig
        bpy.ops.object.mode_set(mode="EDIT")
        yield rig.data.edit_bones
    finally:
        if rig.mode == "EDIT":
            bpy.ops.object.mode_set(mode="OBJECT")
        rig.select_set(False)
        rig.hide_set(hidden)
        if linked and not directly_linked:
            context.scene.collection.objects.unlink(rig)
        layer.objects.active = previous
        for obj in selected:
            obj.select_set(True)


def _rail(transaction):
    vertices, edges = [], []
    for lo, hi in ((-.5, -.025), (.025, .5)):
        start = len(vertices)
        mid = (lo + hi) / 2
        vertices.extend((x, 0, z) for x, z in ((lo,-.5),(hi,-.5),(hi,.5),(lo,.5),(mid,-.5),(mid,.5)))
        edges.extend((start+a, start+b) for a,b in ((0,1),(1,2),(2,3),(3,0),(4,5)))
    mesh = transaction.track(bpy.data.meshes.new("白珀面控 · 左右横轨"))
    mesh.from_pydata(vertices, edges, [])
    return transaction.track(bpy.data.objects.new("WGT·白珀左右眼横轨", mesh))


def drive_switch(owner, prop, rig, side, stroke):
    driver = owner.driver_add(prop).driver
    var = driver.variables.new()
    var.name, var.type = "v", "TRANSFORMS"
    t = var.targets[0]
    t.id, t.bone_target = rig, side + ".眉纹切换"
    t.transform_type, t.transform_space = "LOC_X", "LOCAL_SPACE"
    expr = f"v >= {stroke*.5:.9g}" if side == "L" else f"v <= {-stroke*.5:.9g}"
    if rig.get("bp_face_legacy_keys"):
        master = driver.variables.new()
        master.name, master.type = "master", "TRANSFORMS"
        target = master.targets[0]
        target.id, target.bone_target = rig, SWITCH
        target.transform_type, target.transform_space = "LOC_Z", "LOCAL_SPACE"
        expr = "(" + expr + ") or master < -0.072"
    driver.expression = expr


def switches(transaction, rig, objects):
    head_frame = rig.pose.bones["面部切换框"]
    (xmin, xmax), (depth, _), (bottom, top) = bounds(head_frame)
    unit = xmax - xmin
    # The sprite is immediately to the right of the head selector. Native mob
    # sources can omit the sprite; its authored proportions match the selector.
    x0, x1 = xmax + .45*unit, xmax + 4.65*unit
    z = top + .04*(top-bottom)
    thumbnails = [o for o in objects if o.type == "MESH" and any(
        slot.material and "面部兼容图片" in slot.material.name for slot in o.material_slots)]
    if len(thumbnails) == 1:
        thumb = thumbnails[0]
        matrix = rig.matrix_world.inverted() @ thumb.matrix_world
        pts = [matrix @ Vector(corner) for corner in thumb.bound_box]
        a, b = min(p.x for p in pts), max(p.x for p in pts)
        if b-a > unit and b-a < 6*unit:
            x0, x1 = a, b
            z = max(p.z for p in pts) + unit*.96
    width = x1-x0
    size = min(unit*.70, width*.19)
    stroke = width*.475 - size - .06*unit
    names = ("L.眉纹切换", "R.眉纹切换")
    if any(n in rig.data.bones for n in names):
        raise ValueError("已有同名左右眼控制骨：" + rig.name)
    def remove_new():
        with _edit(rig) as bones:
            for name in names:
                if name in bones:
                    bones.remove(bones[name])
    transaction.on_rollback(remove_new)
    with _edit(rig) as bones:
        for name, x in zip(names, (x1-size/2-unit*.03, x0+size/2+unit*.03)):
            bone = bones.new(name)
            bone.head = (x, depth-.012, z)
            bone.tail = (x, depth-.092, z)
            bone.align_roll(Vector((0,0,1)))
            bone.parent = bones["头"]
            bone.use_deform = False
            for coll in bones["面部兼容"].collections:
                coll.assign(bone)
    old, frame = rig.pose.bones[SWITCH], rig.pose.bones[TRACK]
    legacy_on = old.location.z < -.072
    if rig.animation_data:
        from .human_armor import animated
        if animated(rig, old.path_from_id("location")):
            transaction.on_rollback(lambda: rig.__delitem__("bp_face_legacy_keys"))
            rig["bp_face_legacy_keys"] = True
    previous_shape = frame.custom_shape
    transaction.on_rollback(lambda: setattr(frame, "custom_shape", previous_shape))
    frame.custom_shape = _rail(transaction)
    _keep(transaction, old.bone, "hide")
    old.bone.hide = True
    _place(transaction, frame, ((x0+x1)/2, depth, z), width, size*1.20, rig.pose.bones["面部切换框"].color)
    for sign, name in zip((1,-1), names):
        p = rig.pose.bones[name]
        p.custom_shape = old.custom_shape
        p.use_custom_shape_bone_size = False
        _place(transaction, p, p.bone.head_local, size, size, rig.pose.bones["面部兼容"].color)
        p.lock_location = (False, True, True)
        p.lock_rotation = p.lock_scale = (True,True,True)
        p.lock_rotation_w = True
        c = p.constraints.new("LIMIT_LOCATION")
        c.name = "眼部表情 · 仅向中间"
        c.owner_space = "LOCAL"
        c.use_transform_limit = True
        for axis in "xyz":
            setattr(c, "use_min_"+axis, True)
            setattr(c, "use_max_"+axis, True)
        c.min_x, c.max_x = min(0,sign*stroke), max(0,sign*stroke)
        if legacy_on and not rig.get("bp_face_legacy_keys"):
            p.location.x = sign*stroke
    return stroke
