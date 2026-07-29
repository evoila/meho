# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the 5 vmware-rest read-composite handler functions.

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
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
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


class _RecordingConnector:
    """Stub connector session that records sub-calls and serves canned JSON.

    Stands in for :class:`VmwareRestConnector` on the direct-dispatch path.
    GET (Automation ``/vcenter/*``) sub-ops call ``mount_op_path`` to
    resolve the live mount and then ``_get_json`` on the returned path.
    POST (vmomi VI-JSON) sub-ops call ``_post_vmomi_json`` with the
    spec-relative path -- the connector's VI-JSON ``/sdk/vim25`` mount +
    ``/api`` fallback is what that seam owns (#2466); the fake re-applies
    ``mount_prefix`` for the recorded wire path so response keys stay in
    the ``/api/...`` / ``/rest/...`` form. This double records every call
    as ``{"method", "path", "query", "body", "vmomi"}`` and serves a
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

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        # Exercise the real mount-flavor adaptation against this fake's
        # mount prefix so the recorded query matches the wire form.
        return adapt_filter_params(self._mount_prefix, query)

    async def _get_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append(
            {"method": "GET", "path": path, "query": params, "body": None, "vmomi": False}
        )
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
        self.calls.append(
            {"method": "POST", "path": path, "query": None, "body": json, "vmomi": False}
        )
        return self._serve(path)

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> Any:
        # #2466: vmomi POST sub-ops route through the connector's VI-JSON
        # mount seam, NOT mount_op_path. The handler passes the
        # spec-relative path; the fake re-applies mount_prefix so the
        # recorded wire path + response keys keep the pre-#2466 form.
        mounted = f"{self._mount_prefix}{path}"
        self.calls.append(
            {"method": "POST", "path": mounted, "query": None, "body": json, "vmomi": True}
        )
        return self._serve(mounted)

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
    # Per-DS VM call filters by datastore; on the modern /api mount the
    # param is sent bare (#2298), not ``filter.datastores``.
    assert conn.calls[2]["path"] == "/api/vcenter/vm"
    assert conn.calls[2]["query"] == {"datastores": ["datastore-1"]}
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
    """``filter_names`` flows into the listing sub-op; bare ``names`` on /api (#2298)."""
    conn = _RecordingConnector([[]])
    await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter_names": ["ds-prod-1", "ds-prod-2"]},
        connector=conn,  # type: ignore[arg-type]
    )
    assert conn.calls[0]["query"] == {"names": ["ds-prod-1", "ds-prod-2"]}


@pytest.mark.asyncio
async def test_datastore_usage_filter_params_keep_prefix_on_legacy_mount() -> None:
    """On the legacy/vcsim ``/rest`` mount, filter params keep the ``filter.`` prefix (#2298)."""
    listing = [{"datastore": "datastore-1", "name": "ds-1", "type": "VMFS", "capacity": 10}]
    sequence = [listing, {"capacity": 10, "free_space": 4}, [{"name": "vm-a"}]]
    conn = _RecordingConnector(sequence, mount_prefix="/rest")
    await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter_names": ["ds-prod-1"]},
        connector=conn,  # type: ignore[arg-type]
    )
    # Listing keeps ``filter.names``; per-DS VM leg keeps ``filter.datastores``.
    assert conn.calls[0]["path"] == "/rest/vcenter/datastore"
    assert conn.calls[0]["query"] == {"filter.names": ["ds-prod-1"]}
    assert conn.calls[2]["query"] == {"filter.datastores": ["datastore-1"]}


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
    # On the modern /api mount the filter params are sent bare (#2298).
    assert conn.calls[1]["path"] == "/api/vcenter/network"
    assert conn.calls[1]["query"] == {"types": ["DISTRIBUTED_PORTGROUP"]}
    # Per-PG VM call filters by network + power state, bare on /api.
    assert conn.calls[2]["path"] == "/api/vcenter/vm"
    assert conn.calls[2]["query"] == {
        "networks": ["pg-1"],
        "power_states": ["POWERED_ON"],
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
    assert conn.calls[0]["query"] == {"vdses": ["dvs-prod"]}
    # Portgroup call is type-filtered only -- no vdses filter.
    assert conn.calls[1]["query"] == {"types": ["DISTRIBUTED_PORTGROUP"]}


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
    assert "networks" in vm_call["query"]
    assert "power_states" not in vm_call["query"]


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
async def test_portgroup_audit_keeps_filter_prefix_on_legacy_mount() -> None:
    """Legacy/vcsim ``/rest`` portgroup audit keeps the ``filter.*`` param style (#2298).

    Regression pin for the mount-flavor split: the same request that sends
    bare params on modern ``/api`` must still send ``filter.types`` /
    ``filter.networks`` / ``filter.power_states`` on the legacy mount vcsim
    serves, or the existing vcsim fixtures break.
    """
    dvs_listing = [{"vds": "dvs-1", "name": "DVS-A"}]
    pg_listing = [{"network": "pg-1", "name": "PG-A", "type": "DISTRIBUTED_PORTGROUP"}]
    conn = _RecordingConnector(
        [dvs_listing, pg_listing, [{"name": "vm-a"}]],
        mount_prefix="/rest",
    )
    await network_portgroup_audit_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter_dvs": "dvs-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert conn.calls[0]["query"] == {"filter.vdses": ["dvs-1"]}
    assert conn.calls[1]["query"] == {"filter.types": ["DISTRIBUTED_PORTGROUP"]}
    assert conn.calls[2]["query"] == {
        "filter.networks": ["pg-1"],
        "filter.power_states": ["POWERED_ON"],
    }


@pytest.mark.asyncio
async def test_every_read_composite_dispatches_directly_on_the_session() -> None:
    """All 5 read handlers reach their sub-ops via the connector session only.

    Load-bearing for the #2253 contract: no ``dispatch_child``, no
    ingested descriptor, no L2 pre-flight -- so the composites work on a
    fresh boot with zero catalog ingest. Each handler is exercised with a
    minimal happy-path stub and must (a) record at least one session call
    and (b) route each sub-op through the right seam -- GET Automation
    sub-ops via ``mount_op_path``, POST vmomi sub-ops via the VI-JSON seam
    (:meth:`_post_vmomi_json`), so no vmomi path reaches the bare
    Automation mount (#2466).
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
        get_calls = [c for c in conn.calls if c["method"] == "GET"]
        # GET (Automation /vcenter) sub-ops resolve through mount_op_path.
        assert len(conn.mount_calls) == len(get_calls), (
            f"{handler.__qualname__} GET sub-op bypassed mount_op_path"
        )
        # POST (vmomi) sub-ops resolve through the VI-JSON seam, never the
        # bare Automation mount (#2466).
        assert all(c["vmomi"] for c in conn.calls if c["method"] == "POST"), (
            f"{handler.__qualname__} vmomi sub-op bypassed _post_vmomi_json"
        )
