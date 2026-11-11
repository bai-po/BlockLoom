"""Run the cooperative EMT bus on Blender's main-thread application timer."""

import logging
from collections.abc import Callable
from typing import cast

import bpy
from bpy.app.handlers import persistent
from bpy.types import WindowManager

from .bus import Bus

logger = logging.getLogger(__name__)
INTERVAL = 0.02
BUDGET_SECONDS = 0.004
MAX_STEPS = 32

_bus: Bus | None = None
_rendering = False
_cancel_after_render = False
_shutdown: Callable[[], None] | None = None


class MutationBlockedError(RuntimeError):
    """An expected refusal while Blender is rendering or otherwise locked."""


def _host_unlocked() -> bool:
    if _rendering or bpy.app.is_job_running("RENDER"):
        return False
    manager = cast(WindowManager | None, bpy.context.window_manager)
    return manager is None or not manager.is_interface_locked


def mutation_allowed() -> bool:
    """Shared by submission, direct tools and native Undo replay."""
    return _shutdown is None and not _cancel_after_render and _host_unlocked()


def ensure_mutation_allowed() -> None:
    if not mutation_allowed():
        raise MutationBlockedError("渲染、锁定作业或停用期间暂不能修改 OXAL 数据")


@persistent
def _render_begin(_scene: object) -> None:
    global _rendering, _cancel_after_render
    _rendering = True
    # No callbacks or generator finalizers may run inside a render handler.
    _cancel_after_render = True


@persistent
def _render_end(_scene: object) -> None:
    global _rendering
    _rendering = False


def get_bus() -> Bus:
    if _bus is None:
        raise RuntimeError("OXAL MC Studio bus is not registered")
    return _bus


def _redraw() -> None:
    for manager in bpy.data.window_managers:
        for window in manager.windows:
            if window.screen is not None:
                for area in window.screen.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()


def _tick() -> float | None:
    global _shutdown, _cancel_after_render
    bus = _bus
    if bus is None and _shutdown is None:
        return None
    if not _host_unlocked():
        return INTERVAL
    if _shutdown is not None:
        callback, _shutdown = _shutdown, None
        _cancel_after_render = False
        try:
            callback()
        except Exception:
            logger.exception("Deferred plugin shutdown failed")
        return None
    if bus is None:
        return None
    try:
        if _cancel_after_render:
            _cancel_after_render = False
            bus.cancel_pending(reason="渲染已开始，请在渲染结束后重新提交")
        if bus.pump(budget_seconds=BUDGET_SECONDS, max_steps=MAX_STEPS):
            _redraw()
    except Exception:
        # A scheduler failure must not silently remove Blender's timer.
        logger.exception("OXAL MC Studio bus scheduler failed")
    return INTERVAL if _bus is bus else None


@persistent
def _before_load(_unused: object) -> None:
    global _rendering, _cancel_after_render
    _rendering = False
    _cancel_after_render = False
    if _bus is not None:
        # Never carry old-scene tasks or callbacks into another file.
        _bus.discard_pending()


def register() -> None:
    global _bus
    ensure_mutation_allowed()
    if _bus is not None:
        raise RuntimeError("OXAL MC Studio bus is already registered")
    _bus = Bus()


def request_shutdown(callback: Callable[[], None]) -> None:
    """Defer teardown until native jobs have released the scene and RNA."""
    global _shutdown
    _shutdown = callback
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=INTERVAL, persistent=True)


def start() -> None:
    """Start after all business handlers and UI classes have registered."""
    ensure_mutation_allowed()
    get_bus()
    if _before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_before_load)
    for handlers, callback in _render_handlers():
        if callback not in handlers:
            handlers.append(callback)
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.0, persistent=True)


def stop() -> None:
    """Stop scheduling and discard outstanding callbacks before removing UI."""
    ensure_mutation_allowed()
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_before_load)
    for handlers, callback in _render_handlers():
        if callback in handlers:
            handlers.remove(callback)
    if _bus is not None:
        _bus.discard_pending()


def unregister() -> None:
    global _bus, _rendering, _cancel_after_render, _shutdown
    stop()
    if _bus is not None:
        _bus.close()
        _bus = None
    _rendering = False
    _cancel_after_render = False
    _shutdown = None


def _render_handlers():
    return (
        (bpy.app.handlers.render_init, _render_begin),
        (bpy.app.handlers.render_complete, _render_end),
        (bpy.app.handlers.render_cancel, _render_end),
    )
