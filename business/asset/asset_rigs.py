"""Instance-private native armatures for authored library presentation.

The local.31 bone and widget names are a data contract. Geometry is built by
the new resource backend, then its temporary part hierarchy is compiled into
native pose bones. No legacy module, handler or runtime is imported.
"""

import json
import math
from contextlib import contextmanager
from typing import cast

import bpy
from bpy.types import ViewLayer
from mathutils import Matrix, Vector

from ...core.runtime import ensure_mutation_allowed

ITEMS = frozenset({
    "lead", "trident", "spyglass", "shield", "fishing_rod",
    *(f"{material}_spear" for material in
      ("wooden", "stone", "copper", "iron", "golden", "diamond", "netherite")),
    *(f"{material}_sword" for material in
      ("wooden", "stone", "copper", "iron", "golden", "diamond", "netherite")),
})
LABELS: dict[str, str] = {
    "left_paddle": "左船桨", "right_paddle": "右船桨", "lid": "箱盖",
    "lock": "锁扣", "chest_lid": "箱盖", "chest_lock": "锁扣",
    "left_lid": "左封面", "right_lid": "右封面", "left_pages": "左页块",
    "right_pages": "右页块", "flip_page1": "翻页一", "flip_page2": "翻页二",
    "seam": "书脊", "outer_glass": "外层水晶", "inner_glass": "内层水晶",
    "cube": "水晶核心", "jaw": "下颌", "OXAL hinge": "门体开合",
    "OXAL piston head": "活塞头伸缩", "OXAL piston rod": "活塞杆长度",
    "OXAL gate left": "左扇开合", "OXAL gate right": "右扇开合",
}
SOURCE_NAMES: dict[str, str] = {
    "jaw": "下颌", "OXAL hinge": "门体开合",
    "OXAL piston head": "活塞头伸缩", "OXAL piston rod": "活塞杆长度",
    "OXAL gate left": "左扇开合", "OXAL gate right": "右扇开合",
}
ALIASES = {
    "auto_paddles": ("自动划桨", 0.0, 1.0, "自动划桨"),
    "banner_motion": ("旗帜摆动", 0.0, 1.0, "旗帜动画"),
    "banner_wall": ("挂墙",0.0,1.0,"挂墙 / 立地"),
    "dragon_motion": ("龙首动画", 0.0, 1.0, "龙首动画"),
    "body_visible": ("本体显示204", 0.0, 1.0, "本体显示"),
    "line_cast": ("鱼线展开", 0.0, 1.0, "鱼线与鱼钩"),
    "rope_visible": ("绳索显示", 0.0, 1.0, "绳索显示"),
    "fence_variant": ("栅栏拴绳变体42", 0.0, 1.0, "拴绳变体"),
    "bed_color": ("状态 · 床颜色", 0.0, 15.0, "床颜色"),
    "open": ("开启度", 0.0, 1.0, "打开 / 关闭"),
    "progress": ("活塞伸出", 0.0, 1.0, "活塞伸缩"),
    "jaw_open": ("龙首张嘴", 0.0, 1.0, "龙首张嘴"),
    "book_open": ("书本开合", 0.0, 1.0, "书本打开"),
    "book_hover": ("漂浮幅度", 0.0, 0.5, "书本漂浮"),
    "book_speed": ("翻页速度", 0.0, 10.0, "翻页速度"),
    "runes_enabled": ("符文粒子", 0.0, 1.0, "符文动画"),
    "beam_enabled": ("开启信标", 0.0, 1.0, "信标开关"),
    "beam_height": ("光束高度", 0.0, 64.0, "光束高度"),
    "beam_radius": ("光束宽度", 0.01, 2.0, "光束宽度"),
    "beam_brightness": ("光束亮度", 0.0, 20.0, "光束亮度"),
    "crystal_spin": ("自动水晶动画", 0.0, 1.0, "水晶动画"),
    "crystal_speed": ("水晶动画速度", 0.0, 10.0, "水晶速度"),
}


