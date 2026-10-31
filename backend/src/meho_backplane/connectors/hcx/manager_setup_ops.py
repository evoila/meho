# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the HCX Manager setup operations (the bodies live in ``manager_setup``).

``hcx.manager.config.show`` and ``hcx.manager.service.status`` are reads
(``safe``). The changes wait for a person's approval:
``hcx.manager.location.set`` is ``caution``; ``hcx.manager.vcenter.set``,
``hcx.manager.nsx.set``, ``hcx.manager.role_mapping.set``,
``hcx.manager.activate`` and ``hcx.manager.restart`` are ``dangerous``. The
restart metadata lives in :mod:`.manager_restart_ops`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.hcx.op_types import NO_PARAMS_SCHEMA, HcxTypedOp, string_schema

__all__ = [
    "GROUP_MANAGER_SETUP",
    "HCX_MANAGER_SETUP_OPS",
    "HCX_MANAGER_SETUP_WHEN_TO_USE",
    "SECRET_REF_TEXT",
    "SETUP_PRECONDITION",
    "STEPS_SCHEMA",
]

GROUP_MANAGER_SETUP: Final = "hcx-manager-setup"

HCX_MANAGER_SETUP_WHEN_TO_USE: Final[dict[str, str]] = {
    GROUP_MANAGER_SETUP: (
        "Use to set up a new HCX Manager through its appliance management API (port "
        "9443). Reads: the setup as it is now (hcx.manager.config.show) and the state of "
        "the HCX application service (hcx.manager.service.status). Changes: connect "
        "vCenter and SSO (hcx.manager.vcenter.set) and NSX (hcx.manager.nsx.set), set "
        "the site location (hcx.manager.location.set) and role mapping "
        "(hcx.manager.role_mapping.set), activate HCX (hcx.manager.activate) and "
        "restart the application service so a change takes effect (hcx.manager.restart)."
    ),
}

#: Shared text of every change.
SETUP_PRECONDITION: Final = (
    "Waits for a second person's approval. An agent needs a grant for dangerous "
    "changes. The target's appliance account (extras.appliance_secret_ref, or the "
    "target's own secret) must be the HCX appliance admin account."
)

SECRET_REF_TEXT: Final = (
    "Vault reference (path) of a secret with the fields username and password, read "
    "under your identity just before the call. Never the password itself."
)

_SECRET_REF_FIELD: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": SECRET_REF_TEXT,
}

_URL_PATTERN: Final = r"^https://[^/\s]+(/\S*)?$"

STEPS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "The steps in order, with state done, failed, skipped, not_run or unknown.",
    "items": {
        "type": "object",
        "properties": {
            "step": {"type": "string", "description": "Name of the step."},
            "state": {
                "type": "string",
                "enum": ["done", "failed", "skipped", "not_run", "unknown"],
                "description": "What happened to the step.",
            },
            "error": string_schema("The vendor's error text when the step failed."),
        },
    },
}

_VCENTER_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The registered vCenter; null when none is registered.",
    "properties": {
        "url": string_schema("Address of vCenter."),
        "user_name": string_schema("User HCX logs in to vCenter with."),
        "name": string_schema("vCenter name as HCX reports it."),
        "version": string_schema("vCenter version."),
        "build": string_schema("vCenter build."),
        "vc_uuid": string_schema("vCenter instance id."),
        "uuid": string_schema("Id of the registration in HCX."),
    },
}

_LOOKUP_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The registered SSO lookup service; null when none is registered.",
    "properties": {
        "url": string_schema("Lookup service address."),
        "provider_type": string_schema("Vendor provider type, for example PSC."),
        "uuid": string_schema("Id of the registration in HCX."),
    },
}

_NSX_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The registered NSX Manager; null when none is registered.",
    "properties": {
        "url": string_schema("Address of NSX."),
        "user_name": string_schema("User HCX logs in to NSX with."),
        "version": string_schema("NSX version."),
        "type": string_schema("Vendor NSX type."),
        "nsx_uuid": string_schema("NSX id."),
        "uuid": string_schema("Id of the registration in HCX."),
    },
}

