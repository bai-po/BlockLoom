"""Portable packed image paths, prepared only during an explicit import.

Blender stores Image.filepath and every ImagePackedFile.filepath separately.
Native pack-all reads the latter even when packed pixels already exist. Keep
both paths valid without re-reading a removed development resource directory.
"""

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import bpy

from ..core.runtime import ensure_mutation_allowed

TEXTURE_KEY = "oxal_mc_studio_texture_key"
SOURCE_PATH = "oxal_mc_studio_image_source_path"
CACHE_PATH = "oxal_mc_studio_image_cache_path"
_LIBRARY = Path(__file__).resolve().parents[1] / "asset/library"


def semantic_filepath(image) -> str:
    """Ignore only our proven cache relocation, preserving real user path edits."""
    current = str(image.filepath)
    cached = str(image.get(CACHE_PATH, ""))
    if cached and SOURCE_PATH in image:
        current_path = os.path.normcase(os.path.abspath(bpy.path.abspath(current, library=image.library)))
        cached_path = os.path.normcase(os.path.abspath(bpy.path.abspath(cached)))
        if current_path == cached_path:
            return str(image[SOURCE_PATH])
    return current


def _logical_key(image) -> str:
    for raw in (image.get(TEXTURE_KEY, ""), image.get("oxal_resource_path164", ""), image.filepath):
        value = str(raw).replace("\\", "/")
        if value.startswith("assets/") and "/textures/" in value:
            namespace, relative = value[7:].split("/textures/", 1)
            return namespace + ":" + relative.removesuffix(".png")
        if "/assets/" in value and "/textures/" in value:
            namespace, relative = value.rsplit("/assets/", 1)[1].split("/textures/", 1)
            return namespace + ":" + relative.removesuffix(".png")
        if ":" in value and not Path(value).is_absolute() and not value.startswith("//"):
            namespace, relative = value.split(":", 1)
            if namespace and relative and not relative.startswith(("/", "\\")):
                return namespace + ":" + relative.removesuffix(".png")
    return ""


def canonical_texture(image, key="", *, required=False) -> Path | None:
    logical = str(key or _logical_key(image))
    if not logical:
        return None
    namespace, separator, relative = logical.partition(":")
    if not separator or not namespace or not relative or "\\" in logical:
        raise ValueError("打包纹理的逻辑身份无效")
    path = _LIBRARY / "assets" / namespace / "textures" / (relative + ".png")
    if not path.resolve().is_relative_to((_LIBRARY / "assets").resolve()):
        raise ValueError("打包纹理路径越界")
    if not path.is_file():
        if required:
            raise FileNotFoundError("内置纹理未随包提供：" + logical)
        return None
    return path


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _suffix(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"v/1\x01"):
        return ".exr"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and data[12:16] in (b"VP8 ", b"VP8L", b"VP8X"):
        return ".webp"
    raise ValueError("内置打包纹理格式无法确认，未生成错误缓存")


@dataclass
class _ImageState:
    image: Any
    filepath: str
    packed_paths: tuple[str, ...]
    properties: dict[str, tuple[bool, Any]]
    was_packed: bool
    packed_here: bool = False


