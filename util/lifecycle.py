"""Explicit module ownership with best-effort reverse cleanup."""

import logging
from collections.abc import Sequence
from types import ModuleType

logger = logging.getLogger(__name__)


def register_modules(modules: Sequence[ModuleType]) -> None:
    registered = []
    try:
        for module in modules:
            module.register()
            registered.append(module)
    except Exception:
        for module in reversed(registered):
            try:
                module.unregister()
            except Exception:
                logger.exception("Registration rollback failed: %s", module.__name__)
        raise


def unregister_modules(modules: Sequence[ModuleType]) -> None:
    errors = []
    for module in reversed(modules):
        try:
            module.unregister()
        except Exception as exc:
            logger.exception("Module cleanup failed: %s", module.__name__)
            errors.append(exc)
    if errors:
        raise ExceptionGroup("OXAL module cleanup failed", errors)
