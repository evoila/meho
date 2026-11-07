# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Acknowledge a Sensor finding: who, until when, and why (#4128).

What these tests check:

* REST create: who is recorded, the request's audit row, and every refusal
  (no end time, no time zone, past or too far, bad reason, blank note, green
  or never-checked Sensor, a second open one, another tenant, a read-only
  role, a machine token).
* The end: at the end time (``expired``) and early when the state gets worse
  (``state_worse``), each with an audit row; a state that is not worse keeps
  it.
* The advisory says "acknowledged" once, then nothing; after the end time the
  warning comes back.
* The warning mail is not sent while acknowledged; it comes back after the
  end time and after an early end; the all-clear mail is still sent.
* The Dashboard detail (REST and console) shows who, until when and why next
  to the real state.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from structlog.testing import capture_logs

import meho_backplane.checks.notify as notify
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import get_broadcast_client, reset_broadcast_client_for_testing
from meho_backplane.checks.acknowledgement import (
    ACK_END_PATH,
    ACK_EXPIRE_PATH,
    SensorAcknowledgementCreate,
    acknowledge_sensor,
)
from meho_backplane.checks.advisory import CHECKS_ADVISORY_EXTRAS_KEY, build_checks_alert_advisory
from meho_backplane.checks.dashboard_service import CheckDashboardAdminService
from meho_backplane.checks.notify import DashboardNotice, MemberChange, NotifyMember
from meho_backplane.checks.repository import record_sensor_result
from meho_backplane.connectors.mail.transport import MailSendResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    SENSOR_ACK_END_REASONS,
    SENSOR_ACK_REASONS,
    SENSOR_ACK_STATES,
    AuditLog,
    CheckDashboard,
    CheckDashboardSensor,
    Sensor,
    SensorAcknowledgement,
    Tenant,
)
from meho_backplane.main import app
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.checks.views import project_detail

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._vault_fakes import install_fake_vault

_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SENSOR_RUNNER_ENABLED", "false")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("CHECKS_ALERT_ADVISORY_WINDOW_MINUTES", "30")
    monkeypatch.setenv("MAIL_RECIPIENT_ALLOWLIST", "example.com")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_broadcast_client_for_testing()
    yield
    reset_broadcast_client_for_testing()
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _headers(
    key: Any,
    *,
    sub: str = "op-alice",
    role: TenantRole = TenantRole.OPERATOR,
    tenant_id: UUID = _TENANT_A,
    principal_kind: str | None = None,
) -> dict[str, str]:
    token = mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=str(tenant_id),
        principal_kind=principal_kind,
    )
    return {"Authorization": f"Bearer {token}"}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _seed_tenant(tenant_id: UUID) -> None:
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, tenant_id) is None:
            slug = str(tenant_id)[:8]
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
            await session.commit()


async def _seed_sensor(
    *,
    name: str = "disk-space",
    tenant_id: UUID = _TENANT_A,
    last_state: str = "degraded",
    evaluated: bool = True,
) -> UUID:
    await _seed_tenant(tenant_id)
    sensor_id = uuid4()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=tenant_id,
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
                last_evidence={"observed": 7},
                last_evaluated_at=now - timedelta(seconds=30) if evaluated else None,
                state_since=now - timedelta(hours=1),
                identity_sub="__sensor__",
                created_by_sub="op-admin",
            )
        )
        await session.commit()
    return sensor_id


async def _seed_dashboard(
    sensor_states: dict[UUID, str],
    *,
    state: str = "critical",
    name: str = "estate",
) -> UUID:
    dashboard_id = uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=_TENANT_A,
                name=name,
                created_by_sub="op-admin",
                last_rollup_state=state,
                notify_email="oncall@example.com",
                notify_min_state="degraded",
            )
        )
        for sensor_id, member_state in sensor_states.items():
            session.add(
                CheckDashboardSensor(
                    dashboard_id=dashboard_id,
                    sensor_id=sensor_id,
                    last_member_state=member_state,
                )
            )
        await session.commit()
    return dashboard_id


