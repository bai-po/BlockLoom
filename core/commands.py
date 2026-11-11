"""General scene commands reuse the resource request and native Undo adapter."""

from collections.abc import Mapping
from typing import TYPE_CHECKING

import bpy
from bpy.props import StringProperty
from bpy.types import Context, Operator

from .importing import ImportEndpoint, Loader, ResultT, find_endpoint
from .runtime import mutation_allowed

if TYPE_CHECKING:
    from bpy.stub_internal.rna_enums import OperatorReturnItems


class CommandEndpoint(ImportEndpoint[ResultT]):
    def __init__(self, topic: str, loader: Loader[ResultT], *, allowed_modes: tuple[str, ...] = ("OBJECT",)) -> None:
        super().__init__(topic, "oxal_mc_studio.apply_command", loader, result_type=None,
                         allowed_modes=allowed_modes)

    def success_message(self, result: ResultT | None) -> str:
        if isinstance(result, str):
            return result
        if isinstance(result, Mapping):
            return str(result.get("message", "操作已完成"))
        return str(getattr(result, "message", "操作已完成"))


class OXALMCSTUDIO_OT_apply_command(Operator):
    bl_idname = "oxal_mc_studio.apply_command"
    bl_label = "执行 OXAL 操作"
    bl_options = {"INTERNAL", "UNDO"}  # noqa: RUF012 -- Blender RNA metadata.

    topic: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    request_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    scene_uid: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    options_json: StringProperty(default="{}", options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context: Context) -> bool:
        return mutation_allowed()

    def execute(self, context: Context) -> "set[OperatorReturnItems]":
        try:
            endpoint = find_endpoint(self.topic)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        ok = endpoint.apply(self.request_id, self.scene_uid, self.options_json)
        return {"FINISHED"} if ok else {"CANCELLED"}


def register() -> None:
    bpy.utils.register_class(OXALMCSTUDIO_OT_apply_command)


def unregister() -> None:
    bpy.utils.unregister_class(OXALMCSTUDIO_OT_apply_command)
