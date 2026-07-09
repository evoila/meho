# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the 7 vmware-rest read-composite handler functions.

Post-#2253 the read composites dispatch their sub-ops **directly on the
connector session** -- ``connector._get_json`` / ``connector._post_json``
mounted through ``connector.mount_op_path`` -- rather than through the
catalog-routed ``dispatch_child`` seam. These tests therefore stub the
connector session with a recording double and assert the call-shape
contract: which HTTP method, against which mounted path, with what query
/ body, in what order -- plus the aggregation the handler builds from the
canned responses.

Coverage matrix (carried over from G3.1-T5 / #508, mechanism swapped):

* Per-composite: assert the correct sub-op paths fire in the expected
  order, mounted onto the target's live prefix, with the right query /
  body shape.
* Aggregation correctness: the handler's returned dict matches the
  spec sketched in #508's issue body -- byte-for-byte unchanged by the
  dispatch-mechanism swap.
* Envelope tolerance: ``{"value": [...]}`` (legacy) and bare lists
  (modern) both parse correctly.
* Filter pass-through: ``filter_names`` / ``filter_dvs`` / ``moId`` /
  ``entity_moid`` flow into the sub-op query / path / body.
* Error fan-out: a load-bearing sub-op raising ``httpx.HTTPStatusError``
  propagates out of the handler (the dispatcher's outer branch wraps it
  as ``connector_error`` for the composite parent); best-effort legs
  catch it and degrade.
* Mount routing: every sub-op is mounted via ``mount_op_path`` so a
  legacy/vcsim target (``/rest``) is reached correctly.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest.composites._read import (
    cluster_drs_recommendations_composite,
    datastore_usage_composite,
    event_tail_composite,
    host_network_uplinks_composite,
    host_vsan_health_composite,
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


class _RecordingConnector:
    """Stub connector session that records sub-calls and serves canned JSON.

    Stands in for :class:`VmwareRestConnector` on the direct-dispatch path:
    the handlers call ``mount_op_path`` to resolve the live mount and then
    ``_get_json`` / ``_post_json`` on the returned path. This double records
    every call as ``{"method", "path", "query", "body"}`` and serves a
    response keyed either by the resolved (mounted) path or, in list form,
    sequentially -- the list form lets a test drive several calls to the
    same path (e.g. per-portgroup ``GET /api/vcenter/vm``) with distinct
    payloads. A canned value that is an :class:`Exception` is raised, which
    is how the transport-failure paths are exercised.

    ``mount_prefix`` selects the mount the fake ``mount_op_path`` applies
    (``/api`` modern by default, ``/rest`` for the legacy/vcsim coverage),
    so the tests assert the real mounted wire path.
    """

    def __init__(
        self,
        responses: dict[str, Any] | list[Any],
        *,
        mount_prefix: str = "/api",
    ) -> None:
        self._responses = responses
        self._seq_index = 0
        self._mount_prefix = mount_prefix
        self.calls: list[dict[str, Any]] = []
        self.mount_calls: list[str] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        self.mount_calls.append(path)
        return f"{self._mount_prefix}{path}"

    async def _get_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"method": "GET", "path": path, "query": params, "body": None})
        return self._serve(path)

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append({"method": "POST", "path": path, "query": None, "body": json})
        return self._serve(path)

    def _serve(self, path: str) -> Any:
        if isinstance(self._responses, dict):
            payload = self._responses[path]
        else:
            payload = self._responses[self._seq_index]
            self._seq_index += 1
        if isinstance(payload, Exception):
            raise payload
        return payload


def _http_error(status: int, url: str) -> httpx.HTTPStatusError:
    """Build an ``httpx.HTTPStatusError`` whose ``str`` carries status + URL.

    Mirrors the shape ``raise_for_status`` raises when the connector
    session hits a 4xx/5xx -- the status line + offending URL the
    composite's best-effort note / load-bearing propagation surface.
    """
    reason = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden"}.get(status, "Error")
    request = httpx.Request("GET", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status} {reason}' for url '{url}'",
        request=request,
        response=response,
    )


# ---------------------------------------------------------------------------
# vmware.composite.cluster.drs_recommendations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cluster_drs_recommendations_reads_summary_and_drs_in_order() -> None:
    """Two GETs fire: cluster summary then DRS config, both mounted, cluster in path."""
    cluster_payload = {"name": "Cluster-A", "drs_enabled": True}
    drs_payload = {"enabled": True, "automation_level": "FULLY_AUTOMATED"}
    conn = _RecordingConnector(
        {
            "/api/vcenter/cluster/domain-c123": cluster_payload,
            "/api/vcenter/cluster/domain-c123/drs": drs_payload,
        }
    )

    out = await cluster_drs_recommendations_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c123"},
        connector=conn,  # type: ignore[arg-type]
    )

    assert [(c["method"], c["path"]) for c in conn.calls] == [
        ("GET", "/api/vcenter/cluster/domain-c123"),
        ("GET", "/api/vcenter/cluster/domain-c123/drs"),
    ]
    # Both paths were mounted (spec-relative -> live prefix).
    assert conn.mount_calls == [
        "/vcenter/cluster/domain-c123",
        "/vcenter/cluster/domain-c123/drs",
    ]
    assert out == {"cluster": cluster_payload, "drs": drs_payload}


