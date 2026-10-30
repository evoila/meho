# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and AI guidance of ``vmware.composite.vcenter.appliance.deploy`` (#4394).

Also the parts the other install schemas share (:mod:`._vcenter_install_schemas`):
the name patterns, the three Vault fields of a password named by reference,
and the issue row.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import CONTENT_LIBRARY_ID_PATTERN

__all__ = [
    "APPLIANCE_DEPLOY_LLM_INSTRUCTIONS",
    "APPLIANCE_DEPLOY_PARAMETER_SCHEMA",
    "APPLIANCE_DEPLOY_RESPONSE_SCHEMA",
    "HOST_NAME_PATTERN",
    "ISSUE_SCHEMA",
    "secret_ref_fields",
]

# ``$(?!\n)`` ends a pattern: Python's ``re.search`` lets a bare ``$`` match
# before a trailing newline.
HOST_NAME_PATTERN: Final = r"^[A-Za-z0-9]([A-Za-z0-9.:-]{0,251}[A-Za-z0-9])?$(?!\n)"
_IP_PATTERN: Final = r"^[0-9A-Fa-f:.]{2,45}$(?!\n)"
_RESOURCE_POOL_PATTERN: Final = r"^resgroup-v?[0-9]+$(?!\n)"
_DATASTORE_PATTERN: Final = r"^datastore-[0-9]+$(?!\n)"
_HOST_PATTERN: Final = r"^host-[0-9]+$(?!\n)"
_FOLDER_PATTERN: Final = r"^group-v[0-9]+$(?!\n)"
_PORT_GROUP_PATTERN: Final = r"^(network|dvportgroup)-[0-9]+$(?!\n)"


def secret_ref_fields(prefix: str, what: str) -> dict[str, Any]:
    """The three Vault fields of a password the call names by reference."""
    return {
        f"{prefix}_secret_ref": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "description": (
                f"Vault path of the secret that holds {what}. Never the password itself. "
                "MEHO reads it when the call runs, under the identity of the person who "
                "runs it (after an approval: the approver)."
            ),
        },
        f"{prefix}_secret_mount": {
            "type": "string",
            "minLength": 1,
            "description": "Vault KV version 2 mount of the secret (default 'secret').",
        },
        f"{prefix}_secret_key": {
            "type": "string",
            "minLength": 1,
            "description": "Key in the secret that holds the password (default 'password').",
        },
    }


ISSUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "description": "Which step the issue comes from."},
        "severity": {
            "type": "string",
            "enum": ["error", "warning", "info"],
            "description": "error: the call did not do what was asked.",
        },
        "message": {"type": "string", "description": "What happened, in plain words."},
    },
    "required": ["category", "severity", "message"],
}

# ===========================================================================
# vcenter.appliance.deploy
# ===========================================================================

_NETWORK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Network settings of the new appliance. mode static needs address, prefix and "
        "gateway; mode dhcp needs none of them."
    ),
    "properties": {
        "ip_family": {
            "type": "string",
            "enum": ["ipv4", "ipv6"],
            "description": "IP family (default ipv4).",
        },
        "mode": {
            "type": "string",
            "enum": ["static", "dhcp"],
            "description": "static (fixed address) or dhcp.",
        },
        "address": {
            "type": "string",
            "pattern": _IP_PATTERN,
            "description": "IP address of the appliance (mode static).",
        },
        "prefix": {
            "type": "integer",
            "minimum": 0,
            "maximum": 128,
            "description": "Network prefix length, for example 24 (mode static).",
        },
        "gateway": {
            "type": "string",
            "pattern": _IP_PATTERN,
            "description": "Default gateway (mode static).",
        },
        "dns_servers": {
            "type": "array",
            "items": {
                "type": "string",
                "pattern": _IP_PATTERN,
                "description": "IP address of one DNS server.",
            },
            "minItems": 1,
            "maxItems": 4,
            "description": "DNS servers. The host name must resolve on them for step 2.",
        },
    },
    "required": ["mode"],
    "oneOf": [
        {
            "properties": {"mode": {"const": "static", "description": "A fixed address."}},
            "required": ["address", "prefix", "gateway"],
        },
        {"properties": {"mode": {"const": "dhcp", "description": "An address from DHCP."}}},
    ],
    "additionalProperties": False,
}

