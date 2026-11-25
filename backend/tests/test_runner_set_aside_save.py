# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A failed save of the set-aside notices, and the limits of their fields.

With ``RUNNER_CHECKS_ENABLED`` on:

* a save that fails keeps nothing of the notices and answers 503 with the code
  ``set_aside_not_saved``; the runner keeps its notices and sends them again,
* a count outside the integer column or a time without a time zone is dropped,
  the rest of the post is kept and the answer is 200,
* MEHO reads at most the first 1,000 notices of one post, and the runner sends
  at most 1,000 in one post.

The helpers come from :mod:`tests.test_runner_checks_count` and
:mod:`tests.test_runner_set_aside`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

import meho_backplane.api.v1.checks as checks_api
import meho_backplane.checks.investigate as inv
from meho_backplane.checks.schemas import SensorRead
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerAssignmentRow, Sensor
from meho_backplane.gateway.runner_checks import _kept_time
from meho_backplane.runner import wire
from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.loop import RunnerState, run_one_tick
from meho_backplane.runner.spool import ResultSpool
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401  (autouse)
    _isolated_jwks_cache,  # noqa: F401  (autouse)
    _settings_env,  # noqa: F401  (autouse)
)
from .test_gateway_assignment_api import (
    _RUNNER_A_ID,
    _TENANT,
    _build_app,
    _runner_token,
    _seed_identities,
)
from .test_runner_checks_count import (
    _KEY,
    _outcome,
    _result,
    _seed_dashboard,
    _seed_sensor,
    _sensor,
    _stored_count,
)
from .test_runner_set_aside import _Central, _ticks

_RUNNER = "runner-a"
_STORED_AT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


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


