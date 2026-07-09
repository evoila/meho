# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pre-existing 7-composite handler module
# (>1200 lines before #2251); this task only appends the direct-session
# opt-in documentation to the existing "four reasons" note. Splitting the
# module is I-B migration work (Initiative #2248/#2249), out of scope here.

"""Read-only ``vmware.composite.*`` handler functions (7 composites).

Each handler is a module-level ``async def`` that takes the dispatcher's
composite-branch keyword args ``(operator, target, params,
dispatch_child)`` and returns a single aggregated dict via 2-3 calls to
``dispatch_child`` (the
:class:`~meho_backplane.operations.composite.DispatchChild` callable
the dispatcher builds in
:func:`~meho_backplane.operations.dispatcher._run_source_kind_branch`).

Why module-level functions
--------------------------

:func:`~meho_backplane.operations.typed_register.derive_handler_ref`
rejects closures, ``functools.partial``, and lambdas at registration
time (``__qualname__`` containing ``<locals>``). Module-level
``async def`` is the only shape the dispatcher can resolve via
``importlib.import_module`` + chained ``getattr`` at first-dispatch
time.

Why ``dispatch_child`` not direct httpx
---------------------------------------

Composite handlers MUST route every sub-call through ``dispatch_child``
rather than calling the connector's ``_request_json`` directly, for
four load-bearing reasons (per #508's issue body + the
:class:`DispatchChild` Protocol docstring):

1. **Audit-tree linkage** -- ``dispatch_child`` binds
   :data:`~meho_backplane.operations._audit.parent_audit_id_var` to the
   composite parent's audit row, so every sub-op's audit row carries
   ``parent_audit_id`` automatically. Direct httpx breaks the chain.
2. **Bounded recursion** -- the
   :data:`~meho_backplane.operations.composite.composite_depth_var`
   contextvar enforces :attr:`Settings.composite_max_depth`. A
   misbehaving handler that recursed into another composite would be
   caught here, not at request-volume scale.
3. **Policy + broadcast** -- the dispatcher's policy gate (G2.x) and
   broadcast publish (G6.x) run on every dispatched sub-op. Direct
   httpx evades both.
4. **Param validation** -- each sub-op's ``parameter_schema`` validates
   inbound params at dispatch time; direct httpx skips validation.

Direct-session opt-in (the substrate, #2251)
--------------------------------------------

The composite handler contract also lets a handler receive the
resolved connector instance directly, by declaring a ``connector``
parameter alongside (or instead of) ``dispatch_child``. The dispatcher
forwards the instance it already resolved for the composite's target
(:func:`~meho_backplane.operations.dispatcher._resolve_connector_instance`);
:func:`~meho_backplane.operations._branches.dispatch_composite` passes
it only when the handler declares the parameter, so the existing
``dispatch_child``-only handlers below are unchanged. A handler that
opts in issues its sub-calls through the connector's own session
(``connector._get_json`` / ``connector._post_json`` +
``connector.mount_op_path``) with **no** ``endpoint_descriptor``
lookup.

This is the substrate the I-B migrations (Initiative #2248) build on;
no composite in this module is migrated yet. Taking the direct path
deliberately drops two of the four ``dispatch_child`` guarantees and
relocates the other two:

* **(2) Bounded recursion is moot** -- a direct session call cannot
  re-enter the dispatcher, so there is no recursion to bound.
* **(4) Per-sub-op param validation goes away** -- for a
  code-constructed request body this is the point, not a loss:
  re-validating a hand-built vmomi body against a persisted spec
  schema is the schema-drift defect the two-world model (Goal #2247)
  exists to remove.
* **(1) Audit-tree linkage** collapses to the top-level op's own
  audit row (the row a forensic query reads anyway); the per-sub-op
  child rows disappear.
* **(3) Per-sub-op policy-gate + broadcast is evaded** -- acceptable
  for **read** composites (the top-level op is already gated), but
  **load-bearing for write** composites whose sub-ops may be
  approval-gated. A write composite on the direct path re-applies the
  gate per governed sub-call through the reusable seam
  :func:`~meho_backplane.operations.composite.enforce_subop_policy`
  (Task #2254): the handler calls it before each direct write sub-call
  with the sub-op's declared ``safety_level`` / ``requires_approval``,
  and returns the seam's ``awaiting_approval`` / ``denied``
  :class:`OperationResult` verbatim when the gate does not clear -- so
  an approval-gated sub-op still queues instead of executing. The
  curated composite's own top-level ``requires_approval`` remains the
  primary governing decision (Initiative #2249); the seam guarantees no
  internal write drops below the governance it had under
  ``dispatch_child``.

Op_id contract for sub-ops
--------------------------

The sub-op ``op_id`` strings used below are the canonical
``METHOD:/path`` keys that the ingest path (:func:`parse_openapi`)
generates from ``vcenter.yaml`` + ``vi-json.yaml`` -- e.g.
``"GET:/vcenter/datastore"``, ``"POST:/EventManager/{moId}/QueryEvents"``.
These mirror the rows the G0.7 canary asserts on
(``tests/acceptance/test_g07_vsphere_canary.py``'s
``GOVC_PARITY_BENCHMARK`` tuple); the canary is the de-facto registry
of canonical op_ids.

Response envelope handling
--------------------------

The vSphere REST surface returns JSON shapes that vary by endpoint:

* vSphere 7+ REST: bare arrays / objects (``[{"datastore": ...}, ...]``).
* Pre-7 REST: ``{"value": [...]}`` envelopes.
* vi-json: bare arrays / objects.

The composite handlers tolerate both via :func:`_unwrap_value` so they
work uniformly against modern vCenter and vcsim simulator targets. The
helper is intentionally permissive -- composite tests stub responses
in either shape; production sub-op responses pass through the
``HttpConnector._request_json`` decoder which preserves the upstream
shape verbatim.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._preflight import (
    preflight_l2_dependencies,
)
from meho_backplane.operations.composite import DispatchChild

__all__ = [
    "CompositeSubOpError",
    "cluster_drs_recommendations_composite",
    "datastore_usage_composite",
    "event_tail_composite",
    "host_network_uplinks_composite",
    "host_vsan_health_composite",
    "network_portgroup_audit_composite",
    "performance_summary_composite",
]


# Sub-op ids the handlers dispatch through. Centralised so the
# registration tests can assert against the same constants without
# re-spelling the paths.
_CONNECTOR_ID = "vmware-rest-9.0"
_OP_GET_CLUSTER = "GET:/vcenter/cluster/{cluster}"
_OP_GET_CLUSTER_DRS = "GET:/vcenter/cluster/{cluster}/drs"
_OP_POST_QUERY_EVENTS = "POST:/EventManager/{moId}/QueryEvents"
_OP_POST_QUERY_AVAILABLE_PERF_METRIC = "POST:/PerformanceManager/{moId}/QueryAvailablePerfMetric"
_OP_POST_QUERY_PERF = "POST:/PerformanceManager/{moId}/QueryPerf"
_OP_LIST_DATASTORES = "GET:/vcenter/datastore"
_OP_GET_DATASTORE = "GET:/vcenter/datastore/{datastore}"
_OP_LIST_VMS = "GET:/vcenter/vm"
# vSphere Automation REST keys the distributed-switch listing under the
# *plural* resource path (a preview feature on the appliance-served
# ``vcenter.yaml``); the singular ``distributed-switch`` spelling that
# G3.1-T5 #508 shipped does not exist in the spec and never resolved
# against a real ingest (#1602). The DVS-list response carries a
# ``vds``/``distributed_switch`` moid per entry, which drives the
# ``dvs_index`` enrichment below.
_OP_LIST_DVS = "GET:/vcenter/network/distributed-switches"
# There is NO dedicated ``distributed-portgroup(s)`` list resource in
# the REST Automation API: distributed portgroups are enumerated via the
# generic network resource filtered to ``DISTRIBUTED_PORTGROUP`` (the
# singular ``distributed-portgroup`` op_id #508 declared was absent from
# every ingest -- #1602). The generic ``Network`` summary returns only
# ``{network (id), name, type}`` -- it carries no parent-DVS field, so
# the per-portgroup ``dvs``/``dvs_name`` enrichment is best-effort (see
# the handler note).
_OP_LIST_NETWORK = "GET:/vcenter/network"
_NETWORK_TYPE_DISTRIBUTED_PORTGROUP = "DISTRIBUTED_PORTGROUP"
# Host listing (vCenter Automation REST) + per-host property read
# (vi-json). The pnic link-state / uplink mapping the operator wants
# lives on the Web-Services-API ``HostSystem.config.network`` object,
# not the plain REST host summary -- so the composite lists hosts via
# the REST resource and then reads ``config.network.pnic`` +
# ``config.network.proxySwitch`` per host through the PropertyCollector
# ``RetrievePropertiesEx`` vi-json method (moId ``propertyCollector``,
# the canonical singleton).
_OP_LIST_HOSTS = "GET:/vcenter/host"
_OP_RETRIEVE_PROPERTIES = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
_PROPERTY_COLLECTOR_MOID = "propertyCollector"
_HOST_SYSTEM_MO_TYPE = "HostSystem"
_HOST_NET_PROP_PNIC = "config.network.pnic"
_HOST_NET_PROP_PROXYSWITCH = "config.network.proxySwitch"
# vSAN health is a health-service-only read: the plain vSphere
# Automation REST surface exposes no vSAN health resource. It is served
# by the dedicated ``/vsanHealth`` vmomi endpoint, whose
# ``VsanVcClusterHealthSystem`` managed object (the singleton moId
# ``vsan-cluster-health-system``) answers
# ``VsanQueryVcClusterHealthSummary`` at cluster grain -- the ``govc
# vsan.health.*`` equivalent. The method takes the target cluster's
# MoRef and returns a ``VsanClusterHealthSummary`` carrying an
# ``overallHealth`` colour plus a ``groups`` list of health-test groups
# (each group -> ``groupTests`` list of individual checks).
_OP_VSAN_QUERY_HEALTH_SUMMARY = (
    "POST:/VsanVcClusterHealthSystem/{moId}/VsanQueryVcClusterHealthSummary"
)
_VSAN_CLUSTER_HEALTH_SYSTEM_MOID = "vsan-cluster-health-system"
_CLUSTER_COMPUTE_RESOURCE_MO_TYPE = "ClusterComputeResource"

# Composite op_ids -- used by the preflight cache key. Centralised here
# so the test-side coverage assertion (every registered composite has
# both a sub-op_id tuple and a preflight-cache-key constant) can read
# them by name rather than re-spelling.
_COMPOSITE_OP_ID_CLUSTER_DRS_RECS = "vmware.composite.cluster.drs_recommendations"
_COMPOSITE_OP_ID_EVENT_TAIL = "vmware.composite.event.tail"
_COMPOSITE_OP_ID_PERFORMANCE_SUMMARY = "vmware.composite.performance.summary"
_COMPOSITE_OP_ID_DATASTORE_USAGE = "vmware.composite.datastore.usage"
_COMPOSITE_OP_ID_NETWORK_PORTGROUP_AUDIT = "vmware.composite.network.portgroup.audit"
_COMPOSITE_OP_ID_HOST_NETWORK_UPLINKS = "vmware.composite.host.network_uplinks"
_COMPOSITE_OP_ID_HOST_VSAN_HEALTH = "vmware.composite.host.vsan_health"

# Per-composite sub-op-id tuples consumed by the L2 pre-flight check
# (G0.14-T10 / #1151). Each tuple lists the L2 raw-REST sub-ops the
# composite dispatches against; the pre-flight helper walks them
# against ``endpoint_descriptor`` before any ``dispatch_child`` call so
# a missing-L2 deployment surfaces as a structured
# ``composite_l2_missing`` error rather than a mid-flight ``unknown_op``
# from a sub-op call. See ``_preflight.py`` for the design rationale
# (Option B / lazy pre-resolve).
_SUB_OPS_CLUSTER_DRS_RECS: tuple[str, ...] = (
    _OP_GET_CLUSTER,
    _OP_GET_CLUSTER_DRS,
)
_SUB_OPS_EVENT_TAIL: tuple[str, ...] = (_OP_POST_QUERY_EVENTS,)
_SUB_OPS_PERFORMANCE_SUMMARY: tuple[str, ...] = (
    _OP_POST_QUERY_AVAILABLE_PERF_METRIC,
    _OP_POST_QUERY_PERF,
)
_SUB_OPS_DATASTORE_USAGE: tuple[str, ...] = (
    _OP_LIST_DATASTORES,
    _OP_GET_DATASTORE,
    _OP_LIST_VMS,
)
_SUB_OPS_NETWORK_PORTGROUP_AUDIT: tuple[str, ...] = (
    _OP_LIST_DVS,
    _OP_LIST_NETWORK,
    _OP_LIST_VMS,
)
_SUB_OPS_HOST_NETWORK_UPLINKS: tuple[str, ...] = (
    _OP_LIST_HOSTS,
    _OP_RETRIEVE_PROPERTIES,
)
_SUB_OPS_HOST_VSAN_HEALTH: tuple[str, ...] = (_OP_VSAN_QUERY_HEALTH_SUMMARY,)


def _unwrap_value(payload: Any) -> Any:
    """Return the inner ``value`` field on a pre-7 envelope, else *payload*.

    vSphere's REST API straddles two response shapes:

    * Modern (7.0+): bare arrays / objects (``[{...}, {...}]``).
    * Legacy (pre-7, plus some vcsim builds): wraps the body in
      ``{"value": [...]}``.

    Composite handlers don't care which shape they receive -- the
    underlying typed sub-ops are the same. The unwrap is purely a
    parser-side ergonomic.
    """
    if isinstance(payload, dict) and set(payload.keys()) == {"value"}:
        return payload["value"]
    return payload


def _describe_sub_op_failure(result: OperationResult) -> str:
    """Render the most diagnostic line a failed sub-op result carries.

    The dispatcher's structured-error builders (``operations/_errors.py``)
    put different fields on a sub-op's ``extras`` depending on the failure
    class:

    * An upstream ``403`` / ``422`` / ``401`` / ``440`` lands a structured
      ``http_status`` plus the upstream's own ``upstream_message`` -- the
      single most useful diagnostic line.
    * Every other upstream status (``400``, ``404``, ``5xx`` ...) falls
      through to the generic ``connector_error`` builder, which keeps the
      stringified ``httpx.HTTPStatusError`` -- ``"Client error '400 Bad
      Request' for url 'https://.../api/vcenter/vm?filter.datastores=...'"``
      -- under ``exception_message``. That string already carries the
      status code *and* the offending URL, so surfacing it is what the
      ``filter.datastores`` 400 (#1908) needed.

    Prefer the structured ``http_status`` + ``upstream_message`` when the
    builder extracted them; otherwise fall back to the capped
    ``exception_message``; otherwise the bare ``error`` summary. The detail
    is appended to ``error`` only when it adds information beyond it.
    """
    extras = result.extras
    http_status = extras.get("http_status")
    upstream_message = extras.get("upstream_message")
    detail = extras.get("exception_message")
    parts: list[str] = []
    if result.error:
        parts.append(result.error)
    if http_status is not None:
        status_clause = f"HTTP {http_status}"
        if isinstance(upstream_message, str) and upstream_message.strip():
            status_clause = f"{status_clause}: {upstream_message}"
        parts.append(status_clause)
    elif isinstance(detail, str) and detail.strip() and detail != result.error:
        parts.append(detail)
    if not parts:
        parts.append("<no error message>")
    return " -- ".join(parts)


class CompositeSubOpError(RuntimeError):
    """A composite sub-op returned a non-OK :class:`OperationResult`.

    Raised by :func:`_require_ok` when a *load-bearing* sub-op fails (the
    datastore listing, a per-datastore detail read, an event/perf query).
    The dispatcher's outer exception branch wraps the raised exception
    into a ``connector_error`` :class:`OperationResult` for the composite
    parent (``operations/_errors.py::result_connector_error``), which
    records ``type(exc).__name__`` and the capped ``str(exc)`` under the
    parent's ``extras``.

    The pre-#1908 shape raised a bare :class:`RuntimeError` whose message
    stopped at ``status='error'`` plus the sub-op's terse ``error``
    summary (``connector_error: HTTPStatusError``) -- the actual status
    code and offending URL only showed when the operator replayed the
    sub-op by hand. This class threads the sub-op's structured failure
    (``op_id`` / ``status`` / ``error`` / ``extras``) through as
    attributes *and* folds the most diagnostic line
    (:func:`_describe_sub_op_failure` -- a structured ``http_status`` +
    upstream message, or the stringified ``HTTPStatusError`` carrying the
    status + URL) into ``str(self)`` so it lands in the composite parent's
    ``extras["exception_message"]`` rather than being lost.

    The ``returned status='<status>'`` clause is preserved verbatim so
    existing consumers that string-match it keep working.
    """

    def __init__(self, result: OperationResult) -> None:
        self.op_id = result.op_id
        self.status = result.status
        self.sub_op_error = result.error
        self.sub_op_extras = dict(result.extras)
        super().__init__(
            f"composite sub-op {result.op_id!r} returned status="
            f"{result.status!r}: {_describe_sub_op_failure(result)}"
        )


def _require_ok(result: OperationResult) -> Any:
    """Return :attr:`OperationResult.result` or raise on a non-OK status.

    The composite handlers fail loudly when a *load-bearing* sub-op errors
    -- a swallowed error would silently produce a malformed aggregation.
    The dispatcher's outer exception branch wraps the raised
    :class:`CompositeSubOpError` into a ``connector_error``
    :class:`OperationResult` for the composite parent, surfacing the
    underlying sub-op's failure (status code + URL where the sub-op
    carried them) on ``extras["exception_message"]``.

    Optional enrichment legs (e.g. the per-datastore VM-placement lookup
    in :func:`datastore_usage_composite`) must NOT route through this
    helper -- they degrade best-effort instead of sinking the whole
    composite.
    """
    if result.status != "ok":
        raise CompositeSubOpError(result)
    return result.result


async def cluster_drs_recommendations_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Read cluster summary + DRS state in one composite call.

    Op-id: ``vmware.composite.cluster.drs_recommendations``.

    Sub-ops dispatched (sequential):

    1. ``GET:/vcenter/cluster/{cluster}`` -- cluster summary (name,
       resource pool, default host, DRS-enabled flag).
    2. ``GET:/vcenter/cluster/{cluster}/drs`` -- DRS configuration
       (enabled, automation level, migration threshold).

    Returns
    -------
    dict[str, Any]
        ``{"cluster": <summary dict>, "drs": <drs config dict>,
        "recommendations_history": <optional list>}``. The
        ``recommendations_history`` key appears only when the operator
        sets ``include_recommendations_history=True``; otherwise it is
        omitted.

    The ``include_recommendations_history`` flag is a placeholder for
    a future Task that adds a third sub-op (DRS recommendations list).
    The vSphere REST surface does not expose a stable
    "recommendations" endpoint in 9.0; the issue body's "DRS state read
    + performance read + format" hint is satisfied by reading the
    cluster summary plus the DRS config -- the format/aggregation is
    what differentiates the composite from a raw GET.
    """
    await preflight_l2_dependencies(
        composite_op_id=_COMPOSITE_OP_ID_CLUSTER_DRS_RECS,
        sub_op_ids=_SUB_OPS_CLUSTER_DRS_RECS,
        connector_id=_CONNECTOR_ID,
        tenant_id=operator.tenant_id,
    )
    cluster_moid = params["cluster"]
    include_history = bool(params.get("include_recommendations_history", False))

    cluster_result = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_GET_CLUSTER,
            params={"cluster": cluster_moid},
        )
    )
    drs_result = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_GET_CLUSTER_DRS,
            params={"cluster": cluster_moid},
        )
    )
    out: dict[str, Any] = {
        "cluster": _unwrap_value(cluster_result),
        "drs": _unwrap_value(drs_result),
    }
    if include_history:
        # Surface the history slice from the DRS payload when present.
        # vSphere 9.0 returns ``{"drs_config": ..., "history": [...]}``;
        # the key is absent on legacy targets. Empty list rather than
        # None keeps the operator-visible shape stable.
        drs_payload = out["drs"]
        history = drs_payload.get("history", []) if isinstance(drs_payload, dict) else []
        # Guard against non-list ``history`` values (e.g. a target that
        # returns the field as a scalar / dict). ``list(history)`` would
        # iterate keys on a dict or fail on a scalar; the contract is
        # "always a list when surfaced", so coerce to an empty list when
        # the payload disagrees.
        out["recommendations_history"] = history if isinstance(history, list) else []
    return out


