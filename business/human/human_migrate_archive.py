"""Explicit legacy-human collection archive with reversible layer state.

The archive contains existing user data and never joins the new human's owned
data registry. Its saved journal is passive metadata for explicit restoration.
"""

from dataclasses import dataclass, field
from typing import Any

import bpy

JOURNAL = "oxal_mc_studio_human_migration_archive"
_FLAGS = ("exclude", "hide_viewport", "holdout", "indirect_only")


@dataclass(frozen=True)
class _LayerState:
    view_layer: str
    path: tuple[Any, ...]
    flags: tuple[bool, ...]


@dataclass(frozen=True)
class _BaseState:
    view_layer: str
    obj: Any
    hidden: bool


def _walk(layer: Any, path: tuple[Any, ...] = ()) -> Any:
    current = (*path, layer.collection)
    yield current, layer
    for child in layer.children:
        yield from _walk(child, current)


def _parents(collection: Any) -> tuple[Any, ...]:
    candidates = (*bpy.data.collections, *(scene.collection for scene in bpy.data.scenes))
    return tuple(parent for parent in candidates if collection in tuple(parent.children))


def _layer(scene: Any, view_name: str, path: tuple[Any, ...]) -> Any:
    view = scene.view_layers.get(view_name)
    if view is None:
        raise ValueError("归档前的视图层已变更，无法恢复原集合状态")
    layer = view.layer_collection
    if not path or layer.collection != path[0]:
        raise ValueError("归档场景根已变更，无法恢复原集合路径")
    for collection in path[1:]:
        matches = [child for child in layer.children if child.collection == collection]
        if len(matches) != 1:
            raise ValueError("归档前的实际集合路径已变更，无法恢复")
        layer = matches[0]
    return layer


def _validate(scene: Any, root: Any, rig: Any, objects: tuple[Any, ...]) -> tuple[Any, ...]:
    if root.library or root.override_library:
        raise ValueError("链接旧人模不可归档")
    domain = (root, *root.children_recursive)
    if root not in tuple(scene.collection.children_recursive):
        raise ValueError("旧人模根不属于目标场景")
    scenes = [item for item in bpy.data.scenes if root in tuple(item.collection.children_recursive)]
    if scenes != [scene]:
        raise ValueError("旧人模根被多个场景共享，不可归档")
    parents = _parents(root)
    if len(parents) != 1 or parents[0] not in (scene.collection, *scene.collection.children_recursive):
        raise ValueError("旧人模根必须只有一个场景挂载父合集")
    if any(collection.library or collection.override_library for collection in domain):
        raise ValueError("旧人模包含链接子合集，不可归档")
    if any(parent not in domain for collection in domain[1:] for parent in _parents(collection)):
        raise ValueError("旧人模子合集存在归档范围外的父合集")
    if len(set(objects)) != len(objects) or set(objects) != set(root.all_objects) or rig not in objects:
        raise ValueError("旧人模归档对象与已确认范围不一致")
    if rig.parent in objects:
        raise ValueError("旧人模主骨架的父对象也在归档范围内，不可迁移")
    if any(collection not in domain for obj in objects for collection in obj.users_collection):
        raise ValueError("旧人模对象存在归档范围外的合集链接")
    return parents


