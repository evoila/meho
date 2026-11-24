# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The finding step for runs that a Dashboard event trigger started (#6183).

With ``CHECKS_AGENT_EVENTS`` on, the event drain stores and mails the finding
of a succeeded run whose run key has the Dashboard form. Every other run
writes nothing. Runs on the SQLite engine from :mod:`tests.conftest`.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

import meho_backplane.checks.findings as findings_mod
import meho_backplane.checks.investigate as inv
import meho_backplane.checks.notify as notify
from meho_backplane.agent.invocation import reset_agent_invoker_for_testing
from meho_backplane.checks.events import publish_dashboard_changed
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentDefinition,
    AgentRun,
    CheckDashboard,
    EventOutbox,
    ScheduledTrigger,
    Tenant,
)
from meho_backplane.events import drain
from meho_backplane.events.drain import run_one_drain_tick
from meho_backplane.events.matcher import _coalesce_work_ref, _payload_contains
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.operations.agent_run import AGENT_RUN_COMPLETED_EVENT_KIND
from meho_backplane.settings import get_settings

_TENANT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_OTHER_TENANT = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_GROUP = "target-vc-01-1a2b3c4d"

#: The filter of the example trigger in ``examples/checks-investigator``.
_EXAMPLE_FILTER: dict[str, object] = {
    "event": "check.dashboard.changed",
    "worsening": True,
    "known_noise": False,
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    monkeypatch.setenv("MAIL_RECIPIENT_ALLOWLIST", "example.test")
    monkeypatch.setenv("CHECKS_AGENT_EVENTS", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    reset_agent_invoker_for_testing(None)
    notify._NOTIFICATIONS.clear()


@pytest.fixture
def mails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def _fake_send(*, to: list[str], subject: str, body: str) -> Any:
        sent.append({"to": to, "subject": subject, "body": body})
        return SimpleNamespace(sent=True, reason=None)

    monkeypatch.setattr(notify, "send_email", _fake_send)
    return sent


_FINDING = findings_mod.ChecksFinding(
    verdict="benign",
    re_escalate=False,
    summary="Known maintenance window on vc-01.",
    evidence=["maintenance mode"],
)


async def _seed_tenant(tenant_id: UUID) -> None:
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:6]}", name="Tenant"))
            await session.commit()


async def _seed_dashboard(
    *, tenant_id: UUID = _TENANT, notify_email: str | None = "ops@example.test"
) -> UUID:
    dashboard_id = uuid4()
    await _seed_tenant(tenant_id)
    async with get_sessionmaker()() as session:
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=tenant_id,
                name="prod",
                description=None,
                last_rollup_state=None,
                notify_email=notify_email,
                created_by_sub="op-admin",
            )
        )
        await session.commit()
    return dashboard_id


async def _seed_definition() -> UUID:
    await _seed_tenant(_TENANT)
    name = f"investigator-{uuid4().hex[:8]}"
    definition = AgentDefinition(
        tenant_id=_TENANT,
        name=name,
        identity_ref=f"agent:{name}",
        model_tier="standard",
        system_prompt="You investigate check dashboards.",
        toolset={},
        turn_budget=5,
        output_schema=None,
        enabled=True,
        created_by_sub="seed-admin",
    )
    async with get_sessionmaker()() as session:
        session.add(definition)
        await session.commit()
    return definition.id


async def _seed_trigger(
    definition_id: UUID, *, kind: str = "event", work_ref: str | None = None
) -> ScheduledTrigger:
    trigger = ScheduledTrigger(
        id=uuid4(),
        tenant_id=_TENANT,
        agent_definition_id=definition_id,
        kind=kind,
        cron_expr="*/5 * * * *" if kind == "cron" else None,
        timezone="UTC",
        fire_at=None,
        event_filter=dict(_EXAMPLE_FILTER) if kind == "event" else None,
        next_fire_at=datetime.now(UTC) + timedelta(hours=1) if kind == "cron" else None,
        status="active",
        in_flight_policy="fail_into_audit",
        inputs="Look at the Dashboard." if kind == "cron" else None,
        identity_sub="__scheduler__",
        created_by_sub="seed-admin",
        work_ref=work_ref,
    )
    async with get_sessionmaker()() as session:
        session.add(trigger)
        await session.commit()
    return trigger