async def event_tail_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Tail recent events via EventManager.QueryEvents (vi-json).

    Op-id: ``vmware.composite.event.tail``.

    Sub-ops dispatched (sequential, single call):

    1. ``POST:/EventManager/{moId}/QueryEvents`` -- recent events. The
       vi-json call returns an array of event dicts; the handler caps
       the array client-side to ``max_events`` (default 100).

    Returns
    -------
    dict[str, Any]
        ``{"events": <list[event dict]>, "count": <int>,
        "moId": <str>, "max_events_applied": <int>}``. ``count`` is
        the post-cap length so operators can detect truncation.
    """
    await preflight_l2_dependencies(
        composite_op_id=_COMPOSITE_OP_ID_EVENT_TAIL,
        sub_op_ids=_SUB_OPS_EVENT_TAIL,
        connector_id=_CONNECTOR_ID,
        tenant_id=operator.tenant_id,
    )
    mo_id = params.get("moId", "EventManager")
    max_events = int(params.get("max_events", 100))
    raw = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_POST_QUERY_EVENTS,
            params={"moId": mo_id},
        )
    )
    events = _unwrap_value(raw)
    if not isinstance(events, list):
        # vi-json QueryEvents always returns a list. A non-list payload
        # is a connector-side bug -- surface it to the caller rather
        # than guess at the shape.
        raise RuntimeError(
            f"event_tail: expected list from {_OP_POST_QUERY_EVENTS!r}, got {type(events).__name__}"
        )
    capped = events[:max_events]
    return {
        "events": capped,
        "count": len(capped),
        "moId": mo_id,
        "max_events_applied": max_events,
    }


async def performance_summary_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Summarise performance metrics for one entity via PerformanceManager (vi-json).

    Op-id: ``vmware.composite.performance.summary``.

    Sub-ops dispatched (sequential):

    1. ``POST:/PerformanceManager/{moId}/QueryAvailablePerfMetric`` --
       discover available counter IDs for the target entity.
    2. ``POST:/PerformanceManager/{moId}/QueryPerf`` -- fetch sample
       values for those counters.

    Returns
    -------
    dict[str, Any]
        ``{"entity_moid": <str>, "perf_manager_moid": <str>,
        "available_counters": <list>, "samples": <list>,
        "interval_seconds": <int>, "max_samples_applied": <int>}``.

    The handler does not pre-filter counters in v0.2; the entire
    available-counter list is forwarded to QueryPerf so the operator
    gets a complete snapshot. A counter-curation flag (e.g.
    ``counter_ids``) is an explicit v0.2.next concern per the issue
    body's *Out of scope* section.
    """
    await preflight_l2_dependencies(
        composite_op_id=_COMPOSITE_OP_ID_PERFORMANCE_SUMMARY,
        sub_op_ids=_SUB_OPS_PERFORMANCE_SUMMARY,
        connector_id=_CONNECTOR_ID,
        tenant_id=operator.tenant_id,
    )
    entity_moid = params["entity_moid"]
    perf_mgr_moid = params.get("perf_manager_moid", "PerfMgr")
    interval_s = int(params.get("interval_seconds", 20))
    max_samples = int(params.get("max_samples", 60))

    available_raw = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_POST_QUERY_AVAILABLE_PERF_METRIC,
            params={"moId": perf_mgr_moid, "entity": entity_moid},
        )
    )
    available = _unwrap_value(available_raw)
    if not isinstance(available, list):
        raise RuntimeError(
            "performance_summary: expected list from "
            f"{_OP_POST_QUERY_AVAILABLE_PERF_METRIC!r}, "
            f"got {type(available).__name__}"
        )

    samples_raw = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_POST_QUERY_PERF,
            params={
                "moId": perf_mgr_moid,
                "entity": entity_moid,
                "interval_seconds": interval_s,
            },
        )
    )
    samples = _unwrap_value(samples_raw)
    if not isinstance(samples, list):
        raise RuntimeError(
            "performance_summary: expected list from "
            f"{_OP_POST_QUERY_PERF!r}, got {type(samples).__name__}"
        )
    capped = samples[:max_samples]
    return {
        "entity_moid": entity_moid,
        "perf_manager_moid": perf_mgr_moid,
        "available_counters": available,
        "samples": capped,
        "interval_seconds": interval_s,
        "max_samples_applied": max_samples,
    }


