# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bounded ``query_topology`` answers and their completeness (#3764).

The closure traversal and ``query_history`` are patched at the tool's
import site, so these tests need no PostgreSQL. The rows go through the
real shared reducer into an in-memory result store, and are read back
through the real ``result_query`` core. Each limit must give its own,
distinct answer:

* traversal limits (``hop_limit``, ``node_limit``, ``byte_limit``):
  ``incomplete``; ``time_budget``: ``unavailable`` with no nodes;
* store limits (``store_cap``: ``incomplete``; ``store_unavailable``:
  ``unavailable``);
* the wire ceiling (``delivery_cap``): ``delivery`` is ``partial`` when the
  handle is readable and ``unavailable`` when it is not. The finished
  answer, whatever its form, is measured and fits the ceiling.

History answers get the same envelope, and their inline preview leaves
out the ``snapshot`` payloads. Every answer is checked against the tool's
declared ``outputSchema``.
"""

from __future__ import annotations

import json
import types
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import jsonschema
import pytest
from fastapi.testclient import TestClient

import meho_backplane.mcp.tools._topology_answer as answer_module
import meho_backplane.operations.jsonflux_reducer as reducer_module
import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import SpillResult
from meho_backplane.jsonflux.query.result_catalog import AdmissionGuard
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.operations.delivery_budget import measure_delivered_wire_bytes
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.topology.query import BoundedClosure, ClosureBudget, ClosureLimit
from meho_backplane.topology.schemas import (
    TopologyHistoryEntry,
    TopologyHistoryResult,
    TopologyNode,
)
from tests._result_store_helpers import MemoryResultStore
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

pytestmark = pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)

_CLOSURE_PATCH = "meho_backplane.mcp.tools.topology.bounded_closure"
_HISTORY_PATCH = "meho_backplane.mcp.tools.topology.query_history"


class _RefusingStore(MemoryResultStore):
    """A store that keeps nothing, like a full or unreachable result cache."""

    async def spill(self, **kwargs: Any) -> SpillResult:
        del kwargs
        return SpillResult(stored_rows=0)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryResultStore:
    """Route the reducer's spill and the read core to one in-memory store."""
    memory = MemoryResultStore()
    monkeypatch.setattr(reducer_module, "get_result_handle_store", lambda: memory)
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: memory)
    return memory


def _nodes(count: int) -> list[TopologyNode]:
    root = UUID(int=1)
    return [
        TopologyNode(
            id=UUID(int=i + 1),
            kind="host" if i == 0 else "vm",
            name="hub" if i == 0 else f"leaf-{i:04d}",
            source="auto",
            properties={},
            depth=0 if i == 0 else 1,
            via_edge_kind=None if i == 0 else "runs-on",
            parent_node_id=None if i == 0 else root,
            via_edge_id=None if i == 0 else UUID(int=10_000 + i),
        )
        for i in range(count)
    ]


def _closure(count: int, limit_hit: ClosureLimit | None = None) -> AsyncMock:
    nodes = _nodes(max(count, 1))
    kept = tuple(nodes[:count])
    return AsyncMock(return_value=BoundedClosure(nodes[0], kept, limit_hit, 16, ClosureBudget()))


