# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) read ops for :class:`VmwareRestConnector`.

The hand-authored vmware-rest surface has, until now, been composites
only (``vmware.composite.*`` -- registered via
:func:`~meho_backplane.connectors.vmware_rest.composites._register.register_vmware_composite_operations`
and dispatched through ``dispatch_child`` against ingested L2 sub-ops).
A composite therefore depends on the vCenter REST catalog having been
ingested first: its ``dispatch_child`` legs resolve ``GET:/vcenter/host``
and ``POST:/PropertyCollector/{moId}/RetrievePropertiesEx`` against
``endpoint_descriptor`` rows that only exist after ``vcenter.yaml`` is
ingested (the L2 pre-flight in ``composites/_preflight.py`` enforces
exactly that).

``vmware.host.usage`` is the first vmware **typed** op
(``source_kind="typed"``). It is a bound method on
:class:`VmwareRestConnector` that reads per-host utilisation directly
through the connector's own authenticated session -- no ``dispatch_child``,
no ingested descriptor -- so it works on a fresh boot with **zero catalog
ingest**. The sibling precedent is
:mod:`meho_backplane.connectors.argocd.ops`: metadata dataclasses here,
thin bound-method handlers on the connector, a module-level registrar
queued onto :func:`register_typed_op_registrar`.

Why the Web-Services-API and not plain REST
-------------------------------------------

``GET /api/vcenter/host`` returns only a ``HostSummary``
(``host`` / ``name`` / ``connection_state`` / ``power_state``) -- liveness,
not load. Per-host CPU/memory utilisation, hardware totals, and the
maintenance-mode flag live on the WS-API ``HostSystem`` managed object:

* ``summary.quickStats`` (:class:`HostListSummaryQuickStats`) --
  ``overallCpuUsage`` (MHz), ``overallMemoryUsage`` (MB), ``uptime`` (s).
* ``summary.hardware`` (:class:`HostHardwareSummary`) -- ``cpuMhz``
  (per-core MHz), ``numCpuPkgs`` / ``numCpuCores`` / ``numCpuThreads``,
  ``memorySize`` (bytes), ``cpuModel``.
* ``runtime.inMaintenanceMode`` (bool).

Those are read via the PropertyCollector ``RetrievePropertiesEx`` vi-json
method (singleton moId ``propertyCollector``), exactly as the
``host.network_uplinks`` composite reads ``config.network.*`` -- but here
the call is issued on the connector session and mounted through
:meth:`VmwareRestConnector.mount_op_path` so it lands on ``/api`` (modern
vCenter) or ``/rest`` (legacy / vcsim) without an ingested descriptor.

