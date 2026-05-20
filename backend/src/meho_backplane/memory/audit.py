# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Internal audit-row writer for system-driven memory operations (G5.2-T1).

The chassis :class:`~meho_backplane.audit.AuditMiddleware` writes one
row per authenticated HTTP request; the MCP path's
:func:`~meho_backplane.mcp.audit.write_mcp_audit_row` writes one row
per JSON-RPC tool dispatch. Both pull the operator identity out of a
validated :class:`~meho_backplane.auth.operator.Operator` because both
surfaces have an authenticated caller.

The G5.2 memory-expiry sweeper has no caller: it is the background
``asyncio`` task the FastAPI lifespan owns, running off a fixed cadence
to delete ``source="memory"`` rows whose ``metadata.expires_at`` has
passed. It still needs to write an ``audit_log`` row per affected
tenant so G8's forensic queries can answer "what did the system clean
up overnight?" -- but the row's ``operator_sub`` is the synthetic
``"system"`` identity, not a JWT-derived ``sub``, and there is no
:class:`Operator` to pass in.

:func:`write_internal_audit_row` mirrors the
:func:`~meho_backplane.mcp.audit.write_mcp_audit_row` shape verbatim
except for taking ``operator_sub`` + ``tenant_id`` as explicit keyword
arguments. Field semantics:

* ``operator_sub`` -- typically the literal ``"system"`` for sweeper
  rows; callers writing their own internal operations name themselves
  (e.g. a future ``system:retention-sweeper``) so audit filters can
  partition by which background job ran.
* ``method`` -- the literal ``"INTERNAL"`` for memory-expiry sweeper
  rows. Distinguishes background-process rows from the chassis HTTP
  rows (``GET`` / ``POST`` / ...) and MCP rows (``"MCP"``) at audit-
  query time without joining on ``path`` -- the same axis the chassis
  + MCP writers already partition on.
* ``path`` -- the op identifier *within* the INTERNAL channel, e.g.
  ``"memory.expire"`` for the sweeper. The ``method``-is-channel,
  ``path``-is-op-id convention is documented in
  ``docs/architecture/audit.md`` so G8.2 (#219) audit-query consumers
  pick these rows up by path when ``meho audit query --op
  memory.expire`` ships.
* ``status_code`` -- HTTP-style projection of the operation's outcome
  (``200`` on success, ``500`` on a per-tick failure). Mirrors the
  MCP writer's HTTP-style mapping so dashboards that group by
  ``status_code`` see HTTP + MCP + INTERNAL traffic on the same axis.
* ``payload`` -- the operation-specific structured payload. For the
  memory sweeper: ``{"expired_count": N, "scopes": ["memory-user",
  ...]}`` so audit replays can reconstruct what was reaped per tick
  without keeping the deleted document bodies.

Fail-closed contract: the helper raises on commit failure. The
caller's per-tick ``try`` / ``except`` block (see
:func:`meho_backplane.memory.expiry._run_one_tick`) logs and continues
to the next cadence -- one bad audit-write must not kill the sweeper
loop. This is the opposite of the chassis HTTP path (which fail-closes
the *request*); for an internal background task, the loop-survival
contract dominates: a single failed audit row is preferable to the
loop dying and never cleaning up expired memories again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog

__all__ = ["write_internal_audit_row"]

#: Synthetic ``operator_sub`` value for sweeper-written rows. Centralised
#: so audit-query filters and downstream G8 dashboards can pin to one
#: stable literal rather than rediscovering it per call site.
SYSTEM_OPERATOR_SUB: str = "system"

#: ``method`` value for every INTERNAL-channel row. Carried here so the
#: caller never spells the literal at the call site and the channel-vs-
#: op-id convention stays auditable from one symbol.
INTERNAL_METHOD: str = "INTERNAL"

#: Canonical ``path`` value for the memory-expiry sweeper. Defined here
#: (rather than in :mod:`expiry`) so the audit-doc, the sweeper, and any
#: future audit-query consumer share one symbol.
MEMORY_EXPIRE_PATH: str = "memory.expire"


