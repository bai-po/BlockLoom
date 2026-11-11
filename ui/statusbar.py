"""Native status-bar branding; no scene data or update callbacks."""

import bpy

from ..util.identity import DISPLAY_VERSION, PRODUCT_NAME

_registered = False


def draw_version(self, _context):
    row = self.layout.row(align=True)
    row.alignment = "RIGHT"
    row.label(text=f"{PRODUCT_NAME} | {DISPLAY_VERSION}", translate=False)


def register():
    global _registered
    if not _registered:
        bpy.types.STATUSBAR_HT_header.append(draw_version)
        _registered = True


def unregister():
    global _registered
    if _registered:
        bpy.types.STATUSBAR_HT_header.remove(draw_version)
        _registered = False
