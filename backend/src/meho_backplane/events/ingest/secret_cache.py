# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A short per-process copy of event source secrets for the ingest path.

Each MEHO process keeps the secret it read from Vault for a source, so a
burst of deliveries -- good or bad -- reads Vault about once per source,
window and process instead of once per delivery.

A kept secret is used only while both of these hold:

* it was read in the current rate-limit window (one minute), and
* the source's **secret version** in Valkey
  (``meho:ingest:secret-version:{secret_ref}``) is the same as when it was
  read.

The update route calls :func:`bump_secret_version` after it writes a new
secret to Vault. Every process sees the new version on its next delivery
and reads Vault again, so a changed secret takes effect at once in every
process. The version is read **before** the Vault read, so a read that
races a change is stored under the old version and is not used again.

Within one process, deliveries that find no usable copy wait on one lock
per source, so only one of them reads Vault. The secret stays a
:class:`~pydantic.SecretStr`; it is never written to Valkey.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from pydantic import SecretStr

from meho_backplane.broadcast.client import get_broadcast_client

__all__ = [
    "bump_secret_version",
    "cached_secret",
    "clear_secret_cache",
]


@dataclass(frozen=True)
class _Entry:
    window: int
    version: str | None
    secret: SecretStr


_entries: dict[tuple[UUID, str], _Entry] = {}
_locks: dict[tuple[UUID, str], asyncio.Lock] = {}


def _version_key(secret_ref: str) -> str:
    return f"meho:ingest:secret-version:{secret_ref}"


async def _secret_version(secret_ref: str) -> str | None:
    value = await get_broadcast_client().get(_version_key(secret_ref))
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


async def bump_secret_version(secret_ref: str) -> None:
    """Mark the secret at *secret_ref* as changed for every MEHO process.

    Call it after the new secret is written to Vault. A Valkey failure
    propagates, so the caller can fail the change loudly.
    """
    await get_broadcast_client().incr(_version_key(secret_ref))


def _usable(entry: _Entry | None, window: int, version: str | None) -> bool:
    return entry is not None and entry.window == window and entry.version == version


async def cached_secret(
    source_id: UUID,
    secret_ref: str,
    window: int,
    read: Callable[[], Awaitable[SecretStr]],
) -> SecretStr:
    """Return the source's secret, from this process's copy when it is usable.

    *read* is called (at most once at a time per source) when the copy is
    missing, from another window, or from another secret version. A failed
    *read* stores nothing and its error propagates.
    """
    key = (source_id, secret_ref)
    version = await _secret_version(secret_ref)
    entry = _entries.get(key)
    if _usable(entry, window, version):
        assert entry is not None
        return entry.secret
    async with _locks.setdefault(key, asyncio.Lock()):
        entry = _entries.get(key)
        if _usable(entry, window, version):
            assert entry is not None
            return entry.secret
        secret = await read()
        for old_key in [k for k, e in _entries.items() if e.window != window]:
            del _entries[old_key]
        _entries[key] = _Entry(window=window, version=version, secret=secret)
        return secret


def clear_secret_cache() -> None:
    """Drop every kept secret and lock (tests)."""
    _entries.clear()
    _locks.clear()
