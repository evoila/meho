# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.operations.approval_queue`.

Initiative #803 (G11.2 Agent permission model), Task #817 (T4). Covers
the durable approval queue: create, approve, reject, expire, and the
two-audit-row invariant.

Test matrix
-----------

* **create_pending_request** -- inserts the pending row + one "request"
  audit row in the same transaction; the row's fields are populated correctly.
* **approve_request** -- flips status to ``approved``, stamps
  ``reviewed_by`` / ``decided_at``, writes one "decision" audit row.
* **reject_request** -- flips status to ``rejected``, same audit row.
* **Two-audit-row invariant** -- after approve or reject, exactly two
  audit rows exist: one ``approval.request`` + one ``approval.decision``.
* **Hash mismatch on approve** -- raises :class:`ParamsMismatchError`
  when the caller supplies different params.
* **Already-decided guard** -- raising on a non-pending row.
* **Tenant isolation** -- cross-tenant approve raises 404-equivalent.
* **Role gate** -- read_only operator raises :class:`UnauthorizedApprovalError`.
* **expire_stale_requests** -- transitions past-deadline rows to
  ``expired`` + writes decision audit rows.

Pause → approve → resume → execute
-----------------------------------

The "pause -> approve -> resume -> execute" acceptance criterion is
exercised by the ``test_pause_approve_resume_execute`` integration-style
test: it drives the full dispatcher → pending row → approve → re-dispatch
path against a registered typed op.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ApprovalRequestAlreadyDecidedError,
    ParamsMismatchError,
    SelfApprovalForbiddenError,
    UnauthorizedApprovalError,
    approve_request,
    create_pending_request,
    expire_stale_requests,
    list_pending,
    reject_request,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Module-level handlers (closures are rejected by derive_handler_ref)
# ---------------------------------------------------------------------------


async def _approval_test_ok_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Module-level handler for ``approval-test.op`` (integration tests)."""
    return {"result": "executed", "params": params}


async def _approval_test_dangerous_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Module-level handler for ``reject-test.op`` (integration tests)."""
    return {"executed": True}


#: Records each execution of ``_approval_test_recording_handler`` so a
#: test can assert the approved op ran exactly once (#1503). Reset at the
#: top of every test that uses it.
_RECORDED_EXECUTIONS: list[dict[str, Any]] = []


async def _approval_test_recording_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Append each call to ``_RECORDED_EXECUTIONS`` and echo the params (#1503).

    Lets a test prove a parked direct op approved via ``/decide`` or MCP
    by-id re-dispatched with the stored params and executed exactly once.
    """
    _RECORDED_EXECUTIONS.append({"params": params})
    return {"executed": True, "params": params}


async def _approval_test_severity_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Module-level handler for the ``safety_level`` legibility test (#1855)."""
    return {"executed": True}


async def _approval_test_target_reading_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Handler that reads ``target.name`` — proves the resume target resolves.

    G11.7-T1 #1401 resume-target hardening: a write op whose handler
    consumes the target object must receive the re-hydrated target on the
    approve re-dispatch, not ``None``. This handler echoes the target's
    name so a test can assert it survived the round-trip; a ``None``
    target would raise ``AttributeError`` and fail the dispatch.
    """
    return {"executed": True, "target_name": target.name, "params": params}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000b0b0")
_OTHER_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000c0c0")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Open session against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator(
    *,
    sub: str = "reviewer-sub",
    role: TenantRole = TenantRole.OPERATOR,
    tenant_id: uuid.UUID = _TENANT_ID,
    principal_kind: PrincipalKind = PrincipalKind.AGENT,
) -> Operator:
    # Defaults to an AGENT principal: the approval queue only fires for
    # agent principals (the G11.2-T3 gate hard-denies requires_approval
    # for human/service principals). Service-level tests that call the
    # approval API directly are unaffected by the kind.
    return Operator(
        sub=sub,
        name="Test Reviewer",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=principal_kind,
    )


# ---------------------------------------------------------------------------
# create_pending_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pending_request_inserts_row(session: AsyncSession) -> None:
    """create_pending_request inserts a pending row with correct fields."""
    operator = _make_operator(sub="agent-sub")
    params = {"key": "value"}
    params_hash = compute_params_hash(params)

    request = await create_pending_request(
        session,
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    assert request.id is not None
    assert request.status == ApprovalRequestStatus.PENDING.value
    assert request.op_id == "vault.kv.write"
    assert request.connector_id == "vault-1.x"
    assert request.params_hash == params_hash
    assert request.principal_sub == "agent-sub"
    assert request.tenant_id == _TENANT_ID
    assert request.run_id is None
    assert request.reviewed_by is None
    assert request.decided_at is None


@pytest.mark.asyncio
async def test_create_pending_request_records_principal_act_on_delegated_run(
    session: AsyncSession,
) -> None:
    """#1481 AC: a delegated agent run records ``principal_act=agent:<name>``.

    A human-initiated agent run binds the acting agent into the
    ``actor_delegation`` contextvar (RFC 8693 ``act``); the approval row
    must carry that actor so its ``principal_sub`` + ``principal_act``
    lineage matches the synchronous audit log's ``actor_sub``. The prior
    ``getattr(operator, "identity_act", None)`` dropped it (dead read of
    a nonexistent field).
    """
    # operator.sub is the *human* subject who triggered the run.
    operator = _make_operator(sub="human-sub", principal_kind=PrincipalKind.USER)

    with actor_delegation("agent:incident-bot"):
        request = await create_pending_request(
            session,
            operator=operator,
            connector_id="vault-1.x",
            op_id="vault.kv.write",
            target=None,
            params={"key": "value"},
            params_hash=compute_params_hash({"key": "value"}),
        )
    await session.commit()

    assert request.principal_sub == "human-sub"
    assert request.principal_act == "agent:incident-bot"


@pytest.mark.asyncio
async def test_create_pending_request_principal_act_null_without_delegation(
    session: AsyncSession,
) -> None:
    """#1481 AC (no regression): a direct call with no actor binding
    leaves ``principal_act`` NULL.

    Outside an ``actor_delegation`` block, ``resolve_actor_sub()``
    returns ``None`` — the correct value for a direct human request or
    an autonomous agent run (the agent is the subject, with no separate
    actor).
    """
    operator = _make_operator(sub="agent-sub")

    request = await create_pending_request(
        session,
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params={},
        params_hash=compute_params_hash({}),
    )
    await session.commit()

    assert request.principal_act is None


@pytest.mark.asyncio
async def test_create_pending_request_writes_request_audit_row(session: AsyncSession) -> None:
    """create_pending_request writes a synchronous 'request' audit row."""
    operator = _make_operator()
    params = {}
    params_hash = compute_params_hash(params)

    request = await create_pending_request(
        session,
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.pod.delete",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.request")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.method == "APPROVAL"
    assert row.status_code == 202
    assert row.payload["approval_request_id"] == str(request.id)
    assert row.payload["op_id"] == "k8s.pod.delete"


@pytest.mark.asyncio
async def test_create_pending_request_sets_run_id(session: AsyncSession) -> None:
    """run_id is stored on the pending row when provided."""
    run_id = uuid.uuid4()
    operator = _make_operator()
    request = await create_pending_request(
        session,
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params={},
        params_hash=compute_params_hash({}),
        run_id=run_id,
    )
    await session.commit()
    assert request.run_id == run_id


# ---------------------------------------------------------------------------
# work_ref (I2-T1 #1659)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pending_request_captures_bound_work_ref(
    session: AsyncSession,
) -> None:
    """A parked request captures the bound ``work_ref_var`` value.

    work_ref I2-T1 #1659 AC1: a USER principal with no standing grant
    dispatched under a bound work_ref parks an :class:`ApprovalRequest`
    carrying the matching ``work_ref``, surfaced on the read view and the
    MCP dict.
    """
    from meho_backplane.api.v1.approvals import _view
    from meho_backplane.mcp.tools.approvals import _row_to_dict

    operator = _make_operator(sub="human-sub", principal_kind=PrincipalKind.USER)
    token = work_ref_var.set("gh:evoila/meho#1659")
    try:
        request = await create_pending_request(
            session,
            operator=operator,
            connector_id="vault-1.x",
            op_id="vault.kv.write",
            target=None,
            params={"k": "v"},
            params_hash=compute_params_hash({"k": "v"}),
        )
        await session.commit()
    finally:
        work_ref_var.reset(token)

    assert request.work_ref == "gh:evoila/meho#1659"
    assert _view(request).work_ref == "gh:evoila/meho#1659"
    assert _row_to_dict(request)["work_ref"] == "gh:evoila/meho#1659"


@pytest.mark.asyncio
async def test_create_pending_request_work_ref_null_when_unbound(
    session: AsyncSession,
) -> None:
    """No bound work_ref → the parked row's ``work_ref`` is NULL."""
    operator = _make_operator()
    request = await create_pending_request(
        session,
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params={},
        params_hash=compute_params_hash({}),
    )
    await session.commit()
    assert request.work_ref is None


@pytest.mark.asyncio
async def test_list_pending_filters_by_work_ref(session: AsyncSession) -> None:
    """``list_pending(work_ref=...)`` returns only exact-match requests.

    work_ref I2-T1 #1659 AC2 (service layer behind ``meho approvals list
    --work-ref``): an exact ``work_ref`` filter narrows to the matching
    requests; an unrelated ref returns none.
    """
    operator = _make_operator()

    async def _park(ref: str | None) -> None:
        token = work_ref_var.set(ref) if ref is not None else None
        try:
            await create_pending_request(
                session,
                operator=operator,
                connector_id="vault-1.x",
                op_id="vault.kv.write",
                target=None,
                params={"r": ref},
                params_hash=compute_params_hash({"r": ref}),
            )
        finally:
            if token is not None:
                work_ref_var.reset(token)

    await _park("gh:evoila/meho#10")
    await _park("gh:evoila/meho#20")
    await _park(None)
    await session.commit()

    matched = await list_pending(
        session,
        tenant_id=_TENANT_ID,
        status=None,
        work_ref="gh:evoila/meho#10",
    )
    assert len(matched) == 1
    assert matched[0].work_ref == "gh:evoila/meho#10"

    none_matched = await list_pending(
        session,
        tenant_id=_TENANT_ID,
        status=None,
        work_ref="gh:evoila/meho#999",
    )
    assert none_matched == []

    all_rows = await list_pending(session, tenant_id=_TENANT_ID, status=None)
    assert len(all_rows) == 3


# ---------------------------------------------------------------------------
# approve_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_request_flips_status(session: AsyncSession) -> None:
    """approve_request transitions the row to 'approved'."""
    # Requester != approver so the self-approval guard (G11.7-T1 #1401)
    # does not fire — this test exercises the happy-path status flip.
    requester = _make_operator(sub="requester-sub")
    reviewer = _make_operator(sub="reviewer-sub")
    params = {"name": "test"}
    params_hash = compute_params_hash(params)

    pending = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        updated = await approve_request(s2, pending.id, operator=reviewer, params=params)
        await s2.commit()

    assert updated.status == ApprovalRequestStatus.APPROVED.value
    assert updated.reviewed_by == reviewer.sub
    assert updated.decided_at is not None


@pytest.mark.asyncio
async def test_approve_request_writes_decision_audit_row(session: AsyncSession) -> None:
    """approve_request writes a 'decision' audit row synchronously."""
    requester = _make_operator(sub="requester-sub")
    reviewer = _make_operator(sub="reviewer-sub")
    params = {}
    params_hash = compute_params_hash(params)

    pending = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        await approve_request(s2, pending.id, operator=reviewer, params=params)
        await s2.commit()

    # Two audit rows total: one request + one decision.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        request_rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.request")))
            .scalars()
            .all()
        )
        decision_rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    assert len(request_rows) == 1
    assert len(decision_rows) == 1
    assert decision_rows[0].status_code == 200
    assert decision_rows[0].payload["decision"] == "approved"


