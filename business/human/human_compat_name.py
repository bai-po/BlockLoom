"""Explicit, source-backed recovery of a missing legacy name plate.

oxal.human.legacy_name / 1; NEW_INSTANCE for the selected native rig only.
No old callback, source armature, scene or selection observer is appended.
"""

import hashlib
import json
import re
from typing import Any

import bpy
from mathutils import Matrix

from ...core.runtime import ensure_mutation_allowed
from ...util.geometry_nodes import input_path
from ...util.resources import AppendTransaction, live_ids
from .human_source import DATA
from .human_state import SOURCE_NAME

SCHEMA = "oxal_legacy_name_schema"
RIG = "oxal_legacy_name_rig"
VERSION = "oxal_legacy_name_version"
FACE_CAMERA = "oxal_legacy_name_face_camera"
_catalog = None


def prepare_catalog():
    global _catalog
    document = json.loads((DATA / "COMPAT_NAMES.json").read_text(encoding="utf8"))
    if document.get("schema") != 1 or document.get("resource") != "oxal_compat_names.blend":
        raise ValueError("旧人模名字资源声明无效")
    _catalog = document


def clear_catalog():
    global _catalog
    _catalog = None


def available(version):
    return _catalog is not None and version in _catalog["versions"]


def _close(first, second):
    return all(abs(float(a) - float(b)) <= 2e-5
               for row_a, row_b in zip(first, second, strict=True)
               for a, b in zip(row_a, row_b, strict=True))


def _container(target):
    candidates = [collection for collection in target.containers
                  if collection.library is None and collection.override_library is None
                  and {obj for obj in collection.all_objects if obj.type == "ARMATURE"} == {target.rig}]
    if len(candidates) != 1:
        raise ValueError("旧人模没有唯一私有实例合集，未创建名字")
    return candidates[0]


def _name_role(obj):
    name = str(obj.get(SOURCE_NAME, obj.name))
    while name != "名字":
        stem, separator, number = name.rpartition(".")
        if not separator or not number.isdigit():
            return False
        name = stem
    return True


def _driver_path(obj, record):
    """Resolve this source-declared bool input through current native RNA."""
    match = re.fullmatch(r'modifiers\[("(?:[^"\\]|\\.)*")\]\[("(?:[^"\\]|\\.)*")\]', record["path"])
    if match is None:
        raise ValueError("内置名字原生朝向输入声明无效")
    modifier_name, identifier = (json.loads(value) for value in match.groups())
    modifier = obj.modifiers.get(modifier_name)
    if modifier is None or modifier.type != "NODES" or modifier.node_group is None:
        raise ValueError("内置名字原生朝向修改器缺失")
    interface = modifier.node_group.interface
    sockets = [item for item in interface.items_tree
               if getattr(item, "item_type", "") == "SOCKET"
               and getattr(item, "in_out", "") == "INPUT"
               and getattr(item, "identifier", "") == identifier
               and getattr(item, "socket_type", "") == "NodeSocketBool"] if interface else []
    if len(sockets) != 1:
        raise ValueError("内置名字原生朝向布尔输入已改变")
    return input_path(modifier, identifier)


