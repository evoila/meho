# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Private query + cascade helpers for the review-queue service.

Module-level free functions split out of
:mod:`meho_backplane.operations.ingest.service` so the public
:class:`~meho_backplane.operations.ingest.service.ReviewService`
stays under the project's per-file size budget. The query/cascade
helpers are session-bound (they take an :class:`AsyncSession` plus a
scope and never open their own transaction) — composing them in
:class:`ReviewService` keeps the public methods as thin
transaction-scoped coordinators. The enable-time advisory builder at
the bottom is the one session-free exception (it reads the in-process
connector registry, not the DB).

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

import structlog
from sqlalchemy import CursorResult, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest.api_schemas import EditOpWarning
from meho_backplane.operations.ingest.connector_registration import (
    resolved_auto_shim_class,
    resolved_profiled_connector_class,
)
from meho_backplane.operations.ingest.exceptions import ConnectorNotFoundError

_log = structlog.get_logger(__name__)

__all__ = [
    "AUDIT_METHOD",
    "OP_DELETE_CONNECTOR",
    "OP_DISABLE_CONNECTOR",
    "OP_EDIT_GROUP",
    "OP_EDIT_OP",
    "OP_ENABLE_CONNECTOR",
    "OP_ENABLE_GROUP",
    "OP_ENABLE_READS",
    "OP_LLM_GROUPING",
    "OP_PROFILE_STAMP",
    "READ_HTTP_METHODS",
    "VALID_SAFETY_LEVELS",
    "ConnectorScope",
    "apply_op_overrides",
    "audit_profile_stamp",
    "bulk_enable_read_ops",
    "cascade_is_enabled",
    "enable_time_auto_shim_warnings",
    "load_group",
    "load_groups",
    "load_op",
    "load_ops_in_groups",
    "operator_disabled_op_ids",
    "scope_has_groups",
    "validate_edit_op_args",
    "write_audit_row",
]

#: Audit-row ``method`` value for every service-level row written
#: by this package. Distinct from ``"GET"`` / ``"POST"`` (HTTP
#: rows) and ``"MCP"`` (MCP rows) so G8 audit dashboards can split
#: the three surfaces cleanly.
AUDIT_METHOD: Final[str] = "SERVICE"

#: Op-ids (audit-row ``path`` column) for the mutating actions.
OP_ENABLE_CONNECTOR: Final[str] = "meho.connector.enable"
OP_DISABLE_CONNECTOR: Final[str] = "meho.connector.disable"
OP_ENABLE_GROUP: Final[str] = "meho.connector.enable_group"
OP_ENABLE_READS: Final[str] = "meho.connector.enable_reads"
OP_EDIT_GROUP: Final[str] = "meho.connector.edit_group"
OP_EDIT_OP: Final[str] = "meho.connector.edit_op"
OP_DELETE_CONNECTOR: Final[str] = "meho.connector.delete"
OP_LLM_GROUPING: Final[str] = "meho.connector.llm_grouping"
#: Audit op-id for the first stamp of an :class:`ExecutionProfile` onto an
#: ingested connector (G0.28-T5 #1971). The stamp makes the connector
#: *dispatchable* but deliberately does NOT auto-enable its ops — they stay
#: ``is_enabled=False`` / ``review_status='staged'`` behind the review gate.
#: The audit row makes the dispatchability change durable and attributable.
OP_PROFILE_STAMP: Final[str] = "meho.connector.profile_stamp"

#: HTTP methods that classify an ingested op as *read-class*. The
#: bulk read-class enable path (G0.25-T7 #1749) flips ``is_enabled``
#: only on ingested ops whose ``method`` is one of these; every
#: write-shaped verb (POST / PUT / PATCH / DELETE) stays default-deny.
#: ``EndpointDescriptor`` carries no ``op_class`` column — the
#: read/write taxonomy the MCP tool registry uses lives on
#: :class:`~meho_backplane.mcp.registry.ToolDefinition`, not the
#: descriptor row — so HTTP method is the per-row read-class signal
#: for ingested operations (typed / composite ops have ``method``
#: NULL and are never matched). Stored uppercase to compare against
#: the verbatim spec verbs the ingest parser writes.
READ_HTTP_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})

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