@pytest.mark.asyncio
async def test_approve_request_records_reason_in_decision_payload(
    session: AsyncSession,
) -> None:
    """A non-empty approve ``reason`` lands in the decision audit payload;
    omitting it leaves the payload free of a ``reason`` key — mirroring reject.
    """
    requester = _make_operator(sub="requester-sub")
    reviewer = _make_operator(sub="reviewer-sub")
    params = {}
    params_hash = compute_params_hash(params)

    with_reason = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    without_reason = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        await approve_request(
            s2, with_reason.id, operator=reviewer, params=params, reason="rollback window approved"
        )
        await approve_request(s2, without_reason.id, operator=reviewer, params=params)
        await s2.commit()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        decision_rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    by_request = {row.payload["approval_request_id"]: row.payload for row in decision_rows}
    assert by_request[str(with_reason.id)]["decision"] == "approved"
    assert by_request[str(with_reason.id)]["reason"] == "rollback window approved"
    # Omitting the reason leaves the payload unchanged from today (no key).
    assert "reason" not in by_request[str(without_reason.id)]


@pytest.mark.asyncio
async def test_approve_and_reject_reason_payloads_are_symmetric(
    session: AsyncSession,
) -> None:
    """approve and reject decision-row payloads carry the ``reason`` field
    in the same structural shape (side-by-side assertion)."""
    requester = _make_operator(sub="requester-sub")
    reviewer = _make_operator(sub="reviewer-sub")
    params = {}
    params_hash = compute_params_hash(params)

    to_approve = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    to_reject = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.delete",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        await approve_request(
            s2, to_approve.id, operator=reviewer, params=params, reason="why-approve"
        )
        await reject_request(s2, to_reject.id, operator=reviewer, reason="why-reject")
        await s2.commit()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = {
            row.payload["approval_request_id"]: row.payload
            for row in (
                await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.decision"))
            )
            .scalars()
            .all()
        }
    approve_payload = rows[str(to_approve.id)]
    reject_payload = rows[str(to_reject.id)]
    assert approve_payload["decision"] == "approved"
    assert reject_payload["decision"] == "rejected"
    assert approve_payload["reason"] == "why-approve"
    assert reject_payload["reason"] == "why-reject"
    # Same key set up to the decision-specific value.
    assert set(approve_payload) == set(reject_payload)


@pytest.mark.asyncio
async def test_approve_request_raises_on_hash_mismatch(session: AsyncSession) -> None:
    """Supplying different params raises ParamsMismatchError."""
    requester = _make_operator(sub="requester-sub")
    operator = _make_operator(sub="reviewer-sub")
    original_params = {"a": 1}
    params_hash = compute_params_hash(original_params)

    pending = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=original_params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        with pytest.raises(ParamsMismatchError):
            await approve_request(
                s2,
                pending.id,
                operator=operator,
                params={"a": 99},  # different!
            )


