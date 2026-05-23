# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.1-T8 end-to-end agent-flow acceptance test.

Closes #227's "Agent flow tested end-to-end" Definition-of-done line:

    search_connectors finds vmware-rest-9.0
        → list_operation_groups returns the enabled groups
        → search_operations(query="list VMs in cluster X") returns ranked candidates
        → call_operation(...) against vcsim returns structured response.

This test exercises the four agent-surface meta-tools as a single
chain so a regression in any link (connector visibility, group
visibility, search ranking, dispatch) breaks the chain rather than
manifesting as a degraded individual response.

Where this test stops + where #519 picks up
==========================================

* This test verifies the chain produces a ``status='ok'``
  :class:`OperationResult` carrying real vcsim data on
  :attr:`OperationResult.result`.
* #519's ``test_g07_canary_vcsim_dispatch`` already verifies the
  audit + broadcast contract — that audit row counts go up by
  exactly one per dispatch and broadcast events fire for the
  same op_id. This test doesn't re-prove that contract; it focuses
  on the chain producing real data.

Scope vs the canary's existing tests
====================================

The canary's ``test_g07_vsphere_canary.py`` already exercises
``search_connectors`` / ``list_operation_groups`` /
``search_operations`` against the full 1,275-op ``vcenter.yaml``
ingest. This test deliberately uses a minimal hand-rolled
descriptor set (6 ops under one enabled group, no embedding /
LLM grouping) — its purpose is to prove the four-step chain
**dispatches** end-to-end, not to re-prove search quality. The
search-quality contract lives in the canary.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.ingest import list_ingested_connectors
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import (
    UnknownConnectorError,
    call_operation,
    list_operation_groups,
    search_operations,
)
from meho_backplane.operations.reducer import PassThroughReducer
from tests.acceptance._canary_fixtures import (
    CANARY_CONNECTOR_ID,
    IngestedCanaryVcsim,
)


@pytest.fixture
def production_default_reducer() -> Any:
    """Install the production-default :class:`JsonFluxReducer` as the dispatcher default.

    The FastAPI lifespan installs ``JsonFluxReducer()`` (default
    ``row_threshold=50`` / ``byte_threshold=4096``) as the module-level
    dispatcher default (``main.py``'s ``set_default_reducer`` call). This
    fixture pins that same production default for the agent-flow chain so
    the test asserts the shape the deployed backplane actually returns —
    independent of whichever reducer a prior test in the same pytest
    worker left installed (the module-level default is process-global and
    the lifespan does not restore it on shutdown).

    Teardown restores :class:`PassThroughReducer` (the dispatcher's
    import-time default) and drops the dispatcher caches so no
    connector-instance state leaks into a follow-on test.
    """
    set_default_reducer(JsonFluxReducer())
    try:
        yield
    finally:
        set_default_reducer(PassThroughReducer())
        reset_dispatcher_caches()


