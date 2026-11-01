# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameters, answers and AI guidance of the host NTP, DNS and syslog changes.

The handlers live in :mod:`._host_config_writes`. The patterns keep each list
entry to one plain value: a space or a comma would let a caller add NTP
options or a second syslog target inside one entry.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.typed_ops_host_config import (
    DNS_STATE_SCHEMA,
    HOST_PARAMETER,
    NTP_STATE_SCHEMA,
    SYSLOG_STATE_SCHEMA,
)

__all__ = [
    "DNS_SET_LLM_INSTRUCTIONS",
    "DNS_SET_PARAMETER_SCHEMA",
    "DNS_SET_RESPONSE_SCHEMA",
    "NTP_SET_LLM_INSTRUCTIONS",
    "NTP_SET_PARAMETER_SCHEMA",
    "NTP_SET_RESPONSE_SCHEMA",
    "SYSLOG_SET_LLM_INSTRUCTIONS",
    "SYSLOG_SET_PARAMETER_SCHEMA",
    "SYSLOG_SET_RESPONSE_SCHEMA",
]

#: A host name or an IP address, with no space, comma or '#'.
_SERVER_PATTERN: Final = r"^[A-Za-z0-9.:\-\[\]]+$(?!\n)"
#: A DNS name (letters, digits, dots and dashes).
_DOMAIN_PATTERN: Final = r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$(?!\n)"
#: One label: the host name without the domain.
_HOST_LABEL_PATTERN: Final = r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$(?!\n)"
#: An IPv4 or IPv6 address.
_IP_PATTERN: Final = r"^[0-9A-Fa-f.:]+$(?!\n)"
#: A syslog target: optional udp://, tcp:// or ssl://, a host, an optional port.
_SYSLOG_TARGET_PATTERN: Final = r"^((udp|tcp|ssl)://)?[A-Za-z0-9.\-\[\]:]+$(?!\n)"

_NULL_STR: Final = {"type": ["string", "null"]}
_STEPS: Final = {
    "type": "array",
    "items": {"type": "string"},
    "description": "The vSphere calls that ran, in order (for a failed or partial change).",
}
_ISSUES: Final = {
    "type": "array",
    "description": "Problems; an entry with severity error marks the call as failed.",
    "items": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Where the problem is."},
            "severity": {"type": "string", "description": "error, warning or info."},
            "message": {"type": "string", "description": "What happened."},
        },
    },
}
_COMMON_STATUSES: Final = [
    "applied",
    "unchanged",
    "failed",
    "not_verified",
    "host_required",
    "host_not_found",
    "ambiguous_host",
    "unsupported_host_target",
    "config_manager_unreadable",
]
_STATUS_TEXT: Final = (
    "applied: changed and read back. unchanged: the host already had these settings, "
    "nothing sent. failed: vSphere refused a call (see fault). not_verified: the read-back "
    "does not show the new settings. The rest: the host was not found, nothing sent."
)


def _response(state: dict[str, Any], extra_statuses: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [*_COMMON_STATUSES, *extra_statuses],
                "description": _STATUS_TEXT,
            },
            "host": {**_NULL_STR, "description": "Host moid, or the input on a refusal."},
            "requested": {"type": ["object", "null"], "description": "What the call asked for."},
            "before": {**state, "description": "The settings before the change."},
            "after": {**state, "description": "The settings read back after the change."},
            "applied_steps": _STEPS,
            "fault": {**_NULL_STR, "description": "vSphere fault class and text on failed."},
            "candidate_hosts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Host moids on ambiguous_host.",
            },
            "guidance": {**_NULL_STR, "description": "What happened and what to do next."},
            "issues": _ISSUES,
        },
        "required": ["status", "host"],
    }


# ---------------------------------------------------------------------------
# host.ntp.set
# ---------------------------------------------------------------------------

NTP_SET_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "host": HOST_PARAMETER,
        "servers": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "pattern": _SERVER_PATTERN,
                "description": "One NTP server: a host name or an IP address, no options.",
            },
            "description": "The NTP servers the host uses. They replace the current list.",
        },
        "policy": {
            "type": "string",
            "enum": ["on", "automatic"],
            "default": "on",
            "description": (
                "When ntpd starts: on (with the host, the default) or automatic (when its "
                "firewall port is open). To stop ntpd, use host.service_control."
            ),
        },
    },
    "required": ["host", "servers"],
    "additionalProperties": False,
    "examples": [
        {"host": "<host-name>", "servers": ["ntp-1.example.invalid", "ntp-2.example.invalid"]}
    ],
}

NTP_SET_RESPONSE_SCHEMA: Final[dict[str, Any]] = _response(NTP_STATE_SCHEMA, [])