async def _publish_group_event(
    dashboard_id: UUID, *, group: str = _GROUP, known_noise: bool = False
) -> EventOutbox:
    await publish_dashboard_changed(
        tenant_id=_TENANT,
        dashboard_id=dashboard_id,
        dashboard_name="prod",
        previous_state="ok",
        state="critical",
        worsening=True,
        group_key=group,
        known_noise=known_noise,
    )
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(EventOutbox.tenant_id == _TENANT)
            .order_by(EventOutbox.event_id.desc())
            .limit(1)
        )
        event = rows.scalar_one()
        # The drain already handled this event: it started the run.
        event.processed_at = datetime.now(UTC)
        await session.commit()
        return event


async def _dashboard_origin(
    dashboard_id: UUID, *, group: str = _GROUP, known_noise: bool = False
) -> tuple[str, UUID]:
    """Seed an event trigger and a Dashboard event; return ``(run key, agent id)``.

    The run key is made by the event matcher itself, as for a real run.
    """
    definition_id = await _seed_definition()
    trigger = await _seed_trigger(definition_id)
    event = await _publish_group_event(dashboard_id, group=group, known_noise=known_noise)
    work_ref = _coalesce_work_ref(trigger, event)
    assert work_ref is not None
    return work_ref, definition_id


async def _seed_run(
    *,
    work_ref: str | None,
    output: dict[str, object] | None,
    status: str = "succeeded",
    tenant_id: UUID = _TENANT,
    definition_id: UUID | None = None,
    trigger: str = "scheduled",
) -> UUID:
    run_id = uuid4()
    await _seed_tenant(tenant_id)
    async with get_sessionmaker()() as session:
        session.add(
            AgentRun(
                id=run_id,
                tenant_id=tenant_id,
                agent_definition_id=definition_id,
                identity_sub="agent:my-investigator",
                trigger=trigger,
                model_tier="standard",
                status=status,
                work_ref=work_ref,
                output=output,
            )
        )
        await session.commit()
    return run_id


def _payload(run_id: UUID, work_ref: str | None, *, status: str = "succeeded") -> dict[str, object]:
    return {
        "run_id": str(run_id),
        "tenant_id": str(_TENANT),
        "status": status,
        "agent_definition_id": str(uuid4()),
        "work_ref": work_ref,
    }


async def _noise_entry(group: str = _GROUP) -> Any:
    operator = inv.investigator_operator(_TENANT)
    return await MemoryService().recall(operator, MemoryScope.TENANT, f"checks-noise-{group}")


async def _finding_run(
    *,
    finding: findings_mod.ChecksFinding = _FINDING,
    dashboard_id: UUID,
    group: str = _GROUP,
) -> tuple[UUID, str]:
    work_ref, definition_id = await _dashboard_origin(dashboard_id, group=group)
    run_id = await _seed_run(
        work_ref=work_ref,
        output={"text": finding.model_dump_json()},
        definition_id=definition_id,
    )
    return run_id, work_ref


# ---------------------------------------------------------------------------
# The finding step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finding_is_stored_and_mailed_once(mails: list[dict[str, Any]]) -> None:
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id)

    assert await findings_mod.record_finding_for_event(_payload(run_id, work_ref)) is True
    await notify._await_pending_notifications()

    entry = await _noise_entry()
    assert entry is not None
    assert entry.metadata["re_escalate"] is False
    assert entry.metadata["run_id"] == str(run_id)
    assert len(mails) == 1
    assert mails[0]["to"] == ["ops@example.test"]
    assert "Known maintenance window" in mails[0]["body"]


@pytest.mark.asyncio
async def test_same_run_twice_stores_and_mails_once(mails: list[dict[str, Any]]) -> None:
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id)

    assert await findings_mod.record_finding_for_event(_payload(run_id, work_ref)) is True
    assert await findings_mod.record_finding_for_event(_payload(run_id, work_ref)) is False
    await notify._await_pending_notifications()

    assert len(mails) == 1


@pytest.mark.asyncio
async def test_known_noise_is_published_and_example_trigger_does_not_match() -> None:
    """After the stored finding, the next change for the group is known noise."""
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id)
    assert await findings_mod.record_finding_for_event(_payload(run_id, work_ref)) is True

    suppressed = await inv._is_suppressed(
        MemoryService(), inv.investigator_operator(_TENANT), _GROUP
    )
    assert suppressed is True
    await publish_dashboard_changed(
        tenant_id=_TENANT,
        dashboard_id=dashboard_id,
        dashboard_name="prod",
        previous_state="ok",
        state="critical",
        worsening=True,
        group_key=_GROUP,
        known_noise=suppressed,
    )
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(EventOutbox.tenant_id == _TENANT)
            .order_by(EventOutbox.event_id.desc())
            .limit(1)
        )
    payload = rows.scalar_one().payload
    assert payload["known_noise"] is True
    assert not _payload_contains(payload, _EXAMPLE_FILTER)


