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
VM placement, per-host property read) degrade best-effort
instead -- they catch the transport error, null the enriched fields,
and record an ``enrichment_note`` rather than sinking the whole
aggregation. (The former per-host ``network_uplinks`` and per-cluster
``vsan_health`` best-effort reads were re-shipped as typed ops in #2258;
see :mod:`~meho_backplane.connectors.vmware_rest.typed_ops`. Since #4441
``vsan_health`` raises on a failed vSAN call instead of degrading.)

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
from meho_backplane.connectors.vmware_rest.composites._write import (
    _DVPG_MO_TYPE,
    _DVS_MO_TYPE,
    _PROP_DVPG_DEFAULT_PORT_CONFIG,
    _PROP_DVPG_DVS,
    _observed_vlan_identity,
    _vlan_identity_view,
)
from meho_backplane.connectors.vmware_rest.typed_ops import _int_or_none
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    fault_type_name,
    retrieve_properties_body,
    unwrap_vim_value,
    vim_moref,
)
from meho_backplane.connectors.vmware_rest.vim_retrieve import retrieve_all_pages

from .schemas import DATASTORE_USAGE_MAX_VM_NAMES

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "cluster_drs_recommendations_composite",
    "datastore_refresh_composite",
    "datastore_usage_composite",
    "event_tail_composite",
    "network_portgroup_audit_composite",
    "performance_summary_composite",
    "read_portgroup_switch_vlan_and_backing",
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
# singleton (``configurationEx`` + optional ``drsRecommendation``) -- the
# same vim seam the vm.migrate DRS lookup uses (#2970).
_OP_RETRIEVE_PROPERTIES = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
# The datastore-usage placement read asks for every VM in one call and follows
# the continuation pages vCenter may send (#4862).
_OP_CONTINUE_RETRIEVE_PROPERTIES = "POST:/PropertyCollector/{moId}/ContinueRetrievePropertiesEx"
_OP_POST_QUERY_EVENTS = "POST:/EventManager/{moId}/QueryEvents"
_OP_POST_QUERY_AVAILABLE_PERF_METRIC = "POST:/PerformanceManager/{moId}/QueryAvailablePerfMetric"
_OP_POST_QUERY_PERF = "POST:/PerformanceManager/{moId}/QueryPerf"
_OP_LIST_DATASTORES = "GET:/vcenter/datastore"
_OP_GET_DATASTORE = "GET:/vcenter/datastore/{datastore}"
# vim (VI-JSON) datastore cache-refresh methods (#3789). Both take only the
# Datastore MoRef (moid rides the path) and return 204/void; the pinned
# vi-json.yaml serves them as System.Read-privilege ops (a read-side refresh,
# not a config write). The ingested REST binding for RefreshDatastore is routed
# under ``/api`` and 400s on a standalone ESXi target (the #3534 class), so the
# composite dispatches them through the VI-JSON / ESXi-SOAP seam like every
# other vmomi read leg here.
_OP_POST_REFRESH_DATASTORE = "POST:/Datastore/{moId}/RefreshDatastore"
_OP_POST_REFRESH_DATASTORE_STORAGE_INFO = "POST:/Datastore/{moId}/RefreshDatastoreStorageInfo"
_OP_LIST_VMS = "GET:/vcenter/vm"
# There is NO distributed-switch list resource in the pinned REST spec at
# all: the plural ``distributed-switches`` path #1602 repointed to exists
# only under ``/vcenter/namespace-management/networks/nsx/`` (NSX-scoped)
# in the canonical ``vcenter.yaml`` -- the #2970 real-spec reconcile
# finding. There is likewise NO dedicated ``distributed-portgroup(s)`` list
# resource: distributed portgroups are enumerated via the generic network
# resource filtered to ``DISTRIBUTED_PORTGROUP`` (the singular
# ``distributed-portgroup`` op_id #508 declared was absent from every
# ingest -- #1602). The generic ``Network`` summary returns only
# ``{network (id), name, type}``, so the portgroup audit reads the parent
# switch, the VLAN and the connected VMs from the portgroup managed object
# through the PropertyCollector (#3802).
_OP_LIST_NETWORK = "GET:/vcenter/network"
_NETWORK_TYPE_DISTRIBUTED_PORTGROUP = "DISTRIBUTED_PORTGROUP"

