"""Fit the inner edge of each native closed eyelid to its head variant."""

from statistics import median
from typing import Any, cast

import bpy
from mathutils import Vector


def _fit_variant(transaction, mesh, to_rig, slot, faces):
    keys = mesh.shape_keys
    basis = keys.reference_key
    shell = keys.key_blocks.get(f"头型状态 {slot} · 原壳体位置") if slot else basis
    if not shell:
        raise ValueError(f"T{slot} 缺少原壳体位置")
    base = [to_rig @ p.co for p in basis.data]
    rest = [to_rig @ p.co for p in shell.data]
    to_mesh = to_rig.to_3x3().inverted()
    tag = mesh.attributes["Human247_LidCover_Upper"]
    for sign, side, label in ((-1, "R", "右"), (1, "L", "左")):
        blink, = [k for k in keys.key_blocks if k.name.startswith(f"T{slot}::闭{label}")]
        lower_key = keys.key_blocks[f"T{slot}::{side}下眼皮"]
        upper = {i for f in faces if tag.data[f.index].value
                 and all(sign * base[j].x > 0 for j in f.vertices) for i in f.vertices}
        inner = min(sign * base[i].x for i in upper)
        width = max(sign * base[i].x for i in upper) - inner
        tol = width * .006
        # The stationary column next to the eye supplies this variant's
        # actual skin depth, including its independent shell shape key.
        skin = []
        for face in faces:
            points = [rest[i] for i in face.vertices]
            xs = [sign * v.x for v in points]
            if (min(xs) < inner - .1 * width and abs(max(xs) - inner) < tol
                    and max(p.y for p in points) - min(p.y for p in points) < tol * .1):
                skin.append(sum(p.y for p in points) / len(points))
        if not skin:
            raise ValueError(f"T{slot} 的{label}眼内缘缺少相邻脸面")
        depth = median(skin)
        lower = {i for i, (a, b) in enumerate(zip(lower_key.data, basis.data))
                 if (a.co - b.co).length > 1e-6 and abs(base[i].y - min(base[j].y for j in upper)) < tol * .1}
        border = [i for i in upper | lower if abs(sign * base[i].x - inner) < tol]
        if not (set(border) & upper and set(border) & lower):
            raise ValueError(f"T{slot} 的{label}眼缺少原生闭眼内缘")
        saved = [(i, blink.data[i].co.copy()) for i in border]

        def restore(key=blink, coords=saved):
            for i, co in coords:
                key.data[i].co = co

        transaction.on_rollback(restore)
        for i, old in saved:
            closed = to_rig @ old + (rest[i] - base[i])
            # Give the two edges a slight lap at closure; the middle of the
            # nose and the open eye are untouched. Keep the upper lid ahead
            # of the lower lid across their existing horizontal overlap.
            target = depth - width * (.0007 if i in upper else .0002)
            blink.data[i].co += to_mesh @ Vector((-sign * width * .014, target - closed.y, 0))


def install(transaction, rig, objects):
    for obj in objects:
        if obj.type != "MESH":
            continue
        mesh = obj.data
        variants = mesh.attributes.get("V3_HeadVariant")
        if not (variants and mesh.shape_keys and "Human247_LidCover_Upper" in mesh.attributes
                and "T0::L下眼皮" in mesh.shape_keys.key_blocks):
            continue
        if transaction.owner is not None:
            from ...util.ownership import require_no_external_users, require_owned

            ids = (mesh, mesh.shape_keys)
            require_owned(transaction.owner, ids)
            require_no_external_users(cast(Any, bpy.context.scene), transaction.owner, ids)
        matrix = rig.matrix_world.inverted() @ obj.matrix_world
        for slot in range(7):
            faces = [f for f in mesh.polygons if variants.data[f.index].value == slot]
            _fit_variant(transaction, mesh, matrix, slot, faces)