async def scope_has_groups(
    session: AsyncSession,
    scope: ConnectorScope,
) -> bool:
    """Return whether any :class:`OperationGroup` row exists under *scope*.

    A non-raising existence probe used by the shared scope resolver
    (:meth:`~meho_backplane.operations.ingest.service.ReviewService._resolve_existing_scope`)
    to detect when a ``connector_id`` maps to both a tenant-curated row
    (``tenant_id = scope.tenant_id``) and a built-in row
    (``tenant_id IS NULL``). Unlike :func:`load_groups`, it returns a
    bool instead of raising :class:`ConnectorNotFoundError` on an empty
    result — the resolver needs to test each scope independently before
    deciding between "resolve", "ambiguous", and "not found", so a
    raising helper would force a try/except per probe. ``SELECT 1 ...
    LIMIT 1`` keeps the probe cheap (no row hydration); the same
    ``tenant_id IS NULL`` / ``tenant_id = :tid`` split every sibling
    helper uses honours scope isolation.
    """
    stmt = (
        select(literal(1))
        .select_from(OperationGroup)
        .where(
            OperationGroup.product == scope.product,
            OperationGroup.version == scope.version,
            OperationGroup.impl_id == scope.impl_id,
        )
    )
    if scope.tenant_id is None:
        stmt = stmt.where(OperationGroup.tenant_id.is_(None))
    else:
        stmt = stmt.where(OperationGroup.tenant_id == scope.tenant_id)
    stmt = stmt.limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


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


async def count_ops_in_scope(session: AsyncSession, scope: ConnectorScope) -> int:
    """Count **every** :class:`EndpointDescriptor` row for *scope*, group or not.

    The review payload's ``total_op_count`` sums only ops in *rendered* groups,
    so ops with a null ``group_id`` (or in an unrendered group) are excluded
    from it. This counts the same universe the ``GET /api/v1/connectors``
    listing does — the listing's
    :func:`~meho_backplane.operations.ingest.list_connectors._operation_count_by_connector`
    counts ``count(EndpointDescriptor.id)`` by the connector triple, no group
    join — so the review can report a ``ungrouped_op_count`` that reconciles
    ``total_op_count + ungrouped_op_count`` to the listing's ``operation_count``
    (#125). Same scope predicate as :func:`load_ops_in_groups` minus the
    ``group_id`` filter, so the two read one consistent universe.
    """
    stmt = select(func.count(EndpointDescriptor.id)).where(
        EndpointDescriptor.product == scope.product,
        EndpointDescriptor.version == scope.version,
        EndpointDescriptor.impl_id == scope.impl_id,
    )
    if scope.tenant_id is None:
        stmt = stmt.where(EndpointDescriptor.tenant_id.is_(None))
    else:
        stmt = stmt.where(EndpointDescriptor.tenant_id == scope.tenant_id)
    return int(await session.scalar(stmt) or 0)


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


async def audit_profile_stamp(
    session: AsyncSession,
    *,
    operator_sub: str,
    operator_tenant_id: UUID,
    connector_id: str,
    scope: ConnectorScope,
    connector_class: str,
) -> uuid.UUID:
    """Append one :class:`AuditLog` row for the first stamp of an ExecutionProfile.

    G0.28-T5 (#1971). Called once, the first time an
    :class:`ExecutionProfile` is stamped onto an ingested connector
    (i.e. the first time a
    :class:`~meho_backplane.connectors.profiled.ProfiledRestConnector`
    becomes the resolved class for the connector's ``(product, version,
    impl_id)``). The stamp transitions the connector from
    non-dispatchable to **dispatchable**, but does NOT touch any op's
    ``is_enabled`` / ``review_status`` — the review gate stays the
    interlock, so a stamp can never auto-enable dispatch.

    Reuses the open *session* so the stamp's audit row commits in the
    same transaction as the registration mutation; the caller's outer
    ``await session.commit()`` makes it durable. The payload records the
    connector triple and the resolved profiled class name so an operator
    (or an audit query) can see *when* and *by whom* the connector
    became dispatchable — distinct from the later per-op
    :data:`OP_EDIT_OP` rows that record the review-gate clearance.
    """
    return await write_audit_row(
        session,
        operator_sub=operator_sub,
        operator_tenant_id=operator_tenant_id,
        op_id=OP_PROFILE_STAMP,
        payload={
            "connector_id": connector_id,
            "product": scope.product,
            "version": scope.version,
            "impl_id": scope.impl_id,
            "connector_class": connector_class,
        },
    )


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


