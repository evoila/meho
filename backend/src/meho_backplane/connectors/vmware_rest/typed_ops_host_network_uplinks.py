# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.host.network_uplinks`` typed op (#2258).

Re-ships the former ``vmware.composite.host.network_uplinks`` read as a
``source_kind="typed"`` bound method on :class:`VmwareRestConnector`, in
the :mod:`~meho_backplane.connectors.vmware_rest.typed_ops` mould
established by ``vmware.host.usage`` (#2257). The op reads per-host
physical-NIC link state + speed and their proxy-switch uplink
association directly on the connector session -- no ``dispatch_child``,
no ingested descriptor -- so it works on a fresh boot with **zero
catalog ingest**.

The pnic link-state / uplink mapping is the one read the plain vSphere
Automation REST surface cannot reproduce: pnic link state, speed, and
proxy-switch uplink association are Web-Services-API ``HostNetworkInfo``
properties (``config.network.pnic`` + ``config.network.proxySwitch``),
reached via the PropertyCollector ``RetrievePropertiesEx`` vi-json
method. This is what drives physical switch-port-occupancy reasoning
("are we out of switch ports?").

The request-building + parse logic is carried over verbatim from the
composite; only the dispatch mechanism changed (composite
``_read_sub_op`` -> the same ``mount_op_path`` + ``_get_json`` /
``_post_json`` calls ``host.usage`` issues).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "HOST_NETWORK_UPLINKS_GROUP_KEY",
    "HOST_NETWORK_UPLINKS_WHEN_TO_USE",
    "VMWARE_HOST_NETWORK_UPLINKS_OP",
    "build_host_network_uplinks_retrieve_params",
    "host_network_uplinks_impl",
]

_log = structlog.get_logger(__name__)

# vCenter Automation REST host listing (spec-relative; mounted onto
# /api or /rest per-target by mount_op_path).
_LIST_HOSTS_PATH = "/vcenter/host"
# PropertyCollector RetrievePropertiesEx against the singleton
# ``propertyCollector`` moId (carried in the path, so the body is only
# the method arguments).
_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_HOST_SYSTEM_MO_TYPE = "HostSystem"
_HOST_NET_PROP_PNIC = "config.network.pnic"
_HOST_NET_PROP_PROXYSWITCH = "config.network.proxySwitch"
_HOST_NET_PATH_SET = (_HOST_NET_PROP_PNIC, _HOST_NET_PROP_PROXYSWITCH)

HOST_NETWORK_UPLINKS_GROUP_KEY = "vmware-host-network-uplinks"


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
    single-host query the op issues, the first object's propSet holds
    the two requested property paths. Returns the raw pnic and
    proxySwitch lists (empty when absent).
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
                prop_by_name[prop["name"]] = prop.get("val")
    pnics = prop_by_name.get(_HOST_NET_PROP_PNIC)
    proxy_switches = prop_by_name.get(_HOST_NET_PROP_PROXYSWITCH)
    return (
        pnics if isinstance(pnics, list) else [],
        proxy_switches if isinstance(proxy_switches, list) else [],
    )


