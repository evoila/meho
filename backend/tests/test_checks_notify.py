# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Dashboard email notifier (#2719).

Initiative #2716 (parent goal #221), Task #2719. Coverage:

* **Threshold rule** -- the ``max(rank(previous), rank(current)) >=
  rank(notify_min_state)`` matrix, including the ``critical -> ok``
  all-clear, the ``ok -> degraded`` silence at ``min_state='critical'``,
  ``unknown`` ranking with ``degraded``, and ``skip`` never clearing the
  bar on its own.
* **Unconfigured** -- a ``notify_email``-less Dashboard sends nothing.
* **Failure posture** -- a ``sent=False`` transport result and a raising
  transport are both swallowed; neither reaches the caller.
* **Message content** -- the mail names the Dashboard, the edge, and the
  non-green members; a recovery reads as an all-clear; a control-character
  Dashboard name cannot inject an SMTP header.
* **Integration through the persist seam** -- two raced
  ``investigate_on_transition`` calls on one Dashboard produce exactly one
  send (the compare-and-swap claim is the dedupe), and the recovery edge
  back to ``ok`` produces a second one.

:func:`~meho_backplane.connectors.mail.transport.send_email` is replaced by
a recording fake in every test, so no SMTP session is opened; the DB layer
is real against the SQLite engine from :mod:`tests.conftest`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

import meho_backplane.checks.investigate as inv
import meho_backplane.checks.notify as notify
from meho_backplane.checks.notify import (
    DashboardNotice,
    NotifyMember,
    notify_dashboard_transition,
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

_TENANT = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

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
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_notifications() -> Iterator[None]:
    """Clear the in-flight notification / investigation sets per test."""
    yield
    notify._NOTIFICATIONS.clear()
    inv._INVESTIGATIONS.clear()


class _RecordingTransport:
    """Stands in for ``send_email``; records every call, returns a preset."""

    def __init__(
        self,
        *,
        result: MailSendResult | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result or MailSendResult(sent=True)
        self._exc = exc

    async def __call__(self, *, to: list[str], subject: str, body: str) -> MailSendResult:
        self.calls.append({"to": list(to), "subject": subject, "body": body})
        if self._exc is not None:
            raise self._exc
        return self._result


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: Any,
) -> _RecordingTransport:
    fake = _RecordingTransport(**kwargs)
    monkeypatch.setattr(notify, "send_email", fake)
    return fake


def _notice(
    *,
    previous: str = "ok",
    current: str = "critical",
    email: str | None = "oncall@example.com",
    min_state: str = "critical",
    members: tuple[NotifyMember, ...] = (),
    name: str = "prod-health",
) -> DashboardNotice:
    return DashboardNotice(
        dashboard_id=uuid4(),
        name=name,
        previous_state=previous,
        current_state=current,
        notify_email=email,
        notify_min_state=min_state,
        members=members,
    )


def _member(
    name: str = "disk-space",
    *,
    state: str = "critical",
    last_value: object = 3,
    evidence: dict[str, object] | None = None,
) -> NotifyMember:
    return NotifyMember(
        name=name,
        effective_state=state,
        last_value=last_value,
        last_evidence=evidence if evidence is not None else {"observed": 3, "bound": 10},
    )


# ---------------------------------------------------------------------------
# Seeding helpers (integration through the persist seam)
# ---------------------------------------------------------------------------


async def _seed_tenant(tenant_id: UUID = _TENANT) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug="tenant-notify", name="Tenant Notify"))
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
    last_rollup_state: str | None = None,
    notify_email: str | None = "oncall@example.com",
    notify_min_state: str = "critical",
) -> UUID:
    dashboard_id = uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=_TENANT,
                name=name,
                description=None,
                last_rollup_state=last_rollup_state,
                notify_email=notify_email,
                notify_min_state=notify_min_state,
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
# Threshold rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous", "current", "min_state", "should_send"),
    [
        # The binding rule: max(rank(previous), rank(current)) >= rank(min).
        pytest.param("ok", "critical", "critical", True, id="ok-to-critical-at-critical"),
        pytest.param("critical", "ok", "critical", True, id="critical-to-ok-all-clear"),
        pytest.param("ok", "degraded", "critical", False, id="ok-to-degraded-silent"),
        pytest.param("degraded", "ok", "critical", False, id="degraded-to-ok-silent"),
        pytest.param("ok", "degraded", "degraded", True, id="ok-to-degraded-at-degraded"),
        pytest.param("degraded", "ok", "degraded", True, id="degraded-to-ok-all-clear"),
        pytest.param("degraded", "critical", "critical", True, id="degraded-to-critical"),
        pytest.param("critical", "degraded", "critical", True, id="critical-to-degraded"),
        # unknown ranks with degraded.
        pytest.param("ok", "unknown", "degraded", True, id="unknown-ranks-degraded"),
        pytest.param("ok", "unknown", "critical", False, id="unknown-below-critical"),
        # skip ranks with ok -- it never clears the bar on its own.
        pytest.param("ok", "skip", "degraded", False, id="skip-never-fires-alone"),
        pytest.param("skip", "ok", "critical", False, id="skip-to-ok-silent"),
        pytest.param("critical", "skip", "critical", True, id="critical-to-skip-other-side"),
    ],
)
async def test_threshold_matrix(
    monkeypatch: pytest.MonkeyPatch,
    previous: str,
    current: str,
    min_state: str,
    should_send: bool,
) -> None:
    """The notify rule is symmetric in the two states and keyed on the floor."""
    fake = _install_transport(monkeypatch)
    await notify_dashboard_transition(
        _notice(previous=previous, current=current, min_state=min_state)
    )
    assert (len(fake.calls) == 1) is should_send


