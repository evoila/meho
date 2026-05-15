# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the 5 vmware-rest read-composite handler functions.

Coverage matrix (G3.1-T5 / #508 acceptance criteria):

* Per-composite: assert the correct sub-op_ids fire in the expected
  order, with the right ``connector_id`` and ``params`` shape.
* Aggregation correctness: the handler's returned dict matches the
  spec sketched in #508's issue body.
* Envelope tolerance: ``{"value": [...]}`` (legacy) and bare lists
  (modern) both parse correctly.
* Filter pass-through: ``filter_names`` / ``filter_dvs`` / ``moId``
  / ``entity_moid`` flow into the sub-op params.
* Error fan-out: a sub-op returning ``status="error"`` causes the
  handler to raise ``RuntimeError`` (load-bearing for the dispatcher's
  ``connector_error`` wrapping at the composite parent).

Each test mocks ``dispatch_child`` as an :class:`AsyncMock`-style
callable that returns canned ``OperationResult`` objects keyed on
``op_id``. The handlers are invoked directly (no dispatcher in the
loop) so the assertion target is the call-shape contract: which
sub-ops, in what order, with what params.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._read import (
    cluster_drs_recommendations_composite,
    datastore_usage_composite,
    event_tail_composite,
    network_portgroup_audit_composite,
    performance_summary_composite,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_operator() -> Operator:
    """Synthetic operator for composite-handler unit tests."""
    return Operator(
        sub="op-composite-read",
        name="Composite Read Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


class _RecordingDispatchChild:
    """Lightweight ``dispatch_child`` stub that returns canned responses.

    Avoids :class:`unittest.mock.AsyncMock` so the test reads as a plain
    keyword-args call sequence and the matching ``DispatchChild``
    Protocol shape is preserved (``AsyncMock.__call__`` doesn't enforce
    keyword-only).
    """

    def __init__(self, responses: dict[str, Any] | list[Any]) -> None:
        """Build a recording stub.

        Parameters
        ----------
        responses:
            Either a mapping of ``op_id -> result`` (the handler dispatches
            each op once; subsequent calls reuse the same payload), or a
            sequential list of ``OperationResult`` values returned in
            order. The list form lets tests assert per-call payloads when
            the same op_id is dispatched multiple times.
        """
        self._responses = responses
        self._sequence_index = 0
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        connector_id: str,
        op_id: str,
        params: dict[str, Any],
        target: Any = None,
    ) -> OperationResult:
        # Record before serving so the assertion sees the call even
        # if the handler raises on the (canned) result.
        self.calls.append(
            {
                "connector_id": connector_id,
                "op_id": op_id,
                "params": dict(params),
                "target": target,
            }
        )
        if isinstance(self._responses, dict):
            payload = self._responses[op_id]
        else:
            payload = self._responses[self._sequence_index]
            self._sequence_index += 1
        if isinstance(payload, OperationResult):
            return payload
        return _ok_result(op_id, payload)


def _ok_result(op_id: str, result: Any) -> OperationResult:
    """Build an OK :class:`OperationResult` with ``result`` as the body."""
    return OperationResult(
        status="ok",
        op_id=op_id,
        result=result,
        duration_ms=1.0,
    )


def _err_result(op_id: str, error: str) -> OperationResult:
    """Build an error :class:`OperationResult` for failure-fan-out tests."""
    return OperationResult(
        status="error",
        op_id=op_id,
        error=error,
        duration_ms=1.0,
    )


# ---------------------------------------------------------------------------
# vmware.composite.cluster.drs_recommendations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cluster_drs_recommendations_dispatches_summary_and_drs_in_order() -> None:
    """Two sub-ops fire: cluster summary then DRS config, both with the cluster moid."""
    cluster_payload = {"name": "Cluster-A", "drs_enabled": True}
    drs_payload = {"enabled": True, "automation_level": "FULLY_AUTOMATED"}
    dispatch = _RecordingDispatchChild(
        {
            "GET:/vcenter/cluster/{cluster}": cluster_payload,
            "GET:/vcenter/cluster/{cluster}/drs": drs_payload,
        }
    )

    out = await cluster_drs_recommendations_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c123"},
        dispatch_child=dispatch,
    )

    assert [c["op_id"] for c in dispatch.calls] == [
        "GET:/vcenter/cluster/{cluster}",
        "GET:/vcenter/cluster/{cluster}/drs",
    ]
    assert all(c["connector_id"] == "vmware-rest-9.0" for c in dispatch.calls)
    assert all(c["params"] == {"cluster": "domain-c123"} for c in dispatch.calls)
    assert out == {
        "cluster": cluster_payload,
        "drs": drs_payload,
    }


@pytest.mark.asyncio
async def test_cluster_drs_recommendations_history_flag_surfaces_history_slice() -> None:
    """``include_recommendations_history=True`` surfaces ``history`` from the DRS payload."""
    dispatch = _RecordingDispatchChild(
        {
            "GET:/vcenter/cluster/{cluster}": {"name": "Cluster-A"},
            "GET:/vcenter/cluster/{cluster}/drs": {
                "enabled": True,
                "history": [{"recommendation_id": "rec-1", "reason": "load balance"}],
            },
        }
    )
    out = await cluster_drs_recommendations_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c1", "include_recommendations_history": True},
        dispatch_child=dispatch,
    )
    assert "recommendations_history" in out
    assert out["recommendations_history"] == [
        {"recommendation_id": "rec-1", "reason": "load balance"}
    ]


