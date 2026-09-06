# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: cohesive CRUD + enforcement unit (create-time review,
# lookup, dispatch-time consult) that predates this change at ~650 lines;
# splitting the grant service from its enforcement path is out of scope for a
# surgical bug fix.

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

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import Any

import structlog
from sqlalchemy import func, or_, select, update
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
    "count_live_grants_for_principal",
    "delete_shaped_refusal_reason",
    "find_live_grant",
    "null_target_only_park_hint",
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


#: A well-formed HTTP-style ``op_id`` ending in a **literal** query string.
#:
#: Several governed vCenter operations carry a ``?action=<verb>`` (or a
#: multi-param) query string as a literal part of their canonical op id — the
#: ``vm.power`` / ``vm.deploy_from_library`` / host-software composite sub-ops
#: (``POST:/vcenter/vm/{vm}/power?action=start``,
#: ``POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy``,
#: ``POST:/esx/settings/hosts/{host}/software?action=apply&vmw-task=true``) and
#: every ``?action=`` endpoint the ingest parser emits. That ``?`` is part of
#: the exact op id, not a glob — grant matching is exact string equality (see
#: :func:`find_live_grant`), so the literal is safe. This matches exactly one
#: ``?`` (no ``*``), followed by one or more ``key=value`` pairs joined by
#: ``&`` (keys / values are ``[A-Za-z0-9_.-]``, covering ``vmw-task``).
_LITERAL_QUERY_STRING_OP_ID: re.Pattern[str] = re.compile(
    r"^[A-Z]+:/[^?*]+\?[A-Za-z0-9_.-]+=[A-Za-z0-9_.-]+"
    r"(?:&[A-Za-z0-9_.-]+=[A-Za-z0-9_.-]+)*$"
)


def _reject_wildcards(payload: ServiceGrantCreate) -> None:
    """Refuse glob metacharacters in the exact-scope fields (#3151).

    Creating a grant is the operator's explicit per-op review, so
    ``op_id``, ``connector_id``, and ``principal_sub`` must each name one
    exact value — a ``*`` (or a bare ``?``) would silently widen the
    unattended surface past what the operator reviewed. ``*`` is never
    permitted anywhere.

    The one exception is a **literal** query string on an HTTP-style
    ``op_id`` (e.g. ``POST:/vcenter/vm/{vm}/power?action=start``): several
    governed vCenter ops carry a ``?action=`` key as a literal part of their
    exact op id, and grant matching is exact string equality, so such an op
    id is accepted verbatim (see :data:`_LITERAL_QUERY_STRING_OP_ID`). A
    ``?`` anywhere else — a malformed op id, or a ``connector_id`` /
    ``principal_sub`` — is still a rejected glob.
    """

    def _wildcard_error(field: str, value: str) -> GrantValidationError:
        return GrantValidationError(
            f"{field} must be an exact value; wildcards are not permitted "
            f"(got {value!r}). A standing grant is the operator's explicit "
            "per-op review, not a pattern."
        )

    for field, value in (
        ("op_id", payload.op_id),
        ("connector_id", payload.connector_id),
        ("principal_sub", payload.principal_sub),
    ):
        if "*" in value:
            raise _wildcard_error(field, value)
        if "?" in value:
            if field == "op_id" and _LITERAL_QUERY_STRING_OP_ID.match(value):
                continue
            if field == "op_id":
                raise GrantValidationError(
                    "op_id may contain a literal query string like "
                    f"'?action=start' (got {value!r}); glob wildcards are not "
                    "permitted."
                )
            raise _wildcard_error(field, value)


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


