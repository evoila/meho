# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Response schema of ``vmware.network.portgroup.show`` (kept apart to keep the op module small)."""

from __future__ import annotations

from typing import Any

__all__ = ["PORTGROUP_SHOW_RESPONSE_SCHEMA"]

_NULLABLE_BOOL: dict[str, Any] = {"type": ["boolean", "null"]}
_INHERITED: dict[str, Any] = {
    "type": ["boolean", "null"],
    "description": "True when the setting comes from the switch, not the port group.",
}


def _shaping_schema(direction: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": f"Traffic shaping for {direction} traffic.",
        "properties": {
            "enabled": {**_NULLABLE_BOOL, "description": "Whether traffic shaping is on."},
            "average_bandwidth_bps": {
                "type": ["integer", "null"],
                "description": "Average bandwidth in bits per second.",
            },
            "peak_bandwidth_bps": {
                "type": ["integer", "null"],
                "description": "Peak bandwidth during bursts in bits per second.",
            },
            "burst_size_bytes": {
                "type": ["integer", "null"],
                "description": "Largest burst in bytes.",
            },
            "inherited": _INHERITED,
        },
    }


PORTGROUP_SHOW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "portgroup": {"type": "string", "description": "Managed-object ID of the port group."},
        "name": {"type": ["string", "null"], "description": "Port group name."},
        "key": {"type": ["string", "null"], "description": "Port group key on the switch."},
        "description": {"type": ["string", "null"], "description": "Port group description."},
        "dvs": {"type": ["string", "null"], "description": "Managed-object ID of the switch."},
        "dvs_name": {"type": ["string", "null"], "description": "Name of the switch."},
        "port_binding": {
            "type": ["string", "null"],
            "description": "Port binding: earlyBinding (static), ephemeral or lateBinding.",
        },
        "num_ports": {"type": ["integer", "null"], "description": "Number of ports."},
        "auto_expand": {
            **_NULLABLE_BOOL,
            "description": "Whether vCenter adds ports when all are in use.",
        },
        "uplink_portgroup": {
            **_NULLABLE_BOOL,
            "description": "True for the switch's uplink port group.",
        },
        "config_version": {
            "type": ["string", "null"],
            "description": "Version of the settings; it changes on every change.",
        },
        "vlan": {
            "type": ["object", "null"],
            "description": (
                "VLAN: {mode: access, vlan_id}, {mode: trunk, ranges: [{start, end}]} or "
                "{mode: pvlan, pvlan_id}. Null when the switch reports no VLAN."
            ),
            "properties": {
                "mode": {"type": "string", "enum": ["access", "trunk", "pvlan"]},
                "vlan_id": {"type": ["integer", "null"], "description": "VLAN id, 0 = none."},
                "ranges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "start": {"type": ["integer", "null"]},
                            "end": {"type": ["integer", "null"]},
                        },
                    },
                    "description": "Trunk VLAN ranges.",
                },
                "pvlan_id": {
                    "type": ["integer", "null"],
                    "description": "Secondary private VLAN id.",
                },
            },
            "required": ["mode"],
        },
        "security": {
            "type": "object",
            "description": "Security policy.",
            "properties": {
                "allow_promiscuous": {**_NULLABLE_BOOL, "description": "Promiscuous mode."},
                "mac_changes": {**_NULLABLE_BOOL, "description": "MAC address changes."},
                "forged_transmits": {**_NULLABLE_BOOL, "description": "Forged transmits."},
                "inherited": _INHERITED,
            },
        },
        "teaming": {
            "type": "object",
            "description": "Teaming and failover.",
            "properties": {
                "load_balancing": {
                    "type": ["string", "null"],
                    "description": (
                        "Load balancing: loadbalance_srcid, loadbalance_srcmac, loadbalance_ip, "
                        "loadbalance_loadbased or failover_explicit."
                    ),
                },
                "active_uplinks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Active uplinks, in failover order.",
                },
                "standby_uplinks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Standby uplinks.",
                },
                "failback": {
                    **_NULLABLE_BOOL,
                    "description": "Whether traffic goes back to a recovered active uplink.",
                },
                "notify_switches": {
                    **_NULLABLE_BOOL,
                    "description": "Whether the physical switches are told on failover.",
                },
                "beacon_probing": {
                    **_NULLABLE_BOOL,
                    "description": "Failure detection by beacon probing (false = link status).",
                },
                "inherited": _INHERITED,
            },
        },
        "traffic_shaping": {
            "type": "object",
            "description": "Traffic shaping in both directions.",
            "properties": {
                "ingress": _shaping_schema("incoming"),
                "egress": _shaping_schema("outgoing"),
            },
        },
        "blocked": {**_NULLABLE_BOOL, "description": "Whether the ports are blocked."},
    },
    "required": ["portgroup", "vlan", "security", "teaming", "traffic_shaping"],
}
