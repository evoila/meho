# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.cluster.list`` typed op -- the clusters of one vCenter.

Reads the vCenter on the connector's own session, so it works on a fresh
install with no catalog ingest:

1. ``GET /vcenter/datacenter`` lists the datacenters (moid and name), or only
   the one the ``datacenter`` filter names. This call must work.
2. Per datacenter, ``GET /vcenter/cluster`` with the ``datacenters`` filter
   (and the ``folders`` filter when ``folder`` is set) lists its clusters with
   the HA and DRS state. These calls must work too, so a datacenter's clusters
   are never missing without an error. The ``name`` pattern is applied here.
3. One VI-JSON property read of all listed clusters
   (``PropertyCollector.RetrievePropertiesEx``, every page) for the host count
   (``summary.numHosts``) and the vSAN state (``configurationEx``, read whole:
   vCenter 8.0 refuses a path inside it). This read is best-effort: when it
   fails, the two fields are ``null`` and ``read_note`` says why.

The rows are flat, so a long list rides a result handle like any other list.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.vim_body import (
    fault_type_name,
    retrieve_properties_body,
    unwrap_vim_value,
)
from meho_backplane.connectors.vmware_rest.vim_retrieve import retrieve_all_pages

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "CLUSTER_DATACENTER_FILTER_PARAM",
    "CLUSTER_FOLDER_FILTER_PARAM",
    "CLUSTER_LIST_GROUP_KEY",
    "CLUSTER_LIST_PATH",
    "CLUSTER_LIST_WHEN_TO_USE",
    "CLUSTER_MO_TYPE",
    "CLUSTER_PROPERTY_PATHS",
    "CLUSTER_SUMMARY_FIELDS",
    "DATACENTER_LIST_PATH",
    "VMWARE_CLUSTER_LIST_OP",
    "cluster_list_impl",
]

#: Spec-relative REST paths; ``mount_op_path`` puts them on ``/api`` or ``/rest``.
DATACENTER_LIST_PATH: Final = "/vcenter/datacenter"
CLUSTER_LIST_PATH: Final = "/vcenter/cluster"

#: FilterSpec fields, written in the legacy ``filter.`` form; ``adapt_op_query``
#: drops the prefix on ``/api``. ``datacenters`` works on both list calls.
CLUSTER_DATACENTER_FILTER_PARAM: Final = "datacenters"
CLUSTER_FOLDER_FILTER_PARAM: Final = "folders"

#: The fields of a REST ``Cluster.Summary`` the operation reads.
CLUSTER_SUMMARY_FIELDS: Final = ("cluster", "name", "ha_enabled", "drs_enabled")

#: The VI-JSON object type and the two properties read for every cluster.
CLUSTER_MO_TYPE: Final = "ClusterComputeResource"
_PROP_NUM_HOSTS: Final = "summary.numHosts"
_PROP_CONFIGURATION_EX: Final = "configurationEx"
CLUSTER_PROPERTY_PATHS: Final = (_PROP_NUM_HOSTS, _PROP_CONFIGURATION_EX)

CLUSTER_LIST_GROUP_KEY: Final = "vmware-clusters"

CLUSTER_LIST_WHEN_TO_USE: Final = (
    "Use to see the clusters of a vCenter: their names and ids, the datacenter of "
    "each, how many hosts each has, and whether HA, DRS and vSAN are on. Start here "
    "when you need a cluster id for another call. Read-only."
)


def _log() -> Any:
    """Get the logger per call, so ``structlog.testing.capture_logs`` sees every event."""
    return structlog.get_logger(__name__)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _name_matches(name: Any, pattern: str | None) -> bool:
    """Match *name* against a ``*`` / ``?`` pattern; letter case does not matter."""
    if pattern is None:
        return True
    if not isinstance(name, str):
        return False
    return fnmatch.fnmatchcase(name.casefold(), pattern.casefold())


async def _get_list(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    path: str,
    query: dict[str, Any] | None,
) -> list[Any]:
    """GET one REST list on the target's mount; raise when the answer is not a list."""
    mounted = await connector.mount_op_path(target, path, operator)
    wire_query = await connector.adapt_op_query(target, query, operator)
    answer = _unwrap_value(
        await connector._get_json(target, mounted, operator=operator, params=wire_query)
    )
    if not isinstance(answer, list):
        raise RuntimeError(
            f"vmware.cluster.list: expected a list from GET {path!r}, got {type(answer).__name__}"
        )
    return answer