def control_bone_name(key):
    """Retain literal authored names where a control is not prefix-based."""
    return "绳索显示" if key == "rope_visible" else "控制 · " + ALIASES[key][3]


def requires_rig(identifier):
    from .asset_extensions import requires_rig as extension_rig
    from .asset_states import has_states
    short = identifier.split(":")[-1]
    return short in ITEMS or short in {
        "armor_stand", "end_crystal", "enchanting_table", "beacon", "bell",
        "dragon_head", "end_rod", "piston", "sticky_piston", "bow", "crossbow",
        "clock", "compass", "recovery_compass", "sunflower", "item_frame",
        "glow_item_frame",
    } or short.endswith((
        "_bed", "_boat", "_raft", "minecart", "chest", "shulker_box", "_banner",
        "_door", "_trapdoor", "_fence_gate", "_shelf", "copper_golem_statue",
    )) or has_states(identifier) or extension_rig(identifier)


def build_subrigs(transaction, scene, collection, root, identifier):
    if root.get("oxal_mc_studio_native_prefab"):
        return
    if identifier.split(":")[-1] != "enchanting_table":
        return
    orbit = next((obj for obj in root.children if obj.type == "EMPTY" and obj.name.startswith("OXAL enchanting book")), None)
    if orbit is None:
        raise ValueError("附魔台缺少原生书本机构")
    data = transaction.track(bpy.data.armatures.new("附魔台书本 · 骨架"))
    book = transaction.track(bpy.data.objects.new("附魔台 · 浮动书控制器", data))
    collection.objects.link(book)
    book.parent = root
    orbit.parent = book
    build(transaction, scene, collection, book, identifier)
    book["资产ID"] = "附魔台书本"
    root["内部书本骨架"] = book


def _manual_part(identifier, name):
    short = identifier.split(":")[-1]
    if short.endswith(("_boat", "_raft")):
        return name in {"left_paddle","right_paddle","chest_lid","chest_lock"}
    if short.endswith("chest"):
        return name in {"lid", "lock"}
    if short.endswith("minecart"):
        return name.endswith(("lid", "lock"))
    if short.endswith("shulker_box"):
        return name == "lid"
    if short.endswith("_banner"):
        return name.endswith(("_flag", "_pole", "_bar"))
    if short == "armor_stand":
        return name in {"head", "body", "left_arm", "right_arm", "left_leg", "right_leg"}
    if short == "end_crystal":
        return name in {"outer_glass", "inner_glass", "cube"}
    if short == "enchanting_table":
        return name in {"left_lid", "right_lid", "left_pages", "right_pages", "flip_page1", "flip_page2", "seam"}
    if short == "dragon_head":
        return name == "下颌"
    if short.endswith(("_door", "_trapdoor")):
        return name == "门体开合"
    return False


@contextmanager
def _edit(root, scene):
    """Use the actual active scene; restore selection even when staging fails."""
    ensure_mutation_allowed()
    if bpy.context.scene != scene or bpy.context.mode not in {"OBJECT","POSE"}:
        raise ValueError("请在目标场景的物体或姿态模式构建资产")
    layer = cast(ViewLayer, bpy.context.view_layer)
    active = layer.objects.active
    selected = tuple(bpy.context.selected_objects)
    previous_mode = bpy.context.mode
    if previous_mode == "POSE":
        bpy.ops.object.mode_set(mode="OBJECT")
    try:
        for obj in selected:
            obj.select_set(False)
        root.select_set(True)
        layer.objects.active = root
        bpy.ops.object.mode_set(mode="EDIT")
        yield root.data.edit_bones
    finally:
        if root.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        root.select_set(False)
        for obj in selected:
            if layer.objects.get(obj.name) == obj:
                obj.select_set(True)
        if active is not None and layer.objects.get(active.name) == active:
            layer.objects.active = active
            if previous_mode == "POSE":
                bpy.ops.object.mode_set(mode="POSE")