async def _ack(
    sensor_id: UUID,
    *,
    hours: float = 4,
    reason: str = "fix_planned",
    note: str = "Disk swap on Friday",
) -> None:
    created = await acknowledge_sensor(
        tenant_id=_TENANT_A,
        sensor_id=sensor_id,
        created_by_sub="op-alice",
        payload=SensorAcknowledgementCreate(
            expires_at=datetime.now(UTC) + timedelta(hours=hours),
            reason=reason,  # type: ignore[arg-type]
            note=note,
        ),
    )
    assert created is not None


async def _move_end_time(sensor_id: UUID, expires_at: datetime) -> None:
    """Move the open acknowledgement's end time, as if the time had passed."""
    async with get_sessionmaker()() as session:
        row = await session.scalar(
            select(SensorAcknowledgement).where(SensorAcknowledgement.sensor_id == sensor_id)
        )
        assert row is not None
        row.expires_at = expires_at
        await session.commit()


async def _acks(sensor_id: UUID) -> list[SensorAcknowledgement]:
    async with get_sessionmaker()() as session:
        return list(
            (
                await session.scalars(
                    select(SensorAcknowledgement).where(
                        SensorAcknowledgement.sensor_id == sensor_id
                    )
                )
            ).all()
        )


async def _audit(path: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        return list((await session.scalars(select(AuditLog).where(AuditLog.path == path))).all())


async def _record(sensor_id: UUID, state: str, *, at: datetime | None = None) -> None:
    async with get_sessionmaker()() as session:
        await record_sensor_result(
            session,
            sensor_id=sensor_id,
            state=state,  # type: ignore[arg-type]
            value=1,
            evidence={},
            evaluated_at=at or datetime.now(UTC),
        )
        await session.commit()


def _future(hours: float = 4) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------------------
# REST create and refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acknowledge_records_who_until_when_why_and_audits(client: TestClient) -> None:
    sensor_id = await _seed_sensor(last_state="degraded")
    key = make_rsa_keypair("kid-ack")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/sensors/{sensor_id}/acknowledgement",
            json={"expires_at": _future(), "reason": "accepted_risk", "note": "  Known, low  "},
            headers=_headers(key),
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created_by_sub"] == "op-alice"
    assert body["reason"] == "accepted_risk"
    assert body["note"] == "Known, low"
    assert body["acked_state"] == "degraded"
    assert body["ended_at"] is None

    rows = await _audit(f"/api/v1/sensors/{sensor_id}/acknowledgement")
    assert len(rows) == 1
    payload = dict(rows[0].payload or {})
    assert payload["op_id"] == "sensor.acknowledge"
    assert payload["after"]["reason"] == "accepted_risk"
    assert payload["after"]["created_by_sub"] == "op-alice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "detail"),
    [
        pytest.param({"reason": "fix_planned", "note": "x"}, None, id="no-end-time"),
        pytest.param(
            {"expires_at": "2099-01-01T10:00:00", "reason": "fix_planned", "note": "x"},
            None,
            id="no-time-zone",
        ),
        pytest.param(
            {"expires_at": "2020-01-01T10:00:00Z", "reason": "fix_planned", "note": "x"},
            "sensor_ack_invalid_end_time",
            id="in-the-past",
        ),
        pytest.param(
            {"expires_at": "LATER", "reason": "fix_planned", "note": "x"},
            None,
            id="not-a-time",
        ),
        pytest.param(
            {"expires_at": "FAR", "reason": "fix_planned", "note": "x"},
            "sensor_ack_invalid_end_time",
            id="more-than-90-days",
        ),
        pytest.param(
            {"expires_at": "SOON", "reason": "because", "note": "x"}, None, id="unknown-reason"
        ),
        pytest.param({"expires_at": "SOON", "reason": "false_alarm"}, None, id="no-note"),
        pytest.param(
            {"expires_at": "SOON", "reason": "false_alarm", "note": "   "}, None, id="blank-note"
        ),
        pytest.param(
            {"expires_at": "SOON", "reason": "false_alarm", "note": "x", "paused": True},
            None,
            id="unknown-field",
        ),
    ],
)
async def test_acknowledge_refuses_a_bad_body(
    client: TestClient, body: dict[str, Any], detail: str | None
) -> None:
    sensor_id = await _seed_sensor()
    body = dict(body)
    if body.get("expires_at") == "SOON":
        body["expires_at"] = _future()
    elif body.get("expires_at") == "FAR":
        body["expires_at"] = _future(hours=24 * 91)
    key = make_rsa_keypair("kid-ack-bad")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/sensors/{sensor_id}/acknowledgement", json=body, headers=_headers(key)
        )
    assert resp.status_code == 422, resp.text
    if detail is not None:
        assert resp.json()["detail"] == detail
    assert await _acks(sensor_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("last_state", "evaluated"),
    [("ok", True), ("unknown", False)],
    ids=["green", "never-checked"],
)
async def test_acknowledge_refuses_a_sensor_that_is_not_failing(
    client: TestClient, last_state: str, evaluated: bool
) -> None:
    sensor_id = await _seed_sensor(last_state=last_state, evaluated=evaluated)
    key = make_rsa_keypair("kid-ack-green")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/sensors/{sensor_id}/acknowledgement",
            json={"expires_at": _future(), "reason": "false_alarm", "note": "x"},
            headers=_headers(key),
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "sensor_ack_not_failing"


