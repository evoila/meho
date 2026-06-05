# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Valkey-backed spill store for materialized JSONFlux result handles.

G0.20-T7 (#1507). The default reducer
(:class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`)
materializes a large set-shaped response into an in-memory DuckDB table,
samples the first/last N rows for the inline preview, and then closes the
engine — discarding every row past the sample. This store gives those rows
somewhere to live so an agent can read the rest back over MCP via the
``result_query`` meta-tool.

Why Valkey
==========

The full-set persistence design was first drafted in G3.1-T4 (#304,
closed-superseded) as a ``HandleStore`` wrapping Valkey; this is that
design, revived and narrowed to the reduce-time spill use case. Valkey is
already the broadcast substrate
(:mod:`meho_backplane.broadcast.client`) so no new infrastructure is
introduced, and its native key TTL bounds the store **by construction** —
a spilled handle expires server-side without any sweeper, so the store
cannot become an unbounded-growth / memory-leak vector even if a process
crashes mid-flight.

Key shape + isolation
=====================

::

    KEY:   meho:reshandle:{tenant_id}:{handle_id}
    VALUE: JSON {operator_sub, op_id, rows, total_rows, stored_rows, created_at}
    TTL:   the handle's ttl_seconds (server-enforced)

JSON (not msgpack) so the value is ``str``-compatible with the shared
broadcast client's ``decode_responses=True`` posture; the rows are
already JSON-shaped (they came from a JSON op response through DuckDB),
so no information is lost.

The tenant prefix scopes the keyspace; the stored ``operator_sub`` is
checked on every read so another operator in the same tenant gets a miss
(``None``), not another operator's rows. This mirrors the #304 contract
exactly.

Bounded size
============

A pathological op could return millions of rows; spilling all of them
would blow the per-key value size. :meth:`spill` caps the persisted row
count at ``max_rows`` (the reducer passes
:attr:`~meho_backplane.settings.Settings.result_handle_max_spill_rows`)
and records both the cap-applied ``stored_rows`` and the true
``total_rows`` so a reader can tell when the tail was truncated. Combined
with the TTL, the store's footprint is bounded on both axes.

Fail-open
=========

Every method swallows Valkey/serialization errors and degrades to the
"no spill" path (``spill`` returns ``False``; ``fetch_window`` returns
``None``). A reduce must never fail because the spill backend is
unreachable — the inline sample still ships, exactly as it did before
this store existed. The MCP read tool surfaces the miss as a typed
"handle not found / expired" to the agent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import msgspec
import redis.asyncio as redis
import structlog

__all__ = [
    "ResultHandleStore",
    "SpilledWindow",
    "get_result_handle_store",
    "reset_result_handle_store_for_testing",
]

_log = structlog.get_logger(__name__)

#: Key namespace for spilled result-handle rows. Distinct from the
#: broadcast stream keys so the two never collide on a shared Valkey.
_KEY_PREFIX = "meho:reshandle"


def _key(tenant_id: UUID, handle_id: UUID) -> str:
    """Tenant-scoped Valkey key for a spilled handle."""
    return f"{_KEY_PREFIX}:{tenant_id}:{handle_id}"


def _to_bytes(raw: str | bytes) -> bytes:
    """Normalize a Valkey ``GET`` result to bytes for the JSON decoder.

    The shared broadcast client runs ``decode_responses=True`` so it
    returns ``str``; a bytes-mode client (or a test fake) returns
    ``bytes``. ``msgspec.json.decode`` accepts both, but normalizing here
    keeps the type explicit and the decode call total.
    """
    return raw.encode("utf-8") if isinstance(raw, str) else raw


class SpilledWindow(msgspec.Struct, frozen=True):
    """A read-back window served by :meth:`ResultHandleStore.fetch_window`.

    ``rows`` is the requested slice ``[offset : offset + limit]`` of the
    spilled rows. ``total_rows`` is the full collection size the reducer
    saw (may exceed ``stored_rows`` when the spill was capped).
    ``stored_rows`` is how many rows are actually retrievable from the
    store — windows past it return empty. ``truncated`` is ``True`` when
    the reducer capped the spill (``stored_rows < total_rows``), so the
    tail beyond ``stored_rows`` is not retrievable.
    """

    rows: list[dict[str, Any]]
    total_rows: int
    stored_rows: int
    truncated: bool


class _StoredPayload(msgspec.Struct):
    """Wire shape persisted under a spilled-handle key."""

    operator_sub: str
    op_id: str | None
    rows: list[dict[str, Any]]
    total_rows: int
    stored_rows: int
    created_at: str


class ResultHandleStore:
    """Persist + read back the full materialized rows of a reduced dispatch.

    A thin wrapper over an async Valkey client. One instance is shared
    process-wide via :func:`get_result_handle_store`; the reducer spills
    into it at materialize time and the ``result_query`` MCP tool reads
    windows back out of it.
    """

    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    async def spill(
        self,
        *,
        tenant_id: UUID,
        operator_sub: str,
        handle_id: UUID,
        op_id: str | None,
        rows: list[dict[str, Any]],
        total_rows: int,
        ttl_seconds: int,
        max_rows: int,
    ) -> bool:
        """Persist *rows* (capped at *max_rows*) under the handle key.

        Returns ``True`` when the rows were stored and are retrievable via
        :meth:`fetch_window`, ``False`` when the spill was skipped or
        failed (Valkey unreachable, serialization error, empty input). A
        ``False`` return is non-fatal: the caller keeps the inline sample
        and leaves the handle's drill-in surface marked unavailable.

        The TTL is enforced server-side by Valkey (``SET ... EX``); no
        sweeper is needed and a crashed process leaves no orphaned key
        past the TTL.
        """
        if not rows or ttl_seconds <= 0 or max_rows <= 0:
            return False
        stored = rows[:max_rows]
        payload = _StoredPayload(
            operator_sub=operator_sub,
            op_id=op_id,
            rows=stored,
            total_rows=total_rows,
            stored_rows=len(stored),
            created_at=datetime.now(UTC).isoformat(),
        )
        try:
            encoded = msgspec.json.encode(payload)
            await self._client.set(_key(tenant_id, handle_id), encoded, ex=ttl_seconds)
        except (redis.RedisError, msgspec.EncodeError, OSError) as exc:
            # Fail-open: a reduce must never fail because the spill
            # backend is unreachable. The inline sample still ships.
            _log.warning(
                "result_handle_spill_failed",
                handle_id=str(handle_id),
                tenant_id=str(tenant_id),
                op_id=op_id,
                error=str(exc),
            )
            return False
        _log.info(
            "result_handle_spilled",
            handle_id=str(handle_id),
            tenant_id=str(tenant_id),
            op_id=op_id,
            stored_rows=len(stored),
            total_rows=total_rows,
            ttl_seconds=ttl_seconds,
        )
        return True

    async def fetch_window(
        self,
        *,
        tenant_id: UUID,
        operator_sub: str,
        handle_id: UUID,
        offset: int,
        limit: int,
    ) -> SpilledWindow | None:
        """Return the ``[offset : offset+limit]`` slice of a spilled handle.

        Returns ``None`` when the handle is unknown, expired, belongs to
        a different operator, or the store is unreachable — the four
        cases the MCP tool surfaces to the agent as the same typed
        "handle not found or expired" miss (cross-operator access is
        deliberately indistinguishable from "not found" so the store
        leaks no existence signal across the operator boundary).

        ``offset`` past ``stored_rows`` yields an empty ``rows`` list with
        the metadata still populated, so a paging caller learns it has
        reached the end without a separate count call.
        """
        try:
            raw = await self._client.get(_key(tenant_id, handle_id))
        except (redis.RedisError, OSError) as exc:
            _log.warning(
                "result_handle_fetch_failed",
                handle_id=str(handle_id),
                tenant_id=str(tenant_id),
                error=str(exc),
            )
            return None
        if raw is None:
            return None
        try:
            payload = msgspec.json.decode(_to_bytes(raw), type=_StoredPayload)
        except (msgspec.DecodeError, msgspec.ValidationError) as exc:
            _log.warning(
                "result_handle_decode_failed",
                handle_id=str(handle_id),
                tenant_id=str(tenant_id),
                error=str(exc),
            )
            return None
        if payload.operator_sub != operator_sub:
            # Cross-operator access within the same tenant: a miss, not
            # another operator's rows. Mirrors #304's isolation contract.
            return None
        start = max(offset, 0)
        window = payload.rows[start : start + limit] if limit > 0 else []
        return SpilledWindow(
            rows=window,
            total_rows=payload.total_rows,
            stored_rows=payload.stored_rows,
            truncated=payload.stored_rows < payload.total_rows,
        )


#: Process-wide singleton. Lazily built from the broadcast Valkey client
#: the first time the reducer spills or the read tool fetches.
_STORE: ResultHandleStore | None = None


def get_result_handle_store() -> ResultHandleStore:
    """Return the process-wide :class:`ResultHandleStore`.

    Reuses the broadcast fast client's connection pool
    (:func:`~meho_backplane.broadcast.client.get_broadcast_client`) rather
    than opening a second pool: spilled-handle reads/writes are small,
    non-blocking ``SET`` / ``GET`` calls that fit the fast client's
    bounded-latency posture (a hung Valkey fails fast instead of stranding
    the reduce path). The store encodes its payload as JSON so the value
    is ``str``-compatible with that client's ``decode_responses=True``
    posture; :func:`_to_bytes` normalizes the read side regardless of the
    client's decode mode.
    """
    global _STORE
    if _STORE is None:
        # Imported lazily so importing this module doesn't pull the
        # broadcast client (and its settings read) at import time.
        from meho_backplane.broadcast.client import get_broadcast_client

        _STORE = ResultHandleStore(get_broadcast_client())
    return _STORE


def reset_result_handle_store_for_testing() -> None:
    """Clear the cached store. Test-only.

    Tests inject a fake client by constructing :class:`ResultHandleStore`
    directly; this resets the lazy singleton so a later production-path
    call rebuilds it from the (possibly re-pointed) broadcast client.
    """
    global _STORE
    _STORE = None