def _shape(transaction, kind):
    if kind == "root":
        points = [(-.2, 0, 0), (.2, 0, 0), (0, -.2, 0), (0, .2, 0),
                  (-.14, -.14, 0), (.14, -.14, 0), (.14, .14, 0), (-.14, .14, 0)]
        edges = [(0, 1), (2, 3), (4, 5), (5, 6), (6, 7), (7, 4)]
    elif kind == "slider":
        points = [(-.06, 0, 0), (0, 0, .06), (.06, 0, 0), (0, 0, -.06)]
        edges = [(0, 1), (1, 2), (2, 3), (3, 0)]
    elif kind == "rail":
        points = [(-.07, 0, -.07), (.42, 0, -.07), (.42, 0, .07),
                  (-.07, 0, .07), (0, 0, 0), (.35, 0, 0)]
        edges = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5)]
    else:
        points = [(math.cos(i * math.tau / 32) * .12, 0,
                   math.sin(i * math.tau / 32) * .12) for i in range(32)]
        edges = [(i, (i + 1) % 32) for i in range(32)]
    mesh = transaction.track(bpy.data.meshes.new("WGT · OXAL资产 · " + kind))
    mesh.from_pydata(points, edges, [])
    obj = transaction.track(bpy.data.objects.new(mesh.name, mesh))
    obj.hide_render = True
    return obj


def _style(pose, shape):
    pose.custom_shape = shape
    pose.use_custom_shape_bone_size = False
    pose.custom_shape_wire_width = 2.0
    pose.rotation_mode = "XYZ"
    for palette in (pose.color, pose.bone.color):
        palette.palette = "CUSTOM"
        palette.custom.normal = (.15, .72, .64)
        palette.custom.select = (.5, 1, .88)
        palette.custom.active = (1, .73, .25)


def _copy_variables(source, target, remap):
    for variable in source.variables:
        new = target.variables.new()
        new.name, new.type = variable.name, variable.type
        for old, new_target in zip(variable.targets, new.targets, strict=True):
            for key in ("id_type", "id", "data_path", "bone_target", "transform_type",
                        "transform_space", "rotation_mode", "use_fallback_value", "fallback_value"):
                prop = new_target.bl_rna.properties.get(key)
                if prop is not None and not prop.is_readonly and hasattr(old, key):
                    setattr(new_target, key, getattr(old, key))
            if old.id in remap:
                rig, bone = remap[old.id]
                new_target.id = rig
                if variable.type == "TRANSFORMS":
                    new_target.bone_target = bone
                elif variable.type == "SINGLE_PROP":
                    new_target.data_path = rig.pose.bones[bone].path_from_id() + old.data_path


def _bind(obj, root, bone_name, *, source_bone=None):
    basis, inverse = obj.matrix_basis.copy(), obj.matrix_parent_inverse.copy()
    bone = root.data.bones[bone_name]
    tail = Matrix.Translation((0, bone.length, 0))
    obj.parent = root
    obj.parent_type = "BONE"
    obj.parent_bone = bone_name
    source_rest = root.data.bones[source_bone].matrix_local if source_bone else Matrix.Identity(4)
    obj.matrix_parent_inverse = tail.inverted() @ bone.matrix_local.inverted() @ source_rest @ inverse
    obj.matrix_basis = basis


def _control_driver(root, path, expression, target_path):
    from .asset_geometry import initialize_driver
    curve = root.driver_add(path)
    initialize_driver(curve)
    curve.driver.type = "SCRIPTED"
    curve.driver.expression = expression
    variable = curve.driver.variables.new()
    variable.name = "v"
    variable.type = "SINGLE_PROP"
    variable.targets[0].id = root
    variable.targets[0].data_path = target_path


