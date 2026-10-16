# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Valkey-backed spill store for materialized result handles.

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

Dedicated store + reader-first move (#3639)
===========================================

Result handles used to share one Valkey with the governance keys
(broadcast, rate limits, approval waits). A burst of large handles could
fill that Valkey and take governance down with it. Handles now move to a
dedicated result-cache Valkey (``RESULT_CACHE_REDIS_URL``, chart subchart
``result-cache``, ``maxmemory`` + ``noeviction``). The move is reader-first:

* Reads check the ``v2`` key on the dedicated client first, then the old key
  on the shared broadcast client. Both keys carry the same tenant id, so the
  fallback never crosses tenants. Old keys drain by their own TTL.
* Writes go to the old key on the shared client until the writer gate
  (``RESULT_CACHE_WRITER_ENABLED``) is on; then to the ``v2`` key on the
  dedicated client. There is no dual write.
* With ``RESULT_CACHE_REDIS_URL`` unset both clients are the broadcast
  client, so nothing changes before the pod ships.

Metrics are aggregate only (counts and memory bytes); no row content, key,
or vendor total is ever a label or a log field.

Key shape + isolation
=====================

::

    KEY:   meho:reshandle:v2:{tenant_id}:{handle_id}   (dedicated client)
           meho:reshandle:{tenant_id}:{handle_id}      (legacy, shared client)
    VALUE: JSON {operator_sub, op_id, rows, total_rows, stored_rows, created_at,
                 ..., record_kind, document}
    TTL:   the handle's ttl_seconds (server-enforced)

``record_kind`` is ``"rows"`` (a reduced collection, paged and queried) or
``"document"`` (one whole payload with no single collection, read with a
JSON Pointer ``path``, #3633). Records written before the field existed
decode as ``"rows"``. A rows record may also carry a ``document``: the
top-level fields the reducer left out of the inline summary because they
did not fit its byte budget. They are read with the same ``path`` read.

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
would blow the per-key value size. :meth:`spill` caps the persisted record by
row count and encoded bytes, using the configured limits.
It records both the cap-applied ``stored_rows`` and the true
``total_rows`` so a reader can tell when the tail was truncated. Combined
with the TTL, the store's footprint is bounded on both axes.

Fail-open
=========

Every method swallows Valkey/serialization errors and degrades to the
"no spill" path (``spill`` returns ``0``; ``fetch_window`` returns
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
from prometheus_client import Counter, Gauge

from meho_backplane.jsonflux.query.contract import QueryContractError
from meho_backplane.settings import get_settings

__all__ = [
    "ResultHandleStore",
    "ResultRecordKindError",
    "SpillResult",
    "SpilledDocument",
    "SpilledRowSet",
    "SpilledWindow",
    "dispose_result_handle_store",
    "get_result_handle_store",
    "reset_result_handle_store_for_testing",
]

_log = structlog.get_logger(__name__)

#: Key namespace for spilled result-handle rows. Distinct from the
#: broadcast stream keys so the two never collide on a shared Valkey.
_KEY_PREFIX = "meho:reshandle"

#: The two record kinds a handle key can hold. A rows record keeps a reduced
#: collection (paged and queried); a document record keeps one whole payload
#: that has no single collection (read with a JSON Pointer ``path``).
_ROWS_RECORD = "rows"
_DOCUMENT_RECORD = "document"

#: Namespace for handles written to the dedicated result-cache store.
_V2_KEY_PREFIX = f"{_KEY_PREFIX}:v2"

#: Same short timeouts as the broadcast fast client: a hung store fails
#: fast and the reduce path keeps its inline sample.
_SOCKET_TIMEOUT_SECONDS = 5.0
_CONNECT_TIMEOUT_SECONDS = 3.0

#: Result records the store tried to write, by outcome (``admitted`` /
#: ``rejected``) and namespace (``v2`` / ``legacy``). ``rejected`` covers a
#: record over the byte cap and a write the store refused or failed (for
#: example ``noeviction`` at ``maxmemory``).
RESULT_CACHE_RECORDS_TOTAL: Counter = Counter(
    "result_cache_records_total",
    "Result-handle records the store tried to write, by outcome and namespace.",
    labelnames=("outcome", "namespace"),
)

#: Result-handle reads, by where the record was found: ``v2`` (dedicated
#: store), ``legacy`` (fallback to the shared store), or ``miss``.
RESULT_CACHE_READS_TOTAL: Counter = Counter(
    "result_cache_reads_total",
    "Result-handle reads, by where the record was found (v2, legacy, miss).",
    labelnames=("source",),
)

#: Memory use of the dedicated result-cache store, sampled from
#: ``INFO memory`` after each ``v2`` write. ``kind`` is ``used`` or ``max``.
RESULT_CACHE_MEMORY_BYTES: Gauge = Gauge(
    "result_cache_memory_bytes",
    "Dedicated result-cache store memory in bytes (used, max), sampled after v2 writes.",
    labelnames=("kind",),
)


def _key(tenant_id: UUID, handle_id: UUID) -> str:
    """Tenant-scoped legacy Valkey key for a spilled handle (shared client)."""
    return f"{_KEY_PREFIX}:{tenant_id}:{handle_id}"


def _v2_key(tenant_id: UUID, handle_id: UUID) -> str:
    """Tenant-scoped Valkey key for a handle on the dedicated store."""
    return f"{_V2_KEY_PREFIX}:{tenant_id}:{handle_id}"


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


class SpilledRowSet(msgspec.Struct, frozen=True):
    """The full authorized row set served by :meth:`ResultHandleStore.fetch_rows`.

    ``rows`` is every row actually retrievable from the store — the reduce's
    normalized set, capped at spill time to ``stored_rows``. ``total_rows``
    is the full collection size the reducer saw; when ``stored_rows <
    total_rows`` the spill was capped and a query over ``rows`` covers only
    the stored subset (the ``result_query`` core labels that coverage so a
    capped aggregate is never presented as a whole-inventory total).
    """

    rows: list[dict[str, Any]]
    total_rows: int
    stored_rows: int


class SpilledDocument(msgspec.Struct, frozen=True):
    """What a ``path`` read resolves against, served by :meth:`ResultHandleStore.fetch_document`.

    For a document record this is the whole retained payload: one payload
    that had no single paginable collection (a detail object with several
    lists, a dict of scalars, or a scalar root), stored whole or not at all.
    For a rows record it is the dict of top-level fields the reducer left
    out of the inline summary (#3633).
    """

    document: Any


class SpillResult(msgspec.Struct, frozen=True):
    """What :meth:`ResultHandleStore.spill` kept.

    ``stored_rows`` is the number of rows retrievable via
    :meth:`ResultHandleStore.fetch_window`; ``0`` means nothing was stored.
    ``document_stored`` is ``True`` only when the omitted fields passed as
    *document* were kept beside the rows, so a ``path`` read on the handle
    works. The reducer uses it to advertise that read only when it works
    (#3633).
    """

    stored_rows: int
    document_stored: bool = False


class ResultRecordKindError(QueryContractError):
    """The handle holds the other record kind than the read asked for.

    A ``path`` read needs a document record; paging and relational queries
    need a rows record. Recoverable: the message names the read that works.
    """

    def __init__(self, handle_id: UUID, record_kind: str) -> None:
        self.handle_id = handle_id
        self.record_kind = record_kind
        if record_kind == _DOCUMENT_RECORD:
            hint = (
                "it holds one document, not rows. Read it with "
                '`query={"path": "<JSON Pointer>"}`, for example `"/name"`.'
            )
        else:
            hint = (
                "it holds rows and no fields left out of the inline summary. "
                "Page it with `offset` / `limit` or query it."
            )
        super().__init__(f"handle {handle_id} cannot be read this way: {hint}")


class _StoredPayload(msgspec.Struct):
    """Wire shape persisted under a spilled-handle key.

    ``record_kind`` and ``document`` are additive: a record written before
    they existed decodes as a rows record with no document, because msgspec
    fills missing fields from their defaults. On a rows record ``document``
    is ``None`` or the dict of top-level fields the reducer left out of the
    inline summary (#3633); on a document record it is the whole payload.
    """

    operator_sub: str
    op_id: str | None
    rows: list[dict[str, Any]]
    total_rows: int
    stored_rows: int
    created_at: str
    captured_rows: int = 0
    storage_coverage: str = "unknown"
    source_coverage: str = "unknown"
    metadata: dict[str, Any] = msgspec.field(default_factory=dict)
    record_kind: str = _ROWS_RECORD
    document: Any = None


def _without_document(
    baseline: _StoredPayload, *, handle_id: UUID, tenant_id: UUID, max_record_bytes: int
) -> tuple[_StoredPayload, int]:
    """Return *baseline* without its document, and the new encoded size (logged)."""
    _log.warning(
        "result_handle_omitted_fields_dropped",
        handle_id=str(handle_id),
        tenant_id=str(tenant_id),
        op_id=baseline.op_id,
        record_bytes=len(msgspec.json.encode(baseline)),
        max_record_bytes=max_record_bytes,
    )
    baseline = msgspec.structs.replace(baseline, document=None)
    return baseline, len(msgspec.json.encode(baseline))


def _select_record(
    baseline: _StoredPayload,
    candidates: list[dict[str, Any]],
    captured_rows: int,
    max_record_bytes: int,
    *,
    handle_id: UUID,
    tenant_id: UUID,
) -> tuple[_StoredPayload, list[dict[str, Any]]]:
    """Return the record base and the longest row prefix that fit the cap together.

    The omitted fields a rows record carries (#3633) must never cost the
    caller its rows. When they leave no room for even the first row --
    because they alone break the cap, or because they fit but the first row
    no longer does beside them -- they are dropped with a warning and the
    rows are selected again without them.
    """
    size = len(msgspec.json.encode(baseline))
    selected = _rows_within_cap(candidates, captured_rows, size, max_record_bytes)
    if not selected and baseline.document is not None:
        baseline, size = _without_document(
            baseline, handle_id=handle_id, tenant_id=tenant_id, max_record_bytes=max_record_bytes
        )
        selected = _rows_within_cap(candidates, captured_rows, size, max_record_bytes)
    return baseline, selected


def _rows_within_cap(
    candidates: list[dict[str, Any]], captured_rows: int, baseline_size: int, max_record_bytes: int
) -> list[dict[str, Any]]:
    """Return the longest prefix of *candidates* whose record stays under the cap.

    *baseline_size* is the encoded record with no rows. Each row adds its
    own bytes plus a separator; the running ``stored_rows`` digit count and
    the ``complete`` coverage label are charged as they grow, so the final
    encoded record is never a surprise.
    """
    selected: list[dict[str, Any]] = []
    size = baseline_size
    for row in candidates:
        row_size = len(msgspec.json.encode(row))
        candidate_size = size + row_size + (1 if selected else 0)
        count = len(selected) + 1
        complete = count == captured_rows
        candidate_size += len(str(count)) - 1 + (1 if complete else 0)
        if candidate_size > max_record_bytes:
            break
        selected.append(row)
        size += row_size + (1 if len(selected) > 1 else 0)
    return selected


class ResultHandleStore:
    """Persist + read back the full materialized rows of a reduced dispatch.

    A thin wrapper over two async Valkey clients: ``client`` is the shared
    broadcast client (legacy keys) and ``dedicated_client`` the result-cache
    client (``v2`` keys; defaults to ``client``). ``writer_enabled`` picks
    where :meth:`spill` and :meth:`spill_document` write. One instance is
    shared process-wide via :func:`get_result_handle_store`; the reducer
    spills into it at materialize time and the ``result_query`` MCP tool
    reads windows back out of it.
    """

    def __init__(
        self,
        client: redis.Redis,
        *,
        dedicated_client: redis.Redis | None = None,
        writer_enabled: bool = False,
    ) -> None:
        self._client = client
        self._dedicated = dedicated_client if dedicated_client is not None else client
        self._writer_enabled = writer_enabled

    def _write_target(self, tenant_id: UUID, handle_id: UUID) -> tuple[str, redis.Redis, str]:
        """Return ``(namespace, client, key)`` for a write, per the writer gate."""
        if self._writer_enabled:
            return "v2", self._dedicated, _v2_key(tenant_id, handle_id)
        return "legacy", self._client, _key(tenant_id, handle_id)

    async def _sample_memory(self) -> None:
        """Record the dedicated store's memory use. Fail-open, best effort."""
        try:
            info = await self._dedicated.info("memory")
            RESULT_CACHE_MEMORY_BYTES.labels(kind="used").set(int(info.get("used_memory", 0)))
            RESULT_CACHE_MEMORY_BYTES.labels(kind="max").set(int(info.get("maxmemory", 0)))
        except (redis.RedisError, OSError, TypeError, ValueError, AttributeError):
            return

    async def _after_write(self, namespace: str, *, admitted: bool) -> None:
        """Count a write attempt; sample store memory after a ``v2`` attempt."""
        outcome = "admitted" if admitted else "rejected"
        RESULT_CACHE_RECORDS_TOTAL.labels(outcome=outcome, namespace=namespace).inc()
        if namespace == "v2":
            await self._sample_memory()

    async def _get_record(self, tenant_id: UUID, handle_id: UUID) -> str | bytes | None:
        """Reader-first lookup: ``v2`` key on the dedicated client, then legacy.

        Both lookups use the caller's tenant, so the fallback never reads
        another tenant's record. A failing client counts as a miss for that
        namespace; ``None`` is returned only when both miss.
        """
        lookups = (
            ("v2", self._dedicated, _v2_key(tenant_id, handle_id)),
            ("legacy", self._client, _key(tenant_id, handle_id)),
        )
        for source, client, key in lookups:
            try:
                raw: str | bytes | None = await client.get(key)
            except (redis.RedisError, OSError) as exc:
                _log.warning(
                    "result_handle_fetch_failed",
                    handle_id=str(handle_id),
                    tenant_id=str(tenant_id),
                    namespace=source,
                    error=str(exc),
                )
                continue
            if raw is not None:
                RESULT_CACHE_READS_TOTAL.labels(source=source).inc()
                return raw
        RESULT_CACHE_READS_TOTAL.labels(source="miss").inc()
        return None

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
        max_record_bytes: int | None = None,
        metadata: dict[str, Any] | None = None,
        document: dict[str, Any] | None = None,
    ) -> SpillResult:
        """Persist a row-capped, byte-capped prefix of *rows* under the handle key.

        Returns a :class:`SpillResult` whose ``stored_rows`` is the positive
        count of rows retrievable via :meth:`fetch_window`, or ``0`` when the
        spill was skipped or failed (Valkey unreachable, serialization
        error, empty input). A zero count is non-fatal: the caller keeps the
        inline sample and leaves the handle's drill-in surface marked
        unavailable.

        *document* (#3633) carries the payload's top-level fields that did
        not fit the inline summary, so :meth:`fetch_document` can serve them
        back. It counts against the same record cap as the rows. When it
        leaves no room for even the first row it is dropped with a warning
        and the rows are stored as before, so a large sibling can never cost
        the caller its rows; ``document_stored`` tells the caller which
        happened.

        The TTL is enforced server-side by Valkey (``SET ... EX``); no
        sweeper is needed and a crashed process leaves no orphaned key
        past the TTL.
        """
        if not rows or ttl_seconds <= 0 or max_rows <= 0:
            return SpillResult(stored_rows=0)
        if max_record_bytes is None:
            max_record_bytes = get_settings().result_handle_max_record_bytes
        if max_record_bytes <= 0:
            return SpillResult(stored_rows=0)
        namespace, target, key = self._write_target(tenant_id, handle_id)
        created_at = datetime.now(UTC).isoformat()
        metadata = metadata or {}
        candidates = rows[:max_rows]
        captured_rows = len(rows)
        try:
            baseline = _StoredPayload(
                operator_sub=operator_sub,
                op_id=op_id,
                rows=[],
                total_rows=total_rows,
                stored_rows=0,
                created_at=created_at,
                captured_rows=captured_rows,
                storage_coverage="partial",
                metadata=metadata,
                document=document,
            )
            baseline, selected = _select_record(
                baseline,
                candidates,
                captured_rows,
                max_record_bytes,
                handle_id=handle_id,
                tenant_id=tenant_id,
            )
            if not selected:
                await self._after_write(namespace, admitted=False)
                return SpillResult(stored_rows=0)
            coverage = "complete" if len(selected) == captured_rows else "partial"
            payload = msgspec.structs.replace(
                baseline, rows=selected, stored_rows=len(selected), storage_coverage=coverage
            )
            encoded = msgspec.json.encode(payload)
            if len(encoded) > max_record_bytes:
                await self._after_write(namespace, admitted=False)
                return SpillResult(stored_rows=0)
            await target.set(key, encoded, ex=ttl_seconds)
        except (redis.RedisError, msgspec.EncodeError, TypeError, OSError) as exc:
            # Fail-open: a reduce must never fail because the spill
            # backend is unreachable or full. The inline sample still ships.
            _log.warning(
                "result_handle_spill_failed",
                handle_id=str(handle_id),
                tenant_id=str(tenant_id),
                op_id=op_id,
                namespace=namespace,
                error=str(exc),
            )
            await self._after_write(namespace, admitted=False)
            return SpillResult(stored_rows=0)
        await self._after_write(namespace, admitted=True)
        _log.info(
            "result_handle_spilled",
            handle_id=str(handle_id),
            tenant_id=str(tenant_id),
            op_id=op_id,
            stored_rows=len(selected),
            total_rows=total_rows,
            ttl_seconds=ttl_seconds,
            document_stored=payload.document is not None,
            namespace=namespace,
        )
        return SpillResult(stored_rows=len(selected), document_stored=payload.document is not None)

    async def spill_document(
        self,
        *,
        tenant_id: UUID,
        operator_sub: str,
        handle_id: UUID,
        op_id: str | None,
        document: Any,
        ttl_seconds: int,
        max_record_bytes: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Persist *document* whole under the handle key, as a document record.

        Returns ``True`` when the record was written. Returns ``False`` when
        the encoded record is over the per-record byte cap (a document is
        never cut, so it is kept whole or not at all), the TTL is not
        positive, or the store failed. Fail-open like :meth:`spill`.
        """
        if ttl_seconds <= 0:
            return False
        if max_record_bytes is None:
            max_record_bytes = get_settings().result_handle_max_record_bytes
        namespace, target, key = self._write_target(tenant_id, handle_id)
        record = _StoredPayload(
            operator_sub=operator_sub,
            op_id=op_id,
            rows=[],
            total_rows=0,
            stored_rows=0,
            created_at=datetime.now(UTC).isoformat(),
            storage_coverage="complete",
            source_coverage="complete",
            metadata=metadata or {},
            record_kind=_DOCUMENT_RECORD,
            document=document,
        )
        try:
            encoded = msgspec.json.encode(record)
            if len(encoded) > max_record_bytes:
                _log.warning(
                    "result_handle_document_over_record_cap",
                    handle_id=str(handle_id),
                    tenant_id=str(tenant_id),
                    op_id=op_id,
                    record_bytes=len(encoded),
                    max_record_bytes=max_record_bytes,
                )
                await self._after_write(namespace, admitted=False)
                return False
            await target.set(key, encoded, ex=ttl_seconds)
        except (redis.RedisError, msgspec.EncodeError, TypeError, OSError) as exc:
            _log.warning(
                "result_handle_spill_failed",
                handle_id=str(handle_id),
                tenant_id=str(tenant_id),
                op_id=op_id,
                namespace=namespace,
                error=str(exc),
            )
            await self._after_write(namespace, admitted=False)
            return False
        await self._after_write(namespace, admitted=True)
        _log.info(
            "result_handle_document_spilled",
            handle_id=str(handle_id),
            tenant_id=str(tenant_id),
            op_id=op_id,
            record_bytes=len(encoded),
            ttl_seconds=ttl_seconds,
            namespace=namespace,
        )
        return True

    async def _load(
        self,
        *,
        tenant_id: UUID,
        operator_sub: str,
        handle_id: UUID,
    ) -> _StoredPayload | None:
        """Read and decode the record for *handle_id*, or ``None`` on a miss.

        A miss covers an unknown or expired key, another operator's record,
        an unreachable store, and an undecodable value. They are deliberately
        the same answer so the store leaks no existence signal across the
        operator boundary (the #304 isolation contract).
        """
        raw = await self._get_record(tenant_id, handle_id)
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
            return None
        return payload

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

        Raises :class:`ResultRecordKindError` when the handle holds a
        document record, which has no rows to page.
        """
        payload = await self._load(
            tenant_id=tenant_id, operator_sub=operator_sub, handle_id=handle_id
        )
        if payload is None:
            return None
        if payload.record_kind == _DOCUMENT_RECORD:
            raise ResultRecordKindError(handle_id, _DOCUMENT_RECORD)
        start = max(offset, 0)
        window = payload.rows[start : start + limit] if limit > 0 else []
        return SpilledWindow(
            rows=window,
            total_rows=payload.total_rows,
            stored_rows=payload.stored_rows,
            truncated=payload.stored_rows < payload.total_rows,
        )

    async def fetch_rows(
        self,
        *,
        tenant_id: UUID,
        operator_sub: str,
        handle_id: UUID,
    ) -> SpilledRowSet | None:
        """Return the **full** authorized row set of a spilled handle.

        The sibling of :meth:`fetch_window` for the ``result_query`` query
        surface (#3366): where ``fetch_window`` serves an
        ``[offset:offset+limit]`` slice for paging, this returns every stored
        row so the core can register them in a hardened DuckDB engine and run
        one bounded, compiled ``SELECT`` over the set.

        Reuses ``fetch_window``'s isolation and non-disclosure model exactly:
        the tenant scopes the key, the stored ``operator_sub`` is checked so
        another operator in the same tenant gets the same miss (``None``) as a
        stranger, and an unknown / expired / cross-operator handle or an
        unreachable store all return ``None`` — the four cases the read tools
        surface as one typed "handle not found or expired", leaking no
        existence signal across the operator boundary.

        Raises :class:`ResultRecordKindError` when the handle holds a
        document record: a relational query needs rows.
        """
        payload = await self._load(
            tenant_id=tenant_id, operator_sub=operator_sub, handle_id=handle_id
        )
        if payload is None:
            return None
        if payload.record_kind == _DOCUMENT_RECORD:
            raise ResultRecordKindError(handle_id, _DOCUMENT_RECORD)
        return SpilledRowSet(
            rows=payload.rows,
            total_rows=payload.total_rows,
            stored_rows=payload.stored_rows,
        )

    async def fetch_document(
        self,
        *,
        tenant_id: UUID,
        operator_sub: str,
        handle_id: UUID,
    ) -> SpilledDocument | None:
        """Return the document a ``path`` read resolves against.

        For a document record that is the whole retained payload. For a rows
        record it is the dict of top-level fields the reducer left out of the
        inline summary (#3633), when the spill kept one. Same isolation and
        ``None``-on-miss rules as :meth:`fetch_rows`. Raises
        :class:`ResultRecordKindError` when the handle holds rows and nothing
        beside them: a ``path`` read then has nothing to resolve against.
        """
        payload = await self._load(
            tenant_id=tenant_id, operator_sub=operator_sub, handle_id=handle_id
        )
        if payload is None:
            return None
        if payload.record_kind != _DOCUMENT_RECORD and payload.document is None:
            raise ResultRecordKindError(handle_id, _ROWS_RECORD)
        return SpilledDocument(document=payload.document)


#: Process-wide singleton. Lazily built from the broadcast and result-cache
#: Valkey clients the first time the reducer spills or the read tool fetches.
_STORE: ResultHandleStore | None = None


def get_result_handle_store() -> ResultHandleStore:
    """Return the process-wide :class:`ResultHandleStore`.

    Keeps the broadcast fast client
    (:func:`~meho_backplane.broadcast.client.get_broadcast_client`) for
    legacy reads (and legacy writes while the writer gate is off) and builds
    a dedicated client from ``result_cache_redis_url`` with the same
    fail-fast timeouts and ``decode_responses=True`` posture. When that URL
    and password equal the broadcast ones (the unset-env fallback), the
    broadcast client is reused instead of opening a second pool.
    """
    global _STORE
    if _STORE is None:
        # Imported lazily so importing this module doesn't pull the
        # broadcast client (and its settings read) at import time.
        from meho_backplane.broadcast.client import get_broadcast_client

        settings = get_settings()
        shared = get_broadcast_client()
        same_store = (
            settings.result_cache_redis_url == settings.broadcast_redis_url
            and settings.result_cache_redis_password == settings.broadcast_redis_password
        )
        dedicated = (
            shared
            if same_store
            else redis.from_url(
                settings.result_cache_redis_url,
                password=settings.result_cache_redis_password,
                decode_responses=True,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            )
        )
        _STORE = ResultHandleStore(
            shared,
            dedicated_client=dedicated,
            writer_enabled=settings.result_cache_writer_enabled,
        )
    return _STORE


async def dispose_result_handle_store() -> None:
    """Close the dedicated result-cache client at shutdown.

    Only a client the store opened itself is closed; the shared broadcast
    client is closed by its own dispose helper. Idempotent.
    """
    global _STORE
    store = _STORE
    _STORE = None
    if store is None or store._dedicated is store._client:
        return
    await store._dedicated.aclose()


def reset_result_handle_store_for_testing() -> None:
    """Clear the cached store. Test-only.

    Tests inject a fake client by constructing :class:`ResultHandleStore`
    directly; this resets the lazy singleton so a later production-path
    call rebuilds it from the (possibly re-pointed) broadcast client.
    """
    global _STORE
    _STORE = None
