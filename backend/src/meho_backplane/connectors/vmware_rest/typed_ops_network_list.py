# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.network.list`` typed op -- every network of one vCenter.

Lists the networks a VM can use: standard port groups, distributed port
groups (an NSX segment on a distributed switch is one of them) and opaque
networks (managed outside vSphere, the older host-level NSX switch). It reads the
vSphere Automation REST API on the connector's own session, so it works on
a fresh install with no catalog ingest:

1. ``GET /vcenter/datacenter`` lists the datacenters (moid and name).
2. ``GET /vcenter/network`` once per datacenter (filter ``datacenters``),
   with the optional ``types`` and ``folders`` filters. So every row knows
   its datacenter. The optional ``name`` pattern is applied here, in MEHO.
   A failed list fails the operation.
3. For the distributed port groups only, the parent switch, the default
   VLAN and the backing type come from the vim properties
   ``config.distributedVirtualSwitch``, ``config.defaultPortConfig`` and
   ``config.backingType`` of ``DistributedVirtualPortgroup``
   (:func:`~meho_backplane.connectors.vmware_rest.composites._read.read_portgroup_switch_vlan_and_backing`,
   the switch and VLAN code of ``vmware.composite.network.portgroup.audit``).
   Since vSphere 7.0 an NSX segment is a distributed port group with backing
   type ``nsx``, so the REST list types it ``DISTRIBUTED_PORTGROUP``; the row
   field ``nsx_segment`` shows it. A failed vim read leaves the switch, VLAN
   and ``nsx_segment`` ``null``; ``read_note`` says why.

The rows are flat, so a long list rides a result handle like any other list.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "DATACENTER_LIST_PATH",
    "NETWORK_FILTER_PARAMS",
    "NETWORK_LIST_GROUP_KEY",
    "NETWORK_LIST_PATH",
    "NETWORK_LIST_WHEN_TO_USE",
    "NETWORK_TYPES",
    "VMWARE_NETWORK_LIST_OP",
    "network_list_impl",
]

#: Spec-relative REST paths; ``mount_op_path`` puts them on ``/api`` or ``/rest``.
DATACENTER_LIST_PATH = "/vcenter/datacenter"
NETWORK_LIST_PATH = "/vcenter/network"

#: The values of the vendor enum ``Network.Type``.
NETWORK_TYPES: tuple[str, ...] = ("STANDARD_PORTGROUP", "DISTRIBUTED_PORTGROUP", "OPAQUE_NETWORK")
_DISTRIBUTED_PORTGROUP = "DISTRIBUTED_PORTGROUP"
#: ``config.backingType`` of a distributed port group made by NSX Manager.
_NSX_BACKING = "nsx"

#: Operation parameter -> the FilterSpec field of ``GET /vcenter/network``.
#: Sent in the legacy ``filter.`` form; ``adapt_op_query`` drops the prefix on ``/api``.
NETWORK_FILTER_PARAMS: dict[str, str] = {"type": "types", "folder": "folders"}
_DATACENTER_FILTER = "datacenters"

NETWORK_LIST_GROUP_KEY = "vmware-networks"

NETWORK_LIST_WHEN_TO_USE = (
    "Use to see the networks of a vCenter that a VM can use: standard port groups, "
    "distributed port groups and opaque networks, with their ids, datacenter and, for a "
    "distributed port group, its switch, VLAN and whether it is an NSX segment. Start "
    "here when you need a network id for another call. Read-only."
)


def _log() -> Any:
    """Get the logger per call, so ``structlog.testing.capture_logs`` sees every event."""
    return structlog.get_logger(__name__)


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
    query: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """GET one REST list on the target's mount; raise when the answer is not a list."""
    mounted = await connector.mount_op_path(target, path, operator)
    wire_query = await connector.adapt_op_query(target, query, operator)
    answer = _unwrap_value(
        await connector._get_json(target, mounted, operator=operator, params=wire_query)
    )
    if not isinstance(answer, list):
        raise RuntimeError(
            f"vmware.network.list: expected a list from GET {path!r}, got {type(answer).__name__}"
        )
    return [entry for entry in answer if isinstance(entry, dict)]


async def _datacenters(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    only: str | None,
) -> dict[str, str | None]:
    """Datacenter moid -> name; only *only* when the caller gave a datacenter."""
    names: dict[str, str | None] = {}
    for entry in await _get_list(connector, operator, target, DATACENTER_LIST_PATH):
        moid = entry.get("datacenter")
        if isinstance(moid, str):
            name = entry.get("name")
            names[moid] = name if isinstance(name, str) else None
    if only is None:
        return names
    return {only: names.get(only)}