APPLIANCE_DEPLOY_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "library_item": {
            "type": "string",
            "pattern": CONTENT_LIBRARY_ID_PATTERN,
            "description": (
                "Content-library item id (a UUID) of the vCenter appliance file (OVA). Give "
                "exactly one of library_item and library_item_name."
            ),
        },
        "library_item_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name of the content-library item of the vCenter appliance file. No match "
                "gives item_not_found, more than one gives ambiguous_item; nothing is imported."
            ),
        },
        "library_name": {
            "type": "string",
            "minLength": 1,
            "description": "Optional name of the library that holds library_item_name.",
        },
        "resource_pool": {
            "type": "string",
            "pattern": _RESOURCE_POOL_PATTERN,
            "description": "Resource pool id (for a cluster: its root pool) to import into.",
        },
        "datastore": {
            "type": "string",
            "pattern": _DATASTORE_PATTERN,
            "description": "Datastore id for the disks of the new appliance.",
        },
        "host": {
            "type": "string",
            "pattern": _HOST_PATTERN,
            "description": "Optional host id; leave it out for a DRS cluster.",
        },
        "folder": {
            "type": "string",
            "pattern": _FOLDER_PATTERN,
            "description": "Optional VM folder id; default the datacenter's VM folder.",
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "description": "Name of the new appliance VM.",
        },
        "port_group": {
            "type": "string",
            "pattern": _PORT_GROUP_PATTERN,
            "description": "Network id (port group) the appliance connects to.",
        },
        "ovf_network_name": {
            "type": "string",
            "minLength": 1,
            "description": "OVF network name in the appliance file (default 'Network 1').",
        },
        "size": {
            "type": "string",
            "enum": ["tiny", "small", "medium", "large", "xlarge"],
            "description": "Appliance size: how many hosts and VMs the new vCenter can manage.",
        },
        "storage_size": {
            "type": "string",
            "enum": ["default", "large", "xlarge"],
            "description": "Disk size of the appliance (default 'default').",
        },
        "disk_mode": {
            "type": "string",
            "enum": ["thin", "thick", "eagerZeroedThick"],
            "description": "Disk provisioning of all disks (default thin).",
        },
        "host_name": {
            "type": "string",
            "pattern": HOST_NAME_PATTERN,
            "description": (
                "System name of the appliance: its fully qualified host name (or its IP "
                "address). It must resolve in DNS, forward and reverse, for step 2."
            ),
        },
        "network": _NETWORK_SCHEMA,
        **secret_ref_fields("root_password", "the root password the new appliance gets"),
        "power_on": {
            "type": "boolean",
            "description": "Power on the appliance after the import (default true).",
        },
    },
    "required": [
        "resource_pool",
        "datastore",
        "name",
        "port_group",
        "size",
        "host_name",
        "network",
        "root_password_secret_ref",
    ],
    "oneOf": [{"required": ["library_item"]}, {"required": ["library_item_name"]}],
    "additionalProperties": False,
    "examples": [
        {
            "library_item_name": "<vcenter-appliance-item>",
            "resource_pool": "resgroup-8",
            "datastore": "datastore-15",
            "port_group": "dvportgroup-21",
            "name": "<new-vcenter-vm-name>",
            "size": "small",
            "host_name": "new-vcenter.example.test",
            "network": {"mode": "dhcp"},
            "root_password_secret_ref": "<vault-path-of-the-root-password>",
        }
    ],
}

APPLIANCE_DEPLOY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "deployed",
                "deploy_failed",
                "deploy_error",
                "invalid_reference",
                "library_not_found",
                "ambiguous_library",
                "item_not_found",
                "ambiguous_item",
                "resolve_error",
            ],
            "description": (
                "deployed: imported (and powered on when asked). deploy_failed: vCenter refused "
                "the file or a setting before any change. deploy_error: the import or the copy "
                "failed; MEHO aborted it. The others: the library item was not found."
            ),
        },
        "vm_id": {"type": ["string", "null"], "description": "Id of the new appliance VM."},
        "resource_type": {
            "type": ["string", "null"],
            "description": "VirtualMachine (or VirtualApp).",
        },
        "library_item_id": {"type": ["string", "null"], "description": "The item imported."},
        "name": {"type": ["string", "null"], "description": "Name of the appliance VM."},
        "deployment_option": {
            "type": ["string", "null"],
            "description": "The OVF deployment option sent, for example small-lstorage.",
        },
        "powered_on": {"type": "boolean", "description": "True when the VM was started."},
        "settings_sent": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ids of the OVF properties MEHO sent (never their values).",
        },
        "settings_not_used": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Sent property ids that a vCenter warning says it did not use.",
        },
        "issues": {
            "type": "array",
            "items": ISSUE_SCHEMA,
            "description": "Vendor errors and warnings, and the power-on result.",
        },
        "transfer": {
            "type": "array",
            "description": "One row per file copied to the new VM.",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File name in the appliance file."},
                    "device_id": {"type": "string", "description": "Disk of the new VM."},
                    "size_bytes": {"type": "integer", "description": "Bytes copied."},
                    "sha256": {"type": "string", "description": "SHA-256 of the bytes copied."},
                },
            },
        },
        "candidates": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "Item or library ids when the name matched more than one.",
        },
    },
    "required": ["status", "vm_id", "powered_on", "issues", "transfer"],
}

APPLIANCE_DEPLOY_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call as step 1 of a new vCenter: on an existing vCenter target, to import the "
        "vCenter appliance file (OVA) from its content library. Use MEHO's async mode "
        "(async: true): the copy of the disks can take an hour, and the call runs until the "
        "copy and the clean-up are done. MEHO answers 202 with a run handle at once; the "
        "stored result of the run has the final answer."
    ),
    "preconditions": (
        "The appliance file is in a content library of this vCenter. The root password is "
        "in Vault at root_password_secret_ref. The host name resolves in DNS, forward and "
        "reverse. No VM with this name exists in the folder."
    ),
    "parameter_hints": {
        "size": "tiny (10 hosts, 100 VMs) to xlarge (2,000 hosts, 35,000 VMs).",
        "network": "static: address, prefix, gateway and dns_servers. dhcp: no address.",
        "host_name": "The FQDN the new vCenter will use. MEHO sends it as net.pnid.",
        "port_group": "Id of a network or distributed port group, from a network read.",
    },
    "output_shape": (
        "{status, vm_id, resource_type, library_item_id, name, deployment_option, powered_on, "
        "settings_sent, settings_not_used, issues: [{category, severity, message}], "
        "transfer: [{path, device_id, size_bytes, sha256}], candidates}. Never a password."
    ),
    "next_step": (
        "Wait until the VM has booted (a few minutes). Add the new appliance as a MEHO target "
        "on port 5480 with a Vault login of user root and this root password. Its factory "
        "certificate names no host, so that target needs verify_tls false until the setup "
        "has replaced it. Then call vmware.vcenter.deployment.status on it (state ready) and "
        "vmware.composite.vcenter.install. If the run broke part-way, check the VM name in "
        "the inventory first: a half-made VM is removed by vCenter, a finished one stays."
    ),
    "result_scalars": {
        "keys": ["status", "vm_id", "library_item_id", "name", "deployment_option", "powered_on"]
    },
}