# vim constants for the cluster DRS read. The PropertyCollector is a vim
# singleton whose moId is the literal ``propertyCollector`` -- the
# concrete value the ``{moId}`` path template takes at call time (the
# same convention the typed reads and write composites use).
_PROPERTY_COLLECTOR_MOID = "propertyCollector"
#: Objects per ``RetrievePropertiesEx`` in :func:`_retrieve_props_by_moid` --
#: small enough that vCenter does not page the answer.
_RETRIEVE_CHUNK = 50
_CLUSTER_COMPUTE_RESOURCE_MO_TYPE = "ClusterComputeResource"
# The cluster's whole configuration (a ``ClusterConfigInfoEx`` on a
# cluster). Its ``drsConfig`` member is the ``ClusterDrsConfigInfo``
# (``enabled`` / ``defaultVmBehavior`` / ``vmotionRate`` / ...),
# spec-verified against the pinned vi-json.yaml. The vim API declares the
# property as the base type ``ComputeResourceConfigInfo``, which has no
# ``drsConfig`` member, and vCenter 8.0.x rejects the path
# ``configurationEx.drsConfig`` with an HTTP 500 ``InvalidProperty`` (#3617).
# So the composite reads the whole object and picks ``drsConfig`` in code.
_PROP_CONFIGURATION_EX = "configurationEx"
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

# vim constants for the datastore-refresh read-back. ``Datastore.summary`` is a
# ``DatastoreSummary`` DataObject (name / url / capacity / freeSpace /
# accessible / type / ...); the composite reads it back through the same
# bounded PropertyCollector read after invoking RefreshDatastore.
_DATASTORE_MO_TYPE = "Datastore"
_PROP_DATASTORE_SUMMARY = "summary"

