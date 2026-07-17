# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the tiered-triage investigator wiring (#2507).

Initiative #2416 (parent goal #221), Task #2507. Coverage:

* **Transition detection** -- the ``last_rollup_state`` memo compare-and-set,
  the worsening-transition matrix, and equality suppression.
* **Correlation** -- topology forward-closure + union-find grouping (with
  ``find_dependencies`` stubbed, since the recursive CTE is PG-only and the
  correlation *logic* is what this Task owns), determinism, and the
  ``NodeNotFoundError`` singleton fallback.
* **One-cause-one-investigation** -- a correlated group fires exactly one
  ``run_scheduled``.
* **Suppression + in-flight dedupe** -- known-noise and already-running groups
  record zero invocations.
* **Closed loop** -- a terminal finding writes the noise-suppression policy;
  unparseable output skips the write.
* **Containment** -- budget refusal, an unconfigured agent, unresolved
  credentials, and an investigation crash are all caught.
* **Diagnose-only** -- the module imports no operations dispatcher.

The :class:`~meho_backplane.agent.invocation.AgentInvoker` is replaced by a
recording stub (``reset_agent_invoker_for_testing``) so no model is hit; the
memory + DB layers are real against the SQLite engine from :mod:`tests.conftest`.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

import meho_backplane.checks.investigate as inv
from meho_backplane.agent.invocation import (
    AgentRunOutcome,
    AgentRunStatusView,
    reset_agent_invoker_for_testing,
)
from meho_backplane.agent.run import BudgetExceededError
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.investigate import (
    ChecksFinding,
    investigate_on_transition,
    run_investigation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    CheckDashboard,
    CheckDashboardSensor,
    Sensor,
    Tenant,
)
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.scheduler.credentials import AgentCredentialsUnresolvedError
from meho_backplane.settings import get_settings
from meho_backplane.topology.query import TopologyNode
from meho_backplane.topology.resolvers import NodeNotFoundError

_TENANT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env :class:`Settings` requires so ``get_settings()`` constructs."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://kc.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho")
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SENSOR_RUNNER_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_investigator() -> Iterator[None]:
    """Clear the invoker singleton + the in-flight investigation set per test."""
    yield
    reset_agent_invoker_for_testing(None)
    inv._INVESTIGATIONS.clear()


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(tenant_id: UUID = _TENANT) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug="tenant-a", name="Tenant A"))
            await session.commit()


async def _seed_sensor(
    *,
    name: str,
    last_state: str = "critical",
    severity: str = "critical",
    for_seconds: int = 0,
    status: str = "active",
    tenant_id: UUID = _TENANT,
    target: dict[str, object] | None = None,
) -> UUID:
    """Insert an active Sensor whose projection folds to *last_state* on read."""
    now = datetime.now(UTC)
    sensor_id = uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=tenant_id,
                name=name,
                connector_id="vmware-rest-9.0",
                op_id="vmware.vm.list",
                target=target,
                params={},
                assertion=_ASSERTION,
                status=status,
                cadence_kind="interval",
                interval_seconds=60,
                cron_expr=None,
                timezone="UTC",
                next_fire_at=now + timedelta(seconds=60),
                severity=severity,
                for_seconds=for_seconds,
                last_state=last_state,
                last_value=None,
                last_evidence=None,
                last_evaluated_at=now - timedelta(seconds=30),
                state_since=now - timedelta(hours=1),
                identity_sub="__sensor__",
                created_by_sub="op-admin",
            )
        )
        await session.commit()
    return sensor_id


async def _seed_dashboard(
    *,
    name: str,
    sensor_ids: list[UUID],
    last_rollup_state: str | None = None,
    tenant_id: UUID = _TENANT,
) -> UUID:
    """Insert a Dashboard + memberships with an explicit memo state."""
    dashboard_id = uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=tenant_id,
                name=name,
                description=None,
                last_rollup_state=last_rollup_state,
                created_by_sub="op-admin",
            )
        )
        for sid in sensor_ids:
            session.add(CheckDashboardSensor(dashboard_id=dashboard_id, sensor_id=sid))
        await session.commit()
    return dashboard_id


