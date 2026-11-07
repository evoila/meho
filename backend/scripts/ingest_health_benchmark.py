# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Benchmark: the health checks during a large spec ingest, under a CPU limit.

What it checks: while MEHO ingests a large API file, ``/healthz`` and
``/ready`` answer within the probe deadlines of the chart, and another
request (the job status poll) keeps answering. First a dry run, then the
real run that stores the operations and embeds one text per operation
with the real embedding model.

Two processes:

* **The server.** A small FastAPI app with MEHO's real health routes
  (``/healthz``, and ``/ready`` with the database probe) and one
  benchmark route that runs the real ingest pipeline as a background
  task. uvicorn serves it on one event loop, like MEHO itself. The
  database is a temporary SQLite file with the migrations applied. The
  LLM grouping step is stubbed: it needs an LLM and plays no part in
  the health check question. With ``--cpu-limit`` the server runs in a
  ``systemd-run`` scope with that CPU quota, like a pod with a CPU limit.
* **The prober.** This process, outside the CPU limit, like the kubelet.
  It sends the probes with the chart's timeouts (``/healthz`` 1 s,
  ``/ready`` 2 s) and counts every answer that misses its deadline.

Usage (from ``backend/``)::

    uv run python -m scripts.ingest_health_benchmark
    uv run python -m scripts.ingest_health_benchmark --cpu-limit 1
    uv run python -m scripts.ingest_health_benchmark --cpu-limit 1 --threads 32
    uv run python -m scripts.ingest_health_benchmark --spec path/to/openapi.json

``--spec`` ingests the file under its own ``info.version`` (the pipeline
rejects a label with another major version); a file without one is
ingested under ``1.0``. ``--threads`` forces the thread count of the embedding model. A count
above the CPU limit shows the problem this benchmark guards against.
The exit code is 1 when a probe missed its deadline or an ingest failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import FastAPI, HTTPException

#: The chart's ``probes.liveness.timeoutSeconds``.
LIVENESS_TIMEOUT_S = 1.0
#: The chart's ``probes.readiness.timeoutSeconds``.
READINESS_TIMEOUT_S = 2.0
#: The deadline for the job status poll, the "other request".
STATUS_TIMEOUT_S = 2.0
#: The operation count of the vCenter file from the report.
DEFAULT_OP_COUNT = 1275
#: Denser than the chart's probe periods (10 s and 5 s), so a short pause
#: cannot hide between two probes.
DEFAULT_PROBE_INTERVAL_S = 0.5
PHASES = ("dry_run", "store")
#: Every probed endpoint with its deadline in seconds.
ENDPOINT_DEADLINES_S = {
    "/healthz": LIVENESS_TIMEOUT_S,
    "/ready": READINESS_TIMEOUT_S,
    "/benchmark/ingest": STATUS_TIMEOUT_S,
}

_PRODUCT = "benchmark"
#: The version label of a spec without ``info.version``.
_DEFAULT_VERSION = "1.0"
_IMPL_ID = "benchmark-rest"

RunIngest = Callable[[bool], Awaitable[dict[str, Any]]]


def synth_openapi_spec(op_count: int) -> str:
    """An OpenAPI 3.1 file with *op_count* read operations.

    Each operation carries a description of the length a vendor file has,
    so the embedding work per operation is realistic.
    """
    paths = {
        f"/inventory/items/{i}": {
            "get": {
                "operationId": f"readInventoryItem{i}",
                "summary": f"Read inventory item {i}",
                "description": (
                    f"Returns the configuration, the current power state, the attached "
                    f"storage and the network settings of inventory item {i}. Use it "
                    "before a change to read the identifiers the change needs."
                ),
                "responses": {"200": {"description": "ok"}},
            }
        }
        for i in range(op_count)
    }
    return json.dumps(
        {
            "openapi": "3.1.0",
            "info": {"title": "Ingest health benchmark", "version": "1.0.0"},
            "paths": paths,
        }
    )


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


@dataclass
class _IngestState:
    phase: str | None = None
    state: str = "idle"
    result: dict[str, Any] | None = None
    error: str | None = None
    task: asyncio.Task[None] | None = None


