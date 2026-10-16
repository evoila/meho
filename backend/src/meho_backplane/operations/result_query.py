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

Both cores pass :func:`~meho_backplane.operations.result_read_admission.admit_result_read`
before they touch the store, so a read that is over the rate cap or finds
every worker slot busy is refused with a recoverable
:class:`ResultReadRejectedError` and no Valkey ``GET`` is made. Query mode
then runs the CPU-heavy part (catalog, compile, register, ``SELECT``,
encode) on the dedicated worker pool, under one deadline that also covers
the store fetch.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any
from uuid import UUID

import duckdb
import msgspec
import pyarrow as pa

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.result_handle_store import get_result_handle_store
from meho_backplane.jsonflux.query.contract import (
    RESULT_TABLE,
    ROW_ORDINAL,
    CompiledQuery,
    QueryContractError,
    ResultQuerySpec,
    compile_query,
)
from meho_backplane.jsonflux.query.engine import QueryEngine
from meho_backplane.jsonflux.query.result_catalog import (
    ResultFieldCatalog,
    _build_catalog_from_admitted,
)
from meho_backplane.operations.result_read_admission import (
    ResultReadCapacityError,
    ResultReadLease,
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
    store is read when the read is not admitted.
    """
    if operator.tenant_id is None:
        raise ResultHandleNotFoundError(handle_id)

    async with admit_result_read(operator):
        window = await get_result_handle_store().fetch_window(
            tenant_id=operator.tenant_id,
            operator_sub=operator.sub,
            handle_id=handle_id,
            offset=offset,
            limit=limit,
        )
    if window is None:
        raise ResultHandleNotFoundError(handle_id)

    # Byte ceiling shared with the query path (#3387): even a window within the
    # row cap can serialize past the output budget when rows are wide or deeply
    # nested, and no agent may receive an over-budget page (CLAUDE.md postulate
    # 6). Encoding is for the size check only — the window rows are returned
    # verbatim, so an in-budget read is byte-for-byte unchanged.
    max_bytes = get_settings().result_query_max_output_bytes
    encoded_rows = msgspec.json.encode(window.rows)
    if len(encoded_rows) > max_bytes:
        raise ResultQueryOutputTooLargeError(
            handle_id,
            len(encoded_rows),
            max_bytes,
            remediation=ResultQueryOutputTooLargeError.PAGING_REMEDIATION,
        )

    return {
        "handle_id": str(handle_id),
        "rows": window.rows,
        "offset": offset,
        "limit": limit,
        "returned_rows": len(window.rows),
        "total_rows": window.total_rows,
        "stored_rows": window.stored_rows,
        "truncated": window.truncated,
    }


class ResultQueryTimeoutError(Exception):
    """A ``result_query`` read exceeded its wall-time budget.

    The ``result_query_timeout_seconds`` budget covers the whole read: the
    store fetch, and the catalog, compile, ``SELECT``, and encode steps on
    the worker thread. DuckDB has no native Python query timeout, so on
    expiry the core calls ``conn.interrupt()``; a worker that is still busy
    keeps its slot until it finishes, but the caller gets this error at
    once. Recoverable: the
    caller narrows the query (a tighter ``filter``, fewer ``group_by`` keys,
    a lower ``limit``) and retries. Each transport maps it to its own
    recoverable-error shape.
    """

    def __init__(self, handle_id: UUID, timeout_seconds: int) -> None:
        self.handle_id = handle_id
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"query over handle {handle_id} exceeded the {timeout_seconds}s time "
            "budget and was aborted. Narrow the query (a tighter filter, fewer "
            "group_by keys, or a lower limit) and retry."
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
    Each transport maps it to its own recoverable shape with
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
    ) -> None:
        self.handle_id = handle_id
        self.output_bytes = output_bytes
        self.max_bytes = max_bytes
        self.remediation = remediation or self.QUERY_REMEDIATION
        super().__init__(
            f"result for handle {handle_id} is {output_bytes} serialized bytes, "
            f"over the {max_bytes}-byte output budget. {self.remediation}"
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
    worker thread so :func:`run_result_query` can call
    ``engine.conn.interrupt()`` from the event loop on timeout.
    """
    cursor = engine.conn.execute(compiled.sql, compiled.params)
    columns = [desc[0] for desc in cursor.description]
    return columns, cursor.fetchall()


class _QueryAbandonedError(Exception):
    """The caller gave up; the worker stops at its next step."""


class _EngineGuard:
    """Lets the event loop interrupt a worker's engine safely.

    The worker opens and closes the engine; the event loop may call
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
    guard: _EngineGuard,
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


async def _run_compiled_query(
    rows: list[dict[str, Any]],
    spec: ResultQuerySpec,
    handle_id: UUID,
    *,
    max_output_rows: int,
    timeout_seconds: int,
    lease: ResultReadLease | None = None,
) -> tuple[list[dict[str, Any]], bool, int, bytes]:
    """Run :func:`_query_pipeline_sync` on the dedicated pool, time-bounded.

    The whole pipeline runs on a worker thread so the event loop stays
    free. On timeout (or when the caller is cancelled) the DuckDB query is
    interrupted and the caller returns at once; the worker keeps *lease*
    until it finishes. Raises :class:`ResultQueryTimeoutError` on timeout.
    """
    guard = _EngineGuard()
    try:
        async with asyncio.timeout(timeout_seconds):
            return await run_on_result_read_executor(
                lease,
                _query_pipeline_sync,
                guard,
                rows,
                spec,
                max_output_rows,
                on_abandon=guard.abandon,
            )
    except TimeoutError as exc:
        raise ResultQueryTimeoutError(handle_id, timeout_seconds) from exc


async def run_result_query(
    operator: Operator,
    handle_id: UUID,
    spec: ResultQuerySpec,
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
    """
    if operator.tenant_id is None:
        raise ResultHandleNotFoundError(handle_id)

    settings = get_settings()
    timeout_seconds = settings.result_query_timeout_seconds
    async with admit_result_read(operator) as lease:
        # One deadline over the fetch and the worker pipeline; the inner
        # timeout in ``_run_compiled_query`` never outlasts it.
        try:
            async with asyncio.timeout(timeout_seconds):
                row_set = await get_result_handle_store().fetch_rows(
                    tenant_id=operator.tenant_id,
                    operator_sub=operator.sub,
                    handle_id=handle_id,
                )
                if row_set is None:
                    raise ResultHandleNotFoundError(handle_id)
                _, truncated, effective_limit, encoded_rows = await _run_compiled_query(
                    row_set.rows,
                    spec,
                    handle_id,
                    max_output_rows=settings.result_query_max_output_rows,
                    timeout_seconds=timeout_seconds,
                    lease=lease,
                )
        except TimeoutError as exc:
            raise ResultQueryTimeoutError(handle_id, timeout_seconds) from exc

    # Byte cap applied AFTER the row cap: wide/nested rows can overflow the
    # token budget even under the row ceiling. The worker encoded the rows,
    # which also normalizes any non-JSON-native value (datetime, Decimal) to
    # a JSON-safe form for the transport layer, in one pass.
    if len(encoded_rows) > settings.result_query_max_output_bytes:
        raise ResultQueryOutputTooLargeError(
            handle_id, len(encoded_rows), settings.result_query_max_output_bytes
        )
    json_safe_rows: list[dict[str, Any]] = msgspec.json.decode(encoded_rows)

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
        "rows": json_safe_rows,
        "offset": 0,
        "limit": effective_limit,
        "returned_rows": len(json_safe_rows),
        "total_rows": row_set.total_rows,
        "stored_rows": row_set.stored_rows,
        "truncated": truncated,
        "coverage": "partial" if partial else "complete",
        "coverage_note": coverage_note,
    }
