# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the satellite-runner tick loop (#2497).

A single tick is driven end-to-end against :class:`httpx.MockTransport`
(central routes land in #2499) and the real executor: it fetches a canned
assignment, executes a ``net.tcp_check`` against a live loopback listener,
and POSTs the result batch. A second test proves that when the fetch
fails, the loop reuses the cached assignment and still executes it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs

from meho_backplane.auth.operator import TenantRole
from meho_backplane.runner import wire
from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.loop import RunnerState, run_one_tick
from meho_backplane.runner.spool import ResultSpool
from meho_backplane.runner.wire import RunnerAssignment, RunnerPrincipal, RunnerWorkItem

_Handler = Callable[[httpx.Request], httpx.Response]


@asynccontextmanager
async def _loopback_listener() -> AsyncIterator[int]:
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _assignment(port: int, *, version: str) -> RunnerAssignment:
    item = RunnerWorkItem(
        check_ref="c1",
        op_id="net.tcp_check",
        product="net",
        version="1.x",
        impl_id="net-probe",
        handler_ref="meho_backplane.connectors.net.ops.net_tcp_check",
        params={"host": "127.0.0.1", "port": port},
        safety_level="safe",
        principal=RunnerPrincipal(
            sub="s", tenant_id=uuid.uuid4(), tenant_role=TenantRole.READ_ONLY
        ),
    )
    return RunnerAssignment(assignment_version=version, items=[item])


async def _run_tick(handler: _Handler, spool: ResultSpool, state: RunnerState) -> None:
    client = RunnerClient(
        central_url="https://central.example",
        runner_id="r1",
        token="tok",
        transport=httpx.MockTransport(handler),
    )
    async with client:
        await run_one_tick(client=client, spool=spool, state=state, runner_id="r1")


async def test_tick_fetch_execute_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEHO_NETDIAG_PROBE_ALLOWLIST", "127.0.0.1")
    posted: list[str] = []

    async with _loopback_listener() as port:
        assignment_json = _assignment(port, version="v1").model_dump(mode="json")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/assignment"):
                return httpx.Response(200, json=assignment_json)
            posted.append(request.content.decode())
            return httpx.Response(202)

        state = RunnerState()
        await _run_tick(handler, ResultSpool(tmp_path, max_files=100), state)

    assert state.assignment_version == "v1"
    assert state.assignment is not None
    # Exactly one result batch was POSTed, carrying the executed op's ok result.
    assert len(posted) == 1
    body = posted[0].replace(" ", "")
    assert '"status":"ok"' in body
    assert '"connected":true' in body


async def test_fetch_failure_reuses_cached_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEHO_NETDIAG_PROBE_ALLOWLIST", "127.0.0.1")
    posted: list[str] = []

    async with _loopback_listener() as port:

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/assignment"):
                raise httpx.ConnectError("uplink down")
            posted.append(request.content.decode())
            return httpx.Response(202)

        # Prime the cache as if a prior tick had fetched this assignment.
        state = RunnerState(
            assignment=_assignment(port, version="cached-v"),
            assignment_version="cached-v",
        )
        await _run_tick(handler, ResultSpool(tmp_path, max_files=100), state)

    # Cache untouched by the failed fetch; the cached assignment still ran.
    assert state.assignment_version == "cached-v"
    assert len(posted) == 1
    assert '"status":"ok"' in posted[0].replace(" ", "")


async def test_wire_version_refusal_runs_nothing_and_logs_both_versions(tmp_path: Path) -> None:
    """Central refuses the runner's wire version: drop the cache, run nothing, log it (#5020)."""
    calls: list[str] = []
    refusal = wire.wire_version_mismatch_message(wire.SCHEMA_VERSION, "2")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(409, json={"detail": refusal})

    # A cached assignment from before central changed version must not run.
    state = RunnerState(
        assignment=_assignment(9, version="cached-v"), assignment_version="cached-v"
    )
    with capture_logs() as logs:
        await _run_tick(handler, ResultSpool(tmp_path, max_files=100), state)

    assert calls == ["/api/v1/checks/assignment"]  # no result post: nothing ran
    assert state.assignment is None
    assert state.assignment_version is None
    (event,) = [e for e in logs if e["event"] == "runner_wire_version_mismatch"]
    assert event["log_level"] == "error"
    assert event["runner_wire_version"] == wire.SCHEMA_VERSION
    assert event["error"] == refusal
    assert f"runner wire version {wire.SCHEMA_VERSION}" in event["error"]
    assert "central MEHO needs 2" in event["error"]


async def test_wire_version_refusal_while_draining_spool_keeps_results_and_runs_nothing(
    tmp_path: Path,
) -> None:
    """A refused spool re-post ends the tick: the file stays, the cache is dropped (#5020)."""
    calls: list[str] = []
    refusal = wire.wire_version_mismatch_message(wire.SCHEMA_VERSION, "2")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(409, json={"detail": refusal})

    spool = ResultSpool(tmp_path, max_files=100)
    spool.write_batch(wire.RunnerResultBatch(runner_id="r1", results=[]))
    state = RunnerState(
        assignment=_assignment(9, version="cached-v"), assignment_version="cached-v"
    )
    with capture_logs() as logs:
        await _run_tick(handler, spool, state)

    # The refused re-post was the only call: no fetch, and the cached work did not run.
    assert calls == ["/api/v1/checks/results"]
    assert len(spool.load_oldest_first()) == 1
    assert state.assignment is None
    assert state.assignment_version is None
    (event,) = [e for e in logs if e["event"] == "runner_wire_version_mismatch"]
    assert event["error"] == refusal


async def test_wire_version_refusal_on_result_post_spools_and_drops_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused result post keeps the results in the spool and drops the cache (#5020).

    On the next tick the uplink is down for the fetch. Before the fix the
    runner would have run the cached work then; now it runs nothing.
    """
    monkeypatch.setenv("MEHO_NETDIAG_PROBE_ALLOWLIST", "127.0.0.1")
    calls: list[str] = []
    refusal = wire.wire_version_mismatch_message(wire.SCHEMA_VERSION, "2")
    spool = ResultSpool(tmp_path, max_files=100)
    state = RunnerState()

    async with _loopback_listener() as port:
        assignment_json = _assignment(port, version="v1").model_dump(mode="json")

        def first_tick(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("/assignment"):
                return httpx.Response(200, json=assignment_json)
            return httpx.Response(409, json={"detail": refusal})

        with capture_logs() as logs:
            await _run_tick(first_tick, spool, state)

    assert calls == ["/api/v1/checks/assignment", "/api/v1/checks/results"]
    spooled = spool.load_oldest_first()
    assert len(spooled) == 1
    assert spooled[0][1].results[0].status == "ok"
    assert state.assignment is None
    assert state.assignment_version is None
    assert [e["event"] for e in logs if "wire_version" in e["event"]] == [
        "runner_wire_version_mismatch"
    ]

    calls.clear()

    def second_tick(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/assignment"):
            raise httpx.ConnectError("uplink down")
        return httpx.Response(409, json={"detail": refusal})

    await _run_tick(second_tick, spool, state)

    # Only the refused spool re-post: no fetch, nothing executed, results kept.
    assert calls == ["/api/v1/checks/results"]
    assert len(spool.load_oldest_first()) == 1
    assert state.assignment is None