@pytest.mark.asyncio
async def test_re_escalate_true_keeps_the_next_change_open() -> None:
    dashboard_id = await _seed_dashboard()
    finding = findings_mod.ChecksFinding(
        verdict="actionable", re_escalate=True, summary="Disk is full."
    )
    run_id, work_ref = await _finding_run(finding=finding, dashboard_id=dashboard_id)
    assert await findings_mod.record_finding_for_event(_payload(run_id, work_ref)) is True

    suppressed = await inv._is_suppressed(
        MemoryService(), inv.investigator_operator(_TENANT), _GROUP
    )
    assert suppressed is False


# ---------------------------------------------------------------------------
# Cases that write nothing
# ---------------------------------------------------------------------------


async def _assert_nothing_written(
    run_id: UUID, work_ref: str | None, mails: list[dict[str, Any]], *, status: str = "succeeded"
) -> None:
    payload = _payload(run_id, work_ref, status=status)
    assert await findings_mod.record_finding_for_event(payload) is False
    await notify._await_pending_notifications()
    assert await _noise_entry() is None
    assert mails == []


@pytest.mark.asyncio
async def test_non_finding_answer_writes_nothing(mails: list[dict[str, Any]]) -> None:
    dashboard_id = await _seed_dashboard()
    work_ref, definition_id = await _dashboard_origin(dashboard_id)
    run_id = await _seed_run(
        work_ref=work_ref, output={"text": "All looks fine to me."}, definition_id=definition_id
    )
    await _assert_nothing_written(run_id, work_ref, mails)


@pytest.mark.asyncio
async def test_run_without_output_writes_nothing(mails: list[dict[str, Any]]) -> None:
    dashboard_id = await _seed_dashboard()
    work_ref, definition_id = await _dashboard_origin(dashboard_id)
    run_id = await _seed_run(work_ref=work_ref, output=None, definition_id=definition_id)
    await _assert_nothing_written(run_id, work_ref, mails)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_run_that_did_not_succeed_writes_nothing(
    status: str, mails: list[dict[str, Any]]
) -> None:
    dashboard_id = await _seed_dashboard()
    work_ref, definition_id = await _dashboard_origin(dashboard_id)
    run_id = await _seed_run(
        work_ref=work_ref,
        output={"text": _FINDING.model_dump_json()},
        status=status,
        definition_id=definition_id,
    )
    await _assert_nothing_written(run_id, work_ref, mails, status=status)


# Fixed values: parametrize ids must be the same in every xdist worker.
_FIXED_EVENT = "11111111-1111-4111-8111-111111111111"
_FIXED_RUN = "22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work_ref",
    [
        None,
        "checks:abcd1234:prod:target-vc-01",
        f"event:{_FIXED_EVENT}:{_FIXED_RUN}",
        f"event:{_FIXED_EVENT}:c:sensor:{_FIXED_RUN}",
        f"event:{_FIXED_EVENT}:c:dashboard:not-a-uuid:{_GROUP}",
        f"event:{_FIXED_EVENT}:c:dashboard:{_FIXED_RUN}",
    ],
)
async def test_run_key_of_another_form_writes_nothing(
    work_ref: str | None, mails: list[dict[str, Any]]
) -> None:
    run_id = await _seed_run(work_ref=work_ref, output={"text": _FINDING.model_dump_json()})
    await _assert_nothing_written(run_id, work_ref, mails)


@pytest.mark.asyncio
async def test_dashboard_of_another_tenant_writes_nothing(mails: list[dict[str, Any]]) -> None:
    other_dashboard = await _seed_dashboard(tenant_id=_OTHER_TENANT)
    run_id, work_ref = await _finding_run(dashboard_id=other_dashboard)
    await _assert_nothing_written(run_id, work_ref, mails)


@pytest.mark.asyncio
async def test_missing_dashboard_writes_nothing(mails: list[dict[str, Any]]) -> None:
    run_id, work_ref = await _finding_run(dashboard_id=uuid4())
    await _assert_nothing_written(run_id, work_ref, mails)


@pytest.mark.asyncio
async def test_unknown_run_writes_nothing(mails: list[dict[str, Any]]) -> None:
    dashboard_id = await _seed_dashboard()
    work_ref, _ = await _dashboard_origin(dashboard_id)
    await _assert_nothing_written(uuid4(), work_ref, mails)