@pytest.mark.asyncio
async def test_approve_request_params_none_skips_hash_check(
    session: AsyncSession,
) -> None:
    """``params=None`` skips the hash check (G11.2-T5 operator-decision path).

    The MCP/CLI operator does not have the agent's original params and
    approves by id alone. ``approve_request`` skips the hash verification
    when ``params is None`` and still flips status + writes the decision
    audit row. The agent's REST path keeps supplying params (the swap
    defence stays on the agent branch).
    """
    requester = _make_operator(sub="requester-sub")
    operator = _make_operator(sub="reviewer-sub")
    original_params = {"x": 1, "secret": "z"}
    params_hash = compute_params_hash(original_params)
    pending = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=original_params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        # No params supplied — must NOT raise ParamsMismatchError.
        row = await approve_request(s2, pending.id, operator=operator, params=None)
        await s2.commit()
        assert row.status == ApprovalRequestStatus.APPROVED.value
        # The transient audit_id attr is exposed so callers can publish the
        # broadcast event AFTER commit, with the real audit row's id.
        assert isinstance(row._audit_id, uuid.UUID)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_publish_approval_event_audit_id_matches_decision_row(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The broadcast event's ``audit_id`` is the decision row's real id.

    BroadcastEvent.audit_id is documented as the FK to ``audit_log.id``; a
    subscriber that wants the full row queries audit_log by this id. The
    helper must therefore receive the audit row's actual id (threaded
    via ``request._audit_id``) — not a fresh UUID. This test stubs the
    broadcast publisher, exercises approve_request + the publish call,
    and asserts the published event's audit_id equals the audit row's
    primary key.
    """
    captured: list[Any] = []

    async def _capture(event: Any) -> None:
        captured.append(event)

    monkeypatch.setattr("meho_backplane.broadcast.publisher.publish_event", _capture)
    from meho_backplane.operations.approval_queue import publish_approval_event

    requester = _make_operator(sub="requester-sub")
    operator = _make_operator(sub="reviewer-sub")
    original_params = {"x": 1}
    params_hash = compute_params_hash(original_params)
    pending = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=original_params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        row = await approve_request(s2, pending.id, operator=operator, params=None)
        await s2.commit()
        decision_audit_id: uuid.UUID = row._audit_id  # type: ignore[attr-defined]
        await publish_approval_event(
            tenant_id=operator.tenant_id,
            request=row,
            decision="approved",
            principal_sub=operator.sub,
            audit_id=decision_audit_id,
        )

    # The event's audit_id must be the real decision audit row's id.
    assert len(captured) == 1
    assert captured[0].audit_id == decision_audit_id
    # And the audit_log row at that id must exist.
    async with get_sessionmaker()() as s3:
        audited = await s3.get(AuditLog, decision_audit_id)
        assert audited is not None
        assert audited.path == "approval.decision"


@pytest.mark.asyncio
async def test_approve_request_raises_on_already_decided(session: AsyncSession) -> None:
    """Approving an already-approved row raises ApprovalRequestAlreadyDecidedError."""
    requester = _make_operator(sub="requester-sub")
    operator = _make_operator(sub="reviewer-sub")
    params = {}
    params_hash = compute_params_hash(params)

    pending = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        await approve_request(s2, pending.id, operator=operator, params=params)
        await s2.commit()

    async with get_sessionmaker()() as s3:
        with pytest.raises(ApprovalRequestAlreadyDecidedError) as exc_info:
            await approve_request(s3, pending.id, operator=operator, params=params)
    assert exc_info.value.status == "approved"


@pytest.mark.asyncio
async def test_approve_request_raises_not_found_on_missing(session: AsyncSession) -> None:
    """Approving a non-existent id raises ApprovalNotFoundError."""
    operator = _make_operator()
    async with get_sessionmaker()() as s:
        with pytest.raises(ApprovalNotFoundError):
            await approve_request(s, uuid.uuid4(), operator=operator, params={})


@pytest.mark.asyncio
async def test_approve_request_tenant_isolation(session: AsyncSession) -> None:
    """Approve from a different tenant raises ApprovalNotFoundError (tenant isolation)."""
    owner_op = _make_operator(tenant_id=_TENANT_ID)
    params = {}
    params_hash = compute_params_hash(params)

    pending = await create_pending_request(
        session,
        operator=owner_op,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    other_op = _make_operator(tenant_id=_OTHER_TENANT_ID)
    async with get_sessionmaker()() as s2:
        with pytest.raises(ApprovalNotFoundError):
            await approve_request(s2, pending.id, operator=other_op, params=params)


# ---------------------------------------------------------------------------
# reject_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_request_flips_status(session: AsyncSession) -> None:
    """reject_request transitions the row to 'rejected'."""
    operator = _make_operator()
    params = {}
    params_hash = compute_params_hash(params)

    pending = await create_pending_request(
        session,
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.delete",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        updated = await reject_request(s2, pending.id, operator=operator, reason="too risky")
        await s2.commit()

    assert updated.status == ApprovalRequestStatus.REJECTED.value
    assert updated.reviewed_by == operator.sub
    assert updated.decided_at is not None


@pytest.mark.asyncio
async def test_reject_request_writes_decision_audit_row(session: AsyncSession) -> None:
    """reject_request writes one 'decision' audit row with decision='rejected'."""
    operator = _make_operator()
    params = {}
    params_hash = compute_params_hash(params)

    pending = await create_pending_request(
        session,
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.delete",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        await reject_request(s2, pending.id, operator=operator, reason="forbidden")
        await s2.commit()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        decision_rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    assert len(decision_rows) == 1
    assert decision_rows[0].status_code == 403
    assert decision_rows[0].payload["decision"] == "rejected"
    assert decision_rows[0].payload["reason"] == "forbidden"


@pytest.mark.asyncio
async def test_reject_request_raises_on_already_decided(session: AsyncSession) -> None:
    """Rejecting an already-rejected row raises ApprovalRequestAlreadyDecidedError."""
    operator = _make_operator()
    params = {}
    params_hash = compute_params_hash(params)

    pending = await create_pending_request(
        session,
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.delete",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        await reject_request(s2, pending.id, operator=operator)
        await s2.commit()

    async with get_sessionmaker()() as s3:
        with pytest.raises(ApprovalRequestAlreadyDecidedError):
            await reject_request(s3, pending.id, operator=operator)


# ---------------------------------------------------------------------------
# Role gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_only_operator_cannot_approve(session: AsyncSession) -> None:
    """read_only operator raises UnauthorizedApprovalError on approve."""
    owner_op = _make_operator()
    params = {}
    params_hash = compute_params_hash(params)

    pending = await create_pending_request(
        session,
        operator=owner_op,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    read_only_op = _make_operator(role=TenantRole.READ_ONLY)
    async with get_sessionmaker()() as s2:
        with pytest.raises(UnauthorizedApprovalError):
            await approve_request(s2, pending.id, operator=read_only_op, params=params)


@pytest.mark.asyncio
async def test_read_only_operator_cannot_reject(session: AsyncSession) -> None:
    """read_only operator raises UnauthorizedApprovalError on reject."""
    owner_op = _make_operator()
    params = {}
    params_hash = compute_params_hash(params)

    pending = await create_pending_request(
        session,
        operator=owner_op,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    read_only_op = _make_operator(role=TenantRole.READ_ONLY)
    async with get_sessionmaker()() as s2:
        with pytest.raises(UnauthorizedApprovalError):
            await reject_request(s2, pending.id, operator=read_only_op)


# ---------------------------------------------------------------------------
# Self-approval guard (G11.7-T1 #1401)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_approval_forbidden_by_default(session: AsyncSession) -> None:
    """The requester may not approve their own request (default fail-closed).

    ``approval_allow_self_approval`` defaults to ``False`` — the operator
    who parked the request (``principal_sub``) gets
    :class:`SelfApprovalForbiddenError` (mapped to 403 at the route
    layer) when they try to approve it. The role check passes (the
    requester is an OPERATOR), so this isolates the requester != approver
    guard.
    """
    requester = _make_operator(sub="solo-operator", role=TenantRole.OPERATOR)
    params = {"name": "test"}
    params_hash = compute_params_hash(params)

    pending = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.put",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        with pytest.raises(SelfApprovalForbiddenError):
            await approve_request(s2, pending.id, operator=requester, params=params)

    # The row stays pending — a refused self-approval is not a decision.
    async with get_sessionmaker()() as s3:
        row = await s3.get(ApprovalRequest, pending.id)
        assert row is not None
        assert row.status == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_self_approval_allowed_under_break_glass(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break-glass config lets a single operator self-approve, still audited.

    ``APPROVAL_ALLOW_SELF_APPROVAL=true`` opts into the audited
    single-operator mode: the self-approval succeeds (status flips) AND
    still writes its decision audit row, so the break-glass use is
    forensically visible.
    """
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    get_settings.cache_clear()

    requester = _make_operator(sub="solo-operator", role=TenantRole.OPERATOR)
    params = {"name": "test"}
    params_hash = compute_params_hash(params)

    pending = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.put",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        row = await approve_request(s2, pending.id, operator=requester, params=params)
        await s2.commit()
    assert row.status == ApprovalRequestStatus.APPROVED.value
    assert row.reviewed_by == requester.sub

    # Decision audit row still landed — break-glass is not silent.
    async with get_sessionmaker()() as s3:
        decision_rows = (
            (await s3.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    assert len(decision_rows) == 1
    assert decision_rows[0].payload["decision"] == "approved"


@pytest.mark.asyncio
async def test_self_reject_is_always_allowed(session: AsyncSession) -> None:
    """An operator may reject (withdraw) their own request regardless of config.

    Reject is unguarded — withdrawing one's own pending request is never
    a privilege escalation, so the self-approval guard does not apply to
    :func:`reject_request`.
    """
    requester = _make_operator(sub="solo-operator", role=TenantRole.OPERATOR)
    params = {"name": "test"}
    params_hash = compute_params_hash(params)

    pending = await create_pending_request(
        session,
        operator=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.put",
        target=None,
        params=params,
        params_hash=params_hash,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        row = await reject_request(s2, pending.id, operator=requester, reason="changed my mind")
        await s2.commit()
    assert row.status == ApprovalRequestStatus.REJECTED.value


# ---------------------------------------------------------------------------
# expire_stale_requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expire_stale_requests_transitions_past_deadline_rows(
    session: AsyncSession,
) -> None:
    """expire_stale_requests transitions past-expires_at rows to 'expired'."""
    operator = _make_operator()
    params = {}
    params_hash = compute_params_hash(params)

    past = datetime.now(UTC) - timedelta(hours=1)
    future = datetime.now(UTC) + timedelta(hours=1)

    pending_stale = await create_pending_request(
        session,
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.expire-stale",
        target=None,
        params=params,
        params_hash=params_hash,
        expires_at=past,
    )
    pending_fresh = await create_pending_request(
        session,
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.expire-fresh",
        target=None,
        params=params,
        params_hash=params_hash,
        expires_at=future,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        expired = await expire_stale_requests(s2, operator=operator)
        await s2.commit()

    assert len(expired) == 1
    assert expired[0].id == pending_stale.id

    # Fresh row is still pending.
    async with get_sessionmaker()() as fresh:
        fresh_row = await fresh.get(ApprovalRequest, pending_fresh.id)
        assert fresh_row is not None
        assert fresh_row.status == ApprovalRequestStatus.PENDING.value

        stale_row = await fresh.get(ApprovalRequest, pending_stale.id)
        assert stale_row is not None
        assert stale_row.status == ApprovalRequestStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_expire_stale_requests_writes_decision_audit_rows(
    session: AsyncSession,
) -> None:
    """expire_stale_requests writes one 'decision' audit row per expired request."""
    operator = _make_operator()
    past = datetime.now(UTC) - timedelta(minutes=5)

    await create_pending_request(
        session,
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.expire-audit",
        target=None,
        params={},
        params_hash=compute_params_hash({}),
        expires_at=past,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        await expire_stale_requests(s2, operator=operator)
        await s2.commit()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        decision_rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    assert len(decision_rows) == 1
    assert decision_rows[0].payload["decision"] == "expired"
    assert decision_rows[0].status_code == 410


@pytest.mark.asyncio
async def test_expire_stale_requests_respects_tenant_isolation(
    session: AsyncSession,
) -> None:
    """expire_stale_requests only touches rows owned by the operator's tenant."""
    op_a = _make_operator(tenant_id=_TENANT_ID)
    op_b = _make_operator(tenant_id=_OTHER_TENANT_ID)
    past = datetime.now(UTC) - timedelta(hours=1)

    pending_a = await create_pending_request(
        session,
        operator=op_a,
        connector_id="vault-1.x",
        op_id="vault.kv.expire-a",
        target=None,
        params={},
        params_hash=compute_params_hash({}),
        expires_at=past,
    )
    await session.commit()

    # Expire with op_b's tenant -- should not touch op_a's row.
    async with get_sessionmaker()() as s2:
        expired = await expire_stale_requests(s2, operator=op_b)
        await s2.commit()

    assert len(expired) == 0

    async with get_sessionmaker()() as fresh:
        row = await fresh.get(ApprovalRequest, pending_a.id)
        assert row is not None
        assert row.status == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_expire_stale_requests_publishes_approval_expired_broadcast(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller publishes one ``approval.expired`` event per expired row with FK audit_id.

    #1114 follow-up to T4: the fourth lifecycle transition (expiry) now
    surfaces on the broadcast feed alongside pending / approved /
    rejected. The function exposes ``request._audit_id`` (the real
    decision row's primary key) so the caller can publish **after
    commit** — same publish-after-commit invariant the other three
    transitions follow. The broadcast event's ``audit_id`` field is
    documented as the FK to ``audit_log.id``; subscribers that want the
    full row query audit_log by this id.
    """
    captured: list[Any] = []

    async def _capture(event: Any) -> None:
        captured.append(event)

    monkeypatch.setattr("meho_backplane.broadcast.publisher.publish_event", _capture)
    from meho_backplane.operations.approval_queue import publish_approval_event

    operator = _make_operator()
    past = datetime.now(UTC) - timedelta(hours=1)

    pending = await create_pending_request(
        session,
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.expire-broadcast",
        target=None,
        params={},
        params_hash=compute_params_hash({}),
        expires_at=past,
    )
    await session.commit()

    async with get_sessionmaker()() as s2:
        expired = await expire_stale_requests(s2, operator=operator)
        await s2.commit()

    assert len(expired) == 1
    assert expired[0].id == pending.id
    decision_audit_id: uuid.UUID = expired[0]._audit_id  # type: ignore[attr-defined]
    assert isinstance(decision_audit_id, uuid.UUID)

    # Publish AFTER commit, mirroring the dispatcher / REST / MCP sites.
    for row in expired:
        await publish_approval_event(
            tenant_id=operator.tenant_id,
            request=row,
            decision="expired",
            principal_sub=operator.sub,
            audit_id=row._audit_id,  # type: ignore[attr-defined]
        )

    # Exactly one ``approval.expired`` event landed with the FK audit_id.
    assert len(captured) == 1
    event = captured[0]
    assert event.op_id == "approval.expired"
    assert event.audit_id == decision_audit_id
    assert event.tenant_id == operator.tenant_id
    assert event.payload["decision"] == "expired"
    assert event.payload["approval_request_id"] == str(pending.id)

    # The audit_log row at that id exists and is the expiry decision row.
    async with get_sessionmaker()() as fresh:
        audited = await fresh.get(AuditLog, decision_audit_id)
        assert audited is not None
        assert audited.path == "approval.decision"
        assert audited.status_code == 410
        assert audited.payload["decision"] == "expired"


# ---------------------------------------------------------------------------
# pause → approve → resume → execute (integration-style)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_approve_resume_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full pause → approve → resume → execute path via the dispatcher.

    Drives the dispatcher with a ``requires_approval=True`` typed op:

    1. First dispatch: policy gate fires → pending row created → result
       is ``awaiting_approval``.
    2. approve_request: row flips to ``approved`` + decision audit row.
    3. Second dispatch (re-dispatch via approve route logic): op executes
       normally → result is ``ok``.
    """
    from unittest.mock import AsyncMock

    import meho_backplane.operations._audit as audit_module
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import clear_registry, register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
    from meho_backplane.operations import (
        dispatch,
        register_typed_operation,
        reset_dispatcher_caches,
    )

    # Stub out broadcast.
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()

    captured: list[Any] = []

    async def _capture(event: Any) -> None:
        captured.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)

    reset_dispatcher_caches()
    clear_registry()

    class _OkConnector(Connector):
        product = "apptest"
        version = "1.x"
        impl_id = "apptest"
        priority = 10

        async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
            return FingerprintResult(
                probe=ProbeResult(reachable=True, probe_method="none"),
                product="apptest",
                version="1.x",
            )

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(  # type: ignore[override]
            self,
            target: Any,
            op_id: str,
            params: dict[str, Any],
        ) -> Any:
            # Typed op: the registered handler does the work, not this.
            raise NotImplementedError

    register_connector_v2(
        product="apptest",
        version="",
        impl_id="",
        cls=_OkConnector,
    )

    stub_emb = AsyncMock()
    stub_emb.encode_one.return_value = [0.1] * 384
    stub_emb.encode.return_value = [[0.1] * 384]
    stub_emb.dimension = 384

    await register_typed_operation(
        product="apptest",
        version="1.x",
        impl_id="apptest",
        op_id="apptest.op",
        handler=_approval_test_ok_handler,
        summary="Test op requiring approval.",
        description="Test.",
        parameter_schema={"type": "object"},
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_emb,
    )

    operator = _make_operator(sub="agent-run-sub")

    class _FakeTarget:
        product = "apptest"
        id = uuid.UUID("00000000-0000-0000-0000-000000000001")

    target = _FakeTarget()
    params = {"x": 42}

    # Step 1: first dispatch → awaiting_approval.
    result1 = await dispatch(
        operator=operator,
        connector_id="apptest-1.x",
        op_id="apptest.op",
        target=target,
        params=params,
    )
    assert result1.status == "awaiting_approval"
    approval_request_id = uuid.UUID(result1.extras["approval_request_id"])

    # Step 2: approve the request. A distinct human reviewer approves —
    # the requester (the agent run) may not approve its own request
    # (self-approval guard, G11.7-T1 #1401).
    reviewer = _make_operator(sub="human-reviewer-sub", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(s, approval_request_id, operator=reviewer, params=params)
        await s.commit()
    assert row.status == ApprovalRequestStatus.APPROVED.value

    # Step 3: re-dispatch the approved op via the bypass the approval route
    # uses (``_approved=True``). The descriptor still has
    # requires_approval=True, so a naive re-dispatch would re-queue; the
    # bypass — set only after a human approval — skips the gate so the op
    # actually executes (#817 DoD: "approval runs the original dispatch").
    result2 = await dispatch(
        operator=operator,
        connector_id="apptest-1.x",
        op_id="apptest.op",
        target=target,
        params=params,
        _approved=True,
    )
    assert result2.status == "ok"

    reset_dispatcher_caches()
    clear_registry()


@pytest.mark.asyncio
async def test_work_ref_inherited_through_park_approve_redispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """work_ref survives park → decide → re-dispatch onto every audit row.

    work_ref I2-T1 #1659 AC3: a USER op parked under a bound work_ref,
    approved by a different operator and re-dispatched via the shared
    :func:`resume_dispatch_after_approval` helper, stamps the same
    ``work_ref`` onto the decision audit row AND the re-dispatched op's
    DISPATCH audit row — even though the approving / resuming task has no
    work_ref bound on its own ContextVar.
    """
    from unittest.mock import AsyncMock

    import meho_backplane.operations._audit as audit_module
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import clear_registry, register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
    from meho_backplane.operations import (
        dispatch,
        register_typed_operation,
        reset_dispatcher_caches,
    )
    from meho_backplane.operations.approval_queue import resume_dispatch_after_approval

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()

    async def _capture(event: Any) -> None:
        pass

    monkeypatch.setattr(audit_module, "publish_event", _capture)

    reset_dispatcher_caches()
    clear_registry()

    class _OkConnector(Connector):
        product = "reftest"
        version = "1.x"
        impl_id = "reftest"
        priority = 10

        async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
            return FingerprintResult(
                probe=ProbeResult(reachable=True, probe_method="none"),
                product="reftest",
                version="1.x",
            )

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(  # type: ignore[override]
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> Any:
            raise NotImplementedError

    register_connector_v2(product="reftest", version="", impl_id="", cls=_OkConnector)

    stub_emb = AsyncMock()
    stub_emb.encode_one.return_value = [0.1] * 384
    stub_emb.encode.return_value = [[0.1] * 384]
    stub_emb.dimension = 384

    await register_typed_operation(
        product="reftest",
        version="1.x",
        impl_id="reftest",
        op_id="reftest.op",
        handler=_approval_test_ok_handler,
        summary="Test op requiring approval.",
        description="Test.",
        parameter_schema={"type": "object"},
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_emb,
    )

    work_ref = "gh:evoila/meho#1659"
    requester = _make_operator(sub="human-requester", principal_kind=PrincipalKind.USER)
    params = {"x": 7}

    # Step 1: park the op under a bound work_ref.
    token = work_ref_var.set(work_ref)
    try:
        result1 = await dispatch(
            operator=requester,
            connector_id="reftest-1.x",
            op_id="reftest.op",
            target=None,
            params=params,
        )
    finally:
        work_ref_var.reset(token)
    assert result1.status == "awaiting_approval"
    approval_request_id = uuid.UUID(result1.extras["approval_request_id"])

    # Step 2: a different operator approves — its task has no work_ref
    # bound, so the decision audit row must source the ref from the row.
    reviewer = _make_operator(sub="human-reviewer", principal_kind=PrincipalKind.USER)
    assert work_ref_var.get() is None
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, approval_request_id, operator=reviewer)
        await s.commit()
    assert approved.work_ref == work_ref

    # Step 3: re-dispatch via the shared resume helper (var still unset on
    # this task). The helper re-binds the row's work_ref around dispatch.
    assert work_ref_var.get() is None
    result2 = await resume_dispatch_after_approval(operator=reviewer, request=approved)
    assert result2.status == "ok"
    # The re-bind is scoped: the var is reset after the re-dispatch.
    assert work_ref_var.get() is None

    # Assert: the decision audit row AND the re-dispatched DISPATCH audit
    # row both carry the work_ref.
    async with get_sessionmaker()() as fresh:
        decision_row = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .one()
        )
        dispatch_rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.method != "APPROVAL")))
            .scalars()
            .all()
        )
    assert decision_row.work_ref == work_ref
    assert dispatch_rows, "expected a re-dispatched DISPATCH audit row"
    assert all(r.work_ref == work_ref for r in dispatch_rows)

    reset_dispatcher_caches()
    clear_registry()


# ---------------------------------------------------------------------------
# Human principal: queue (not hard-deny) + resume with explicit target
# (G11.7-T1 #1401, integration-style)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_requires_approval_queues_not_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A USER principal hitting requires_approval is queued, not denied (AC1).

    Pre-G11.7 the policy gate hard-denied a human/service principal on a
    ``requires_approval`` op (``status=denied``). This drives the full
    round-trip for a USER principal:

    1. First dispatch → ``awaiting_approval`` (queued + resumable), NOT
       ``denied``.
    2. A distinct human reviewer approves the parked request.
    3. The approve re-dispatch (``_approved=True``) re-hydrates the
       stored target by id and runs the op — the handler reads
       ``target.name``, proving the resume target resolved (AC3) rather
       than passing ``None``.
    """
    from unittest.mock import AsyncMock

    import meho_backplane.operations._audit as audit_module
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import clear_registry, register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
    from meho_backplane.db.models import Target as TargetORM
    from meho_backplane.operations import (
        dispatch,
        register_typed_operation,
        reset_dispatcher_caches,
    )
    from meho_backplane.targets.resolver import resolve_target_by_id

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()

    async def _capture(event: Any) -> None:
        pass

    monkeypatch.setattr(audit_module, "publish_event", _capture)

    reset_dispatcher_caches()
    clear_registry()

    class _OkConnector(Connector):
        product = "humantest"
        version = "1.x"
        impl_id = "humantest"
        priority = 10

        async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
            return FingerprintResult(
                probe=ProbeResult(reachable=True, probe_method="none"),
                product="humantest",
                version="1.x",
            )

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(  # type: ignore[override]
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> Any:
            raise NotImplementedError

    register_connector_v2(product="humantest", version="", impl_id="", cls=_OkConnector)

    stub_emb = AsyncMock()
    stub_emb.encode_one.return_value = [0.1] * 384
    stub_emb.encode.return_value = [[0.1] * 384]
    stub_emb.dimension = 384

    await register_typed_operation(
        product="humantest",
        version="1.x",
        impl_id="humantest",
        op_id="humantest.write",
        handler=_approval_test_target_reading_handler,
        summary="Write op requiring approval.",
        description="Test.",
        parameter_schema={"type": "object"},
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_emb,
    )

    # Persist a real Target row so resolve_target_by_id can re-hydrate it
    # on the resume re-dispatch.
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name="prod-vault",
                product="humantest",
                host="vault.prod.invalid",
                aliases=[],
            )
        )
        await s.commit()

    # The operator who runs the op is a *human* (USER) principal.
    requester = _make_operator(sub="ops-human", principal_kind=PrincipalKind.USER)

    class _Target:
        product = "humantest"
        id = target_id
        name = "prod-vault"

    params = {"path": "secret/db"}

    # Step 1: human dispatch → awaiting_approval (NOT denied).
    result1 = await dispatch(
        operator=requester,
        connector_id="humantest-1.x",
        op_id="humantest.write",
        target=_Target(),
        params=params,
    )
    assert result1.status == "awaiting_approval", result1.error
    assert result1.status != "denied"
    approval_request_id = uuid.UUID(result1.extras["approval_request_id"])

    # The pending row stored the target_id for the resume path.
    async with get_sessionmaker()() as s:
        pending_row = await s.get(ApprovalRequest, approval_request_id)
        assert pending_row is not None
        assert pending_row.target_id == target_id

    # Step 2: a distinct human reviewer approves.
    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(s, approval_request_id, operator=reviewer, params=params)
        await s.commit()
    assert row.status == ApprovalRequestStatus.APPROVED.value

    # Step 3: resume — re-hydrate the target by id and re-dispatch. The
    # handler reads target.name, so a None target would crash the op.
    async with get_sessionmaker()() as s:
        resolved = await resolve_target_by_id(s, _TENANT_ID, row.target_id)
    assert resolved is not None
    assert resolved.name == "prod-vault"

    result2 = await dispatch(
        operator=reviewer,
        connector_id="humantest-1.x",
        op_id="humantest.write",
        target=resolved,
        params=params,
        _approved=True,
    )
    assert result2.status == "ok", result2.error
    assert result2.result is not None
    assert result2.result["target_name"] == "prod-vault"  # type: ignore[index]

    reset_dispatcher_caches()
    clear_registry()


# ---------------------------------------------------------------------------
# pause → reject → abort (integration-style)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_reject_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full pause → reject → abort path.

    A ``requires_approval`` dispatch parks → reviewer rejects → row is
    ``rejected``; a second audit row (decision) exists; the original op
    is not executed.
    """
    from unittest.mock import AsyncMock

    import meho_backplane.operations._audit as audit_module
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import clear_registry, register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
    from meho_backplane.operations import (
        dispatch,
        register_typed_operation,
        reset_dispatcher_caches,
    )

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()

    async def _capture(event: Any) -> None:
        pass

    monkeypatch.setattr(audit_module, "publish_event", _capture)

    reset_dispatcher_caches()
    clear_registry()

    class _TrackingConnector(Connector):
        product = "rejecttest"
        version = "1.x"
        impl_id = "rejecttest"
        priority = 10

        async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
            return FingerprintResult(
                probe=ProbeResult(reachable=True, probe_method="none"),
                product="rejecttest",
                version="1.x",
            )

    register_connector_v2(
        product="rejecttest",
        version="",
        impl_id="",
        cls=_TrackingConnector,
    )

    stub_emb = AsyncMock()
    stub_emb.encode_one.return_value = [0.1] * 384
    stub_emb.encode.return_value = [[0.1] * 384]
    stub_emb.dimension = 384

    await register_typed_operation(
        product="rejecttest",
        version="1.x",
        impl_id="rejecttest",
        op_id="rejecttest.op",
        handler=_approval_test_dangerous_handler,
        summary="Dangerous.",
        description="Test.",
        parameter_schema={"type": "object"},
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_emb,
    )

    operator = _make_operator(sub="reject-agent")
    params = {"target": "vm-1"}

    # Step 1: dispatch → pending.
    result1 = await dispatch(
        operator=operator,
        connector_id="rejecttest-1.x",
        op_id="rejecttest.op",
        target=None,
        params=params,
    )
    assert result1.status == "awaiting_approval"
    approval_request_id = uuid.UUID(result1.extras["approval_request_id"])

    # Step 2: reject the request.
    async with get_sessionmaker()() as s:
        row = await reject_request(s, approval_request_id, operator=operator, reason="too risky")
        await s.commit()

    assert row.status == ApprovalRequestStatus.REJECTED.value

    # Verify two audit rows: request + decision.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        req_rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.request")))
            .scalars()
            .all()
        )
        dec_rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    assert len(req_rows) == 1
    assert len(dec_rows) == 1
    assert dec_rows[0].payload["decision"] == "rejected"
    assert dec_rows[0].payload["reason"] == "too risky"

    reset_dispatcher_caches()
    clear_registry()


