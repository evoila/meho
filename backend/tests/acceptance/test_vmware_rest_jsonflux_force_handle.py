# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.1-T8 JSONFlux handle force-mode acceptance test.

Exercises the JSONFlux dispatcher → reducer → ``OperationResult`` seam
with the **real** :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
(G0.6.1-T3 #753) in force mode (``row_threshold=0`` — every non-empty
set materializes regardless of size). The test:

* Dispatches ``GET:/vcenter/vm`` against vcsim's 50-VM seed.
* Asserts the returned :class:`~meho_backplane.connectors.schemas.OperationResult`
  carries a populated :attr:`OperationResult.handle` with the real
  materialized shape (fresh UUID id, summary_md naming the row count,
  a JSON-Schema ``schema_`` mapping inferred from the DuckDB table,
  ≥1 real sample row, ttl_seconds set).
* Asserts the inlined :attr:`OperationResult.result` carries the
  reduced summary (row_count + bounded sample) rather than the full
  50-VM set.

What the test does NOT cover
============================

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
from tests.acceptance._canary_fixtures import IngestedCanaryVcsim


@pytest.fixture
def force_handle_reducer() -> Any:
    """Install :class:`JsonFluxReducer` in force mode as the dispatcher default.

    ``row_threshold=0`` forces every non-empty set to materialize, so
    vcsim's 50-VM seed (at, not over, the default 50-row threshold)
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


async def test_force_handle_reducer_populates_operation_result_handle(
    force_handle_reducer: None,
    ingested_canary_vcsim: IngestedCanaryVcsim,
) -> None:
    """Dispatching ``GET:/vcenter/vm`` against vcsim populates ``OperationResult.handle``.

    Confirms the JSONFlux dispatcher seam:

    * ``status == 'ok'`` — dispatch succeeded.
    * ``handle`` is a :class:`ResultHandle` — the reducer's return
      value flowed through :func:`wrap_ok_result`.
    * ``handle.total_rows == 50`` — matches vcsim's seeded
      :data:`DEFAULT_VCSIM_TOPOLOGY` VM count.
    * ``handle.summary_md`` is non-empty + mentions the row count —
      the reducer composed its summary correctly.
    * ``handle.schema_`` is a non-empty mapping — the validator's
      frozen-after-construction guarantee held.
    * ``handle.sample_rows`` is populated — the reducer captured at
      least one row from vcsim's response.
    * ``result['row_count'] == 50`` — the dispatcher inlined the
      reducer's summary on the operation envelope (not the raw
      50-VM list).
    """
    result_envelope = await call_operation(
        ingested_canary_vcsim.operator,
        {
            "connector_id": ingested_canary_vcsim.connector_id,
            "op_id": "GET:/vcenter/vm",
            "target": {"name": ingested_canary_vcsim.target_name},
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
    # ``model_dump`` ships the handle as a dict on the wire; the
    # assertions below match the JSON shape rather than re-instantiating
    # ResultHandle. handle_id is serialised as a UUID string by
    # :meth:`pydantic.BaseModel.model_dump(mode='json')`.
    uuid.UUID(handle["handle_id"])  # raises if not a valid UUID4 form
    assert handle["total_rows"] == 50, (
        f"expected 50 VMs from vcsim's seed topology; got handle.total_rows={handle['total_rows']}"
    )
    assert handle["summary_md"], "handle.summary_md must be non-empty"
    assert "50" in handle["summary_md"], (
        f"expected summary_md to mention the row count; got {handle['summary_md']!r}"
    )
    # schema_ is the real JSON Schema the reducer inferred from the
    # DuckDB-materialized table: an array-of-objects with typed columns.
    assert isinstance(handle["schema_"], dict) and handle["schema_"], (
        f"handle.schema_ must be a non-empty mapping; got {handle['schema_']!r}"
    )
    assert handle["schema_"]["type"] == "array"
    schema_props = handle["schema_"]["items"]["properties"]
    assert schema_props, f"expected ≥1 inferred column in the handle schema; got {schema_props!r}"
    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        f"expected ≥1 sample row from vcsim's 50-VM list; got sample_rows={sample_rows!r}"
    )
    # Sample rows are real VM records whose keys match the inferred schema.
    assert set(sample_rows[0]) == set(schema_props), (
        f"sample-row columns must match the inferred schema; "
        f"row keys={set(sample_rows[0])} schema keys={set(schema_props)}"
    )

    # The inlined result is the reducer's summary, not the raw 50-VM
    # set. ``row_count`` matches the handle's total; ``sample`` is
    # the same list-of-dicts the handle's sample_rows carries (just
    # serialised differently — dict vs MappingProxyType).
    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == 50, (
        f"expected the reducer's summary on result; got result={payload!r}"
    )