_LOCATION_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The site location; null when none is set.",
    "properties": {
        "city": string_schema("City."),
        "country": string_schema("Country."),
        "province": string_schema("Province or state."),
        "latitude": {"type": ["number", "null"], "description": "Latitude."},
        "longitude": {"type": ["number", "null"], "description": "Longitude."},
    },
}

ROLE_MAPPINGS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "One row per HCX role with the SSO groups that get it.",
    "items": {
        "type": "object",
        "properties": {
            "role": string_schema("HCX role name, for example System Administrator."),
            "user_groups": {
                "type": "array",
                "items": {"type": "string"},
                "description": "SSO groups with the role, as DOMAIN\\group.",
            },
        },
    },
}

ACTIVATION_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": "The activation state. The key never comes back.",
    "properties": {
        "activated": {
            "type": ["boolean", "null"],
            "description": "True when HCX is active; null when the Manager has no state call.",
        },
        "status": string_schema("Vendor activation status."),
        "type": string_schema("activation_key (HCX 4.x) or the vendor activation type."),
        "source": {
            "type": ["string", "null"],
            "enum": ["hcx_config", "activation_status", None],
            "description": "Which call gave the state: the 4.x key config or the 9.x status.",
        },
        "server_url": string_schema("Activation server of HCX 4.x."),
        "grace_period_expiration": string_schema("End of the grace period, when reported."),
    },
}

_CONFIG_SHOW = HcxTypedOp(
    op_id="hcx.manager.config.show",
    handler_attr="manager_config_show",
    summary="Show how the HCX Manager is set up: vCenter, SSO, NSX, location, roles, activation.",
    description=(
        "Reads the HCX Manager setup from its appliance management API: the registered "
        "vCenter, SSO lookup service and NSX, the site location, the role mapping and "
        "whether HCX is active. Never a password or key."
    ),
    parameter_schema=NO_PARAMS_SCHEMA,
    response_schema={
        "type": "object",
        "properties": {
            "vcenter": _VCENTER_SCHEMA,
            "lookup_service": _LOOKUP_SCHEMA,
            "nsx": _NSX_SCHEMA,
            "location": _LOCATION_SCHEMA,
            "role_mappings": ROLE_MAPPINGS_SCHEMA,
            "activation": ACTIVATION_SCHEMA,
            "missing": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Sections whose call this HCX version does not have.",
            },
        },
    },
    group_key=GROUP_MANAGER_SETUP,
    tags=("read-only", "hcx", "manager-setup"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first when you set up an HCX Manager, and again after each change and "
            "restart, to see what is registered and what is missing."
        ),
        "output_shape": (
            "{vcenter, lookup_service, nsx, location, role_mappings: [{role, user_groups}], "
            "activation: {activated, status, type, source, server_url, "
            "grace_period_expiration}, missing}. A null section is not set up yet."
        ),
        "next_step": (
            "Set up what is missing in this order: hcx.manager.vcenter.set, "
            "hcx.manager.nsx.set, hcx.manager.location.set, hcx.manager.role_mapping.set, "
            "hcx.manager.activate, then hcx.manager.restart."
        ),
    },
)

_VCENTER_SET = HcxTypedOp(
    op_id="hcx.manager.vcenter.set",
    handler_attr="manager_vcenter_set",
    summary="Connect the HCX Manager to its vCenter and to the SSO lookup service.",
    description=(
        "Registers vCenter with a user and password from Vault, then the SSO lookup "
        "service. A restart (hcx.manager.restart) makes it take effect."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "vcenter_url": {
                "type": "string",
                "pattern": _URL_PATTERN,
                "description": "vCenter address, https://<vcenter-fqdn>.",
            },
            "vcenter_secret_ref": _SECRET_REF_FIELD,
            "lookup_service_url": {
                "type": "string",
                "pattern": _URL_PATTERN,
                "description": "SSO lookup service address, often https://<vcenter-fqdn>.",
            },
        },
        "required": ["vcenter_url", "vcenter_secret_ref", "lookup_service_url"],
        "additionalProperties": False,
        "examples": [
            {
                "vcenter_url": "https://<vcenter-fqdn>",
                "vcenter_secret_ref": "<vault-path>",
                "lookup_service_url": "https://<vcenter-fqdn>",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["done", "failed"],
                "description": "done, or failed when the second step failed.",
            },
            "vcenter": _VCENTER_SCHEMA,
            "lookup_service": _LOOKUP_SCHEMA,
            "steps": STEPS_SCHEMA,
            "restart_needed": {"type": "boolean", "description": "Always true."},
        },
    },
    group_key=GROUP_MANAGER_SETUP,
    tags=("write", "hcx", "manager-setup", "vcenter"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": "Call on a new HCX Manager, or to point it at another vCenter.",
        "preconditions": (
            SETUP_PRECONDITION + " HCX must trust the vCenter certificate; if not, trust "
            "it first with hcx.certificate.trust."
        ),
        "output_shape": (
            "{status, vcenter, lookup_service, steps: [{step, state, error}], "
            "restart_needed}. status failed: vCenter was registered, the lookup service "
            "step failed; nothing is undone."
        ),
        "next_step": (
            "Run hcx.manager.restart so the change takes effect. If status is failed, fix "
            "the cause and call again. If the reply was lost, read hcx.manager.config.show "
            "before you call again. A refusal that names the certificate means: trust it "
            "first with hcx.certificate.trust."
        ),
    },
)

