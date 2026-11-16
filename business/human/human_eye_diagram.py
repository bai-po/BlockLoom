"""Preview the selected per-eye expressions on the native eight-head board."""

from array import array
from typing import Any, cast

import bpy

from ...util.geometry_nodes import input_path
from .human_eye_selector import SWITCH, mode_expression, positions
from .human_face_preview import _MODIFIER, _channel, _uv_tree

# First four indices match the native expression selector. The final column
# illustrates an ordinary eyelid closure when the expression is inactive.
COLUMNS = 5
_TILE = 128
_ROLLS = ("画面左眼转角", "画面右眼转角")


def _atlas(transaction, old, original, sprites):
    if tuple(old.size) != (128, 256) or tuple(original.size) != (8,64) or not old.packed_file:
        raise ValueError("面部示意图与原生四态图集不一致：" + old.name)
    data = array("f", [0]) * (128 * 256 * 4)
    old.pixels.foreach_get(data)
    source = array("f", original.pixels[:])
    # Blender may reopen the older packed sheet as scene-linear floats and
    # the source artwork as sRGB bytes. Every normal/brow/lid texel belongs
    # to the source palette. Recover its exact source color before packing.
    palette = {}
    for y in range(64):
        for x in range(8):
            i = (y*4*128 + x*4)*4
            j = (y*8+x)*4
            palette[tuple(data[i:i+4])] = source[j:j+4]
    colors = [(im.size[0], im.size[1], tuple(im.pixels[:])) for im in sprites]
    # White sclera pixels in each open head tile tell us its exact vertical
    # eye window. The unrigged T7 icon uses the original skin's eye rows.
    eye_rows = []
    out = array("f")
    white = array("f", [1, 1, 1, 1])
    def pixel(tile, col, row, cell):
        start = ((tile * 32 + row) * 128 + cell * 32 + col) * 4
        return palette[tuple(data[start:start+4])]

    for tile in range(8):
        rows = [y for y in range(8) if min(pixel(tile, 6, 4*y+2, 0)[:3]) > .99]
        bottom, top = (min(rows), max(rows)+1) if rows else (1, 3)
        eye_rows.append((bottom, top))
        for y in range(_TILE):
            cells = []
            for cell in (0, 1, 3):
                row = array("f")
                for x in range(32):
                    color = pixel(tile, x, y//4, cell)
                    if tile == 0 and cell == 1 and bottom*16 <= y < top*16 and (4 <= x < 12 or 20 <= x < 28):
                        # T7 has no drawn eyes. Borrow only the strokes of the
                        # native T0 expression, preserving T7's base artwork.
                        offset = (((7*32+y//4)*128)+32+x)*4
                        if max(data[offset:offset+3]) < .05:
                            color = palette[tuple(data[offset:offset+4])]
                    row.extend(color * 4)
                cells.append(row)
            glyphs = []
            for width, height, rgba in colors:
                row = white * _TILE
                # Preserve sprite aspect on the short eyes as well as tall eyes.
                side = min((top-bottom)*16, 32)
                y0 = (bottom+top)*8 - side//2
                if y0 <= y < y0+side:
                    v = (y-y0)*height//side
                    for center in (32, 96):
                        start = center - side//2
                        for dx in range(side):
                            i = 4*(v*width+dx*width//side)
                            alpha = rgba[i+3]
                            k = 4*(start+dx)
                            for channel in range(3):
                                row[k+channel] = rgba[i+channel]*alpha + (1-alpha)
                glyphs.append(row)
            for row in (*cells[:2], *glyphs, *cells[2:]):
                out.extend(row)
    sheet = transaction.track(bpy.data.images.new("白珀面控 · 原皮与四档眼部表情", _TILE*COLUMNS, _TILE*8, alpha=True))
    sheet.colorspace_settings.name = old.colorspace_settings.name
    sheet.pixels.foreach_set(out)
    sheet.pack()
    return sheet, tuple(reversed(eye_rows))


def glyph_coordinates(group, src, uv, center_x, state, eye_rows, brow_layers=False):
    """Rotate the inverse UV sample within a head's eye window in texture mode."""
    nodes, link = group.nodes, group.links.new
    for name in _ROLLS:
        group.interface.new_socket(name=name, in_out="INPUT", socket_type="NodeSocketFloat")

    def feed(socket, value):
        if isinstance(value, (int, float)):
            socket.default_value = value
        else:
            link(value, socket)

    def number(op, a, b=0.0):
        n = nodes.new("ShaderNodeMath")
        n.operation = op
        feed(n.inputs[0], a)
        feed(n.inputs[1], b)
        return n.outputs[0]

    def choose(kind, condition, a, b):
        n = nodes.new("GeometryNodeSwitch")
        n.input_type = kind
        link(condition, n.inputs["Switch"])
        feed(n.inputs["False"], a)
        feed(n.inputs["True"], b)
        return n.outputs[0]

    # Select by face centre to avoid interpolating the face and eye tiles
    # across their shared edges. The diagram already has one quad per texel.
    face_uv = nodes.new("GeometryNodeFieldOnDomain")
    face_uv.data_type, face_uv.domain = "FLOAT_VECTOR", "FACE"
    link(uv, face_uv.inputs["Value"])
    face_axes = nodes.new("ShaderNodeSeparateXYZ")
    link(face_uv.outputs["Value"], face_axes.inputs[0])
    right = number("GREATER_THAN", center_x, .5)
    middle_x = choose("FLOAT", right, .25, .75)
    limits = nodes.new("GeometryNodeIndexSwitch")
    limits.data_type = "VECTOR"
    while len(limits.index_switch_items) < len(eye_rows):
        limits.index_switch_items.new()
    link(src.outputs["头型"], limits.inputs["Index"])
    for index, (bottom, top) in enumerate(eye_rows):
        limits.inputs[index+1].default_value = (bottom/8, top/8, (bottom+top)/16)
    axes = nodes.new("ShaderNodeSeparateXYZ")
    link(limits.outputs[0], axes.inputs[0])
    x_in = number("LESS_THAN", number("ABSOLUTE", number("SUBTRACT", center_x, middle_x)), .125)
    y_in = number("MULTIPLY", number("GREATER_THAN", face_axes.outputs["Y"], axes.outputs["X"]),
                  number("LESS_THAN", face_axes.outputs["Y"], axes.outputs["Y"]))
    in_eye = number("MULTIPLY", x_in, y_in)
    glyph = number("MULTIPLY", number("GREATER_THAN", state, 1.5), number("LESS_THAN", state, 3.5))
    use_glyph = number("MULTIPLY", glyph, in_eye)
    panel = choose("INT", glyph, state, choose("INT", in_eye, 0, state))
    if brow_layers:
        for name in ("眉毛模式", "黑脸"):
            group.interface.new_socket(name=name, in_out="INPUT", socket_type="NodeSocketBool")
        group.interface.new_socket(name="独眼标记", in_out="INPUT", socket_type="NodeSocketInt")
        closed_surface = number("MULTIPLY", use_glyph, src.outputs["眉毛模式"])
        panel = number("ADD", panel, number("MULTIPLY", closed_surface, 3))
        closed_lid = number("COMPARE", state, 4)
        panel = choose("INT", src.outputs["黑脸"], panel, number("ADD", 7, closed_lid))
        raw = src.outputs["独眼标记"]
        marked = choose("BOOLEAN", right, number("LESS_THAN", raw, 0), number("GREATER_THAN", raw, 0))
        # On the raw side omit the normal illustrated eye. An explicitly
        # enabled expression is still drawn on top of that skin.
        plain = number("MULTIPLY", marked, number("MAXIMUM", src.outputs["黑脸"],
                        number("MAXIMUM", number("COMPARE", state, 0), closed_lid)))
        panel = choose("INT", plain, panel, number("ADD", 9, src.outputs["黑脸"]))
    pivot = nodes.new("ShaderNodeCombineXYZ")
    link(middle_x, pivot.inputs["X"])
    link(axes.outputs["Z"], pivot.inputs["Y"])
    rot = nodes.new("ShaderNodeVectorRotate")
    rot.rotation_type, rot.invert = "Z_AXIS", True
    link(uv, rot.inputs["Vector"])
    link(pivot.outputs[0], rot.inputs["Center"])
    link(choose("FLOAT", right, src.outputs[_ROLLS[0]], src.outputs[_ROLLS[1]]), rot.inputs["Angle"])
    result = nodes.new("ShaderNodeSeparateXYZ")
    link(choose("VECTOR", use_glyph, uv, rot.outputs[0]), result.inputs[0])
    return panel, result


def _state_driver(transaction, obj, rig, mod, name):
    socket = mod.node_group.interface.items_tree[name]
    path = input_path(mod, socket.identifier)
    curve = obj.animation_data.drivers.find(path)
    if not curve:
        raise ValueError("原生示意图缺少逐眼驱动：" + name)
    driver, original = curve.driver, curve.driver.expression
    if not original.startswith("2 if raw ") or " else (1 if (" not in original:
        raise ValueError("示意图眼部驱动已经自定义：" + name)
    if any(v.name in {"emote", "roll"} or not v.targets or v.targets[0].id != rig for v in driver.variables):
        raise ValueError("示意图眼部控制引用冲突：" + name)
    mode = mode_expression(driver, rig)
    added = driver.variables[-1]
    transaction.on_rollback(lambda: driver.variables.remove(added))
    transaction.on_rollback(lambda: setattr(driver, "expression", original))
    # Use the old native enable condition, including any legacy master keys.
    # The illustrated board is independent of the separate skin switch.
    _, enabled = original.split(" else (1 if (", 1)
    enabled, tail = enabled.rsplit(") else ", 1)
    if tail != "(3 if lid <= -0.075 else 0))":
        raise ValueError("无法迁移已编辑的示意图驱动：" + name)
    raw = driver.variables.get("raw")
    if not raw or raw.type != "TRANSFORMS" or raw.targets[0].bone_target != "单眼皮肤":
        raise ValueError("无法迁移示意图的原皮控制：" + name)
    driver.variables.remove(raw)
    transaction.on_rollback(lambda: _channel(driver, "raw", rig, "单眼皮肤", "LOC_X"))
    halfway = positions(rig)[1] / 2
    driver.expression = f"{mode} if ({enabled}) and emote<={halfway:.9g} else (4 if lid<=-0.075 else 0)"


def install(transaction, rig, objects):
    from .human_eye_glyphs import sprite_images

    if SWITCH not in rig.pose.bones:
        return
    thumbnails = [(obj, obj.modifiers.get(_MODIFIER)) for obj in objects if obj.modifiers.get(_MODIFIER)]
    if len(thumbnails) != 1:
        raise ValueError("表情滑轨需要唯一的人模示意图：" + rig.name)
    obj, mod = thumbnails[0]
    material = obj.active_material
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned
        require_owned(transaction.owner, (obj, material))
        require_no_external_users(cast(Any, bpy.context.scene), transaction.owner, (material,))
    image_node = material.node_tree.nodes.get("头型与左右眼示意") if material and material.node_tree else None
    if not image_node or not image_node.image or not obj.data.uv_layers.active:
        raise ValueError("示意图已更换原生图像或 UV：" + obj.name)
    previous_tree, previous_sheet = mod.node_group, image_node.image
    setters = [n for n in previous_tree.nodes if n.bl_idname == "GeometryNodeSetMaterial"]
    if len(setters) != 1 or not setters[0].inputs["Material"].default_value:
        raise ValueError("示意图的原皮材质已更改：" + obj.name)
    originals = [n.image for n in material.node_tree.nodes if n.type == "TEX_IMAGE" and n.image and tuple(n.image.size) == (8,64)]
    if len(originals) != 1:
        raise ValueError("找不到唯一的原生八型头像图：" + obj.name)
    sheet, rows = _atlas(transaction, previous_sheet, originals[0], sprite_images(transaction))
    group, identifiers = _uv_tree(transaction, obj.data.uv_layers.active.name, sheet, None, eye_rows=rows)
    for name, key in zip(("头型", "画面左眼", "画面右眼"), identifiers):
        if key != previous_tree.interface.items_tree[name].identifier:
            raise ValueError("示意图节点组端口已更改：" + name)
    transaction.on_rollback(lambda: setattr(mod, "node_group", previous_tree))
    mod.node_group = group
    transaction.on_rollback(lambda: setattr(image_node, "image", previous_sheet))
    image_node.image = sheet
    for name, rotation, side in zip(("画面左眼", "画面右眼"), _ROLLS, ("R", "L")):
        _state_driver(transaction, obj, rig, mod, name)
        path = input_path(mod, group.interface.items_tree[rotation].identifier)
        driver = obj.driver_add(path).driver
        transaction.on_rollback(lambda p=path: obj.driver_remove(p))
        _channel(driver, "roll", rig, side + ".眼睛", "ROT_Y")
        driver.variables["roll"].targets[0].rotation_mode = "YXZ"
        driver.expression = "roll"


def _brow_atlas(transaction, sheet, original):
    from .human_eye_glyphs import _heart, _spiral

    if tuple(sheet.size) != (_TILE*COLUMNS, _TILE*8) or tuple(original.size)!=(8,64):
        raise ValueError("示意图不再使用原生五列眼睛图集：" + sheet.name)
    artwork = array("f", original.pixels[:])
    source = array("f", [0]) * (_TILE*COLUMNS*_TILE*8*4)
    sheet.pixels.foreach_get(source)
    output = array("f")
    rows = []
    sprites = ((9,_heart()),(15,_spiral()))
    width = _TILE*4

    def tint(c, scale):
        linear = c/12.92 if c<=.04045 else ((c+.055)/1.055)**2.4
        linear *= scale
        return 12.92*linear if linear<=.0031308 else 1.055*linear**(1/2.4)-.055

    patch = (
        "            ++",
        "  ++++++++ +# ",
        " +##########  ",
        " +#======#+   ",
        " +####### +   ",
        "  +######+    ",
        "   +####+     ",
        "    ++++      ",
    )
    patch_colors = {key:array("f", (*rgb,1)) for key,rgb in (
        ("+",(.57,.58,.62)), ("#",(.11,.12,.15)), ("=",(.29,.30,.34)))}
    for tile in range(8):
        def swatch(x, y, tile=tile):
            i = ((tile*8+y)*8+x)*4
            return artwork[i:i+4]
        visible = [y for y in range(8) if min(swatch(1,y)[:3])>.99]
        bottom, top = (min(visible), max(visible)+1) if visible else (1,3)
        rows.append((bottom,top))
        side = min((top-bottom)*16,32)
        y0 = (bottom+top)*8-side//2
        ink = swatch(2,bottom) if visible else artwork[((7*8+1)*8+2)*4:((7*8+1)*8+2)*4+4]
        for y in range(_TILE):
            base = (tile*_TILE+y)*width*COLUMNS
            cells = [source[base+i*width:base+(i+1)*width] for i in range(COLUMNS)]
            skin = swatch(3, max(bottom,min(top-1,y//16)))
            # A short eye still gets two distinct diagonal strokes in its own
            # window. The normal, unmodified head drawing remains column zero.
            if bottom*16<=y<top*16:
                v = (y+.5-bottom*16)/((top-bottom)*16)
                for x0 in (16,80):
                    for dx in range(32):
                        u = (dx+.5)/32 if x0==16 else 1-(dx+.5)/32
                        mark = .095<=u<=.885 and abs(abs(v-.5)-.37*(.89-u)/.80)<.105
                        i=(x0+dx)*4
                        cells[1][i:i+4]=ink if mark else skin
            closed = []
            for idx,(n,pixels) in enumerate(sprites):
                row = skin*_TILE
                if y0 <= y < y0+side:
                    iy=(y-y0)*n//side
                    for pivot in (32,96):
                        for dx in range(side):
                            if pixels[(iy*n+dx*n//side)*4+3]:
                                i=(pivot-side//2+dx)*4
                                row[i:i+4]=cells[2+idx][i:i+4]
                closed.append(row)
            t = min(1,max(0,((y+.5)/_TILE-.06)/.54))
            scale = 1-.90*t*t*(3-2*t)
            lut = {c:tint(c,scale) for c in set(cells[0]) | set(cells[4])}
            shaded = []
            for row in (cells[0],cells[4]):
                shaded.append(array("f",(v if i%4==3 else lut[v] for i,v in enumerate(row))))
            marked = [array("f", cells[0]), array("f", shaded[0])]
            py = y-top*16
            if 0<=py<2*len(patch):
                pattern = patch[-1-py//2]
                for i,symbol in enumerate(pattern):
                    if symbol not in patch_colors:
                        continue
                    for x in (9+2*i, 10+2*i):
                        for at in (x, _TILE-1-x):
                            for row in marked:
                                row[at*4:at*4+4] = patch_colors[symbol]
            for data in (*cells,*closed,*shaded,*marked):
                output.extend(data)
    image = transaction.track(bpy.data.images.new("白珀面控 · 五档表情与闭眼示意", _TILE*11, _TILE*8, alpha=True))
    image.colorspace_settings.name = sheet.colorspace_settings.name
    image.pixels.foreach_set(output)
    image.pack()
    return image, tuple(reversed(rows))


def upgrade_brow_preview(transaction, rig, objects):
    from .human_eye_selector import BROW_SWITCH, BROW_THRESHOLD

    boards = [(obj, obj.modifiers[_MODIFIER]) for obj in objects if obj.modifiers.get(_MODIFIER)]
    if len(boards)!=1:
        raise ValueError("缺少独立的原生面部示意图：" + rig.name)
    obj, mod = boards[0]
    material = obj.active_material
    if transaction.owner is not None:
        from ...util.ownership import require_no_external_users, require_owned

        require_owned(transaction.owner,(obj,material))
        require_no_external_users(cast(Any,bpy.context.scene),transaction.owner,(material,))
    nodes = material.node_tree.nodes
    node = nodes.get("头型与左右眼示意")
    originals = [n.image for n in nodes if n.type=="TEX_IMAGE" and n.image and tuple(n.image.size)==(8,64)]
    if not node or not node.image or len(originals)!=1:
        raise ValueError("人模示意图的原始图层已更改：" + obj.name)
    old, previous = node.image, mod.node_group
    sheet, rows = _brow_atlas(transaction,old,originals[0])
    group, _ = _uv_tree(transaction,obj.data.uv_layers.active.name,sheet,None,eye_rows=rows,brow_layers=True)
    for name in ("头型","画面左眼","画面右眼",*_ROLLS):
        if group.interface.items_tree[name].identifier!=previous.interface.items_tree[name].identifier:
            raise ValueError("示意图的既有动画端口发生变化：" + name)
    transaction.on_rollback(lambda: setattr(node,"image",old))
    transaction.on_rollback(lambda: setattr(mod,"node_group",previous))
    node.image, mod.node_group = sheet, group
    black_edge = (positions(rig)[3]+positions(rig)[4])/2
    for name in ("画面左眼","画面右眼"):
        path = input_path(mod, group.interface.items_tree[name].identifier)
        driver = obj.animation_data.drivers.find(path).driver
        if not all(v in driver.variables for v in ("emote","lid")):
            raise ValueError("小头像的表情驱动已被自定义：" + name)
        prev = driver.expression
        transaction.on_rollback(lambda d=driver,p=prev:setattr(d,"expression",p))
        driver.expression = f"(4 if lid<=-0.075 else 0) if emote<={black_edge:.9g} else ({prev})"
    for name,bone,value in (("眉毛模式",BROW_SWITCH,BROW_THRESHOLD),("黑脸",SWITCH,black_edge)):
        path = input_path(mod,group.interface.items_tree[name].identifier)
        d = obj.driver_add(path).driver
        transaction.on_rollback(lambda p=path:obj.driver_remove(p))
        _channel(d,"switch",rig,bone,"LOC_Z")
        d.expression = f"switch<={value:.9g}"
    from .human_face_single import SWITCH as RAW_SWITCH
    from .human_face_single import threshold

    raw_path = input_path(mod,group.interface.items_tree["独眼标记"].identifier)
    driver = obj.driver_add(raw_path).driver
    transaction.on_rollback(lambda:obj.driver_remove(raw_path))
    _channel(driver,"slide",rig,RAW_SWITCH,"LOC_X")
    at = threshold(rig)
    driver.expression = f"-1 if slide<={-at:.9g} else (1 if slide>={at:.9g} else 0)"
