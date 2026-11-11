"""Scoped .blend append ownership and plain resource results.

Used by the independent human/inventory importers. Never loads assets on import.
"""

import os
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self, cast
from uuid import uuid4

import bpy
from bpy.types import ID, Collection, Object, Scene, ViewLayer, Window

DATA_KINDS = (
    "collections",
    "objects",
    "meshes",
    "curves",
    "materials",
    "images",
    "armatures",
    "lattices",
    "node_groups",
    "actions",
    "fonts",
    "shape_keys",
    "lights",
    "cameras",
    "textures",
    "volumes",
    "libraries",
    "worlds",
)
ASSET_ID = "oxal_mc_studio_asset_id"
VERSION = "oxal_mc_studio_asset_version"
INSTANCE = "oxal_mc_studio_instance_id"


def instance_creation_id(options: Mapping[str, object]) -> str:
    """A logical creation identity is replayable, but never proves ownership."""
    value = options.get("creation_id")
    if value is None:
        return uuid4().hex
    if (not isinstance(value, str) or len(value) != 32
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError("创建实例标识必须是32位小写十六进制值")
    return value


def prepare_creation_options(options: Mapping[str, object]) -> dict[str, object]:
    """Freeze creation intent before the native Undo operator serializes it."""
    prepared = dict(options)
    prepared["creation_id"] = instance_creation_id(prepared)
    return prepared


@dataclass(frozen=True, slots=True)
class ImportResult:
    asset_id: str
    asset_version: str
    instance_id: str
    collection_name: str
    rig_name: str
    source_reused: bool
    created: bool
    service_ms: float
    library_ms: float
    prepare_ms: float
    update_ms: float


def resolve_scene(uid: int) -> Scene:
    for scene in bpy.data.scenes:
        if scene.session_uid == uid:
            if scene.library or scene.override_library:
                raise ValueError("目标场景不可编辑")
            return scene
    raise ValueError("提交请求的场景已不存在，请重新导入")


def resolve_view_layer(scene: Scene, *, require_active: bool = False) -> ViewLayer:
    """Use the actual native layer; partial context overrides cannot retarget Ops.

    Pure RNA backends may address another Scene explicitly. Context-sensitive
    Operators must refuse a scene/window switch before touching any data.
    """
    layer = cast(ViewLayer | None, bpy.context.view_layer)
    window = cast(Window | None, bpy.context.window)
    if (
        bpy.context.scene == scene
        and layer in tuple(scene.view_layers)
        and (window is None or (window.scene == scene and window.view_layer == layer))
    ):
        return cast(ViewLayer, layer)
    if require_active:
        raise ValueError("目标场景或活动视图层已切换，请切回原场景后重新操作")
    return scene.view_layers[0]


def snapshot_ids() -> set[ID]:
    return {item for kind in DATA_KINDS for item in getattr(bpy.data, kind, ())}


def live_ids(items: Iterable[ID]) -> tuple[ID, ...]:
    """Discard wrappers invalidated when Blender frees an owning data-block.

    Removing a Mesh can also free its Key without a second Python removal.
    An invalid RNA wrapper must never be passed back to native batch_remove.
    This checks only the supplied proven IDs; it does not discover ownership.
    """
    result = []
    for item in tuple(items):
        try:
            if item.as_pointer():
                # as_pointer() alone is not a liveness guarantee on every host.
                _ = item.name
                result.append(item)
        except ReferenceError:
            continue
    return tuple(result)


def _path(value: str) -> str:
    return os.path.normcase(os.path.abspath(bpy.path.abspath(value)))


class AppendTransaction:
    """Append once; claim source-backed IDs and their imported object references.

    New IDs created by other handlers are NOT owned merely because they are new.
    The scene itself and all its existing IDs stay outside this transaction.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.owned: set[ID] = set()
        self._source_libraries: set[ID] = set()
        self._before: set[ID] = set()
        self._committed = False
        self._commit_prepared = False
        from .packed_images import ImagePaths

        self._image_paths = ImagePaths()

    def __enter__(self) -> Self:
        from ..core.runtime import ensure_mutation_allowed

        ensure_mutation_allowed()
        if not self.path.is_file():
            raise FileNotFoundError(f"未找到内置资源：{self.path}")
        self._before = snapshot_ids()
        return self

    def _claim(self) -> None:
        candidates = snapshot_ids() - self._before
        source = _path(str(self.path))
        for item in candidates:
            ref = item.library_weak_reference
            if ref is not None and _path(ref.filepath) == source:
                self.owned.add(item)
        # Native append can retain its source Library as a local data-block.
        # Claim it only alongside actual source-backed IDs from this read;
        # an unrelated new Library is not provenance for this transaction.
        if self.owned:
            libraries = {item for item in candidates
                         if isinstance(item, bpy.types.Library) and _path(item.filepath) == source}
            self._source_libraries.update(libraries)
            self.owned.update(libraries)
        # Objects have no library_weak_reference in Blender 4.5. Establish
        # ownership from source collections and explicit driver/rig references.
        for item in tuple(self.owned):
            if isinstance(item, Collection):
                self.owned.update(obj for obj in item.all_objects if obj in candidates)
        visited: set[ID] = set()
        while todo := self.owned - visited:
            for item in todo:
                visited.add(item)
                referenced = []
                referenced.append(getattr(item, "shape_keys", None))
                if isinstance(item, Object):
                    referenced.extend((item.parent, item.data))
                    if item.pose:
                        referenced.extend(b.custom_shape for b in item.pose.bones)
                    for modifier in item.modifiers:
                        for key in (
                            "object",
                            "target",
                            "mirror_object",
                            "offset_object",
                        ):
                            referenced.append(getattr(modifier, key, None))
                    referenced.extend(
                        getattr(c, "target", None) for c in item.constraints
                    )
                for owner in (item, getattr(item, "node_tree", None)):
                    animation = getattr(owner, "animation_data", None)
                    if animation:
                        referenced.append(animation.action)
                        strips = [strip for track in animation.nla_tracks for strip in track.strips]
                        while strips:
                            strip = strips.pop()
                            referenced.append(strip.action)
                            strips.extend(getattr(strip, "strips", ()))
                        referenced.extend(
                            target.id
                            for curve in animation.drivers
                            for variable in curve.driver.variables
                            for target in variable.targets
                        )
                self.owned.update(
                    ref
                    for ref in referenced
                    if isinstance(ref, ID) and ref in candidates
                )

    def append_collection(self, name: str) -> Collection:
        return self.append_bundle(name)[0]

    def append_bundle(
        self, name: str, *, worlds: Iterable[str] = (), meshes: Iterable[str] = ()
    ) -> tuple[Collection, dict[str, Any], dict[str, Any]]:
        from ..core.runtime import ensure_mutation_allowed

        ensure_mutation_allowed()
        world_names, mesh_names = tuple(worlds), tuple(meshes)
        try:
            context = cast(
                AbstractContextManager[tuple[Any, Any]],
                bpy.data.libraries.load(str(self.path), link=False),
            )
            with context as (source, target):
                if name not in source.collections:
                    raise ValueError(f"内置资源缺少合集：{name}")
                if set(world_names) - set(source.worlds):
                    raise ValueError("内置资源缺少请求的 World")
                if set(mesh_names) - set(source.meshes):
                    raise ValueError("内置资源缺少请求的网格模板")
                target.collections = [name]
                target.worlds = list(world_names)
                target.meshes = list(mesh_names)
            root = cast(Collection | None, target.collections[0])
            if root is None:
                raise ValueError("资源合集追加失败")
        finally:
            # Freeze provenance before preparation/update handlers can add IDs.
            self._claim()
        for image in tuple(self.owned):
            if isinstance(image, bpy.types.Image):
                self._image_paths.prepare(image)
        return (
            root,
            dict(zip(world_names, target.worlds, strict=True)),
            dict(zip(mesh_names, target.meshes, strict=True)),
        )

    def track(self, item: ID) -> None:
        """For an explicit copy/new ID created by this importer only."""
        if item in self._before:
            raise ValueError("不能接管导入前已存在的数据")
        self.owned.add(item)

    def remove(self, items: Iterable[ID]) -> None:
        from ..core.runtime import ensure_mutation_allowed

        ensure_mutation_allowed()
        self.owned = set(live_ids(self.owned))
        requested = set(live_ids(items))
        if not requested <= self.owned:
            raise ValueError("拒绝删除本次导入未拥有的数据")
        # Native parent removal reclaims shape keys. Retire their proof too,
        # and avoid submitting both an owner and its dependent Key for removal.
        dependents = {
            dependent
            for item in requested
            if (dependent := getattr(item, "shape_keys", None)) is not None
        }
        retired = requested | (dependents & self.owned)
        native = tuple(requested - dependents)
        self.owned.difference_update(retired)
        try:
            if native:
                bpy.data.batch_remove(ids=native)
        except Exception:
            self.owned.update(live_ids(retired))
            raise
        finally:
            self.owned = set(live_ids(self.owned))

    def prune_unused(self) -> None:
        for _ in range(12):
            self.owned = set(live_ids(self.owned))
            unused = {
                item
                for item in self.owned
                if item.users == 0 and not item.use_fake_user
            }
            if not unused:
                break
            self.remove(unused)

    def prepare_commit(self, *, image_paths=None) -> None:
        """Finish fallible publication work while prior IDs can still roll back."""
        from .shader_layout import arrange_owned_shaders

        arrange_owned_shaders(live_ids(self.owned))
        if image_paths is not None:
            self._image_paths.transfer_to(image_paths)
        self._commit_prepared = True

    def finish_commit(self) -> None:
        """Pure Python final flag; caller checks preparation before retirement."""
        self._committed = True

    def commit(self, *, image_paths=None) -> None:
        self.prepare_commit(image_paths=image_paths)
        self.finish_commit()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            if not self._committed:
                # Publication may retire registry references before failing.
                # Retain the read's proven native Library IDs independently;
                # Blender's weak-source cache has special user-count rules.
                self.owned.update(live_ids(self._source_libraries))
                self.remove(live_ids(self.owned))
        finally:
            if not self._committed:
                self._image_paths.rollback()
            self._before.clear()
            self._source_libraries.clear()
            self.owned.clear()