# Pre-existing >100-line handler from G3.1-T5 #508; G0.14-T10 #1151
# added a 6-line pre-flight call at the top and G0.27 #1908 made the
# per-datastore VM-placement leg best-effort -- both extend an already
# block-sized handler. Refactor (e.g. extracting the per-datastore row
# builder) is out of scope here.
# code-quality-allow: pre-existing G3.1-T5 #508 handler; #1908 best-effort enrichment only
async def datastore_usage_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """List datastores with capacity + free + VM placement summary.

    Op-id: ``vmware.composite.datastore.usage``.

    Sub-ops dispatched (per-datastore, sequential):

    1. ``GET:/vcenter/datastore`` -- list every datastore (optionally
       narrowed via ``filter.names``).
    2. For each datastore:
       a. ``GET:/vcenter/datastore/{datastore}`` -- detailed capacity /
          free / type / accessible flag (load-bearing: a failure here
          sinks the composite).
       b. ``GET:/vcenter/vm`` with ``filter.datastores`` -- VMs whose
          working directory sits on this datastore. Drives the
          ``vm_count`` + ``vm_names`` aggregation. This leg is
          **best-effort** (#1908): the capacity/free/type read -- the
          data the "which datastores are filling up?" use case needs --
          is already done by the time it runs, so when the VM lookup
          errors (e.g. a vCenter that rejects the ``filter.datastores``
          query with a 400) the row is still returned with
          ``vm_count`` / ``vm_names`` set to ``null`` and an
          ``enrichment_note`` recording why, rather than failing the
          whole composite.

    Sequential dispatch is intentional: each datastore's detail call
    inherits the prior call's authentication state, and the audit
    chain reads cleanly as ``listing -> detail(ds1) -> vms(ds1) ->
    detail(ds2) -> vms(ds2) -> ...``. A future v0.2.next optimisation
    could ``asyncio.gather`` the per-datastore detail + VM calls
    pairwise, but the simpler shape is easier to audit-trace through
    operator UIs.

    Returns
    -------
    dict[str, Any]
        ``{"datastores": [{"id": ..., "name": ..., "type": ...,
        "capacity": ..., "free_space": ..., "vm_count": ...,
        "vm_names": [...]}, ...]}``. The ``capacity`` / ``free_space``
        fields may be ``None`` if the upstream payload omits them
        (e.g. a partially-mounted datastore). When the per-datastore
        VM-placement enrichment errors, ``vm_count`` and ``vm_names``
        are ``None`` and the row carries an ``enrichment_note`` string
        describing the skipped enrichment; on success the row has no
        ``enrichment_note`` key.
    """
    await preflight_l2_dependencies(
        composite_op_id=_COMPOSITE_OP_ID_DATASTORE_USAGE,
        sub_op_ids=_SUB_OPS_DATASTORE_USAGE,
        connector_id=_CONNECTOR_ID,
        tenant_id=operator.tenant_id,
    )
    filter_names: list[str] = list(params.get("filter_names") or [])

    listing_params: dict[str, Any] = {}
    if filter_names:
        listing_params["filter.names"] = filter_names

    listing = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_DATASTORES,
            params=listing_params,
        )
    )
    entries = _unwrap_value(listing)
    if not isinstance(entries, list):
        raise RuntimeError(
            f"datastore_usage: expected list from {_OP_LIST_DATASTORES!r}, "
            f"got {type(entries).__name__}"
        )

    aggregated: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ds_id = entry.get("datastore")
        if not isinstance(ds_id, str):
            # vSphere REST always returns the moid under the key
            # ``datastore``; absence is an upstream malformation. Skip
            # silently rather than abort the aggregation.
            continue
        detail = _require_ok(
            await dispatch_child(
                connector_id=_CONNECTOR_ID,
                op_id=_OP_GET_DATASTORE,
                params={"datastore": ds_id},
            )
        )
        detail_payload = _unwrap_value(detail)
        detail_capacity = (
            detail_payload.get("capacity") if isinstance(detail_payload, dict) else None
        )
        detail_free_space = (
            detail_payload.get("free_space") if isinstance(detail_payload, dict) else None
        )
        # The per-datastore detail ``Datastore.Info`` is the primary source,
        # but some vCenter builds (observed on 8.0.3 against the 9.0 spec,
        # #2078) return a detail payload that omits/nulls ``capacity`` while
        # still populating ``free_space``. The ``GET:/vcenter/datastore`` list
        # row already carries both fields, so fall back to it when the detail
        # value is absent -- otherwise the composite silently discards a
        # capacity it already fetched, leaving %-full uncomputable.
        capacity = detail_capacity if detail_capacity is not None else entry.get("capacity")
        free_space = detail_free_space if detail_free_space is not None else entry.get("free_space")
        row: dict[str, Any] = {
            "id": ds_id,
            "name": entry.get("name"),
            "type": entry.get("type"),
            "capacity": capacity,
            "free_space": free_space,
        }

        # VM-placement enrichment is best-effort (#1908). The
        # capacity/free/type read above already satisfies the
        # storage-usage use case, so a failure on the optional VM lookup
        # (e.g. a vCenter that 400s the ``filter.datastores`` query)
        # nulls vm_count/vm_names and records why, rather than raising
        # through ``_require_ok`` and sinking every datastore row.
        vms_result = await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_VMS,
            params={"filter.datastores": [ds_id]},
        )
        if vms_result.status == "ok":
            vm_entries = _unwrap_value(vms_result.result)
            if not isinstance(vm_entries, list):
                vm_entries = []
            vm_names = [
                v["name"]
                for v in vm_entries
                if isinstance(v, dict) and isinstance(v.get("name"), str)
            ]
            row["vm_count"] = len(vm_names)
            row["vm_names"] = vm_names
        else:
            row["vm_count"] = None
            row["vm_names"] = None
            row["enrichment_note"] = (
                f"vm-placement enrichment skipped: sub-op {_OP_LIST_VMS!r} "
                f"returned status={vms_result.status!r}: "
                f"{_describe_sub_op_failure(vms_result)}"
            )
        aggregated.append(row)
    return {"datastores": aggregated}


