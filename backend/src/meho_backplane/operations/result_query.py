# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Transport-neutral core for the ``result_query`` JSONFlux read-back surface.

The windowed spill read that both operator fronts share (#3179). When
``call_operation`` reduces a large set-shaped response, the caller gets an
inline sample plus a
:class:`~meho_backplane.connectors.schemas.ResultHandle`; the full set is
spilled to the
:class:`~meho_backplane.connectors.result_handle_store.ResultHandleStore`
(Valkey) keyed by ``(tenant_id, handle_id)`` with a bounded TTL. This module
owns the read over that store — the same logic the MCP ``result_query`` tool
(:mod:`meho_backplane.mcp.tools.result_query`) and the REST route
``POST /api/v1/operations/result-query``
(:mod:`meho_backplane.api.v1.operations`) both wrap, so the two surfaces
cannot drift on scoping or not-found semantics.

Isolation
=========

The tenant is taken from the operator's authenticated identity (each
transport resolves it from the JWT), **never** from the arguments — a
cross-tenant probe cannot read another tenant's handle. The store
additionally checks the spilling operator's ``sub`` so another operator in
the same tenant gets the same not-found miss as a stranger, leaking no
existence signal across the operator boundary.

Not-found is recoverable
========================

A handle that is unknown, expired (TTL elapsed), or belongs to a different
operator raises :class:`ResultHandleNotFoundError`. Each transport maps it to
its own recoverable-error shape (MCP: a typed ``-32602`` with
``data.reason=handle_not_found``; REST: a ``404`` with a structured
``detail``). The caller learns the handle is gone (re-run the operation)
rather than getting an opaque internal error.

Bounded work
============

Every read (paging, query, and a ``path`` read) goes through
:func:`_run_admitted_read`. It passes
:func:`~meho_backplane.operations.result_read_admission.admit_result_read`
before it touches the store, so a read that is over the rate cap or finds
every worker slot busy is refused with a recoverable
:class:`ResultReadRejectedError` and no Valkey ``GET`` is made. The ``GET``
then runs on the event loop and everything after it — decoding the record,
the operator check, the CPU-heavy shaping (for a query: catalog, compile,
register, ``SELECT``, encode), and the wire-size check on the finished
response — runs as one job on the dedicated worker pool. One deadline
(``result_query_timeout_seconds``) covers the ``GET`` and the job, so no
step can hold the event loop or a worker slot past the budget without the
caller hearing about it.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from typing import Any
from uuid import UUID

import duckdb
import msgspec
import pyarrow as pa

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.result_handle_store import (
    ResultHandleStore,
    SpilledRowSet,
    get_result_handle_store,
)
from meho_backplane.jsonflux.query.contract import (
    RESULT_TABLE,
    ROW_ORDINAL,
    CompiledQuery,
    QueryContractError,
    ResultQuerySpec,
    compile_query,
)
from meho_backplane.jsonflux.query.engine import QueryEngine
from meho_backplane.jsonflux.query.json_pointer import (
    JsonPointerError,
    json_type,
    parse_json_pointer,
    resolve_json_pointer,
)
from meho_backplane.jsonflux.query.result_catalog import (
    ResultFieldCatalog,
    _build_catalog_from_admitted,
)
from meho_backplane.operations.delivery_budget import measure_delivered_wire_bytes
from meho_backplane.operations.result_read_admission import (
    ResultReadCapacityError,
    ResultReadRateLimitError,
    ResultReadRejectedError,
    admit_result_read,
    run_on_result_read_executor,
)
from meho_backplane.settings import get_settings

__all__ = [
    "MAX_LIMIT",
    "QueryContractError",
    "ResultHandleNotFoundError",
    "ResultQueryOutputTooLargeError",
    "ResultQuerySpec",
    "ResultQueryTimeoutError",
    "ResultReadCapacityError",
    "ResultReadRateLimitError",
    "ResultReadRejectedError",
    "read_result_window",
    "run_result_query",
]

#: Upper bound on a single read-back page: one call returns at most this many
#: rows so a reader can't pull an unbounded slice in one request. Matches the
#: ``search_operations`` / ``list_targets`` max-page convention; both the MCP
#: tool's ``inputSchema`` and the REST body's Pydantic bound import this so
#: the two surfaces share one ceiling.
MAX_LIMIT = 500