def _call(client: TestClient, arguments: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "query_topology", "arguments": arguments},
        },
    )
    body = response.json()
    assert body["result"]["isError"] is False, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload == body["result"]["structuredContent"]
    entry = mcp_registry.get_tool("query_topology")
    assert entry is not None
    jsonschema.validate(
        instance=payload,
        schema=entry[0].outputSchema,
        cls=jsonschema.Draft202012Validator,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    return payload


def _dependents(client: TestClient, mock: AsyncMock) -> dict[str, Any]:
    with patch(_CLOSURE_PATCH, new=mock):
        return _call(client, {"kind": "dependents", "target": "hub"})


def test_small_complete_closure_stays_inline_with_counts_and_limits(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
) -> None:
    client, _op = client_with_operator
    payload = _dependents(client, _closure(3))

    assert [n["name"] for n in payload["nodes"]] == ["hub", "leaf-0001", "leaf-0002"]
    assert "handle" not in payload
    assert payload["root"] == {
        "id": str(UUID(int=1)),
        "kind": "host",
        "name": "hub",
        "source_scope": None,
    }
    assert payload["node_count"] == 3
    assert payload["edge_count"] == 2
    assert payload["ordering"] == "depth, name, id"
    assert payload["query"]["target"] == "hub"
    assert payload["delivery"] == "complete"
    completeness = payload["completeness"]
    assert completeness["state"] == "complete"
    assert completeness["limits_hit"] == []
    assert completeness["found"] == completeness["retained"] == 3
    assert completeness["guidance"] is None
    assert set(completeness["limits"]) == {
        "max_depth",
        "max_nodes",
        "max_bytes",
        "time_budget_ms",
    }


@pytest.mark.parametrize("limit", ["hop_limit", "node_limit", "byte_limit"])
def test_traversal_limits_are_incomplete_and_name_the_limit(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
    limit: ClosureLimit,
) -> None:
    client, _op = client_with_operator
    payload = _dependents(client, _closure(3, limit))

    assert payload["completeness"]["state"] == "incomplete"
    assert payload["completeness"]["limits_hit"] == [limit]
    assert payload["completeness"]["guidance"]
    assert len(payload["nodes"]) == 3


def test_time_budget_is_unavailable_with_the_root_still_named(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
) -> None:
    client, _op = client_with_operator
    payload = _dependents(client, _closure(0, "time_budget"))

    assert payload["completeness"]["state"] == "unavailable"
    assert payload["completeness"]["limits_hit"] == ["time_budget"]
    assert payload["nodes"] == []
    assert payload["node_count"] == 0
    assert payload["root"]["name"] == "hub"
    assert "nothing depends on it" in payload["completeness"]["guidance"]


def test_large_closure_is_kept_behind_a_readable_handle_in_order(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
) -> None:
    client, _op = client_with_operator
    payload = _dependents(client, _closure(80))

    assert "nodes" not in payload
    assert payload["row_count"] == 80
    assert payload["completeness"]["state"] == "complete"
    assert payload["completeness"]["retained"] == 80
    drill_in = payload["handle"]["fetch_more"]["drill_in"]
    assert drill_in["available"] is True
    assert drill_in["stored_rows"] == 80

    page = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "result_query",
                "arguments": {"handle_id": payload["handle"]["handle_id"], "limit": 100},
            },
        },
    ).json()
    rows = json.loads(page["result"]["content"][0]["text"])["rows"]
    assert [row["name"] for row in rows] == ["hub"] + [f"leaf-{i:04d}" for i in range(1, 80)]


