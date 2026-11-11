"""Per-human skin history shared by owned and native legacy instances."""

import json
from os import fspath
from typing import Any

KEY = "oxal_human_recent_skins"
LIMIT = 12


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("人模最近皮肤记录包含无效路径，未修改记录")
    return value


def recent_paths(owner: Any) -> tuple[str, ...]:
    """Read persisted paths without checking files or changing Blender data."""
    if KEY not in owner:
        return ()
    raw = owner[KEY]
    if not isinstance(raw, str):
        raise TypeError("人模最近皮肤记录格式无效，未修改记录")
    try:
        values = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise ValueError("人模最近皮肤记录格式无效，未修改记录") from error
    if not isinstance(values, list):
        raise TypeError("人模最近皮肤记录应为路径列表，未修改记录")
    # Validate every stored item before limiting the displayed history.
    return tuple(dict.fromkeys(_path(value) for value in values))[:LIMIT]


def remember(transaction: Any, owner: Any, source: Any) -> None:
    """Journal the existing property before recording an applied source path."""
    previous = recent_paths(owner)
    source_path = _path(fspath(source))
    existed = KEY in owner
    original = owner[KEY] if existed else None

    def restore():
        if existed:
            owner[KEY] = original
        elif KEY in owner:
            del owner[KEY]

    transaction.on_rollback(restore)
    owner[KEY] = json.dumps(
        [source_path, *(path for path in previous if path != source_path)][:LIMIT],
        ensure_ascii=False,
    )
