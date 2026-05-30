# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end CI exercise for the R2 operator-approval-gate example.

Initiative #807 (G11.6 Reference patterns), Task #1082 (R2). This test
is the CI sentinel for the recipe shipped in
``examples/r2-approval-gate/``: it drives the full
pause → approve → resume → execute cycle against the real backplane
primitives so the example cannot silently rot when the primitives
evolve.

What it covers
==============

The Initiative #807 acceptance criterion for R2:

> The sample pauses a change-class action for approval and resumes on
> approve (CI-exercised against the integration stack).

…interpreted as the unit-lane shape: the test stubs the broadcast
Valkey client (the same shape ``test_agent_approval_resume.py`` uses)
and exercises the in-process backplane primitives end-to-end. The
``approval_queue`` / ``approval_wait`` / ``policy_gate`` modules are
the real code; only the Valkey transport is faked. A testcontainers
variant under ``tests/integration/`` would add Valkey wall-clock cost
without changing the contract this gate enforces.

What it deliberately does NOT do
================================

* It does NOT run a Pydantic AI loop. The wrapped ``call_operation``
  tool's behaviour is the unit of interest; ``test_agent_invoke.py``
  already exercises the loop wiring with a ``FunctionModel``.
* It does NOT register a real connector. The handler is a module-level
  function registered via ``register_typed_operation`` with
  ``requires_approval=True`` — the gate fires on the descriptor flag,
  not on the connector identity.
* It does NOT mint real JWTs. The :class:`Operator` is built directly;
  the surface tests in ``test_api_v1_approvals.py`` cover the JWT/RBAC
  layer.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agent.approval_wait import resume_or_surface_awaiting_approval
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.broadcast.events import BroadcastEvent
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    AuditLog,
    PermissionVerdict,
    Tenant,
)
from meho_backplane.settings import get_settings

# No module-level ``pytestmark = pytest.mark.asyncio`` — the conftest sets
# ``asyncio_mode = "auto"`` in pyproject.toml, so async test bodies are
# auto-marked. Setting the marker module-wide here would over-mark the
# two sync ``test_*_fixture`` tests and emit a PytestWarning.

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Tenant the example agent runs under. Distinct from the other approval-
#: queue test files' tenant ids so a parallel xdist worker cannot bleed rows.
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000e2e2")

#: The agent principal's ``sub`` claim. In production this is the Keycloak
#: agent client's ``sub`` (G11.2-T1 #815); here we hard-code one.
_AGENT_SUB = "agent:r2-approval-gate-example"

#: The human operator's ``sub`` — the reviewer who decides on the request.
_OPERATOR_SUB = "operator:r2-approval-gate-example"

#: The change-class op the demo agent exercises. Distinct from the existing
#: ``vmware.composite.vm.snapshot.revert`` op id so the test does not collide
#: with the real composite registry — the gate's behaviour is identical for
#: any op carrying ``requires_approval=True``.
_OP_ID = "examples.r2.snapshot.revert"

#: The directory the example's JSON fixtures live in. The test asserts the
#: fixtures parse + match the test's expectations so a future edit to the
#: fixtures (or to the test) does not silently desync the two.
_EXAMPLE_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "r2-approval-gate"


# ---------------------------------------------------------------------------
# Module-level handler (closures are rejected by derive_handler_ref)
# ---------------------------------------------------------------------------


