# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the stored result of a Dashboard's alert mail.

A failed alert mail must show up on the Dashboard row, in the audit log and
in the ``checks_alert_mail_total`` counter, not only in the log. The transport is a
recording fake; the database is the SQLite engine from ``tests.conftest``.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest

import meho_backplane.checks.notify as notify
from meho_backplane.audit_query import AuditQueryFilters, query_audit
from meho_backplane.audit_query.schemas import AuditEntry
from meho_backplane.checks import mail_result
from meho_backplane.checks.mail_result import (
    ALERT_MAIL_OP_ID,
    ALERT_MAIL_TOTAL,
    AlertMail,
    record_mail_result,
)
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


def _notice(dashboard_id: UUID, email: str = "oncall@example.com") -> notify.DashboardNotice:
    return notify.DashboardNotice(
        tenant_id=_TENANT,
        dashboard_id=dashboard_id,
        name="prod-health",
        previous_state="ok",
        current_state="critical",
        notify_email=email,
        notify_min_state="critical",
        members=(),
    )


_MAIL = AlertMail(
    dashboard_name="x", previous_state="ok", current_state="critical", subject="s", to=()
)


async def _audit_rows(dashboard_id: UUID, **filters: str) -> list[AuditEntry]:
    async with get_sessionmaker()() as session:
        result = await query_audit(AuditQueryFilters(**filters), tenant_id=_TENANT, session=session)
    return [r for r in result.rows if r.payload.get("dashboard_id") == str(dashboard_id)]


@pytest.mark.asyncio
async def test_new_dashboard_has_no_mail_result() -> None:
    row = await _row(await _seed_dashboard())
    assert (row.last_mail_at, row.last_mail_sent, row.last_mail_reason) == (None, None, None)


@pytest.mark.asyncio
async def test_sent_mail_is_stored_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport())
    dashboard_id = await _seed_dashboard()
    before = _count("sent")
    await notify.notify_dashboard_transition(_notice(dashboard_id))
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
    await notify.notify_dashboard_transition(_notice(dashboard_id))
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
    await notify.notify_dashboard_transition(_notice(dashboard_id))
    row = await _row(dashboard_id)
    assert (row.last_mail_sent, row.last_mail_reason) == (False, "unexpected_error")


@pytest.mark.asyncio
async def test_no_allowed_recipient_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport())
    dashboard_id = await _seed_dashboard()
    await notify.notify_dashboard_transition(_notice(dashboard_id, email="oncall@blocked.test"))
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
    await notify.notify_dashboard_transition(_notice(dashboard_id))
    monkeypatch.setattr(notify, "send_email", _Transport())
    await notify.notify_dashboard_transition(_notice(dashboard_id))
    row = await _row(dashboard_id)
    assert (row.last_mail_sent, row.last_mail_reason) == (True, None)


@pytest.mark.asyncio
async def test_skipped_mail_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport())
    dashboard_id = await _seed_dashboard()
    await notify.notify_dashboard_transition(
        notify.DashboardNotice(
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
    await record_mail_result(
        tenant_id=uuid4(), dashboard_id=dashboard_id, mail=_MAIL, sent=False, reason="x"
    )
    assert (await _row(dashboard_id)).last_mail_at is None


@pytest.mark.asyncio
async def test_database_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(mail_result, "get_sessionmaker", _broken)
    before = _count("smtp_error")
    await record_mail_result(
        tenant_id=_TENANT, dashboard_id=uuid4(), mail=_MAIL, sent=False, reason="smtp_error"
    )
    assert _count("smtp_error") == before + 1


@pytest.mark.asyncio
async def test_sent_mail_leaves_an_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport())
    dashboard_id = await _seed_dashboard()
    await notify.notify_dashboard_transition(_notice(dashboard_id))
    (entry,) = await _audit_rows(dashboard_id, op_id="checks*")
    assert entry.op_id == ALERT_MAIL_OP_ID
    assert entry.principal_sub == "system:checks-notify"
    assert entry.event_class == "operation_execution"
    assert entry.result_status == "ok"
    assert entry.ts is not None
    assert entry.payload["dashboard_name"] == "prod-health"
    assert (entry.payload["previous_state"], entry.payload["current_state"]) == ("ok", "critical")
    assert (entry.payload["sent"], entry.payload["result"]) == (True, "sent")
    assert entry.payload["to"] == ["oncall@example.com"]
    assert entry.payload["refused"] == []
    assert entry.payload["subject"] == "[MEHO] all clear - prod-health: ok -> critical"
    assert entry.payload["sensor"] is None
    assert "body" not in entry.payload
    assert [e.id for e in await _audit_rows(dashboard_id, op_id="*mail*")] == [entry.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["smtp_connect_error", "smtp_auth_error"])
async def test_failed_mail_audit_row_is_an_error(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport(MailSendResult(sent=False, reason=reason)))
    dashboard_id = await _seed_dashboard()
    await notify.notify_dashboard_transition(_notice(dashboard_id))
    (entry,) = await _audit_rows(dashboard_id, result_status="error")
    assert (entry.payload["sent"], entry.payload["result"]) == (False, reason)
    assert entry.payload["to"] == ["oncall@example.com"]
    assert await _audit_rows(dashboard_id, result_status="ok") == []


@pytest.mark.asyncio
async def test_raising_transport_leaves_an_error_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport(exc=RuntimeError("boom")))
    dashboard_id = await _seed_dashboard()
    await notify.notify_dashboard_transition(_notice(dashboard_id))
    (entry,) = await _audit_rows(dashboard_id, op_id=ALERT_MAIL_OP_ID)
    assert (entry.result_status, entry.payload["result"]) == ("error", "unexpected_error")
    assert "boom" not in str(entry.payload)


