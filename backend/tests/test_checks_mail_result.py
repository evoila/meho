# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the stored result of a Dashboard's alert mail.

A failed alert mail must show up on the Dashboard row and in the
``checks_alert_mail_total`` counter, not only in the log. The transport is a
recording fake; the database is the SQLite engine from ``tests.conftest``.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest

import meho_backplane.checks.notify as notify
from meho_backplane.checks import mail_result
from meho_backplane.checks.mail_result import ALERT_MAIL_TOTAL, record_mail_result
from meho_backplane.checks.notify import DashboardNotice, notify_dashboard_transition
from meho_backplane.connectors.mail.transport import MailSendResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import CheckDashboard, Tenant
from meho_backplane.settings import get_settings

_TENANT = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://kc.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SENSOR_RUNNER_ENABLED", "false")
    monkeypatch.setenv("MAIL_RECIPIENT_ALLOWLIST", "example.com")
    # No flap window, so no Valkey call is made.
    monkeypatch.setenv("CHECKS_NOTIFY_SUPPRESSION_WINDOW_MINUTES", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _count(result: str) -> float:
    return ALERT_MAIL_TOTAL.labels(result=result)._value.get()


class _Transport:
    def __init__(self, result: MailSendResult | None = None, exc: Exception | None = None) -> None:
        self._result = result or MailSendResult(sent=True)
        self._exc = exc

    async def __call__(self, **_: object) -> MailSendResult:
        if self._exc is not None:
            raise self._exc
        return self._result


async def _seed_dashboard() -> UUID:
    dashboard_id = uuid4()
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="tenant-mail-result", name="Tenant Mail Result"))
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=_TENANT,
                name=f"dash-{dashboard_id}",
                notify_email="oncall@example.com",
                created_by_sub="op-admin",
            )
        )
        await session.commit()
    return dashboard_id


async def _row(dashboard_id: UUID) -> CheckDashboard:
    async with get_sessionmaker()() as session:
        row = await session.get(CheckDashboard, dashboard_id)
        assert row is not None
        return row


def _notice(dashboard_id: UUID, email: str = "oncall@example.com") -> DashboardNotice:
    return DashboardNotice(
        tenant_id=_TENANT,
        dashboard_id=dashboard_id,
        name="prod-health",
        previous_state="ok",
        current_state="critical",
        notify_email=email,
        notify_min_state="critical",
        members=(),
    )


@pytest.mark.asyncio
async def test_new_dashboard_has_no_mail_result() -> None:
    row = await _row(await _seed_dashboard())
    assert (row.last_mail_at, row.last_mail_sent, row.last_mail_reason) == (None, None, None)


@pytest.mark.asyncio
async def test_sent_mail_is_stored_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport())
    dashboard_id = await _seed_dashboard()
    before = _count("sent")
    await notify_dashboard_transition(_notice(dashboard_id))
    row = await _row(dashboard_id)
    assert row.last_mail_at is not None
    assert row.last_mail_sent is True
    assert row.last_mail_reason is None
    assert _count("sent") == before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["smtp_connect_error", "smtp_auth_error", "smtp_unconfigured"])
async def test_refused_mail_keeps_its_reason(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport(MailSendResult(sent=False, reason=reason)))
    dashboard_id = await _seed_dashboard()
    before = _count(reason)
    await notify_dashboard_transition(_notice(dashboard_id))
    row = await _row(dashboard_id)
    assert row.last_mail_at is not None
    assert row.last_mail_sent is False
    assert row.last_mail_reason == reason
    assert _count(reason) == before + 1


@pytest.mark.asyncio
async def test_raising_transport_is_recorded_as_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport(exc=RuntimeError("boom")))
    dashboard_id = await _seed_dashboard()
    await notify_dashboard_transition(_notice(dashboard_id))
    row = await _row(dashboard_id)
    assert (row.last_mail_sent, row.last_mail_reason) == (False, "unexpected_error")


@pytest.mark.asyncio
async def test_no_allowed_recipient_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport())
    dashboard_id = await _seed_dashboard()
    await notify_dashboard_transition(_notice(dashboard_id, email="oncall@blocked.test"))
    row = await _row(dashboard_id)
    assert (row.last_mail_sent, row.last_mail_reason) == (False, "no_allowed_recipients")


@pytest.mark.asyncio
async def test_later_success_clears_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    dashboard_id = await _seed_dashboard()
    monkeypatch.setattr(
        notify,
        "send_email",
        _Transport(MailSendResult(sent=False, reason="smtp_connect_error")),
    )
    await notify_dashboard_transition(_notice(dashboard_id))
    monkeypatch.setattr(notify, "send_email", _Transport())
    await notify_dashboard_transition(_notice(dashboard_id))
    row = await _row(dashboard_id)
    assert (row.last_mail_sent, row.last_mail_reason) == (True, None)


@pytest.mark.asyncio
async def test_skipped_mail_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport())
    dashboard_id = await _seed_dashboard()
    await notify_dashboard_transition(
        DashboardNotice(
            tenant_id=_TENANT,
            dashboard_id=dashboard_id,
            name="x",
            previous_state="ok",
            current_state="degraded",
            notify_email="oncall@example.com",
            notify_min_state="critical",
            members=(),
        )
    )
    assert (await _row(dashboard_id)).last_mail_at is None


@pytest.mark.asyncio
async def test_other_tenant_row_is_not_touched() -> None:
    dashboard_id = await _seed_dashboard()
    await record_mail_result(tenant_id=uuid4(), dashboard_id=dashboard_id, sent=False, reason="x")
    assert (await _row(dashboard_id)).last_mail_at is None


@pytest.mark.asyncio
async def test_database_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(mail_result, "get_sessionmaker", _broken)
    before = _count("smtp_error")
    await record_mail_result(
        tenant_id=_TENANT, dashboard_id=uuid4(), sent=False, reason="smtp_error"
    )
    assert _count("smtp_error") == before + 1
