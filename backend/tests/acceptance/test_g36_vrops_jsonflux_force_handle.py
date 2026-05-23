# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.6-T2 JSONFlux force-mode acceptance for the vROps connector.

Drives the **real**
:class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
(G0.6.1-T3 #753) in force mode (``row_threshold=0``) as the dispatcher
default. Mirrors :mod:`tests.acceptance.test_g35_nsx_jsonflux_force_handle`,
swapping in vROps as the dispatched connector: it dispatches
``GET:/suite-api/api/resources`` against the seeded vROps core and
asserts the
:class:`~meho_backplane.connectors.schemas.OperationResult`'s
``handle`` field carries a populated :class:`ResultHandle` with the
real materialized shape.

vROps' suite-api wraps list payloads under noun-specific keys
(``resourceList``, ``alerts``, ``symptoms``, etc.) rather than the
generic ``results`` / ``value`` keys NSX and vSphere use. The reducer's
collection detection falls back to the largest top-level list value
when no known envelope key matches, so it locates ``resourceList``
without the test leaking vendor-specific shape into the reducer.
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
from tests.acceptance._vrops_canary_fixtures import (
    VROPS_CANARY_RESOURCES,
    VROPS_FORCE_HANDLE_LIST_OP_ID,
    IngestedVropsCanary,
)


@pytest.fixture
def force_handle_reducer() -> Any:
    """Install :class:`JsonFluxReducer` in force mode as the dispatcher default.

    ``row_threshold=0`` forces every non-empty set to materialize, so
    the seeded resource list (below the default 50-row threshold)
    produces a handle. Teardown restores :class:`PassThroughReducer` so
    a follow-on test in the same session sees the v0.2 default.
    """
    set_default_reducer(JsonFluxReducer(row_threshold=0))
    try:
        yield
    finally:
        set_default_reducer(PassThroughReducer())
        reset_dispatcher_caches()


async def test_force_handle_reducer_populates_operation_result_handle_for_vrops(
    force_handle_reducer: None,
    ingested_vrops_canary: IngestedVropsCanary,
) -> None:
    """Dispatching the vROps resource list populates ``OperationResult.handle``.

    Confirms the JSONFlux dispatcher seam end-to-end for vROps:

    * ``status == 'ok'`` — dispatch succeeded (HTTP Basic + GET
      against the resource-list path).
    * ``handle`` is a :class:`ResultHandle` — the reducer's return
      value flowed through ``wrap_ok_result``.
    * ``handle.total_rows`` matches the seeded resource count from
      :data:`VROPS_CANARY_RESOURCES`.
    * ``handle.summary_md`` is non-empty and mentions the row count —
      the reducer composed its summary correctly.
    * ``handle.schema_`` is a non-empty mapping.
    * ``handle.sample_rows`` is populated — ≥1 row from the seeded
      resource list.
    * ``result['row_count']`` matches the handle's total — the
      dispatcher inlined the reducer's summary on the operation
      envelope (not the raw resource list).
    """
    resource_list = VROPS_CANARY_RESOURCES["resourceList"]
    assert isinstance(resource_list, list)
    expected_rows = len(resource_list)

    result_envelope = await call_operation(
        ingested_vrops_canary.operator,
        {
            "connector_id": ingested_vrops_canary.connector_id,
            "op_id": VROPS_FORCE_HANDLE_LIST_OP_ID,
            "target": {"name": ingested_vrops_canary.target_name},
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
    uuid.UUID(handle["handle_id"])
    assert handle["total_rows"] == expected_rows, (
        f"expected {expected_rows} resources from the seeded VROPS_CANARY_RESOURCES; "
        f"got handle.total_rows={handle['total_rows']}"
    )
    assert handle["summary_md"], "handle.summary_md must be non-empty"
    assert str(expected_rows) in handle["summary_md"], (
        f"expected summary_md to mention the row count; got {handle['summary_md']!r}"
    )
    assert isinstance(handle["schema_"], dict) and handle["schema_"], (
        f"handle.schema_ must be a non-empty mapping; got {handle['schema_']!r}"
    )
    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        f"expected ≥1 sample row from the seeded resource list; got sample_rows={sample_rows!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"expected the reducer's summary on result; got result={payload!r}"
    )