def _vlan_fields(vlan: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten the portgroup audit's VLAN view into three row fields."""
    if vlan is None:
        return {"vlan_mode": None, "vlan_id": None, "vlan_trunk_ranges": None}
    if vlan["mode"] == "trunk":
        ranges = ",".join(
            str(r["start"]) if r["start"] == r["end"] else f"{r['start']}-{r['end']}"
            for r in vlan["ranges"]
        )
        return {"vlan_mode": "trunk", "vlan_id": None, "vlan_trunk_ranges": ranges}
    return {"vlan_mode": "access", "vlan_id": vlan["vlan_id"], "vlan_trunk_ranges": None}


_PortgroupDetails = tuple[str | None, str | None, dict[str, Any] | None, str | None]


async def _portgroup_details(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    portgroup_ids: list[str],
) -> tuple[dict[str, _PortgroupDetails], str | None]:
    """Switch, VLAN and backing type per distributed port group. Returns ``(by id, note)``."""
    from meho_backplane.connectors.vmware_rest.composites._read import (
        read_portgroup_switch_vlan_and_backing,
    )

    if not portgroup_ids:
        return {}, None
    try:
        found = await read_portgroup_switch_vlan_and_backing(
            connector, target, operator, portgroup_ids
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        return {}, f"switch, VLAN and backing not read: {type(exc).__name__}: {exc}"
    return found, None


async def network_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.network.list``. Returns ``{"networks": [...]}``."""
    raw_pattern = params.get("name")
    pattern = raw_pattern if isinstance(raw_pattern, str) else None
    raw_dc = params.get("datacenter")
    rest_filter = {
        f"filter.{field}": [params[param]]
        for param, field in NETWORK_FILTER_PARAMS.items()
        if isinstance(params.get(param), str)
    }
    datacenters = await _datacenters(
        connector, operator, target, raw_dc if isinstance(raw_dc, str) else None
    )
    # network moid -> (summary, datacenter moid, datacenter name)
    listed: dict[str, tuple[dict[str, Any], str, str | None]] = {}
    for dc_id, dc_name in datacenters.items():
        query = {**rest_filter, f"filter.{_DATACENTER_FILTER}": [dc_id]}
        for entry in await _get_list(connector, operator, target, NETWORK_LIST_PATH, query):
            net_id = entry.get("network")
            if (
                isinstance(net_id, str)
                and net_id not in listed
                and _name_matches(entry.get("name"), pattern)
            ):
                listed[net_id] = (entry, dc_id, dc_name)

    portgroup_ids = [
        net_id
        for net_id, (entry, _, _) in listed.items()
        if entry.get("type") == _DISTRIBUTED_PORTGROUP
    ]
    details, note = await _portgroup_details(connector, operator, target, portgroup_ids)

    rows: list[dict[str, Any]] = []
    for net_id, (entry, dc_id, dc_name) in listed.items():
        is_portgroup = net_id in portgroup_ids
        switch_id, switch_name, vlan, backing = details.get(net_id, (None, None, None, None))
        name = entry.get("name")
        net_type = entry.get("type")
        rows.append(
            {
                "id": net_id,
                "name": name if isinstance(name, str) else None,
                "type": net_type if isinstance(net_type, str) else None,
                "datacenter": dc_id,
                "datacenter_name": dc_name,
                "switch_id": switch_id,
                "switch_name": switch_name,
                **_vlan_fields(vlan),
                "nsx_segment": None if backing is None else backing == _NSX_BACKING,
                "read_note": note if is_portgroup else None,
            }
        )
    _log().info(
        "vmware_network_list_read",
        target=target.name,
        network_count=len(rows),
        filtered=bool(rest_filter) or pattern is not None or isinstance(raw_dc, str),
    )
    return {"networks": rows}


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_MOID_PARAM: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 128}

_NETWORK_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Network moid, for example 'network-12'."},
        "name": {"type": ["string", "null"], "description": "Network name."},
        "type": {
            "type": ["string", "null"],
            "description": (
                "STANDARD_PORTGROUP (on the hosts), DISTRIBUTED_PORTGROUP (on a "
                "distributed switch; an NSX segment is one, see nsx_segment) or "
                "OPAQUE_NETWORK (managed outside vSphere, for example on the older "
                "host-level NSX switch)."
            ),
        },
        "datacenter": {"type": "string", "description": "Moid of the datacenter of the network."},
        "datacenter_name": {"type": ["string", "null"], "description": "Datacenter name."},
        "switch_id": {
            "type": ["string", "null"],
            "description": "Moid of the distributed switch. Only for a distributed port group.",
        },
        "switch_name": {
            "type": ["string", "null"],
            "description": "Name of the distributed switch. Only for a distributed port group.",
        },
        "vlan_mode": {
            "type": ["string", "null"],
            "enum": ["access", "trunk", None],
            "description": (
                "Default VLAN of a distributed port group: access (one VLAN) or trunk "
                "(VLAN ranges). Null for other types, a private VLAN or an unknown shape."
            ),
        },
        "vlan_id": {
            "type": ["integer", "null"],
            "description": "The VLAN id in access mode. 0 means no VLAN tag.",
        },
        "vlan_trunk_ranges": {
            "type": ["string", "null"],
            "description": "The VLAN ranges in trunk mode, for example '100-200,300'.",
        },
        "nsx_segment": {
            "type": ["boolean", "null"],
            "description": (
                "Only for a distributed port group: true when NSX made it (an NSX "
                "segment, config.backingType 'nsx'), false when vCenter made it. Null "
                "for other types or when the read failed."
            ),
        },
        "read_note": {
            "type": ["string", "null"],
            "description": (
                "Why the switch, VLAN and nsx_segment of a distributed port group are null."
            ),
        },
    },
    "required": [
        "id",
        "name",
        "type",
        "datacenter",
        "datacenter_name",
        "switch_id",
        "switch_name",
        "vlan_mode",
        "vlan_id",
        "vlan_trunk_ranges",
        "nsx_segment",
        "read_note",
    ],
}

VMWARE_NETWORK_LIST_OP = VmwareTypedOp(
    op_id="vmware.network.list",
    handler_attr="network_list",
    summary="List the networks of a vCenter: port groups, NSX segments and opaque networks.",
    description=(
        "Reads GET /vcenter/network per datacenter. Returns one row per network: id (moid), "
        "name, type, datacenter and, for a distributed port group, its switch, default "
        "VLAN and nsx_segment. Filters: type, datacenter, folder, name pattern. vCenter "
        "only. Needs no catalog ingest. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": list(NETWORK_TYPES),
                "description": (
                    "Optional. Show only one type: STANDARD_PORTGROUP, "
                    "DISTRIBUTED_PORTGROUP (this includes NSX segments on a distributed "
                    "switch) or OPAQUE_NETWORK (the older host-level NSX switch)."
                ),
            },
            "datacenter": {
                **_MOID_PARAM,
                "description": (
                    "Optional. Datacenter moid, for example 'datacenter-3'. Show only "
                    "the networks in this datacenter."
                ),
            },
            "folder": {
                **_MOID_PARAM,
                "description": (
                    "Optional. Network folder moid, for example 'group-n5'. Show only "
                    "the networks in this folder."
                ),
            },
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": (
                    "Optional. Show only networks whose name matches this pattern. Use * "
                    "for any text and ? for one character. Letter case does not matter."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{}, {"type": "DISTRIBUTED_PORTGROUP", "name": "<network-name>*"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "networks": {
                "type": "array",
                "description": "One row per network that matches the filters.",
                "items": _NETWORK_ROW_SCHEMA,
            }
        },
        "required": ["networks"],
    },
    group_key=NETWORK_LIST_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "network", "networking", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which networks a vCenter has, to get a network id for another "
            "call (for example to connect a VM NIC), to check the switch and VLAN of "
            "a distributed port group, or to find the NSX segments."
        ),
        "parameter_hints": {
            "type": (
                "Omit it for every type, or pass one value of the enum. For NSX segments "
                "pass DISTRIBUTED_PORTGROUP and keep the rows with nsx_segment true."
            ),
            "datacenter": "A datacenter moid such as 'datacenter-3'; omit for every one.",
            "folder": "A network folder moid such as 'group-n5'; omit for every folder.",
            "name": "Omit it for every network, or pass a pattern such as 'prod*'.",
        },
        "output_shape": (
            "{networks: [{id, name, type, datacenter, datacenter_name, switch_id, "
            "switch_name, vlan_mode, vlan_id, vlan_trunk_ranges, nsx_segment, read_note}]}. "
            "The switch, VLAN and nsx_segment fields are set only for a distributed port "
            "group; read_note says why they are null when the read failed."
        ),
        "next_step": (
            "Use id where another call needs a network moid. For the VMs on each "
            "distributed port group, call vmware.composite.network.portgroup.audit."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_NETWORK_LIST_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {NETWORK_LIST_GROUP_KEY: NETWORK_LIST_WHEN_TO_USE}
