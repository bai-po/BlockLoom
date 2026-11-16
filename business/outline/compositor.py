"""Scene-local depth edges, composited through an object Cryptomatte exclusion."""

import struct
from collections import defaultdict
from typing import Any, cast

import bpy

MARK = "blockloom_depth_outline"
OUTLINE_READY = "已连接 Viewer 描边预览，可按 F12 查看最终渲染"
GEOMETRY_TYPES = {
    "MESH",
    "CURVE",
    "SURFACE",
    "META",
    "FONT",
    "VOLUME",
    "POINTCLOUD",
    "CURVES",
    "GREASEPENCIL",
    "GPENCIL",
}


def exclusion_targets(scene, selected):
    """Snapshot renderable geometry before enqueueing a selection-based edit."""
    objects = set(scene.objects)
    selection = set(selected)
    if not selection <= objects:
        raise ValueError("选中的对象已移出当前场景")
    members = {obj for obj in selection if obj.type in GEOMETRY_TYPES}
    pending = [obj for obj in selection if obj.type == "ARMATURE"]
    if pending:
        children = defaultdict(set)
        for obj in objects:
            if obj.parent is not None:
                children[obj.parent].add(obj)  # Includes bone-parented objects.
            for modifier in obj.modifiers:
                if modifier.type == "ARMATURE" and modifier.object is not None:
                    children[modifier.object].add(obj)
            for constraint in obj.constraints:
                if (
                    constraint.type == "CHILD_OF"
                    and constraint.target is not None
                    and constraint.target.type == "ARMATURE"
                ):
                    children[constraint.target].add(obj)
        visited = set()
        while pending:
            obj = pending.pop()
            if obj in visited:
                continue
            visited.add(obj)
            if obj.type in GEOMETRY_TYPES:
                members.add(obj)
            pending.extend(children.get(obj, ()))
    if not members:
        raise ValueError("请选择可渲染物体，或选择带模型的骨架或姿态骨骼")
    # Include temporarily hidden geometry so animated outfit changes stay covered.
    return sorted(obj.session_uid for obj in members)


def _preview(scene, tree, new, link, target):
    source = _source(target)
    if source is None:
        raise ValueError("合成器没有可预览的图像输出")
    viewers = [node for node in tree.nodes if node.bl_idname == "CompositorNodeViewer"]
    viewer = tree.nodes.active
    if viewer not in viewers:
        viewer = max(
            viewers,
            key=lambda node: (
                _source(node.inputs["Image"]) == source,
                node.get(MARK) == "viewer",
            ),
            default=None,
        )
    if viewer is None:
        x, y = target.node.location_absolute
        viewer = new("CompositorNodeViewer", "合成预览", int(x), int(y - 220))
        viewer[MARK] = "viewer"
    link(source, viewer.inputs["Image"])
    viewer.mute = False
    # Blender uses the active Viewer as the preview image when several exist.
    tree.nodes.active = viewer
    if bpy.app.background:
        return
    # Refresh a compositor that is already displayed. Opening the Compositing
    # workspace later picks up the saved Viewer without touching other editors.
    context = cast(Any, bpy.context)
    activate_viewer = cast(Any, bpy.ops.node.activate_viewer)
    for window in context.window_manager.windows:
        if window.scene != scene:
            continue
        for area in window.screen.areas:
            if (
                area.ui_type != "CompositorNodeTree"
                or area.spaces.active.edit_tree != tree
            ):
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is not None:
                with context.temp_override(window=window, area=area, region=region):
                    if activate_viewer.poll():
                        activate_viewer()


def _source(socket):
    return socket.links[0].from_socket if socket.is_linked else None


def _includes(source, node):
    """Follow the rendered image chain, including ordinary user compositor nodes."""
    pending = [source.node] if source is not None else []
    seen = set()
    while pending:
        current = pending.pop()
        if current == node:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(
                link.from_node for socket in current.inputs for link in socket.links
            )
    return False


def _color_inputs(node):
    return [
        socket for socket in node.inputs if socket.type == "RGBA" and socket.enabled
    ]