@pytest.mark.asyncio
async def test_cluster_drs_recommendations_history_flag_default_omits_key() -> None:
    """Default ``include_recommendations_history=False`` omits the key."""
    dispatch = _RecordingDispatchChild(
        {
            "GET:/vcenter/cluster/{cluster}": {"name": "Cluster-A"},
            "GET:/vcenter/cluster/{cluster}/drs": {"enabled": False, "history": [1, 2]},
        }
    )
    out = await cluster_drs_recommendations_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c1"},
        dispatch_child=dispatch,
    )
    assert "recommendations_history" not in out


# ---------------------------------------------------------------------------
# vmware.composite.event.tail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_tail_dispatches_query_events_with_default_mo_id() -> None:
    """Default ``moId='EventManager'`` and ``max_events=100`` flow through."""
    events_payload = [{"id": f"evt-{n}", "summary": f"event {n}"} for n in range(5)]
    dispatch = _RecordingDispatchChild({"POST:/EventManager/{moId}/QueryEvents": events_payload})
    out = await event_tail_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        dispatch_child=dispatch,
    )
    assert len(dispatch.calls) == 1
    only = dispatch.calls[0]
    assert only["op_id"] == "POST:/EventManager/{moId}/QueryEvents"
    assert only["connector_id"] == "vmware-rest-9.0"
    assert only["params"] == {"moId": "EventManager"}
    assert out["events"] == events_payload
    assert out["count"] == 5
    assert out["moId"] == "EventManager"
    assert out["max_events_applied"] == 100


@pytest.mark.asyncio
async def test_event_tail_caps_results_to_max_events() -> None:
    """The handler caps client-side to ``max_events``."""
    events_payload = [{"id": f"evt-{n}"} for n in range(200)]
    dispatch = _RecordingDispatchChild({"POST:/EventManager/{moId}/QueryEvents": events_payload})
    out = await event_tail_composite(
        operator=_make_operator(),
        target=object(),
        params={"max_events": 7},
        dispatch_child=dispatch,
    )
    assert len(out["events"]) == 7
    assert out["count"] == 7
    assert out["max_events_applied"] == 7


@pytest.mark.asyncio
async def test_event_tail_tolerates_legacy_value_envelope() -> None:
    """A ``{"value": [...]}`` envelope on the sub-op response unwraps cleanly."""
    dispatch = _RecordingDispatchChild(
        {"POST:/EventManager/{moId}/QueryEvents": {"value": [{"id": "evt-1"}, {"id": "evt-2"}]}}
    )
    out = await event_tail_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        dispatch_child=dispatch,
    )
    assert out["count"] == 2
    assert out["events"] == [{"id": "evt-1"}, {"id": "evt-2"}]


