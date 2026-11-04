# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ingest health benchmark: the health checks keep answering during a large ingest.

``scripts/ingest_health_benchmark.py`` runs MEHO's real ingest pipeline in
a server process and probes ``/healthz``, ``/ready`` and the job status
from outside, with the chart's probe deadlines. These tests pin:

* the benchmark app runs the ingest in the background and reports its
  end, its result and a failure;
* the prober sees every answer when the event loop is free, and counts
  the missed answers when the loop is frozen;
* the deadlines are the chart's probe timeouts;
* the server runs in a CPU quota scope when asked.

The two slow tests run the whole benchmark with the real embedding model
on the full-size spec, once without a limit and once under a one-CPU
limit. They are opt-in (``MEHO_RUN_SLOW_TESTS=1``); the second also needs
a user session that can start ``systemd-run`` scopes.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from argparse import Namespace
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
import yaml
from fastapi import FastAPI
from scripts.ingest_health_benchmark import (
    ENDPOINT_DEADLINES_S,
    LIVENESS_TIMEOUT_S,
    READINESS_TIMEOUT_S,
    ProbeSample,
    build_app,
    run_phase,
    server_command,
    summarize,
    synth_openapi_spec,
)

from meho_backplane.health import ProbeResult, register_probe

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_CHART_VALUES = _BACKEND_DIR.parent / "deploy" / "charts" / "meho" / "values.yaml"
_RUN_SLOW_TESTS = os.environ.get("MEHO_RUN_SLOW_TESTS") == "1"

#: Generous deadlines for the in-process tests: a loaded CI runner must
#: not fail them. The chart deadlines are pinned by their own test.
_RELAXED_DEADLINES_S = dict.fromkeys(ENDPOINT_DEADLINES_S, 10.0)
#: A short deadline for the frozen-loop test, so the freeze is sure to miss it.
_SHORT_DEADLINES_S = dict.fromkeys(ENDPOINT_DEADLINES_S, 0.3)
_FREEZE_S = 1.0


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the settings ``/ready`` reads; the benchmark server sets the same ones."""
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_spec_has_one_read_operation_per_item() -> None:
    spec = json.loads(synth_openapi_spec(7))
    assert len(spec["paths"]) == 7
    op_ids = {item["get"]["operationId"] for item in spec["paths"].values()}
    assert len(op_ids) == 7


def test_probe_deadlines_are_the_chart_probe_timeouts() -> None:
    probes = yaml.safe_load(_CHART_VALUES.read_text())["probes"]
    assert probes["liveness"]["timeoutSeconds"] == LIVENESS_TIMEOUT_S
    assert probes["readiness"]["timeoutSeconds"] == READINESS_TIMEOUT_S
    assert probes["liveness"]["httpGet"]["path"] == "/healthz"
    assert probes["readiness"]["httpGet"]["path"] == "/ready"


def test_summary_counts_failed_probes_per_phase_and_endpoint() -> None:
    samples = [
        ProbeSample("store", "/healthz", 0.01, None),
        ProbeSample("store", "/healthz", 0.02, None),
        ProbeSample("store", "/healthz", 1.0, "timeout"),
        ProbeSample("store", "/healthz", 0.03, "late"),
        ProbeSample("store", "/ready", 0.5, None),
    ]
    rows = summarize(samples)
    assert [(row.phase, row.endpoint, row.probes, row.failed) for row in rows] == [
        ("store", "/healthz", 4, 2),
        ("store", "/ready", 1, 0),
    ]
    assert rows[0].p95_ms == 1000.0
    assert rows[0].max_ms == 1000.0
    assert rows[1].p95_ms == 500.0


def test_server_runs_in_a_cpu_quota_scope_when_asked() -> None:
    args = Namespace(ops=50, spec=None, threads=None, cpu_limit=1.5)
    command = server_command(args, "python3", 8765)
    assert command[:6] == [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--property=CPUQuota=150%",
        "--",
    ]
    assert command[6] == "python3"
    assert command[7].endswith("scripts/ingest_health_benchmark.py")
    assert command[8:] == ["--serve", "--port", "8765", "--ops", "50"]


def test_server_runs_without_a_scope_and_passes_spec_and_threads() -> None:
    args = Namespace(ops=50, spec="vendor.json", threads=4, cpu_limit=None)
    command = server_command(args, "python3", 8765)
    assert command[0] == "python3"
    assert "systemd-run" not in command
    assert command[-6:] == ["--ops", "50", "--spec", "vendor.json", "--threads", "4"]


async def _wait_for_end(client: httpx.AsyncClient) -> dict[str, Any]:
    for _ in range(100):
        status: dict[str, Any] = (await client.get("/benchmark/ingest")).json()
        if status["state"] in ("done", "failed"):
            return status
        await asyncio.sleep(0.02)
    raise AssertionError("the ingest did not end")


@pytest.mark.asyncio
async def test_app_runs_the_ingest_in_the_background_and_reports_its_result() -> None:
    calls: list[bool] = []

    async def run_ingest(dry_run: bool) -> dict[str, Any]:
        calls.append(dry_run)
        await asyncio.sleep(0.05)
        return {"operations": 3}

    app = build_app(run_ingest, threads=2)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://benchmark") as client:
        started = await client.post("/benchmark/ingest", params={"dry_run": True})
        assert started.status_code == 202
        assert started.json()["state"] == "running"
        again = await client.post("/benchmark/ingest", params={"dry_run": False})
        assert again.status_code == 409
        assert (await client.get("/healthz")).status_code == 200
        assert await _wait_for_end(client) == {
            "phase": "dry_run",
            "state": "done",
            "result": {"operations": 3},
            "error": None,
            "threads": 2,
        }
    assert calls == [True]


@pytest.mark.asyncio
async def test_app_reports_a_failed_ingest() -> None:
    async def run_ingest(dry_run: bool) -> dict[str, Any]:
        raise RuntimeError("spec broken")

    transport = httpx.ASGITransport(app=build_app(run_ingest, threads=None))
    async with httpx.AsyncClient(transport=transport, base_url="http://benchmark") as client:
        started = await client.post("/benchmark/ingest", params={"dry_run": False})
        assert started.status_code == 202
        status = await _wait_for_end(client)
    assert status["state"] == "failed"
    assert status["error"] == "RuntimeError: spec broken"
    assert status["phase"] == "store"


@asynccontextmanager
async def _served(app: FastAPI) -> AsyncIterator[str]:
    """Serve *app* with uvicorn on the test's event loop, like the benchmark server."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await task
        sock.close()


