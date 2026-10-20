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
* Filter pass-through: ``filter_names`` / ``moId`` /
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
from meho_backplane.connectors.vmware_rest.vim_body import vim_moref
from tests._vmware_cluster_config_fake import cluster_config_ex, reject_config_ex_member_paths

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
        reject_config_ex_member_paths(json)
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


_RETRIEVE_PROPERTIES_PATH = "/api/PropertyCollector/propertyCollector/RetrievePropertiesEx"


def _retrieve_result(prop_sets: dict[str, Any]) -> dict[str, Any]:
    """Build a single-object ``RetrievePropertiesEx`` result payload."""
    return {
        "objects": [
            {
                "obj": {"type": "ClusterComputeResource", "value": "domain-c123"},
                "propSet": [{"name": name, "val": val} for name, val in prop_sets.items()],
            }
        ]
    }


def _retrieve_result_multi(
    objects: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Build a multi-object ``RetrievePropertiesEx`` result payload.

    Keyed by ``(mo_type, moid)`` so a single read can carry per-object
    property sets -- the shape the datastore-usage placement read
    (``VirtualMachine.datastore`` across every VM) consumes.
    """
    return {
        "objects": [
            {
                "obj": {"type": mo_type, "value": moid},
                "propSet": [{"name": name, "val": val} for name, val in props.items()],
            }
            for (mo_type, moid), props in objects.items()
        ]
    }


def _vm_placement(datastore_moids: list[str]) -> dict[str, Any]:
    """A boxed ``VirtualMachine.datastore`` prop value: an array of Datastore MoRefs.

    Mirrors the live VI-JSON 8.0.3 boxing (#3106): array-valued props
    arrive as ``{"_typeName": "ArrayOf...", "_value": [...]}``.
    """
    return {
        "_typeName": "ArrayOfManagedObjectReference",
        "_value": [vim_moref("Datastore", moid) for moid in datastore_moids],
    }


@pytest.mark.asyncio
async def test_cluster_drs_recommendations_reads_summary_then_vim_drs_config() -> None:
    """Cluster summary GET then a vmomi RetrievePropertiesEx read of drsConfig.

    The pinned vcenter.yaml serves no cluster DRS REST resource (#2986),
    so the second leg is a vi-json property read on the PropertyCollector
    singleton -- routed through ``_post_vmomi_json``, never the Automation
    mount.
    """
    cluster_payload = {"name": "Cluster-A", "drs_enabled": True}
    drs_config = {
        "_typeName": "ClusterDrsConfigInfo",
        "enabled": True,
        "defaultVmBehavior": "fullyAutomated",
        "vmotionRate": 3,
    }
    conn = _RecordingConnector(
        {
            "/api/vcenter/cluster/domain-c123": cluster_payload,
            _RETRIEVE_PROPERTIES_PATH: _retrieve_result(
                {"configurationEx": cluster_config_ex(drs_config=drs_config)}
            ),
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
        ("POST", _RETRIEVE_PROPERTIES_PATH),
    ]
    # Only the Automation GET resolves through mount_op_path; the vmomi
    # leg rides the VI-JSON seam (#2466).
    assert conn.mount_calls == ["/vcenter/cluster/domain-c123"]
    assert conn.calls[1]["vmomi"] is True
    # The filter spec scopes one PropertyFilterSpec to the cluster MoRef
    # and reads the whole configurationEx (vCenter 8.0.x rejects the path
    # configurationEx.drsConfig); no history requested.
    body = conn.calls[1]["body"]
    assert body == {
        "specSet": [
            {
                "_typeName": "PropertyFilterSpec",
                "propSet": [
                    {
                        "_typeName": "PropertySpec",
                        "type": "ClusterComputeResource",
                        "pathSet": ["configurationEx"],
                    }
                ],
                "objectSet": [
                    {
                        "_typeName": "ObjectSpec",
                        "obj": {
                            "_typeName": "ManagedObjectReference",
                            "type": "ClusterComputeResource",
                            "value": "domain-c123",
                        },
                    }
                ],
            }
        ],
        "options": {"_typeName": "RetrieveOptions"},
    }
    assert out == {"cluster": cluster_payload, "drs": drs_config}


@pytest.mark.asyncio
async def test_cluster_drs_recommendations_history_flag_widens_read_and_surfaces_recs() -> None:
    """``include_recommendations_history=True`` reads ``drsRecommendation`` too.

    The array-valued ``drsRecommendation`` arrives **boxed** on live
    VI-JSON (#3106); the composite surfaces the un-boxed rows.
    """
    recs = [{"key": "1", "rating": 5, "migrationList": []}]
    conn = _RecordingConnector(
        {
            "/api/vcenter/cluster/domain-c123": {"name": "Cluster-A"},
            _RETRIEVE_PROPERTIES_PATH: _retrieve_result(
                {
                    "configurationEx": cluster_config_ex(drs_config={"enabled": True}),
                    "drsRecommendation": {
                        "_typeName": "ArrayOfClusterDrsRecommendation",
                        "_value": recs,
                    },
                }
            ),
        }
    )
    out = await cluster_drs_recommendations_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c123", "include_recommendations_history": True},
        connector=conn,  # type: ignore[arg-type]
    )
    # The flag widens the same RetrievePropertiesEx read -- no extra call.
    assert len(conn.calls) == 2
    path_set = conn.calls[1]["body"]["specSet"][0]["propSet"][0]["pathSet"]
    assert path_set == ["configurationEx", "drsRecommendation"]
    assert out["drs"] == {"enabled": True}
    assert out["recommendations_history"] == recs


@pytest.mark.asyncio
async def test_cluster_drs_recommendations_history_flag_default_omits_key() -> None:
    """Default ``include_recommendations_history=False`` omits the key and the prop."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/cluster/domain-c123": {"name": "Cluster-A"},
            _RETRIEVE_PROPERTIES_PATH: _retrieve_result(
                {"configurationEx": cluster_config_ex(drs_config={"enabled": False})}
            ),
        }
    )
    out = await cluster_drs_recommendations_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c123"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert "recommendations_history" not in out
    path_set = conn.calls[1]["body"]["specSet"][0]["propSet"][0]["pathSet"]
    assert "drsRecommendation" not in path_set


@pytest.mark.asyncio
async def test_cluster_drs_recommendations_tolerates_missing_props_and_envelope() -> None:
    """Unset vim properties degrade to the documented defaults.

    vim omits unset properties from ``propSet`` (they land in
    ``missingSet``), and a legacy target may wrap the RetrieveResult in
    the pre-7 ``{"value": ...}`` envelope: ``drs`` degrades to ``{}``
    and ``recommendations_history`` to ``[]``.
    """
    conn = _RecordingConnector(
        {
            "/api/vcenter/cluster/domain-c123": {"name": "Cluster-A"},
            _RETRIEVE_PROPERTIES_PATH: {"value": _retrieve_result({})},
        }
    )
    out = await cluster_drs_recommendations_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "domain-c123", "include_recommendations_history": True},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["drs"] == {}
    assert out["recommendations_history"] == []


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


_QUERY_EVENTS_PATH = "/api/EventManager/EventManager/QueryEvents"


async def _run_event_tail(conn: _RecordingConnector, params: dict[str, Any]) -> dict[str, Any]:
    return await event_tail_composite(
        operator=_make_operator(),
        target=object(),
        params=params,
        connector=conn,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_event_tail_unfiltered_sends_a_tagged_filter_spec_body() -> None:
    """No params still sends a non-empty ``EventFilterSpec`` (8.0.x 500s on an empty body)."""
    conn = _RecordingConnector({_QUERY_EVENTS_PATH: []})
    out = await _run_event_tail(conn, {})
    assert len(conn.calls) == 1
    only = conn.calls[0]
    assert only["method"] == "POST"
    assert only["vmomi"] is True
    assert only["path"] == _QUERY_EVENTS_PATH
    assert only["body"] == {
        "filter": {
            "_typeName": "EventFilterSpec",
            "disableFullMessage": False,
            "maxCount": 100,
        }
    }
    assert out == {"events": [], "count": 0, "moId": "EventManager", "max_count_applied": 100}


@pytest.mark.asyncio
async def test_event_tail_maps_every_filter_param_onto_the_filter_spec() -> None:
    """``begin_time`` / ``end_time`` / ``event_type_ids`` / ``entity`` / ``max_count`` land."""
    conn = _RecordingConnector({_QUERY_EVENTS_PATH: []})
    await _run_event_tail(
        conn,
        {
            "begin_time": "2026-10-19T08:00:00Z",
            "end_time": "2026-10-19T09:30:00+02:00",
            "event_type_ids": ["esx.problem.storage.apd.start", "NASDatastoreEvent"],
            "entity": {"moId": "datastore-12", "type": "Datastore", "recursion": "self"},
            "max_count": 250,
        },
    )
    assert conn.calls[0]["body"] == {
        "filter": {
            "_typeName": "EventFilterSpec",
            "entity": {
                "_typeName": "EventFilterSpecByEntity",
                "entity": vim_moref("Datastore", "datastore-12"),
                "recursion": "self",
            },
            "time": {
                "_typeName": "EventFilterSpecByTime",
                "beginTime": "2026-10-19T08:00:00Z",
                "endTime": "2026-10-19T09:30:00+02:00",
            },
            "disableFullMessage": False,
            "eventTypeId": ["esx.problem.storage.apd.start", "NASDatastoreEvent"],
            "maxCount": 250,
        }
    }


@pytest.mark.asyncio
async def test_event_tail_entity_recursion_defaults_to_all_and_time_may_be_one_sided() -> None:
    """``recursion`` defaults to ``all``; a lone ``begin_time`` sends only ``beginTime``."""
    conn = _RecordingConnector({_QUERY_EVENTS_PATH: []})
    await _run_event_tail(
        conn,
        {
            "begin_time": "2026-10-19T08:00:00Z",
            "entity": {"moId": "domain-c8", "type": "ClusterComputeResource"},
        },
    )
    spec = conn.calls[0]["body"]["filter"]
    assert spec["entity"]["recursion"] == "all"
    assert spec["time"] == {
        "_typeName": "EventFilterSpecByTime",
        "beginTime": "2026-10-19T08:00:00Z",
    }
    assert "eventTypeId" not in spec


@pytest.mark.asyncio
async def test_event_tail_reduces_each_event_to_a_short_row() -> None:
    """Classic, ``EventEx`` and ``ExtendedEvent`` events project to the four-key row."""
    events_payload = [
        {
            "_typeName": "VmPoweredOnEvent",
            "key": 101,
            "createdTime": "2026-10-19T08:01:00Z",
            "fullFormattedMessage": "web-01 on esx-1 is powered on",
            "host": {
                "_typeName": "HostEventArgument",
                "name": "esx-1",
                "host": vim_moref("HostSystem", "host-9"),
            },
            "vm": {
                "_typeName": "VmEventArgument",
                "name": "web-01",
                "vm": vim_moref("VirtualMachine", "vm-42"),
            },
        },
        {
            "_typeName": "EventEx",
            "createdTime": "2026-10-19T08:02:00Z",
            "eventTypeId": "esx.problem.storage.apd.start",
            "fullFormattedMessage": "Device entered the all paths down state.",
            "objectId": "datastore-12",
            "objectType": "Datastore",
            "objectName": "nfs-01",
        },
        {
            "_typeName": "ExtendedEvent",
            "createdTime": "2026-10-19T08:03:00Z",
            "eventTypeId": "com.example.extension.event",
            "managedObject": vim_moref("HostSystem", "host-9"),
        },
        {"_typeName": "GeneralUserEvent", "createdTime": "2026-10-19T08:04:00Z"},
    ]
    conn = _RecordingConnector({_QUERY_EVENTS_PATH: events_payload})
    out = await _run_event_tail(conn, {})
    assert out["count"] == 4
    assert out["events"] == [
        {
            "createdTime": "2026-10-19T08:01:00Z",
            "eventTypeId": "VmPoweredOnEvent",
            "fullFormattedMessage": "web-01 on esx-1 is powered on",
            "entity": {"type": "VirtualMachine", "moId": "vm-42", "name": "web-01"},
        },
        {
            "createdTime": "2026-10-19T08:02:00Z",
            "eventTypeId": "esx.problem.storage.apd.start",
            "fullFormattedMessage": "Device entered the all paths down state.",
            "entity": {"type": "Datastore", "moId": "datastore-12", "name": "nfs-01"},
        },
        {
            "createdTime": "2026-10-19T08:03:00Z",
            "eventTypeId": "com.example.extension.event",
            "fullFormattedMessage": None,
            "entity": {"type": "HostSystem", "moId": "host-9", "name": None},
        },
        {
            "createdTime": "2026-10-19T08:04:00Z",
            "eventTypeId": "GeneralUserEvent",
            "fullFormattedMessage": None,
            "entity": None,
        },
    ]


@pytest.mark.asyncio
async def test_event_tail_prefers_the_event_own_object_over_its_context() -> None:
    """``objectId`` / ``managedObject`` win over inherited ``Event`` context members.

    Both ``EventEx`` and ``ExtendedEvent`` inherit ``datacenter`` / ``host`` /
    ``ds`` / ... from ``Event``. A row must name the object the event is about,
    not the surrounding datacenter or host.
    """
    events_payload = [
        {
            "_typeName": "EventEx",
            "createdTime": "2026-10-19T09:01:00Z",
            "eventTypeId": "esx.problem.storage.apd.start",
            "datacenter": {
                "_typeName": "DatacenterEventArgument",
                "name": "dc-01",
                "datacenter": vim_moref("Datacenter", "datacenter-2"),
            },
            "ds": {
                "_typeName": "DatastoreEventArgument",
                "name": "nfs-01",
                "datastore": vim_moref("Datastore", "datastore-12"),
            },
            "objectId": "datastore-12",
            "objectType": "Datastore",
            "objectName": "nfs-01",
        },
        {
            "_typeName": "ExtendedEvent",
            "createdTime": "2026-10-19T09:02:00Z",
            "eventTypeId": "com.example.extension.event",
            "host": {
                "_typeName": "HostEventArgument",
                "name": "esx-1",
                "host": vim_moref("HostSystem", "host-9"),
            },
            "managedObject": vim_moref("VirtualMachine", "vm-42"),
        },
        {
            "_typeName": "EventEx",
            "createdTime": "2026-10-19T09:03:00Z",
            "eventTypeId": "com.example.no.object",
            "datacenter": {
                "_typeName": "DatacenterEventArgument",
                "name": "dc-01",
                "datacenter": vim_moref("Datacenter", "datacenter-2"),
            },
        },
    ]
    conn = _RecordingConnector({_QUERY_EVENTS_PATH: events_payload})
    out = await _run_event_tail(conn, {})
    assert [row["entity"] for row in out["events"]] == [
        {"type": "Datastore", "moId": "datastore-12", "name": "nfs-01"},
        {"type": "VirtualMachine", "moId": "vm-42", "name": None},
        {"type": "Datacenter", "moId": "datacenter-2", "name": "dc-01"},
    ]


@pytest.mark.asyncio
async def test_event_tail_cuts_to_max_count_when_the_server_ignores_it() -> None:
    """A server that returns more than ``maxCount`` is cut to ``max_count`` rows."""
    events_payload = [{"_typeName": "Event", "key": n} for n in range(200)]
    conn = _RecordingConnector({_QUERY_EVENTS_PATH: events_payload})
    out = await _run_event_tail(conn, {"max_count": 7})
    assert conn.calls[0]["body"]["filter"]["maxCount"] == 7
    assert len(out["events"]) == 7
    assert out["count"] == 7
    assert out["max_count_applied"] == 7


@pytest.mark.asyncio
async def test_event_tail_custom_mo_id_rides_the_path() -> None:
    """A per-call ``moId`` override lands in the mounted path segment."""
    conn = _RecordingConnector({"/api/EventManager/em-2/QueryEvents": []})
    await _run_event_tail(conn, {"moId": "em-2"})
    assert conn.calls[0]["path"] == "/api/EventManager/em-2/QueryEvents"


@pytest.mark.asyncio
async def test_event_tail_tolerates_legacy_value_envelope_and_null() -> None:
    """A ``{"value": [...]}`` envelope unwraps; a ``null`` answer is no events."""
    conn = _RecordingConnector([{"value": [{"_typeName": "Event"}, {"_typeName": "Event"}]}, None])
    out = await _run_event_tail(conn, {})
    assert out["count"] == 2
    out = await _run_event_tail(conn, {})
    assert out["count"] == 0
    assert out["events"] == []


@pytest.mark.asyncio
async def test_event_tail_raises_on_non_list_payload() -> None:
    """Non-list payload from QueryEvents raises ``RuntimeError`` (audit-visible)."""
    conn = _RecordingConnector({_QUERY_EVENTS_PATH: {"not": "a list"}})
    with pytest.raises(RuntimeError, match="expected list"):
        await _run_event_tail(conn, {})


@pytest.mark.asyncio
async def test_event_tail_propagates_load_bearing_error() -> None:
    """QueryEvents transport failure propagates (no aggregation)."""
    conn = _RecordingConnector([_http_error(400, f"https://vc{_QUERY_EVENTS_PATH}")])
    with pytest.raises(httpx.HTTPStatusError):
        await _run_event_tail(conn, {})


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
    # PerfMgr singleton rides the path. ``entity`` is an annotated MoRef and
    # QueryPerf takes a PerfQuerySpec list (#4038).
    entity = vim_moref("VirtualMachine", "vm-1234")
    assert conn.calls[0]["body"] == {"entity": entity, "intervalId": 20}
    assert conn.calls[1]["body"] == {
        "querySpec": [
            {
                "_typeName": "PerfQuerySpec",
                "entity": entity,
                "intervalId": 20,
                "maxSample": 60,
            }
        ]
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
    # AltPerfMgr rides the path; the interval rides both bodies as intervalId.
    assert conn.calls[1]["path"] == "/api/PerformanceManager/AltPerfMgr/QueryPerf"
    assert conn.calls[0]["body"]["intervalId"] == 300
    assert conn.calls[1]["body"]["querySpec"][0]["intervalId"] == 300


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_moid", "mo_type"),
    [
        ("host-5678", "HostSystem"),
        ("vm-12", "VirtualMachine"),
        ("domain-c9", "ClusterComputeResource"),
    ],
)
async def test_performance_summary_entity_type_from_moid_prefix(
    entity_moid: str, mo_type: str
) -> None:
    """The moid prefix picks the MoRef type; vCenter 8.0.3 rejects a bare string (#4038)."""
    conn = _RecordingConnector([[], []])
    await performance_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"entity_moid": entity_moid},
        connector=conn,  # type: ignore[arg-type]
    )
    expected = {"_typeName": "ManagedObjectReference", "type": mo_type, "value": entity_moid}
    assert conn.calls[0]["body"]["entity"] == expected
    assert conn.calls[1]["body"]["querySpec"][0]["entity"] == expected