async def _datacenters(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    datacenter: str | None,
) -> list[tuple[str, str | None]]:
    """``(moid, name)`` of every datacenter, or of the one *datacenter* names."""
    query = {f"filter.{CLUSTER_DATACENTER_FILTER_PARAM}": [datacenter]} if datacenter else None
    entries = await _get_list(connector, operator, target, DATACENTER_LIST_PATH, query)
    found: list[tuple[str, str | None]] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("datacenter"), str):
            found.append((entry["datacenter"], _text(entry.get("name"))))
    return found


def _rest_row(entry: Any, dc_id: str, dc_name: str | None) -> dict[str, Any] | None:
    """The REST half of one row, or ``None`` for an entry without a moid."""
    if not isinstance(entry, dict) or not isinstance(entry.get("cluster"), str):
        return None
    return {
        "id": entry["cluster"],
        "name": _text(entry.get("name")),
        "datacenter_id": dc_id,
        "datacenter_name": dc_name,
        "host_count": None,
        "ha_enabled": _bool(entry.get("ha_enabled")),
        "drs_enabled": _bool(entry.get("drs_enabled")),
        "vsan_enabled": None,
        "read_note": None,
    }


def _object_moid(obj: dict[str, Any]) -> str | None:
    ref = obj.get("obj")
    return _text(ref.get("value")) if isinstance(ref, dict) else None


def _vsan_enabled(configuration_ex: Any) -> bool | None:
    """``vsanConfigInfo.enabled``; a cluster config with no vSAN part means vSAN is off."""
    if not isinstance(configuration_ex, dict):
        return None
    vsan = configuration_ex.get("vsanConfigInfo")
    if vsan is None:
        return False
    return _bool(vsan.get("enabled")) if isinstance(vsan, dict) else None


def _apply_properties(rows: dict[str, dict[str, Any]], result: dict[str, Any]) -> None:
    """Fill ``host_count`` and ``vsan_enabled`` from one property read; note what is missing."""
    seen: set[str] = set()
    for obj in result.get("objects", []):
        moid = _object_moid(obj)
        row = rows.get(moid) if moid else None
        if row is None:
            continue
        seen.add(row["id"])
        props = {
            prop["name"]: unwrap_vim_value(prop.get("val"))
            for prop in obj.get("propSet") or []
            if isinstance(prop, dict) and isinstance(prop.get("name"), str)
        }
        row["host_count"] = _int(props.get(_PROP_NUM_HOSTS))
        row["vsan_enabled"] = _vsan_enabled(props.get(_PROP_CONFIGURATION_EX))
        missing = [
            f"{entry.get('path')} ({fault_type_name(entry.get('fault')) or 'fault'})"
            for entry in obj.get("missingSet") or []
            if isinstance(entry, dict)
        ]
        if missing:
            row["read_note"] = f"vCenter could not read {', '.join(missing)}"
    for moid, row in rows.items():
        if moid not in seen:
            row["read_note"] = "vCenter sent no host count or vSAN state for this cluster"


async def _add_properties(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    rows: list[dict[str, Any]],
) -> None:
    """Best-effort host count and vSAN state for all *rows*, in one paged read."""
    by_moid = {row["id"]: row for row in rows}
    body = retrieve_properties_body(CLUSTER_MO_TYPE, list(by_moid), CLUSTER_PROPERTY_PATHS)
    try:
        result = await retrieve_all_pages(connector, target, operator, body)
    except (httpx.HTTPError, RuntimeError) as exc:
        note = (
            f"host count and vSAN state not read: RetrievePropertiesEx failed with "
            f"{type(exc).__name__}: {exc}"
        )
        for row in rows:
            row["read_note"] = note
        return
    _apply_properties(by_moid, result)


