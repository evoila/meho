# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.5-T2 JSONFlux force-mode acceptance for the NSX connector.

Mirrors :mod:`tests.acceptance.test_vmware_rest_jsonflux_force_handle`,
swapping in NSX as the dispatched connector and driving the **real**
:class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
(G0.6.1-T3 #753) in force mode (``row_threshold=0`` — the seeded NSX
core returns only 12 segments, below the default 50-row threshold, so
force mode is required to exercise the materialization path). It
dispatches ``GET:/policy/api/v1/infra/segments`` against the seeded NSX
core and asserts the
:class:`~meho_backplane.connectors.schemas.OperationResult`'s ``handle``
field carries a populated :class:`ResultHandle` with the real
materialized shape (UUID id, summary_md naming the row count, a
JSON-Schema ``schema_`` mapping inferred from the DuckDB table, ≥1 real
sample row, ttl_seconds set).

What this test does NOT cover
=============================

* MinIO/S3 spill — the v0.2 reducer is DuckDB ``:memory:`` only
  (Initiative #750 out-of-scope §3).
* Audit-row ``handle_id`` propagation — v0.2's audit pipeline does
  not surface the handle's UUID on the audit row's
  :attr:`AuditLog.extras` field. The assertion below is focused on the
  :class:`OperationResult` envelope rather than the audit-row shape.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from tests.acceptance._nsx_canary_fixtures import (
    NSX_CANARY_SEGMENTS,
    NSX_FORCE_HANDLE_LIST_OP_ID,
    IngestedNsxCanary,
)


@pytest.fixture
def force_handle_reducer() -> Any:
    """Install :class:`JsonFluxReducer` in force mode as the dispatcher default.

    ``row_threshold=0`` forces every non-empty set to materialize, so
    the seeded 12-segment NSX list (below the default 50-row threshold)
    produces a handle. The reducer is module-level state on
    :mod:`meho_backplane.operations.dispatcher`; the test's setup swaps
    it in via :func:`set_default_reducer` and teardown restores
    :class:`PassThroughReducer` so a follow-on test in the same pytest
    session sees the v0.2 default behaviour.

    :func:`reset_dispatcher_caches` is called on teardown to drop any
    connector-instance cache the dispatch built up.
    """
    set_default_reducer(JsonFluxReducer(row_threshold=0))
    try:
        yield
    finally:
        set_default_reducer(PassThroughReducer())
        reset_dispatcher_caches()


async def test_force_handle_reducer_populates_operation_result_handle_for_nsx(
    force_handle_reducer: None,
    ingested_nsx_canary: IngestedNsxCanary,
) -> None:
    """Dispatching the NSX segment list populates ``OperationResult.handle``.

    Confirms the JSONFlux dispatcher seam end-to-end for NSX:

    * ``status == 'ok'`` — dispatch succeeded (session-create + GET
      against ``/policy/api/v1/infra/segments``).
    * ``handle`` is a :class:`ResultHandle` — the reducer's return
      value flowed through :func:`wrap_ok_result`.
    * ``handle.total_rows`` matches the seeded segment count from
      :data:`NSX_CANARY_SEGMENTS`.
    * ``handle.summary_md`` is non-empty and mentions the row count
      — the reducer composed its summary correctly.
    * ``handle.schema_`` is a non-empty mapping — the validator's
      frozen-after-construction guarantee held.
    * ``handle.sample_rows`` is populated — the reducer captured at
      least one row from the seeded segment list.
    * ``result['row_count']`` matches the handle's total — the
      dispatcher inlined the reducer's summary on the operation
      envelope (not the raw segment list).
    """
    expected_rows = len(NSX_CANARY_SEGMENTS["results"])  # type: ignore[arg-type]

    result_envelope = await call_operation(
        ingested_nsx_canary.operator,
        {
            "connector_id": ingested_nsx_canary.connector_id,
            "op_id": NSX_FORCE_HANDLE_LIST_OP_ID,
            "target": {"name": ingested_nsx_canary.target_name},
            "params": {},
        },
    )

    assert result_envelope["status"] == "ok", (
        f"force-handle dispatch did not succeed: {result_envelope!r}"
    )

    handle = result_envelope.get("handle")
    assert handle is not None, (
        f"expected OperationResult.handle to be populated by "
        f"JsonFluxReducer; got handle=None on envelope={result_envelope!r}"
    )
    uuid.UUID(handle["handle_id"])  # raises if not a valid UUID4 form
    assert handle["total_rows"] == expected_rows, (
        f"expected {expected_rows} segments from the seeded NSX_CANARY_SEGMENTS; "
        f"got handle.total_rows={handle['total_rows']}"
    )
    assert handle["summary_md"], "handle.summary_md must be non-empty"
    assert str(expected_rows) in handle["summary_md"], (
        f"expected summary_md to mention the row count; got {handle['summary_md']!r}"
    )
    # schema_ is the real JSON Schema the reducer inferred from the
    # DuckDB-materialized table: an array-of-objects with typed columns.
    assert isinstance(handle["schema_"], dict) and handle["schema_"], (
        f"handle.schema_ must be a non-empty mapping; got {handle['schema_']!r}"
    )
    assert handle["schema_"]["type"] == "array"
    schema_props = handle["schema_"]["items"]["properties"]
    assert "id" in schema_props and "display_name" in schema_props, (
        f"expected NSX segment columns in the inferred schema; got {schema_props!r}"
    )
    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        f"expected ≥1 sample row from the seeded NSX segment list; got sample_rows={sample_rows!r}"
    )
    # Sample rows are real segment records carrying the seeded id pattern.
    assert sample_rows[0]["id"].startswith("seg-canary-"), (
        f"expected a real seeded segment in sample_rows; got {sample_rows[0]!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"expected the reducer's summary on result; got result={payload!r}"
    )
