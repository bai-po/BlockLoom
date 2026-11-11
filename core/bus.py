"""Single-threaded, cooperative request bus; deliberately independent of bpy."""

import logging
import math
from collections import deque
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from inspect import isasyncgenfunction, iscoroutinefunction
from time import monotonic
from types import GeneratorType, MappingProxyType
from typing import cast
from uuid import uuid4

from .messages import Callback, Failure, Handler, Message, Reply, ReplyStatus

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Request:
    message: Message
    callback: Callback
    deadline: float | None
    task: Generator[None, None, object] | None = None


class Bus:
    """One handler per topic, with deferred replies and bounded pump passes.

    All methods belong on the same thread. Sending never invokes user code.
    A handler may return a result directly or yield between small work batches
    and return its final result. The budget cannot preempt a handler/callback.
    """

    def __init__(
        self,
        *,
        max_pending: int = 256,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._handlers: dict[str, Handler] = {}
        self._pending: dict[str, _Request] = {}
        self._work: deque[str] = deque()
        self._replies: deque[tuple[_Request, Reply]] = deque()
        self._clock = clock
        self._max_pending = max_pending
        self._closed = False
        self._pumping = False

    @property
    def pending_count(self) -> int:
        """Includes completed requests whose callbacks have not run yet."""
        return len(self._pending) + len(self._replies)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Bus is closed")

    def register(self, topic: str, handler: Handler) -> None:
        self._ensure_open()
        if not topic or not callable(handler):
            raise ValueError("A nonempty topic and callable handler are required")
        if iscoroutinefunction(handler) or isasyncgenfunction(handler):
            raise TypeError("Use a normal function or a generator, not async def")
        if topic in self._handlers:
            raise ValueError(f"Topic already registered: {topic}")
        self._handlers[topic] = handler

    def unregister(self, topic: str) -> None:
        self._handlers.pop(topic, None)
        for request_id, request in tuple(self._pending.items()):
            if request.message.topic == topic:
                self.cancel(request_id, reason="Handler unregistered")

    def send(
        self,
        topic: str,
        *,
        on_result: Callback,
        payload: Mapping[str, object] | None = None,
        timeout: float | None = 30.0,
    ) -> str:
        """Record the callback and enqueue an EMT; return its ID immediately."""
        self._ensure_open()
        if not topic or not callable(on_result):
            raise ValueError("A nonempty topic and callable callback are required")
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be a finite positive number or None")
        if self.pending_count >= self._max_pending:
            raise RuntimeError("Bus request queue is full")
        request_id = uuid4().hex
        message = Message(request_id, topic, MappingProxyType(dict(payload or {})))
        self._pending[request_id] = _Request(
            message,
            on_result,
            None if timeout is None else self._clock() + timeout,
        )
        self._work.append(request_id)
        return request_id

    def is_pending(self, request_id: str) -> bool:
        return request_id in self._pending or any(
            reply.request_id == request_id for _, reply in self._replies
        )

    @staticmethod
    def _close_task(request: _Request) -> None:
        task, request.task = request.task, None
        if task is not None:
            try:
                task.close()
            except Exception:
                logger.exception("Task cleanup failed: %s", request.message.topic)

    def _finish(
        self,
        request: _Request,
        status: ReplyStatus,
        *,
        value: object = None,
        error: Failure | None = None,
    ) -> None:
        message = request.message
        # A handler may have cancelled itself or stopped the bus while running.
        if self._pending.pop(message.request_id, None) is not request:
            return
        self._close_task(request)
        self._replies.append(
            (request, Reply(message.request_id, message.topic, status, value, error))
        )

    def cancel(self, request_id: str, *, reason: str = "Request cancelled") -> bool:
        request = self._pending.get(request_id)
        if request is None:
            return False
        self._finish(request, "cancelled", error=Failure("cancelled", reason))
        return True

    def cancel_pending(self, *, reason: str) -> int:
        """Cancel unfinished work; call only when task cleanup is safe."""
        request_ids = tuple(self._pending)
        for request_id in request_ids:
            self.cancel(request_id, reason=reason)
        return len(request_ids)

    def discard_pending(self) -> None:
        """Drop tasks AND callbacks, e.g. before unregister or loading a file.

        This is cleanup, not a transaction rollback. Generator finally blocks
        must remain short and must not submit replacement work during cleanup.
        """
        requests = tuple(self._pending.values())
        self._pending.clear()
        self._work.clear()
        self._replies.clear()
        for request in requests:
            self._close_task(request)

    def close(self) -> None:
        self._closed = True
        self.discard_pending()
        self._handlers.clear()

    def _advance(self, request: _Request) -> None:
        try:
            if request.task is None:
                handler = self._handlers.get(request.message.topic)
                if handler is None:
                    self._finish(
                        request,
                        "failed",
                        error=Failure("unknown_topic", "No handler for this topic"),
                    )
                    return
                result = handler(request.message)
                if self._pending.get(request.message.request_id) is not request:
                    if isinstance(result, GeneratorType):
                        result.close()
                    return
                if not isinstance(result, GeneratorType):
                    self._finish(request, "succeeded", value=result)
                    return
                request.task = cast(Generator[None, None, object], result)
            next(request.task)
        except StopIteration as done:
            self._finish(request, "succeeded", value=done.value)
        except Exception as exc:
            logger.exception("Request failed: %s", request.message.topic)
            self._finish(request, "failed", error=Failure("handler_error", str(exc)))
        else:
            if request.message.request_id in self._pending:
                self._work.append(request.message.request_id)

    def pump(self, *, budget_seconds: float = 0.004, max_steps: int = 32) -> int:
        """Deliver old replies, then advance each eligible task at most once.

        New replies are delivered in a later pump, never inline with a handler.
        Timeouts/cancellation take effect between steps, not inside a function.
        """
        self._ensure_open()
        if self._pumping:
            raise RuntimeError("Bus.pump() cannot be called recursively")
        if not math.isfinite(budget_seconds) or budget_seconds <= 0 or max_steps < 1:
            raise ValueError("A positive budget and max_steps are required")
        self._pumping = True
        steps = 0
        deadline = self._clock() + budget_seconds
        reply_count = len(self._replies)
        try:
            for request in tuple(self._pending.values()):
                if request.deadline is not None and self._clock() >= request.deadline:
                    self._finish(
                        request,
                        "failed",
                        error=Failure("timeout", "Request deadline exceeded"),
                    )
            # Snapshot the pass so callbacks cannot create an unbounded loop.
            for _ in range(reply_count):
                if (
                    self._closed
                    or not self._replies
                    or steps >= max_steps
                    or self._clock() >= deadline
                ):
                    break
                request, reply = self._replies.popleft()
                steps += 1
                try:
                    request.callback(reply)
                except Exception:
                    logger.exception("Reply callback failed: %s", reply.request_id)
            for _ in range(len(self._work)):
                if (
                    self._closed
                    or not self._work
                    or steps >= max_steps
                    or self._clock() >= deadline
                ):
                    break
                request_id = self._work.popleft()
                request = self._pending.get(request_id)
                if request is None:
                    continue
                steps += 1
                self._advance(request)
        finally:
            self._pumping = False
        return steps
