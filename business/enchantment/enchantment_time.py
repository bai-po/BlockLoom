"""Scene-scoped, read-only native glint time uniforms.

asset_id: oxal.glint.time; version: 1; policy: REUSE_DATABLOCK.
One explicitly created Empty belongs to one Scene, independently of actors.
Its native drivers retain the original double-precision time expressions.
Shader Attribute nodes read evaluated values through a Scene RNA path; shader
trees never hold a reverse Scene/clock ID reference or a per-frame driver.
"""

import bpy

from ...core.runtime import ensure_mutation_allowed
from ...util.ownership import (
    ROOT_UID,
    SCENE_ROOTS,
    owned_ids,
    require_owned,
    seal_owned,
)

REVISION = 1
MARKER = "oxal_mc_studio_glint_time_schema"
TOKEN = "oxal_mc_studio_glint_time_root"
SCENE_CLOCK = "oxal_mc_studio_glint_time_clock"
_ASSET = "oxal_mc_studio_glint_time_asset"
_VERSION = "oxal_mc_studio_glint_time_version"
_ASSET_ID = "oxal.glint.time"
_PARAMETERS = (("fps", "render.fps"), ("base", "render.fps_base"), ("first", "frame_start"))
_EXPRESSIONS = {
    "u": "-fmod(fmod(floor((frame-first)*base/max(fps,1)*4000),110000)+110000,110000)/110000",
    "v": "fmod(fmod(floor((frame-first)*base/max(fps,1)*4000),30000)+30000,30000)/30000",
    "seconds": "(frame-first)*base/max(fps,1)",
}


def metadata(material):
    """Return sealed scalar metadata; absent keys retain historical bytes."""
    if MARKER not in material and TOKEN not in material:
        return None
    token = material.get(TOKEN)
    if (type(material.get(MARKER)) is not int or material[MARKER] != REVISION
            or not isinstance(token, str) or len(token) != 32
            or any(char not in "0123456789abcdef" for char in token)):
        raise ValueError("未知附魔原生时间版本或来源标识，未覆盖")
    return [REVISION, token]


def _validate(scene, clock):
    if any(SCENE_CLOCK in layer for layer in scene.view_layers):
        raise ValueError("视图层含同名附魔时间字段，未覆盖用户属性")
    if (not isinstance(clock, bpy.types.Object) or clock.type != "EMPTY" or clock.data is not None
            or clock.parent is not None or clock.constraints or clock.instance_collection is not None
            or clock.get(_ASSET) != _ASSET_ID
            or type(clock.get(_VERSION)) is not int or clock[_VERSION] != REVISION
            or scene.get(SCENE_CLOCK) != clock):
        raise ValueError("附魔原生时间来源已变化，未接管场景数据")
    if (not clock.hide_select or clock.hide_viewport or clock.hide_render or clock.empty_display_type != "IMAGE"
            or clock.show_empty_image_orthographic or clock.show_empty_image_perspective
            or any(clock.hide_get(view_layer=layer) for layer in scene.view_layers)):
        raise ValueError("附魔原生时间求值或展示设置已变化，未覆盖")
    require_owned(clock, (clock,))
    if owned_ids(clock):
        raise ValueError("附魔原生时间根含未知成员，未覆盖")
    token = clock.get(ROOT_UID)
    bindings = []
    for candidate in bpy.data.scenes:
        roots = candidate.get(SCENE_ROOTS)
        if roots is not None and hasattr(roots, "items"):
            bindings.extend((candidate, key) for key, value in roots.items() if value == clock or key == token)
        if candidate != scene and candidate.get(SCENE_CLOCK) == clock:
            raise ValueError("附魔原生时间被其它场景引用，未修改")
    if bindings != [(scene, token)]:
        raise ValueError("附魔原生时间场景登记不唯一，未修改")
    animation = clock.animation_data
    if (animation is None or animation.action is not None or animation.nla_tracks
            or len(animation.drivers) != len(_EXPRESSIONS)):
        raise ValueError("附魔原生时间含用户动画或缺失驱动，未覆盖")
    curves = {(curve.data_path, curve.array_index): curve for curve in animation.drivers}
    for key, expression in _EXPRESSIONS.items():
        curve = curves.get(('["' + key + '"]', 0))
        if (type(clock.get(key)) is not float or curve is None or curve.mute
                or curve.modifiers or curve.keyframe_points or curve.sampled_points
                or curve.extrapolation != "CONSTANT"):
            raise ValueError("附魔原生时间驱动已被修改，未覆盖")
        driver = curve.driver
        if (driver is None or driver.type != "SCRIPTED" or driver.use_self
                or driver.expression != expression or len(driver.variables) != len(_PARAMETERS)):
            raise ValueError("附魔原生时间驱动已被修改，未覆盖")
        for variable, (name, path) in zip(driver.variables, _PARAMETERS, strict=True):
            if (variable.name != name or variable.type != "SINGLE_PROP"
                    or len(variable.targets) != 1 or variable.targets[0].id != scene
                    or variable.targets[0].id_type != "SCENE"
                    or variable.targets[0].data_path != path):
                raise ValueError("附魔原生时间驱动目标已变化，未覆盖")
    return token