def build_app(run_ingest: RunIngest, *, threads: int | None) -> FastAPI:
    """The benchmark app: MEHO's health routes plus the ingest trigger."""
    from meho_backplane.health import router as health_router

    app = FastAPI(title="ingest health benchmark")
    app.include_router(health_router)
    state = _IngestState()

    async def _run(dry_run: bool) -> None:
        try:
            state.result = await run_ingest(dry_run)
            state.state = "done"
        except Exception as exc:
            # The prober reads the failure from the status route.
            state.error = f"{type(exc).__name__}: {exc}"
            state.state = "failed"

    @app.get("/benchmark/ingest")
    async def status() -> dict[str, Any]:
        return {
            "phase": state.phase,
            "state": state.state,
            "result": state.result,
            "error": state.error,
            "threads": threads,
        }

    @app.post("/benchmark/ingest", status_code=202)
    async def start(dry_run: bool) -> dict[str, Any]:
        if state.state == "running":
            raise HTTPException(status_code=409, detail="an ingest is running")
        state.phase = "dry_run" if dry_run else "store"
        state.state = "running"
        state.result = None
        state.error = None
        state.task = asyncio.create_task(_run(dry_run))
        return await status()

    return app


def spec_version(spec_content: str) -> str:
    """The version label to ingest *spec_content* under: its ``info.version``.

    The pipeline rejects a label whose major version differs from the
    spec's, so a vendor file must be ingested under its own version.
    """
    from meho_backplane.operations.ingest import read_spec_info_version

    return read_spec_info_version("spec:benchmark", content=spec_content) or _DEFAULT_VERSION


def ingest_runner(embedding_service: Any, spec_content: str, *, version: str) -> RunIngest:
    """The real ingest pipeline on *spec_content* under *version*, with the LLM grouping stubbed."""
    from meho_backplane.auth.operator import Operator, TenantRole
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations.ingest import GroupingResult, SpecSource
    from meho_backplane.operations.ingest.pipeline import IngestionPipelineService

    operator = Operator(
        sub=f"benchmark-{uuid.uuid4()}",
        name="Ingest health benchmark",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )

    async def _no_grouping(**_kwargs: Any) -> GroupingResult:
        return GroupingResult(
            connector_id=f"{_IMPL_ID}-{version}",
            groups_created=0,
            operations_assigned=0,
            operations_unassigned=0,
            llm_call_count=0,
            llm_duration_ms=0.0,
        )

    async def run(dry_run: bool) -> dict[str, Any]:
        service = IngestionPipelineService(
            operator,
            sessionmaker=get_sessionmaker(),
            embedding_service=embedding_service,
        )
        service._run_grouping_phase = _no_grouping  # type: ignore[method-assign]
        started = time.monotonic()
        result = await service.ingest(
            product=_PRODUCT,
            version=version,
            impl_id=_IMPL_ID,
            specs=[SpecSource(uri="spec:benchmark", content=spec_content)],
            tenant_id=None,
            dry_run=dry_run,
        )
        return {
            "operations": result.ingestion.inserted_count,
            "connector_registered": result.ingestion.connector_registered,
            "seconds": round(time.monotonic() - started, 1),
        }

    return run


def _prepare_database(work_dir: Path) -> None:
    from alembic import command

    from meho_backplane.db.migrations import alembic_config

    db_file = work_dir / "benchmark.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    os.environ["DATABASE_URL"] = url
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    # The store step writes every operation in one transaction. Without
    # this, SQLite blocks the readiness probe's read until that commit,
    # which PostgreSQL never does. Write-ahead logging lets readers
    # through, like PostgreSQL. The setting stays in the file.
    with sqlite3.connect(db_file) as connection:
        connection.execute("PRAGMA journal_mode=WAL")