async def write_internal_audit_row(
    *,
    operator_sub: str,
    tenant_id: uuid.UUID,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    payload: dict[str, Any],
    request_id: uuid.UUID | None = None,
    audit_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Commit one ``AuditLog`` row for a system-driven internal operation.

    Mirrors :func:`~meho_backplane.mcp.audit.write_mcp_audit_row`'s shape
    exactly (same DB columns, same ``Decimal(str(...))`` round-trip for
    ``duration_ms`` to avoid float-binary artefacts) but takes
    ``operator_sub`` + ``tenant_id`` as explicit keyword arguments rather
    than deriving them from an :class:`Operator`: the memory-expiry
    sweeper that calls this helper runs without a JWT, so there is no
    validated operator to thread through.

    Parameters
    ----------
    operator_sub:
        Synthetic identity for the background job. Use
        :data:`SYSTEM_OPERATOR_SUB` (``"system"``) for the memory
        sweeper; future internal writers should pick a stable name
        (``"system:retention-sweeper"`` etc.) so audit filters can
        partition by background-job source.
    tenant_id:
        Which tenant this operation touched. The sweeper writes one row
        per affected tenant so cross-tenant audit replays do not need
        to fan out into joins on a ``payload.tenant_ids`` array.
    method:
        Channel literal -- :data:`INTERNAL_METHOD` for sweeper rows.
        Distinguishes background-process rows from HTTP / MCP rows in
        audit queries.
    path:
        Operation identifier within the channel -- e.g.
        :data:`MEMORY_EXPIRE_PATH` (``"memory.expire"``). Documented in
        ``docs/architecture/audit.md`` as the forward reference for
        G8.2 (#219) audit-query consumers.
    status_code:
        HTTP-style projection of the operation outcome (200 on success,
        500 on per-tick failure). Mirrors the MCP writer's HTTP-style
        mapping.
    duration_ms:
        Wall-clock duration of the operation. Round-tripped through
        ``Decimal(str(...))`` for shape consistency with the chassis +
        MCP writers; the substrate column is :class:`Numeric` so float
        binary artefacts would otherwise leak into stored values.
    payload:
        Operation-specific structured payload. For the memory sweeper:
        ``{"expired_count": N, "scopes": ["memory-user", ...]}``.
    request_id:
        Optional request-id for back-correlation. Background-job rows
        typically have no request id; the column is nullable so passing
        ``None`` is the supported shape.
    audit_id:
        Optional pre-generated id. The G6.1-T3 publish-on-write hook
        pre-generates the id at the dispatch call site so the broadcast
        event can reference the same id post-commit. Sweeper callers
        that don't broadcast leave this ``None`` and a fresh ``uuid4``
        is generated.

    Returns
    -------
    uuid.UUID
        The audit-row id used for the row.

    Raises
    ------
    Exception
        Any DB-side exception propagates verbatim. The caller's per-tick
        ``try`` / ``except`` block decides whether to swallow it (the
        sweeper does so loud-but-non-fatal) or surface it.
    """
    log = structlog.get_logger(__name__)
    if audit_id is None:
        audit_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = AuditLog(
            id=audit_id,
            occurred_at=datetime.now(UTC),
            operator_sub=operator_sub,
            tenant_id=tenant_id,
            method=method,
            path=path,
            status_code=status_code,
            request_id=request_id,
            # Convert ``duration_ms`` via ``Decimal(str(...))`` for shape
            # consistency with the chassis + MCP writers. SQLAlchemy's
            # ``Numeric`` type accepts both ``float`` and ``Decimal``, but
            # the ``Decimal(str(value))`` path round-trips through the
            # JSON string representation and avoids the float->Decimal
            # binary conversion artefacts ``Decimal(float)`` would
            # introduce.
            duration_ms=Decimal(str(duration_ms)),
            payload=payload,
        )
        session.add(row)
        await session.commit()
    log.info(
        "internal_audit_row_written",
        operator_sub=operator_sub,
        method=method,
        path=path,
        status_code=status_code,
        duration_ms=duration_ms,
    )
    return audit_id