@pytest.mark.asyncio
async def test_performance_summary_entity_type_overrides_prefix() -> None:
    """An explicit ``entity_type`` wins, so other entity kinds work too."""
    conn = _RecordingConnector([[], []])
    await performance_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"entity_moid": "resgroup-4", "entity_type": "ResourcePool"},
        connector=conn,  # type: ignore[arg-type]
    )
    expected = vim_moref("ResourcePool", "resgroup-4")
    assert conn.calls[0]["body"]["entity"] == expected
    assert conn.calls[1]["body"]["querySpec"][0]["entity"] == expected


@pytest.mark.asyncio
async def test_performance_summary_unknown_prefix_without_type_raises() -> None:
    """An unknown prefix and no ``entity_type`` fails before any call."""
    conn = _RecordingConnector([[], []])
    with pytest.raises(ValueError, match="entity_type"):
        await performance_summary_composite(
            operator=_make_operator(),
            target=object(),
            params={"entity_moid": "resgroup-4"},
            connector=conn,  # type: ignore[arg-type]
        )
    assert conn.calls == []


# ---------------------------------------------------------------------------
# vmware.composite.datastore.usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_datastore_usage_aggregates_capacity_and_authoritative_placement() -> None:
    """Listing + per-DS detail + one batched authoritative placement read;
    aggregated payload matches spec (#2975)."""
    listing = [
        {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"},
        {"datastore": "datastore-2", "name": "ds-2", "type": "NFS"},
    ]
    global_vms = [
        {"vm": "vm-1", "name": "vm-a"},
        {"vm": "vm-2", "name": "vm-b"},
        {"vm": "vm-3", "name": "vm-c"},
    ]
    placement = _retrieve_result_multi(
        {
            ("VirtualMachine", "vm-1"): {"datastore": _vm_placement(["datastore-1"])},
            ("VirtualMachine", "vm-2"): {"datastore": _vm_placement(["datastore-1"])},
            ("VirtualMachine", "vm-3"): {"datastore": _vm_placement(["datastore-2"])},
        }
    )
    conn = _RecordingConnector(
        [
            listing,
            {"capacity": 100, "free_space": 40, "type": "VMFS"},
            {"capacity": 500, "free_space": 250, "type": "NFS"},
            global_vms,
            placement,
        ]
    )

    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )

    # 1 listing + 2 per-DS detail + 1 global VM listing + 1 placement read.
    assert len(conn.calls) == 5
    assert conn.calls[0]["method"] == "GET"
    assert conn.calls[0]["path"] == "/api/vcenter/datastore"
    # No filter -> no query string.
    assert conn.calls[0]["query"] is None
    # Per-DS detail calls embed the datastore moid in the mounted path;
    # they run consecutively before the batched placement read.
    assert conn.calls[1]["path"] == "/api/vcenter/datastore/datastore-1"
    assert conn.calls[2]["path"] == "/api/vcenter/datastore/datastore-2"
    # The VM listing is global -- no per-datastore scoping query is sent
    # upstream; scoping is done client-side off vim placement (#2975).
    assert conn.calls[3]["path"] == "/api/vcenter/vm"
    assert conn.calls[3]["query"] is None
    assert conn.calls[4]["path"] == "/api/PropertyCollector/propertyCollector/RetrievePropertiesEx"
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
async def test_datastore_usage_listing_filter_keeps_prefix_on_legacy_mount() -> None:
    """On the legacy/vcsim ``/rest`` mount the listing filter keeps the
    ``filter.`` prefix (#2298); the global VM listing carries no filter."""
    listing = [{"datastore": "datastore-1", "name": "ds-1", "type": "VMFS", "capacity": 10}]
    sequence = [listing, {"capacity": 10, "free_space": 4}, []]
    conn = _RecordingConnector(sequence, mount_prefix="/rest")
    await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter_names": ["ds-prod-1"]},
        connector=conn,  # type: ignore[arg-type]
    )
    # Listing keeps ``filter.names`` on /rest; the global VM leg is unfiltered
    # (per-datastore placement is resolved off vim, not a REST filter, #2975).
    assert conn.calls[0]["path"] == "/rest/vcenter/datastore"
    assert conn.calls[0]["query"] == {"filter.names": ["ds-prod-1"]}
    assert conn.calls[2]["path"] == "/rest/vcenter/vm"
    assert conn.calls[2]["query"] is None


