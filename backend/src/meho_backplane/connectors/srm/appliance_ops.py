# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata for the SRM appliance setup operations.

The reads and changes of the appliance setup API (``/api/rest/configure/v2``):
``srm.appliance.show``, ``srm.appliance.task.get``,
``srm.appliance.register``, ``srm.appliance.network.set`` and
``srm.appliance.time.set``. The certificate operations and the storage
replication adapter install are in
:mod:`meho_backplane.connectors.srm.appliance_ops_more`. Both lists are
joined into ``SRM_TYPED_OPS`` in :mod:`meho_backplane.connectors.srm.typed_ops`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.srm._op import SrmTypedOp

__all__ = [
    "APPLIANCE_REF",
    "APPLIANCE_TYPED_OPS",
    "GROUP_APPLIANCE",
    "GROUP_STORAGE_ADAPTERS",
    "JOB_ANSWER_PROPERTIES",
    "SETUP_WHEN_TO_USE_BY_GROUP",
    "SSO_REF",
    "STEPS_SCHEMA",
]

GROUP_APPLIANCE: Final = "srm-appliance"
GROUP_STORAGE_ADAPTERS: Final = "srm-storage-adapters"

#: Group texts. Each says which reads and changes the group holds.
SETUP_WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    GROUP_APPLIANCE: (
        "Use to read and change the setup of a Site Recovery Manager appliance through "
        "its setup API, logged in with the appliance's own admin user. Reads: "
        "srm.appliance.show (version, registration with vCenter, network, time, "
        "certificate) and srm.appliance.task.get (the state of one setup task). Changes: "
        "srm.appliance.register (register with vCenter and SSO), srm.appliance.network.set "
        "(DNS, host name, interface address), srm.appliance.time.set (time zone and time "
        "sync), srm.appliance.csr.create (a new key and certificate signing request) and "
        "srm.appliance.certificate.set (server certificate, trusted CA certificates)."
    ),
    GROUP_STORAGE_ADAPTERS: (
        "Use to install a storage replication adapter on a Site Recovery Manager "
        "appliance (srm.storage_adapter.install): MEHO fetches the storage vendor's "
        "adapter archive from a URL, checks its SHA-256 and uploads it to the appliance. "
        "One change; the group has no reads."
    ),
}

APPLIANCE_REF: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Vault path (KV v2) of the appliance's own admin user, with the fields username "
        "and password. MEHO logs in to the setup API with it. Never the password itself."
    ),
}
SSO_REF: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Vault path (KV v2) of the vCenter single sign-on admin user, with the fields "
        "username and password. Never the password itself."
    ),
}

STEPS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "The steps in order: done, failed (with error) or not_run.",
    "items": {
        "type": "object",
        "properties": {
            "step": {"type": "string"},
            "status": {"type": "string", "enum": ["done", "failed", "not_run"]},
            "error": {"type": "string"},
        },
    },
}

_TASK_PROPERTIES: Final[dict[str, Any]] = {
    "task_id": {"type": ["string", "null"]},
    "state": {
        "type": "string",
        "enum": [
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelled",
            "unknown",
            "not_reachable",
        ],
    },
    "vendor_status": {"type": ["string", "null"]},
    "progress": {"type": ["integer", "null"]},
    "type": {"type": ["string", "null"]},
    "description": {"type": ["string", "null"]},
    "error": {"type": ["object", "null"]},
    "result": {},
    "start_time": {"type": ["integer", "null"]},
    "complete_time": {"type": ["integer", "null"]},
}

#: The answer of a start call of a vendor job (register, server certificate).
JOB_ANSWER_PROPERTIES: Final[dict[str, Any]] = {
    "outcome": {
        "type": "string",
        "enum": ["accepted", "already_running", "refused", "failed", "done"],
    },
    "task_id": {"type": ["string", "null"]},
    "task": {"type": ["object", "null"], "properties": _TASK_PROPERTIES},
    "status_read": {"type": ["object", "null"]},
    "steps": STEPS_SCHEMA,
}

_JOB_OUTPUT_SHAPE: Final = (
    "outcome: accepted (the vendor took the job; it is NOT done yet), already_running "
    "(a job of the same kind runs; its task_id is returned and nothing new started), "
    "refused (a check stopped it before any change) or failed. task_id, task, "
    "status_read (op_id srm.appliance.task.get and its task_id), steps (done, failed "
    "with the vendor's error, not_run). Nothing that ran is undone."
)
_JOB_NEXT_STEP: Final = (
    "accepted or already_running: call srm.appliance.task.get with task_id and the same "
    "appliance_secret_ref until state is succeeded, failed or cancelled. not_reachable "
    "means read again later, not failed. If the reply of the start call was lost, do not "
    "start again: read srm.appliance.show or the task first. Start with async: true so "
    "MEHO keeps the answer (with task_id) under a run handle."
)

