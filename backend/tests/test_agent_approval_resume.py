# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for the agent-runtime approval-resume substrate (G11.1-T9 / #1117).

Covers the four acceptance criteria the issue body specifies:

1. The wrapped ``call_operation`` tool the agent loop calls subscribes (via the
   existing broadcast SSE/watch substrate) and reacts to
   ``approval.{approved,rejected}`` events keyed on its own pending
   ``approval_request_id``.
2. On ``approval.approved``, the runtime resumes by re-invoking
   :func:`~meho_backplane.operations.dispatch` with ``_approved=True`` and the
   original in-memory params; the audit chain shows the op actually executed.
3. On ``approval.rejected``, the runtime surfaces the rejection to the tool
   result (the model's view) so the agent can reason about the operator's
   decision.
4. The test below pauses an agent run → operator approves via the
   :func:`~meho_backplane.operations.approval_queue.approve_request` /
   ``broadcast_publish_event`` path the ``/decide`` and MCP ``meho.approvals.*``
   handlers use (**not** the REST ``/approve+params`` re-dispatch express lane)
   → the agent run resumes and the op executes with the correct audit shape.

What the tests deliberately do NOT do
-------------------------------------

* They do NOT spawn a real Pydantic AI loop — the wrapped tool's behaviour is
  the unit of interest (the loop just calls it). Driving a real
  ``FunctionModel`` would add ~1s per case for zero coverage gain; the
  parallel suite ``test_agent_invoke.py`` already exercises the loop wiring.
* They do NOT hit Valkey — ``get_broadcast_blocking_client`` is stubbed with an
  AsyncMock whose ``xread`` returns a single seeded entry once and then
  idles (the BLOCK-timeout shape the existing UI feed tests use).
* They do NOT cover process-restart resume — that's an explicit out-of-scope
  item in the #1117 task body (live-wait only; durable-checkpoint follow-up
  belongs to a separate G11.1 task).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agent.approval_wait import (
    AWAITING_APPROVAL_TIMEOUT_ERROR_CODE,
    resume_or_surface_awaiting_approval,
    wait_for_approval_decision,
)
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.broadcast.events import BroadcastEvent
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
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


def _build_decision_entry(
    *,
    approval_request_id: uuid.UUID,
    decision: str,
    tenant_id: uuid.UUID = _TENANT_ID,
    audit_id: uuid.UUID | None = None,
) -> tuple[str, dict[str, str]]:
    """Build one XREAD entry shaped exactly like ``publish_approval_event`` writes.

    Mirrors :func:`meho_backplane.operations.approval_queue.publish_approval_event`
    verbatim so the test's stubbed stream is wire-compatible with what the
    publisher would produce: one ``event`` field carrying the JSON-serialised
    :class:`BroadcastEvent`, payload keyed by ``approval_request_id`` /
    ``decision``. Each test seeds one decision entry; the AsyncMock idles
    forever after, simulating the broadcast tail.
    """
    event = BroadcastEvent(
        event_id=uuid.uuid4(),
        ts=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        tenant_id=tenant_id,
        principal_sub="reviewer-sub",
        op_id=f"approval.{decision}",
        op_class="other",
        result_status="ok",
        audit_id=audit_id or uuid.uuid4(),
        payload={
            "op_class": "other",
            "result_status": "ok",
            "approval_request_id": str(approval_request_id),
            "decision": decision,
            "connector_id": "apprestest-1.x",
            "approval_op_id": "apprestest.op",
        },
    )
    return ("1715600000000-0", {"event": event.model_dump_json()})


def _stub_broadcast_client_with_decision(
    *,
    monkeypatch: pytest.MonkeyPatch,
    decision_entry: tuple[str, dict[str, str]],
) -> AsyncMock:
    """Stub the agent's blocking-client getter; yield *decision_entry* once, then idle.

    Subsequent ``xread`` calls return ``None`` (the BLOCK-timeout shape) so
    the wait loop idles cleanly until the test completes or the wall-clock
    cap fires. Patches the agent's ``get_broadcast_blocking_client``
    (the long-poll client switched in for RDC #789 N1 / Initiative
    #1353) and the module-level binding on
    :mod:`meho_backplane.broadcast.client` so callers via either path
    see the same fake.

    Returns the AsyncMock so callers can assert against its ``xread`` call
    history.
    """
    stream_key = f"meho:feed:{_TENANT_ID}"
    call_state = {"n": 0}

    async def _xread_side_effect(*args: object, **kwargs: object) -> object:
        call_state["n"] += 1
        if call_state["n"] == 1:
            return [(stream_key, [decision_entry])]
        # Honour the caller's block window so the next iteration doesn't
        # tight-loop the event loop.
        block_ms = kwargs.get("block")
        if isinstance(block_ms, int) and block_ms > 0:
            await asyncio.sleep(min(0.01, block_ms / 1000))
        return None

    client = AsyncMock()
    client.xread = AsyncMock(side_effect=_xread_side_effect)
    monkeypatch.setattr(
        "meho_backplane.agent.approval_wait.get_broadcast_blocking_client",
        lambda: client,
    )
    monkeypatch.setattr(
        "meho_backplane.broadcast.client.get_broadcast_blocking_client",
        lambda: client,
    )
    return client