@pytest.mark.asyncio
async def test_unknown_state_after_a_check_can_be_acknowledged(client: TestClient) -> None:
    sensor_id = await _seed_sensor(last_state="unknown")
    key = make_rsa_keypair("kid-ack-unknown")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/sensors/{sensor_id}/acknowledgement",
            json={"expires_at": _future(), "reason": "false_alarm", "note": "probe host down"},
            headers=_headers(key),
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["acked_state"] == "unknown"


@pytest.mark.asyncio
async def test_second_open_acknowledgement_is_409_and_an_expired_one_does_not_block(
    client: TestClient,
) -> None:
    sensor_id = await _seed_sensor()
    key = make_rsa_keypair("kid-ack-twice")
    body = {"expires_at": _future(), "reason": "fix_planned", "note": "x"}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        assert (
            client.post(
                f"/api/v1/sensors/{sensor_id}/acknowledgement", json=body, headers=_headers(key)
            ).status_code
            == 201
        )
        again = client.post(
            f"/api/v1/sensors/{sensor_id}/acknowledgement", json=body, headers=_headers(key)
        )
        assert again.status_code == 409
        assert again.json()["detail"] == "sensor_ack_already_open"

        await _move_end_time(sensor_id, datetime.now(UTC) - timedelta(minutes=1))
        renewed = client.post(
            f"/api/v1/sensors/{sensor_id}/acknowledgement", json=body, headers=_headers(key)
        )
    assert renewed.status_code == 201, renewed.text
    rows = await _acks(sensor_id)
    assert sorted(str(r.end_reason) for r in rows) == ["None", "expired"]
    assert len(await _audit(ACK_EXPIRE_PATH)) == 1


@pytest.mark.asyncio
async def test_acknowledge_other_tenant_is_404(client: TestClient) -> None:
    sensor_id = await _seed_sensor(tenant_id=_TENANT_B)
    key = make_rsa_keypair("kid-ack-tenant")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/sensors/{sensor_id}/acknowledgement",
            json={"expires_at": _future(), "reason": "fix_planned", "note": "x"},
            headers=_headers(key, tenant_id=_TENANT_A),
        )
        missing = client.post(
            f"/api/v1/sensors/{uuid4()}/acknowledgement",
            json={"expires_at": _future(), "reason": "fix_planned", "note": "x"},
            headers=_headers(key, tenant_id=_TENANT_A),
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "sensor_not_found"
    assert missing.status_code == 404
    assert await _acks(sensor_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "principal_kind"),
    [
        (TenantRole.READ_ONLY, None),
        (TenantRole.OPERATOR, "agent"),
        (TenantRole.OPERATOR, "service"),
    ],
    ids=["read-only", "agent", "service"],
)
async def test_acknowledge_needs_a_person_with_operator_role(
    client: TestClient, role: TenantRole, principal_kind: str | None
) -> None:
    sensor_id = await _seed_sensor()
    key = make_rsa_keypair("kid-ack-role")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/sensors/{sensor_id}/acknowledgement",
            json={"expires_at": _future(), "reason": "fix_planned", "note": "x"},
            headers=_headers(key, role=role, principal_kind=principal_kind),
        )
    assert resp.status_code == 403, resp.text
    assert await _acks(sensor_id) == []