_SHOW = SrmTypedOp(
    op_id="srm.appliance.show",
    handler_attr="appliance_show",
    summary="Read the SRM appliance's setup: version, vCenter registration, network, time.",
    description=(
        "Reads the appliance setup API: version and build, the registration with vCenter "
        "(state, site name, SSO address), DNS and interfaces, time zone and NTP, and the "
        "server certificate (subject, issuer, valid to). Logs in with the appliance admin."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"appliance_secret_ref": APPLIANCE_REF},
        "required": ["appliance_secret_ref"],
        "additionalProperties": False,
        "examples": [{"appliance_secret_ref": "<vault-path-of-appliance-admin>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "appliance": {
                "type": "object",
                "properties": {
                    "hostname": {"type": ["string", "null"]},
                    "appliance_version": {"type": ["string", "null"]},
                    "appliance_build_number": {"type": ["string", "null"]},
                },
            },
            "registration": {
                "type": "object",
                "properties": {
                    "state": {"type": ["string", "null"]},
                    "site_name": {"type": ["string", "null"]},
                    "psc_uri": {"type": ["string", "null"]},
                    "services": {"type": "array"},
                },
            },
            "network": {
                "type": "object",
                "properties": {"dns": {"type": "object"}, "interfaces": {"type": "array"}},
            },
            "time": {
                "type": "object",
                "properties": {
                    "zone": {"type": ["string", "null"]},
                    "sync_mode": {"type": ["string", "null"]},
                    "ntp_servers": {"type": "array"},
                },
            },
            "certificate": {
                "type": "object",
                "properties": {
                    "subject_dn": {"type": ["string", "null"]},
                    "issuer_dn": {"type": ["string", "null"]},
                    "not_after": {"type": ["integer", "null"]},
                },
            },
        },
    },
    group_key=GROUP_APPLIANCE,
    tags=("read-only", "srm", "live-recovery", "appliance"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call before and after any appliance setup change, to see whether the "
            "appliance is registered with vCenter (registration.state CONFIGURED or "
            "UNCONFIGURED), and to read its network, time and certificate."
        ),
        "output_shape": (
            "appliance (hostname, version, build), registration (state, site_name, "
            "admin_email, psc_uri, extension_key, services with state), network (dns: "
            "mode, hostname, servers; interfaces: name, status, ipv4), time (zone, "
            "sync_mode, ntp_servers), certificate (subject_dn, issuer_dn, not_after in "
            "ms, thumbprint, subject_alternative_names). No password or PEM text."
        ),
        "next_step": (
            "Not registered: srm.appliance.register. Wrong DNS or address: "
            "srm.appliance.network.set. Wrong time: srm.appliance.time.set. Certificate "
            "to replace: srm.appliance.csr.create, then srm.appliance.certificate.set."
        ),
    },
)

_TASK_GET = SrmTypedOp(
    op_id="srm.appliance.task.get",
    handler_attr="appliance_task_get",
    summary="Read one task of the SRM appliance setup API: state, progress and error.",
    description=(
        "Reads GET /api/rest/configure/v2/tasks/{task_id}: the vendor's state, progress "
        "and error text. The status read of srm.appliance.register and "
        "srm.appliance.certificate.set. Says not_reachable when the appliance does not answer."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "appliance_secret_ref": APPLIANCE_REF,
            "task_id": {
                "type": "string",
                "minLength": 1,
                "description": "The task id from the start call's answer (task_id).",
            },
        },
        "required": ["appliance_secret_ref", "task_id"],
        "additionalProperties": False,
        "examples": [
            {"appliance_secret_ref": "<vault-path-of-appliance-admin>", "task_id": "<task-id>"}
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            **_TASK_PROPERTIES,
            "reachable": {"type": "boolean"},
            "checked_at": {"type": "string"},
            "unreachable_reason": {"type": ["string", "null"]},
        },
    },
    group_key=GROUP_APPLIANCE,
    tags=("read-only", "srm", "live-recovery", "appliance", "task"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call after srm.appliance.register or srm.appliance.certificate.set returned "
            "accepted or already_running, with its task_id, until the task ends."
        ),
        "output_shape": (
            "task_id, state (queued, running, succeeded, failed, cancelled, unknown or "
            "not_reachable), vendor_status, progress (percent), type, description, error "
            "(message, details) when it failed, result, start_time and complete_time "
            "(ms), reachable, checked_at, unreachable_reason."
        ),
        "next_step": (
            "queued or running: read again in a minute. not_reachable: the appliance did "
            "not answer (for example while it restarts); read again later, it is not a "
            "failure. succeeded: check with srm.appliance.show. failed: report error.message."
        ),
    },
)

