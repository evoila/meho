# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reference resolution shared by the audit + broadcast detail drawers.

Both console detail drawers -- the audit row drawer
(:mod:`meho_backplane.ui.routes.audit.routes`) and the broadcast event
drawer (:mod:`meho_backplane.ui.routes.broadcast.event`) -- resolve the
canonical ``audit_log`` row and, historically, rendered its reference
columns **raw**: ``operator_sub`` as an OIDC sub, ``target_id`` /
``parent_audit_id`` / ``agent_session_id`` / ``run_id`` as bare UUIDs,
``op_id`` as a machine handle. A human reading the drawer saw
identifiers, not "who did what to which target and what came back"
(internal#236).

This module turns those references into named, linked, human-legible
substance -- resolving **only** from stores the backplane already has
(:class:`~meho_backplane.db.models.Target`,
:class:`~meho_backplane.db.models.EndpointDescriptor` /
:class:`~meho_backplane.db.models.OperationGroup`,
:class:`~meho_backplane.db.models.RunbookRun`, and ``audit_log`` itself).
Nothing is invented: a reference that cannot be resolved (deleted,
cross-tenant, missing, or simply a store that carries no name for it --
e.g. a chassis HTTP ``op_id`` has no ``endpoint_descriptor`` row)
degrades to the raw id with an ``unresolved`` marker, and the raw id
stays reachable everywhere.

Tenant scoping
==============

Every join is scoped on the audit row's ``tenant_id`` so a resolution
can never surface another tenant's name. When the row carries no tenant
(pre-G0.1 chassis rows), name resolution is skipped and the reference
degrades to its raw id -- resolving cross-tenant would be the only
alternative, and that leaks.

Principal display name + service marker
=======================================

``operator_sub`` -> a display name is resolved from the row's own
``payload['principal_name']`` / ``payload['principal_email']`` (the MCP
audit writer hoists ``Operator.name`` / ``Operator.email`` there since
G0.15-T3 #1212). There is no ``sub`` -> name join table -- the agent /
runner principal tables key on the Keycloak client id, not the token
``sub`` -- so an HTTP-chassis row (which carries no name in payload)
degrades to its raw sub. The **service marker** is derived from the
row's own typed columns: a row bearing an ``agent_session_id`` (an MCP
agent session) or an ``actor_sub`` (an RFC 8693 delegated actor) is a
non-interactive principal, flagged so a human does not read it as a
person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    OperationGroup,
    RunbookRun,
    Target,
)

__all__ = [
    "AuditReferences",
    "OperationRef",
    "ParentRef",
    "PrincipalRef",
    "RunRef",
    "SessionRef",
    "StatusRef",
    "TargetRef",
    "TimeRef",
    "WorkRef",
    "humanize_relative",
    "resolve_audit_references",
    "resolve_status",
]

#: ``work_ref`` shorthand for a GitHub issue / PR, e.g. ``gh:evoila/meho#9``.
#: The single external-reference shape the backplane's own runbook + work_ref
#: tooling emits; anything else stays raw (never guessed into a URL).
_GH_WORK_REF = re.compile(r"^gh:(?P<owner>[^/]+)/(?P<repo>[^#]+)#(?P<number>\d+)$")


@dataclass(frozen=True)
class PrincipalRef:
    """Resolved originating principal for an audit row."""

    sub: str
    name: str | None
    email: str | None
    #: Non-interactive principal (agent session or delegated actor).
    is_service: bool
    #: RFC 8693 delegated actor sub, when the row carries one.
    actor_sub: str | None

    @property
    def resolved(self) -> bool:
        """True when a human-readable display name was found."""
        return self.name is not None

    @property
    def display(self) -> str:
        """The best human label available (name, else the raw sub)."""
        return self.name or self.sub


@dataclass(frozen=True)
class TargetRef:
    """Resolved operation target (a registered connector endpoint)."""

    #: ``target_id`` as a string, or ``None`` when the op touched no target.
    id: str | None
    name: str | None
    #: Link to the per-target detail surface, when the name resolved.
    href: str | None

    @property
    def present(self) -> bool:
        """True when the row bound a ``target_id`` at all."""
        return self.id is not None

    @property
    def resolved(self) -> bool:
        """True when the ``target_id`` resolved to a live target name."""
        return self.name is not None


@dataclass(frozen=True)
class OperationRef:
    """Resolved operation: the op id plus its human summary + group."""

    op_id: str
    op_class: str
    summary: str | None
    group_name: str | None

    @property
    def resolved(self) -> bool:
        """True when an ``endpoint_descriptor`` row carried a summary/group."""
        return self.summary is not None or self.group_name is not None


