# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Private query + cascade helpers for the review-queue service.

Module-level free functions split out of
:mod:`meho_backplane.operations.ingest.service` so the public
:class:`~meho_backplane.operations.ingest.service.ReviewService`
stays under the project's per-file size budget. The functions here
are all session-bound (they take an :class:`AsyncSession` plus a
scope and never open their own transaction) — composing them in
:class:`ReviewService` keeps the public methods as thin
transaction-scoped coordinators.

Underscore-prefixed names indicate non-public contract: the
package's ``__init__.py`` does not re-export anything from this
module. v0.2.next refactors are free to reshape the helpers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest.exceptions import ConnectorNotFoundError

__all__ = [
    "AUDIT_METHOD",
    "OP_DISABLE_CONNECTOR",
    "OP_EDIT_GROUP",
    "OP_EDIT_OP",
    "OP_ENABLE_CONNECTOR",
    "OP_ENABLE_GROUP",
    "VALID_SAFETY_LEVELS",
    "ConnectorScope",
    "cascade_is_enabled",
    "load_group",
    "load_groups",
    "load_op",
    "load_ops_in_groups",
    "operator_disabled_op_ids",
    "write_audit_row",
]

#: Audit-row ``method`` value for every service-level row written
#: by this package. Distinct from ``"GET"`` / ``"POST"`` (HTTP
#: rows) and ``"MCP"`` (MCP rows) so G8 audit dashboards can split
#: the three surfaces cleanly.
AUDIT_METHOD: Final[str] = "SERVICE"

#: Op-ids (audit-row ``path`` column) for the five mutating actions.
OP_ENABLE_CONNECTOR: Final[str] = "meho.connector.enable"
OP_DISABLE_CONNECTOR: Final[str] = "meho.connector.disable"
OP_ENABLE_GROUP: Final[str] = "meho.connector.enable_group"
OP_EDIT_GROUP: Final[str] = "meho.connector.edit_group"
OP_EDIT_OP: Final[str] = "meho.connector.edit_op"

#: Allowed values for :attr:`EndpointDescriptor.safety_level`. The
#: same set is enforced by the DB CHECK constraint; the Python-side
#: validator catches bad input before the round-trip.
VALID_SAFETY_LEVELS: Final[frozenset[str]] = frozenset(
    {"safe", "caution", "dangerous"},
)


@dataclass(frozen=True, slots=True)
class ConnectorScope:
    """Resolved ``(product, version, impl_id, tenant_id)`` query scope.

    Built once at the top of every service method from the public
    ``(connector_id, tenant_id)`` arguments so downstream helpers
    take a single tuple instead of repeating the same four
    positional fields.
    """

    product: str
    version: str
    impl_id: str
    tenant_id: UUID | None


# ---------------------------------------------------------------------------
# DB query helpers
# ---------------------------------------------------------------------------


async def load_groups(
    session: AsyncSession,
    scope: ConnectorScope,
    connector_id: str,
) -> list[OperationGroup]:
    """Return every :class:`OperationGroup` row in *scope* sorted by ``group_key``.

    Empty result → :class:`ConnectorNotFoundError`. The "is_None"
    guard matches the SQL semantics SQLAlchemy emits
    (``tenant_id IS NULL`` vs ``tenant_id = :tid``). The deterministic
    sort makes review payloads round-trippable across calls and
    across DB implementations.
    """
    stmt = select(OperationGroup).where(
        OperationGroup.product == scope.product,
        OperationGroup.version == scope.version,
        OperationGroup.impl_id == scope.impl_id,
    )
    if scope.tenant_id is None:
        stmt = stmt.where(OperationGroup.tenant_id.is_(None))
    else:
        stmt = stmt.where(OperationGroup.tenant_id == scope.tenant_id)
    stmt = stmt.order_by(OperationGroup.group_key)
    result = await session.execute(stmt)
    groups = list(result.scalars().all())
    if not groups:
        raise ConnectorNotFoundError(
            connector_id=connector_id,
            tenant_id=scope.tenant_id,
        )
    return groups