def _identity_edges(new, link, mask, label, *, close_gaps=False):
    # Pick carries the raw hash in R; G/B are its bounded display colors.
    # Object boundaries remain visible even when surfaces have equal depth.
    channels = new("CompositorNodeSeparateColor", label + "识别色", -800, -730)
    link(mask.outputs["Pick"], channels.inputs[0])
    color = new("CompositorNodeCombineColor", label + "边界颜色", -610, -730)
    for channel in ("Green", "Blue"):
        signal = channels.outputs[channel]
        if close_gaps:
            # Close single-pixel gaps between touching body parts before tracing the silhouette.
            for size, name in ((1, "填补接缝"), (-1, "还原外沿")):
                morph = new("CompositorNodeDilateErode", label + name, -680, -980)
                morph.inputs["Size"].default_value = size
                link(signal, morph.inputs["Mask"])
                signal = morph.outputs[0]
        link(signal, color.inputs[channel])
    boundaries = new("CompositorNodeFilter", label + "边缘", -410, -730)
    if boundaries.inputs.get("Type"):
        boundaries.inputs["Type"].default_value = "Sobel"
    else:
        boundaries.filter_type = "SOBEL"
    link(color.outputs[0], boundaries.inputs["Image"])
    return boundaries


def _install_outline_modes(new, link, mix, layer):
    factor = _source(mix.inputs[0])
    if factor is None:
        raise ValueError("原描边缺少排除遮罩连接")
    if factor.node.get(MARK) == "outline_aa":
        return
    subtract = factor.node
    full = _source(subtract.inputs[0])
    if subtract.bl_idname != "ShaderNodeMath" or subtract.operation != "SUBTRACT" or full is None:
        raise ValueError("原描边遮罩连接已修改，请检查排除节点")
    if full.node.get(MARK) == "outline_mode":
        _antialias_outline(new, link, subtract, mix)
        return
    asset = new("CompositorNodeCryptomatteV2", "模型轮廓", -1100, -950)
    asset.layer_name = layer.name + ".CryptoAsset"
    edges = _identity_edges(new, link, asset, "模型", close_gaps=True)
    binary = new("ShaderNodeMath", "模型外沿", -320, -950)
    binary.operation = "GREATER_THAN"
    binary.inputs[1].default_value = 0.002
    link(edges.outputs[0], binary.inputs[0])
    mode = new("CompositorNodeSwitch", "全描边", -160, -290)
    mode[MARK] = "outline_mode"
    mode.inputs["Switch"].default_value = False
    link(binary.outputs[0], mode.inputs["Off"])
    link(full, mode.inputs["On"])
    link(mode.outputs[0], subtract.inputs[0])
    _antialias_outline(new, link, subtract, mix)


def _antialias_outline(new, link, subtract, mix):
    aa = new("CompositorNodeAntiAliasing", "轮廓抗锯齿", 70, -210)
    aa[MARK] = "outline_aa"
    aa.inputs["Threshold"].default_value = .1
    link(subtract.outputs[0], aa.inputs["Image"])
    link(aa.outputs[0], mix.inputs[0])


def outline_mode_socket(scene):
    if outline_color_socket(scene) is None:
        return None
    _, _, _, tree = _target_tree(scene, "mix")
    mode = next((n for n in tree.nodes if n.get(MARK) == "outline_mode"), None)
    return mode.inputs["Switch"] if mode else None


def _add_edge_signals(new, link, depth_source, edge, threshold, mask):
    # A uniform change in scene scale adds a constant in log space. The
    # spatial derivative therefore no longer disappears beyond a fixed distance.
    depth = new("ShaderNodeMath", "对数深度", -1000, -200)
    depth.operation = "LOGARITHM"
    depth.inputs[1].default_value = 2
    link(depth_source, depth.inputs[0])
    link(depth.outputs[0], edge.inputs["Image"])
    boundaries = _identity_edges(new, link, mask, '物体')
    combined = new("ShaderNodeMath", "结合深度与物体边缘", -170, -340)
    combined.operation = "MAXIMUM"
    link(_source(threshold.inputs[0]), combined.inputs[0])
    link(boundaries.outputs[0], combined.inputs[1])
    link(combined.outputs[0], threshold.inputs[0])


