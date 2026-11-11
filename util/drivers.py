"""Exact local driver journals for explicit transactions, never queued payloads.

Unchanged curves retain their native identity. Only altered/removed curves are
restored, including mapping points, modifiers, mute, self context and targets.
No Action or NLA binding is replaced by this journal.
"""

from ..core.runtime import ensure_mutation_allowed


def _properties(owner, *, exclude=()):
    values = {}
    for prop in owner.bl_rna.properties:
        name = prop.identifier
        if prop.is_readonly or name in {"rna_type", *exclude} or prop.type == "COLLECTION":
            continue
        value = getattr(owner, name)
        if getattr(prop, "is_array", False):
            value = tuple(value)
        elif prop.type == "ENUM" and isinstance(value, set):
            value = set(value)
        if prop.type in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING", "POINTER"}:
            values[name] = value
    return values


def _write(owner, values):
    # These fields change the shape/type of subsequent writable RNA fields.
    first = ("type", "mode", "poly_order", "id_type", "id", "function_type")
    for name in (*[key for key in first if key in values], *[key for key in values if key not in first]):
        current = getattr(owner, name)
        prop = owner.bl_rna.properties[name]
        current = tuple(current) if getattr(prop, "is_array", False) else current
        if current != values[name]:
            setattr(owner, name, values[name])


def _record(curve):
    modifiers = []
    for modifier in curve.modifiers:
        record = {"type": modifier.type, "properties": _properties(modifier, exclude=("type",))}
        if modifier.type == "ENVELOPE":
            record["points"] = [_properties(point) for point in modifier.control_points]
        modifiers.append(record)
    variables = []
    for variable in curve.driver.variables:
        targets = []
        for target in variable.targets:
            values = _properties(target)
            # Transform targets have an implied Object type, not a writable
            # generic ID category; assigning it is rejected by native RNA.
            if variable.type in {"LOC_DIFF", "ROTATION_DIFF", "TRANSFORMS"}:
                values.pop("id_type", None)
            targets.append(values)
        variables.append({"properties": _properties(variable), "targets": targets})
    return {
        "path": curve.data_path, "index": curve.array_index,
        "properties": _properties(curve, exclude=("data_path", "array_index")),
        "driver": _properties(curve.driver), "variables": variables,
        "keys": [_properties(point) for point in curve.keyframe_points],
        "samples": [tuple(point.co) for point in curve.sampled_points],
        "modifiers": modifiers,
    }


def capture(owner):
    """Capture temporary full state; actual ID references stay in this Tool."""
    animation = getattr(owner, "animation_data", None)
    return {"animation": animation is not None,
            "curves": sorted((_record(curve) for curve in animation.drivers), key=lambda record: (record["path"], record["index"])) if animation else []}


def _mapping(curve, record):
    samples = record["samples"]
    if curve.sampled_points:
        if [tuple(point.co) for point in curve.sampled_points] == samples:
            return
        curve.convert_to_keyframes(start=0, end=max(1, len(curve.sampled_points)))
    curve.keyframe_points.clear()
    if samples:
        # Native conversion allocates the otherwise read-only sample array.
        # A constant temporary map avoids evaluating any user driver script.
        for x in (0, max(1, len(samples))):
            curve.keyframe_points.insert(x, 0)
        curve.convert_to_samples(start=0, end=len(samples))
        if len(curve.sampled_points) != len(samples):
            raise ValueError("原生采样曲线长度无法还原，未静默忽略")
        for point, co in zip(curve.sampled_points, samples, strict=True):
            point.co = co
    else:
        curve.keyframe_points.add(len(record["keys"]))
        for point, values in zip(curve.keyframe_points, record["keys"], strict=True):
            _write(point, values)
        curve.update()


def _restore_curve(owner, curve, record):
    if curve is None:
        value = owner.path_resolve(record["path"])
        indexed = hasattr(value, "__len__") and not isinstance(value, str)
        curve = owner.driver_add(record["path"], record["index"]) if indexed else owner.driver_add(record["path"])
    # Recreate only this altered curve's native state, leaving all unrelated
    # FCurves (and their modifiers/point identities) entirely untouched.
    curve.mute = True
    for modifier in tuple(curve.modifiers):
        curve.modifiers.remove(modifier)
    for variable in tuple(curve.driver.variables):
        curve.driver.variables.remove(variable)
    curve.driver.type, curve.driver.expression, curve.driver.use_self = "SCRIPTED", "0", False
    _mapping(curve, record)
    _write(curve.driver, record["driver"])
    for saved in record["variables"]:
        variable = curve.driver.variables.new()
        _write(variable, saved["properties"])
        for target, values in zip(variable.targets, saved["targets"], strict=True):
            _write(target, values)
    for saved in record["modifiers"]:
        modifier = curve.modifiers.new(saved["type"])
        _write(modifier, {key: value for key, value in saved["properties"].items() if key != "active"})
        for values in saved.get("points", ()):
            point = modifier.control_points.add(values["frame"])
            _write(point, values)
    curve.modifiers.active = next((modifier for modifier, saved in zip(curve.modifiers, record["modifiers"], strict=True)
                                   if saved["properties"]["active"]), None)
    _write(curve, record["properties"])


def restore(owner, snapshot):
    """Restore a capture inside the same explicit transaction and safe phase."""
    ensure_mutation_allowed()
    animation = getattr(owner, "animation_data", None)
    current = {(curve.data_path, curve.array_index): curve for curve in animation.drivers} if animation else {}
    saved = {(record["path"], record["index"]): record for record in snapshot["curves"]}
    for key, curve in tuple(current.items()):
        if key not in saved:
            owner.driver_remove(curve.data_path, curve.array_index)
    for key, record in saved.items():
        curve = current.get(key)
        if curve is None or _record(curve) != record:
            _restore_curve(owner, curve, record)
    animation = getattr(owner, "animation_data", None)
    if not snapshot["animation"] and animation is not None and not animation.action and not animation.nla_tracks and not animation.drivers:
        owner.animation_data_clear()