@pytest.mark.asyncio
async def test_cluster_drs_recommendations_history_flag_surfaces_history_slice() -> None:
    """``include_recommendations_history=True`` surfaces ``history`` from the DRS payload."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/cluster/domain-c1": {"name": "Cluster-A"},
            "/api/vcenter/cluster/domain-c1/drs": {
                "enabled": True,
                "history": [{"recommendation_id": "rec-1", "reason": "load balance"}],
            },
        }
    )
    out = await cluster_drs_recommendations_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c1", "include_recommendations_history": True},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["recommendations_history"] == [
        {"recommendation_id": "rec-1", "reason": "load balance"}
    ]


@pytest.mark.asyncio
async def test_cluster_drs_recommendations_history_flag_default_omits_key() -> None:
    """Default ``include_recommendations_history=False`` omits the key."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/cluster/domain-c1": {"name": "Cluster-A"},
            "/api/vcenter/cluster/domain-c1/drs": {"enabled": False, "history": [1, 2]},
        }
    )
    out = await cluster_drs_recommendations_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert "recommendations_history" not in out


@pytest.mark.asyncio
async def test_cluster_drs_recommendations_propagates_load_bearing_error() -> None:
    """A load-bearing sub-op failure propagates as ``httpx.HTTPStatusError``.

    Load-bearing for the dispatcher's outer branch: the composite parent
    sees the failure as a structured ``connector_error`` result carrying
    the underlying status + URL from ``str(exc)``.
    """
    conn = _RecordingConnector(
        {
            "/api/vcenter/cluster/bogus": _http_error(404, "https://vc/api/vcenter/cluster/bogus"),
        }
    )
    with pytest.raises(httpx.HTTPStatusError):
        await cluster_drs_recommendations_composite(
            operator=_make_operator(),
            target=object(),
            params={"cluster": "bogus"},
            connector=conn,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# vmware.composite.event.tail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_tail_reads_query_events_with_default_mo_id() -> None:
    """Default ``moId='EventManager'`` rides the path; ``max_events=100`` applied."""
    events_payload = [{"id": f"evt-{n}", "summary": f"event {n}"} for n in range(5)]
    conn = _RecordingConnector({"/api/EventManager/EventManager/QueryEvents": events_payload})
    out = await event_tail_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )
    assert len(conn.calls) == 1
    only = conn.calls[0]
    assert only["method"] == "POST"
    assert only["path"] == "/api/EventManager/EventManager/QueryEvents"
    # QueryEvents carries no method-arg body (parity: the pre-migration
    # dispatch passed only the moId path param).
    assert only["body"] is None
    assert out["events"] == events_payload
    assert out["count"] == 5
    assert out["moId"] == "EventManager"
    assert out["max_events_applied"] == 100


@pytest.mark.asyncio
async def test_event_tail_caps_results_to_max_events() -> None:
    """The handler caps client-side to ``max_events``."""
    events_payload = [{"id": f"evt-{n}"} for n in range(200)]
    conn = _RecordingConnector({"/api/EventManager/EventManager/QueryEvents": events_payload})
    out = await event_tail_composite(
        operator=_make_operator(),
        target=object(),
        params={"max_events": 7},
        connector=conn,  # type: ignore[arg-type]
    )
    assert len(out["events"]) == 7
    assert out["count"] == 7
    assert out["max_events_applied"] == 7


