"""Shared native enchantment controls; drawing only reads persistent data."""


def draw_external(layout, context):
    from ..business.enchantment import enchantment_external as external
    from ..business.enchantment import enchantment_external_outline as outline
    from ..business.enchantment import enchantment_external_relief as relief
    from ..business.enchantment.enchantment_pixels import display_prepared

    row = layout.row(align=True)
    for text, action, icon in (("添加像素流光", "ENCHANT_GENERIC_PIXELS", "SHADING_RENDERED"),):
        row.operator("oxal_mc_studio.asset_action", text=text, icon=icon).action = action
    layout.operator("oxal_mc_studio.asset_action", text="移除描边", icon="X").action = "ENCHANT_GENERIC_REMOVE_OUTLINE"
    row = layout.row(align=True)
    row.operator("oxal_mc_studio.asset_action", text="添加3D凹凸", icon="MOD_DISPLACE").action = "ENCHANT_GENERIC_3D"
    row.operator("oxal_mc_studio.asset_action", text="移除3D凹凸", icon="X").action = "ENCHANT_GENERIC_REMOVE_3D"
    layout.label(text="支持普通模型、曲线和文字；选父物体可批量应用", icon="INFO")
    candidates = external.targets((context.object,)) if context.object is not None else ()
    for obj in candidates[:8]:
        owner = external.controller(obj)
        if owner is None:
            continue
        layout.label(text=obj.name, icon="OBJECT_DATA")
        draw_controls(layout, owner, geometry_allowed=True, prepared=relief.prepared(owner), outline_prepared=outline.prepared(owner),
                      pixel_materials=tuple(material for material in owner.get(external.SURFACES, {}).values()
                                            if material is not None and material.node_tree is not None
                                            and material.get("oxal_mc_studio_glint_controls") == owner
                                            and display_prepared(material)))
    if len(candidates) > 8:
        layout.label(text="更多对象可分别选中后调节", icon="INFO")


def _prop(layout, owner, key, label):
    if key in owner:
        layout.prop(owner, '["' + key + '"]', text=label)


def _armor_owner(root, part):
    """Check the exact local registry before exposing editable native fields."""
    from ..business.human.human_armor import GLINT_CONTROLS
    from ..business.human.human_state import OWNER
    from ..util.ownership import DATA_UID, REGISTRY
    from ..util.resources import INSTANCE

    owner = root.get(GLINT_CONTROLS, {}).get(part)
    if (getattr(owner, "type", None) != "EMPTY" or owner.get(OWNER) != root.get(INSTANCE)
            or root.objects.get(owner.name) != owner
            or root.get(REGISTRY, {}).get(owner.get(DATA_UID, "")) != owner):
        return None
    return owner


def draw_armor_controls(layout, root, parts, *, ui_state=None):
    """Present part metadata; execution alone validates shader contracts.

    Hashing the private shader through ``glint_owner`` belongs to an explicit
    Tool operation, never the redraw path. These direct persisted references
    only choose controls to display and do not grant permission to mutate IDs.
    """
    from ..business.enchantment import enchantment
    from ..business.enchantment.enchantment_outline import outline_prepared
    from ..business.enchantment.enchantment_pixels import (
        display_prepared as pixels_prepared,
    )
    from ..business.human.human import parameter_owner
    from ..business.human.human_armor import objects
    from ..business.human.human_state import key

    titles = {"head": "头盔附魔", "chest": "胸甲附魔", "legs": "护腿附魔", "feet": "靴子附魔"}
    parts = tuple(parts)
    multiple = len(parts) > 1
    if multiple:
        layout.label(text="按所选部位添加并启用，单部位可调节", icon="INFO")
    for part in parts:
        members = objects(root, part)
        owner = _armor_owner(root, part)
        materials = tuple({material for obj in members for slot in obj.material_slots
                           if (material := slot.material) is not None
                           and material.get(enchantment._OWNER) == owner
                           and material.get(enchantment._PROFILE) == "ARMOR"
                           and material.node_tree and material.node_tree.nodes.get(enchantment.PREFIX + "Mix")}) if owner else ()
        if multiple:
            row = layout.row(align=True)
            if ui_state is not None:
                row.prop_enum(ui_state, "armor_target", part.upper(), text=titles[part])
            else:
                row.label(text=titles[part])
            row.label(text="已附魔" if materials else "未附魔" if members else "未添加")
            if materials and owner is not None:
                status = layout.row(align=True)
                pixel_ready = all(pixels_prepared(material) for material in materials)
                outline_ready = bool(members) and all(outline_prepared(obj) for obj in members)
                for title, ready, enabled in (
                        ("像素流光", pixel_ready, owner.get("glint_pixel_enabled")),
                        ("附魔描边", outline_ready, owner.get("glint_outline_enabled"))):
                    state = "已启用" if ready and enabled else "已准备，未启用" if ready else "未准备"
                    status.label(text=title + "：" + state, icon="CHECKMARK" if ready else "DOT")
            continue
        if materials:
            layout.label(text=titles[part])
            draw_controls(layout, owner, geometry_allowed=False,
                          outline_allowed=not parameter_owner(root).get(key(part + "_3d"), False),
                          outline_prepared=all(outline_prepared(obj) for obj in members),
                          pixel_materials=materials)


