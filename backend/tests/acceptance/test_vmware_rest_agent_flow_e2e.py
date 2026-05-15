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

from meho_backplane.operations.ingest import list_ingested_connectors
from meho_backplane.operations.meta_tools import (
    call_operation,
    list_operation_groups,
    search_operations,
)
from tests.acceptance._canary_fixtures import (
    CANARY_CONNECTOR_ID,
    IngestedCanaryVcsim,
)


async def test_agent_flow_end_to_end_against_vcsim(
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
       return ``status='ok'`` carrying real data from vcsim (the
       seeded topology has 50 VMs, so the inlined ``result['value']``
       list has 50 entries).
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
    payload = result_envelope.get("result")
    assert payload is not None, f"expected non-null OperationResult.result; got {result_envelope!r}"
    # vCenter REST returns set-shaped responses as ``{"value": [...]}``
    # on the legacy ``/rest`` mount and as a bare list on the modern
    # ``/api`` mount. vcsim serves both shapes depending on which
    # endpoint the connector hits; assert on the union.
    vms = payload["value"] if isinstance(payload, dict) and "value" in payload else payload
    assert isinstance(vms, list), (
        f"expected a list of VMs from vcsim's 50-VM seed topology; "
        f"got payload type {type(payload).__name__} body={payload!r}"
    )
    assert len(vms) == 50, (
        f"expected 50 VMs from vcsim's seed topology; got {len(vms)} "
        f"(payload shape={type(payload).__name__})"
    )


async def test_search_operations_unknown_connector_returns_no_hits(
    ingested_canary_vcsim: IngestedCanaryVcsim,
) -> None:
    """An unknown connector_id returns an empty hit list, not an error.

    Same shape the canary's
    ``test_canary_search_operations_unknown_connector_returns_empty``
    asserts — the meta-tool's contract is "unknown connector → empty
    hits" rather than "→ error". Keeping the assertion here makes
    the agent-flow surface self-contained: any tester running this
    file alone sees both the happy path and the empty-input path.
    """
    operator = ingested_canary_vcsim.operator
    response = await search_operations(
        operator,
        {
            "connector_id": "no-such-connector-9.99",
            "query": "anything",
            "limit": 3,
        },
    )
    assert response["hits"] == [], f"expected empty hits for unknown connector; got {response!r}"