@pytest.mark.asyncio
async def test_event_tail_raises_on_non_list_payload() -> None:
    """Non-list payload from QueryEvents raises ``RuntimeError`` (audit-visible)."""
    dispatch = _RecordingDispatchChild({"POST:/EventManager/{moId}/QueryEvents": {"not": "a list"}})
    with pytest.raises(RuntimeError, match="expected list"):
        await event_tail_composite(
            operator=_make_operator(),
            target=object(),
            params={},
            dispatch_child=dispatch,
        )


# ---------------------------------------------------------------------------
# vmware.composite.performance.summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_performance_summary_dispatches_two_sub_ops_in_order() -> None:
    """QueryAvailablePerfMetric first, then QueryPerf -- both per-entity."""
    available_payload = [
        {"counterId": 1, "instance": ""},
        {"counterId": 2, "instance": "vmnic0"},
    ]
    samples_payload = [
        {"counterId": 1, "value": 42},
        {"counterId": 2, "value": 100},
    ]
    dispatch = _RecordingDispatchChild(
        {
            "POST:/PerformanceManager/{moId}/QueryAvailablePerfMetric": (available_payload),
            "POST:/PerformanceManager/{moId}/QueryPerf": samples_payload,
        }
    )
    out = await performance_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"entity_moid": "vm-1234"},
        dispatch_child=dispatch,
    )
    assert [c["op_id"] for c in dispatch.calls] == [
        "POST:/PerformanceManager/{moId}/QueryAvailablePerfMetric",
        "POST:/PerformanceManager/{moId}/QueryPerf",
    ]
    # Both calls use the default PerfMgr singleton + the entity moid.
    assert dispatch.calls[0]["params"] == {"moId": "PerfMgr", "entity": "vm-1234"}
    assert dispatch.calls[1]["params"] == {
        "moId": "PerfMgr",
        "entity": "vm-1234",
        "interval_seconds": 20,
    }
    assert out == {
        "entity_moid": "vm-1234",
        "perf_manager_moid": "PerfMgr",
        "available_counters": available_payload,
        "samples": samples_payload,
        "interval_seconds": 20,
        "max_samples_applied": 60,
    }


@pytest.mark.asyncio
async def test_performance_summary_caps_samples() -> None:
    """``max_samples`` caps the sample list client-side."""
    available = [{"counterId": 1}]
    samples = [{"counterId": 1, "value": n} for n in range(150)]
    dispatch = _RecordingDispatchChild(
        {
            "POST:/PerformanceManager/{moId}/QueryAvailablePerfMetric": available,
            "POST:/PerformanceManager/{moId}/QueryPerf": samples,
        }
    )
    out = await performance_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"entity_moid": "vm-7", "max_samples": 12},
        dispatch_child=dispatch,
    )
    assert len(out["samples"]) == 12
    assert out["max_samples_applied"] == 12


@pytest.mark.asyncio
async def test_performance_summary_passes_through_interval_and_custom_perf_mgr() -> None:
    """Operator-supplied ``interval_seconds`` + ``perf_manager_moid`` propagate."""
    dispatch = _RecordingDispatchChild(
        {
            "POST:/PerformanceManager/{moId}/QueryAvailablePerfMetric": [],
            "POST:/PerformanceManager/{moId}/QueryPerf": [],
        }
    )
    await performance_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "entity_moid": "vm-99",
            "perf_manager_moid": "AltPerfMgr",
            "interval_seconds": 300,
        },
        dispatch_child=dispatch,
    )
    assert dispatch.calls[1]["params"]["moId"] == "AltPerfMgr"
    assert dispatch.calls[1]["params"]["interval_seconds"] == 300


