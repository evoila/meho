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
    FingerprintResult,
    ProbeResult,
    ResultHandle,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
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
    * the inlined summary carries ``row_count`` and the bounded
      ``sample`` — never the full raw list.
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

    # The inlined summary is the reduced view, not the raw 60-row list.
    assert isinstance(reduced, dict)
    assert reduced["row_count"] == 60
    assert len(reduced["sample"]) <= 5
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

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
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
    assert captured_events[0].result_status == "error"
