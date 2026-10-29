# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.network.portgroup.show`` typed op -- all settings of one distributed port group.

The vCenter REST API lists port groups (``GET /vcenter/network``) but shows only
their id, name and type. The settings live on the VIM object
``DistributedVirtualPortgroup.config`` (a ``DVPortgroupConfigInfo``). This read
gets that property with one ``PropertyCollector.RetrievePropertiesEx`` call, the
same read path ``vmware.vm.info`` uses, and the parent switch name with a second
one. It is a ``source_kind="typed"`` bound method on :class:`VmwareRestConnector`,
so it works on a fresh install with no catalog ingest, on vCenter 8 and 9.

The whole ``config`` object is read, never a deep path such as
``config.defaultPortConfig.vlan``: vCenter 8.0.x answers deep paths into a
``DVPortSetting`` with HTTP 500.

Field names follow the VIM types in the pinned ``vi-json.yaml``:

* ``defaultPortConfig`` is a ``VMwareDVSPortSetting``: ``vlan``,
  ``securityPolicy`` (``DVSSecurityPolicy``), ``uplinkTeamingPolicy``
  (``VmwareUplinkPortTeamingPolicy``), ``inShapingPolicy`` /
  ``outShapingPolicy`` (``DVSTrafficShapingPolicy``) and ``blocked``.
* Each setting is a policy object (``BoolPolicy`` / ``StringPolicy`` /
  ``LongPolicy``) that carries ``value`` and ``inherited``.
* ``rollingOrder`` true means the uplink does **not** fail back, so the
  operation reports ``failback = not rollingOrder``.

A missing port group is a ``not_found`` result. A ``config`` property vCenter
could not read (a ``missingSet`` entry, for example no permission) fails the
call: empty settings would be a wrong answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest._portgroup_show_schemas import (
    PORTGROUP_SHOW_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "DVPG_MO_TYPE",
    "PORTGROUP_CONFIG_PROPERTY",
    "PORTGROUP_LIST_PATH",
    "PORTGROUP_SHOW_GROUP_KEY",
    "PORTGROUP_SHOW_WHEN_TO_USE",
    "PORT_SETTING_FIELDS",
    "VMWARE_NETWORK_PORTGROUP_SHOW_OP",
    "portgroup_show_impl",
]

_log = structlog.get_logger(__name__)

#: REST network list, used only to find a port group id from its name.
PORTGROUP_LIST_PATH = "/vcenter/network"
_NETWORK_TYPE_DISTRIBUTED_PORTGROUP = "DISTRIBUTED_PORTGROUP"
#: PropertyCollector ``RetrievePropertiesEx`` on the singleton ``propertyCollector``.
_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
DVPG_MO_TYPE = "DistributedVirtualPortgroup"
_DVS_MO_TYPE = "DistributedVirtualSwitch"
PORTGROUP_CONFIG_PROPERTY = "config"
_PROP_NAME = "name"

#: The VIM fields this read uses, per VIM type. The reconcile test checks each
#: one against the pinned ``vi-json.yaml`` files.
PORT_SETTING_FIELDS: dict[str, tuple[str, ...]] = {
    "DVPortgroupConfigInfo": (
        "key",
        "name",
        "numPorts",
        "distributedVirtualSwitch",
        "defaultPortConfig",
        "description",
        "type",
        "configVersion",
        "autoExpand",
        "uplink",
    ),
    "DVPortSetting": ("blocked", "inShapingPolicy", "outShapingPolicy"),
    "VMwareDVSPortSetting": ("vlan", "uplinkTeamingPolicy", "securityPolicy"),
    "DVSSecurityPolicy": ("allowPromiscuous", "macChanges", "forgedTransmits"),
    "VmwareUplinkPortTeamingPolicy": (
        "policy",
        "notifySwitches",
        "rollingOrder",
        "failureCriteria",
        "uplinkPortOrder",
    ),
    "VMwareUplinkPortOrderPolicy": ("activeUplinkPort", "standbyUplinkPort"),
    "DVSFailureCriteria": ("checkBeacon",),
    "DVSTrafficShapingPolicy": ("enabled", "averageBandwidth", "peakBandwidth", "burstSize"),
    "VmwareDistributedVirtualSwitchVlanIdSpec": ("vlanId",),
    "VmwareDistributedVirtualSwitchTrunkVlanSpec": ("vlanId",),
    "VmwareDistributedVirtualSwitchPvlanSpec": ("pvlanId",),
    "NumericRange": ("start", "end"),
    "InheritablePolicy": ("inherited",),
}

