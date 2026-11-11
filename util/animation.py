"""Read Action curves through the animation capabilities available in Blender."""

from bpy.types import Action, ActionSlot, FCurve


def action_fcurves(
    action: Action | None, *, slot: ActionSlot | None = None
) -> tuple[FCurve, ...]:
    """Return each existing curve once, optionally limited to its bound slot.

    Layered Actions expose their curves in per-slot channelbags. Older legacy
    Actions expose ``fcurves`` directly. Inspect the actual layout rather than
    creating compatibility data or selecting a path from a version number.
    """
    if action is None:
        return ()
    if slot is not None and slot not in tuple(getattr(action, "slots", ())):
        raise ValueError("The animation slot does not belong to this Action")
    curves: dict[int, FCurve] = {}
    layers = getattr(action, "layers", ())
    if layers:
        for layer in layers:
            for strip in layer.strips:
                for bag in getattr(strip, "channelbags", ()):
                    if slot is not None and bag.slot_handle != slot.handle:
                        continue
                    for curve in bag.fcurves:
                        curves[curve.as_pointer()] = curve
    else:
        for curve in getattr(action, "fcurves", ()):
            curves[curve.as_pointer()] = curve
    return tuple(curves.values())