NTP_SET_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to set the NTP servers of one ESXi host and make sure ntpd runs and starts "
        "with the host, like 'govc host.date.change -server'."
    ),
    "preconditions": (
        "Read the host first with vmware.host.config.show. The servers must be reachable "
        "from the host on UDP 123. The list replaces the current one. The change waits "
        "for approval. It does not change the clock protocol: on a host with protocol "
        "'ptp' it still starts ntpd, so do not call it there."
    ),
    "parameter_hints": {
        "servers": "Host names or IP addresses, e.g. ['<ntp-server-1>'].",
        "policy": "on (default) or automatic.",
    },
    "output_shape": (
        "{status: applied|unchanged|failed|not_verified|<host refusal>, host, requested: "
        "{servers, policy}, before and after: {servers, time_zone, protocol, service_policy, "
        "service_running}, applied_steps, fault, guidance, issues}."
    ),
    "next_step": (
        "Safe to run twice: unchanged sends nothing. MEHO restarts ntpd when it runs, else "
        "starts it. On failed, read fault. On not_verified, compare before and after with "
        "vmware.host.config.show."
    ),
}

# ---------------------------------------------------------------------------
# host.dns.set
# ---------------------------------------------------------------------------

DNS_SET_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "host": HOST_PARAMETER,
        "servers": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "string",
                "minLength": 2,
                "maxLength": 45,
                "pattern": _IP_PATTERN,
                "description": "One DNS server IP address.",
            },
            "description": "DNS server addresses, in order. Replaces the current list.",
        },
        "search_domains": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 253,
                "pattern": _DOMAIN_PATTERN,
                "description": "One search domain.",
            },
            "description": "DNS search domains, in order. Replaces the current list.",
        },
        "host_name": {
            "type": "string",
            "pattern": _HOST_LABEL_PATTERN,
            "description": "The host name without the domain (no dots).",
        },
        "domain_name": {
            "type": "string",
            "maxLength": 253,
            "pattern": _DOMAIN_PATTERN,
            "description": "The domain name of the host.",
        },
    },
    "required": ["host"],
    "anyOf": [
        {"required": ["servers"]},
        {"required": ["search_domains"]},
        {"required": ["host_name"]},
        {"required": ["domain_name"]},
    ],
    "additionalProperties": False,
    "examples": [
        {
            "host": "<host-name>",
            "search_domains": ["example.invalid"],
        }
    ],
}

DNS_SET_RESPONSE_SCHEMA: Final[dict[str, Any]] = _response(DNS_STATE_SCHEMA, ["dhcp_in_use"])

DNS_SET_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to set the DNS servers, search domains, host name or domain name of one ESXi "
        "host. Fields you leave out keep their current value."
    ),
    "preconditions": (
        "Read the host first with vmware.host.config.show. The host must use static DNS "
        "(dns.dhcp false); with DHCP the call answers dhcp_in_use and sends nothing. A new "
        "host name or domain also needs a matching DNS record, or vCenter may lose the host. "
        "The change waits for approval."
    ),
    "parameter_hints": {
        "servers": "IP addresses, e.g. ['<dns-server-ip>'].",
        "host_name": "Only the first label, e.g. '<host-name>', not the full name.",
    },
    "output_shape": (
        "{status: applied|unchanged|dhcp_in_use|failed|not_verified|<host refusal>, host, "
        "requested, before and after: {dhcp, host_name, domain_name, servers, "
        "search_domains}, applied_steps, fault, guidance, issues}."
    ),
    "next_step": (
        "Safe to run twice: unchanged sends nothing. After a name change, check that the "
        "host is still connected in vCenter. On failed, read fault."
    ),
}

# ---------------------------------------------------------------------------
# host.syslog.set
# ---------------------------------------------------------------------------

SYSLOG_SET_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "host": HOST_PARAMETER,
        "targets": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 270,
                "pattern": _SYSLOG_TARGET_PATTERN,
                "description": "One target, such as 'udp://<log-server>:514'.",
            },
            "description": (
                "Remote syslog targets. They replace the current ones. An empty list "
                "removes all remote targets."
            ),
        },
    },
    "required": ["host", "targets"],
    "additionalProperties": False,
    "examples": [{"host": "<host-name>", "targets": ["udp://log-1.example.invalid:514"]}],
}

SYSLOG_SET_RESPONSE_SCHEMA: Final[dict[str, Any]] = _response(SYSLOG_STATE_SCHEMA, [])

SYSLOG_SET_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to set where one ESXi host sends its logs (Syslog.global.logHost), like "
        "'govc host.option.set Syslog.global.logHost'."
    ),
    "preconditions": (
        "Read the host first with vmware.host.config.show. The host's firewall must allow "
        "outgoing syslog (the syslog rule set) for udp or tcp targets on the usual ports. "
        "The change waits for approval."
    ),
    "parameter_hints": {
        "targets": "e.g. ['udp://<log-server>:514', 'ssl://<log-server>:1514']; [] removes all.",
    },
    "output_shape": (
        "{status: applied|unchanged|failed|not_verified|<host refusal>, host, requested: "
        "{targets}, before and after: {log_host, targets}, applied_steps, fault, guidance, "
        "issues}."
    ),
    "next_step": (
        "Safe to run twice: unchanged sends nothing. The host applies the new value itself; "
        "there is no separate reload call. If logs do not arrive, check the firewall rule set."
    ),
}
