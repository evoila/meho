# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Op metadata for the pfSense OpenVPN reads.

The schemas, texts and :data:`OPENVPN_OPS` tuple of ``pfsense.openvpn.clients``
and ``pfsense.openvpn.log``. The handlers are in
:mod:`meho_backplane.connectors.pfsense.ops_openvpn`.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.pfsense.ops import PfSenseOp
from meho_backplane.connectors.pfsense.ops_openvpn import _DEFAULT_LIMIT, _MAX_LIMIT

__all__ = ["OPENVPN_OPS"]

# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_CLIENTS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

_CLIENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "common_name": {"type": ["string", "null"]},
        "real_address": {"type": ["string", "null"]},
        "virtual_address": {"type": ["string", "null"]},
        "virtual_ipv6_address": {"type": ["string", "null"]},
        "bytes_received": {"type": ["integer", "null"]},
        "bytes_sent": {"type": ["integer", "null"]},
        "connected_since": {"type": ["string", "null"]},
        "username": {"type": ["string", "null"]},
    },
}

_CLIENTS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "servers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "proto": {"type": ["string", "null"]},
                    "port": {"type": ["string", "null"]},
                    "readable": {"type": "boolean"},
                    "clients": {"type": "array", "items": _CLIENT_SCHEMA},
                },
                "required": ["server", "proto", "port", "readable", "clients"],
            },
        },
        "client_count": {"type": "integer"},
    },
    "required": ["servers", "client_count"],
    "additionalProperties": False,
}

_LOG_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "common_name": {
            "type": "string",
            "minLength": 2,
            "maxLength": 64,
            "description": (
                "The VPN user's common name: letters, digits, '.', '_', '@' or '-'. "
                "Only log lines with the whole name count."
            ),
        },
        "since": {
            "type": "string",
            "description": (
                "Start of the window, ISO 8601 with a zone, for example 2026-11-10T09:00:00Z."
            ),
        },
        "until": {
            "type": "string",
            "description": "End of the window, ISO 8601 with a zone. Default: no end.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_LIMIT,
            "default": _DEFAULT_LIMIT,
            "description": f"Most events to return (the newest). 1 to {_MAX_LIMIT}.",
        },
    },
    "required": ["common_name"],
    "additionalProperties": False,
    "examples": [
        {"common_name": "jdoe", "since": "2026-11-10T09:00:00Z", "until": "2026-11-10T10:00:00Z"},
    ],
}

_LOG_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "common_name": {"type": "string"},
        "since": {"type": ["string", "null"]},
        "until": {"type": ["string", "null"]},
        "event_count": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time": {"type": ["string", "null"]},
                    "event": {
                        "type": "string",
                        "enum": ["connect", "disconnect", "restart", "error", "other"],
                    },
                    "reason": {"type": ["string", "null"]},
                    "line": {"type": "string"},
                },
                "required": ["time", "event", "reason", "line"],
            },
        },
    },
    "required": ["common_name", "since", "until", "event_count", "truncated", "events"],
    "additionalProperties": False,
}

_CLIENTS_WHEN = (
    "Call first for 'the VPN is slow' or 'the VPN drops': is the user "
    "connected now, from which address, since when, and how much traffic "
    "went through. Read-only."
)
_LOG_WHEN = (
    "Call to see what the OpenVPN log says for one user in a time window: "
    "connects, disconnects, restarts (for example ping-restart), "
    "inactivity timeouts and TLS errors. Use it after "
    "pfsense.openvpn.clients when a session dropped. Read-only."
)

OPENVPN_OPS: tuple[PfSenseOp, ...] = (
    PfSenseOp(
        op_id="pfsense.openvpn.clients",
        handler_attr="openvpn_clients",
        summary="List the VPN clients that are connected now, per OpenVPN server.",
        description=(
            "Reads each OpenVPN server's status on the pfSense firewall and returns "
            "the connected clients: common name, real address, tunnel address, bytes "
            "received and sent, and the time the session began. Also returns each "
            "server's protocol (UDP or TCP) and port. Changes nothing."
        ),
        parameter_schema=_CLIENTS_PARAMETER_SCHEMA,
        response_schema=_CLIENTS_RESPONSE_SCHEMA,
        group_key="openvpn",
        tags=("read-only", "openvpn", "vpn", "sessions", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": _CLIENTS_WHEN,
            "when_to_use": _CLIENTS_WHEN,
            "parameter_hints": {},
            "output_shape": (
                "{servers: [{server, proto, port, readable, clients: [{common_name, "
                "real_address, virtual_address, virtual_ipv6_address, bytes_received, "
                "bytes_sent, connected_since, username}]}], client_count}. A server "
                "with readable=false could not be read: its client list is unknown, "
                "not empty."
            ),
            "next_step": (
                "If the user is not in the list, or connected_since is recent, read "
                "the log with pfsense.openvpn.log for that common name."
            ),
        },
    ),
    PfSenseOp(
        op_id="pfsense.openvpn.log",
        handler_attr="openvpn_log",
        summary="Read the OpenVPN log for one VPN user in a time window, with event types.",
        description=(
            "Reads the OpenVPN log on the pfSense firewall, keeps the lines that name "
            "one common name, and returns the ones inside the window. Each line has an "
            "event type: connect, disconnect (with the reason), restart (for example "
            "ping-restart), error or other. Changes nothing."
        ),
        parameter_schema=_LOG_PARAMETER_SCHEMA,
        response_schema=_LOG_RESPONSE_SCHEMA,
        group_key="openvpn",
        tags=("read-only", "openvpn", "vpn", "logs", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": _LOG_WHEN,
            "when_to_use": _LOG_WHEN,
            "parameter_hints": {
                "common_name": "Required. The user's common name, as in pfsense.openvpn.clients.",
                "since": "Optional. Window start, ISO 8601 with a zone.",
                "until": "Optional. Window end, ISO 8601 with a zone.",
                "limit": f"Optional. Newest events to return (default {_DEFAULT_LIMIT}).",
            },
            "output_shape": (
                "{common_name, since, until, event_count, truncated, events: [{time, "
                "event, reason, line}]}. events are oldest first. truncated=true means "
                "older lines may be missing: ask again with a later since "
                "or a smaller window."
            ),
            "next_step": (
                "A restart with reason ping-restart or an inactivity-timeout "
                "disconnect means the client or the path stopped answering. A TLS "
                "error points at the client certificate or a blocked path."
            ),
            "preconditions": (
                "Lines are matched by the common name text. A line from before the "
                "server knew the user (for example an early TLS error) has no name and "
                "is not returned."
            ),
        },
    ),
)
