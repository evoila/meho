# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the two guest customization spec reads.

``vmware.guest.customization_spec.list`` and
``vmware.guest.customization_spec.get`` are ``safe`` typed reads. The code
that runs is in :mod:`.customization_specs`; the connector methods
``customization_spec_list`` / ``customization_spec_get`` delegate to it.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.customization_specs import (
    GET_OP_ID,
    GUEST_GROUP_KEY,
    LIST_OP_ID,
    OS_TYPES,
)
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp

__all__ = ["CUSTOMIZATION_SPEC_OPS"]

_TAGS: Final[tuple[str, ...]] = (
    "read-only",
    "vmware",
    "vcenter",
    "guest",
    "customization",
    "provisioning",
)

_NULLABLE_TEXT: Final[dict[str, Any]] = {"type": ["string", "null"]}

_NAME_PARAM: Final[dict[str, Any]] = {"type": "string", "minLength": 1, "maxLength": 512}

_SUMMARY_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {**_NULLABLE_TEXT, "description": "Name of the spec. Pass it to the get read."},
        "description": {**_NULLABLE_TEXT, "description": "Description of the spec."},
        "os_type": {
            "type": ["string", "null"],
            "enum": [*OS_TYPES, None],
            "description": "Guest OS the spec is for: WINDOWS or LINUX.",
        },
        "last_modified": {
            **_NULLABLE_TEXT,
            "description": "When the spec was last changed (ISO 8601 date and time).",
        },
    },
    "required": ["name", "description", "os_type", "last_modified"],
}

VMWARE_CUSTOMIZATION_SPEC_LIST_OP = VmwareTypedOp(
    op_id=LIST_OP_ID,
    handler_attr="customization_spec_list",
    summary="List the saved guest customization specs of a vCenter: name, OS type, last change.",
    description=(
        "Reads GET /vcenter/guest/customization-specs. Returns one row per saved spec: "
        "name, description, OS type (WINDOWS or LINUX) and last change. Filters: OS type "
        "and a name pattern. vCenter only. Needs no catalog ingest. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "os_type": {
                "type": "string",
                "enum": list(OS_TYPES),
                "description": "Optional. Show only the specs for this guest OS.",
            },
            "name": {
                **_NAME_PARAM,
                "description": (
                    "Optional. Show only specs whose name matches this pattern. Use * for "
                    "any text and ? for one character. Letter case does not matter."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{}, {"os_type": "LINUX", "name": "<spec-name>*"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "specs": {
                "type": "array",
                "description": "One row per saved spec that matches the filters.",
                "items": _SUMMARY_ROW_SCHEMA,
            }
        },
        "required": ["specs"],
    },
    group_key=GUEST_GROUP_KEY,
    tags=_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which guest customization specs a vCenter has, for example to pick "
            "one for a clone, a deploy from a VM template, or vm.customize."
        ),
        "parameter_hints": {
            "os_type": "WINDOWS or LINUX; omit it for both.",
            "name": "A pattern such as 'web*'; omit it for every spec.",
        },
        "output_shape": "{specs: [{name, description, os_type, last_modified}]}.",
        "next_step": (
            "Read one spec in full with vmware.guest.customization_spec.get. Pass a name "
            "as guest_customization_spec to vmware.composite.library.vm_template.deploy, "
            "or as customization_spec_name to vmware.composite.vm.clone_from_template."
        ),
    },
)

_CUSTOMIZATION_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": (
        "The spec as vCenter stores it (the vendor CustomizationSpec), with secrets hidden. "
        "Field names are the vendor's."
    ),
    "properties": {
        "configuration_spec": {
            "type": ["object", "null"],
            "description": (
                "Guest OS settings: windows_config (sysprep: computer name, domain, time "
                "zone), linux_config (host name, domain, time zone) or cloud_config "
                "(cloud-init)."
            ),
        },
        "global_dns_settings": {
            "type": ["object", "null"],
            "description": "DNS servers and DNS search suffixes for all NICs.",
        },
        "interfaces": {
            "type": ["array", "null"],
            "description": "IP settings per NIC, in NIC order: DHCP or a fixed address.",
            "items": {"type": "object"},
        },
    },
}

VMWARE_CUSTOMIZATION_SPEC_GET_OP = VmwareTypedOp(
    op_id=GET_OP_ID,
    handler_attr="customization_spec_get",
    summary="Read one saved guest customization spec in full; passwords and scripts are hidden.",
    description=(
        "Reads GET /vcenter/guest/customization-specs/{name}. Returns the spec's guest OS "
        "settings, DNS and the IP settings per NIC. Passwords, keys, scripts and sysprep XML "
        "are never returned; hidden_fields names them. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                **_NAME_PARAM,
                "description": (
                    "Name of the spec, exactly as vmware.guest.customization_spec.list shows it."
                ),
            },
        },
        "required": ["name"],
        "additionalProperties": False,
        "examples": [{"name": "<spec-name>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the spec."},
            "description": {**_NULLABLE_TEXT, "description": "Description of the spec."},
            "last_modified": {**_NULLABLE_TEXT, "description": "When the spec was last changed."},
            "fingerprint": {
                **_NULLABLE_TEXT,
                "description": "vCenter's version mark of the spec; it changes with each edit.",
            },
            "config_kind": {
                "type": ["string", "null"],
                "enum": ["windows", "linux", "cloud_init", None],
                "description": "Which guest OS settings the spec holds.",
            },
            "customization": _CUSTOMIZATION_SCHEMA,
            "hidden_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Paths of the fields MEHO hid, for example "
                    "'configuration_spec.windows_config.sysprep.gui_unattended.password'. "
                    "A hidden field is set in vCenter."
                ),
            },
        },
        "required": [
            "name",
            "description",
            "last_modified",
            "fingerprint",
            "config_kind",
            "customization",
            "hidden_fields",
        ],
    },
    group_key=GUEST_GROUP_KEY,
    tags=_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check what a saved guest customization spec does before you use it: the "
            "host name rule, the IP settings per NIC, DNS and the domain join."
        ),
        "parameter_hints": {
            "name": "The exact spec name from vmware.guest.customization_spec.list.",
        },
        "output_shape": (
            "{name, description, last_modified, fingerprint, config_kind, customization: "
            "{configuration_spec, global_dns_settings, interfaces}, hidden_fields}. A spec that "
            "does not exist gives not_found."
        ),
        "next_step": (
            "Never ask for the hidden values: MEHO does not return them. To use the spec, "
            "pass its name to a clone or a deploy from a VM template."
        ),
    },
)

#: The two reads, in the order :data:`~.typed_ops.VMWARE_TYPED_OPS` lists them.
CUSTOMIZATION_SPEC_OPS: Final[tuple[VmwareTypedOp, ...]] = (
    VMWARE_CUSTOMIZATION_SPEC_LIST_OP,
    VMWARE_CUSTOMIZATION_SPEC_GET_OP,
)