# ---------------------------------------------------------------------------
# Only a run that a Dashboard event trigger started counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["direct", "agent-invoked"])
async def test_run_not_started_by_a_trigger_writes_nothing(
    source: str, mails: list[dict[str, Any]]
) -> None:
    """A caller can give any run key to a run it starts itself."""
    dashboard_id = await _seed_dashboard()
    work_ref, definition_id = await _dashboard_origin(dashboard_id)
    run_id = await _seed_run(
        work_ref=work_ref,
        output={"text": _FINDING.model_dump_json()},
        definition_id=definition_id,
        trigger=source,
    )
    await _assert_nothing_written(run_id, work_ref, mails)


@pytest.mark.asyncio
async def test_run_of_another_agent_writes_nothing(mails: list[dict[str, Any]]) -> None:
    dashboard_id = await _seed_dashboard()
    work_ref, _ = await _dashboard_origin(dashboard_id)
    other_definition = await _seed_definition()
    run_id = await _seed_run(
        work_ref=work_ref,
        output={"text": _FINDING.model_dump_json()},
        definition_id=other_definition,
    )
    await _assert_nothing_written(run_id, work_ref, mails)


@pytest.mark.asyncio
async def test_cron_trigger_that_copies_the_run_key_writes_nothing(
    mails: list[dict[str, Any]],
) -> None:
    dashboard_id = await _seed_dashboard()
    work_ref, definition_id = await _dashboard_origin(dashboard_id)
    await _seed_trigger(definition_id, kind="cron", work_ref=work_ref)
    run_id = await _seed_run(
        work_ref=work_ref,
        output={"text": _FINDING.model_dump_json()},
        definition_id=definition_id,
    )
    await _assert_nothing_written(run_id, work_ref, mails)


@pytest.mark.asyncio
async def test_run_key_naming_a_cron_trigger_writes_nothing(mails: list[dict[str, Any]]) -> None:
    dashboard_id = await _seed_dashboard()
    definition_id = await _seed_definition()
    cron = await _seed_trigger(definition_id, kind="cron")
    event = await _publish_group_event(dashboard_id)
    work_ref = _coalesce_work_ref(cron, event)
    run_id = await _seed_run(
        work_ref=work_ref,
        output={"text": _FINDING.model_dump_json()},
        definition_id=definition_id,
    )
    await _assert_nothing_written(run_id, work_ref, mails)


@pytest.mark.asyncio
async def test_run_key_without_a_dashboard_event_writes_nothing(
    mails: list[dict[str, Any]],
) -> None:
    dashboard_id = await _seed_dashboard()
    definition_id = await _seed_definition()
    trigger = await _seed_trigger(definition_id)
    work_ref = f"event:{trigger.id}:c:dashboard:{dashboard_id}:{_GROUP}"
    run_id = await _seed_run(
        work_ref=work_ref,
        output={"text": _FINDING.model_dump_json()},
        definition_id=definition_id,
    )
    await _assert_nothing_written(run_id, work_ref, mails)


@pytest.mark.asyncio
async def test_event_that_the_trigger_filter_rejects_writes_nothing(
    mails: list[dict[str, Any]],
) -> None:
    dashboard_id = await _seed_dashboard()
    work_ref, definition_id = await _dashboard_origin(dashboard_id, known_noise=True)
    run_id = await _seed_run(
        work_ref=work_ref,
        output={"text": _FINDING.model_dump_json()},
        definition_id=definition_id,
    )
    await _assert_nothing_written(run_id, work_ref, mails)


@pytest.mark.asyncio
async def test_long_group_key_with_a_hashed_run_key_is_stored(
    mails: list[dict[str, Any]],
) -> None:
    """The matcher hashes a key over 200 characters; the event still names the group."""
    long_group = "target-" + "a" * 160 + "-1a2b3c4d"
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id, group=long_group)
    assert long_group not in work_ref

    assert await findings_mod.record_finding_for_event(_payload(run_id, work_ref)) is True
    await notify._await_pending_notifications()

    entry = await _noise_entry(long_group)
    assert entry is not None
    assert entry.metadata["run_id"] == str(run_id)
    assert len(mails) == 1