# ---------------------------------------------------------------------------
# wait_for_approval_decision — the read-side primitive
# ---------------------------------------------------------------------------


async def test_wait_returns_approved_when_decision_broadcast_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait observes an ``approval.approved`` entry on the tenant stream.

    Asserts the read-side primitive's contract: subscribe → filter on the
    request id → return ``"approved"``. This is the building block the
    wrapped ``call_operation`` tool uses.
    """
    approval_request_id = uuid.uuid4()
    _stub_broadcast_client_with_decision(
        monkeypatch=monkeypatch,
        decision_entry=_build_decision_entry(
            approval_request_id=approval_request_id,
            decision="approved",
        ),
    )

    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID,
        approval_request_id=approval_request_id,
        timeout_seconds=2.0,
    )
    assert decision == "approved"


async def test_wait_returns_rejected_when_decision_broadcast_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait observes an ``approval.rejected`` entry and reports it."""
    approval_request_id = uuid.uuid4()
    _stub_broadcast_client_with_decision(
        monkeypatch=monkeypatch,
        decision_entry=_build_decision_entry(
            approval_request_id=approval_request_id,
            decision="rejected",
        ),
    )

    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID,
        approval_request_id=approval_request_id,
        timeout_seconds=2.0,
    )
    assert decision == "rejected"


async def test_wait_ignores_other_requests_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decision broadcast for a DIFFERENT request id does not satisfy the wait.

    Guards against the "wait returns on first decision event" foot-gun: in
    a busy tenant, multiple pending approvals are decided in parallel; each
    waiter must filter on its own request id.
    """
    other_request_id = uuid.uuid4()
    target_request_id = uuid.uuid4()
    _stub_broadcast_client_with_decision(
        monkeypatch=monkeypatch,
        decision_entry=_build_decision_entry(
            approval_request_id=other_request_id,
            decision="approved",
        ),
    )

    # Sub-second timeout via the fixture; the wait should idle past the
    # mismatched event and return "timeout" rather than wrongly returning
    # "approved".
    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID,
        approval_request_id=target_request_id,
        timeout_seconds=0.3,
    )
    assert decision == "timeout"


async def test_wait_times_out_when_no_decision_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No decision event → wait returns ``"timeout"`` after the cap elapses."""
    client = AsyncMock()
    client.xread = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "meho_backplane.agent.approval_wait.get_broadcast_blocking_client",
        lambda: client,
    )

    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID,
        approval_request_id=uuid.uuid4(),
        timeout_seconds=0.2,
    )
    assert decision == "timeout"