def _default_mapping(curve):
    if not curve.keyframe_points:
        return True
    # Blender 5 initializes every new driver with this exact identity map.
    # The released Beta can contain it; other point maps remain conflicts.
    if len(curve.keyframe_points) != 2 or curve.extrapolation != "LINEAR":
        return False
    for index,point in enumerate(curve.keyframe_points):
        if (point.interpolation != "BEZIER" or point.handle_left_type != "AUTO_CLAMPED"
                or point.handle_right_type != "AUTO_CLAMPED" or point.easing != "AUTO"
                or point.type != "KEYFRAME"):
            return False
        expected = ((index,index),(index-1/3,index-1/3),(index+1/3,index+1/3))
        if any(abs(float(value)-wanted)>1e-6 for coordinates,values in zip((point.co,point.handle_left,point.handle_right),expected,strict=True) for value,wanted in zip(coordinates,values,strict=True)):
            return False
    return True


def driver_records(root):
    """Capture native driver state locally for transactional rig replacement."""
    records = []
    if root.animation_data is None:
        return records
    for curve in root.animation_data.drivers:
        if curve.driver.use_self or not _default_mapping(curve) or curve.sampled_points:
            raise ValueError("资产驱动含用户映射或self上下文，未覆盖")
        for modifier in curve.modifiers:
            identity = (modifier.type == "GENERATOR" and modifier.mode == "POLYNOMIAL"
                        and modifier.poly_order == 1 and tuple(modifier.coefficients) == (0.,1.)
                        and not modifier.use_additive and not modifier.mute and not modifier.use_restricted_range
                        and (not modifier.use_influence or abs(modifier.influence-1)<1e-7)
                        and modifier.blend_in == 0 and modifier.blend_out == 0)
            if not identity:
                raise ValueError("资产生成驱动含用户修改器，保留现状，未覆盖")
        variables = []
        for var in curve.driver.variables:
            targets = []
            for target in var.targets:
                values = {}
                for key in ("id_type", "id", "data_path", "bone_target", "transform_type", "transform_space", "rotation_mode", "use_fallback_value", "fallback_value"):
                    prop = target.bl_rna.properties.get(key)
                    if prop is not None and not prop.is_readonly and hasattr(target, key):
                        values[key] = getattr(target, key)
                targets.append(values)
            variables.append((var.name, var.type, targets))
        records.append((curve.data_path, curve.array_index, curve.driver.type, curve.driver.expression, variables))
    return records


def install_drivers(root, records, *, remap=None):
    from .asset_geometry import initialize_driver
    if root.animation_data:
        for curve in tuple(root.animation_data.drivers):
            root.driver_remove(curve.data_path, curve.array_index)
    for path, axis, kind, expression, variables in records:
        value = root.path_resolve(path)
        indexed = hasattr(value,"__len__") and not isinstance(value,str)
        curve = root.driver_add(path,axis) if indexed else root.driver_add(path)
        initialize_driver(curve)
        curve.driver.type, curve.driver.expression = kind, expression
        for name, variable_type, targets in variables:
            var = curve.driver.variables.new()
            var.name, var.type = name, variable_type
            for target, values in zip(var.targets, targets, strict=True):
                for key, value in values.items():
                    if key == "id_type" and variable_type in {"LOC_DIFF", "ROTATION_DIFF", "TRANSFORMS"}:
                        if target.id_type != value:
                            raise ValueError("原生变换驱动目标类型与记录不一致")
                        continue
                    if key == "id" and remap is not None and value == remap:
                        value = root
                    setattr(target, key, value)


def driver_contract(root):
    records = driver_records(root)
    for _, _, _, _, variables in records:
        for _, _, targets in variables:
            for target in targets:
                value = target.get("id")
                if value is not None:
                    target["id"] = (
                        "SELF" if value == root else "SCENE"
                        if isinstance(value, bpy.types.Scene) and value in root.users_scene
                        else "EXTERNAL:" + type(value).__name__
                    )
    return json.dumps(records, ensure_ascii=False, sort_keys=True)


