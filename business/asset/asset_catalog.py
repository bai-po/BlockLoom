"""Read-only Minecraft model planning; independent of Blender and legacy code."""

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "asset/library"
VERSION = "26.4-snapshot-5"
# Pinned Java client: https://www.minecraft.net/article/minecraft-26-4-snapshot-5
RESOURCE_PACK_FORMAT = (100, 2)


def resource(identifier: str, folder: str, suffix: str = ".json") -> Path:
    namespace, name = (
        identifier.split(":", 1) if ":" in identifier else ("minecraft", identifier)
    )
    if not namespace or not name or "\\" in identifier:
        raise ValueError("无效资源标识")
    path = ROOT / "assets" / namespace / folder / (name + suffix)
    if not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("资源路径越界")
    return path


@lru_cache(maxsize=8192)
def read(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf8"))
    if not isinstance(data, dict):
        raise TypeError(f"资源必须是对象：{path.name}")
    return data


@dataclass(frozen=True)
class Entry:
    identifier: str
    label: str
    categories: tuple[str, ...]


WALL_ITEMS={"wall_torch":"torch","soul_wall_torch":"soul_torch","copper_wall_torch":"copper_torch","redstone_wall_torch":"redstone_torch"}


def item_identifier(identifier: str) -> str:
    namespace,short=identifier.split(":",1) if ":" in identifier else ("minecraft",identifier)
    return namespace+":"+WALL_ITEMS.get(short,short)


@lru_cache(maxsize=1)
def entries() -> tuple[Entry, ...]:
    categories: dict[str, set[str]] = {}
    for category in read(ROOT / "creative_inventory_categories.json")["categories"]:
        for identifier in category["items"]:
            key = identifier if ":" in identifier else "minecraft:" + identifier
            categories.setdefault(key, set()).add(category["id"])
    language = read(resource("minecraft:zh_cn", "lang"))
    found = []
    for path in sorted((ROOT / "assets/minecraft/items").glob("*.json")):
        if path.stem == "air":
            continue  # Air is an invisible sentinel, not an importable item.
        identifier = "minecraft:" + path.stem
        label = language.get(
            "item.minecraft." + path.stem,
            language.get("block.minecraft." + path.stem, path.stem),
        )
        found.append(
            Entry(identifier, label, tuple(sorted(categories.get(identifier, ()))))
        )
    for short in ("fire", "soul_fire"):
        found.append(Entry("minecraft:"+short,language.get("block.minecraft."+short,short),("functional_blocks",)))
    for short,item in WALL_ITEMS.items():
        found.append(Entry("minecraft:"+short,language.get("block.minecraft."+item,item)+"（墙面）",("functional_blocks",)))
    effect_path = ROOT/"effect_catalog.json"
    if effect_path.is_file():
        for item in read(effect_path).get("entries",[]):
            found.append(Entry(item["identifier"],item["label"],tuple(item.get("categories",("EFFECTS",)))))
    return tuple(found)


def filtered(search: str = "", category: str = "ALL") -> tuple[Entry, ...]:
    needle = search.casefold().strip()
    return tuple(
        entry
        for entry in entries()
        if (needle or category == "ALL" or category in entry.categories)
        and (
            not needle or needle in entry.label.casefold() or needle in entry.identifier
        )
    )


@lru_cache(maxsize=4096)
def model(identifier: str, stack: tuple[str, ...] = ()) -> dict:
    if identifier in stack or len(stack) > 64:
        raise ValueError("模型父级循环")
    if identifier in {
        "builtin/generated",
        "minecraft:builtin/generated",
        "builtin/entity",
        "minecraft:builtin/entity",
    }:
        return {"builtin": identifier.rsplit("/", 1)[-1]}
    data = read(resource(identifier, "models"))
    parent = model(data["parent"], (*stack, identifier)) if "parent" in data else {}
    merged = {**parent, **data}
    merged["textures"] = {**parent.get("textures", {}), **data.get("textures", {})}
    merged["display"] = {**parent.get("display", {}), **data.get("display", {})}
    return merged


def texture(key: str | dict, textures: dict) -> str:
    if isinstance(key, dict):
        key = key.get("sprite", "")
    if not isinstance(key, str) or not key:
        raise ValueError("贴图缺少 sprite 标识")
    if not key.startswith("#") and key in textures:
        key = "#" + key
    visited = set()
    while key.startswith("#"):
        if key in visited or key[1:] not in textures:
            raise ValueError("贴图引用无效或循环：" + key)
        visited.add(key)
        key = textures[key[1:]]
        if isinstance(key, dict):
            key = key.get("sprite", "")
        if not isinstance(key, str) or not key:
            raise ValueError("贴图引用缺少 sprite 标识")
    return key if ":" in key else "minecraft:" + key


def item_nodes(node: dict, *, context="fixed", selection=0, state=None):
    parts = _item_nodes(node, context=context, selection=selection, state=state)
    if node.get("transformation") and node.get("type", "").split(":")[-1] not in {
        "model",
        "special",
    }:
        return [
            {
                **part,
                "_oxal_transform_chain": [
                    node["transformation"],
                    *part.get("_oxal_transform_chain", []),
                ],
            }
            for part in parts
        ]
    return parts


def _item_nodes(
    node: dict, *, context: str = "fixed", selection: int = 0, state: dict | None = None
) -> list[dict]:
    kind = node.get("type", "").removeprefix("minecraft:")
    if kind in {"model", "special"}:
        return [node]
    if kind == "empty":
        return []
    if kind == "composite":
        return [
            part
            for child in node.get("models", [])
            for part in item_nodes(
                child, context=context, selection=selection, state=state
            )
        ]
    if kind == "condition":
        return item_nodes(
            node.get("on_false", {}), context=context, selection=selection, state=state
        )
    if kind == "select":
        cases = node.get("cases", [])
        selected = node.get("fallback")
        if node.get("property") == "minecraft:display_context":
            selected = next(
                (
                    case["model"]
                    for case in cases
                    if context
                    in (
                        case["when"]
                        if isinstance(case["when"], list)
                        else [case["when"]]
                    )
                ),
                selected,
            )
        elif node.get("block_state_property") and state:
            wanted = state.get(node["block_state_property"])
            selected = next(
                (
                    case["model"]
                    for case in cases
                    if wanted
                    in (
                        case["when"]
                        if isinstance(case["when"], list)
                        else [case["when"]]
                    )
                ),
                selected,
            )
        if selected is None and cases:
            selected = cases[selection % len(cases)]["model"]
        return item_nodes(
            selected or {}, context=context, selection=selection, state=state
        )
    if kind == "range_dispatch":
        choices = node.get("entries", [])
        selected = node.get("fallback") or (choices[0]["model"] if choices else {})
        return item_nodes(selected, context=context, selection=selection, state=state)
    raise ValueError("尚未支持的物品模型类型：" + kind)


@lru_cache(maxsize=4096)
def schema(identifier: str) -> dict[str, tuple[str, ...]]:
    path = resource(identifier, "blockstates")
    data = read(path) if path.is_file() else {}
    values: dict[str, set[str]] = {}
    original = read(ROOT / "block_state_properties.json").get("blocks", {}).get(identifier.split(":")[-1], {})

    def add(condition):
        if isinstance(condition, dict):
            for key, value in condition.items():
                if key in {"AND", "OR"}:
                    for child in value:
                        add(child)
                else:
                    values.setdefault(key, set()).update(str(value).split("|"))

    for key in data.get("variants", {}):
        add(dict(pair.split("=", 1) for pair in key.split(",") if "=" in pair))
    for part in data.get("multipart", []):
        add(part.get("when", {}))
    for key, choices in original.get("values", {}).items():
        if key in values:
            values[key].update(str(value) for value in choices)
    # Multipart conditions commonly mention only the enabled branch. The
    # missing branch is still a legal boolean state (notably fire faces).
    for choices in values.values():
        if choices and choices <= {"true", "false"}:
            choices.update(("true", "false"))
    short = identifier.split(":")[-1]
    if short in {"fire","soul_fire"}:
        for direction in ("north","east","south","west","up"):
            values[direction] = {"false","true"}
    if short.endswith("chest") and "chestplate" not in short:
        values["facing"] = {"north","east","south","west"}
        if short != "ender_chest":
            values["type"] = {"single","left","right"}
    if short.endswith("shulker_box"):
        values["facing"] = {"up","down","north","east","south","west"}
    if short.endswith("_banner"):
        values["attachment"] = {"floor","wall"}
        values["facing"] = {"north","east","south","west"}

    def item_properties(node):
        if isinstance(node, dict):
            prop=node.get("block_state_property")
            if isinstance(prop,str) and node.get("cases"):
                choices=values.setdefault(prop,set())
                for case in node["cases"]:
                    selected=case["when"] if isinstance(case["when"],list) else [case["when"]]
                    choices.update(str(value) for value in selected)
                fallback=node.get("fallback",{})
                special=fallback.get("model",{}) if isinstance(fallback,dict) else {}
                if prop=="copper_golem_pose" and isinstance(special,dict) and special.get("pose"):
                    choices.add(special["pose"])
                if prop=="level" and isinstance(special,str):
                    suffix=special.rsplit("_",1)[-1]
                    if suffix.isdigit():
                        choices.add(str(int(suffix)))
            for child in node.values():
                item_properties(child)
        elif isinstance(node,list):
            for child in node:
                item_properties(child)

    item_path=resource(identifier,"items")
    if item_path.is_file():
        item_properties(read(item_path))
    return {key: tuple(sorted(options)) for key, options in values.items()}


def _matches(condition: dict, state: dict) -> bool:
    for key, value in condition.items():
        if key == "OR":
            if not any(_matches(child, state) for child in value):
                return False
        elif key == "AND":
            if not all(_matches(child, state) for child in value):
                return False
        elif str(state.get(key, "")) not in str(value).split("|"):
            return False
    return True


@lru_cache(maxsize=4096)
def shape_schema(identifier: str) -> dict:
    """Immutable choice labels prepared only by an explicit catalog request."""
    path = resource(identifier, "blockstates")
    data = read(path) if path.is_file() else {}
    groups = []
    language = read(resource("minecraft:zh_cn", "lang"))
    def labels(choices):
        result = []
        for choice in choices:
            model_id = choice.get("model", "")
            stem = model_id.split(":")[-1].removeprefix("block/")
            mirrored = stem.endswith("_mirrored")
            base = stem.removesuffix("_mirrored")
            label = language.get("block.minecraft." + base, base.replace("_", " "))
            if mirrored:
                label += " · 镜像"
            rotations = [axis.upper() + "轴 " + str(choice[axis]) + "°" for axis in ("x", "y", "z") if choice.get(axis)]
            if rotations:
                label += " · " + "/".join(rotations)
            result.append({"label": label, "token": {key: value for key, value in choice.items() if key != "weight"}})
        return result
    for key, choices in data.get("variants", {}).items():
        if isinstance(choices, list) and len(choices) > 1:
            groups.append({"when": dict(pair.split("=", 1) for pair in key.split(",") if "=" in pair), "choices": labels(choices)})
    for part in data.get("multipart", ()):
        choices = part["apply"]
        if isinstance(choices, list) and len(choices) > 1:
            groups.append({"when": part.get("when", {}), "choices": labels(choices)})
    return {"groups": groups}


def shape_items(specification: dict, state: dict) -> tuple[tuple[str, str, str, int], ...]:
    """Pure data projection for UI drawing: no resources or Blender access."""
    groups = [row["choices"] for row in specification.get("groups", ()) if _matches(row["when"], state)]
    if not groups:
        return (("0", "默认造型", "", 0),)
    count = math.lcm(*(len(group) for group in groups))
    result, seen = [], set()
    for index in range(count):
        choices = [group[index % len(group)] for group in groups]
        token = json.dumps([choice["token"] for choice in choices], sort_keys=True)
        if token in seen:
            continue
        seen.add(token)
        result.append((str(index), " / ".join(dict.fromkeys(choice["label"] for choice in choices)), "原版模型资源中的有效造型", index))
    return tuple(result)


def default_state(identifier: str, options=None) -> dict:
    options = schema(identifier) if options is None else options
    preferred = {
        "facing": "north",
        "axis": "y",
        "half": "lower",
        "type": "single",
        "shape": "straight",
        "part": "foot",
        "hinge": "left",
        "rotation": "0",
        "age": "0",
        "level": "0",
        "delay": "1",
        "layers": "1",
        "candles": "1",
        "eggs": "1",
        "pickles": "1",
        "distance": "7",
        "copper_golem_pose": "standing",
    }
    result = {}
    for key, values in options.items():
        requested = preferred.get(key, "false")
        result[key] = requested if requested in values else values[0]
    if identifier.split(":")[-1].endswith("shulker_box") and "facing" in result:
        result["facing"] = "up"
    if identifier == "minecraft:end_rod" and "facing" in result:
        result["facing"] = "up"
    if identifier.split(":")[-1].endswith("chest") and "facing" in result:
        result["facing"] = "south"
    return result


def plan(
    identifier: str,
    *,
    state: dict | None = None,
    shape: int = 0,
    special: bool = False,
    complete: bool = True,
    context: str = "world",
) -> tuple[list[dict], dict]:
    """Return model descriptors and normalized state, with no Blender side effects."""
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("请选择资产")
    options = schema(identifier)
    normalized = default_state(identifier, options)
    for key, value in (state or {}).items():
        if key not in options or str(value) not in options[key]:
            raise ValueError(f"方块状态无效：{key}={value}")
        normalized[key] = str(value)
    if context == "world" and complete and set(options.get("half", ())) == {"lower", "upper"}:
        normalized["half"] = "lower"
        lower, _ = plan(
            identifier,
            state={**normalized, "half": "lower"},
            shape=shape,
            special=special,
            complete=False,
        )
        upper, _ = plan(
            identifier,
            state={**normalized, "half": "upper"},
            shape=shape,
            special=special,
            complete=False,
        )
        return (
            [{**part, "_oxal_structure_part": "lower"} for part in lower]
            + [
                {**part, "_oxal_offset": [0, 1, 0], "_oxal_structure_part": "upper"}
                for part in upper
            ]
        ), normalized
    if context == "world" and complete and set(options.get("part", ())) == {"head", "foot"}:
        normalized["part"] = "foot"
        foot, _ = plan(
            identifier,
            state={**normalized, "part": "foot"},
            shape=shape,
            special=special,
            complete=False,
        )
        head, _ = plan(
            identifier,
            state={**normalized, "part": "head"},
            shape=shape,
            special=special,
            complete=False,
        )
        offset = {
            "north": [0, 0, -1],
            "south": [0, 0, 1],
            "east": [1, 0, 0],
            "west": [-1, 0, 0],
        }.get(str(normalized.get("facing", "north")), [0, 0, -1])
        return (
            [{**part, "_oxal_structure_part": "foot"} for part in foot]
            + [
                {**part, "_oxal_offset": offset, "_oxal_structure_part": "head"}
                for part in head
            ]
        ), normalized
    block_path = resource(identifier, "blockstates")
    if context == "world" and block_path.is_file():
        block = read(block_path)
        descriptors = []
        for key, choice in block.get("variants", {}).items():
            condition = dict(
                pair.split("=", 1) for pair in key.split(",") if "=" in pair
            )
            if _matches(condition, normalized):
                descriptors.append(
                    choice[shape % len(choice)] if isinstance(choice, list) else choice
                )
        for part in block.get("multipart", []):
            if _matches(part.get("when", {}), normalized):
                choice = part["apply"]
                descriptors.append(
                    choice[shape % len(choice)] if isinstance(choice, list) else choice
                )
        if descriptors and any(
            model(desc["model"]).get("elements") for desc in descriptors
        ):
            return descriptors, normalized
    node = read(resource(item_identifier(identifier), "items"))["model"]
    return item_nodes(
        node,
        context=("firstperson_righthand" if special else "fixed") if context == "world" else context,
        selection=shape,
        state=normalized,
    ), normalized


def preview_path(identifier: str) -> Path | None:
    """Resolve a texture for an explicitly requested catalog refresh."""
    for suffix in (".webp", ".png"):
        baked = ROOT / "preview" / (identifier.replace(":", "_") + suffix)
        if baked.is_file():
            return baked
    try:
        descriptors, _ = plan(identifier)
        for desc in descriptors:
            if desc.get("type", "").endswith(":special"):
                continue
            data = model(desc["model"])
            textures = data.get("textures", {})
            for key in ("layer0", "all", "side", "front", "particle", *textures):
                if key in textures:
                    path = resource(
                        texture(textures[key], textures), "textures", ".png"
                    )
                    if path.is_file():
                        return path
    except (OSError, KeyError, ValueError, TypeError):
        return None
    return None
