# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the vSAN reads (``vmware.vsan.*``).

Six typed reads give an agent the vSAN of a cluster: its settings, its
capacity, its performance, the objects that resync, the object health, and
what a host's maintenance mode would do to vSAN data. Each lives in its own
module; this one holds what they share:

* the group (:data:`VSAN_GROUP_KEY`) and its text;
* small readers that turn a vSAN answer into plain values. vCenter 9.x sends
  the answer as VI-JSON. vCenter 8.0 sends it as SOAP
  (:meth:`VmwareRestConnector._post_vsan` picks the transport), where a number
  is text and a list with one entry is a single value. The readers accept both.
* :func:`vsan_call`, which sends one vSAN method and raises when the answer
  is not the data object the method returns. So a failed read never looks
  like an empty, healthy one.
* :func:`read_properties`, one property read of core vSphere objects
  (``PropertyCollector.RetrievePropertiesEx``, every page).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.typed_ops import _int_or_none, _unwrap_value
from meho_backplane.connectors.vmware_rest.vim_body import (
    retrieve_properties_body,
    unwrap_vim_value,
    vim_moref,
)
from meho_backplane.connectors.vmware_rest.vim_retrieve import retrieve_all_pages

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest._mount import VsanMethod
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "CLUSTER_MO_TYPE",
    "CLUSTER_PARAM_SCHEMA",
    "FROM_CACHE_PARAM_SCHEMA",
    "HOST_MO_TYPE",
    "VSAN_GROUP_KEY",
    "VSAN_WHEN_TO_USE",
    "as_bool",
    "as_float",
    "as_int",
    "as_text",
    "cluster_ref",
    "dict_items",
    "health_summary_body",
    "read_properties",
    "text_items",
    "vsan_call",
]

CLUSTER_MO_TYPE: Final = "ClusterComputeResource"
HOST_MO_TYPE: Final = "HostSystem"

VSAN_GROUP_KEY: Final = "vmware-vsan"

VSAN_WHEN_TO_USE: Final = (
    "Use to read the vSAN of a cluster. The group holds six reads: the vSAN "
    "settings of a cluster with its fault domains and witness host "
    "(vmware.vsan.cluster.info), capacity and space use with the effect of a "
    "host failure (vmware.vsan.capacity), latency, IOPS and throughput over a "
    "time window (vmware.vsan.performance), the objects that resync now "
    "(vmware.vsan.resync.status), object health with the VMs of unhealthy "
    "objects (vmware.vsan.object.health), and what a host's maintenance mode "
    "would do to vSAN data (vmware.vsan.maintenance.precheck). It holds no "
    "changes. For the vSAN health tests of a cluster, use vmware.host.vsan_health."
)

#: The ``cluster`` parameter every vSAN read takes.
CLUSTER_PARAM_SCHEMA: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Managed object id of the vSAN cluster, for example 'domain-c7'. "
        "vmware.cluster.list lists the clusters."
    ),
}


#: The ``from_cache`` parameter of the reads that use the health summary.
FROM_CACHE_PARAM_SCHEMA: Final[dict[str, Any]] = {
    "type": "boolean",
    "default": False,
    "description": (
        "When true, vSAN answers from the last stored health check instead of a fresh "
        "check. Use it while a host is down or in maintenance mode, when a fresh check can "
        "be too slow. Default false."
    ),
}


def cluster_ref(cluster_moid: str) -> dict[str, str]:
    """The ``cluster`` argument of a vSAN method: the cluster's managed object reference."""
    return vim_moref(CLUSTER_MO_TYPE, cluster_moid)


def dict_items(value: Any) -> list[dict[str, Any]]:
    """The data objects of a vSAN list field, as a list (SOAP sends one entry bare)."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def text_items(value: Any) -> list[str]:
    """The strings of a vSAN list field, as a list (SOAP sends one entry bare)."""
    items = value if isinstance(value, list) else [value]
    return [item for item in items if isinstance(item, str) and item]


def as_text(value: Any) -> str | None:
    """A non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


def as_int(value: Any) -> int | None:
    """A whole number from VI-JSON (a number) or SOAP (text), else ``None``."""
    return _int_or_none(value)


def as_float(value: Any) -> float | None:
    """A number from VI-JSON (a number) or SOAP (text), else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def as_bool(value: Any) -> bool | None:
    """``True`` / ``False`` (the SOAP codec already reads ``true`` / ``false``), else ``None``."""
    return value if isinstance(value, bool) else None


def health_summary_body(
    cluster_moid: str, *, field: str, include_obj_uuids: bool, from_cache: bool
) -> dict[str, Any]:
    """A ``VsanQueryVcClusterHealthSummary`` body that asks for one part of the summary.

    The keys follow the order the method defines (``cluster``,
    ``includeObjUuids``, ``fields``, ``fetchFromCache``); SOAP needs it.
    """
    body: dict[str, Any] = {
        "cluster": cluster_ref(cluster_moid),
        "includeObjUuids": include_obj_uuids,
        "fields": [field],
    }
    if from_cache:
        body["fetchFromCache"] = True
    return body


async def vsan_call(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    method: VsanMethod,
    body: dict[str, Any],
    *,
    what: str,
    empty_ok: bool = False,
) -> Any:
    """Send one vSAN method and return its answer, with the VI-JSON boxes removed.

    A vSAN fault is not caught: the dispatcher answers with vCenter's fault
    type and text (a missing cluster becomes ``not_found``). An empty answer
    (no body, ``{}`` or no SOAP ``returnval``) raises :class:`RuntimeError`
    naming *what* was read, so a failed read never looks like an empty one.
    With *empty_ok* (a method whose result is an optional list, such as the
    witness hosts) an empty answer is ``[]`` instead.
    """
    answer = unwrap_vim_value(
        _unwrap_value(await connector._post_vsan(target, method, operator=operator, json=body))
    )
    if isinstance(answer, (dict, list)) and answer:
        return answer
    if empty_ok and answer in (None, {}, []):
        return []
    raise RuntimeError(f"vSAN {what}: vCenter sent no answer to {method.method}")


async def read_properties(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    mo_type: str,
    moids: list[str],
    paths: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Read *paths* of the *moids* objects: ``{moid: {path: value}}``.

    One ``RetrievePropertiesEx`` with every page. A missing object (for example
    a wrong cluster id) becomes MEHO's ``not_found``. A property vCenter could
    not read is left out of the object's dict.
    """
    body = retrieve_properties_body(mo_type, moids, paths)
    result = await retrieve_all_pages(connector, target, operator, body, promote_missing=True)
    found: dict[str, dict[str, Any]] = {}
    for obj in result.get("objects", []):
        ref = obj.get("obj") if isinstance(obj, dict) else None
        moid = as_text(ref.get("value")) if isinstance(ref, dict) else None
        if moid is None:
            continue
        found[moid] = {
            prop["name"]: unwrap_vim_value(prop.get("val"))
            for prop in obj.get("propSet") or []
            if isinstance(prop, dict) and isinstance(prop.get("name"), str)
        }
    return found
