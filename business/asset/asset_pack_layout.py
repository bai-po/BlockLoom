"""Deterministic Java resource-pack texture layers from declared pack.mcmeta.

This pure read plan never extracts data. The explicit asset importer supplies
the pinned catalog resource format, validates byte budgets, and owns writes.
Overlay precedence follows the author's declaration order, not ZIP ordering.
"""

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class TexturePlan:
    entries: tuple[tuple[str, str, int], ...]
    root: str
    overlays: tuple[str, ...]
    resource_format: tuple[int, int]


def _safe(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute() or ".." in path.parts
            or any(":" in part or "\0" in part for part in path.parts)):
        raise ValueError("材质包包含越界路径")
    return path


def _version(value, *, maximum=False):
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value, 0x7FFFFFFF if maximum else 0
    if (isinstance(value, list) and len(value) in {1, 2}
            and all(isinstance(part, int) and not isinstance(part, bool) and part >= 0 for part in value)):
        return value[0], value[1] if len(value) == 2 else 0x7FFFFFFF if maximum else 0
    raise ValueError("材质包资源格式版本无效")


def _range(entry):
    if "min_format" in entry or "max_format" in entry:
        if "min_format" not in entry or "max_format" not in entry:
            raise ValueError("材质包覆盖层缺少资源格式范围")
        low, high = _version(entry["min_format"]), _version(entry["max_format"], maximum=True)
    else:
        formats = entry.get("formats", entry.get("supported_formats"))
        if isinstance(formats, int) and not isinstance(formats, bool):
            low, high = _version(formats), _version(formats, maximum=True)
        elif isinstance(formats, list) and len(formats) == 2:
            # The legacy two-integer array is an inclusive major range.
            low, high = _version(formats[0]), _version(formats[1], maximum=True)
        elif isinstance(formats, dict) and set(formats) == {"min_inclusive", "max_inclusive"}:
            low, high = _version(formats["min_inclusive"]), _version(formats["max_inclusive"], maximum=True)
        else:
            raise ValueError("材质包覆盖层资源格式范围无效")
    if low > high:
        raise ValueError("材质包覆盖层资源格式范围倒置")
    return low, high


def plan(names, read_metadata, resource_format):
    """Select one pack root and declared applicable texture overlays only."""
    names = tuple(names)
    paths = {name: _safe(name) for name in names}
    if len(set(names)) != len(names):
        raise ValueError("资源包同一层存在重复文件条目")
    textures = tuple(name for name in names if "/textures/" in name and name.endswith((".png", ".png.mcmeta")))
    texture_names = set(textures)
    if "pack.mcmeta" in paths:
        root = ""
    else:
        candidates = []
        for name, path in paths.items():
            if path.name == "pack.mcmeta":
                prefix = name.removesuffix("pack.mcmeta")
                if any(item.startswith(prefix + "assets/") for item in textures):
                    candidates.append(prefix)
        if len(candidates) > 1:
            raise ValueError("资源包含多个独立pack.mcmeta根目录，未混合材质")
        if candidates:
            root = candidates[0]
        else:
            # Legacy texture-only ZIPs remain supported, but never merge roots.
            roots = {"/".join(path.parts[:path.parts.index("assets")]) for name, path in paths.items()
                     if name in texture_names and "assets" in path.parts}
            if len(roots) != 1:
                raise ValueError("资源包缺少唯一assets根目录")
            value = roots.pop()
            root = value + "/" if value else ""
    metadata_name = root + "pack.mcmeta"
    metadata = json.loads(read_metadata(metadata_name).decode("utf-8-sig")) if metadata_name in paths else {}
    if not isinstance(metadata, dict):
        raise TypeError("材质包pack.mcmeta必须是对象")
    overlays = metadata.get("overlays", {})
    if not isinstance(overlays, dict) or not isinstance(overlays.get("entries", []), list):
        raise TypeError("材质包覆盖层声明无效")
    selected, declared = [], set()
    for entry in overlays.get("entries", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("directory"), str):
            raise TypeError("材质包覆盖层目录声明无效")
        directory = entry["directory"]
        path = _safe(directory)
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+", directory) or directory != path.as_posix()
                or directory in {".", "assets"} or directory in declared):
            raise ValueError("材质包覆盖层目录存在歧义")
        declared.add(directory)
        low, high = _range(entry)
        if low <= resource_format <= high:
            selected.append(directory)
    entries = []
    for index, prefix in enumerate((root + "assets/", *(root + value + "/assets/" for value in selected))):
        seen = set()
        for name in sorted(textures):
            if not name.startswith(prefix):
                continue
            relative = "assets/" + name.removeprefix(prefix)
            folded = relative.casefold()
            if folded in seen:
                raise ValueError("资源包同一层存在歧义的重复贴图")
            seen.add(folded)
            entries.append((name, relative, index))
    return TexturePlan(tuple(entries), root, tuple(selected), resource_format)