class ResultHandleNotFoundError(Exception):
    """A handle is not readable: unknown, expired, or another operator's.

    Transport-neutral: the MCP tool maps it to a typed ``-32602``
    (``data.reason=handle_not_found``) and the REST route to a ``404`` with
    a structured ``detail``. Carries the ``handle_id`` so each mapper can
    echo it back in its own error shape.
    """

    def __init__(self, handle_id: UUID) -> None:
        self.handle_id = handle_id
        super().__init__(
            f"handle {handle_id} is not readable: it does not exist, has "
            "expired, or belongs to a different operator. Re-run the "
            "operation to get a fresh handle."
        )


async def read_result_window(
    operator: Operator,
    handle_id: UUID,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Return the ``[offset : offset+limit]`` window of a spilled handle.

    The tenant + principal come from *operator* (JWT-resolved), never from
    the request — the arguments carry no tenant, by design. *offset* and
    *limit* are validated by each transport's own edge (the MCP
    ``inputSchema``; the REST body's Pydantic bounds), so this core takes
    them as given.

    Raises :class:`ResultHandleNotFoundError` when the operator has no tenant
    (it can never own a spilled handle — the reducer keys the spill on
    ``tenant_id``) or when the store returns no window (unknown, expired, or
    cross-operator handle). The two causes are deliberately indistinguishable
    so the store leaks no existence signal.

    Raises :class:`ResultQueryOutputTooLargeError` (#3387) when the window
    serializes past ``result_query_max_output_bytes`` — the same ceiling the
    query path applies, so no agent receives an over-budget page — with a
    paging-specific remediation (read a smaller ``limit``, since paging has no
    ``select`` / ``filter``).

    Raises :class:`ResultReadRejectedError` (rate or capacity) before the
    store is read when the read is not admitted, and
    :class:`ResultQueryTimeoutError` when the fetch and decode run past the
    time budget (#3640).
    """
    store = get_result_handle_store()
    max_bytes = get_settings().result_query_max_output_bytes

    def work(raw: bytes, guard: _WorkerGuard) -> dict[str, Any]:
        window = store.decode_window(
            raw, operator_sub=operator.sub, handle_id=handle_id, offset=offset, limit=limit
        )
        if window is None:
            raise ResultHandleNotFoundError(handle_id)
        guard.check()
        # Byte ceiling shared with the query path (#3387): even a window
        # within the row cap can serialize past the output budget when rows
        # are wide or deeply nested, and no agent may receive an over-budget
        # page (CLAUDE.md postulate 6). Encoding is for the size check only —
        # the window rows are returned verbatim, so an in-budget read is
        # byte-for-byte unchanged.
        output_bytes = len(msgspec.json.encode(window.rows))
        if output_bytes > max_bytes:
            raise ResultQueryOutputTooLargeError(
                handle_id,
                output_bytes,
                max_bytes,
                remediation=ResultQueryOutputTooLargeError.PAGING_REMEDIATION,
            )
        payload = {
            "handle_id": str(handle_id),
            "rows": window.rows,
            "offset": offset,
            "limit": limit,
            "returned_rows": len(window.rows),
            "total_rows": window.total_rows,
            "stored_rows": window.stored_rows,
            "truncated": window.truncated,
        }
        _check_wire_ceiling(payload, handle_id, ResultQueryOutputTooLargeError.PAGING_REMEDIATION)
        return payload

    return await _run_admitted_read(
        operator,
        handle_id,
        store,
        work,
        timeout_remediation=ResultQueryTimeoutError.PAGING_REMEDIATION,
    )


class ResultQueryTimeoutError(Exception):
    """A result read exceeded its wall-time budget.

    The ``result_query_timeout_seconds`` budget covers the whole read on
    every path (paging, query, ``path``): the store fetch, the decode, the
    shaping steps on the worker thread (for a query: catalog, compile,
    ``SELECT``, encode), and the wire-size check on the finished response.
    DuckDB has no native Python query timeout, so on
    expiry the core calls ``conn.interrupt()``; a worker that is still busy
    keeps its slot until it finishes, but the caller gets this error at
    once. Recoverable: the remediation names the way out for the caller's
    surface. Each transport maps it to its own recoverable-error shape.
    """

    #: Remediation for the query surface (it has ``select`` / ``filter``).
    QUERY_REMEDIATION = (
        "Narrow the query (a tighter filter, fewer group_by keys, or a lower limit) and retry."
    )
    #: Remediation for the paging surface (no projection/filter — only ``limit``).
    PAGING_REMEDIATION = "Read a smaller window: lower `limit` and retry."

    def __init__(
        self, handle_id: UUID, timeout_seconds: int, remediation: str | None = None
    ) -> None:
        self.handle_id = handle_id
        self.timeout_seconds = timeout_seconds
        self.remediation = remediation or self.QUERY_REMEDIATION
        super().__init__(
            f"read of handle {handle_id} exceeded the {timeout_seconds}s time "
            f"budget and was aborted. {self.remediation}"
        )


class ResultQueryOutputTooLargeError(Exception):
    """A handle read exceeded the serialized-output byte budget.

    Applied **after** the row cap: even ≤ ``MAX_LIMIT`` rows can overflow the
    ``result_query_max_output_bytes`` budget when they are wide or deeply
    nested. Raised from both read paths that share the ceiling — the query
    core (:func:`run_result_query`) and the paging core
    (:func:`read_result_window`, #3387) — so no agent ever receives an
    over-budget payload. Recoverable, and the remediation is tailored to the
    caller's surface: the query path can project fewer columns or filter
    harder; the paging path has neither and can only read a smaller window.
    Each transport maps this to its own recoverable shape with
    ``reason="output_too_large"``.
    """

    #: Remediation for the query surface (it has ``select`` / ``filter``).
    QUERY_REMEDIATION = (
        "Narrow the result: project fewer columns with `select`, add a `filter`, or lower `limit`."
    )
    #: Remediation for the paging surface (no projection/filter — only ``limit``).
    PAGING_REMEDIATION = "Read a smaller window: lower `limit` and retry."

    def __init__(
        self,
        handle_id: UUID,
        output_bytes: int,
        max_bytes: int,
        remediation: str | None = None,
        *,
        wire: bool = False,
    ) -> None:
        self.handle_id = handle_id
        self.output_bytes = output_bytes
        self.max_bytes = max_bytes
        self.remediation = remediation or self.QUERY_REMEDIATION
        if wire:
            detail = (
                f"the response for handle {handle_id} is {output_bytes} bytes on the "
                f"wire, over the {max_bytes}-byte delivery limit, so it was not "
                "delivered (delivery unavailable)."
            )
        else:
            detail = (
                f"result for handle {handle_id} is {output_bytes} serialized bytes, "
                f"over the {max_bytes}-byte output budget."
            )
        super().__init__(f"{detail} {self.remediation}")


def _check_wire_ceiling(payload: dict[str, Any], handle_id: UUID, remediation: str) -> None:
    """Refuse a read-back response that is too large on the wire (#3641).

    The terminal check, after the pre-encode row-bytes check: it measures
    the whole response as the MCP ``tools/call`` wire carries it (text block
    plus ``structuredContent``) against ``result_query_response_wire_max_bytes``.
    The REST twin shares the bound; it sends the payload once, so it is
    always smaller than this measurement.
    """
    max_bytes = get_settings().result_query_response_wire_max_bytes
    size = measure_delivered_wire_bytes(payload)
    if size > max_bytes:
        raise ResultQueryOutputTooLargeError(
            handle_id, size, max_bytes, remediation=remediation, wire=True
        )


#: Arrow type for each catalog scalar family in the explicit projection.
_FAMILY_ARROW_TYPES: dict[str, pa.DataType] = {
    "boolean": pa.bool_(),
    "integer": pa.int64(),
    "number": pa.float64(),
    "string": pa.string(),
}


def _project_rows(rows: list[dict[str, Any]], catalog: ResultFieldCatalog) -> pa.Table:
    """Build the relational table the compiled ``SELECT`` runs over.

    One typed column per queryable catalog field, named by its generated
    internal identifier, plus the hidden row ordinal. Fields that cannot be
    queried are left out: they are only ever returned raw, by ordinal.
    """
    columns: dict[str, pa.Array] = {ROW_ORDINAL: pa.array(range(len(rows)), type=pa.int64())}
    for entry in catalog.fields:
        if entry.queryable and entry.scalar_family is not None:
            arrow_type = _FAMILY_ARROW_TYPES[entry.scalar_family]
            columns[entry.column] = pa.array([row.get(entry.name) for row in rows], type=arrow_type)
    return pa.table(columns)


def _execute_sync(engine: QueryEngine, compiled: CompiledQuery) -> tuple[list[str], list[Any]]:
    """Run the compiled SELECT on the engine's connection (worker thread).

    Returns the result column names and the raw fetched rows. Runs on a
    worker thread so :func:`_run_admitted_read` can call
    ``engine.conn.interrupt()`` from the event loop on timeout.
    """
    cursor = engine.conn.execute(compiled.sql, compiled.params)
    columns = [desc[0] for desc in cursor.description]
    return columns, cursor.fetchall()


class _QueryAbandonedError(Exception):
    """The caller gave up; the worker stops at its next step."""


class _WorkerGuard:
    """Lets the event loop stop a worker job safely.

    The worker calls :meth:`check` between its steps and opens and closes
    its engine (query mode only) through the guard; the event loop may call
    :meth:`abandon` at any time. A lock keeps ``interrupt()`` and
    ``close()`` from running at the same time, and an abandoned guard stops
    the worker before its next step.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engine: QueryEngine | None = None
        self._abandoned = False

    def check(self) -> None:
        if self._abandoned:
            raise _QueryAbandonedError

    def open(self) -> QueryEngine:
        engine = QueryEngine()
        with self._lock:
            if self._abandoned:
                engine.close()
                raise _QueryAbandonedError
            self._engine = engine
        return engine

    def close(self) -> None:
        with self._lock:
            engine, self._engine = self._engine, None
        if engine is not None:
            engine.close()

    def abandon(self) -> None:
        with self._lock:
            self._abandoned = True
            if self._engine is not None:
                with contextlib.suppress(duckdb.Error):
                    self._engine.conn.interrupt()


def _query_pipeline_sync(
    guard: _WorkerGuard,
    rows: list[dict[str, Any]],
    spec: ResultQuerySpec,
    max_output_rows: int,
) -> tuple[list[dict[str, Any]], bool, int, bytes]:
    """Catalog, compile, register, SELECT, rehydrate, encode (worker thread).

    The field catalog is built from every stored row (the rows passed the
    reducer's admission guard before they were spilled). The result is
    capped to ``max_output_rows``; ``truncated`` is ``True`` when the
    underlying result had more rows. Output rows come from the compiled
    output plan: flat queries copy the raw stored rows. Returns the rows,
    the flag, the effective limit, and the rows encoded as JSON bytes.
    """
    catalog = _build_catalog_from_admitted(rows)
    compiled = compile_query(spec, catalog, max_limit=max_output_rows)
    table = _project_rows(rows, catalog)
    engine = guard.open()
    try:
        engine.conn.register(RESULT_TABLE, table)
        _, fetched = _execute_sync(engine, compiled)
    finally:
        guard.close()
    guard.check()

    truncated = len(fetched) > compiled.effective_limit
    capped = fetched[: compiled.effective_limit]
    result_rows = compiled.output_plan.rehydrate(capped, raw_rows=rows)
    guard.check()
    return result_rows, truncated, compiled.effective_limit, msgspec.json.encode(result_rows)


async def _run_admitted_read[T](
    operator: Operator,
    handle_id: UUID,
    store: ResultHandleStore,
    work: Callable[[bytes, _WorkerGuard], T],
    *,
    timeout_remediation: str | None = None,
) -> T:
    """Admit, fetch the raw record, and run *work* on the pool — one deadline.

    The order is fixed: admission first (no store access on a refusal),
    then the Valkey ``GET`` on the event loop, then ``work(raw, guard)`` as
    one job on the dedicated worker pool. *work* decodes the record (the
    operator check included), shapes the answer, and checks its wire size;
    it calls
    ``guard.check()`` between its steps so an abandoned job stops early.
    ``result_query_timeout_seconds`` covers the ``GET`` and the job
    together. On expiry — or when the caller is cancelled — the job is
    abandoned (a running DuckDB query is interrupted), the caller returns
    at once, and the worker keeps the lease until it finishes.

    Raises :class:`ResultHandleNotFoundError` when the operator has no
    tenant or the store holds no record, :class:`ResultReadRejectedError`
    from admission, :class:`ResultQueryTimeoutError` on expiry, and
    whatever *work* raises.
    """
    if operator.tenant_id is None:
        raise ResultHandleNotFoundError(handle_id)
    timeout_seconds = get_settings().result_query_timeout_seconds
    guard = _WorkerGuard()
    async with admit_result_read(operator) as lease:
        try:
            async with asyncio.timeout(timeout_seconds):
                raw = await store.fetch_record(tenant_id=operator.tenant_id, handle_id=handle_id)
                if raw is None:
                    raise ResultHandleNotFoundError(handle_id)
                return await run_on_result_read_executor(
                    lease, work, raw, guard, on_abandon=guard.abandon
                )
        except TimeoutError as exc:
            raise ResultQueryTimeoutError(
                handle_id, timeout_seconds, remediation=timeout_remediation
            ) from exc


async def run_result_query(
    operator: Operator,
    handle_id: UUID,
    spec: ResultQuerySpec,
    *,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Run a bounded, validated structured query over a spilled handle (#3366).

    Loads the full authorized row set (same tenant + ``operator_sub``
    isolation and None-on-miss non-disclosure as the paging read), registers
    it in a fresh hardened :class:`QueryEngine`, compiles *spec* into one
    parameterized read-only ``SELECT`` (:func:`compile_query`), and runs it
    under the output-row / output-byte / wall-time bounds from settings.

    The result carries coverage metadata: when the spill was capped
    (``stored_rows < total_rows``) the query ran over the stored subset only,
    so it is labelled ``coverage="partial"`` and an aggregate over it is
    never presented as a whole-inventory total.

    Raises :class:`ResultHandleNotFoundError` (unknown / expired /
    cross-operator handle, or no tenant), :class:`ResultReadRejectedError`
    (rate or capacity, raised before the store is read),
    :class:`QueryContractError` (a field-vs-schema or value-shape violation),
    :class:`ResultQueryTimeoutError` (time budget exceeded), or
    :class:`ResultQueryOutputTooLargeError` (byte budget exceeded). Each
    transport maps these to its own recoverable-error shape.

    A spec with ``path`` is a document read, not a query: it branches to
    :func:`_read_document_path`, which uses *offset* / *limit* to page an
    array value. *offset* / *limit* are ignored for a relational query.
    """
    if spec.path is not None:
        return await _read_document_path(operator, handle_id, spec.path, offset, limit)

    store = get_result_handle_store()
    settings = get_settings()
    max_output_rows = settings.result_query_max_output_rows
    max_bytes = settings.result_query_max_output_bytes

    def work(raw: bytes, guard: _WorkerGuard) -> dict[str, Any]:
        row_set = store.decode_rows(raw, operator_sub=operator.sub, handle_id=handle_id)
        if row_set is None:
            raise ResultHandleNotFoundError(handle_id)
        guard.check()
        _, truncated, effective_limit, encoded_rows = _query_pipeline_sync(
            guard, row_set.rows, spec, max_output_rows
        )
        # Byte cap applied AFTER the row cap: wide/nested rows can overflow
        # the token budget even under the row ceiling. The encode also
        # normalizes any non-JSON-native value (datetime, Decimal) to a
        # JSON-safe form for the transport layer, in one pass.
        if len(encoded_rows) > max_bytes:
            raise ResultQueryOutputTooLargeError(handle_id, len(encoded_rows), max_bytes)
        json_safe_rows: list[dict[str, Any]] = msgspec.json.decode(encoded_rows)
        guard.check()
        payload = _query_payload(handle_id, row_set, json_safe_rows, truncated, effective_limit)
        # The wire check encodes the whole response again, so it is worker
        # work under the same deadline, like the paging and ``path`` reads.
        _check_wire_ceiling(payload, handle_id, ResultQueryOutputTooLargeError.QUERY_REMEDIATION)
        return payload

    return await _run_admitted_read(operator, handle_id, store, work)


def _query_payload(
    handle_id: UUID,
    row_set: SpilledRowSet,
    rows: list[dict[str, Any]],
    truncated: bool,
    effective_limit: int,
) -> dict[str, Any]:
    """Shape the query response with its coverage metadata (worker thread)."""
    partial = row_set.stored_rows < row_set.total_rows
    coverage_note = (
        (
            f"This query ran over the {row_set.stored_rows} stored rows only; the "
            f"full result had {row_set.total_rows} rows (the spill was capped). "
            "Counts and aggregates cover the stored subset, not the whole inventory."
        )
        if partial
        else None
    )
    return {
        "handle_id": str(handle_id),
        "rows": rows,
        "offset": 0,
        "limit": effective_limit,
        "returned_rows": len(rows),
        "total_rows": row_set.total_rows,
        "stored_rows": row_set.stored_rows,
        "truncated": truncated,
        "coverage": "partial" if partial else "complete",
        "coverage_note": coverage_note,
    }


#: Remediation for an over-budget or timed-out ``path`` read.
_PATH_REMEDIATION = (
    "Point `path` at a smaller part of the document, or page an array value with a smaller `limit`."
)


async def _read_document_path(
    operator: Operator,
    handle_id: UUID,
    path: str,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Resolve the RFC 6901 *path* against a handle's document (#3633).

    For a document handle that is the whole retained payload. For a rows
    handle it is the dict of top-level fields the reducer left out of the
    inline summary (``omitted_scalar_count``), so ``/stderr`` reads a value
    that was too big to ride inline. An array value pages: ``rows`` holds
    ``[offset : offset+limit]`` of its items, a non-object item wrapped as
    ``{"value": item}`` (the same shape the reducer gives a list of scalars).
    Any other value comes back whole in ``value``. A pointer that does not
    resolve, or a rows handle with nothing kept beside its rows, is a
    :class:`QueryContractError`. The output byte budget is the same as for
    paging and queries, and so are admission and the deadline (#3640).
    """
    try:
        tokens = parse_json_pointer(path)
    except JsonPointerError as exc:  # already checked by the spec; kept for direct callers
        raise QueryContractError(str(exc)) from exc
    store = get_result_handle_store()
    max_bytes = get_settings().result_query_max_output_bytes

    def work(raw: bytes, guard: _WorkerGuard) -> dict[str, Any]:
        stored = store.decode_document(raw, operator_sub=operator.sub, handle_id=handle_id)
        if stored is None:
            raise ResultHandleNotFoundError(handle_id)
        try:
            node = resolve_json_pointer(stored.document, tokens)
        except JsonPointerError as exc:
            raise QueryContractError(str(exc)) from exc
        guard.check()

        result: dict[str, Any] = {
            "handle_id": str(handle_id),
            "path": path,
            "value_type": json_type(node),
            "offset": offset,
            "limit": limit,
            "truncated": False,
            "coverage": "complete",
            "coverage_note": None,
        }
        if isinstance(node, list):
            window = node[offset : offset + limit]
            rows = [item if isinstance(item, dict) else {"value": item} for item in window]
            result.update(
                rows=rows, returned_rows=len(rows), total_rows=len(node), stored_rows=len(node)
            )
            budgeted: Any = rows
        else:
            result.update(rows=[], value=node, returned_rows=1, total_rows=1, stored_rows=1)
            budgeted = node

        output_bytes = len(msgspec.json.encode(budgeted))
        if output_bytes > max_bytes:
            raise ResultQueryOutputTooLargeError(
                handle_id, output_bytes, max_bytes, remediation=_PATH_REMEDIATION
            )
        _check_wire_ceiling(result, handle_id, _PATH_REMEDIATION)
        return result

    return await _run_admitted_read(
        operator, handle_id, store, work, timeout_remediation=_PATH_REMEDIATION
    )