# Pre-existing >100-line handler from G3.1-T5 #508; G0.14-T10 #1151
# added a 6-line pre-flight call at the top, pushing the diff-only
# checker into block territory. Refactor is out of scope for T10
# (the L2-dependency strategy).
# code-quality-allow: pre-existing G3.1-T5 #508 handler, T10 added preflight only
async def network_portgroup_audit_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Audit distributed portgroups with parent DVS + connected-VM aggregation.

    Op-id: ``vmware.composite.network.portgroup.audit``.

    Sub-ops dispatched:

    1. ``GET:/vcenter/network/distributed-switches`` -- list DVS
       entries (filtered to ``filter_dvs`` via the resource's
       ``filter.vdses`` query when supplied). Drives the DVS index
       used to enrich each portgroup with its switch name.
    2. ``GET:/vcenter/network`` with ``filter.types=[DISTRIBUTED_PORTGROUP]``
       -- list distributed portgroups. The REST Automation API has no
       dedicated distributed-portgroup resource; portgroups are
       enumerated through the generic ``Network`` resource filtered to
       the ``DISTRIBUTED_PORTGROUP`` type (#1602). ``filter_dvs`` is
       *not* applied here -- the generic ``Network`` FilterSpec exposes
       ``types``/``names``/``networks``/``datacenters``/``folders`` but
       no per-DVS filter, so DVS scoping narrows the index (and thus the
       enriched ``dvs_name``) rather than the portgroup set.
    3. Per portgroup: ``GET:/vcenter/vm`` with ``filter.networks`` --
       VMs connected to the portgroup. Drives the ``vm_count`` +
       ``vm_names`` aggregation.

    Returns
    -------
    dict[str, Any]
        ``{"portgroups": [{"id": ..., "name": ..., "dvs": <id|None>,
        "dvs_name": <str|None>, "type": ..., "vm_count": ...,
        "vm_names": [...]}, ...]}``.

    The generic ``Network`` summary carries only ``{network (id), name,
    type}`` -- it has no parent-DVS reference -- so ``dvs``/``dvs_name``
    are best-effort: populated when the upstream payload happens to
    expose a ``vds``/``distributed_switch`` field (e.g. a richer target
    or a future spec revision), ``None`` otherwise.
    """
    await preflight_l2_dependencies(
        composite_op_id=_COMPOSITE_OP_ID_NETWORK_PORTGROUP_AUDIT,
        sub_op_ids=_SUB_OPS_NETWORK_PORTGROUP_AUDIT,
        connector_id=_CONNECTOR_ID,
        tenant_id=operator.tenant_id,
    )
    filter_dvs = params.get("filter_dvs")
    include_disconnected = bool(params.get("include_disconnected_vms", False))

    dvs_params: dict[str, Any] = {}
    if isinstance(filter_dvs, str):
        dvs_params["filter.vdses"] = [filter_dvs]

    dvs_listing = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_DVS,
            params=dvs_params,
        )
    )
    dvs_entries = _unwrap_value(dvs_listing)
    if not isinstance(dvs_entries, list):
        dvs_entries = []
    # Build a moid->name lookup so the per-portgroup row carries the
    # DVS name in addition to its id.
    dvs_index: dict[str, str | None] = {}
    for entry in dvs_entries:
        if not isinstance(entry, dict):
            continue
        dvs_id = entry.get("vds") or entry.get("distributed_switch")
        if isinstance(dvs_id, str):
            name = entry.get("name") if isinstance(entry.get("name"), str) else None
            dvs_index[dvs_id] = name

    # Distributed portgroups come from the generic network resource
    # filtered to the DISTRIBUTED_PORTGROUP type -- there is no
    # standalone distributed-portgroup list endpoint. ``filter_dvs`` has
    # no analogue on this FilterSpec, so it is deliberately not threaded
    # in here (it scopes the DVS index above instead).
    pg_params: dict[str, Any] = {"filter.types": [_NETWORK_TYPE_DISTRIBUTED_PORTGROUP]}

    pg_listing = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_NETWORK,
            params=pg_params,
        )
    )
    pg_entries = _unwrap_value(pg_listing)
    if not isinstance(pg_entries, list):
        raise RuntimeError(
            f"network_portgroup_audit: expected list from {_OP_LIST_NETWORK!r}, "
            f"got {type(pg_entries).__name__}"
        )

    aggregated: list[dict[str, Any]] = []
    for entry in pg_entries:
        if not isinstance(entry, dict):
            continue
        pg_id = entry.get("network") or entry.get("portgroup")
        if not isinstance(pg_id, str):
            continue
        vm_params: dict[str, Any] = {"filter.networks": [pg_id]}
        if not include_disconnected:
            # vSphere REST accepts a power-state filter; the
            # ``include_disconnected`` flag toggles it. Default is
            # active VMs only.
            vm_params["filter.power_states"] = ["POWERED_ON"]
        vms = _require_ok(
            await dispatch_child(
                connector_id=_CONNECTOR_ID,
                op_id=_OP_LIST_VMS,
                params=vm_params,
            )
        )
        vm_entries = _unwrap_value(vms)
        if not isinstance(vm_entries, list):
            vm_entries = []
        vm_names = [
            v["name"] for v in vm_entries if isinstance(v, dict) and isinstance(v.get("name"), str)
        ]
        dvs_ref = entry.get("vds") or entry.get("distributed_switch")
        dvs_ref_str = dvs_ref if isinstance(dvs_ref, str) else None
        aggregated.append(
            {
                "id": pg_id,
                "name": entry.get("name"),
                "dvs": dvs_ref_str,
                "dvs_name": dvs_index.get(dvs_ref_str) if dvs_ref_str else None,
                "type": entry.get("type"),
                "vm_count": len(vm_names),
                "vm_names": vm_names,
            }
        )
    return {"portgroups": aggregated}


def _pnic_device_from_key(pnic_key: str) -> str:
    """Recover the device name from a WS-API physical-NIC key.

    ``HostProxySwitch.pnic`` lists the uplink physical NICs as their
    WS-API keys (``key-vim.host.PhysicalNic-vmnic3``), not their device
    names. The device name is the trailing segment after the last
    ``-``; when the string doesn't carry that shape (e.g. a bare device
    name from a simulator) it is returned verbatim.
    """
    _, _, tail = pnic_key.rpartition("-")
    return tail or pnic_key


def _parse_pnic(pnic: dict[str, Any]) -> dict[str, Any]:
    """Flatten one WS-API ``PhysicalNic`` into the operator-facing row.

    ``linkSpeed`` (a ``PhysicalNicLinkInfo``) is present only when the
    link is up -- the API omits it on a down link -- so its presence is
    the link-state signal (``link_up``), and its ``speedMb`` / ``duplex``
    fields carry the speed. Absent link -> ``speed_mb`` / ``duplex``
    are ``None``.
    """
    link_speed = pnic.get("linkSpeed")
    link_up = isinstance(link_speed, dict)
    speed_mb = link_speed.get("speedMb") if isinstance(link_speed, dict) else None
    duplex = link_speed.get("duplex") if isinstance(link_speed, dict) else None
    return {
        "device": pnic.get("device"),
        "mac": pnic.get("mac"),
        "driver": pnic.get("driver"),
        "link_up": link_up,
        "speed_mb": speed_mb,
        "duplex": duplex,
    }


def _parse_proxy_switch(proxy_switch: dict[str, Any]) -> dict[str, Any]:
    """Flatten one WS-API ``HostProxySwitch`` into the operator-facing row.

    The proxy switch is the host-side backing of a DVS. Its ``pnic``
    field lists the uplink physical NICs as WS-API keys; the row
    surfaces them as device names so the operator can read which
    physical ports back each uplink.
    """
    raw_uplinks = proxy_switch.get("pnic")
    uplink_pnics: list[str] = []
    if isinstance(raw_uplinks, list):
        uplink_pnics = [_pnic_device_from_key(key) for key in raw_uplinks if isinstance(key, str)]
    return {
        "key": proxy_switch.get("key"),
        "dvs_name": proxy_switch.get("dvsName"),
        "dvs_uuid": proxy_switch.get("dvsUuid"),
        "uplink_pnics": uplink_pnics,
    }


def _extract_host_network_props(retrieve_result: Any) -> tuple[list[Any], list[Any]]:
    """Pull ``config.network.pnic`` + ``config.network.proxySwitch`` from RetrievePropertiesEx.

    ``RetrievePropertiesEx`` returns a ``RetrieveResult`` whose
    ``objects`` list carries one ``ObjectContent`` per queried object,
    each with a ``propSet`` list of ``{name, val}`` pairs. For the
    single-host query the composite issues, the first object's propSet
    holds the two requested property paths. Returns the raw pnic and
    proxySwitch lists (empty when absent).
    """
    payload = _unwrap_value(retrieve_result)
    # RetrievePropertiesEx wraps the objects under ``objects``; a bare
    # list (some simulators / the legacy RetrieveProperties shape) is
    # tolerated too.
    if isinstance(payload, dict):
        objects = payload.get("objects", [])
    elif isinstance(payload, list):
        objects = payload
    else:
        objects = []
    prop_by_name: dict[str, Any] = {}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and isinstance(prop.get("name"), str):
                prop_by_name[prop["name"]] = prop.get("val")
    pnics = prop_by_name.get(_HOST_NET_PROP_PNIC)
    proxy_switches = prop_by_name.get(_HOST_NET_PROP_PROXYSWITCH)
    return (
        pnics if isinstance(pnics, list) else [],
        proxy_switches if isinstance(proxy_switches, list) else [],
    )


def _build_retrieve_properties_params(host_moid: str) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` specSet for one host's network config.

    A single ``PropertyFilterSpec`` scoped directly to the host object
    (no ContainerView / TraversalSpec) requesting the two network
    config property paths. ``moId`` targets the ``propertyCollector``
    singleton.
    """
    return {
        "moId": _PROPERTY_COLLECTOR_MOID,
        "specSet": [
            {
                "propSet": [
                    {
                        "type": _HOST_SYSTEM_MO_TYPE,
                        "pathSet": [
                            _HOST_NET_PROP_PNIC,
                            _HOST_NET_PROP_PROXYSWITCH,
                        ],
                    }
                ],
                "objectSet": [{"obj": {"type": _HOST_SYSTEM_MO_TYPE, "value": host_moid}}],
            }
        ],
        "options": {},
    }