@dataclass(frozen=True)
class WorkRef:
    """Resolved external change-ticket reference (``work_ref``)."""

    raw: str | None
    #: Outbound link when the ref matches a known shape (``gh:owner/repo#N``).
    href: str | None
    #: Compact human label, e.g. ``owner/repo#N``; the raw string otherwise.
    label: str | None

    @property
    def present(self) -> bool:
        """True when the row carried a ``work_ref``."""
        return self.raw is not None

    @property
    def resolved(self) -> bool:
        """True when the ref matched a shape we can link out to."""
        return self.href is not None


@dataclass(frozen=True)
class RunRef:
    """Resolved originating runbook run (``run_id`` / ``step_id``)."""

    id: str | None
    step_id: str | None
    href: str | None
    state: str | None
    template: str | None

    @property
    def present(self) -> bool:
        """True when the row was issued inside a runbook run."""
        return self.id is not None

    @property
    def resolved(self) -> bool:
        """True when the ``run_id`` resolved to a live run row."""
        return self.state is not None


@dataclass(frozen=True)
class ParentRef:
    """Resolved composite-operation parent (``parent_audit_id``)."""

    id: str | None
    href: str | None
    #: Labelled lineage line, e.g. ``vsphere.vm.create on lab-vcenter``.
    summary_line: str | None

    @property
    def present(self) -> bool:
        """True when the row carried a ``parent_audit_id``."""
        return self.id is not None

    @property
    def resolved(self) -> bool:
        """True when the parent row resolved in the operator's tenant."""
        return self.summary_line is not None


@dataclass(frozen=True)
class SessionRef:
    """Resolved originating agent session (``agent_session_id``)."""

    id: str | None
    replay_href: str | None
    #: True only for a ``tenant_admin`` lift (the replay surface is gated).
    replay_enabled: bool

    @property
    def present(self) -> bool:
        """True when the row carried an ``agent_session_id``."""
        return self.id is not None


@dataclass(frozen=True)
class StatusRef:
    """Human meaning for the row's HTTP status code."""

    code: int
    label: str
    #: DaisyUI tone token: ``success`` / ``warning`` / ``error`` / ``neutral``.
    tone: str


@dataclass(frozen=True)
class TimeRef:
    """Humanized timestamp: absolute (ISO + display) plus relative."""

    absolute: str
    display: str
    relative: str


@dataclass(frozen=True)
class AuditReferences:
    """Everything a detail drawer needs to render substance, not GUIDs."""

    audit_id: str
    request_id: str | None
    principal: PrincipalRef
    target: TargetRef
    operation: OperationRef
    work_ref: WorkRef
    run: RunRef
    parent: ParentRef
    session: SessionRef
    status: StatusRef
    time: TimeRef
    what_happened: str
    #: One-click link from an event to its fully-resolved audit row.
    audit_href: str


def humanize_relative(then: datetime, *, now: datetime) -> str:
    """Return a coarse "X ago" label for *then* relative to *now*.

    One-minute resolution, matching the console's existing
    ``_relative_time`` template macro; the exact instant stays available
    in the absolute timestamp. A future timestamp (clock skew) collapses
    to ``"just now"`` rather than rendering a negative delta.

    Both instants are coerced to UTC-aware before subtraction: the
    ``DateTime(timezone=True)`` column reads back tz-aware on PostgreSQL
    but naive on SQLite (no tz storage), so a naive ``occurred_at`` is
    treated as the UTC it was written as rather than raising on the
    aware-minus-naive subtraction.
    """
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    delta = int((now - then).total_seconds())
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600} h ago"
    if delta < 604800:
        return f"{delta // 86400} d ago"
    return then.strftime("%Y-%m-%d")


def resolve_status(status_code: int) -> StatusRef:
    """Map an HTTP status code to a human label + DaisyUI tone.

    Mirrors the audit substrate's ``result_status`` partition (401/403 ->
    denied, 202 -> the "awaiting approval" synthetic pending code, other
    4xx/5xx -> error) but renders operator-facing prose + a colour tone
    rather than the machine token.
    """
    if status_code in (401, 403):
        return StatusRef(code=status_code, label="Denied", tone="error")
    if status_code == 202:
        return StatusRef(code=status_code, label="Awaiting approval", tone="warning")
    if 200 <= status_code < 400:
        return StatusRef(code=status_code, label="OK", tone="success")
    if 400 <= status_code < 500:
        return StatusRef(code=status_code, label="Client error", tone="error")
    if status_code >= 500:
        return StatusRef(code=status_code, label="Server error", tone="error")
    return StatusRef(code=status_code, label=f"HTTP {status_code}", tone="neutral")


