# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.host.usage``: CPU and memory load of each ESXi host.

The first vmware **typed** op (``source_kind="typed"``). It is a bound
method on :class:`VmwareRestConnector` that reads per-host utilisation
directly through the connector's own authenticated session -- no
``dispatch_child``, no ingested descriptor -- so it works on a fresh boot
with **zero catalog ingest**. The sibling precedent is
:mod:`meho_backplane.connectors.argocd.ops`: metadata dataclasses, thin
bound-method handlers on the connector, a module-level registrar queued
onto :func:`register_typed_op_registrar`.

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

from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest._typed_response_schemas import (
    HOST_USAGE_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VmwareTypedOp,
    _int_or_none,
    _unwrap_value,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "HOST_USAGE_GROUP_KEY",
    "HOST_USAGE_WHEN_TO_USE",
    "TYPED_OPS",
    "VMWARE_HOST_USAGE_OP",
    "WHEN_TO_USE_BY_GROUP",
    "build_host_usage_retrieve_params",
    "host_usage_impl",
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

HOST_USAGE_GROUP_KEY = "vmware-host-usage"


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


HOST_USAGE_WHEN_TO_USE = (
    "Use to read per-ESXi-host resource utilisation across a vCenter: "
    "current CPU load (overall_cpu_usage_mhz) and memory load "
    "(overall_memory_usage_mb) against each host's hardware capacity "
    "(cpu_mhz per core x num_cpu_cores, memory_size_bytes), plus whether "
    "the host is in maintenance mode. The right op when the question is "
    "'which hosts are hot / near capacity?', 'how much CPU/RAM headroom "
    "is left on this host?', or 'is this host in maintenance?' -- data the "
    "plain vCenter REST host summary (liveness only) cannot supply. "
    "Read-only."
)

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
    group_key=HOST_USAGE_GROUP_KEY,
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

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_HOST_USAGE_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {HOST_USAGE_GROUP_KEY: HOST_USAGE_WHEN_TO_USE}