async def _seed_definition(
    *,
    name: str = "checks-investigator",
    enabled: bool = True,
    tenant_id: UUID = _TENANT,
) -> None:
    await _seed_tenant(tenant_id)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AgentDefinition(
                tenant_id=tenant_id,
                name=name,
                identity_ref=f"agent:{name}",
                model_tier="standard",
                system_prompt="You investigate check dashboards.",
                toolset={},
                turn_budget=5,
                output_schema=None,
                enabled=enabled,
                created_by_sub="seed-admin",
            )
        )
        await session.commit()


async def _seed_agent_run(*, work_ref: str, status: str, tenant_id: UUID = _TENANT) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AgentRun(
                id=uuid4(),
                tenant_id=tenant_id,
                identity_sub="agent:checks-investigator",
                trigger="scheduled",
                model_tier="standard",
                status=status,
                work_ref=work_ref,
            )
        )
        await session.commit()


async def _memo(dashboard_id: UUID) -> str | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(CheckDashboard, dashboard_id)
        assert row is not None
        return row.last_rollup_state


async def _agent_run_count(tenant_id: UUID = _TENANT) -> int:
    from sqlalchemy import func, select

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(func.count()).select_from(AgentRun).where(AgentRun.tenant_id == tenant_id)
        )
        return int(result.scalar_one())


def _admin_operator(tenant_id: UUID = _TENANT) -> Operator:
    return Operator(
        sub="op-admin",
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubInvoker:
    """Records ``run_scheduled`` calls; returns a preset outcome or raises."""

    def __init__(
        self,
        *,
        outcome: AgentRunOutcome | None = None,
        exc: Exception | None = None,
        poll_views: list[Any] | None = None,
    ) -> None:
        self.calls: list[SimpleNamespace] = []
        self._outcome = outcome
        self._exc = exc
        self._poll_views = list(poll_views or [])
        self.poll_calls = 0

    async def run_scheduled(
        self,
        name: str,
        inputs: str,
        *,
        agent_client_id: str,
        agent_client_secret: Any,
        work_ref: str | None = None,
    ) -> AgentRunOutcome:
        self.calls.append(
            SimpleNamespace(
                name=name,
                inputs=inputs,
                work_ref=work_ref,
                agent_client_id=agent_client_id,
            )
        )
        if self._exc is not None:
            raise self._exc
        assert self._outcome is not None
        return self._outcome

    async def poll(self, operator: Operator, run_id: UUID) -> Any:
        if not self._poll_views:
            raise AssertionError("poll must not be called when the run is terminal")
        self.poll_calls += 1
        # Return each staged view once, then repeat the last (terminal) one.
        index = min(self.poll_calls - 1, len(self._poll_views) - 1)
        return self._poll_views[index]


def _install_stub(**kwargs: Any) -> _StubInvoker:
    stub = _StubInvoker(**kwargs)
    reset_agent_invoker_for_testing(stub)  # type: ignore[arg-type]
    return stub


def _succeeded_outcome(finding: ChecksFinding, *, run_id: UUID | None = None) -> AgentRunOutcome:
    return AgentRunOutcome(
        run_id=run_id or uuid4(),
        status=AgentRunStatus.SUCCEEDED,
        output={"text": finding.model_dump_json()},
        converted_to_async=False,
    )


def _node(kind: str, name: str, depth: int) -> TopologyNode:
    return TopologyNode(
        id=uuid4(),
        kind=kind,
        name=name,
        source="auto",
        properties={},
        depth=depth,
        via_edge_kind=None,
    )


def _member(
    name: str,
    *,
    target: dict[str, object] | None = None,
    effective: str = "critical",
    raw: str = "critical",
) -> inv._MemberSnapshot:
    return inv._MemberSnapshot(
        sensor_id=uuid4(),
        name=name,
        connector_id="vmware-rest-9.0",
        op_id="vmware.vm.list",
        target=target,
        severity="critical",
        raw_state=raw,
        effective_state=effective,
        last_value=None,
        last_evidence=None,
    )


def _dash(*, name: str = "prod", prev: str = "ok", cur: str = "critical") -> inv._DashboardSnapshot:
    return inv._DashboardSnapshot(
        dashboard_id=uuid4(),
        name=name,
        slug=inv._slugify(name),
        previous_state=prev,
        current_state=cur,
    )


def _patch_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(identity_ref: str) -> tuple[str, str]:
        return identity_ref, "secret"

    monkeypatch.setattr(inv, "resolve_agent_credentials", _fake)


# ---------------------------------------------------------------------------
# Transition detection + memo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memo_maintained_and_equality_suppresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memo tracks the computed state; a second identical evaluation is a no-op."""
    fired: list[dict[str, Any]] = []
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **kw: fired.append(kw))
    await _seed_tenant()
    sid = await _seed_sensor(name="crit", last_state="critical")
    dash = await _seed_dashboard(name="prod", sensor_ids=[sid], last_rollup_state=None)

    await investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
    assert await _memo(dash) == "critical"
    assert len(fired) == 1

    fired.clear()
    await investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
    assert await _memo(dash) == "critical"
    assert fired == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous", "last_state", "expected_current", "should_fire"),
    [
        pytest.param(None, "critical", "critical", True, id="null-to-critical-fires"),
        pytest.param("ok", "degraded", "degraded", True, id="ok-to-degraded-fires"),
        pytest.param("degraded", "critical", "critical", True, id="degraded-to-critical-fires"),
        pytest.param("critical", "degraded", "degraded", False, id="critical-to-degraded-no-fire"),
        pytest.param("ok", "ok", "ok", False, id="ok-to-ok-no-fire"),
    ],
)
async def test_transition_matrix(
    monkeypatch: pytest.MonkeyPatch,
    previous: str | None,
    last_state: str,
    expected_current: str,
    should_fire: bool,
) -> None:
    """The worsening-transition matrix fires only on a green->worse-non-green edge."""
    fired: list[dict[str, Any]] = []
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **kw: fired.append(kw))
    await _seed_tenant()
    sid = await _seed_sensor(name="s", last_state=last_state)
    dash = await _seed_dashboard(name="d", sensor_ids=[sid], last_rollup_state=previous)

    await investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)

    assert await _memo(dash) == expected_current
    assert (len(fired) == 1) is should_fire


