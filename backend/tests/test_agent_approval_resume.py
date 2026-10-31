# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for the agent-runtime approval-resume substrate (G11.1-T9 / #1117).

Covers the four acceptance criteria the issue body specifies:

1. The wrapped ``call_operation`` tool the agent loop calls reads its own
   stored approval row (by ``approval_request_id``) until it shows a
   decision. Since #3758 no approval event is on Broadcast.
2. On ``approved``, the runtime resumes by re-invoking
   :func:`~meho_backplane.operations.dispatch` with ``_approved=True`` and the
   original in-memory params; the audit chain shows the op actually executed.
3. On ``rejected``, the runtime surfaces the rejection to the tool
   result (the model's view) so the agent can reason about the operator's
   decision.
4. The test below pauses an agent run → operator approves via the
   :func:`~meho_backplane.operations.approval_queue.approve_request` path the
   ``/decide`` handler uses (**not** the REST ``/approve+params`` re-dispatch express lane)
   → the agent run resumes and the op executes with the correct audit shape.

What the tests deliberately do NOT do
-------------------------------------

* They do NOT spawn a real Pydantic AI loop — the wrapped tool's behaviour is
  the unit of interest (the loop just calls it). Driving a real
  ``FunctionModel`` would add ~1s per case for zero coverage gain; the
  parallel suite ``test_agent_invoke.py`` already exercises the loop wiring.
* They do NOT hit Valkey — the wait does not use it any more.
* They do NOT cover process-restart resume — that's an explicit out-of-scope
  item in the #1117 task body (live-wait only; durable-checkpoint follow-up
  belongs to a separate G11.1 task).
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agent.approval_wait import (
    AWAITING_APPROVAL_TIMEOUT_ERROR_CODE,
    resume_or_surface_awaiting_approval,
    wait_for_approval_decision,
)
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations.approval_handoff import ApprovalExecutionPayload
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000d0d0")


# ---------------------------------------------------------------------------
# Module-level handler (closures are rejected by derive_handler_ref)
# ---------------------------------------------------------------------------


async def _approval_resume_test_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Module-level handler for ``apprestest.op`` (integration tests)."""
    return {"executed": True, "params": params}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires + reset between tests."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    # Short wait timeout so the timeout-path test doesn't slow the suite.
    monkeypatch.setenv("AGENT_APPROVAL_WAIT_TIMEOUT_SECONDS", "0.5")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Open session against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator(
    *,
    sub: str = "agent-resume-sub",
    role: TenantRole = TenantRole.OPERATOR,
    tenant_id: uuid.UUID = _TENANT_ID,
    principal_kind: PrincipalKind = PrincipalKind.AGENT,
) -> Operator:
    """An AGENT-kind operator — only agent principals reach the approval gate.

    The G11.2-T3 gate hard-denies ``requires_approval`` for human/service
    principals; only agents land in the awaiting-approval branch. The
    resume substrate this Task ships only matters for agent runs.
    """
    return Operator(
        sub=sub,
        name="Agent Resume Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=principal_kind,
    )


# ---------------------------------------------------------------------------
# wait_for_approval_decision — the read-side primitive
# ---------------------------------------------------------------------------


async def test_wait_never_uses_valkey() -> None:
    """The wait reads only the stored row; it has no Valkey client (#3758)."""
    import meho_backplane.agent.approval_wait as approval_wait_module

    assert not hasattr(approval_wait_module, "get_broadcast_blocking_client")


async def test_wait_ignores_other_requests_decisions() -> None:
    """Another request's approved row does not satisfy this wait."""
    await _park_request(requester=_make_operator(), status=ApprovalRequestStatus.APPROVED)
    pending = await _park_request(requester=_make_operator())

    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID,
        approval_request_id=pending.id,
        timeout_seconds=0.3,
    )
    assert decision == "timeout"


async def test_wait_times_out_when_no_row_exists() -> None:
    """No stored row → the wait returns ``"timeout"`` after the cap."""
    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID,
        approval_request_id=uuid.uuid4(),
        timeout_seconds=0.2,
    )
    assert decision == "timeout"


# ---------------------------------------------------------------------------
# resume_or_surface_awaiting_approval — the agent-facing entry point
# ---------------------------------------------------------------------------