_NETWORK_SET = SrmTypedOp(
    op_id="srm.appliance.network.set",
    handler_attr="appliance_network_set",
    summary="Set the SRM appliance's DNS servers and host name, or an interface's IPv4 setup.",
    description=(
        "Sets DNS (PUT .../settings/network/dns) and then the IPv4 address, prefix and "
        "gateway of one interface (POST .../network/interfaces/{name}). A new address on "
        "the interface MEHO uses ends MEHO's session and needs allow_address_change."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "appliance_secret_ref": APPLIANCE_REF,
            "dns": {
                "type": "object",
                "description": "The DNS settings to set. Leave out to keep them.",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["DHCP", "STATIC"],
                        "description": "DHCP: DNS from DHCP. STATIC: the servers below.",
                    },
                    "hostname": {
                        "type": "string",
                        "description": "The appliance's host name.",
                    },
                    "servers": {
                        "type": "array",
                        "description": "DNS server addresses, for mode STATIC.",
                        "items": {"type": "string", "description": "One DNS server address."},
                    },
                },
                "required": ["mode"],
                "additionalProperties": False,
            },
            "interface": {
                "type": "object",
                "description": "One network interface and its new IPv4 setup.",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Interface name as srm.appliance.show lists it.",
                    },
                    "ipv4": {
                        "type": "object",
                        "description": "The IPv4 setup of the interface.",
                        "properties": {
                            "assignment_mode": {
                                "type": "string",
                                "enum": ["DHCP", "STATIC"],
                                "description": "DHCP or a STATIC address.",
                            },
                            "address": {
                                "type": "string",
                                "description": "IPv4 address, for STATIC.",
                            },
                            "prefix": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 32,
                                "description": "Network prefix length, for STATIC.",
                            },
                            "default_gateway": {
                                "type": "string",
                                "description": "IPv4 address of the default gateway.",
                            },
                        },
                        "required": ["assignment_mode"],
                        "additionalProperties": False,
                    },
                },
                "required": ["name", "ipv4"],
                "additionalProperties": False,
            },
            "allow_address_change": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Must be true when the interface MEHO uses gets a new address. The "
                    "change ends MEHO's session; then change the target's host."
                ),
            },
        },
        "required": ["appliance_secret_ref"],
        "anyOf": [{"required": ["dns"]}, {"required": ["interface"]}],
        "additionalProperties": False,
        "examples": [
            {
                "appliance_secret_ref": "<vault-path-of-appliance-admin>",
                "dns": {"mode": "STATIC", "hostname": "<appliance-fqdn>", "servers": ["<dns-ip>"]},
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["done", "failed"]},
            "steps": STEPS_SCHEMA,
            "session_ended": {"type": "boolean"},
            "target_must_change": {"type": ["object", "null"]},
        },
    },
    group_key=GROUP_APPLIANCE,
    tags=("write", "srm", "live-recovery", "appliance", "network"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when the appliance's DNS servers, host name or IPv4 address must change, "
            "for example before it is registered with vCenter."
        ),
        "preconditions": (
            "Read srm.appliance.show first and use the interface name it lists. A wrong "
            "address or gateway can make the appliance unreachable. A new address on the "
            "interface MEHO uses needs allow_address_change: true."
        ),
        "output_shape": (
            "outcome done or failed; steps set_dns and set_interface (done, failed with the "
            "vendor's error, not_run); session_ended true when MEHO's address changed; "
            "target_must_change with the new address and what to do."
        ),
        "next_step": (
            "session_ended: change the target's host to the new address, then call "
            "srm.appliance.show. Otherwise call srm.appliance.show to check the result."
        ),
    },
)

