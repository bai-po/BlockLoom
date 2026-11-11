"""Offline standalone-resource cleanup and deterministic native driver contracts."""

import ast
import json
import math
import re

CONTROL_NAMES = {
    "启用air_empty贴图":"显示空氧气图标",
    "oxal_inventory_hands_enabled":"第一人称手部显示",
}
for _slot in (*range(1,10),"offhand"):
    _label = "副手" if _slot=="offhand" else "格"+str(_slot)
    for _field,_title in (("enabled","启用耐久条"),("percent","剩余耐久百分比"),("show_full","满耐久也显示")):
        CONTROL_NAMES[f"oxal_durability_{_slot}_{_field}"] = _label+" · "+_title


def control_key(owner,legacy):
    """New resources use real Chinese properties; existing Beta stays readable."""
    translated = CONTROL_NAMES.get(legacy,legacy)
    return translated if translated in owner or legacy not in owner else legacy


def control_path(owner,legacy):
    return '['+json.dumps(control_key(owner,legacy),ensure_ascii=False)+']'


def driver_owners(items):
    return tuple({owner for item in items for owner in (item, getattr(item, "node_tree", None)) if owner is not None})


def owner_roles(items):
    def role(item):
        saved = item.get("oxal_inventory_native_role")
        reference = item.library_weak_reference
        name = saved or (reference.id_name[2:] if reference else re.sub(r"\.\d{3}$", "", item.name))
        return item.id_type + ":" + str(name)
    result = {item: role(item) for item in items}
    for item in items:
        tree = getattr(item, "node_tree", None)
        if tree is not None:
            result[tree] = "EMBEDDED:" + result[item]
        user = getattr(item, "user", None)
        if user is not None:
            result[item] = "KEY:" + role(user)
    return result


def _write(owner, curve, value):
    path = curve.data_path
    current = owner.path_resolve(path)
    if hasattr(current, "__len__") and not isinstance(current, str):
        current[curve.array_index] = value
    elif path.startswith('["'):
        owner[json.loads(path[1:-1])] = value
    else:
        prefix, name = path.rsplit(".", 1) if "." in path else ("", path)
        setattr(owner.path_resolve(prefix) if prefix else owner, name, value)


def clean_derived(items, *, allowed=None):
    """Called by the isolated resource builder, never a draw/registration callback."""
    counts = {"unused_variables": 0, "constant_drivers": 0, "unused_channels": 0}
    owners = driver_owners(items)
    functions = {name: getattr(math, name) for name in ("floor", "ceil", "sin", "cos", "sqrt")}
    functions.update(abs=abs, min=min, max=max, round=round, pow=pow)
    for owner in owners:
        animation = getattr(owner, "animation_data", None)
        if animation is None:
            continue
        for curve in tuple(animation.drivers):
            if allowed is not None and (owner, curve.data_path, curve.array_index) not in allowed:
                continue
            driver = curve.driver
            if driver.type != "SCRIPTED":
                continue
            parsed = ast.parse(driver.expression or "0", mode="eval")
            names = {node.id for node in ast.walk(parsed) if isinstance(node, ast.Name)}
            for variable in tuple(driver.variables):
                if variable.name not in names:
                    driver.variables.remove(variable)
                    counts["unused_variables"] += 1
            if not driver.variables and names <= functions.keys():
                value = eval(compile(parsed, "<standalone-resource>", "eval"), {"__builtins__": {}}, functions)
                if not isinstance(value, (int, float, bool)) or not math.isfinite(value):
                    raise ValueError("独立物品栏常量驱动不能安全折叠")
                _write(owner, curve, value)
                animation.drivers.remove(curve)
                counts["constant_drivers"] += 1
    # Remove migrated helper channels only when no actual RNA consumer remains.
    while True:
        referenced = {(target.id, target.data_path) for owner in owners if getattr(owner, "animation_data", None) for curve in owner.animation_data.drivers for variable in curve.driver.variables for target in variable.targets}
        removed = 0
        for owner in owners:
            animation = getattr(owner, "animation_data", None)
            if animation is None:
                continue
            for curve in tuple(animation.drivers):
                if curve.data_path.startswith('["独立_') and (owner, curve.data_path) not in referenced:
                    if allowed is not None and (owner, curve.data_path, curve.array_index) not in allowed:
                        continue
                    name = json.loads(curve.data_path[1:-1])
                    animation.drivers.remove(curve)
                    del owner[name]
                    removed += 1
        counts["unused_channels"] += removed
        if not removed:
            break
    return counts


def driver_baseline(items):
    result = []
    roles = owner_roles(items)
    for owner in driver_owners(items):
        animation = getattr(owner, "animation_data", None)
        if animation is None:
            continue
        for curve in animation.drivers:
            result.append({"owner": owner.name, "role": roles[owner], "path": curve.data_path, "index": curve.array_index, "type": curve.driver.type, "expression": curve.driver.expression, "variables": [{"name": v.name, "type": v.type, "targets": [{"id": t.id.name if t.id else None, "path": t.data_path, "bone": t.bone_target, "transform": t.transform_type, "space": t.transform_space} for t in v.targets]} for v in curve.driver.variables]})
    return sorted(result, key=lambda value: (value["owner"], value["path"], value["index"]))