@pytest.mark.asyncio
async def test_datastore_usage_tolerates_legacy_envelope_on_reads() -> None:
    """``{"value": [...]}`` envelopes on the listing / detail / VM legs are unwrapped."""
    placement = _retrieve_result_multi(
        {("VirtualMachine", "vm-1"): {"datastore": _vm_placement(["datastore-1"])}}
    )
    sequence = [
        {"value": [{"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"}]},
        {"value": {"capacity": 10, "free_space": 5}},
        {"value": [{"vm": "vm-1", "name": "vm-x"}]},
        placement,
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
async def test_datastore_usage_placement_read_error_is_best_effort() -> None:
    """A failed batched placement read keeps every row's capacity but nulls
    vm_count/vm_names + records a note on all rows (best-effort, #1908 / #2975)."""
    listing = [
        {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"},
        {"datastore": "datastore-2", "name": "ds-2", "type": "NFS"},
    ]
    sequence = [
        listing,
        {"capacity": 100, "free_space": 40},
        {"capacity": 500, "free_space": 250},
        # The global VM listing succeeds; the placement RetrievePropertiesEx
        # 403s -> the whole batched enrichment degrades on every row.
        [{"vm": "vm-1", "name": "vm-a"}],
        _http_error(403, "https://vc/api/PropertyCollector/propertyCollector/RetrievePropertiesEx"),
    ]
    conn = _RecordingConnector(sequence)
    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )

    # All 5 sub-ops fired -- the failed placement read did not short-circuit
    # the load-bearing capacity reads.
    assert len(conn.calls) == 5
    rows = out["datastores"]
    assert len(rows) == 2

    for row, cap, free in ((rows[0], 100, 40), (rows[1], 500, 250)):
        # Core capacity data preserved; only the enrichment is dropped.
        assert row["capacity"] == cap
        assert row["free_space"] == free
        assert row["vm_count"] is None
        assert row["vm_names"] is None
        note = row["enrichment_note"]
        assert "placement read failed" in note
        assert "403 Forbidden" in note


@pytest.mark.asyncio
async def test_datastore_usage_identical_vm_sets_across_rows_trip_guard() -> None:
    """Identical non-empty VM sets on every row -- an anomaly authoritative
    placement cannot produce in practice -- still trip the belt-and-braces
    guard: vm_count/vm_names nulled + note on every row (#3049 / #2975)."""
    listing = [
        {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"},
        {"datastore": "datastore-2", "name": "ds-2", "type": "NFS"},
        {"datastore": "datastore-3", "name": "ds-3", "type": "vSAN"},
    ]
    global_vms = [
        {"vm": "vm-1", "name": "vm-a"},
        {"vm": "vm-2", "name": "vm-b"},
        {"vm": "vm-3", "name": "vm-c"},
    ]
    # Every VM reports it sits on all three datastores -> identical set per row.
    all_three = ["datastore-1", "datastore-2", "datastore-3"]
    placement = _retrieve_result_multi(
        {
            ("VirtualMachine", "vm-1"): {"datastore": _vm_placement(all_three)},
            ("VirtualMachine", "vm-2"): {"datastore": _vm_placement(all_three)},
            ("VirtualMachine", "vm-3"): {"datastore": _vm_placement(all_three)},
        }
    )
    sequence: list[Any] = [listing]
    for _ in listing:
        sequence.append({"capacity": 100, "free_space": 40})
    sequence.append(global_vms)
    sequence.append(placement)
    conn = _RecordingConnector(sequence)

    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )

    rows = out["datastores"]
    assert len(rows) == 3
    for row in rows:
        # Core capacity data is preserved; only the wrong enrichment is dropped.
        assert row["capacity"] == 100
        assert row["free_space"] == 40
        assert row["vm_count"] is None
        assert row["vm_names"] is None
        note = row["enrichment_note"]
        assert "identical" in note
        assert "3 enriched datastores" in note


@pytest.mark.asyncio
async def test_datastore_usage_distinct_vm_sets_across_rows_left_populated() -> None:
    """Genuinely-distinct per-datastore VM sets are real placement data: the
    identical-sets guard does not fire and the rows stay populated (#2975)."""
    listing = [
        {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"},
        {"datastore": "datastore-2", "name": "ds-2", "type": "NFS"},
        {"datastore": "datastore-3", "name": "ds-3", "type": "vSAN"},
    ]
    global_vms = [
        {"vm": "vm-1", "name": "vm-a"},
        {"vm": "vm-2", "name": "vm-b"},
        {"vm": "vm-3", "name": "vm-c"},
        {"vm": "vm-4", "name": "vm-d"},
        {"vm": "vm-5", "name": "vm-e"},
    ]
    placement = _retrieve_result_multi(
        {
            ("VirtualMachine", "vm-1"): {"datastore": _vm_placement(["datastore-1"])},
            ("VirtualMachine", "vm-2"): {"datastore": _vm_placement(["datastore-1"])},
            ("VirtualMachine", "vm-3"): {"datastore": _vm_placement(["datastore-2"])},
            ("VirtualMachine", "vm-4"): {"datastore": _vm_placement(["datastore-3"])},
            ("VirtualMachine", "vm-5"): {"datastore": _vm_placement(["datastore-3"])},
        }
    )
    sequence: list[Any] = [listing]
    for _ in listing:
        sequence.append({"capacity": 100, "free_space": 40})
    sequence.append(global_vms)
    sequence.append(placement)
    conn = _RecordingConnector(sequence)

    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )

    rows = out["datastores"]
    assert [r["vm_count"] for r in rows] == [2, 1, 2]
    assert rows[0]["vm_names"] == ["vm-a", "vm-b"]
    assert rows[1]["vm_names"] == ["vm-c"]
    assert rows[2]["vm_names"] == ["vm-d", "vm-e"]
    assert all("enrichment_note" not in r for r in rows)


@pytest.mark.asyncio
async def test_datastore_usage_vm_spanning_two_datastores_appears_in_both() -> None:
    """A VM whose files span two datastores is real placement on both, so it
    appears in both rows -- the union semantics of ``VirtualMachine.datastore``
    (#2975)."""
    listing = [
        {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"},
        {"datastore": "datastore-2", "name": "ds-2", "type": "NFS"},
    ]
    global_vms = [
        {"vm": "vm-1", "name": "vm-a"},
        {"vm": "vm-2", "name": "vm-span"},
    ]
    placement = _retrieve_result_multi(
        {
            ("VirtualMachine", "vm-1"): {"datastore": _vm_placement(["datastore-1"])},
            # vm-span has disks on both datastores.
            ("VirtualMachine", "vm-2"): {
                "datastore": _vm_placement(["datastore-1", "datastore-2"])
            },
        }
    )
    sequence: list[Any] = [
        listing,
        {"capacity": 100, "free_space": 40},
        {"capacity": 500, "free_space": 250},
        global_vms,
        placement,
    ]
    conn = _RecordingConnector(sequence)

    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )

    rows = out["datastores"]
    assert rows[0]["vm_names"] == ["vm-a", "vm-span"]
    assert rows[1]["vm_names"] == ["vm-span"]
    assert all("enrichment_note" not in r for r in rows)


@pytest.mark.asyncio
async def test_datastore_usage_scopes_vms_per_datastore_from_authoritative_placement() -> None:
    """Reproduction of #2975: the raw VM listing is the WHOLE inventory (the
    upstream per-datastore filter is not honoured), yet each row must carry
    only the VMs actually placed on that datastore.

    The composite scopes client-side off the authoritative vim
    ``VirtualMachine.datastore`` property (one batched
    ``RetrievePropertiesEx`` over every VM), never the server-side filter.
    Two datastores hosting disjoint VM sets therefore get DIFFERENT lists
    and the identical-sets guard (#3049) never fires -- the guard's firing
    condition is made impossible in practice by the real fix."""
    listing = [
        {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"},
        {"datastore": "datastore-2", "name": "ds-2", "type": "NFS"},
    ]
    # The raw ``GET:/vcenter/vm`` listing carries the whole inventory with
    # zero per-datastore scoping -- the exact symptom shape (#2975).
    global_vms = [
        {"vm": "vm-1", "name": "vm-a"},
        {"vm": "vm-2", "name": "vm-b"},
        {"vm": "vm-3", "name": "vm-c"},
    ]
    # Authoritative placement (vim ``VirtualMachine.datastore``): disjoint.
    placement = _retrieve_result_multi(
        {
            ("VirtualMachine", "vm-1"): {"datastore": _vm_placement(["datastore-1"])},
            ("VirtualMachine", "vm-2"): {"datastore": _vm_placement(["datastore-1"])},
            ("VirtualMachine", "vm-3"): {"datastore": _vm_placement(["datastore-2"])},
        }
    )
    sequence: list[Any] = [
        listing,
        {"capacity": 100, "free_space": 40},
        {"capacity": 500, "free_space": 250},
        global_vms,
        placement,
    ]
    conn = _RecordingConnector(sequence)

    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )

    # Call shape: listing -> per-DS detail x2 -> global VM listing (NO
    # per-datastore filter) -> one RetrievePropertiesEx placement read.
    assert [(c["method"], c["path"]) for c in conn.calls] == [
        ("GET", "/api/vcenter/datastore"),
        ("GET", "/api/vcenter/datastore/datastore-1"),
        ("GET", "/api/vcenter/datastore/datastore-2"),
        ("GET", "/api/vcenter/vm"),
        ("POST", "/api/PropertyCollector/propertyCollector/RetrievePropertiesEx"),
    ]
    # The VM listing is global -- no scoping query is sent upstream.
    assert conn.calls[3]["query"] is None
    # The placement read scopes VirtualMachine.datastore over every VM moid.
    body = conn.calls[4]["body"]
    prop = body["specSet"][0]["propSet"][0]
    assert prop["type"] == "VirtualMachine"
    assert prop["pathSet"] == ["datastore"]
    assert {o["obj"]["value"] for o in body["specSet"][0]["objectSet"]} == {
        "vm-1",
        "vm-2",
        "vm-3",
    }

    rows = out["datastores"]
    ds1, ds2 = rows[0], rows[1]
    assert ds1["vm_count"] == 2
    assert ds1["vm_names"] == ["vm-a", "vm-b"]
    assert ds2["vm_count"] == 1
    assert ds2["vm_names"] == ["vm-c"]
    assert "enrichment_note" not in ds1
    assert "enrichment_note" not in ds2


@pytest.mark.asyncio
async def test_datastore_usage_all_empty_vm_sets_do_not_trip_guard() -> None:
    """No VMs anywhere is real data, not the global-list symptom; the guard's
    non-empty check excludes it and the placement read short-circuits (#2975)."""
    listing = [
        {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"},
        {"datastore": "datastore-2", "name": "ds-2", "type": "NFS"},
    ]
    sequence: list[Any] = [
        listing,
        {"capacity": 100, "free_space": 40},
        {"capacity": 100, "free_space": 40},
        [],  # global VM listing empty -> every datastore has zero VMs
    ]
    conn = _RecordingConnector(sequence)

    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        connector=conn,  # type: ignore[arg-type]
    )

    # An empty inventory skips the RetrievePropertiesEx placement read.
    assert len(conn.calls) == 4
    retrieve_path = "/api/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    assert all(c["path"] != retrieve_path for c in conn.calls)
    rows = out["datastores"]
    assert [r["vm_count"] for r in rows] == [0, 0]
    assert [r["vm_names"] for r in rows] == [[], []]
    assert all("enrichment_note" not in r for r in rows)


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

_DVPG_TYPE = "DistributedVirtualPortgroup"
_VDS_TYPE = "VmwareDistributedVirtualSwitch"
_VM_TYPE = "VirtualMachine"
_DEEP_VLAN_PATH = "config.defaultPortConfig.vlan"


def _boxed(value: Any) -> Any:
    """Box a primitive the way live VI-JSON puts it in ``DynamicProperty.val``."""
    if isinstance(value, str):
        return {"_typeName": "string", "_value": value}
    return value


def _moref_array(mo_type: str, moids: list[str]) -> dict[str, Any]:
    return {
        "_typeName": "ArrayOfManagedObjectReference",
        "_value": [vim_moref(mo_type, moid) for moid in moids],
    }


def _access_port_config(vlan_id: int) -> dict[str, Any]:
    return {
        "_typeName": "VMwareDVSPortSetting",
        "vlan": {
            "_typeName": "VmwareDistributedVirtualSwitchVlanIdSpec",
            "inherited": False,
            "vlanId": vlan_id,
        },
    }


def _trunk_port_config(ranges: list[tuple[int, int]]) -> dict[str, Any]:
    return {
        "_typeName": "VMwareDVSPortSetting",
        "vlan": {
            "_typeName": "VmwareDistributedVirtualSwitchTrunkVlanSpec",
            "inherited": False,
            "vlanId": [
                {"_typeName": "NumericRange", "start": start, "end": end} for start, end in ranges
            ],
        },
    }


# Two portgroups on two switches, with disjoint VLANs and disjoint VMs,
# except ``vm-5`` which has one NIC on each portgroup. ``vm-4`` is
# powered off. ``vm-9`` exists but sits on neither portgroup.
_PG_INVENTORY: dict[str, dict[str, dict[str, Any]]] = {
    _DVPG_TYPE: {
        "dvportgroup-11": {
            "config.distributedVirtualSwitch": vim_moref(_VDS_TYPE, "dvs-1"),
            "config.defaultPortConfig": _access_port_config(110),
            "vm": _moref_array(_VM_TYPE, ["vm-1", "vm-2", "vm-5"]),
        },
        "dvportgroup-12": {
            "config.distributedVirtualSwitch": vim_moref(_VDS_TYPE, "dvs-2"),
            "config.defaultPortConfig": _trunk_port_config([(300, 300), (200, 210)]),
            "vm": _moref_array(_VM_TYPE, ["vm-3", "vm-4", "vm-5"]),
        },
    },
    _VDS_TYPE: {
        "dvs-1": {"name": "DSwitch-Prod"},
        "dvs-2": {"name": "DSwitch-Lab"},
    },
    _VM_TYPE: {
        "vm-1": {"name": "web-01", "runtime.powerState": "poweredOn"},
        "vm-2": {"name": "web-02", "runtime.powerState": "poweredOn"},
        "vm-3": {"name": "db-01", "runtime.powerState": "poweredOn"},
        "vm-4": {"name": "db-02", "runtime.powerState": "poweredOff"},
        "vm-5": {"name": "router-01", "runtime.powerState": "poweredOn"},
        "vm-9": {"name": "unrelated-01", "runtime.powerState": "poweredOn"},
    },
}

_PG_LISTING = [
    {"network": "dvportgroup-11", "name": "PG-Prod", "type": "DISTRIBUTED_PORTGROUP"},
    {"network": "dvportgroup-12", "name": "PG-Lab", "type": "DISTRIBUTED_PORTGROUP"},
]


class _InventoryConnector(_RecordingConnector):
    """Fake that answers each PropertyCollector read from ``_PG_INVENTORY``.

    The answer depends on the object type, the moids and the property
    paths in the request body -- not on call order -- so a handler that
    reads the wrong object gets the wrong data back. The one GET (the
    portgroup listing) is served from *listing*. Like vCenter 8.0.x, the
    fake fails a request for the deep ``config.defaultPortConfig.vlan``
    path.
    """

    def __init__(
        self,
        listing: Any,
        *,
        mount_prefix: str = "/api",
        inventory: dict[str, dict[str, dict[str, Any]]] = _PG_INVENTORY,
        paged_type: str | None = None,
        unreadable: tuple[str, str, str, Any] | None = None,
    ) -> None:
        super().__init__([listing], mount_prefix=mount_prefix)
        self._inventory = inventory
        # When set, every read of this object type answers with a ``token``,
        # the way vCenter does when it pages a result.
        self._paged_type = paged_type
        # ``(mo_type, moid, path, fault)``: the call succeeds, but this one
        # property lands in the object's ``missingSet`` with that fault,
        # the way vCenter reports a property it could not read.
        self._unreadable = unreadable

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> Any:
        mounted = f"{self._mount_prefix}{path}"
        self.calls.append(
            {"method": "POST", "path": mounted, "query": None, "body": json, "vmomi": True}
        )
        assert json is not None
        spec = json["specSet"][0]
        mo_type = spec["propSet"][0]["type"]
        path_set = spec["propSet"][0]["pathSet"]
        if _DEEP_VLAN_PATH in path_set:
            raise _http_error(500, mounted)
        objects = []
        for obj_spec in spec["objectSet"]:
            moid = obj_spec["obj"]["value"]
            props = self._inventory[mo_type][moid]
            missing: list[dict[str, Any]] = []
            if self._unreadable and self._unreadable[:2] == (mo_type, moid):
                _, _, bad_path, fault = self._unreadable
                if bad_path in path_set:
                    props = {name: val for name, val in props.items() if name != bad_path}
                    missing.append(
                        {"_typeName": "MissingProperty", "path": bad_path, "fault": fault}
                    )
            objects.append(
                {
                    "obj": vim_moref(mo_type, moid),
                    "propSet": [
                        {"name": name, "val": _boxed(props[name])}
                        for name in path_set
                        if name in props
                    ],
                    "missingSet": missing,
                }
            )
        result: dict[str, Any] = {"_typeName": "RetrieveResult", "objects": objects}
        if mo_type == self._paged_type:
            result["token"] = "1"
        return result


def _read_targets(conn: _RecordingConnector) -> list[tuple[str, list[str], list[str]]]:
    """``(type, moids, pathSet)`` of every PropertyCollector call, in order."""
    targets = []
    for call in conn.calls:
        if call["method"] != "POST":
            continue
        spec = call["body"]["specSet"][0]
        targets.append(
            (
                spec["propSet"][0]["type"],
                [o["obj"]["value"] for o in spec["objectSet"]],
                spec["propSet"][0]["pathSet"],
            )
        )
    return targets


async def _run_audit(conn: _RecordingConnector, params: dict[str, Any]) -> dict[str, Any]:
    return await network_portgroup_audit_composite(
        operator=_make_operator(),
        target=object(),
        params=params,
        connector=conn,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_network_portgroup_audit_rows_carry_only_their_own_vms_vlan_and_switch() -> None:
    """#3802: each row gets only its own VMs, its own VLAN and its own switch.

    Two portgroups with disjoint VM sets and VLANs. The fake answers by
    the requested object, so any cross-portgroup bleed (the old
    whole-inventory VM list on every row) shows up as a wrong row.
    """
    conn = _InventoryConnector(_PG_LISTING)

    out = await _run_audit(conn, {})

    assert out["portgroups"] == [
        {
            "id": "dvportgroup-11",
            "name": "PG-Prod",
            "dvs": "dvs-1",
            "dvs_name": "DSwitch-Prod",
            "type": "DISTRIBUTED_PORTGROUP",
            "vlan": {"mode": "access", "vlan_id": 110},
            "vm_count": 3,
            "vm_names": ["web-01", "web-02", "router-01"],
        },
        {
            "id": "dvportgroup-12",
            "name": "PG-Lab",
            "dvs": "dvs-2",
            "dvs_name": "DSwitch-Lab",
            "type": "DISTRIBUTED_PORTGROUP",
            "vlan": {
                "mode": "trunk",
                "ranges": [{"start": 200, "end": 210}, {"start": 300, "end": 300}],
            },
            # vm-4 is powered off, so it is left out by default.
            "vm_count": 2,
            "vm_names": ["db-01", "router-01"],
        },
    ]


@pytest.mark.asyncio
async def test_network_portgroup_audit_reads_portgroup_objects_not_the_vm_list() -> None:
    """The audit lists portgroups once, then reads vim properties in batches.

    No ``GET /vcenter/vm`` call is made (it has no ``networks`` filter),
    and the port config is read whole, never the deep VLAN path.
    """
    conn = _InventoryConnector(_PG_LISTING)

    await _run_audit(conn, {})

    get_calls = [c for c in conn.calls if c["method"] == "GET"]
    assert [c["path"] for c in get_calls] == ["/api/vcenter/network"]
    assert get_calls[0]["query"] == {"types": ["DISTRIBUTED_PORTGROUP"]}
    assert all(
        c["path"] == "/api/PropertyCollector/propertyCollector/RetrievePropertiesEx"
        for c in conn.calls
        if c["method"] == "POST"
    )
    assert _read_targets(conn) == [
        (
            _DVPG_TYPE,
            ["dvportgroup-11", "dvportgroup-12"],
            ["config.distributedVirtualSwitch", "config.defaultPortConfig", "vm"],
        ),
        (_VDS_TYPE, ["dvs-1", "dvs-2"], ["name"]),
        (_VM_TYPE, ["vm-1", "vm-2", "vm-5", "vm-3", "vm-4"], ["name", "runtime.powerState"]),
    ]
    for _, _, path_set in _read_targets(conn):
        assert _DEEP_VLAN_PATH not in path_set


@pytest.mark.asyncio
async def test_network_portgroup_audit_include_disconnected_keeps_powered_off_vms() -> None:
    """``include_disconnected_vms=True`` keeps VMs that are not powered on."""
    conn = _InventoryConnector(_PG_LISTING)

    out = await _run_audit(conn, {"include_disconnected_vms": True})

    lab = out["portgroups"][1]
    assert lab["vm_names"] == ["db-01", "db-02", "router-01"]
    assert lab["vm_count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("filter_dvs", ["dvs-2", "DSwitch-Lab"])
async def test_network_portgroup_audit_filter_dvs_keeps_matching_switch_only(
    filter_dvs: str,
) -> None:
    """``filter_dvs`` matches the parent switch moid or name; VMs of other rows are not read."""
    conn = _InventoryConnector(_PG_LISTING)

    out = await _run_audit(conn, {"filter_dvs": filter_dvs})

    assert [row["id"] for row in out["portgroups"]] == ["dvportgroup-12"]
    assert _read_targets(conn)[-1][1] == ["vm-3", "vm-4", "vm-5"]


@pytest.mark.asyncio
async def test_network_portgroup_audit_empty_listing_makes_one_call() -> None:
    """No distributed portgroups -> only the listing call, no vim reads."""
    conn = _InventoryConnector([])

    out = await _run_audit(conn, {})

    assert out == {"portgroups": []}
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_network_portgroup_audit_tolerates_unset_properties() -> None:
    """A portgroup without a reported switch, VLAN or VMs gets null / empty fields.

    vim leaves unset properties out of ``propSet``; the row must not
    crash or borrow another portgroup's data.
    """
    conn = _InventoryConnector(
        [{"network": "dvportgroup-99", "name": "PG-Bare", "type": "DISTRIBUTED_PORTGROUP"}],
        inventory={_DVPG_TYPE: {"dvportgroup-99": {}}},
    )

    out = await _run_audit(conn, {})

    assert out["portgroups"] == [
        {
            "id": "dvportgroup-99",
            "name": "PG-Bare",
            "dvs": None,
            "dvs_name": None,
            "type": "DISTRIBUTED_PORTGROUP",
            "vlan": None,
            "vm_count": 0,
            "vm_names": [],
        }
    ]
    # Only the portgroup read runs: no switch or VM to resolve.
    assert len(_read_targets(conn)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("paged_type", [_DVPG_TYPE, _VM_TYPE])
async def test_network_portgroup_audit_fails_on_a_paged_answer(paged_type: str) -> None:
    """A paged vim answer (a ``token``) fails the audit; it never returns short rows.

    vCenter may return part of the objects plus a ``token`` for the rest.
    The audit does not follow the token, so it must raise instead of
    returning rows with missing VMs or portgroups.
    """
    conn = _InventoryConnector(_PG_LISTING, paged_type=paged_type)

    with pytest.raises(RuntimeError, match="paged"):
        await _run_audit(conn, {})


# The two VI-JSON shapes of a ``missingSet`` fault: 9.x wraps the concrete
# fault in a ``LocalizedMethodFault``; 8.0.x flattens it onto ``fault``.
_NO_PERMISSION_NESTED = {
    "_typeName": "LocalizedMethodFault",
    "fault": {"_typeName": "NoPermission", "object": vim_moref(_VM_TYPE, "vm-1")},
    "localizedMessage": "Permission to perform this operation was denied.",
}
_NO_PERMISSION_FLAT = {"_typeName": "NoPermission", "object": vim_moref(_VM_TYPE, "vm-1")}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mo_type", "moid", "path"),
    [
        (_VM_TYPE, "vm-1", "name"),
        (_VM_TYPE, "vm-5", "runtime.powerState"),
        (_VDS_TYPE, "dvs-2", "name"),
        (_DVPG_TYPE, "dvportgroup-11", "vm"),
        (_DVPG_TYPE, "dvportgroup-12", "config.distributedVirtualSwitch"),
    ],
)
async def test_network_portgroup_audit_fails_when_a_property_cannot_be_read(
    mo_type: str, moid: str, path: str
) -> None:
    """A property vCenter could not read fails the audit; it never shrinks a count.

    The HTTP call succeeds, but one object answers with the property in
    ``missingSet`` and a ``NoPermission`` fault. Dropping that VM, switch
    or portgroup would report a wrong VM count or a null switch, so the
    audit raises instead. The message names the path and the fault type,
    never the fault text.
    """
    conn = _InventoryConnector(_PG_LISTING, unreadable=(mo_type, moid, path, _NO_PERMISSION_NESTED))

    with pytest.raises(RuntimeError, match="could not read") as excinfo:
        await _run_audit(conn, {"include_disconnected_vms": True})

    message = str(excinfo.value)
    assert mo_type in message
    assert f"{path} (NoPermission)" in message
    assert "denied" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        (_NO_PERMISSION_FLAT, "name (NoPermission)"),
        ({"_typeName": "SystemError", "reason": "boom"}, "name (SystemError)"),
        (None, "name (unknown fault)"),
    ],
)
async def test_network_portgroup_audit_reads_the_fault_type_of_an_unreadable_vm(
    fault: Any, expected: str
) -> None:
    """The 8.0.x flat fault shape and a fault without a type name also fail the audit."""
    conn = _InventoryConnector(_PG_LISTING, unreadable=(_VM_TYPE, "vm-2", "name", fault))

    with pytest.raises(RuntimeError, match="could not read") as excinfo:
        await _run_audit(conn, {})

    assert expected in str(excinfo.value)
    assert "boom" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_network_portgroup_audit_unreadable_vm_in_a_later_chunk_still_fails() -> None:
    """An unreadable VM in the third chunk of 50 fails the audit, after the first chunks passed."""
    vm_moids = [f"vm-{n}" for n in range(1, 121)]
    inventory: dict[str, dict[str, dict[str, Any]]] = {
        _DVPG_TYPE: {
            "dvportgroup-11": {
                "config.distributedVirtualSwitch": vim_moref(_VDS_TYPE, "dvs-1"),
                "config.defaultPortConfig": _access_port_config(110),
                "vm": _moref_array(_VM_TYPE, vm_moids),
            },
        },
        _VDS_TYPE: {"dvs-1": {"name": "DSwitch-Prod"}},
        _VM_TYPE: {
            moid: {"name": f"name-{moid}", "runtime.powerState": "poweredOn"} for moid in vm_moids
        },
    }
    conn = _InventoryConnector(
        _PG_LISTING[:1],
        inventory=inventory,
        unreadable=(_VM_TYPE, "vm-111", "runtime.powerState", _NO_PERMISSION_NESTED),
    )

    with pytest.raises(RuntimeError, match=r"runtime\.powerState \(NoPermission\)"):
        await _run_audit(conn, {})

    vm_reads = [moids for mo_type, moids, _ in _read_targets(conn) if mo_type == _VM_TYPE]
    assert vm_reads == [vm_moids[0:50], vm_moids[50:100], vm_moids[100:120]]


@pytest.mark.asyncio
async def test_network_portgroup_audit_reads_many_vms_in_chunks_of_50() -> None:
    """More than 50 VMs are read in chunks of 50 and the chunks are merged."""
    vm_moids = [f"vm-{n}" for n in range(1, 121)]
    inventory: dict[str, dict[str, dict[str, Any]]] = {
        _DVPG_TYPE: {
            "dvportgroup-11": {
                "config.distributedVirtualSwitch": vim_moref(_VDS_TYPE, "dvs-1"),
                "config.defaultPortConfig": _access_port_config(110),
                "vm": _moref_array(_VM_TYPE, vm_moids),
            },
        },
        _VDS_TYPE: {"dvs-1": {"name": "DSwitch-Prod"}},
        _VM_TYPE: {
            moid: {"name": f"name-{moid}", "runtime.powerState": "poweredOn"} for moid in vm_moids
        },
    }
    conn = _InventoryConnector(_PG_LISTING[:1], inventory=inventory)

    out = await _run_audit(conn, {})

    row = out["portgroups"][0]
    assert row["vm_count"] == 120
    assert row["vm_names"] == [f"name-{moid}" for moid in vm_moids]
    vm_reads = [moids for mo_type, moids, _ in _read_targets(conn) if mo_type == _VM_TYPE]
    assert vm_reads == [vm_moids[0:50], vm_moids[50:100], vm_moids[100:120]]


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
    bare params on modern ``/api`` must still send ``filter.types`` on the
    legacy mount vcsim serves, or the existing vcsim fixtures break.
    """
    conn = _InventoryConnector(_PG_LISTING, mount_prefix="/rest")
    await _run_audit(conn, {})
    assert conn.calls[0]["path"] == "/rest/vcenter/network"
    assert conn.calls[0]["query"] == {"filter.types": ["DISTRIBUTED_PORTGROUP"]}


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