# ---------------------------------------------------------------------------
# #1503 — parked direct operator op approved via /decide or MCP by-id is
# re-dispatched using the stored params (execute-after-approve on every
# surface, not only REST /approve). Must NOT double-execute an agent-run
# request (the in-process agent runtime resumes those off the broadcast).
# ---------------------------------------------------------------------------


async def _register_recording_requires_approval_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register a ``requires_approval`` typed op backed by the recording handler.

    Shared setup for the #1503 surface tests: a clean registry, a stubbed
    broadcast publisher, and one ``rectest.write`` op whose handler appends
    to ``_RECORDED_EXECUTIONS`` so a test can assert the approved op ran
    exactly once.
    """
    from unittest.mock import AsyncMock

    import meho_backplane.operations._audit as audit_module
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
    from meho_backplane.operations import register_typed_operation

    async def _capture(event: Any) -> None:
        pass

    monkeypatch.setattr(audit_module, "publish_event", _capture)

    class _RecConnector(Connector):
        product = "rectest"
        version = "1.x"
        impl_id = "rectest"
        priority = 10

        async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
            return FingerprintResult(
                probe=ProbeResult(reachable=True, probe_method="none"),
                product="rectest",
                version="1.x",
            )

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(  # type: ignore[override]
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> Any:
            raise NotImplementedError

    register_connector_v2(product="rectest", version="", impl_id="", cls=_RecConnector)

    stub_emb = AsyncMock()
    stub_emb.encode_one.return_value = [0.1] * 384
    stub_emb.encode.return_value = [[0.1] * 384]
    stub_emb.dimension = 384

    await register_typed_operation(
        product="rectest",
        version="1.x",
        impl_id="rectest",
        op_id="rectest.write",
        handler=_approval_test_recording_handler,
        summary="Direct write op requiring approval.",
        description="Test.",
        parameter_schema={"type": "object"},
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_emb,
    )


@pytest.mark.asyncio
async def test_decide_approve_redispatches_direct_op_with_stored_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked direct op approved via ``/decide`` executes once (#1503, AC1+AC2).

    1. A human operator dispatches a ``requires_approval`` op directly →
       parked (``awaiting_approval``); the row stores the original params.
    2. A distinct operator approves via the ``/decide`` REST route (by id
       alone, no params in-band).
    3. ``/decide`` re-dispatches using the **stored** params → the handler
       runs exactly once and the response carries the dispatch outcome.
    """
    from meho_backplane.api.v1.approvals import DecideRequestBody, decide_approval_request
    from meho_backplane.connectors.registry import clear_registry
    from meho_backplane.operations import dispatch, reset_dispatcher_caches

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()

    _RECORDED_EXECUTIONS.clear()
    reset_dispatcher_caches()
    clear_registry()
    await _register_recording_requires_approval_op(monkeypatch)

    requester = _make_operator(sub="ops-human", principal_kind=PrincipalKind.USER)
    params = {"path": "secret/db", "value": "s3cr3t"}

    # Step 1: direct dispatch → parked.
    result1 = await dispatch(
        operator=requester,
        connector_id="rectest-1.x",
        op_id="rectest.write",
        target=None,
        params=params,
    )
    assert result1.status == "awaiting_approval", result1.error
    approval_request_id = uuid.UUID(result1.extras["approval_request_id"])
    assert _RECORDED_EXECUTIONS == [], "op must not run while parked"

    # The row persisted the original params + has no run_id (direct op).
    async with get_sessionmaker()() as s:
        pending = await s.get(ApprovalRequest, approval_request_id)
        assert pending is not None
        assert pending.run_id is None
        assert pending.params == params

    # Step 2+3: a distinct operator decides "approved" via /decide.
    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    response = await decide_approval_request(
        approval_request_id,
        DecideRequestBody(decision="approved"),
        operator=reviewer,
    )

    assert response.decision == "approved"
    assert response.dispatch_status == "ok", response.dispatch_error
    assert response.dispatch_op_id == "rectest.write"
    # Executed exactly once, with the stored params (not re-supplied).
    assert len(_RECORDED_EXECUTIONS) == 1
    assert _RECORDED_EXECUTIONS[0]["params"] == params

    # The row is terminal (approved) — a second decide cannot double-execute.
    async with get_sessionmaker()() as s:
        decided = await s.get(ApprovalRequest, approval_request_id)
        assert decided is not None
        assert decided.status == ApprovalRequestStatus.APPROVED.value

    reset_dispatcher_caches()
    clear_registry()