_NSX_SET = HcxTypedOp(
    op_id="hcx.manager.nsx.set",
    handler_attr="manager_nsx_set",
    summary="Connect the HCX Manager to NSX Manager.",
    description=(
        "Registers NSX Manager with a user and password from Vault. A restart "
        "(hcx.manager.restart) makes it take effect."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "nsx_url": {
                "type": "string",
                "pattern": _URL_PATTERN,
                "description": "NSX Manager address, https://<nsx-fqdn>.",
            },
            "nsx_secret_ref": _SECRET_REF_FIELD,
        },
        "required": ["nsx_url", "nsx_secret_ref"],
        "additionalProperties": False,
        "examples": [{"nsx_url": "https://<nsx-fqdn>", "nsx_secret_ref": "<vault-path>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["done"], "description": "done."},
            "nsx": _NSX_SCHEMA,
            "restart_needed": {"type": "boolean", "description": "Always true."},
        },
    },
    group_key=GROUP_MANAGER_SETUP,
    tags=("write", "hcx", "manager-setup", "nsx"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": "Call on a new HCX Manager after hcx.manager.vcenter.set.",
        "preconditions": (
            SETUP_PRECONDITION + " HCX must trust the NSX certificate; if not, trust it "
            "first with hcx.certificate.trust."
        ),
        "output_shape": "{status, nsx: {url, user_name, version, type, nsx_uuid, uuid}, ...}.",
        "next_step": (
            "Run hcx.manager.restart so the change takes effect. If the reply was lost, "
            "read hcx.manager.config.show before you call again."
        ),
    },
)

_LOCATION_SET = HcxTypedOp(
    op_id="hcx.manager.location.set",
    handler_attr="manager_location_set",
    summary="Set the HCX site location to a city from HCX's own city list.",
    description=(
        "Searches HCX's city list and sets the one city that matches as the site "
        "location, which HCX shows on its site map. Refuses when no city or several match."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "city": {"type": "string", "minLength": 2, "description": "City name, in English."},
            "country": {"type": "string", "description": "Country, to pick one of several."},
            "province": {"type": "string", "description": "Province or state, to pick one."},
        },
        "required": ["city"],
        "additionalProperties": False,
        "examples": [{"city": "<city>", "country": "<country>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["done"], "description": "done."},
            "location": _LOCATION_SCHEMA,
        },
    },
    group_key=GROUP_MANAGER_SETUP,
    tags=("write", "hcx", "manager-setup", "location"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": "Call on a new HCX Manager to set where the site is.",
        "preconditions": SETUP_PRECONDITION,
        "output_shape": "{status, location: {city, country, province, latitude, longitude}}.",
        "next_step": (
            "If the call says several cities match, call again with country (and "
            "province). If none matches, pick the nearest larger city it lists."
        ),
    },
)

HCX_MANAGER_SETUP_OPS: Final[tuple[HcxTypedOp, ...]] = (
    _CONFIG_SHOW,
    _VCENTER_SET,
    _NSX_SET,
    _LOCATION_SET,
)