def serve(args: argparse.Namespace) -> int:
    """The server process: prepare the database and the model, then serve."""
    import uvicorn

    # The settings need a Keycloak issuer; the benchmark never logs in.
    os.environ.setdefault("KEYCLOAK_ISSUER_URL", "https://keycloak.invalid/realms/benchmark")
    os.environ.setdefault("KEYCLOAK_AUDIENCE", "meho-backplane")
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    work_dir = Path(tempfile.mkdtemp(prefix="meho-ingest-health-benchmark-"))
    try:
        _prepare_database(work_dir)

        from meho_backplane.db.migrations import db_migration_probe
        from meho_backplane.health import register_probe
        from meho_backplane.retrieval.cpu_limit import embedding_thread_count
        from meho_backplane.retrieval.embedding import EmbeddingService
        from meho_backplane.settings import get_settings

        register_probe("db", db_migration_probe)
        settings = get_settings()
        service = EmbeddingService(
            model_name=settings.retrieval_embedding_model,
            cache_dir=settings.retrieval_model_cache_dir,
            threads=args.threads,
        )
        threads = args.threads if args.threads is not None else embedding_thread_count()
        spec_content = Path(args.spec).read_text() if args.spec else synth_openapi_spec(args.ops)
        runner = ingest_runner(service, spec_content, version=spec_version(spec_content))
        app = build_app(runner, threads=threads)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", lifespan="off")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    return 0


# ---------------------------------------------------------------------------
# The prober
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeSample:
    """One probe: how long the answer took, and why it failed, if it did."""

    phase: str
    endpoint: str
    seconds: float
    failure: str | None


@dataclass(frozen=True)
class EndpointSummary:
    phase: str
    endpoint: str
    probes: int
    failed: int
    p95_ms: float
    max_ms: float


def probe(
    client: httpx.Client, phase: str, endpoint: str, deadline_s: float
) -> tuple[ProbeSample, dict[str, Any] | None]:
    """Send one probe. A failure is a timeout, an error, a 4xx/5xx, or a late answer."""
    started = time.monotonic()
    body: dict[str, Any] | None = None
    try:
        response = client.get(endpoint, timeout=deadline_s)
    except httpx.TimeoutException:
        failure: str | None = "timeout"
    except httpx.HTTPError as exc:
        failure = type(exc).__name__
    else:
        failure = None if response.status_code < 400 else f"status {response.status_code}"
        if failure is None and endpoint == "/benchmark/ingest":
            body = response.json()
    seconds = time.monotonic() - started
    if failure is None and seconds > deadline_s:
        failure = "late"
    return ProbeSample(phase, endpoint, seconds, failure), body


def run_phase(
    client: httpx.Client,
    phase: str,
    *,
    interval_s: float,
    deadlines: Mapping[str, float] = ENDPOINT_DEADLINES_S,
) -> tuple[list[ProbeSample], dict[str, Any]]:
    """Start one ingest phase and probe every endpoint until it ends."""
    response = client.post(
        "/benchmark/ingest", params={"dry_run": phase == "dry_run"}, timeout=STATUS_TIMEOUT_S
    )
    response.raise_for_status()
    samples: list[ProbeSample] = []
    while True:
        tick = time.monotonic()
        status: dict[str, Any] | None = None
        for endpoint, deadline_s in deadlines.items():
            sample, body = probe(client, phase, endpoint, deadline_s)
            samples.append(sample)
            if body is not None:
                status = body
        if status is not None and status["state"] in ("done", "failed"):
            return samples, status
        time.sleep(max(0.0, interval_s - (time.monotonic() - tick)))