@pytest.mark.asyncio
async def test_no_dashboard_is_noop() -> None:
    """A Sensor in no Dashboard produces no work (the runner-tick common case)."""
    await _seed_tenant()
    sid = await _seed_sensor(name="lonely", last_state="critical")
    stub = _install_stub()
    await investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
    await inv._await_pending_investigations()
    assert stub.calls == []


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correlation_two_groups_and_determinism(monkeypatch: pytest.MonkeyPatch) -> None:
    """A + B share a dependency node, C does not -> two groups; keys deterministic."""
    a = _member("a", target={"name": "anchor-a"})
    b = _member("b", target={"name": "anchor-b"})
    c = _member("c", target={"name": "anchor-c"})

    closures = {
        "anchor-a": [_node("target", "anchor-a", 0), _node("datastore", "shared", 1)],
        "anchor-b": [_node("target", "anchor-b", 0), _node("datastore", "shared", 1)],
        "anchor-c": [_node("target", "anchor-c", 0), _node("datastore", "other", 1)],
    }

    async def _fake_deps(operator: Operator, name: str, *, kind: str | None = None, **_: Any):
        return closures[name]

    monkeypatch.setattr(inv, "find_dependencies", _fake_deps)
    operator = _admin_operator()

    groups = await inv._correlate(operator, [a, b, c])
    assert len(groups) == 2
    grouped_names = {frozenset(m.name for m in g.members) for g in groups}
    assert grouped_names == {frozenset({"a", "b"}), frozenset({"c"})}

    keys_first = [g.key for g in groups]
    keys_second = [g.key for g in await inv._correlate(operator, [a, b, c])]
    assert keys_first == keys_second
    # The A+B group is keyed on the deepest shared node.
    ab_group = next(g for g in groups if {m.name for m in g.members} == {"a", "b"})
    assert ab_group.key == "datastore-shared"


