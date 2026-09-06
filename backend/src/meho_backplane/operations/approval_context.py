# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reviewer-context resolution for a parked :class:`ApprovalRequest` (#3353).

The approval row persists machine-truthful coordinates -- ``op_id`` (a
placeholder path for a subject-agnostic composite-child gate),
``connector_id``, a ``target_id`` GUID, ``principal_sub``, a
``params_hash`` -- but nothing a *human* can read at the moment they are
asked to sign off. This module resolves those coordinates into a single
redacted, human-legible summary and is the **one shared contract** both
operator-facing surfaces render from:

* the console "Review approval request" modal
  (:mod:`meho_backplane.ui.routes.approvals`), and
* the CLI ``meho approvals show`` (via the additive
  :attr:`ApprovalRequestView.reviewer_context` field on
  ``GET /api/v1/approvals/{id}``).

Both call :func:`resolve_reviewer_context`, so the two can never drift --
a regression test asserts parity between them.

What it resolves
----------------

* **target** -- the registered target's ``name`` + ``product`` /
  ``version`` alongside the bare ``target_id`` GUID (tenant-scoped;
  never leaks a cross-tenant name).
* **subject** -- for a path-variable op (e.g.
  ``POST:/vcenter/vm/{vm}/power?action=start``) the concrete entity the
  write touches, recovered from the *identity* fields the caller supplied
  on the gate params (the VM ``name`` and/or its path-variable moid), so
  the reviewer reads ``web-01 (vm-1042)`` instead of an unresolved
  ``{vm}``. Fail-open to ``None`` (the surface keeps the placeholder).
* **parent composite** -- for a composite *child* park, the op id of the
  composite that fanned it out, walked off the ``request_audit_id`` ->
  ``parent_audit_id`` lineage the #3348 fix now records on the direct
  seam.
* **run context** -- the ``work_ref`` change-ticket the automation stamps
  and the originating ``run_id``.
* **blast radius** -- the preview's blast-radius block when one was
  produced (destructive tier / #3312 preview parity), lifted from
  ``proposed_effect`` so both surfaces render it identically.
* **summary** -- a redacted plain-language "what will happen" sentence
  built from the operation's catalog summary + the resolved subject +
  target. Never carries a raw param / body / secret value.

Secret hygiene (the hard constraint)
------------------------------------

Params stay hidden by design. This resolver reads **identity fields
only** -- the path-variable moids in the op path and a caller-supplied
``name`` -- never the request body, arguments, or env values. A
credential-class op (:func:`~meho_backplane.broadcast.events.classify_op`)
gets **no** subject echo at all. Every candidate identity value is run
through the same connector-boundary redaction engine the response path
uses; anything the engine flags as secret-shaped is dropped rather than
surfaced. See the tests under
``tests/test_approval_context.py`` that assert no secret-shaped value
reaches the summary.

Fail-open everywhere
--------------------

Every resolution degrades to the raw id / a ``None`` field and never
raises: a resolution failure must never block an approval decision or
slow the surface. :func:`resolve_reviewer_context` catches at the seam
so a single failed sub-resolution cannot take down the whole context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
import structlog

from meho_backplane.broadcast.events import classify_op
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    Target,
)
from meho_backplane.redaction import apply_connector_boundary_redaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ReviewerContext",
    "resolve_reviewer_context",
]

_log = structlog.get_logger(__name__)

#: Sensitivity classes whose subject must never be echoed -- a
#: credential-class op gets no identity echo at all (mirrors the
#: ``_SENSITIVE_CLASSES`` set the preview builder suppresses on).
_CREDENTIAL_CLASSES: Final[frozenset[str]] = frozenset(
    {"credential_read", "credential_mint", "credential_write"}
)

#: Non-path-variable param keys that name the *human* subject the caller
#: supplied for a governed write. Deliberately narrow: an identity label,
#: never a body field. Path-variable names (parsed from the op id) are
#: added to the read set per-op on top of these.
_SUBJECT_NAME_KEYS: Final[tuple[str, ...]] = ("name", "vm_name", "display_name")