# vim properties for the portgroup audit (#3802). ``vm`` is the inherited
# ``Network.vm`` property: the VMs with a NIC on this portgroup. The
# portgroup's switch + port config property names are shared with the
# portgroup write composites. ``config.defaultPortConfig`` is read whole,
# never the deep ``config.defaultPortConfig.vlan`` path (vCenter 8.0.x
# answers that path with HTTP 500).
_PROP_NAME = "name"
_PROP_NETWORK_VM = "vm"
_PROP_VM_POWER_STATE = "runtime.powerState"
_VM_POWERED_ON = "poweredOn"
_PORTGROUP_AUDIT_PATHS: tuple[str, ...] = (
    _PROP_DVPG_DVS,
    _PROP_DVPG_DEFAULT_PORT_CONFIG,
    _PROP_NETWORK_VM,
)
#: The backing type of a distributed portgroup (read by ``vmware.network.list``
#: only, not by the audit): ``standard`` (made by vCenter) or ``nsx`` (an NSX
#: segment, made by NSX Manager). Since vSphere 7.0; unset means ``standard``.
_PROP_DVPG_BACKING_TYPE = "config.backingType"
_DVPG_DEFAULT_BACKING_TYPE = "standard"

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
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
)
_SUB_OPS_NETWORK_PORTGROUP_AUDIT: tuple[str, ...] = (
    _OP_LIST_NETWORK,
    _OP_RETRIEVE_PROPERTIES,
)
_SUB_OPS_DATASTORE_REFRESH: tuple[str, ...] = (
    _OP_POST_REFRESH_DATASTORE,
    _OP_POST_REFRESH_DATASTORE_STORAGE_INFO,
    _OP_RETRIEVE_PROPERTIES,
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
       -- the cluster's whole ``configurationEx``, from which the handler
       picks the DRS configuration (``drsConfig``, a
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
    path_set = [_PROP_CONFIGURATION_EX]
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
    config_ex = props.get(_PROP_CONFIGURATION_EX)
    drs_config = config_ex.get("drsConfig") if isinstance(config_ex, dict) else None
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


# vim types of the QueryEvents request body (spec-verified against the pinned
# vi-json.yaml: ``QueryEventsRequestType.filter`` is an ``EventFilterSpec``;
# its ``time`` / ``entity`` members are ``EventFilterSpecByTime`` /
# ``EventFilterSpecByEntity``). vCenter 8.0.x rejects a QueryEvents call with
# no body (HTTP 500 ``InvalidArgument``), and its deserialiser wants the
# ``_typeName`` tag on every DataObject (#3103), so the filter is always sent
# and always tagged. The body shape is the same on 8.0.x and 9.0: only the
# ``/sdk/vim25/{release}`` path segment differs. 9.0 adds an optional
# ``eventViewSpec`` argument, which this composite does not send.
_EVENT_FILTER_SPEC_TYPE = "EventFilterSpec"
_EVENT_FILTER_SPEC_BY_TIME_TYPE = "EventFilterSpecByTime"
_EVENT_FILTER_SPEC_BY_ENTITY_TYPE = "EventFilterSpecByEntity"
_EVENT_TAIL_DEFAULT_MAX_COUNT = 100
_EVENT_TAIL_DEFAULT_RECURSION = "all"
# Event argument fields that name the managed entity an event is about, most
# specific first. Each is an ``EntityEventArgument`` subtype carrying ``name``
# plus one MoRef member (``vm`` / ``datastore`` / ``host`` / ...).
_EVENT_ENTITY_ARGUMENTS: tuple[str, ...] = (
    "vm",
    "ds",
    "host",
    "net",
    "dvs",
    "computeResource",
    "datacenter",
)


def _build_query_events_body(params: dict[str, Any], max_count: int) -> dict[str, Any]:
    """Build the ``QueryEvents`` method args: one tagged ``EventFilterSpec``.

    Members are added in the order the vim schema declares them (``entity``,
    ``time``, ``disableFullMessage``, ``eventTypeId``, ``maxCount``).
    ``disableFullMessage`` is sent as ``false`` because the spec says an unset
    flag leaves ``fullFormattedMessage`` out, and the reduced rows need it.
    Time strings pass through as given; the parameter schema already checked
    they are RFC 3339.
    """
    spec: dict[str, Any] = {VIM_TYPE_NAME_KEY: _EVENT_FILTER_SPEC_TYPE}
    entity = params.get("entity")
    if isinstance(entity, dict):
        spec["entity"] = {
            VIM_TYPE_NAME_KEY: _EVENT_FILTER_SPEC_BY_ENTITY_TYPE,
            "entity": vim_moref(entity["type"], entity["moId"]),
            "recursion": entity.get("recursion", _EVENT_TAIL_DEFAULT_RECURSION),
        }
    begin_time = params.get("begin_time")
    end_time = params.get("end_time")
    if begin_time is not None or end_time is not None:
        window: dict[str, Any] = {VIM_TYPE_NAME_KEY: _EVENT_FILTER_SPEC_BY_TIME_TYPE}
        if begin_time is not None:
            window["beginTime"] = begin_time
        if end_time is not None:
            window["endTime"] = end_time
        spec["time"] = window
    spec["disableFullMessage"] = False
    event_type_ids = params.get("event_type_ids")
    if event_type_ids:
        spec["eventTypeId"] = list(event_type_ids)
    spec["maxCount"] = max_count
    return {"filter": spec}


def _event_explicit_entity(event: dict[str, Any]) -> dict[str, Any] | None:
    """The object an ``EventEx`` / ``ExtendedEvent`` names itself, else ``None``.

    ``EventEx`` carries ``objectId`` / ``objectType`` / ``objectName``;
    ``ExtendedEvent`` carries ``managedObject``. Both event classes also
    inherit the base ``Event`` context members, so these fields must be read
    before them.
    """
    object_id = event.get("objectId")
    if isinstance(object_id, str):
        object_type = event.get("objectType")
        object_name = event.get("objectName")
        return {
            "type": object_type if isinstance(object_type, str) else None,
            "moId": object_id,
            "name": object_name if isinstance(object_name, str) else None,
        }
    managed_object = event.get("managedObject")
    if isinstance(managed_object, dict) and isinstance(managed_object.get("value"), str):
        mo_type = managed_object.get("type")
        return {
            "type": mo_type if isinstance(mo_type, str) else None,
            "moId": managed_object["value"],
            "name": None,
        }
    return None


def _event_argument_entity(event: dict[str, Any]) -> dict[str, Any] | None:
    """The most specific ``EntityEventArgument`` member of an event, else ``None``."""
    for field in _EVENT_ENTITY_ARGUMENTS:
        argument = event.get(field)
        if not isinstance(argument, dict):
            continue
        moref = next(
            (
                value
                for value in argument.values()
                if isinstance(value, dict)
                and isinstance(value.get("type"), str)
                and isinstance(value.get("value"), str)
            ),
            None,
        )
        name = argument.get("name")
        return {
            "type": moref["type"] if moref else None,
            "moId": moref["value"] if moref else None,
            "name": name if isinstance(name, str) else None,
        }
    return None


def _event_entity(event: dict[str, Any]) -> dict[str, Any] | None:
    """The entity an event is about, as ``{type, moId, name}``, else ``None``.

    The event's own object (``EventEx`` ``objectId``, ``ExtendedEvent``
    ``managedObject``) wins. Only when an event names no object of its own do
    the base ``Event`` context members (``vm`` / ``ds`` / ``host`` / ...)
    count, most specific first. Reading the context first would turn an
    ``EventEx`` about a datastore into a row about its datacenter.
    """
    return _event_explicit_entity(event) or _event_argument_entity(event)


def _reduce_event(event: Any) -> dict[str, Any]:
    """Project one vim ``Event`` to ``{createdTime, eventTypeId, fullFormattedMessage, entity}``.

    ``eventTypeId`` follows the vim matching rule for
    ``EventFilterSpec.eventTypeId``: ``EventEx`` / ``ExtendedEvent`` carry an
    ``eventTypeId`` member, every other event's type id is its class name
    (the ``_typeName`` tag). So the value can be fed back as a filter.
    """
    raw = event if isinstance(event, dict) else {}
    created = raw.get("createdTime")
    type_id = raw.get("eventTypeId")
    if not isinstance(type_id, str):
        type_id = raw.get(VIM_TYPE_NAME_KEY)
    message = raw.get("fullFormattedMessage")
    return {
        "createdTime": created if isinstance(created, str) else None,
        "eventTypeId": type_id if isinstance(type_id, str) else None,
        "fullFormattedMessage": message if isinstance(message, str) else None,
        "entity": _event_entity(raw),
    }


async def event_tail_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Read recent vCenter events via EventManager.QueryEvents (vi-json).

    Op-id: ``vmware.composite.event.tail``.

    Sub-op read directly on the connector session (single call):

    1. ``POST:/EventManager/{moId}/QueryEvents`` -- always with a tagged
       ``EventFilterSpec`` body (see :func:`_build_query_events_body`).
       ``begin_time`` / ``end_time`` map to ``filter.time``,
       ``event_type_ids`` to ``filter.eventTypeId``, ``entity`` to
       ``filter.entity`` and ``max_count`` to ``filter.maxCount``. The
       server-side ``maxCount`` is the bound; the handler also cuts the list
       to ``max_count`` in case a server ignores it.

    Returns
    -------
    dict[str, Any]
        ``{"events": <list[reduced row]>, "count": <int>, "moId": <str>,
        "max_count_applied": <int>}``. Each row is ``{createdTime,
        eventTypeId, fullFormattedMessage, entity}``. The ``events`` list is
        what the dispatcher turns into a result handle when it is large.
    """
    mo_id = params.get("moId", "EventManager")
    max_count = int(params.get("max_count", _EVENT_TAIL_DEFAULT_MAX_COUNT))
    raw = await _read_sub_op(
        connector,
        target,
        operator,
        _OP_POST_QUERY_EVENTS,
        path_params={"moId": mo_id},
        body=_build_query_events_body(params, max_count),
    )
    events = _unwrap_value(raw)
    if events is None:
        # The pinned spec marks the QueryEvents response ``nullable``.
        events = []
    if not isinstance(events, list):
        # vi-json QueryEvents always returns a list. A non-list payload
        # is a connector-side bug -- surface it to the caller rather
        # than guess at the shape.
        raise RuntimeError(
            f"event_tail: expected list from {_OP_POST_QUERY_EVENTS!r}, got {type(events).__name__}"
        )
    rows = [_reduce_event(event) for event in events[:max_count]]
    return {
        "events": rows,
        "count": len(rows),
        "moId": mo_id,
        "max_count_applied": max_count,
    }


def _normalise_datastore_summary(summary: Any) -> dict[str, Any]:
    """Project a raw ``DatastoreSummary`` DataObject to the composite's summary row.

    *summary* is the unwrapped ``Datastore.summary`` value (a
    ``DatastoreSummary`` dict, or ``{}`` when the read returned nothing).
    ``capacity`` / ``freeSpace`` are ``xsd:long`` -- a JSON number on the
    VI-JSON arm, a bare numeric string on the ESXi SOAP arm (soap rule 8) --
    so both funnel through :func:`_int_or_none`; ``accessible`` is coerced to
    a strict ``bool`` (``None`` when absent / non-bool). Absent string fields
    map to ``None`` so the shape is stable regardless of transport.
    """
    ds = summary if isinstance(summary, dict) else {}
    accessible = ds.get("accessible")
    name = ds.get("name")
    ds_type = ds.get("type")
    url = ds.get("url")
    return {
        "name": name if isinstance(name, str) else None,
        "capacity": _int_or_none(ds.get("capacity")),
        "free_space": _int_or_none(ds.get("freeSpace")),
        "accessible": accessible if isinstance(accessible, bool) else None,
        "type": ds_type if isinstance(ds_type, str) else None,
        "url": url if isinstance(url, str) else None,
    }


async def datastore_refresh_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Refresh a datastore's cached capacity/free-space, then read the summary back.

    Op-id: ``vmware.composite.datastore.refresh``.

    ESXi caches an NFS datastore's capacity/freeSpace from mount time; after
    the export grows, ``Datastore.summary`` keeps the stale numbers until the
    host is told to re-probe. This composite issues that re-probe through the
    vim ``Datastore.RefreshDatastore`` method (and, when ``storage_info=true``,
    the deeper ``RefreshDatastoreStorageInfo``) and returns the freshly-read
    ``Datastore.summary`` -- so a downstream deploy pre-check sees the grown
    capacity without any SSH / govc. Read-side only: both methods are
    ``System.Read``-privilege in the pinned spec and change no configuration.

    Sub-ops read directly on the connector session, in order:

    1. ``POST:/Datastore/{moId}/RefreshDatastore`` -- the base cache refresh
       (void / 204). Load-bearing: a transport failure (e.g. a missing
       datastore's ``NotFound`` vim fault, HTTP 500) propagates so the
       dispatcher records it as ``connector_error``.
    2. ``POST:/Datastore/{moId}/RefreshDatastoreStorageInfo`` -- only when
       ``storage_info=true`` (void / 204).
    3. ``POST:/PropertyCollector/{moId}/RetrievePropertiesEx`` -- reads the
       refreshed ``Datastore.summary`` back (bounded single-object read, the
       same seam ``datastore.usage`` and ``object.collect`` use).

    Every POST leg routes through
    :meth:`~...connector.VmwareRestConnector._post_vmomi_json`, which serves
    VI-JSON on a vCenter target and hand-rolled SOAP on a standalone ESXi
    target (where ``Datastore.summary`` -- and the moid, a ``server:/export``
    NAS identifier -- ride the ``<_this type="Datastore">`` self-reference).

    Returns
    -------
    dict[str, Any]
        ``{"datastore": <moid>, "refreshed": True, "storage_info_refreshed":
        <bool>, "summary": {name, capacity, free_space, accessible, type,
        url}}``. ``capacity`` / ``free_space`` are bytes; a summary field is
        ``null`` only when the ``DatastoreSummary`` omitted it.
    """
    moid = params["datastore"]
    storage_info = bool(params.get("storage_info", False))

    await _read_sub_op(
        connector, target, operator, _OP_POST_REFRESH_DATASTORE, path_params={"moId": moid}
    )
    if storage_info:
        await _read_sub_op(
            connector,
            target,
            operator,
            _OP_POST_REFRESH_DATASTORE_STORAGE_INFO,
            path_params={"moId": moid},
        )

    retrieve = await _read_sub_op(
        connector,
        target,
        operator,
        _OP_RETRIEVE_PROPERTIES,
        path_params={"moId": _PROPERTY_COLLECTOR_MOID},
        body=retrieve_properties_body(_DATASTORE_MO_TYPE, [moid], [_PROP_DATASTORE_SUMMARY]),
    )
    props = _extract_props_by_moid(retrieve).get(moid, {})
    return {
        "datastore": moid,
        "refreshed": True,
        "storage_info_refreshed": storage_info,
        "summary": _normalise_datastore_summary(props.get(_PROP_DATASTORE_SUMMARY)),
    }


# vim types for the performance-summary entity (#4038). VI-JSON wants the
# ``entity`` argument of QueryAvailablePerfMetric / PerfQuerySpec as an
# annotated ManagedObjectReference; vCenter 8.0.3 answers a bare moid string
# with 500 ``InvalidArgument: Unexpected string value``. The type comes from
# the caller's ``entity_type`` or, failing that, from the moid prefix.
_PERF_ENTITY_TYPE_BY_MOID_PREFIX: tuple[tuple[str, str], ...] = (
    ("host-", "HostSystem"),
    ("vm-", _VIRTUAL_MACHINE_MO_TYPE),
    ("domain-c", _CLUSTER_COMPUTE_RESOURCE_MO_TYPE),
)
_PERF_QUERY_SPEC_TYPE = "PerfQuerySpec"


def _perf_entity_moref(entity_moid: str, entity_type: str | None) -> dict[str, str]:
    """The annotated ``ManagedObjectReference`` for the perf *entity_moid*.

    *entity_type* wins when given; otherwise the vim type comes from the
    moid prefix. Raises :class:`ValueError` when neither yields a type.
    """
    if entity_type:
        return vim_moref(entity_type, entity_moid)
    for prefix, mo_type in _PERF_ENTITY_TYPE_BY_MOID_PREFIX:
        if entity_moid.startswith(prefix):
            return vim_moref(mo_type, entity_moid)
    known = ", ".join(f"{prefix!r}" for prefix, _ in _PERF_ENTITY_TYPE_BY_MOID_PREFIX)
    raise ValueError(
        f"performance_summary: cannot tell the vim type of entity_moid "
        f"{entity_moid!r} from its prefix (known: {known}); pass entity_type"
    )


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

    The vi-json method arguments become the flat JSON request body; the
    ``moId`` targets the PerformanceManager singleton in the path -- the
    same method-args-as-body shape the ``vmware.host.usage`` typed op
    sends for RetrievePropertiesEx. Per the pinned ``vi-json.yaml``:
    QueryAvailablePerfMetric takes ``entity`` + ``intervalId``; QueryPerf
    takes ``querySpec`` (a ``PerfQuerySpec`` list carrying ``entity`` /
    ``intervalId`` / ``maxSample``). ``entity`` is an annotated
    ``ManagedObjectReference`` built by :func:`_perf_entity_moref` (#4038);
    vCenter 8.0.3 rejects a bare moid string.
    """
    entity_moid = params["entity_moid"]
    entity = _perf_entity_moref(entity_moid, params.get("entity_type"))
    perf_mgr_moid = params.get("perf_manager_moid", "PerfMgr")
    interval_s = int(params.get("interval_seconds", 20))
    max_samples = int(params.get("max_samples", 60))

    available_raw = await _read_sub_op(
        connector,
        target,
        operator,
        _OP_POST_QUERY_AVAILABLE_PERF_METRIC,
        path_params={"moId": perf_mgr_moid},
        body={"entity": entity, "intervalId": interval_s},
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
        body={
            "querySpec": [
                {
                    VIM_TYPE_NAME_KEY: _PERF_QUERY_SPEC_TYPE,
                    "entity": entity,
                    "intervalId": interval_s,
                    "maxSample": max_samples,
                }
            ]
        },
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
       ``datastore`` (the union of datastores the VM's files sit on), with
       every continuation page vCenter sends (#4862, through
       :func:`~meho_backplane.connectors.vmware_rest.vim_retrieve.retrieve_all_pages`).
       A VM with disks spanning two datastores therefore appears under
       both -- genuine placement, unlike the ignored-filter global list.

    VM-listing order is preserved so the per-datastore ``vm_names`` sample
    is deterministic. Returns ``({}, None)`` when there are no VMs. The
    read is **best-effort** (#1908): any transport error, a failed page
    or an answer that never ends returns ``(None, note)`` so the caller
    nulls enrichment on every row while keeping the capacity data the
    storage-usage use case needs.
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
        retrieve = await retrieve_all_pages(
            connector,
            target,
            operator,
            retrieve_properties_body(
                _VIRTUAL_MACHINE_MO_TYPE, list(names_by_moid), [_PROP_VM_DATASTORE]
            ),
        )
    except (httpx.HTTPError, RuntimeError) as exc:
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


async def _retrieve_props_by_moid(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    mo_type: str,
    moids: list[str],
    path_set: list[str],
) -> dict[str, dict[str, Any]]:
    """Read *path_set* on every ``(mo_type, moid)`` object, in chunks of 50.

    Returns ``{moid: {property: value}}``. No call is made when *moids* is
    empty, so an empty inventory costs no round trip.

    vCenter may page a ``RetrievePropertiesEx`` answer: it returns some
    objects plus a ``token`` for the rest. Small chunks keep a normal
    inventory from paging (same size as ``_teardown_reads.read_many``).
    If an answer still carries a ``token``, this raises instead of
    returning rows with missing objects.

    A successful call can also carry a property vCenter could not read
    (a ``missingSet`` entry with a fault, e.g. no permission). This raises
    on that too, so a caller never counts an object as absent because its
    data was unreadable. An unset property (no ``missingSet`` entry) is
    fine: it is simply absent from the returned dict.
    """
    by_moid: dict[str, dict[str, Any]] = {}
    for start in range(0, len(moids), _RETRIEVE_CHUNK):
        chunk = moids[start : start + _RETRIEVE_CHUNK]
        result = await _read_sub_op(
            connector,
            target,
            operator,
            _OP_RETRIEVE_PROPERTIES,
            path_params={"moId": _PROPERTY_COLLECTOR_MOID},
            body=retrieve_properties_body(mo_type, chunk, path_set),
        )
        payload = _unwrap_value(result)
        if isinstance(payload, dict) and payload.get("token"):
            raise RuntimeError(
                f"vCenter paged the {_OP_RETRIEVE_PROPERTIES!r} answer for "
                f"{len(chunk)} {mo_type} objects (it returned a token), so the "
                "answer is incomplete; refusing to return partial results"
            )
        unreadable = _unreadable_properties(result)
        if unreadable:
            detail = ", ".join(sorted(set(unreadable)))
            raise RuntimeError(
                f"vCenter could not read {len(unreadable)} of the requested "
                f"{mo_type} properties: {detail}; refusing to return partial results"
            )
        by_moid.update(_extract_props_by_moid(result))
    return by_moid


def _unreadable_properties(retrieve_result: Any) -> list[str]:
    """``"<path> (<fault type>)"`` for every ``missingSet`` entry in the answer.

    The HTTP call can succeed while vCenter still could not read a
    property on one of the objects (no permission, a system error, an
    object that vanished). Such a property is not in ``propSet``; it is
    in the object's ``missingSet`` with a fault. A property that is simply
    unset is left out of ``propSet`` with no ``missingSet`` entry, so this
    returns nothing for it. Only the fault **type name** is kept, never
    the fault text, which can echo a private identifier (#3708).
    """
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    unreadable: list[str] = []
    if not isinstance(objects, list):
        return unreadable
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for entry in obj.get("missingSet", []) or []:
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path, str):
                continue
            fault_type = fault_type_name(entry.get("fault")) or "unknown fault"
            unreadable.append(f"{path} ({fault_type})")
    return unreadable


def _moref_value(ref: Any) -> str | None:
    """The moid (``value``) of a vim MoRef dict, or ``None``."""
    value = ref.get("value") if isinstance(ref, dict) else None
    return value if isinstance(value, str) else None


async def _read_dvs_names(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    dvs_refs: list[dict[str, Any]],
) -> dict[str, str]:
    """Resolve ``{dvs_moid: display name}`` for the parent switches.

    The MoRef from ``config.distributedVirtualSwitch`` names the concrete
    switch type (``VmwareDistributedVirtualSwitch`` on a vSphere switch),
    so the read uses that type, one call per distinct type.
    """
    moids_by_type: dict[str, list[str]] = {}
    for ref in dvs_refs:
        moid = _moref_value(ref)
        ref_type = ref.get("type")
        mo_type = ref_type if isinstance(ref_type, str) else _DVS_MO_TYPE
        if moid is not None and moid not in moids_by_type.setdefault(mo_type, []):
            moids_by_type[mo_type].append(moid)
    names: dict[str, str] = {}
    for mo_type, moids in moids_by_type.items():
        props = await _retrieve_props_by_moid(
            connector, target, operator, mo_type, moids, [_PROP_NAME]
        )
        for moid, values in props.items():
            name = values.get(_PROP_NAME)
            if isinstance(name, str):
                names[moid] = name
    return names


async def _read_vm_names(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm_moids: list[str],
    *,
    powered_on_only: bool,
) -> dict[str, str]:
    """Resolve ``{vm_moid: name}``, keeping only powered-on VMs unless told otherwise."""
    props = await _retrieve_props_by_moid(
        connector,
        target,
        operator,
        _VIRTUAL_MACHINE_MO_TYPE,
        vm_moids,
        [_PROP_NAME, _PROP_VM_POWER_STATE],
    )
    names: dict[str, str] = {}
    for moid, values in props.items():
        name = values.get(_PROP_NAME)
        if not isinstance(name, str):
            continue
        if powered_on_only and values.get(_PROP_VM_POWER_STATE) != _VM_POWERED_ON:
            continue
        names[moid] = name
    return names


def _dvs_refs(pg_props: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The parent-switch MoRefs in a ``{portgroup moid: properties}`` read."""
    return [
        ref for props in pg_props.values() if isinstance(ref := props.get(_PROP_DVPG_DVS), dict)
    ]


def _portgroup_switch_and_vlan(
    props: dict[str, Any], dvs_names: dict[str, str]
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """``(switch moid, switch name, VLAN view)`` of one distributed portgroup.

    The VLAN view is the one :func:`_vlan_identity_view` renders, or ``None``
    for a private VLAN or a shape the switch did not report.
    """
    dvs_moid = _moref_value(props.get(_PROP_DVPG_DVS))
    dvs_name = dvs_names.get(dvs_moid) if dvs_moid is not None else None
    port_setting = props.get(_PROP_DVPG_DEFAULT_PORT_CONFIG)
    raw_vlan = port_setting.get("vlan") if isinstance(port_setting, dict) else None
    return dvs_moid, dvs_name, _vlan_identity_view(_observed_vlan_identity(raw_vlan))


async def read_portgroup_switch_vlan_and_backing(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    portgroup_moids: list[str],
) -> dict[str, tuple[str | None, str | None, dict[str, Any] | None, str | None]]:
    """Read the parent switch, default VLAN and backing type of distributed portgroups.

    Returns ``{portgroup moid: (switch moid, switch name, VLAN view, backing
    type)}`` for every moid passed. The backing type is ``standard`` or
    ``nsx``; an unset value is the vendor default ``standard``, and a
    portgroup the read did not return gets ``None``. The same vim reads as
    the portgroup audit (plus ``config.backingType``): one
    ``RetrievePropertiesEx`` over the portgroups, then one per switch type
    for the switch names. No call is made for an empty list. A failed read
    raises (:exc:`httpx.HTTPError` or :exc:`RuntimeError`).
    """
    pg_props = await _retrieve_props_by_moid(
        connector,
        target,
        operator,
        _DVPG_MO_TYPE,
        portgroup_moids,
        [_PROP_DVPG_DVS, _PROP_DVPG_DEFAULT_PORT_CONFIG, _PROP_DVPG_BACKING_TYPE],
    )
    dvs_names = await _read_dvs_names(connector, target, operator, _dvs_refs(pg_props))
    found: dict[str, tuple[str | None, str | None, dict[str, Any] | None, str | None]] = {}
    for moid in portgroup_moids:
        props = pg_props.get(moid, {})
        backing = props.get(_PROP_DVPG_BACKING_TYPE)
        if not isinstance(backing, str):
            backing = _DVPG_DEFAULT_BACKING_TYPE if props else None
        found[moid] = (*_portgroup_switch_and_vlan(props, dvs_names), backing)
    return found


def _portgroup_vm_moids(props: dict[str, Any]) -> list[str]:
    """The moids in a portgroup's ``vm`` property (VMs with a NIC on it)."""
    refs = props.get(_PROP_NETWORK_VM)
    if not isinstance(refs, list):
        return []
    return [moid for moid in (_moref_value(ref) for ref in refs) if moid is not None]


async def network_portgroup_audit_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Audit distributed portgroups: parent switch, VLAN and connected VMs.

    Op-id: ``vmware.composite.network.portgroup.audit``.

    Sub-ops read directly on the connector session:

    1. ``GET:/vcenter/network`` with ``filter.types=[DISTRIBUTED_PORTGROUP]``
       -- list distributed portgroups. The REST Automation API has no
       dedicated distributed-portgroup resource (#1602).
    2. One ``RetrievePropertiesEx`` over every portgroup reading
       ``config.distributedVirtualSwitch`` (parent switch),
       ``config.defaultPortConfig`` (read whole; ``vlan`` is picked in code
       because the deep ``config.defaultPortConfig.vlan`` path fails on
       vCenter 8.0.x) and ``vm`` (the VMs with a NIC on that portgroup).
    3. One ``RetrievePropertiesEx`` per switch type reading the parent
       switches' ``name``.
    4. One ``RetrievePropertiesEx`` over the union of connected VMs reading
       ``name`` + ``runtime.powerState``.

    Each vim read sends at most 50 objects per call. A paged answer (a
    ``token``) or a property vCenter could not read (a ``missingSet``
    fault, e.g. no permission on a VM's ``name``) fails the audit instead
    of dropping objects and reporting a wrong count. An unset property
    (no switch, VLAN or VMs reported) is fine.

    #3802: the old per-portgroup ``GET:/vcenter/vm`` call, filtered by
    network, is gone. The VM list resource has no network filter, so
    vCenter ignored it and every row got the whole VM inventory. The
    portgroup's own ``vm`` property scopes each row to its own VMs.

    ``filter_dvs`` keeps only the portgroups whose parent switch moid or
    name equals the value. Unless ``include_disconnected_vms`` is true,
    only powered-on VMs are counted.

    Returns
    -------
    dict[str, Any]
        ``{"portgroups": [{"id": ..., "name": ..., "dvs": <moid|None>,
        "dvs_name": <str|None>, "type": ..., "vlan": <view|None>,
        "vm_count": ..., "vm_names": [...]}, ...]}``. ``vlan`` is
        ``{"mode": "access", "vlan_id": N}`` or ``{"mode": "trunk",
        "ranges": [{"start", "end"}, ...]}``, and ``None`` for a private
        VLAN or a shape the switch did not report.
    """
    include_disconnected = bool(params.get("include_disconnected_vms", False))
    filter_dvs = params.get("filter_dvs")

    pg_query: dict[str, Any] = {"filter.types": [_NETWORK_TYPE_DISTRIBUTED_PORTGROUP]}
    pg_listing = await _read_sub_op(connector, target, operator, _OP_LIST_NETWORK, query=pg_query)
    pg_entries = _unwrap_value(pg_listing)
    if not isinstance(pg_entries, list):
        raise RuntimeError(
            f"network_portgroup_audit: expected list from {_OP_LIST_NETWORK!r}, "
            f"got {type(pg_entries).__name__}"
        )
    listed: dict[str, dict[str, Any]] = {}
    for entry in pg_entries:
        if not isinstance(entry, dict):
            continue
        pg_id = entry.get("network") or entry.get("portgroup")
        if isinstance(pg_id, str):
            listed[pg_id] = entry

    pg_props = await _retrieve_props_by_moid(
        connector, target, operator, _DVPG_MO_TYPE, list(listed), list(_PORTGROUP_AUDIT_PATHS)
    )
    dvs_names = await _read_dvs_names(connector, target, operator, _dvs_refs(pg_props))

    rows: list[tuple[dict[str, Any], list[str]]] = []
    for pg_id, entry in listed.items():
        props = pg_props.get(pg_id, {})
        dvs_moid, dvs_name, vlan = _portgroup_switch_and_vlan(props, dvs_names)
        if filter_dvs and filter_dvs not in (dvs_moid, dvs_name):
            continue
        row = {
            "id": pg_id,
            "name": entry.get("name"),
            "dvs": dvs_moid,
            "dvs_name": dvs_name,
            "type": entry.get("type"),
            "vlan": vlan,
        }
        rows.append((row, _portgroup_vm_moids(props)))

    all_vm_moids = list(dict.fromkeys(moid for _, moids in rows for moid in moids))
    vm_names_by_moid = await _read_vm_names(
        connector, target, operator, all_vm_moids, powered_on_only=not include_disconnected
    )
    aggregated: list[dict[str, Any]] = []
    for row, vm_moids in rows:
        vm_names = [vm_names_by_moid[moid] for moid in vm_moids if moid in vm_names_by_moid]
        row["vm_count"] = len(vm_names)
        row["vm_names"] = vm_names
        aggregated.append(row)
    return {"portgroups": aggregated}