# ---------------------------------------------------------------------------
# vmware.composite.datastore.usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_datastore_usage_three_ops_per_datastore_aggregates_correctly() -> None:
    """Listing + per-DS detail + per-DS VM-listing; aggregated payload matches spec."""
    listing = [
        {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"},
        {"datastore": "datastore-2", "name": "ds-2", "type": "NFS"},
    ]
    detail_by_id = {
        "datastore-1": {"capacity": 100, "free_space": 40, "type": "VMFS"},
        "datastore-2": {"capacity": 500, "free_space": 250, "type": "NFS"},
    }
    vms_by_ds: dict[str, list[dict[str, Any]]] = {
        "datastore-1": [{"name": "vm-a"}, {"name": "vm-b"}],
        "datastore-2": [{"name": "vm-c"}],
    }

    sequence: list[OperationResult] = [_ok_result("GET:/vcenter/datastore", listing)]
    for entry in listing:
        sequence.append(
            _ok_result("GET:/vcenter/datastore/{datastore}", detail_by_id[entry["datastore"]])
        )
        sequence.append(_ok_result("GET:/vcenter/vm", vms_by_ds[entry["datastore"]]))
    dispatch = _RecordingDispatchChild(sequence)

    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        dispatch_child=dispatch,
    )

    # 1 listing + 2 datastores * 2 sub-ops each = 5 calls total.
    assert len(dispatch.calls) == 5
    assert dispatch.calls[0]["op_id"] == "GET:/vcenter/datastore"
    # No filter -> params is empty mapping.
    assert dispatch.calls[0]["params"] == {}
    # Per-DS detail call includes the datastore moid.
    assert dispatch.calls[1]["op_id"] == "GET:/vcenter/datastore/{datastore}"
    assert dispatch.calls[1]["params"] == {"datastore": "datastore-1"}
    # Per-DS VM call uses ``filter.datastores`` with the moid.
    assert dispatch.calls[2]["op_id"] == "GET:/vcenter/vm"
    assert dispatch.calls[2]["params"] == {"filter.datastores": ["datastore-1"]}
    # Final aggregated shape.
    assert out == {
        "datastores": [
            {
                "id": "datastore-1",
                "name": "ds-1",
                "type": "VMFS",
                "capacity": 100,
                "free_space": 40,
                "vm_count": 2,
                "vm_names": ["vm-a", "vm-b"],
            },
            {
                "id": "datastore-2",
                "name": "ds-2",
                "type": "NFS",
                "capacity": 500,
                "free_space": 250,
                "vm_count": 1,
                "vm_names": ["vm-c"],
            },
        ]
    }


@pytest.mark.asyncio
async def test_datastore_usage_filter_names_passes_through_to_listing() -> None:
    """``filter_names`` flows into the listing sub-op as ``filter.names``."""
    sequence = [_ok_result("GET:/vcenter/datastore", [])]
    dispatch = _RecordingDispatchChild(sequence)
    await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter_names": ["ds-prod-1", "ds-prod-2"]},
        dispatch_child=dispatch,
    )
    assert dispatch.calls[0]["params"] == {"filter.names": ["ds-prod-1", "ds-prod-2"]}


@pytest.mark.asyncio
async def test_datastore_usage_tolerates_legacy_envelope_on_listing() -> None:
    """``{"value": [...]}`` listing envelope is unwrapped."""
    sequence = [
        _ok_result(
            "GET:/vcenter/datastore",
            {
                "value": [
                    {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"},
                ]
            },
        ),
        _ok_result(
            "GET:/vcenter/datastore/{datastore}",
            {"value": {"capacity": 10, "free_space": 5}},
        ),
        _ok_result("GET:/vcenter/vm", {"value": [{"name": "vm-x"}]}),
    ]
    dispatch = _RecordingDispatchChild(sequence)
    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        dispatch_child=dispatch,
    )
    assert out["datastores"][0]["capacity"] == 10
    assert out["datastores"][0]["vm_names"] == ["vm-x"]


@pytest.mark.asyncio
async def test_datastore_usage_skips_malformed_listing_entries() -> None:
    """Listing entries without a string ``datastore`` key are skipped silently."""
    sequence = [
        _ok_result(
            "GET:/vcenter/datastore",
            [
                {"datastore": "datastore-1", "name": "good"},
                {"name": "missing-id"},  # no ``datastore`` key
                "not-a-dict",
            ],
        ),
        _ok_result("GET:/vcenter/datastore/{datastore}", {"capacity": 1}),
        _ok_result("GET:/vcenter/vm", []),
    ]
    dispatch = _RecordingDispatchChild(sequence)
    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        dispatch_child=dispatch,
    )
    # Only the well-formed entry produces sub-ops + a result row.
    assert len(out["datastores"]) == 1
    assert out["datastores"][0]["id"] == "datastore-1"


