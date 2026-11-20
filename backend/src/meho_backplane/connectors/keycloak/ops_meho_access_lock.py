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

The lock works in one process (an :class:`asyncio.Lock` per realm) and
across pods (a PostgreSQL advisory lock that the database gives back at
the end of the transaction, ``pg_advisory_xact_lock``). Outside a scope
(the check before approval, which writes nothing) no lock is taken.
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

from meho_backplane.db.engine import get_engine

__all__ = ["hold_meho_access_lock", "meho_access_scope"]

#: Waiting for the lock gives up after this many seconds.
LOCK_TIMEOUT_S: Final = 60.0


@dataclass
class _Scope:
    stack: AsyncExitStack
    held: set[str] = field(default_factory=set)


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
async def _realm_lock(realm: str) -> AsyncIterator[None]:
    async with _local_lock(realm):
        engine = get_engine()
        if engine.dialect.name != "postgresql":
            yield
            return
        async with engine.connect() as conn, conn.begin():
            await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _db_key(realm)})
            yield


async def hold_meho_access_lock(realm: str) -> None:
    """Take the lock of *realm* until the scope ends; nothing outside a scope.

    Raises when the lock cannot be taken in time or the database fails.
    """
    scope = _SCOPE.get()
    if scope is None:
        return
    key = realm.casefold()
    if key in scope.held:
        return
    async with asyncio.timeout(LOCK_TIMEOUT_S):
        await scope.stack.enter_async_context(_realm_lock(key))
    scope.held.add(key)
