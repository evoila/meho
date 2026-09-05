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
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from uuid import UUID

import msgspec

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.result_handle_store import get_result_handle_store
from meho_backplane.jsonflux.query.contract import (
    RESULT_TABLE,
    CompiledQuery,
    QueryContractError,
    ResultQuerySpec,
    compile_query,
)
from meho_backplane.jsonflux.query.engine import QueryEngine
from meho_backplane.settings import get_settings

__all__ = [
    "MAX_LIMIT",
    "QueryContractError",
    "ResultHandleNotFoundError",
    "ResultQueryOutputTooLargeError",
    "ResultQuerySpec",
    "ResultQueryTimeoutError",
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
    """
    if operator.tenant_id is None:
        raise ResultHandleNotFoundError(handle_id)

    window = await get_result_handle_store().fetch_window(
        tenant_id=operator.tenant_id,
        operator_sub=operator.sub,
        handle_id=handle_id,
        offset=offset,
        limit=limit,
    )
    if window is None:
        raise ResultHandleNotFoundError(handle_id)

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
    """A compiled ``result_query`` ``SELECT`` exceeded its wall-time budget.

    DuckDB has no native Python query timeout, so the core runs
    ``conn.execute()`` on a worker thread and calls ``conn.interrupt()`` when
    the ``result_query_timeout_seconds`` budget elapses. Recoverable: the
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
    """A ``result_query`` result exceeded the serialized-output byte budget.

    Applied **after** the row cap: even ≤ ``MAX_LIMIT`` rows can overflow the
    ``result_query_max_output_bytes`` budget when they are wide or deeply
    nested. Recoverable: the caller narrows the projection (a smaller
    ``select``), filters harder, or lowers ``limit`` and retries.
    """

    def __init__(self, handle_id: UUID, output_bytes: int, max_bytes: int) -> None:
        self.handle_id = handle_id
        self.output_bytes = output_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"query result for handle {handle_id} is {output_bytes} serialized "
            f"bytes, over the {max_bytes}-byte output budget. Narrow the result: "
            "project fewer columns with `select`, add a `filter`, or lower `limit`."
        )


def _known_columns(engine: QueryEngine) -> list[str]:
    """Return the registered handle table's column names (its known schema).

    The contract compiler validates every referenced field against this set,
    so an unknown field is rejected before any SQL runs.
    """
    described = engine.conn.execute(f"DESCRIBE {RESULT_TABLE}").fetchall()
    return [str(row[0]) for row in described]


def _execute_sync(engine: QueryEngine, compiled: CompiledQuery) -> tuple[list[str], list[Any]]:
    """Run the compiled SELECT on the engine's connection (worker thread).

    Returns the result column names and the raw fetched rows. Runs on a
    worker thread so :func:`run_result_query` can call
    ``engine.conn.interrupt()`` from the event loop on timeout.
    """
    cursor = engine.conn.execute(compiled.sql, compiled.params)
    columns = [desc[0] for desc in cursor.description]
    return columns, cursor.fetchall()


async def _run_compiled_query(
    rows: list[dict[str, Any]],
    spec: ResultQuerySpec,
    handle_id: UUID,
    *,
    max_output_rows: int,
    timeout_seconds: int,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Compile *spec*, run it under the time budget; return rows + flags.

    The engine is created + the rows registered + the contract compiled
    inline (bounded work), then only the ``SELECT`` execution runs on a
    worker thread so the event loop can interrupt it on timeout. The result
    is capped to ``max_output_rows`` and ``truncated`` is ``True`` when the
    underlying result had more rows than the cap admits.
    """
    engine = QueryEngine()
    try:
        engine.register(RESULT_TABLE, rows, unwrap="auto")
        columns = _known_columns(engine)
        compiled = compile_query(spec, columns, max_limit=max_output_rows)

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, _execute_sync, engine, compiled)
        try:
            result_columns, fetched = await asyncio.wait_for(
                asyncio.shield(future), timeout_seconds
            )
        except TimeoutError as exc:  # asyncio.TimeoutError aliases this on 3.11+
            with contextlib.suppress(Exception):
                engine.conn.interrupt()
            with contextlib.suppress(BaseException):
                await future
            raise ResultQueryTimeoutError(handle_id, timeout_seconds) from exc
    finally:
        engine.close()

    truncated = len(fetched) > compiled.effective_limit
    capped = fetched[: compiled.effective_limit]
    result_rows = [dict(zip(result_columns, row, strict=False)) for row in capped]
    return result_rows, truncated, compiled.effective_limit


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
    cross-operator handle, or no tenant), :class:`QueryContractError` (a
    field-vs-schema or value-shape violation), :class:`ResultQueryTimeoutError`
    (time budget exceeded), or :class:`ResultQueryOutputTooLargeError` (byte
    budget exceeded). Each transport maps these to its own recoverable-error
    shape.
    """
    if operator.tenant_id is None:
        raise ResultHandleNotFoundError(handle_id)

    row_set = await get_result_handle_store().fetch_rows(
        tenant_id=operator.tenant_id,
        operator_sub=operator.sub,
        handle_id=handle_id,
    )
    if row_set is None:
        raise ResultHandleNotFoundError(handle_id)

    settings = get_settings()
    result_rows, truncated, effective_limit = await _run_compiled_query(
        row_set.rows,
        spec,
        handle_id,
        max_output_rows=settings.result_query_max_output_rows,
        timeout_seconds=settings.result_query_timeout_seconds,
    )

    # Byte cap applied AFTER the row cap: wide/nested rows can overflow the
    # token budget even under the row ceiling. Encoding also normalizes any
    # non-JSON-native value (datetime, Decimal) to a JSON-safe form for the
    # transport layer, in one pass.
    encoded_rows = msgspec.json.encode(result_rows)
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