@pytest.mark.asyncio
async def test_unconfigured_dashboard_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``notify_email`` NULL is the off switch -- the transport is never reached."""
    fake = _install_transport(monkeypatch)
    with capture_logs() as logs:
        await notify_dashboard_transition(_notice(email=None))
    assert fake.calls == []
    assert any(entry["event"] == "checks_notify_skipped_unconfigured" for entry in logs)


@pytest.mark.asyncio
async def test_empty_email_string_is_also_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty-string recipient is treated as unset, not mailed to nobody."""
    fake = _install_transport(monkeypatch)
    await notify_dashboard_transition(_notice(email=""))
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Failure posture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``sent=False`` refusal warn-logs its reason code and never raises."""
    _install_transport(
        monkeypatch,
        result=MailSendResult(sent=False, reason="not_in_recipient_allowlist"),
    )
    with capture_logs() as logs:
        await notify_dashboard_transition(_notice())
    failed = [entry for entry in logs if entry["event"] == "checks_notify_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "not_in_recipient_allowlist"


@pytest.mark.asyncio
async def test_transport_exception_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected transport exception is contained, not propagated."""
    _install_transport(monkeypatch, exc=RuntimeError("socket exploded"))
    with capture_logs() as logs:
        await notify_dashboard_transition(_notice())
    failed = [entry for entry in logs if entry["event"] == "checks_notify_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "unexpected_error"


@pytest.mark.asyncio
async def test_successful_send_logs_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delivered mail records ``checks_notify_sent`` with the edge."""
    _install_transport(monkeypatch)
    with capture_logs() as logs:
        await notify_dashboard_transition(_notice(previous="critical", current="ok"))
    sent = [entry for entry in logs if entry["event"] == "checks_notify_sent"]
    assert len(sent) == 1
    assert sent[0]["previous_state"] == "critical"
    assert sent[0]["current_state"] == "ok"


# ---------------------------------------------------------------------------
# Message content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_mail_names_dashboard_edge_and_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mail carries the Dashboard, the edge, and each non-green member."""
    fake = _install_transport(monkeypatch)
    await notify_dashboard_transition(
        _notice(
            previous="ok",
            current="critical",
            members=(
                _member("disk-space", state="critical", last_value=3),
                _member("cpu-load", state="degraded", last_value=91),
            ),
        )
    )
    call = fake.calls[0]
    assert call["to"] == ["oncall@example.com"]
    assert "prod-health" in call["subject"]
    assert "ok -> critical" in call["subject"]
    assert "all clear" not in call["subject"]
    body = call["body"]
    assert "Dashboard: prod-health" in body
    assert "State: ok -> critical" in body
    for fragment in ("disk-space", "[critical]", "cpu-load", "[degraded]", "91"):
        assert fragment in body


@pytest.mark.asyncio
async def test_recovery_mail_is_an_all_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no non-green member left the mail reads as the all-clear."""
    fake = _install_transport(monkeypatch)
    await notify_dashboard_transition(_notice(previous="critical", current="ok"))
    call = fake.calls[0]
    assert "all clear" in call["subject"]
    assert "critical -> ok" in call["subject"]
    assert "All clear" in call["body"]
    assert "State: critical -> ok" in call["body"]


@pytest.mark.asyncio
async def test_subject_folds_control_characters_in_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CR/LF-bearing Dashboard name cannot inject an SMTP header."""
    fake = _install_transport(monkeypatch)
    await notify_dashboard_transition(_notice(name="prod\r\nBcc: attacker@evil.test"))
    subject = fake.calls[0]["subject"]
    assert "\r" not in subject
    assert "\n" not in subject


@pytest.mark.asyncio
async def test_body_bounds_members_and_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """The body caps the member list and clips each untrusted field."""
    fake = _install_transport(monkeypatch)
    members = tuple(
        _member(f"sensor-{i:03d}", last_value="x" * 5000)
        for i in range(notify._MAX_MEMBER_LINES + 5)
    )
    await notify_dashboard_transition(_notice(members=members))
    body = fake.calls[0]["body"]
    assert "sensor-000" in body
    assert f"sensor-{notify._MAX_MEMBER_LINES:03d}" not in body
    assert "further member(s) not shown" in body
    assert "x" * (notify._MAX_FIELD_CHARS + 1) not in body


# ---------------------------------------------------------------------------
# Integration through the persist seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_transitions_send_exactly_one_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two raced persists on one Dashboard mail once -- the CAS claim dedupes.

    Then a recovery to ``ok`` mails a second time: the notifier acts on both
    edge directions, unlike the investigator's worsening-only gate.
    """
    fake = _install_transport(monkeypatch)
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **_: None)
    await _seed_tenant()
    sid_a = await _seed_sensor(name="sensor-a", last_state="critical")
    sid_b = await _seed_sensor(name="sensor-b", last_state="critical")
    dash = await _seed_dashboard(name="prod-health", sensor_ids=[sid_a, sid_b])

    await asyncio.gather(
        inv.investigate_on_transition(sensor_id=sid_a, tenant_id=_TENANT),
        inv.investigate_on_transition(sensor_id=sid_b, tenant_id=_TENANT),
    )
    await notify._await_pending_notifications()

    assert len(fake.calls) == 1, "the compare-and-swap claim must dedupe the raced edge"
    assert "ok -> critical" in fake.calls[0]["subject"]
    assert "sensor-a" in fake.calls[0]["body"]
    assert "sensor-b" in fake.calls[0]["body"]

    # Recovery: both members go green, the Dashboard folds back to ok.
    await _set_sensor_state(sid_a, "ok")
    await _set_sensor_state(sid_b, "ok")
    await inv.investigate_on_transition(sensor_id=sid_a, tenant_id=_TENANT)
    await notify._await_pending_notifications()

    assert len(fake.calls) == 2, "the recovery edge must mail the all-clear"
    assert "all clear" in fake.calls[1]["subject"]
    assert "critical -> ok" in fake.calls[1]["subject"]

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(CheckDashboard, dash)
        assert row is not None
        assert row.last_rollup_state == "ok"


@pytest.mark.asyncio
async def test_unconfigured_dashboard_sends_nothing_through_the_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Dashboard with no recipient transitions silently (the default row)."""
    fake = _install_transport(monkeypatch)
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **_: None)
    await _seed_tenant()
    sid = await _seed_sensor(name="lonely", last_state="critical")
    await _seed_dashboard(name="quiet", sensor_ids=[sid], notify_email=None)

    await inv.investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
    await notify._await_pending_notifications()

    assert fake.calls == []


