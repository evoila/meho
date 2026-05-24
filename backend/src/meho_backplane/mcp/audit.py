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
from decimal import Decimal
from typing import Any

import structlog

from meho_backplane.audit import _resolve_audit_payload
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


def _resolve_uuid_contextvar(name: str) -> uuid.UUID | None:
    """Read structlog contextvar *name* and parse it as :class:`uuid.UUID`.

    Mirrors :func:`~meho_backplane.audit._resolve_target_id`: the binder
    stores the value as ``str(some_uuid)`` and this reader parses it
    back. Used for the two graph columns on the MCP audit row:

    * ``mcp_session_id`` — bound by
      :func:`~meho_backplane.mcp.server._bind_mcp_session_id` from the
      inbound ``Mcp-Session-Id`` header (G8.2-T2 #1010); lands on
      ``AuditLog.agent_session_id``.
    * ``parent_audit_id`` — forward-compat hook (G8.2-T2 #1010): in v0.2
      no MCP caller binds it, but reading it now means a future
      tool-calls-tool flow only has to ``bind_contextvars`` the parent
      op's audit id for the closure walk (G8.2 recursive-CTE replay) to
      see the edge.

    Unbound → ``None`` (the chassis-era default for HTTP rows, and the
    v0.2 default for ``parent_audit_id``). A bound value that fails the
    type / UUID-parse check is a programming error (the binders only
    bind canonical UUID strings); the row is still committed with the
    column ``None`` and the malformed value is logged so the invariant
    violation is visible rather than silently fatal.
    """
    raw = structlog.contextvars.get_contextvars().get(name)
    if raw is None:
        return None
    log = structlog.get_logger(__name__)
    if not isinstance(raw, str):
        log.error("mcp_audit_malformed_uuid_contextvar", name=name, value=raw)
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        log.error("mcp_audit_malformed_uuid_contextvar", name=name, value=raw)
        return None


# code-quality-allow: linear fail-closed audit write; the length is a
# compliance-critical docstring documenting the merge + fail-closed contract,
# not branching logic — extracting helpers would fragment one DB transaction.
async def write_mcp_audit_row(
    *,
    operator: Operator,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    payload: dict[str, Any],
    request_id: uuid.UUID | None = None,
    audit_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Commit one ``AuditLog`` row for an MCP tool call or resource read.

    Mirrors :func:`~meho_backplane.audit._write_audit_row` (the chassis
    HTTP audit-write helper) but takes its operator + tenant data from
    the validated :class:`Operator` rather than from contextvars. The
    MCP dispatcher already resolved the :class:`Operator` via
    :func:`~meho_backplane.mcp.auth.verify_mcp_jwt_and_bind`; passing it
    explicitly keeps the audit row's tenant attribution auditable at
    the call site rather than buried in a contextvar resolver.

    The final ``payload`` written to the row is the caller's explicit
    payload **merged on top of** any ``audit_*`` contextvars routed
    through :func:`~meho_backplane.audit._resolve_audit_payload`. This
    matches the REST middleware's behaviour
    (:func:`~meho_backplane.audit.AuditMiddleware.dispatch` reads the
    same helper at audit-write time) and lets MCP-side handlers enrich
    the row by binding ``audit_*`` contextvars without the envelope
    having to know about every per-handler field. Caller-supplied
    keys win on collision (``op_id`` / ``op_class`` / ``params_hash``
    / ``broadcast_detail_*`` are load-bearing identity and must not
    be overwritten by stale contextvars from an earlier call on the
    same task). #710's ``meho.broadcast.overrides.set`` (REST + MCP
    parity) is the v0.2 caller; future MCP meta-tools that bind
    ``audit_*`` contextvars inherit the same surfacing automatically.

    The ``agent_session_id`` / ``parent_audit_id`` graph columns are
    read from contextvars via :func:`_resolve_uuid_contextvar`
    (G8.2-T2 #1010), not the ``audit_*`` payload merge.

    Raises any DB-side exception verbatim — the caller's ``finally``
    block converts it into a JSON-RPC ``-32603`` so the client sees
    the operation as failed and the audit-write failure is the
    operator's signal to investigate. Detail strings deliberately
    don't leak DSN substrings or exception messages into the audit
    row itself.

    ``audit_id`` is optional. The G6.1-T3 publish-on-write hook
    (#309) pre-generates the id at the dispatch call site so it can
    reference the same id on the
    :class:`~meho_backplane.broadcast.events.BroadcastEvent` after
    the audit commit succeeds. Callers that don't need the id (e.g.
    the helper-level test that exercises the writer directly) can
    omit the kwarg and get a fresh uuid4 generated here. Returns the
    audit id used for the row so the dispatch call site can plumb it
    through to ``publish_event`` either way.
    """
    # Merge ``audit_*`` contextvars into the row payload — same
    # surfacing the REST middleware does. Caller's payload wins on
    # collision so the MCP envelope's load-bearing identity keys
    # (``op_id`` / ``op_class`` / ``params_hash``) are never
    # overwritten by stale contextvars.
    contextvar_payload = _resolve_audit_payload()
    if contextvar_payload:
        payload = {**contextvar_payload, **payload}

    # Graph columns off contextvars (G8.2-T2 #1010) — real columns, not
    # payload keys; see :func:`_resolve_uuid_contextvar` for bind sources.
    agent_session_id = _resolve_uuid_contextvar("mcp_session_id")
    parent_audit_id = _resolve_uuid_contextvar("parent_audit_id")

    log = structlog.get_logger(__name__)
    if audit_id is None:
        audit_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = AuditLog(
            id=audit_id,
            occurred_at=datetime.now(UTC),
            operator_sub=operator.sub,
            tenant_id=operator.tenant_id,
            agent_session_id=agent_session_id,
            parent_audit_id=parent_audit_id,
            method=method,
            path=path,
            status_code=status_code,
            request_id=request_id,
            # Convert ``duration_ms`` via ``Decimal(str(...))`` for shape
            # consistency with the chassis ``_write_audit_row`` (in
            # ``meho_backplane.audit``). SQLAlchemy's ``Numeric`` type
            # accepts both ``float`` and ``Decimal``, but the
            # ``Decimal(str(value))`` path round-trips through the JSON
            # string representation and avoids the float→Decimal binary
            # conversion artifacts ``Decimal(float)`` would introduce.
            duration_ms=Decimal(str(duration_ms)),
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
    return audit_id
