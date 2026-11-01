# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One change at a time to one object: a lock from the read to the write.

Several IaaS changes read an object, build its new whole from what they
read and write the whole back (an image mapping, a flavor mapping, the
member lists of a project, the fields of a cloud zone). Two such changes
to the same object at the same time would each read the same object, add
their own part and write the whole back, so the second write would drop
the first part, and both would report success.

:func:`resource_lock` holds a lock for one object of one target from the
read to the write: an :class:`asyncio.Lock` for calls in this process and
a PG advisory lock (:func:`meho_backplane.db.advisory.advisory_lock`) for
calls in other workers. A second call waits; after the wait it fails
instead of writing a stale whole. Used by the image name change
(:mod:`._images`) and by the cloud zone, flavor profile and project member
changes (:mod:`._infra_changes`).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from meho_backplane.connectors._shared.cache_key import TenantScopedTargetLike
from meho_backplane.db.advisory import advisory_lock

__all__ = ["LOCK_WAIT_SECONDS", "lock_key", "resource_lock"]

#: How long a change waits for another change to the same object.
LOCK_WAIT_SECONDS: float = 30.0


@dataclass(slots=True)
class _Held:
    lock: asyncio.Lock
    users: int = 0


#: In-process locks by key; an entry lives while a call holds or waits for it.
_locks: dict[int, _Held] = {}


def lock_key(target: TenantScopedTargetLike, kind: str, resource_id: str) -> int:
    """A stable signed-63-bit key for one object of one target (a PG ``bigint``).

    *kind* names the kind of object in plain words (``"flavor profile"``), so
    two kinds whose ids could be the same never share a key.
    """
    raw = f"{target.tenant_id}\0{target.id}\0{kind}\0{resource_id}".encode()
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


@asynccontextmanager
async def resource_lock(
    target: TenantScopedTargetLike,
    kind: str,
    resource_id: str,
    *,
    wait_seconds: float | None = None,
) -> AsyncIterator[None]:
    """Hold the object's lock in this process and across workers, or fail after the wait.

    Only taking the locks is bounded by *wait_seconds* (default
    :data:`LOCK_WAIT_SECONDS`). Once held, the locks stay for the whole
    block, however long the reads and the write take: a timer over the
    write could cancel it while the appliance still applies it, and then
    say that nothing was written.
    """
    wait = LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
    key = lock_key(target, kind, resource_id)
    held = _locks.setdefault(key, _Held(asyncio.Lock()))
    held.users += 1
    try:
        async with AsyncExitStack() as locks:
            try:
                async with asyncio.timeout(wait):
                    await locks.enter_async_context(held.lock)
                    locked = await locks.enter_async_context(
                        advisory_lock(
                            key, subsystem=f"vcfa_{kind.replace(' ', '_')}", wait_seconds=wait
                        )
                    )
                    if not locked:
                        raise TimeoutError
            except TimeoutError:
                raise RuntimeError(
                    f"another change to {kind} {resource_id!r} is still running; "
                    "nothing was written, run this op again in a minute"
                ) from None
            yield
    finally:
        held.users -= 1
        if held.users == 0:
            _locks.pop(key, None)
