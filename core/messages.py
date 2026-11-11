"""Messages shared by request producers, the bus and feature handlers."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

ReplyStatus = Literal["succeeded", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class Message:
    """One EMT envelope. Payload is a shallow, read-only snapshot."""

    request_id: str
    topic: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Failure:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Reply:
    request_id: str
    topic: str
    status: ReplyStatus
    value: object = None
    error: Failure | None = None

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


Handler = Callable[[Message], object]
Callback = Callable[[Reply], None]
