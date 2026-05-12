# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MCP-specific audit-row writer + payload-hash helper (G0.5-T5).

The chassis :class:`~meho_backplane.audit.AuditMiddleware` writes one
row per authenticated HTTP request. The MCP transport is JSON-RPC over
``POST /mcp``, so the chassis middleware would attribute every MCP call
to a single envelope-level row regardless of how many ``tools/call`` or
``resources/read`` invocations live inside it. That's the wrong
granularity: compliance queries (G8) want one row per operation, not
per JSON-RPC POST.

T5 splits the responsibilities:

* :class:`~meho_backplane.audit.AuditMiddleware` skips ``/mcp`` paths
  (path-prefix exclusion added in this Task).
* MCP handlers wrap each operation in a try/finally that calls
  :func:`write_mcp_audit_row` before propagating the result or
  exception.

Field semantics (matched against
:class:`~meho_backplane.db.models.AuditLog`):

* ``method`` — literal ``"MCP"``. Distinguishes MCP rows from HTTP
  rows at query time so G8's audit-trail filters can target either
  surface without joining on ``path``.
* ``path`` — ``"/mcp/tools/call/{tool_name}"`` or
  ``"/mcp/resources/read/{uri}"`` — synthetic, mirrors the
  HTTP-side ``path`` shape so dashboards that group by path see
  MCP and HTTP traffic in the same axis.
* ``status_code`` — derived from the handler outcome. The MCP
  transport itself returns 200 with JSON-RPC error envelopes; the
  status_code here is the *audit* projection, mapping the JSON-RPC
  outcome onto familiar HTTP semantics (400 / 403 / 404 / 500 / 200).
* ``payload`` — for ``tools/call``: ``{op_id, params_hash, op_class}``.
  For ``resources/read``: ``{uri, op_class: "read"}``. ``params_hash``
  is SHA-256 of canonical JSON so G8 can answer "find all calls with
  these arguments" without persisting the arguments themselves —
  important for privacy-sensitive tool inputs (a future
  ``vault.kv.read`` tool's ``arguments`` would carry secret-path
  references).

Fail-closed contract: the helper raises on commit failure. The
caller's ``finally`` block surfaces the audit failure as a JSON-RPC
``-32603`` Internal Error and the operation is considered
unsuccessful — the same fail-closed posture the chassis
:class:`AuditMiddleware` enforces for HTTP routes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog

__all__ = ["compute_params_hash", "write_mcp_audit_row"]


def compute_params_hash(params: dict[str, Any]) -> str:
    """SHA-256 hex digest of *params*, canonicalised for cross-call stability.

    Canonical JSON via ``sort_keys=True`` + ``separators=(",", ":")``.
    The hash is the content-addressable token G8's audit queries use to
    answer "find all calls with these arguments" without persisting
    the (potentially sensitive) arguments themselves. Determinism is
    the load-bearing contract: same dict → same hex digest across
    Python versions, dict insertion order, and key-presence variations
    that JSON canonicalisation absorbs.

    Empty dict resolves to the canonical empty-object hash
    ``b04d…78a4`` (regression-locked by :mod:`tests.test_mcp_audit`).
    """
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def write_mcp_audit_row(
    *,
    operator: Operator,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    payload: dict[str, Any],
    request_id: uuid.UUID | None = None,
) -> None:
    """Commit one ``AuditLog`` row for an MCP tool call or resource read.

    Mirrors :func:`~meho_backplane.audit._write_audit_row` (the chassis
    HTTP audit-write helper) but takes its operator + tenant data from
    the validated :class:`Operator` rather than from contextvars. The
    MCP dispatcher already resolved the :class:`Operator` via
    :func:`~meho_backplane.mcp.auth.verify_mcp_jwt_and_bind`; passing it
    explicitly keeps the audit row's tenant attribution auditable at
    the call site rather than buried in a contextvar resolver.

    Raises any DB-side exception verbatim — the caller's ``finally``
    block converts it into a JSON-RPC ``-32603`` so the client sees
    the operation as failed and the audit-write failure is the
    operator's signal to investigate. Detail strings deliberately
    don't leak DSN substrings or exception messages into the audit
    row itself.
    """
    log = structlog.get_logger(__name__)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = AuditLog(
            id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
            operator_sub=operator.sub,
            tenant_id=operator.tenant_id,
            method=method,
            path=path,
            status_code=status_code,
            request_id=request_id,
            duration_ms=duration_ms,
            payload=payload,
        )
        session.add(row)
        await session.commit()
    log.info(
        "mcp_audit_row_written",
        method=method,
        path=path,
        status_code=status_code,
        duration_ms=duration_ms,
    )