#: ``{token}`` path variables in an op id, e.g. ``{vm}`` / ``{moId}``.
_PATH_VAR = re.compile(r"\{([^}/]+)\}")


@dataclass(frozen=True)
class ReviewerContext:
    """A parked request resolved into redacted, human-legible substance.

    The single shared contract the console modal and ``meho approvals
    show`` both render (#3353). Every field is optional and fails open to
    ``None`` -- a surface renders the raw id it already had when a field
    is absent. Carries **no** raw param / body / secret value: identity
    fields only.
    """

    #: Resolved target display name, or ``None`` (surface keeps the GUID).
    target_name: str | None
    #: Target product family (e.g. ``vmware``), when the name resolved.
    target_product: str | None
    #: Operator-asserted target version (e.g. ``9.0``), when recorded.
    target_version: str | None
    #: Resolved subject for a path-variable op, e.g. ``web-01 (vm-1042)``.
    #: ``None`` when no identity field was supplied (keep the placeholder).
    subject: str | None
    #: Op id of the composite that fanned out this child park, or ``None``.
    parent_composite_op_id: str | None
    #: External change-ticket ref (``work_ref``), or ``None``.
    work_ref: str | None
    #: Originating run id (stringified), or ``None``.
    run_id: str | None
    #: The preview's blast-radius block, when one was produced.
    blast_radius: dict[str, Any] | None
    #: Redacted plain-language "what will happen" sentence.
    summary: str | None

    @property
    def is_empty(self) -> bool:
        """True when nothing resolved -- the surface adds no context block."""
        return not any(
            (
                self.target_name,
                self.subject,
                self.parent_composite_op_id,
                self.work_ref,
                self.run_id,
                self.blast_radius,
                self.summary,
            )
        )


async def resolve_reviewer_context(
    session: AsyncSession,
    request: ApprovalRequest,
) -> ReviewerContext:
    """Resolve *request* into a redacted :class:`ReviewerContext`.

    Tenant-scoped off ``request.tenant_id`` and fail-open throughout:
    every sub-resolution degrades to ``None`` and the whole call is
    wrapped so a failure returns an empty context rather than raising.
    Reads identity fields only -- never the hidden ``params`` body -- so
    no secret-shaped value can reach the reviewer (see module docstring).
    """
    try:
        target_name, target_product, target_version = await _resolve_target(session, request)
        op_summary = await _resolve_op_summary(session, request)
        subject = _resolve_subject(request)
        parent_op = await _resolve_parent_composite_op(session, request)
        blast = _resolve_blast_radius(request)
        summary = _build_summary(
            op_summary=op_summary,
            op_id=request.op_id,
            subject=subject,
            target_name=target_name,
            target_product=target_product,
            target_version=target_version,
        )
        return ReviewerContext(
            target_name=target_name,
            target_product=target_product,
            target_version=target_version,
            subject=subject,
            parent_composite_op_id=parent_op,
            work_ref=request.work_ref,
            run_id=str(request.run_id) if request.run_id is not None else None,
            blast_radius=blast,
            summary=summary,
        )
    except Exception:  # pragma: no cover - defensive; resolution must never block a decision
        _log.warning(
            "reviewer_context_resolution_failed",
            request_id=str(request.id),
            op_id=request.op_id,
            exc_info=True,
        )
        return ReviewerContext(
            target_name=None,
            target_product=None,
            target_version=None,
            subject=None,
            parent_composite_op_id=None,
            work_ref=request.work_ref,
            run_id=str(request.run_id) if request.run_id is not None else None,
            blast_radius=None,
            summary=None,
        )