async def load_group(
    session: AsyncSession,
    scope: ConnectorScope,
    connector_id: str,
    group_key: str,
) -> OperationGroup:
    """Return a single :class:`OperationGroup` matching ``group_key``.

    Missing → :class:`ConnectorNotFoundError`. The exception also
    covers "connector exists but this group_key doesn't" — same
    conflation as the cross-tenant case, intentionally, to keep
    the operator-facing failure surface uniform.
    """
    stmt = select(OperationGroup).where(
        OperationGroup.product == scope.product,
        OperationGroup.version == scope.version,
        OperationGroup.impl_id == scope.impl_id,
        OperationGroup.group_key == group_key,
    )
    if scope.tenant_id is None:
        stmt = stmt.where(OperationGroup.tenant_id.is_(None))
    else:
        stmt = stmt.where(OperationGroup.tenant_id == scope.tenant_id)
    result = await session.execute(stmt)
    group = result.scalar_one_or_none()
    if group is None:
        raise ConnectorNotFoundError(
            connector_id=connector_id,
            tenant_id=scope.tenant_id,
        )
    return group


async def load_op(
    session: AsyncSession,
    scope: ConnectorScope,
    connector_id: str,
    op_id: str,
) -> EndpointDescriptor:
    """Return a single :class:`EndpointDescriptor` for ``op_id``."""
    stmt = select(EndpointDescriptor).where(
        EndpointDescriptor.product == scope.product,
        EndpointDescriptor.version == scope.version,
        EndpointDescriptor.impl_id == scope.impl_id,
        EndpointDescriptor.op_id == op_id,
    )
    if scope.tenant_id is None:
        stmt = stmt.where(EndpointDescriptor.tenant_id.is_(None))
    else:
        stmt = stmt.where(EndpointDescriptor.tenant_id == scope.tenant_id)
    result = await session.execute(stmt)
    op = result.scalar_one_or_none()
    if op is None:
        raise ConnectorNotFoundError(
            connector_id=connector_id,
            tenant_id=scope.tenant_id,
        )
    return op


async def load_ops_in_groups(
    session: AsyncSession,
    scope: ConnectorScope,
    group_ids: list[UUID],
) -> list[EndpointDescriptor]:
    """Return every :class:`EndpointDescriptor` whose ``group_id`` is in *group_ids*.

    Returns ``[]`` when no rows match (a freshly-ingested group
    with no operations yet is legal — the staged-status guard
    then applies trivially). Results are sorted by ``op_id`` so
    review payloads round-trip deterministically.
    """
    if not group_ids:
        return []
    stmt = select(EndpointDescriptor).where(
        EndpointDescriptor.product == scope.product,
        EndpointDescriptor.version == scope.version,
        EndpointDescriptor.impl_id == scope.impl_id,
        EndpointDescriptor.group_id.in_(group_ids),
    )
    if scope.tenant_id is None:
        stmt = stmt.where(EndpointDescriptor.tenant_id.is_(None))
    else:
        stmt = stmt.where(EndpointDescriptor.tenant_id == scope.tenant_id)
    stmt = stmt.order_by(EndpointDescriptor.op_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Audit emission
# ---------------------------------------------------------------------------


async def write_audit_row(
    session: AsyncSession,
    *,
    operator_sub: str,
    operator_tenant_id: UUID,
    op_id: str,
    payload: dict[str, Any],
) -> uuid.UUID:
    """Append one :class:`AuditLog` row for a service-level action.

    Reuses the open *session* so the audit row commits in the same
    transaction as the state mutation. The caller's outer
    ``await session.commit()`` makes the row durable.

    ``operator_sub`` / ``operator_tenant_id`` are the
    *attribution* values: who performed the action and from which
    tenant. Distinct from the target scope (a ``tenant_admin``
    operator in tenant A reviewing a built-in NULL-scope connector
    writes a row attributed to operator A's tenant but the
    affected rows still carry ``tenant_id=NULL``).

    ``status_code`` is ``200`` on success. Failures raise before
    the audit write and never produce a row. ``duration_ms`` is
    set to ``Decimal("0")``: the outer HTTP / MCP request already
    times the parent invocation, and a service-level timer would
    be redundant. The ``Decimal(str(...))`` shape mirrors
    :func:`~meho_backplane.audit._write_audit_row` so SQLAlchemy's
    ``Numeric`` adapter sees the same input across every audit
    writer in the codebase.
    """
    audit_id = uuid.uuid4()
    row = AuditLog(
        id=audit_id,
        occurred_at=datetime.now(UTC),
        operator_sub=operator_sub,
        tenant_id=operator_tenant_id,
        method=AUDIT_METHOD,
        path=op_id,
        status_code=200,
        request_id=None,
        duration_ms=Decimal("0"),
        payload=payload,
    )
    session.add(row)
    return audit_id


# ---------------------------------------------------------------------------
# Operator-override walk + cascade
# ---------------------------------------------------------------------------


async def operator_disabled_op_ids(
    session: AsyncSession,
    scope: ConnectorScope,
    group_ids: list[UUID],
) -> list[str]:
    """Return ``op_id``s the operator explicitly disabled via ``edit_op``.

    Walks every :data:`OP_EDIT_OP` audit row; the most-recent row
    per ``op_id`` wins. Returns the list of ``op_id``s whose most-
    recent edit set ``is_enabled=False`` so
    :func:`cascade_is_enabled` can skip them.

    Implementation notes:

    * The audit table has no index on JSON payload fields. For
      v0.2 the corpus is small (O(thousands) ops, O(tens) of edits
      per connector); a full scan is acceptable. v0.2.next: add a
      covering index or a denormalised
      ``endpoint_descriptor.operator_overridden_at`` column once
      audit growth makes the scan slow.
    * The walk is per-call rather than cached because the override
      set can change between :meth:`ReviewService.enable_connector`
      invocations.
    * Sessions / dialects: ``payload`` is JSONB on PG and
      JSON-stored-as-TEXT on SQLite. Filtering the SQL side would
      need dialect-specific JSON operators; instead we filter
      Python-side after loading the candidate rows (rows with
      ``path = 'meho.connector.edit_op'``).
    """
    if not group_ids:
        return []
    ops = await load_ops_in_groups(session, scope, group_ids)
    valid_op_ids = {op.op_id for op in ops}
    if not valid_op_ids:
        return []
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.method == AUDIT_METHOD,
            AuditLog.path == OP_EDIT_OP,
        )
        .order_by(AuditLog.occurred_at.desc())
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    latest_decision: dict[str, bool] = {}
    for row in rows:
        payload = row.payload
        if not isinstance(payload, dict):
            continue
        audit_op_id = payload.get("op_id")
        if not isinstance(audit_op_id, str) or audit_op_id not in valid_op_ids:
            continue
        if audit_op_id in latest_decision:
            # Newer row already recorded; skip the older one.
            continue
        if "is_enabled_set_to" not in payload:
            # This edit_op call did not touch is_enabled — the
            # operator override (if any) lives on an older row;
            # continue scanning.
            continue
        decision = payload.get("is_enabled_set_to")
        if not isinstance(decision, bool):
            continue
        latest_decision[audit_op_id] = decision
    return [op_id for op_id, enabled in latest_decision.items() if enabled is False]