_TRUNK_VLAN_SPEC_TYPE = "VmwareDistributedVirtualSwitchTrunkVlanSpec"
_VLAN_ID_SPEC_TYPE = "VmwareDistributedVirtualSwitchVlanIdSpec"
_PVLAN_SPEC_TYPE = "VmwareDistributedVirtualSwitchPvlanSpec"

PORTGROUP_SHOW_GROUP_KEY = "vmware-portgroup-settings"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int_or_none(value: Any) -> int | None:
    """An int from a JSON number or numeric string; ``None`` for bools and junk."""
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


def _policy_value(policy: Any) -> Any:
    """The ``value`` of a ``BoolPolicy`` / ``StringPolicy`` / ``LongPolicy``."""
    return policy.get("value") if isinstance(policy, dict) else None


def _bool_value(policy: Any) -> bool | None:
    value = _policy_value(policy)
    return value if isinstance(value, bool) else None


def _inherited(policy: Any) -> bool | None:
    value = policy.get("inherited") if isinstance(policy, dict) else None
    return value if isinstance(value, bool) else None


def _moref_value(ref: Any) -> str | None:
    value = ref.get("value") if isinstance(ref, dict) else None
    return value if isinstance(value, str) else None


def _vlan_view(raw: Any) -> dict[str, Any] | None:
    """``{mode: access|trunk|pvlan, ...}`` from the VLAN spec; ``None`` when unknown."""
    if not isinstance(raw, dict):
        return None
    type_name = raw.get(VIM_TYPE_NAME_KEY)
    vlan_id = raw.get("vlanId")
    if type_name == _PVLAN_SPEC_TYPE or "pvlanId" in raw:
        return {"mode": "pvlan", "pvlan_id": _int_or_none(raw.get("pvlanId"))}
    if type_name == _TRUNK_VLAN_SPEC_TYPE or isinstance(vlan_id, list):
        ranges = [
            {"start": _int_or_none(rng.get("start")), "end": _int_or_none(rng.get("end"))}
            for rng in (vlan_id if isinstance(vlan_id, list) else [])
            if isinstance(rng, dict)
        ]
        return {"mode": "trunk", "ranges": ranges}
    if type_name == _VLAN_ID_SPEC_TYPE or _int_or_none(vlan_id) is not None:
        return {"mode": "access", "vlan_id": _int_or_none(vlan_id)}
    return None


def _security_view(raw: Any) -> dict[str, Any]:
    policy = _as_dict(raw)
    return {
        "allow_promiscuous": _bool_value(policy.get("allowPromiscuous")),
        "mac_changes": _bool_value(policy.get("macChanges")),
        "forged_transmits": _bool_value(policy.get("forgedTransmits")),
        "inherited": _inherited(raw),
    }


def _uplink_names(value: Any) -> list[str]:
    return [name for name in value if isinstance(name, str)] if isinstance(value, list) else []


def _teaming_view(raw: Any) -> dict[str, Any]:
    policy = _as_dict(raw)
    order = _as_dict(policy.get("uplinkPortOrder"))
    rolling_order = _bool_value(policy.get("rollingOrder"))
    load_balancing = _policy_value(policy.get("policy"))
    return {
        "load_balancing": load_balancing if isinstance(load_balancing, str) else None,
        "active_uplinks": _uplink_names(order.get("activeUplinkPort")),
        "standby_uplinks": _uplink_names(order.get("standbyUplinkPort")),
        "failback": None if rolling_order is None else not rolling_order,
        "notify_switches": _bool_value(policy.get("notifySwitches")),
        "beacon_probing": _bool_value(_as_dict(policy.get("failureCriteria")).get("checkBeacon")),
        "inherited": _inherited(raw),
    }


