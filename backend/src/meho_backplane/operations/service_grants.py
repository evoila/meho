# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Standing scoped auto-approval grants for service principals (#3151 / #3152).

Two layers over the :class:`~meho_backplane.db.models.ServicePrincipalGrant`
table:

* **CRUD service** — :class:`ServicePrincipalGrantService`, the single code
  path the operator-only REST surface
  (:mod:`meho_backplane.api.v1.service_grants`) dispatches through. Create
  IS the review (``reason`` required, deny-by-default absent a match, no
  wildcards, delete-shaped ops refused), list, and revoke (soft-delete).

* **Enforcement** — :func:`consult_and_record_grant`, called by the
  non-agent policy gate
  (:func:`meho_backplane.operations._validate._non_agent_verdict`) for a
  **service** principal whose op would otherwise park. It looks up a live
  matching grant and, when one exists, records the use in the **approvals
  audit ledger** ("auto-granted by standing grant ``<id>``") with the same
  ``method='APPROVAL'`` / ``path='approval.decision'`` shape a human
  approval decision writes — so a grant use is as visible on the ledger as
  a human clicking Approve — then returns the grant id so the gate can
  clear.

Enforcement is **service-principal-only** (mirroring how
:class:`AgentPermission` is consulted only for ``principal_kind=agent``):
a human ``USER`` operator keeps the default-allow + queue-on-approval
contract, and agents use the agent-permission model.

Delete-shaped guardrail
-----------------------

A grant is the *floor* of what runs unattended, never a bypass of a
modeled destructive gate, so :meth:`ServicePrincipalGrantService.create`
refuses delete-shaped ops: op ids matching a configured pattern set
(``Settings.service_grant_delete_shaped_patterns`` — at minimum
``DELETE:*`` raw ops plus ``*.delete`` / ``*.destroy`` / ``*.remove`` /
``*.purge`` typed ops) and, best-effort when a descriptor resolves,
anything whose descriptor carries ``method='DELETE'`` or a ``destructive``
tag.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import Any

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, ServicePrincipalGrant
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate, ServiceGrantRead

__all__ = [
    "GrantValidationError",
    "ServicePrincipalGrantService",
    "consult_and_record_grant",
    "find_live_grant",
]

_log = structlog.get_logger(__name__)

#: Default paging cap for :meth:`ServicePrincipalGrantService.list_`.
DEFAULT_LIST_LIMIT: int = 100

#: Synthetic audit fields mirroring an ``approval.decision`` row so a grant
#: use is queryable identically to a human decision (see
#: :func:`~meho_backplane.operations.approval_queue._write_audit_row`).
_GRANT_USE_METHOD: str = "APPROVAL"
_GRANT_USE_PATH: str = "approval.decision"
_GRANT_USE_STATUS_CODE: int = 200