def _upgrade_outline(new, link, mix, mask):
    # Only upgrade a connected legacy BlockLoom depth branch. Verify its
    # structure before replacing either link; labels are editable by users.
    def upstream(socket, kind):
        source = _source(socket)
        if source is None or source.node.bl_idname != kind:
            raise ValueError("原描边节点连接已修改，请在合成器中检查深度分支")
        return source.node

    subtract = upstream(mix.inputs[0], "ShaderNodeMath")
    threshold = upstream(subtract.inputs[0], "ShaderNodeMath")
    magnitude = upstream(threshold.inputs[0], "ShaderNodeMath")
    edge = upstream(magnitude.inputs[0], "CompositorNodeFilter")
    inverse = upstream(edge.inputs["Image"], "ShaderNodeMath")
    if (
        subtract.operation,
        threshold.operation,
        magnitude.operation,
        inverse.operation,
    ) != ("SUBTRACT", "GREATER_THAN", "ABSOLUTE", "DIVIDE"):
        raise ValueError("原描边深度运算已修改，请在合成器中检查该分支")
    depth = _source(inverse.inputs[1])
    if depth is None:
        raise ValueError("原描边缺少渲染深度输入")
    _add_edge_signals(new, link, depth, edge, threshold, mask)
    return inverse


def _reconnect_outline(link, mix, target):
    current = _source(target)
    if _includes(current, mix):
        return
    alpha = next(
        (
            conn.to_node
            for socket in mix.outputs
            for conn in socket.links
            if conn.to_node.bl_idname == "CompositorNodeSetAlpha"
        ),
        None,
    )
    if alpha is None:
        raise ValueError("原描边缺少透明度输出，请检查合成器节点连接")
    split = _source(alpha.inputs["Alpha"])
    if split is None or split.node.bl_idname != "CompositorNodeSeparateColor":
        raise ValueError("原描边缺少透明度输入，请检查合成器节点连接")
    if current is not None:
        link(current, _color_inputs(mix)[0])
        link(current, split.node.inputs[0])
    link(alpha.outputs[0], target)


def _encoded_matte(name: str) -> float:
    """Cryptomatte's MurmurHash3_32 and exponent remapping for an object name."""
    data = name.encode("utf-8")
    bits = 0xFFFFFFFF
    digest = 0
    for start in range(0, len(data), 4):
        chunk = data[start : start + 4]
        part = int.from_bytes(chunk, "little") * 0xCC9E2D51 & bits
        part = ((part << 15) | (part >> 17)) & bits
        digest ^= part * 0x1B873593 & bits
        if len(chunk) == 4:
            digest = ((digest << 13) | (digest >> 19)) & bits
            digest = (digest * 5 + 0xE6546B64) & bits
    digest ^= len(data)
    digest = ((digest ^ (digest >> 16)) * 0x85EBCA6B) & bits
    digest = ((digest ^ (digest >> 13)) * 0xC2B2AE35) & bits
    digest ^= digest >> 16
    if ((digest >> 23) & 255) in (0, 255):
        digest ^= 1 << 23
    (value,) = struct.unpack("<f", struct.pack("<I", digest))
    return value


def _matte_token(name: str, encoded: float) -> str:
    # Blender splits the editable matte string on every comma, trims spaces
    # and reserves angle brackets for numeric hashes.
    if (
        not name
        or "," in name
        or name != name.strip(" ")
        or (name.startswith("<") and name.endswith(">"))
    ):
        return f"<{encoded:.9g}>"
    return name


