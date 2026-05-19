# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler-ref resolution + connector-instance caching for the G0.6 dispatcher.

Two related concerns the dispatcher needs but that don't belong in
:mod:`dispatcher` itself:

* :func:`import_handler` -- resolve a dotted ``handler_ref`` (produced
  by :func:`~meho_backplane.operations.typed_register.derive_handler_ref`)
  back to its callable via :func:`importlib.import_module` plus a
  :func:`getattr` walk. Module-level functions resolve in one
  ``getattr`` step; bound methods (``"pkg.mod.Class.method"``) resolve
  in two. Cached per-process; the cache is module-local so tests
  reset it via :func:`reset_handler_cache`.
* :func:`get_or_create_connector_instance` -- the resolver
  (:func:`~meho_backplane.connectors.resolver.resolve_connector`)
  returns a connector class; the dispatcher needs a single instance
  per class so the per-target transport cache
  (:class:`httpx.AsyncClient` pool on :class:`HttpConnector`,
  :class:`asyncssh.SSHClientConnection` pool on :class:`SshConnector`)
  persists across dispatches. The cache is module-local; reset via
  :func:`reset_connector_instance_cache`.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from meho_backplane.connectors.base import Connector

__all__ = [
    "get_or_create_connector_instance",
    "import_handler",
    "is_unbound_method",
    "reset_connector_instance_cache",
    "reset_handler_cache",
]

# ``handler_ref`` -> resolved callable cache.
_HANDLER_CACHE: dict[str, Callable[..., Awaitable[Any]]] = {}

# ``type[Connector]`` -> singleton instance cache.
_CONNECTOR_INSTANCE_CACHE: dict[type[Connector], Connector] = {}


def reset_handler_cache() -> None:
    """Empty the handler-ref cache. Test-only."""
    _HANDLER_CACHE.clear()


def reset_connector_instance_cache() -> None:
    """Empty the connector-instance cache. Test-only."""
    _CONNECTOR_INSTANCE_CACHE.clear()


def import_handler(handler_ref: str) -> Callable[..., Awaitable[Any]]:
    """Resolve a dotted *handler_ref* to its callable.

    Algorithm:

    1. Split on ``.``. Try the longest prefix as the module
       (e.g. ``"a.b.c.d"`` -> module ``"a.b.c"``, attr ``"d"``). On
       :class:`ImportError`, peel one more segment back to the left.
       Bottom-out at a single-segment module name.
    2. Walk the remaining segments via :func:`getattr` (handles
       bound-method paths like ``"pkg.mod.ClassName.method"`` --
       :func:`getattr` finds ``ClassName`` then ``ClassName.method``).
    3. Result MUST be callable; otherwise the caller sees
       ``handler_unreachable`` (this function raises :class:`TypeError`).

    Cached by ``handler_ref`` -- the import walk runs once per ref per
    process. Tests that swap a module's symbols reset the cache via
    :func:`reset_handler_cache`.
    """
    cached = _HANDLER_CACHE.get(handler_ref)
    if cached is not None:
        return cached

    parts = handler_ref.split(".")
    if not parts or any(not p for p in parts):
        raise ImportError(f"handler_ref must be a dotted path: {handler_ref!r}")

    # Walk from the longest-possible module prefix down to a single
    # segment, picking the deepest prefix that imports cleanly. This
    # handles ``pkg.mod.func`` (module = pkg.mod, attr = func) and
    # ``pkg.mod.Cls.meth`` (module = pkg.mod, attrs = Cls, meth)
    # without forcing the caller to know which segments are modules.
    module = None
    split_idx = len(parts)
    while split_idx > 0:
        candidate = ".".join(parts[:split_idx])
        try:
            module = importlib.import_module(candidate)
            break
        except ImportError:
            split_idx -= 1
            continue
    if module is None:
        raise ImportError(f"handler_ref {handler_ref!r} -- could not import any prefix as a module")

    obj: Any = module
    for attr in parts[split_idx:]:
        try:
            obj = getattr(obj, attr)
        except AttributeError as exc:
            raise ImportError(
                f"handler_ref {handler_ref!r} -- attribute {attr!r} not found on {obj!r}"
            ) from exc

    if not callable(obj):
        raise TypeError(f"handler_ref {handler_ref!r} resolved to non-callable {obj!r}")

    handler: Callable[..., Awaitable[Any]] = obj
    _HANDLER_CACHE[handler_ref] = handler
    return handler


def get_or_create_connector_instance(cls: type[Connector]) -> Connector:
    """Return the module-level cached instance of *cls*, creating it on first use.

    The resolver returns the connector **class**; the dispatcher needs
    a single instance per class so that the connector's per-target
    transport cache (:class:`httpx.AsyncClient` pool on
    :class:`HttpConnector`, the :class:`asyncssh.SSHClientConnection`
    pool on :class:`SshConnector`) persists across dispatches.
    Instantiation is lazy -- a connector class registered at import
    time but never dispatched against pays zero connection-pool cost.
    """
    cached = _CONNECTOR_INSTANCE_CACHE.get(cls)
    if cached is not None:
        return cached
    instance = cls()
    _CONNECTOR_INSTANCE_CACHE[cls] = instance
    return instance


def is_unbound_method(handler: Any, connector_cls: type[Connector]) -> bool:
    """True when *handler* is an unbound function defined anywhere on *connector_cls*'s MRO.

    :func:`import_handler` walks the dotted path with :func:`getattr`,
    which returns the **unbound** function for class-attribute lookups
    in Python 3 (no descriptor binding without an instance). The
    dispatcher rebinds these against the connector instance the resolver
    chose so bound-method handlers hit the right transport.

    Identity-based, not a string heuristic. The previous implementation
    tested ``handler.__qualname__.startswith(f"{connector_cls.__name__}.")``,
    which silently returned ``False`` — leaving the handler **unbound** and
    surfacing a misleading ``handler_unreachable`` (#697) — in two real
    cases:

    * The resolved connector instance is a **subclass** of the class that
      defines the handler. E.g. the bind9 E2E harness seeds a
      ``_SeededBind9Connector`` instance under the ``Bind9Connector``
      registry key (the documented test pattern):
      ``"Bind9Connector.about"`` does not start with
      ``"_SeededBind9Connector."``.
    * The handler is defined on a base / mixin, so ``__qualname__``
      carries the base's name rather than the concrete connector's.

    Walking ``connector_cls.__mro__`` and matching the exact function
    object stored in a class ``__dict__`` is subclass- and mixin-correct.
    Already-bound methods (``inspect.ismethod``) are not unbound — return
    ``False`` so they are not double-bound. Module-level function handlers
    are not in any connector's MRO, so they correctly return ``False`` and
    are dispatched as ``handler(operator, target, params)`` unchanged.
    """
    if inspect.ismethod(handler):
        return False
    name = getattr(handler, "__name__", None)
    if name is None:
        return False
    return any(klass.__dict__.get(name) is handler for klass in connector_cls.__mro__)
