# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One role change at a time per SSO realm (VCF Operations 9.1 fleet roles).

The vendor's ``PUT .../principals/{principalId}/roles`` replaces all role
assignments of a user or group, so ``vrops.fleet.role.assign`` reads the
current set, merges its change and sends the whole set back. Two changes in
the same realm at the same time would each read the same set, so the second
write would drop the first change; two removals of the administrator role
could each still see the other administrator and both go through.

:func:`change_lock` therefore holds a lock for the realm from the read to the
write: an ``asyncio.Lock`` for calls in this process and a PG advisory lock
(:func:`meho_backplane.db.advisory.advisory_lock`) for calls in other
workers. A second call waits; after :data:`CHANGE_LOCK_WAIT_SECONDS` it fails
instead of writing a stale set. The same shape serializes the image profile
changes of the VCF Automation connector.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from meho_backplane.db.advisory import advisory_lock

if TYPE_CHECKING:
    from meho_backplane.connectors.vcf_operations.session import VcfOperationsTargetLike

__all__ = ["CHANGE_LOCK_WAIT_SECONDS", "change_lock", "held_locks", "lock_key"]

#: How long a role change waits for the lock of its realm before it fails.
CHANGE_LOCK_WAIT_SECONDS: float = 30.0


@dataclass(slots=True)
class _HeldLock:
    lock: asyncio.Lock
    users: int = 0


#: In-process locks by key; an entry lives while a call holds or waits for it.
#: Tests check that it is empty after every call.
held_locks: dict[int, _HeldLock] = {}


def lock_key(target: VcfOperationsTargetLike, realm_id: str) -> int:
    """A stable signed-63-bit key for one SSO realm of one target (a PG ``bigint``)."""
    raw = f"{target.tenant_id}\0{target.id}\0{realm_id.lower()}".encode()
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


@asynccontextmanager
async def change_lock(target: VcfOperationsTargetLike, realm_id: str) -> AsyncIterator[None]:
    """Hold the realm's lock in this process and across workers, or fail after the wait.

    Only taking the locks is bounded by :data:`CHANGE_LOCK_WAIT_SECONDS`.
    Once held, the locks stay for the whole block, however long the reads
    and the write take: a timer over the write could cancel it while the
    appliance still applies it, and then say that nothing was written.
    """
    key = lock_key(target, realm_id)
    held = held_locks.setdefault(key, _HeldLock(asyncio.Lock()))
    held.users += 1
    try:
        async with AsyncExitStack() as locks:
            try:
                async with asyncio.timeout(CHANGE_LOCK_WAIT_SECONDS):
                    await locks.enter_async_context(held.lock)
                    locked = await locks.enter_async_context(
                        advisory_lock(
                            key,
                            subsystem="vrops_fleet_roles",
                            wait_seconds=CHANGE_LOCK_WAIT_SECONDS,
                        )
                    )
                    if not locked:
                        raise TimeoutError
            except TimeoutError:
                raise RuntimeError(
                    f"another role change in SSO realm {realm_id!r} is still running; "
                    "nothing was written, run this op again in a minute"
                ) from None
            yield
    finally:
        held.users -= 1
        if held.users == 0:
            held_locks.pop(key, None)