def test_store_cap_is_incomplete_and_says_how_many_rows_were_kept(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _op = client_with_operator
    monkeypatch.setattr(answer_module, "_REDUCER", JsonFluxReducer(store=store, max_spill_rows=10))
    payload = _dependents(client, _closure(80))

    completeness = payload["completeness"]
    assert completeness["state"] == "incomplete"
    assert completeness["limits_hit"] == ["store_cap"]
    assert completeness["found"] == 80
    assert completeness["retained"] == 10


def test_store_refusal_is_unavailable(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _op = client_with_operator
    monkeypatch.setattr(answer_module, "_REDUCER", JsonFluxReducer(store=_RefusingStore()))
    payload = _dependents(client, _closure(80))

    completeness = payload["completeness"]
    assert completeness["state"] == "unavailable"
    assert completeness["limits_hit"] == ["store_unavailable"]
    assert completeness["retained"] == 0
    assert payload["handle"]["fetch_more"]["drill_in"]["available"] is False


def test_unprofiled_rows_keep_the_schema_and_read_as_unavailable(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows over the reducer's work limit give `reason`, never `status: unprofiled`."""
    client, _op = client_with_operator
    guard = AdmissionGuard(max_nodes=20)
    monkeypatch.setattr(
        answer_module, "_REDUCER", JsonFluxReducer(store=store, admission_guard=guard)
    )
    payload = _dependents(client, _closure(80))  # validated against the outputSchema

    assert "status" not in payload
    assert payload["reason"] == "admission_limit_exceeded"
    assert payload["completeness"]["state"] == "unavailable"
    assert payload["completeness"]["limits_hit"] == ["store_unavailable"]
    assert payload["completeness"]["retained"] == 0
    assert payload["handle"]["fetch_more"]["drill_in"]["available"] is False


def _ceiling(monkeypatch: pytest.MonkeyPatch, max_bytes: int) -> None:
    settings = types.SimpleNamespace(result_complete_response_wire_max_bytes=max_bytes)
    monkeypatch.setattr(answer_module, "get_settings", lambda: settings)


def test_delivery_cap_with_a_readable_handle_is_partial(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _op = client_with_operator
    _ceiling(monkeypatch, 9000)
    payload = _dependents(client, _closure(80))

    assert payload["delivery"] == "partial"
    assert payload["handle"]["sample_rows"] is None
    assert payload["completeness"]["state"] == "complete"
    assert payload["completeness"]["limits_hit"] == ["delivery_cap"]
    assert payload["completeness"]["retained"] == 80
    assert payload["delivery_max_bytes"] == 9000
    assert measure_delivered_wire_bytes(payload) <= 9000


def test_delivery_cap_without_a_handle_is_unavailable(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _op = client_with_operator
    _ceiling(monkeypatch, 2048)
    payload = _dependents(client, _closure(3))

    assert payload["delivery"] == "unavailable"
    assert "nodes" not in payload
    assert payload["completeness"]["state"] == "unavailable"
    assert payload["completeness"]["limits_hit"] == ["delivery_cap"]
    assert payload["completeness"]["retained"] == 0
    assert measure_delivered_wire_bytes(payload) <= 2048


def test_finished_answer_is_measured_with_its_completeness_and_delivery(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The size check covers the whole answer, so a ceiling of exactly its size passes."""
    client, _op = client_with_operator
    _ceiling(monkeypatch, 1 << 20)
    whole = _dependents(client, _closure(3))
    size = measure_delivered_wire_bytes(whole)

    _ceiling(monkeypatch, size)
    assert _dependents(client, _closure(3))["delivery"] == "complete"

    _ceiling(monkeypatch, size - 1)
    smaller = _dependents(client, _closure(3))
    assert smaller["delivery"] == "unavailable"
    assert smaller["delivered_bytes"] == size
    assert measure_delivered_wire_bytes(smaller) <= size - 1


def test_every_smaller_answer_fits_the_ceiling_it_was_cut_for(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """partial, then unavailable, then the bare receipt: each fits its ceiling.

    Each next ceiling sits a few bytes under the previous answer, not one
    byte, because the two byte counts in the answer can lose a digit
    between calls.
    """
    client, _op = client_with_operator
    _ceiling(monkeypatch, 1 << 20)
    whole_size = measure_delivered_wire_bytes(_dependents(client, _closure(80)))

    _ceiling(monkeypatch, whole_size - 1)
    partial = _dependents(client, _closure(80))
    assert partial["delivery"] == "partial"
    partial_size = measure_delivered_wire_bytes(partial)
    assert partial_size <= whole_size - 1

    _ceiling(monkeypatch, partial_size - 16)
    unavailable = _dependents(client, _closure(80))
    assert unavailable["delivery"] == "unavailable"
    assert "handle" not in unavailable
    assert unavailable["root"]["name"] == "hub"
    unavailable_size = measure_delivered_wire_bytes(unavailable)
    assert unavailable_size <= partial_size - 16

    _ceiling(monkeypatch, unavailable_size - 16)
    receipt = _dependents(client, _closure(80))
    assert set(receipt) == {
        "kind",
        "completeness",
        "delivery",
        "delivered_bytes",
        "delivery_max_bytes",
    }
    assert receipt["completeness"]["state"] == "unavailable"
    assert receipt["completeness"]["limits_hit"] == ["delivery_cap"]
    assert measure_delivered_wire_bytes(receipt) <= unavailable_size - 16
    for answer in (partial, unavailable, receipt):
        assert answer["delivered_bytes"] == whole_size


def _history(count: int, snapshot_bytes: int) -> TopologyHistoryResult:
    snapshot = {"before": None, "after": {"pad": "x" * snapshot_bytes}}
    rows = tuple(
        TopologyHistoryEntry(
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            history_id=count - i,
            source="node",
            change_kind="updated",
            resource_id=UUID(int=1),
            snapshot=snapshot,
            audit_id=None,
        )
        for i in range(count)
    )
    return TopologyHistoryResult(anchor_node_id=UUID(int=1), include_edges=False, rows=rows)


async def test_large_history_keeps_snapshots_behind_the_handle_only(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
) -> None:
    client, op = client_with_operator
    with patch(_HISTORY_PATCH, new=AsyncMock(return_value=_history(30, 2000))):
        payload = _call(client, {"kind": "history", "target": "hub"})

    assert "rows" not in payload
    assert payload["preview_omits"] == ["snapshot"]
    assert all("snapshot" not in row for row in payload["handle"]["sample_rows"] or [])
    assert payload["completeness"]["state"] == "complete"
    assert payload["completeness"]["found"] == 30
    assert payload["anchor_node_id"] == str(UUID(int=1))

    handle_id = UUID(payload["handle"]["handle_id"])
    window = await result_query_core.read_result_window(op, handle_id, 0, 5)
    assert window["rows"][0]["snapshot"]["after"]["pad"] == "x" * 2000


def test_full_history_page_is_never_reported_complete(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
) -> None:
    client, _op = client_with_operator
    with patch(_HISTORY_PATCH, new=AsyncMock(return_value=_history(2, 10))):
        payload = _call(client, {"kind": "history", "target": "hub", "limit": 2})

    assert len(payload["rows"]) == 2
    assert payload["completeness"]["state"] == "incomplete"
    assert payload["completeness"]["limits_hit"] == ["row_limit"]


async def test_handle_of_another_caller_is_not_readable(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    store: MemoryResultStore,
) -> None:
    client, op = client_with_operator
    payload = _dependents(client, _closure(80))
    handle_id = UUID(payload["handle"]["handle_id"])

    other = op.model_copy(update={"sub": f"other-{uuid4()}"})
    with pytest.raises(result_query_core.ResultHandleNotFoundError):
        await result_query_core.read_result_window(other, handle_id, 0, 5)
