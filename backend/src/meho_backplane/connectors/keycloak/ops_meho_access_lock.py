# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One write at a time per login realm, from the count of platform admins to the write.

Before some Keycloak writes in MEHO's own login realm, MEHO counts the
other platform admins
(:mod:`~meho_backplane.connectors.keycloak.ops_user_meho_access_guard`).
Two such writes that run at the same moment must not both see the other
platform admin and both pass. So the run of such a write holds a lock for
its login realm from the count until the write is done:

* :func:`meho_access_scope` wraps the run of the write. It holds no lock
  by itself.
* :func:`hold_meho_access_lock` takes the lock of the realm inside that
  scope, right before the count. The scope gives it back when the write is
  done, also when the write fails.
* :func:`confirm_meho_access_lock` checks, after the count and before the
  write is sent, that the database still holds each lock of the scope.
* :func:`meho_access_check_only` wraps the check before approval, which
  writes nothing. There :func:`hold_meho_access_lock` takes no lock.

:func:`hold_meho_access_lock` outside both raises
:exc:`MehoAccessScopeError`: a run path without a scope fails and sends no
write.

The lock works in one process (an :class:`asyncio.Lock` per realm) and
across pods (a PostgreSQL advisory lock that the database gives back at
the end of the transaction, ``pg_advisory_xact_lock``). The transaction
of the lock turns off the database's idle and transaction time limits for
itself, so the database does not end it while MEHO reads Keycloak.
"""

from __future__ import annotations

import asyncio
import hashlib
import weakref
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from meho_backplane.db.engine import get_engine

__all__ = [
    "MehoAccessScopeError",
    "confirm_meho_access_lock",
    "hold_meho_access_lock",
    "meho_access_check_only",
    "meho_access_scope",
]

#: Waiting for the lock gives up after this many seconds.
LOCK_TIMEOUT_S: Final = 60.0


#: The time limits the lock's transaction turns off for itself, where the database has them.
_NO_TIME_LIMITS: Final = text(
    "SELECT set_config(name, '0', true) FROM pg_settings "
    "WHERE name IN ('idle_in_transaction_session_timeout', 'transaction_timeout')"
)


class MehoAccessScopeError(RuntimeError):
    """A guarded write asked for the lock outside :func:`meho_access_scope`."""


@dataclass
class _Scope:
    stack: AsyncExitStack | None
    #: realm -> the database connection that holds its lock (``None``: no database lock)
    held: dict[str, AsyncConnection | None] = field(default_factory=dict)


_SCOPE: ContextVar[_Scope | None] = ContextVar("meho_access_scope", default=None)

#: The in-process locks, per event loop and realm (a lock works in one loop only).
_LOCAL: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)


@asynccontextmanager
async def meho_access_scope() -> AsyncIterator[None]:
    """Give back every lock taken in this scope when the block ends."""
    if _SCOPE.get() is not None:
        yield
        return
    async with AsyncExitStack() as stack:
        token = _SCOPE.set(_Scope(stack))
        try:
            yield
        finally:
            _SCOPE.reset(token)


@asynccontextmanager
async def meho_access_check_only() -> AsyncIterator[None]:
    """Mark the check before approval: it writes nothing, so it takes no lock."""
    if _SCOPE.get() is not None:
        yield
        return
    token = _SCOPE.set(_Scope(None))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _db_key(realm: str) -> int:
    digest = hashlib.blake2b(f"meho:keycloak-meho-access\0{realm}".encode(), digest_size=8)
    return int.from_bytes(digest.digest(), "big") & 0x7FFF_FFFF_FFFF_FFFF


def _local_lock(realm: str) -> asyncio.Lock:
    locks = _LOCAL.setdefault(asyncio.get_running_loop(), {})
    lock = locks.get(realm)
    if lock is None:
        lock = locks[realm] = asyncio.Lock()
    return lock


@asynccontextmanager
async def _realm_lock(realm: str) -> AsyncIterator[AsyncConnection | None]:
    async with _local_lock(realm):
        engine = get_engine()
        if engine.dialect.name != "postgresql":
            yield None
            return
        async with engine.connect() as conn, conn.begin():
            await conn.execute(_NO_TIME_LIMITS)
            await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _db_key(realm)})
            yield conn


async def hold_meho_access_lock(realm: str) -> None:
    """Take the lock of *realm* until the scope ends.

    Takes no lock in :func:`meho_access_check_only`. Raises
    :exc:`MehoAccessScopeError` outside a scope, and another error when the
    lock cannot be taken in time or the database fails.
    """
    scope = _SCOPE.get()
    if scope is None:
        raise MehoAccessScopeError("a guarded Keycloak write ran without its lock scope")
    if scope.stack is None:
        return
    key = realm.casefold()
    if key in scope.held:
        return
    async with asyncio.timeout(LOCK_TIMEOUT_S):
        scope.held[key] = await scope.stack.enter_async_context(_realm_lock(key))


async def confirm_meho_access_lock(realm: str) -> None:
    """Raise when the scope took the lock of *realm* and the database does not hold it now.

    Nothing to check in :func:`meho_access_check_only` or without a database lock.
    """
    scope = _SCOPE.get()
    if scope is None:
        raise MehoAccessScopeError("a guarded Keycloak write ran without its lock scope")
    if scope.stack is None:
        return
    key = realm.casefold()
    if key not in scope.held:
        raise MehoAccessScopeError("the lock of the realm was not taken")
    conn = scope.held[key]
    if conn is None:
        return
    # Taking a lock this transaction already holds answers true at once; a
    # lost transaction raises, and a lock another session took answers false.
    held = await conn.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _db_key(key)})
    if held is not True:
        raise RuntimeError("the database does not hold the lock of the realm now")
