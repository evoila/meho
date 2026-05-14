# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Application-layer load harness for the broadcast publish→SSE→MCP seam (G6.1-T7 shape #1).

50 RPS for 30 seconds = 1500 events, fanned across two tenants
(750 each). Two SSE consumers tail their tenant's stream by driving
:func:`~meho_backplane.api.v1.feed._feed_generator` directly — the
HTTP layer adds a cancellation race that PR #353 explicitly avoided
by switching the unit suite to direct-generator drives; this harness
follows the same discipline. After the producers finish, one MCP
poll per tenant via :func:`~meho_backplane.mcp.resources.tenant_feed._tenant_feed_handler`
verifies the snapshot surface.

Asserts (5 ACs from #386 shape #1):

1. All 1500 events appear in the SSE feed (one rendered frame per
   event).
2. p99 publish→SSE-receive end-to-end latency < 5 s (hard fail).
   The < 1 s target is informational — logged but not gated, per
   the AC's escape valve.
3. Tenant boundary: tenant-A's SSE consumer never receives an event
   tagged ``tenant_id == TENANT_B`` (and vice versa).
4. ``broadcast_publish_errors_total`` Prometheus counter has zero
   delta across the run.
5. ``broadcast_events_published_total{op_class=read,result_status=ok}``
   increments by exactly 1500.

Plus one MCP-side check per tenant: the final ``_tenant_feed_handler``
poll returns the last 50 events for that tenant, with every entry
tagged the correct ``tenant_id``.

Shape #2 (chart-CI integration + Valkey-pod chaos) lands in a separate
PR after the chart-CI hardening soft dependency (PR #347 follow-up) is
satisfied.

Slow-test gating
================

The test runs the full 30 s production load shape — that's the AC.
Wrapping with ``@pytest.mark.slow`` keeps the default test suite under
its existing wall-clock budget; CI's slow lane sets
``MEHO_RUN_SLOW_TESTS=1`` and the chart-CI shape-#2 follow-up will
move this test (or its successor) into the chart-CI gating lane.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import statistics
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import redis.exceptions
import structlog

from meho_backplane.api.v1.feed import _feed_generator
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    BROADCAST_EVENTS_PUBLISHED_TOTAL,
    BROADCAST_PUBLISH_ERRORS_TOTAL,
    BroadcastEvent,
    dispose_broadcast_client,
    publish_event,
    reset_broadcast_client_for_testing,
)
from meho_backplane.mcp.resources.tenant_feed import _tenant_feed_handler
from meho_backplane.settings import get_settings

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Shape constants — match the AC verbatim
# ---------------------------------------------------------------------------

#: Aggregate request rate across both tenants. AC #1 spec value.
_TOTAL_RPS: float = 50.0
#: Wall-clock duration of the load run. AC #1 spec value.
_DURATION_SECONDS: float = 30.0
#: Total events produced across both tenants — derived from above.
#: Pinned at 1500 to match the AC's "1500 events total" verbatim.
_TOTAL_EVENTS: int = int(_TOTAL_RPS * _DURATION_SECONDS)
#: Two tenants partitioned evenly. AC #4 (tenant boundary) requires
#: at least two distinct tenants in flight at the same time.
_NUM_TENANTS: int = 2
#: Per-tenant publish count — 750 each at 25 RPS each, summing to 50
#: RPS aggregate.
_EVENTS_PER_TENANT: int = _TOTAL_EVENTS // _NUM_TENANTS
#: Per-tenant request rate — 25 RPS each, 50 RPS aggregate.
_PER_TENANT_RPS: float = _TOTAL_RPS / _NUM_TENANTS

#: Hard-fail threshold for p99 publish→SSE-receive latency. Set well
#: above the < 1 s documented target because dev-machine event-loop
#: scheduling jitter routinely pushes p99 into the 1-2 s range under
#: load — flagging "real regression only" matches the AC's escape
#: valve ("fail only on > 5 s p99 (real regression)").
_P99_HARD_FAIL_SECONDS: float = 5.0
#: Documented (non-gating) target. Logged for visibility; failing
#: this informational check would alert on infrastructure regressions
#: in a future CI lane that wires a baseline-comparison check.
_P99_TARGET_SECONDS: float = 1.0

#: Pinned tenant UUIDs — match the integration conftest's seeded
#: tenant rows. Reusing the existing constants keeps the test
#: harness's tenant identity stable across the suite.
_TENANT_A: UUID = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B: UUID = UUID("22222222-2222-2222-2222-222222222222")
_TENANTS: tuple[UUID, ...] = (_TENANT_A, _TENANT_B)

#: How long the SSE consumers stay parked after producers finish.
#: The producer is rate-limited at 50 RPS, so by the time the last
#: event lands in Valkey, the consumers' XREAD BLOCK loops only need
#: one more cycle to drain. 5 s is generous; any event still missing
#: after this drain is a real bug, not a timing artefact.
_DRAIN_TIMEOUT_SECONDS: float = 5.0


# ---------------------------------------------------------------------------
# Gating — Docker socket + slow-mark env var
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_DOCKER_AVAILABLE: bool = _docker_socket_present()
_RUN_SLOW_TESTS: bool = os.environ.get("MEHO_RUN_SLOW_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(tenant_id: UUID, idx: int) -> BroadcastEvent:
    """Build a minimal :class:`BroadcastEvent` for the load run.

    All fields are intentionally constant across the run except
    ``event_id`` (uniqueness for latency tracking), ``audit_id``
    (same shape as production where each audit row gets a fresh UUID),
    and ``tenant_id`` (partitions the stream).
    """
    return BroadcastEvent(
        event_id=uuid4(),
        ts=datetime.now(UTC),
        tenant_id=tenant_id,
        principal_sub=f"loadtest-tenant-{idx}",
        op_id="vsphere.vm.list",
        op_class="read",
        result_status="ok",
        audit_id=uuid4(),
        payload={"idx": idx},
    )


def _operator(tenant_id: UUID) -> Operator:
    """Construct an :class:`Operator` for the SSE generator without a JWT.

    The generator's contract takes the operator object directly, not a
    JWT — the production route extracts the operator via
    ``Depends(require_role(...))`` *before* calling the generator.
    Driving the generator directly skips the JWT chain.
    """
    return Operator(
        sub="loadtest-subscriber",
        name=None,
        email=None,
        raw_jwt="<test-loadtest>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _producer(
    tenant_id: UUID,
    count: int,
    per_tenant_rps: float,
    publish_times: dict[UUID, float],
) -> None:
    """Publish *count* events at *per_tenant_rps* RPS into the per-tenant stream.

    Records ``publish_times[event_id] = perf_counter()`` *immediately
    before* :func:`publish_event` runs — the latency measurement is
    "time from the producer's intent to send" to "time the consumer's
    SSE generator yielded the frame", which is what end-to-end SSE
    delivery latency means operationally.

    Rate limiting is a simple per-iteration sleep that compensates for
    the publish call's own wall-clock cost. Drift over 30 s is
    sub-100ms in practice; the harness doesn't fan out to a separate
    producer pool because 25 RPS per task is well inside one event
    loop's capacity.
    """
    interval = 1.0 / per_tenant_rps
    for i in range(count):
        loop_start = time.perf_counter()
        event = _make_event(tenant_id, i)
        publish_times[event.event_id] = time.perf_counter()
        await publish_event(event)
        elapsed = time.perf_counter() - loop_start
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)


def _parse_frame(frame: str) -> tuple[UUID, UUID] | None:
    """Extract ``(event_id, tenant_id)`` from one SSE frame, or ``None``.

    Heartbeat comment frames (``: heartbeat\\n\\n``) return ``None``.
    Malformed frames also return ``None``; the consumer drops them
    silently to match the SSE generator's own forward-compat skip.
    """
    if frame.startswith(":"):
        return None
    data_line: str | None = None
    for line in frame.split("\n"):
        if line.startswith("data: "):
            data_line = line[len("data: ") :]
            break
    if data_line is None:
        return None
    try:
        decoded = json.loads(data_line)
        return UUID(decoded["event_id"]), UUID(decoded["tenant_id"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


async def _sse_consumer(
    tenant_id: UUID,
    expected: int,
    receive_times: dict[UUID, float],
    seen_tenant_ids: set[UUID],
) -> None:
    """Tail the per-tenant SSE feed; record receive timestamps + tenant ids seen.

    The consumer drives :func:`_feed_generator` with ``cursor="0"``
    (Valkey's "from the beginning of the stream") so it doesn't miss
    events that get published in the gap between consumer startup and
    the first ``XREAD`` BLOCK landing. Production SSE clients use
    ``$`` (live tail) or a ``Last-Event-Id`` cursor — both are
    XREAD-shape-compatible with the from-beginning cursor used here.

    Termination:

    * **All expected events received** — break out of the loop. By
      the time the producer's :func:`publish_event` returns, the XADD
      has hit Valkey and the consumer's BLOCK XREAD wakes within
      milliseconds with the frame. The "saw all 750 events" path is
      the steady-state exit.
    * **Generator stalls past 5 s of quiet** — redis-py's
      ``socket_timeout=5.0`` (pinned in ``broadcast/client.py:77``)
      raises :class:`redis.exceptions.TimeoutError` from inside the
      BLOCK XREAD when no entries arrive within the socket-read
      window. Absorbed here as the "drained" signal; the assertion
      below catches any genuinely missed events with a clear count
      delta. Latent inconsistency with the SSE generator's
      ``_XREAD_BLOCK_MS=30_000`` window — recorded as an adjacent
      finding in the PR body.

    The generator's ``aclose()`` runs via the ``finally`` arm so the
    redis-py BLOCK is cancelled cleanly when the consumer exits early.
    """
    gen = _feed_generator(
        operator=_operator(tenant_id),
        cursor="0",
        op_class=None,
        principal=None,
        target=None,
    )
    seen = 0
    try:
        try:
            async for frame in gen:
                parsed = _parse_frame(frame)
                if parsed is None:
                    continue
                event_id, frame_tenant_id = parsed
                seen_tenant_ids.add(frame_tenant_id)
                # First sighting wins — duplicates from at-most-once
                # publish semantics are not expected, but a duplicate
                # entry-id sighting would just overwrite the same value.
                receive_times.setdefault(event_id, time.perf_counter())
                seen += 1
                if seen >= expected:
                    break
        except redis.exceptions.TimeoutError:
            # 5 s of quiet on the per-tenant stream — production
            # XREAD BLOCK was 30 s but the redis-py socket_timeout
            # caps reads at 5 s. Drained.
            pass
    finally:
        await gen.aclose()


def _counter_value(counter, **labels: str) -> float:
    """Read the current ``_value`` of a prometheus-client Counter.

    Labelled counters require ``.labels(...)._value.get()``; unlabelled
    counters expose ``._value.get()`` directly. Sniff by checking for
    the public ``_metrics`` dict — labelled counters maintain it.
    Bypasses the public ``Collector.collect()`` snapshot to keep the
    delta read fast (we call this in a hot path that runs per tenant
    per assertion).
    """
    if labels:
        return float(counter.labels(**labels)._value.get())
    return float(counter._value.get())


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(
    not _DOCKER_AVAILABLE,
    reason="Docker socket unavailable; runs in CI where Valkey container provisions.",
)
@pytest.mark.skipif(
    not _RUN_SLOW_TESTS,
    reason="Slow harness — opt in via MEHO_RUN_SLOW_TESTS=1 (CI slow lane).",
)
class TestBroadcastLoad:
    """Shape #1: application-layer asyncio harness against testcontainers Valkey.

    No HTTP layer, no Postgres, no audit middleware — the harness
    measures the publish→SSE→MCP seam by driving the publisher and
    the consumer generator/handler directly. Sufficient for shape
    #1's AC contract; shape #2 wires the same load shape through the
    chart-CI helm-test job once chart-CI hardening lands.
    """

    @pytest.fixture
    async def valkey_url(self, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
        """Boot a Valkey container, pin env vars, yield the URL.

        Mirrors :class:`tests.test_broadcast_publisher.TestBroadcastIntegration`'s
        fixture verbatim — same image override env var, same env-var
        pinning shape, same cache-clear ordering. The MCP / SSE
        consumers both call :func:`get_broadcast_client` which honours
        ``BROADCAST_REDIS_URL``.
        """
        from testcontainers.redis import RedisContainer

        image = os.environ.get("MEHO_TEST_VALKEY_IMAGE", "valkey/valkey:8")
        with RedisContainer(image) as container:
            host = container.get_container_host_ip()
            port = container.get_exposed_port(6379)
            url = f"redis://{host}:{port}"
            monkeypatch.setenv("BROADCAST_REDIS_URL", url)
            monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/m")
            monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
            monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
            monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
            get_settings.cache_clear()
            reset_broadcast_client_for_testing()
            try:
                yield url
            finally:
                await dispose_broadcast_client()
                get_settings.cache_clear()

    async def test_50_rps_30s_1500_events(self, valkey_url: str) -> None:
        """All 5 shape-#1 ACs in one harness run."""
        # AC #4 / #5 — capture counter baselines before any publish.
        # The Prometheus counters are process-global and accumulate
        # across the suite; the assertion checks deltas.
        published_baseline = _counter_value(
            BROADCAST_EVENTS_PUBLISHED_TOTAL, op_class="read", result_status="ok"
        )
        errors_baseline = _counter_value(BROADCAST_PUBLISH_ERRORS_TOTAL)

        # Shared state — single-event-loop asyncio guarantees no
        # concurrent mutation, so a plain dict is safe across the
        # producer + consumer tasks.
        publish_times: dict[UUID, float] = {}
        receive_times_a: dict[UUID, float] = {}
        receive_times_b: dict[UUID, float] = {}
        seen_tenant_ids_a: set[UUID] = set()
        seen_tenant_ids_b: set[UUID] = set()

        # Start consumers FIRST so they're parked on XREAD before any
        # event lands. cursor="0" guarantees from-beginning replay so
        # the consumer-startup gap doesn't drop events, but starting
        # the consumers first keeps the steady-state latency
        # measurement honest.
        consumer_a = asyncio.create_task(
            _sse_consumer(
                tenant_id=_TENANT_A,
                expected=_EVENTS_PER_TENANT,
                receive_times=receive_times_a,
                seen_tenant_ids=seen_tenant_ids_a,
            )
        )
        consumer_b = asyncio.create_task(
            _sse_consumer(
                tenant_id=_TENANT_B,
                expected=_EVENTS_PER_TENANT,
                receive_times=receive_times_b,
                seen_tenant_ids=seen_tenant_ids_b,
            )
        )
        # Give the consumers one event-loop tick to enter their XREAD
        # BLOCK; without this the producers can race ahead of the
        # subscriber, which is operationally fine (cursor="0" replays)
        # but adds latency noise to the first few events' measurements.
        await asyncio.sleep(0.05)

        # Run both producers concurrently. asyncio.gather completes when
        # both producers have published their 750 events; total
        # wall-clock = ~30 s (rate-limited).
        producer_a = _producer(
            tenant_id=_TENANT_A,
            count=_EVENTS_PER_TENANT,
            per_tenant_rps=_PER_TENANT_RPS,
            publish_times=publish_times,
        )
        producer_b = _producer(
            tenant_id=_TENANT_B,
            count=_EVENTS_PER_TENANT,
            per_tenant_rps=_PER_TENANT_RPS,
            publish_times=publish_times,
        )
        producer_start = time.perf_counter()
        await asyncio.gather(producer_a, producer_b)
        producer_wall_seconds = time.perf_counter() - producer_start

        # Drain — let consumers process the tail of events. By the
        # time ``producer.gather()`` returns each producer's final
        # XADD has hit Valkey; the consumer's BLOCK XREAD wakes
        # within milliseconds with the trailing frames and breaks
        # out via ``seen >= expected``. The consumer's internal
        # ``redis.exceptions.TimeoutError`` catch absorbs the 5 s
        # socket-timeout floor if for any reason the trailing frames
        # don't arrive — the assertion grid below catches missing
        # events explicitly. Cap the drain in case a consumer task
        # gets wedged beyond both exits (defensive; never observed).
        try:
            await asyncio.wait_for(
                asyncio.gather(consumer_a, consumer_b),
                timeout=_DRAIN_TIMEOUT_SECONDS + 6.0,  # 5s socket + 1s margin
            )
        except TimeoutError:
            consumer_a.cancel()
            consumer_b.cancel()
            for task in (consumer_a, consumer_b):
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        # ─── AC #5 — publish counter ───────────────────────────────────
        published_delta = (
            _counter_value(BROADCAST_EVENTS_PUBLISHED_TOTAL, op_class="read", result_status="ok")
            - published_baseline
        )
        assert published_delta == _TOTAL_EVENTS, (
            f"broadcast_events_published_total delta = {published_delta}, expected {_TOTAL_EVENTS}"
        )

        # ─── AC #4 — publish-errors counter (must be 0) ───────────────
        errors_delta = _counter_value(BROADCAST_PUBLISH_ERRORS_TOTAL) - errors_baseline
        assert errors_delta == 0, (
            f"broadcast_publish_errors_total delta = {errors_delta}, expected 0"
        )

        # ─── AC #1 — every published event appears in SSE feed ────────
        published_ids = set(publish_times.keys())
        seen_a = set(receive_times_a.keys())
        seen_b = set(receive_times_b.keys())
        seen_total = seen_a | seen_b
        missing = published_ids - seen_total
        assert not missing, (
            f"{len(missing)} event(s) published but not seen on SSE: "
            f"{sorted(missing)[:5]}{'…' if len(missing) > 5 else ''}"
        )
        assert len(seen_total) == _TOTAL_EVENTS, (
            f"SSE consumers saw {len(seen_total)} unique events; expected {_TOTAL_EVENTS}"
        )

        # ─── AC #3 — tenant boundary ──────────────────────────────────
        # tenant-A's consumer must have seen ONLY events tagged
        # tenant_id == TENANT_A (and likewise for B). The SSE
        # generator filters by per-tenant stream key, so a leak here
        # would mean the publisher mis-keyed an event onto the wrong
        # stream — which would also break the existing per-tenant
        # tests, but the load-shape assertion catches it under
        # concurrent fan-in.
        assert seen_tenant_ids_a == {_TENANT_A}, (
            f"tenant-A consumer saw tenant_ids {seen_tenant_ids_a}; expected only {{{_TENANT_A}}}"
        )
        assert seen_tenant_ids_b == {_TENANT_B}, (
            f"tenant-B consumer saw tenant_ids {seen_tenant_ids_b}; expected only {{{_TENANT_B}}}"
        )

        # ─── AC #2 — p99 publish→SSE-receive latency ───────────────────
        latencies_s: list[float] = []
        for ev_id, publish_t in publish_times.items():
            receive_t = receive_times_a.get(ev_id) or receive_times_b.get(ev_id)
            if receive_t is not None:
                latencies_s.append(receive_t - publish_t)
        # statistics.quantiles(..., n=100) returns 99 cut points; the
        # p99 is the 99th cut point, index 98 in the 0-indexed slice.
        # Requires len >= 2; with 1500 samples this is trivially met.
        cuts = statistics.quantiles(latencies_s, n=100)
        p99 = cuts[98]
        p50 = cuts[49]
        max_latency = max(latencies_s)
        _log.info(
            "broadcast_load_p99",
            n=len(latencies_s),
            p50_s=round(p50, 3),
            p99_s=round(p99, 3),
            max_s=round(max_latency, 3),
            producer_wall_s=round(producer_wall_seconds, 2),
            target_p99_s=_P99_TARGET_SECONDS,
            hard_fail_p99_s=_P99_HARD_FAIL_SECONDS,
        )
        # Hard fail per the AC's escape-valve threshold. The < 1 s
        # target is recorded as informational above; future CI tooling
        # can wire a baseline-comparison check against the logged
        # p50/p99 numbers without changing this assertion.
        assert p99 < _P99_HARD_FAIL_SECONDS, (
            f"p99 SSE delivery latency {p99:.3f}s exceeds hard-fail "
            f"threshold {_P99_HARD_FAIL_SECONDS}s "
            f"(documented target {_P99_TARGET_SECONDS}s; n={len(latencies_s)})"
        )

        # ─── MCP snapshot — last 50 events visible per tenant ─────────
        # The MCP resource exposes the last 50 events in chronological
        # order. After the load run, the snapshot for each tenant
        # should return 50 events, all tagged with that tenant_id.
        # The exact event_ids returned are the most-recent 50, which
        # under the interleaved producer pattern won't match a clean
        # "last 50 by publish index" — but every snapshot event must
        # be among the produced set for that tenant.
        for tenant_id in _TENANTS:
            result = await _tenant_feed_handler(
                _operator(tenant_id),
                {"tenant_id": str(tenant_id)},
            )
            assert result["tenant_id"] == str(tenant_id)
            assert result["count"] == 50, (
                f"tenant {tenant_id} MCP snapshot returned {result['count']} "
                "events; expected exactly 50 (the resource's pinned snapshot size)"
            )
            for snapshot_event in result["events"]:
                assert snapshot_event["tenant_id"] == str(tenant_id), (
                    f"MCP snapshot for tenant {tenant_id} contained an event "
                    f"tagged with foreign tenant_id={snapshot_event['tenant_id']}"
                )
