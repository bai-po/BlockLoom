"""Scene-local style nodes with one reusable, versioned compositor group per file."""

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, cast

import bpy

ROLE = "blockloom_stylized_role"
TEMPLATE = "blockloom_style_template"
VERSION = 2
NAME = "BlockLoom · 风格化描边"
SOURCE = Path(__file__).resolve().parents[2] / "asset" / "outline" / "stylized_outline.blend"
SUBJECT_ACTIONS = {
    "COMP_SUBJECT_ADD": "COMP_EXCLUDE",
    "COMP_SUBJECT_REMOVE": "COMP_RESTORE",
    "COMP_SUBJECT_CLEAR": "COMP_CLEAR",
}
ACTIONS = frozenset({
    "COMP_GLOW", "COMP_OUTLINE", "COMP_STYLE", "COMP_REMOVE",
    "COMP_EXCLUDE", "COMP_RESTORE", "COMP_CLEAR", "COMP_TOGGLE_ENABLE", *SUBJECT_ACTIONS,
})


def role(tree, key):
    return next((n for n in tree.nodes if n.get(ROLE) == key), None) if tree else None


def enable_socket(tree):
    control = role(tree, "enable_control")
    if control:
        return control.inputs["Switch"]
    style = role(tree, "style")
    return style.inputs.get("启用描边") if style else None


def enable_output(style):
    """Expose the original animated socket without moving its links or keyframes."""
    group = style.node_tree
    if not group or not group.get(TEMPLATE):
        raise ValueError("请检查风格化描边节点组")
    name = "启用描边"
    if not style.outputs.get(name):
        if group.library:
            raise ValueError("请先将风格化描边节点组设为本地使用")
        inputs = next(n for n in group.nodes if n.type == "GROUP_INPUT")
        outputs = [n for n in group.nodes if n.type == "GROUP_OUTPUT"]
        if not outputs:
            raise ValueError("风格化描边节点组缺少输出")
        port = group.interface.new_socket(name=name, in_out="OUTPUT", socket_type="NodeSocketBool")
        try:
            for output in outputs:
                group.links.new(inputs.outputs[name], output.inputs[port.identifier])
        except Exception:
            group.interface.remove(port)
            raise
    return style.outputs[name]


def _template(imported):
    for group in bpy.data.node_groups:
        if group.library is None and group.bl_idname == "CompositorNodeTree" and group.get(TEMPLATE) == VERSION:
            return group
    # Only this returned root and its freshly appended group dependencies are ours.
    before = set(bpy.data.node_groups)
    library = cast(AbstractContextManager[tuple[Any, Any]], bpy.data.libraries.load(str(SOURCE), link=False))
    with library as (available, requested):
        if NAME not in available.node_groups:
            raise ValueError("随包资源缺少风格化描边节点组")
        requested.node_groups = [NAME]
    root = cast(Any, requested.node_groups[0])
    pending = [root]
    while pending:
        group = pending.pop()
        if group is not None and group not in before and group not in imported:
            imported.append(group)
            pending.extend(n.node_tree for n in group.nodes if n.type == "GROUP")
    if root is None or root.get(TEMPLATE) != VERSION:
        raise ValueError("风格化描边节点组版本不匹配")
    return root


def discard_unused(imported):
    # A dependency can be shared by two imported groups.
    pending = list(imported)
    while unused := [g for g in pending if not g.users]:
        for group in unused:
            pending.remove(group)
            bpy.data.node_groups.remove(group)


def install(tree, scene, layer, new, link, alpha, mask, mix, source, imported):
    if source is None or not alpha.inputs["Image"].is_linked:
        raise ValueError("请检查描边的原图与透明度连接")
    classic = alpha.inputs["Image"].links[0].from_socket
    render = next((n for n in tree.nodes if n.type == "R_LAYERS" and
                   n.scene in {None, scene} and n.layer == layer.name), None)
    if render is None:
        render = new("CompositorNodeRLayers", "风格化描边通道", -1220, -1320)
        render.layer = layer.name
    template = _template(imported)
    asset = new("CompositorNodeCryptomatteV2", "模型外轮廓", -900, -1290)
    asset[ROLE] = "asset"
    asset.layer_name = layer.name + ".CryptoAsset"
    subject = new("CompositorNodeCryptomatteV2", "主体描边权重", -900, -1640)
    subject[ROLE] = "subject"
    subject.layer_name = layer.name + ".CryptoObject"
    style = new("CompositorNodeGroup", NAME, -430, -1240)
    style[ROLE] = "style"
    style.node_tree = template
    style.width = 270
    control = role(tree, "enable_control")
    if control:
        link(control.outputs[0], style.inputs["启用描边"])
    for name, socket in (
        ("图像", source), ("深度", render.outputs["Depth"]), ("法线", render.outputs["Normal"]),
        ("排除遮罩", mask.outputs["Matte"]), ("物体识别色", mask.outputs["Pick"]),
        ("模型识别色", asset.outputs["Pick"]), ("主体遮罩", subject.outputs["Matte"]),
    ):
        link(socket, style.inputs[name])
    # Seed common controls from the regular outline; switching styles leaves its animation intact.
    colors = [s for s in mix.inputs if s.type == "RGBA" and s.enabled]
    width = next((n for n in tree.nodes if n.get("blockloom_depth_outline") == "outline_width"), None)
    mode = next((n for n in tree.nodes if n.get("blockloom_depth_outline") == "outline_mode"), None)
    for name, old in (
        ("描边颜色", colors[1] if len(colors) > 1 else None),
        ("描边扩展（像素）", width.inputs["Size"] if width else None),
        ("全描边（含内部线条）", mode.inputs["Switch"] if mode else None),
    ):
        if old is not None:
            if old.is_linked:
                link(old.links[0].from_socket, style.inputs[name])
            else:
                style.inputs[name].default_value = old.default_value
    switch = new("CompositorNodeSwitch", "普通 / 风格化描边", 100, -1170)
    switch[ROLE] = "switch"
    switch.inputs["Switch"].default_value = True
    link(classic, switch.inputs["Off"])
    link(style.outputs["图像"], switch.inputs["On"])
    link(switch.outputs[0], alpha.inputs["Image"])
    return switch

