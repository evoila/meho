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

from typing import Any
from uuid import UUID

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.result_handle_store import get_result_handle_store

__all__ = ["MAX_LIMIT", "ResultHandleNotFoundError", "read_result_window"]

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