async def test_resume_re_dispatches_on_approval_via_decide_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance-criterion test — operator approves via /decide-shape path.

    End-to-end the wait calls in the agent layer drive the resume:

    1. Register a ``requires_approval=True`` typed op.
    2. Agent dispatches via the wrapped ``call_operation`` — gets
       ``awaiting_approval`` with an ``approval_request_id``.
    3. Operator flips the row to ``approved`` (the
       :func:`~meho_backplane.operations.approval_queue.approve_request`
       call the ``/decide`` route + MCP ``meho_approvals_approve`` use —
       NOT the REST ``/approve+params`` re-dispatch express lane).
    4. The agent's wait reads the approved row and the wrapper re-invokes
       ``dispatch(..., _approved=True)`` with the original params.
    5. The audit chain shows the op actually executed.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import clear_registry, register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
    from meho_backplane.operations import (
        dispatch,
        register_typed_operation,
        reset_dispatcher_caches,
    )
    from meho_backplane.operations.approval_queue import approve_request

    reset_dispatcher_caches()
    clear_registry()

    class _OkConnector(Connector):
        product = "apprestest"
        version = "1.x"
        impl_id = "apprestest"
        priority = 10

        async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
            return FingerprintResult(
                probe=ProbeResult(reachable=True, probe_method="none"),
                product="apprestest",
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
            raise NotImplementedError

    register_connector_v2(
        product="apprestest",
        version="",
        impl_id="",
        cls=_OkConnector,
    )

    stub_emb = AsyncMock()
    stub_emb.encode_one.return_value = [0.1] * 384
    stub_emb.encode.return_value = [[0.1] * 384]
    stub_emb.dimension = 384

    await register_typed_operation(
        product="apprestest",
        version="1.x",
        impl_id="apprestest",
        op_id="apprestest.op",
        handler=_approval_resume_test_handler,
        summary="Test op requiring approval.",
        description="Test.",
        parameter_schema={"type": "object"},
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_emb,
    )

    operator = _make_operator(sub="agent-resume-acceptance")
    call_arguments: dict[str, Any] = {
        "connector_id": "apprestest-1.x",
        "op_id": "apprestest.op",
        "params": {"x": 42},
        "target": None,
    }

    # Step 1: the agent's first dispatch (via the same code path the agent
    # tool calls -- ``dispatch`` is the seam underneath ``call_operation``).
    # Yields awaiting_approval.
    first = await dispatch(
        operator=operator,
        connector_id="apprestest-1.x",
        op_id="apprestest.op",
        target=None,
        params=call_arguments["params"],
    )
    assert first.status == "awaiting_approval"
    approval_request_id = uuid.UUID(first.extras["approval_request_id"])

    # Step 2: a distinct human operator approves via the operator-decision
    # path. This is the path /decide (REST) and meho_approvals_approve
    # (MCP) take -- the ``approve_request`` call by id alone, **without**
    # the params re-dispatch the legacy REST /approve+params route does.
    # The reviewer must differ from the agent requester (self-approval
    # guard, G11.7-T1 #1401).
    reviewer = _make_operator(sub="human-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(s, approval_request_id, operator=reviewer, params=None)
        await s.commit()
    assert row.status == "approved"

    # Step 3: drive the agent's resume helper -- the wait reads the
    # approved row and the wrapper re-dispatches with _approved=True.
    awaiting_envelope = first.model_dump(mode="json")
    resumed = await resume_or_surface_awaiting_approval(
        operator=operator,
        call_arguments=call_arguments,
        awaiting_envelope=awaiting_envelope,
        timeout_seconds=2.0,
    )

    # Step 5: assert the op actually executed (the handler returned
    # {"executed": True, "params": {"x": 42}}). The dispatch result's
    # ``result`` field is what's in the OperationResult envelope's
    # ``result`` key after model_dump.
    assert resumed["status"] == "ok", f"expected ok, got {resumed!r}"
    assert resumed["result"]["executed"] is True
    assert resumed["result"]["params"] == {"x": 42}

    # Step 6: audit attribution. The dispatcher writes an audit row per
    # dispatch; the awaiting_approval first call wrote a request audit row;
    # the resumed call's audit row records the executed dispatch under the
    # SAME agent principal (subject), with the operator's approval row
    # carrying the reviewer identity. Verify a recent audit_log row for
    # the agent principal exists with operator_sub matching.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        # ``approval.decision`` row was written by approve_request as the
        # synchronous decision audit row.
        decision_audits = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
        assert len(decision_audits) == 1
        assert decision_audits[0].payload["decision"] == "approved"

    reset_dispatcher_caches()
    clear_registry()


async def test_resume_stamps_parent_audit_id_so_chain_replays_as_one_subtree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2323 (completing #2086): the agent-resumed dispatch back-links to the request.

    The in-process agent waiter re-dispatches on the agent's own task, where
    ``parent_audit_id_var`` still carries the original top-level value
    (``None`` for a plain tool call). Before the fix the executed DISPATCH
    row therefore emitted ``parent_audit_id = NULL`` and replayed as a
    *second root*, breaking the one-subtree invariant #2086 established.

    This drives the full agent decide-path resume with an ``agent_session_id``
    bound (as a real agent run has), then asserts:

    1. The executed DISPATCH row's ``parent_audit_id`` equals the parking
       ``approval.request`` row's audit id (the same anchor the decision row
       parents to).
    2. The chain has exactly one null-parent row — the ``approval.request``
       root — not two.
    3. ``replay_session`` reconstructs one subtree: the request row as root
       with the decision and the executed dispatch as its children.
    """
    from meho_backplane.audit_query.replay import replay_session
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import clear_registry, register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
    from meho_backplane.operations import (
        dispatch,
        register_typed_operation,
        reset_dispatcher_caches,
    )
    from meho_backplane.operations._audit import agent_session_id_var, parent_audit_id_var
    from meho_backplane.operations.approval_queue import approve_request

    reset_dispatcher_caches()
    clear_registry()

    class _OkConnector(Connector):
        product = "appres2test"
        version = "1.x"
        impl_id = "appres2test"
        priority = 10

        async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
            return FingerprintResult(
                probe=ProbeResult(reachable=True, probe_method="none"),
                product="appres2test",
                version="1.x",
            )

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(  # type: ignore[override]
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> Any:
            raise NotImplementedError

    register_connector_v2(product="appres2test", version="", impl_id="", cls=_OkConnector)

    stub_emb = AsyncMock()
    stub_emb.encode_one.return_value = [0.1] * 384
    stub_emb.encode.return_value = [[0.1] * 384]
    stub_emb.dimension = 384

    await register_typed_operation(
        product="appres2test",
        version="1.x",
        impl_id="appres2test",
        op_id="appres2test.op",
        handler=_approval_resume_test_handler,
        summary="Test op requiring approval.",
        description="Test.",
        parameter_schema={"type": "object"},
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_emb,
    )

    operator = _make_operator(sub="agent-lineage-requester")
    call_arguments: dict[str, Any] = {
        "connector_id": "appres2test-1.x",
        "op_id": "appres2test.op",
        "params": {"x": 7},
        "target": None,
    }

    # Step 1: park the op under a bound agent session — exactly what
    # AgentInvoker sets around a run, so the parked row (and every resumed
    # row) anchors in the session and replay can reconstruct the chain.
    session_id = uuid.uuid4()
    session_token = agent_session_id_var.set(session_id)
    try:
        first = await dispatch(
            operator=operator,
            connector_id="appres2test-1.x",
            op_id="appres2test.op",
            target=None,
            params=call_arguments["params"],
        )
    finally:
        agent_session_id_var.reset(session_token)
    assert first.status == "awaiting_approval"
    approval_request_id = uuid.UUID(first.extras["approval_request_id"])

    async with get_sessionmaker()() as s:
        from meho_backplane.db.models import ApprovalRequest

        parked = await s.get(ApprovalRequest, approval_request_id)
        assert parked is not None
        assert parked.agent_session_id == session_id
        assert parked.request_audit_id is not None
        request_audit_id = parked.request_audit_id

    # Step 2: a distinct human operator approves via the /decide path.
    reviewer = _make_operator(sub="human-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(s, approval_request_id, operator=reviewer, params=None)
        await s.commit()
    assert row.status == "approved"

    # Step 3: drive the agent resume with the session re-bound (as the
    # agent's own task still carries it while it awaits the decision).
    awaiting_envelope = first.model_dump(mode="json")
    session_token = agent_session_id_var.set(session_id)
    try:
        resumed = await resume_or_surface_awaiting_approval(
            operator=operator,
            call_arguments=call_arguments,
            awaiting_envelope=awaiting_envelope,
            timeout_seconds=2.0,
        )
    finally:
        agent_session_id_var.reset(session_token)
    assert resumed["status"] == "ok", f"expected ok, got {resumed!r}"
    # The bind is token-scoped: the var is back to its default after resume.
    assert parent_audit_id_var.get() is None

    async with get_sessionmaker()() as fresh:
        dispatch_row = (
            (await fresh.execute(select(AuditLog).where(AuditLog.method == "DISPATCH")))
            .scalars()
            .one()
        )
        # (1) the executed dispatch back-links to the parking request row.
        assert dispatch_row.parent_audit_id == request_audit_id
        assert dispatch_row.agent_session_id == session_id

        # (2) exactly one null-parent row across the chain — the request root.
        anchored = (
            (await fresh.execute(select(AuditLog).where(AuditLog.agent_session_id == session_id)))
            .scalars()
            .all()
        )
        assert len(anchored) >= 3
        null_parents = [r for r in anchored if r.parent_audit_id is None]
        assert len(null_parents) == 1
        assert null_parents[0].path == "approval.request"

        # (3) replay renders one subtree, not two roots.
        forest = await replay_session(session_id, tenant_id=_TENANT_ID, session=fresh)

    assert len(forest) == 1
    chain_root = forest[0]
    assert chain_root.path == "approval.request"
    child_paths = sorted(child.path for child in chain_root.children)
    assert child_paths == ["appres2test.op", "approval.decision"]

    reset_dispatcher_caches()
    clear_registry()


async def test_resume_surfaces_rejection_to_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``approval.rejected`` event yields an envelope the model can reason about.

    The wrapper returns the original ``awaiting_approval`` envelope with
    ``extras["error_code"] = "approval_rejected"`` and ``extras["decision"]
    = "rejected"`` plus a human-readable ``error`` message, so the agent's
    model sees a structured tool result rather than a crash.
    """
    rejected = await _park_request(
        requester=_make_operator(), status=ApprovalRequestStatus.REJECTED
    )
    approval_request_id = rejected.id
    awaiting_envelope: dict[str, Any] = {
        "status": "awaiting_approval",
        "op_id": "apprestest.op",
        "result": None,
        "error": "awaiting_approval: 'apprestest.op' requires approval before execution",
        "duration_ms": 1.0,
        "extras": {
            "error_code": "awaiting_approval",
            "approval_request_id": str(approval_request_id),
        },
    }

    operator = _make_operator()
    out = await resume_or_surface_awaiting_approval(
        operator=operator,
        call_arguments={
            "connector_id": "apprestest-1.x",
            "op_id": "apprestest.op",
            "params": {},
            "target": None,
        },
        awaiting_envelope=awaiting_envelope,
        timeout_seconds=2.0,
    )

    # The envelope's status stays awaiting_approval (NOT denied -- that's
    # the policy gate's verdict shape, not this layer's): the agent learns
    # of the rejection via extras["decision"] + the rewritten error prose.
    assert out["status"] == "awaiting_approval"
    assert out["extras"]["error_code"] == "approval_rejected"
    assert out["extras"]["decision"] == "rejected"
    assert "operator rejected" in out["error"]


async def test_resume_surfaces_timeout_with_distinct_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No decision before the timeout → envelope carries the timeout error code.

    Guards the third branch of the resume contract: when the wait elapses
    without seeing a decision, the wrapper returns a timeout-annotated
    envelope so the model can distinguish "still pending, timed out" from
    "decision happened, was X".
    """
    approval_request_id = uuid.uuid4()
    awaiting_envelope: dict[str, Any] = {
        "status": "awaiting_approval",
        "op_id": "apprestest.op",
        "result": None,
        "error": "awaiting_approval: 'apprestest.op' requires approval before execution",
        "duration_ms": 1.0,
        "extras": {
            "error_code": "awaiting_approval",
            "approval_request_id": str(approval_request_id),
        },
    }

    # No stored row exists for this id; the wait elapses.
    operator = _make_operator()
    out = await resume_or_surface_awaiting_approval(
        operator=operator,
        call_arguments={
            "connector_id": "apprestest-1.x",
            "op_id": "apprestest.op",
            "params": {},
            "target": None,
        },
        awaiting_envelope=awaiting_envelope,
        timeout_seconds=0.2,
    )

    assert out["extras"]["error_code"] == AWAITING_APPROVAL_TIMEOUT_ERROR_CODE
    assert out["extras"]["approval_request_id"] == str(approval_request_id)
    assert out["extras"]["wait_timeout_seconds"] == 0.2
    assert "awaiting_approval_timeout" in out["error"]


async def test_resume_raises_on_missing_approval_request_id() -> None:
    """A dispatcher contract violation (no approval_request_id) is a hard error.

    Guards against silent skip on a broken envelope shape: if the
    ``result_awaiting_approval`` contract somehow degrades to omit the
    request id, this layer must fail loud rather than burn a 30-minute
    wait on an un-resumable request.
    """
    operator = _make_operator()
    with pytest.raises(ValueError, match="missing extras"):
        await resume_or_surface_awaiting_approval(
            operator=operator,
            call_arguments={"connector_id": "x", "op_id": "y", "params": {}, "target": None},
            awaiting_envelope={"status": "awaiting_approval", "extras": {}},
            timeout_seconds=1.0,
        )


# ---------------------------------------------------------------------------
# Exactly-one-resumer claim — the waiter loses the race (#2293)
# ---------------------------------------------------------------------------


async def test_credential_write_agent_claim_winner_discards_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent winner deletes its ciphertext after claiming the resume."""
    import meho_backplane.operations.meta_tools as meta_tools_module
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed
    from meho_backplane.operations._validate import compute_params_hash
    from meho_backplane.operations.approval_queue import create_pending_request

    secret = "test-only-credential-value"
    params = {"value": secret}
    requester = _make_operator(sub="agent-credential-winner")
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=requester,
            connector_id="vault-1.x",
            op_id="vault.kv.put",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            run_id=uuid.uuid4(),
        )
        request.status = ApprovalRequestStatus.APPROVED.value
        request.decided_at = datetime.now(UTC)
        await session.commit()
    assert request.execution_handle is not None

    async def _redispatch(
        _operator: Operator, arguments: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        assert arguments["params"] == params
        return {"status": "ok"}

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _redispatch)
    resumed = await _resume_approved_or_already_resumed(
        operator=requester,
        call_arguments={
            "connector_id": "vault-1.x",
            "op_id": "vault.kv.put",
            "params": params,
            "target": None,
        },
        awaiting_envelope={"op_id": "vault.kv.put", "extras": {}},
        approval_request_id=request.id,
    )

    assert resumed == {"status": "ok"}
    async with get_sessionmaker()() as fresh:
        assert await fresh.get(ApprovalExecutionPayload, request.execution_handle) is None


@pytest.mark.asyncio
async def test_resume_no_ops_when_operator_surface_already_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-process waiter no-ops when an operator surface won the claim.

    #2293 double-dispatch polarity, agent side: the request is approved and
    an operator approval surface has already won the exactly-one-resumer
    claim (and re-dispatched). When the agent waiter then reads the
    approved row, it must lose the claim and surface a
    benign ``already_resumed`` envelope to the agent — **without** calling
    ``call_operation_with_approval`` a second time (the write must not run
    twice).
    """
    import meho_backplane.operations.meta_tools as meta_tools_module
    from meho_backplane.operations._validate import compute_params_hash
    from meho_backplane.operations.approval_queue import (
        claim_resume,
        create_pending_request,
    )

    requester = _make_operator(sub="agent-double-dispatch")
    params = {"value": "test-only-credential-value"}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        request = await create_pending_request(
            s,
            operator=requester,
            connector_id="vault-1.x",
            op_id="vault.kv.put",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            run_id=uuid.uuid4(),  # a run-bound request (the waiter is its resumer)
        )
        await s.commit()
    approval_request_id = request.id

    # The operator surface approved the request and then won the claim. The approve flip is
    # required because ``claim_resume`` now (F12 / #274) only latches an
    # ``approved`` row; without it the claim would fail closed on a pending
    # row rather than modelling the winning operator-surface resumer.
    async with sessionmaker() as s:
        row = await s.get(ApprovalRequest, approval_request_id)
        assert row is not None
        row.status = ApprovalRequestStatus.APPROVED.value
        row.decided_at = datetime.now(UTC)
        await s.commit()

    # An operator surface won the claim first (and re-dispatched).
    assert await claim_resume(approval_request_id) is True

    # If the waiter's guard regressed and it re-dispatched anyway, this spy
    # would fire — a second execution of the approved op.
    redispatched = 0

    async def _spy_call_with_approval(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal redispatched
        redispatched += 1
        raise AssertionError("the waiter must not re-dispatch after losing the claim")

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _spy_call_with_approval)

    awaiting_envelope: dict[str, Any] = {
        "status": "awaiting_approval",
        "op_id": "vault.kv.put",
        "extras": {"approval_request_id": str(approval_request_id)},
    }
    resumed = await resume_or_surface_awaiting_approval(
        operator=requester,
        call_arguments={
            "connector_id": "vault-1.x",
            "op_id": "vault.kv.put",
            "params": params,
            "target": None,
        },
        awaiting_envelope=awaiting_envelope,
        timeout_seconds=2.0,
    )

    assert resumed["status"] == "already_resumed", resumed
    assert resumed["extras"]["decision"] == "approved"
    assert resumed["extras"]["error_code"] == "already_resumed"
    assert redispatched == 0
    assert request.execution_handle is not None
    async with sessionmaker() as fresh:
        assert await fresh.get(ApprovalExecutionPayload, request.execution_handle) is not None


# ---------------------------------------------------------------------------
# The stored approval row is the only source (#3732, #3758)
# ---------------------------------------------------------------------------


async def _park_request(
    *,
    requester: Operator,
    status: ApprovalRequestStatus = ApprovalRequestStatus.PENDING,
    connector_id: str = "apprestest-1.x",
    op_id: str = "apprestest.op",
    params: dict[str, Any] | None = None,
) -> ApprovalRequest:
    """Park one run-bound request, optionally already moved to *status*."""
    from meho_backplane.operations._validate import compute_params_hash
    from meho_backplane.operations.approval_queue import create_pending_request

    params = params if params is not None else {"x": 1}
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=requester,
            connector_id=connector_id,
            op_id=op_id,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            run_id=uuid.uuid4(),
        )
        if status is not ApprovalRequestStatus.PENDING:
            request.status = status.value
            request.decided_at = datetime.now(UTC)
        await s.commit()
    return request


async def _set_status(request_id: uuid.UUID, status: ApprovalRequestStatus) -> None:
    """Commit a decision on the stored row."""
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        row.status = status.value
        row.decided_at = datetime.now(UTC)
        await s.commit()


async def _set_status_later(
    request_id: uuid.UUID, status: ApprovalRequestStatus, delay: float
) -> None:
    await asyncio.sleep(delay)
    await _set_status(request_id, status)


def _record_stored_read_times(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the loop time at which each stored-row read starts.

    Wraps the real ``get_request`` so the read still runs; the list of
    timestamps lets a test measure the gap between two reads.
    """
    import meho_backplane.operations.approval_queue as approval_queue_module

    real_get_request = approval_queue_module.get_request
    read_times: list[float] = []

    async def _timed_get_request(*args: Any, **kwargs: Any) -> Any:
        read_times.append(asyncio.get_running_loop().time())
        return await real_get_request(*args, **kwargs)

    monkeypatch.setattr(approval_queue_module, "get_request", _timed_get_request)
    return read_times


def _forbid_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every re-dispatch attempt; the caller asserts the list stays empty."""
    import meho_backplane.operations.meta_tools as meta_tools_module

    calls: list[dict[str, Any]] = []

    async def _spy(
        _operator: Operator, arguments: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        calls.append(arguments)
        return {"status": "ok"}

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _spy)
    return calls


def _awaiting(request_id: uuid.UUID, op_id: str = "apprestest.op") -> dict[str, Any]:
    return {
        "status": "awaiting_approval",
        "op_id": op_id,
        "error": f"awaiting_approval: '{op_id}' requires approval before execution",
        "extras": {"error_code": "awaiting_approval", "approval_request_id": str(request_id)},
    }


async def _resumed_at(request_id: uuid.UUID) -> datetime | None:
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        return row.resumed_at


@pytest.mark.parametrize(
    "status",
    [
        ApprovalRequestStatus.APPROVED,
        ApprovalRequestStatus.REJECTED,
        ApprovalRequestStatus.EXPIRED,
    ],
)
async def test_durable_decision_before_subscribe_is_returned(
    monkeypatch: pytest.MonkeyPatch, status: ApprovalRequestStatus
) -> None:
    """A decision committed before the wait starts is read from the stored row.

    The first stored read sees it, so the wait returns at once.
    """
    request = await _park_request(requester=_make_operator(), status=status)

    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID, approval_request_id=request.id, timeout_seconds=2.0
    )

    assert decision == status.value


async def test_decision_landing_mid_wait_is_returned() -> None:
    """A decision committed while the wait runs is seen by a later read."""
    request = await _park_request(requester=_make_operator())
    flip = asyncio.create_task(_set_status_later(request.id, ApprovalRequestStatus.APPROVED, 0.1))

    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID, approval_request_id=request.id, timeout_seconds=5.0
    )
    await flip

    assert decision == "approved"


async def test_poll_interval_bounds_the_gap_between_stored_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is read at least every :data:`_POLL_SECONDS` until it changes."""
    import meho_backplane.agent.approval_wait as approval_wait_module

    poll_s = 0.05
    monkeypatch.setattr(approval_wait_module, "_POLL_SECONDS", poll_s)
    request = await _park_request(requester=_make_operator())
    read_times = _record_stored_read_times(monkeypatch)
    flip = asyncio.create_task(_set_status_later(request.id, ApprovalRequestStatus.EXPIRED, 0.3))

    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID, approval_request_id=request.id, timeout_seconds=5.0
    )
    await flip

    assert decision == "expired"
    gaps = [later - earlier for earlier, later in itertools.pairwise(read_times)]
    assert len(gaps) >= 2
    # Each read takes a few milliseconds itself; allow for that and jitter.
    assert max(gaps) < poll_s + 0.1