async def bulk_enable_read_ops(
    session: AsyncSession,
    scope: ConnectorScope,
) -> int:
    """Flip ``is_enabled=True`` on every staged read-class ingested op in *scope*.

    The bulk read-class enable path (G0.25-T7 #1749). Read-class is
    HTTP method ∈ :data:`READ_HTTP_METHODS` (``GET`` / ``HEAD``) on an
    ``source_kind='ingested'`` row; every write-shaped verb
    (POST / PUT / PATCH / DELETE) and every typed / composite op
    (``method`` NULL) is left untouched so writes stay default-deny.

    Returns the number of rows actually changed. The
    ``is_enabled != True`` guard is what makes the action idempotent:
    re-running once the reads are enabled matches no rows and returns
    ``0``, so the caller writes no audit row. The whole-connector
    filter (no ``group_id`` predicate) is deliberate — the path
    enables reads across the connector regardless of which groups
    are staged vs. enabled, because read-class coverage is the goal,
    not a group-level review transition.

    Scope is honoured the same way every sibling helper honours it:
    the ``(product, version, impl_id)`` triple plus the
    ``tenant_id IS NULL`` / ``tenant_id = :tid`` split, so an
    operator-tenant call never touches built-in rows and vice versa.
    The ``CursorResult`` cast mirrors :func:`cascade_is_enabled` —
    mypy can't see ``rowcount`` on the generic ``Result`` the async
    ``execute`` is typed to return, but an ``UPDATE`` produces a
    ``CursorResult`` at runtime.
    """
    stmt = (
        update(EndpointDescriptor)
        .where(
            EndpointDescriptor.product == scope.product,
            EndpointDescriptor.version == scope.version,
            EndpointDescriptor.impl_id == scope.impl_id,
            EndpointDescriptor.source_kind == "ingested",
            EndpointDescriptor.method.in_(sorted(READ_HTTP_METHODS)),
            EndpointDescriptor.is_enabled.is_(False),
        )
        .values(is_enabled=True)
    )
    if scope.tenant_id is None:
        stmt = stmt.where(EndpointDescriptor.tenant_id.is_(None))
    else:
        stmt = stmt.where(EndpointDescriptor.tenant_id == scope.tenant_id)
    result: CursorResult[Any] = await session.execute(stmt)  # type: ignore[assignment]
    rowcount = result.rowcount if result.rowcount is not None else 0
    return max(rowcount, 0)


# ---------------------------------------------------------------------------
# edit_op phase helpers (split from ReviewService.edit_op for the
# per-function size budget — same rationale as this module's header)
# ---------------------------------------------------------------------------


def validate_edit_op_args(
    *,
    custom_description: str | None,
    safety_level: str | None,
    requires_approval: bool | None,
    is_enabled: bool | None,
    llm_instructions: dict[str, object] | None,
) -> None:
    """Reject an ``edit_op`` call that edits nothing or names a bad enum.

    Raises :class:`ValueError` when every override is ``None`` (the
    PATCH would be a silent no-op) or when ``safety_level`` falls
    outside :data:`VALID_SAFETY_LEVELS`. Mirrors the checks the REST
    body schema enforces, for callers that reach the service directly
    (MCP tools, tests).
    """
    if (
        custom_description is None
        and safety_level is None
        and requires_approval is None
        and is_enabled is None
        and llm_instructions is None
    ):
        raise ValueError(
            "edit_op requires at least one of custom_description, "
            "safety_level, requires_approval, is_enabled, llm_instructions",
        )
    if safety_level is not None and safety_level not in VALID_SAFETY_LEVELS:
        raise ValueError(
            f"safety_level {safety_level!r} not in {sorted(VALID_SAFETY_LEVELS)}",
        )


def apply_op_overrides(
    op_row: EndpointDescriptor,
    *,
    custom_description: str | None,
    safety_level: str | None,
    requires_approval: bool | None,
    is_enabled: bool | None,
    llm_instructions: dict[str, object] | None,
) -> list[str]:
    """Set every non-``None`` override on *op_row*; return the field names set.

    Pure PATCH application — ``None`` means "leave unchanged" (the
    omitted-vs-null distinction is resolved by the callers before the
    service layer). The returned list feeds the audit payload's
    ``fields_updated`` key verbatim.
    """
    fields_updated: list[str] = []
    if custom_description is not None:
        op_row.custom_description = custom_description
        fields_updated.append("custom_description")
    if safety_level is not None:
        op_row.safety_level = safety_level
        fields_updated.append("safety_level")
    if requires_approval is not None:
        op_row.requires_approval = requires_approval
        fields_updated.append("requires_approval")
    if is_enabled is not None:
        op_row.is_enabled = is_enabled
        fields_updated.append("is_enabled")
    if llm_instructions is not None:
        op_row.llm_instructions = llm_instructions
        fields_updated.append("llm_instructions")
    return fields_updated


# ---------------------------------------------------------------------------
# Enable-time advisories (G0.23-T4 #1630)
# ---------------------------------------------------------------------------


