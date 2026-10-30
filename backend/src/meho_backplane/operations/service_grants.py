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
  Every read flags **orphaned** grants (#3430): a concrete ``target_id``
  that no longer points at a live target (:func:`classify_orphaned_grants`).

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
refuses delete-shaped ops. Three signs on the op id or its stored row:

* a configured pattern (``Settings.service_grant_delete_shaped_patterns``
  — at minimum ``DELETE:*`` raw ops plus ``*.delete`` / ``*.destroy`` /
  ``*.remove`` / ``*.purge`` typed ops);
* a vendor action name that means delete (``Destroy``, ``Delete``,
  ``Remove``, ``Unregister`` — the word list the ingest parser uses, in
  :mod:`meho_backplane.delete_names`). VI-JSON exposes a VM destroy as
  ``POST:/VirtualMachine/{moId}/Destroy_Task``, so neither the HTTP method
  nor a pattern sees it. This sign does not depend on the pattern setting
  (#4632);
* best-effort when a descriptor resolves, the ``destructive`` safety tier,
  ``method='DELETE'`` or a ``destructive`` tag.

The action-name sign and the descriptor signs are checked again when a
grant is consulted at dispatch (:func:`consult_and_record_grant`), so a
grant row that already exists for such an op never lets it run.

Sandbox mode (#4622)
--------------------

On an instance in sandbox mode (:mod:`meho_backplane.sandbox_mode`,
``docs/decisions/sandbox-mode.md``) a tenant admin may create a **delete
grant**: a standing grant for an operation with a delete sign, or for a
delete step of a multi-step operation. Such a grant is stored in its own
table (:class:`~meho_backplane.db.models.SandboxDeleteGrant`), which an
older build and an instance with the mode off never read. It is made in
the web console (``from_console``), or, with the boot setting
``MEHO_SANDBOX_DELETE_GRANTS_FROM_API`` on (#5251), through REST by a
tenant admin. It expires within
:data:`~meho_backplane.sandbox_mode.SANDBOX_DELETE_GRANT_MAX_DAYS` days.
At dispatch, a ``destructive`` call under a delete grant still needs its
preview hash and a valid blast radius; both are checked before the
grant-use row is written, and the row stores them.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fnmatch import fnmatchcase
from glob import escape as glob_escape
from typing import Any, cast

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditEventClass,
    AuditLog,
    EndpointDescriptor,
    SandboxDeleteGrant,
    ServicePrincipalGrant,
    Target,
)
from meho_backplane.delete_names import destructive_action_name_in_op_id
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate, ServiceGrantRead
from meho_backplane.sandbox_mode import SANDBOX_DELETE_GRANT_MAX_DAYS, sandbox_mode_active

__all__ = [
    "DeleteGrantConsoleOnlyError",
    "DeleteGrantNeedsTenantAdminError",
    "GrantModel",
    "GrantValidationError",
    "ServicePrincipalGrantService",
    "classify_orphaned_grants",
    "consult_and_record_grant",
    "count_live_grants_for_principal",
    "delete_grant_reason",
    "delete_shaped_refusal_reason",
    "find_live_grant",
    "null_target_only_park_hint",
]

#: Either standing-grant table. The lookup, list and revoke code takes one.
GrantModel = type[ServicePrincipalGrant] | type[SandboxDeleteGrant]
#: A row of either table.
GrantRow = ServicePrincipalGrant | SandboxDeleteGrant

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


class DeleteGrantConsoleOnlyError(Exception):
    """A delete grant was requested from outside the web console (#4622).

    Raised by :meth:`ServicePrincipalGrantService.create` in sandbox mode
    when the op has a delete sign, the call did not come from the console
    route, and ``MEHO_SANDBOX_DELETE_GRANTS_FROM_API`` is off (#5251). The
    REST route maps this to HTTP 403 ``delete_grant_console_only``.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class DeleteGrantNeedsTenantAdminError(Exception):
    """A delete grant was requested through REST by a caller who is no tenant admin (#5251).

    Raised by :meth:`ServicePrincipalGrantService.create` in sandbox mode
    with ``MEHO_SANDBOX_DELETE_GRANTS_FROM_API`` on, when the op has a
    delete sign and the caller has only the ``operator`` role. The REST
    route maps this to HTTP 403 ``grants_require_tenant_admin``, the
    console's answer.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Create-time validation helpers
# ---------------------------------------------------------------------------


#: A well-formed HTTP-style ``op_id`` ending in a **literal** query string.
#:
#: Some ingested operations carry a ``?action=<verb>`` (or a multi-param)
#: query string as a literal part of their canonical op id. The ingest parser
#: keeps the query of such a path, so the op id looks like
#: ``POST:/items/{id}?action=start``. That ``?`` is part of the exact op id,
#: not a glob — grant matching is exact string equality (see
#: :func:`find_live_grant`), so the literal is safe. This matches exactly one
#: ``?`` (no ``*``), followed by one or more ``key=value`` pairs joined by
#: ``&`` (keys / values are ``[A-Za-z0-9_.-]``, so hyphenated keys fit).
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
    ``op_id`` (for example an op id ending in ``?action=start``): some
    ingested ops carry a ``?key=value`` suffix as a literal part of their
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


def _validate_delete_grant_expires_at(expires_at: datetime | None) -> None:
    """A delete grant must expire, within the sandbox limit (#4622)."""
    if expires_at is None:
        raise GrantValidationError(
            "a delete grant needs an expiry: set expires_at to a time within "
            f"{SANDBOX_DELETE_GRANT_MAX_DAYS} days. MEHO never renews a delete "
            "grant by itself; a person reviews and renews it."
        )
    latest = datetime.now(UTC) + timedelta(days=SANDBOX_DELETE_GRANT_MAX_DAYS)
    if expires_at > latest:
        raise GrantValidationError(
            f"a delete grant must expire within {SANDBOX_DELETE_GRANT_MAX_DAYS} days "
            f"(expires_at {expires_at.isoformat()} is later than "
            f"{latest.isoformat()})"
        )


def delete_grant_reason(
    op_id: str,
    patterns: tuple[str, ...],
    descriptor: EndpointDescriptor | None = None,
) -> str | None:
    """Return why *op_id* (and its row, when known) carries a delete sign.

    One rule for every reader: a configured name pattern, a vendor delete
    action name (#4632), and, when a descriptor is given, the ``destructive``
    safety level, the HTTP verb ``DELETE`` or the ``destructive`` tag.
    ``None`` when the op has no delete sign. In sandbox mode (#4622) an op
    with a delete sign needs a delete grant; elsewhere it can never be
    granted.
    """
    reason = delete_shaped_refusal_reason(op_id, patterns)
    if reason is None and descriptor is not None:
        reason = _delete_shaped_reason_by_descriptor(descriptor)
    return reason


def delete_shaped_refusal_reason(op_id: str, patterns: tuple[str, ...]) -> str | None:
    """Return why *op_id* is delete-shaped by its id alone, or ``None`` (#3349).

    Two signs need no stored row: a configured pattern and a vendor action
    name that means delete (#4632). The governed-subop discovery surface
    (:func:`meho_backplane.operations.governed_subops.classify_subop_grantability`)
    flags a child op un-grantable through this single-sourced rule — the same
    one :meth:`ServicePrincipalGrantService.create` refuses on — so a rollback
    / delete leg is flagged on discovery exactly as it would be refused at
    grant-create time.
    """
    pattern_reason = _delete_shaped_reason_by_pattern(op_id, patterns)
    if pattern_reason is not None:
        return pattern_reason
    return _delete_shaped_reason_by_action_name(op_id)


def _delete_shaped_reason_by_action_name(op_id: str) -> str | None:
    """Return a refusal reason if the op id ends in a vendor delete action name.

    ``POST:/VirtualMachine/{moId}/Destroy_Task`` is a delete even though its
    method is ``POST`` and no pattern names it. The rule and the word list
    are the ingest parser's (:mod:`meho_backplane.delete_names`), so the
    grant checks and the parser cannot drift. Independent of
    ``Settings.service_grant_delete_shaped_patterns``.
    """
    action_name = destructive_action_name_in_op_id(op_id)
    if action_name is None:
        return None
    return (
        f"op {op_id!r} is a delete (vendor action name {action_name!r}); "
        "delete operations are never grantable — a standing grant is the floor "
        "of what runs unattended, not a bypass of a destructive gate"
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
# Orphaned-grant classification (read time, #3430)
# ---------------------------------------------------------------------------


def _orphan_remediation(target_id: uuid.UUID, old_name: str | None) -> str:
    """Plain-language fix for a grant whose concrete target is gone.

    ``target_name_pattern`` is an ``fnmatchcase`` glob, and target names may
    hold ``*``, ``?`` or ``[``. The suggested pattern escapes them so it
    matches the old name literally, not a different target.
    """
    example = (
        f"target_name_pattern={glob_escape(old_name)!r}"
        if old_name
        else "target_name_pattern or target_product"
    )
    return (
        f"Target {target_id} no longer exists: it was deleted, or registered "
        "again under a new id. This grant can never match, so the op waits for "
        "approval. To fix it, create a new grant with a target selector (for "
        f"example {example}), which matches by name or product and survives a "
        "new registration. Then revoke this grant."
    )


async def classify_orphaned_grants(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    entries: Sequence[ServiceGrantRead],
) -> list[ServiceGrantRead]:
    """Flag grants whose concrete ``target_id`` no longer resolves (#3430).

    A grant is **orphaned** when it is not revoked, carries a concrete
    ``target_id``, and no live (``deleted_at IS NULL``) target with that id
    exists in the tenant. That happens when the target was deleted, or when
    it was registered again: the new registration gets a new id, so the old
    grant never matches again (:func:`find_live_grant` matches ``target_id``
    exactly).

    This is a read-time check only. It never revokes or changes a grant;
    cleanup stays an operator decision. Selector and targetless grants are
    never orphaned (they do not point at one target id). One query finds the
    live targets; a second, only when something is orphaned, reads the old
    target names (soft-deleted rows keep them) for the remediation hint.
    """
    candidate_ids = {
        entry.target_id
        for entry in entries
        if entry.target_id is not None and entry.revoked_at is None
    }
    if not candidate_ids:
        return list(entries)

    live_ids = set(
        (
            await session.execute(
                select(Target.id).where(
                    Target.tenant_id == tenant_id,
                    Target.id.in_(candidate_ids),
                    Target.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    missing_ids = candidate_ids - live_ids
    if not missing_ids:
        return list(entries)

    old_names: dict[uuid.UUID, str] = {
        row.id: row.name
        for row in (
            await session.execute(
                select(Target.id, Target.name).where(
                    Target.tenant_id == tenant_id,
                    Target.id.in_(missing_ids),
                )
            )
        ).all()
    }
    classified: list[ServiceGrantRead] = []
    for entry in entries:
        if entry.target_id in missing_ids and entry.revoked_at is None:
            entry = entry.model_copy(
                update={
                    "orphaned": True,
                    "orphan_remediation": _orphan_remediation(
                        entry.target_id, old_names.get(entry.target_id)
                    ),
                }
            )
        classified.append(entry)
    return classified


def _grant_models() -> tuple[GrantModel, ...]:
    """The grant tables this instance reads: the delete-grant table only in sandbox mode.

    With the mode off the delete-grant table is never queried, so this
    build behaves like an older build that does not know it (#4622).
    """
    if sandbox_mode_active():
        return (ServicePrincipalGrant, SandboxDeleteGrant)
    return (ServicePrincipalGrant,)


def _read_entry(row: ServicePrincipalGrant | SandboxDeleteGrant) -> ServiceGrantRead:
    """Build the read shape and mark which table the row came from."""
    entry = ServiceGrantRead.model_validate(row)
    if isinstance(row, SandboxDeleteGrant):
        entry = entry.model_copy(update={"delete_grant": True})
    return entry


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
        *,
        from_console: bool = False,
        by_tenant_admin: bool = False,
    ) -> ServiceGrantRead:
        """Create one standing grant row after the full create-time review.

        Refuses wildcards, delete-shaped ops, and past/naive expiries;
        raises :exc:`GrantValidationError` (→ 422) on any of those or on a
        duplicate active grant for the same fully-scoped key (a duplicate
        target selector included, #3349).

        In sandbox mode (#4622) an op with a delete sign becomes a **delete
        grant** instead of a refusal: it is stored in
        :class:`~meho_backplane.db.models.SandboxDeleteGrant`, it must come
        from the web console (``from_console`` is set only by the console
        route, in server code; no request field, header or token claim can
        set it; otherwise :exc:`DeleteGrantConsoleOnlyError`), and it must
        expire within :data:`~meho_backplane.sandbox_mode.SANDBOX_DELETE_GRANT_MAX_DAYS`
        days. With ``MEHO_SANDBOX_DELETE_GRANTS_FROM_API`` on (#5251) a
        caller outside the console may create one too, but only when
        ``by_tenant_admin`` is set (otherwise
        :exc:`DeleteGrantNeedsTenantAdminError`). Ordinary grants keep
        today's rules in every mode.
        """
        model = await self._review(
            tenant_id, payload, from_console=from_console, by_tenant_admin=by_tenant_admin
        )
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            entry = await self._insert(session, tenant_id, created_by_sub, payload, model)
            await session.commit()
            (entry,) = await classify_orphaned_grants(session, tenant_id, [entry])

        self._log.info(
            "service_grant_created",
            tenant_id=str(tenant_id),
            grant_id=str(entry.id),
            delete_grant=entry.delete_grant,
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
    async def _review(
        tenant_id: uuid.UUID,
        payload: ServiceGrantCreate,
        *,
        from_console: bool,
        by_tenant_admin: bool = False,
    ) -> GrantModel:
        """Run the full create-time review; return the table the grant goes in.

        Every grant, new or renewed, passes here (#4622: a renewal is
        checked like a new grant). Opens no session of its own except the
        descriptor lookup, so a caller may run it before it writes.
        """
        from meho_backplane.settings import get_settings

        _reject_wildcards(payload)
        _validate_expires_at(payload.expires_at)

        descriptor = await _resolve_descriptor_for_classification(
            tenant_id, payload.connector_id, payload.op_id
        )
        delete_reason = delete_grant_reason(
            payload.op_id, get_settings().service_grant_delete_shaped_patterns, descriptor
        )
        if delete_reason is None:
            return ServicePrincipalGrant
        if not sandbox_mode_active():
            raise GrantValidationError(delete_reason)
        if not from_console:
            if not get_settings().sandbox_delete_grants_from_api:
                raise DeleteGrantConsoleOnlyError(
                    f"op {payload.op_id!r} has a delete sign, so this is a delete grant. "
                    "A delete grant is made in the web console, by a tenant admin, "
                    "and only on an instance in sandbox mode. REST and the CLI refuse it "
                    "unless MEHO_SANDBOX_DELETE_GRANTS_FROM_API is on."
                )
            if not by_tenant_admin:
                raise DeleteGrantNeedsTenantAdminError(
                    f"op {payload.op_id!r} has a delete sign, so this is a delete grant. "
                    "Only a tenant admin creates a delete grant."
                )
        _validate_delete_grant_expires_at(payload.expires_at)
        return SandboxDeleteGrant

    async def _insert(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        created_by_sub: str,
        payload: ServiceGrantCreate,
        model: GrantModel,
    ) -> ServiceGrantRead:
        """Add one reviewed grant row to *session* and flush it; the caller commits.

        A duplicate active grant raises :exc:`GrantValidationError` after
        the session is rolled back, so a multi-row caller writes nothing.
        """
        is_selector = payload.target_product is not None or payload.target_name_pattern is not None
        row = model(
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
        # Selector grants are not covered by a DB partial unique index
        # (the nullable selector columns make a portable NULL-safe unique
        # index awkward), so enforce "at most one active selector per
        # (key, product, pattern)" in the CRUD layer to preserve the
        # same duplicate-refusal contract the targeted / targetless
        # indexes give.
        if is_selector:
            await self._reject_duplicate_selector(session, tenant_id, payload, model)
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
        return _read_entry(row)

    async def renew_delete_grants(
        self,
        tenant_id: uuid.UUID,
        principal_sub: str,
        renewed_by_sub: str,
        *,
        expires_at: datetime | None,
        reason: str | None = None,
        from_console: bool = False,
    ) -> list[ServiceGrantRead]:
        """Renew the delete grants of one service principal as one set (#4622).

        The set is every live delete grant of *principal_sub*: not revoked
        and not expired. Each one is checked like a new grant with the new
        expiry (:meth:`_review`): the mode is on, the call comes from the
        console, the op still has a delete sign, and the expiry is within
        :data:`~meho_backplane.sandbox_mode.SANDBOX_DELETE_GRANT_MAX_DAYS`
        days. One failed check renews nothing. Then, in one transaction,
        each old row is revoked with the reason ``renewed`` and a new row
        with the new expiry takes its place. ``reason`` replaces the
        recorded reason of every grant; ``None`` keeps each one. Returns
        the new entries. MEHO never calls this by itself.
        """
        if not sandbox_mode_active():
            raise GrantValidationError(
                "this instance is not in sandbox mode, so it has no delete grants to renew"
            )
        if not from_console:
            raise DeleteGrantConsoleOnlyError(
                "delete grants are renewed only in the web console, by a tenant admin"
            )
        now = datetime.now(UTC)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(SandboxDeleteGrant)
                .where(
                    SandboxDeleteGrant.tenant_id == tenant_id,
                    SandboxDeleteGrant.principal_sub == principal_sub,
                    SandboxDeleteGrant.revoked_at.is_(None),
                    or_(
                        SandboxDeleteGrant.expires_at.is_(None),
                        SandboxDeleteGrant.expires_at > now,
                    ),
                )
                .order_by(SandboxDeleteGrant.created_at)
            )
            old_rows = list(result.scalars().all())
        if not old_rows:
            raise GrantValidationError(
                f"service principal {principal_sub!r} has no live delete grant to renew"
            )

        # Every check first, every write after: one refusal renews nothing.
        reviewed: list[tuple[uuid.UUID, ServiceGrantCreate]] = []
        for old in old_rows:
            payload = ServiceGrantCreate(
                principal_sub=old.principal_sub,
                op_id=old.op_id,
                connector_id=old.connector_id,
                target_id=old.target_id,
                target_product=old.target_product,
                target_name_pattern=old.target_name_pattern,
                reason=reason or old.reason,
                expires_at=expires_at,
            )
            model = await self._review(tenant_id, payload, from_console=True)
            if model is not SandboxDeleteGrant:
                raise GrantValidationError(
                    f"op {old.op_id!r} on connector {old.connector_id!r} no longer has a "
                    "delete sign, so its grant is not a delete grant any more. Revoke it "
                    "and make an ordinary grant instead."
                )
            reviewed.append((old.id, payload))

        async with sessionmaker() as session:
            renewed: list[ServiceGrantRead] = []
            for old_id, payload in reviewed:
                gone = await session.execute(
                    update(SandboxDeleteGrant)
                    .where(
                        SandboxDeleteGrant.tenant_id == tenant_id,
                        SandboxDeleteGrant.id == old_id,
                        SandboxDeleteGrant.revoked_at.is_(None),
                    )
                    .values(
                        revoked_at=now,
                        revoked_by_sub=renewed_by_sub,
                        revoked_reason="renewed",
                    )
                    .returning(SandboxDeleteGrant.id)
                )
                if gone.scalar_one_or_none() is None:
                    await session.rollback()
                    raise GrantValidationError(
                        f"delete grant {old_id} was revoked while the set was reviewed; "
                        "review the set again"
                    )
                renewed.append(
                    await self._insert(
                        session, tenant_id, renewed_by_sub, payload, SandboxDeleteGrant
                    )
                )
            await session.commit()
            renewed = await classify_orphaned_grants(session, tenant_id, renewed)

        self._log.info(
            "service_delete_grants_renewed",
            tenant_id=str(tenant_id),
            principal_sub=principal_sub,
            renewed_by_sub=renewed_by_sub,
            count=len(renewed),
            grant_ids=[str(entry.id) for entry in renewed],
            expires_at=expires_at.isoformat() if expires_at else None,
        )
        return renewed

    @staticmethod
    async def _reject_duplicate_selector(
        session: AsyncSession,
        tenant_id: uuid.UUID,
        payload: ServiceGrantCreate,
        model: GrantModel = ServicePrincipalGrant,
    ) -> None:
        """Raise if a live selector grant with the identical scope exists (#3349).

        The CRUD-layer twin of the ``uq_<table>_*`` partial indexes for
        selector grants (which those indexes deliberately do not cover).
        Matches an active (``revoked_at IS NULL``) selector row on the full
        key plus the exact ``(target_product, target_name_pattern)`` pair.
        """
        existing = await session.execute(
            select(model.id)
            .where(
                model.tenant_id == tenant_id,
                model.principal_sub == payload.principal_sub,
                model.op_id == payload.op_id,
                model.connector_id == payload.connector_id,
                model.target_id.is_(None),
                # NULL-safe equality (``IS NOT DISTINCT FROM`` on Postgres,
                # ``IS`` on SQLite) so a NULL selector column compares equal
                # to NULL — a selector with only a product set collides with
                # another product-only selector for the same product.
                model.target_product.is_not_distinct_from(payload.target_product),
                model.target_name_pattern.is_not_distinct_from(payload.target_name_pattern),
                model.revoked_at.is_(None),
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
        *,
        reason: str | None = None,
    ) -> bool:
        """Soft-delete the grant matching ``(tenant_id, grant_id)``.

        Stamps ``revoked_at`` / ``revoked_by_sub`` / ``revoked_reason`` on a
        still-live row (the row is retained for history). ``reason`` is
        optional: unlike create, a revoke without a reason stays valid.
        Returns ``True`` when a live row was revoked, ``False`` when none
        matched (absent, already revoked, or cross-tenant — the ``tenant_id``
        predicate hides other tenants).
        """
        now = datetime.now(UTC)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            revoked = False
            for model in _grant_models():
                result = await session.execute(
                    update(model)
                    .where(
                        model.tenant_id == tenant_id,
                        model.id == grant_id,
                        model.revoked_at.is_(None),
                    )
                    .values(
                        revoked_at=now,
                        revoked_by_sub=revoked_by_sub,
                        revoked_reason=reason,
                    )
                    .returning(model.id)
                )
                if result.scalar_one_or_none() is not None:
                    revoked = True
                    break
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
            row: GrantRow | None = None
            for model in _grant_models():
                result = await session.execute(
                    select(model).where(model.tenant_id == tenant_id, model.id == grant_id)
                )
                row = cast("GrantRow | None", result.scalar_one_or_none())
                if row is not None:
                    break
            if row is None:
                return None
            (entry,) = await classify_orphaned_grants(session, tenant_id, [_read_entry(row)])
        return entry

    async def list_(
        self,
        tenant_id: uuid.UUID,
        *,
        principal_sub: str | None = None,
        include_revoked: bool = False,
        include_expired: bool = True,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[ServiceGrantRead]:
        """Return up to *limit* grants for *tenant_id*, newest-first.

        ``include_revoked=False`` (default) hides soft-deleted rows;
        ``True`` returns the full history (revoked rows included).
        ``include_expired=True`` preserves the historic list behavior;
        ``False`` returns only rows that have not reached their expiry.
        Each entry carries the read-time ``orphaned`` flag (#3430, see
        :func:`classify_orphaned_grants`). In sandbox mode the list also
        holds the delete grants (``delete_grant=True``), merged newest-first
        with the ordinary grants; with the mode off the delete-grant table
        is never read.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0; got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0; got {offset}")
        if limit == 0:
            return []

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            entries: list[ServiceGrantRead] = []
            for model in _grant_models():
                stmt = (
                    select(model)
                    .where(model.tenant_id == tenant_id)
                    .order_by(model.created_at.desc())
                    .limit(limit + offset)
                )
                if principal_sub is not None:
                    stmt = stmt.where(model.principal_sub == principal_sub)
                if not include_revoked:
                    stmt = stmt.where(model.revoked_at.is_(None))
                if not include_expired:
                    now = datetime.now(UTC)
                    stmt = stmt.where(or_(model.expires_at.is_(None), model.expires_at > now))
                result = await session.execute(stmt)
                entries.extend(_read_entry(cast("GrantRow", row)) for row in result.scalars().all())
            entries.sort(key=lambda entry: entry.created_at, reverse=True)
            page = entries[offset : offset + limit]
            return await classify_orphaned_grants(session, tenant_id, page)


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
    model: GrantModel = ServicePrincipalGrant,
) -> tuple[Any, ...]:
    """The scope + liveness predicates shared by every grant lookup.

    Exact on ``(tenant, principal_sub, op_id, connector_id)`` with
    revocation and expiry honoured at dispatch time (``revoked_at IS NULL``
    and (``expires_at IS NULL`` or ``expires_at > now``)). The target
    dimension (exact id / selector / targetless) is applied by the caller.
    """
    return (
        model.tenant_id == tenant_id,
        model.principal_sub == principal_sub,
        model.op_id == op_id,
        model.connector_id == connector_id,
        model.revoked_at.is_(None),
        or_(
            model.expires_at.is_(None),
            model.expires_at > cutoff,
        ),
    )


def _selector_matches(
    grant: ServicePrincipalGrant | SandboxDeleteGrant,
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
    model: GrantModel = ServicePrincipalGrant,
) -> GrantRow | None:
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

    ``model`` picks the table: the ordinary grants by default, the delete
    grants (sandbox mode, #4622) when the caller asks for them.
    """
    cutoff = now or datetime.now(UTC)
    scope = _live_grant_scope_predicates(
        tenant_id=tenant_id,
        principal_sub=principal_sub,
        op_id=op_id,
        connector_id=connector_id,
        cutoff=cutoff,
        model=model,
    )

    if target_id is None:
        stmt = (
            select(model)
            .where(
                *scope,
                model.target_id.is_(None),
                model.target_product.is_(None),
                model.target_name_pattern.is_(None),
            )
            .limit(1)
        )
        return cast("GrantRow | None", (await session.execute(stmt)).scalar_one_or_none())

    # Target-scoped dispatch: prefer a concrete-target grant, then selectors.
    exact_stmt = select(model).where(*scope, model.target_id == target_id).limit(1)
    exact = cast("GrantRow | None", (await session.execute(exact_stmt)).scalar_one_or_none())
    if exact is not None:
        return exact

    selector_stmt = select(model).where(
        *scope,
        model.target_id.is_(None),
        or_(
            model.target_product.isnot(None),
            model.target_name_pattern.isnot(None),
        ),
    )
    candidates = (await session.execute(selector_stmt)).scalars().all()
    for candidate in candidates:
        grant = cast("GrantRow", candidate)
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
    preview_hash: str | None = None,
    params: dict[str, Any] | None = None,
    record: bool = True,
) -> uuid.UUID | None:
    """Look up a live standing grant and, on a hit, record its use.

    Returns the grant id when a live grant authorises this dispatch (the
    caller then clears the gate to ``AUTO_EXECUTE``), or ``None`` when no
    grant matches (the caller parks the op). On a hit it writes the
    grant-use audit row (``approval.decision`` shape) in its own committed
    transaction before returning. The audit row is the record; no
    Broadcast event is published (#3758).

    A ``destructive``-tier op (#3183) is refused here **before** any grant
    lookup: the tier is non-grantable, so even a stale grant row that
    predates the op's promotion into the tier can never auto-approve it.
    This is the dispatch-time twin of the create-time refusal in
    :func:`_delete_shaped_reason_by_descriptor`; together they make
    "a standing grant can never satisfy a destructive op" hold whether the
    grant is being created or consulted.

    An op id whose vendor action name means delete (#4632) is refused the
    same way. A composite's inner step arrives as an in-memory record with
    its declared level only — no method, no tags — so for it the op id is
    the only sign.

    **Sandbox mode (#4622).** When the instance runs in sandbox mode and
    the op has any delete sign, a live **delete grant** from
    :class:`~meho_backplane.db.models.SandboxDeleteGrant` is consulted
    first. For a ``destructive`` op the caller's ``preview_hash`` and
    ``params`` are needed: the preview hash is re-computed and compared and
    the blast radius is built and checked (the same checks the park path
    runs) **before** the grant-use row is written, so a refused call leaves
    no row. A step gate or a child call presents no preview hash, so a
    ``destructive`` step or child stays refused. A delete below
    ``destructive`` needs no hash; its grant-use row stores the proposed
    effect. With no delete grant the call falls through to today's rules,
    so an ordinary grant never changes its verdict. With the mode off the
    delete-grant table is never read.

    ``record=False`` (#4923) only looks the grant up and writes nothing:
    the preview asks "would a grant clear this call?" without using the
    grant. A delete grant is then consulted without building its evidence,
    because that evidence may read the target and the preview never
    contacts the target; a ``destructive`` delete stays refused here, as it
    does for a call that presents no preview hash.
    """
    if sandbox_mode_active():
        grant_id = await _consult_delete_grant(
            operator=operator,
            descriptor=descriptor,
            target=target,
            connector_id=connector_id,
            preview_hash=preview_hash,
            params=params,
            record=record,
        )
        if grant_id is not None:
            return grant_id

    destructive_reason = _delete_shaped_reason_by_action_name(
        descriptor.op_id
    ) or _delete_shaped_reason_by_descriptor(descriptor)
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

    target_id, target_product, target_name = _target_fields(target)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        grant = await find_live_grant(
            session,
            tenant_id=operator.tenant_id,
            principal_sub=operator.sub,
            op_id=descriptor.op_id,
            connector_id=connector_id,
            target_id=target_id,
            target_product=target_product,
            target_name=target_name,
        )
    if grant is None:
        return None

    matched_by_selector = grant.target_id is None and (
        grant.target_product is not None or grant.target_name_pattern is not None
    )
    if not record:
        return grant.id
    await _record_grant_use(
        operator=operator,
        grant=grant,
        connector_id=connector_id,
        target_id=target_id,
        matched_by_selector=matched_by_selector,
    )
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


def _target_fields(target: Any) -> tuple[uuid.UUID | None, str | None, str | None]:
    """``(target_id, target_product, target_name)`` of a dispatch target."""
    target_product = getattr(target, "product", None) if target is not None else None
    target_name = getattr(target, "name", None) if target is not None else None
    return (
        _target_uuid(target),
        target_product if isinstance(target_product, str) else None,
        target_name if isinstance(target_name, str) else None,
    )


async def _consult_delete_grant(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str,
    preview_hash: str | None,
    params: dict[str, Any] | None,
    record: bool = True,
) -> uuid.UUID | None:
    """Sandbox mode only: clear a delete under a live delete grant (#4622).

    Returns the grant id when a delete grant covers the dispatch and its
    evidence checks pass, else ``None`` (the caller falls through to
    today's rules, which park or refuse the delete). With ``record=False``
    the evidence is neither built nor stored (see
    :func:`consult_and_record_grant`).
    """
    from meho_backplane.settings import get_settings

    delete_reason = delete_grant_reason(
        descriptor.op_id, get_settings().service_grant_delete_shaped_patterns, descriptor
    )
    if delete_reason is None:
        return None
    target_id, target_product, target_name = _target_fields(target)
    async with get_sessionmaker()() as session:
        grant = await find_live_grant(
            session,
            tenant_id=operator.tenant_id,
            principal_sub=operator.sub,
            op_id=descriptor.op_id,
            connector_id=connector_id,
            target_id=target_id,
            target_product=target_product,
            target_name=target_name,
            model=SandboxDeleteGrant,
        )
    if grant is None:
        return None
    if not record:
        return None if descriptor.safety_level == "destructive" else grant.id
    evidence = await _delete_grant_evidence(
        operator=operator,
        descriptor=descriptor,
        target=target,
        connector_id=connector_id,
        preview_hash=preview_hash,
        params=params,
    )
    if evidence is None:
        _log.info(
            "sandbox_delete_grant_evidence_refused",
            grant_id=str(grant.id),
            op_id=descriptor.op_id,
            connector_id=connector_id,
            safety_level=descriptor.safety_level,
            principal_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
        )
        return None
    matched_by_selector = grant.target_id is None and (
        grant.target_product is not None or grant.target_name_pattern is not None
    )
    await _record_grant_use(
        operator=operator,
        grant=grant,
        connector_id=connector_id,
        target_id=target_id,
        matched_by_selector=matched_by_selector,
        evidence=evidence,
    )
    _log.info(
        "sandbox_delete_grant_auto_approved",
        grant_id=str(grant.id),
        op_id=descriptor.op_id,
        connector_id=connector_id,
        safety_level=descriptor.safety_level,
        principal_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        matched_by_selector=matched_by_selector,
    )
    return grant.id


async def _delete_grant_evidence(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str,
    preview_hash: str | None,
    params: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The evidence a delete-grant use stores, or ``None`` when the call is refused.

    A ``destructive`` op needs the caller's preview hash and a valid blast
    radius, checked by the dispatcher's own binding check
    (:func:`~meho_backplane.operations.dispatcher.destructive_binding_evidence`).
    No hash (a step gate, a child call) or a failed check refuses the
    grant; the park path then returns the exact refusal. A delete below
    ``destructive`` stores the proposed effect an approval request would
    show a person.
    """
    from meho_backplane.operations.dispatcher import (
        _build_proposed_effect,
        destructive_binding_evidence,
    )

    if descriptor.safety_level == "destructive":
        if preview_hash is None or params is None:
            return None
        return await destructive_binding_evidence(
            op_id=descriptor.op_id,
            connector_id=connector_id,
            operator=operator,
            descriptor=descriptor,
            target=target,
            params=params,
            preview_hash=preview_hash,
        )
    proposed_effect = await _build_proposed_effect(
        op_id=descriptor.op_id,
        connector_id=connector_id,
        descriptor=descriptor,
        operator=operator,
        target=target,
        params=params or {},
    )
    return {"proposed_effect": proposed_effect}


async def _record_grant_use(
    *,
    operator: Operator,
    grant: ServicePrincipalGrant | SandboxDeleteGrant,
    connector_id: str,
    target_id: uuid.UUID | None,
    matched_by_selector: bool = False,
    evidence: dict[str, Any] | None = None,
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

    Lineage (#3695): the row carries the same ids the parked path stamps, so
    an auto-approval links to the call it cleared. ``request_id`` is the
    request's correlation id (the dispatch's ``DISPATCH`` row carries the
    same value), ``agent_session_id`` is the agent-run or MCP session (NULL
    for a plain REST call), and ``parent_audit_id`` is the composite parent's
    audit id when a composite child is the call being cleared (NULL for a
    top-level call).

    A delete-grant use (sandbox mode, #4622) carries ``delete_grant=True``
    and its ``evidence``: ``preview_hash`` and ``blast_radius`` for a
    ``destructive`` call, ``proposed_effect`` for a delete below it. The
    evidence sits in ``payload``, which MEHO keeps, not in ``raw_payload``.
    """
    from meho_backplane.operations._audit import (
        parent_audit_id_var,
        resolve_agent_session_id,
        work_ref_var,
    )
    from meho_backplane.request_usage.collector import current_request_id

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
    if isinstance(grant, SandboxDeleteGrant):
        payload["delete_grant"] = True
        payload.update(evidence or {})
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = AuditLog(
            id=audit_id,
            event_class=AuditEventClass.APPROVAL_DECISION,
            occurred_at=datetime.now(UTC),
            operator_sub=operator.sub,
            tenant_id=operator.tenant_id,
            target_id=target_id,
            parent_audit_id=parent_audit_id_var.get(),
            agent_session_id=resolve_agent_session_id(),
            method=_GRANT_USE_METHOD,
            path=_GRANT_USE_PATH,
            status_code=_GRANT_USE_STATUS_CODE,
            request_id=current_request_id(),
            duration_ms=Decimal("0.00"),
            payload=payload,
            work_ref=work_ref_var.get(),
        )
        session.add(row)
        await session.commit()
    return audit_id