async def test_timeout_honours_the_deadline() -> None:
    """The wait ends at its deadline, after one last stored read."""
    request = await _park_request(requester=_make_operator())

    started = asyncio.get_running_loop().time()
    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID, approval_request_id=request.id, timeout_seconds=0.3
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert decision == "timeout"
    assert elapsed < 0.3 + 0.2


async def test_default_poll_interval_is_at_most_five_seconds() -> None:
    from meho_backplane.agent.approval_wait import _POLL_SECONDS

    assert _POLL_SECONDS <= 5.0


async def test_deadline_reread_returns_late_durable_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decision committed after the last re-read is still seen at the deadline."""
    import meho_backplane.agent.approval_wait as approval_wait_module

    # No periodic re-read inside this short wait: only the final one can see it.
    monkeypatch.setattr(approval_wait_module, "_POLL_SECONDS", 100.0)
    request = await _park_request(requester=_make_operator())
    flip = asyncio.create_task(_set_status_later(request.id, ApprovalRequestStatus.REJECTED, 0.05))

    # The write runs on a SQLite helper thread and must commit before the
    # deadline read. A 2 s deadline leaves it ~1.95 s even on a loaded runner;
    # 0.3 s left ~0.25 s and timed out once in CI.
    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID, approval_request_id=request.id, timeout_seconds=2.0
    )
    await flip

    assert decision == "rejected"


async def test_timeout_only_when_row_still_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    request = await _park_request(requester=_make_operator())

    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID, approval_request_id=request.id, timeout_seconds=0.3
    )

    assert decision == "timeout"


async def test_timeout_when_stored_row_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing stored-row read is a controlled "no decision seen", not a crash."""
    from sqlalchemy.exc import OperationalError

    import meho_backplane.operations.approval_queue as approval_queue_module

    async def _broken_get_request(*_args: Any, **_kwargs: Any) -> ApprovalRequest:
        raise OperationalError("SELECT", {}, Exception("database unavailable"))

    monkeypatch.setattr(approval_queue_module, "get_request", _broken_get_request)

    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID, approval_request_id=uuid.uuid4(), timeout_seconds=0.3
    )

    assert decision == "timeout"