def _p95_ms(values: Sequence[float]) -> float:
    ordered = sorted(values)
    index = max(0, -(-95 * len(ordered) // 100) - 1)
    return round(ordered[index] * 1000, 1)


def summarize(samples: Sequence[ProbeSample]) -> list[EndpointSummary]:
    """One row per phase and endpoint, in the order the probes ran."""
    groups: dict[tuple[str, str], list[ProbeSample]] = {}
    for sample in samples:
        groups.setdefault((sample.phase, sample.endpoint), []).append(sample)
    return [
        EndpointSummary(
            phase=phase,
            endpoint=endpoint,
            probes=len(group),
            failed=sum(1 for sample in group if sample.failure is not None),
            p95_ms=_p95_ms([sample.seconds for sample in group]),
            max_ms=round(max(sample.seconds for sample in group) * 1000, 1),
        )
        for (phase, endpoint), group in groups.items()
    ]


def server_command(args: argparse.Namespace, python: str, port: int) -> list[str]:
    """The command for the server process, in a CPU quota scope when asked."""
    command = [python, str(Path(__file__).resolve()), "--serve", "--port", str(port)]
    command += ["--ops", str(args.ops)]
    if args.spec:
        command += ["--spec", args.spec]
    if args.threads is not None:
        command += ["--threads", str(args.threads)]
    if args.cpu_limit is not None:
        quota = f"--property=CPUQuota={round(args.cpu_limit * 100)}%"
        command = ["systemd-run", "--user", "--scope", "--quiet", quota, "--", *command]
    return command


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_up(
    client: httpx.Client, process: subprocess.Popen[bytes], timeout_s: float
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the server exited early with code {process.returncode}")
        try:
            if client.get("/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"the server did not answer /healthz within {timeout_s:.0f} s")


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    """Start the server, run both phases, and return the report."""
    port = _free_port()
    samples: list[ProbeSample] = []
    phases: dict[str, dict[str, Any]] = {}
    # The server's output (migration log lines, warnings) goes to a file.
    # It is shown only when the server fails, so the report stays readable.
    with tempfile.TemporaryFile(mode="w+") as server_log:
        process = subprocess.Popen(
            server_command(args, sys.executable, port), stdout=server_log, stderr=server_log
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
                _wait_until_up(client, process, args.startup_timeout)
                for phase in PHASES:
                    phase_samples, status = run_phase(client, phase, interval_s=args.interval)
                    samples.extend(phase_samples)
                    phases[phase] = status
        finally:
            process.terminate()
            process.wait(timeout=30)
            if any(status["state"] == "failed" for status in phases.values()) or len(phases) < len(
                PHASES
            ):
                server_log.seek(0)
                sys.stderr.write("".join(server_log.readlines()[-40:]))
    summary = summarize(samples)
    failed = sum(row.failed for row in summary)
    return {
        "threads": next(iter(phases.values()))["threads"] if phases else None,
        "cpu_limit": args.cpu_limit,
        "phases": phases,
        "probes": [asdict(row) for row in summary],
        "failed_probes": failed,
        "ok": failed == 0 and all(status["state"] == "done" for status in phases.values()),
    }


def render_table(report: dict[str, Any]) -> str:
    lines = [
        f"embedding threads: {report['threads']}   CPU limit: {report['cpu_limit']}",
        "",
        "| phase | result |",
        "|---|---|",
    ]
    for phase, status in report["phases"].items():
        detail = status["error"] if status["state"] == "failed" else json.dumps(status["result"])
        lines.append(f"| {phase} | {status['state']}: {detail} |")
    lines += ["", "| phase | endpoint | probes | missed | p95 | max |", "|---|---|---|---|---|---|"]
    for row in report["probes"]:
        lines.append(
            f"| {row['phase']} | {row['endpoint']} | {row['probes']} | {row['failed']} "
            f"| {row['p95_ms']} ms | {row['max_ms']} ms |"
        )
    lines += ["", "all probes answered in time" if report["ok"] else "FAILED"]
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ops", type=int, default=DEFAULT_OP_COUNT, help="operations in the spec")
    parser.add_argument("--spec", help="an OpenAPI file to ingest instead of the built one")
    parser.add_argument(
        "--cpu-limit", type=float, help="run the server under this many CPUs (systemd-run)"
    )
    parser.add_argument(
        "--threads", type=int, help="force this thread count for the embedding model"
    )
    parser.add_argument("--interval", type=float, default=DEFAULT_PROBE_INTERVAL_S)
    parser.add_argument("--startup-timeout", type=float, default=300.0)
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.serve:
        return serve(args)
    report = run_benchmark(args)
    print(json.dumps(report, indent=2) if args.format == "json" else render_table(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