@pytest.mark.asyncio
async def test_correlation_untracked_anchor_is_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """A member whose anchor raises NodeNotFoundError is keyed on its own slug."""
    m = _member("lonely-vm", target={"name": "untracked"})

    async def _fake_deps(operator: Operator, name: str, *, kind: str | None = None, **_: Any):
        raise NodeNotFoundError(name)

    monkeypatch.setattr(inv, "find_dependencies", _fake_deps)

    groups = await inv._correlate(_admin_operator(), [m])
    assert len(groups) == 1
    assert groups[0].key == inv._sensor_group_key(m)


# ---------------------------------------------------------------------------
# Invocation path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_cause_one_investigation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three correlated non-green Sensors fire exactly one run with a group work_ref."""
    await _seed_definition()
    _patch_creds(monkeypatch)
    stub = _install_stub(
        outcome=_succeeded_outcome(
            ChecksFinding(verdict="acknowledged", re_escalate=False, summary="ok")
        )
    )

    members = [
        _member("a", target={"name": "anchor-a"}),
        _member("b", target={"name": "anchor-b"}),
        _member("c", target={"name": "anchor-c"}),
    ]
    shared = _node("datastore", "shared", 1)

    async def _fake_deps(operator: Operator, name: str, *, kind: str | None = None, **_: Any):
        return [_node("target", name, 0), shared]

    monkeypatch.setattr(inv, "find_dependencies", _fake_deps)

    dashboard = _dash(name="prod")
    await run_investigation(tenant_id=_TENANT, dashboard=dashboard, members=members)

    assert len(stub.calls) == 1
    assert stub.calls[0].work_ref == "checks:prod:datastore-shared"


@pytest.mark.asyncio
async def test_in_flight_dedupe_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing RUNNING run for the same (tenant, work_ref) suppresses a re-fire."""
    await _seed_definition()
    _patch_creds(monkeypatch)
    stub = _install_stub()
    member = _member("solo", target=None)
    group_key = inv._sensor_group_key(member)
    await _seed_agent_run(work_ref=f"checks:prod:{group_key}", status=AgentRunStatus.RUNNING.value)

    with capture_logs() as logs:
        await run_investigation(tenant_id=_TENANT, dashboard=_dash(name="prod"), members=[member])

    assert stub.calls == []
    assert any(e["event"] == "checks_investigation_skipped_in_flight" for e in logs)


@pytest.mark.asyncio
async def test_suppression_reescalate_false_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """A known-noise memory entry (re_escalate=false) suppresses the fire."""
    await _seed_definition()
    _patch_creds(monkeypatch)
    stub = _install_stub()
    member = _member("solo", target=None)
    group_key = inv._sensor_group_key(member)
    await MemoryService().remember(
        _admin_operator(),
        MemoryScope.TENANT,
        "verdict: benign\n",
        slug=f"checks-noise-{group_key}",
        metadata={"source": "checks-investigator", "re_escalate": False},
    )

    with capture_logs() as logs:
        await run_investigation(tenant_id=_TENANT, dashboard=_dash(name="prod"), members=[member])

    assert stub.calls == []
    assert any(e["event"] == "checks_investigation_suppressed" for e in logs)


@pytest.mark.asyncio
async def test_suppression_reescalate_true_invokes(monkeypatch: pytest.MonkeyPatch) -> None:
    """An re_escalate=true entry does not suppress -- the investigation fires."""
    await _seed_definition()
    _patch_creds(monkeypatch)
    stub = _install_stub(
        outcome=_succeeded_outcome(
            ChecksFinding(verdict="actionable", re_escalate=True, summary="still bad")
        )
    )
    member = _member("solo", target=None)
    group_key = inv._sensor_group_key(member)
    await MemoryService().remember(
        _admin_operator(),
        MemoryScope.TENANT,
        "verdict: actionable\n",
        slug=f"checks-noise-{group_key}",
        metadata={"source": "checks-investigator", "re_escalate": True},
    )

    await run_investigation(tenant_id=_TENANT, dashboard=_dash(name="prod"), members=[member])
    assert len(stub.calls) == 1


# ---------------------------------------------------------------------------
# Closed loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closed_loop_writes_policy_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal ChecksFinding writes a noise-suppression policy to tenant memory."""
    await _seed_definition()
    _patch_creds(monkeypatch)
    run_id = uuid4()
    finding = ChecksFinding(
        verdict="acknowledged",
        re_escalate=False,
        summary="Expected nightly maintenance window.",
        evidence=["window 02:00-03:00 UTC"],
    )
    _install_stub(outcome=_succeeded_outcome(finding, run_id=run_id))
    member = _member("solo", target=None)
    group_key = inv._sensor_group_key(member)

    await run_investigation(tenant_id=_TENANT, dashboard=_dash(name="prod"), members=[member])

    entries = await MemoryService().list_memories(
        _admin_operator(),
        scope=MemoryScope.TENANT,
        slug_pattern=f"checks-noise-{group_key}",
        limit=10,
    )
    entry = next(e for e in entries if e.slug == f"checks-noise-{group_key}")
    assert entry.scope == MemoryScope.TENANT
    assert entry.metadata["source"] == "checks-investigator"
    assert entry.metadata["verdict"] == "acknowledged"
    assert entry.metadata["re_escalate"] is False
    assert entry.metadata["run_id"] == str(run_id)
    assert "verdict: acknowledged" in entry.body
    assert "## Summary" in entry.body