async def test_durable_read_is_tenant_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Another tenant's approved row is never seen as this wait's decision."""
    other_tenant = uuid.UUID("00000000-0000-0000-0000-00000000e0e0")
    request = await _park_request(
        requester=_make_operator(tenant_id=other_tenant),
        status=ApprovalRequestStatus.APPROVED,
    )

    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID, approval_request_id=request.id, timeout_seconds=0.2
    )

    assert decision == "timeout"


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        (ApprovalRequestStatus.REJECTED, "approval_rejected"),
        (ApprovalRequestStatus.EXPIRED, "approval_expired"),
    ],
)
async def test_durable_reject_or_expiry_never_dispatches(
    monkeypatch: pytest.MonkeyPatch, status: ApprovalRequestStatus, error_code: str
) -> None:
    requester = _make_operator()
    request = await _park_request(requester=requester)
    dispatched = _forbid_dispatch(monkeypatch)
    flip = asyncio.create_task(_set_status_later(request.id, status, 0.05))

    out = await resume_or_surface_awaiting_approval(
        operator=requester,
        call_arguments={"connector_id": "apprestest-1.x", "op_id": "apprestest.op"},
        awaiting_envelope=_awaiting(request.id),
        timeout_seconds=5.0,
    )
    await flip

    assert out["status"] == "awaiting_approval"
    assert out["extras"]["error_code"] == error_code
    assert out["extras"]["decision"] == status.value
    assert dispatched == []
    assert await _resumed_at(request.id) is None


async def test_cancellation_propagates_without_later_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled wait raises, and an approval that lands later runs nothing."""
    requester = _make_operator()
    request = await _park_request(requester=requester)
    dispatched = _forbid_dispatch(monkeypatch)

    task = asyncio.create_task(
        resume_or_surface_awaiting_approval(
            operator=requester,
            call_arguments={"connector_id": "apprestest-1.x", "op_id": "apprestest.op"},
            awaiting_envelope=_awaiting(request.id),
            timeout_seconds=5.0,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _set_status(request.id, ApprovalRequestStatus.APPROVED)
    await asyncio.sleep(0.1)
    assert dispatched == []
    assert await _resumed_at(request.id) is None


async def test_durable_approval_lost_claim_keeps_winner_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approval seen on the stored row still goes through ``claim_resume``.

    Another resumer already won the claim: this waiter returns
    ``already_resumed``, does not dispatch, and leaves the winner's
    credential-write custody row alone.
    """
    from meho_backplane.operations.approval_queue import claim_resume

    requester = _make_operator(sub="agent-durable-loser")
    params = {"value": "test-only-credential-value"}
    request = await _park_request(
        requester=requester,
        status=ApprovalRequestStatus.APPROVED,
        connector_id="vault-1.x",
        op_id="vault.kv.put",
        params=params,
    )
    assert request.execution_handle is not None
    assert await claim_resume(request.id) is True
    dispatched = _forbid_dispatch(monkeypatch)

    out = await resume_or_surface_awaiting_approval(
        operator=requester,
        call_arguments={"connector_id": "vault-1.x", "op_id": "vault.kv.put", "params": params},
        awaiting_envelope=_awaiting(request.id, op_id="vault.kv.put"),
        timeout_seconds=2.0,
    )

    assert out["status"] == "already_resumed"
    assert dispatched == []
    async with get_sessionmaker()() as fresh:
        assert await fresh.get(ApprovalExecutionPayload, request.execution_handle) is not None


async def test_durable_approval_winner_claims_and_discards_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requester = _make_operator(sub="agent-durable-winner")
    params = {"value": "test-only-credential-value"}
    request = await _park_request(
        requester=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.put",
        params=params,
    )
    assert request.execution_handle is not None
    dispatched = _forbid_dispatch(monkeypatch)
    flip = asyncio.create_task(_set_status_later(request.id, ApprovalRequestStatus.APPROVED, 0.05))

    out = await resume_or_surface_awaiting_approval(
        operator=requester,
        call_arguments={"connector_id": "vault-1.x", "op_id": "vault.kv.put", "params": params},
        awaiting_envelope=_awaiting(request.id, op_id="vault.kv.put"),
        timeout_seconds=5.0,
    )
    await flip

    assert out == {"status": "ok"}
    assert len(dispatched) == 1
    assert await _resumed_at(request.id) is not None
    async with get_sessionmaker()() as fresh:
        assert await fresh.get(ApprovalExecutionPayload, request.execution_handle) is None


async def test_competing_resumers_dispatch_at_most_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two waiters on one approved row: one dispatches, the other is already_resumed."""
    requester = _make_operator(sub="agent-durable-race")
    request = await _park_request(requester=requester, status=ApprovalRequestStatus.APPROVED)
    dispatched = _forbid_dispatch(monkeypatch)

    async def _resume() -> dict[str, Any]:
        return await resume_or_surface_awaiting_approval(
            operator=requester,
            call_arguments={"connector_id": "apprestest-1.x", "op_id": "apprestest.op"},
            awaiting_envelope=_awaiting(request.id),
            timeout_seconds=2.0,
        )

    outcomes = await asyncio.gather(_resume(), _resume())

    assert sorted(o["status"] for o in outcomes) == ["already_resumed", "ok"]
    assert len(dispatched) == 1


@pytest.mark.parametrize(
    "dispatch_envelope",
    [
        {
            "status": "error",
            "error": "upstream returned 500",
            "extras": {"error_code": "upstream_error"},
        },
        {
            "status": "error",
            "error": "upstream timed out; the outcome is unknown",
            "extras": {"error_code": "upstream_timeout"},
        },
    ],
    ids=["error", "uncertain"],
)
async def test_post_approval_dispatch_outcome_is_returned_unchanged(
    monkeypatch: pytest.MonkeyPatch, dispatch_envelope: dict[str, Any]
) -> None:
    """Approval is not success: the dispatcher's own envelope is what the agent sees."""
    import meho_backplane.operations.meta_tools as meta_tools_module

    requester = _make_operator(sub="agent-durable-outcome")
    request = await _park_request(requester=requester, status=ApprovalRequestStatus.APPROVED)

    async def _dispatch(
        _operator: Operator, _arguments: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        return dispatch_envelope

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _dispatch)

    out = await resume_or_surface_awaiting_approval(
        operator=requester,
        call_arguments={"connector_id": "apprestest-1.x", "op_id": "apprestest.op"},
        awaiting_envelope=_awaiting(request.id),
        timeout_seconds=2.0,
    )

    assert out == dispatch_envelope


# ---------------------------------------------------------------------------
# Composite sub-op re-entry vs. direct park (#3421)
# ---------------------------------------------------------------------------


async def _park_approved(
    *,
    requester: Operator,
    connector_id: str,
    op_id: str,
    params: dict[str, Any],
    resume_parent: dict[str, Any] | None = None,
    target_id: uuid.UUID | None = None,
) -> ApprovalRequest:
    """Park one request and flip it to ``approved`` (what a reviewer decision does).

    *target_id* is saved on the row the way a real park saves it: the queue
    reads only ``target.id`` from the dispatch target.
    """
    from meho_backplane.operations._validate import compute_params_hash
    from meho_backplane.operations.approval_queue import create_pending_request

    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=requester,
            connector_id=connector_id,
            op_id=op_id,
            target=SimpleNamespace(id=target_id) if target_id is not None else None,
            params=params,
            params_hash=compute_params_hash(params),
            run_id=uuid.uuid4(),
            resume_parent=resume_parent,
        )
        request.status = ApprovalRequestStatus.APPROVED.value
        request.decided_at = datetime.now(UTC)
        await s.commit()
    return request


async def test_direct_park_resume_redispatches_original_call_on_stored_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct park (``resume_parent IS NULL``) keeps the generic re-dispatch.

    The waiter passes the agent's original ``call_arguments`` through, with
    ``connector_id`` replaced by the catalog the row stored, and binds no
    composite resume state.
    """
    import meho_backplane.operations.meta_tools as meta_tools_module
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed
    from meho_backplane.operations.composite import (
        composite_resume_scope_var,
        composite_resume_var,
    )

    requester = _make_operator(sub="agent-direct-park")
    params = {"name": "direct"}
    request = await _park_approved(
        requester=requester, connector_id="apprestest-1.x", op_id="apprestest.op", params=params
    )
    call_arguments = {
        "connector_id": "apprestest-1.x",
        "op_id": "apprestest.op",
        "params": params,
        "target": None,
        "work_ref": "CHG-1",
    }
    seen: list[tuple[dict[str, Any], Any, Any]] = []

    async def _spy(
        _operator: Operator, arguments: dict[str, Any], *, resolved_target: Any = None
    ) -> dict[str, Any]:
        assert resolved_target is None, "tenant-wide park: no saved target"
        seen.append((arguments, composite_resume_var.get(), composite_resume_scope_var.get()))
        return {"status": "ok"}

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _spy)
    resumed = await _resume_approved_or_already_resumed(
        operator=requester,
        call_arguments=call_arguments,
        awaiting_envelope={"op_id": "apprestest.op", "extras": {}},
        approval_request_id=request.id,
    )

    assert resumed == {"status": "ok"}
    assert seen == [(call_arguments, None, None)]
    assert seen[0][0] is not call_arguments, "the agent's own dict must stay untouched"


async def test_credential_composite_subop_resume_uses_handoff_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential-write composite sub-op park re-enters its parent from custody.

    The row stores only the parent's ``op_id``; the parent's params live in the
    encrypted custody copy. The agent claim winner consumes that copy (it does
    not just discard it), re-enters the parent composite with the approved
    sub-op cleared, and leaves no custody row behind.
    """
    import meho_backplane.operations.meta_tools as meta_tools_module
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed
    from meho_backplane.operations.composite import (
        composite_resume_scope_var,
        composite_resume_var,
    )

    requester = _make_operator(sub="agent-credential-composite")
    subop_params = {"value": "test-only-credential-value"}
    parent = {"op_id": "composite.rotate", "params": {"password": "test-only-parent-value"}}
    request = await _park_approved(
        requester=requester,
        connector_id="vault-1.x",
        op_id="vault.kv.put",
        params=subop_params,
        resume_parent=parent,
    )
    assert request.resume_parent == {"op_id": "composite.rotate"}
    assert request.execution_handle is not None
    seen: list[tuple[dict[str, Any], Any, Any]] = []

    async def _spy(
        _operator: Operator, arguments: dict[str, Any], *, resolved_target: Any = None
    ) -> dict[str, Any]:
        assert resolved_target is None, "tenant-wide park: no saved target"
        seen.append((arguments, composite_resume_var.get(), composite_resume_scope_var.get()))
        return {"status": "ok"}

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _spy)
    resumed = await _resume_approved_or_already_resumed(
        operator=requester,
        call_arguments={
            "connector_id": "vault-1.x",
            "op_id": "composite.rotate",
            "params": {},
            "target": None,
        },
        awaiting_envelope={"op_id": "vault.kv.put", "extras": {}},
        approval_request_id=request.id,
    )

    assert resumed == {"status": "ok"}
    approved_subop = ("vault.kv.put", request.params_hash)
    assert seen == [
        (
            {
                "connector_id": "vault-1.x",
                "op_id": "composite.rotate",
                "params": parent["params"],
                "target": None,
            },
            approved_subop,
            approved_subop,
        )
    ]
    assert composite_resume_var.get() is None
    async with get_sessionmaker()() as fresh:
        assert await fresh.get(ApprovalExecutionPayload, request.execution_handle) is None
        row = await fresh.get(ApprovalRequest, request.id)
        assert row is not None
        assert row.resume_parent == {"op_id": "composite.rotate"}


