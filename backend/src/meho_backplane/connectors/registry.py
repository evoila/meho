# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector registry — module-level lookup table for connector classes.

Every connector module in ``connectors/<product>/`` calls
:func:`register_connector` at the top of its ``__init__.py``.
:func:`_eager_import_connectors` (called from the lifespan hook) imports
each subpackage in turn, triggering those registrations before the first
request arrives.

Duplicate registration raises :exc:`RuntimeError` — two modules claiming
the same product slug is a programming bug, not a runtime condition, and
should surface as a deploy failure.
"""

import importlib
import pkgutil

import structlog

from meho_backplane.connectors.base import Connector

__all__ = [
    "_eager_import_connectors",
    "all_connectors",
    "clear_registry",
    "get_connector",
    "register_connector",
]

_log = structlog.get_logger(__name__)

_REGISTRY: dict[str, type[Connector]] = {}


def register_connector(product: str, cls: type[Connector]) -> None:
    """Register a connector class under a product slug.

    Called at module import time from ``connectors/<product>/__init__.py``.
    Raises :exc:`TypeError` when ``cls`` is not a :class:`Connector` subclass.
    Raises :exc:`RuntimeError` on duplicate registration.
    """
    if not (isinstance(cls, type) and issubclass(cls, Connector)):
        raise TypeError(f"connector class for product={product!r} must subclass Connector: {cls!r}")
    if product in _REGISTRY:
        raise RuntimeError(
            f"connector already registered for product={product!r}: "
            f"existing={_REGISTRY[product].__name__}, attempted={cls.__name__}"
        )
    _REGISTRY[product] = cls
    _log.info("connector_registered", product=product, cls=cls.__name__)


def get_connector(product: str) -> type[Connector] | None:
    """Look up a connector class by product slug. Returns ``None`` if not found."""
    return _REGISTRY.get(product)


def all_connectors() -> dict[str, type[Connector]]:
    """Return a snapshot of the registry — for diagnostics / introspection."""
    return dict(_REGISTRY)


def _eager_import_connectors() -> None:
    """Import every ``connectors/<product>/`` subpackage so registrations land.

    Called from ``main.py`` lifespan. Each subpackage self-registers by
    calling :func:`register_connector` at module top-level.
    """
    import meho_backplane.connectors as pkg

    for _, name, ispkg in pkgutil.iter_modules(pkg.__path__):
        if ispkg:
            importlib.import_module(f"{pkg.__name__}.{name}")


def clear_registry() -> None:
    """Empty the registry. Test-only — never call from production code."""
    _REGISTRY.clear()