def _db_ok() -> ProbeResult:
    return ProbeResult(name="db", ok=True, detail="")


@pytest.mark.asyncio
async def test_prober_sees_every_answer_when_the_event_loop_is_free() -> None:
    register_probe("db", _db_ok)

    async def run_ingest(dry_run: bool) -> dict[str, Any]:
        await asyncio.sleep(0.8)
        return {"operations": 1, "dry_run": dry_run}

    async with _served(build_app(run_ingest, threads=1)) as base_url:
        with httpx.Client(base_url=base_url) as client:
            samples, status = await asyncio.to_thread(
                run_phase, client, "store", interval_s=0.1, deadlines=_RELAXED_DEADLINES_S
            )
    assert status["state"] == "done"
    assert status["result"] == {"operations": 1, "dry_run": False}
    assert {sample.endpoint for sample in samples} == set(ENDPOINT_DEADLINES_S)
    assert [sample.failure for sample in samples if sample.failure] == []
    assert all(sample.phase == "store" for sample in samples)
    assert len(samples) >= 2 * len(ENDPOINT_DEADLINES_S)


@pytest.mark.asyncio
async def test_prober_counts_a_frozen_event_loop_as_missed_probes() -> None:
    register_probe("db", _db_ok)

    async def run_ingest(dry_run: bool) -> dict[str, Any]:
        # Blocks the loop on purpose: this is the failure the benchmark exists for.
        time.sleep(_FREEZE_S)
        return {"operations": 1}

    async with _served(build_app(run_ingest, threads=1)) as base_url:
        with httpx.Client(base_url=base_url) as client:
            samples, status = await asyncio.to_thread(
                run_phase, client, "dry_run", interval_s=0.1, deadlines=_SHORT_DEADLINES_S
            )
    assert status["state"] == "done"
    missed = [sample for sample in samples if sample.failure is not None]
    assert missed, "no probe missed its deadline while the loop was frozen"
    assert {sample.failure for sample in missed} <= {"timeout", "late"}
    assert sum(row.failed for row in summarize(samples)) == len(missed)


@pytest.mark.asyncio
async def test_prober_counts_a_not_ready_answer_as_a_failed_probe() -> None:
    # No probe registered: /ready answers 503 on purpose (fail-closed).
    async def run_ingest(dry_run: bool) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {"operations": 1}

    async with _served(build_app(run_ingest, threads=1)) as base_url:
        with httpx.Client(base_url=base_url) as client:
            samples, _status = await asyncio.to_thread(
                run_phase, client, "store", interval_s=0.1, deadlines=_RELAXED_DEADLINES_S
            )
    ready = [sample for sample in samples if sample.endpoint == "/ready"]
    assert ready
    assert {sample.failure for sample in ready} == {"status 503"}
    assert [s.failure for s in samples if s.endpoint == "/healthz" and s.failure] == []


def _run_benchmark(*extra: str) -> dict[str, Any]:
    command = [sys.executable, "-m", "scripts.ingest_health_benchmark", "--format", "json", *extra]
    completed = subprocess.run(
        command, cwd=_BACKEND_DIR, capture_output=True, text=True, timeout=1800, check=False
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report: dict[str, Any] = json.loads(completed.stdout)
    return report


def _cpu_quota_scopes_work() -> bool:
    try:
        probe = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--property=CPUQuota=1%", "--", "true"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


@pytest.mark.slow
@pytest.mark.skipif(
    not _RUN_SLOW_TESTS, reason="Real-model benchmark skipped unless MEHO_RUN_SLOW_TESTS=1."
)
def test_benchmark_passes_with_the_real_model_on_the_full_spec() -> None:
    report = _run_benchmark()
    assert report["ok"], report
    assert report["phases"]["dry_run"]["state"] == "done"
    assert report["phases"]["store"]["result"]["operations"] == 1275
    assert report["failed_probes"] == 0


@pytest.mark.slow
@pytest.mark.skipif(
    not _RUN_SLOW_TESTS, reason="Real-model benchmark skipped unless MEHO_RUN_SLOW_TESTS=1."
)
@pytest.mark.skipif(
    not _cpu_quota_scopes_work(), reason="systemd-run --user scopes are not available here."
)
def test_benchmark_passes_under_a_one_cpu_limit() -> None:
    report = _run_benchmark("--cpu-limit", "1")
    assert report["ok"], report
    assert report["threads"] == 1
    assert report["phases"]["store"]["result"]["operations"] == 1275
    assert report["failed_probes"] == 0