# ---------------------------------------------------------------------------
# Composite re-entry dispatches against the target the row saved by id
# ---------------------------------------------------------------------------


async def _persist_target(
    name: str, *, product: str = "apprestest", version: str | None = None
) -> uuid.UUID:
    """Persist one live target row in the test tenant and return its id."""
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name=name,
                product=product,
                version=version,
                host="target.invalid",
                aliases=[],
            )
        )
        await s.commit()
    return target_id


async def _park_composite_subop_on_target(target_id: uuid.UUID) -> ApprovalRequest:
    """Park an approved composite sub-op that was dispatched against *target_id*."""
    return await _park_approved(
        requester=_make_operator(sub="agent-composite-target"),
        connector_id="apprestest-1.x",
        op_id="apprestest.sub",
        params={"vm": "vm-1"},
        resume_parent={"op_id": "apprestest.composite", "params": {"vm": "vm-1"}},
        target_id=target_id,
    )


async def test_composite_resume_uses_saved_target_when_name_was_reassigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renamed target: the re-entry runs against the saved target, not the name.

    The agent parked while the target was called ``vc-a``. While the request
    waited, that target was renamed and a new target took the name ``vc-a``.
    The composite re-entry must dispatch against the original target (loaded
    by the saved id) and must not look the stale name up at all.
    """
    import meho_backplane.operations.meta_tools as meta_tools_module
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed
    from meho_backplane.operations.composite import (
        composite_resume_scope_var,
        composite_resume_var,
    )

    original_id = await _persist_target("vc-a")
    request = await _park_composite_subop_on_target(original_id)
    assert request.target_id == original_id

    async with get_sessionmaker()() as s:
        original = await s.get(TargetORM, original_id)
        assert original is not None
        original.name = "vc-b"
        await s.commit()
    impostor_id = await _persist_target("vc-a")

    seen: list[dict[str, Any]] = []

    async def _spy(
        _operator: Operator, arguments: dict[str, Any], *, resolved_target: Any = None
    ) -> dict[str, Any]:
        seen.append(
            {
                "arguments": arguments,
                "target_id": getattr(resolved_target, "id", None),
                "target_name": getattr(resolved_target, "name", None),
                "resume": composite_resume_var.get(),
                "scope": composite_resume_scope_var.get(),
            }
        )
        return {"status": "ok"}

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _spy)
    resumed = await _resume_approved_or_already_resumed(
        operator=_make_operator(sub="agent-composite-target"),
        call_arguments={
            "connector_id": "apprestest-1.x",
            "op_id": "apprestest.composite",
            "params": {"vm": "vm-1"},
            "target": {"name": "vc-a"},
        },
        awaiting_envelope={"op_id": "apprestest.sub", "extras": {}},
        approval_request_id=request.id,
    )

    assert resumed == {"status": "ok"}
    assert len(seen) == 1
    assert seen[0]["target_id"] == original_id
    assert seen[0]["target_id"] != impostor_id
    assert seen[0]["target_name"] == "vc-b"
    assert seen[0]["arguments"]["target"] is None
    assert seen[0]["arguments"]["op_id"] == "apprestest.composite"
    approved_subop = ("apprestest.sub", request.params_hash)
    assert seen[0]["resume"] == approved_subop
    assert seen[0]["scope"] == approved_subop


async def test_composite_resume_fails_closed_when_saved_target_deleted_and_name_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted target whose name was reused: refuse, dispatch nothing, keep the claim.

    The saved target was soft-deleted while the request waited and a new
    target now carries its old name. The re-entry must not run against the
    new target. It returns a ``denied`` envelope and leaves the one-resumer
    claim free, the same as the operator surface.
    """
    import meho_backplane.operations.meta_tools as meta_tools_module
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed

    original_id = await _persist_target("vc-a")
    request = await _park_composite_subop_on_target(original_id)

    async with get_sessionmaker()() as s:
        original = await s.get(TargetORM, original_id)
        assert original is not None
        original.deleted_at = datetime.now(UTC)
        await s.commit()
    await _persist_target("vc-a")

    calls: list[Any] = []

    async def _spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        return {"status": "ok"}

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _spy)
    resumed = await _resume_approved_or_already_resumed(
        operator=_make_operator(sub="agent-composite-target"),
        call_arguments={
            "connector_id": "apprestest-1.x",
            "op_id": "apprestest.composite",
            "params": {"vm": "vm-1"},
            "target": {"name": "vc-a"},
        },
        awaiting_envelope={"op_id": "apprestest.sub", "extras": {}},
        approval_request_id=request.id,
    )

    assert resumed["status"] == "denied"
    assert resumed["extras"]["error_code"] == "denied"
    assert str(original_id) in resumed["error"]
    assert calls == []
    async with get_sessionmaker()() as fresh:
        row = await fresh.get(ApprovalRequest, request.id)
    assert row is not None
    assert row.resumed_at is None, "a refused resume must not burn the claim"