# ---------------------------------------------------------------------------
# The end: expiry and early end, each with an audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expiry_ends_the_acknowledgement_with_an_audit_row() -> None:
    sensor_id = await _seed_sensor(last_state="degraded")
    await _ack(sensor_id)
    end = datetime.now(UTC) + timedelta(seconds=5)
    await _move_end_time(sensor_id, end)

    await _record(sensor_id, "degraded")  # before the end time: still open
    assert (await _acks(sensor_id))[0].ended_at is None

    await _record(sensor_id, "degraded", at=end + timedelta(seconds=1))
    (row,) = await _acks(sensor_id)
    assert row.end_reason == "expired"
    assert _utc(row.ended_at) == end  # type: ignore[arg-type]
    (audit,) = await _audit(ACK_EXPIRE_PATH)
    assert audit.tenant_id == _TENANT_A
    assert audit.method == "INTERNAL"
    assert audit.payload["end_reason"] == "expired"
    assert audit.payload["acknowledged_by"] == "op-alice"
    assert audit.payload["sensor_id"] == str(sensor_id)


@pytest.mark.asyncio
async def test_worse_state_ends_the_acknowledgement_early_with_an_audit_row() -> None:
    sensor_id = await _seed_sensor(last_state="degraded")
    await _ack(sensor_id)

    await _record(sensor_id, "critical")
    (row,) = await _acks(sensor_id)
    assert row.end_reason == "state_worse"
    assert row.ended_at is not None
    (audit,) = await _audit(ACK_END_PATH)
    assert audit.payload["acked_state"] == "degraded"
    assert audit.payload["state"] == "critical"


@pytest.mark.asyncio
@pytest.mark.parametrize("new_state", ["degraded", "unknown", "ok"])
async def test_a_state_that_is_not_worse_keeps_the_acknowledgement(new_state: str) -> None:
    sensor_id = await _seed_sensor(last_state="degraded")
    await _ack(sensor_id)
    await _record(sensor_id, new_state)
    (row,) = await _acks(sensor_id)
    assert row.ended_at is None
    assert await _audit(ACK_END_PATH) == []


# ---------------------------------------------------------------------------
# Advisory
# ---------------------------------------------------------------------------


