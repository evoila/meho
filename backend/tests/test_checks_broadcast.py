# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``checks.transition`` broadcast publisher (#2720).

Initiative #2716 (parent goal #221), Task #2720. Coverage:

* **Event shape** -- op-id, op-class, tenant, principal, and the four
  payload fields; plus the pin that the op-id the publisher emits is the
  one :func:`~meho_backplane.broadcast.events.classify_op` recognises.
* **Fail-open** -- a raising publisher never reaches the caller, and
  emits the structured warning operators alert on.
* **Integration through the persist seam** -- two raced
  ``investigate_on_transition`` calls on one Dashboard publish exactly one
  event (the compare-and-swap claim is the dedupe, same proof shape as
  #2719's email test), the recovery edge back to ``ok`` publishes a
  second, and a publisher outage leaves the memo committed and the mail
  sent.

:func:`~meho_backplane.broadcast.publisher.publish_event` is replaced by a
recording fake in every test, so no Valkey connection is opened; the DB
layer is real against the SQLite engine from :mod:`tests.conftest`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

import meho_backplane.checks.broadcast as checks_broadcast
import meho_backplane.checks.investigate as inv
import meho_backplane.checks.notify as notify
from meho_backplane.broadcast.events import BroadcastEvent, classify_op
from meho_backplane.broadcast.history import OP_CLASS_ENUM
from meho_backplane.checks.broadcast import (
    CHECK_TRANSITION_OP_ID,
    publish_check_transition_event,
)
from meho_backplane.connectors.mail.transport import MailSendResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    CheckDashboard,
    CheckDashboardSensor,
    Sensor,
    Tenant,
)
from meho_backplane.settings import get_settings

_TENANT = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_DASHBOARD = UUID("11111111-1111-1111-1111-111111111111")

_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


