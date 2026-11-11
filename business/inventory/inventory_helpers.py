"""Presentation of precisely owned native binding helpers, at explicit commit."""

from ...util.ownership import require_owned

HELPER = "oxal_mc_studio_slot_internal_helper"


def configure(transaction, scene, root):
    """Hide internal Empty/bounds overlays without hiding represented meshes."""
    if root.get("oxal_mc_studio_presentation_context") != "slot":
        return
    for obj in (root, *tuple(root.children_recursive)):
        if obj.type != "EMPTY" and not obj.get("oxal_inventory_depth_bounds"):
            continue
        if obj not in transaction.ids:
            require_owned(root, (obj,))
        layers = [(layer, obj.hide_get(view_layer=layer), obj.select_get(view_layer=layer))
                  for layer in scene.view_layers if layer.objects.get(obj.name) == obj]
        before = (obj.hide_select, obj.empty_display_size if obj.type == "EMPTY" else None,
                  HELPER in obj, obj.get(HELPER))

        def restore(obj=obj, before=before, layers=layers):
            obj.hide_select = before[0]
            if before[1] is not None:
                obj.empty_display_size = before[1]
            for layer, hidden, selected in layers:
                obj.hide_set(hidden, view_layer=layer)
                obj.select_set(selected, view_layer=layer)
            if before[2]:
                obj[HELPER] = before[3]
            elif HELPER in obj:
                del obj[HELPER]

        transaction.on_rollback(restore)
        obj[HELPER] = True
        obj.hide_select = True
        if obj.type == "EMPTY":
            obj.empty_display_size = .0001
        for layer, _hidden, _selected in layers:
            obj.hide_set(True, view_layer=layer)


def visible_selection(root):
    """Select the rendered editable asset, not an internal Empty at the origin."""
    held = root.get("oxal_inventory_held")
    gui = root.get("oxal_inventory_gui")
    for pivot in (held, gui):
        if pivot is None:
            continue
        candidates = tuple(pivot.children_recursive)
        visible = [obj for obj in candidates if obj.type == "MESH" and not obj.get("oxal_mc_studio_effect_geometry")
                   and not obj.hide_get() and not obj.hide_viewport and not obj.hide_render
                   and int(obj.get("oxal_mc_studio_slot_phase", 0)) == 0]
        if visible:
            return visible[0]
    return None
