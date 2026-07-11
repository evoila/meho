# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Regression pin: a large-spec **non-dry-run** ingest keeps the event loop responsive.

At v0.8.0 the ``dry_run=false`` ingest of a real-world OpenAPI spec (the
canonical signal: 7.5 MB / 1275 typed ops) ran the parse + DB-commit +
LLM-grouping passes synchronously on the request thread. It starved the
kubelet liveness probe for ~30 s and the pod was graceful-killed
mid-ingest (exitCode 0 / reason "Completed" — not OOM). The documented
remediation for a missing L2 composite pointed operators *at* that
command, so every affected composite became a dead-end.

The crash mechanics were fixed on main incrementally:

* the route hands the pipeline off the request thread via
  ``asyncio.create_task`` and returns ``202`` + a job handle
  (#2275 / PR #2317);
* the synchronous parse + proto-build run inside ``asyncio.to_thread``
  (``operations/ingest/pipeline.py`` ``_register_one_spec``);
* YAML uses ``CSafeLoader``.

What was never pinned end-to-end is the *event-loop responsiveness* of a
**non-dry-run** ingest of the 1275-op class — specifically that the
DB-commit pass yields (it awaits per-op, one commit per spec) rather than
blocking the loop for the whole batch. This module is that pin. Two of
the tests drive :meth:`IngestionPipelineService.ingest` (``dry_run=False``)
concurrently with a heartbeat coroutine and assert the loop kept making
progress; the third pins that the ``catalog_entry`` (``--catalog``) shape
resolves onto the same async job path rather than a separate synchronous
one.

These are the failure modes each test would catch if the fix regressed:

* :func:`test_nondryrun_large_spec_ingest_keeps_event_loop_responsive` —
  a register/commit pass that stopped yielding (one big synchronous
  batch) would collapse the heartbeat's tick count and spike a single
  gap past the ceiling.
* :func:`test_blocking_parse_stays_off_the_event_loop` — dropping the
  ``asyncio.to_thread`` hop (running the parse on the loop) would freeze
  the loop for the whole parse; the deterministic injected block makes
  that crisp and CPU-speed-independent.
* :func:`test_catalog_entry_ingest_rides_the_async_job_path` — a
  ``--catalog`` request that fell onto the synchronous branch would
  reintroduce the crash for the exact command the catalog next-step hint
  prints.

The pipeline is driven directly (not through the HTTP 202 wrapper): the
blocking risk lives inside ``ingest()``; the ``create_task`` wrapper only
detaches it. The grouping phase is stubbed to an empty result — it needs
an LLM and is out of scope for the responsiveness proof — while the real
parse + register passes run against the autouse-migrated sqlite engine.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.ingest import GroupingResult, SpecSource
from meho_backplane.operations.ingest.api_schemas import IngestRequest
from meho_backplane.operations.ingest.pipeline import IngestionPipelineService

_PRODUCT = "loadtest"
_VERSION = "1.0"
_IMPL_ID = "loadtest-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"

#: The op count the canonical 7.5 MB vmware/9.0 signal carried. Kept at
#: the real number so this is a proof for the "1275-op class", not a
#: scaled-down proxy.
_OP_COUNT = 1275

#: Heartbeat cadence. The loop is polled every 10 ms; a gap materially
#: larger than this means the loop was starved for that long.
_HEARTBEAT_INTERVAL_S = 0.01

#: Ceiling on any single observed heartbeat gap during the real
#: end-to-end ingest. The parse (in ``to_thread``) contends for the GIL
#: and produces a single ~0.1 s gap locally; the register pass yields
#: per-op with gaps <= ~0.03 s. 0.5 s is ~5x over the observed worst
#: case (CI-slack headroom) yet an order of magnitude below the ~1.7 s a
#: non-yielding register batch, or the ~30 s the pre-fix synchronous
#: pass, would produce.
_MAX_GAP_CEILING_S = 0.5

#: A ~2 s ingest polled every 10 ms yields ~170 ticks locally. Requiring
#: >= 50 proves the loop ran *throughout* the ingest, not just before and
#: after it — a starved loop would tick only a handful of times.
_MIN_TICKS = 50


def _synth_openapi_spec(op_count: int) -> str:
    """Build a structurally valid OpenAPI 3.1 spec with *op_count* GET ops.

    Synthesised programmatically rather than committed as a ~7 MB fixture:
    the DB-commit responsiveness proof needs *op count*, not real vendor
    bytes, and a checked-in multi-MB blob is a maintenance and repo-size
    cost the pin does not require. Each path carries a distinct
    ``operationId`` so the parser emits ``op_count`` non-colliding
    descriptor protos. ``info.version`` shares the major/minor band of the
    ``_VERSION`` label so the spec-vs-label cross-check proceeds.
    """
    paths: dict[str, Any] = {
        f"/resources/{i}": {
            "get": {
                "operationId": f"getResource{i}",
                "summary": f"Read resource {i}",
                "responses": {"200": {"description": "ok"}},
            }
        }
        for i in range(op_count)
    }
    return json.dumps(
        {
            "openapi": "3.1.0",
            "info": {"title": "Loadtest fixture", "version": "1.0.0"},
            "paths": paths,
        }
    )


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars engine / Operator construction depend on transitively."""
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


def _operator() -> Operator:
    return Operator(
        sub=f"test-operator-{uuid.uuid4()}",
        name="Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def _stub_embedding() -> AsyncMock:
    """Stub the embedding service so the register pass never pulls fastembed."""
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def _service(monkeypatch: pytest.MonkeyPatch) -> IngestionPipelineService:
    """A pipeline service wired to sqlite with the grouping phase stubbed out."""
    service = IngestionPipelineService(
        _operator(),
        sessionmaker=get_sessionmaker(),
        embedding_service=_stub_embedding(),
    )

    async def _no_grouping(**_kwargs: Any) -> GroupingResult:
        return GroupingResult(
            connector_id=_CONNECTOR_ID,
            groups_created=0,
            operations_assigned=0,
            operations_unassigned=0,
            llm_call_count=0,
            llm_duration_ms=0.0,
        )

    monkeypatch.setattr(service, "_run_grouping_phase", _no_grouping)
    return service


async def _run_with_heartbeat(work: Awaitable[Any]) -> tuple[Any, list[float]]:
    """Await *work* while a heartbeat coroutine measures event-loop gaps.

    The heartbeat re-arms an ``asyncio.sleep(interval)`` in a loop and
    records the *actual* wall gap between wake-ups. Under a responsive
    loop each gap hugs ``interval``; a synchronous block on the loop
    stretches the gap spanning it to ~= the block duration, because the
    sleep's callback cannot fire until the loop is free again. The gap
    series is the loop-lag signal the assertions read.
    """
    stop = asyncio.Event()
    armed = asyncio.Event()
    gaps: list[float] = []

    async def _heartbeat() -> None:
        loop = asyncio.get_running_loop()
        armed.set()
        prev = loop.time()
        while not stop.is_set():
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            now = loop.time()
            gaps.append(now - prev)
            prev = now

    beat = asyncio.create_task(_heartbeat())
    # Arm the heartbeat *before* the work begins: let it run up to its
    # first parked ``asyncio.sleep``. Without this, a synchronous block at
    # the very start of *work* — before the heartbeat is ever scheduled —
    # would starve the heartbeat into recording *zero* ticks rather than
    # one long gap, so a stall at the head of the coroutine could slip past
    # a max-gap assertion. Armed, an on-loop block instead delays the
    # parked sleep's callback and surfaces as a single gap ~= its duration.
    await armed.wait()
    await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
    try:
        result = await work
    finally:
        stop.set()
        await beat
    return result, gaps


@pytest.mark.asyncio
async def test_nondryrun_large_spec_ingest_keeps_event_loop_responsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-dry-run 1275-op ingest commits every op without starving the loop.

    Drives the real parse + register (DB-commit) passes against sqlite —
    the one leg the crash signal implicated that was not yet pinned
    end-to-end — while a heartbeat measures loop lag. Asserts the commit
    landed all 1275 ops (so this is genuinely the non-dry-run path, not a
    parse-only dry run) and that the loop kept ticking throughout with no
    single gap past the ceiling.
    """
    service = _service(monkeypatch)
    spec = SpecSource(uri="spec:loadtest-large", content=_synth_openapi_spec(_OP_COUNT))

    result, gaps = await _run_with_heartbeat(
        service.ingest(
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            specs=[spec],
            tenant_id=None,
            dry_run=False,
        )
    )

    # The full non-dry-run commit landed — every op persisted.
    assert result.ingestion.inserted_count == _OP_COUNT, result.ingestion
    assert result.ingestion.connector_registered is True

    # The loop made progress *during* the ingest (not just around it), and
    # never froze for a materially long stretch.
    assert len(gaps) >= _MIN_TICKS, (
        f"heartbeat only ticked {len(gaps)} times during the ingest; "
        "the event loop was starved (a non-yielding commit pass would do this)"
    )
    worst = max(gaps)
    assert worst < _MAX_GAP_CEILING_S, (
        f"event loop stalled for {worst:.3f}s during the ingest "
        f"(ceiling {_MAX_GAP_CEILING_S}s); the parse/commit pass is blocking the loop"
    )


@pytest.mark.asyncio
async def test_blocking_parse_stays_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deliberately-blocking parse must not freeze the loop — proving the ``to_thread`` hop.

    Deterministic companion to the timing-sensitive end-to-end test:
    instead of relying on the real parse being CPU-heavy enough to expose
    an on-loop regression, inject a fixed blocking ``time.sleep`` into the
    parse call. Because ``_register_one_spec`` runs it via
    ``asyncio.to_thread``, the block executes on a worker thread and the
    loop stays free — the heartbeat keeps ticking and the worst gap stays
    far below the block. If the ``to_thread`` hop were removed the block
    would run on the loop and the worst gap would be >= the block, failing
    this test regardless of the runner's CPU speed.
    """
    block_s = 0.3
    real_parse = None
    from meho_backplane.operations.ingest import pipeline as _pipeline_mod

    real_parse = _pipeline_mod.parse_openapi_with_provenance

    def _blocking_parse(*args: Any, **kwargs: Any) -> Any:
        # Runs on the ``to_thread`` worker in the real code path; the
        # blocking sleep here therefore must not be visible to the loop.
        time.sleep(block_s)
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(_pipeline_mod, "parse_openapi_with_provenance", _blocking_parse)

    service = _service(monkeypatch)
    # A tiny spec keeps the register pass negligible so the block dominates
    # the run and the assertion isolates the parse-offload property.
    spec = SpecSource(uri="spec:loadtest-tiny", content=_synth_openapi_spec(3))

    _result, gaps = await _run_with_heartbeat(
        service.ingest(
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            specs=[spec],
            tenant_id=None,
            dry_run=False,
        )
    )

    worst = max(gaps)
    assert worst < block_s / 2, (
        f"event loop stalled for {worst:.3f}s while a {block_s}s parse ran; "
        "the parse is not being offloaded to a thread (asyncio.to_thread regressed)"
    )
    # The loop ticked repeatedly *while* the block ran — direct evidence the
    # block was off-loop (a ~0.3 s block at 10 ms cadence yields ~25 ticks).
    ticks_during_block = sum(1 for g in gaps if g < block_s / 2)
    assert ticks_during_block >= 15, (
        f"only {ticks_during_block} responsive ticks during the {block_s}s parse; "
        "the loop was not free while the parse ran"
    )


def _install_fake_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the ingest route's ``load_catalog`` at a single synthetic entry.

    ``load_catalog`` is ``lru_cache``-d and imported into the route module,
    so the cache is cleared and the route-module symbol is patched — the
    same swap the existing route tests use.
    """
    from meho_backplane.operations.ingest import catalog as _catalog_mod
    from meho_backplane.operations.ingest.catalog import (
        ConnectorSpecCatalog,
        ConnectorSpecEntry,
    )

    fake = ConnectorSpecCatalog(
        entries=(
            ConnectorSpecEntry(
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                requires_connector_class="GenericRestConnector",
                upstream=("https://catalog.test/loadtest/spec.yaml",),
            ),
        )
    )
    _catalog_mod.load_catalog.cache_clear()
    import meho_backplane.api.v1.connectors_ingest as _route_mod

    monkeypatch.setattr(_route_mod, "load_catalog", lambda: fake)


def test_catalog_entry_ingest_rides_the_async_job_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``catalog_entry`` (``--catalog``) request resolves onto the async 202 path.

    The catalog next-step hint prints
    ``meho connector ingest --catalog <product>/<version>`` as the
    remediation for an un-ingested connector. That command is only safe if
    it rides the off-thread job path rather than the synchronous pass that
    crashed the pod. The route resolves ``catalog_entry`` to the
    explicit-quadruple shape *before* the sync-vs-async branch, so a
    resolved catalog body carries the request's ``async_`` / ``dry_run``
    verbatim and takes the identical branch. This pins that invariant: the
    resolved body defaults to ``async_=True`` / ``dry_run=False``, which is
    exactly the ``202`` + job-handle path.
    """
    _install_fake_catalog(monkeypatch)
    from meho_backplane.api.v1.connectors_ingest import _resolve_catalog_entry_if_set

    body = IngestRequest(catalog_entry=f"{_PRODUCT}/{_VERSION}")
    resolved, _compat = _resolve_catalog_entry_if_set(body)

    # Resolved into the explicit-quadruple shape the shared pipeline path
    # consumes — the catalog shape is sugar over the same dispatch.
    assert resolved.catalog_entry is None
    assert resolved.product == _PRODUCT
    assert resolved.version == _VERSION
    assert resolved.impl_id == _IMPL_ID
    assert [s.uri for s in resolved.specs] == ["https://catalog.test/loadtest/spec.yaml"]

    # The route dispatches synchronously iff ``dry_run or not async_``.
    # A default catalog request is neither, so it takes the async job path.
    assert resolved.dry_run is False
    assert resolved.async_ is True
    takes_sync_path = resolved.dry_run or not resolved.async_
    assert takes_sync_path is False, (
        "a --catalog ingest fell onto the synchronous pass — it must ride "
        "the same 202 + job-handle path the async default provides"
    )