async def cascade_is_enabled(
    session: AsyncSession,
    scope: ConnectorScope,
    group_ids: list[UUID],
    *,
    target: bool,
    excluded_op_ids: list[str],
) -> int:
    """Bulk-update child ops' ``is_enabled`` to *target*.

    Returns the number of rows actually changed (``UPDATE``
    affects rows where the new value differs from the old; the
    return value drives the audit payload's
    ``ops_cascade_count`` for operator-facing confirmation
    messages).

    ``excluded_op_ids`` is a Python-side list rather than a
    sub-select to keep the SQL dialect-portable across PG +
    SQLite. v0.2 connector sizes (vCenter ~961 ops) fit easily
    in a ``WHERE op_id NOT IN (...)`` clause; v0.2.next can switch
    to a sub-select if the exclusion list grows past SQL parameter
    limits.
    """
    if not group_ids:
        return 0
    stmt = (
        update(EndpointDescriptor)
        .where(
            EndpointDescriptor.product == scope.product,
            EndpointDescriptor.version == scope.version,
            EndpointDescriptor.impl_id == scope.impl_id,
            EndpointDescriptor.group_id.in_(group_ids),
            EndpointDescriptor.is_enabled != target,
        )
        .values(is_enabled=target)
    )
    if scope.tenant_id is None:
        stmt = stmt.where(EndpointDescriptor.tenant_id.is_(None))
    else:
        stmt = stmt.where(EndpointDescriptor.tenant_id == scope.tenant_id)
    if excluded_op_ids:
        stmt = stmt.where(
            EndpointDescriptor.op_id.notin_(excluded_op_ids),
        )
    # The cast is to CursorResult because AsyncSession.execute is
    # typed to return the generic Result whose rowcount mypy can't
    # see; at runtime an UPDATE produces a CursorResult that does.
    result: CursorResult[Any] = await session.execute(stmt)  # type: ignore[assignment]
    rowcount = result.rowcount if result.rowcount is not None else 0
    return max(rowcount, 0)
