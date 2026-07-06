# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G0.6.1-T3 (#753) acceptance tests for :class:`JsonFluxReducer`.

Three axes, per the Task:

* **pass-through** — a payload at/under the threshold returns unchanged
  with ``handle is None`` (the v0.1-spec §4 boundary; the agent sees the
  full small list inline).
* **materialize** — a payload over the threshold returns a reduced
  summary plus a populated :class:`ResultHandle` whose real fields
  (``handle_id`` / ``summary_md`` / ``schema_`` / ``total_rows`` /
  ``sample_rows`` / ``ttl_seconds``) reflect the DuckDB-materialized
  table, not a synthetic placeholder.
* **exception tolerance via the dispatcher** — a reducer that raises
  propagates as a ``connector_error`` :class:`OperationResult` through
  :func:`~meho_backplane.operations.dispatcher._reduce_or_error`, and the
  audit row + broadcast event still commit (the dispatcher's
  never-raises contract).

The third test wires a deliberately-broken reducer through the real
dispatch path the same way :mod:`tests.test_operations_dispatcher` does
(register a typed op, install the reducer via
:func:`~meho_backplane.operations.dispatcher.set_default_reducer`,
dispatch, assert on the structured error + the persisted audit row +
the captured broadcast event).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import (
    FetchMore,
    FetchMoreDrillIn,
    FetchMoreNativePagination,
    FingerprintResult,
    PaginationHint,
    ProbeResult,
    ResultHandle,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.jsonflux.query.engine import QueryEngine
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import (
    _TRUNCATION_MARKER,
    JsonFluxReducer,
    _detect_collection,
    _fit_sample_to_budget,
    _query_sample,
    _sample_from_tail,
    _serialize,
)
from meho_backplane.operations.reducer import PassThroughReducer, Reducer
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Unit tests — reducer in isolation (no dispatcher)
# ---------------------------------------------------------------------------


async def test_pass_through_for_small_payload() -> None:
    """A ≤threshold set returns unchanged with ``handle is None``.

    The default 50-row threshold is exclusive (``> 50`` materializes), so
    a 10-row collection passes straight through: identity-preserved
    payload, no handle. This is the v0.2 default the agent relies on for
    small lists.
    """
    reducer = JsonFluxReducer()
    # Structural-typing contract: the adapter satisfies the Protocol.
    assert isinstance(reducer, Reducer)

    payload = {"value": [{"vm": f"vm-{i}", "power": "on"} for i in range(10)]}

    reduced, handle = await reducer.reduce(payload, None)

    assert handle is None, f"≤threshold payload must not produce a handle; got {handle!r}"
    assert reduced is payload, "pass-through must return the exact input payload object"


async def test_materialize_handle_for_large_set() -> None:
    """A >threshold set returns a reduced summary + a real ResultHandle.

    Asserts the **real** materialization shape, not just field presence:

    * ``total_rows`` equals the full collection size (the count a future
      ``result_describe(handle)`` reports).
    * ``schema_`` is a JSON-Schema mapping inferred from the DuckDB
      table — ``type: array`` over ``items.properties`` with one entry
      per column, typed (``id`` → string, ``count`` → integer).
    * ``sample_rows`` is a bounded non-empty slice of real rows.
    * ``summary_md`` mentions the row count and is non-empty.
    * the inlined summary carries ``row_count`` and the sample *count*
      (``sample_rows_returned``) — never the full raw list, and never a
      duplicate copy of the sample rows (#134).
    """
    reducer = JsonFluxReducer(sample_size=5)
    rows = [{"id": f"seg-{i}", "name": f"canary-{i}", "count": i} for i in range(60)]
    payload = {"results": rows, "result_count": 60}

    reduced, handle = await reducer.reduce(payload, None)

    assert handle is not None, "a 60-row set is over the 50-row threshold; expected a handle"
    assert isinstance(handle, ResultHandle)

    # handle_id is a fresh UUID.
    assert isinstance(handle.handle_id, uuid.UUID)

    # total_rows reflects the materialized table, not the envelope.
    assert handle.total_rows == 60

    # summary_md is non-empty and names the row count.
    assert handle.summary_md
    assert "60" in handle.summary_md

    # schema_ is a frozen JSON-Schema mapping with typed columns.
    assert isinstance(handle.schema_, Mapping) and handle.schema_
    assert handle.schema_["type"] == "array"
    properties = handle.schema_["items"]["properties"]
    assert set(properties) == {"id", "name", "count"}
    assert properties["id"]["type"] == "string"
    assert properties["count"]["type"] == "integer"

    # sample_rows is a bounded non-empty slice of real rows.
    assert handle.sample_rows is not None
    assert 0 < len(handle.sample_rows) <= 5 < handle.total_rows
    first = handle.sample_rows[0]
    assert set(first) == {"id", "name", "count"}

    # ttl_seconds carries the configured default.
    assert handle.ttl_seconds == 3600

    # The inlined summary is the reduced view, not the raw 60-row list. The
    # inline sample is serialized once — it lives on ``handle.sample_rows``,
    # not duplicated into the summary (#134); the summary reports its count.
    assert isinstance(reduced, dict)
    assert reduced["row_count"] == 60
    assert "sample" not in reduced
    assert reduced["sample_rows_returned"] == len(handle.sample_rows)
    assert 0 < reduced["sample_rows_returned"] <= 5
    assert "results" not in reduced


async def test_materialize_handle_for_under_row_over_byte_threshold() -> None:
    """A set ≤ row_threshold but > byte_threshold materializes via the byte branch.

    Pins the *size*-triggered materialization path independently of the
    row-count path: ``_over_threshold`` returns True when
    ``len(_serialize(payload)) > byte_threshold`` even though
    ``len(rows) <= row_threshold``. This is the branch the production
    default exercises against vcsim's 50-VM seed (50 rows == the 50-row
    threshold, so ``50 > 50`` is False, but the serialized payload is
    ≈5 KB > the 4 KB ``byte_threshold``) — it had no dedicated unit
    coverage and broke the agent-flow e2e in CI (#962 B1) before this
    test was added.

    The fixture builds a 5-row set whose values are padded so the
    serialized JSON clears the default 4 KB ``byte_threshold`` while
    staying well under the 50-row default ``row_threshold``.
    """
    reducer = JsonFluxReducer()  # production defaults: row=50, byte=4096

    # Five rows, each carrying a ~1.2 KB blob → ~6 KB serialized: comfortably
    # over the 4 KB byte_threshold, comfortably under the 50-row threshold.
    rows = [{"id": f"seg-{i}", "blob": "x" * 1200} for i in range(5)]
    payload = {"value": rows}

    # Guard the test's own premise: row count is under the threshold, byte
    # count is over it — so only the byte branch can trigger materialization.
    assert len(rows) <= reducer._row_threshold
    assert len(json.dumps(payload).encode()) > reducer._byte_threshold

    reduced, handle = await reducer.reduce(payload, None)

    assert handle is not None, (
        "a 5-row set serializing over the 4 KB byte_threshold must "
        "materialize a handle even though it is under the row threshold"
    )
    assert isinstance(handle, ResultHandle)
    assert handle.total_rows == 5, (
        f"total_rows must reflect the 5-row collection; got {handle.total_rows}"
    )
    assert handle.sample_rows is not None and handle.sample_rows, (
        "the byte-triggered handle must still carry a bounded sample"
    )

    # The inlined summary is the reduced view, not the raw 5-row list.
    assert isinstance(reduced, dict)
    assert reduced["row_count"] == 5
    assert "value" not in reduced


# ---------------------------------------------------------------------------
# Single-serialization + byte-bounded inline sample (#134)
# ---------------------------------------------------------------------------


async def test_inline_sample_lives_in_exactly_one_location() -> None:
    """The sample is carried once — on ``handle.sample_rows``, not the summary.

    #134 acceptance criterion 1: a reduced ``call_operation`` envelope must
    not carry the inline sample in *both* ``result.sample`` and
    ``handle.sample_rows`` as full copies. The reducer keeps the preview on
    ``handle.sample_rows`` (the audit hoist + every connector e2e reads it
    there) and the compact summary reports only its *count*.
    """
    reducer = JsonFluxReducer(sample_size=5)
    rows = [{"id": f"seg-{i}", "name": f"canary-{i}"} for i in range(60)]

    reduced, handle = await reducer.reduce({"results": rows}, None)

    assert handle is not None
    # The handle carries the sample.
    assert handle.sample_rows is not None and len(handle.sample_rows) > 0
    # The summary does NOT duplicate it — no ``sample`` key at all.
    assert "sample" not in reduced
    # It reports the count instead, and the count agrees with the handle.
    assert reduced["sample_rows_returned"] == len(handle.sample_rows)


async def test_inline_sample_stays_under_byte_budget_independent_of_row_size() -> None:
    """The reduced envelope size is bounded by bytes, independent of ``K`` (#134).

    #134 acceptance criterion 2: given ``N`` rows each ~``K`` bytes where a
    fixed 5-row sample would blow the budget, the serialized sample stays
    under a fixed ceiling regardless of ``K`` — the row count shrinks as
    ``K`` grows, never below one. Feed 8 KB and 50 KB rows and assert the
    same ceiling holds for both while the sample row count drops.
    """
    budget = 4096

    async def _sample_for_row_size(blob_bytes: int) -> list[dict[str, Any]]:
        reducer = JsonFluxReducer(sample_size=5, sample_byte_budget=budget)
        rows = [{"id": f"row-{i}", "blob": "x" * blob_bytes} for i in range(60)]
        _reduced, handle = await reducer.reduce({"results": rows}, None)
        assert handle is not None and handle.sample_rows is not None
        return [dict(row) for row in handle.sample_rows]

    sample_8k = await _sample_for_row_size(8 * 1024)
    sample_50k = await _sample_for_row_size(50 * 1024)

    # Each row alone exceeds the budget, so the sample shrinks to a single
    # row whose oversized ``blob`` is truncated to fit — but never empty.
    for sample in (sample_8k, sample_50k):
        assert len(sample) >= 1
        serialized = len(_serialize(sample))
        assert serialized <= budget, (
            f"serialized sample ({serialized} bytes) must stay under the "
            f"{budget}-byte budget regardless of per-row size"
        )
    # A larger per-row size cannot produce a larger serialized sample.
    assert len(_serialize(sample_50k)) <= budget
    assert len(_serialize(sample_8k)) <= budget


def test_fit_sample_to_budget_drops_rows_then_truncates() -> None:
    """``_fit_sample_to_budget`` shrinks by rows first, then truncates values.

    Three regimes, all bounded by the budget:

    * a sample already under budget is returned unchanged;
    * a multi-row sample over budget drops rows down toward one;
    * a single row over budget has its oversized string values truncated
      (marked with :data:`_TRUNCATION_MARKER`) rather than dropped to zero.
    """
    # Under budget — unchanged.
    small = [{"k": "v"}, {"k": "w"}]
    assert _fit_sample_to_budget(small, 4096) == small

    # Multi-row over budget — rows drop, result fits, never below one.
    fat_rows = [{"id": i, "blob": "x" * 2000} for i in range(5)]
    fitted = _fit_sample_to_budget(fat_rows, 4096)
    assert 1 <= len(fitted) < len(fat_rows)
    assert len(_serialize(fitted)) <= 4096

    # A single row larger than the whole budget — truncated, not emptied.
    huge = [{"id": "only", "blob": "x" * 20000}]
    clipped = _fit_sample_to_budget(huge, 4096)
    assert len(clipped) == 1
    assert clipped[0]["id"] == "only"
    assert clipped[0]["blob"].endswith(_TRUNCATION_MARKER)
    assert len(_serialize(clipped)) <= 4096

    # Empty input stays empty.
    assert _fit_sample_to_budget([], 4096) == []


# ---------------------------------------------------------------------------
# Sample ordering — head (default) vs tail (G0.19-T1 #1479)
# ---------------------------------------------------------------------------


def _register_ordered_table(engine: QueryEngine, n: int) -> None:
    """Register an ``n``-row table whose ``seq`` column is the row order.

    ``seq`` ascends with registration order so an assertion can name the
    expected head / tail rows without depending on DuckDB's (unguaranteed)
    scan order for a bare ``SELECT``.
    """
    engine.register(
        "result",
        [{"seq": i, "line": f"line-{i}"} for i in range(n)],
        unwrap="auto",
    )


def test_query_sample_default_returns_head() -> None:
    """``_query_sample`` without ``from_tail`` returns the first N rows.

    The order-agnostic default: a Vault key list or topology set has no
    "more recent" end, so the head sample is the right preview. Pins the
    pre-#1479 behaviour so the tail path is strictly additive.
    """
    engine = QueryEngine()
    try:
        _register_ordered_table(engine, 20)
        sample = _query_sample(engine, 5)
    finally:
        engine.close()

    assert [row["seq"] for row in sample] == [0, 1, 2, 3, 4]


def test_query_sample_from_tail_returns_most_recent_in_chronological_order() -> None:
    """``_query_sample(from_tail=True)`` returns the LAST N rows, oldest-first.

    The #1479 fix: a ``k8s.logs(tail=500)`` reduce must preview the
    most-recent lines (the bottom of the window), not the oldest five
    (health-probe noise). The slice is the tail of the collection,
    re-sorted ascending so it reads like the bottom of a ``kubectl logs``
    window rather than reversed.
    """
    engine = QueryEngine()
    try:
        _register_ordered_table(engine, 20)
        sample = _query_sample(engine, 5, from_tail=True)
    finally:
        engine.close()

    # The five most-recent rows (16..19 plus 15), in chronological order.
    assert [row["seq"] for row in sample] == [15, 16, 17, 18, 19]


def test_query_sample_zero_size_returns_empty_in_both_modes() -> None:
    """``sample_size <= 0`` short-circuits to ``[]`` regardless of ``from_tail``."""
    engine = QueryEngine()
    try:
        _register_ordered_table(engine, 10)
        assert _query_sample(engine, 0) == []
        assert _query_sample(engine, 0, from_tail=True) == []
    finally:
        engine.close()


def test_sample_from_tail_resolves_only_the_tail_ordering() -> None:
    """``_sample_from_tail`` is True only for ``{"sample": "tail"}``.

    Every other shape — no context, no hint, a non-dict value, a dict
    without ``sample``, or a different ``sample`` value — resolves to the
    head-first default. The hint is purely additive.
    """
    assert _sample_from_tail({"result_ordering": {"sample": "tail"}}) is True

    assert _sample_from_tail(None) is False
    assert _sample_from_tail({}) is False
    assert _sample_from_tail({"result_ordering": None}) is False
    assert _sample_from_tail({"result_ordering": {"sample": "head"}}) is False
    assert _sample_from_tail({"result_ordering": {}}) is False
    # Malformed (non-dict) value must not raise — falls back to head.
    assert _sample_from_tail({"op_id": "x", "result_ordering": "tail"}) is False


async def test_reduce_tail_op_samples_most_recent_lines() -> None:
    """A tail-ordered op's reduce surfaces the most-recent rows inline.

    The agent-facing acceptance path for a ``k8s.logs``-shaped response:
    the reducer materializes the >threshold ``lines`` collection and the
    inline ``sample`` carries the **most-recent** lines (the requested
    tail) rather than the oldest. This is the inline "obtain the requested
    tail" reachability the #1479 DoD requires for log-shaped ops.
    """
    reducer = JsonFluxReducer(sample_size=5)
    # Oldest-first, like k8s.logs ``lines``: line-0 is the oldest.
    lines = [f"line-{i:03d}" for i in range(495)]
    payload = {"lines": lines}
    context = {"op_id": "k8s.logs", "result_ordering": {"sample": "tail"}}

    reduced, handle = await reducer.reduce(payload, None, context)

    assert handle is not None
    assert handle.total_rows == 495
    # The inline sample lives once on ``handle.sample_rows`` (#134) and is
    # the five MOST-RECENT lines, chronological.
    assert handle.sample_rows is not None
    sample_values = [dict(row)["value"] for row in handle.sample_rows]
    assert sample_values == [
        "line-490",
        "line-491",
        "line-492",
        "line-493",
        "line-494",
    ]
    # The summary reports the preview count, not a duplicate copy.
    assert "sample" not in reduced
    assert reduced["sample_rows_returned"] == len(handle.sample_rows)


async def test_reduce_without_ordering_hint_keeps_oldest_first_sample() -> None:
    """No ``result_ordering`` hint → the sample stays head-first (oldest).

    Backwards-compat guard: an op that never declared the hint behaves
    exactly as it did before #1479 — the first N rows of the collection.
    """
    reducer = JsonFluxReducer(sample_size=5)
    lines = [f"line-{i:03d}" for i in range(495)]
    payload = {"lines": lines}

    _reduced, handle = await reducer.reduce(payload, None, {"op_id": "k8s.logs"})

    assert handle is not None
    assert handle.sample_rows is not None
    sample_values = [dict(row)["value"] for row in handle.sample_rows]
    assert sample_values == [
        "line-000",
        "line-001",
        "line-002",
        "line-003",
        "line-004",
    ]


async def test_reduce_leaves_string_shaped_payload_untouched() -> None:
    """A string-shaped op output (e.g. ``k8s.exec``) is not reduced.

    Regression guard (#1479 AC4): ``k8s.exec`` returns
    ``{stdout: str, stderr: str, exit_code, ...}`` — no list-shaped
    collection — so ``_detect_collection`` finds nothing and the reducer
    passes the payload through verbatim with ``handle is None``. The tail-
    ordering work must not start reducing string-shaped streams (whose
    per-stream byte cap is a separate concern enforced in the handler).
    """
    reducer = JsonFluxReducer(row_threshold=0)  # force-reduce any collection
    # A huge stdout string must still pass through: it is not a list.
    payload = {
        "stdout": "x" * 200_000,
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "truncated": False,
    }

    reduced, handle = await reducer.reduce(payload, None, {"op_id": "k8s.exec"})

    assert handle is None, "string-shaped exec output must not materialize a handle"
    assert reduced is payload, "pass-through must return the exact input payload object"


# ---------------------------------------------------------------------------
# Dispatcher integration — broken reducer → connector_error
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for the dispatch test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry around every test."""
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation`` skips ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace :func:`publish_event` with a recording stub.

    Mirrors :mod:`tests.test_operations_dispatcher`: the audit helper
    invokes ``publish_event`` via the imported reference inside
    :mod:`meho_backplane.operations._audit`, so patching that module's
    attribute captures every event the dispatch emits.
    """
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


class _NoOpVaultConnector(Connector):
    """Connector class used to satisfy resolver lookups in the dispatch test."""

    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


class _BrokenReducer:
    """Reducer that always raises — exercises the dispatcher's reduce guard."""

    async def reduce(
        self,
        payload: Any,
        schema: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[Any, ResultHandle | None]:
        del payload, schema, context
        raise RuntimeError("simulated reducer explosion")


class _FakeFingerprint:
    """Duck-typed fingerprint the resolver reads ``version`` off of."""

    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """Minimal target the resolver / dispatcher reads from."""

    def __init__(self, *, product: str = "vault") -> None:
        self.product = product
        self.fingerprint = _FakeFingerprint(version=None)
        self.preferred_impl_id: str | None = None
        self.id = uuid.uuid4()
        self.name = "test-target"
        self.host = "test.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Construct an :class:`Operator` directly — no JWT round-trip."""
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _module_handler(
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Typed handler returning a small set-shaped payload for the reducer."""
    del target
    return {"value": [{"echo": params}]}


@pytest.fixture
async def _registered_typed_op(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Register the connector + a typed op the broken-reducer test dispatches."""
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.list",
        handler=_module_handler,
        summary="List secrets.",
        description="List secrets.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )
    yield


async def test_reducer_exception_yields_connector_error_via_dispatcher(
    _registered_typed_op: None,
    captured_events: list[BroadcastEvent],
) -> None:
    """A reducer raise propagates as ``connector_error``; audit + broadcast commit.

    Pins the dispatcher's never-raises contract for the JSONFlux seam
    (:func:`~meho_backplane.operations.dispatcher._reduce_or_error`):

    * ``status == 'error'`` with ``error`` prefixed ``connector_error:``
      and ``extras['error_code'] == 'connector_error'`` — the reducer's
      ``RuntimeError`` was converted, not propagated.
    * exactly one ``audit_log`` row for the op carries
      ``result_status == 'error'`` — the audit write committed despite
      the reducer failure.
    * exactly one broadcast event fired with ``result_status == 'error'``
      — the failure is observable on the feed.
    """
    set_default_reducer(_BrokenReducer())
    try:
        result = await dispatch(
            operator=_make_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.list",
            target=_FakeTarget(),
            params={"path": "/secret"},
        )
    finally:
        set_default_reducer(PassThroughReducer())

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "RuntimeError"

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "vault.kv.list")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"

    assert len(captured_events) == 1


# ---------------------------------------------------------------------------
# G0.15-T8 (#1219) — fetch_more envelope + audit-row handle metadata
# ---------------------------------------------------------------------------


async def test_handle_carries_fetch_more_unavailable_branches_without_context() -> None:
    """Every reducing-response handle ships a ``fetch_more`` block.

    With no ``pagination_hint`` in the reducer context, **both** branches
    return ``available=False`` with a non-empty rationale -- the contract
    is self-documenting regardless of whether a hint exists. This pins
    the v0.7.x state where the drill-in route is deferred to v0.8/0.9
    and most ops don't yet register a ``pagination_hint``.
    """
    reducer = JsonFluxReducer()
    rows = [{"id": f"row-{i}", "label": f"item-{i}"} for i in range(60)]
    payload = {"results": rows}

    _reduced, handle = await reducer.reduce(payload, None)

    assert handle is not None
    assert isinstance(handle.fetch_more, FetchMore)
    assert isinstance(handle.fetch_more.drill_in, FetchMoreDrillIn)
    assert handle.fetch_more.drill_in.available is False
    assert handle.fetch_more.drill_in.rationale, (
        "the drill_in branch must carry a non-empty rationale explaining the workaround"
    )
    # The unavailable branch names the narrower-params workaround (G0.20-T7
    # #1507: no spill happened because there is no tenant in the bare
    # reduce context), carries the machine-readable reason (#1629), and
    # leaves the available-only fields empty.
    assert "narrower" in handle.fetch_more.drill_in.rationale.lower()
    assert handle.fetch_more.drill_in.reason == "no_tenant_context"
    assert handle.fetch_more.drill_in.mcp_tool is None
    assert handle.fetch_more.drill_in.example_call is None
    assert handle.fetch_more.drill_in.expires_at is None

    assert isinstance(handle.fetch_more.native_pagination, FetchMoreNativePagination)
    assert handle.fetch_more.native_pagination.available is False
    assert handle.fetch_more.native_pagination.params is None
    assert handle.fetch_more.native_pagination.example_next_call is None
    assert handle.fetch_more.native_pagination.rationale, (
        "native_pagination must carry a rationale when available=False"
    )


async def test_handle_fetch_more_native_pagination_populated_from_context_hint() -> None:
    """``context['pagination_hint']`` populates the ``native_pagination`` branch verbatim.

    The reducer accepts both the validated :class:`PaginationHint` and a
    plain dict shape (the dispatcher reads ``llm_instructions`` as
    primitive JSON). Both paths produce ``available=True`` with the
    hint's ``params`` + ``example_next_call`` copied through.
    """
    reducer = JsonFluxReducer()
    rows = [{"vm": f"vm-{i}", "power": "on"} for i in range(80)]
    payload = {"value": rows}
    hint_dict = {
        "params": {
            "continue_token": "Server-emitted cursor.",
            "label_selector": "k8s label selector.",
        },
        "example_next_call": {
            "tool": "call_operation",
            "args": {"op_id": "k8s.pod.list", "params": {"all_namespaces": True}},
        },
    }

    # Dict path (the dispatcher's natural shape).
    _reduced, handle = await reducer.reduce(
        payload, None, {"op_id": "k8s.pod.list", "pagination_hint": hint_dict}
    )

    assert handle is not None
    native = handle.fetch_more.native_pagination
    assert native.available is True
    assert native.params is not None and dict(native.params) == hint_dict["params"]
    assert (
        native.example_next_call is not None
        and dict(native.example_next_call) == hint_dict["example_next_call"]
    )

    # Validated-instance path (callers that wire PaginationHint themselves).
    hint = PaginationHint.model_validate(hint_dict)
    _r2, handle2 = await reducer.reduce(
        payload, None, {"op_id": "k8s.pod.list", "pagination_hint": hint}
    )
    assert handle2 is not None
    assert handle2.fetch_more.native_pagination.available is True
    assert dict(handle2.fetch_more.native_pagination.params or {}) == hint_dict["params"]


async def test_handle_fetch_more_malformed_pagination_hint_falls_back_to_unavailable() -> None:
    """A malformed ``pagination_hint`` dict does not raise; the reducer logs and falls back.

    A reduce-time exception would otherwise convert into a
    ``connector_error`` ``OperationResult`` via the dispatcher's
    ``_reduce_or_error`` guard -- failing a real read because an
    operator-facing metadata field had a typo is the wrong fail mode.
    The validation surfaces at op-registration time when a connector
    author writes the hint as a :class:`PaginationHint` literal there.
    """
    reducer = JsonFluxReducer()
    rows = [{"id": i} for i in range(60)]
    payload = {"results": rows}

    # ``params`` must be a dict; the connector author typoed.
    bad_hint = {"params": "not-a-dict", "example_next_call": {"tool": "x"}}
    _reduced, handle = await reducer.reduce(
        payload, None, {"op_id": "broken.op", "pagination_hint": bad_hint}
    )

    assert handle is not None
    native = handle.fetch_more.native_pagination
    assert native.available is False, (
        "a malformed hint must collapse to the unavailable branch -- "
        "raising would lose the user-visible read result"
    )
    assert native.rationale, "unavailable branch must still carry a rationale"


# ---------------------------------------------------------------------------
# G0.20-T7 (#1507) — spill to the read-back store + drill-in availability
# ---------------------------------------------------------------------------


class _FakeStore:
    """In-memory stand-in for :class:`ResultHandleStore` the reducer spills into.

    Records every ``spill`` call so a test can assert the full rows (not
    just the inline sample) were persisted, and serves them back through
    ``fetch_window`` so the round-trip is exercised without Valkey.
    """

    def __init__(self) -> None:
        self.spills: list[dict[str, Any]] = []
        self._rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._totals: dict[tuple[str, str], int] = {}

    async def spill(
        self,
        *,
        tenant_id: Any,
        operator_sub: str,
        handle_id: Any,
        op_id: str | None,
        rows: list[dict[str, Any]],
        total_rows: int,
        ttl_seconds: int,
        max_rows: int,
    ) -> bool:
        stored = rows[:max_rows]
        self.spills.append(
            {
                "tenant_id": str(tenant_id),
                "operator_sub": operator_sub,
                "handle_id": str(handle_id),
                "op_id": op_id,
                "stored_rows": len(stored),
                "total_rows": total_rows,
                "ttl_seconds": ttl_seconds,
            }
        )
        self._rows[(str(tenant_id), str(handle_id))] = stored
        self._totals[(str(tenant_id), str(handle_id))] = total_rows
        return bool(stored)

    async def fetch_window(
        self,
        *,
        tenant_id: Any,
        operator_sub: str,
        handle_id: Any,
        offset: int,
        limit: int,
    ) -> dict[str, Any] | None:
        """Serve a spilled window back, mirroring the real store's contract.

        Returns the ``[offset : offset+limit]`` slice plus ``total_rows`` /
        ``stored_rows`` / ``truncated`` so a test can page the spilled
        handle end-to-end and confirm recovery is unaffected by the inline
        sample's byte-budgeting (#134).
        """
        key = (str(tenant_id), str(handle_id))
        rows = self._rows.get(key)
        if rows is None:
            return None
        stored = len(rows)
        total = self._totals[key]
        window = rows[max(offset, 0) : max(offset, 0) + limit] if limit > 0 else []
        return {
            "rows": window,
            "total_rows": total,
            "stored_rows": stored,
            "truncated": stored < total,
        }


async def test_reduce_spills_full_rows_and_flips_drill_in_available() -> None:
    """A reducing dispatch spills the full set and marks drill-in available.

    The #1507 acceptance path in isolation: with a tenant + operator in
    context, the reducer persists the FULL 60-row set (not the 5-row
    sample) to the store and the handle's ``fetch_more.drill_in`` flips to
    ``available=True`` naming the ``result_query`` tool, an
    ``example_call`` carrying the handle id, and an ``expires_at``.
    """
    store = _FakeStore()
    reducer = JsonFluxReducer(sample_size=5, store=store, max_spill_rows=10000)
    rows = [{"id": f"seg-{i}", "v": i} for i in range(60)]
    payload = {"results": rows}
    context = {
        "op_id": "vault.kv.list.bulk",
        "operator_sub": "op-a",
        "tenant_id": "00000000-0000-0000-0000-00000000a0a0",
    }

    _reduced, handle = await reducer.reduce(payload, None, context)

    assert handle is not None
    # The FULL set was spilled, keyed by the handle id, not just the sample.
    assert len(store.spills) == 1
    spill = store.spills[0]
    assert spill["stored_rows"] == 60
    assert spill["total_rows"] == 60
    assert spill["handle_id"] == str(handle.handle_id)
    assert spill["operator_sub"] == "op-a"
    assert spill["ttl_seconds"] == handle.ttl_seconds

    drill_in = handle.fetch_more.drill_in
    assert drill_in.available is True
    assert drill_in.reason is None, "#1629: the reason field is no-spill-only"
    assert drill_in.mcp_tool == "result_query"
    assert drill_in.example_call is not None
    assert drill_in.example_call["tool"] == "result_query"
    assert drill_in.example_call["args"]["handle_id"] == str(handle.handle_id)
    assert drill_in.expires_at is not None
    assert "result_query" in drill_in.rationale


async def test_recovery_unchanged_when_inline_sample_is_byte_bounded() -> None:
    """Byte-budgeting the inline sample leaves the spilled full set intact (#134).

    #134 acceptance criterion 4: the recovery path is untouched. Even when
    the inline sample is shrunk / truncated to fit the byte budget, the
    **full** object-heavy rows are spilled verbatim; paging the handle to
    its last row via the store returns the row byte-for-byte, with
    ``total_rows`` correct and ``truncated=False`` (no cap applied).
    """
    store = _FakeStore()
    reducer = JsonFluxReducer(
        sample_size=5, sample_byte_budget=4096, store=store, max_spill_rows=10000
    )
    # Object-heavy rows: each ~8 KB, so the inline sample must shrink+truncate.
    rows = [{"id": f"row-{i}", "blob": f"{i}-" + "x" * 8000} for i in range(60)]
    context = {
        "op_id": "k8s.apps.list",
        "operator_sub": "op-a",
        "tenant_id": "00000000-0000-0000-0000-00000000a0a0",
    }

    _reduced, handle = await reducer.reduce({"results": rows}, None, context)

    assert handle is not None
    # The inline sample was byte-bounded (a single truncated row).
    assert handle.sample_rows is not None
    assert len(_serialize([dict(r) for r in handle.sample_rows])) <= 4096

    # The FULL, un-truncated rows were spilled — recovery is unaffected.
    window = await store.fetch_window(
        tenant_id=context["tenant_id"],
        operator_sub="op-a",
        handle_id=handle.handle_id,
        offset=59,
        limit=1,
    )
    assert window is not None
    assert window["total_rows"] == 60
    assert window["truncated"] is False
    # The last row round-trips byte-for-byte — the spill is full fidelity,
    # NOT the truncated inline preview.
    assert window["rows"] == [{"id": "row-59", "blob": "59-" + "x" * 8000}]


async def test_reduce_without_tenant_skips_spill_and_stays_unavailable() -> None:
    """No tenant in context → no spill, drill-in stays unavailable.

    A non-dispatch reduce (or an operator with no tenant) cannot key the
    spill, so the store is untouched and the handle keeps the
    narrower-params workaround branch — exactly the pre-#1507 behaviour,
    now with the explicit ``no_tenant_context`` reason (#1629).
    """
    store = _FakeStore()
    reducer = JsonFluxReducer(sample_size=5, store=store, max_spill_rows=10000)
    rows = [{"id": f"seg-{i}"} for i in range(60)]

    _reduced, handle = await reducer.reduce(
        {"results": rows}, None, {"op_id": "x", "operator_sub": "op-a"}
    )

    assert handle is not None
    assert store.spills == []
    assert handle.fetch_more.drill_in.available is False
    assert handle.fetch_more.drill_in.reason == "no_tenant_context"
    assert handle.fetch_more.drill_in.mcp_tool is None


async def test_reduce_with_malformed_tenant_id_reports_no_tenant_context() -> None:
    """A tenant_id that does not parse as a UUID is unusable context (#1629).

    The spill cannot be keyed, so the store stays untouched and the
    drill-in branch carries the same ``no_tenant_context`` reason as the
    absent-tenant case — the operator-facing taxonomy stays two-valued.
    """
    store = _FakeStore()
    reducer = JsonFluxReducer(sample_size=5, store=store, max_spill_rows=10000)
    rows = [{"id": f"seg-{i}"} for i in range(60)]
    context = {"op_id": "x", "operator_sub": "op-a", "tenant_id": "not-a-uuid"}

    _reduced, handle = await reducer.reduce({"results": rows}, None, context)

    assert handle is not None
    assert store.spills == []
    drill_in = handle.fetch_more.drill_in
    assert drill_in.available is False
    assert drill_in.reason == "no_tenant_context"


async def test_reduce_store_rejection_reports_result_store_unavailable() -> None:
    """A store that cannot persist the rows yields the store reason (#1629).

    The Valkey-backed store is fail-open: ``spill`` returns ``False`` on
    an unreachable backend / rejected write instead of raising. The
    reduce must still ship the inline sample, and the drill-in branch
    must say *why* paging is unavailable — ``result_store_unavailable``,
    not the ambiguous catch-all the RDC cycle-8 ``k8s.logs tail=300``
    operators hit.
    """

    class _DownStore:
        async def spill(self, **_kwargs: Any) -> bool:
            return False

    reducer = JsonFluxReducer(sample_size=5, store=_DownStore(), max_spill_rows=10000)
    rows = [{"id": f"seg-{i}"} for i in range(60)]
    context = {
        "op_id": "k8s.logs",
        "operator_sub": "op-a",
        "tenant_id": "00000000-0000-0000-0000-00000000a0a0",
    }

    reduced, handle = await reducer.reduce({"results": rows}, None, context)

    assert handle is not None
    assert reduced["row_count"] == 60
    assert handle.sample_rows is not None and len(handle.sample_rows) == 5, (
        "the inline sample must still ship"
    )
    drill_in = handle.fetch_more.drill_in
    assert drill_in.available is False
    assert drill_in.reason == "result_store_unavailable"
    assert "store" in drill_in.rationale.lower()
    assert "narrower" in drill_in.rationale.lower()
    assert drill_in.mcp_tool is None
    assert drill_in.example_call is None


async def test_reduce_spill_capped_reports_truncated_tail() -> None:
    """A spill capped below total reports the truncation in the rationale."""
    store = _FakeStore()
    reducer = JsonFluxReducer(sample_size=5, store=store, max_spill_rows=40)
    rows = [{"id": i} for i in range(60)]
    context = {
        "op_id": "x",
        "operator_sub": "op-a",
        "tenant_id": "00000000-0000-0000-0000-00000000a0a0",
    }

    _reduced, handle = await reducer.reduce({"results": rows}, None, context)

    assert handle is not None
    assert store.spills[0]["stored_rows"] == 40
    drill_in = handle.fetch_more.drill_in
    assert drill_in.available is True
    # The rationale flags that only the first 40 of 60 rows are retrievable.
    assert "40" in drill_in.rationale
    assert "60" in drill_in.rationale


# ---------------------------------------------------------------------------
# #1629 — k8s.logs tail=300 diagnosis repro (RDC cycle-8, reported as a
# #1507 regression; attribution disproved here)
# ---------------------------------------------------------------------------


def _k8s_logs_payload(line_count: int) -> dict[str, Any]:
    """The exact response shape ``k8s_logs`` returns for ``tail=N``.

    A flat dict whose ``lines`` list sits NEXT TO scalar keys — none of
    the reducer's priority envelope keys (``value`` / ``results`` / ...)
    match, so collection detection must fall through to the
    largest-list branch and report ``source_key="lines"``, exactly what
    the RDC cycle-8 consumer saw.
    """
    return {
        "pod": "argocd-server-7d8f9c-x2x9z",
        "namespace": "argocd",
        "container": "argocd-server",
        "lines": [f"2026-05-31T10:00:{i % 60:02d} log line {i}" for i in range(line_count)],
        "truncated": False,
    }


async def test_k8s_logs_shape_with_tenant_context_spills_and_pages() -> None:
    """The consumer's ``tail=300`` repro against a healthy store: NO shape gap.

    Diagnosis evidence for #1629 acceptance criterion 1: the full
    ``k8s.logs`` response shape (scalar siblings + a 300-element
    ``lines`` list + the tail ordering hint) reduces, spills, and flips
    drill-in available under a tenant-scoped context. The #1507 spill
    infrastructure handles the k8s.logs shape correctly — the RDC
    cycle-8 ``handle: null`` cannot be a k8s.logs-shape gap, leaving a
    runtime skip (store or context) as the only candidates, both of
    which now self-identify via ``drill_in.reason``.
    """
    store = _FakeStore()
    reducer = JsonFluxReducer(sample_size=5, store=store, max_spill_rows=10000)
    context = {
        "op_id": "k8s.logs",
        "operator_sub": "rdc-operator",
        "tenant_id": "00000000-0000-0000-0000-00000000a0a0",
        "result_ordering": {"sample": "tail"},
    }

    reduced, handle = await reducer.reduce(_k8s_logs_payload(300), None, context)

    assert handle is not None, "a reducing k8s.logs response always mints a handle"
    assert reduced["row_count"] == 300
    assert reduced["total"] == 300
    assert reduced["source_key"] == "lines"
    assert handle.sample_rows is not None and len(handle.sample_rows) == 5
    # Tail ordering: the sample is the five most-recent lines.
    assert dict(handle.sample_rows[-1])["value"].endswith("log line 299")
    # The full 300 rows were spilled and the drill-in teaches the page-back.
    assert len(store.spills) == 1
    assert store.spills[0]["stored_rows"] == 300
    drill_in = handle.fetch_more.drill_in
    assert drill_in.available is True
    assert drill_in.reason is None
    assert drill_in.mcp_tool == "result_query"


async def test_k8s_logs_shape_store_down_states_the_reason() -> None:
    """The hardened #1629 envelope for the consumer's actual failure mode.

    Same ``tail=300`` payload, but the spill store cannot persist —
    the only no-spill branch reachable on a real authenticated dispatch
    (``Operator.tenant_id`` / ``sub`` are required fields, so the
    tenant-context branch cannot fire there). The 5-of-300 sample still
    ships, and the response now says *why* it cannot be paged instead
    of silently omitting the read-back route.
    """

    class _DownStore:
        async def spill(self, **_kwargs: Any) -> bool:
            return False

    reducer = JsonFluxReducer(sample_size=5, store=_DownStore(), max_spill_rows=10000)
    context = {
        "op_id": "k8s.logs",
        "operator_sub": "rdc-operator",
        "tenant_id": "00000000-0000-0000-0000-00000000a0a0",
        "result_ordering": {"sample": "tail"},
    }

    reduced, handle = await reducer.reduce(_k8s_logs_payload(300), None, context)

    assert handle is not None
    assert reduced["row_count"] == 300
    assert handle.sample_rows is not None and len(handle.sample_rows) == 5
    drill_in = handle.fetch_more.drill_in
    assert drill_in.available is False
    assert drill_in.reason == "result_store_unavailable"
    assert "store" in drill_in.rationale.lower()


# ---------------------------------------------------------------------------
# #2113 — single-object detail ops (dict-of-arrays) must not be reduced to
# one arbitrary sub-array. ``k8s.pod.info`` returns a flat detail object
# whose sibling arrays (containers / container_statuses / volumes /
# conditions) are coordinate fields, NOT pages of one collection. The
# pre-#2113 largest-list fallback materialized ``conditions`` (the longest,
# least useful array) and silently dropped ``container_statuses``.
# ---------------------------------------------------------------------------


def test_detect_collection_exempts_dict_of_arrays_detail_object() -> None:
    """A dict with >1 list-valued field is a detail object, not a collection.

    Unit-level anchor for the #2113 fix at the detection boundary: the
    ``k8s.pod.info`` shape (several sibling arrays next to scalar fields)
    must return ``(None, None)`` so the reducer passes it through verbatim
    instead of picking the longest array (``conditions``) and discarding
    ``container_statuses`` / ``containers`` / ``volumes``.
    """
    detail = {
        "name": "app-pod",
        "node": "node-1",
        "qos_class": "Burstable",
        "containers": [{"name": "app"}],
        "container_statuses": [{"name": "app", "ready": True}],
        "volumes": [{"name": "cfg"}],
        "conditions": [{"type": "Ready", "status": "True"}, {"type": "PodScheduled"}],
    }

    envelope_key, rows = _detect_collection(detail)

    assert (envelope_key, rows) == (None, None), (
        "a dict-of-arrays detail object must not be treated as a paginable "
        "collection — no sub-array may be selected as THE collection"
    )


def test_detect_collection_keeps_single_list_flat_dict_as_collection() -> None:
    """A flat dict with exactly ONE list field is still a real collection.

    The k8s.logs shape (``lines`` next to scalar siblings, no envelope
    key) must keep flowing through the largest-list fallback so genuine
    list ops still reduce — the #2113 fix narrows the fallback to the
    single-list case, it does not disable it.
    """
    payload = {"pod": "p", "namespace": "n", "lines": ["a", "b", "c"], "truncated": False}

    envelope_key, rows = _detect_collection(payload)

    assert envelope_key == "lines"
    assert rows == ["a", "b", "c"]


def test_detect_collection_reduces_paginated_collection_with_hateoas_metadata() -> None:
    """A ``{resourceList, pageInfo, links}`` payload still reduces (#2184).

    Regression guard for the vROps / VCF-operations resource-list shape:
    the single real collection (``resourceList``) is wrapped next to a
    ``pageInfo`` cursor block and a HATEOAS ``links`` array. Both are
    transport metadata, not coordinate fields of a detail object, so
    ``_detect_collection`` must exclude them from the list-field count and
    keep the payload classified as ONE real list field — otherwise
    (pre-#2184) the extra ``links`` array tipped it into the #2113
    multi-list detail exemption and a genuine large collection shipped
    UNREDUCED with ``handle=None``. Locks in that a future change can't
    silently re-break vROps pagination.
    """
    payload = {
        "resourceList": [{"identifier": f"r-{i}", "name": f"vm-{i}"} for i in range(10)],
        "pageInfo": {"totalCount": 10, "page": 0, "pageSize": 1000},
        "links": [{"href": "/suite-api/api/resources", "rel": "SELF", "name": "current"}],
    }

    envelope_key, rows = _detect_collection(payload)

    assert envelope_key == "resourceList", (
        "the paginated resource list must be detected as the collection, not "
        "exempted as a dict-of-arrays detail object because of the HATEOAS "
        "``links`` metadata array"
    )
    assert rows == payload["resourceList"]


def _large_application_pod(now: datetime) -> Any:
    """A real Deployment pod whose ``pod_info`` projection trips the reducer.

    Mirrors reproduction case **B** in #2113: a populated ``env`` + multi
    container-status + several conditions, so the serialized detail clears
    the byte threshold. Returned via the real ``pod_info`` projection so
    the test asserts against the documented contract, not a hand-rolled
    dict.
    """
    from kubernetes_asyncio.client.models import (
        V1Container,
        V1ContainerState,
        V1ContainerStateRunning,
        V1ContainerStatus,
        V1EnvVar,
        V1ObjectMeta,
        V1Pod,
        V1PodCondition,
        V1PodSpec,
        V1PodStatus,
        V1Volume,
    )

    big_env = [V1EnvVar(name=f"CONFIG_KEY_{i}", value=f"value-{i}-" + "x" * 40) for i in range(60)]
    return V1Pod(
        metadata=V1ObjectMeta(
            name="payments-api-7d8f9c-x2x9z",
            namespace="payments",
            creation_timestamp=now - timedelta(seconds=3600),
            labels={"app": "payments-api"},
        ),
        spec=V1PodSpec(
            containers=[
                V1Container(name="payments-api", image="registry/payments-api:2.3.1", env=big_env),
            ],
            volumes=[V1Volume(name="cfg"), V1Volume(name="tls")],
            node_name="rke2-meho-03",
        ),
        status=V1PodStatus(
            phase="Running",
            pod_ip="10.0.4.7",
            qos_class="Burstable",
            container_statuses=[
                V1ContainerStatus(
                    name="payments-api",
                    image="registry/payments-api:2.3.1",
                    image_id="sha256:abc",
                    ready=True,
                    restart_count=2,
                    state=V1ContainerState(running=V1ContainerStateRunning()),
                )
            ],
            conditions=[
                V1PodCondition(type="PodReadyToStartContainers", status="True"),
                V1PodCondition(type="Initialized", status="True"),
                V1PodCondition(type="Ready", status="True"),
                V1PodCondition(type="ContainersReady", status="True"),
                V1PodCondition(type="PodScheduled", status="True"),
            ],
        ),
    )


async def test_pod_info_above_threshold_keeps_container_statuses_inline() -> None:
    """#2113 acceptance 1+2: a big pod's ``container_statuses`` survives reduce.

    A populated application pod (case B) projected via ``pod_info`` clears
    the 4 KB byte threshold, so the pre-fix reducer collapsed it to
    ``source_key="conditions"`` and dropped everything else. The fix
    exempts the dict-of-arrays detail object from list reduction, so the
    full flat object ships inline with no handle and every sibling array
    is retrievable.
    """
    from meho_backplane.connectors.kubernetes.ops_workload import pod_info

    now = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)
    projection = pod_info(_large_application_pod(now), now=now)
    # Guard the premise: this projection really is over the byte threshold,
    # so we are exercising the reducing path, not the trivial small-pod one.
    reducer = JsonFluxReducer()
    assert len(_serialize(projection)) > reducer._byte_threshold, (
        "test premise: the application-pod projection must exceed the byte "
        "threshold, otherwise it never reaches the reduction boundary"
    )

    reduced, handle = await reducer.reduce(projection, None)

    # The detail object passes through verbatim — no handle, no collapse.
    assert handle is None, "a single-object detail op must not mint a list handle"
    assert reduced is projection
    assert "source_key" not in reduced, (
        "the reduced response must NOT collapse to a single sub-array's "
        "source_key (was 'conditions' pre-#2113)"
    )
    # Every sibling array pod_info() projects is intact.
    for key in ("containers", "container_statuses", "volumes", "node", "qos_class"):
        assert key in reduced, f"sibling field {key!r} was silently discarded"
    # container_statuses carries the per-container name/image/ready/restart/state.
    cs = reduced["container_statuses"][0]
    assert cs["name"] == "payments-api"
    assert cs["image"] == "registry/payments-api:2.3.1"
    assert cs["ready"] is True
    assert cs["restart_count"] == 2
    assert cs["state"] == "running"


async def test_pod_info_below_threshold_still_returns_full_object_inline() -> None:
    """#2113 acceptance 4: the small-pod path is unchanged (full flat object).

    A trivial pod (empty env, one container, no volumes) is well under the
    threshold, so it always passed through inline. The fix must not alter
    that: full object, no handle.
    """
    from kubernetes_asyncio.client.models import (
        V1Container,
        V1ContainerStatus,
        V1ObjectMeta,
        V1Pod,
        V1PodCondition,
        V1PodSpec,
        V1PodStatus,
    )

    from meho_backplane.connectors.kubernetes.ops_workload import pod_info

    now = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)
    pod = V1Pod(
        metadata=V1ObjectMeta(
            name="mongo-0", namespace="db", creation_timestamp=now - timedelta(seconds=60)
        ),
        spec=V1PodSpec(
            containers=[V1Container(name="mongo", image="mongo:8.0")], node_name="node-1"
        ),
        status=V1PodStatus(
            phase="Running",
            qos_class="BestEffort",
            container_statuses=[
                V1ContainerStatus(
                    name="mongo",
                    image="docker.io/library/mongo:8.0",
                    image_id="sha256:def",
                    ready=True,
                    restart_count=0,
                )
            ],
            conditions=[V1PodCondition(type="Ready", status="True")],
        ),
    )
    projection = pod_info(pod, now=now)
    reducer = JsonFluxReducer()
    assert len(_serialize(projection)) <= reducer._byte_threshold

    reduced, handle = await reducer.reduce(projection, None)

    assert handle is None
    assert reduced is projection
    assert reduced["container_statuses"][0]["image"] == "docker.io/library/mongo:8.0"
    assert reduced["qos_class"] == "BestEffort"