async def _r2_example_revert_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Stand-in handler for the demo's change-class op.

    Returns a deterministic payload the test can assert against; never
    touches a real vCenter. The `examples.r2.snapshot.revert` op id this
    handler backs is otherwise identical in shape to the production
    `vmware.composite.vm.snapshot.revert` op (`requires_approval=True`,
    `safety_level="dangerous"`).
    """
    return {
        "vm_id": params.get("vm_id"),
        "snapshot_name": params.get("snapshot_name"),
        "executed": True,
        "operator_sub": operator.sub,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires + reset broadcast state.

    Mirrors the env-pinning convention every approval-queue test file uses
    (``test_agent_approval_resume.py``, ``test_approval_queue.py``). The
    short ``AGENT_APPROVAL_WAIT_TIMEOUT_SECONDS`` keeps the timeout-path
    assertion (``test_resume_surfaces_rejection_to_model``-style) cheap
    even though this file does not exercise that path.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("AGENT_APPROVAL_WAIT_TIMEOUT_SECONDS", "2.0")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Open one session against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_agent_operator(sub: str = _AGENT_SUB) -> Operator:
    """Construct the agent's :class:`Operator`.

    ``principal_kind=AGENT`` is load-bearing — only agent principals reach
    the ``needs-approval`` branch of the policy gate; humans / service
    accounts are hard-denied on a ``requires_approval`` op (the v0.2
    contract preserved by :func:`policy_gate`).
    """
    return Operator(
        sub=sub,
        name="R2 Example Agent",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


def _make_human_operator(sub: str = _OPERATOR_SUB) -> Operator:
    """Construct the human reviewer's :class:`Operator`.

    Used for the approval-decision side of the test (the agent's principal
    files the request; the human's principal decides it). Both share the
    same ``tenant_id`` so the tenant-isolation gate passes.
    """
    return Operator(
        sub=sub,
        name="R2 Example Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _build_approval_decision_entry(
    *,
    approval_request_id: uuid.UUID,
    decision: str,
) -> tuple[str, dict[str, str]]:
    """Build one ``XREAD`` entry shaped like ``publish_approval_event`` writes.

    Mirrors the producer in
    :func:`~meho_backplane.operations.approval_queue.publish_approval_event`
    verbatim so the stubbed Valkey stream is wire-compatible with the real
    publisher. The agent's wait reads ``payload.approval_request_id`` off
    the parsed entry; everything else is decoration.
    """
    event = BroadcastEvent(
        event_id=uuid.uuid4(),
        ts=datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC),
        tenant_id=_TENANT_ID,
        principal_sub=_OPERATOR_SUB,
        op_id=f"approval.{decision}",
        op_class="other",
        result_status="ok",
        audit_id=uuid.uuid4(),
        payload={
            "op_class": "other",
            "result_status": "ok",
            "approval_request_id": str(approval_request_id),
            "decision": decision,
            "connector_id": "examples-1.x",
            "approval_op_id": _OP_ID,
        },
    )
    return ("1715600000000-0", {"event": event.model_dump_json()})


def _stub_broadcast_client(
    *,
    monkeypatch: pytest.MonkeyPatch,
    decision_entry: tuple[str, dict[str, str]],
) -> AsyncMock:
    """Stub the long-poll client (RDC #789 N1 / #1353) with a one-shot Valkey fake.

    The first ``xread`` call yields ``decision_entry``; subsequent calls
    idle by honouring the BLOCK window (matching the
    ``test_agent_approval_resume`` stub shape — see
    ``_stub_broadcast_client_with_decision`` there for the same idiom).
    Both the agent module's binding and the
    :mod:`meho_backplane.broadcast.client` module's binding are patched
    so callers via either path see the same fake.
    """
    stream_key = f"meho:feed:{_TENANT_ID}"
    call_state = {"n": 0}

    async def _xread_side_effect(*args: object, **kwargs: object) -> object:
        call_state["n"] += 1
        if call_state["n"] == 1:
            return [(stream_key, [decision_entry])]
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
# Fixture-fidelity assertions
# ---------------------------------------------------------------------------


def test_agent_definition_fixture_is_valid_against_schema() -> None:
    """``examples/r2-approval-gate/agent_definition.json`` parses cleanly.

    The fixture is the runnable artifact end-users copy; if Pydantic's
    schema drifts (a new required field, a renamed enum), this test fires
    so the example is updated alongside the schema change. The
    placeholder ``identity_ref`` is swapped for a fake-UUID-like string
    before construction so the schema's ``min_length=1`` is satisfied.
    """
    from meho_backplane.agents.schemas import AgentDefinitionCreate

    payload = json.loads((_EXAMPLE_DIR / "agent_definition.json").read_text())
    # `identity_ref` ships as a placeholder; swap for a stable test value
    # so Pydantic's min_length=1 is satisfied without modifying the
    # fixture on disk.
    payload["identity_ref"] = _AGENT_SUB

    parsed = AgentDefinitionCreate(**payload)

    assert parsed.name == "vmware-snapshot-revert-agent"
    assert parsed.identity_ref == _AGENT_SUB
    assert parsed.model_tier.value == "standard"
    assert parsed.turn_budget == 5
    assert parsed.enabled is True
    # toolset shape: { "kind": "explicit", "op_ids": [...] }. Free-shaped
    # in the schema (T2 stores it verbatim, T3 owns the contract), so we
    # only assert the example's claim about which op the agent calls.
    assert "vmware.composite.vm.snapshot.revert" in parsed.toolset.get("op_ids", [])


def test_permissions_fixture_parses_and_matches_resolver_assumptions() -> None:
    """``examples/r2-approval-gate/permissions.json`` is a well-formed grant set.

    Asserts each row's verdict is one of the closed :class:`PermissionVerdict`
    values and the op_pattern hierarchy matches the README's specificity
    discussion (the exact-id row wins over the broader glob).
    """
    payload = json.loads((_EXAMPLE_DIR / "permissions.json").read_text())
    rows = payload["permissions"]
    assert len(rows) == 2

    closed_verdicts = {v.value for v in PermissionVerdict}

    for row in rows:
        assert row["verdict"] in closed_verdicts, (
            f"permission row verdict {row['verdict']!r} is outside the closed enum "
            f"{closed_verdicts}; update either permissions.json or PermissionVerdict"
        )
        assert row["target_scope"] in ("*",) or len(row["target_scope"]) > 0
        assert row["op_pattern"]
        assert row["principal_sub"]

    # Specificity ordering: the exact-id row's pattern is longer than the
    # glob row's literal prefix (the README's claim).
    exact = next(r for r in rows if "*" not in r["op_pattern"])
    glob = next(r for r in rows if "*" in r["op_pattern"])
    glob_literal_prefix = glob["op_pattern"].split("*")[0]
    assert len(exact["op_pattern"]) > len(glob_literal_prefix), (
        "permissions.json's exact-op_id row must out-rank the broader glob "
        "(specificity = length of literal prefix before first wildcard); "
        "fix permissions.json or revise the README's tie-break paragraph"
    )


# ---------------------------------------------------------------------------
# End-to-end pause → approve → resume → execute
# ---------------------------------------------------------------------------


async def test_pause_approve_resume_execute_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The R2 example's full cycle — pause, approve, resume, execute.

    Mirrors the example's README §5 ("Verifying it works end-to-end"). The
    steps map 1:1 to the README's flow diagram:

    1. Register a ``requires_approval=True`` typed op (the agent's only tool).
    2. Insert an :class:`AgentPermission` row that mirrors
       ``permissions.json`` — granting the agent ``needs-approval`` on the
       op. This is the **permission setup** the README covers in §1.
    3. Agent dispatches the op via the real ``dispatch`` seam underneath the
       wrapped ``call_operation`` tool → asserts ``status="awaiting_approval"``
       with a fresh ``approval_request_id``. This is the **request flow** (§2).
    4. Operator flips the row to ``approved`` via ``approve_request`` — the
       service-layer function REST ``/decide``, MCP ``meho.approvals.approve``,
       and CLI ``meho approvals approve`` all call. This is the **response
       flow** (§3).
    5. The broadcast publishes ``approval.approved``; stub Valkey so the wait
       observes it.
    6. ``resume_or_surface_awaiting_approval`` re-dispatches with
       ``_approved=True`` and the original in-memory params — the op
       executes, the handler returns its deterministic payload. This is the
       **resume flow** (§4).
    7. Verify the audit-row attribution: the executed op's audit row carries
       the agent's ``sub`` as ``operator_sub``; the approval-decision audit
       row carries the human operator's ``sub`` as ``reviewed_by``. The
       full audit chain reflects both identities (per RFC 8693 §1.1 the
       MEHO substrate synthesises).
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

    # Silence the audit publish so the dispatcher's audit hook does not
    # try to reach a real Valkey. The wait stub patches the broadcast
    # client separately.
    captured_audit_events: list[Any] = []

    async def _capture_audit_event(event: Any) -> None:
        captured_audit_events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture_audit_event)

    reset_dispatcher_caches()
    clear_registry()

    # ---- Phase 0: seed the tenant row (FK target for AgentPermission) ----
    async with get_sessionmaker()() as s:
        s.add(Tenant(id=_TENANT_ID, slug="r2-example", name="R2 Example Tenant"))
        await s.commit()

    # ---- Phase 1: register the change-class op ----
    class _ExampleConnector(Connector):
        product = "examples"
        version = "1.x"
        impl_id = "examples"
        priority = 10

        async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
            return FingerprintResult(
                probe=ProbeResult(reachable=True, probe_method="none"),
                product="examples",
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
        product="examples",
        version="",
        impl_id="",
        cls=_ExampleConnector,
    )

    # The embedding service is invoked only by the descriptor's "when to
    # use" index (search_operations); the dispatcher's policy gate does
    # not consult it. Stubbed to a deterministic vector.
    stub_emb = AsyncMock()
    stub_emb.encode_one.return_value = [0.1] * 384
    stub_emb.encode.return_value = [[0.1] * 384]
    stub_emb.dimension = 384

    await register_typed_operation(
        product="examples",
        version="1.x",
        impl_id="examples",
        op_id=_OP_ID,
        handler=_r2_example_revert_handler,
        summary="Demo revert op for the R2 approval-gate example.",
        description=(
            "Stand-in for vmware.composite.vm.snapshot.revert. Carries "
            "requires_approval=True so the policy gate routes agent "
            "principals through the approval queue."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "vm_id": {"type": "string"},
                "snapshot_name": {"type": "string"},
            },
            "required": ["vm_id", "snapshot_name"],
        },
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_emb,
    )

    # ---- Phase 2: insert the AgentPermission grant ----
    # Mirrors examples/r2-approval-gate/permissions.json's first row. The
    # row alone is not strictly necessary (the op's requires_approval=True
    # floor would already produce needs-approval), but inserting it makes
    # the test reflect the README's §1 setup exactly.
    async with get_sessionmaker()() as s:
        s.add(
            AgentPermission(
                tenant_id=_TENANT_ID,
                principal_sub=_AGENT_SUB,
                op_pattern=_OP_ID,
                target_scope="*",
                verdict=PermissionVerdict.NEEDS_APPROVAL.value,
                created_by_sub=_OPERATOR_SUB,
            )
        )
        await s.commit()

    # ---- Phase 3: agent dispatches → pauses ----
    agent_operator = _make_agent_operator()
    call_arguments: dict[str, Any] = {
        "connector_id": "examples-1.x",
        "op_id": _OP_ID,
        "target": None,
        "params": {"vm_id": "vm-42", "snapshot_name": "pre-upgrade"},
    }

    first = await dispatch(
        operator=agent_operator,
        connector_id="examples-1.x",
        op_id=_OP_ID,
        target=None,
        params=call_arguments["params"],
    )
    assert first.status == "awaiting_approval", (
        f"expected awaiting_approval after agent dispatch on a "
        f"requires_approval=True op; got {first.status!r}: {first.error!r}"
    )
    approval_request_id = uuid.UUID(first.extras["approval_request_id"])

    # ---- Phase 4: operator approves ----
    human_operator = _make_human_operator()
    async with get_sessionmaker()() as s:
        row = await approve_request(
            s,
            approval_request_id,
            operator=human_operator,
            params=None,  # /decide path — no hash check
        )
        await s.commit()
    assert row.status == "approved"
    assert row.reviewed_by == _OPERATOR_SUB

    # ---- Phase 5: broadcast publishes approval.approved ----
    _stub_broadcast_client(
        monkeypatch=monkeypatch,
        decision_entry=_build_approval_decision_entry(
            approval_request_id=approval_request_id,
            decision="approved",
        ),
    )

    # ---- Phase 6: agent's resume helper observes + re-dispatches ----
    awaiting_envelope = first.model_dump(mode="json")
    resumed = await resume_or_surface_awaiting_approval(
        operator=agent_operator,
        call_arguments=call_arguments,
        awaiting_envelope=awaiting_envelope,
        timeout_seconds=2.0,
    )

    assert resumed["status"] == "ok", f"expected status=ok after resume; got {resumed!r}"
    assert resumed["result"]["executed"] is True
    assert resumed["result"]["vm_id"] == "vm-42"
    assert resumed["result"]["snapshot_name"] == "pre-upgrade"
    # The handler observed the AGENT principal as ``operator``, not the
    # human reviewer — the audit chain's subject is the agent on the
    # executed op; the reviewer's identity lives on the approval-decision
    # row instead (asserted below).
    assert resumed["result"]["operator_sub"] == _AGENT_SUB

    # ---- Phase 7: verify audit-row attribution ----
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        # The approve_request call wrote exactly one approval.decision row.
        decision_rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
        assert len(decision_rows) == 1, (
            f"expected exactly one approval.decision audit row; got {len(decision_rows)}"
        )
        decision_row = decision_rows[0]
        assert decision_row.operator_sub == _OPERATOR_SUB, (
            "approval.decision audit row's operator_sub must be the human "
            "reviewer's sub — the row is the durable record of WHO decided"
        )
        assert decision_row.payload["decision"] == "approved"
        assert decision_row.payload["reviewed_by"] == _OPERATOR_SUB

        # And exactly one approval.request row (from create_pending_request,
        # written synchronously alongside the pending ApprovalRequest row).
        request_rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.request")))
            .scalars()
            .all()
        )
        assert len(request_rows) == 1
        # The request audit row carries the AGENT's sub — the principal
        # that made the dispatch landing in the queue.
        assert request_rows[0].operator_sub == _AGENT_SUB

        # The ApprovalRequest row itself reflects both identities: principal
        # = agent, reviewed_by = human operator.
        approval_row = (await fresh.execute(select(ApprovalRequest))).scalar_one()
        assert approval_row.principal_sub == _AGENT_SUB
        assert approval_row.reviewed_by == _OPERATOR_SUB
        assert approval_row.status == "approved"

    reset_dispatcher_caches()
    clear_registry()
