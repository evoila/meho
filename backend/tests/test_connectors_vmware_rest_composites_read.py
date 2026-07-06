# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the 6 vmware-rest read-composite handler functions.

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

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import _preflight
from meho_backplane.connectors.vmware_rest.composites._read import (
    CompositeSubOpError,
    cluster_drs_recommendations_composite,
    datastore_usage_composite,
    event_tail_composite,
    host_network_uplinks_composite,
    network_portgroup_audit_composite,
    performance_summary_composite,
)


@pytest.fixture(autouse=True)
def _prime_preflight_cache() -> Iterator[None]:
    """Prime the L2 pre-flight cache so handler-direct tests skip the DB walk.

    The handler-direct tests in this module bypass the dispatcher and call
    the composite handlers as plain async functions. The G0.14-T10 (#1151)
    pre-flight check would otherwise issue a
    :func:`~meho_backplane.operations._lookup.lookup_descriptor` query
    against an unconfigured session at the top of every handler. Priming
    the per-process cache with each composite's op_id before the test
    fires (and clearing it after) keeps these tests handler-direct
    without re-shaping every fixture to stand up a DB session.

    Per-composite preflight behaviour (cache miss -> DB walk; cache miss
    on missing L2 -> structured exception) is exercised in the dedicated
    module ``test_connectors_vmware_rest_composites_l2_preflight.py``
    where a stub ``lookup_descriptor`` exercises both the all-present
    and the missing-L2 code paths.
    """
    _preflight.reset_preflight_cache()
    _preflight._PREFLIGHT_CACHE.update(
        {
            "vmware.composite.cluster.drs_recommendations",
            "vmware.composite.event.tail",
            "vmware.composite.performance.summary",
            "vmware.composite.datastore.usage",
            "vmware.composite.network.portgroup.audit",
            "vmware.composite.host.network_uplinks",
        }
    )
    yield
    _preflight.reset_preflight_cache()


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


def _connector_error_result(op_id: str, exception_message: str) -> OperationResult:
    """Build a generic ``connector_error`` result the dispatcher emits for a 4xx/5xx.

    Mirrors
    :func:`meho_backplane.operations._errors.result_connector_error`: the
    terse ``error`` summary plus the stringified ``httpx.HTTPStatusError``
    (status line + URL) stashed under ``extras["exception_message"]``.
    This is the exact shape a ``filter.datastores`` 400 (#1908) produces,
    where the status code + offending URL live only in the
    ``exception_message`` string (no structured ``http_status``).
    """
    return OperationResult(
        status="error",
        op_id=op_id,
        error="connector_error: HTTPStatusError",
        duration_ms=1.0,
        extras={
            "error_code": "connector_error",
            "exception_class": "HTTPStatusError",
            "exception_message": exception_message,
        },
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


@pytest.mark.asyncio
async def test_datastore_usage_vm_enrichment_is_best_effort_on_sub_op_error() -> None:
    """A failed VM-placement sub-op keeps the row; vm_count/vm_names null + note (#1908).

    The capacity/free/type read is load-bearing and has already succeeded
    by the time the optional ``filter.datastores`` VM lookup runs. When
    that lookup 400s (the version-skew filter param the issue hit), the
    datastore row is still returned -- capacity/free/type intact -- with
    ``vm_count``/``vm_names`` nulled and an ``enrichment_note`` recording
    why, rather than the whole composite failing.
    """
    sequence = [
        _ok_result(
            "GET:/vcenter/datastore",
            [
                {"datastore": "datastore-1", "name": "ds-1", "type": "VMFS"},
                {"datastore": "datastore-2", "name": "ds-2", "type": "NFS"},
            ],
        ),
        # datastore-1: detail OK, VM enrichment 400s -> best-effort skip.
        _ok_result("GET:/vcenter/datastore/{datastore}", {"capacity": 100, "free_space": 40}),
        _connector_error_result(
            "GET:/vcenter/vm",
            "Client error '400 Bad Request' for url "
            "'https://vc/api/vcenter/vm?filter.datastores=datastore-1'",
        ),
        # datastore-2: detail OK, VM enrichment OK -> fully enriched.
        _ok_result("GET:/vcenter/datastore/{datastore}", {"capacity": 500, "free_space": 250}),
        _ok_result("GET:/vcenter/vm", [{"name": "vm-c"}]),
    ]
    dispatch = _RecordingDispatchChild(sequence)
    out = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        dispatch_child=dispatch,
    )

    # All 5 sub-ops fired -- the failed VM leg did not short-circuit.
    assert len(dispatch.calls) == 5
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
    assert "enrichment_note" in ds1
    # The note bubbles the sub-op id, its status, the 400, and the URL.
    note = ds1["enrichment_note"]
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
async def test_datastore_usage_listing_error_bubbles_structured_detail() -> None:
    """A load-bearing sub-op failure bubbles the sub-op's status code + URL (#1908).

    Suggestion 2: the composite's failure envelope previously stopped at
    ``connector_error: HTTPStatusError``; the actual 400 + offending URL
    only showed on a manual replay. The raised
    :class:`CompositeSubOpError` now folds the sub-op's diagnostic line
    (status code + URL, from the stringified ``HTTPStatusError``) into its
    message and exposes the sub-op's structured fields as attributes.
    """
    failing = _connector_error_result(
        "GET:/vcenter/datastore",
        "Client error '400 Bad Request' for url "
        "'https://vc/api/vcenter/datastore?filter.names=bogus'",
    )
    dispatch = _RecordingDispatchChild([failing])
    with pytest.raises(CompositeSubOpError) as excinfo:
        await datastore_usage_composite(
            operator=_make_operator(),
            target=object(),
            params={},
            dispatch_child=dispatch,
        )
    # No per-datastore calls fire after the listing fails.
    assert len(dispatch.calls) == 1
    exc = excinfo.value
    # Backward-compatible substring (existing consumers string-match it).
    assert "returned status='error'" in str(exc)
    # The structured detail now rides the message.
    assert "400 Bad Request" in str(exc)
    assert "filter.names=bogus" in str(exc)
    # And the sub-op's structured fields are addressable as attributes.
    assert exc.op_id == "GET:/vcenter/datastore"
    assert exc.status == "error"
    assert exc.sub_op_extras["error_code"] == "connector_error"