async def _build_host_uplink_row(
    host_id: str,
    host_name: Any,
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Build one host row: identity + best-effort pnic / proxy-switch detail.

    The per-host WS-API property read is best-effort -- the host is
    already identified by the REST listing, so a failed vi-json
    ``RetrievePropertiesEx`` call nulls the network detail and records
    why (``read_note``) rather than sinking the whole composite.
    """
    row: dict[str, Any] = {"id": host_id, "name": host_name}
    props_result = await dispatch_child(
        connector_id=_CONNECTOR_ID,
        op_id=_OP_RETRIEVE_PROPERTIES,
        params=_build_retrieve_properties_params(host_id),
    )
    if props_result.status == "ok":
        raw_pnics, raw_proxy_switches = _extract_host_network_props(props_result.result)
        row["pnics"] = [_parse_pnic(p) for p in raw_pnics if isinstance(p, dict)]
        row["proxy_switches"] = [
            _parse_proxy_switch(ps) for ps in raw_proxy_switches if isinstance(ps, dict)
        ]
    else:
        row["pnics"] = None
        row["proxy_switches"] = None
        row["read_note"] = (
            f"host-network property read skipped: sub-op "
            f"{_OP_RETRIEVE_PROPERTIES!r} returned status="
            f"{props_result.status!r}: {_describe_sub_op_failure(props_result)}"
        )
    return row


async def host_network_uplinks_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Per host: physical NICs (link state + speed) and their proxy-switch uplinks.

    Op-id: ``vmware.composite.host.network_uplinks``.

    Sub-ops dispatched:

    1. ``GET:/vcenter/host`` -- list every host (optionally narrowed via
       ``filter_hosts``). Load-bearing: a failure here sinks the
       composite.
    2. Per host: ``POST:/PropertyCollector/{moId}/RetrievePropertiesEx``
       requesting ``config.network.pnic`` +
       ``config.network.proxySwitch`` on that single HostSystem object.
       This leg is **best-effort**: the plain REST host listing already
       identifies the host, so when the WS-API property read errors (a
       host that rejects the vi-json call, a transient auth expiry) the
       row is still returned with ``pnics`` / ``proxy_switches`` set to
       ``null`` and a ``read_note`` recording why, rather than failing
       the whole composite.

    The pnic link-state / uplink mapping is the one read that the plain
    vSphere Automation REST surface cannot reproduce: pnic link state,
    speed, and proxy-switch uplink association are Web-Services-API
    ``HostNetworkInfo`` properties, so the composite reaches them via
    the PropertyCollector vi-json method. This is what drives physical
    switch-port-occupancy reasoning ("are we out of switch ports?").

    Returns
    -------
    dict[str, Any]
        ``{"hosts": [{"id": ..., "name": ..., "pnics": [...],
        "proxy_switches": [...]}, ...]}``. Each pnic row carries
        ``device`` / ``mac`` / ``driver`` / ``link_up`` / ``speed_mb`` /
        ``duplex``; each proxy-switch row carries ``key`` / ``dvs_name``
        / ``dvs_uuid`` / ``uplink_pnics`` (physical-NIC device names).
        When the per-host property read is skipped, ``pnics`` and
        ``proxy_switches`` are ``None`` and the row carries a
        ``read_note``.
    """
    await preflight_l2_dependencies(
        composite_op_id=_COMPOSITE_OP_ID_HOST_NETWORK_UPLINKS,
        sub_op_ids=_SUB_OPS_HOST_NETWORK_UPLINKS,
        connector_id=_CONNECTOR_ID,
        tenant_id=operator.tenant_id,
    )
    filter_hosts: list[str] = list(params.get("filter_hosts") or [])

    listing_params: dict[str, Any] = {}
    if filter_hosts:
        listing_params["filter.hosts"] = filter_hosts

    listing = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_HOSTS,
            params=listing_params,
        )
    )
    entries = _unwrap_value(listing)
    if not isinstance(entries, list):
        raise RuntimeError(
            f"host_network_uplinks: expected list from {_OP_LIST_HOSTS!r}, "
            f"got {type(entries).__name__}"
        )

    aggregated: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        host_id = entry.get("host")
        if not isinstance(host_id, str):
            # vSphere REST returns the moid under ``host``; absence is an
            # upstream malformation -- skip rather than abort.
            continue
        aggregated.append(await _build_host_uplink_row(host_id, entry.get("name"), dispatch_child))
    return {"hosts": aggregated}


