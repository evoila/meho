# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Resolver microbenchmark for the G6.3 PII overrides cache (#383).

Per Initiative #376's Definition of Done line *"Per-tenant override
cache holds <1ms p99 lookup on the publish path; verified via load
test"*, this module times
:func:`~meho_backplane.broadcast.overrides.compute_effective_broadcast_detail`
under the realistic worst case the Initiative committed to: 100 rules
in a single tenant (tenants typically run < 10; the order-of-magnitude
headroom is the safety margin).

Shape
=====

1. Boot the package-level Postgres testcontainer via ``pg_engine``
   and seed 100 :class:`BroadcastOverride` rows under tenant A.
2. Issue one resolver call to warm the per-tenant cache (the first
   call within a TTL window is the only one that hits the DB; every
   subsequent call inside the same ~60 s TTL serves from the
   in-process dict).
3. Time 10 000 resolver calls back-to-back with
   :func:`time.perf_counter_ns` to keep nanosecond resolution
   regardless of platform clock granularity.
4. Compute p99 via :func:`statistics.quantiles` with ``n=100`` (each
   cut point separates 1 % buckets; the 99th cut is the p99 lower
   bound, which is the correct conservative reading for "no more
   than 1 % of calls exceed this").
5. Assert ``p99 < 1 ms`` per the Initiative DoD. Log the realised p50
   / p95 / p99 alongside the count so future regressions surface a
   readable delta in the test output.

Marker discipline
=================

Decorated with ``@pytest.mark.load`` per the issue body. The marker
is registered in ``backend/pyproject.toml`` and the default ``addopts
= ["-m", "not load"]`` excludes it from the always-on lane; ``pytest
-m load`` selects it explicitly. The body also self-skips when
``MEHO_RUN_LOAD_TESTS`` is not set so an accidental ``pytest -m load``
in the always-on lane reports the gating contract instead of
spending the wall-clock budget. Same env-var-plus-marker discipline
:mod:`tests.integration.test_broadcast_load` uses with ``slow`` and
``MEHO_RUN_SLOW_TESTS``.
"""

from __future__ import annotations

import logging
import os
import statistics
import time
from typing import Final
from uuid import UUID

import pytest

from meho_backplane.broadcast.overrides import (
    compute_effective_broadcast_detail,
    reset_overrides_cache_for_testing,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import BroadcastOverride

_log = logging.getLogger(__name__)

#: Pinned tenant -- matches the integration conftest's ``pg_engine``
#: re-seed so the lookups exercise the real tenancy-scoped index path.
_TENANT_A: Final[UUID] = UUID("11111111-1111-1111-1111-111111111111")

#: Realistic worst case the Initiative committed to. Tenants typically
#: ship < 10 rules each; 100 is the safety margin the DoD pinned.
_SEED_RULE_COUNT: Final[int] = 100

#: Sample size the timing loop runs; 10 000 keeps p99 the
#: 100th-worst call (one bucket per percentile) so the assertion is
#: against a stable statistic, not a single tail outlier.
_TIMING_SAMPLES: Final[int] = 10_000

#: DoD threshold from Initiative #376: p99 < 1 ms on the publish
#: hot path. Expressed in nanoseconds for direct comparison against
#: :func:`time.perf_counter_ns` deltas (no float drift).
_P99_THRESHOLD_NS: Final[int] = 1_000_000  # 1 ms

#: Self-skip env var. Matches the marker name so the operator can
#: discover the gate from the marker alone:
#: ``MEHO_RUN_LOAD_TESTS=1 uv run pytest -m load``.
_RUN_LOAD_TESTS: Final[bool] = os.environ.get("MEHO_RUN_LOAD_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


async def _seed_rules(tenant_id: UUID, count: int) -> None:
    """Insert *count* rules under *tenant_id*.

    The patterns are designed to be intentionally non-matching for
    the timing query's op_id below, so the resolver walks the full
    rule list every call (``fnmatch.fnmatchcase`` returns False on
    every row, modelling the realistic mostly-non-matching path).
    A small number of distinct patterns + distinct scope tuples
    keeps PG's btree happy and avoids the composite-unique-index
    409 path.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for idx in range(count):
            session.add(
                BroadcastOverride(
                    tenant_id=tenant_id,
                    # Distinct patterns guarantee the composite-unique
                    # index never trips even though every row shares
                    # the same scope tuple shape.
                    op_id_pattern=f"benchmark.op.{idx:04d}",
                    scope_field="namespace",
                    scope_value=f"ns-{idx:04d}",
                    detail="aggregate",
                    created_by_sub="loadtest",
                ),
            )
        await session.commit()


@pytest.mark.load
@pytest.mark.skipif(
    not _RUN_LOAD_TESTS,
    reason="Load harness -- opt in via MEHO_RUN_LOAD_TESTS=1 (or `pytest -m load`).",
)
async def test_resolver_p99_under_one_millisecond(pg_engine: None) -> None:
    """Microbenchmark: 10 000 calls, p99 < 1 ms with a 100-rule cache."""
    reset_overrides_cache_for_testing()
    await _seed_rules(_TENANT_A, _SEED_RULE_COUNT)

    # Warm the per-tenant cache so the timing loop measures the
    # in-process dict + fnmatch + scope-match path, not the cache-
    # miss DB pull (which itself is < 5 ms on a local PG but is
    # explicitly outside the publish hot path the DoD measures).
    await compute_effective_broadcast_detail(
        op_id="meho.audit.query",
        tenant_id=_TENANT_A,
        raw_params={"namespace": "production"},
        request_override=None,
    )

    durations_ns: list[int] = []
    for _ in range(_TIMING_SAMPLES):
        start = time.perf_counter_ns()
        await compute_effective_broadcast_detail(
            op_id="meho.audit.query",
            tenant_id=_TENANT_A,
            raw_params={"namespace": "production"},
            request_override=None,
        )
        durations_ns.append(time.perf_counter_ns() - start)

    quantiles = statistics.quantiles(durations_ns, n=100)
    p50_ns = quantiles[49]
    p95_ns = quantiles[94]
    p99_ns = quantiles[98]

    _log.info(
        "resolver_p99_benchmark samples=%d p50=%dns p95=%dns p99=%dns threshold=%dns",
        _TIMING_SAMPLES,
        p50_ns,
        p95_ns,
        p99_ns,
        _P99_THRESHOLD_NS,
    )
    assert p99_ns < _P99_THRESHOLD_NS, (
        f"resolver p99 = {p99_ns / 1_000:.1f} us, "
        f"threshold = {_P99_THRESHOLD_NS / 1_000:.1f} us "
        f"(p50={p50_ns / 1_000:.1f} us, p95={p95_ns / 1_000:.1f} us, "
        f"samples={_TIMING_SAMPLES})"
    )


def test_module_imports_cleanly() -> None:
    """Module-import smoke -- runs even without the load marker selected."""
    assert callable(compute_effective_broadcast_detail)
    assert callable(_seed_rules)
    assert UUID("11111111-1111-1111-1111-111111111111") == _TENANT_A