# ---------------------------------------------------------------------------
# Fixtures + doubles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env :class:`Settings` requires so ``get_settings()`` constructs."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://kc.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SENSOR_RUNNER_ENABLED", "false")
    # The notifier pre-screens recipients against this floor (#2764); allowlist
    # the ``example.com`` domain the seeded Dashboard mails to.
    monkeypatch.setenv("MAIL_RECIPIENT_ALLOWLIST", "example.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_background_tasks() -> Iterator[None]:
    """Clear the in-flight notification / investigation sets per test."""
    yield
    notify._NOTIFICATIONS.clear()
    inv._INVESTIGATIONS.clear()


class _RecordingPublisher:
    """Stands in for ``publish_event``; records every event, optionally raises."""

    def __init__(self, *, exc: Exception | None = None) -> None:
        self.events: list[BroadcastEvent] = []
        self._exc = exc

    async def __call__(self, event: BroadcastEvent) -> None:
        self.events.append(event)
        if self._exc is not None:
            raise self._exc


def _install_publisher(
    monkeypatch: pytest.MonkeyPatch, *, exc: Exception | None = None
) -> _RecordingPublisher:
    """Patch the publisher the checks module imported (not the source module)."""
    fake = _RecordingPublisher(exc=exc)
    monkeypatch.setattr(checks_broadcast, "publish_event", fake)
    return fake


class _RecordingTransport:
    """Stands in for ``send_email`` so the notifier opens no SMTP session."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *, to: list[str], subject: str, body: str) -> MailSendResult:
        self.calls.append({"to": list(to), "subject": subject, "body": body})
        return MailSendResult(sent=True)


def _install_transport(monkeypatch: pytest.MonkeyPatch) -> _RecordingTransport:
    fake = _RecordingTransport()
    monkeypatch.setattr(notify, "send_email", fake)
    return fake


# ---------------------------------------------------------------------------
# DB seeds (mirror tests/test_checks_notify.py)
# ---------------------------------------------------------------------------


async def _seed_tenant(tenant_id: UUID = _TENANT) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug="tenant-bcast", name="Tenant Broadcast"))
            await session.commit()


async def _seed_sensor(*, name: str, last_state: str = "critical") -> UUID:
    now = datetime.now(UTC)
    sensor_id = uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=_TENANT,
                name=name,
                connector_id="vmware-rest-9.0",
                op_id="vmware.vm.list",
                target=None,
                params={},
                assertion=_ASSERTION,
                status="active",
                cadence_kind="interval",
                interval_seconds=60,
                cron_expr=None,
                timezone="UTC",
                next_fire_at=now + timedelta(seconds=60),
                severity="critical",
                for_seconds=0,
                last_state=last_state,
                last_value=7,
                last_evidence={"observed": 7, "bound": 10},
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
    dashboard_id: UUID = _DASHBOARD,
    notify_email: str | None = "oncall@example.com",
) -> UUID:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=_TENANT,
                name=name,
                description=None,
                last_rollup_state=None,
                notify_email=notify_email,
                notify_min_state="critical",
                created_by_sub="op-admin",
            )
        )
        for sid in sensor_ids:
            session.add(CheckDashboardSensor(dashboard_id=dashboard_id, sensor_id=sid))
        await session.commit()
    return dashboard_id


async def _set_sensor_state(sensor_id: UUID, state: str) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        row.last_state = state
        row.state_since = datetime.now(UTC) - timedelta(hours=1)
        row.last_evaluated_at = datetime.now(UTC)
        await session.commit()


# ---------------------------------------------------------------------------
# Event shape
# ---------------------------------------------------------------------------


def test_op_id_is_the_one_classify_op_recognises() -> None:
    """The publisher's op-id and the classifier's allowlist cannot drift.

    The two live in different layers on purpose --
    :mod:`meho_backplane.broadcast.events` must not import from
    :mod:`meho_backplane.checks` -- so the literal is written twice. This
    pins them together, the same way the op-class enum is pinned to the
    tool schemas.
    """
    assert classify_op(CHECK_TRANSITION_OP_ID) == "checks"


def test_checks_is_filterable_from_the_agent_surface() -> None:
    """``checks`` is in the op_class filter vocabulary the MCP tools advertise.

    ``inputSchema`` validation is jsonschema-enforced at the dispatcher,
    so a class absent from :data:`OP_CLASS_ENUM` is a ``-32602`` before
    the handler runs -- the class would be published but unreachable from
    ``meho_broadcast_recent`` / ``.watch``.
    """
    assert "checks" in OP_CLASS_ENUM


@pytest.mark.asyncio
async def test_event_carries_the_edge_and_the_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One publish per call, with the documented op-id / class / payload."""
    fake = _install_publisher(monkeypatch)

    await publish_check_transition_event(
        tenant_id=_TENANT,
        dashboard_id=_DASHBOARD,
        dashboard_name="prod-health",
        previous_state="ok",
        new_state="critical",
    )

    assert len(fake.events) == 1
    event = fake.events[0]
    assert event.kind == "operation"
    assert event.op_id == "checks.transition"
    assert event.op_class == "checks"
    assert event.tenant_id == _TENANT
    assert event.principal_sub == "__checks__"
    assert event.result_status == "ok"
    assert event.payload == {
        "op_class": "checks",
        "result_status": "ok",
        "dashboard_id": str(_DASHBOARD),
        "dashboard_name": "prod-health",
        "previous_state": "ok",
        "new_state": "critical",
    }


@pytest.mark.asyncio
async def test_audit_id_is_the_nil_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """No audit row backs a rollup edge, so the FK is the nil sentinel.

    A fabricated id would be indistinguishable from a stale one to the UI
    drawer, which resolves ``/ui/broadcast/event/{audit_id}`` against
    ``audit_log``.
    """
    fake = _install_publisher(monkeypatch)

    await publish_check_transition_event(
        tenant_id=_TENANT,
        dashboard_id=_DASHBOARD,
        dashboard_name="prod-health",
        previous_state="degraded",
        new_state="ok",
    )

    assert fake.events[0].audit_id == UUID(int=0)


# ---------------------------------------------------------------------------
# Failure posture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publisher_failure_is_swallowed_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising publish never reaches the caller; it logs and returns.

    The injected failure stands in for what the guard actually covers: a
    fault *before* the ``XADD``, e.g. lineage resolution or
    ``BroadcastEvent`` validation. A Valkey outage never gets this far --
    :func:`~meho_backplane.broadcast.publisher.publish_event` swallows it
    and logs ``broadcast_publish_failed`` on its own.
    """
    _install_publisher(monkeypatch, exc=RuntimeError("event construction failed"))

    with capture_logs() as logs:
        await publish_check_transition_event(
            tenant_id=_TENANT,
            dashboard_id=_DASHBOARD,
            dashboard_name="prod-health",
            previous_state="ok",
            new_state="critical",
        )

    warnings = [entry for entry in logs if entry["event"] == "checks_transition_broadcast_failed"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["dashboard_id"] == str(_DASHBOARD)


# ---------------------------------------------------------------------------
# Integration through the persist seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_transitions_publish_exactly_one_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two raced persists on one Dashboard publish once -- the CAS claim dedupes.

    Then a recovery to ``ok`` publishes a second event: the feed carries
    both edge directions, so a watcher who saw the Dashboard redden sees
    it clear.
    """
    fake = _install_publisher(monkeypatch)
    _install_transport(monkeypatch)
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **_: None)
    await _seed_tenant()
    sid_a = await _seed_sensor(name="sensor-a", last_state="critical")
    sid_b = await _seed_sensor(name="sensor-b", last_state="critical")
    await _seed_dashboard(name="prod-health", sensor_ids=[sid_a, sid_b])

    await asyncio.gather(
        inv.investigate_on_transition(sensor_id=sid_a, tenant_id=_TENANT),
        inv.investigate_on_transition(sensor_id=sid_b, tenant_id=_TENANT),
    )

    assert len(fake.events) == 1, "the compare-and-swap claim must dedupe the raced edge"
    worsening = fake.events[0]
    assert worsening.op_id == "checks.transition"
    assert worsening.payload["dashboard_id"] == str(_DASHBOARD)
    assert worsening.payload["dashboard_name"] == "prod-health"
    assert worsening.payload["previous_state"] == "ok"
    assert worsening.payload["new_state"] == "critical"

    await _set_sensor_state(sid_a, "ok")
    await _set_sensor_state(sid_b, "ok")
    await inv.investigate_on_transition(sensor_id=sid_a, tenant_id=_TENANT)

    assert len(fake.events) == 2, "the recovery edge must publish too"
    assert fake.events[1].payload["previous_state"] == "critical"
    assert fake.events[1].payload["new_state"] == "ok"