async def _resolve_target(
    session: AsyncSession, request: ApprovalRequest
) -> tuple[str | None, str | None, str | None]:
    """Resolve ``target_id`` -> ``(name, product, version)``, tenant-scoped.

    A ``target_id`` that belongs to another tenant resolves to all-``None``
    rather than leaking the other tenant's name (the row keeps no FK on the
    column). No target on the row -> all ``None``.
    """
    if request.target_id is None or request.tenant_id is None:
        return None, None, None
    row = (
        await session.execute(
            sa.select(Target.name, Target.product, Target.version).where(
                Target.id == request.target_id,
                Target.tenant_id == request.tenant_id,
            )
        )
    ).first()
    if row is None:
        return None, None, None
    return row.name, row.product, row.version


async def _resolve_op_summary(session: AsyncSession, request: ApprovalRequest) -> str | None:
    """Resolve the op's human summary from a matching descriptor, or ``None``.

    Matches an ``endpoint_descriptor`` by ``op_id`` (tenant-scoped or
    global), preferring a row that carries prose. A composite-child gate's
    placeholder op id has no persisted descriptor and degrades to ``None``
    (the summary then falls back to the raw op id).
    """
    stmt = (
        sa.select(EndpointDescriptor.summary, EndpointDescriptor.custom_description)
        .where(EndpointDescriptor.op_id == request.op_id)
        .where(
            sa.or_(
                EndpointDescriptor.tenant_id == request.tenant_id,
                EndpointDescriptor.tenant_id.is_(None),
            )
        )
        .order_by(
            EndpointDescriptor.custom_description.is_(None),
            EndpointDescriptor.summary.is_(None),
        )
        .limit(1)
    )
    match = (await session.execute(stmt)).first()
    if match is None:
        return None
    summary: str | None = match.custom_description or match.summary
    return summary


def _resolve_subject(request: ApprovalRequest) -> str | None:
    """Resolve the concrete subject of a path-variable op from identity params.

    Recovers the entity the write touches from the *identity* fields the
    caller supplied -- a human ``name`` and/or the moid bound to a path
    variable in the op id -- so a reviewer reads ``web-01 (vm-1042)``
    rather than an unresolved ``{vm}``. Reads **only** those allowlisted
    identity keys, never the request body, and returns ``None`` for a
    credential-class op or when no identity field was supplied (the surface
    then keeps the placeholder). Every candidate value is run through the
    connector-boundary redaction engine; a secret-shaped value is dropped.
    """
    params = request.params if isinstance(request.params, dict) else {}
    if not params:
        return None
    # A credential-class op gets no identity echo at all -- the whole point
    # of the credential suppression is that nothing rides the durable row.
    if classify_op(request.op_id) in _CREDENTIAL_CLASSES:
        return None

    path_vars = tuple(_PATH_VAR.findall(request.op_id))
    tenant = str(request.tenant_id) if request.tenant_id is not None else None

    # The human name the caller supplied (an identity label), then the moid
    # bound to the op's own path variable(s). Read keys in a fixed order so
    # the label is deterministic; a path-var key duplicated in the name set
    # is read once.
    name_value = _first_identity_value(
        params, _SUBJECT_NAME_KEYS, op_id=request.op_id, tenant=tenant
    )
    moid_value = _first_identity_value(params, path_vars, op_id=request.op_id, tenant=tenant)

    if name_value and moid_value and name_value != moid_value:
        return f"{name_value} ({moid_value})"
    return name_value or moid_value or None


def _first_identity_value(
    params: dict[str, Any],
    keys: tuple[str, ...],
    *,
    op_id: str,
    tenant: str | None,
) -> str | None:
    """Return the first redaction-clean scalar identity value among *keys*.

    Skips non-scalar values (an identity field is a name / id, never a
    nested body) and any value the connector-boundary redaction engine
    flags as secret-shaped (defence-in-depth over the allowlist).
    """
    for key in keys:
        if key not in params:
            continue
        value = params[key]
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            continue
        text = str(value).strip()
        if not text:
            continue
        if _looks_secret(text, op_id=op_id, tenant=tenant):
            continue
        return text
    return None