@dataclass
class ArchivePlan:
    """An operation-local journal; do not carry it across file loads or Undo."""

    scene: Any
    root: Any
    rig: Any
    objects: tuple[Any, ...]
    parents: tuple[Any, ...]
    layers: tuple[_LayerState, ...]
    bases: tuple[_BaseState, ...] = ()
    archive: Any = field(default=None, init=False)
    _new_root: Any = field(default=None, init=False, repr=False)
    _metadata_reserved: bool = field(default=False, init=False, repr=False)
    _applied: bool = field(default=False, init=False, repr=False)

    def _restore_layers(self, archived: bool) -> None:
        for state in sorted(self.layers, key=lambda item: len(item.path)):
            path = state.path
            if archived:
                start = path.index(self.root)
                path = (self.scene.collection, self.archive, *path[start:])
            for name, value in zip(_FLAGS, state.flags, strict=True):
                # Collection link edits and exclude setters may resynchronize
                # the layer tree. Resolve every write from actual Collection IDs.
                setattr(_layer(self.scene, state.view_layer, path), name, value)

    def _restore_bases(self) -> None:
        """Restore native per-view-layer hide bits after the old tree is back.

        Excluding a collection frees its Bases; hide_get() then returns False,
        and unexcluding creates new Bases without their former local hide bits.
        Object viewport/render flags and native drivers are separate data.
        """
        for view in self.scene.view_layers:
            states = tuple(state for state in self.bases if state.view_layer == view.name)
            if not states:
                continue
            view.update()
            objects = set(view.objects)
            if any(state.obj not in objects for state in states):
                raise ValueError("旧人模原有视图层对象状态无法恢复")
            for state in states:
                state.obj.hide_set(state.hidden, view_layer=view)

    def _journal(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "scene": self.scene,
            "archive": self.archive,
            "old_roots": {"0": self.root},
            "parents": {str(index): parent for index, parent in enumerate(self.parents)
                        if parent != self.scene.collection},
            "parent_is_scene_master": self.parents == (self.scene.collection,),
            "layers": {
                str(index): {
                    "view_layer": state.view_layer,
                    # The Scene reference identifies its embedded master.
                    "path": {str(depth): collection for depth, collection in enumerate(state.path[1:])},
                    **dict(zip(_FLAGS, state.flags, strict=True)),
                }
                for index, state in enumerate(self.layers)
            },
            "bases": {
                str(index): {"view_layer": state.view_layer, "object": state.obj,
                             "hidden": state.hidden}
                for index, state in enumerate(self.bases)
            },
        }

    def apply(self, new_root: Any) -> None:
        """Reparent only the proven old root, then exclude the archive last."""
        if self.archive is not None or self._applied:
            raise ValueError("旧人模归档计划不可重复执行")
        if _validate(self.scene, self.root, self.rig, self.objects) != self.parents:
            raise ValueError("旧人模原父合集已变更，不可归档")
        if new_root == self.root or JOURNAL in new_root:
            raise ValueError("新实例已存在旧人模归档记录")
        self._new_root = new_root
        try:
            self.archive = bpy.data.collections.new("旧人模")
            self.scene.collection.children.link(self.archive)
            self.archive.children.link(self.root)
            for parent in self.parents:
                parent.children.unlink(self.root)
            self._restore_layers(archived=True)
            self._metadata_reserved = True
            new_root[JOURNAL] = self._journal()
            # Native exclude recursively overwrites descendant exclude flags.
            # Restoring a descendant afterwards would make it visible again.
            for view in self.scene.view_layers:
                _layer(self.scene, view.name, (self.scene.collection, self.archive)).exclude = True
            self._applied = True
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        """Restore old links and flags; delete only our empty new archive."""
        archive = self.archive
        if archive is None:
            return
        for view in self.scene.view_layers:
            paths = [path for path, layer in _walk(view.layer_collection) if layer.collection == archive]
            for path in paths:
                _layer(self.scene, view.name, path).exclude = False
        for parent in self.parents:
            if self.root not in tuple(parent.children):
                parent.children.link(self.root)
        if self.root in tuple(archive.children):
            archive.children.unlink(self.root)
        self._restore_layers(archived=False)
        self._restore_bases()
        if self._metadata_reserved:
            # AppendTransaction may have removed the new instance before the
            # outer failure handler gets to this old-data rollback journal.
            try:
                has_metadata = JOURNAL in self._new_root
            except ReferenceError:
                has_metadata = False
            if has_metadata:
                del self._new_root[JOURNAL]
            self._metadata_reserved = False
        if archive.objects or archive.children:
            raise ValueError("本次归档合集出现额外内容，已保留以避免删除用户数据")
        parents = _parents(archive)
        if any(parent != self.scene.collection for parent in parents):
            raise ValueError("本次归档合集出现额外链接，已保留以避免删除用户数据")
        if self.scene.collection in parents:
            self.scene.collection.children.unlink(archive)
        if archive.users:
            raise ValueError("本次归档合集出现额外引用，已保留以避免删除用户数据")
        bpy.data.collections.remove(archive)
        self.archive = None
        self._new_root = None
        self._applied = False


def plan(scene: Any, root: Any, rig: Any, objects: Any) -> ArchivePlan:
    """Validate an exact caller-proven object allowlist and capture layer paths."""
    members = tuple(objects)
    parents = _validate(scene, root, rig, members)
    layers = tuple(
        _LayerState(view.name, path, tuple(bool(getattr(layer, name)) for name in _FLAGS))
        for view in scene.view_layers
        for path, layer in _walk(view.layer_collection)
        if root in path
    )
    if not layers or any(not any(item.view_layer == view.name for item in layers) for view in scene.view_layers):
        raise ValueError("旧人模在视图层中缺少可恢复的实际集合路径")
    bases = []
    for view in scene.view_layers:
        in_view = set(view.objects)
        bases.extend(_BaseState(view.name, obj, bool(obj.hide_get(view_layer=view)))
                     for obj in members if obj in in_view)
    return ArchivePlan(scene, root, rig, members, parents, layers, tuple(bases))
