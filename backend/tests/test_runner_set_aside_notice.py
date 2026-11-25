# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What MEHO keeps of a set-aside notice, and which checks it leaves alone.

With ``RUNNER_CHECKS_ENABLED`` on:

* MEHO keeps a notice's ``result_uid`` and ``code`` only as 1 to 64 lowercase
  letters, digits and underscores, and its ``status`` only as 400 to 499. A
  value in another form never reaches the check's reading, the Dashboard mail
  or the investigator, and the post still answers 200,
* a notice without a runner time keeps a counted result,
* each guard of the notice and of the left-off marking has its own test.

The helpers come from :mod:`tests.test_runner_checks_count`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import meho_backplane.checks.investigate as inv
from meho_backplane.checks.schemas import SensorRead
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerAssignmentRow
from meho_backplane.gateway.runner_checks import record_set_aside
from meho_backplane.runner import wire
from meho_backplane.runner.client import _code_of
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401  (autouse)
    _isolated_jwks_cache,  # noqa: F401  (autouse)
    _settings_env,  # noqa: F401  (autouse)
)
from .test_gateway_assignment_api import (
    _OP_SAFE,
    _RUNNER_A_ID,
    _TENANT,
    _build_app,
    _register_connector,
    _runner_token,
    _seed_descriptor,
    _seed_identities,
    _seed_target,
)
from .test_runner_checks_count import (
    _KEY,
    _OTHER_TENANT,
    _post,
    _result,
    _seed_dashboard,
    _seed_sensor,
    _sensor,
)

_RUNNER = "runner-a"


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """The Dashboard mails and the investigator calls, in place of sending them."""
    out: dict[str, list[Any]] = {"mails": [], "investigations": []}
    monkeypatch.setattr(
        inv, "_schedule_investigation", lambda **kw: out["investigations"].append(kw)
    )
    monkeypatch.setattr(inv, "schedule_dashboard_notification", out["mails"].append)
    return out


def _post_notices(client: TestClient, *notices: dict[str, Any]) -> Any:
    """Post a batch without results that carries *notices*, as the runner does."""
    body = {
        "runner_id": _RUNNER,
        "results": [],
        "set_aside": list(notices),
        "set_aside_total": len(notices),
        "set_aside_last_at": datetime.now(UTC).isoformat(),
    }
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
        return client.post(
            "/api/v1/checks/results",
            json=body,
            headers={"Authorization": f"Bearer {_runner_token(_KEY, runner_id=_RUNNER_A_ID)}"},
        )


def _notice(
    sensor_id: uuid.UUID,
    *,
    uid: str = "abc123",
    produced_at: datetime | None = None,
) -> wire.SetAsideNotice:
    return wire.SetAsideNotice(
        check_ref=str(sensor_id),
        result_uid=uid,
        status=400,
        code="results_refused",
        produced_at=produced_at,
    )


async def _add_assignment_row(tenant_id: uuid.UUID = _TENANT) -> None:
    async with get_sessionmaker()() as session:
        session.add(RunnerAssignmentRow(tenant_id=tenant_id, runner_name=_RUNNER, items=[]))
        await session.commit()


async def _record(*notices: wire.SetAsideNotice) -> None:
    await record_set_aside(
        tenant_id=_TENANT,
        runner_name=_RUNNER,
        notices=list(notices),
        total=len(notices),
        last_at=datetime.now(UTC),
    )


async def _counted_critical(client: TestClient) -> uuid.UUID:
    """A check of runner-a with a counted ``critical`` result of now."""
    await _seed_identities()
    sensor_id = await _seed_sensor()
    resp = _post(client, _RUNNER_A_ID, _RUNNER, _result(sensor_id, payload={"count": 50}))
    assert resp.status_code == 200
    row = await _sensor(sensor_id)
    assert row.last_state == "critical"
    assert row.runner_produced_at is not None
    return sensor_id


# ---------------------------------------------------------------------------
# Point: MEHO keeps a notice only in short, fixed forms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notice_values_in_another_form_reach_no_reading_mail_or_investigator(
    client: TestClient, switch_on: None, sent: dict[str, list[Any]]
) -> None:
    await _seed_identities()
    kept_id = await _seed_sensor(name="kept")
    skipped_id = await _seed_sensor(name="skipped")
    await _seed_dashboard(kept_id, memo="ok", name="site-kept")
    await _seed_dashboard(skipped_id, memo="ok", name="site-skipped")
    marker = uuid.uuid4().hex[:12]
    host_code = f"node-{marker}.example.test"
    odd_uid = f"Run-{marker}/x"

    resp = _post_notices(
        client,
        {
            "check_ref": str(kept_id),
            "result_uid": uuid.uuid4().hex,
            "status": 999,
            "code": host_code,
            "produced_at": datetime.now(UTC).isoformat(),
        },
        {
            "check_ref": str(skipped_id),
            "result_uid": odd_uid,
            "status": 400,
            "code": "results_refused",
            "produced_at": datetime.now(UTC).isoformat(),
        },
    )

    assert resp.status_code == 200
    kept = SensorRead.model_validate(await _sensor(kept_id), from_attributes=True)
    assert kept.last_state == "unknown"
    assert kept.last_evidence is not None
    assert kept.last_evidence["reason"] == "result_refused"
    assert kept.last_evidence["code"] is None  # the code rule
    assert kept.last_evidence["http_status"] == 0  # the status rule
    skipped = await _sensor(skipped_id)
    assert skipped.last_state == "ok"  # the result id rule: the notice is skipped
    assert sent["mails"], "the kept notice sends the Dashboard mail"
    seen = repr((kept.model_dump(), skipped.last_evidence, sent["mails"], sent["investigations"]))
    assert marker not in seen
    assert "999" not in repr(kept.last_evidence)


