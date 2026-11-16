"""Native eye-state thumbnail and depth alignment for the two expression switches."""

from typing import cast

import bpy
from mathutils import Vector

from ...util.geometry_nodes import input_path
from .human_face_controls import TRACK, _keep, bounds
from .human_face_switches import drive_switch

# A head tile is eight Minecraft pixels square. Four subpixels per source
# pixel keep the diagonal brows legible on the small control-board thumbnail.
_TILE = 32
_STATES = 4  # open, expression brow, raw skin, closed lid
_MODIFIER = "图示 · 头型与左右眼"


def bind_instance_skin(root, transaction, copies):
    """Keep the preview's native Set Material in step with private mesh slots."""
    from .human_source import SKIN_ROLE

    for obj in root.all_objects:
        mod = obj.modifiers.get(_MODIFIER)
        if not mod or mod.type != "NODES" or not mod.node_group:
            continue
        group = mod.node_group
        if not any(n.bl_idname == "GeometryNodeSetMaterial" for n in group.nodes):
            continue  # The newer illustrated board doesn't sample the player's skin.
        if group not in transaction.owned:
            group = group.copy()
            transaction.track(group)
            mod.node_group = group
        for node in group.nodes:
            if node.bl_idname != "GeometryNodeSetMaterial":
                continue
            socket = node.inputs["Material"]
            source = socket.default_value
            if source and source.get(SKIN_ROLE) == "skin":
                if source not in copies:
                    raise ValueError("原皮示意图的材质与所导入人模不一致：" + obj.name)
                socket.default_value = copies[source]


def _align_switches(transaction, rig):
    bones = rig.pose.bones
    handle_y = bounds(bones["面部兼容"])[1][0]
    frame_y = bounds(bones["面部切换框"])[1][0]
    gap = abs(frame_y - handle_y)
    for name, depth in ((TRACK, frame_y + gap), ("L.眉纹切换", frame_y), ("R.眉纹切换", frame_y)):
        p = bones[name]
        front = bounds(p)[1][0]
        unit = p.bone.length if p.use_custom_shape_bone_size else 1.0
        basis = (p.custom_shape_transform or p).bone.matrix_local.to_3x3()
        _keep(transaction, p, "custom_shape_translation")
        p.custom_shape_translation += basis.inverted() @ Vector((0, (depth - front) / unit, 0))


def _sheet(transaction, source):
    """Composite eye states from the native packed artwork; never edit its pixels."""
    if tuple(source.size) != (8, 64):
        raise ValueError("无法识别面部示意图的八型图集：" + source.name)
    pixels = tuple(source.pixels[:])

    def sample(x, y):
        offset = (y * 8 + x) * 4
        return pixels[offset:offset + 4]

    rows = []
    for head in range(8):
        # Images count up from the bottom; T0 is the top tile. Read the eye
        # rows from the white sclera in the original icon for this head.
        base = (7 - head) * 8
        active = [y for y in range(8) if min(sample(1, base + y)[:3]) > .99]
        for row in range(_TILE):
            y = row * 8 // _TILE
            for state in range(_STATES):
                for col in range(_TILE):
                    x = col * 8 // _TILE
                    color = sample(x, base + y)
                    if state == 2 and y in (active or [1, 2]) and x in (1, 2, 5, 6):
                        # The zero-alpha cells are filled by the rig's actual skin
                        # material in the native diagram modifier, not a skin swatch.
                        color = (*color[:3], 0)
                    elif active and y in active and x in (1, 2, 5, 6) and state:
                        color = sample(3, base + y)
                        horizontal = ((col + .5) / 4 - 1) / 2
                        if x >= 5:
                            horizontal = (7 - (col + .5) / 4) / 2
                        vertical = ((row + .5) / 4 - min(active)) / len(active)
                        chevron = abs(abs(vertical - .5) - (.36 * (1 - horizontal))) < .15
                        closed = abs(vertical - .45) <= .14
                        if (state == 1 and chevron) or (state == 3 and closed):
                            color = sample(2, base + min(active))
                    rows.extend(color)
    # Our loop visits tiles in head order, whereas Blender's row 0 is T7.
    stride = _TILE * _TILE * _STATES * 4
    ordered = [v for i in reversed(range(8)) for v in rows[i*stride:(i+1)*stride]]
    img = transaction.track(bpy.data.images.new("白珀面控 · 示意图眼部合成", _TILE * _STATES, _TILE * 8, alpha=True))
    img.colorspace_settings.name = source.colorspace_settings.name
    img.pixels.foreach_set(ordered)
    img.pack()
    return img