def _looks_secret(value: str, *, op_id: str, tenant: str | None) -> bool:
    """True when the connector-boundary redaction engine flags *value*.

    The same engine the response path and the generic params-echo run.
    A non-empty manifest means the engine recognised a secret shape (a
    JWT, a bearer token, a labelled ``key=value`` secret) in the value,
    so it must not reach the reviewer summary.
    """
    try:
        result = apply_connector_boundary_redaction(
            value, connector_id=None, tenant=tenant, op=op_id
        )
    except Exception:  # pragma: no cover - defensive; treat an engine fault as unsafe
        return True
    return len(result.manifest) > 0


async def _resolve_parent_composite_op(
    session: AsyncSession, request: ApprovalRequest
) -> str | None:
    """Resolve the parent composite's op id for a child park, or ``None``.

    Walks the ``request_audit_id`` -> ``parent_audit_id`` lineage the
    #3348 direct-seam fix records: a composite child's ``approval.request``
    audit row carries the composite dispatch's ``audit_id`` as its parent.
    A top-level (non-composite) park has no parent audit row and resolves
    to ``None``. Tenant-scoped; a cross-tenant / missing parent degrades to
    ``None``.
    """
    if request.request_audit_id is None or request.tenant_id is None:
        return None
    child = (
        await session.execute(
            sa.select(AuditLog.parent_audit_id).where(
                AuditLog.id == request.request_audit_id,
                AuditLog.tenant_id == request.tenant_id,
            )
        )
    ).first()
    if child is None or child.parent_audit_id is None:
        return None
    parent = await session.get(AuditLog, child.parent_audit_id)
    if parent is None or parent.tenant_id != request.tenant_id:
        return None
    return _audit_op_id(parent)


def _audit_op_id(row: AuditLog) -> str:
    """Recover an audit row's op id (payload ``op_id`` else the HTTP form).

    Mirrors the shared ``resolve_op_id`` shape locally (the same fallback
    the audit middleware / broadcast publisher use) to avoid a cross-layer
    import.
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    op_id = payload.get("op_id")
    if isinstance(op_id, str) and op_id:
        return op_id
    return f"http.{row.method.lower()}:{row.path}"


def _resolve_blast_radius(request: ApprovalRequest) -> dict[str, Any] | None:
    """Lift the preview's blast-radius block off ``proposed_effect``.

    The dispatcher promotes a destructive op's blast-radius block to the
    top level of the envelope (#3197); #3312 extended preview coverage to
    approval-requiring typed ops. Surface it in the shared context so both
    the console and the CLI render the same block. ``None`` when no preview
    produced one (e.g. a composite-child gate whose envelope is the bare
    op-identity default).
    """
    effect = request.proposed_effect if isinstance(request.proposed_effect, dict) else {}
    blast = effect.get("blast_radius")
    return blast if isinstance(blast, dict) and blast else None


def _build_summary(
    *,
    op_summary: str | None,
    op_id: str,
    subject: str | None,
    target_name: str | None,
    target_product: str | None,
    target_version: str | None,
) -> str | None:
    """Compose the redacted "what will happen" sentence.

    Built from the operation's catalog summary (or the raw op id when no
    descriptor resolved), the resolved subject, and the target name +
    product/version. Carries only resolved identity substance -- never a
    raw param value -- so it is redaction-safe by construction.
    """
    op_label = op_summary or op_id
    parts = [op_label]
    if subject:
        parts.append(f"on {subject}")
    if target_name:
        target_clause = f"target {target_name}"
        if target_product:
            version = f" {target_version}" if target_version else ""
            target_clause += f" ({target_product}{version})"
        parts.append(f"@ {target_clause}")
    if len(parts) == 1 and op_summary is None:
        # Nothing beyond the raw op id resolved -- no legibility to add.
        return None
    return " ".join(parts)
