# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.7-T8 JSONFlux force-mode acceptance for the Hetzner Robot connector.

Drives the **real**
:class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
(G0.6.1-T3 #753) in force mode (``row_threshold=0``) as the dispatcher
default. Mirrors :mod:`tests.acceptance.test_g36_vrops_jsonflux_force_handle`,
swapping in Hetzner Robot as the dispatched connector: it dispatches
``GET:/server`` against the seeded Robot core and asserts the
:class:`~meho_backplane.connectors.schemas.OperationResult`'s ``handle``
field carries a populated :class:`ResultHandle` with the real
materialized shape.

Hetzner Robot's ``GET:/server`` returns a top-level JSON array of server
wrapper objects (``[{"server": {...}}, ...]``). The reducer's collection
detection hits its bare-top-level-list branch, so the dispatcher-seam
test doesn't depend on a vendor-specific envelope key.

Proves AC4 of G3.7-T8 #849: ``server.list`` returns a JSONFlux handle;
``result_describe`` / ``result_query`` resolve against it (the seam
test proves the dispatcher is ready for the production reducer once it
ships per Goal #214).
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
from tests.acceptance._robot_canary_fixtures import (
    ROBOT_CANARY_SERVERS,
    ROBOT_FORCE_HANDLE_LIST_OP_ID,
    ROBOT_FORCE_HANDLE_PARAMS,
    IngestedRobotCanary,
)


@pytest.fixture
def force_handle_reducer() -> Any:
    """Install :class:`JsonFluxReducer` in force mode as the dispatcher default.

    ``row_threshold=0`` forces every non-empty set to materialize, so
    the seeded server list (below the default 50-row threshold) produces
    a handle. Teardown restores :class:`PassThroughReducer` so a
    follow-on test in the same session sees the v0.2 default.
    """
    set_default_reducer(JsonFluxReducer(row_threshold=0))
    try:
        yield
    finally:
        set_default_reducer(PassThroughReducer())
        reset_dispatcher_caches()


async def test_force_handle_reducer_populates_operation_result_handle_for_robot(
    force_handle_reducer: None,
    ingested_robot_canary: IngestedRobotCanary,
) -> None:
    """Dispatching the Robot server list populates ``OperationResult.handle``.

    Confirms the JSONFlux dispatcher seam end-to-end for Hetzner Robot:

    * ``status == 'ok'`` — dispatch succeeded (HTTP Basic auth + GET
      against the server list path).
    * ``handle`` is a :class:`ResultHandle` — the reducer's return value
      flowed through ``wrap_ok_result``.
    * ``handle.total_rows`` matches the seeded server count from
      :data:`ROBOT_CANARY_SERVERS`.
    * ``handle.summary_md`` is non-empty and mentions the row count.
    * ``handle.schema_`` is a non-empty mapping.
    * ``handle.sample_rows`` is populated — at least one row from the
      seeded server list.
    * ``result['row_count']`` matches the handle's total.

    The server list response is a JSON array of ``{"server": {...}}``
    wrapper objects (not a pagination envelope), so the reducer's
    bare-top-level-list branch is exercised here — different from the
    NSX and SDDC tests which hit the ``results[]`` and ``elements[]``
    branches respectively.

    Proves AC4 of #849 (G3.7-T8): ``server.list`` returns a JSONFlux
    handle; the dispatcher seam is wired and ready for the production
    reducer (Goal #214 scope).
    """
    expected_rows = len(ROBOT_CANARY_SERVERS)

    result_envelope = await call_operation(
        ingested_robot_canary.operator,
        {
            "connector_id": ingested_robot_canary.connector_id,
            "op_id": ROBOT_FORCE_HANDLE_LIST_OP_ID,
            "target": {"name": ingested_robot_canary.target_name},
            "params": ROBOT_FORCE_HANDLE_PARAMS,
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
        f"expected {expected_rows} servers from the seeded ROBOT_CANARY_SERVERS; "
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
        f"expected ≥1 sample row from the seeded server list; got sample_rows={sample_rows!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"expected the reducer's summary on result; got result={payload!r}"
    )