# ---------------------------------------------------------------------------
# vmware.composite.network.portgroup.audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_portgroup_audit_dispatches_three_phases() -> None:
    """DVS + portgroup listings + per-portgroup VM listings aggregate to the expected shape."""
    dvs_listing = [{"vds": "dvs-1", "name": "DVS-A"}]
    pg_listing = [
        {"network": "pg-1", "name": "PG-A", "vds": "dvs-1", "type": "DISTRIBUTED"},
        {"network": "pg-2", "name": "PG-B", "vds": "dvs-1", "type": "DISTRIBUTED"},
    ]
    vms_per_pg = {
        "pg-1": [{"name": "vm-pg1-a"}, {"name": "vm-pg1-b"}],
        "pg-2": [],
    }
    sequence: list[OperationResult] = [
        _ok_result("GET:/vcenter/network/distributed-switch", dvs_listing),
        _ok_result("GET:/vcenter/network/distributed-portgroup", pg_listing),
    ]
    for pg in pg_listing:
        sequence.append(_ok_result("GET:/vcenter/vm", vms_per_pg[pg["network"]]))
    dispatch = _RecordingDispatchChild(sequence)

    out = await network_portgroup_audit_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        dispatch_child=dispatch,
    )

    # 1 + 1 + 2 portgroups = 4 calls.
    assert len(dispatch.calls) == 4
    assert dispatch.calls[0]["op_id"] == "GET:/vcenter/network/distributed-switch"
    assert dispatch.calls[1]["op_id"] == "GET:/vcenter/network/distributed-portgroup"
    # Per-PG VM call uses ``filter.networks`` and the default power-state filter.
    assert dispatch.calls[2]["op_id"] == "GET:/vcenter/vm"
    assert dispatch.calls[2]["params"] == {
        "filter.networks": ["pg-1"],
        "filter.power_states": ["POWERED_ON"],
    }
    assert out["portgroups"] == [
        {
            "id": "pg-1",
            "name": "PG-A",
            "dvs": "dvs-1",
            "dvs_name": "DVS-A",
            "type": "DISTRIBUTED",
            "vm_count": 2,
            "vm_names": ["vm-pg1-a", "vm-pg1-b"],
        },
        {
            "id": "pg-2",
            "name": "PG-B",
            "dvs": "dvs-1",
            "dvs_name": "DVS-A",
            "type": "DISTRIBUTED",
            "vm_count": 0,
            "vm_names": [],
        },
    ]


@pytest.mark.asyncio
async def test_network_portgroup_audit_filter_dvs_passes_through() -> None:
    """``filter_dvs`` flows into both DVS + PG listings as ``filter.vdses``."""
    sequence = [
        _ok_result("GET:/vcenter/network/distributed-switch", []),
        _ok_result("GET:/vcenter/network/distributed-portgroup", []),
    ]
    dispatch = _RecordingDispatchChild(sequence)
    await network_portgroup_audit_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter_dvs": "dvs-prod"},
        dispatch_child=dispatch,
    )
    assert dispatch.calls[0]["params"] == {"filter.vdses": ["dvs-prod"]}
    assert dispatch.calls[1]["params"] == {"filter.vdses": ["dvs-prod"]}


@pytest.mark.asyncio
async def test_network_portgroup_audit_include_disconnected_drops_power_filter() -> None:
    """``include_disconnected_vms=True`` removes the ``POWERED_ON`` filter on the VM call."""
    sequence = [
        _ok_result("GET:/vcenter/network/distributed-switch", []),
        _ok_result(
            "GET:/vcenter/network/distributed-portgroup",
            [{"network": "pg-1", "name": "PG-A"}],
        ),
        _ok_result("GET:/vcenter/vm", []),
    ]
    dispatch = _RecordingDispatchChild(sequence)
    await network_portgroup_audit_composite(
        operator=_make_operator(),
        target=object(),
        params={"include_disconnected_vms": True},
        dispatch_child=dispatch,
    )
    # VM call's params include ``filter.networks`` but NOT
    # ``filter.power_states``.
    vm_call = dispatch.calls[2]
    assert "filter.networks" in vm_call["params"]
    assert "filter.power_states" not in vm_call["params"]