def build(transaction, scene, collection, root, identifier):
    """Compile freshly-created part objects to an original-form native rig."""
    ensure_mutation_allowed()
    if root.get("oxal_mc_studio_native_prefab"):
        return
    if root.type != "ARMATURE":
        return
    children = tuple(root.children_recursive)
    parts = [obj for obj in children if obj.type == "EMPTY" and not obj.get("oxal_mc_studio_native_clock")]
    positions, names, parents = {}, {}, {}
    for part in parts:
        local = Vector(part.location)
        positions[part] = local
        parents[part] = part.parent
    def head(part):
        return positions[part] + (head(parents[part]) if parents[part] in positions else Vector())
    # Pure translation rest matrices make location/rotation/scale channels map
    # exactly to the source hierarchy, including nontrivial authored rotations.
    def relative_matrix(obj):
        parent = obj.parent
        return (relative_matrix(parent) if parent is not None and parent != root else Matrix.Identity(4)) @ obj.matrix_parent_inverse @ obj.matrix_basis
    source_objects = [obj for obj in children if obj.type == "MESH" and obj.parent == root and not obj.get("oxal_mc_studio_effect_geometry")]
    if not source_objects:
        source_objects = [obj for obj in children if obj.type == "MESH" and not obj.get("oxal_mc_studio_effect_geometry")]
    all_points = [relative_matrix(obj) @ vertex.co for obj in source_objects
                  if obj.type == "MESH" and not obj.get("oxal_mc_studio_effect_geometry")
                  for vertex in obj.data.vertices]
    center = Vector((0, 0, 0))
    if "oxal_mc_studio_master_pivot" in root:
        center = Vector(root["oxal_mc_studio_master_pivot"])
    elif all_points and identifier.split(":")[-1] not in ITEMS:
        center = Vector(tuple((min(p[i] for p in all_points) + max(p[i] for p in all_points)) / 2
                              for i in range(3)))
    elif "oxal_mc_studio_grip" in root:
        center = Vector(root["oxal_mc_studio_grip"])
    from .asset_states import aliases as state_aliases
    aliases = [(key, spec) for key, spec in ALIASES.items() if key in root] + state_aliases(root)
    slider_names = {}
    with _edit(root, scene) as bones:
        main = bones.new("总控制")
        main.head, main.tail, main.use_deform = center, center + Vector((0, 0, .3)), False
        base = bones.new("根控制")
        base.head, base.tail, base.parent = center, center + Vector((0, .3, 0)), main
        for part in parts:
            source = part.get("oxal_mc_studio_model_part", part.name.rsplit(".", 1)[0])
            source = SOURCE_NAMES.get(source, source)
            bone = bones.new(source)
            bone.head, bone.tail = head(part), head(part) + Vector((0, .15, 0))
            names[part] = str(bone.name)
        for part in parts:
            bones[names[part]].parent = bones[names[parents[part]]] if parents[part] in names else base
        for part in parts:
            name = str(names[part])
            if not _manual_part(identifier, name):
                continue
            manual = bones.new("手动 · " + LABELS.get(name, name))
            manual.head, manual.tail, manual.parent = head(part), head(part) + Vector((0, 0, .2)), bones[name]
            for child in parts:
                if parents[child] == part:
                    bones[names[child]].parent = manual
        if identifier.split(":")[-1] == "armor_stand":
            for name in ("head", "left_arm", "right_arm", "left_body_stick", "right_body_stick", "shoulder_stick"):
                if name in bones:
                    bones[name].parent = bones["手动 · body"]
        if "活塞头伸缩" in bones:
            source = bones["活塞头伸缩"]
            binding = bones.new("绑定 · 活塞头")
            binding.head, binding.tail, binding.parent = source.head, source.tail, source
            binding.use_deform = False
        bounds = max((p.x for p in all_points), default=.5), max((p.z for p in all_points), default=.5)
        for index, (key, (_, _, _, label)) in enumerate(aliases):
            position = Vector((bounds[0] + .3, center.y, bounds[1] - index * .22))
            weapon = identifier.split(":")[-1]
            if weapon in {"bow","crossbow"} and all_points:
                left,right = min(p.x for p in all_points),max(p.x for p in all_points)
                position = Vector((left+(.67 if weapon == "bow" else .85)*(right-left)-(.175 if weapon == "bow" else .42),center.y,bounds[1]+.02+index*.22))
            rail = bones.new("标尺 · " + label)
            rail.head, rail.tail = position, position + Vector((0, .1, 0))
            rail.parent, rail.use_deform = main, False
            slider = bones.new("绳索显示" if key == "rope_visible" else "控制 · " + label)
            slider.head, slider.tail = rail.head, rail.tail
            slider.parent, slider.use_deform = rail, False
            if weapon in {"bow","crossbow"}:
                rail.tail = rail.head+Vector((0,-.1,0))
                slider.tail = rail.tail
                rail.roll = slider.roll = math.pi
            slider_names[key] = slider.name, rail.name
        slots = int(root.get("oxal_mc_studio_display_slots", 0))
        from .asset_display import slot_layout
        display_layout = slot_layout(root,identifier,all_points,source_objects,relative_matrix) if slots else []
        for index in range(slots):
            name = "展示 · " + (("左格", "中格", "右格")[index] if slots == 3 else "展示物品")
            bone = bones.new(name)
            point,normal = display_layout[index]
            bone.head, bone.tail, bone.parent = point, point + normal*.18, base
            bone.align_roll(Vector((0,0,1)))
            bone.use_deform = False
    exposed = root.data.collections.new("控制器")
    internal = root.data.collections.new("内部机构（隐藏）")
    internal.is_visible = False
    shapes = {kind: _shape(transaction, kind) for kind in ("root", "joint", "slider", "rail")}
    for bone in tuple(root.data.bones):
        visible = bone.name in {"总控制", "绳索显示"} or bone.name.startswith(("手动 · ", "控制 · ", "标尺 · ", "绑定 · ", "展示 · "))
        if "lock" in bone.name or "锁扣" in bone.name or bone.name in {"手动 · banner_pole", "手动 · banner_bar"}:
            visible = False
        if identifier.split(":")[-1] == "enchanting_table" and bone.name.startswith("手动 · ") and bone.name not in {"手动 · 翻页一", "手动 · 翻页二"}:
            visible = False
        (exposed if visible else internal).assign(bone)
        pose = root.pose.bones[bone.name]
        pose.rotation_mode = "XYZ"
        if visible:
            kind = "root" if bone.name == "总控制" else "slider" if bone.name == "绳索显示" or bone.name.startswith("控制 · ") else "rail" if bone.name.startswith("标尺 · ") else "joint"
            _style(pose, shapes[kind])
            if bone.name.startswith("标尺 · "):
                bone.hide_select = True
            if bone.name in {"绳索显示", "标尺 · 绳索显示"}:
                pose.custom_shape_rotation_euler.x = math.pi / 2
    remap = {part: (root, names[part]) for part in parts}
    for part in parts:
        pose = root.pose.bones[names[part]]
        pose.rotation_euler, pose.scale = part.rotation_euler, part.scale
        for key in part.keys():  # noqa: SIM118 -- Blender ID iteration returns attributes, not property keys.
            if not key.startswith("oxal_mc_studio_"):
                pose[key] = part[key]
        if part.animation_data:
            for curve in part.animation_data.drivers:
                path, axis = curve.data_path, curve.array_index
                if path not in {"location", "rotation_euler", "scale"} and not path.startswith("["):
                    raise ValueError("资产部件含无法原生编译的驱动：" + path)
                destination = pose.path_from_id(path)
                new = root.driver_add(destination, axis) if not path.startswith("[") else root.driver_add(destination)
                from .asset_geometry import initialize_driver
                initialize_driver(new)
                new.driver.type = curve.driver.type
                expression = curve.driver.expression
                if path == "location":
                    expression = f"({expression})-({positions[part][axis]})"
                new.driver.expression = expression
                _copy_variables(curve.driver, new.driver, remap)
    for obj in children:
        if obj in names or obj.get("oxal_mc_studio_native_clock"):
            continue
        parent = obj.parent
        if parent in names:
            part_name = str(names[parent])
            manual = "手动 · " + LABELS.get(part_name, part_name)
            _bind(obj, root, manual if manual in root.pose.bones else part_name, source_bone=part_name)
        elif parent == root:
            _bind(obj, root, "根控制")
    # References between newly created components are remapped before reclaiming
    # temporary IDs. Shared/user IDs are never scanned or rewritten.
    for owner in tuple(transaction.ids):
        for candidate in (owner, getattr(owner, "node_tree", None)):
            animation = getattr(candidate, "animation_data", None)
            if animation is None or owner in names:
                continue
            for curve in animation.drivers:
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if target.id in names:
                            part = target.id
                            target.id = root
                            if variable.type == "TRANSFORMS":
                                target.bone_target = names[part]
                            else:
                                target.data_path = root.pose.bones[names[part]].path_from_id() + target.data_path
    for part in parts:
        transaction.forget(part)
        bpy.data.objects.remove(part, do_unlink=True)
    specs = []
    for key, (legacy, minimum, maximum, label) in aliases:
        slider_name, _ = slider_names[key]
        pose = root.pose.bones[slider_name]
        value = float(root[key])
        root[legacy] = value
        root.id_properties_ui(legacy).update(min=minimum, max=maximum)
        pose.location.x = .35 * max(0, min(1, (value - minimum) / (maximum - minimum)))
        pose.lock_location = (False, True, True)
        pose.lock_rotation = pose.lock_scale = (True, True, True)
        constraint = pose.constraints.new("LIMIT_LOCATION")
        constraint.owner_space, constraint.use_transform_limit = "LOCAL", True
        for axis in "xyz":
            setattr(constraint, "use_min_" + axis, True)
            setattr(constraint, "use_max_" + axis, True)
            setattr(constraint, "min_" + axis, 0)
            setattr(constraint, "max_" + axis, .35 if axis == "x" else 0)
        expression = "v>.175" if isinstance(root[key], bool) else f"{minimum}+min(1,max(0,v/.35))*{maximum-minimum}"
        _control_driver(root, '["' + legacy + '"]', expression, pose.path_from_id("location") + "[0]")
        _control_driver(root, '["' + key + '"]', "v", '["' + legacy + '"]')
        specs.append({"name": label, "bone": slider_name, "keys": [legacy], "min": minimum, "max": maximum})
    root["骨骼控件"] = json.dumps(specs, ensure_ascii=False)
    root["资产部件"] = json.dumps(list(names.values()), ensure_ascii=False)
    root["资产ID"] = identifier.split(":")[-1]
    if identifier.split(":")[-1] in {"item_frame","glow_item_frame"}:
        root["展示物品旋转"] = 0
        root.id_properties_ui("展示物品旋转").update(min=0,max=7)
        from .asset_geometry import driver
        driver(root.pose.bones["展示 · 展示物品"],"rotation_euler","v*.7853981633974483",root,"展示物品旋转",1)
    root["oxal_mc_studio_native_rig"] = 1
    if identifier.split(":")[-1] in ITEMS:
        root.pose.bones["总控制"].custom_shape_scale_xyz = (.5, .5, .5)
        root.pose.bones["总控制"].custom_shape_rotation_euler.z = math.pi / 4
    root.data.display_type, root.data.show_names, root.show_in_front = "WIRE", False, True
    root["oxal_mc_studio_rig_driver_contract"] = driver_contract(root)
