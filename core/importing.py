"""Small Bus/Undo adapter shared by real resource import endpoints."""

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Generic, TypeVar, cast

import bpy
from bpy.app.handlers import persistent

from ..util.resources import (
    ImportResult,
    prepare_creation_options,
    resolve_scene,
    resolve_view_layer,
)
from .messages import Message, Reply
from .runtime import MutationBlockedError, ensure_mutation_allowed, get_bus

logger = logging.getLogger(__name__)
ResultT = TypeVar("ResultT")
Loader = Callable[[int, Mapping[str, object]], ResultT]


@dataclass(slots=True)
class ImportState(Generic[ResultT]):
    request_id: str | None = None
    message: str = ""
    succeeded: bool = False
    submitted_at: float = 0.0
    total_ms: float = 0.0
    dispatch_ms: float = 0.0
    result: ResultT | None = None


@dataclass(slots=True)
class _Invocation(Generic[ResultT]):
    result: ResultT | None = None
    error: str = ""


_endpoints: set["ImportEndpoint[Any]"] = set()


@persistent
def _before_load(_unused: object) -> None:
    for endpoint in _endpoints:
        endpoint.reset()


class ImportEndpoint(Generic[ResultT]):
    def __init__(
        self,
        topic: str,
        operator: str,
        loader: Loader[ResultT],
        *,
        result_type: type | None = ImportResult,
        allowed_modes: tuple[str, ...] = ("OBJECT",),
        new_instance: bool = False,
    ) -> None:
        if not allowed_modes or not set(allowed_modes) <= {"OBJECT", "POSE"}:
            raise ValueError("执行端点必须明确声明支持的物体/姿态模式")
        self.topic = topic
        self.operator = operator
        self.loader = loader
        self.result_type = result_type
        self.allowed_modes = frozenset(allowed_modes)
        self.new_instance = new_instance
        self.states: dict[int, ImportState[ResultT]] = {}
        self._requests: dict[str, int] = {}
        self._invocations: dict[str, _Invocation[ResultT]] = {}

    def state(self, scene_uid: int) -> ImportState[ResultT]:
        return self.states.setdefault(scene_uid, ImportState())

    def busy(self, scene_uid: int) -> bool:
        state = self.state(scene_uid)
        return state.request_id is not None and get_bus().is_pending(state.request_id)

    def submit(self, scene_uid: int, options: Mapping[str, object]) -> str:
        ensure_mutation_allowed()
        if self.busy(scene_uid):
            raise ValueError("该场景的导入请求尚未完成")
        # Only plain serializable options cross the deferred Undo boundary.
        prepared = prepare_creation_options(options) if self.new_instance else dict(options)
        params = json.dumps(prepared, ensure_ascii=False, allow_nan=False)
        state = self.state(scene_uid)
        state.submitted_at = perf_counter()
        request_id = get_bus().send(
            self.topic,
            payload={"scene_uid": scene_uid, "options_json": params},
            on_result=self.receive_result,
        )
        state.request_id = request_id
        state.message = ""
        state.succeeded = False
        state.result = None
        state.total_ms = state.dispatch_ms = 0.0
        self._requests[request_id] = scene_uid
        return request_id

    def handle(self, message: Message) -> ResultT:
        ensure_mutation_allowed()
        uid = message.payload.get("scene_uid")
        params = message.payload.get("options_json")
        if (
            isinstance(uid, bool)
            or not isinstance(uid, int)
            or not isinstance(params, str)
        ):
            raise TypeError("导入消息参数格式错误")
        if bpy.context.mode not in self.allowed_modes:
            raise ValueError("当前模式不支持此 OXAL 操作")
        scene = resolve_scene(uid)
        resolve_view_layer(scene, require_active=True)
        command = _Invocation[ResultT]()
        self._invocations[message.request_id] = command
        started = perf_counter()
        try:
            namespace, name = self.operator.split(".", 1)
            operation = getattr(getattr(bpy.ops, namespace), name)
            status = operation(
                "EXEC_DEFAULT",
                True,
                request_id=message.request_id,
                scene_uid=str(uid),
                options_json=params,
                topic=self.topic,
            )
            if command.error:
                raise RuntimeError(command.error)
            if status != {"FINISHED"} or command.result is None:
                raise RuntimeError("资源导入未完成")
            try:
                self.after_execute(command.result)
            except Exception:
                # Scene work already succeeded inside its native Undo boundary.
                # A presentation failure must not invite a duplicate import.
                logger.exception("Result presentation failed: %s", self.topic)
            self.state(uid).dispatch_ms = (perf_counter() - started) * 1000
            return command.result
        finally:
            self._invocations.pop(message.request_id, None)

    def apply(self, request_id: str, scene_uid: str, options_json: str) -> bool:
        command = self._invocations.get(request_id)
        try:
            ensure_mutation_allowed()
            if bpy.context.mode not in self.allowed_modes:
                raise ValueError("当前模式不支持此 OXAL 操作")
            options = json.loads(options_json)
            if not isinstance(options, dict):
                raise TypeError("导入选项必须是字典")
            scene = resolve_scene(int(scene_uid))
            resolve_view_layer(scene, require_active=True)
            result = self.loader(scene.session_uid, options)
        except Exception as exc:
            if isinstance(exc, MutationBlockedError):
                logger.info("Scene mutation blocked: %s: %s", self.topic, exc)
            else:
                logger.exception("Resource operation failed: %s", self.topic)
            if command is not None:
                command.error = str(exc)
            return False
        try:
            self.after_apply(result)
        except Exception:
            # Native Redo bypasses handle()/after_execute(). Repair transient
            # selection here, without recasting committed scene work as failed.
            logger.exception("Result selection failed: %s", self.topic)
        if command is not None:
            command.result = result
        return True

    def receive_result(self, reply: Reply) -> None:
        uid = self._requests.pop(reply.request_id, None)
        if uid is None:
            return
        state = self.state(uid)
        if state.request_id != reply.request_id:
            return
        state.request_id = None
        state.total_ms = (perf_counter() - state.submitted_at) * 1000
        state.result = (
            cast(ResultT, reply.value)
            if (
                reply.value is not None
                and (
                    self.result_type is None
                    or isinstance(reply.value, self.result_type)
                )
            )
            else None
        )
        state.succeeded = reply.ok and state.result is not None
        state.message = (
            self.success_message(state.result)
            if state.succeeded and state.result is not None
            else reply.error.message
            if reply.error
            else "资源导入失败"
        )

    def after_execute(self, result: ResultT) -> None:
        """Publish module UI state after native apply, before the queued reply.

        UI lists/previews are coordinated here; the later result callback only
        updates lightweight status. Tools do not depend on this hook.
        """

    def after_apply(self, result: ResultT) -> None:
        """Refresh transient selection after native apply, including Redo.

        This is not a second business stage: no resource IO, request submission,
        topology edits or preview decoding belongs in a selection hook.
        """

    def success_message(self, result: ResultT | None) -> str:
        return (
            f"已导入：{result.collection_name}"
            if isinstance(result, ImportResult)
            else "操作已完成"
        )

    def reset(self) -> None:
        self.states.clear()
        self._requests.clear()
        self._invocations.clear()

    def register(self) -> None:
        get_bus().register(self.topic, self.handle)
        self.reset()
        _endpoints.add(self)
        if _before_load not in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.append(_before_load)

    def unregister(self) -> None:
        get_bus().unregister(self.topic)
        self.reset()
        _endpoints.discard(self)
        if not _endpoints and _before_load in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.remove(_before_load)


def find_endpoint(topic: str) -> ImportEndpoint:
    for endpoint in _endpoints:
        if endpoint.topic == topic:
            return endpoint
    raise ValueError("资源导入模块尚未注册")
