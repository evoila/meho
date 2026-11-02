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
* :func:`forget_target_in_connectors` -- drop the logins and credentials
  that every cached connector instance keeps for one target, so a changed
  target is used fresh on the next dispatch (#3879).
* :func:`forget_target_if_changed` -- the same drop, run by every process
  that executes an operation (a backplane server, a satellite runner) when
  the target row it reads was changed since this process last saw it. The
  process that served the change drops at once; every other process drops
  on its next call (#3879).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.base import Connector

__all__ = [
    "forget_target_if_changed",
    "forget_target_in_connectors",
    "get_or_create_connector_instance",
    "import_handler",
    "is_unbound_method",
    "reset_connector_instance_cache",
    "reset_handler_cache",
]

_log = structlog.get_logger(__name__)

#: The duck-typed connector hooks that drop what a connector keeps for one
#: target: ``invalidate_session`` drops the login token, and
#: ``invalidate_credentials`` drops the secret read from Vault (and, for some
#: connectors, the client built from it).
_TARGET_FORGET_HOOKS = ("invalidate_session", "invalidate_credentials")

# ``handler_ref`` -> resolved callable cache.
_HANDLER_CACHE: dict[str, Callable[..., Awaitable[Any]]] = {}

# ``type[Connector]`` -> singleton instance cache.
_CONNECTOR_INSTANCE_CACHE: dict[type[Connector], Connector] = {}

# ``target_cache_key`` -> the ``updated_at`` this process last saw for the
# target. Per process, like the connector caches it guards (#3879).
_SEEN_TARGET_VERSIONS: dict[tuple[str, str], Any] = {}

# ``target_cache_key`` -> the lock a drop for the target runs under. A call
# that arrives while a drop runs waits for it, instead of reading the new
# version as already handled and reusing an old login (#3879).
_TARGET_FORGET_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def reset_handler_cache() -> None:
    """Empty the handler-ref cache. Test-only."""
    _HANDLER_CACHE.clear()


def reset_connector_instance_cache() -> None:
    """Empty the connector-instance cache and the seen target versions. Test-only."""
    _CONNECTOR_INSTANCE_CACHE.clear()
    _SEEN_TARGET_VERSIONS.clear()
    _TARGET_FORGET_LOCKS.clear()


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


async def forget_target_in_connectors(target: Any) -> None:
    """Drop what every cached connector instance keeps for *target*.

    Connectors keep logins and credentials per target, keyed on the
    ``(tenant_id, id)`` pair, which a target change does not alter. Without
    this, a changed ``secret_ref`` (or host, TLS setting, ...) is used only
    after the kept login expires (#3879). The target update route calls this
    after it changes the row.

    Each instance in the cache gets every hook in :data:`_TARGET_FORGET_HOOKS`
    that it has. The hooks only remove this target's own entries, so calling
    them on a connector that never saw the target is a no-op. All instances
    are asked, not only the one the target resolves to now, because the
    change may have moved the target to another connector. A failing hook is
    logged and skipped: the target change must still go through, and one
    connector must not stop the others from forgetting the target.

    The drop runs under the target's lock, and the target's ``updated_at``
    is recorded as seen only when every hook is done. So a dispatch that
    arrives while a hook still runs waits for the drop, and does not reuse a
    login that is about to be dropped. After that, the next dispatch in this
    process does not drop the same change a second time.
    """
    async with _forget_lock(target):
        await _run_forget_hooks(target)
        _record_seen_version(target)


def _forget_lock(target: Any) -> asyncio.Lock:
    key = target_cache_key(target)
    lock = _TARGET_FORGET_LOCKS.get(key)
    if lock is None:
        lock = _TARGET_FORGET_LOCKS[key] = asyncio.Lock()
    return lock


async def _run_forget_hooks(target: Any) -> None:
    for instance in list(_CONNECTOR_INSTANCE_CACHE.values()):
        for hook_name in _TARGET_FORGET_HOOKS:
            hook = getattr(instance, hook_name, None)
            if not callable(hook):
                continue
            try:
                await hook(target)
            except Exception as exc:
                _log.warning(
                    "target_forget_hook_failed",
                    connector=type(instance).__name__,
                    hook=hook_name,
                    target_id=str(getattr(target, "id", "?")),
                    error=f"{type(exc).__name__}: {exc}",
                )


def _record_seen_version(target: Any) -> None:
    """Store *target*'s ``updated_at`` as the version this process saw last."""
    _SEEN_TARGET_VERSIONS[target_cache_key(target)] = getattr(target, "updated_at", None)


async def forget_target_if_changed(target: Any) -> bool:
    """Drop the kept logins for *target* when its row changed since this process last saw it.

    #3879. The target update route drops kept logins only in the process
    that serves the PATCH. Every other backplane server, and every satellite
    runner, keeps its own connector caches. So every execution path calls
    this with the target it just read, before a connector reuses a login:
    the dispatcher does, and so does the runner executor. A change shows as
    a new ``updated_at`` on the row (the update route always refreshes it,
    also when the same ``secret_ref`` is sent again). When the value differs
    from the one this process saw last, the kept logins are dropped and the
    next login uses the new values.

    The check and the drop run under the target's lock. A second call with
    the same new version waits until the first call's drop is done, then
    sees the version as handled. Without the lock, it would see the version
    as handled while a hook still runs, and reuse the old login.

    The first sighting only records the value: nothing was kept for the
    target by a dispatch yet. A target object with no ``updated_at`` (a test
    double) is never treated as changed. Returns ``True`` when a drop ran.
    """
    updated_at = getattr(target, "updated_at", None)
    if updated_at is None:
        return False
    key = target_cache_key(target)
    async with _forget_lock(target):
        if key not in _SEEN_TARGET_VERSIONS:
            _record_seen_version(target)
            return False
        if _SEEN_TARGET_VERSIONS[key] == updated_at:
            return False
        _log.info(
            "target_changed_since_last_seen",
            target_id=str(getattr(target, "id", "?")),
            target=getattr(target, "name", None),
        )
        await _run_forget_hooks(target)
        _record_seen_version(target)
        return True


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