# ---------------------------------------------------------------------------
# Direct park on a sibling catalog (#3600): the resume uses the stored catalog
# ---------------------------------------------------------------------------

_GUARD_OP_ID = "POST:/api/vcenter/vm/example"


@pytest.fixture
async def _vmware_sibling_target() -> AsyncIterator[uuid.UUID]:
    """The 9.0 and 8.0 vCenter catalogs, the guard op in both, and one 8.0.3 target.

    Reuses the dispatcher suite's catalog doubles so the park and the resume
    run the production catalog guard and sibling fallback. The op is
    ``caution``: an agent needs no grant for that level, and it still parks
    for approval.
    """
    from meho_backplane.connectors.registry import clear_registry
    from meho_backplane.operations import reset_dispatcher_caches
    from tests.test_operations_dispatcher import (
        _add_vmware_guard_descriptor,
        _register_vmware_sibling_catalogs,
    )

    reset_dispatcher_caches()
    clear_registry()
    _register_vmware_sibling_catalogs()
    async with get_sessionmaker()() as s:
        await _add_vmware_guard_descriptor(s, version="9.0", safety_level="caution")
        await _add_vmware_guard_descriptor(s, version="8.0", safety_level="caution")
    target_id = await _persist_target("vc-sibling", product="vmware", version="8.0.3")
    yield target_id
    reset_dispatcher_caches()
    clear_registry()