def _parse_vsan_health_test(test: dict[str, Any]) -> dict[str, Any]:
    """Flatten one WS-API ``VsanClusterHealthTest`` into the operator-facing row.

    A single health check inside a group: ``testId`` /``testName`` /
    ``testHealth`` (the ``green`` / ``yellow`` / ``red`` colour) plus the
    short human-readable description. The vSAN health-service owns the
    inner colour vocabulary; the composite passes it through verbatim.
    """
    return {
        "test_id": test.get("testId"),
        "test_name": test.get("testName"),
        "test_health": test.get("testHealth"),
        "test_short_description": test.get("testShortDescription"),
    }


def _parse_vsan_health_group(group: dict[str, Any]) -> dict[str, Any]:
    """Flatten one WS-API ``VsanClusterHealthGroup`` into the operator-facing row.

    A group buckets related health checks (network, physical disk,
    cluster, data, …). ``groupHealth`` is the group-level roll-up
    colour; ``groupTests`` is the per-check list flattened via
    :func:`_parse_vsan_health_test`. A missing / non-list ``groupTests``
    degrades to an empty list rather than raising -- the group-level
    colour is still meaningful on its own.
    """
    raw_tests = group.get("groupTests")
    tests = (
        [_parse_vsan_health_test(t) for t in raw_tests if isinstance(t, dict)]
        if isinstance(raw_tests, list)
        else []
    )
    return {
        "group_id": group.get("groupId"),
        "group_name": group.get("groupName"),
        "group_health": group.get("groupHealth"),
        "tests": tests,
    }