def _uv_tree(transaction, uv_name, sheet, skin, *, eye_rows=None, brow_layers=False):
    group = transaction.track(bpy.data.node_groups.new("白珀面控 · 示意图分区", "GeometryNodeTree"))
    def socket(name, kind, direction="INPUT"):
        return group.interface.new_socket(name=name, in_out=direction, socket_type=kind)
    socket("几何", "NodeSocketGeometry")
    controls = [socket(name, "NodeSocketInt") for name in ("头型", "画面左眼", "画面右眼")]
    socket("几何", "NodeSocketGeometry", "OUTPUT")
    nodes, link = group.nodes, group.links.new
    src, out = nodes.new("NodeGroupInput"), nodes.new("NodeGroupOutput")
    sub = nodes.new("GeometryNodeSubdivideMesh")
    sub.inputs["Level"].default_value = 3  # One face per icon pixel.
    link(src.outputs["几何"], sub.inputs["Mesh"])
    uv = nodes.new("GeometryNodeInputNamedAttribute")
    uv.data_type = "FLOAT_VECTOR"
    uv.inputs["Name"].default_value = uv_name
    axes = nodes.new("ShaderNodeSeparateXYZ")
    link(uv.outputs["Attribute"], axes.inputs[0])
    # Select by face centre, so that corner UVs on the central seam follow
    # their own half instead of interpolating across two different states.
    center = nodes.new("GeometryNodeFieldOnDomain")
    center.data_type, center.domain = "FLOAT", "FACE"
    link(axes.outputs["X"], center.inputs["Value"])

    def math_node(op, a, b):
        n = nodes.new("ShaderNodeMath")
        n.operation = op
        for input_socket, value in zip(n.inputs, (a, b)):
            if isinstance(value, (float, int)):
                input_socket.default_value = value
            else:
                link(value, input_socket)
        return n.outputs[0]

    state = nodes.new("GeometryNodeSwitch")
    state.input_type = "INT"
    link(math_node("GREATER_THAN", center.outputs["Value"], .5), state.inputs["Switch"])
    link(src.outputs["画面左眼"], state.inputs["False"])
    link(src.outputs["画面右眼"], state.inputs["True"])
    panel = state.outputs[0]
    columns = _STATES
    if eye_rows is not None:
        from .human_eye_diagram import COLUMNS, glyph_coordinates
        columns = sheet.size[0] // 128 if brow_layers else COLUMNS
        panel, axes = glyph_coordinates(group, src, uv.outputs["Attribute"], center.outputs["Value"], panel,
                                       eye_rows, brow_layers)
    x = math_node("DIVIDE", math_node("ADD", axes.outputs["X"], panel), columns)
    y = math_node("DIVIDE", math_node("ADD", axes.outputs["Y"], math_node("SUBTRACT", 7, src.outputs["头型"])), 8)
    vector = nodes.new("ShaderNodeCombineXYZ")
    link(x, vector.inputs["X"])
    link(y, vector.inputs["Y"])
    if skin is None:
        store = nodes.new("GeometryNodeStoreNamedAttribute")
        store.domain, store.data_type = "CORNER", "FLOAT2"
        store.inputs["Name"].default_value = uv_name
        link(sub.outputs["Mesh"], store.inputs["Geometry"])
        link(vector.outputs[0], store.inputs["Value"])
        link(store.outputs["Geometry"], out.inputs["几何"])
        return group, [s.identifier for s in controls]

    # Sample the atlas mask at one centre per face. In particular every
    # corner on a skin/art border must follow its own face's UV space.
    cell = nodes.new("GeometryNodeFieldOnDomain")
    cell.data_type, cell.domain = "FLOAT_VECTOR", "FACE"
    link(vector.outputs[0], cell.inputs["Value"])
    mask = nodes.new("GeometryNodeImageTexture")
    mask.interpolation = "Closest"
    mask.inputs["Image"].default_value = sheet
    link(cell.outputs["Value"], mask.inputs["Vector"])
    raw = math_node("LESS_THAN", mask.outputs["Alpha"], .5)
    material = nodes.new("GeometryNodeSetMaterial")
    material.inputs["Material"].default_value = skin
    link(sub.outputs[0], material.inputs["Geometry"])
    link(raw, material.inputs["Selection"])

    # Minecraft head front is [8:16, 8:16] in a 64-pixel atlas. Feeding
    # the normalized UVs to the body material also handles a 128-pixel skin
    # and any replacement image already bound to this instance.
    skin_uv = nodes.new("ShaderNodeVectorMath")
    skin_uv.operation = "MULTIPLY_ADD"
    link(uv.outputs["Attribute"], skin_uv.inputs[0])
    skin_uv.inputs[1].default_value = (.125, .125, 0)
    skin_uv.inputs[2].default_value = (.125, .75, 0)
    coords = nodes.new("GeometryNodeSwitch")
    coords.input_type = "VECTOR"
    link(raw, coords.inputs["Switch"])
    link(vector.outputs[0], coords.inputs["False"])
    link(skin_uv.outputs[0], coords.inputs["True"])
    store = nodes.new("GeometryNodeStoreNamedAttribute")
    store.domain, store.data_type = "CORNER", "FLOAT2"
    store.inputs["Name"].default_value = uv_name
    link(material.outputs["Geometry"], store.inputs["Geometry"])
    link(coords.outputs[0], store.inputs["Value"])
    link(store.outputs["Geometry"], out.inputs["几何"])
    return group, [s.identifier for s in controls]