@pytest.mark.asyncio
async def test_converted_to_async_polls_then_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that outlived the sync timeout is polled to terminal, then persisted."""
    await _seed_definition()
    _patch_creds(monkeypatch)
    monkeypatch.setattr(inv, "_POLL_INTERVAL_SECONDS", 0.001)
    run_id = uuid4()
    finding = ChecksFinding(verdict="benign", re_escalate=False, summary="transient blip")

    def _view(status: AgentRunStatus, output: dict[str, object] | None) -> AgentRunStatusView:
        return AgentRunStatusView(
            run_id=run_id,
            status=status,
            turns=1,
            provider="anthropic",
            model="claude",
            output=output,
            error=None,
        )

    stub = _StubInvoker(
        outcome=AgentRunOutcome(
            run_id=run_id, status=AgentRunStatus.RUNNING, output=None, converted_to_async=True
        ),
        poll_views=[
            _view(AgentRunStatus.RUNNING, None),
            _view(AgentRunStatus.SUCCEEDED, {"text": finding.model_dump_json()}),
        ],
    )
    reset_agent_invoker_for_testing(stub)  # type: ignore[arg-type]
    member = _member("solo", target=None)
    group_key = inv._sensor_group_key(member)

    await run_investigation(tenant_id=_TENANT, dashboard=_dash(name="prod"), members=[member])

    assert stub.poll_calls >= 2
    entries = await MemoryService().list_memories(
        _admin_operator(),
        scope=MemoryScope.TENANT,
        slug_pattern=f"checks-noise-{group_key}",
        limit=10,
    )
    assert any(e.slug == f"checks-noise-{group_key}" for e in entries)


@pytest.mark.asyncio
async def test_unparseable_output_skips_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-JSON terminal answer writes no memory and logs unparseable."""
    await _seed_definition()
    _patch_creds(monkeypatch)
    _install_stub(
        outcome=AgentRunOutcome(
            run_id=uuid4(),
            status=AgentRunStatus.SUCCEEDED,
            output={"text": "not json at all"},
            converted_to_async=False,
        )
    )
    member = _member("solo", target=None)
    group_key = inv._sensor_group_key(member)

    with capture_logs() as logs:
        await run_investigation(tenant_id=_TENANT, dashboard=_dash(name="prod"), members=[member])

    assert any(e["event"] == "checks_investigation_unparseable" for e in logs)
    entries = await MemoryService().list_memories(
        _admin_operator(),
        scope=MemoryScope.TENANT,
        slug_pattern=f"checks-noise-{group_key}",
        limit=10,
    )
    assert [e for e in entries if e.slug == f"checks-noise-{group_key}"] == []


