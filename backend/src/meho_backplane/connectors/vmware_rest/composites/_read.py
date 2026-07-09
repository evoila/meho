# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pre-existing 7-composite handler module
# (>1200 lines); #2253 migrated the sub-call mechanism in place (direct
# session, no ingested sub-ops) without adding a new handler. Splitting
# the module is separate refactor work, out of scope here.

"""Read-only ``vmware.composite.*`` handler functions (7 composites).

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
no descriptor row and the composite failed with ``composite_l2_missing``
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
  approval-gated, so the write composites keep ``dispatch_child``.
  Migrating a write composite to the direct path must first resolve how
  the top-level policy/approval gate still covers the now-internal
  writes (Initiative #2249, the property-3 question).

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
and record a ``read_note`` / ``enrichment_note`` rather than sinking
the whole aggregation.

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

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "cluster_drs_recommendations_composite",
    "datastore_usage_composite",
    "event_tail_composite",
    "host_network_uplinks_composite",
    "host_vsan_health_composite",
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

# Per-composite sub-op-id tuples. Each tuple lists the raw-REST /
# vi-json sub-ops the composite issues directly on the connector
# session. Pre-#2253 these fed the L2 pre-flight check that guarded a
# missing catalog ingest; the direct-session migration removed that
# coupling (the composites no longer need ingested descriptor rows), so
# the tuples now serve as the canonical sub-op-path manifest the
# ingest-reconcile acceptance guard
# (``tests/acceptance/test_portgroup_audit_op_id_reconcile.py``) checks
# against the vCenter spec.
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
    GET query-string bucket (``filter.*``); ``body`` is the vi-json POST
    method-argument object (moid excluded -- it rides the path).

    Returns the raw parsed JSON (``value``-envelope handling stays with
    the caller's :func:`_unwrap_value`). Transport / status failures raise
    :exc:`httpx.HTTPError`; load-bearing callers let it propagate (the
    dispatcher's outer branch wraps it as ``connector_error`` for the
    composite parent, whose ``str(exc)`` carries the upstream status code
    + offending URL), best-effort callers catch it.
    """
    method, _, path_template = op_id.partition(":")
    path = path_template.format(**path_params) if path_params else path_template
    mounted = await connector.mount_op_path(target, path, operator)
    if method == "GET":
        return await connector._get_json(target, mounted, operator=operator, params=query or None)
    return await connector._post_json(target, mounted, operator=operator, json=body)


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
    cluster_moid = params["cluster"]
    include_history = bool(params.get("include_recommendations_history", False))

    cluster_result = await _read_sub_op(
        connector, target, operator, _OP_GET_CLUSTER, path_params={"cluster": cluster_moid}
    )
    drs_result = await _read_sub_op(
        connector, target, operator, _OP_GET_CLUSTER_DRS, path_params={"cluster": cluster_moid}
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


# Pre-existing >100-line handler from G3.1-T5 #508; G0.27 #1908 made the
# per-datastore VM-placement leg best-effort. Refactor (e.g. extracting
# the per-datastore row builder) is out of scope here.
# code-quality-allow: pre-existing G3.1-T5 #508 handler; #1908 best-effort enrichment only
async def datastore_usage_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """List datastores with capacity + free + VM placement summary.

    Op-id: ``vmware.composite.datastore.usage``.

    Sub-ops read directly on the connector session (per-datastore, sequential):

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

        # VM-placement enrichment is best-effort (#1908). The
        # capacity/free/type read above already satisfies the
        # storage-usage use case, so a failure on the optional VM lookup
        # (e.g. a vCenter that 400s the ``filter.datastores`` query)
        # nulls vm_count/vm_names and records why, rather than
        # propagating the transport error and sinking every datastore row.
        try:
            vms_raw = await _read_sub_op(
                connector, target, operator, _OP_LIST_VMS, query={"filter.datastores": [ds_id]}
            )
        except httpx.HTTPError as exc:
            row["vm_count"] = None
            row["vm_names"] = None
            row["enrichment_note"] = (
                f"vm-placement enrichment skipped: sub-op {_OP_LIST_VMS!r} "
                f"failed with {type(exc).__name__}: {exc}"
            )
        else:
            vm_entries = _unwrap_value(vms_raw)
            if not isinstance(vm_entries, list):
                vm_entries = []
            vm_names = [
                v["name"]
                for v in vm_entries
                if isinstance(v, dict) and isinstance(v.get("name"), str)
            ]
            row["vm_count"] = len(vm_names)
            row["vm_names"] = vm_names
        aggregated.append(row)
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
    """Audit distributed portgroups with parent DVS + connected-VM aggregation.

    Op-id: ``vmware.composite.network.portgroup.audit``.

    Sub-ops read directly on the connector session:

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
    filter_dvs = params.get("filter_dvs")
    include_disconnected = bool(params.get("include_disconnected_vms", False))

    dvs_query: dict[str, Any] = {}
    if isinstance(filter_dvs, str):
        dvs_query["filter.vdses"] = [filter_dvs]

    dvs_listing = await _read_sub_op(connector, target, operator, _OP_LIST_DVS, query=dvs_query)
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


def _build_retrieve_properties_body(host_moid: str) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` request body for one host's network config.

    A single ``PropertyFilterSpec`` scoped directly to the host object
    (no ContainerView / TraversalSpec) requesting the two network
    config property paths. The ``propertyCollector`` singleton moId
    rides the request path (:data:`_OP_RETRIEVE_PROPERTIES`), so the
    body is just the ``specSet`` + ``options`` method arguments -- the
    VI-JSON ``RetrievePropertiesExRequestType`` shape the
    ``vmware.host.usage`` typed op sends.
    """
    return {
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
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    host_id: str,
    host_name: Any,
) -> dict[str, Any]:
    """Build one host row: identity + best-effort pnic / proxy-switch detail.

    The per-host WS-API property read is best-effort -- the host is
    already identified by the REST listing, so a failed vi-json
    ``RetrievePropertiesEx`` call nulls the network detail and records
    why (``read_note``) rather than sinking the whole composite.
    """
    row: dict[str, Any] = {"id": host_id, "name": host_name}
    try:
        props_result = await _read_sub_op(
            connector,
            target,
            operator,
            _OP_RETRIEVE_PROPERTIES,
            path_params={"moId": _PROPERTY_COLLECTOR_MOID},
            body=_build_retrieve_properties_body(host_id),
        )
    except httpx.HTTPError as exc:
        row["pnics"] = None
        row["proxy_switches"] = None
        row["read_note"] = (
            f"host-network property read skipped: sub-op "
            f"{_OP_RETRIEVE_PROPERTIES!r} failed with {type(exc).__name__}: {exc}"
        )
        return row
    raw_pnics, raw_proxy_switches = _extract_host_network_props(props_result)
    row["pnics"] = [_parse_pnic(p) for p in raw_pnics if isinstance(p, dict)]
    row["proxy_switches"] = [
        _parse_proxy_switch(ps) for ps in raw_proxy_switches if isinstance(ps, dict)
    ]
    return row


async def host_network_uplinks_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Per host: physical NICs (link state + speed) and their proxy-switch uplinks.

    Op-id: ``vmware.composite.host.network_uplinks``.

    Sub-ops read directly on the connector session:

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
    filter_hosts: list[str] = list(params.get("filter_hosts") or [])

    listing_query: dict[str, Any] = {}
    if filter_hosts:
        listing_query["filter.hosts"] = filter_hosts

    listing = await _read_sub_op(connector, target, operator, _OP_LIST_HOSTS, query=listing_query)
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
        aggregated.append(
            await _build_host_uplink_row(connector, target, operator, host_id, entry.get("name"))
        )
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


def _build_vsan_query_health_body(cluster_moid: str) -> dict[str, Any]:
    """Build the ``VsanQueryVcClusterHealthSummary`` request body for one cluster.

    The ``vsan-cluster-health-system`` singleton moId rides the request
    path (:data:`_OP_VSAN_QUERY_HEALTH_SUMMARY`); the body carries the
    method's ``cluster`` argument -- the target cluster's MoRef (a
    ``ClusterComputeResource``). Every other parameter of the method
    (``includeObjUuids`` / ``fields`` / …) is optional and left to the
    health service's defaults so the read returns the full summary.
    """
    return {
        "cluster": {"type": _CLUSTER_COMPUTE_RESOURCE_MO_TYPE, "value": cluster_moid},
    }


async def host_vsan_health_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Per cluster: vSAN health-test groups + overall health status.

    Op-id: ``vmware.composite.host.vsan_health``.

    Sub-op read directly on the connector session:

    1. ``POST:/VsanVcClusterHealthSystem/{moId}/VsanQueryVcClusterHealthSummary``
       against the ``vsan-cluster-health-system`` singleton, scoped to
       the target cluster's MoRef. This leg is **best-effort**: the
       cluster is already identified by the ``cluster`` param, so when
       the health-service read errors (a cluster with vSAN disabled, a
       ``/vsanHealth`` endpoint that rejects the vi-json call, a
       transient auth expiry) the summary is returned with ``groups`` /
       ``overall_health`` set to ``null`` and a ``read_note`` recording
       why, rather than propagating the transport error.

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
    cluster_moid = params["cluster"]

    out: dict[str, Any] = {"cluster": cluster_moid}
    try:
        health_result = await _read_sub_op(
            connector,
            target,
            operator,
            _OP_VSAN_QUERY_HEALTH_SUMMARY,
            path_params={"moId": _VSAN_CLUSTER_HEALTH_SYSTEM_MOID},
            body=_build_vsan_query_health_body(cluster_moid),
        )
    except httpx.HTTPError as exc:
        out["overall_health"] = None
        out["groups"] = None
        out["read_note"] = (
            f"vsan health-service read skipped: sub-op "
            f"{_OP_VSAN_QUERY_HEALTH_SUMMARY!r} failed with {type(exc).__name__}: {exc}"
        )
        return out
    summary = _unwrap_value(health_result)
    raw_groups = summary.get("groups") if isinstance(summary, dict) else None
    overall = summary.get("overallHealth") if isinstance(summary, dict) else None
    out["overall_health"] = overall
    out["groups"] = (
        [_parse_vsan_health_group(g) for g in raw_groups if isinstance(g, dict)]
        if isinstance(raw_groups, list)
        else []
    )
    return out