@pytest.mark.asyncio
async def test_event_tail_custom_mo_id_rides_the_path() -> None:
    """A per-call ``moId`` override lands in the mounted path segment."""
    conn = _RecordingConnector({"/api/EventManager/em-2/QueryEvents": []})
    await event_tail_composite(
        operator=_make_operator(),
        target=object(),
        params={"moId": "em-2"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert conn.calls[0]["path"] == "/api/EventManager/em-2/QueryEvents"


@pytest.mark.asyncio
async def test_event_tail_tolerates_legacy_value_envelope() -> None:
    """A ``{"value": [...]}`` envelope on the sub-op response unwraps cleanly."""
    conn = _RecordingConnector(
        {
            "/api/EventManager/EventManager/QueryEvents": {
                "value": [{"id": "evt-1"}, {"id": "evt-2"}]
            }
        }
    )
    out = await event_tail_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["count"] == 2
    assert out["events"] == [{"id": "evt-1"}, {"id": "evt-2"}]


@pytest.mark.asyncio
async def test_event_tail_raises_on_non_list_payload() -> None:
    """Non-list payload from QueryEvents raises ``RuntimeError`` (audit-visible)."""
    conn = _RecordingConnector({"/api/EventManager/EventManager/QueryEvents": {"not": "a list"}})
    with pytest.raises(RuntimeError, match="expected list"):
        await event_tail_composite(
            operator=_make_operator(),
            target=object(),
            params={},
            connector=conn,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_event_tail_propagates_load_bearing_error() -> None:
    """QueryEvents transport failure propagates (no aggregation)."""
    conn = _RecordingConnector(
        [_http_error(400, "https://vc/api/EventManager/EventManager/QueryEvents")]
    )
    with pytest.raises(httpx.HTTPStatusError):
        await event_tail_composite(
            operator=_make_operator(),
            target=object(),
            params={},
            connector=conn,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# vmware.composite.performance.summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_performance_summary_reads_two_sub_ops_in_order() -> None:
    """QueryAvailablePerfMetric first, then QueryPerf -- both per-entity, method-args as body."""
    available_payload = [
        {"counterId": 1, "instance": ""},
        {"counterId": 2, "instance": "vmnic0"},
    ]
    samples_payload = [
        {"counterId": 1, "value": 42},
        {"counterId": 2, "value": 100},
    ]
    conn = _RecordingConnector(
        {
            "/api/PerformanceManager/PerfMgr/QueryAvailablePerfMetric": available_payload,
            "/api/PerformanceManager/PerfMgr/QueryPerf": samples_payload,
        }
    )
    out = await performance_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"entity_moid": "vm-1234"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert [c["path"] for c in conn.calls] == [
        "/api/PerformanceManager/PerfMgr/QueryAvailablePerfMetric",
        "/api/PerformanceManager/PerfMgr/QueryPerf",
    ]
    # The vi-json method arguments become the flat JSON request body; the
    # PerfMgr singleton rides the path.
    assert conn.calls[0]["body"] == {"entity": "vm-1234"}
    assert conn.calls[1]["body"] == {"entity": "vm-1234", "interval_seconds": 20}
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
    conn = _RecordingConnector(
        {
            "/api/PerformanceManager/PerfMgr/QueryAvailablePerfMetric": available,
            "/api/PerformanceManager/PerfMgr/QueryPerf": samples,
        }
    )
    out = await performance_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"entity_moid": "vm-7", "max_samples": 12},
        connector=conn,  # type: ignore[arg-type]
    )
    assert len(out["samples"]) == 12
    assert out["max_samples_applied"] == 12


@pytest.mark.asyncio
async def test_performance_summary_passes_through_interval_and_custom_perf_mgr() -> None:
    """Operator-supplied ``interval_seconds`` + ``perf_manager_moid`` propagate."""
    conn = _RecordingConnector(
        {
            "/api/PerformanceManager/AltPerfMgr/QueryAvailablePerfMetric": [],
            "/api/PerformanceManager/AltPerfMgr/QueryPerf": [],
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
        connector=conn,  # type: ignore[arg-type]
    )
    # AltPerfMgr rides the path; the interval rides the QueryPerf body.
    assert conn.calls[1]["path"] == "/api/PerformanceManager/AltPerfMgr/QueryPerf"
    assert conn.calls[1]["body"] == {"entity": "vm-99", "interval_seconds": 300}


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

    sequence: list[Any] = [listing]
    for entry in listing:
        sequence.append(detail_by_id[entry["datastore"]])
        sequence.append(vms_by_ds[entry["datastore"]])
    conn = _RecordingConnector(sequence)

    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )

    # 1 listing + 2 datastores * 2 sub-ops each = 5 calls total.
    assert len(conn.calls) == 5
    assert conn.calls[0]["method"] == "GET"
    assert conn.calls[0]["path"] == "/api/vcenter/datastore"
    # No filter -> no query string.
    assert conn.calls[0]["query"] is None
    # Per-DS detail call embeds the datastore moid in the mounted path.
    assert conn.calls[1]["path"] == "/api/vcenter/datastore/datastore-1"
    # Per-DS VM call uses ``filter.datastores`` with the moid.
    assert conn.calls[2]["path"] == "/api/vcenter/vm"
    assert conn.calls[2]["query"] == {"filter.datastores": ["datastore-1"]}
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
async def test_datastore_usage_capacity_falls_back_to_list_row_when_detail_omits_it() -> None:
    """Detail without ``capacity`` (live 8.0.3 shape) -> capacity from the list row (#2078)."""
    listing = [
        {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS", "capacity": 1000},
    ]
    sequence = [
        listing,
        # Detail carries free_space but NO ``capacity`` key at all.
        {"free_space": 400, "type": "VMFS"},
        [{"name": "vm-a"}],
    ]
    conn = _RecordingConnector(sequence)
    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )
    row = out["datastores"][0]
    assert row["capacity"] == 1000
    assert row["free_space"] == 400


@pytest.mark.asyncio
async def test_datastore_usage_capacity_null_when_neither_detail_nor_list_carry_it() -> None:
    """Neither detail nor list row carries ``capacity`` -> row ``capacity`` is null (#2078)."""
    listing = [
        {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"},
    ]
    sequence = [listing, {"free_space": 400}, []]
    conn = _RecordingConnector(sequence)
    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )
    row = out["datastores"][0]
    assert row["capacity"] is None
    assert row["free_space"] == 400


@pytest.mark.asyncio
async def test_datastore_usage_detail_capacity_wins_over_list_row() -> None:
    """When the detail payload supplies ``capacity``, the detail value wins (#2078)."""
    listing = [
        {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS", "capacity": 1000},
    ]
    sequence = [listing, {"capacity": 100, "free_space": 40}, []]
    conn = _RecordingConnector(sequence)
    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )
    row = out["datastores"][0]
    assert row["capacity"] == 100
    assert row["free_space"] == 40


@pytest.mark.asyncio
async def test_datastore_usage_filter_names_passes_through_to_listing() -> None:
    """``filter_names`` flows into the listing sub-op as ``filter.names``."""
    conn = _RecordingConnector([[]])
    await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter_names": ["ds-prod-1", "ds-prod-2"]},
        connector=conn,  # type: ignore[arg-type]
    )
    assert conn.calls[0]["query"] == {"filter.names": ["ds-prod-1", "ds-prod-2"]}


@pytest.mark.asyncio
async def test_datastore_usage_tolerates_legacy_envelope_on_listing() -> None:
    """``{"value": [...]}`` listing envelope is unwrapped."""
    sequence = [
        {"value": [{"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"}]},
        {"value": {"capacity": 10, "free_space": 5}},
        {"value": [{"name": "vm-x"}]},
    ]
    conn = _RecordingConnector(sequence)
    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["datastores"][0]["capacity"] == 10
    assert out["datastores"][0]["vm_names"] == ["vm-x"]


@pytest.mark.asyncio
async def test_datastore_usage_skips_malformed_listing_entries() -> None:
    """Listing entries without a string ``datastore`` key are skipped silently."""
    sequence = [
        [
            {"datastore": "datastore-1", "name": "good"},
            {"name": "missing-id"},  # no ``datastore`` key
            "not-a-dict",
        ],
        {"capacity": 1},
        [],
    ]
    conn = _RecordingConnector(sequence)
    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )
    assert len(out["datastores"]) == 1
    assert out["datastores"][0]["id"] == "datastore-1"