async def test_agent_flow_end_to_end_against_vcsim(
    prewarmed_embeddings: None,
    production_default_reducer: None,
    ingested_canary_vcsim: IngestedCanaryVcsim,
) -> None:
    """The four-step agent chain produces real vcsim data on call_operation.

    Walks the chain in the order the agent surface contract
    documents:

    1. ``list_ingested_connectors`` (the agent's ``search_connectors``
       surface) must surface ``vmware-rest-9.0`` with at least one
       enabled group.
    2. ``list_operation_groups(connector_id="vmware-rest-9.0")`` must
       return at least one enabled group with a non-empty ``when_to_use``
       hint.
    3. ``search_operations(connector_id="vmware-rest-9.0",
       query="list virtual machines")`` must return ``GET:/vcenter/vm``
       in the top-3 hits. The minimal descriptor set this fixture
       seeds has six ops with short, descriptive ``summary`` strings;
       BM25 alone is enough to rank ``GET:/vcenter/vm`` first on the
       "list virtual machines" query (the canary's full-corpus search-
       quality assertion lives in ``test_g07_vsphere_canary.py``).
    4. ``call_operation(op_id="GET:/vcenter/vm", target=<vcsim>)`` must
       return ``status='ok'``. With the production-default
       :class:`JsonFluxReducer` installed (G0.6.1-T3 #753), the 50-VM
       seed materializes into a JSONFlux handle: the row count is *at*
       the 50-row threshold (``50 > 50`` is False) but the serialized
       payload exceeds the 4 KB ``byte_threshold``, so the byte branch
       fires. The chain therefore returns the reduced summary on
       ``result`` plus a populated :class:`ResultHandle` (the agent
       drills into the full set via ``result_query`` / ``result_export``
       — CLAUDE.md postulate 6 / v0.1-spec §4) rather than a raw
       50-element list inline.
    """
    operator = ingested_canary_vcsim.operator

    # 1. search_connectors surface.
    connectors = await list_ingested_connectors(operator=operator)
    matching = [c for c in connectors if c.connector_id == CANARY_CONNECTOR_ID]
    assert matching, (
        f"expected {CANARY_CONNECTOR_ID!r} in connector listing; "
        f"got {[c.connector_id for c in connectors]!r}"
    )
    assert matching[0].enabled_group_count >= 1, (
        f"expected at least one enabled group on {CANARY_CONNECTOR_ID!r}; "
        f"got {matching[0].enabled_group_count}"
    )

    # 2. list_operation_groups surface.
    groups_response = await list_operation_groups(operator, {"connector_id": CANARY_CONNECTOR_ID})
    groups = groups_response["groups"]
    assert groups, f"expected at least one enabled group; got groups={groups!r}"
    assert all(g["when_to_use"] for g in groups), (
        f"every group must carry a non-empty when_to_use; got {groups!r}"
    )

    # 3. search_operations surface.
    search_response = await search_operations(
        operator,
        {
            "connector_id": CANARY_CONNECTOR_ID,
            "query": "list virtual machines",
            "limit": 3,
        },
    )
    hits = search_response["hits"]
    assert hits, f"search_operations returned no hits: {search_response!r}"
    top_three = [h["op_id"] for h in hits[:3]]
    assert "GET:/vcenter/vm" in top_three, (
        f"expected GET:/vcenter/vm in top-3 hits; got {top_three!r}"
    )

    # 4. call_operation surface — the load-bearing dispatch leg.
    result_envelope = await call_operation(
        operator,
        {
            "connector_id": CANARY_CONNECTOR_ID,
            "op_id": "GET:/vcenter/vm",
            "target": {"name": ingested_canary_vcsim.target_name},
            "params": {},
        },
    )
    assert result_envelope["status"] == "ok", f"dispatch did not succeed: {result_envelope!r}"

    # With the production-default JsonFluxReducer installed, the 50-VM
    # seed (≈5 KB serialized, over the 4 KB byte_threshold) materializes
    # into a handle. The agent never sees the raw 50-element list inline:
    # ``result`` carries the reduced summary, ``handle`` carries the
    # ResultHandle the agent drills into.
    handle = result_envelope.get("handle")
    assert handle is not None, (
        f"expected OperationResult.handle to be populated by the "
        f"production-default JsonFluxReducer; got handle=None on "
        f"envelope={result_envelope!r}"
    )
    assert handle["total_rows"] == 50, (
        f"expected 50 VMs from vcsim's seed topology; got handle.total_rows={handle['total_rows']}"
    )
    # The bounded sample lets the agent preview the set without
    # materializing all 50 rows; it must be non-empty and capped below
    # the full row count.
    sample_rows = handle.get("sample_rows")
    assert sample_rows, f"expected a bounded sample on the handle; got sample_rows={sample_rows!r}"
    assert 0 < len(sample_rows) < handle["total_rows"], (
        f"sample must be a bounded slice of the 50-VM set; got {len(sample_rows)} rows"
    )

    # The inlined result is the reducer's summary, not the raw 50-VM set.
    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == 50, (
        f"expected the reducer's reduced summary on result with "
        f"row_count==50; got result={payload!r}"
    )


async def test_search_operations_unknown_connector_raises(
    prewarmed_embeddings: None,
    ingested_canary_vcsim: IngestedCanaryVcsim,
) -> None:
    """An unknown connector_id raises UnknownConnectorError (REST → 404).

    G0.8-T5 (#630) reversed the prior "unknown connector → empty hits"
    contract: an empty success was indistinguishable from a known
    connector with no matching ops, so a mis-shaped connector_id read
    as an empty catalog. The meta-tool now fails loud; the REST route
    maps it to a 404. This file stays self-contained — any tester
    running it alone sees both the happy path and the fail-loud path.
    """
    operator = ingested_canary_vcsim.operator
    with pytest.raises(UnknownConnectorError):
        await search_operations(
            operator,
            {
                "connector_id": "no-such-connector-9.99",
                "query": "anything",
                "limit": 3,
            },
        )
