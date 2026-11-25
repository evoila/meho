# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A batch that MEHO refuses on every try is set aside; later results go on.

The runner keeps a saved batch in its place while a send can still pass (no
connection, a time-out, a 5xx answer, a rate limit, a login to renew), so
results reach MEHO in order. A client error that a retry does not change sets
the batch aside after three tries, with the status and the code. The runner
then tells MEHO, and the checks of those results read ``unknown``.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerAssignmentRow
from meho_backplane.gateway.repository import get_set_aside_markers
from meho_backplane.gateway.runner_checks import record_set_aside
from meho_backplane.runner import loop as runner_loop
from meho_backplane.runner import wire
from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.loop import SET_ASIDE_AFTER_TRIES, RunnerState, run_one_tick
from meho_backplane.runner.spool import ResultSpool
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
    _runner_token,
    _seed_identities,
)
from .test_runner_checks_count import _KEY, _seed_sensor, _sensor


def _result(uid: str, check_ref: str = "chk") -> wire.RunnerResult:
    return wire.RunnerResult(
        result_uid=uid,
        check_ref=check_ref,
        op_id="op",
        status="ok",
        result={"count": 1},
        produced_at=datetime.now(UTC),
    )


def _batch(*uids: str) -> wire.RunnerResultBatch:
    return wire.RunnerResultBatch(runner_id="runner-a", results=[_result(u) for u in uids])


class _Central:
    """A fake central: answers each post of a batch that holds ``bad`` with *bad_answer*."""

    def __init__(self, bad_answer: httpx.Response | None = None) -> None:
        self.bad_answer = bad_answer
        self.received: list[wire.RunnerResultBatch] = []
        self.refused_posts = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assignment"):
            return httpx.Response(304)
        batch = wire.RunnerResultBatch.model_validate_json(request.content)
        if self.bad_answer is not None and any(r.result_uid == "bad" for r in batch.results):
            self.refused_posts += 1
            return self.bad_answer
        self.received.append(batch)
        return httpx.Response(200, json={"accepted": len(batch.results), "duplicates": 0})

    def uids(self) -> list[str]:
        return [r.result_uid for b in self.received for r in b.results]


def _client(central: _Central) -> RunnerClient:
    return RunnerClient(
        central_url="https://central.example",
        runner_id="runner-a",
        token="tok",
        transport=httpx.MockTransport(central),
    )


async def _ticks(central: _Central, spool: ResultSpool, state: RunnerState, count: int) -> None:
    async with _client(central) as runner:
        for _ in range(count):
            await run_one_tick(client=runner, spool=spool, state=state, runner_id="runner-a")


def _bad_400() -> httpx.Response:
    return httpx.Response(400, json={"detail": {"code": "results_refused", "note": "free text"}})


@pytest.mark.asyncio
async def test_a_batch_refused_every_time_is_set_aside_and_later_results_go_on(
    tmp_path: Path,
) -> None:
    central = _Central(_bad_400())
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    spool.write_batch(_batch("next-1"))
    spool.write_batch(_batch("next-2"))
    state = RunnerState()

    await _ticks(central, spool, state, SET_ASIDE_AFTER_TRIES - 1)
    assert central.uids() == []  # still waiting in its place

    await _ticks(central, spool, state, 1)

    assert central.uids() == ["next-1", "next-2"]  # in order
    assert spool.load_oldest_first() == []
    count, newest = spool.set_aside_summary()
    assert count == 1 and newest is not None
    kept = next((tmp_path / "set-aside").glob("*.json"))
    record = json.loads(kept.read_text())
    assert record["status"] == 400
    assert record["code"] is None or record["code"] == "results_refused"
    assert "free text" not in kept.read_text()
    assert [r["result_uid"] for r in record["batch"]["results"]] == ["bad"]


@pytest.mark.asyncio
async def test_the_runner_tells_central_about_the_set_aside_results(tmp_path: Path) -> None:
    central = _Central(httpx.Response(400, json={"detail": {"code": "results_refused"}}))
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    state = RunnerState()

    await _ticks(central, spool, state, SET_ASIDE_AFTER_TRIES)

    notices = [n for b in central.received for n in b.set_aside]
    assert [(n.result_uid, n.status, n.code) for n in notices] == [("bad", 400, "results_refused")]
    assert central.received[-1].set_aside_total == 1
    assert state.notices == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503, 401, 403, 408, 429])
async def test_a_failure_that_can_pass_keeps_its_place_and_holds_later_results_back(
    tmp_path: Path, status: int
) -> None:
    central = _Central(httpx.Response(status))
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    spool.write_batch(_batch("next"))
    state = RunnerState()

    await _ticks(central, spool, state, SET_ASIDE_AFTER_TRIES + 3)

    assert central.uids() == []
    assert len(spool.load_oldest_first()) == 2
    assert spool.set_aside_summary()[0] == 0