@pytest.mark.asyncio
async def test_send_failure_through_the_seam_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken MTA cannot convert a committed claim into a persist failure."""
    _install_transport(monkeypatch, exc=RuntimeError("MTA down"))
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **_: None)
    await _seed_tenant()
    sid = await _seed_sensor(name="crit", last_state="critical")
    dash = await _seed_dashboard(name="prod-health", sensor_ids=[sid])

    await inv.investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
    await notify._await_pending_notifications()

    # The memo still advanced: the claim is independent of delivery.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(CheckDashboard, dash)
        assert row is not None
        assert row.last_rollup_state == "critical"


@pytest.mark.asyncio
async def test_investigator_gate_is_unchanged_by_the_notify_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovery edge notifies but never schedules an investigation."""
    fake = _install_transport(monkeypatch)
    fired: list[dict[str, Any]] = []
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **kw: fired.append(kw))
    await _seed_tenant()
    sid = await _seed_sensor(name="recovering", last_state="ok")
    await _seed_dashboard(name="prod-health", sensor_ids=[sid], last_rollup_state="critical")

    await inv.investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
    await notify._await_pending_notifications()

    assert fired == [], "the improving edge must not reach the investigator"
    assert len(fake.calls) == 1, "the improving edge must reach the notifier"