def _shaping_view(raw: Any) -> dict[str, Any]:
    policy = _as_dict(raw)
    return {
        "enabled": _bool_value(policy.get("enabled")),
        "average_bandwidth_bps": _int_or_none(_policy_value(policy.get("averageBandwidth"))),
        "peak_bandwidth_bps": _int_or_none(_policy_value(policy.get("peakBandwidth"))),
        "burst_size_bytes": _int_or_none(_policy_value(policy.get("burstSize"))),
        "inherited": _inherited(raw),
    }


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _portgroup_row(portgroup: str, config: dict[str, Any], dvs_name: str | None) -> dict[str, Any]:
    """The operator-facing row from a ``DVPortgroupConfigInfo``."""
    port = _as_dict(config.get("defaultPortConfig"))
    auto_expand = config.get("autoExpand")
    uplink = config.get("uplink")
    return {
        "portgroup": portgroup,
        "name": _str_or_none(config.get("name")),
        "key": _str_or_none(config.get("key")),
        "description": _str_or_none(config.get("description")),
        "dvs": _moref_value(config.get("distributedVirtualSwitch")),
        "dvs_name": dvs_name,
        "port_binding": _str_or_none(config.get("type")),
        "num_ports": _int_or_none(config.get("numPorts")),
        "auto_expand": auto_expand if isinstance(auto_expand, bool) else None,
        "uplink_portgroup": uplink if isinstance(uplink, bool) else None,
        "config_version": _str_or_none(config.get("configVersion")),
        "vlan": _vlan_view(port.get("vlan")),
        "security": _security_view(port.get("securityPolicy")),
        "teaming": _teaming_view(port.get("uplinkTeamingPolicy")),
        "traffic_shaping": {
            "ingress": _shaping_view(port.get("inShapingPolicy")),
            "egress": _shaping_view(port.get("outShapingPolicy")),
        },
        "blocked": _bool_value(port.get("blocked")),
    }