def _resolve_principal(row: AuditLog) -> PrincipalRef:
    """Resolve the originating principal from the row + its payload.

    Name / email come from ``payload`` (MCP writer, #1212); the service
    marker is derived from the row's own ``agent_session_id`` / ``actor_sub``
    columns. No external store is consulted -- there is no ``sub`` -> name
    join table -- so an HTTP-chassis row degrades to its raw sub.
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    name_raw = payload.get("principal_name")
    name = name_raw if isinstance(name_raw, str) and name_raw else None
    email_raw = payload.get("principal_email")
    email = email_raw if isinstance(email_raw, str) and email_raw else None
    actor_sub = row.actor_sub
    is_service = row.agent_session_id is not None or actor_sub is not None
    return PrincipalRef(
        sub=row.operator_sub,
        name=name,
        email=email,
        is_service=is_service,
        actor_sub=actor_sub,
    )


async def _resolve_target(db_session: AsyncSession, row: AuditLog) -> TargetRef:
    """Resolve ``target_id`` -> a live target name + detail link.

    Tenant-scoped: a ``target_id`` that belongs to another tenant (allowed
    today -- ``audit_log`` keeps no FK on the column) resolves to
    ``name=None`` rather than leaking the other tenant's name.
    """
    if row.target_id is None:
        return TargetRef(id=None, name=None, href=None)
    target_id = str(row.target_id)
    if row.tenant_id is None:
        return TargetRef(id=target_id, name=None, href=None)
    name = await db_session.scalar(
        sa.select(Target.name).where(
            Target.id == row.target_id,
            Target.tenant_id == row.tenant_id,
        )
    )
    if name is None:
        return TargetRef(id=target_id, name=None, href=None)
    href = f"/ui/connectors/{quote(name, safe='')}"
    return TargetRef(id=target_id, name=name, href=href)


async def _resolve_operation(
    db_session: AsyncSession, row: AuditLog, *, op_id: str, op_class: str
) -> OperationRef:
    """Resolve ``op_id`` -> its human summary + operation-group name.

    Matches an ``endpoint_descriptor`` row by ``op_id`` (tenant-scoped or
    global), preferring one that carries a description, and joins its
    operation group for the group name. Chassis HTTP op ids
    (``http.get:/...``) and ``ui.view.*`` ops have no descriptor row and
    degrade to the bare op id -- the honest state, not a fabricated summary.
    """
    stmt = (
        sa.select(
            EndpointDescriptor.summary,
            EndpointDescriptor.custom_description,
            OperationGroup.name.label("group_name"),
        )
        .outerjoin(OperationGroup, EndpointDescriptor.group_id == OperationGroup.id)
        .where(EndpointDescriptor.op_id == op_id)
        .where(
            sa.or_(
                EndpointDescriptor.tenant_id == row.tenant_id,
                EndpointDescriptor.tenant_id.is_(None),
            )
        )
        # Prefer a row that actually carries prose: ``is_(None)`` sorts
        # False (0, non-null) before True (1) on both PG and SQLite.
        .order_by(
            EndpointDescriptor.custom_description.is_(None),
            EndpointDescriptor.summary.is_(None),
        )
        .limit(1)
    )
    match = (await db_session.execute(stmt)).first()
    summary: str | None = None
    group_name: str | None = None
    if match is not None:
        summary = match.custom_description or match.summary
        group_name = match.group_name
    return OperationRef(op_id=op_id, op_class=op_class, summary=summary, group_name=group_name)


def _resolve_work_ref(row: AuditLog) -> WorkRef:
    """Resolve the external change-ticket reference to a label + link.

    Only the backplane's own ``gh:owner/repo#N`` shorthand is linked out;
    any other opaque ref renders raw (never guessed into a URL).
    """
    raw = row.work_ref
    if raw is None:
        return WorkRef(raw=None, href=None, label=None)
    gh = _GH_WORK_REF.match(raw)
    if gh is None:
        return WorkRef(raw=raw, href=None, label=raw)
    owner, repo, number = gh.group("owner"), gh.group("repo"), gh.group("number")
    href = f"https://github.com/{quote(owner)}/{quote(repo)}/issues/{number}"
    return WorkRef(raw=raw, href=href, label=f"{owner}/{repo}#{number}")


async def _resolve_run(db_session: AsyncSession, row: AuditLog) -> RunRef:
    """Resolve ``run_id`` -> the originating runbook run (state + template)."""
    if row.run_id is None:
        return RunRef(id=None, step_id=row.step_id, href=None, state=None, template=None)
    run_id = str(row.run_id)
    href = f"/ui/runbooks/runs/{run_id}"
    if row.tenant_id is None:
        return RunRef(id=run_id, step_id=row.step_id, href=href, state=None, template=None)
    match = (
        await db_session.execute(
            sa.select(RunbookRun.state, RunbookRun.template_slug).where(
                RunbookRun.run_id == row.run_id,
                RunbookRun.tenant_id == row.tenant_id,
            )
        )
    ).first()
    if match is None:
        return RunRef(id=run_id, step_id=row.step_id, href=href, state=None, template=None)
    return RunRef(
        id=run_id,
        step_id=row.step_id,
        href=href,
        state=match.state,
        template=match.template_slug,
    )


async def _resolve_parent(db_session: AsyncSession, row: AuditLog) -> ParentRef:
    """Resolve ``parent_audit_id`` -> a labelled lineage line + drawer link.

    Fetches the parent row (tenant-scoped, joined to its target name) so
    the drawer shows "``<op>`` on ``<target>``" rather than a bare UUID.
    A parent that no longer resolves degrades to the raw id (still linked
    -- the drawer route renders its own not-found fragment on click).
    """
    if row.parent_audit_id is None:
        return ParentRef(id=None, href=None, summary_line=None)
    parent_id = str(row.parent_audit_id)
    href = f"/ui/audit/show/{parent_id}"
    if row.tenant_id is None:
        return ParentRef(id=parent_id, href=href, summary_line=None)
    match = (
        await db_session.execute(
            sa.select(AuditLog, Target.name.label("target_name"))
            .outerjoin(
                Target,
                sa.and_(
                    AuditLog.target_id == Target.id,
                    Target.tenant_id == row.tenant_id,
                ),
            )
            .where(
                AuditLog.id == row.parent_audit_id,
                AuditLog.tenant_id == row.tenant_id,
            )
        )
    ).first()
    if match is None:
        return ParentRef(id=parent_id, href=href, summary_line=None)
    parent_row = match.AuditLog
    parent_op = _op_id_of(parent_row)
    target_name = match.target_name
    summary_line = f"{parent_op} on {target_name}" if target_name else parent_op
    return ParentRef(id=parent_id, href=href, summary_line=summary_line)


def _op_id_of(row: AuditLog) -> str:
    """Recover a row's op id (payload op_id, else the HTTP heuristic).

    Duplicates the shared ``resolve_op_id`` shape locally to avoid a
    circular import between this module and the broadcast aggregate gate;
    the ``http.{method}:{path}`` fallback matches the audit middleware /
    broadcast publisher byte-for-byte.
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    op_id = payload.get("op_id")
    if isinstance(op_id, str) and op_id:
        return op_id
    return f"http.{row.method.lower()}:{row.path}"