@pytest.mark.asyncio
async def test_a_hidden_value_form_shows_in_neither_an_error_reading_nor_a_notice_reading(
    client: TestClient, switch_on: None, sent: dict[str, list[Any]]
) -> None:
    await _seed_identities()
    error_id = await _seed_sensor(name="error")
    notice_id = await _seed_sensor(name="notice")
    value = uuid.uuid4().hex[:16]
    pair = "pass" + "word=" + value

    error_result = _result(error_id, status="error")
    error_result["error"] = f"login refused {pair}"
    assert _post(client, _RUNNER_A_ID, _RUNNER, error_result).status_code == 200
    resp = _post_notices(
        client,
        {
            "check_ref": str(notice_id),
            "result_uid": uuid.uuid4().hex,
            "status": 400,
            "code": pair,
            "produced_at": datetime.now(UTC).isoformat(),
        },
    )

    assert resp.status_code == 200
    error_row = await _sensor(error_id)
    notice_row = await _sensor(notice_id)
    assert error_row.last_evidence is not None
    assert error_row.last_evidence["reason"] == "runner_error"
    assert notice_row.last_evidence is not None
    assert notice_row.last_evidence["reason"] == "result_refused"
    assert notice_row.last_evidence["code"] is None
    assert value not in repr(error_row.last_evidence)
    assert value not in repr(notice_row.last_evidence)


def test_the_runner_keeps_no_code_with_a_dot() -> None:
    dotted = httpx.Response(400, json={"detail": {"code": "results.refused"}})
    plain = httpx.Response(400, json={"detail": {"code": "results_refused"}})

    assert _code_of(dotted) is None
    assert _code_of(plain) == "results_refused"


# ---------------------------------------------------------------------------
# Point: a notice without a runner time keeps a counted result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_notice_without_a_runner_time_keeps_a_counted_critical_result(
    client: TestClient, switch_on: None, sent: dict[str, list[Any]]
) -> None:
    sensor_id = await _counted_critical(client)
    before = (await _sensor(sensor_id)).last_evaluated_at

    resp = _post_notices(
        client,
        {
            "check_ref": str(sensor_id),
            "result_uid": uuid.uuid4().hex,
            "status": 400,
            "code": "results_refused",
        },
    )

    assert resp.status_code == 200
    row = await _sensor(sensor_id)
    assert row.last_state == "critical"
    assert row.last_evaluated_at == before


# ---------------------------------------------------------------------------
# Point: one test for each guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_notice_of_runner_a_leaves_a_check_of_runner_b_alone(switch_on: None) -> None:
    """Guard: the ``runner_name`` filter of ``_runner_sensor_by_ref``."""
    await _seed_identities()
    await _add_assignment_row()
    sensor_id = await _seed_sensor(runner_name="runner-b")

    await _record(_notice(sensor_id, produced_at=datetime.now(UTC)))

    assert (await _sensor(sensor_id)).last_state == "ok"


@pytest.mark.asyncio
async def test_a_notice_leaves_a_check_of_another_tenant_alone(switch_on: None) -> None:
    """Guard: the ``tenant_id`` filter of ``_runner_sensor_by_ref``."""
    await _seed_identities()
    await _add_assignment_row()
    sensor_id = await _seed_sensor(tenant_id=_OTHER_TENANT, runner_name=_RUNNER)

    await _record(_notice(sensor_id, produced_at=datetime.now(UTC)))

    assert (await _sensor(sensor_id)).last_state == "ok"


@pytest.mark.asyncio
async def test_a_notice_for_an_older_result_keeps_a_newer_counted_critical_result(
    client: TestClient, switch_on: None, sent: dict[str, list[Any]]
) -> None:
    """Guard: the runner-time compare of ``_refusal_shown``."""
    sensor_id = await _counted_critical(client)
    before = (await _sensor(sensor_id)).last_evaluated_at

    await _record(_notice(sensor_id, produced_at=datetime.now(UTC) - timedelta(minutes=1)))

    row = await _sensor(sensor_id)
    assert row.last_state == "critical"
    assert row.last_evaluated_at == before


@pytest.mark.asyncio
async def test_the_same_notice_posted_twice_writes_the_unknown_reading_once(
    switch_on: None, sent: dict[str, list[Any]]
) -> None:
    """Guard: the ``result_uid`` compare of ``_refusal_shown``."""
    await _seed_identities()
    await _add_assignment_row()
    sensor_id = await _seed_sensor()
    notice = _notice(sensor_id, produced_at=datetime.now(UTC))

    await _record(notice)
    first = await _sensor(sensor_id)
    await _record(notice)
    second = await _sensor(sensor_id)

    assert first.last_state == "unknown"
    assert second.last_evaluated_at == first.last_evaluated_at


@pytest.mark.asyncio
async def test_a_second_poll_with_the_same_reason_writes_no_new_reading(
    client: TestClient, switch_on: None, sent: dict[str, list[Any]]
) -> None:
    """Guard: ``_already_marked`` in ``mark_left_off_sensors``."""
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()
    sensor_id = await _seed_sensor()
    headers = {"Authorization": f"Bearer {_runner_token(_KEY, runner_id=_RUNNER_A_ID)}"}

    def poll() -> Any:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
            return client.get(f"/api/v1/checks/assignment?runner={_RUNNER}", headers=headers)

    assert poll().json()["items"] == []  # no feature word: the check is left off
    first = await _sensor(sensor_id)
    poll()
    second = await _sensor(sensor_id)

    assert first.last_state == "unknown"
    assert first.last_evidence is not None
    assert first.last_evidence["reason"] == "not_on_runner_list"
    assert second.last_evaluated_at == first.last_evaluated_at