class GrantValidationError(Exception):
    """Raised for semantic validation failures on grant creation.

    Covers: a wildcard in ``op_id`` / ``connector_id`` / ``principal_sub``;
    a delete-shaped op (never grantable); a past / naive ``expires_at``; or
    a duplicate active grant for the same fully-scoped key. The REST route
    maps this to HTTP 422.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Create-time validation helpers
# ---------------------------------------------------------------------------


def _reject_wildcards(payload: ServiceGrantCreate) -> None:
    """Refuse any glob metacharacter in the exact-scope fields (#3151).

    Creating a grant is the operator's explicit per-op review, so
    ``op_id``, ``connector_id``, and ``principal_sub`` must each name one
    exact value — a ``*`` / ``?`` would silently widen the unattended
    surface past what the operator reviewed.
    """
    for field, value in (
        ("op_id", payload.op_id),
        ("connector_id", payload.connector_id),
        ("principal_sub", payload.principal_sub),
    ):
        if "*" in value or "?" in value:
            raise GrantValidationError(
                f"{field} must be an exact value; wildcards are not permitted "
                f"(got {value!r}). A standing grant is the operator's explicit "
                "per-op review, not a pattern."
            )


def _validate_expires_at(expires_at: datetime | None) -> None:
    """Raise :exc:`GrantValidationError` when *expires_at* is naive or past."""
    if expires_at is None:
        return
    if expires_at.tzinfo is None:
        raise GrantValidationError("expires_at must be a timezone-aware datetime (UTC preferred)")
    if expires_at <= datetime.now(UTC):
        raise GrantValidationError(
            f"expires_at {expires_at.isoformat()} is in the past; "
            "a time-bounded grant must expire in the future"
        )


def _delete_shaped_reason_by_pattern(op_id: str, patterns: tuple[str, ...]) -> str | None:
    """Return a refusal reason if *op_id* matches a configured delete-shaped glob.

    Case-sensitive ``fnmatchcase`` over the exact op id — raw HTTP ops are
    upper-cased (``DELETE:/path``) and typed ops are dotted lower-case
    (``vault.sys.policy.delete``), so the default pattern set is spelled to
    match both without case folding.
    """
    for pattern in patterns:
        if fnmatchcase(op_id, pattern):
            return (
                f"op {op_id!r} is delete-shaped (matches configured pattern "
                f"{pattern!r}); delete-shaped operations are never grantable — "
                "a standing grant is the floor of what runs unattended, not a "
                "bypass of a destructive gate"
            )
    return None


def _delete_shaped_reason_by_descriptor(descriptor: EndpointDescriptor) -> str | None:
    """Return a refusal reason if the resolved descriptor marks destruction.

    Best-effort second check (only when a descriptor resolves): the HTTP
    ``DELETE`` verb, or a hand-authored ``destructive`` tag on a typed op.
    """
    method = (descriptor.method or "").upper()
    if method == "DELETE":
        return (
            f"op {descriptor.op_id!r} is a DELETE operation; delete-shaped "
            "operations are never grantable"
        )
    tags = descriptor.tags or []
    if "destructive" in tags:
        return (
            f"op {descriptor.op_id!r} carries the 'destructive' tag; "
            "delete-shaped operations are never grantable"
        )
    return None


async def _resolve_descriptor_for_classification(
    tenant_id: uuid.UUID,
    connector_id: str,
    op_id: str,
) -> EndpointDescriptor | None:
    """Best-effort descriptor lookup for the delete-shaped tag/method check.

    Returns ``None`` (skip the descriptor-level check, keep the pattern
    check authoritative) when ``connector_id`` does not parse or no
    descriptor resolves — a grant must not be blocked purely on ingestion
    timing / version drift.
    """
    from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id

    product, version, impl_id = parse_connector_id(connector_id)
    return await lookup_descriptor(
        tenant_id=tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
    )


# ---------------------------------------------------------------------------
# CRUD service (operator-only REST surface)
# ---------------------------------------------------------------------------


class ServicePrincipalGrantService:
    """Tenant-scoped CRUD over :class:`~meho_backplane.db.models.ServicePrincipalGrant`.

    Stateless and async; each public method opens its own session, commits,
    and closes (mirrors :class:`~meho_backplane.agents.grants.AgentGrantService`).
    Callers own the ``require_role(TenantRole.OPERATOR)`` gate — the service
    does not enforce roles.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger()

    async def create(
        self,
        tenant_id: uuid.UUID,
        created_by_sub: str,
        payload: ServiceGrantCreate,
    ) -> ServiceGrantRead:
        """Create one standing grant row after the full create-time review.

        Refuses wildcards, delete-shaped ops, and past/naive expiries;
        raises :exc:`GrantValidationError` (→ 422) on any of those or on a
        duplicate active grant for the same fully-scoped key.
        """
        from meho_backplane.settings import get_settings

        _reject_wildcards(payload)
        _validate_expires_at(payload.expires_at)

        pattern_reason = _delete_shaped_reason_by_pattern(
            payload.op_id, get_settings().service_grant_delete_shaped_patterns
        )
        if pattern_reason is not None:
            raise GrantValidationError(pattern_reason)
        descriptor = await _resolve_descriptor_for_classification(
            tenant_id, payload.connector_id, payload.op_id
        )
        if descriptor is not None:
            descriptor_reason = _delete_shaped_reason_by_descriptor(descriptor)
            if descriptor_reason is not None:
                raise GrantValidationError(descriptor_reason)

        row = ServicePrincipalGrant(
            tenant_id=tenant_id,
            principal_sub=payload.principal_sub,
            op_id=payload.op_id,
            connector_id=payload.connector_id,
            target_id=payload.target_id,
            reason=payload.reason,
            created_by_sub=created_by_sub,
            expires_at=payload.expires_at,
        )
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            session.add(row)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                raise GrantValidationError(
                    f"an active grant for principal {payload.principal_sub!r} on "
                    f"op {payload.op_id!r} / connector {payload.connector_id!r} / "
                    f"target {payload.target_id} already exists; revoke it first"
                ) from exc
            await session.refresh(row)
            entry = ServiceGrantRead.model_validate(row)
            await session.commit()

        self._log.info(
            "service_grant_created",
            tenant_id=str(tenant_id),
            grant_id=str(entry.id),
            principal_sub=payload.principal_sub,
            op_id=payload.op_id,
            connector_id=payload.connector_id,
            target_id=str(payload.target_id) if payload.target_id else None,
            created_by_sub=created_by_sub,
            expires_at=payload.expires_at.isoformat() if payload.expires_at else None,
        )
        return entry

    async def revoke(
        self,
        tenant_id: uuid.UUID,
        grant_id: uuid.UUID,
        revoked_by_sub: str,
    ) -> bool:
        """Soft-delete the grant matching ``(tenant_id, grant_id)``.

        Stamps ``revoked_at`` / ``revoked_by_sub`` on a still-live row
        (the row is retained for history). Returns ``True`` when a live row
        was revoked, ``False`` when none matched (absent, already revoked,
        or cross-tenant — the ``tenant_id`` predicate hides other tenants).
        """
        now = datetime.now(UTC)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                update(ServicePrincipalGrant)
                .where(
                    ServicePrincipalGrant.tenant_id == tenant_id,
                    ServicePrincipalGrant.id == grant_id,
                    ServicePrincipalGrant.revoked_at.is_(None),
                )
                .values(revoked_at=now, revoked_by_sub=revoked_by_sub)
                .returning(ServicePrincipalGrant.id)
            )
            revoked = result.scalar_one_or_none() is not None
            await session.commit()

        self._log.info(
            "service_grant_revoked",
            tenant_id=str(tenant_id),
            grant_id=str(grant_id),
            revoked_by_sub=revoked_by_sub,
            revoked=revoked,
        )
        return revoked

    async def get(
        self,
        tenant_id: uuid.UUID,
        grant_id: uuid.UUID,
    ) -> ServiceGrantRead | None:
        """Fetch one grant by ``(tenant_id, grant_id)``; ``None`` if absent."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(ServicePrincipalGrant).where(
                    ServicePrincipalGrant.tenant_id == tenant_id,
                    ServicePrincipalGrant.id == grant_id,
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return ServiceGrantRead.model_validate(row)

    async def list_(
        self,
        tenant_id: uuid.UUID,
        *,
        principal_sub: str | None = None,
        include_revoked: bool = False,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[ServiceGrantRead]:
        """Return up to *limit* grants for *tenant_id*, newest-first.

        ``include_revoked=False`` (default) hides soft-deleted rows;
        ``True`` returns the full history (revoked rows included). Expired
        rows are always returned (they are history, not deletions).
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0; got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0; got {offset}")
        if limit == 0:
            return []

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = (
                select(ServicePrincipalGrant)
                .where(ServicePrincipalGrant.tenant_id == tenant_id)
                .order_by(ServicePrincipalGrant.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            if principal_sub is not None:
                stmt = stmt.where(ServicePrincipalGrant.principal_sub == principal_sub)
            if not include_revoked:
                stmt = stmt.where(ServicePrincipalGrant.revoked_at.is_(None))
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [ServiceGrantRead.model_validate(row) for row in rows]


# ---------------------------------------------------------------------------
# Enforcement (dispatch-time)
# ---------------------------------------------------------------------------


def _target_uuid(target: Any) -> uuid.UUID | None:
    """Extract a target's UUID id, or ``None`` for a targetless op."""
    raw = getattr(target, "id", None) if target is not None else None
    return raw if isinstance(raw, uuid.UUID) else None


async def find_live_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    op_id: str,
    connector_id: str,
    target_id: uuid.UUID | None,
    now: datetime | None = None,
) -> ServicePrincipalGrant | None:
    """Return the live grant covering this exact dispatch, or ``None``.

    Exact match on every scope — ``target_id`` too, including the
    targetless (``NULL``) case — with revocation and expiry both honoured
    **at dispatch time**: ``revoked_at IS NULL`` and (``expires_at IS
    NULL`` or ``expires_at > now``). No wildcard widening.
    """
    cutoff = now or datetime.now(UTC)
    stmt = (
        select(ServicePrincipalGrant)
        .where(
            ServicePrincipalGrant.tenant_id == tenant_id,
            ServicePrincipalGrant.principal_sub == principal_sub,
            ServicePrincipalGrant.op_id == op_id,
            ServicePrincipalGrant.connector_id == connector_id,
            ServicePrincipalGrant.revoked_at.is_(None),
            or_(
                ServicePrincipalGrant.expires_at.is_(None),
                ServicePrincipalGrant.expires_at > cutoff,
            ),
        )
        .limit(1)
    )
    if target_id is None:
        stmt = stmt.where(ServicePrincipalGrant.target_id.is_(None))
    else:
        stmt = stmt.where(ServicePrincipalGrant.target_id == target_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def consult_and_record_grant(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str,
) -> uuid.UUID | None:
    """Look up a live standing grant and, on a hit, record its use.

    Returns the grant id when a live grant authorises this dispatch (the
    caller then clears the gate to ``AUTO_EXECUTE``), or ``None`` when no
    grant matches (the caller parks the op). On a hit it writes the
    grant-use audit row (``approval.decision`` shape) in its own committed
    transaction and publishes a fail-open broadcast — same visibility as a
    human approval decision — before returning.
    """
    target_id = _target_uuid(target)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        grant = await find_live_grant(
            session,
            tenant_id=operator.tenant_id,
            principal_sub=operator.sub,
            op_id=descriptor.op_id,
            connector_id=connector_id,
            target_id=target_id,
        )
    if grant is None:
        return None

    audit_id = await _record_grant_use(
        operator=operator,
        grant=grant,
        connector_id=connector_id,
        target_id=target_id,
    )
    await _publish_grant_use_event(operator=operator, grant=grant, audit_id=audit_id)
    _log.info(
        "service_grant_auto_approved",
        grant_id=str(grant.id),
        op_id=grant.op_id,
        connector_id=connector_id,
        principal_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
    )
    return grant.id


async def _record_grant_use(
    *,
    operator: Operator,
    grant: ServicePrincipalGrant,
    connector_id: str,
    target_id: uuid.UUID | None,
) -> uuid.UUID:
    """Write one ``approval.decision`` audit row for a standing-grant use.

    Mirrors :func:`~meho_backplane.operations.approval_queue._write_audit_row`
    (``method='APPROVAL'``, ``path='approval.decision'``, status ``200``) so
    the row is indistinguishable in the ledger from a human approval,
    except the ``reviewed_by`` reads ``grant:<id>`` and the payload carries
    ``decision='auto-approved'`` + ``grant_id``. Written in its own
    committed transaction so the authorisation is durable before the op
    runs (the synchronous-audit invariant).
    """
    from meho_backplane.operations._audit import resolve_agent_session_id, work_ref_var

    audit_id = uuid.uuid4()
    reason = f"auto-granted by standing grant {grant.id}"
    payload: dict[str, Any] = {
        "decision": "auto-approved",
        "reviewed_by": f"grant:{grant.id}",
        "grant_id": str(grant.id),
        "op_id": grant.op_id,
        "connector_id": connector_id,
        "principal_sub": operator.sub,
        "reason": reason,
        "result_status": "decision",
    }
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = AuditLog(
            id=audit_id,
            occurred_at=datetime.now(UTC),
            operator_sub=operator.sub,
            tenant_id=operator.tenant_id,
            target_id=target_id,
            agent_session_id=resolve_agent_session_id(),
            method=_GRANT_USE_METHOD,
            path=_GRANT_USE_PATH,
            status_code=_GRANT_USE_STATUS_CODE,
            request_id=None,
            duration_ms=Decimal("0.00"),
            payload=payload,
            work_ref=work_ref_var.get(),
        )
        session.add(row)
        await session.commit()
    return audit_id


async def _publish_grant_use_event(
    *,
    operator: Operator,
    grant: ServicePrincipalGrant,
    audit_id: uuid.UUID,
) -> None:
    """Publish a fail-open ``approval.auto_approved`` broadcast for the grant use.

    Parity with a human decision (which broadcasts ``approval.approved``).
    Fail-open: a broadcast outage never blocks the durable grant use — the
    audit row is the source of truth.
    """
    try:
        from meho_backplane.broadcast.events import BroadcastEvent, classify_op
        from meho_backplane.broadcast.publisher import publish_event
        from meho_backplane.operations._audit import resolve_broadcast_lineage

        op_id = "approval.auto_approved"
        lineage = resolve_broadcast_lineage()
        event = BroadcastEvent(
            event_id=uuid.uuid4(),
            ts=datetime.now(UTC),
            tenant_id=operator.tenant_id,
            principal_sub=operator.sub,
            op_id=op_id,
            op_class=classify_op(op_id),
            result_status="ok",
            audit_id=audit_id,
            payload={
                "op_class": classify_op(op_id),
                "result_status": "ok",
                "decision": "auto-approved",
                "grant_id": str(grant.id),
                "connector_id": grant.connector_id,
                "approval_op_id": grant.op_id,
            },
            actor_sub=lineage.actor_sub,
            agent_session_id=lineage.agent_session_id,
            work_ref=lineage.work_ref,
        )
        await publish_event(event)
    except Exception:
        _log.exception(
            "service_grant_broadcast_failed",
            grant_id=str(grant.id),
            op_id=grant.op_id,
        )