def test_pod_info_docstring_documents_container_statuses_contract() -> None:
    """#2113 acceptance 5: the projection contract the reduced path preserves.

    The reduced-path response now exposes exactly what ``pod_info``'s
    docstring promises (per-container statuses with readiness + restart
    count + state). Pin the docstring so the contract this fix preserves
    cannot silently drift away from the code that produces it.
    """
    from kubernetes_asyncio.client.models import (
        V1ContainerState,
        V1ContainerStateRunning,
        V1ContainerStatus,
    )

    from meho_backplane.connectors.kubernetes.ops_workload import (
        container_status_row,
        pod_info,
    )

    doc = pod_info.__doc__ or ""
    assert "Container statuses" in doc
    assert "restartCount" in doc
    # The row projection the contract names actually emits exactly the
    # per-container fields #2113 asserts survive the reduced path.
    row = container_status_row(
        V1ContainerStatus(
            name="c",
            image="img:1",
            image_id="sha256:x",
            ready=True,
            restart_count=3,
            state=V1ContainerState(running=V1ContainerStateRunning()),
        )
    )
    assert {"name", "image", "ready", "restart_count", "state"} <= row.keys()
    assert row["restart_count"] == 3
    assert row["state"] == "running"


async def _set_shaped_handler(
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Module-level handler returning a 60-row set so JsonFluxReducer materializes."""
    del target, params
    return {"results": [{"k": f"k-{i}", "v": i} for i in range(60)]}


@pytest.fixture
async def _registered_set_shaped_op(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Register the connector + a typed op the audit-hoist test dispatches.

    Returns a 60-row payload so the production-default
    :class:`JsonFluxReducer` actually materializes a handle (the
    fixture used by other dispatcher tests returns a 1-row payload
    that passes through). The op_id is intentionally distinct so it
    doesn't share state with :func:`_registered_typed_op`.
    """
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.list.bulk",
        handler=_set_shaped_handler,
        summary="List many secrets.",
        description="List many secrets.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )
    yield


async def test_reducing_dispatch_writes_handle_metadata_into_audit_payload(
    _registered_set_shaped_op: None,
    captured_events: list[BroadcastEvent],
) -> None:
    """A reducing dispatch hoists ``handle_id`` / ``total_rows`` / ``sample_rows_returned``.

    G0.15-T8 (#1219). After the reducer materializes, the dispatcher
    derives the audit-payload hoist dict via
    ``_handle_metadata_for_audit(handle)`` and threads it through
    ``audit_and_broadcast_safe(..., handle_metadata=...)`` →
    ``write_audit_row(..., handle_metadata=...)`` →
    ``_build_audit_payload(..., handle_metadata=...)``, where
    ``payload.update(handle_metadata)`` merges the three keys onto the
    ``audit_log.payload`` JSON. A consumer reading the audit row
    attributes *"what the agent saw"* (the handle id + total rows +
    the bounded sample size) without joining against the reducer's
    in-memory state.
    """
    set_default_reducer(JsonFluxReducer(sample_size=5))
    try:
        result = await dispatch(
            operator=_make_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.list.bulk",
            target=_FakeTarget(),
            params={"path": "/secret"},
        )
    finally:
        set_default_reducer(PassThroughReducer())

    assert result.status == "ok", (
        f"expected ok; got status={result.status!r} error={result.error!r}"
    )
    assert result.handle is not None, "a 60-row response must materialize through JsonFluxReducer"

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "vault.kv.list.bulk")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["result_status"] == "ok"
    assert payload["handle_id"] == str(result.handle.handle_id)
    assert payload["total_rows"] == 60
    assert payload["sample_rows_returned"] == 5

    # The broadcast event also fired.
    assert len(captured_events) == 1
    assert captured_events[0].result_status == "ok"