_TIME_SET = SrmTypedOp(
    op_id="srm.appliance.time.set",
    handler_attr="appliance_time_set",
    summary="Set the SRM appliance's time zone and time sync (NTP servers or the host).",
    description=(
        "Sets PUT /api/rest/configure/v2/appliance/settings/time: the time zone and the "
        "time sync mode (NTP with servers, HOST for the ESXi host, or DISABLED), then "
        "reads the time settings back."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "appliance_secret_ref": APPLIANCE_REF,
            "zone": {
                "type": "string",
                "description": "Time zone name, for example Etc/UTC.",
            },
            "sync_mode": {
                "type": "string",
                "enum": ["NTP", "HOST", "DISABLED"],
                "description": "NTP: the servers below. HOST: the ESXi host. DISABLED: none.",
            },
            "ntp_servers": {
                "type": "array",
                "description": "NTP server names or addresses; needed for sync_mode NTP.",
                "items": {"type": "string", "description": "One NTP server."},
            },
        },
        "required": ["appliance_secret_ref"],
        "anyOf": [{"required": ["zone"]}, {"required": ["sync_mode"]}],
        "additionalProperties": False,
        "examples": [
            {
                "appliance_secret_ref": "<vault-path-of-appliance-admin>",
                "zone": "Etc/UTC",
                "sync_mode": "NTP",
                "ntp_servers": ["<ntp-server>"],
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["done"]},
            "time": {
                "type": "object",
                "properties": {
                    "zone": {"type": ["string", "null"]},
                    "sync_mode": {"type": ["string", "null"]},
                    "ntp_servers": {"type": "array"},
                    "current_time": {"type": ["integer", "null"]},
                },
            },
        },
    },
    group_key=GROUP_APPLIANCE,
    tags=("write", "srm", "live-recovery", "appliance", "time"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when the appliance's time zone or time sync is wrong. SSO and vCenter "
            "registration fail when the clock differs from vCenter."
        ),
        "preconditions": (
            "Read srm.appliance.show first. For NTP, check that the servers answer, for "
            "example with net.ntp_check."
        ),
        "output_shape": "outcome done; time (zone, sync_mode, ntp_servers, current_time in ms).",
        "next_step": "Check time.current_time against vCenter; then continue the setup.",
    },
)

_REGISTER = SrmTypedOp(
    op_id="srm.appliance.register",
    handler_attr="appliance_register",
    summary="Register the SRM appliance with its vCenter and SSO; returns the vendor task.",
    description=(
        "Checks the vCenter certificate fingerprint, the SSO login and existing "
        "registrations, then PUTs the configuration. Sends the vCenter as "
        "https://<name>:443. Up to 9.0 it also sends the vCenter id. Returns the task; "
        "the status read is srm.appliance.task.get."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "appliance_secret_ref": APPLIANCE_REF,
            "sso_secret_ref": SSO_REF,
            "vcenter": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Host name of the vCenter (and its SSO). MEHO sends https://<name>:443."
                ),
            },
            "vcenter_fingerprint": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "SHA-256 fingerprint of the vCenter certificate, 64 hex digits, with or "
                    "without colons. A person must confirm it (net.tls_inspect shows it)."
                ),
            },
            "site_name": {
                "type": "string",
                "minLength": 1,
                "description": "The name of this Site Recovery Manager site.",
            },
            "admin_email": {
                "type": "string",
                "minLength": 1,
                "description": "E-mail address of the SRM administrator.",
            },
            "extension_key": {
                "type": "string",
                "default": "com.vmware.vcDr",
                "description": "The SRM extension key in vCenter. Keep the default.",
            },
        },
        "required": [
            "appliance_secret_ref",
            "sso_secret_ref",
            "vcenter",
            "vcenter_fingerprint",
            "site_name",
            "admin_email",
        ],
        "additionalProperties": False,
        "examples": [
            {
                "appliance_secret_ref": "<vault-path-of-appliance-admin>",
                "sso_secret_ref": "<vault-path-of-sso-admin>",
                "vcenter": "<vcenter-fqdn>",
                "vcenter_fingerprint": "<sha256-fingerprint-of-vcenter-certificate>",
                "site_name": "<site-name>",
                "admin_email": "<admin-email>",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            **JOB_ANSWER_PROPERTIES,
            "seen_fingerprint": {"type": "string"},
            "existing_registrations": {"type": "array"},
        },
    },
    group_key=GROUP_APPLIANCE,
    tags=("write", "srm", "live-recovery", "appliance", "registration", "vendor-job"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call once per site, when srm.appliance.show says registration.state "
            "UNCONFIGURED, to connect the appliance to its vCenter and SSO."
        ),
        "preconditions": (
            "The appliance's DNS, time and host name are right (srm.appliance.show). A "
            "person confirmed the vCenter certificate fingerprint. The SSO admin user is "
            "in Vault. If check_existing_registrations finds a registration, the call "
            "stops; a person decides what to do."
        ),
        "output_shape": _JOB_OUTPUT_SHAPE
        + " refused also has seen_fingerprint or existing_registrations.",
        "next_step": _JOB_NEXT_STEP,
    },
)

APPLIANCE_TYPED_OPS: Final[tuple[SrmTypedOp, ...]] = (
    _SHOW,
    _TASK_GET,
    _REGISTER,
    _NETWORK_SET,
    _TIME_SET,
)