@pytest.mark.asyncio
async def test_no_connection_keeps_the_place(tmp_path: Path) -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    spool.write_batch(_batch("next"))
    state = RunnerState()
    async with RunnerClient(
        central_url="https://central.example",
        runner_id="runner-a",
        token="tok",
        transport=httpx.MockTransport(down),
    ) as runner:
        for _ in range(SET_ASIDE_AFTER_TRIES + 2):
            await run_one_tick(client=runner, spool=spool, state=state, runner_id="runner-a")

    assert len(spool.load_oldest_first()) == 2
    assert spool.set_aside_summary()[0] == 0


def _every_tick_assignment(check_ref: str) -> wire.RunnerAssignment:
    """An assignment with one check that is due on every tick."""
    item = wire.RunnerWorkItem(
        check_ref=check_ref,
        op_id=_OP_SAFE,
        product="net",
        handler_ref="meho_backplane.connectors.net.ops.net_tcp_check",
        safety_level="safe",
        principal=wire.RunnerPrincipal(
            sub="s", tenant_id=_TENANT, tenant_role=TenantRole.READ_ONLY
        ),
    )
    return wire.RunnerAssignment(assignment_version="v1", items=[item])


def _execute_with_clock_ahead(monkeypatch: pytest.MonkeyPatch, ahead: timedelta) -> None:
    """Each run of a check gives a new result, with a runner time *ahead* of now."""
    numbers = itertools.count(1)

    async def execute(item: wire.RunnerWorkItem) -> wire.RunnerResult:
        return wire.RunnerResult(
            result_uid=f"r{next(numbers):02d}",
            check_ref=item.check_ref,
            op_id=item.op_id,
            status="ok",
            result={"count": 1},
            produced_at=datetime.now(UTC) + ahead,
        )

    monkeypatch.setattr(runner_loop, "execute_work_item", execute)


class _RefusesResults(_Central):
    """A fake central that refuses each batch with a result and takes each notice."""

    def __init__(self, notice_answers: list[int] | None = None) -> None:
        super().__init__()
        self.notice_answers = list(notice_answers or [])

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assignment"):
            return httpx.Response(304)
        batch = wire.RunnerResultBatch.model_validate_json(request.content)
        if batch.results:
            return httpx.Response(422, json={"detail": {"code": "results_refused"}})
        if self.notice_answers and (status := self.notice_answers.pop(0)) != 200:
            return httpx.Response(status)
        self.received.append(batch)
        return httpx.Response(200, json={"accepted": 0, "duplicates": 0})

    def told(self) -> list[str]:
        return [n.result_uid for b in self.received for n in b.set_aside]


@pytest.mark.asyncio
async def test_the_notices_go_out_while_a_new_refused_batch_arrives_on_every_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _execute_with_clock_ahead(monkeypatch, timedelta(minutes=5))
    central = _RefusesResults()
    spool = ResultSpool(tmp_path, max_files=50)
    state = RunnerState(assignment=_every_tick_assignment("chk"), assignment_version="v1")

    for _ in range(10):
        await _ticks(central, spool, state, 1)
        assert spool.load_oldest_first() != []  # a batch always waits
        assert state.notices == []  # a set-aside batch is told in its own tick

    set_aside, _newest = spool.set_aside_summary()
    assert set_aside > 1
    assert central.told() == [f"r{n:02d}" for n in range(1, set_aside + 1)]  # each once, in order
    assert central.received[-1].set_aside_total == set_aside


@pytest.mark.asyncio
async def test_a_notice_that_could_not_be_sent_goes_out_on_the_next_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _execute_with_clock_ahead(monkeypatch, timedelta(minutes=5))
    central = _RefusesResults(notice_answers=[503])
    spool = ResultSpool(tmp_path, max_files=50)
    state = RunnerState(assignment=_every_tick_assignment("chk"), assignment_version="v1")

    await _ticks(central, spool, state, SET_ASIDE_AFTER_TRIES)
    assert [n.result_uid for n in state.notices] == ["r01"]

    await _ticks(central, spool, state, 1)

    assert central.told() == ["r01"]
    assert state.notices == []
    assert spool.load_oldest_first() != []


@pytest.mark.asyncio
async def test_the_notice_goes_out_while_a_batch_waits_for_a_failure_that_can_pass(
    tmp_path: Path,
) -> None:
    class _Central503Next(_Central):
        async def __call__(self, request: httpx.Request) -> httpx.Response:
            if b'"next"' in request.content:
                return httpx.Response(503)
            return await super().__call__(request)

    central = _Central503Next(_bad_400())
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    spool.write_batch(_batch("next"))
    state = RunnerState()

    await _ticks(central, spool, state, SET_ASIDE_AFTER_TRIES)

    assert [n.result_uid for b in central.received for n in b.set_aside] == ["bad"]
    assert central.uids() == []  # the notice carries no result
    waiting = [r.result_uid for _path, b in spool.load_oldest_first() for r in b.results]
    assert waiting == ["next"]