@pytest.mark.asyncio
async def test_datastore_usage_vm_enrichment_is_best_effort_on_sub_op_error() -> None:
    """A failed VM-placement sub-op keeps the row; vm_count/vm_names null + note (#1908)."""
    sequence = [
        [
            {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"},
            {"datastore": "datastore-2", "name": "ds-2", "type": "NFS"},
        ],
        # datastore-1: detail OK, VM enrichment 400s -> best-effort skip.
        {"capacity": 100, "free_space": 40},
        _http_error(400, "https://vc/api/vcenter/vm?filter.datastores=datastore-1"),
        # datastore-2: detail OK, VM enrichment OK -> fully enriched.
        {"capacity": 500, "free_space": 250},
        [{"name": "vm-c"}],
    ]
    conn = _RecordingConnector(sequence)
    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )

    # All 5 sub-ops fired -- the failed VM leg did not short-circuit.
    assert len(conn.calls) == 5
    rows = out["datastores"]
    assert len(rows) == 2

    # datastore-1: core data preserved, enrichment skipped.
    ds1 = rows[0]
    assert ds1["id"] == "datastore-1"
    assert ds1["capacity"] == 100
    assert ds1["free_space"] == 40
    assert ds1["type"] == "VMFS"
    assert ds1["vm_count"] is None
    assert ds1["vm_names"] is None
    note = ds1["enrichment_note"]
    # The note names the sub-op, the 400, and the offending URL.
    assert "GET:/vcenter/vm" in note
    assert "400 Bad Request" in note
    assert "filter.datastores=datastore-1" in note

    # datastore-2: enriched normally, no note key.
    ds2 = rows[1]
    assert ds2["id"] == "datastore-2"
    assert ds2["vm_count"] == 1
    assert ds2["vm_names"] == ["vm-c"]
    assert "enrichment_note" not in ds2