@pytest.mark.asyncio
async def test_datastore_usage_bubbles_structured_http_status_when_present() -> None:
    """When a sub-op carried a structured ``http_status`` (403/422/auth), it bubbles too.

    The generic 4xx path (400/404/5xx) carries the status + URL only in
    ``exception_message``; the 403/422/401/440 builders instead extract a
    structured ``http_status`` + ``upstream_message``. The bubble-up
    prefers the structured form when it is present.
    """
    failing = OperationResult(
        status="error",
        op_id="GET:/vcenter/datastore",
        error="connector_http_403: the upstream returned HTTP 403 Forbidden ...",
        duration_ms=1.0,
        extras={
            "error_code": "connector_http_403",
            "http_status": 403,
            "upstream_message": "Resource not accessible by integration",
            "permission_headers": {},
        },
    )
    dispatch = _RecordingDispatchChild([failing])
    with pytest.raises(CompositeSubOpError) as excinfo:
        await datastore_usage_composite(
            operator=_make_operator(),
            target=object(),
            params={},
            dispatch_child=dispatch,
        )
    message = str(excinfo.value)
    assert "HTTP 403" in message
    assert "Resource not accessible by integration" in message


# ---------------------------------------------------------------------------
# vmware.composite.network.portgroup.audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_portgroup_audit_dispatches_three_phases() -> None:
    """DVS + portgroup listings + per-portgroup VM listings aggregate to the expected shape."""
    dvs_listing = [{"vds": "dvs-1", "name": "DVS-A"}]
    # Generic ``GET:/vcenter/network`` summaries: ``{network, name,
    # type}``. The summary carries no parent-DVS field, so ``dvs`` is
    # resolved best-effort and is ``None`` for the real REST shape. The
    # first entry carries a synthetic ``vds`` field to exercise the
    # best-effort enrichment path against the DVS index.
    pg_listing = [
        {"network": "pg-1", "name": "PG-A", "vds": "dvs-1", "type": "DISTRIBUTED_PORTGROUP"},
        {"network": "pg-2", "name": "PG-B", "type": "DISTRIBUTED_PORTGROUP"},
    ]
    vms_per_pg = {
        "pg-1": [{"name": "vm-pg1-a"}, {"name": "vm-pg1-b"}],
        "pg-2": [],
    }
    sequence: list[OperationResult] = [
        _ok_result("GET:/vcenter/network/distributed-switches", dvs_listing),
        _ok_result("GET:/vcenter/network", pg_listing),
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
    assert dispatch.calls[0]["op_id"] == "GET:/vcenter/network/distributed-switches"
    # Portgroups come from the generic network resource, type-filtered.
    assert dispatch.calls[1]["op_id"] == "GET:/vcenter/network"
    assert dispatch.calls[1]["params"] == {"filter.types": ["DISTRIBUTED_PORTGROUP"]}
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
            "type": "DISTRIBUTED_PORTGROUP",
            "vm_count": 2,
            "vm_names": ["vm-pg1-a", "vm-pg1-b"],
        },
        {
            # No ``vds`` on the generic Network summary -> dvs/dvs_name None.
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
    """``filter_dvs`` scopes the DVS listing; the portgroup call is type-only.

    The generic ``Network`` FilterSpec has no per-DVS filter, so
    ``filter_dvs`` cannot narrow the portgroup set server-side -- it
    flows only into the distributed-switches ``filter.vdses`` query,
    narrowing the DVS index (and thus the ``dvs_name`` enrichment).
    """
    sequence = [
        _ok_result("GET:/vcenter/network/distributed-switches", []),
        _ok_result("GET:/vcenter/network", []),
    ]
    dispatch = _RecordingDispatchChild(sequence)
    await network_portgroup_audit_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter_dvs": "dvs-prod"},
        dispatch_child=dispatch,
    )
    assert dispatch.calls[0]["params"] == {"filter.vdses": ["dvs-prod"]}
    # Portgroup call is type-filtered only -- no ``filter.vdses``.
    assert dispatch.calls[1]["params"] == {"filter.types": ["DISTRIBUTED_PORTGROUP"]}