async def _publish_unrelated_events(count: int) -> None:
    """Add *count* newer Dashboard events of other groups, all handled already."""
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        session.add_all(
            EventOutbox(
                tenant_id=_TENANT,
                event_kind="check.dashboard.changed",
                payload={
                    "event": "check.dashboard.changed",
                    "coalesce_key": f"dashboard:{uuid4()}:other-{index}",
                    "worsening": True,
                    "known_noise": False,
                },
                processed_at=now,
            )
            for index in range(count)
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [_GROUP, "target-" + "b" * 160 + "-1a2b3c4d"])
async def test_many_newer_events_do_not_hide_the_cause(
    group: str, mails: list[dict[str, Any]]
) -> None:
    """Other Dashboard events that arrive before the run ends never hide its cause."""
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id, group=group)
    await _publish_unrelated_events(600)

    assert await findings_mod.record_finding_for_event(_payload(run_id, work_ref)) is True
    await notify._await_pending_notifications()

    assert (await _noise_entry(group)).metadata["run_id"] == str(run_id)
    assert len(mails) == 1


@pytest.mark.asyncio
async def test_replay_after_a_newer_run_stored_its_finding_writes_nothing(
    mails: list[dict[str, Any]],
) -> None:
    """Run A stores, run B stores for the same group, then A's event comes again."""
    dashboard_id = await _seed_dashboard()
    run_a, work_ref_a = await _finding_run(dashboard_id=dashboard_id)
    newer = findings_mod.ChecksFinding(
        verdict="actionable", re_escalate=True, summary="Disk is full.", evidence=[]
    )
    run_b, work_ref_b = await _finding_run(finding=newer, dashboard_id=dashboard_id)

    assert await findings_mod.record_finding_for_event(_payload(run_a, work_ref_a)) is True
    assert await findings_mod.record_finding_for_event(_payload(run_b, work_ref_b)) is True
    assert await findings_mod.record_finding_for_event(_payload(run_a, work_ref_a)) is False
    await notify._await_pending_notifications()

    entry = await _noise_entry()
    assert entry.metadata["run_id"] == str(run_b)
    assert entry.metadata["re_escalate"] is True
    assert len(mails) == 2


@pytest.mark.asyncio
async def test_switch_off_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, mails: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("CHECKS_AGENT_EVENTS", "false")
    get_settings.cache_clear()
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id)

    assert await findings_mod.record_finding_for_event(_payload(run_id, work_ref)) is False
    assert await _noise_entry() is None
    assert mails == []


@pytest.mark.asyncio
async def test_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id)

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("memory is down")

    monkeypatch.setattr(findings_mod, "persist_finding", _boom)
    assert await findings_mod.record_finding_for_event(_payload(run_id, work_ref)) is False


# ---------------------------------------------------------------------------
# Through the drain
# ---------------------------------------------------------------------------


async def _publish_completed(run_id: UUID, work_ref: str | None) -> None:
    from meho_backplane.events.outbox import publish

    async with get_sessionmaker()() as session:
        await publish(
            session,
            tenant_id=_TENANT,
            event_kind=AGENT_RUN_COMPLETED_EVENT_KIND,
            payload=_payload(run_id, work_ref),
        )
        await session.commit()


@pytest.mark.asyncio
async def test_drain_stores_the_finding(mails: list[dict[str, Any]]) -> None:
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id)
    await _publish_completed(run_id, work_ref)

    assert await run_one_drain_tick() == 1
    await notify._await_pending_notifications()

    assert await _noise_entry() is not None
    assert len(mails) == 1


@pytest.mark.asyncio
async def test_drain_replay_after_a_failed_processed_stamp_mails_once(
    monkeypatch: pytest.MonkeyPatch, mails: list[dict[str, Any]]
) -> None:
    """The first tick stores and mails, then fails to mark the event processed."""
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id)
    await _publish_completed(run_id, work_ref)

    async def _fail_stamp(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("commit failed")

    real_stamp = drain._mark_processed
    monkeypatch.setattr(drain, "_mark_processed", _fail_stamp)
    assert await run_one_drain_tick() == 0
    monkeypatch.setattr(drain, "_mark_processed", real_stamp)
    assert await run_one_drain_tick() == 1
    await notify._await_pending_notifications()

    assert (await _noise_entry()).metadata["run_id"] == str(run_id)
    assert len(mails) == 1


@pytest.mark.asyncio
async def test_drain_with_switch_off_does_nothing(
    monkeypatch: pytest.MonkeyPatch, mails: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("CHECKS_AGENT_EVENTS", "false")
    get_settings.cache_clear()
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id)
    await _publish_completed(run_id, work_ref)

    assert await run_one_drain_tick() == 1
    assert await _noise_entry() is None
    assert mails == []


def test_module_never_imports_operations_dispatcher() -> None:
    source = inspect.getsource(findings_mod)
    assert "operations.dispatcher" not in source
    assert "import dispatch" not in source