def build_host_network_uplinks_retrieve_params(host_moid: str) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` request body for one host's network config.

    A single ``PropertyFilterSpec`` scoped directly to the host object
    (no ContainerView / TraversalSpec) requesting the two network config
    property paths. The ``propertyCollector`` singleton moId rides the
    request path (:data:`_RETRIEVE_PROPERTIES_PATH`), so the body is just
    the ``specSet`` + ``options`` method arguments -- the VI-JSON
    ``RetrievePropertiesExRequestType`` shape ``vmware.host.usage``
    sends.
    """
    return {
        "specSet": [
            {
                "propSet": [
                    {
                        "type": _HOST_SYSTEM_MO_TYPE,
                        "pathSet": list(_HOST_NET_PATH_SET),
                    }
                ],
                "objectSet": [{"obj": {"type": _HOST_SYSTEM_MO_TYPE, "value": host_moid}}],
            }
        ],
        "options": {},
    }


async def _read_host_uplink_row(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    host_moid: str,
    host_name: Any,
) -> dict[str, Any]:
    """Build one host row: identity + best-effort pnic / proxy-switch detail.

    The per-host WS-API property read is best-effort -- the host is
    already identified by the REST listing, so a failed vi-json
    ``RetrievePropertiesEx`` call nulls the network detail and records
    why (``read_note``) rather than sinking the whole op. Mirrors
    ``host.usage``' best-effort per-host leg.
    """
    row: dict[str, Any] = {"id": host_moid, "name": host_name}
    try:
        props_result = await connector._post_vmomi_json(
            target,
            _RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=build_host_network_uplinks_retrieve_params(host_moid),
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        row["pnics"] = None
        row["proxy_switches"] = None
        row["read_note"] = (
            f"host-network property read skipped: RetrievePropertiesEx for host "
            f"{host_moid!r} failed with {type(exc).__name__}: {exc}"
        )
        return row
    raw_pnics, raw_proxy_switches = _extract_host_network_props(props_result)
    row["pnics"] = [_parse_pnic(p) for p in raw_pnics if isinstance(p, dict)]
    row["proxy_switches"] = [
        _parse_proxy_switch(ps) for ps in raw_proxy_switches if isinstance(ps, dict)
    ]
    return row


async def host_network_uplinks_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.network_uplinks`` -- per-host pnic uplinks.

    Reads, directly on the connector session (no ``dispatch_child``, no
    ingested descriptor):

    1. ``GET /vcenter/host`` (mounted) -- lists hosts, optionally narrowed
       by ``filter_hosts`` (a list of host MoRef ids). Load-bearing: a
       failure here raises and the dispatcher records it as a
       ``connector_error`` for the whole op.
    2. Per host: ``POST .../PropertyCollector/propertyCollector/RetrievePropertiesEx``
       requesting ``config.network.pnic`` +
       ``config.network.proxySwitch`` on that single HostSystem object.
       Best-effort per host.

    The listing GET routes through
    :meth:`VmwareRestConnector.mount_op_path` (``/api`` modern / ``/rest``
    legacy). The vmomi ``RetrievePropertiesEx`` read routes through
    :meth:`VmwareRestConnector._post_vmomi_json`, which mounts it on the
    documented VI-JSON base ``/sdk/vim25/{release}`` (with a single ``/api``
    fallback) so it resolves on vCenter 8.0.x instead of 404ing (#2466).

    Returns ``{"hosts": [{"id", "name", "pnics", "proxy_switches"}, ...]}``.
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
            f"vmware.host.network_uplinks: expected a list of hosts from GET "
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
            await _read_host_uplink_row(connector, operator, target, host_moid, entry.get("name"))
        )
    _log.info("vmware_host_network_uplinks_read", target=target.name, host_count=len(hosts))
    return {"hosts": hosts}


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filter_hosts": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Optional list of host managed-object IDs (e.g. 'host-42'). "
                "When supplied, only these hosts are surfaced; the listing "
                "narrows to them via the mount's host filter param. Empty / "
                "absent returns every host the operator can see."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hosts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Host managed-object ID.",
                    },
                    "name": {
                        "type": ["string", "null"],
                        "description": "Host name from the listing row.",
                    },
                    "pnics": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "object",
                            "properties": {
                                "device": {
                                    "type": ["string", "null"],
                                    "description": "Physical NIC device name (e.g. 'vmnic0').",
                                },
                                "mac": {
                                    "type": ["string", "null"],
                                    "description": "MAC address of the physical NIC.",
                                },
                                "driver": {
                                    "type": ["string", "null"],
                                    "description": "Driver name backing the physical NIC.",
                                },
                                "link_up": {
                                    "type": "boolean",
                                    "description": (
                                        "True when the WS-API ``linkSpeed`` object is "
                                        "present (the API omits it when the link is "
                                        "down)."
                                    ),
                                },
                                "speed_mb": {
                                    "type": ["integer", "null"],
                                    "description": (
                                        "Current link speed in Mb/s from "
                                        "``linkSpeed.speedMb``; ``null`` when the link "
                                        "is down."
                                    ),
                                },
                                "duplex": {
                                    "type": ["boolean", "null"],
                                    "description": (
                                        "Full-duplex flag from ``linkSpeed.duplex``; "
                                        "``null`` when the link is down."
                                    ),
                                },
                            },
                            "required": ["device", "link_up"],
                        },
                        "description": (
                            "Physical NICs on the host from "
                            "``config.network.pnic``; ``null`` when the best-effort "
                            "per-host property read was skipped (see ``read_note``)."
                        ),
                    },
                    "proxy_switches": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {
                                    "type": ["string", "null"],
                                    "description": "Proxy-switch key on the host.",
                                },
                                "dvs_name": {
                                    "type": ["string", "null"],
                                    "description": (
                                        "Name of the DVS this proxy switch backs (``dvsName``)."
                                    ),
                                },
                                "dvs_uuid": {
                                    "type": ["string", "null"],
                                    "description": "UUID of the backing DVS (``dvsUuid``).",
                                },
                                "uplink_pnics": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Physical-NIC device names bound to this proxy "
                                        "switch as uplinks (from the proxy switch's "
                                        "``pnic`` backing)."
                                    ),
                                },
                            },
                            "required": ["uplink_pnics"],
                        },
                        "description": (
                            "Proxy switches on the host from "
                            "``config.network.proxySwitch``; ``null`` when the "
                            "best-effort per-host property read was skipped (see "
                            "``read_note``)."
                        ),
                    },
                    "read_note": {
                        "type": "string",
                        "description": (
                            "Present only when the per-host property read was "
                            "skipped; records the failing method, its status, and "
                            "the underlying error."
                        ),
                    },
                },
                "required": ["id"],
            },
            "description": "One row per host in scope.",
        },
    },
    "required": ["hosts"],
}

#: Curated ``when_to_use`` blurb for the host-network-uplinks group.
HOST_NETWORK_UPLINKS_WHEN_TO_USE = (
    "Use to read per-ESXi-host physical-NIC (pnic) inventory across a "
    "vCenter: each pnic's device / MAC / driver / link state (up when the "
    "WS-API linkSpeed is present) / speed, plus each proxy switch (the "
    "host-side backing of a DVS) with the pnic device names bound to it "
    "as uplinks. The one read the plain vSphere Automation REST surface "
    "cannot reproduce -- the right op for 'are we out of physical switch "
    "ports?' / 'which pnics back this DVS uplink?' / 'is this uplink "
    "down?'. Reads config.network.pnic + config.network.proxySwitch via "
    "PropertyCollector directly on the connector session, so it works "
    "with zero catalog ingest. Read-only."
)

VMWARE_HOST_NETWORK_UPLINKS_OP = VmwareTypedOp(
    op_id="vmware.host.network_uplinks",
    handler_attr="host_network_uplinks",
    summary="Per host, physical NIC link state + speed and their proxy-switch uplinks.",
    description=(
        "Returns one row per ESXi host in the vCenter with its physical "
        "NICs (device / MAC / driver / link_up / speed_mb / duplex) and "
        "proxy switches (key / dvs_name / dvs_uuid / uplink_pnics -- the "
        "physical-NIC device names backing each DVS uplink). Reads "
        "config.network.pnic + config.network.proxySwitch off a per-host "
        "Web-Services-API RetrievePropertiesEx directly on the connector "
        "session, so it works with zero catalog ingest -- the pnic "
        "link-state / uplink mapping is the one read the plain vSphere "
        "Automation REST surface cannot reproduce (it drives physical "
        "switch-port-occupancy reasoning). Optional filter_hosts narrows "
        "to specific host MoRef ids. The per-host read is best-effort: a "
        "failed read nulls pnics / proxy_switches with a read_note rather "
        "than sinking the op. safety_level=safe, read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=HOST_NETWORK_UPLINKS_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "host", "networking", "pnic"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator asks about physical NIC inventory, link "
            "state / speed, or which pnics back a DVS uplink on ESXi hosts -- "
            "physical switch-port occupancy the plain vCenter REST host "
            "listing cannot answer."
        ),
        "parameter_hints": {
            "filter_hosts": "List of host MoRef ids to narrow to; omit for all hosts.",
        },
        "output_shape": (
            "{hosts: [{id, name, pnics: [{device, mac, driver, link_up, "
            "speed_mb, duplex}], proxy_switches: [{key, dvs_name, dvs_uuid, "
            "uplink_pnics}]}, ...]}. When a host's property read failed, "
            "pnics/proxy_switches are null and the row carries a read_note."
        ),
    },
)
