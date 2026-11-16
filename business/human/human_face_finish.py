"""Fit the native blink surfaces and match the authored expression controls."""

from mathutils import Vector

from .human_face_strips import EXPRESSION_SCALE, FIELDS, SIDE, offsets


def fit_brow_depth(transaction, rig, targets):
    """Refit only depth. Keep existing expression silhouettes and parked strips."""
    from .human_face_controls import _brow_points

    for obj, key, control in targets:
        attributes = obj.data.attributes
        main, second, side = (attributes.get(n) for n in (*FIELDS[:2], SIDE))
        if not main or not second or not side:
            raise ValueError("原生眉片缺少定位属性：" + obj.name)
        points, to_rig = _brow_points(rig, obj, key)
        depth_axis = to_rig.inverted() @ Vector((0, 1, 0))
        saved = []
        def restore(values=saved):
            for item, coordinate in values:
                item.vector = coordinate
        transaction.on_rollback(restore)
        for index, (upper, lower, _, _) in offsets(points):
            # Some villager brows share centre vertices between both lift keys.
            # Use the same side as the native modifier driving that vertex.
            if side.data[index].value != control.startswith("L."):
                continue
            for attr, target in ((main, upper), (second, lower)):
                item = attr.data[index]
                previous = item.vector.copy()
                saved.append((item, previous))
                item.vector = previous + depth_axis * (target.y - (to_rig @ previous).y)


def _resize_existing(transaction, obj, lift, resized):
    mesh = obj.data
    base = mesh.shape_keys.reference_key
    main, second = [mesh.attributes[name] for name in FIELDS[:2]]
    indices = [i for i, (p, k) in enumerate(zip(base.data, lift.data)) if (p.co-k.co).length > 1e-6]
    corner = min(indices, key=lambda i: (main.data[i].vector-second.data[i].vector).length)
    if (main.data[corner].vector-second.data[corner].vector).length > 1e-5:
        raise ValueError("原眉片内端未相接，请核对自定义网格：" + obj.name)
    joint = base.data[corner].co + main.data[corner].vector
    # Native villager heads reference the same central brow vertices from
    # both lift keys. Each physical vertex receives the scale exactly once.
    remaining = [i for i in indices if i not in resized]
    resized.update(remaining)
    for attr in (main, second):
        previous = [(i, attr.data[i].vector.copy()) for i in remaining]
        def restore(a=attr, saved=previous):
            for index, vector in saved:
                a.data[index].vector = vector
        transaction.on_rollback(restore)
        for i, offset in previous:
            point = base.data[i].co + offset
            attr.data[i].vector = offset + (1-EXPRESSION_SCALE)*(joint-point)


def _blink_offsets(mesh, key, base, to_rig, sign, lower_control):
    """Return pose-space offsets at full closure. The rest shape stays editable."""
    end = {i: to_rig @ v.co for i, v in enumerate(key.data)
           if (v.co-mesh.shape_keys.reference_key.data[i].co).length > 1e-6}
    if not end:
        # Some mobs animate the eye texture and have a separate native lower
        # rim. Use its front edge for the last fraction of a pixel at closure.
        rim = [i for i, (p, v) in enumerate(zip(mesh.shape_keys.reference_key.data, lower_control.data))
               if (p.co-v.co).length > 1e-6]
        front = min(base[i].y for i in rim)
        rim = [i for i in rim if base[i].y < front+.002]
        width = max(base[i].x for i in rim)-min(base[i].x for i in rim)
        return {i: Vector((0, 0, width*.015)) for i in rim}

    upper_tag = mesh.attributes.get("Human247_LidCover_Upper")
    # The unified human key can include helper faces on the opposite side.
    # Creature keys already isolate the side, even on a mirrored native head.
    faces = [f for f in mesh.polygons if all(i in end and (not upper_tag or sign*base[i].x > 0)
                                            for i in f.vertices)]
    brow_tag = mesh.attributes.get("BP_BrowSurface")
    lids = [f for f in faces if not (brow_tag and brow_tag.data[f.index].value)]
    if upper_tag:
        upper = {i for f in lids if upper_tag.data[f.index].value for i in f.vertices}
        if not upper:
            raise ValueError("缺少原生上眼皮：" + key.name)
        front = min(base[i].y for i in upper)
        bottom = min(base[i].z for i in upper)
        lower = {i for f in lids if all(abs(base[j].y-front)<4e-5 and base[j].z<bottom-.02
                                         for j in f.vertices) for i in f.vertices}
    else:
        upper = {i for f in lids if all(end[j].z < base[j].z for j in f.vertices) for i in f.vertices}
        lower = {i for f in lids if all(end[j].z > base[j].z for j in f.vertices) for i in f.vertices}
    if not upper or not lower:
        raise ValueError("无法识别原生闭眼边缘：" + mesh.name + " / " + key.name)
    height = max(base[i].z for i in lower)-min(base[i].z for i in lower)
    bottom = min(base[i].z for i in upper)
    top = max(base[i].z for i in lower)
    upper_edge = [i for i in upper if base[i].z < bottom+height*.08]
    lower_edge = [i for i in lower if base[i].z > top-height*.08]
    brows = [i for f in faces if brow_tag and brow_tag.data[f.index].value for i in f.vertices]
    depth = (min(base[i].y for i in upper)-min(base[i].y for i in brows)) if brows else height*.012
    result = {i: Vector((0, -max(0, depth)*(.72 if i in upper else .58), 0)) for i in upper | lower}
    # A short overlap at matching columns closes the subpixel slit. Keep the
    # top and bottom lids at different depths to prevent coplanar flicker.
    for i in lower_edge:
        j = min(upper_edge, key=lambda idx: abs(base[idx].x-base[i].x))
        result[i].z = max(0, end[j].z + height*.03 - end[i].z)
    return result


def finish(transaction, rig, eyes, old_brows):
    from .human_face_controls import SIDES, TRACK, _keep

    resized = {}
    for obj, key, _ in old_brows:
        _resize_existing(transaction, obj, key, resized.setdefault(obj.data, set()))
    bones = rig.pose.bones
    for name in [TRACK, *(side+"."+suffix for side in SIDES for suffix in ("眉纹切换", "眉毛", "下眼皮"))]:
        p = bones[name]
        _keep(transaction, p, "custom_shape_wire_width")
        p.custom_shape_wire_width = bones["面部切换框" if name == TRACK else "面部兼容"].custom_shape_wire_width

    for obj in eyes:
        mesh = obj.data
        keys = mesh.shape_keys
        mat = rig.matrix_world.inverted() @ obj.matrix_world
        inv = mat.to_3x3().inverted()
        base = [mat @ v.co for v in keys.reference_key.data]
        for side, cn, sign in (("L","左",1), ("R","右",-1)):
            for key in keys.key_blocks:
                prefix, _, label = key.name.rpartition("::")
                if not label.startswith("闭"+cn):
                    continue
                lower = keys.key_blocks[prefix+"::"+side+"下眼皮" if prefix else side+"·下眼皮微调"]
                offsets = _blink_offsets(mesh, key, base, mat, sign, lower)
                saved = [(i, key.data[i].co.copy()) for i in offsets]
                def restore(k=key, values=saved):
                    for index, co in values:
                        k.data[index].co = co
                transaction.on_rollback(restore)
                for i, delta in offsets.items():
                    key.data[i].co += inv @ delta