@pytest.mark.asyncio
async def test_a_runner_with_a_clock_ahead_and_a_check_on_every_tick_shows_unknown_in_meho(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole way: the runner loop, the results endpoint, the check and the count."""
    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()
    _execute_with_clock_ahead(monkeypatch, timedelta(minutes=5))
    await _seed_identities()
    sensor_id: uuid.UUID = await _seed_sensor()
    async with get_sessionmaker()() as session:
        session.add(RunnerAssignmentRow(tenant_id=_TENANT, runner_name="runner-a", items=[]))
        await session.commit()
    app = TestClient(_build_app())
    answers: list[int] = []

    async def central(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assignment"):
            return httpx.Response(304)
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
            resp = app.post(request.url.path, content=request.content, headers=request.headers)
        answers.append(resp.status_code)
        return httpx.Response(resp.status_code, content=resp.content)

    spool = ResultSpool(tmp_path, max_files=50)
    state = RunnerState(assignment=_every_tick_assignment(str(sensor_id)), assignment_version="v1")
    async with RunnerClient(
        central_url="https://central.example",
        runner_id="runner-a",
        token=_runner_token(_KEY, runner_id=_RUNNER_A_ID),
        transport=httpx.MockTransport(central),
    ) as runner:
        for _ in range(SET_ASIDE_AFTER_TRIES + 3):
            await run_one_tick(client=runner, spool=spool, state=state, runner_id="runner-a")

    assert spool.load_oldest_first() != []
    assert set(answers) == {422, 200}
    row = await _sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    assert row.last_evidence["reason"] == "result_refused"
    assert row.last_evidence["http_status"] == 422
    assert row.last_evidence["code"] == "results_refused"
    async with get_sessionmaker()() as session:
        markers = await get_set_aside_markers(session, tenant_id=_TENANT)
    assert markers["runner-a"][0] == spool.set_aside_summary()[0] > 0


@pytest.mark.asyncio
async def test_a_set_aside_batch_is_not_sent_again(tmp_path: Path) -> None:
    central = _Central(_bad_400())
    spool = ResultSpool(tmp_path, max_files=10)
    spool.write_batch(_batch("bad"))
    state = RunnerState()
    await _ticks(central, spool, state, SET_ASIDE_AFTER_TRIES)
    posts_before = central.refused_posts

    await _ticks(central, spool, state, 3)

    assert central.refused_posts == posts_before


@pytest.mark.asyncio
async def test_central_shows_the_checks_of_refused_results_as_unknown_and_keeps_the_count() -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    async with get_sessionmaker()() as session:
        session.add(RunnerAssignmentRow(tenant_id=_TENANT, runner_name="runner-a", items=[]))
        await session.commit()
    notice = wire.SetAsideNotice(
        check_ref=str(sensor_id),
        result_uid="bad",
        status=400,
        code="results_refused",
        produced_at=datetime.now(UTC),
    )
    when = datetime.now(UTC)

    for _ in range(2):  # a notice sent twice writes once
        await record_set_aside(
            tenant_id=_TENANT, runner_name="runner-a", notices=[notice], total=2, last_at=when
        )

    row = await _sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    assert row.last_evidence["reason"] == "result_refused"
    assert row.last_evidence["http_status"] == 400
    async with get_sessionmaker()() as session:
        markers = await get_set_aside_markers(session, tenant_id=_TENANT)
        stored = (await session.execute(select(RunnerAssignmentRow))).scalars().one()
    assert markers["runner-a"][0] == 2
    assert stored.set_aside_last_at is not None


def test_the_set_aside_folder_keeps_at_most_max_files_batches(tmp_path: Path) -> None:
    spool = ResultSpool(tmp_path, max_files=2)
    names: list[str] = []
    with capture_logs() as logs:
        for uid in ("first", "second", "third"):
            path = spool.write_batch(_batch(uid))
            names.append(path.name)
            spool.set_aside(path, _batch(uid), status=400, code="results_refused")

    kept = sorted(p.name for p in (tmp_path / "set-aside").glob("*.json"))
    assert kept == names[1:]  # the newest two
    count, _newest = spool.set_aside_summary()
    assert count == 2
    drops = [e for e in logs if e["event"] == "runner_set_aside_overflow_drop"]
    assert len(drops) == 1
    assert drops[0]["log_level"] == "warning"
    assert (drops[0]["path"], drops[0]["status"], drops[0]["code"]) == (
        names[0],
        400,
        "results_refused",
    )