@pytest.fixture
def save_fails_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first commit inside ``record_set_aside`` fails after its rows were flushed."""
    state = {"inside": False, "failed": False}
    real_record = checks_api.record_set_aside
    real_commit = AsyncSession.commit

    async def record(**kwargs: Any) -> None:
        state["inside"] = True
        try:
            await real_record(**kwargs)
        finally:
            state["inside"] = False

    async def commit(self: AsyncSession) -> None:
        if state["inside"] and not state["failed"]:
            state["failed"] = True
            await self.flush()
            raise RuntimeError("commit failed")
        await real_commit(self)

    monkeypatch.setattr(checks_api, "record_set_aside", record)
    monkeypatch.setattr(AsyncSession, "commit", commit)


async def _add_row(*, count: int = 0, last_at: datetime | None = None) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            RunnerAssignmentRow(
                tenant_id=_TENANT,
                runner_name=_RUNNER,
                items=[],
                set_aside_count=count,
                set_aside_last_at=last_at,
            )
        )
        await session.commit()


async def _stored_summary() -> tuple[int, datetime | None]:
    async with get_sessionmaker()() as session:
        row = (await session.execute(select(RunnerAssignmentRow))).scalars().one()
    last_at = row.set_aside_last_at
    if last_at is not None and last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=UTC)
    return row.set_aside_count, last_at


def _notice(sensor_id: uuid.UUID | str) -> dict[str, Any]:
    return {
        "check_ref": str(sensor_id),
        "result_uid": uuid.uuid4().hex,
        "status": 422,
        "code": "results_refused",
        "produced_at": datetime.now(UTC).isoformat(),
    }


def _post_batch(
    client: TestClient,
    *notices: dict[str, Any],
    results: list[dict[str, Any]] | None = None,
    total: Any = 1,
    last_at: Any = None,
) -> Any:
    body = {
        "runner_id": _RUNNER,
        "results": results or [],
        "set_aside": list(notices),
        "set_aside_total": total,
        "set_aside_last_at": last_at or datetime.now(UTC).isoformat(),
    }
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
        return client.post(
            "/api/v1/checks/results",
            json=body,
            headers={"Authorization": f"Bearer {_runner_token(_KEY, runner_id=_RUNNER_A_ID)}"},
        )


# ---------------------------------------------------------------------------
# A save that fails once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_save_answers_503_keeps_nothing_and_the_next_post_records_once(
    client: TestClient, switch_on: None, sent: dict[str, list[Any]], save_fails_once: None
) -> None:
    await _seed_identities()
    await _add_row()
    sensor_id = await _seed_sensor()
    await _seed_dashboard(sensor_id, memo="ok")
    before = (await _sensor(sensor_id)).last_evaluated_at
    notice = _notice(sensor_id)

    with capture_logs() as logs:
        first = _post_batch(client, notice, total=3)

    assert first.status_code == 503
    assert first.json()["detail"]["code"] == "set_aside_not_saved"
    assert [e["event"] for e in logs].count("runner_set_aside_record_failed") == 1
    row = await _sensor(sensor_id)
    assert row.last_state == "ok"
    assert row.last_evaluated_at == before
    assert await _stored_summary() == (0, None)
    assert sent == {"mails": [], "investigations": []}

    second = _post_batch(client, notice, total=3)

    assert second.status_code == 200
    row = await _sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    assert row.last_evidence["reason"] == "result_refused"
    assert len(sent["mails"]) == 1
    assert (await _stored_summary())[0] == 3

    third = _post_batch(client, notice, total=3)

    assert third.status_code == 200
    assert (await _sensor(sensor_id)).last_evaluated_at == row.last_evaluated_at
    assert len(sent["mails"]) == 1


@pytest.mark.asyncio
async def test_the_runner_keeps_the_notice_of_a_failed_save_and_sends_it_on_the_next_tick(
    client: TestClient,
    switch_on: None,
    sent: dict[str, list[Any]],
    save_fails_once: None,
    tmp_path: Path,
) -> None:
    """The whole way: the runner loop, the results endpoint and the check."""
    await _seed_identities()
    await _add_row()
    sensor_id = await _seed_sensor()
    answers: list[int] = []

    async def central(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assignment"):
            return httpx.Response(304)
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
            resp = client.post(request.url.path, content=request.content, headers=request.headers)
        answers.append(resp.status_code)
        return httpx.Response(resp.status_code, content=resp.content)

    spool = ResultSpool(tmp_path, max_files=10)
    state = RunnerState(notices=[wire.SetAsideNotice.model_validate(_notice(sensor_id))])
    held = list(state.notices)
    async with RunnerClient(
        central_url="https://central.example",
        runner_id=_RUNNER,
        token=_runner_token(_KEY, runner_id=_RUNNER_A_ID),
        transport=httpx.MockTransport(central),
    ) as runner:
        await run_one_tick(client=runner, spool=spool, state=state, runner_id=_RUNNER)
        assert answers == [503]
        assert state.notices == held
        assert (await _sensor(sensor_id)).last_state == "ok"

        await run_one_tick(client=runner, spool=spool, state=state, runner_id=_RUNNER)

    assert answers == [503, 200]
    assert state.notices == []
    row = await _sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    assert row.last_evidence["result_uid"] == held[0].result_uid


@pytest.mark.asyncio
async def test_results_of_a_post_with_a_failed_notice_save_are_stored_and_counted_once(
    client: TestClient, switch_on: None, sent: dict[str, list[Any]], save_fails_once: None
) -> None:
    await _seed_identities()
    await _add_row()
    first_id = await _seed_sensor(name="first")
    second_id = await _seed_sensor(name="second")
    noticed_id = await _seed_sensor(name="noticed")
    results = [_result(first_id, payload={"count": 50}), _result(second_id)]
    notice = _notice(noticed_id)
    counted_before = _outcome("counted")

    assert _post_batch(client, notice, results=results).status_code == 503
    second = _post_batch(client, notice, results=results)

    assert second.status_code == 200
    assert second.json() == {"accepted": 0, "duplicates": 2}
    assert await _stored_count() == 2
    assert _outcome("counted") - counted_before == 2
    assert (await _sensor(first_id)).last_state == "critical"
    assert (await _sensor(noticed_id)).last_state == "unknown"


# ---------------------------------------------------------------------------
# The limits of the notice fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("total", [2_147_483_648, -1, 10**30, "many", 1.5])
async def test_a_count_outside_the_limits_leaves_the_stored_count_and_keeps_the_notice(
    client: TestClient, switch_on: None, sent: dict[str, list[Any]], total: Any
) -> None:
    await _seed_identities()
    await _add_row(count=5, last_at=_STORED_AT)
    sensor_id = await _seed_sensor()

    resp = _post_batch(client, _notice(sensor_id), total=total)

    assert resp.status_code == 200
    count, last_at = await _stored_summary()
    assert count == 5
    assert last_at is not None and last_at > _STORED_AT  # a value inside the limits is kept
    assert (await _sensor(sensor_id)).last_state == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("total", [0, 2_147_483_647])
async def test_a_count_at_the_edge_of_the_limits_is_kept(
    client: TestClient, switch_on: None, total: int
) -> None:
    await _seed_identities()
    await _add_row(count=5)

    assert _post_batch(client, _notice("chk"), total=total).status_code == 200
    assert (await _stored_summary())[0] == total


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "last_at",
    [
        "2026-11-20T10:00:00",  # no time zone
        "soon",
        "0001-01-01T00:00:00+12:00",  # no such time in UTC
        "9999-12-31T23:59:59-12:00",
    ],
)
async def test_a_time_outside_the_limits_leaves_the_stored_time_and_keeps_the_notice(
    client: TestClient, switch_on: None, sent: dict[str, list[Any]], last_at: str
) -> None:
    await _seed_identities()
    await _add_row(count=5, last_at=_STORED_AT)
    sensor_id = await _seed_sensor()
    await _seed_dashboard(sensor_id, memo="ok")

    resp = _post_batch(client, _notice(sensor_id), total=6, last_at=last_at)

    assert resp.status_code == 200
    assert await _stored_summary() == (6, _STORED_AT)
    row = SensorRead.model_validate(await _sensor(sensor_id), from_attributes=True)
    assert row.last_state == "unknown"
    assert sent["mails"]
    assert last_at not in repr((row.last_evidence, sent["mails"], sent["investigations"]))


def test_a_time_with_a_zone_is_kept_in_utc() -> None:
    with_zone = datetime.fromisoformat("2026-11-20T10:00:00+02:00")

    assert _kept_time(with_zone) == datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    assert _kept_time(None) is None


@pytest.mark.asyncio
async def test_meho_reads_the_first_1000_notices_of_a_post_and_skips_the_rest(
    client: TestClient, switch_on: None, sent: dict[str, list[Any]]
) -> None:
    await _seed_identities()
    await _add_row()
    ids = await _seed_many_sensors(wire.SET_ASIDE_NOTICES_MAX + 1)

    with capture_logs() as logs:
        resp = _post_batch(client, *(_notice(sensor_id) for sensor_id in ids))

    assert resp.status_code == 200
    async with get_sessionmaker()() as session:
        unknown = await session.scalar(
            select(func.count()).select_from(Sensor).where(Sensor.last_state == "unknown")
        )
    assert unknown == 1000
    assert (await _sensor(ids[0])).last_state == "unknown"
    assert (await _sensor(ids[-1])).last_state == "ok"
    skips = [e for e in logs if e["event"] == "runner_set_aside_notices_skipped"]
    assert [(e["runner"], e["skipped"]) for e in skips] == [(_RUNNER, 1)]


@pytest.mark.asyncio
async def test_the_runner_sends_1000_notices_in_one_tick_and_the_rest_in_the_next(
    tmp_path: Path,
) -> None:
    central = _Central()
    spool = ResultSpool(tmp_path, max_files=10)
    notices = [wire.SetAsideNotice.model_validate(_notice("chk")) for _ in range(1001)]
    state = RunnerState(notices=list(notices))

    await _ticks(central, spool, state, 1)
    assert central.received[0].set_aside == notices[:1000]
    assert state.notices == notices[1000:]

    await _ticks(central, spool, state, 1)
    assert central.received[1].set_aside == notices[1000:]
    assert state.notices == []


async def _seed_many_sensors(count: int) -> list[uuid.UUID]:
    """*count* active checks of the runner; a copy of the one ``_seed_sensor`` makes."""
    first = await _seed_sensor(name="many-0")
    ids = [first]
    async with get_sessionmaker()() as session:
        pattern = await session.get(Sensor, first)
        assert pattern is not None
        values = {
            c.key: getattr(pattern, c.key)
            for c in Sensor.__mapper__.column_attrs
            if c.key not in {"id", "name"}
        }
        for number in range(1, count):
            ids.append(uuid.uuid4())
            session.add(Sensor(id=ids[-1], name=f"many-{number}", **values))
        await session.commit()
    return ids