def draw_controls(layout, owner, *, prepared=False, geometry_allowed=True, outline_allowed=True, outline_prepared=False, stable_sprite=False,
                  pixel_materials=()):
    row = layout.row(align=True)
    _prop(row, owner, "glint_enabled", "附魔")
    _prop(row, owner, "glint_color", "附魔色")
    row = layout.row(align=True)
    _prop(row, owner, "glint_brightness", "亮度")
    _prop(row, owner, "glint_gradient", "渐变")
    if "glint_pixel_enabled" in owner:
        from ..business.enchantment.enchantment_pixels import (
            display_prepared as pixels_prepared,
        )
        pixel = layout.box()
        ready = bool(pixel_materials) and all(pixels_prepared(material) for material in pixel_materials)
        if ready:
            _prop(pixel, owner, "glint_pixel_enabled", "像素格流光")
            if owner.get("glint_pixel_enabled"):
                _prop(pixel, owner, "glint_pixel_size", "像素格大小")
                _prop(pixel, owner, "glint_pixel_speed", "流动速度")
                from ..business.enchantment.enchantment import PIXEL_BRIGHTNESS
                if all(material.get(PIXEL_BRIGHTNESS) == 1 and material.node_tree.nodes.get("OXAL Glint Pixel Brightness Multiplier")
                       for material in pixel_materials):
                    _prop(pixel, owner, "glint_pixel_brightness_multiplier", "流光亮度倍率")
                else:
                    pixel.label(text="重新准备像素格流光可调亮度倍率", icon="INFO")
        else:
            pixel.label(text="准备像素格流光后可启用及插帧" if not geometry_allowed
                        else "重新应用一键附魔以准备像素格流光", icon="INFO")
    if "glint_outline_enabled" not in owner:
        return
    if outline_prepared:
        outline = layout.box()
        outline.enabled = outline_allowed
        _prop(outline, owner, "glint_outline_enabled", "附魔描边")
        _prop(outline, owner, "glint_outline_color", "描边色")
        _prop(outline, owner, "glint_outline_width", "描边粗细")
        _prop(outline, owner, "glint_outline_brightness", "描边亮度")
        if stable_sprite:
            outline.label(text="像素物品自动使用稳定法线", icon="INFO")
        else:
            _prop(outline, owner, "glint_outline_quality_normals", "高质量法线")
    else:
        layout.label(text="准备附魔描边后可调节及插帧", icon="INFO")
    if not geometry_allowed:
        return
    if not prepared:
        layout.label(text="先准备3D，再调节像素凹凸", icon="INFO")
        return
    geometry = layout.box()
    _prop(geometry, owner, "glint_geometry_enabled", "像素3D凹凸")
    _prop(geometry, owner, "glint_geometry_subdivision", "细分等级（重新添加应用）")
    geometry.label(text="沿用原贴图与材质", icon="MATERIAL")
    row = geometry.row(align=True)
    _prop(row, owner, "glint_geometry_outer", "向内厚度")
    _prop(row, owner, "glint_geometry_inner", "向外厚度")
    _prop(geometry, owner, "glint_geometry_random_enabled", "随机高低凸出")
    if owner.get("glint_geometry_random_enabled"):
        row = geometry.row(align=True)
        _prop(row, owner, "glint_geometry_random_factor", "随机系数")
        _prop(row, owner, "glint_geometry_height_ratio", "高低比例")
        _prop(geometry, owner, "glint_geometry_seed", "随机种子")
    row = geometry.row(align=True)
    _prop(row, owner, "glint_geometry_center_falloff", "中心衰减")
    _prop(row, owner, "glint_geometry_center_range", "衰减范围")