def _edit_exclusion(mask, scene, action, targets):
    if action == "COMP_CLEAR":
        mask.matte_id = ""
        return {"message": "已清空描边排除列表"}
    by_uid = {obj.session_uid: obj for obj in scene.objects}
    if not targets or any(uid not in by_uid for uid in targets):
        raise ValueError("未选中可渲染物体，或选中的对象已移出当前场景")
    # Preserve native entries, including items picked manually in Blender
    # and numeric entries from older scenes. Names and hashes denote the same ID.
    entries = {
        entry.encoded_hash: _matte_token(entry.name, entry.encoded_hash)
        for entry in mask.entries
    }
    count = len(entries)
    for uid in targets:
        name = by_uid[uid].name
        encoded = _encoded_matte(name)
        if action == "COMP_EXCLUDE":
            entries.setdefault(encoded, _matte_token(name, encoded))
        else:
            entries.pop(encoded, None)
    if len(entries) != count:
        mask.matte_id = ",".join(entries.values())
    verb = "新增排除" if action == "COMP_EXCLUDE" else "恢复描边"
    return {
        "message": f"已{verb} {abs(len(entries) - count)} 个物体，当前排除 {len(entries)} 个"
    }


def _target_tree(scene, marker):
    effects = getattr(scene, "compositor_effects", None)
    effect = None
    if effects is not None:
        # In 5.3 the active effect is an editor selection. The installed effect
        # still belongs to its original group after the user selects another.
        matches = [
            item
            for item in effects
            if item.node_group
            and any(n.get(MARK) == marker for n in item.node_group.nodes)
        ]
        if len(matches) > 1 and effects.active not in matches:
            raise ValueError("请先选择要修改的合成器效果")
        effect = (
            effects.active if not matches or effects.active in matches else matches[0]
        )
        original = effect.node_group if effect else None
        grouped = True
    else:
        grouped = hasattr(scene, "compositing_node_group")
        original = scene.compositing_node_group if grouped else scene.node_tree
    return effects, effect, grouped, original


def outline_color_socket(scene):
    """Expose the installed outline's actual color, including existing saved colors."""
    try:
        _, _, grouped, tree = _target_tree(scene, "mix")
    except ValueError:
        return None
    if not tree or tree.library or (grouped and tree.users > 1):
        return None
    mix = next((node for node in tree.nodes if node.get(MARK) == "mix"), None)
    colors = _color_inputs(mix) if mix else []
    return colors[1] if len(colors) > 1 else None