def ensure(transaction, scene, target):
    """Return a proven existing plate or append one native FONT transactionally."""
    ensure_mutation_allowed()
    if transaction.owner is not None or not available(target.version):
        raise ValueError("该旧人模没有可用的原生名字资源")
    rig = target.rig
    if not target.editable or scene.objects.get(rig.name) != rig:
        raise ValueError("旧人模目标已改变或不可编辑")
    if rig.library or rig.override_library or rig.data.library or rig.data.override_library:
        raise ValueError("旧人模骨架来自只读关联数据，未创建名字")
    assert _catalog is not None
    profile, template = _catalog["versions"][target.version], _catalog["template"]
    bone = rig.data.bones.get("头")
    rest = profile["head_rest"]
    if (bone is None or (bone.parent.name if bone.parent else "") != rest["parent"]
            or not _close(bone.matrix_local, rest["matrix"])):
        raise ValueError("旧人模头部休止绑定已改变，未创建名字")
    container = _container(target)
    fonts = [obj for obj in container.all_objects if obj.type == "FONT"]
    bound = [obj for obj in scene.objects if obj.type == "FONT" and any(c.type == "CHILD_OF" and c.target == rig
                                        and c.subtarget == "头" for c in obj.constraints)]
    if bound:
        if len(bound) != 1:
            raise ValueError("旧人模存在多份头部文字，未重复创建名字")
        obj = bound[0]
        if not _name_role(obj) and not (obj.get(SCHEMA) == 1 and obj.get(RIG) == rig):
            raise ValueError("旧人模已有未知头部文字，未将其当作名字")
        from .human_compat import _rigs

        if (_rigs(obj) != {rig} or obj.library or obj.override_library
                or tuple(obj.users_scene) != (scene,)):
            raise ValueError("旧名字存在其它骨架或场景引用，未接管")
        return obj
    if any(_name_role(obj) for obj in fonts):
        raise ValueError("旧名字的原生绑定已改变，未重复创建")
    source = DATA / _catalog["resource"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != _catalog["resource_sha256"]:
        raise ValueError("旧名字资源哈希与声明不符，未创建")
    source_rig, target_rig = Matrix(template["rig_matrix"]), rig.matrix_world.copy()
    if abs(target_rig.determinant()) < 1e-12:
        raise ValueError("人模缩放不可逆，未创建名字")
    with AppendTransaction(source) as appended:
        staging = appended.append_collection(template["collection"])
        objects = tuple(staging.all_objects)
        if len(objects) != 1 or objects[0].type != "FONT" or objects[0] not in appended.owned:
            raise ValueError("内置名字资源对象契约损坏")
        obj = objects[0]
        if obj.parent is not None or len(obj.constraints) != 1:
            raise ValueError("内置名字资源绑定契约损坏")
        constraint = obj.constraints.get(template["constraint"])
        if (constraint is None or constraint.type != "CHILD_OF" or constraint.target is not None
                or constraint.subtarget != "头"):
            raise ValueError("内置名字资源头部约束损坏")
        # Child Of evaluates T * inverse * basis. Transform both factors so
        # the authored head-local placement survives target rotation/scale.
        obj.matrix_basis = target_rig @ source_rig.inverted() @ Matrix(template["basis"])
        constraint.inverse_matrix = Matrix(template["inverse"]) @ source_rig @ target_rig.inverted()
        constraint.target = rig
        drivers = tuple(obj.animation_data.drivers) if obj.animation_data else ()
        if len(drivers) != len(template["drivers"]):
            raise ValueError("内置名字原生朝向驱动契约损坏")
        for record in template["drivers"]:
            native_path = _driver_path(obj, record)
            curves = [curve for curve in drivers if curve.data_path == native_path
                      and curve.array_index == record["index"]]
            driver = curves[0].driver if len(curves) == 1 else None
            if (driver is None or driver.expression != record["expression"]
                    or len(driver.variables) != 1):
                raise ValueError("内置名字原生朝向驱动已改变")
            variable = driver.variables[0]
            if variable.type != "SINGLE_PROP" or variable.targets[0].id is not None:
                raise ValueError("内置名字原生朝向来源无效")
            if "名字朝向镜头" in rig.pose.bones["根"]:
                variable.targets[0].id = rig
                variable.targets[0].data_path = record["source_path"]
            else:
                obj[FACE_CAMERA] = template["face_camera_default"]
                ui = obj.id_properties_ui(FACE_CAMERA)
                if ui is not None:
                    ui.update(description="沿用原生名字朝向镜头节点")
                variable.targets[0].id = obj
                variable.targets[0].data_path = '["' + FACE_CAMERA + '"]'
        obj.name = "名字"
        obj[SCHEMA], obj[RIG], obj[VERSION] = 1, rig, target.version
        obj.hide_viewport = obj.hide_render = False
        for collection in tuple(obj.users_collection):
            collection.objects.unlink(obj)
        container.objects.link(obj)
        retained: set[Any] = {obj}
        users = bpy.data.user_map(subset=tuple(appended.owned))
        while True:
            dependencies = {item for item, consumers in users.items()
                            if any(consumer in retained for consumer in consumers)}
            if dependencies <= retained:
                break
            retained.update(dependencies)
        appended.remove(set(appended.owned) - retained)
        appended.prepare_commit(image_paths=transaction.image_paths)
        for item in live_ids(appended.owned):
            transaction.track(item)
        appended.finish_commit()
    return obj
