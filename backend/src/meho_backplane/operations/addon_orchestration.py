# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Out-of-process audit parent-linkage for paired add-on orchestrations (#3028).

Initiative #2900 scope item 4. #2086 gave *in-process* dispatches a single
audit-replay subtree: the approval-resume path re-binds ``parent_audit_id`` /
``agent_session_id`` before a resumed dispatch, so the executed op's audit row
nests under the parking call instead of orphaning as a second root. A paired
add-on orchestrating from *outside* the backplane has no in-process parent
audit row to hang its per-step dispatches off — each ``call_operation`` it
issues would otherwise land as an independent replay root.

This module synthesises the missing parent. The first ``call_operation`` a
paired principal issues under a given ``work_ref`` **opens** an orchestration
run: one :class:`~meho_backplane.db.models.AddonOrchestrationRun` row keyed by
``(keycloak_client_id, work_ref)`` carrying a fresh ``session_id`` (the replay
anchor) and ``anchor_audit_id``, plus one ``audit_log`` "orchestration-root"
row written under those ids. Every subsequent dispatch for the same work_ref
**resolves** that run and binds the two ids around the dispatch (via
:func:`bound_parent_linkage`), so its DISPATCH audit row carries the shared
``agent_session_id`` and back-links to the anchor. ``/audit/sessions/{id}/
replay`` — which anchors on ``agent_session_id`` and descends by
``parent_audit_id`` — then reconstructs one subtree spanning the orchestration
and all resulting dispatches.

Authorization (the "only from the paired principal for its own work_refs"
contract):

* Linkage is offered **only** to a ``PrincipalKind.SERVICE`` principal whose
  recovered ``client_id`` resolves to a live :class:`AddonPairing` in the same
  tenant. A non-paired principal (or a user / agent token) gets ``None`` — no
  linkage, dispatches stay independent exactly as before.
* A run is keyed by the **caller's own** ``keycloak_client_id``, so a different
  paired principal presenting the same work_ref string opens (or resolves) its
  own run and can never attach to — or even observe — another add-on's subtree.

The row is the out-of-process analogue of ``ApprovalRequest``'s
``{request_audit_id, agent_session_id}`` durable copies (#2086): resolve once,
re-bind on every later dispatch. Audit stays synchronous append-only — the
anchor is an ordinary ``audit_log`` row (its own columns only; no
back-references), consistent with the v0.1-spec §6 discipline.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AddonOrchestrationRun, AuditLog
from meho_backplane.operations._audit import (
    agent_session_id_var,
    parent_audit_id_var,
)

# NB: ``addon_pairing`` is imported lazily inside
# :func:`resolve_or_open_orchestration_run`, not at module scope. It pulls in
# the ``addon_pairing_schemas -> runner_principals -> scheduler -> agent``
# chain, and the agent layer imports this package's ``meta_tools`` (which in
# turn imports *this* module) — an eager import here would close that cycle.
# The pairing lookup is only needed at dispatch time, so the deferral is free.

__all__ = [
    "ORCHESTRATION_METHOD",
    "ORCHESTRATION_PATH",
    "OrchestrationRun",
    "bound_parent_linkage",
    "resolve_or_open_orchestration_run",
]

_log = structlog.get_logger(__name__)

#: ``audit_log.method`` for the synthesised orchestration-root row. A new verb
#: alongside ``DISPATCH`` / ``APPROVAL``; ``method`` is free-form text (no CHECK
#: constraint), and audit queries surface the row by it.
ORCHESTRATION_METHOD = "ORCHESTRATION"

#: ``audit_log.path`` for the orchestration-root row — a stable op-shaped label.
ORCHESTRATION_PATH = "addon.orchestration"


@dataclass(frozen=True)
class OrchestrationRun:
    """The resolved parent-linkage anchor for one ``(client_id, work_ref)`` run."""

    session_id: uuid.UUID
    anchor_audit_id: uuid.UUID
    work_ref: str
    keycloak_client_id: str


def _to_run(row: AddonOrchestrationRun) -> OrchestrationRun:
    return OrchestrationRun(
        session_id=row.session_id,
        anchor_audit_id=row.anchor_audit_id,
        work_ref=row.work_ref,
        keycloak_client_id=row.keycloak_client_id,
    )


async def _select_run(keycloak_client_id: str, work_ref: str) -> OrchestrationRun | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(AddonOrchestrationRun).where(
                    AddonOrchestrationRun.keycloak_client_id == keycloak_client_id,
                    AddonOrchestrationRun.work_ref == work_ref,
                )
            )
        ).scalar_one_or_none()
    return _to_run(row) if row is not None else None