@pytest.mark.asyncio
async def test_network_portgroup_audit_include_disconnected_drops_power_filter() -> None:
    """``include_disconnected_vms=True`` removes the ``POWERED_ON`` filter on the VM call."""
    sequence = [
        _ok_result("GET:/vcenter/network/distributed-switches", []),
        _ok_result(
            "GET:/vcenter/network",
            [{"network": "pg-1", "name": "PG-A", "type": "DISTRIBUTED_PORTGROUP"}],
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
# vmware.composite.host.network_uplinks
# ---------------------------------------------------------------------------


def _retrieve_result(pnics: list[Any], proxy_switches: list[Any]) -> dict[str, Any]:
    """Build a RetrievePropertiesEx RetrieveResult for one host.

    Mirrors the WS-API ``RetrieveResult`` shape: an ``objects`` list of
    ``ObjectContent``, each with a ``propSet`` of ``{name, val}`` pairs
    keyed on the requested property paths.
    """
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
    """Host listing + per-host RetrievePropertiesEx; aggregation matches the spec.

    ``config.network.pnic`` flattens to device / mac / driver / link
    state + speed; ``config.network.proxySwitch`` flattens to the DVS
    backing with its uplink pnic device names (recovered from the
    WS-API pnic keys).
    """
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
    sequence: list[OperationResult] = [_ok_result("GET:/vcenter/host", listing)]
    for entry in listing:
        sequence.append(
            _ok_result(
                "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
                props_by_host[entry["host"]],
            )
        )
    dispatch = _RecordingDispatchChild(sequence)

    out = await host_network_uplinks_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        dispatch_child=dispatch,
    )

    # 1 listing + 2 hosts * 1 property read = 3 calls.
    assert len(dispatch.calls) == 3
    assert dispatch.calls[0]["op_id"] == "GET:/vcenter/host"
    assert dispatch.calls[0]["params"] == {}
    # Per-host property read targets the propertyCollector singleton and
    # requests the two host-network config paths on the specific host.
    prop_call = dispatch.calls[1]
    assert prop_call["op_id"] == "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
    assert prop_call["params"]["moId"] == "propertyCollector"
    spec = prop_call["params"]["specSet"][0]
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
    # host-2: empty pnic/proxySwitch lists.
    assert hosts[1]["pnics"] == []
    assert hosts[1]["proxy_switches"] == []


@pytest.mark.asyncio
async def test_host_network_uplinks_filter_hosts_passes_through_to_listing() -> None:
    """``filter_hosts`` flows into the host listing as ``filter.hosts``."""
    dispatch = _RecordingDispatchChild([_ok_result("GET:/vcenter/host", [])])
    await host_network_uplinks_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter_hosts": ["host-9", "host-10"]},
        dispatch_child=dispatch,
    )
    assert dispatch.calls[0]["params"] == {"filter.hosts": ["host-9", "host-10"]}