def _build_what_happened(
    *, principal: PrincipalRef, operation: OperationRef, target: TargetRef, status: StatusRef
) -> str:
    """Compose the plain-language "what happened" line (op + target + outcome)."""
    op_label = operation.summary or operation.op_id
    if target.name:
        target_clause = f" on {target.name}"
    elif target.present:
        target_clause = " on an unresolved target"
    else:
        target_clause = ""
    return f"{principal.display} ran {op_label}{target_clause} → {status.label}"


async def resolve_audit_references(
    db_session: AsyncSession,
    row: AuditLog,
    *,
    op_id: str,
    op_class: str,
    is_admin: bool,
    now: datetime | None = None,
) -> AuditReferences:
    """Resolve every reference on *row* into named, linked substance.

    *op_id* / *op_class* are the drawer's already-computed read-time
    classification (from the shared aggregate gate) so this helper does
    not re-derive them. *is_admin* gates the session-replay deep-link
    (the replay surface is ``tenant_admin``-only). *now* is injectable for
    deterministic relative-time tests; it defaults to the current instant.

    Every resolution is tenant-scoped and degrades gracefully -- a missing
    / cross-tenant / nameless reference falls back to its raw id, never
    raising.
    """
    resolved_now = now or datetime.now(UTC)

    principal = _resolve_principal(row)
    target = await _resolve_target(db_session, row)
    operation = await _resolve_operation(db_session, row, op_id=op_id, op_class=op_class)
    work_ref = _resolve_work_ref(row)
    run = await _resolve_run(db_session, row)
    parent = await _resolve_parent(db_session, row)
    status = resolve_status(row.status_code)

    session_id = str(row.agent_session_id) if row.agent_session_id is not None else None
    session = SessionRef(
        id=session_id,
        replay_href=(f"/ui/audit/sessions/{session_id}/replay" if session_id else None),
        replay_enabled=is_admin and session_id is not None,
    )

    time = TimeRef(
        absolute=row.occurred_at.isoformat(),
        display=row.occurred_at.strftime("%Y-%m-%d %H:%M"),
        relative=humanize_relative(row.occurred_at, now=resolved_now),
    )

    return AuditReferences(
        audit_id=str(row.id),
        request_id=str(row.request_id) if row.request_id is not None else None,
        principal=principal,
        target=target,
        operation=operation,
        work_ref=work_ref,
        run=run,
        parent=parent,
        session=session,
        status=status,
        time=time,
        what_happened=_build_what_happened(
            principal=principal, operation=operation, target=target, status=status
        ),
        audit_href=f"/ui/audit?audit_id={row.id}",
    )