@pytest.mark.asyncio
async def test_datastore_usage_listing_error_propagates() -> None:
    """A load-bearing listing failure propagates; no per-DS calls fire."""
    conn = _RecordingConnector(
        [_http_error(400, "https://vc/api/vcenter/datastore?filter.names=bogus")]
    )
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await datastore_usage_composite(
            operator=_make_operator(),
            target=object(),
            params={},
            connector=conn,  # type: ignore[arg-type]
        )
    # No per-datastore calls fire after the listing fails.
    assert len(conn.calls) == 1
    message = str(excinfo.value)
    assert "400 Bad Request" in message
    assert "filter.names=bogus" in message


@pytest.mark.asyncio
async def test_datastore_usage_detail_error_propagates() -> None:
    """A load-bearing per-datastore detail failure propagates (403)."""
    sequence = [
        [{"datastore": "datastore-1", "name": "ds-1"}],
        _http_error(403, "https://vc/api/vcenter/datastore/datastore-1"),
    ]
    conn = _RecordingConnector(sequence)
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await datastore_usage_composite(
            operator=_make_operator(),
            target=object(),
            params={},
            connector=conn,  # type: ignore[arg-type]
        )
    assert "403 Forbidden" in str(excinfo.value)


# ---------------------------------------------------------------------------
# vmware.composite.network.portgroup.audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_portgroup_audit_reads_three_phases() -> None:
    """DVS + portgroup listings + per-portgroup VM listings aggregate to the expected shape."""
    dvs_listing = [{"vds": "dvs-1", "name": "DVS-A"}]
    pg_listing = [
        {"network": "pg-1", "name": "PG-A", "vds": "dvs-1", "type": "DISTRIBUTED_PORTGROUP"},
        {"network": "pg-2", "name": "PG-B", "type": "DISTRIBUTED_PORTGROUP"},
    ]
    vms_per_pg = {
        "pg-1": [{"name": "vm-pg1-a"}, {"name": "vm-pg1-b"}],
        "pg-2": [],
    }
    sequence: list[Any] = [dvs_listing, pg_listing]
    for pg in pg_listing:
        sequence.append(vms_per_pg[pg["network"]])
    conn = _RecordingConnector(sequence)

    out = await network_portgroup_audit_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )

    # 1 + 1 + 2 portgroups = 4 calls.
    assert len(conn.calls) == 4
    assert conn.calls[0]["path"] == "/api/vcenter/network/distributed-switches"
    # Portgroups come from the generic network resource, type-filtered.
    assert conn.calls[1]["path"] == "/api/vcenter/network"
    assert conn.calls[1]["query"] == {"filter.types": ["DISTRIBUTED_PORTGROUP"]}
    # Per-PG VM call uses ``filter.networks`` and the default power-state filter.
    assert conn.calls[2]["path"] == "/api/vcenter/vm"
    assert conn.calls[2]["query"] == {
        "filter.networks": ["pg-1"],
        "filter.power_states": ["POWERED_ON"],
    }
    assert out["portgroups"] == [
        {
            "id": "pg-1",
            "name": "PG-A",
            "dvs": "dvs-1",
            "dvs_name": "DVS-A",
            "type": "DISTRIBUTED_PORTGROUP",
            "vm_count": 2,
            "vm_names": ["vm-pg1-a", "vm-pg1-b"],
        },
        {
            "id": "pg-2",
            "name": "PG-B",
            "dvs": None,
            "dvs_name": None,
            "type": "DISTRIBUTED_PORTGROUP",
            "vm_count": 0,
            "vm_names": [],
        },
    ]


@pytest.mark.asyncio
async def test_network_portgroup_audit_filter_dvs_scopes_dvs_listing_only() -> None:
    """``filter_dvs`` scopes the DVS listing; the portgroup call is type-only."""
    sequence = [[], []]
    conn = _RecordingConnector(sequence)
    await network_portgroup_audit_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter_dvs": "dvs-prod"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert conn.calls[0]["query"] == {"filter.vdses": ["dvs-prod"]}
    # Portgroup call is type-filtered only -- no ``filter.vdses``.
    assert conn.calls[1]["query"] == {"filter.types": ["DISTRIBUTED_PORTGROUP"]}


@pytest.mark.asyncio
async def test_network_portgroup_audit_include_disconnected_drops_power_filter() -> None:
    """``include_disconnected_vms=True`` removes the ``POWERED_ON`` filter on the VM call."""
    sequence = [
        [],
        [{"network": "pg-1", "name": "PG-A", "type": "DISTRIBUTED_PORTGROUP"}],
        [],
    ]
    conn = _RecordingConnector(sequence)
    await network_portgroup_audit_composite(
        operator=_make_operator(),
        target=object(),
        params={"include_disconnected_vms": True},
        connector=conn,  # type: ignore[arg-type]
    )
    vm_call = conn.calls[2]
    assert "filter.networks" in vm_call["query"]
    assert "filter.power_states" not in vm_call["query"]


