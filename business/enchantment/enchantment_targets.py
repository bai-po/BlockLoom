"""Read-only feature eligibility from the imported asset's logical identity."""

_WEAPONS = frozenset({"bow", "crossbow", "trident", "mace"})
_WEAPON_SUFFIXES = ("_sword", "_axe", "_spear")


def weapon(root):
    identifier = str(root.get("oxal_mc_studio_library_identifier", ""))
    namespace, separator, name = identifier.partition(":")
    return bool(separator and namespace == "minecraft"
                and (name in _WEAPONS or name.endswith(_WEAPON_SUFFIXES)))


def pixel_geometry_allowed(root):
    """All asset families may explicitly prepare their native pixel relief."""
    return True


def asset_meshes(root):
    """Read the same asset surfaces for Tools and UI; effects stay independent.

    This membership filter grants no ownership. Tools validate the actual root
    and its members before editing; drawing only uses it to present readiness.
    """
    from ..asset.assets import members

    return tuple(obj for obj in members(root)
                 if obj.type == "MESH" and not obj.get("oxal_mc_studio_effect_geometry"))
