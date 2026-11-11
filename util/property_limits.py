"""Journal UI metadata changes on exact IDs already proved by their caller."""

from ..core.runtime import ensure_mutation_allowed


def update(transaction, owner, settings):
    """Change limits without changing property values, Actions or drivers."""
    ensure_mutation_allowed()
    rows = [(name, options, owner.id_properties_ui(name).as_dict())
            for name, options in settings.items() if name in owner]

    def restore():
        for name, _options, before in rows:
            if name in owner:
                owner.id_properties_ui(name).update(**before)

    transaction.on_rollback(restore)
    try:
        for name, options, _before in rows:
            owner.id_properties_ui(name).update(**options)
    except Exception:
        restore()
        raise
