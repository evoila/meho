# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.5-T5 JSONFlux force-mode acceptance for the SDDC Manager connector.

Mirrors :mod:`tests.acceptance.test_g35_nsx_jsonflux_force_handle`,
swapping in SDDC Manager as the dispatched connector and driving the
**real** :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
(G0.6.1-T3 #753) in force mode (``row_threshold=0`` — the seeded SDDC
core returns only 12 hosts, below the default 50-row threshold). It
dispatches ``GET:/v1/hosts`` against the seeded SDDC Manager core and
asserts the :class:`~meho_backplane.connectors.schemas.OperationResult`'s
``handle`` field carries a populated :class:`ResultHandle` with the real
materialized shape.

The SDDC Manager API returns paginated results under an ``elements[]``
key (vs NSX's ``results[]``); the reducer's envelope detection
recognises ``elements`` so the list materializes to one row per host.
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
from tests.acceptance._sddc_canary_fixtures import (
    SDDC_CANARY_HOSTS,
    SDDC_FORCE_HANDLE_LIST_OP_ID,
    IngestedSddcCanary,
)


@pytest.fixture
def force_handle_reducer() -> Any:
    """Install :class:`JsonFluxReducer` in force mode as the dispatcher default.

    ``row_threshold=0`` forces every non-empty set to materialize, so
    the seeded 12-host SDDC list (below the default 50-row threshold)
    produces a handle. Teardown restores :class:`PassThroughReducer` and
    drops the dispatcher caches.
    """
    set_default_reducer(JsonFluxReducer(row_threshold=0))
    try:
        yield
    finally:
        set_default_reducer(PassThroughReducer())
        reset_dispatcher_caches()


async def test_force_handle_reducer_populates_operation_result_handle_for_sddc(
    force_handle_reducer: None,
    ingested_sddc_canary: IngestedSddcCanary,
) -> None:
    """Dispatching the SDDC Manager host list populates ``OperationResult.handle``.

    Confirms the JSONFlux dispatcher seam end-to-end for SDDC Manager:

    * ``status == 'ok'`` — dispatch succeeded (HTTP Basic auth + GET
      against ``/v1/hosts``).
    * ``handle`` is a :class:`ResultHandle` — the reducer's return value
      flowed through ``wrap_ok_result``.
    * ``handle.total_rows`` matches the seeded host count from
      :data:`SDDC_CANARY_HOSTS`.
    * ``handle.summary_md`` is non-empty and mentions the row count.
    * ``handle.schema_`` is a non-empty mapping.
    * ``handle.sample_rows`` is populated — at least one row from the
      seeded host list.
    * ``result['row_count']`` matches the handle's total.
    """
    expected_rows = len(SDDC_CANARY_HOSTS["elements"])  # type: ignore[arg-type]

    result_envelope = await call_operation(
        ingested_sddc_canary.operator,
        {
            "connector_id": ingested_sddc_canary.connector_id,
            "op_id": SDDC_FORCE_HANDLE_LIST_OP_ID,
            "target": {"name": ingested_sddc_canary.target_name},
            "params": {},
        },
    )

    assert result_envelope["status"] == "ok", (
        f"force-handle dispatch did not succeed: {result_envelope!r}"
    )

    handle = result_envelope.get("handle")
    assert handle is not None, (
        f"expected OperationResult.handle to be populated by JsonFluxReducer; "
        f"got handle=None on envelope={result_envelope!r}"
    )
    uuid.UUID(handle["handle_id"])
    assert handle["total_rows"] == expected_rows, (
        f"expected {expected_rows} hosts from the seeded SDDC_CANARY_HOSTS; "
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
    assert "id" in schema_props and "fqdn" in schema_props, (
        f"expected SDDC host columns in the inferred schema; got {schema_props!r}"
    )
    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        f"expected ≥1 sample row from the seeded SDDC host list; got sample_rows={sample_rows!r}"
    )
    # Sample rows are real host records carrying the seeded id pattern.
    assert sample_rows[0]["id"].startswith("host-"), (
        f"expected a real seeded host in sample_rows; got {sample_rows[0]!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"expected the reducer's summary on result; got result={payload!r}"
    )