def enable_time_auto_shim_warnings(
    connector_id: str,
    op_id: str,
    scope: ConnectorScope,
) -> list[EditOpWarning]:
    """Build the enable-time connector-tier advisory for an op enable, if due.

    Called by :meth:`ReviewService.edit_op` after a successful
    ``is_enabled=True`` write. Replays the production resolver for this
    op's ``(product, version)`` line and emits one advisory keyed on the
    resolved connector's
    :data:`~meho_backplane.connectors.base.ShimKind` tier:

    * **bare** (:class:`GenericRestConnector` auto-shim) — the enable is
      a guaranteed dispatch dead end (``connector_unsupported`` /
      ``cause='unreplaced_auto_shim'``, G0.23-T1 #1627); the advisory
      names the missing per-product subclass with the same remediation
      phrasing the dispatch-time error uses, so the proactive and
      reactive surfaces read as one story.
    * **profiled**
      (:class:`~meho_backplane.connectors.profiled.ProfiledRestConnector`)
      — the connector IS dispatchable, so this is not a dead end. But
      stamping its :class:`ExecutionProfile` deliberately did not
      auto-enable dispatch (the review gate is the interlock, G0.28-T5
      #1971); this ``is_enabled=True`` write is the operator clearing
      that gate. The advisory confirms that the enable — not the stamp —
      made the op callable.

    Returns ``[]`` on the clean path (hand-rolled connector, resolver
    miss, resolver tie). Never raises: the probe decorates the write,
    it must not break it. The two tiers are mutually exclusive (the
    resolver lands on exactly one class), so at most one advisory is
    returned. The two branches are built by dedicated helpers
    (:func:`_bare_shim_warning` / :func:`_profiled_unreviewed_warning`)
    to keep this dispatcher under the size budget.
    """
    shim_class = resolved_auto_shim_class(product=scope.product, version=scope.version)
    if shim_class is not None:
        return [_bare_shim_warning(connector_id, op_id, scope, shim_class)]

    profiled_class = resolved_profiled_connector_class(product=scope.product, version=scope.version)
    if profiled_class is not None:
        return [_profiled_unreviewed_warning(connector_id, op_id, scope, profiled_class)]

    return []


def _bare_shim_warning(
    connector_id: str,
    op_id: str,
    scope: ConnectorScope,
    shim_class: str,
) -> EditOpWarning:
    """Build the ``unreplaced_auto_shim`` dead-end advisory for a bare-shim enable."""
    _log.info(
        "edit_op_auto_shim_warning",
        connector_id=connector_id,
        op_id=op_id,
        product=scope.product,
        version=scope.version,
        impl_id=scope.impl_id,
        connector_class=shim_class,
    )
    message = (
        f"is_enabled=True was applied to op {op_id!r} on connector "
        f"{connector_id!r}, but its resolved connector ({shim_class}) is the "
        f"auto-registered ingest shim, which cannot authenticate or execute "
        f"against the upstream -- dispatching this op will fail with "
        f"connector_unsupported (cause=unreplaced_auto_shim). Register the "
        f"hand-rolled per-product Connector subclass for "
        f"({scope.product!r}, {scope.version!r}, {scope.impl_id!r}) and "
        f"redeploy before dispatching this connector's ops -- re-ingesting "
        f"the spec will NOT replace the shim. See "
        f"docs/codebase/spec-ingestion.md for the auto-shim lifecycle."
    )
    return EditOpWarning(
        code="unreplaced_auto_shim",
        connector_class=shim_class,
        message=message,
    )


def _profiled_unreviewed_warning(
    connector_id: str,
    op_id: str,
    scope: ConnectorScope,
    profiled_class: str,
) -> EditOpWarning:
    """Build the ``profiled_but_unreviewed`` gate-clearance advisory for a profiled enable."""
    _log.info(
        "edit_op_profiled_but_unreviewed_warning",
        connector_id=connector_id,
        op_id=op_id,
        product=scope.product,
        version=scope.version,
        impl_id=scope.impl_id,
        connector_class=profiled_class,
    )
    message = (
        f"is_enabled=True was applied to op {op_id!r} on connector "
        f"{connector_id!r}, whose resolved connector ({profiled_class}) is a "
        f"profiled REST connector backed by a reviewed ExecutionProfile. "
        f"Stamping that profile made the connector dispatchable but did NOT "
        f"auto-enable this op -- this enable is what cleared the review gate "
        f"and made the op callable by agents. Confirm the op was vetted "
        f"before this enable. See docs/codebase/spec-ingestion.md for the "
        f"profile review-gate lifecycle."
    )
    return EditOpWarning(
        code="profiled_but_unreviewed",
        connector_class=profiled_class,
        message=message,
    )
