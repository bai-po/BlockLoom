"""Personal libraries outlive Blender's removal of extension-managed data.

Only explicit Tools call this module. Enabling, drawing, replying and disabling
an extension never copy files. A legacy library is copied without removing its
originals; conflicting destinations refuse the copy before any file is written.
"""

import hashlib
import os
import shutil
import stat
from pathlib import Path
from uuid import uuid4

import bpy

from ..core.runtime import ensure_mutation_allowed

_migrated: set[tuple[str, str]] = set()


def reset_cache() -> None:
    """Registration bookkeeping only; a re-enable rechecks the old library."""
    _migrated.clear()


def _reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _files(directory: Path) -> list[Path]:
    pending, result = [directory], []
    while pending:
        current = pending.pop()
        if _reparse(current):
            raise ValueError("旧个人库含链接或重解析目录，未复制或删除原件")
        with os.scandir(current) as entries:
            for entry in entries:
                path = Path(entry.path)
                if _reparse(path):
                    raise ValueError("旧个人库含链接或重解析文件，未复制或删除原件")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    result.append(path)
                else:
                    raise ValueError("旧个人库包含非常规文件，未复制或删除原件")
    return sorted(result)


def _safe_target(path: Path, directory: Path) -> None:
    boundary = directory.resolve()
    if not path.resolve().is_relative_to(boundary):
        raise ValueError("个人库目标路径越界，未复制或覆盖文件")
    for item in (path, *path.parents):
        if item.exists() and _reparse(item):
            raise ValueError("个人库目标含链接或重解析目录，未复制或覆盖文件")
        if item == directory:
            break


def copy_legacy(source: Path, target: Path) -> int:
    """Copy one precisely selected legacy library, with per-file rollback."""
    ensure_mutation_allowed()
    if not source.exists():
        return 0
    if _reparse(source) or not source.is_dir():
        raise ValueError("旧个人库目录无法安全核验，原件未改动")
    if source.resolve() == target.resolve():
        return 0
    plan = []
    for original in _files(source):
        destination = target / original.relative_to(source)
        _safe_target(destination, target)
        digest = _digest(original)
        if destination.exists():
            if not destination.is_file() or _digest(destination) != digest:
                raise ValueError(f"个人库文件冲突：{original} → {destination}；未覆盖或删除原件")
        else:
            plan.append((original, destination, digest))
    created_files, created_directories = [], []
    try:
        for original, destination, digest in plan:
            _safe_target(destination, target)
            missing = []
            parent = destination.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_directories.append(directory)
            temporary = destination.with_name(destination.name + ".copy-" + uuid4().hex)
            try:
                shutil.copyfile(original, temporary)
                if _digest(temporary) != digest or _digest(original) != digest:
                    raise ValueError("复制期间旧个人库发生变化，未发布新副本")
                # Publish atomically without replacing a concurrently added file.
                # Windows rename refuses existing destinations; POSIX link does too.
                if os.name == "nt":
                    os.rename(temporary, destination)
                else:
                    os.link(temporary, destination)
                created_files.append((destination, digest))
                if _digest(destination) != digest:
                    raise ValueError("个人库副本校验失败，原件未改动")
            finally:
                temporary.unlink(missing_ok=True)
    except Exception:
        for path, digest in reversed(created_files):
            _safe_target(path, target)
            if path.is_file() and _digest(path) == digest:
                path.unlink()
        for directory in reversed(created_directories):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        raise
    return len(created_files)


def personal_directory(relative: str, override_name: str) -> Path:
    """Resolve a Tool's persistent library and protect this package's old copy."""
    ensure_mutation_allowed()
    if relative not in {"library", "human"}:
        raise ValueError("未知个人库目录")
    override = os.environ.get(override_name)
    if override:
        directory = Path(override)
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    directory = Path(bpy.utils.user_resource("DATAFILES")) / "oxal_mc_studio" / relative
    package = (__package__ or "").removesuffix(".util")
    if package.startswith("bl_ext."):
        legacy = Path(bpy.utils.extension_path_user(package, path=relative, create=False))
        identity = str(legacy), str(directory)
        if identity not in _migrated:
            copy_legacy(legacy, directory)
            _migrated.add(identity)
    directory.mkdir(parents=True, exist_ok=True)
    return directory