async def _tail_log_handler(
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Module-level handler returning a 200-line oldest-first ``lines`` set.

    Mirrors ``k8s.logs``' output: ``lines`` is chronological (line-000 is
    the oldest, line-199 the most recent). 200 lines clears the 50-row
    materialization threshold so the dispatch path reduces it.
    """
    del target, params
    return {"lines": [f"line-{i:03d}" for i in range(200)]}


@pytest.fixture
async def _registered_tail_ordered_op(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Register a typed op carrying ``llm_instructions.result_ordering = tail``.

    Exercises the dispatcher → reducer ordering wiring end to end: the
    descriptor's ``llm_instructions`` slot is what
    ``_result_ordering_from_descriptor`` lifts into the reducer context.
    """
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="k8s.logs.like",
        handler=_tail_log_handler,
        summary="Fetch many log lines.",
        description="Fetch many log lines.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        llm_instructions={"result_ordering": {"sample": "tail"}},
        embedding_service=stub_embedding_service,
    )
    yield


async def test_reducing_dispatch_honours_result_ordering_tail_hint(
    _registered_tail_ordered_op: None,
    captured_events: list[BroadcastEvent],
) -> None:
    """A tail-ordered op dispatched end-to-end samples the most-recent lines.

    G0.19-T1 (#1479). The descriptor's
    ``llm_instructions["result_ordering"] = {"sample": "tail"}`` is lifted
    by ``dispatcher._result_ordering_from_descriptor`` into
    ``reducer_context["result_ordering"]``; ``JsonFluxReducer`` then samples
    the tail. This proves the whole wire — not just the reducer in
    isolation — surfaces the requested tail to the agent.
    """
    set_default_reducer(JsonFluxReducer(sample_size=5))
    try:
        result = await dispatch(
            operator=_make_operator(),
            connector_id="vault-1.x",
            op_id="k8s.logs.like",
            target=_FakeTarget(),
            params={},
        )
    finally:
        set_default_reducer(PassThroughReducer())

    assert result.status == "ok", (
        f"expected ok; got status={result.status!r} error={result.error!r}"
    )
    assert result.handle is not None, "a 200-line response must materialize a handle"
    assert result.handle.total_rows == 200
    assert result.handle.sample_rows is not None
    sample_values = [dict(row)["value"] for row in result.handle.sample_rows]
    assert sample_values == [
        "line-195",
        "line-196",
        "line-197",
        "line-198",
        "line-199",
    ], "the dispatched tail-ordered op must surface the most-recent lines inline"