def apply(scene, options):
    layer = scene.view_layers.get(options["layer"])
    if layer is None:
        raise ValueError("目标视图层已移除")
    action = options["action"]
    if action not in {"COMP_GLOW", "COMP_OUTLINE", "COMP_EXCLUDE", "COMP_RESTORE", "COMP_CLEAR"}:
        raise ValueError("未知合成器操作")
    marker = "glow" if action == "COMP_GLOW" else "mix"
    effects, effect, grouped, original = _target_tree(scene, marker)
    # Node subclasses, menu sockets and even the scene tree owner vary by host.
    tree: Any = original
    if grouped and tree and (tree.library or tree.users > 1):
        raise ValueError("请先将共享合成树设为当前场景独立使用")
    if action in {"COMP_EXCLUDE", "COMP_RESTORE", "COMP_CLEAR"}:
        mask = (
            next((n for n in tree.nodes if n.get(MARK) == "mask"), None)
            if tree
            else None
        )
        if mask is None:
            raise ValueError("请先启用合成器描边")
        return _edit_exclusion(mask, scene, action, options.get("targets", ()))
    existing = (
        next((n for n in tree.nodes if n.get(MARK) == marker), None) if tree else None
    )
    if action == "COMP_OUTLINE" and scene.render.engine == "BLENDER_WORKBENCH":
        raise ValueError("合成器描边需要深度与物体通道，请使用 Eevee 或 Cycles 渲染")
    mask = None
    if existing and action == "COMP_OUTLINE":
        mask = next((n for n in tree.nodes if n.get(MARK) == "mask"), None)
        if mask is None:
            raise ValueError("原描边节点缺少物体遮罩，请检查合成器节点连接")
        mask_layer = mask.layer_name.removesuffix(".CryptoObject")
        layer = scene.view_layers.get(mask_layer) or layer
    before = layer.use_pass_z, layer.use_pass_cryptomatte_object, layer.use_pass_cryptomatte_asset
    old_enabled = scene.use_nodes if not grouped else False
    old_compositing = scene.render.use_compositing
    old_effect_enabled = effect.enable_for_render if effect else True
    old_effect_preview = effect.enable_for_preview if effect else True
    created_effect = None
    nodes = []
    socket = None
    previous_links = []
    enabled_nodes = []

    def new(kind: str, label: str, x: int, y: int) -> Any:
        n = tree.nodes.new(kind)
        nodes.append(n)
        n.label, n.location = label, (x, y)
        return n

    def link(source, destination):
        previous = _source(destination)
        if previous == source:
            return
        tree.links.new(source, destination)
        previous_links.append((destination, previous))

    try:
        scene.render.use_compositing = True
        if action == "COMP_OUTLINE":
            layer.use_pass_z = layer.use_pass_cryptomatte_object = layer.use_pass_cryptomatte_asset = True
            layer.update_render_passes()
        if grouped and tree is None:
            tree = cast(
                Any, bpy.data.node_groups.new("BlockLoom 合成", "CompositorNodeTree")
            )
            if effects is not None:
                if effect is None:
                    created_effect = effect = effects.new("BlockLoom 合成")
                effect.node_group = tree
            else:
                scene.compositing_node_group = tree
        elif not grouped:
            scene.use_nodes = True
            tree = scene.node_tree
        if effect:
            effect.enable_for_render = True
            if action == "COMP_OUTLINE":
                effect.enable_for_preview = True
        output = next(
            (
                n
                for n in tree.nodes
                if n.type == ("GROUP_OUTPUT" if grouped else "COMPOSITE")
                and getattr(n, "is_active_output", True)
            ),
            None,
        )
        if output is None:
            if grouped and not any(
                getattr(s, "in_out", "") == "OUTPUT"
                and s.socket_type == "NodeSocketColor"
                for s in tree.interface.items_tree
            ):
                socket = tree.interface.new_socket(
                    name="Image", in_out="OUTPUT", socket_type="NodeSocketColor"
                )
            output = new(
                "NodeGroupOutput" if grouped else "CompositorNodeComposite",
                "合成输出",
                0,
                0,
            )
        target = next((s for s in output.inputs if s.type == "RGBA"), None)
        if target is None:
            raise ValueError("合成树缺少图像输出")
        if existing:
            if action == "COMP_GLOW":
                if existing.label == "便携辉光":
                    existing.label = "合成辉光"
                existing.mute = False
                return {"message": "已启用合成辉光，可按 F12 查看最终渲染"}
            inverse = None
            if not existing.get("blockloom_depth_revision"):
                inverse = _upgrade_outline(new, link, existing, mask)
            _install_outline_modes(new, link, existing, layer)
            _reconnect_outline(link, existing, target)
            _preview(scene, tree, new, link, target)
            for node in (existing, mask):
                if node is not None and node.mute:
                    enabled_nodes.append(node)
                    node.mute = False
            existing["blockloom_depth_revision"] = 2
            if inverse and not inverse.outputs[0].is_linked and not inverse.parent:
                tree.nodes.remove(inverse)
            return {"message": OUTLINE_READY}
        source = _source(target)
        render = None
        if action == "COMP_OUTLINE" or source is None:
            render = next(
                (
                    n
                    for n in tree.nodes
                    if n.type == "R_LAYERS"
                    and n.scene in {None, scene}
                    and n.layer == layer.name
                ),
                None,
            )
            if render is None:
                render = new("CompositorNodeRLayers", "渲染通道", -1200, -200)
                render.layer = layer.name
            if source is None:
                source = render.outputs["Image"]
        if action == "COMP_GLOW":
            glow = new("CompositorNodeGlare", "合成辉光", -200, 0)
            glow[MARK] = "glow"
            if glow.inputs.get("Type"):
                glow.inputs["Type"].default_value = "Fog Glow"
            else:
                glow.glare_type = "FOG_GLOW"
            link(source, glow.inputs["Image"])
            link(glow.outputs["Image"], target)
            if any(node.get(MARK) == "mix" for node in tree.nodes):
                _preview(scene, tree, new, link, target)
            return {"message": "已启用合成辉光，可按 F12 查看最终渲染"}
        if render is None:
            raise ValueError("合成树缺少渲染通道")
        edge = new("CompositorNodeFilter", "深度边缘", -800, -200)
        if edge.inputs.get("Type"):
            edge.inputs["Type"].default_value = "Laplace"
        else:
            edge.filter_type = "LAPLACE"
        magnitude = new("ShaderNodeMath", "深度变化幅度", -590, -200)
        magnitude.operation = "ABSOLUTE"
        link(edge.outputs[0], magnitude.inputs[0])
        threshold = new("ShaderNodeMath", "深度差阈值", -380, -200)
        threshold.operation = "GREATER_THAN"
        threshold.inputs[1].default_value = 0.002
        link(magnitude.outputs[0], threshold.inputs[0])
        mask = new("CompositorNodeCryptomatteV2", "排除物体", -600, -430)
        mask[MARK] = "mask"
        mask.layer_name = layer.name + ".CryptoObject"
        _add_edge_signals(new, link, render.outputs["Depth"], edge, threshold, mask)
        coverage = new("ShaderNodeMath", "物体覆盖范围", -410, -430)
        coverage.operation = "GREATER_THAN"
        coverage.inputs[1].default_value = 0.001
        link(mask.outputs["Matte"], coverage.inputs[0])
        # The 3×3 depth filter crosses silhouettes. Exclude its exterior pixels too.
        expanded = new("CompositorNodeDilateErode", "包含轮廓外沿", -220, -430)
        expanded.inputs["Size"].default_value = 1
        link(coverage.outputs[0], expanded.inputs["Mask"])
        subtract = new("ShaderNodeMath", "排除遮罩", -10, -200)
        subtract.operation = "SUBTRACT"
        subtract.use_clamp = True
        link(threshold.outputs[0], subtract.inputs[0])
        link(expanded.outputs[0], subtract.inputs[1])
        mix = new(
            "ShaderNodeMix" if grouped else "CompositorNodeMixRGB", "合成器描边", 210, 0
        )
        mix[MARK] = "mix"
        mix["blockloom_depth_revision"] = 2
        if grouped:
            mix.data_type = "RGBA"
        color_inputs = _color_inputs(mix)
        color_inputs[1].default_value = (1, 1, 1, 1)
        link(source, color_inputs[0])
        link(subtract.outputs[0], mix.inputs[0])
        _install_outline_modes(new, link, mix, layer)
        separate = new("CompositorNodeSeparateColor", "原始透明度", -220, 200)
        link(source, separate.inputs[0])
        alpha = new("CompositorNodeSetAlpha", "保留透明背景", 440, 0)
        if alpha.inputs.get("Type"):
            alpha.inputs["Type"].default_value = "Replace Alpha"
        else:
            alpha.mode = "REPLACE_ALPHA"
        link(
            next(s for s in mix.outputs if s.type == "RGBA" and s.enabled),
            alpha.inputs["Image"],
        )
        link(separate.outputs["Alpha"], alpha.inputs["Alpha"])
        link(alpha.outputs[0], target)
        _preview(scene, tree, new, link, target)
    except Exception:
        if tree:
            for destination, previous in reversed(previous_links):
                for connection in list(destination.links):
                    tree.links.remove(connection)
                if previous is not None:
                    tree.links.new(previous, destination)
            for node in enabled_nodes:
                node.mute = True
            for n in reversed(nodes):
                tree.nodes.remove(n)
            if socket:
                tree.interface.remove(socket)
            if grouped and original is None:
                if created_effect is not None and effects is not None:
                    effects.remove(created_effect)
                    effect = None
                elif effect is not None:
                    effect.node_group = None
                elif effects is None:
                    scene.compositing_node_group = None
                bpy.data.node_groups.remove(tree)
        if effect:
            effect.enable_for_render = old_effect_enabled
            effect.enable_for_preview = old_effect_preview
        if not grouped:
            scene.use_nodes = old_enabled
        scene.render.use_compositing = old_compositing
        layer.use_pass_z, layer.use_pass_cryptomatte_object, layer.use_pass_cryptomatte_asset = before
        raise
    return {"message": OUTLINE_READY}