@pytest.mark.asyncio
async def test_mcp_approve_redispatches_direct_op_with_stored_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked direct op approved via MCP by-id executes once (#1503, AC1).

    Mirrors the ``/decide`` test for the MCP ``meho.approvals.approve``
    surface: approve by id alone → the handler re-dispatches with the
    stored params, runs once, and returns the dispatch outcome under
    ``dispatch``.
    """
    from meho_backplane.connectors.registry import clear_registry
    from meho_backplane.mcp.tools.approvals import _approve_handler
    from meho_backplane.operations import dispatch, reset_dispatcher_caches

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()

    _RECORDED_EXECUTIONS.clear()
    reset_dispatcher_caches()
    clear_registry()
    await _register_recording_requires_approval_op(monkeypatch)

    requester = _make_operator(sub="ops-human", principal_kind=PrincipalKind.USER)
    params = {"path": "secret/api", "value": "tok-123"}

    result1 = await dispatch(
        operator=requester,
        connector_id="rectest-1.x",
        op_id="rectest.write",
        target=None,
        params=params,
    )
    assert result1.status == "awaiting_approval", result1.error
    approval_request_id = uuid.UUID(result1.extras["approval_request_id"])

    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    out = await _approve_handler(reviewer, {"approval_request_id": str(approval_request_id)})

    assert out["status"] == ApprovalRequestStatus.APPROVED.value
    assert out["dispatch"]["status"] == "ok", out["dispatch"]["error"]
    assert out["dispatch"]["op_id"] == "rectest.write"
    assert len(_RECORDED_EXECUTIONS) == 1
    assert _RECORDED_EXECUTIONS[0]["params"] == params

    reset_dispatcher_caches()
    clear_registry()


@pytest.mark.asyncio
async def test_decide_approve_does_not_redispatch_agent_run_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent-run request approved via ``/decide`` is NOT re-dispatched (#1503, AC3).

    The in-process agent runtime resumes an agent-run op off the
    ``approval.approved`` broadcast. ``/decide`` re-dispatching it too
    would execute the op twice. For a request with ``run_id`` set,
    ``/decide`` must record the decision only and leave the
    ``dispatch_*`` fields empty — the must-not-regress guard.
    """
    from meho_backplane.api.v1.approvals import DecideRequestBody, decide_approval_request
    from meho_backplane.connectors.registry import clear_registry
    from meho_backplane.operations import reset_dispatcher_caches

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()

    _RECORDED_EXECUTIONS.clear()
    reset_dispatcher_caches()
    clear_registry()
    await _register_recording_requires_approval_op(monkeypatch)

    # Park an agent-run request directly (run_id set) — the realistic
    # shape the agent runtime parks via the contextvar.
    requester = _make_operator(sub="agent-run-sub", principal_kind=PrincipalKind.AGENT)
    params = {"path": "secret/agent"}
    run_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        pending = await create_pending_request(
            s,
            operator=requester,
            connector_id="rectest-1.x",
            op_id="rectest.write",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            run_id=run_id,
        )
        await s.commit()
    approval_request_id = pending.id

    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    response = await decide_approval_request(
        approval_request_id,
        DecideRequestBody(decision="approved"),
        operator=reviewer,
    )

    assert response.decision == "approved"
    # No inline re-dispatch — the agent runtime owns that path.
    assert response.dispatch_status is None
    assert response.dispatch_op_id is None
    assert _RECORDED_EXECUTIONS == [], "agent-run op must NOT execute from /decide"

    reset_dispatcher_caches()
    clear_registry()


# ---------------------------------------------------------------------------
# proposed_effect carries catalog safety_level (#1855)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proposed_effect_carries_catalog_safety_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked op's ``proposed_effect`` echoes its catalog ``safety_level`` (#1855).

    Two approval-gated ops with no registered preview builder — one
    ``dangerous``, one ``caution`` — are parked through the full dispatch
    path. Their durable ``ApprovalRequest.proposed_effect`` envelopes must
    differ on a ``safety_level`` field read straight off the catalog
    :class:`EndpointDescriptor` (the same value surfaces over
    ``GET /api/v1/approvals/{id}``, which serializes ``proposed_effect``
    verbatim). This makes orders-of-magnitude-different blast radii
    legible to the reviewer even for ops outside k8s/vmware/argocd, which
    register no bespoke preview.
    """
    from unittest.mock import AsyncMock

    import meho_backplane.operations._audit as audit_module
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import clear_registry, register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
    from meho_backplane.operations import (
        dispatch,
        register_typed_operation,
        reset_dispatcher_caches,
    )

    async def _capture(event: Any) -> None:
        pass

    monkeypatch.setattr(audit_module, "publish_event", _capture)

    reset_dispatcher_caches()
    clear_registry()

    class _SeverityConnector(Connector):
        product = "sevtest"
        version = "1.x"
        impl_id = "sevtest"
        priority = 10

        async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
            return FingerprintResult(
                probe=ProbeResult(reachable=True, probe_method="none"),
                product="sevtest",
                version="1.x",
            )

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(  # type: ignore[override]
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> Any:
            raise NotImplementedError

    register_connector_v2(product="sevtest", version="", impl_id="", cls=_SeverityConnector)

    stub_emb = AsyncMock()
    stub_emb.encode_one.return_value = [0.1] * 384
    stub_emb.encode.return_value = [[0.1] * 384]
    stub_emb.dimension = 384

    for op_id, severity in (
        ("sevtest.realm.create", "dangerous"),
        ("sevtest.user.create", "caution"),
    ):
        await register_typed_operation(
            product="sevtest",
            version="1.x",
            impl_id="sevtest",
            op_id=op_id,
            handler=_approval_test_severity_handler,
            summary=f"{severity} write op requiring approval.",
            description="Test.",
            parameter_schema={"type": "object"},
            safety_level=severity,  # type: ignore[arg-type]
            requires_approval=True,
            when_to_use=None,
            embedding_service=stub_emb,
        )

    # A human (USER) principal hitting a ``requires_approval`` op is
    # routed to the approval queue regardless of ``safety_level``
    # (``_non_agent_verdict``), so both severities park rather than the
    # ``dangerous`` op being default-denied on the agent permission path.
    requester = _make_operator(sub="ops-human-sev", principal_kind=PrincipalKind.USER)

    parked: dict[str, str] = {}
    for op_id, expected in (
        ("sevtest.realm.create", "dangerous"),
        ("sevtest.user.create", "caution"),
    ):
        result = await dispatch(
            operator=requester,
            connector_id="sevtest-1.x",
            op_id=op_id,
            target=None,
            params={},
        )
        assert result.status == "awaiting_approval", result.error
        approval_request_id = uuid.UUID(result.extras["approval_request_id"])
        async with get_sessionmaker()() as s:
            row = await s.get(ApprovalRequest, approval_request_id)
            assert row is not None
            # Read straight off the descriptor, not recomputed.
            assert row.proposed_effect["safety_level"] == expected
            # The identifier base is preserved alongside the new field.
            assert row.proposed_effect["op_id"] == op_id
            parked[op_id] = row.proposed_effect["safety_level"]

    # The two severities are distinguishable on the reviewer-facing row.
    assert parked["sevtest.realm.create"] != parked["sevtest.user.create"]

    reset_dispatcher_caches()
    clear_registry()


# ---------------------------------------------------------------------------
# Session-replay lineage: park → approve → resume as one replay subtree
# (#2086, Initiative #2151)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pending_request_persists_session_lineage(
    session: AsyncSession,
) -> None:
    """#2086 AC1: the parked row persists the originating session + park audit id.

    ``create_pending_request`` runs on the requester's task, where the
    session context is still live. It must durably capture:

    * ``agent_session_id`` — from ``agent_session_id_var`` (the agent-run
      binding takes priority over the MCP transport fallback), and
    * ``request_audit_id`` — the ``approval.request`` audit row's id,

    and the "request" audit row itself must anchor on the session
    (``agent_session_id`` set) without self-parenting
    (``parent_audit_id`` NULL for a top-level park).
    """
    from meho_backplane.operations._audit import agent_session_id_var

    session_id = uuid.uuid4()
    operator = _make_operator(sub="agent-sub")
    params = {"key": "value"}

    token = agent_session_id_var.set(session_id)
    try:
        request = await create_pending_request(
            session,
            operator=operator,
            connector_id="vault-1.x",
            op_id="vault.kv.write",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
    finally:
        agent_session_id_var.reset(token)
    await session.commit()

    assert request.agent_session_id == session_id
    assert request.request_audit_id is not None

    audit_row = (
        (await session.execute(select(AuditLog).where(AuditLog.path == "approval.request")))
        .scalars()
        .one()
    )
    assert audit_row.id == request.request_audit_id
    assert audit_row.agent_session_id == session_id
    assert audit_row.parent_audit_id is None, (
        "the parking row is the chain's root — it must not self-parent"
    )


@pytest.mark.asyncio
async def test_lineage_null_outside_any_session(session: AsyncSession) -> None:
    """No session bound → both lineage reads stay honest.

    ``agent_session_id`` is NULL (a park from the chassis HTTP path has
    no session), while ``request_audit_id`` is always populated — the
    park audit row exists regardless of session context.
    """
    operator = _make_operator(sub="agent-sub")
    params = {"key": "value"}

    request = await create_pending_request(
        session,
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params=params,
        params_hash=compute_params_hash(params),
    )
    await session.commit()

    assert request.agent_session_id is None
    assert request.request_audit_id is not None


@pytest.mark.asyncio
async def test_decision_audit_row_backlinks_and_inherits_session(
    session: AsyncSession,
) -> None:
    """#2086 AC2 (decision half): the decision row joins the replay subtree.

    The approver's task has no session context bound (a different
    operator on a different request), so both lineage values must come
    off the durable row: ``parent_audit_id`` = the parking row's audit
    id, ``agent_session_id`` = the session the request was parked under
    — the same source-from-the-row discipline ``work_ref`` established
    (#1659).
    """
    from meho_backplane.operations._audit import agent_session_id_var

    session_id = uuid.uuid4()
    requester = _make_operator(sub="agent-sub")
    params = {"key": "value"}

    token = agent_session_id_var.set(session_id)
    try:
        request = await create_pending_request(
            session,
            operator=requester,
            connector_id="vault-1.x",
            op_id="vault.kv.write",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
    finally:
        agent_session_id_var.reset(token)
    await session.commit()

    # Approver's task: no session vars bound.
    assert agent_session_id_var.get() is None
    reviewer = _make_operator(sub="reviewer-sub")
    approved = await approve_request(session, request.id, operator=reviewer, params=params)
    await session.commit()

    decision_row = (
        (await session.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
        .scalars()
        .one()
    )
    assert decision_row.parent_audit_id == approved.request_audit_id
    assert decision_row.agent_session_id == session_id


@pytest.mark.asyncio
async def test_mcp_session_lineage_park_approve_resume_replays_as_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2086 AC1-AC5: the approval-gated chain reconstructs as a replay tree.

    Drives the full park → approve → resume-dispatch path under an MCP
    session with ``MCP_REQUIRE_SESSION_ID`` **enforced** (AC4): the
    transport-bound ``mcp_session_id`` structlog contextvar (the value
    :func:`~meho_backplane.mcp.server._bind_mcp_session_id` binds after
    the enforced-mode header check passes) is the session source at park
    time, and the resume task — which has no session context of its own
    — must re-hydrate it from the parked row.

    Asserts the three lineage promises the issue names:

    1. The parked row persists ``agent_session_id`` sourced from the MCP
       transport binding (AC1).
    2. The dispatched-after-approval audit row carries the originating
       ``agent_session_id`` and a ``parent_audit_id`` equal to the
       parking row's audit id (AC1 + AC2).
    3. :func:`~meho_backplane.audit_query.replay.replay_session`
       reconstructs the chain as one subtree — the ``approval.request``
       row as an anchored root with the decision row and the executed
       dispatch as its children, >= 3 session-anchored rows in total
       (AC3, the ``row_count >= 3`` REST-surface contract) — no longer
       ``{root: [], row_count: 0}`` (AC5).
    """
    from unittest.mock import AsyncMock

    import structlog

    import meho_backplane.operations._audit as audit_module
    from meho_backplane.audit_query.replay import replay_session
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import clear_registry, register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
    from meho_backplane.mcp.server import mcp_session_id_capture_mode
    from meho_backplane.operations import (
        dispatch,
        register_typed_operation,
        reset_dispatcher_caches,
    )
    from meho_backplane.operations._audit import agent_session_id_var, parent_audit_id_var
    from meho_backplane.operations.approval_queue import resume_dispatch_after_approval

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("MCP_REQUIRE_SESSION_ID", "true")
    get_settings.cache_clear()
    assert mcp_session_id_capture_mode() == "enforced"

    async def _capture(event: Any) -> None:
        pass

    monkeypatch.setattr(audit_module, "publish_event", _capture)

    reset_dispatcher_caches()
    clear_registry()

    class _OkConnector(Connector):
        product = "lineagetest"
        version = "1.x"
        impl_id = "lineagetest"
        priority = 10

        async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
            return FingerprintResult(
                probe=ProbeResult(reachable=True, probe_method="none"),
                product="lineagetest",
                version="1.x",
            )

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(  # type: ignore[override]
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> Any:
            raise NotImplementedError

    register_connector_v2(product="lineagetest", version="", impl_id="", cls=_OkConnector)

    stub_emb = AsyncMock()
    stub_emb.encode_one.return_value = [0.1] * 384
    stub_emb.encode.return_value = [[0.1] * 384]
    stub_emb.dimension = 384

    await register_typed_operation(
        product="lineagetest",
        version="1.x",
        impl_id="lineagetest",
        op_id="lineagetest.op",
        handler=_approval_test_ok_handler,
        summary="Test op requiring approval.",
        description="Test.",
        parameter_schema={"type": "object"},
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_emb,
    )

    mcp_session_id = uuid.uuid4()
    requester = _make_operator(sub="human-requester", principal_kind=PrincipalKind.USER)
    params = {"x": 7}

    # Step 1: park the op under the MCP transport's session binding —
    # the same structlog contextvar _bind_mcp_session_id sets once the
    # enforced-mode header check passes.
    structlog.contextvars.bind_contextvars(mcp_session_id=str(mcp_session_id))
    try:
        result1 = await dispatch(
            operator=requester,
            connector_id="lineagetest-1.x",
            op_id="lineagetest.op",
            target=None,
            params=params,
        )
    finally:
        structlog.contextvars.unbind_contextvars("mcp_session_id")
    assert result1.status == "awaiting_approval"
    approval_request_id = uuid.UUID(result1.extras["approval_request_id"])

    async with get_sessionmaker()() as s:
        parked = await s.get(ApprovalRequest, approval_request_id)
        assert parked is not None
        assert parked.agent_session_id == mcp_session_id
        assert parked.request_audit_id is not None
        request_audit_id = parked.request_audit_id

    # Step 2: a different operator approves — its task carries no
    # session context (neither the agent var nor the MCP binding).
    reviewer = _make_operator(sub="human-reviewer", principal_kind=PrincipalKind.USER)
    assert agent_session_id_var.get() is None
    assert structlog.contextvars.get_contextvars().get("mcp_session_id") is None
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, approval_request_id, operator=reviewer)
        await s.commit()

    # Step 3: re-dispatch via the shared resume helper. The re-binds are
    # scoped: both vars are reset once the dispatch returns.
    result2 = await resume_dispatch_after_approval(operator=reviewer, request=approved)
    assert result2.status == "ok"
    assert agent_session_id_var.get() is None
    assert parent_audit_id_var.get() is None

    # The executed dispatch's audit row anchors in the originating
    # session AND back-links to the parking row (AC1 + AC2).
    async with get_sessionmaker()() as fresh:
        dispatch_row = (
            (await fresh.execute(select(AuditLog).where(AuditLog.method == "DISPATCH")))
            .scalars()
            .one()
        )
        assert dispatch_row.agent_session_id == mcp_session_id
        assert dispatch_row.parent_audit_id == request_audit_id

        # AC3 row_count contract: the REST surface counts *anchor* rows
        # (agent_session_id == session); the full chain anchors.
        anchored = (
            (
                await fresh.execute(
                    select(AuditLog).where(AuditLog.agent_session_id == mcp_session_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(anchored) >= 3

        # AC3 + AC5: the replay closure reconstructs the chain as one
        # subtree — request row root, decision + dispatch as children.
        forest = await replay_session(mcp_session_id, tenant_id=_TENANT_ID, session=fresh)

    assert forest, "replay must no longer return an empty root for the approval chain"
    roots_by_path = {node.path: node for node in forest}
    assert "approval.request" in roots_by_path
    chain_root = roots_by_path["approval.request"]
    child_paths = sorted(child.path for child in chain_root.children)
    assert child_paths == ["approval.decision", "lineagetest.op"]

    reset_dispatcher_caches()
    clear_registry()