class ImagePaths:
    """One append's path journal; new cache bytes have content-addressed names."""

    def __init__(self):
        self.created: list[tuple[Path, str]] = []
        self.directories: list[Path] = []
        self.changed: list[_ImageState] = []
        self.images: set[tuple[int, int]] = set()
        self.boundary: Path | None = None

    def _cache_root(self) -> Path:
        base = Path(bpy.utils.user_resource("DATAFILES", create=True))
        folder = base / "oxal_mc_studio/packed_textures"
        for path in (base / "oxal_mc_studio", folder):
            if path.exists() and (path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError("打包纹理缓存目录含链接，未写入源目录之外")
        return folder

    def _publish(self, path: Path, data: bytes, digest: str) -> None:
        boundary = self._cache_root().resolve()
        if self.boundary is None:
            self.boundary = boundary
        elif boundary != self.boundary:
            raise ValueError("打包纹理缓存目录在操作期间发生变化")
        if not path.resolve().is_relative_to(boundary):
            raise ValueError("打包纹理缓存路径越界")
        if path.exists():
            if not path.is_file() or path.is_symlink() or _digest(path) != digest:
                raise ValueError("打包纹理缓存已有不同内容，未覆盖：" + str(path))
            return
        missing = []
        parent = path.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            try:
                directory.mkdir()
                self.directories.append(directory)
            except FileExistsError:
                if not directory.is_dir():
                    raise
        temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
            try:
                if os.name == "nt":
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
                self.created.append((path, digest))
            except FileExistsError:
                if not path.is_file() or _digest(path) != digest:
                    raise ValueError("打包纹理缓存被并发修改，未覆盖") from None
        finally:
            temporary.unlink(missing_ok=True)

    def prepare(self, image, *, key="", strict_builtin=False) -> None:
        ensure_mutation_allowed()
        if image.source not in {"FILE", "TILED"}:
            return
        if image.is_dirty:
            raise ValueError("图片包含未保存的像素编辑，先保存这些编辑再修复打包路径：" + image.name)
        identity = (image.as_pointer(), image.session_uid)
        if identity in self.images:
            return
        self.images.add(identity)
        semantic = semantic_filepath(image)
        properties = {name: (name in image, image.get(name)) for name in (SOURCE_PATH, CACHE_PATH, TEXTURE_KEY)}
        saved = _ImageState(image, image.filepath, tuple(part.filepath for part in image.packed_files), properties, bool(image.packed_files))
        self.changed.append(saved)
        original = canonical_texture(image, key, required=strict_builtin)
        packed = tuple(image.packed_files)
        if not packed:
            supplied = Path(bpy.path.abspath(image.filepath, library=image.library))
            def exists(path):
                if image.source == "TILED" and "<UDIM>" in str(path):
                    return bool(image.tiles) and all(Path(str(path).replace("<UDIM>", str(tile.number))).is_file() for tile in image.tiles)
                return path.is_file()
            path = supplied if exists(supplied) else original
            if path is None or not exists(path):
                raise FileNotFoundError("纹理既无打包像素也无有效源文件：" + str(path))
            image.filepath_raw = str(path)
            # Packing does not need to reload or replace the decoded buffers.
            # Reload would lose the original pixels if the source changed.
            saved.packed_here = True
            image.pack()
            packed = tuple(image.packed_files)
        if not packed or any(part.packed_file is None or part.packed_file.size < 1 for part in packed):
            raise ValueError("内置纹理打包数据为空：" + image.name)
        logical = str(key or _logical_key(image))
        entries = [(part, bytes(part.packed_file.data)) for part in packed]
        if strict_builtin and original is not None and len(entries) == 1 and entries[0][1] != original.read_bytes():
            raise ValueError("内置纹理逻辑身份与打包内容不一致，未覆盖像素：" + logical)
        digests = [hashlib.sha256(data).hexdigest() for _part, data in entries]
        suffixes = [_suffix(data) for _part, data in entries]
        if image.source == "TILED":
            if len({part.view for part, _data in entries}) != 1 or len(set(suffixes)) != 1:
                raise ValueError("无法确认多视图 UDIM 纹理缓存契约")
            family = hashlib.sha256("\n".join(f"{part.tile_number}:{digest}" for (part, _data), digest in zip(entries, digests, strict=True)).encode()).hexdigest()
            directory = self._cache_root() / family
            paths = tuple(directory / f"tile.{part.tile_number}{suffixes[0]}" for part, _data in entries)
            template = directory / ("tile.<UDIM>" + suffixes[0])
        elif len(entries) == 1:
            paths = (self._cache_root() / (digests[0] + suffixes[0]),)
            template = paths[0]
        else:
            raise ValueError("无法确认多视图 FILE 纹理缓存契约")
        for destination, (_part, data), digest in zip(paths, entries, digests, strict=True):
            self._publish(destination, data, digest)
        for part, path in zip(packed, paths, strict=True):
            part.filepath = str(path)
        # filepath's RNA update tries to repack the *previous* packed paths.
        # filepath_raw changes the path without that refresh or filesystem IO.
        image.filepath_raw = str(template)
        image[SOURCE_PATH] = semantic
        image[CACHE_PATH] = str(template)
        if logical:
            image[TEXTURE_KEY] = logical
        if min(image.size) < 1:
            raise ValueError("内置打包纹理无法解码：" + image.name)

    def transfer_to(self, target: "ImagePaths") -> None:
        """Move the byte/path rollback journal with explicitly transferred IDs."""
        ensure_mutation_allowed()
        if target is self or self.images & target.images:
            raise ValueError("不能重复转交打包纹理事务")
        if self.boundary is not None and target.boundary not in (None, self.boundary):
            raise ValueError("不能合并不同缓存目录的打包纹理事务")
        target.created.extend(self.created)
        target.directories.extend(self.directories)
        target.changed.extend(self.changed)
        target.images.update(self.images)
        target.boundary = target.boundary or self.boundary
        self.created.clear()
        self.directories.clear()
        self.changed.clear()
        self.images.clear()
        self.boundary = None

    def rollback(self) -> None:
        for saved in reversed(self.changed):
            image = saved.image
            try:
                if saved.packed_here and not saved.was_packed and image.packed_files:
                    # REMOVE only drops embedded bytes; it does not write files
                    # or reload/replace the image's current decoded pixels.
                    image.unpack(method="REMOVE")
                for part, old in zip(image.packed_files, saved.packed_paths, strict=False):
                    part.filepath = old
                image.filepath_raw = saved.filepath
                for name, (existed, value) in saved.properties.items():
                    if existed:
                        image[name] = value
                    elif name in image:
                        del image[name]
            except ReferenceError:
                pass
        used = set()
        for image in bpy.data.images:
            used.add(str(Path(bpy.path.abspath(image.filepath, library=image.library)).resolve()))
            used.update(str(Path(bpy.path.abspath(part.filepath, library=image.library)).resolve()) for part in image.packed_files)
            if image.source == "TILED":
                used.update(str(Path(bpy.path.abspath(image.filepath.replace("<UDIM>", str(tile.number)), library=image.library)).resolve()) for tile in image.tiles)
        for path, digest in reversed(self.created):
            if (self.boundary is not None and path.resolve().is_relative_to(self.boundary)
                    and str(path.resolve()) not in used and path.is_file() and not path.is_symlink()
                    and _digest(path) == digest):
                path.unlink()
        for directory in reversed(self.directories):
            if (directory.is_dir() and not directory.is_symlink()
                    and not getattr(directory.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    and not any(directory.iterdir())):
                directory.rmdir()
        self.changed.clear()
        self.created.clear()
        self.directories.clear()
        self.images.clear()
        self.boundary = None


def rebase_native_image(image, key: str) -> Path:
    """Rebase one proven, private appended image to its bundled official PNG."""
    journal = ImagePaths()
    try:
        journal.prepare(image, key=key, strict_builtin=True)
        return cast(Path, canonical_texture(image, key, required=True))
    except Exception:
        journal.rollback()
        raise
