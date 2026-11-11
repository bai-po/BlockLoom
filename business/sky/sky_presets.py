"""Versioned portable presets and a user-owned library, loaded only on demand."""

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import bpy

from ...core.runtime import ensure_mutation_allowed
from .sky_schema import validated
from .sky_state import SLOT_COUNT, VALUES, effective_snapshot

FORMAT = "oxal-lib-sky"
FORMAT_VERSION = 1
MAX_BYTES = 131072


def library_path() -> Path:
    return Path(bpy.utils.user_resource("CONFIG")) / "oxal_mc_studio" / "sky_presets"


def read(path: Path) -> dict:
    if path.suffix.lower() != ".oxalsky" or path.stat().st_size > MAX_BYTES:
        raise ValueError("请选择有效且小于 128 KiB 的 .oxalsky 文件")
    data = json.loads(path.read_text(encoding="utf8"))
    if (
        not isinstance(data, dict)
        or data.get("format") != FORMAT
        or data.get("version") != FORMAT_VERSION
    ):
        raise ValueError("天空预设格式不兼容；旧插件预设不会自动转换")
    if not isinstance(data.get("parameters"), dict):
        raise TypeError("天空预设缺少参数")
    return {
        "name": str(data.get("name", path.stem))[:120],
        "parameters": validated(data["parameters"]),
    }


def list_presets() -> list[tuple[str, str]]:
    directory = library_path()
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.glob("*.oxalsky")):
        try:
            data = read(path)
            result.append((path.name, data["name"]))
        except (ValueError, TypeError, OSError, UnicodeError):
            result.append((path.name, f"{path.stem}（文件无效）"))
    return result


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".oxal-sky-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf8") as stream:
            json.dump(
                {"format": FORMAT, "version": FORMAT_VERSION, **data},
                stream,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def context_snapshot(scene):
    values = getattr(scene, VALUES)
    return {"slot_index": int(values.preset_slot), "slot_keyframes": bool(values.preset_keyframes)}


def request(scene, action, options):
    """Freeze file contents/export parameters and the chosen target at submission."""
    result = {**context_snapshot(scene), **options}
    if action == "export_preset":
        if "parameters" not in result:
            result["parameters"] = validated(effective_snapshot(scene))
    elif action == "import_preset":
        result["preset_data"] = read(Path(str(options.get("filepath", ""))))
        result.setdefault("library_filename", uuid4().hex + ".oxalsky")
    elif action == "apply_preset":
        filename = options.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".oxalsky"):
            raise ValueError("请选择预设库中的文件")
        result["preset_data"] = read(library_path() / filename)
    return result


def _submitted_data(options):
    data = options.get("preset_data")
    if not isinstance(data, dict) or not isinstance(data.get("parameters"), dict):
        raise TypeError("缺少点击时的天空预设内容")
    return {"name": str(data.get("name", "天空预设"))[:120], "parameters": validated(data["parameters"])}


def operate(scene, action: str, options: Mapping[str, object]) -> str | dict:
    ensure_mutation_allowed()
    if action == "list_presets":
        entries = list_presets()
        return {"message": "已刷新天空预设库", "presets": entries}
    values = getattr(scene, VALUES)
    if action == "import_preset":
        data = _submitted_data(options)
        filename = options.get("library_filename")
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".oxalsky"):
            raise ValueError("缺少冻结的预设库目标文件名")
        _write(library_path() / filename, data)
        return "已导入天空预设库"
    if action == "export_preset":
        path = Path(str(options.get("filepath", "")))
        if path.suffix.lower() != ".oxalsky":
            raise ValueError("导出文件需要 .oxalsky 扩展名")
        parameters = options.get("parameters")
        if not isinstance(parameters, dict):
            raise TypeError("缺少点击时的导出参数")
        _write(path, {"name": path.stem, "parameters": validated(parameters)})
        return "已导出当前天空参数"
    filename = options.get("filename")
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or not filename.endswith(".oxalsky")
    ):
        raise ValueError("请选择预设库中的文件")
    path = library_path() / filename
    if action == "delete_preset":
        path.unlink(missing_ok=True)
        return "已从预设库删除文件"
    if action == "apply_preset":
        from .sky_modes import normalize_modes

        index = options.get("slot_index")
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= SLOT_COUNT:
            raise ValueError("缺少点击时的目标保存值编号")
        data = _submitted_data(options)
        for key, value in normalize_modes(data["parameters"]).items():
            setattr(values, key, value)
        values.preset_keyframes = False
        values.preset_slot = index
        return "已应用天空预设"
    raise ValueError("未知预设操作")
