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
* **Flap suppression (#2732)** -- one delivery per (dashboard, state) per
  window with re-delivery after expiry, escalation and recovery exempt,
  recovery resetting the windows (below-floor recovery included), the
  fail-open Valkey posture on claim and clear, the ``0`` knob
  short-circuiting before any Valkey call, the attempt-based claim, and
  the same flap sequence suppressed through the persist seam while the
  memo keeps advancing.
* **Finding mail (#2721)** -- the second notice kind: verdict / summary /
  ``run_id`` / recommended action rendering, the unconfigured skip, both
  failure shapes (refused result and raising transport), header safety on
  the model-influenced subject, bounding of an unbounded model answer, and
  the off-the-caller scheduling.

:func:`~meho_backplane.connectors.mail.transport.send_email` is replaced by
a recording fake in every test, so no SMTP session is opened; the DB layer
is real against the SQLite engine from :mod:`tests.conftest`. The Valkey
client behind the #2732 suppression window is replaced by
:class:`_FakeSuppressionStore` (autouse) -- real ``SET NX EX`` semantics
with a manually-advanced TTL clock, so window expiry is driven, not
simulated by deleting keys -- and no socket ever opens.
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
    FindingNotice,
    NotifyMember,
    notify_dashboard_transition,
    notify_finding,
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
    # The notifier pre-screens every recipient against this floor (#2764), so
    # the ``@example.com`` addresses these tests mail to must be allowlisted;
    # a non-``example.com`` address exercises the per-entry refusal.
    monkeypatch.setenv("MAIL_RECIPIENT_ALLOWLIST", "example.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_notifications() -> Iterator[None]:
    """Clear the in-flight notification / investigation sets per test."""
    yield
    notify._NOTIFICATIONS.clear()
    inv._INVESTIGATIONS.clear()


class _FakeSuppressionStore:
    """Valkey double for the #2732 flap window: ``SET NX EX`` + ``DEL``.

    Models redis-py 8.0.1's awaited command shape (``set(..., nx=True,
    ex=...)`` returns ``True`` when the key was absent and ``None`` when it
    already existed -- ``parse_set_result``'s contract; ``delete(*names)``
    returns the removed count) plus a **manually-advanced TTL clock**, so
    the window-expiry assertion drives real key expiry through
    :meth:`advance` instead of deleting keys behind the code's back.
    """

    def __init__(self) -> None:
        self.now: float = 0.0
        self.set_calls: list[tuple[str, int]] = []
        self.deleted: list[str] = []
        self.set_exc: Exception | None = None
        self.delete_exc: Exception | None = None
        self._store: dict[str, float] = {}

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _purge(self) -> None:
        self._store = {key: exp for key, exp in self._store.items() if exp > self.now}

    async def set(
        self, name: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if self.set_exc is not None:
            raise self.set_exc
        assert nx, "the suppression claim must be NX -- atomic check-and-claim"
        assert ex is not None and ex > 0, "the suppression claim must carry the window TTL"
        self.set_calls.append((name, ex))
        self._purge()
        if name in self._store:
            return None
        self._store[name] = self.now + ex
        return True

    async def delete(self, *names: str) -> int:
        if self.delete_exc is not None:
            raise self.delete_exc
        self.deleted.extend(names)
        self._purge()
        removed = 0
        for name in names:
            if self._store.pop(name, None) is not None:
                removed += 1
        return removed


@pytest.fixture(autouse=True)
def suppression_store(monkeypatch: pytest.MonkeyPatch) -> _FakeSuppressionStore:
    """Replace the notifier's Valkey client so no test ever opens a socket.

    Autouse because the suppression window is on by default (30 minutes):
    without this, every delivering test would attempt a real connection to
    the settings-default Valkey -- and a developer's local instance would
    leak claims across tests.
    """
    store = _FakeSuppressionStore()
    monkeypatch.setattr(notify, "get_broadcast_client", lambda: store)
    return store


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
    dashboard_id: UUID | None = None,
) -> DashboardNotice:
    return DashboardNotice(
        tenant_id=_TENANT,
        dashboard_id=dashboard_id or uuid4(),
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
# Multi-recipient fan-out (#2764)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_mail_fans_out_to_every_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comma-joined ``notify_email`` delivers the identical mail to all (#2764)."""
    fake = _install_transport(monkeypatch)
    await notify_dashboard_transition(_notice(email="oncall@example.com,team@example.com"))
    assert len(fake.calls) == 1, "one send fans out to every recipient"
    assert fake.calls[0]["to"] == ["oncall@example.com", "team@example.com"]


@pytest.mark.asyncio
async def test_transition_mail_drops_only_the_refused_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused recipient never silences delivery to the allowlisted rest (#2764).

    The allowlist floor is ``example.com`` (the settings fixture); the
    ``@blocked.test`` entry is refused per-entry and warn-logged, while the
    allowlisted address still receives the mail in one ``send_email`` call --
    the transport's own all-or-nothing screen is deliberately left untouched.
    """
    fake = _install_transport(monkeypatch)
    with capture_logs() as logs:
        await notify_dashboard_transition(_notice(email="oncall@example.com,stranger@blocked.test"))
    assert len(fake.calls) == 1, "the allowlisted recipient still receives the mail"
    assert fake.calls[0]["to"] == ["oncall@example.com"]
    refused = [e for e in logs if e["event"] == "checks_notify_recipient_refused"]
    assert len(refused) == 1
    assert refused[0]["recipient"] == "stranger@blocked.test"


@pytest.mark.asyncio
async def test_transition_mail_all_recipients_refused_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every configured recipient is refused there is nothing to send (#2764)."""
    fake = _install_transport(monkeypatch)
    with capture_logs() as logs:
        await notify_dashboard_transition(_notice(email="a@blocked.test,b@blocked.test"))
    assert fake.calls == []
    assert any(e["event"] == "checks_notify_no_allowed_recipients" for e in logs)
    assert [e["recipient"] for e in logs if e["event"] == "checks_notify_recipient_refused"] == [
        "a@blocked.test",
        "b@blocked.test",
    ]


@pytest.mark.asyncio
async def test_one_suppression_claim_per_edge_regardless_of_recipients(
    monkeypatch: pytest.MonkeyPatch,
    suppression_store: _FakeSuppressionStore,
) -> None:
    """A multi-recipient Dashboard claims exactly one flap window per edge (#2764).

    The suppression key carries no recipient segment, so N recipients cannot
    burn N windows -- one claim covers the whole fan-out.
    """
    fake = _install_transport(monkeypatch)
    await notify_dashboard_transition(
        _notice(email="oncall@example.com,team@example.com", dashboard_id=uuid4())
    )
    assert len(fake.calls) == 1
    assert len(fake.calls[0]["to"]) == 2, "both recipients received the one mail"
    assert len(suppression_store.set_calls) == 1, "exactly one claim covers every recipient"


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


# ---------------------------------------------------------------------------
# Flap suppression (#2732)
# ---------------------------------------------------------------------------


_WINDOW_SECONDS = 30 * 60  # the settings default the fixture leaves in place


@pytest.mark.asyncio
async def test_flap_within_window_delivers_once_per_state(
    monkeypatch: pytest.MonkeyPatch,
    suppression_store: _FakeSuppressionStore,
) -> None:
    """``critical <-> unknown`` flapping mails once per distinct state per window.

    The realistic flap shape: a member Sensor going stale and back derives
    ``unknown`` and returns, so the Dashboard re-crosses the same two states.
    Each repeat crossing inside the window is suppressed with a structured
    log; once the window expires the same sequence delivers again.
    """
    fake = _install_transport(monkeypatch)
    dash = uuid4()
    flap = [
        ("critical", "unknown"),
        ("unknown", "critical"),
        ("critical", "unknown"),
        ("unknown", "critical"),
    ]

    with capture_logs() as logs:
        for previous, current in flap:
            await notify_dashboard_transition(
                _notice(previous=previous, current=current, dashboard_id=dash)
            )

    assert ["-> unknown" in c["subject"] for c in fake.calls] == [True, False]
    assert len(fake.calls) == 2, "one delivery per distinct state inside the window"
    suppressed = [e for e in logs if e["event"] == "checks_notify_suppressed"]
    assert len(suppressed) == 2
    assert {e["current_state"] for e in suppressed} == {"unknown", "critical"}

    suppression_store.advance(_WINDOW_SECONDS + 1)
    for previous, current in flap:
        await notify_dashboard_transition(
            _notice(previous=previous, current=current, dashboard_id=dash)
        )

    assert len(fake.calls) == 4, "an expired window delivers the same states again"
    assert all(ex == _WINDOW_SECONDS for _, ex in suppression_store.set_calls)


@pytest.mark.asyncio
async def test_escalation_is_never_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``critical`` page right after a ``degraded`` notice delivers both."""
    fake = _install_transport(monkeypatch)
    dash = uuid4()

    await notify_dashboard_transition(
        _notice(previous="ok", current="degraded", min_state="degraded", dashboard_id=dash)
    )
    await notify_dashboard_transition(
        _notice(previous="degraded", current="critical", min_state="degraded", dashboard_id=dash)
    )

    assert len(fake.calls) == 2, "a different state is a different key -- never suppressed"
    assert "-> degraded" in fake.calls[0]["subject"]
    assert "-> critical" in fake.calls[1]["subject"]


@pytest.mark.asyncio
async def test_recovery_is_never_suppressed_and_resets_windows(
    monkeypatch: pytest.MonkeyPatch,
    suppression_store: _FakeSuppressionStore,
) -> None:
    """The all-clear always delivers, and it ends the incident's windows.

    ``ok -> critical -> ok -> critical -> ok`` inside one window: every edge
    mails. The second ``critical`` is a *new* incident (the recovery cleared
    the keys), and the second all-clear is exempt from suppression outright.
    """
    fake = _install_transport(monkeypatch)
    dash = uuid4()
    for previous, current in [
        ("ok", "critical"),
        ("critical", "ok"),
        ("ok", "critical"),
        ("critical", "ok"),
    ]:
        await notify_dashboard_transition(
            _notice(previous=previous, current=current, dashboard_id=dash)
        )

    assert len(fake.calls) == 4, "recovery must deliver and reset the flap windows"
    assert suppression_store.deleted, "the recovery crossing clears the suppression keys"


@pytest.mark.asyncio
async def test_below_floor_recovery_still_resets_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovery too quiet to mail still ends the incident.

    At the default ``critical`` floor, ``degraded -> ok`` sends nothing --
    but the next ``critical`` crossing is a new incident and must page, so
    the clear runs before the floor gate, not behind it.
    """
    fake = _install_transport(monkeypatch)
    dash = uuid4()
    for previous, current in [
        ("ok", "critical"),  # mails, claims the critical window
        ("critical", "degraded"),  # mails (critical side), claims degraded
        ("degraded", "ok"),  # below the critical floor: silent, but clears
        ("ok", "critical"),  # new incident -- must mail again
    ]:
        await notify_dashboard_transition(
            _notice(previous=previous, current=current, dashboard_id=dash)
        )

    subjects = [c["subject"] for c in fake.calls]
    assert len(subjects) == 3
    assert "-> critical" in subjects[0]
    assert "-> degraded" in subjects[1]
    assert "-> critical" in subjects[2], "the below-floor recovery must have reset the window"


@pytest.mark.asyncio
async def test_valkey_failure_on_claim_fails_open(
    monkeypatch: pytest.MonkeyPatch,
    suppression_store: _FakeSuppressionStore,
) -> None:
    """A Valkey teardown never drops a page -- deliver, warn, move on."""
    fake = _install_transport(monkeypatch)
    suppression_store.set_exc = RuntimeError("valkey down")

    with capture_logs() as logs:
        await notify_dashboard_transition(_notice())

    assert len(fake.calls) == 1, "suppression is fail-open: the mail must be sent"
    failed = [e for e in logs if e["event"] == "checks_notify_suppression_failed"]
    assert len(failed) == 1
    assert failed[0]["phase"] == "claim"


@pytest.mark.asyncio
async def test_valkey_failure_on_clear_fails_open(
    monkeypatch: pytest.MonkeyPatch,
    suppression_store: _FakeSuppressionStore,
) -> None:
    """A failing key clear never blocks the all-clear mail."""
    fake = _install_transport(monkeypatch)
    suppression_store.delete_exc = RuntimeError("valkey down")

    with capture_logs() as logs:
        await notify_dashboard_transition(_notice(previous="critical", current="ok"))

    assert len(fake.calls) == 1
    failed = [e for e in logs if e["event"] == "checks_notify_suppression_failed"]
    assert len(failed) == 1
    assert failed[0]["phase"] == "clear"


@pytest.mark.asyncio
async def test_window_zero_disables_and_skips_valkey(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``0`` restores one-mail-per-edge and short-circuits before any Valkey call."""
    fake = _install_transport(monkeypatch)
    monkeypatch.setenv("CHECKS_NOTIFY_SUPPRESSION_WINDOW_MINUTES", "0")
    get_settings.cache_clear()
    monkeypatch.setattr(
        notify,
        "get_broadcast_client",
        lambda: pytest.fail("window 0 must not touch Valkey"),
    )
    dash = uuid4()

    for _ in range(2):
        await notify_dashboard_transition(_notice(dashboard_id=dash))
    await notify_dashboard_transition(_notice(previous="critical", current="ok", dashboard_id=dash))

    assert len(fake.calls) == 3, "no suppression and no clear when the window is 0"


@pytest.mark.asyncio
async def test_failed_send_still_claims_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window bounds attempts, not confirmed deliveries.

    The claim is atomic check-and-claim *before* the send (claiming after
    would race a slow SMTP session against the next flap edge), so a failed
    attempt is not retried by the next same-state crossing -- the #2719
    one-attempt-per-claimed-transition contract, now per window.
    """
    fake = _install_transport(monkeypatch, exc=RuntimeError("MTA down"))
    dash = uuid4()

    with capture_logs() as logs:
        await notify_dashboard_transition(_notice(dashboard_id=dash))
        await notify_dashboard_transition(_notice(dashboard_id=dash))

    assert len(fake.calls) == 1, "the second same-state edge is suppressed, not retried"
    assert any(e["event"] == "checks_notify_failed" for e in logs)
    assert any(e["event"] == "checks_notify_suppressed" for e in logs)


@pytest.mark.asyncio
async def test_flap_through_the_seam_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suppression bounds delivery only -- the memo keeps advancing per edge.

    A member flapping ``critical <-> degraded`` through the real persist
    seam mails once per state; every edge still moves
    ``last_rollup_state`` (the claim, the broadcast event, and the
    investigator gate are deliberately untouched by #2732).
    """
    fake = _install_transport(monkeypatch)
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **_: None)
    await _seed_tenant()
    sid = await _seed_sensor(name="flappy", last_state="critical")
    dash = await _seed_dashboard(name="prod-health", sensor_ids=[sid], notify_min_state="degraded")

    for state in ("degraded", "critical", "degraded"):
        await inv.investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
        await notify._await_pending_notifications()
        await _set_sensor_state(sid, state)
    await inv.investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
    await notify._await_pending_notifications()

    assert len(fake.calls) == 2, "one mail per distinct state inside the window"
    assert "-> critical" in fake.calls[0]["subject"]
    assert "-> degraded" in fake.calls[1]["subject"]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(CheckDashboard, dash)
        assert row is not None
        assert row.last_rollup_state == "degraded", "the memo must advance on every edge"


# ---------------------------------------------------------------------------
# Finding mail (#2721)
# ---------------------------------------------------------------------------


def _finding(
    *,
    verdict: str = "actionable",
    summary: str = "Datastore ds-01 is 98% full.",
    evidence: tuple[str, ...] = ("capacity 98%",),
    action: str | None = "Reclaim thin-provisioned space.",
    email: str | None = "oncall@example.com",
    name: str = "prod-health",
) -> FindingNotice:
    return FindingNotice(
        dashboard_id=uuid4(),
        dashboard_name=name,
        run_id=uuid4(),
        verdict=verdict,
        summary=summary,
        evidence=evidence,
        recommended_action=action,
        recipient=email,
    )


@pytest.mark.asyncio
async def test_finding_mail_carries_verdict_summary_and_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mail names the verdict, the summary, the run, and the action."""
    fake = _install_transport(monkeypatch)
    notice = _finding()

    await notify_finding(notice)

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["to"] == ["oncall@example.com"]
    assert call["subject"] == "[MEHO] investigation actionable: prod-health"
    body = call["body"]
    assert "Datastore ds-01 is 98% full." in body
    assert str(notice.run_id) in body
    assert "capacity 98%" in body
    assert "Reclaim thin-provisioned space." in body
    # The advisory-only disclaimer rides with the action, not without it.
    assert "diagnose-only" in body


@pytest.mark.asyncio
async def test_finding_mail_fans_out_to_every_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finding mail fans out to the same recipient list as the transition (#2764)."""
    fake = _install_transport(monkeypatch)
    await notify_finding(_finding(email="oncall@example.com,team@example.com"))
    assert len(fake.calls) == 1
    assert fake.calls[0]["to"] == ["oncall@example.com", "team@example.com"]


@pytest.mark.asyncio
async def test_finding_mail_drops_only_the_refused_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused finding recipient drops only itself; the rest still get mailed (#2764)."""
    fake = _install_transport(monkeypatch)
    with capture_logs() as logs:
        await notify_finding(_finding(email="oncall@example.com,stranger@blocked.test"))
    assert fake.calls[0]["to"] == ["oncall@example.com"]
    refused = [e for e in logs if e["event"] == "checks_notify_recipient_refused"]
    assert len(refused) == 1
    assert refused[0]["recipient"] == "stranger@blocked.test"


@pytest.mark.asyncio
async def test_finding_mail_all_recipients_refused_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finding whose every recipient is refused sends nothing, warn-logged (#2764)."""
    fake = _install_transport(monkeypatch)
    with capture_logs() as logs:
        await notify_finding(_finding(email="a@blocked.test,b@blocked.test"))
    assert fake.calls == []
    assert any(e["event"] == "checks_finding_email_no_allowed_recipients" for e in logs)


@pytest.mark.asyncio
async def test_finding_mail_without_action_omits_the_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A benign finding renders no recommended-action section."""
    fake = _install_transport(monkeypatch)

    await notify_finding(_finding(verdict="benign", action=None))

    body = fake.calls[0]["body"]
    assert "## Recommended action" not in body
    assert "## Summary" in body


@pytest.mark.asyncio
async def test_finding_mail_is_skipped_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recipient -> no send, one structured skip log."""
    fake = _install_transport(monkeypatch)

    with capture_logs() as logs:
        await notify_finding(_finding(email=None))

    assert fake.calls == []
    assert any(e["event"] == "checks_finding_email_skipped_unconfigured" for e in logs)


@pytest.mark.asyncio
async def test_finding_mail_refusal_is_logged_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``sent=False`` transport result surfaces the transport's reason code."""
    _install_transport(
        monkeypatch,
        result=MailSendResult(sent=False, reason="not_in_recipient_allowlist"),
    )

    with capture_logs() as logs:
        await notify_finding(_finding())

    failed = [e for e in logs if e["event"] == "checks_finding_email_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "not_in_recipient_allowlist"


@pytest.mark.asyncio
async def test_finding_mail_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising transport is swallowed -- the investigation seam sees nothing."""
    _install_transport(monkeypatch, exc=RuntimeError("MTA down"))

    with capture_logs() as logs:
        await notify_finding(_finding())

    failed = [e for e in logs if e["event"] == "checks_finding_email_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "unexpected_error"


@pytest.mark.asyncio
async def test_finding_subject_folds_control_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newline-bearing Dashboard name cannot inject an SMTP header."""
    fake = _install_transport(monkeypatch)

    await notify_finding(_finding(name="prod\r\nBcc: attacker@evil.test"))

    subject = fake.calls[0]["subject"]
    assert "\r" not in subject and "\n" not in subject
    assert "Bcc: attacker@evil.test" in subject, "the text survives, folded to one line"


@pytest.mark.asyncio
async def test_finding_mail_bounds_a_runaway_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ChecksFinding`` fields are model output with no schema bound; the mail bounds them."""
    fake = _install_transport(monkeypatch)
    evidence = tuple(f"line-{i}" for i in range(notify._MAX_EVIDENCE_LINES + 5))

    await notify_finding(_finding(summary="x" * 12_000, evidence=evidence))

    body = fake.calls[0]["body"]
    assert "x" * (notify._MAX_FINDING_CHARS + 1) not in body
    assert body.count("\n- line-") == notify._MAX_EVIDENCE_LINES
    assert "5 further line(s) not shown." in body


@pytest.mark.asyncio
async def test_finding_mail_has_no_state_floor_and_no_flap_window(
    monkeypatch: pytest.MonkeyPatch,
    suppression_store: _FakeSuppressionStore,
) -> None:
    """The #2732 decision, pinned: finding mail gates on ``notify_email`` alone.

    ``notify_min_state`` is a floor over a transition *edge* and a finding
    is not an edge; the investigator's fire gate is the volume control. So
    a finding about a Dashboard whose ``degraded`` incident never reached
    the default ``critical`` transition floor still mails, and repeated
    findings are not flap-suppressed -- no Valkey key is ever touched.
    """
    fake = _install_transport(monkeypatch)
    dash = uuid4()

    for _ in range(2):
        await notify_finding(
            FindingNotice(
                dashboard_id=dash,
                dashboard_name="prod-health",
                run_id=uuid4(),
                verdict="actionable",
                summary="Sensor degraded below the paging floor.",
                evidence=(),
                recommended_action=None,
                recipient="oncall@example.com",
            )
        )

    assert len(fake.calls) == 2, "no floor and no window applies to finding mail"
    assert suppression_store.set_calls == []
    assert suppression_store.deleted == []


@pytest.mark.asyncio
async def test_finding_send_is_scheduled_off_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``schedule_finding_notification`` returns before the send completes."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(*, to: list[str], subject: str, body: str) -> MailSendResult:
        started.set()
        await release.wait()
        return MailSendResult(sent=True)

    monkeypatch.setattr(notify, "send_email", _slow)

    notify.schedule_finding_notification(_finding())
    await started.wait()  # the caller already returned; the send is in flight
    release.set()
    await notify._await_pending_notifications()

    assert set() == notify._NOTIFICATIONS or all(t.done() for t in notify._NOTIFICATIONS)