# ---------------------------------------------------------------------------
# vmware.composite.host.network_uplinks
# ---------------------------------------------------------------------------


def _retrieve_result(pnics: list[Any], proxy_switches: list[Any]) -> dict[str, Any]:
    """Build a RetrievePropertiesEx RetrieveResult for one host."""
    return {
        "objects": [
            {
                "obj": {"type": "HostSystem", "value": "host-1"},
                "propSet": [
                    {"name": "config.network.pnic", "val": pnics},
                    {"name": "config.network.proxySwitch", "val": proxy_switches},
                ],
            }
        ]
    }


@pytest.mark.asyncio
async def test_host_network_uplinks_lists_then_reads_props_per_host() -> None:
    """Host listing + per-host RetrievePropertiesEx; aggregation matches the spec."""
    listing = [
        {"host": "host-1", "name": "esx-1"},
        {"host": "host-2", "name": "esx-2"},
    ]
    props_by_host = {
        "host-1": _retrieve_result(
            pnics=[
                {
                    "device": "vmnic0",
                    "mac": "aa:bb:cc:00:00:00",
                    "driver": "ixgbe",
                    "linkSpeed": {"speedMb": 10000, "duplex": True},
                },
                {
                    "device": "vmnic1",
                    "mac": "aa:bb:cc:00:00:01",
                    "driver": "ixgbe",
                    # No linkSpeed -> link down.
                },
            ],
            proxy_switches=[
                {
                    "key": "key-vim.host.HostProxySwitch-1",
                    "dvsName": "DVS-A",
                    "dvsUuid": "50 01 aa bb",
                    "pnic": ["key-vim.host.PhysicalNic-vmnic0"],
                }
            ],
        ),
        "host-2": _retrieve_result(pnics=[], proxy_switches=[]),
    }
    sequence: list[Any] = [listing]
    for entry in listing:
        sequence.append(props_by_host[entry["host"]])
    conn = _RecordingConnector(sequence)

    out = await host_network_uplinks_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )

    # 1 listing + 2 hosts * 1 property read = 3 calls.
    assert len(conn.calls) == 3
    assert conn.calls[0]["method"] == "GET"
    assert conn.calls[0]["path"] == "/api/vcenter/host"
    # Per-host property read targets the propertyCollector singleton (in
    # the path) and requests the two host-network config paths (in body).
    prop_call = conn.calls[1]
    assert prop_call["method"] == "POST"
    assert prop_call["path"] == "/api/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    spec = prop_call["body"]["specSet"][0]
    assert spec["propSet"][0]["type"] == "HostSystem"
    assert spec["propSet"][0]["pathSet"] == [
        "config.network.pnic",
        "config.network.proxySwitch",
    ]
    assert spec["objectSet"][0]["obj"] == {"type": "HostSystem", "value": "host-1"}

    hosts = out["hosts"]
    assert len(hosts) == 2
    h1 = hosts[0]
    assert h1["id"] == "host-1"
    assert h1["name"] == "esx-1"
    assert h1["pnics"] == [
        {
            "device": "vmnic0",
            "mac": "aa:bb:cc:00:00:00",
            "driver": "ixgbe",
            "link_up": True,
            "speed_mb": 10000,
            "duplex": True,
        },
        {
            "device": "vmnic1",
            "mac": "aa:bb:cc:00:00:01",
            "driver": "ixgbe",
            "link_up": False,
            "speed_mb": None,
            "duplex": None,
        },
    ]
    assert h1["proxy_switches"] == [
        {
            "key": "key-vim.host.HostProxySwitch-1",
            "dvs_name": "DVS-A",
            "dvs_uuid": "50 01 aa bb",
            "uplink_pnics": ["vmnic0"],
        }
    ]
    assert "read_note" not in h1
    assert hosts[1]["pnics"] == []
    assert hosts[1]["proxy_switches"] == []


@pytest.mark.asyncio
async def test_host_network_uplinks_filter_hosts_passes_through_to_listing() -> None:
    """``filter_hosts`` flows into the host listing as ``filter.hosts``."""
    conn = _RecordingConnector([[]])
    await host_network_uplinks_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter_hosts": ["host-9", "host-10"]},
        connector=conn,  # type: ignore[arg-type]
    )
    assert conn.calls[0]["query"] == {"filter.hosts": ["host-9", "host-10"]}