def _channel(driver, label, rig, name, transform):
    v = driver.variables.new()
    v.name, v.type = label, "TRANSFORMS"
    target = v.targets[0]
    target.id, target.bone_target = rig, name
    target.transform_type, target.transform_space = transform, "LOCAL_SPACE"


def _thumbnail(transaction, obj, rig, skin):
    from .human_face_single import SWITCH, threshold

    material = obj.active_material
    tree = material.node_tree
    shader, = [n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"]
    color = shader.inputs["Base Color"]
    old, = [link.from_socket for link in color.links]
    if old.node.type != "TEX_IMAGE" or not old.node.image or len(obj.data.polygons) != 1 or not obj.data.uv_layers.active:
        raise ValueError("面部示意图的材质或网格已经自定义：" + obj.name)
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned
        require_owned(transaction.owner, (obj, material))
        require_no_external_users(cast(bpy.types.Scene, bpy.context.scene), transaction.owner, (material,))
    sheet = _sheet(transaction, old.node.image)
    active = tree.nodes.active
    image = tree.nodes.new("ShaderNodeTexImage")
    transaction.on_rollback(lambda: tree.nodes.remove(image))
    image.image = sheet
    image.name = "头型与左右眼示意"
    image.interpolation = "Closest"
    image.location = (shader.location.x - 300, shader.location.y + 280)
    transaction.on_rollback(lambda: setattr(tree.nodes, "active", active))
    tree.nodes.active = image  # Workbench Texture uses the active image.
    tree.links.new(image.outputs["Color"], color)
    transaction.on_rollback(lambda: tree.links.new(old, color))

    group, identifiers = _uv_tree(transaction, obj.data.uv_layers.active.name, sheet, skin)
    mod = obj.modifiers.new(_MODIFIER, "NODES")
    transaction.on_rollback(lambda: obj.modifiers.remove(mod))
    mod.node_group = group
    for idx, slot in enumerate(identifiers):
        path = input_path(mod, slot)
        if idx == 0:
            d = obj.driver_add(path).driver
            _channel(d, "head", rig, "面部兼容", "LOC_Z")
            d.expression = "min(7,max(0,floor(-head*25+0.00001)))"
        else:
            side = "R" if idx == 1 else "L"
            switch = rig.pose.bones[side + ".眉纹切换"]
            bounds_x = switch.constraints[0]
            stroke = max(abs(bounds_x.min_x), abs(bounds_x.max_x))
            # Reuse the native brow switch, including any preserved legacy keys.
            drive_switch(obj, path, rig, side, stroke)
            d = obj.animation_data.drivers.find(path).driver
            brow = d.expression
            _channel(d, "raw", rig, SWITCH, "LOC_X")
            _channel(d, "lid", rig, side + ".眼皮", "LOC_Z")
            raw = ("raw <= -" if side == "R" else "raw >= ") + f"{threshold(rig):.9g}"
            d.expression = f"2 if {raw} else (1 if ({brow}) else (3 if lid <= -0.075 else 0))"
        transaction.on_rollback(lambda p=path: obj.driver_remove(p))


def install(transaction, rig, objects):
    """Keep the source diagram intact and animate the private preview on this rig."""
    from .human_face_single import SWITCH
    from .human_source import SKIN_ROLE

    _align_switches(transaction, rig)
    # Some species authoring files contain the old human reference image.
    # Only the rig with a native skin selector owns a dynamic thumbnail.
    if SWITCH not in rig.pose.bones:
        return
    thumbs = [o for o in objects if o.type == "MESH" and o.active_material and "面部兼容图片" in o.active_material.name]
    if len(thumbs) > 1:
        raise ValueError("面部示意图归属不唯一：" + rig.name)
    if thumbs:
        skins = {slot.material for obj in objects for slot in obj.material_slots
                 if slot.material and slot.material.get(SKIN_ROLE) == "skin"}
        if len(skins) != 1:
            raise ValueError("示意图需要唯一的原生人模皮肤材质：" + rig.name)
        _thumbnail(transaction, thumbs[0], rig, skins.pop())
