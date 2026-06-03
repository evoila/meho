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