@pytest.mark.asyncio
async def test_host_network_uplinks_property_read_is_best_effort_on_error() -> None:
    """A failed per-host property read keeps the row; pnics/proxy_switches null + note."""
    listing = [
        {"host": "host-1", "name": "esx-1"},
        {"host": "host-2", "name": "esx-2"},
    ]
    sequence = [
        listing,
        # host-1: property read 400s -> best-effort skip.
        _http_error(
            400,
            "https://vc/api/PropertyCollector/propertyCollector/RetrievePropertiesEx",
        ),
        # host-2: property read OK.
        _retrieve_result(pnics=[], proxy_switches=[]),
    ]
    conn = _RecordingConnector(sequence)
    out = await host_network_uplinks_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )

    assert len(conn.calls) == 3
    rows = out["hosts"]
    assert len(rows) == 2
    h1 = rows[0]
    assert h1["id"] == "host-1"
    assert h1["pnics"] is None
    assert h1["proxy_switches"] is None
    note = h1["read_note"]
    assert "POST:/PropertyCollector/{moId}/RetrievePropertiesEx" in note
    assert "400 Bad Request" in note
    # host-2: enriched normally, no note.
    assert rows[1]["pnics"] == []
    assert "read_note" not in rows[1]


@pytest.mark.asyncio
async def test_host_network_uplinks_tolerates_legacy_value_envelope() -> None:
    """A ``{"value": ...}`` envelope on the listing and property read unwraps cleanly."""
    sequence = [
        {"value": [{"host": "host-1", "name": "esx-1"}]},
        {"value": _retrieve_result(pnics=[{"device": "vmnic0"}], proxy_switches=[])},
    ]
    conn = _RecordingConnector(sequence)
    out = await host_network_uplinks_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["hosts"][0]["pnics"] == [
        {
            "device": "vmnic0",
            "mac": None,
            "driver": None,
            "link_up": False,
            "speed_mb": None,
            "duplex": None,
        }
    ]


@pytest.mark.asyncio
async def test_host_network_uplinks_skips_malformed_listing_entries() -> None:
    """Listing entries without a string ``host`` key are skipped silently."""
    sequence = [
        [
            {"host": "host-1", "name": "good"},
            {"name": "missing-id"},  # no ``host`` key
            "not-a-dict",
        ],
        _retrieve_result(pnics=[], proxy_switches=[]),
    ]
    conn = _RecordingConnector(sequence)
    out = await host_network_uplinks_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )
    assert len(out["hosts"]) == 1
    assert out["hosts"][0]["id"] == "host-1"


@pytest.mark.asyncio
async def test_host_network_uplinks_propagates_listing_error() -> None:
    """A failed host listing propagates; no per-host reads fire."""
    conn = _RecordingConnector([_http_error(403, "https://vc/api/vcenter/host")])
    with pytest.raises(httpx.HTTPStatusError):
        await host_network_uplinks_composite(
            operator=_make_operator(),
            target=object(),
            params={},
            connector=conn,  # type: ignore[arg-type]
        )
    assert len(conn.calls) == 1


# ---------------------------------------------------------------------------
# vmware.composite.host.vsan_health
# ---------------------------------------------------------------------------


_VSAN_PATH = (
    "/api/VsanVcClusterHealthSystem/vsan-cluster-health-system/VsanQueryVcClusterHealthSummary"
)
_VSAN_OP = "POST:/VsanVcClusterHealthSystem/{moId}/VsanQueryVcClusterHealthSummary"


def _vsan_summary(overall: str, groups: list[Any]) -> dict[str, Any]:
    """Build a ``VsanClusterHealthSummary`` payload for one cluster."""
    return {"overallHealth": overall, "groups": groups}


@pytest.mark.asyncio
async def test_host_vsan_health_queries_health_summary_for_cluster() -> None:
    """One health-service query fires, scoped to the cluster; groups + tests flatten."""
    summary = _vsan_summary(
        overall="green",
        groups=[
            {
                "groupId": "com.vmware.vsan.health.test.network",
                "groupName": "Network",
                "groupHealth": "green",
                "groupTests": [
                    {
                        "testId": "com.vmware.vsan.health.test.hostconnectivity",
                        "testName": "vSAN cluster partition",
                        "testHealth": "green",
                        "testShortDescription": "Checks host connectivity.",
                    },
                ],
            },
            {
                "groupId": "com.vmware.vsan.health.test.physicaldisks",
                "groupName": "Physical disk",
                "groupHealth": "yellow",
                "groupTests": [],
            },
        ],
    )
    conn = _RecordingConnector({_VSAN_PATH: summary})

    out = await host_vsan_health_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c123"},
        connector=conn,  # type: ignore[arg-type]
    )

    assert len(conn.calls) == 1
    call = conn.calls[0]
    assert call["method"] == "POST"
    # The singleton moId rides the path; the cluster MoRef is the body arg.
    assert call["path"] == _VSAN_PATH
    assert call["body"] == {"cluster": {"type": "ClusterComputeResource", "value": "domain-c123"}}

    assert out["cluster"] == "domain-c123"
    assert out["overall_health"] == "green"
    assert out["groups"] == [
        {
            "group_id": "com.vmware.vsan.health.test.network",
            "group_name": "Network",
            "group_health": "green",
            "tests": [
                {
                    "test_id": "com.vmware.vsan.health.test.hostconnectivity",
                    "test_name": "vSAN cluster partition",
                    "test_health": "green",
                    "test_short_description": "Checks host connectivity.",
                },
            ],
        },
        {
            "group_id": "com.vmware.vsan.health.test.physicaldisks",
            "group_name": "Physical disk",
            "group_health": "yellow",
            "tests": [],
        },
    ]
    assert "read_note" not in out