# ---------------------------------------------------------------------------
# Error fan-out -- a sub-op error causes the handler to raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cluster_drs_recommendations_raises_on_sub_op_error() -> None:
    """A sub-op ``status="error"`` causes the composite to raise ``RuntimeError``.

    Load-bearing for the dispatcher's outer exception branch: the
    composite parent sees the failure as a structured
    ``connector_error`` result with the underlying op_id and message
    in ``extras["exception_class"]``.
    """
    dispatch = _RecordingDispatchChild(
        {
            "GET:/vcenter/cluster/{cluster}": _err_result(
                "GET:/vcenter/cluster/{cluster}", "cluster not found"
            ),
        }
    )
    with pytest.raises(RuntimeError, match="returned status='error'"):
        await cluster_drs_recommendations_composite(
            operator=_make_operator(),
            target=object(),
            params={"cluster": "bogus"},
            dispatch_child=dispatch,
        )


@pytest.mark.asyncio
async def test_datastore_usage_raises_on_listing_error() -> None:
    """A failed listing surfaces as ``RuntimeError``; no per-DS calls fire."""
    dispatch = _RecordingDispatchChild([_err_result("GET:/vcenter/datastore", "permission denied")])
    with pytest.raises(RuntimeError, match="returned status='error'"):
        await datastore_usage_composite(
            operator=_make_operator(),
            target=object(),
            params={},
            dispatch_child=dispatch,
        )
    assert len(dispatch.calls) == 1


@pytest.mark.asyncio
async def test_event_tail_raises_on_sub_op_error() -> None:
    """QueryEvents error surfaces as ``RuntimeError`` (no aggregation)."""
    dispatch = _RecordingDispatchChild(
        [_err_result("POST:/EventManager/{moId}/QueryEvents", "vi-json transient")]
    )
    with pytest.raises(RuntimeError, match="returned status='error'"):
        await event_tail_composite(
            operator=_make_operator(),
            target=object(),
            params={},
            dispatch_child=dispatch,
        )


# ---------------------------------------------------------------------------
# Connector-id contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_composite_uses_vmware_rest_9_0_connector_id() -> None:
    """All five handlers dispatch sub-ops against ``vmware-rest-9.0`` exclusively.

    Load-bearing for the issue body's *Why dispatch_child not direct
    httpx* contract: the connector_id is what routes the recursive
    dispatch back to :class:`VmwareRestConnector` for sub-call
    authentication.
    """
    handlers: tuple[tuple[Any, dict[str, Any], dict[str, Any]], ...] = (
        (
            cluster_drs_recommendations_composite,
            {"cluster": "c1"},
            {
                "GET:/vcenter/cluster/{cluster}": {},
                "GET:/vcenter/cluster/{cluster}/drs": {},
            },
        ),
        (
            event_tail_composite,
            {},
            {"POST:/EventManager/{moId}/QueryEvents": []},
        ),
        (
            performance_summary_composite,
            {"entity_moid": "vm-1"},
            {
                "POST:/PerformanceManager/{moId}/QueryAvailablePerfMetric": [],
                "POST:/PerformanceManager/{moId}/QueryPerf": [],
            },
        ),
        (
            datastore_usage_composite,
            {},
            {"GET:/vcenter/datastore": []},
        ),
        (
            network_portgroup_audit_composite,
            {},
            {
                "GET:/vcenter/network/distributed-switch": [],
                "GET:/vcenter/network/distributed-portgroup": [],
            },
        ),
    )
    for handler, params, responses in handlers:
        dispatch = _RecordingDispatchChild(dict(responses))
        await handler(
            operator=_make_operator(),
            target=object(),
            params=params,
            dispatch_child=dispatch,
        )
        for call in dispatch.calls:
            assert call["connector_id"] == "vmware-rest-9.0", (
                f"{handler.__qualname__} dispatched to {call['connector_id']}"
            )