def require_material(material, *, scene=None):
    """Resolve a scalar token through the exact persisted Scene registry."""
    record = metadata(material)
    if record is None:
        return None
    token = record[1]
    matches = []
    for candidate in bpy.data.scenes:
        roots = candidate.get(SCENE_ROOTS)
        if roots is not None and hasattr(roots, "get") and roots.get(token) is not None:
            matches.append((candidate, roots[token]))
    if len(matches) != 1 or scene is not None and matches[0][0] != scene:
        raise ValueError("附魔原生时间实际来源缺失或跨场景，未修改")
    candidate, clock = matches[0]
    if _validate(candidate, clock) != token:
        raise ValueError("附魔原生时间实际引用不匹配，未修改")
    return clock


def prepare(transaction, scene):
    """Reuse a verified clock or create one under the caller's rollback."""
    ensure_mutation_allowed()
    if not isinstance(scene, bpy.types.Scene):
        raise TypeError("附魔动画需要明确的场景")
    if any(SCENE_CLOCK in layer for layer in scene.view_layers):
        raise ValueError("视图层含同名附魔时间字段，未覆盖用户属性")
    if SCENE_CLOCK in scene:
        clock = scene[SCENE_CLOCK]
        _validate(scene, clock)
        return clock
    roots = scene.get(SCENE_ROOTS)
    if roots is not None and not hasattr(roots, "items"):
        raise ValueError("场景来源登记已损坏，未创建附魔时间资源")
    # An omitted presentation pointer must not adopt another registered clock.
    if roots is not None and any(isinstance(item, bpy.types.Object) and item.get(_ASSET) == _ASSET_ID
                                 for item in roots.values()):
        raise ValueError("附魔原生时间场景引用缺失，未按标记接管")
    clock = transaction.track(bpy.data.objects.new("附魔原生时间", None))
    # The service is its own Scene root, never a member of an actor's domain.
    transaction.excluded.add(clock)
    had_roots = SCENE_ROOTS in scene

    def restore():
        if scene.get(SCENE_CLOCK) == clock:
            del scene[SCENE_CLOCK]
        table = scene.get(SCENE_ROOTS)
        if table is not None and hasattr(table, "items"):
            for key, item in tuple(table.items()):
                if item == clock:
                    del table[key]
            if not had_roots and not table:
                del scene[SCENE_ROOTS]

    transaction.on_rollback(restore)
    clock[_ASSET], clock[_VERSION] = _ASSET_ID, REVISION
    from .enchantment_collections import link as link_controls

    link_controls(transaction, scene, clock)
    clock.hide_select = True
    # Globally hiding the Object also removes its drivers from evaluation.
    # Empty images have no render geometry; native projection flags suppress
    # their viewport display while leaving time evaluation in the depsgraph.
    clock.empty_display_type = "IMAGE"
    clock.show_empty_image_orthographic = False
    clock.show_empty_image_perspective = False
    for key, expression in _EXPRESSIONS.items():
        clock[key] = 0.
        curve = clock.driver_add('["' + key + '"]')
        curve.keyframe_points.clear()
        for modifier in tuple(curve.modifiers):
            curve.modifiers.remove(modifier)
        curve.extrapolation = "CONSTANT"
        curve.driver.type, curve.driver.expression = "SCRIPTED", expression
        for name, path in _PARAMETERS:
            variable = curve.driver.variables.new()
            variable.name, variable.type = name, "SINGLE_PROP"
            variable.targets[0].id_type = "SCENE"
            variable.targets[0].id = scene
            variable.targets[0].data_path = path
    scene[SCENE_CLOCK] = clock
    seal_owned(clock, (clock,))
    _validate(scene, clock)
    return clock


def output(graph, field, role):
    """Make a native evaluated uniform, with no ID edge from the shader."""
    if field not in _EXPRESSIONS:
        raise ValueError("未知附魔时间通道")
    node = graph.node("ShaderNodeAttribute", role)
    node.attribute_type = "VIEW_LAYER"
    node.attribute_name = '["' + SCENE_CLOCK + '"]["' + field + '"]'
    return node.outputs["Fac"]


def bind(material, clock):
    """Stamp new scalar identity only; no Scene/clock datablock dependency."""
    material[MARKER], material[TOKEN] = REVISION, clock[ROOT_UID]


def clear(material):
    for key in (MARKER, TOKEN):
        if key in material:
            del material[key]
