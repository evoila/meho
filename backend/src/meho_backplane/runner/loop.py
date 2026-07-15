# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Satellite-runner startup + interval-tick loop.

The runner's cadence is moulded on the in-process interval-tick sweepers
(:func:`meho_backplane.topology.scheduler._scheduler_loop`,
:func:`meho_backplane.memory.expiry._sweeper_loop`) — a sweep-then-sleep
forever loop with a fully-guarded body — **not** the DB-session-bound
scheduler trigger loop, which a runner (no local Postgres) cannot run.
Sweep-then-sleep (rather than sleep-then-sweep) so a fresh runner polls
immediately; the sleep-first rationale of the memory sweeper (dodging
eager-init races) does not apply to the runner's tiny startup.

Each tick, in order:

1. drain the on-disk retry spool oldest-first,
2. fetch the assignment (echoing the cached digest as ``known_version``;
   a ``304`` or a fetch failure keeps the cached assignment so the runner
   keeps executing while the uplink is down),
3. execute each work item locally,
4. post the result batch; on failure, spool it.

:func:`run_runner` is the synchronous entry the ``__main__`` module calls.
It loads config (surfacing :class:`RunnerConfigError` to the caller for a
clean exit 1), then runs the async loop under :func:`asyncio.run` with
SIGTERM/SIGINT wired to cancel the loop task for a clean exit 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from dataclasses import dataclass

import structlog

from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.logging import configure_logging
from meho_backplane.runner.client import ASSIGNMENT_UNCHANGED, RunnerClient, RunnerClientError
from meho_backplane.runner.executor import execute_work_item
from meho_backplane.runner.settings import RunnerSettings, get_runner_settings
from meho_backplane.runner.spool import ResultSpool
from meho_backplane.runner.wire import RunnerAssignment, RunnerResultBatch

__all__ = ["run_one_tick", "run_runner"]

_log = structlog.get_logger(__name__)


@dataclass
class RunnerState:
    """Mutable per-process tick state: the cached assignment + its digest."""

    assignment: RunnerAssignment | None = None
    assignment_version: str | None = None


async def run_one_tick(
    *,
    client: RunnerClient,
    spool: ResultSpool,
    state: RunnerState,
    runner_id: str,
) -> None:
    """Run a single tick: drain spool, fetch, execute, report.

    Extracted from the forever loop so it is directly testable. Never
    raises for an expected condition (down uplink) — those are logged and
    turned into spool writes / cache reuse.
    """
    _log.info("runner_tick_start", cached_version=state.assignment_version)
    await _drain_spool(client, spool)
    try:
        fetched = await client.fetch_assignment(state.assignment_version)
    except RunnerClientError as exc:
        # Uplink down: keep executing the last cached assignment.
        _log.warning("runner_assignment_fetch_failed", error=str(exc))
        fetched = ASSIGNMENT_UNCHANGED
    # A ``304`` (the unchanged sentinel) or a failed fetch keeps the cache;
    # only a fresh assignment replaces it.
    if isinstance(fetched, RunnerAssignment):
        state.assignment = fetched
        state.assignment_version = fetched.assignment_version
    await _execute_and_report(client, spool, state, runner_id)
    _log.info("runner_tick_done", cached_version=state.assignment_version)


async def _execute_and_report(
    client: RunnerClient, spool: ResultSpool, state: RunnerState, runner_id: str
) -> None:
    assignment = state.assignment
    if assignment is None or not assignment.items:
        return
    results = [await execute_work_item(item) for item in assignment.items]
    batch = RunnerResultBatch(runner_id=runner_id, results=results)
    try:
        await client.post_results(batch)
    except RunnerClientError as exc:
        _log.warning("runner_results_post_failed", results=len(results), error=str(exc))
        spool.write_batch(batch)


async def _drain_spool(client: RunnerClient, spool: ResultSpool) -> None:
    """Re-post spooled batches oldest-first, stopping at the first failure."""
    for path, batch in spool.load_oldest_first():
        try:
            await client.post_results(batch)
        except RunnerClientError as exc:
            # Still down — leave this and the rest for the next tick.
            _log.warning("runner_spool_drain_failed", path=str(path), error=str(exc))
            return
        spool.remove(path)


async def _run_loop(settings: RunnerSettings) -> None:
    """The forever loop: sweep, sleep one cadence, repeat.

    Per-tick ``try`` / ``except`` means an unexpected error logs and waits
    for the next cadence rather than killing the loop. ``CancelledError``
    (from the SIGTERM/SIGINT handler cancelling this task) propagates so
    the process unwinds and exits cleanly.
    """
    _log.info(
        "runner_started",
        runner_id=settings.runner_id,
        central_url=settings.central_url,
        tick_interval_seconds=settings.tick_interval_seconds,
        spool_dir=settings.spool_dir,
    )
    state = RunnerState()
    spool = ResultSpool(settings.spool_dir, max_files=settings.spool_max_files)
    async with RunnerClient(
        central_url=settings.central_url,
        runner_id=settings.runner_id,
        token=settings.runner_token,
    ) as client:
        while True:
            try:
                await run_one_tick(
                    client=client, spool=spool, state=state, runner_id=settings.runner_id
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning("runner_tick_failed", exc_info=True)
            await asyncio.sleep(settings.tick_interval_seconds)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, task: asyncio.Task[None]) -> None:
    """Wire SIGTERM/SIGINT to cancel *task* for a clean shutdown."""

    def _cancel(signum: int) -> None:
        _log.info("runner_shutdown_signal", signal=signal.Signals(signum).name)
        task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        # ``add_signal_handler`` is unimplemented on some platforms
        # (e.g. Windows ProactorEventLoop); tolerate its absence.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _cancel, sig)


async def _async_main(settings: RunnerSettings) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(_run_loop(settings), name="runner-tick-loop")
    _install_signal_handlers(loop, task)
    with contextlib.suppress(asyncio.CancelledError):
        await task


def run_runner() -> None:
    """Configure logging, load config, import connectors, run the loop.

    Raises:
        RunnerConfigError: a required ``MEHO_RUNNER_*`` var is missing or
            malformed. The ``__main__`` entrypoint catches it and exits 1.
    """
    configure_logging()
    settings = get_runner_settings()
    _eager_import_connectors()
    asyncio.run(_async_main(settings))
