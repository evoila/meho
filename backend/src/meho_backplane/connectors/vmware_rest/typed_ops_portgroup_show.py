# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.network.portgroup.show`` typed op -- all settings of one distributed port group.

The vCenter REST API lists port groups (``GET /vcenter/network``) but shows only
their id, name and type. The settings live on the VIM object
``DistributedVirtualPortgroup.config`` (a ``DVPortgroupConfigInfo``). This read
gets that property with one ``PropertyCollector.RetrievePropertiesEx`` call, the
same read path ``vmware.vm.info`` uses, and the parent switch's ``name`` and
``config.defaultPortConfig`` (its default port settings) with a second one. It
is a ``source_kind="typed"`` bound method on :class:`VmwareRestConnector`, so
it works on a fresh install with no catalog ingest, on vCenter 8 and 9.

The port group's whole ``config`` object is read, never a deep path such as
``config.defaultPortConfig.vlan``: vCenter 8.0.x answers deep paths into a
``DVPortSetting`` with HTTP 500. On the switch, ``config.defaultPortConfig``
is the same path the port group write composites read, and no deeper.

Inheritance. Every setting is an ``InheritablePolicy`` with its own
``inherited`` flag, and a port group can mix local and inherited settings
inside one policy (for example a local load balancing with inherited uplinks).
A setting that is inherited, by itself or through its policy, is taken from
the switch defaults when vCenter gave them; otherwise the port group's own
object is used. Each policy in the row carries ``inherited`` (the whole policy
comes from the switch) and ``inherited_settings`` (one flag per field).

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
call: empty settings would be a wrong answer. Switch defaults vCenter could
not read fail the call too when the port group inherits at least one setting:
an inherited uplink list without the switch value would show as empty. When
the port group inherits nothing, the defaults are not needed, and the read
only logs a warning.
"""

from __future__ import annotations

from collections.abc import Callable
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
    "DVS_MO_TYPE",
    "PORTGROUP_CONFIG_PROPERTY",
    "PORTGROUP_LIST_PATH",
    "PORTGROUP_SHOW_GROUP_KEY",
    "PORTGROUP_SHOW_WHEN_TO_USE",
    "PORT_SETTING_FIELDS",
    "SWITCH_DEFAULTS_PROPERTY",
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
DVS_MO_TYPE = "DistributedVirtualSwitch"
PORTGROUP_CONFIG_PROPERTY = "config"
#: The switch's default port settings (a ``DVPortSetting``), read with its name.
SWITCH_DEFAULTS_PROPERTY = "config.defaultPortConfig"
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
    "DVSConfigInfo": ("defaultPortConfig",),
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


def _inherits_from_switch(setting: Any) -> bool:
    """Whether any policy in this port setting, at any depth, is inherited."""
    if isinstance(setting, dict):
        return setting.get("inherited") is True or any(
            _inherits_from_switch(value) for value in setting.values()
        )
    if isinstance(setting, list):
        return any(_inherits_from_switch(value) for value in setting)
    return False


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


def _pick(
    local: Any, parent: Any, group_inherited: bool | None
) -> tuple[dict[str, Any], bool | None]:
    """The effective policy object and whether it comes from the switch.

    A policy that is inherited, by itself or through its policy group, is taken
    from the switch default *parent* when vCenter gave one. Otherwise the port
    group's own object is used, which is also what vCenter sends when it fills
    inherited values in. The flag is ``None`` when the object does not say.
    """
    own = _inherited(local)
    inherited = True if (group_inherited or own) else own
    if inherited and isinstance(parent, dict):
        return parent, True
    return _as_dict(local), inherited


_Pick = Callable[[str], tuple[dict[str, Any], bool | None]]


def _picker(local: dict[str, Any], parent: dict[str, Any], group_inherited: bool | None) -> _Pick:
    """A ``pick(field)`` closure over one policy and its switch default."""

    def pick(field: str) -> tuple[dict[str, Any], bool | None]:
        return _pick(local.get(field), parent.get(field), group_inherited)

    return pick


def _security_view(raw: Any, parent_raw: Any) -> dict[str, Any]:
    policy, inherited = _pick(raw, parent_raw, None)
    pick = _picker(policy, _as_dict(parent_raw), inherited)
    promiscuous, promiscuous_inherited = pick("allowPromiscuous")
    mac_changes, mac_changes_inherited = pick("macChanges")
    forged, forged_inherited = pick("forgedTransmits")
    return {
        "allow_promiscuous": _bool_value(promiscuous),
        "mac_changes": _bool_value(mac_changes),
        "forged_transmits": _bool_value(forged),
        "inherited": inherited,
        "inherited_settings": {
            "allow_promiscuous": promiscuous_inherited,
            "mac_changes": mac_changes_inherited,
            "forged_transmits": forged_inherited,
        },
    }


def _uplink_names(value: Any) -> list[str]:
    return [name for name in value if isinstance(name, str)] if isinstance(value, list) else []


def _teaming_view(raw: Any, parent_raw: Any) -> dict[str, Any]:
    policy, inherited = _pick(raw, parent_raw, None)
    parent = _as_dict(parent_raw)
    pick = _picker(policy, parent, inherited)
    load_balancing, load_balancing_inherited = pick("policy")
    order, order_inherited = pick("uplinkPortOrder")
    rolling, rolling_inherited = pick("rollingOrder")
    notify, notify_inherited = pick("notifySwitches")
    criteria, criteria_inherited = pick("failureCriteria")
    beacon, beacon_inherited = _pick(
        criteria.get("checkBeacon"),
        _as_dict(parent.get("failureCriteria")).get("checkBeacon"),
        criteria_inherited,
    )
    load_balancing_value = _policy_value(load_balancing)
    rolling_order = _bool_value(rolling)
    return {
        "load_balancing": load_balancing_value if isinstance(load_balancing_value, str) else None,
        "active_uplinks": _uplink_names(order.get("activeUplinkPort")),
        "standby_uplinks": _uplink_names(order.get("standbyUplinkPort")),
        "failback": None if rolling_order is None else not rolling_order,
        "notify_switches": _bool_value(notify),
        "beacon_probing": _bool_value(beacon),
        "inherited": inherited,
        "inherited_settings": {
            "load_balancing": load_balancing_inherited,
            "active_uplinks": order_inherited,
            "standby_uplinks": order_inherited,
            "failback": rolling_inherited,
            "notify_switches": notify_inherited,
            "beacon_probing": beacon_inherited,
        },
    }


def _shaping_view(raw: Any, parent_raw: Any) -> dict[str, Any]:
    policy, inherited = _pick(raw, parent_raw, None)
    pick = _picker(policy, _as_dict(parent_raw), inherited)
    enabled, enabled_inherited = pick("enabled")
    average, average_inherited = pick("averageBandwidth")
    peak, peak_inherited = pick("peakBandwidth")
    burst, burst_inherited = pick("burstSize")
    return {
        "enabled": _bool_value(enabled),
        "average_bandwidth_bps": _int_or_none(_policy_value(average)),
        "peak_bandwidth_bps": _int_or_none(_policy_value(peak)),
        "burst_size_bytes": _int_or_none(_policy_value(burst)),
        "inherited": inherited,
        "inherited_settings": {
            "enabled": enabled_inherited,
            "average_bandwidth_bps": average_inherited,
            "peak_bandwidth_bps": peak_inherited,
            "burst_size_bytes": burst_inherited,
        },
    }


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _portgroup_row(
    portgroup: str,
    config: dict[str, Any],
    dvs_name: str | None,
    switch_defaults: dict[str, Any],
) -> dict[str, Any]:
    """The operator-facing row from a ``DVPortgroupConfigInfo``.

    *switch_defaults* is the switch's ``config.defaultPortConfig``; inherited
    settings are resolved against it. ``{}`` only when nothing is inherited.
    """
    port = _as_dict(config.get("defaultPortConfig"))
    pick = _picker(port, switch_defaults, None)
    vlan, vlan_inherited = pick("vlan")
    blocked, blocked_inherited = pick("blocked")
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
        "vlan": _vlan_view(vlan),
        "security": _security_view(
            port.get("securityPolicy"), switch_defaults.get("securityPolicy")
        ),
        "teaming": _teaming_view(
            port.get("uplinkTeamingPolicy"), switch_defaults.get("uplinkTeamingPolicy")
        ),
        "traffic_shaping": {
            "ingress": _shaping_view(
                port.get("inShapingPolicy"), switch_defaults.get("inShapingPolicy")
            ),
            "egress": _shaping_view(
                port.get("outShapingPolicy"), switch_defaults.get("outShapingPolicy")
            ),
        },
        "blocked": _bool_value(blocked),
        "inherited_settings": {"vlan": vlan_inherited, "blocked": blocked_inherited},
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


async def _switch_defaults(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    switch_ref: Any,
) -> tuple[str | None, dict[str, Any] | None]:
    """The parent switch's display name and its default port settings.

    The defaults are ``None`` when vCenter did not give them (no switch
    reference, the switch missing from the answer, or a ``missingSet`` entry,
    for example no permission). That logs a warning; the caller decides whether
    the defaults were needed.
    """
    moid = _moref_value(switch_ref)
    props: dict[str, Any] = {}
    missing: set[str] = {SWITCH_DEFAULTS_PROPERTY}
    if moid is not None:
        ref_type = switch_ref.get("type")
        mo_type = ref_type if isinstance(ref_type, str) and ref_type else DVS_MO_TYPE
        result = await connector._post_vmomi_json(
            target,
            _RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=retrieve_properties_body(mo_type, [moid], (_PROP_NAME, SWITCH_DEFAULTS_PROPERTY)),
        )
        props, missing = _object_props(result, moid) or (props, missing)
    name = props.get(_PROP_NAME)
    defaults = None if SWITCH_DEFAULTS_PROPERTY in missing else props.get(SWITCH_DEFAULTS_PROPERTY)
    if not isinstance(defaults, dict):
        _log.warning(
            "vmware_portgroup_show_switch_defaults_unreadable", target=target.name, dvs=moid
        )
        defaults = None
    return (name if isinstance(name, str) else None), defaults


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
    3. ``RetrievePropertiesEx`` of the parent switch's ``name`` and
       ``config.defaultPortConfig``, to resolve inherited settings. Defaults
       vCenter could not give fail the call when a setting is inherited.

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
    dvs_name, switch_defaults = await _switch_defaults(connector, operator, target, switch_ref)
    if switch_defaults is None and _inherits_from_switch(config.get("defaultPortConfig")):
        raise RuntimeError(
            "vmware.network.portgroup.show: vCenter could not read the default port settings "
            f"of the switch of port group {portgroup!r} (missing permission or an unreadable "
            "object), and the port group inherits settings from the switch"
        )
    row = _portgroup_row(portgroup, config, dvs_name, switch_defaults or {})
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
    "Each setting says whether it is set on the port group or comes from the switch. It "
    "reads the port group's VIM 'config' property, with no catalog ingest. Read-only. "
    "To list all port groups with their VLAN and VMs, use "
    "vmware.composite.network.portgroup.audit."
)

VMWARE_NETWORK_PORTGROUP_SHOW_OP = VmwareTypedOp(
    op_id="vmware.network.portgroup.show",
    handler_attr="network_portgroup_show",
    summary="All settings of one distributed port group: VLAN, security, teaming, shaping.",
    description=(
        "Returns one distributed port group by id (portgroup) or name: switch, VLAN (single, "
        "trunk or private VLAN), security, teaming and failover, traffic shaping, port "
        "binding and number of ports. Flags say which settings come from the switch. An "
        "unknown port group gives a not_found result. Read-only."
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
            "{allow_promiscuous, mac_changes, forged_transmits, inherited, inherited_settings}, "
            "teaming: {load_balancing, active_uplinks, standby_uplinks, failback, "
            "notify_switches, beacon_probing, inherited, inherited_settings}, traffic_shaping: "
            "{ingress, egress: {enabled, average_bandwidth_bps, peak_bandwidth_bps, "
            "burst_size_bytes, inherited, inherited_settings}}, blocked, inherited_settings: "
            "{vlan, blocked}}. 'inherited' means the whole policy comes from the switch; "
            "'inherited_settings' has one true/false flag per field of that object."
        ),
        "next_step": (
            "To change the VLAN, call vmware.composite.network.portgroup.vlan.set. To change "
            "the security policy, call vmware.composite.network.portgroup.security.set. To "
            "see which VMs use the port group, call vmware.composite.network.portgroup.audit."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_NETWORK_PORTGROUP_SHOW_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {PORTGROUP_SHOW_GROUP_KEY: PORTGROUP_SHOW_WHEN_TO_USE}