@pytest.mark.asyncio
async def test_host_vsan_health_read_is_best_effort_on_error() -> None:
    """A failed health-service read nulls groups/overall + records a ``read_note``."""
    conn = _RecordingConnector(
        [_http_error(400, "https://vc" + _VSAN_PATH.replace("/api", "/vsanHealth"))]
    )
    out = await host_vsan_health_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c404"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["cluster"] == "domain-c404"
    assert out["overall_health"] is None
    assert out["groups"] is None
    note = out["read_note"]
    assert _VSAN_OP in note
    assert "400 Bad Request" in note


@pytest.mark.asyncio
async def test_host_vsan_health_tolerates_legacy_value_envelope() -> None:
    """A ``{"value": ...}`` envelope on the summary unwraps cleanly."""
    conn = _RecordingConnector({_VSAN_PATH: {"value": _vsan_summary(overall="red", groups=[])}})
    out = await host_vsan_health_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["overall_health"] == "red"
    assert out["groups"] == []


@pytest.mark.asyncio
async def test_host_vsan_health_tolerates_missing_groups_key() -> None:
    """A summary without a ``groups`` list degrades to an empty group list."""
    conn = _RecordingConnector({_VSAN_PATH: {"overallHealth": "green"}})
    out = await host_vsan_health_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["overall_health"] == "green"
    assert out["groups"] == []


# ---------------------------------------------------------------------------
# Mount routing (modern + legacy) + zero-catalog-ingest contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_sub_ops_route_through_legacy_rest_mount() -> None:
    """Every sub-op is mounted, so a legacy/vcsim target routes onto ``/rest``.

    A target whose session established on the legacy path serves ops only
    under ``/rest`` (not ``/api``); the composite must mount each sub-op
    through ``mount_op_path`` or it 404s. This is the modern+legacy mount
    coverage the acceptance criteria require, at the unit level.
    """
    listing = [{"datastore": "datastore-1", "name": "ds-1"}]
    conn = _RecordingConnector(
        [listing, {"capacity": 1, "free_space": 1}, []],
        mount_prefix="/rest",
    )
    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )
    # Every recorded call lands under the legacy /rest mount.
    assert all(c["path"].startswith("/rest/") for c in conn.calls)
    assert conn.calls[0]["path"] == "/rest/vcenter/datastore"
    assert out["datastores"][0]["id"] == "datastore-1"


@pytest.mark.asyncio
async def test_every_read_composite_dispatches_directly_on_the_session() -> None:
    """All 7 read handlers reach their sub-ops via the connector session only.

    Load-bearing for the #2253 contract: no ``dispatch_child``, no
    ingested descriptor, no L2 pre-flight -- so the composites work on a
    fresh boot with zero catalog ingest. Each handler is exercised with a
    minimal happy-path stub and must (a) record at least one session call
    and (b) mount every path it hit.
    """
    handlers: tuple[tuple[Any, dict[str, Any], list[Any]], ...] = (
        (
            cluster_drs_recommendations_composite,
            {"cluster": "c1"},
            [{}, {}],
        ),
        (event_tail_composite, {}, [[]]),
        (performance_summary_composite, {"entity_moid": "vm-1"}, [[], []]),
        (datastore_usage_composite, {}, [[]]),
        (network_portgroup_audit_composite, {}, [[], []]),
        (host_network_uplinks_composite, {}, [[]]),
        (
            host_vsan_health_composite,
            {"cluster": "domain-c1"},
            [{"overallHealth": "green", "groups": []}],
        ),
    )
    for handler, params, responses in handlers:
        conn = _RecordingConnector(list(responses))
        await handler(
            operator=_make_operator(),
            target=object(),
            params=params,
            connector=conn,  # type: ignore[arg-type]
        )
        assert conn.calls, f"{handler.__qualname__} issued no session sub-calls"
        # Every session call went through a mount resolution.
        assert len(conn.mount_calls) == len(conn.calls), (
            f"{handler.__qualname__} bypassed mount_op_path on a sub-op"
        )
