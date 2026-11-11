"""Restore the two immutable 193 World images through explicit transactions."""

import hashlib
import json
from contextlib import suppress
from pathlib import Path

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import require_owned
from ...util.packed_images import ImagePaths
from ...util.resources import live_ids

ASSET_FOLDER = Path(__file__).resolve().parents[2] / "asset/sky"


def _digest(image):
    if image.library or image.override_library or image.is_dirty or image.source != "FILE" or len(image.packed_files) != 1:
        raise ValueError("3D 日月图片已自定义、只读或未打包，未替换用户图像")
    return hashlib.sha256(image.packed_file.data).hexdigest()


def _uv_role(node, controller):
    """Separate Sun projection from the Moon's native eight-phase atlas."""
    from .sky import _native_expression
    from .sky_repair import matches

    links = tuple(node.inputs["Vector"].links)
    if len(links) != 1:
        raise ValueError("3D 日月图片坐标已自定义，未替换用户 World")
    source = links[0].from_node
    if source.type == "COMBXYZ":
        return "sun"
    if (source.type == "MAPPING" and source.vector_type == "POINT"
            and tuple(source.inputs["Scale"].default_value) == (.25, .5, 1.)
            and tuple(source.inputs["Rotation"].default_value) == (0., 0., 0.) and all(
        any(matches(node.id_data, source.inputs[1].path_from_id("default_value"), formula,
                    [("phase", controller, '["月相"]')], index)
            for formula in {expression, _native_expression(expression)})
        for index, expression in enumerate(("(phase%4)*0.25", "0.5 if phase>=4 else 0.0"))
    )):
        return "moon"
    raise ValueError("3D 日月图片坐标或月相绑定已自定义，未替换用户 World")


def plan(world, controller, *, root=None, scene=None):
    """Prove exact World selectors and packed image identities without writes."""
    from . import sky
    from .sky_repair import world_contract

    switch, _images = world_contract(world, controller)
    descriptions = json.loads((ASSET_FOLDER / "SOURCE.json").read_text("utf8"))["celestial193"]["images"]
    known = {digest: item for item in descriptions for digest in (item["sha256"], item["legacy_packed_sha256"])}
    nodes = {}
    for link in switch.outputs[0].links:
        if link.to_socket.identifier != "Fac":
            raise ValueError("3D 日月开关存在其它用户连接，未替换 World")
        mixes = tuple(link.to_node.inputs["Color2"].links)
        if len(mixes) != 1 or mixes[0].from_node.type != "TEX_IMAGE":
            raise ValueError("3D 日月选择分支已自定义，未替换用户 World")
        node = mixes[0].from_node
        nodes.setdefault(node, set()).add(mixes[0].from_socket.identifier)
    if len(nodes) != 2 or any(sockets != {"Color", "Alpha"} for sockets in nodes.values()):
        raise ValueError("3D 日月颜色／透明度图片引用不唯一，未替换用户 World")
    if root is not None:
        require_owned(root, (world, controller, *(node.image for node in nodes)))
        domain = sky._dependency_domain(root)
        domain.add(scene)
        if any(user not in domain for user in bpy.data.user_map(subset=(world,)).get(world, ())):
            raise ValueError("天空 World 被其它数据共享，未改变其它场景贴图")
    result, roles = [], set()
    for node in nodes:
        digest = _digest(node.image)
        description = known.get(digest)
        if description is None or description["role"] in roles or description["role"] != _uv_role(node, controller):
            raise ValueError("3D 日月图片不是已知 Beta 原图，未覆盖自定义图片")
        roles.add(description["role"])
        if digest == description["sha256"]:
            if list(node.image.size) != description["size"]:
                raise ValueError("3D 日月图片尺寸与来源不符，未替换")
            continue
        result.append((node, node.image, description))
    if roles != {"sun", "moon"}:
        raise ValueError("3D 日月图片角色不完整，未替换用户 World")
    return result


class Replacement:
    """Own only freshly loaded images and their portable packed-path journal."""

    def __init__(self, changes):
        self.changes = changes
        self.images = []
        self.paths = ImagePaths()

    def apply(self):
        ensure_mutation_allowed()
        prepared = []
        try:
            for node, previous, description in self.changes:
                path = ASSET_FOLDER / description["path"]
                if not path.resolve().is_relative_to(ASSET_FOLDER.resolve()) or not path.is_file():
                    raise FileNotFoundError("193 原始 3D 日月图片未随包提供")
                if hashlib.sha256(path.read_bytes()).hexdigest() != description["sha256"]:
                    raise ValueError("193 原始 3D 日月图片校验失败，未修改 World")
                image = bpy.data.images.load(str(path), check_existing=False)
                self.images.append(image)
                image.name = "OXAL · 193 3D " + description["role"]
                if image.colorspace_settings is None:
                    raise ValueError("193 3D 日月图片缺少原生颜色空间设置")
                image.colorspace_settings.name = description["colorspace"]
                image.alpha_mode = description["alpha_mode"]
                image.pack()
                self.paths.prepare(image, key=description["logical_key"])
                if _digest(image) != description["sha256"] or list(image.size) != description["size"]:
                    raise ValueError("193 原始 3D 日月图片未正确解码或打包")
                prepared.append((node, previous, image))
            for node, _previous, image in prepared:
                node.image = image
            return self
        except Exception:
            self.rollback()
            raise

    def rollback(self):
        for node, previous, _description in self.changes:
            with suppress(ReferenceError):
                node.image = previous
        images = live_ids(self.images)
        if images:
            bpy.data.batch_remove(ids=images)
        self.paths.rollback()
        self.images.clear()