async def test_wait_fail_open_on_broadcast_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Valkey-side error during ``xread`` returns ``"timeout"`` (fail-open).

    Mirrors the publisher's fail-open contract: a broadcast outage during
    the wait surfaces as a timeout so the agent can decide what to do, not
    as an uncaught exception that crashes the run. The audit row + decision
    row remain the durable source of truth.
    """
    from redis.exceptions import ConnectionError as RedisConnectionError

    client = AsyncMock()
    client.xread = AsyncMock(side_effect=RedisConnectionError("valkey unreachable"))
    monkeypatch.setattr(
        "meho_backplane.agent.approval_wait.get_broadcast_blocking_client",
        lambda: client,
    )

    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID,
        approval_request_id=uuid.uuid4(),
        timeout_seconds=0.3,
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
       call the ``/decide`` route + MCP ``meho.approvals.approve`` use —
       NOT the REST ``/approve+params`` re-dispatch express lane).
    4. The broadcast publishes ``approval.approved`` for the request.
    5. The agent's wait observes the event and the wrapper re-invokes
       ``dispatch(..., _approved=True)`` with the original params.
    6. The audit chain shows the op actually executed.
    """
    import meho_backplane.operations._audit as audit_module
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import clear_registry, register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
    from meho_backplane.operations import (
        dispatch,
        register_typed_operation,
        reset_dispatcher_caches,
    )
    from meho_backplane.operations.approval_queue import approve_request

    # Capture publish_event calls so the dispatcher's audit publish doesn't
    # hit Valkey during the test.
    captured: list[Any] = []

    async def _capture(event: Any) -> None:
        captured.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)

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
    # path. This is the path /decide (REST) and meho.approvals.approve
    # (MCP) take -- the ``approve_request`` call by id alone, **without**
    # the params re-dispatch the legacy REST /approve+params route does.
    # The reviewer must differ from the agent requester (self-approval
    # guard, G11.7-T1 #1401).
    reviewer = _make_operator(sub="human-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(s, approval_request_id, operator=reviewer, params=None)
        await s.commit()
    assert row.status == "approved"

    # Step 3: the broadcast publishes approval.approved. Stub the broadcast
    # client so the wait sees the event without a real Valkey.
    _stub_broadcast_client_with_decision(
        monkeypatch=monkeypatch,
        decision_entry=_build_decision_entry(
            approval_request_id=approval_request_id,
            decision="approved",
        ),
    )

    # Step 4: drive the agent's resume helper -- the wait observes the
    # decision event and the wrapper re-dispatches with _approved=True.
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
    import meho_backplane.operations._audit as audit_module
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

    async def _capture(event: Any) -> None:
        pass

    monkeypatch.setattr(audit_module, "publish_event", _capture)

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
    _stub_broadcast_client_with_decision(
        monkeypatch=monkeypatch,
        decision_entry=_build_decision_entry(
            approval_request_id=approval_request_id,
            decision="approved",
        ),
    )
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
    _stub_broadcast_client_with_decision(
        monkeypatch=monkeypatch,
        decision_entry=_build_decision_entry(
            approval_request_id=approval_request_id,
            decision="rejected",
        ),
    )

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

    # No matching event will arrive; the wait elapses.
    client = AsyncMock()
    client.xread = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "meho_backplane.agent.approval_wait.get_broadcast_blocking_client",
        lambda: client,
    )

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


async def test_wait_skips_malformed_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed broadcast entry is logged + skipped, the wait keeps looping.

    Guards against a single bad publisher (or a wire-format drift) poisoning
    every waiting agent. The wait skips the entry and continues until either
    a good entry arrives or the timeout elapses.
    """
    approval_request_id = uuid.uuid4()
    malformed_entry = ("1715600000000-0", {"event": "not-json"})
    good_entry = _build_decision_entry(
        approval_request_id=approval_request_id,
        decision="approved",
    )
    call_state = {"n": 0}

    async def _xread_side_effect(*args: object, **kwargs: object) -> object:
        call_state["n"] += 1
        if call_state["n"] == 1:
            return [(f"meho:feed:{_TENANT_ID}", [malformed_entry])]
        if call_state["n"] == 2:
            return [(f"meho:feed:{_TENANT_ID}", [good_entry])]
        return None

    client = AsyncMock()
    client.xread = AsyncMock(side_effect=_xread_side_effect)
    monkeypatch.setattr(
        "meho_backplane.agent.approval_wait.get_broadcast_blocking_client",
        lambda: client,
    )

    decision = await wait_for_approval_decision(
        tenant_id=_TENANT_ID,
        approval_request_id=approval_request_id,
        timeout_seconds=2.0,
    )
    assert decision == "approved"


# ---------------------------------------------------------------------------
# Exactly-one-resumer claim — the waiter loses the race (#2293)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_no_ops_when_operator_surface_already_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-process waiter no-ops when an operator surface won the claim.

    #2293 double-dispatch polarity, agent side: the request is approved and
    an operator approval surface has already won the exactly-one-resumer
    claim (and re-dispatched). When the agent waiter then observes the
    ``approval.approved`` broadcast, it must lose the claim and surface a
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
    params = {"x": 7}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        request = await create_pending_request(
            s,
            operator=requester,
            connector_id="apprestest-1.x",
            op_id="apprestest.op",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            run_id=uuid.uuid4(),  # a run-bound request (the waiter is its resumer)
        )
        await s.commit()
    approval_request_id = request.id

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

    _stub_broadcast_client_with_decision(
        monkeypatch=monkeypatch,
        decision_entry=_build_decision_entry(
            approval_request_id=approval_request_id,
            decision="approved",
        ),
    )

    awaiting_envelope: dict[str, Any] = {
        "status": "awaiting_approval",
        "op_id": "apprestest.op",
        "extras": {"approval_request_id": str(approval_request_id)},
    }
    resumed = await resume_or_surface_awaiting_approval(
        operator=requester,
        call_arguments={
            "connector_id": "apprestest-1.x",
            "op_id": "apprestest.op",
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
