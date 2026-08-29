# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pre-existing multi-composite handler
# module; #2253 migrated the sub-call mechanism in place (direct session,
# no ingested sub-ops) and #2258 removed the two host reads
# (network_uplinks / vsan_health, now typed ops), leaving 5 composites.
# Splitting the module is separate refactor work, out of scope here.

"""Read-only ``vmware.composite.*`` handler functions (5 composites).

Each handler is a module-level ``async def`` that takes the dispatcher's
composite-branch keyword args ``(operator, target, params, connector)``
and returns a single aggregated dict built from 2-3 sub-calls issued
**directly on the connector's own authenticated session** --
``connector._get_json`` / ``connector._post_json`` mounted through
``connector.mount_op_path`` -- with no ``endpoint_descriptor`` lookup
(#2253, the I-B read migration under Initiative #2249 / Goal #2247).

Why module-level functions
--------------------------

:func:`~meho_backplane.operations.typed_register.derive_handler_ref`
rejects closures, ``functools.partial``, and lambdas at registration
time (``__qualname__`` containing ``<locals>``). Module-level
``async def`` is the only shape the dispatcher can resolve via
``importlib.import_module`` + chained ``getattr`` at first-dispatch
time.

Why direct session, not ``dispatch_child``
------------------------------------------

Before #2253 these read handlers routed every sub-call through
``dispatch_child`` -- the catalog-routed dispatcher seam that resolves
each sub-op against an ``ingested`` ``endpoint_descriptor`` row. That
coupled every read composite to a per-deploy vCenter-catalog ingest:
until an operator ran ``meho connector ingest --catalog vmware/9.0`` the
``GET:/vcenter/datastore`` / ``POST:/PropertyCollector/...`` sub-ops had
no descriptor row and the composite could not dispatch them
(consumer signal 20, ``claude-rdc-hetzner-dc#697``). The two-world op
model (Goal #2247) removes that coupling: the handler receives the
resolved connector instance (the ``connector`` kwarg the #2251 substrate
added to the composite contract) and issues each sub-call on the
connector session, so the composite works on a fresh boot with **zero
catalog ingest**. The precedent is the ``vmware.host.usage`` typed op
(:mod:`~meho_backplane.connectors.vmware_rest.typed_ops`), which reads
the same ``GET:/vcenter/host`` + ``RetrievePropertiesEx`` surface
directly on the session.

``dispatch_child`` gave four guarantees (per #508); the direct path
drops two and relocates the other two, which is why it is a **read**-only
migration:

* **(2) Bounded recursion is moot** -- a direct session call cannot
  re-enter the dispatcher, so there is no recursion to bound.
* **(4) Per-sub-op param validation goes away** -- for a
  code-constructed request body this is the point, not a loss:
  re-validating a hand-built vmomi body against a persisted spec
  schema is the schema-drift defect the two-world model exists to
  remove.
* **(1) Audit-tree linkage** collapses to the top-level composite op's
  own audit row (the row a forensic query reads anyway); the per-sub-op
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

Error handling
--------------

A load-bearing sub-op (the datastore listing, a per-datastore detail
read, a cluster/DRS read, an event/perf query) lets an
:exc:`httpx.HTTPError` propagate: the dispatcher's outer exception
branch wraps it into a ``connector_error`` :class:`OperationResult` for
the composite parent, whose ``str(exc)`` already carries the upstream
status code + offending URL. Optional enrichment legs (per-datastore
VM placement, per-host property read, vSAN health) degrade best-effort
instead -- they catch the transport error, null the enriched fields,
and record an ``enrichment_note`` rather than sinking the whole
aggregation. (The former per-host ``network_uplinks`` and per-cluster
``vsan_health`` best-effort reads were re-shipped as typed ops in #2258;
see :mod:`~meho_backplane.connectors.vmware_rest.typed_ops`.)

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

from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.vim_body import (
    retrieve_properties_body,
    unwrap_vim_value,
)

from .schemas import DATASTORE_USAGE_MAX_VM_NAMES

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "cluster_drs_recommendations_composite",
    "datastore_usage_composite",
    "event_tail_composite",
    "network_portgroup_audit_composite",
    "performance_summary_composite",
]


# Canonical ``METHOD:/path`` sub-op ids. Each handler splits the string
# into its verb + spec-relative path, substitutes ``{var}`` path params,
# and mounts the path onto the target's live ``/api`` (modern) / ``/rest``
# (legacy/vcsim) prefix for the direct session call (see
# :func:`_read_sub_op`). Centralised so the ingest-reconcile acceptance
# guard can assert the composite hits the same canonical paths the vCenter
# catalog would emit.
_OP_GET_CLUSTER = "GET:/vcenter/cluster/{cluster}"
# The pinned vcenter.yaml serves NO cluster DRS REST resource at all: the
# ``GET:/vcenter/cluster/{cluster}/drs`` path #508 declared was unserved
# (the #2970 adjacent finding, fixed in #2986). DRS state is a vim-only
# surface, so the composite reads it through the PropertyCollector
# singleton (``configurationEx.drsConfig`` + optional
# ``drsRecommendation``) -- the same vim seam the vm.migrate DRS lookup
# uses (#2970).
_OP_RETRIEVE_PROPERTIES = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
_OP_POST_QUERY_EVENTS = "POST:/EventManager/{moId}/QueryEvents"
_OP_POST_QUERY_AVAILABLE_PERF_METRIC = "POST:/PerformanceManager/{moId}/QueryAvailablePerfMetric"
_OP_POST_QUERY_PERF = "POST:/PerformanceManager/{moId}/QueryPerf"
_OP_LIST_DATASTORES = "GET:/vcenter/datastore"
_OP_GET_DATASTORE = "GET:/vcenter/datastore/{datastore}"
_OP_LIST_VMS = "GET:/vcenter/vm"
# There is NO distributed-switch list resource in the pinned REST spec at
# all: the plural ``distributed-switches`` path #1602 repointed to exists
# only under ``/vcenter/namespace-management/networks/nsx/`` (NSX-scoped)
# in the canonical ``vcenter.yaml`` -- the #2970 real-spec reconcile
# finding. The audit's DVS-list step is therefore dropped (see the
# handler's degradation note); enumerating switches is a vim-only surface.
# There is likewise NO dedicated ``distributed-portgroup(s)`` list
# resource: distributed portgroups are enumerated via the generic network
# resource filtered to ``DISTRIBUTED_PORTGROUP`` (the singular
# ``distributed-portgroup`` op_id #508 declared was absent from every
# ingest -- #1602). The generic ``Network`` summary returns only
# ``{network (id), name, type}`` -- it carries no parent-DVS field, so
# the per-portgroup ``dvs``/``dvs_name`` enrichment is best-effort (see
# the handler note).
_OP_LIST_NETWORK = "GET:/vcenter/network"
_NETWORK_TYPE_DISTRIBUTED_PORTGROUP = "DISTRIBUTED_PORTGROUP"

# vim constants for the cluster DRS read. The PropertyCollector is a vim
# singleton whose moId is the literal ``propertyCollector`` -- the
# concrete value the ``{moId}`` path template takes at call time (the
# same convention the typed reads and write composites use).
_PROPERTY_COLLECTOR_MOID = "propertyCollector"
_CLUSTER_COMPUTE_RESOURCE_MO_TYPE = "ClusterComputeResource"
# Nested vim property path for the cluster's DRS configuration:
# ``ClusterConfigInfoEx.drsConfig`` is a ``ClusterDrsConfigInfo``
# (``enabled`` / ``defaultVmBehavior`` / ``vmotionRate`` / ...),
# spec-verified against the pinned vi-json.yaml.
_PROP_DRS_CONFIG = "configurationEx.drsConfig"
# The cluster's current DRS recommendation list
# (``ClusterDrsRecommendation[]``). Deprecated in the vim API since
# VI 2.5 but served by the pinned spec and the only surface carrying the
# vm -> destination migration pairs -- the #2970 / PR #2974 decision the
# vm.migrate DRS lookup already relies on.
_PROP_DRS_RECOMMENDATION = "drsRecommendation"

# vim constants for the datastore-usage VM-placement read. The
# ``VirtualMachine.datastore`` property is the vim-authoritative set of
# datastores a VM's files sit on (config + disks + snapshots + swap,
# unioned) -- the placement source the datastore-usage composite scopes
# each row from, instead of trusting a server-side ``GET:/vcenter/vm``
# datastore filter that some builds silently ignore (#2975).
_VIRTUAL_MACHINE_MO_TYPE = "VirtualMachine"
_PROP_VM_DATASTORE = "datastore"

# Per-composite sub-op-id tuples. Each tuple lists the raw-REST /
# vi-json sub-ops the composite issues directly on the connector
# session. Pre-#2253 these fed the L2 pre-flight check that guarded a
# missing catalog ingest; the direct-session migration removed that
# coupling (the composites no longer need ingested descriptor rows), so
# the tuples now serve as the canonical sub-op-path manifest the
# spec-reconcile lanes check against the pinned specs: the exhaustive
# read lane
# (``tests/test_connectors_vmware_rest_composites_read_reconcile.py``,
# #2986 -- every ``_OP_*`` constant, GET legs vs vcenter.yaml, POST legs
# vs vi-json.yaml) plus the portgroup-audit acceptance guard
# (``tests/acceptance/test_portgroup_audit_op_id_reconcile.py``).
_SUB_OPS_CLUSTER_DRS_RECS: tuple[str, ...] = (
    _OP_GET_CLUSTER,
    _OP_RETRIEVE_PROPERTIES,
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
    _OP_RETRIEVE_PROPERTIES,
)
_SUB_OPS_NETWORK_PORTGROUP_AUDIT: tuple[str, ...] = (
    _OP_LIST_NETWORK,
    _OP_LIST_VMS,
)


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


async def _read_sub_op(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    op_id: str,
    *,
    path_params: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    body: Any = None,
) -> Any:
    """Issue one composite sub-call directly on the connector's session.

    Splits the canonical ``METHOD:/path`` *op_id* into its verb + spec-
    relative path, substitutes any ``{var}`` path params, mounts the path
    onto *target*'s live ``/api`` (modern) or ``/rest`` (legacy/vcsim)
    prefix via :meth:`VmwareRestConnector.mount_op_path`, and dispatches
    through the connector's own authenticated session:
    :meth:`~meho_backplane.connectors.adapters.http.HttpConnector._get_json`
    for ``GET`` (tenacity-retried, idempotent) or
    :meth:`~meho_backplane.connectors.adapters.http.HttpConnector._post_json`
    for the vi-json ``POST`` methods. No ``endpoint_descriptor`` lookup,
    so the sub-call works on a fresh boot with zero catalog ingest.

    ``path_params`` substitutes the ``{var}`` placeholders (vCenter moids
    are bare ``[A-Za-z0-9-]`` tokens, so a plain ``str.format`` matches the
    RFC6570 simple-expansion the ingested path did). ``query`` is the
    GET query-string bucket, authored in the legacy ``filter.*`` style;
    :meth:`VmwareRestConnector.adapt_op_query` keys it off the target's
    live mount before dispatch (modern ``/api`` strips the ``filter.``
    prefix — which it 400s — legacy ``/rest`` keeps it). ``body`` is the
    vi-json POST method-argument object (moid excluded -- it rides the
    path).

    ``GET`` legs are vSphere Automation ``/vcenter/*`` paths, mounted on
    ``/api`` / ``/rest`` via :meth:`VmwareRestConnector.mount_op_path`.
    ``POST`` legs are vmomi (VI-JSON) methods (``QueryEvents`` /
    ``QueryAvailablePerfMetric`` / ``QueryPerf`` /
    ``RetrievePropertiesEx``); they route through
    :meth:`VmwareRestConnector._post_vmomi_json`, which mounts them on the
    documented VI-JSON base ``/sdk/vim25/{release}`` (single ``/api``
    fallback) so they resolve on vCenter 8.0.x instead of 404ing (#2466) —
    no vmomi path reaches the bare ``/api`` mount.

    Returns the raw parsed JSON (``value``-envelope handling stays with
    the caller's :func:`_unwrap_value`). Transport / status failures raise
    :exc:`httpx.HTTPError`; load-bearing callers let it propagate (the
    dispatcher's outer branch wraps it as ``connector_error`` for the
    composite parent, whose ``str(exc)`` carries the upstream status code
    + offending URL), best-effort callers catch it.
    """
    method, _, path_template = op_id.partition(":")
    path = path_template.format(**path_params) if path_params else path_template
    if method == "GET":
        mounted = await connector.mount_op_path(target, path, operator)
        params = await connector.adapt_op_query(target, query, operator)
        return await connector._get_json(target, mounted, operator=operator, params=params)
    return await connector._post_vmomi_json(target, path, operator=operator, json=body)


def _build_cluster_props_retrieve_body(cluster_moid: str, path_set: list[str]) -> dict[str, Any]:
    """Build a ``RetrievePropertiesEx`` body reading cluster properties.

    One ``PropertyFilterSpec`` scoped directly to the cluster's MoRef;
    the singleton ``propertyCollector`` moId rides the path, so the body
    is only the method args -- the same shape the write composites'
    config reads send, ``_typeName``-annotated via the shared trio
    helper (#3103).
    """
    return retrieve_properties_body(_CLUSTER_COMPUTE_RESOURCE_MO_TYPE, [cluster_moid], path_set)


def _extract_object_props(retrieve_result: Any) -> dict[str, Any]:
    """Flatten a single-object ``RetrievePropertiesEx`` result to ``{name: val}``.

    vim omits unset properties from ``propSet`` (they surface in
    ``missingSet`` instead), so callers treat an absent key as "property
    not set on this object" and fall back to their documented defaults.
    """
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    props: dict[str, Any] = {}
    if not isinstance(objects, list):
        return props
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for entry in obj.get("propSet", []) or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str):
                props[name] = unwrap_vim_value(entry.get("val"))
    return props


def _extract_props_by_moid(retrieve_result: Any) -> dict[str, dict[str, Any]]:
    """Flatten a multi-object ``RetrievePropertiesEx`` result to ``{moid: {name: val}}``.

    Unlike :func:`_extract_object_props` (which merges every object's
    ``propSet`` into one flat dict), this keeps per-object identity by
    keying on the object's MoRef ``value`` -- what a batched read over
    many objects of the same type (every VM's ``datastore`` property)
    needs. vim omits unset properties from ``propSet``, so an object with
    no matching property simply carries an empty inner dict.
    """
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    by_moid: dict[str, dict[str, Any]] = {}
    if not isinstance(objects, list):
        return by_moid
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        ref = obj.get("obj")
        moid = ref.get("value") if isinstance(ref, dict) else None
        if not isinstance(moid, str):
            continue
        props: dict[str, Any] = {}
        for entry in obj.get("propSet", []) or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str):
                props[name] = unwrap_vim_value(entry.get("val"))
        by_moid[moid] = props
    return by_moid


async def cluster_drs_recommendations_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Read cluster summary + DRS state in one composite call.

    Op-id: ``vmware.composite.cluster.drs_recommendations``.

    Sub-ops read directly on the connector session (sequential):

    1. ``GET:/vcenter/cluster/{cluster}`` -- cluster summary (name,
       resource pool, HA/DRS-enabled flags).
    2. ``POST:/PropertyCollector/{moId}/RetrievePropertiesEx`` (vi-json)
       -- the cluster's DRS configuration
       (``ClusterComputeResource.configurationEx.drsConfig``, a
       ``ClusterDrsConfigInfo``: ``enabled``, ``defaultVmBehavior``,
       ``vmotionRate``, ...) plus, when
       ``include_recommendations_history=True``, the cluster's current
       ``drsRecommendation`` list in the same read. The pinned
       ``vcenter.yaml`` serves no cluster DRS REST resource at all (the
       ``GET:/vcenter/cluster/{cluster}/drs`` path #508 declared was
       unserved -- the #2970 adjacent finding, fixed here per #2986);
       vim is the only DRS surface, mirroring the vm.migrate DRS
       lookup.

    Returns
    -------
    dict[str, Any]
        ``{"cluster": <summary dict>, "drs": <ClusterDrsConfigInfo
        dict>, "recommendations_history": <optional list>}``. ``drs``
        is ``{}`` when the property is unset on the target. The
        ``recommendations_history`` key appears only when the operator
        sets ``include_recommendations_history=True``; it carries the
        cluster's current ``ClusterDrsRecommendation`` rows (empty list
        when DRS has none pending) -- the key name predates the vim
        switch and is retained for envelope stability.
    """
    cluster_moid = params["cluster"]
    include_history = bool(params.get("include_recommendations_history", False))

    cluster_result = await _read_sub_op(
        connector, target, operator, _OP_GET_CLUSTER, path_params={"cluster": cluster_moid}
    )
    path_set = [_PROP_DRS_CONFIG]
    if include_history:
        path_set.append(_PROP_DRS_RECOMMENDATION)
    retrieve_result = await _read_sub_op(
        connector,
        target,
        operator,
        _OP_RETRIEVE_PROPERTIES,
        path_params={"moId": _PROPERTY_COLLECTOR_MOID},
        body=_build_cluster_props_retrieve_body(cluster_moid, path_set),
    )
    props = _extract_object_props(retrieve_result)
    drs_config = props.get(_PROP_DRS_CONFIG)
    out: dict[str, Any] = {
        "cluster": _unwrap_value(cluster_result),
        "drs": drs_config if isinstance(drs_config, dict) else {},
    }
    if include_history:
        recommendations = props.get(_PROP_DRS_RECOMMENDATION)
        out["recommendations_history"] = (
            recommendations if isinstance(recommendations, list) else []
        )
    return out


async def event_tail_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Tail recent events via EventManager.QueryEvents (vi-json).

    Op-id: ``vmware.composite.event.tail``.

    Sub-op read directly on the connector session (single call):

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
    mo_id = params.get("moId", "EventManager")
    max_events = int(params.get("max_events", 100))
    raw = await _read_sub_op(
        connector, target, operator, _OP_POST_QUERY_EVENTS, path_params={"moId": mo_id}
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
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Summarise performance metrics for one entity via PerformanceManager (vi-json).

    Op-id: ``vmware.composite.performance.summary``.

    Sub-ops read directly on the connector session (sequential):

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

    The vi-json method arguments (``entity``, ``interval_seconds``)
    become the flat JSON request body; the ``moId`` targets the
    PerformanceManager singleton in the path -- the same method-args-as-
    body shape the ``vmware.host.usage`` typed op sends for
    RetrievePropertiesEx.
    """
    entity_moid = params["entity_moid"]
    perf_mgr_moid = params.get("perf_manager_moid", "PerfMgr")
    interval_s = int(params.get("interval_seconds", 20))
    max_samples = int(params.get("max_samples", 60))

    available_raw = await _read_sub_op(
        connector,
        target,
        operator,
        _OP_POST_QUERY_AVAILABLE_PERF_METRIC,
        path_params={"moId": perf_mgr_moid},
        body={"entity": entity_moid},
    )
    available = _unwrap_value(available_raw)
    if not isinstance(available, list):
        raise RuntimeError(
            "performance_summary: expected list from "
            f"{_OP_POST_QUERY_AVAILABLE_PERF_METRIC!r}, "
            f"got {type(available).__name__}"
        )

    samples_raw = await _read_sub_op(
        connector,
        target,
        operator,
        _OP_POST_QUERY_PERF,
        path_params={"moId": perf_mgr_moid},
        body={"entity": entity_moid, "interval_seconds": interval_s},
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


async def _read_vm_placement(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
) -> tuple[dict[str, list[str]] | None, str | None]:
    """Resolve authoritative ``{datastore_moid: [vm_name, ...]}`` placement.

    #2975: the placement source is the vim ``VirtualMachine.datastore``
    property, **not** a server-side ``GET:/vcenter/vm`` datastore filter.
    Some vCenter builds silently ignore that filter and return the whole
    inventory on every row -- the reported symptom -- so the composite no
    longer trusts it. Two batched reads, joined client-side:

    1. ``GET:/vcenter/vm`` (global, unfiltered) -- every VM's moid + name.
    2. one ``RetrievePropertiesEx`` over every VM moid reading
       ``datastore`` (the union of datastores the VM's files sit on). A VM
       with disks spanning two datastores therefore appears under both --
       genuine placement, unlike the ignored-filter global list.

    VM-listing order is preserved so the per-datastore ``vm_names`` sample
    is deterministic. Returns ``({}, None)`` when there are no VMs. The
    read is **best-effort** (#1908): any transport error returns
    ``(None, note)`` so the caller nulls enrichment on every row while
    keeping the capacity data the storage-usage use case needs.
    """
    try:
        vms_raw = await _read_sub_op(connector, target, operator, _OP_LIST_VMS)
        vm_entries = _unwrap_value(vms_raw)
        if not isinstance(vm_entries, list):
            vm_entries = []
        names_by_moid: dict[str, str] = {}
        for vm in vm_entries:
            if not isinstance(vm, dict):
                continue
            moid = vm.get("vm")
            name = vm.get("name")
            if isinstance(moid, str) and isinstance(name, str):
                names_by_moid[moid] = name
        if not names_by_moid:
            return {}, None
        retrieve = await _read_sub_op(
            connector,
            target,
            operator,
            _OP_RETRIEVE_PROPERTIES,
            path_params={"moId": _PROPERTY_COLLECTOR_MOID},
            body=retrieve_properties_body(
                _VIRTUAL_MACHINE_MO_TYPE, list(names_by_moid), [_PROP_VM_DATASTORE]
            ),
        )
    except httpx.HTTPError as exc:
        return None, (
            f"vm-placement enrichment skipped: authoritative placement read "
            f"failed with {type(exc).__name__}: {exc}"
        )

    props_by_vm = _extract_props_by_moid(retrieve)
    placement: dict[str, list[str]] = {}
    for moid, name in names_by_moid.items():
        ds_refs = props_by_vm.get(moid, {}).get(_PROP_VM_DATASTORE)
        if not isinstance(ds_refs, list):
            continue
        for ref in ds_refs:
            ds_moid = ref.get("value") if isinstance(ref, dict) else None
            if isinstance(ds_moid, str):
                placement.setdefault(ds_moid, []).append(name)
    return placement, None


async def datastore_usage_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """List datastores with capacity + free + VM placement summary.

    Op-id: ``vmware.composite.datastore.usage``.

    Sub-ops read directly on the connector session:

    1. ``GET:/vcenter/datastore`` -- list every datastore (optionally
       narrowed via ``filter.names``).
    2. For each datastore, ``GET:/vcenter/datastore/{datastore}`` --
       detailed capacity / free / type / accessible flag (load-bearing:
       a failure here sinks the composite).
    3. VM-placement enrichment, resolved **once** for the whole result
       (see :func:`_read_vm_placement`): ``GET:/vcenter/vm`` (global,
       unfiltered) for every VM's moid + name, then one
       ``RetrievePropertiesEx`` over every VM reading the vim-authoritative
       ``VirtualMachine.datastore`` property. The two are joined
       client-side into per-datastore ``vm_count`` + ``vm_names``.

    Why vim placement and not a ``GET:/vcenter/vm`` datastore filter
    (#2975): a server-side per-datastore filter is silently ignored by
    some builds, which returns the whole-inventory VM list on every row --
    the reported bug. ``VirtualMachine.datastore`` is authoritative
    (config + disks + snapshots + swap, unioned), so each row carries only
    the VMs vim reports on that datastore regardless of whether any REST
    filter is honoured. A VM whose disks span two datastores legitimately
    appears under both.

    The enrichment is **best-effort** (#1908): the capacity/free/type read
    already satisfies the "which datastores are filling up?" use case, so a
    transport error on the batched placement read nulls ``vm_count`` /
    ``vm_names`` on every row and records an ``enrichment_note``, rather
    than sinking the whole composite.

    Returns
    -------
    dict[str, Any]
        ``{"datastores": [{"id": ..., "name": ..., "type": ...,
        "capacity": ..., "free_space": ..., "vm_count": ...,
        "vm_names": [...]}, ...]}``. The ``capacity`` / ``free_space``
        fields may be ``None`` if the upstream payload omits them
        (e.g. a partially-mounted datastore). When the batched
        VM-placement read errors, ``vm_count`` and ``vm_names`` are
        ``None`` and every row carries an ``enrichment_note`` string
        describing the skipped enrichment; on success the row has no
        ``enrichment_note`` key. As belt-and-braces (#3049), the same null
        + ``enrichment_note`` treatment is applied when the per-row VM sets
        somehow come back identical and non-empty across every datastore --
        a shape authoritative placement cannot produce in practice.

        ``vm_count`` is the exact VM total; ``vm_names`` is bounded to a
        sample of at most
        :data:`~meho_backplane.connectors.vmware_rest.composites.schemas.DATASTORE_USAGE_MAX_VM_NAMES`
        names, so a one-name ``filter_names`` result stays inline under the
        JSONFlux byte threshold and a per-datastore Sensor can select
        ``$.datastores[0].free_space`` (#2758).
    """
    filter_names: list[str] = list(params.get("filter_names") or [])

    listing_query: dict[str, Any] = {}
    if filter_names:
        listing_query["filter.names"] = filter_names

    listing = await _read_sub_op(
        connector, target, operator, _OP_LIST_DATASTORES, query=listing_query
    )
    entries = _unwrap_value(listing)
    if not isinstance(entries, list):
        raise RuntimeError(
            f"datastore_usage: expected list from {_OP_LIST_DATASTORES!r}, "
            f"got {type(entries).__name__}"
        )

    # Each datastore row paired with its moid, so the batched VM-placement
    # read below can scope names onto it after the detail loop.
    rows: list[tuple[dict[str, Any], str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ds_id = entry.get("datastore")
        if not isinstance(ds_id, str):
            # vSphere REST always returns the moid under the key
            # ``datastore``; absence is an upstream malformation. Skip
            # silently rather than abort the aggregation.
            continue
        detail = await _read_sub_op(
            connector, target, operator, _OP_GET_DATASTORE, path_params={"datastore": ds_id}
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
        rows.append((row, ds_id))

    aggregated = [row for row, _ in rows]
    if not rows:
        return {"datastores": aggregated}

    # VM-placement enrichment (#2975): scope each row to only the VMs vim
    # reports on that datastore, resolved once from the authoritative
    # ``VirtualMachine.datastore`` property rather than a per-datastore
    # server-side filter some builds silently ignore. Best-effort (#1908):
    # a transport error nulls enrichment on every row but keeps capacity.
    placement, note = await _read_vm_placement(connector, target, operator)
    if placement is None:
        for row, _ in rows:
            row["vm_count"] = None
            row["vm_names"] = None
            row["enrichment_note"] = note
        return {"datastores": aggregated}

    # Each enriched row paired with its full (pre-cap) VM-name set, for the
    # cross-row identical-sets guard below (#3049).
    enriched: list[tuple[dict[str, Any], frozenset[str]]] = []
    for row, ds_id in rows:
        vm_names = placement.get(ds_id, [])
        # vm_count is the exact total, taken before the cap; vm_names is
        # bounded to a sample so a large result stays inline under the
        # dispatcher's 4096-byte JSONFlux threshold and a per-datastore
        # Sensor can still select free_space (#2758). vm_count >
        # len(vm_names) is the truncation signal.
        row["vm_count"] = len(vm_names)
        row["vm_names"] = vm_names[:DATASTORE_USAGE_MAX_VM_NAMES]
        enriched.append((row, frozenset(vm_names)))

    # Cross-row identical-sets guard (#3049), retained as belt-and-braces.
    # With authoritative ``VirtualMachine.datastore`` placement an identical
    # non-empty VM set across *every* datastore cannot arise in practice (it
    # would require every VM to sit on every datastore), so the guard's
    # firing condition is effectively impossible now -- but it stays to
    # refuse any populated-but-identical shape rather than emit it as
    # per-datastore placement. The all-empty case (every datastore
    # legitimately has zero VMs) is excluded by the non-empty check.
    if len(enriched) > 1:
        vm_sets = [vm_set for _, vm_set in enriched]
        shared = vm_sets[0]
        if shared and all(vm_set == shared for vm_set in vm_sets[1:]):
            note = (
                "vm-placement enrichment discarded: the VM set was identical "
                f"across all {len(enriched)} enriched datastores, which is not "
                "real per-datastore placement; vm_count/vm_names are "
                "unreliable and have been nulled."
            )
            for row, _ in enriched:
                row["vm_count"] = None
                row["vm_names"] = None
                row["enrichment_note"] = note

    return {"datastores": aggregated}


# Pre-existing >100-line handler from G3.1-T5 #508.
# code-quality-allow: pre-existing G3.1-T5 #508 handler
async def network_portgroup_audit_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Audit distributed portgroups with connected-VM aggregation.

    Op-id: ``vmware.composite.network.portgroup.audit``.

    Sub-ops read directly on the connector session:

    1. ``GET:/vcenter/network`` with ``filter.types=[DISTRIBUTED_PORTGROUP]``
       -- list distributed portgroups. The REST Automation API has no
       dedicated distributed-portgroup resource; portgroups are
       enumerated through the generic ``Network`` resource filtered to
       the ``DISTRIBUTED_PORTGROUP`` type (#1602).
    2. Per portgroup: ``GET:/vcenter/vm`` with ``filter.networks`` --
       VMs connected to the portgroup. Drives the ``vm_count`` +
       ``vm_names`` aggregation.

    Returns
    -------
    dict[str, Any]
        ``{"portgroups": [{"id": ..., "name": ..., "dvs": <id|None>,
        "dvs_name": <str|None>, "type": ..., "vm_count": ...,
        "vm_names": [...]}, ...]}``.

    Degradation note (#2970): the pre-#2970 step 1 listed distributed
    switches via ``GET:/vcenter/network/distributed-switches`` to build a
    moid->name index for ``dvs_name`` enrichment -- but the pinned
    ``vcenter.yaml`` serves that path only under the NSX-scoped
    ``/vcenter/namespace-management/`` tree, so the step 404s on a real
    vCenter 9.0 and is dropped. ``dvs_name`` is therefore always ``None``
    (it already was in practice: the generic ``Network`` summary carries
    only ``{network (id), name, type}`` -- no parent-DVS reference to
    join on -- so the index was never consulted with a hit). ``dvs``
    stays best-effort: populated when the upstream payload happens to
    expose a ``vds``/``distributed_switch`` field, ``None`` otherwise.
    ``filter_dvs`` only ever scoped that index, so it is accepted but
    inert -- see the parameter schema note.
    """
    include_disconnected = bool(params.get("include_disconnected_vms", False))

    # Distributed portgroups come from the generic network resource
    # filtered to the DISTRIBUTED_PORTGROUP type -- there is no
    # standalone distributed-portgroup list endpoint (and no DVS list
    # resource at all; see the degradation note above).
    pg_query: dict[str, Any] = {"filter.types": [_NETWORK_TYPE_DISTRIBUTED_PORTGROUP]}

    pg_listing = await _read_sub_op(connector, target, operator, _OP_LIST_NETWORK, query=pg_query)
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
        vm_query: dict[str, Any] = {"filter.networks": [pg_id]}
        if not include_disconnected:
            # vSphere REST accepts a power-state filter; the
            # ``include_disconnected`` flag toggles it. Default is
            # active VMs only.
            vm_query["filter.power_states"] = ["POWERED_ON"]
        vms = await _read_sub_op(connector, target, operator, _OP_LIST_VMS, query=vm_query)
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
                # Always None post-#2970: the DVS-list step that built the
                # moid->name index is not served by the pinned spec (see
                # the handler degradation note). Key retained for
                # response-envelope stability.
                "dvs_name": None,
                "type": entry.get("type"),
                "vm_count": len(vm_names),
                "vm_names": vm_names,
            }
        )
    return {"portgroups": aggregated}