class _FakeNxStore:
    """Pipelined ``SET NX`` double (same contract as ``test_checks_advisory``)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self._staged: list[str] = []

    def pipeline(self, transaction: bool = True) -> _FakeNxStore:
        self._staged = []
        return self

    async def __aenter__(self) -> _FakeNxStore:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None) -> Any:
        self._staged.append(name)
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for name in self._staged:
            results.append(None if name in self.store else True)
            self.store.setdefault(name, "1")
        self._staged = []
        return results


def _operator() -> Operator:
    return Operator(
        sub="user-b",
        name="user-b",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


@pytest.mark.asyncio
async def test_advisory_says_acknowledged_once_and_the_warning_returns_after_expiry() -> None:
    sensor_id = await _seed_sensor(last_state="critical")
    dashboard_id = await _seed_dashboard({sensor_id: "critical"})
    await _ack(sensor_id)
    store = _FakeNxStore()
    with patch.object(get_broadcast_client(), "pipeline", new=store.pipeline):
        first = await build_checks_alert_advisory(_operator())
        second = await build_checks_alert_advisory(_operator())
        await _move_end_time(sensor_id, datetime.now(UTC) - timedelta(seconds=1))
        after_expiry = await build_checks_alert_advisory(_operator())

    (entry,) = first[CHECKS_ADVISORY_EXTRAS_KEY]
    assert entry["dashboard_id"] == str(dashboard_id)
    assert entry["state"] == "critical"
    assert entry["acknowledged"] is True
    assert "acknowledged_until" in entry
    assert second == {}
    assert after_expiry == {
        CHECKS_ADVISORY_EXTRAS_KEY: [
            {"dashboard_id": str(dashboard_id), "name": "estate", "state": "critical"}
        ]
    }


@pytest.mark.asyncio
async def test_advisory_warns_when_one_non_green_member_is_not_acknowledged() -> None:
    acked = await _seed_sensor(name="a", last_state="critical")
    other = await _seed_sensor(name="b", last_state="degraded")
    healthy = await _seed_sensor(name="c", last_state="ok")
    await _seed_dashboard({acked: "critical", other: "degraded", healthy: "ok"})
    await _ack(acked)
    store = _FakeNxStore()
    with patch.object(get_broadcast_client(), "pipeline", new=store.pipeline):
        advisory = await build_checks_alert_advisory(_operator())
    (entry,) = advisory[CHECKS_ADVISORY_EXTRAS_KEY]
    assert "acknowledged" not in entry


@pytest.mark.asyncio
async def test_advisory_warns_when_no_member_state_is_on_record() -> None:
    sensor_id = await _seed_sensor(last_state="critical")
    await _seed_dashboard({sensor_id: None})  # type: ignore[dict-item]
    await _ack(sensor_id)
    store = _FakeNxStore()
    with patch.object(get_broadcast_client(), "pipeline", new=store.pipeline):
        advisory = await build_checks_alert_advisory(_operator())
    (entry,) = advisory[CHECKS_ADVISORY_EXTRAS_KEY]
    assert "acknowledged" not in entry


# ---------------------------------------------------------------------------
# Warning mails
# ---------------------------------------------------------------------------


class _Transport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, *, to: list[str], subject: str, body: str) -> MailSendResult:
        self.calls.append(subject)
        return MailSendResult(sent=True)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> _Transport:
    fake = _Transport()
    monkeypatch.setattr(notify, "send_email", fake)
    # Window 0: no Valkey call, so only the acknowledgement decides.
    monkeypatch.setenv("CHECKS_NOTIFY_SUPPRESSION_WINDOW_MINUTES", "0")
    get_settings.cache_clear()
    return fake


def _notice(
    sensor_id: UUID,
    dashboard_id: UUID,
    *,
    previous: str = "ok",
    current: str = "critical",
    member_change: MemberChange | None = None,
) -> DashboardNotice:
    return DashboardNotice(
        tenant_id=_TENANT_A,
        dashboard_id=dashboard_id,
        name="estate",
        previous_state=previous,
        current_state=current,
        notify_email="oncall@example.com",
        notify_min_state="degraded",
        members=(
            NotifyMember(
                sensor_id=sensor_id,
                name="disk-space",
                effective_state=current,
                last_value=7,
                last_evidence={},
            ),
        )
        if current != "ok"
        else (),
        member_change=member_change,
    )


@pytest.mark.asyncio
async def test_warning_mail_is_not_sent_while_acknowledged_and_returns_after_expiry(
    transport: _Transport,
) -> None:
    sensor_id = await _seed_sensor(last_state="critical")
    dashboard_id = await _seed_dashboard({sensor_id: "critical"})
    await _ack(sensor_id)

    with capture_logs() as logs:
        await notify.notify_dashboard_transition(_notice(sensor_id, dashboard_id))
    assert transport.calls == []
    assert any(e["event"] == "checks_notify_skipped_acknowledged" for e in logs)

    change = MemberChange(
        sensor_id=sensor_id, name="disk-space", previous_state="ok", current_state="critical"
    )
    await notify.notify_dashboard_transition(
        _notice(sensor_id, dashboard_id, previous="critical", member_change=change)
    )
    assert transport.calls == []

    await _move_end_time(sensor_id, datetime.now(UTC) - timedelta(seconds=1))
    await notify.notify_dashboard_transition(_notice(sensor_id, dashboard_id))
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_warning_mail_returns_after_the_state_gets_worse(transport: _Transport) -> None:
    sensor_id = await _seed_sensor(last_state="degraded")
    dashboard_id = await _seed_dashboard({sensor_id: "degraded"}, state="degraded")
    await _ack(sensor_id)
    await notify.notify_dashboard_transition(
        _notice(sensor_id, dashboard_id, previous="ok", current="degraded")
    )
    assert transport.calls == []

    await _record(sensor_id, "critical")
    await notify.notify_dashboard_transition(
        _notice(sensor_id, dashboard_id, previous="degraded", current="critical")
    )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_all_clear_mail_is_still_sent_while_acknowledged(transport: _Transport) -> None:
    sensor_id = await _seed_sensor(last_state="critical")
    dashboard_id = await _seed_dashboard({sensor_id: "critical"})
    await _ack(sensor_id)
    await notify.notify_dashboard_transition(
        _notice(sensor_id, dashboard_id, previous="critical", current="ok")
    )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_warning_mail_is_sent_when_the_acknowledgement_read_fails(
    transport: _Transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*_a: object, **_k: object) -> bool:
        raise RuntimeError("db down")

    monkeypatch.setattr(notify, "all_acknowledged", _boom)
    await notify.notify_dashboard_transition(_notice(uuid4(), uuid4()))
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# Dashboard detail: REST and console
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_detail_shows_who_until_when_and_why_next_to_the_real_state() -> None:
    acked = await _seed_sensor(name="a", last_state="critical")
    plain = await _seed_sensor(name="b", last_state="degraded")
    dashboard_id = await _seed_dashboard({acked: "critical", plain: "degraded"})
    await _ack(acked, reason="false_alarm", note="Probe host is being moved")

    detail = await CheckDashboardAdminService().get(_TENANT_A, dashboard_id)
    assert detail is not None
    by_name = {m.name: m for m in detail.members}
    assert by_name["a"].raw_state == "critical"
    assert by_name["a"].effective_state == "critical"
    ack = by_name["a"].acknowledgement
    assert ack is not None
    assert ack.created_by_sub == "op-alice"
    assert ack.reason == "false_alarm"
    assert ack.note == "Probe host is being moved"
    assert ack.expires_at > datetime.now(UTC)
    assert by_name["b"].acknowledgement is None
    assert detail.state == "critical"

    view = project_detail(detail)
    shown = {m["name"]: m for m in view["members"]}  # type: ignore[attr-defined]
    assert shown["a"]["acknowledgement"]["by"] == "op-alice"
    assert shown["a"]["acknowledgement"]["reason"] == "false alarm"
    assert shown["a"]["acknowledgement"]["until"] == ack.expires_at
    assert shown["b"]["acknowledgement"] is None

    await _move_end_time(acked, datetime.now(UTC) - timedelta(seconds=1))
    expired = await CheckDashboardAdminService().get(_TENANT_A, dashboard_id)
    assert expired is not None
    assert all(m.acknowledgement is None for m in expired.members)


@pytest.mark.asyncio
async def test_dashboard_detail_rest_carries_the_acknowledgement(client: TestClient) -> None:
    sensor_id = await _seed_sensor(last_state="critical")
    dashboard_id = await _seed_dashboard({sensor_id: "critical"})
    await _ack(sensor_id)
    key = make_rsa_keypair("kid-ack-dash")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(f"/api/v1/checks/dashboards/{dashboard_id}", headers=_headers(key))
    assert resp.status_code == 200, resp.text
    (member,) = resp.json()["members"]
    assert member["raw_state"] == "critical"
    assert member["acknowledgement"]["created_by_sub"] == "op-alice"
    assert member["acknowledgement"]["reason"] == "fix_planned"


# ---------------------------------------------------------------------------
# Migration vocabulary drift guard
# ---------------------------------------------------------------------------


def test_migration_vocabularies_match_the_model() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0142_create_sensor_acknowledgements.py"
    )
    spec = importlib.util.spec_from_file_location("_mig_0142", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._ACK_STATES == SENSOR_ACK_STATES
    assert module._ACK_REASONS == SENSOR_ACK_REASONS
    assert module._ACK_END_REASONS == SENSOR_ACK_END_REASONS