def _object_props(retrieve_result: Any, moid: str) -> tuple[dict[str, Any], set[str]] | None:
    """``(properties, unreadable paths)`` of one object in the answer; ``None`` when absent."""
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    for obj in objects if isinstance(objects, list) else []:
        if not isinstance(obj, dict):
            continue
        ref = obj.get("obj")
        if isinstance(ref, dict) and ref.get("value") != moid:
            continue
        props = {
            entry["name"]: unwrap_vim_value(entry.get("val"))
            for entry in obj.get("propSet") or []
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
        missing = {
            entry["path"]
            for entry in obj.get("missingSet") or []
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        return props, missing
    return None


async def _portgroup_id_by_name(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    name: str,
) -> str:
    """Find the id of the distributed port group with this exact name.

    No match is a ``not_found`` result. More than one match (the same name in
    two datacenters) is an error that names the ids, so the caller can pass one.
    """
    path = await connector.mount_op_path(target, PORTGROUP_LIST_PATH, operator)
    query = await connector.adapt_op_query(
        target,
        {"filter.types": [_NETWORK_TYPE_DISTRIBUTED_PORTGROUP], "filter.names": [name]},
        operator,
    )
    entries = _unwrap_value(
        await connector._get_json(target, path, operator=operator, params=query)
    )
    if not isinstance(entries, list):
        raise RuntimeError(
            f"vmware.network.portgroup.show: expected a list from GET {PORTGROUP_LIST_PATH!r}, "
            f"got {type(entries).__name__}"
        )
    ids: list[str] = [
        moid
        for entry in entries
        if isinstance(entry, dict) and entry.get("name", name) == name
        for moid in [entry.get("network")]
        if isinstance(moid, str)
    ]
    if not ids:
        raise ConnectorResourceNotFoundError(
            [name], f"vmware.network.portgroup.show: no distributed port group named {name!r}"
        )
    if len(ids) > 1:
        raise RuntimeError(
            f"vmware.network.portgroup.show: {len(ids)} distributed port groups are named "
            f"{name!r} ({', '.join(ids)}); pass the 'portgroup' id instead"
        )
    return ids[0]


async def _switch_name(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    switch_ref: Any,
) -> str | None:
    """The display name of the parent switch; ``None`` when vCenter does not give it."""
    moid = _moref_value(switch_ref)
    if moid is None:
        return None
    ref_type = switch_ref.get("type")
    mo_type = ref_type if isinstance(ref_type, str) and ref_type else _DVS_MO_TYPE
    result = await connector._post_vmomi_json(
        target,
        _RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(mo_type, [moid], (_PROP_NAME,)),
    )
    found = _object_props(result, moid)
    name = found[0].get(_PROP_NAME) if found is not None else None
    return name if isinstance(name, str) else None


async def portgroup_show_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.network.portgroup.show``.

    1. With ``name``: ``GET /vcenter/network`` (distributed port groups with that
       name) to find the id. Skipped when ``portgroup`` is given.
    2. ``RetrievePropertiesEx`` of the port group's ``config``.
    3. ``RetrievePropertiesEx`` of the parent switch's ``name``.

    Returns one flat row (see the response schema of
    :data:`VMWARE_NETWORK_PORTGROUP_SHOW_OP`).
    """
    portgroup = params.get("portgroup")
    if not isinstance(portgroup, str):
        portgroup = await _portgroup_id_by_name(connector, operator, target, params["name"])

    result = await connector._post_vmomi_json(
        target,
        _RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(DVPG_MO_TYPE, [portgroup], (PORTGROUP_CONFIG_PROPERTY,)),
        promote_managed_object_not_found=True,
    )
    found = _object_props(result, portgroup)
    if found is None:
        raise ConnectorResourceNotFoundError(
            [portgroup], f"vmware.network.portgroup.show: no distributed port group {portgroup!r}"
        )
    props, missing = found
    if PORTGROUP_CONFIG_PROPERTY in missing:
        raise RuntimeError(
            "vmware.network.portgroup.show: vCenter could not read the settings of port group "
            f"{portgroup!r} (missing permission or an unreadable object)"
        )
    config = _as_dict(props.get(PORTGROUP_CONFIG_PROPERTY))
    switch_ref = config.get("distributedVirtualSwitch")
    dvs_name = await _switch_name(connector, operator, target, switch_ref)
    row = _portgroup_row(portgroup, config, dvs_name)
    _log.info(
        "vmware_portgroup_show_read",
        target=target.name,
        portgroup=portgroup,
        vlan_mode=(row["vlan"] or {}).get("mode"),
    )
    return row


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "portgroup": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Managed-object ID of the distributed port group, e.g. 'dvportgroup-42'. "
                "Supply exactly one of 'portgroup' or 'name'."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Exact name of the distributed port group. An unknown name gives a not_found "
                "result; a name used by two port groups is an error. Supply exactly one of "
                "'portgroup' or 'name'."
            ),
        },
    },
    "oneOf": [{"required": ["portgroup"]}, {"required": ["name"]}],
    "additionalProperties": False,
    "examples": [{"name": "<portgroup-name>"}],
}

#: Curated ``when_to_use`` blurb for the port-group-settings group.
PORTGROUP_SHOW_WHEN_TO_USE = (
    "Use to read all settings of one distributed port group: the switch, the VLAN (single, "
    "trunk ranges or private VLAN), the security policy (promiscuous mode, MAC address "
    "changes, forged transmits), teaming and failover (load balancing, active and standby "
    "uplinks, failback), traffic shaping in and out, port binding and the number of ports. "
    "It reads the port group's VIM 'config' property, with no catalog ingest. Read-only. "
    "To list all port groups with their VLAN and VMs, use "
    "vmware.composite.network.portgroup.audit."
)

VMWARE_NETWORK_PORTGROUP_SHOW_OP = VmwareTypedOp(
    op_id="vmware.network.portgroup.show",
    handler_attr="network_portgroup_show",
    summary="All settings of one distributed port group: VLAN, security, teaming, shaping.",
    description=(
        "Returns one distributed port group by id (portgroup) or name: switch, VLAN (single, "
        "trunk or private VLAN), security policy, teaming and failover, traffic shaping in "
        "and out, port binding and number of ports. An unknown port group gives a not_found "
        "result. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=PORTGROUP_SHOW_RESPONSE_SCHEMA,
    group_key=PORTGROUP_SHOW_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "network", "portgroup", "vlan"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see how one distributed port group is set up: its VLAN, security "
            "policy, teaming and failover, or traffic shaping. Call it before and after you "
            "change one of these settings, to check the change."
        ),
        "parameter_hints": {
            "portgroup": "Port group moid, e.g. 'dvportgroup-42'; supply exactly one of "
            "portgroup / name.",
            "name": "Exact port group name. An unknown name returns status not_found.",
        },
        "output_shape": (
            "{portgroup, name, key, description, dvs, dvs_name, port_binding, num_ports, "
            "auto_expand, uplink_portgroup, config_version, vlan: {mode: access|trunk|pvlan, "
            "vlan_id | ranges: [{start, end}] | pvlan_id} | null, security: "
            "{allow_promiscuous, mac_changes, forged_transmits, inherited}, teaming: "
            "{load_balancing, active_uplinks, standby_uplinks, failback, notify_switches, "
            "beacon_probing, inherited}, traffic_shaping: {ingress, egress: {enabled, "
            "average_bandwidth_bps, peak_bandwidth_bps, burst_size_bytes, inherited}}, blocked}."
        ),
        "next_step": (
            "To change the VLAN, call vmware.composite.network.portgroup.vlan.set. To change "
            "the security policy, call vmware.composite.network.portgroup.security.set. To "
            "see which VMs use the port group, call vmware.composite.network.portgroup.audit."
        ),
    },
)