@pytest.mark.asyncio
async def test_no_allowed_recipient_audit_row_lists_the_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport())
    dashboard_id = await _seed_dashboard()
    await notify.notify_dashboard_transition(_notice(dashboard_id, email="oncall@blocked.test"))
    (entry,) = await _audit_rows(dashboard_id, op_id=ALERT_MAIL_OP_ID)
    assert entry.payload["result"] == "no_allowed_recipients"
    assert (entry.payload["to"], entry.payload["refused"]) == ([], ["oncall@blocked.test"])


@pytest.mark.asyncio
async def test_partly_refused_recipients_are_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport())
    dashboard_id = await _seed_dashboard()
    email = "a@example.com, b@blocked.test,,a2@example.com"
    await notify.notify_dashboard_transition(_notice(dashboard_id, email=email))
    (entry,) = await _audit_rows(dashboard_id, op_id=ALERT_MAIL_OP_ID)
    assert entry.payload["to"] == ["a@example.com", "a2@example.com"]
    assert entry.payload["refused"] == ["b@blocked.test"]


@pytest.mark.asyncio
async def test_new_problem_mail_audit_row_names_the_sensor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport())
    dashboard_id = await _seed_dashboard()
    sensor_id = uuid4()
    change = notify.MemberChange(
        sensor_id=sensor_id, name="disk", previous_state="ok", current_state="critical"
    )
    notice = _notice(dashboard_id)
    notice = notify.DashboardNotice(
        tenant_id=notice.tenant_id,
        dashboard_id=dashboard_id,
        name=notice.name,
        previous_state="critical",
        current_state="critical",
        notify_email=notice.notify_email,
        notify_min_state="critical",
        members=(),
        member_change=change,
    )
    await notify.notify_dashboard_transition(notice)
    (entry,) = await _audit_rows(dashboard_id, op_id=ALERT_MAIL_OP_ID)
    assert entry.payload["sensor"] == {
        "id": str(sensor_id),
        "name": "disk",
        "previous_state": "ok",
        "current_state": "critical",
        "acknowledgement_ended": False,
    }
    assert (entry.payload["previous_state"], entry.payload["current_state"]) == (
        "critical",
        "critical",
    )


@pytest.mark.asyncio
async def test_each_attempt_leaves_its_own_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    dashboard_id = await _seed_dashboard()
    monkeypatch.setattr(
        notify, "send_email", _Transport(MailSendResult(sent=False, reason="smtp_error"))
    )
    await notify.notify_dashboard_transition(_notice(dashboard_id))
    monkeypatch.setattr(notify, "send_email", _Transport())
    await notify.notify_dashboard_transition(_notice(dashboard_id))
    rows = await _audit_rows(dashboard_id, op_id=ALERT_MAIL_OP_ID)
    assert sorted(r.payload["result"] for r in rows) == ["sent", "smtp_error"]


@pytest.mark.asyncio
async def test_skipped_mail_leaves_no_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport())
    dashboard_id = await _seed_dashboard()
    notice = _notice(dashboard_id)
    await notify.notify_dashboard_transition(
        notify.DashboardNotice(
            tenant_id=_TENANT,
            dashboard_id=dashboard_id,
            name=notice.name,
            previous_state="ok",
            current_state="degraded",
            notify_email=notice.notify_email,
            notify_min_state="critical",
            members=(),
        )
    )
    assert await _audit_rows(dashboard_id) == []


@pytest.mark.asyncio
async def test_audit_row_is_not_visible_to_another_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify, "send_email", _Transport())
    dashboard_id = await _seed_dashboard()
    await notify.notify_dashboard_transition(_notice(dashboard_id))
    async with get_sessionmaker()() as session:
        other = await query_audit(
            AuditQueryFilters(op_id=ALERT_MAIL_OP_ID), tenant_id=uuid4(), session=session
        )
    assert other.rows == []