# ---------------------------------------------------------------------------
# Containment: budget, unconfigured, credentials, crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_refused_leaves_no_run_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A budget/kill-switch refusal is caught; no agent_run row is created."""
    await _seed_definition()
    _patch_creds(monkeypatch)
    _install_stub(exc=BudgetExceededError("global kill switch enabled"))

    with capture_logs() as logs:
        await run_investigation(
            tenant_id=_TENANT, dashboard=_dash(name="prod"), members=[_member("solo")]
        )

    assert any(e["event"] == "checks_investigation_budget_refused" for e in logs)
    assert await _agent_run_count() == 0


@pytest.mark.asyncio
async def test_unconfigured_agent_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    """No enabled investigator definition -> logged + skipped, no invocation, no raise."""
    await _seed_tenant()
    _patch_creds(monkeypatch)
    stub = _install_stub()

    with capture_logs() as logs:
        await run_investigation(
            tenant_id=_TENANT, dashboard=_dash(name="prod"), members=[_member("solo")]
        )

    assert stub.calls == []
    assert any(e["event"] == "checks_investigator_unconfigured" for e in logs)


@pytest.mark.asyncio
async def test_disabled_agent_is_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disabled definition is treated as unconfigured (the enabled flag is the switch)."""
    await _seed_definition(enabled=False)
    _patch_creds(monkeypatch)
    stub = _install_stub()

    with capture_logs() as logs:
        await run_investigation(
            tenant_id=_TENANT, dashboard=_dash(name="prod"), members=[_member("solo")]
        )

    assert stub.calls == []
    assert any(e["event"] == "checks_investigator_unconfigured" for e in logs)


@pytest.mark.asyncio
async def test_credentials_unresolved_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unresolved credentials are caught before any invocation."""
    await _seed_definition()
    stub = _install_stub()

    async def _raise(identity_ref: str) -> tuple[str, str]:
        raise AgentCredentialsUnresolvedError("no secret")

    monkeypatch.setattr(inv, "resolve_agent_credentials", _raise)

    with capture_logs() as logs:
        await run_investigation(
            tenant_id=_TENANT, dashboard=_dash(name="prod"), members=[_member("solo")]
        )

    assert stub.calls == []
    assert any(e["event"] == "checks_investigation_credentials_unresolved" for e in logs)


@pytest.mark.asyncio
async def test_investigation_crash_is_contained_and_memo_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash in the backgrounded investigation never propagates; the memo persists."""
    await _seed_definition()
    _patch_creds(monkeypatch)
    _install_stub(exc=RuntimeError("boom"))
    sid = await _seed_sensor(name="crit", last_state="critical")
    dash = await _seed_dashboard(name="prod", sensor_ids=[sid], last_rollup_state="ok")

    with capture_logs() as logs:
        # Never raises (the runner's persist path contract) ...
        await investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
        await inv._await_pending_investigations()

    # ... the in-band memo write still committed ...
    assert await _memo(dash) == "critical"
    # ... and the background crash was contained + logged.
    assert any(e["event"] == "checks_investigation_failed" for e in logs)


# ---------------------------------------------------------------------------
# Diagnose-only + settings guards
# ---------------------------------------------------------------------------


def test_module_never_imports_operations_dispatcher() -> None:
    """The CRUX security property: this wiring imports no execution seam."""
    source = inspect.getsource(inv)
    assert "operations.dispatcher" not in source
    assert "import dispatch" not in source


def test_settings_fields_present() -> None:
    """The two new settings fields carry the documented defaults."""
    settings = get_settings()
    assert settings.checks_investigator_agent == "checks-investigator"
    assert settings.checks_investigation_poll_timeout_seconds == 600