def _build_vsan_query_health_params(cluster_moid: str) -> dict[str, Any]:
    """Build the ``VsanQueryVcClusterHealthSummary`` argument set for one cluster.

    ``moId`` targets the ``vsan-cluster-health-system`` singleton on the
    ``/vsanHealth`` vmomi endpoint; ``cluster`` carries the target
    cluster's MoRef (a ``ClusterComputeResource``). Every other
    parameter of the method (``includeObjUuids`` / ``fields`` / …) is
    optional and left to the health service's defaults so the read
    returns the full summary.
    """
    return {
        "moId": _VSAN_CLUSTER_HEALTH_SYSTEM_MOID,
        "cluster": {"type": _CLUSTER_COMPUTE_RESOURCE_MO_TYPE, "value": cluster_moid},
    }


async def host_vsan_health_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Per cluster: vSAN health-test groups + overall health status.

    Op-id: ``vmware.composite.host.vsan_health``.

    Sub-ops dispatched:

    1. ``POST:/VsanVcClusterHealthSystem/{moId}/VsanQueryVcClusterHealthSummary``
       against the ``vsan-cluster-health-system`` singleton, scoped to
       the target cluster's MoRef. This leg is **best-effort**: the
       cluster is already identified by the ``cluster`` param, so when
       the health-service read errors (a cluster with vSAN disabled, a
       ``/vsanHealth`` endpoint that rejects the vi-json call, a
       transient auth expiry) the summary is returned with ``groups`` /
       ``overall_health`` set to ``null`` and a ``read_note`` recording
       why, rather than raising through ``_require_ok``.

    vSAN health is the one read the plain vSphere Automation REST
    surface cannot reproduce: the health-test-group / overall-status
    roll-up lives on the ``/vsanHealth`` vmomi service, so the composite
    reaches it via the ``VsanVcClusterHealthSystem`` managed object --
    the ``govc vsan.health.*`` equivalent. It drives cluster-health
    triage ('is vSAN healthy?' / 'which health group is red?').

    Returns
    -------
    dict[str, Any]
        ``{"cluster": <moid>, "overall_health": <colour|null>,
        "groups": [{"group_id": ..., "group_name": ...,
        "group_health": ..., "tests": [{"test_id": ...,
        "test_name": ..., "test_health": ...,
        "test_short_description": ...}, ...]}, ...]}``. When the
        best-effort health-service read is skipped, ``overall_health``
        and ``groups`` are ``null`` and the payload carries a
        ``read_note``.
    """
    await preflight_l2_dependencies(
        composite_op_id=_COMPOSITE_OP_ID_HOST_VSAN_HEALTH,
        sub_op_ids=_SUB_OPS_HOST_VSAN_HEALTH,
        connector_id=_CONNECTOR_ID,
        tenant_id=operator.tenant_id,
    )
    cluster_moid = params["cluster"]

    out: dict[str, Any] = {"cluster": cluster_moid}
    health_result = await dispatch_child(
        connector_id=_CONNECTOR_ID,
        op_id=_OP_VSAN_QUERY_HEALTH_SUMMARY,
        params=_build_vsan_query_health_params(cluster_moid),
    )
    if health_result.status == "ok":
        summary = _unwrap_value(health_result.result)
        raw_groups = summary.get("groups") if isinstance(summary, dict) else None
        overall = summary.get("overallHealth") if isinstance(summary, dict) else None
        out["overall_health"] = overall
        out["groups"] = (
            [_parse_vsan_health_group(g) for g in raw_groups if isinstance(g, dict)]
            if isinstance(raw_groups, list)
            else []
        )
    else:
        out["overall_health"] = None
        out["groups"] = None
        out["read_note"] = (
            f"vsan health-service read skipped: sub-op "
            f"{_OP_VSAN_QUERY_HEALTH_SUMMARY!r} returned status="
            f"{health_result.status!r}: {_describe_sub_op_failure(health_result)}"
        )
    return out