@pytest.mark.asyncio
async def test_broadcast_failure_leaves_the_memo_and_the_mail_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing broadcast must not fail the persist seam or the notifier.

    The committed ``last_rollup_state`` memo is the durable truth; the
    feed is the at-most-once real-time view. This is the fail-open
    contract the whole publish path is built on. Raising at the publish
    seam is the strongest form of the failure -- a real Valkey outage is
    weaker still, since
    :func:`~meho_backplane.broadcast.publisher.publish_event` swallows it
    before this module's guard ever sees it.
    """
    _install_publisher(monkeypatch, exc=RuntimeError("broadcast unavailable"))
    mail = _install_transport(monkeypatch)
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **_: None)
    await _seed_tenant()
    sid = await _seed_sensor(name="sensor-solo", last_state="critical")
    await _seed_dashboard(name="prod-health", sensor_ids=[sid])

    await inv.investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
    await notify._await_pending_notifications()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(CheckDashboard, _DASHBOARD)
        assert row is not None
        assert row.last_rollup_state == "critical"
    assert len(mail.calls) == 1, "the email notifier must be unaffected by a feed outage"


@pytest.mark.asyncio
async def test_second_persist_on_an_unchanged_rollup_does_not_republish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the persist that moves the rollup state publishes.

    The Dashboard starts with a NULL memo, so the first
    ``investigate_on_transition`` wins a real ``ok -> critical`` claim and
    publishes once. The second call finds the memo already ``critical``,
    the compare-and-swap claims nothing, and the count must stay at one.
    Guards against the publish being wired outside the claim-win branch,
    which would emit one event per Sensor evaluation instead of one per
    edge.
    """
    fake = _install_publisher(monkeypatch)
    _install_transport(monkeypatch)
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **_: None)
    await _seed_tenant()
    sid = await _seed_sensor(name="sensor-solo", last_state="critical")
    await _seed_dashboard(name="prod-health", sensor_ids=[sid])

    await inv.investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
    assert len(fake.events) == 1, "the first persist claims the edge and publishes it"

    await inv.investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
    assert len(fake.events) == 1, "a no-op persist must not re-publish the edge"