Field names + units are the vim25 / VI-JSON wire contract (camelCase):
`HostListSummaryQuickStats
<https://developer.broadcom.com/xapis/virtual-infrastructure-json-api/latest/data-structures/HostListSummary/>`_.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest._typed_response_schemas import (
    HOST_USAGE_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.vim_body import (
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VCENTER_SSO_ADMIN_API_FILE_GET_OP",
    "VMWARE_ALARM_TRIGGERED_LIST_OP",
    "VMWARE_CLUSTER_LIST_OP",
    "VMWARE_DATACENTER_LIST_OP",
    "VMWARE_FOLDER_LIST_OP",
    "VMWARE_HOST_DATASTORES_OP",
    "VMWARE_HOST_NETWORK_UPLINKS_OP",
    "VMWARE_HOST_STORAGE_DEVICES_OP",
    "VMWARE_HOST_USAGE_OP",
    "VMWARE_HOST_VSAN_HEALTH_OP",
    "VMWARE_LICENSE_ASSIGNED_LIST_OP",
    "VMWARE_LICENSE_LIST_OP",
    "VMWARE_NETWORK_LIST_OP",
    "VMWARE_NETWORK_PORTGROUP_SHOW_OP",
    "VMWARE_OBJECT_COLLECT_OP",
    "VMWARE_RESOURCE_POOL_LIST_OP",
    "VMWARE_SNAPSHOT_LIST_OP",
    "VMWARE_TASKS_HISTORY_OP",
    "VMWARE_TASKS_RECENT_OP",
    "VMWARE_TYPED_OPS",
    "VMWARE_TYPED_WHEN_TO_USE_BY_GROUP",
    "VMWARE_VM_HARDWARE_SHOW_OP",
    "VMWARE_VM_INFO_OP",
    "VmwareTypedOp",
    "host_usage_impl",
    "register_vmware_typed_operations",
    "register_vmware_typed_operations_8_0",
]

_log = structlog.get_logger(__name__)

# vCenter Automation REST host listing (spec-relative; mounted onto
# /api or /rest per-target by mount_op_path).
_LIST_HOSTS_PATH = "/vcenter/host"
# PropertyCollector RetrievePropertiesEx against the singleton
# ``propertyCollector`` moId. Spec-relative; mount_op_path prefixes the
# live mount so a legacy/vcsim target (which serves only /rest) is
# reached correctly rather than 404ing on /api.
_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_HOST_SYSTEM_MO_TYPE = "HostSystem"

# WS-API property paths read per host. Requesting the whole
# ``summary.quickStats`` / ``summary.hardware`` objects (rather than
# individual leaves) keeps the specSet small and returns every field the
# operator-facing row surfaces in one propSet entry each.
_PROP_QUICK_STATS = "summary.quickStats"
_PROP_HARDWARE = "summary.hardware"
_PROP_IN_MAINTENANCE = "runtime.inMaintenanceMode"
_HOST_USAGE_PATH_SET = (_PROP_QUICK_STATS, _PROP_HARDWARE, _PROP_IN_MAINTENANCE)

_HOST_USAGE_GROUP_KEY = "vmware-host-usage"


@dataclass(frozen=True)
class VmwareTypedOp:
    """Metadata for one vmware typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :func:`register_vmware_typed_operations` can splat the
    dataclass into the helper without per-op boilerplate. ``handler_attr``
    is the attribute name on
    :class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`
    exposing the async handler; the registrar resolves the bound method
    against the class so the dispatcher's
    :func:`~meho_backplane.operations._handler_resolve.import_handler` walk
    recovers the callable from the persisted ``module.ClassName.method``
    path. Mirrors
    :class:`~meho_backplane.connectors.argocd.ops.ArgoCdOp`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


def _unwrap_value(payload: Any) -> Any:
    """Return the inner ``value`` on a pre-7 REST envelope, else *payload*.

    vSphere's Automation REST straddles a bare-array modern shape and a
    legacy ``{"value": [...]}`` envelope (some vcsim builds). The caller
    only wants the list either way.
    """
    if isinstance(payload, dict) and set(payload.keys()) == {"value"}:
        return payload["value"]
    return payload


def build_host_usage_retrieve_params(host_moid: str) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` request body for one host.

    A single ``PropertyFilterSpec`` scoped directly to the host object
    (no ContainerView / TraversalSpec) requesting the three utilisation
    property paths. The singleton ``propertyCollector`` moId is carried
    in the URL (:data:`_RETRIEVE_PROPERTIES_PATH`), so the body is just
    the method arguments ``specSet`` + ``options`` -- the VI-JSON
    ``RetrievePropertiesExRequestType`` shape, ``_typeName``-annotated
    via the shared trio helper (#3103).
    """
    return retrieve_properties_body(_HOST_SYSTEM_MO_TYPE, [host_moid], _HOST_USAGE_PATH_SET)


def _extract_host_props(retrieve_result: Any) -> dict[str, Any]:
    """Flatten a single-host ``RetrievePropertiesEx`` result to name->val.

    ``RetrievePropertiesEx`` returns a ``RetrieveResult`` whose
    ``objects`` list carries one ``ObjectContent`` per queried object,
    each with a ``propSet`` list of ``{name, val}`` pairs. For the
    single-host query issued here the first object's propSet holds the
    three requested paths. A bare list (some simulators / the legacy
    ``RetrieveProperties`` shape) is tolerated too.
    """
    payload = _unwrap_value(retrieve_result)
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
                prop_by_name[prop["name"]] = unwrap_vim_value(prop.get("val"))
    return prop_by_name


def _int_or_none(value: Any) -> int | None:
    """Coerce a JSON number/numeric-string to int; anything else -> None.

    VI-JSON serialises the ``xsd:long`` ``memorySize`` and the ``xsd:int``
    quickStats counters as JSON numbers, but some intermediaries render a
    64-bit value as a JSON string. Accept both; reject bools (``True`` is
    an ``int`` subclass in Python and must not read as ``1`` here).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _parse_quick_stats(quick_stats: Any) -> dict[str, Any]:
    """Flatten ``HostListSummaryQuickStats`` into the operator-facing row.

    ``overallCpuUsage`` is aggregated CPU consumption in MHz;
    ``overallMemoryUsage`` is consumed physical memory in MB; ``uptime``
    is host uptime in seconds. Absent fields (a host mid-boot, a
    simulator that omits them) map to ``None``.
    """
    qs = quick_stats if isinstance(quick_stats, dict) else {}
    return {
        "overall_cpu_usage_mhz": _int_or_none(qs.get("overallCpuUsage")),
        "overall_memory_usage_mb": _int_or_none(qs.get("overallMemoryUsage")),
        "uptime_seconds": _int_or_none(qs.get("uptime")),
    }


def _parse_hardware(hardware: Any) -> dict[str, Any]:
    """Flatten ``HostHardwareSummary`` totals into the operator-facing row.

    ``cpuMhz`` is per-core clock speed in MHz; multiply by
    ``num_cpu_cores`` for the host's total CPU capacity against which
    ``overall_cpu_usage_mhz`` is the load. ``memorySize`` is total
    physical memory in **bytes** (quickStats' ``overallMemoryUsage`` is
    in MB -- different units, deliberately passed through raw).
    """
    hw = hardware if isinstance(hardware, dict) else {}
    cpu_model = hw.get("cpuModel")
    return {
        "cpu_model": cpu_model if isinstance(cpu_model, str) else None,
        "cpu_mhz": _int_or_none(hw.get("cpuMhz")),
        "num_cpu_packages": _int_or_none(hw.get("numCpuPkgs")),
        "num_cpu_cores": _int_or_none(hw.get("numCpuCores")),
        "num_cpu_threads": _int_or_none(hw.get("numCpuThreads")),
        "memory_size_bytes": _int_or_none(hw.get("memorySize")),
    }


def _in_maintenance_mode(value: Any) -> bool | None:
    """Coerce ``runtime.inMaintenanceMode`` to bool; absent -> ``None``."""
    return value if isinstance(value, bool) else None


async def _read_host_usage_row(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    host_moid: str,
    host_name: Any,
) -> dict[str, Any]:
    """Build one host row: identity + best-effort quickStats/hardware detail.

    The per-host WS-API property read is best-effort -- the host is
    already identified by the REST listing, so a failed vi-json
    ``RetrievePropertiesEx`` (a host that rejects the call, a transient
    auth expiry) nulls the ``quick_stats`` / ``hardware`` /
    ``in_maintenance_mode`` detail and records ``read_note`` rather than
    sinking the whole op. Mirrors ``host.network_uplinks``' best-effort
    per-host leg.
    """
    row: dict[str, Any] = {"id": host_moid, "name": host_name}
    try:
        props_result = await connector._post_vmomi_json(
            target,
            _RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=build_host_usage_retrieve_params(host_moid),
            promote_managed_object_not_found=True,
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        row["quick_stats"] = None
        row["hardware"] = None
        row["in_maintenance_mode"] = None
        row["read_note"] = (
            f"host-usage property read skipped: RetrievePropertiesEx for host "
            f"{host_moid!r} failed with {type(exc).__name__}: {exc}"
        )
        return row
    props = _extract_host_props(props_result)
    row["quick_stats"] = _parse_quick_stats(props.get(_PROP_QUICK_STATS))
    row["hardware"] = _parse_hardware(props.get(_PROP_HARDWARE))
    row["in_maintenance_mode"] = _in_maintenance_mode(props.get(_PROP_IN_MAINTENANCE))
    return row


async def host_usage_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.usage`` -- per-host utilisation.

    Reads, directly on the connector session (no ``dispatch_child``, no
    ingested descriptor):

    1. ``GET /vcenter/host`` (mounted) -- lists hosts, optionally narrowed
       by ``filter_hosts`` (a list of host MoRef ids). Load-bearing: a
       failure here raises and the dispatcher records it as a
       ``connector_error`` for the whole op.
    2. Per host: ``POST .../PropertyCollector/propertyCollector/RetrievePropertiesEx``
       reading ``summary.quickStats`` (CPU+memory load),
       ``summary.hardware`` (capacity totals), and
       ``runtime.inMaintenanceMode``. Best-effort per host.

    The listing GET routes through
    :meth:`VmwareRestConnector.mount_op_path` (``/api`` modern / ``/rest``
    legacy). The vmomi ``RetrievePropertiesEx`` read routes through
    :meth:`VmwareRestConnector._post_vmomi_json`, which mounts it on the
    documented VI-JSON base ``/sdk/vim25/{release}`` (with a single ``/api``
    fallback) so it resolves on vCenter 8.0.x instead of 404ing (#2466).

    Returns ``{"hosts": [{"id", "name", "quick_stats", "hardware",
    "in_maintenance_mode"}, ...]}``.
    """
    filter_hosts = [h for h in (params.get("filter_hosts") or []) if isinstance(h, str)]
    list_path = await connector.mount_op_path(target, _LIST_HOSTS_PATH, operator)
    listing_params: dict[str, Any] = {"filter.hosts": filter_hosts} if filter_hosts else {}
    # Key the ``filter.hosts`` param off the mount flavor (#2298): modern
    # ``/api`` wants the bare ``hosts`` name and 400s the prefixed form.
    listing_query = await connector.adapt_op_query(target, listing_params, operator)
    listing = await connector._get_json(target, list_path, operator=operator, params=listing_query)
    entries = _unwrap_value(listing)
    if not isinstance(entries, list):
        raise RuntimeError(
            f"vmware.host.usage: expected a list of hosts from GET "
            f"{_LIST_HOSTS_PATH!r}, got {type(entries).__name__}"
        )

    hosts: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        host_moid = entry.get("host")
        if not isinstance(host_moid, str):
            # vSphere REST returns the moid under ``host``; absence is an
            # upstream malformation -- skip rather than abort.
            continue
        hosts.append(
            await _read_host_usage_row(connector, operator, target, host_moid, entry.get("name"))
        )
    _log.info("vmware_host_usage_read", target=target.name, host_count=len(hosts))
    return {"hosts": hosts}


# ---------------------------------------------------------------------------
# Op metadata + registrar
# ---------------------------------------------------------------------------

# The other two vmware typed reads (#2258) live in sibling modules so
# each file stays inside the code-quality file-length budget. They are
# imported here -- after ``VmwareTypedOp`` and ``_unwrap_value`` are
# defined -- to break the import cycle (the sibling modules import both
# names from this module). E402 is expected: moving these to the top
# would re-introduce the cycle.
from meho_backplane.connectors.vmware_rest.typed_ops_alarm_triggered import (  # noqa: E402
    ALARM_TRIGGERED_GROUP_KEY,
    ALARM_TRIGGERED_WHEN_TO_USE,
    VMWARE_ALARM_TRIGGERED_LIST_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_appliance_local_accounts import (  # noqa: E402
    APPLIANCE_LOCAL_ACCOUNTS_GROUP_KEY,
    APPLIANCE_LOCAL_ACCOUNTS_OPS,
    APPLIANCE_LOCAL_ACCOUNTS_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.typed_ops_cluster_list import (  # noqa: E402
    CLUSTER_LIST_GROUP_KEY,
    CLUSTER_LIST_WHEN_TO_USE,
    VMWARE_CLUSTER_LIST_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_content_library import (  # noqa: E402
    CONTENT_LIBRARY_GROUP_KEY,
    CONTENT_LIBRARY_OPS,
    CONTENT_LIBRARY_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.typed_ops_content_library_find import (  # noqa: E402
    VMWARE_CONTENT_LIBRARY_FIND_OP,
    VMWARE_CONTENT_LIBRARY_ITEM_FIND_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_datacenter_list import (  # noqa: E402
    DATACENTER_LIST_GROUP_KEY,
    DATACENTER_LIST_WHEN_TO_USE,
    VMWARE_DATACENTER_LIST_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_folder_list import (  # noqa: E402
    FOLDER_LIST_GROUP_KEY,
    FOLDER_LIST_WHEN_TO_USE,
    VMWARE_FOLDER_LIST_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_config import (  # noqa: E402
    HOST_CONFIG_GROUP_KEY,
    HOST_CONFIG_WHEN_TO_USE,
    VMWARE_HOST_CONFIG_SHOW_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_datastores import (  # noqa: E402
    HOST_DATASTORES_GROUP_KEY,
    HOST_DATASTORES_WHEN_TO_USE,
    VMWARE_HOST_DATASTORES_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_network_uplinks import (  # noqa: E402
    HOST_NETWORK_UPLINKS_GROUP_KEY,
    HOST_NETWORK_UPLINKS_WHEN_TO_USE,
    VMWARE_HOST_NETWORK_UPLINKS_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_storage_devices import (  # noqa: E402
    HOST_STORAGE_DEVICES_GROUP_KEY,
    HOST_STORAGE_DEVICES_WHEN_TO_USE,
    VMWARE_HOST_STORAGE_DEVICES_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_vsan_health import (  # noqa: E402
    HOST_VSAN_HEALTH_GROUP_KEY,
    HOST_VSAN_HEALTH_WHEN_TO_USE,
    VMWARE_HOST_VSAN_HEALTH_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_license import (  # noqa: E402
    LICENSE_GROUP_KEY,
    LICENSE_WHEN_TO_USE,
    VMWARE_LICENSE_ASSIGNED_LIST_OP,
    VMWARE_LICENSE_LIST_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_network_list import (  # noqa: E402
    NETWORK_LIST_GROUP_KEY,
    NETWORK_LIST_WHEN_TO_USE,
    VMWARE_NETWORK_LIST_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_object_collect import (  # noqa: E402
    OBJECT_COLLECT_GROUP_KEY,
    OBJECT_COLLECT_WHEN_TO_USE,
    VMWARE_OBJECT_COLLECT_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_portgroup_show import (  # noqa: E402
    PORTGROUP_SHOW_GROUP_KEY,
    PORTGROUP_SHOW_WHEN_TO_USE,
    VMWARE_NETWORK_PORTGROUP_SHOW_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_resource_pool_list import (  # noqa: E402
    RESOURCE_POOL_LIST_GROUP_KEY,
    RESOURCE_POOL_LIST_WHEN_TO_USE,
    VMWARE_RESOURCE_POOL_LIST_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_snapshot_list import (  # noqa: E402
    SNAPSHOT_LIST_GROUP_KEY,
    SNAPSHOT_LIST_WHEN_TO_USE,
    VMWARE_SNAPSHOT_LIST_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_sso_admin_api_file import (  # noqa: E402
    SSO_ADMIN_API_FILE_GROUP_KEY,
    SSO_ADMIN_API_FILE_WHEN_TO_USE,
    VCENTER_SSO_ADMIN_API_FILE_GET_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_tasks_history import (  # noqa: E402
    TASKS_HISTORY_GROUP_KEY,
    TASKS_HISTORY_WHEN_TO_USE,
    VMWARE_TASKS_HISTORY_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent import (  # noqa: E402
    TASKS_RECENT_GROUP_KEY,
    TASKS_RECENT_WHEN_TO_USE,
    VMWARE_TASKS_RECENT_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vcenter_deployment_status import (  # noqa: E402
    DEPLOYMENT_STATUS_GROUP_KEY,
    DEPLOYMENT_STATUS_WHEN_TO_USE,
    VMWARE_VCENTER_DEPLOYMENT_STATUS_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import (  # noqa: E402
    VM_HARDWARE_GROUP_KEY,
    VM_HARDWARE_WHEN_TO_USE,
    VMWARE_VM_HARDWARE_SHOW_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_info import (  # noqa: E402
    VM_INFO_GROUP_KEY,
    VM_INFO_WHEN_TO_USE,
    VMWARE_VM_INFO_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_question import (  # noqa: E402
    VM_QUESTION_GROUP_KEY,
    VM_QUESTION_WHEN_TO_USE,
    VMWARE_VM_QUESTION_ANSWER_OP,
    VMWARE_VM_QUESTION_GET_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_reads import (  # noqa: E402
    VSAN_GROUP_KEY,
    VSAN_READ_OPS,
    VSAN_WHEN_TO_USE,
)

#: Curated ``when_to_use`` blurb per typed-op group.
#: ``register_typed_operation`` requires a non-empty string whenever
#: ``group_key`` is set (typed_register ``_validate_when_to_use_pairing``).
VMWARE_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _HOST_USAGE_GROUP_KEY: (
        "Use to read per-ESXi-host resource utilisation across a vCenter: "
        "current CPU load (overall_cpu_usage_mhz) and memory load "
        "(overall_memory_usage_mb) against each host's hardware capacity "
        "(cpu_mhz per core x num_cpu_cores, memory_size_bytes), plus whether "
        "the host is in maintenance mode. The right op when the question is "
        "'which hosts are hot / near capacity?', 'how much CPU/RAM headroom "
        "is left on this host?', or 'is this host in maintenance?' -- data the "
        "plain vCenter REST host summary (liveness only) cannot supply. "
        "Read-only."
    ),
    HOST_NETWORK_UPLINKS_GROUP_KEY: HOST_NETWORK_UPLINKS_WHEN_TO_USE,
    HOST_VSAN_HEALTH_GROUP_KEY: HOST_VSAN_HEALTH_WHEN_TO_USE,
    HOST_STORAGE_DEVICES_GROUP_KEY: HOST_STORAGE_DEVICES_WHEN_TO_USE,
    HOST_DATASTORES_GROUP_KEY: HOST_DATASTORES_WHEN_TO_USE,
    VM_INFO_GROUP_KEY: VM_INFO_WHEN_TO_USE,
    VM_HARDWARE_GROUP_KEY: VM_HARDWARE_WHEN_TO_USE,
    OBJECT_COLLECT_GROUP_KEY: OBJECT_COLLECT_WHEN_TO_USE,
    TASKS_RECENT_GROUP_KEY: TASKS_RECENT_WHEN_TO_USE,
    TASKS_HISTORY_GROUP_KEY: TASKS_HISTORY_WHEN_TO_USE,
    SSO_ADMIN_API_FILE_GROUP_KEY: SSO_ADMIN_API_FILE_WHEN_TO_USE,
    SNAPSHOT_LIST_GROUP_KEY: SNAPSHOT_LIST_WHEN_TO_USE,
    VM_QUESTION_GROUP_KEY: VM_QUESTION_WHEN_TO_USE,
    # Shared with the licence assign composite: the same key and text.
    LICENSE_GROUP_KEY: LICENSE_WHEN_TO_USE,
    APPLIANCE_LOCAL_ACCOUNTS_GROUP_KEY: APPLIANCE_LOCAL_ACCOUNTS_WHEN_TO_USE,
    ALARM_TRIGGERED_GROUP_KEY: ALARM_TRIGGERED_WHEN_TO_USE,
    CLUSTER_LIST_GROUP_KEY: CLUSTER_LIST_WHEN_TO_USE,
    RESOURCE_POOL_LIST_GROUP_KEY: RESOURCE_POOL_LIST_WHEN_TO_USE,
    NETWORK_LIST_GROUP_KEY: NETWORK_LIST_WHEN_TO_USE,
    FOLDER_LIST_GROUP_KEY: FOLDER_LIST_WHEN_TO_USE,
    # Shared with the content-library composites: the same key and text.
    CONTENT_LIBRARY_GROUP_KEY: CONTENT_LIBRARY_WHEN_TO_USE,
    DATACENTER_LIST_GROUP_KEY: DATACENTER_LIST_WHEN_TO_USE,
    # Shared with the three install composites: the same key and text.
    DEPLOYMENT_STATUS_GROUP_KEY: DEPLOYMENT_STATUS_WHEN_TO_USE,
    PORTGROUP_SHOW_GROUP_KEY: PORTGROUP_SHOW_WHEN_TO_USE,
    HOST_CONFIG_GROUP_KEY: HOST_CONFIG_WHEN_TO_USE,
    VSAN_GROUP_KEY: VSAN_WHEN_TO_USE,
}

VMWARE_HOST_USAGE_OP = VmwareTypedOp(
    op_id="vmware.host.usage",
    handler_attr="host_usage",
    summary="CPU and memory load, hardware totals and maintenance mode of each ESXi host.",
    description=(
        "Returns one row per ESXi host: CPU in use (MHz), memory in use (MB), uptime, CPU "
        "model, cores and clock speed, memory size (bytes) and whether the host is in "
        "maintenance mode. Reads the vSphere API directly, so it needs no catalog ingest. "
        "Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "filter_hosts": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "Optional list of host MoRef ids (e.g. 'host-12') to narrow "
                    "the report to. Omit to report every host in the vCenter."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{"filter_hosts": ["host-21"]}],
    },
    response_schema=HOST_USAGE_RESPONSE_SCHEMA,
    group_key=_HOST_USAGE_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "host", "utilisation"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see how busy each ESXi host is (CPU and memory), how much room is "
            "left, or whether a host is in maintenance mode. The vCenter host list alone "
            "only says if a host is up."
        ),
        "parameter_hints": {
            "filter_hosts": "Host moids, e.g. ['host-21']; omit for all hosts.",
        },
        "output_shape": (
            "{hosts: [{id, name, quick_stats: {overall_cpu_usage_mhz, "
            "overall_memory_usage_mb, uptime_seconds}, hardware: {cpu_model, cpu_mhz, "
            "num_cpu_packages, num_cpu_cores, num_cpu_threads, memory_size_bytes}, "
            "in_maintenance_mode, read_note}]}. When a host's read failed, quick_stats, "
            "hardware and in_maintenance_mode are null and read_note says why."
        ),
        "next_step": (
            "CPU use in percent is overall_cpu_usage_mhz / (cpu_mhz * num_cpu_cores) * 100. "
            "Memory use in percent is overall_memory_usage_mb * 1048576 / "
            "memory_size_bytes * 100 (MB against bytes). For one VM, use vmware.vm.info "
            "or vmware.composite.performance.summary."
        ),
    },
)

#: The typed ops :class:`VmwareRestConnector` registers at lifespan startup.
VMWARE_TYPED_OPS: tuple[VmwareTypedOp, ...] = (
    VMWARE_HOST_USAGE_OP,
    VMWARE_HOST_NETWORK_UPLINKS_OP,
    VMWARE_HOST_VSAN_HEALTH_OP,
    VMWARE_HOST_STORAGE_DEVICES_OP,
    VMWARE_HOST_DATASTORES_OP,
    VMWARE_VM_INFO_OP,
    VMWARE_VM_HARDWARE_SHOW_OP,
    VMWARE_OBJECT_COLLECT_OP,
    VMWARE_TASKS_RECENT_OP,
    VMWARE_TASKS_HISTORY_OP,
    VCENTER_SSO_ADMIN_API_FILE_GET_OP,
    VMWARE_SNAPSHOT_LIST_OP,
    VMWARE_LICENSE_LIST_OP,
    VMWARE_LICENSE_ASSIGNED_LIST_OP,
    *APPLIANCE_LOCAL_ACCOUNTS_OPS,
    VMWARE_ALARM_TRIGGERED_LIST_OP,
    VMWARE_VM_QUESTION_GET_OP,
    VMWARE_VM_QUESTION_ANSWER_OP,
    VMWARE_RESOURCE_POOL_LIST_OP,
    VMWARE_NETWORK_LIST_OP,
    VMWARE_CONTENT_LIBRARY_FIND_OP,
    VMWARE_CONTENT_LIBRARY_ITEM_FIND_OP,
    VMWARE_CLUSTER_LIST_OP,
    VMWARE_DATACENTER_LIST_OP,
    VMWARE_FOLDER_LIST_OP,
    *CONTENT_LIBRARY_OPS,
    VMWARE_VCENTER_DEPLOYMENT_STATUS_OP,
    VMWARE_NETWORK_PORTGROUP_SHOW_OP,
    VMWARE_HOST_CONFIG_SHOW_OP,
    *VSAN_READ_OPS,
)


async def register_vmware_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
    version: str | None = None,
) -> None:
    """Upsert every op in :data:`VMWARE_TYPED_OPS` into ``endpoint_descriptor``.

    ``version`` picks the catalog; ``None`` means the 9.0 catalog.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after
    :func:`~meho_backplane.connectors.registry._eager_import_connectors`
    has walked every connector subpackage, so the descriptor rows land
    before the first dispatch. Idempotent across pod restarts (the helper
    skips the embedding recompute on unchanged summary / description /
    tags). Mirrors :func:`register_vmware_composite_operations` and the
    argocd typed-op registrar.

    The ``embedding_service`` keyword-only parameter is the runner
    contract: :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar,
    so each registrar must accept the kwarg. It is forwarded to
    :func:`register_typed_operation` (which falls back to the process-wide
    singleton when ``None``).
    """
    # Lazy import: the operations package pulls in the embedding pipeline
    # (ONNX runtime + model), which pure connector/handler unit tests
    # should not pay. Lifespan callers have it warmed by the time this runs.
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    version = version or VmwareRestConnector.version
    for op in VMWARE_TYPED_OPS:
        handler = getattr(VmwareRestConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"VmwareRestConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else VMWARE_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"VmwareRestConnector typed op {op.op_id!r} declares "
                f"group_key={op.group_key!r} but no curated when_to_use exists for "
                f"that key. Add an entry to VMWARE_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=VmwareRestConnector.product,
            version=version,
            impl_id=VmwareRestConnector.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=op.group_key,
            when_to_use=when_to_use,
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
    _log.info(
        "vmware_typed_operations_registered",
        count=len(VMWARE_TYPED_OPS),
        product=VmwareRestConnector.product,
        version=version,
        impl_id=VmwareRestConnector.impl_id,
    )


async def register_vmware_typed_operations_8_0(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """The same typed ops on ``vmware-rest-8.0``, where 8.0.x targets resolve (#3789).

    The dispatcher binds the :class:`VmwareRestConnector` method handlers to
    the resolved :class:`VmwareRest80Connector` instance (a subclass).
    """
    from meho_backplane.connectors.vmware_rest.connector import VmwareRest80Connector

    await register_vmware_typed_operations(
        embedding_service=embedding_service, version=VmwareRest80Connector.version
    )