@pytest.mark.asyncio
async def test_host_network_uplinks_property_read_is_best_effort_on_error() -> None:
    """A failed per-host property read keeps the row; pnics/proxy_switches null + note.

    The plain REST host listing has already identified the host by the
    time the vi-json property read runs, so when that read errors the
    host row is still returned -- pnics/proxy_switches nulled with a
    ``read_note`` -- rather than the whole composite failing.
    """
    listing = [
        {"host": "host-1", "name": "esx-1"},
        {"host": "host-2", "name": "esx-2"},
    ]
    sequence = [
        _ok_result("GET:/vcenter/host", listing),
        # host-1: property read 400s -> best-effort skip.
        _connector_error_result(
            "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
            "Client error '400 Bad Request' for url "
            "'https://vc/sdk/vim25/PropertyCollector/propertyCollector/RetrievePropertiesEx'",
        ),
        # host-2: property read OK.
        _ok_result(
            "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
            _retrieve_result(pnics=[], proxy_switches=[]),
        ),
    ]
    dispatch = _RecordingDispatchChild(sequence)
    out = await host_network_uplinks_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        dispatch_child=dispatch,
    )

    assert len(dispatch.calls) == 3
    rows = out["hosts"]
    assert len(rows) == 2
    h1 = rows[0]
    assert h1["id"] == "host-1"
    assert h1["pnics"] is None
    assert h1["proxy_switches"] is None
    assert "read_note" in h1
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
        _ok_result("GET:/vcenter/host", {"value": [{"host": "host-1", "name": "esx-1"}]}),
        _ok_result(
            "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
            {"value": _retrieve_result(pnics=[{"device": "vmnic0"}], proxy_switches=[])},
        ),
    ]
    dispatch = _RecordingDispatchChild(sequence)
    out = await host_network_uplinks_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        dispatch_child=dispatch,
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
        _ok_result(
            "GET:/vcenter/host",
            [
                {"host": "host-1", "name": "good"},
                {"name": "missing-id"},  # no ``host`` key
                "not-a-dict",
            ],
        ),
        _ok_result(
            "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
            _retrieve_result(pnics=[], proxy_switches=[]),
        ),
    ]
    dispatch = _RecordingDispatchChild(sequence)
    out = await host_network_uplinks_composite(
        operator=_make_operator(),
        target=object(),
        params={},
        dispatch_child=dispatch,
    )
    # Only the well-formed entry produces a property read + a result row.
    assert len(out["hosts"]) == 1
    assert out["hosts"][0]["id"] == "host-1"


@pytest.mark.asyncio
async def test_host_network_uplinks_raises_on_listing_error() -> None:
    """A failed host listing surfaces as ``RuntimeError``; no per-host reads fire."""
    dispatch = _RecordingDispatchChild([_err_result("GET:/vcenter/host", "permission denied")])
    with pytest.raises(RuntimeError, match="returned status='error'"):
        await host_network_uplinks_composite(
            operator=_make_operator(),
            target=object(),
            params={},
            dispatch_child=dispatch,
        )
    assert len(dispatch.calls) == 1


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
    """All six read handlers dispatch sub-ops against ``vmware-rest-9.0`` exclusively.

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
                "GET:/vcenter/network/distributed-switches": [],
                "GET:/vcenter/network": [],
            },
        ),
        (
            host_network_uplinks_composite,
            {},
            {"GET:/vcenter/host": []},
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