def delete_shaped_refusal_reason(op_id: str, patterns: tuple[str, ...]) -> str | None:
    """Public alias for the pattern-based delete-shaped classifier (#3349).

    The governed-subop discovery surface
    (:func:`meho_backplane.operations.governed_subops.classify_subop_grantability`)
    flags a child op un-grantable through this single-sourced rule — the same
    one :meth:`ServicePrincipalGrantService.create` refuses on — so a rollback
    / delete leg is flagged on discovery exactly as it would be refused at
    grant-create time.
    """
    return _delete_shaped_reason_by_pattern(op_id, patterns)


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

    Best-effort second check (only when a descriptor resolves): the
    ``destructive`` safety tier (#3183), the HTTP ``DELETE`` verb, or a
    hand-authored ``destructive`` tag on a typed op. The three inputs
    single-source the delete-shaped classification — ``DELETE:`` verbs and
    ``destructive``-tagged typed ops are the shapes an operator resolves
    into the ``destructive`` tier, so once the tier is set it is itself the
    authoritative signal.
    """
    if descriptor.safety_level == "destructive":
        return (
            f"op {descriptor.op_id!r} is safety_level=destructive; delete-shaped "
            "operations are never grantable — a standing grant is the floor of "
            "what runs unattended, not a bypass of a destructive gate"
        )
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
        duplicate active grant for the same fully-scoped key (a duplicate
        target selector included, #3349).
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

        is_selector = payload.target_product is not None or payload.target_name_pattern is not None

        row = ServicePrincipalGrant(
            tenant_id=tenant_id,
            principal_sub=payload.principal_sub,
            op_id=payload.op_id,
            connector_id=payload.connector_id,
            target_id=payload.target_id,
            target_product=payload.target_product,
            target_name_pattern=payload.target_name_pattern,
            reason=payload.reason,
            created_by_sub=created_by_sub,
            expires_at=payload.expires_at,
        )
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            # Selector grants are not covered by a DB partial unique index
            # (the nullable selector columns make a portable NULL-safe unique
            # index awkward), so enforce "at most one active selector per
            # (key, product, pattern)" in the CRUD layer to preserve the
            # same duplicate-refusal contract the targeted / targetless
            # indexes give.
            if is_selector:
                await self._reject_duplicate_selector(session, tenant_id, payload)
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
            target_product=payload.target_product,
            target_name_pattern=payload.target_name_pattern,
            created_by_sub=created_by_sub,
            expires_at=payload.expires_at.isoformat() if payload.expires_at else None,
        )
        return entry

    @staticmethod
    async def _reject_duplicate_selector(
        session: AsyncSession,
        tenant_id: uuid.UUID,
        payload: ServiceGrantCreate,
    ) -> None:
        """Raise if a live selector grant with the identical scope exists (#3349).

        The CRUD-layer twin of the ``uq_service_principal_grant_*`` partial
        indexes for selector grants (which those indexes deliberately do not
        cover). Matches an active (``revoked_at IS NULL``) selector row on the
        full key plus the exact ``(target_product, target_name_pattern)`` pair.
        """
        existing = await session.execute(
            select(ServicePrincipalGrant.id)
            .where(
                ServicePrincipalGrant.tenant_id == tenant_id,
                ServicePrincipalGrant.principal_sub == payload.principal_sub,
                ServicePrincipalGrant.op_id == payload.op_id,
                ServicePrincipalGrant.connector_id == payload.connector_id,
                ServicePrincipalGrant.target_id.is_(None),
                # NULL-safe equality (``IS NOT DISTINCT FROM`` on Postgres,
                # ``IS`` on SQLite) so a NULL selector column compares equal
                # to NULL — a selector with only a product set collides with
                # another product-only selector for the same product.
                ServicePrincipalGrant.target_product.is_not_distinct_from(payload.target_product),
                ServicePrincipalGrant.target_name_pattern.is_not_distinct_from(
                    payload.target_name_pattern
                ),
                ServicePrincipalGrant.revoked_at.is_(None),
            )
            .limit(1)
        )
        if existing.first() is not None:
            raise GrantValidationError(
                f"an active selector grant for principal {payload.principal_sub!r} on "
                f"op {payload.op_id!r} / connector {payload.connector_id!r} / "
                f"target_product={payload.target_product!r} / "
                f"target_name_pattern={payload.target_name_pattern!r} already exists; "
                "revoke it first"
            )

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


def _live_grant_scope_predicates(
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    op_id: str,
    connector_id: str,
    cutoff: datetime,
) -> tuple[Any, ...]:
    """The scope + liveness predicates shared by every grant lookup.

    Exact on ``(tenant, principal_sub, op_id, connector_id)`` with
    revocation and expiry honoured at dispatch time (``revoked_at IS NULL``
    and (``expires_at IS NULL`` or ``expires_at > now``)). The target
    dimension (exact id / selector / targetless) is applied by the caller.
    """
    return (
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


def _selector_matches(
    grant: ServicePrincipalGrant,
    *,
    target_product: str | None,
    target_name: str | None,
) -> bool:
    """Whether a selector grant's fingerprint predicate covers this target.

    ``target_product`` (when set) is matched exactly; ``target_name_pattern``
    (when set) is an ``fnmatchcase`` glob over the target name. A dimension
    left NULL on the grant is a "don't care" — but at least one is non-NULL
    (the caller only passes selector grants), so the grant is never a blanket
    any-target match by accident.
    """
    product_ok = grant.target_product is None or grant.target_product == target_product
    name_ok = grant.target_name_pattern is None or (
        target_name is not None and fnmatchcase(target_name, grant.target_name_pattern)
    )
    return product_ok and name_ok


async def find_live_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    op_id: str,
    connector_id: str,
    target_id: uuid.UUID | None,
    target_product: str | None = None,
    target_name: str | None = None,
    now: datetime | None = None,
) -> ServicePrincipalGrant | None:
    """Return the live grant covering this dispatch, or ``None``.

    Exact match on ``(tenant, principal_sub, op_id, connector_id)`` with
    revocation and expiry both honoured **at dispatch time**. The target
    dimension resolves in this order:

    * **targetless dispatch** (``target_id is None``) — matches a *pure*
      targetless grant only (``target_id`` and both selector columns NULL).
      A selector needs a target fingerprint to match, so it never covers a
      targetless dispatch; and a null ``target_id`` is still **not** a
      wildcard (the #3349 loud-hint case).
    * **target-scoped dispatch** — a concrete-``target_id`` grant is tried
      first (the exact, pre-#3349 match). Absent that, a **selector** grant
      (#3349) matches when the dispatch's ``target_product`` / ``target_name``
      satisfy its predicate (``product`` exact + ``name`` ``fnmatch`` glob).
      The concrete match is preferred so a specific grant always wins over a
      broad selector.
    """
    cutoff = now or datetime.now(UTC)
    scope = _live_grant_scope_predicates(
        tenant_id=tenant_id,
        principal_sub=principal_sub,
        op_id=op_id,
        connector_id=connector_id,
        cutoff=cutoff,
    )

    if target_id is None:
        stmt = (
            select(ServicePrincipalGrant)
            .where(
                *scope,
                ServicePrincipalGrant.target_id.is_(None),
                ServicePrincipalGrant.target_product.is_(None),
                ServicePrincipalGrant.target_name_pattern.is_(None),
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    # Target-scoped dispatch: prefer a concrete-target grant, then selectors.
    exact_stmt = (
        select(ServicePrincipalGrant)
        .where(*scope, ServicePrincipalGrant.target_id == target_id)
        .limit(1)
    )
    exact = (await session.execute(exact_stmt)).scalar_one_or_none()
    if exact is not None:
        return exact

    selector_stmt = select(ServicePrincipalGrant).where(
        *scope,
        ServicePrincipalGrant.target_id.is_(None),
        or_(
            ServicePrincipalGrant.target_product.isnot(None),
            ServicePrincipalGrant.target_name_pattern.isnot(None),
        ),
    )
    candidates = (await session.execute(selector_stmt)).scalars().all()
    for grant in candidates:
        if _selector_matches(grant, target_product=target_product, target_name=target_name):
            return grant
    return None


async def null_target_only_park_hint(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    op_id: str,
    connector_id: str,
    target_id: uuid.UUID | None,
    now: datetime | None = None,
) -> str | None:
    """Return the #3349 loud-hint when a target-scoped dispatch parks with
    only a *pure* null-target grant as a candidate.

    A grant created with ``target_id=null`` in the belief that null means
    "any target" never matches a target-scoped dispatch (null is matched
    literally, not as a wildcard). Absent a runtime signal that is a silent
    misfire, so when such a dispatch parks and the *only* live candidate for
    ``(principal_sub, op_id, connector_id)`` is a pure null-target grant, the
    gate appends this hint to the park reason. Returns ``None`` for a
    targetless dispatch (there is no mismatch to name) or when no null-target
    grant exists.

    Scoped to the park path only (already the slow path): the extra query
    never touches the auto-execute hot path.
    """
    if target_id is None:
        return None
    cutoff = now or datetime.now(UTC)
    stmt = (
        select(ServicePrincipalGrant.id)
        .where(
            *_live_grant_scope_predicates(
                tenant_id=tenant_id,
                principal_sub=principal_sub,
                op_id=op_id,
                connector_id=connector_id,
                cutoff=cutoff,
            ),
            ServicePrincipalGrant.target_id.is_(None),
            ServicePrincipalGrant.target_product.is_(None),
            ServicePrincipalGrant.target_name_pattern.is_(None),
        )
        .limit(1)
    )
    if (await session.execute(stmt)).first() is None:
        return None
    return (
        "a null-target standing grant does not match a target-scoped dispatch "
        "— null is matched literally, not as an any-target wildcard. Scope the "
        "grant to this target, or use a target selector (target_product / "
        "target_name_pattern) to authorise targets that do not exist yet (#3349)"
    )


async def count_live_grants_for_principal(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    now: datetime | None = None,
) -> int:
    """Count this principal's live standing grants across **all** scopes.

    Liveness matches :func:`find_live_grant` (``revoked_at IS NULL`` and
    (``expires_at IS NULL`` or ``expires_at > now``)) but ignores the
    op/connector/target scope — it answers only "does this ``sub`` hold any
    grant that *should* be evaluated". Used solely by the misclassification
    WARN in :func:`~meho_backplane.operations._validate._non_agent_verdict`
    (#3178): a non-service principal holding a live grant is almost
    certainly a service account whose token missed the service-account
    marker. Never on the auto-execute hot path.
    """
    cutoff = now or datetime.now(UTC)
    stmt = (
        select(func.count())
        .select_from(ServicePrincipalGrant)
        .where(
            ServicePrincipalGrant.tenant_id == tenant_id,
            ServicePrincipalGrant.principal_sub == principal_sub,
            ServicePrincipalGrant.revoked_at.is_(None),
            or_(
                ServicePrincipalGrant.expires_at.is_(None),
                ServicePrincipalGrant.expires_at > cutoff,
            ),
        )
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


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

    A ``destructive``-tier op (#3183) is refused here **before** any grant
    lookup: the tier is non-grantable, so even a stale grant row that
    predates the op's promotion into the tier can never auto-approve it.
    This is the dispatch-time twin of the create-time refusal in
    :func:`_delete_shaped_reason_by_descriptor`; together they make
    "a standing grant can never satisfy a destructive op" hold whether the
    grant is being created or consulted.
    """
    destructive_reason = _delete_shaped_reason_by_descriptor(descriptor)
    if destructive_reason is not None:
        _log.info(
            "service_grant_refused_delete_shaped",
            op_id=descriptor.op_id,
            connector_id=connector_id,
            safety_level=descriptor.safety_level,
            principal_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
            reason=destructive_reason,
        )
        return None

    target_id = _target_uuid(target)
    target_product = getattr(target, "product", None) if target is not None else None
    target_name = getattr(target, "name", None) if target is not None else None
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        grant = await find_live_grant(
            session,
            tenant_id=operator.tenant_id,
            principal_sub=operator.sub,
            op_id=descriptor.op_id,
            connector_id=connector_id,
            target_id=target_id,
            target_product=target_product if isinstance(target_product, str) else None,
            target_name=target_name if isinstance(target_name, str) else None,
        )
    if grant is None:
        return None

    matched_by_selector = grant.target_id is None and (
        grant.target_product is not None or grant.target_name_pattern is not None
    )
    audit_id = await _record_grant_use(
        operator=operator,
        grant=grant,
        connector_id=connector_id,
        target_id=target_id,
        matched_by_selector=matched_by_selector,
    )
    await _publish_grant_use_event(operator=operator, grant=grant, audit_id=audit_id)
    _log.info(
        "service_grant_auto_approved",
        grant_id=str(grant.id),
        op_id=grant.op_id,
        connector_id=connector_id,
        principal_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        matched_by_selector=matched_by_selector,
    )
    return grant.id


async def _record_grant_use(
    *,
    operator: Operator,
    grant: ServicePrincipalGrant,
    connector_id: str,
    target_id: uuid.UUID | None,
    matched_by_selector: bool = False,
) -> uuid.UUID:
    """Write one ``approval.decision`` audit row for a standing-grant use.

    Mirrors :func:`~meho_backplane.operations.approval_queue._write_audit_row`
    (``method='APPROVAL'``, ``path='approval.decision'``, status ``200``) so
    the row is indistinguishable in the ledger from a human approval,
    except the ``reviewed_by`` reads ``grant:<id>`` and the payload carries
    ``decision='auto-approved'`` + ``grant_id``. Written in its own
    committed transaction so the authorisation is durable before the op
    runs (the synchronous-audit invariant).

    When the grant matched via a target **selector** (#3349) the payload
    records ``matched_by='selector'`` plus the grant's selector predicate,
    so the auto-approval is visibly distinct on the ledger from a
    concrete-target / targetless match (the operator can see the op ran on a
    runtime-created target the selector authorised, not a pre-existing one).
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
        "matched_by": "selector" if matched_by_selector else "target",
    }
    if matched_by_selector:
        payload["target_selector"] = {
            "target_product": grant.target_product,
            "target_name_pattern": grant.target_name_pattern,
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
