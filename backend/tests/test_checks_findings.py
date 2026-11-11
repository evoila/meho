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
from meho_backplane.checks.findings import ChecksFinding, record_finding_for_event
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentRun,
    CheckDashboard,
    EventOutbox,
    Tenant,
)
from meho_backplane.events.drain import run_one_drain_tick
from meho_backplane.events.matcher import _payload_contains
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


_FINDING = ChecksFinding(
    verdict="benign",
    re_escalate=False,
    summary="Known maintenance window on vc-01.",
    evidence=["maintenance mode"],
)


async def _seed_dashboard(
    *, tenant_id: UUID = _TENANT, notify_email: str | None = "ops@example.test"
) -> UUID:
    dashboard_id = uuid4()
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:6]}", name="Tenant"))
            await session.commit()
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


def _run_key(dashboard_id: UUID, group: str = _GROUP) -> str:
    return f"event:{uuid4()}:c:dashboard:{dashboard_id}:{group}"


async def _seed_run(
    *,
    work_ref: str | None,
    output: dict[str, object] | None,
    status: str = "succeeded",
    tenant_id: UUID = _TENANT,
) -> UUID:
    run_id = uuid4()
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:6]}", name="Tenant"))
            await session.commit()
        session.add(
            AgentRun(
                id=run_id,
                tenant_id=tenant_id,
                identity_sub="agent:my-investigator",
                trigger="event",
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
    *, finding: ChecksFinding = _FINDING, dashboard_id: UUID
) -> tuple[UUID, str]:
    work_ref = _run_key(dashboard_id)
    run_id = await _seed_run(work_ref=work_ref, output={"text": finding.model_dump_json()})
    return run_id, work_ref


# ---------------------------------------------------------------------------
# The finding step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finding_is_stored_and_mailed_once(mails: list[dict[str, Any]]) -> None:
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id)

    assert await record_finding_for_event(_payload(run_id, work_ref)) is True
    await notify._await_pending_notifications()

    entry = await _noise_entry()
    assert entry is not None
    assert entry.metadata["re_escalate"] is False
    assert entry.metadata["run_id"] == str(run_id)
    assert len(mails) == 1
    assert mails[0]["to"] == ["ops@example.test"]
    assert "Known maintenance window" in mails[0]["body"]


@pytest.mark.asyncio
async def test_known_noise_is_published_and_example_trigger_does_not_match() -> None:
    """After the stored finding, the next change for the group is known noise."""
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id)
    assert await record_finding_for_event(_payload(run_id, work_ref)) is True

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
        rows = (
            (await session.execute(select(EventOutbox).where(EventOutbox.tenant_id == _TENANT)))
            .scalars()
            .all()
        )
    payload = rows[-1].payload
    assert payload["known_noise"] is True
    assert not _payload_contains(payload, _EXAMPLE_FILTER)


@pytest.mark.asyncio
async def test_re_escalate_true_keeps_the_next_change_open() -> None:
    dashboard_id = await _seed_dashboard()
    finding = ChecksFinding(verdict="actionable", re_escalate=True, summary="Disk is full.")
    run_id, work_ref = await _finding_run(finding=finding, dashboard_id=dashboard_id)
    assert await record_finding_for_event(_payload(run_id, work_ref)) is True

    suppressed = await inv._is_suppressed(
        MemoryService(), inv.investigator_operator(_TENANT), _GROUP
    )
    assert suppressed is False


# ---------------------------------------------------------------------------
# Cases that write nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_finding_answer_writes_nothing(mails: list[dict[str, Any]]) -> None:
    dashboard_id = await _seed_dashboard()
    work_ref = _run_key(dashboard_id)
    run_id = await _seed_run(work_ref=work_ref, output={"text": "All looks fine to me."})

    assert await record_finding_for_event(_payload(run_id, work_ref)) is False
    await notify._await_pending_notifications()
    assert await _noise_entry() is None
    assert mails == []


@pytest.mark.asyncio
async def test_run_without_output_writes_nothing(mails: list[dict[str, Any]]) -> None:
    dashboard_id = await _seed_dashboard()
    work_ref = _run_key(dashboard_id)
    run_id = await _seed_run(work_ref=work_ref, output=None)

    assert await record_finding_for_event(_payload(run_id, work_ref)) is False
    assert await _noise_entry() is None
    assert mails == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_run_that_did_not_succeed_writes_nothing(
    status: str, mails: list[dict[str, Any]]
) -> None:
    dashboard_id = await _seed_dashboard()
    work_ref = _run_key(dashboard_id)
    run_id = await _seed_run(
        work_ref=work_ref, output={"text": _FINDING.model_dump_json()}, status=status
    )

    assert await record_finding_for_event(_payload(run_id, work_ref, status=status)) is False
    await notify._await_pending_notifications()
    assert await _noise_entry() is None
    assert mails == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work_ref",
    [
        None,
        "checks:abcd1234:prod:target-vc-01",
        f"event:{uuid4()}:{uuid4()}",
        f"event:{uuid4()}:c:sensor:{uuid4()}",
        f"event:{uuid4()}:c:dashboard:not-a-uuid:{_GROUP}",
        f"event:{uuid4()}:c:dashboard:{uuid4()}",
    ],
)
async def test_run_key_of_another_form_writes_nothing(
    work_ref: str | None, mails: list[dict[str, Any]]
) -> None:
    run_id = await _seed_run(work_ref=work_ref, output={"text": _FINDING.model_dump_json()})

    assert await record_finding_for_event(_payload(run_id, work_ref)) is False
    assert await _noise_entry() is None
    assert mails == []


@pytest.mark.asyncio
async def test_dashboard_of_another_tenant_writes_nothing(mails: list[dict[str, Any]]) -> None:
    other_dashboard = await _seed_dashboard(tenant_id=_OTHER_TENANT)
    work_ref = _run_key(other_dashboard)
    run_id = await _seed_run(work_ref=work_ref, output={"text": _FINDING.model_dump_json()})

    assert await record_finding_for_event(_payload(run_id, work_ref)) is False
    await notify._await_pending_notifications()
    assert await _noise_entry() is None
    assert mails == []


@pytest.mark.asyncio
async def test_missing_dashboard_writes_nothing(mails: list[dict[str, Any]]) -> None:
    work_ref = _run_key(uuid4())
    run_id = await _seed_run(work_ref=work_ref, output={"text": _FINDING.model_dump_json()})

    assert await record_finding_for_event(_payload(run_id, work_ref)) is False
    assert await _noise_entry() is None
    assert mails == []


@pytest.mark.asyncio
async def test_unknown_run_writes_nothing() -> None:
    dashboard_id = await _seed_dashboard()
    assert await record_finding_for_event(_payload(uuid4(), _run_key(dashboard_id))) is False


@pytest.mark.asyncio
async def test_switch_off_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, mails: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("CHECKS_AGENT_EVENTS", "false")
    get_settings.cache_clear()
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id)

    assert await record_finding_for_event(_payload(run_id, work_ref)) is False
    assert await _noise_entry() is None
    assert mails == []


@pytest.mark.asyncio
async def test_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    dashboard_id = await _seed_dashboard()
    run_id, work_ref = await _finding_run(dashboard_id=dashboard_id)

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("memory is down")

    monkeypatch.setattr(findings_mod, "persist_finding", _boom)
    assert await record_finding_for_event(_payload(run_id, work_ref)) is False


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
