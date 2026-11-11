"""Source-authored native beacon, private per imported instance."""

from . import asset_catalog as catalog
from .asset_native_rigs import load


def build_entry(transaction, scene, collection, root, identifier, options, material_for):
    if identifier != "minecraft:beacon":
        return False
    load(transaction, scene, collection, root, catalog.ROOT / "native_effect/rig_beacon.blend", "OXAL Native beacon", pack_digest=options.get("pack_digest"))
    return True