async def _park_sibling_call_as_agent(
    operator: Operator,
) -> tuple[dict[str, Any], dict[str, Any], uuid.UUID]:
    """Park a ``vmware-rest-9.0`` call for the 8.0.3 target and approve it.

    Returns the agent's original ``call_arguments``, the awaiting envelope
    and the request id. The park runs on the 8.0 sibling catalog, so the
    row stores ``vmware-rest-8.0``.
    """
    from meho_backplane.operations.approval_queue import approve_request
    from meho_backplane.operations.meta_tools import call_operation

    call_arguments: dict[str, Any] = {
        "connector_id": "vmware-rest-9.0",
        "op_id": _GUARD_OP_ID,
        "target": "vc-sibling",
        "params": {},
    }
    awaiting = await call_operation(operator, call_arguments)
    assert awaiting["status"] == "awaiting_approval", awaiting
    request_id = uuid.UUID(awaiting["extras"]["approval_request_id"])
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.connector_id == "vmware-rest-8.0"
    reviewer = _make_operator(sub="human-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=reviewer, params=None)
        await s.commit()
    return call_arguments, awaiting, request_id


async def _guard_op_audit_rows() -> list[AuditLog]:
    """The dispatch rows for the guard op. A park writes its row under another path."""
    async with get_sessionmaker()() as fresh:
        rows = await fresh.execute(select(AuditLog).where(AuditLog.path == _GUARD_OP_ID))
        return list(rows.scalars().all())


async def test_agent_resume_runs_on_the_stored_sibling_catalog(
    _vmware_sibling_target: uuid.UUID,
) -> None:
    """The agent resume runs on the catalog the row stored, not the one it named.

    The agent named ``vmware-rest-9.0`` for an 8.0.3 target, so the park ran
    on the 8.0 sibling and stored ``vmware-rest-8.0``. After approval the
    waiter re-dispatches with the agent's original ``call_arguments``. An
    approved call never falls back to a sibling, so re-using the agent's
    ``connector_id`` would refuse the resume. The waiter must use the
    stored catalog: the op then runs on 8.0, as the approver saw it.
    """
    from tests.test_operations_dispatcher import _RecordingVmwareTransport

    operator = _make_operator(sub="agent-sibling-resume")
    call_arguments, awaiting, _request_id = await _park_sibling_call_as_agent(operator)
    assert _RecordingVmwareTransport.sent == []

    resumed = await resume_or_surface_awaiting_approval(
        operator=operator,
        call_arguments=call_arguments,
        awaiting_envelope=awaiting,
        timeout_seconds=2.0,
    )

    assert resumed["status"] == "ok", resumed
    assert _RecordingVmwareTransport.sent == [("8.0", "/api/vcenter/vm/example")]
    assert call_arguments["connector_id"] == "vmware-rest-9.0"
    audits = await _guard_op_audit_rows()
    assert [a.payload["result_status"] for a in audits] == ["ok"]
    assert audits[0].payload["connector_version"] == "8.0"


async def test_agent_resume_is_refused_when_the_target_version_moved(
    _vmware_sibling_target: uuid.UUID,
) -> None:
    """A target that moved to 9.x while the request waited gets no approved run.

    The park stored ``vmware-rest-8.0``. By the time the approval lands the
    target reports 9.0.1. The resume stays on the stored catalog, and that
    catalog no longer fits the target, so the resume is refused with
    ``unqualified_target_version``. Running the 9.0 op here instead would be
    an operation the approver never saw, with the policy gate skipped.
    """
    from tests.test_operations_dispatcher import _RecordingVmwareTransport

    operator = _make_operator(sub="agent-sibling-resume-moved")
    call_arguments, awaiting, _request_id = await _park_sibling_call_as_agent(operator)
    async with get_sessionmaker()() as s:
        target = await s.get(TargetORM, _vmware_sibling_target)
        assert target is not None
        target.version = "9.0.1"
        await s.commit()

    resumed = await resume_or_surface_awaiting_approval(
        operator=operator,
        call_arguments=call_arguments,
        awaiting_envelope=awaiting,
        timeout_seconds=2.0,
    )

    assert resumed["status"] == "error", resumed
    assert resumed["extras"]["error_code"] == "unqualified_target_version"
    assert resumed["extras"]["target_version"] == "9.0.1"
    assert _RecordingVmwareTransport.sent == []
    audits = await _guard_op_audit_rows()
    assert [a.payload["result_status"] for a in audits] == ["unqualified_target_version"]
    assert audits[0].payload["connector_version"] == "8.0"