async def cluster_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.cluster.list``. Returns ``{"clusters": [...]}``."""
    datacenter = _text(params.get("datacenter"))
    folder = _text(params.get("folder"))
    pattern = _text(params.get("name"))
    rows: list[dict[str, Any]] = []
    for dc_id, dc_name in await _datacenters(connector, operator, target, datacenter):
        query: dict[str, Any] = {f"filter.{CLUSTER_DATACENTER_FILTER_PARAM}": [dc_id]}
        if folder:
            query[f"filter.{CLUSTER_FOLDER_FILTER_PARAM}"] = [folder]
        for entry in await _get_list(connector, operator, target, CLUSTER_LIST_PATH, query):
            row = _rest_row(entry, dc_id, dc_name)
            if row is not None and _name_matches(row["name"], pattern):
                rows.append(row)
    if rows:
        await _add_properties(connector, operator, target, rows)
    _log().info(
        "vmware_cluster_list_read",
        target=target.name,
        cluster_count=len(rows),
        filtered=any(value is not None for value in (datacenter, folder, pattern)),
    )
    return {"clusters": rows}


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_ON_OFF = "True when {what} is on for this cluster. Null when vCenter did not say."

_CLUSTER_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Cluster moid, for example 'domain-c8'."},
        "name": {"type": ["string", "null"], "description": "Cluster name."},
        "datacenter_id": {
            "type": "string",
            "description": "Moid of the datacenter that holds the cluster.",
        },
        "datacenter_name": {"type": ["string", "null"], "description": "Datacenter name."},
        "host_count": {
            "type": ["integer", "null"],
            "description": "Number of ESXi hosts in the cluster. Null when it could not be "
            "read; read_note says why.",
        },
        "ha_enabled": {"type": ["boolean", "null"], "description": _ON_OFF.format(what="HA")},
        "drs_enabled": {"type": ["boolean", "null"], "description": _ON_OFF.format(what="DRS")},
        "vsan_enabled": {
            "type": ["boolean", "null"],
            "description": "True when vSAN is on for this cluster. Null when it could not be "
            "read; read_note says why.",
        },
        "read_note": {
            "type": ["string", "null"],
            "description": "Why host_count or vsan_enabled is null. Null when both were read.",
        },
    },
    "required": [
        "id",
        "name",
        "datacenter_id",
        "datacenter_name",
        "host_count",
        "ha_enabled",
        "drs_enabled",
        "vsan_enabled",
        "read_note",
    ],
}

VMWARE_CLUSTER_LIST_OP = VmwareTypedOp(
    op_id="vmware.cluster.list",
    handler_attr="cluster_list",
    summary="List the clusters of a vCenter, with datacenter, host count and HA, DRS and vSAN.",
    description=(
        "Lists the clusters of each datacenter (GET /vcenter/cluster), then reads the host "
        "count and vSAN state of all of them in one property read. One row per cluster. "
        "Filters: datacenter, folder, name pattern. vCenter only. Needs no catalog ingest. "
        "Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "datacenter": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": (
                    "Optional. Moid of one datacenter, for example 'datacenter-3'. Lists only "
                    "its clusters. An unknown id gives an empty list."
                ),
            },
            "folder": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": (
                    "Optional. Moid of a host folder, for example 'group-h4'. Lists only the "
                    "clusters in that folder."
                ),
            },
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": (
                    "Optional. Show only clusters whose name matches this pattern. Use * for "
                    "any text and ? for one character. Letter case does not matter."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{}, {"datacenter": "<datacenter-moid>", "name": "<cluster-name>*"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "clusters": {
                "type": "array",
                "description": "One row per cluster that matches the filters.",
                "items": _CLUSTER_ROW_SCHEMA,
            }
        },
        "required": ["clusters"],
    },
    group_key=CLUSTER_LIST_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "cluster", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which clusters a vCenter has, to get a cluster id for another "
            "call, or for an overview: the datacenter, the number of hosts, and whether "
            "HA, DRS and vSAN are on."
        ),
        "parameter_hints": {
            "datacenter": "A datacenter moid such as 'datacenter-3'. Omit it for all.",
            "folder": "A host folder moid such as 'group-h4'. Omit it for all.",
            "name": "A pattern such as 'prod*'. Omit it for all.",
        },
        "output_shape": (
            "{clusters: [{id, name, datacenter_id, datacenter_name, host_count, ha_enabled, "
            "drs_enabled, vsan_enabled, read_note}]}. id is the moid, for example "
            "'domain-c8'. host_count and vsan_enabled are null when their read failed; "
            "read_note says why. A long list comes back as a result handle."
        ),
        "next_step": (
            "For more fields of one cluster, call vmware.object.collect with type "
            "'ClusterComputeResource', the id as moid, and the property paths you need "
            "(request 'configurationEx' whole for HA and DRS settings). For DRS advice, "
            "call vmware.composite.cluster.drs_recommendations with the id as cluster."
        ),
    },
)