async def _open_run(
    *,
    operator: Operator,
    keycloak_client_id: str,
    work_ref: str,
) -> OrchestrationRun:
    """Insert the run row + its orchestration-root audit row in one transaction.

    Both land atomically so a run can never point at an anchor id that has no
    ``audit_log`` row (which would re-scatter replay into per-dispatch roots).
    On a lost resolve-or-open race the unique index raises ``IntegrityError``;
    the caller falls back to :func:`_select_run`.
    """
    session_id = uuid.uuid4()
    anchor_audit_id = uuid.uuid4()
    now = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AddonOrchestrationRun(
                tenant_id=operator.tenant_id,
                keycloak_client_id=keycloak_client_id,
                work_ref=work_ref,
                session_id=session_id,
                anchor_audit_id=anchor_audit_id,
                opened_by_sub=operator.sub,
                opened_at=now,
            )
        )
        # Flush the run first so its unique constraint fires before the anchor
        # audit row is added — a lost race leaves nothing to roll back.
        await session.flush()
        session.add(
            AuditLog(
                id=anchor_audit_id,
                occurred_at=now,
                operator_sub=operator.sub,
                tenant_id=operator.tenant_id,
                # Root of the run: no parent, carries the anchor session id so
                # replay seeds on it and every dispatch of the run descends here.
                parent_audit_id=None,
                agent_session_id=session_id,
                method=ORCHESTRATION_METHOD,
                path=ORCHESTRATION_PATH,
                status_code=200,
                request_id=None,
                duration_ms=Decimal("0.00"),
                payload={
                    "keycloak_client_id": keycloak_client_id,
                    "work_ref": work_ref,
                    "result_status": "opened",
                },
                work_ref=work_ref,
            )
        )
        await session.commit()
    _log.info(
        "addon_orchestration_run_opened",
        keycloak_client_id=keycloak_client_id,
        work_ref=work_ref,
        session_id=str(session_id),
    )
    return OrchestrationRun(
        session_id=session_id,
        anchor_audit_id=anchor_audit_id,
        work_ref=work_ref,
        keycloak_client_id=keycloak_client_id,
    )


async def resolve_or_open_orchestration_run(
    operator: Operator,
    work_ref: str,
) -> OrchestrationRun | None:
    """Resolve (or open) the parent-linkage anchor for *operator*'s *work_ref*.

    Returns ``None`` when the caller is not eligible for out-of-process linkage
    — i.e. it is not a paired add-on service principal for this tenant. In that
    case the dispatch keeps its pre-#3028 behaviour (an independent audit row).
    Only a ``PrincipalKind.SERVICE`` principal whose ``client_id`` matches a
    live :class:`AddonPairing` in the operator's own tenant is linked, and the
    run is keyed by that client_id so it is scoped to the caller's own
    work_refs.
    """
    client_id = operator.client_id
    if operator.principal_kind is not PrincipalKind.SERVICE or not client_id:
        return None

    # Deferred import — see the module-level note on the import cycle.
    from meho_backplane.operations.addon_pairing import AddonPairingService

    pairing = await AddonPairingService().get_by_client_id(client_id)
    if pairing is None or pairing.tenant_id != operator.tenant_id:
        # Not a paired principal (or a cross-tenant clientId collision that must
        # never link) — decline linkage rather than fabricating a subtree.
        return None

    existing = await _select_run(client_id, work_ref)
    if existing is not None:
        return existing
    try:
        return await _open_run(
            operator=operator,
            keycloak_client_id=client_id,
            work_ref=work_ref,
        )
    except IntegrityError as exc:
        from meho_backplane.operations.addon_pairing import _is_unique_violation

        if not _is_unique_violation(exc):
            raise
        # Lost the resolve-or-open race: a concurrent dispatch for the same
        # (client_id, work_ref) opened the run first. Its row now exists.
        raced = await _select_run(client_id, work_ref)
        if raced is None:  # pragma: no cover - unique violation implies a row
            raise
        return raced


@asynccontextmanager
async def bound_parent_linkage(run: OrchestrationRun) -> AsyncIterator[None]:
    """Bind the run's ``agent_session_id`` + ``parent_audit_id`` for a dispatch.

    Mirrors the approval-resume re-bind
    (:func:`~meho_backplane.operations.approval_queue._dispatch_resume_with_bound_context`):
    token-set both lineage contextvars, yield, then ``reset`` in a ``finally``
    so the binding never leaks past the dispatch.
    :func:`~meho_backplane.operations._audit.write_audit_row` reads exactly
    these vars into the DISPATCH row's columns, so no audit-writer change is
    needed — the row nests under the orchestration anchor automatically.
    """
    session_token = agent_session_id_var.set(run.session_id)
    parent_token = parent_audit_id_var.set(run.anchor_audit_id)
    try:
        yield
    finally:
        parent_audit_id_var.reset(parent_token)
        agent_session_id_var.reset(session_token)
