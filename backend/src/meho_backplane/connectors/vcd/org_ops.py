# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the vCD organisation ops: read one, create, change, delete.

The four ops sit in their own group, ``vcd-organizations``. They all use the
CloudAPI organisation paths (``/cloudapi/1.0.0/orgs`` and
``/cloudapi/1.0.0/orgs/{orgUrn}``), which exist on Cloud Director 10.6. The op
bodies are in :mod:`.org_handlers`; :mod:`.typed_ops` appends
:data:`VCD_ORG_OPS` to the connector's op table, so its registrar registers
them.

Risk levels: the read is ``safe``; create is ``caution``; change is
``dangerous`` (it can disable a tenant); delete is ``destructive`` and has a
preview builder (:mod:`.org_handlers`).
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcd.op_types import VcloudDirectorTypedOp

__all__ = [
    "VCD_ORG_GROUP_KEY",
    "VCD_ORG_OPS",
    "VCD_ORG_WHEN_TO_USE",
]

VCD_ORG_GROUP_KEY: Final[str] = "vcd-organizations"

VCD_ORG_WHEN_TO_USE: Final[str] = (
    "Use this group to work with one Cloud Director organization (a tenant). "
    "It holds one read (vcd.org.get: the settings and object counts of one "
    "organization) and three changes: create an organization (vcd.org.create), "
    "change its display name, description or enabled state (vcd.org.update), "
    "and delete a disabled, empty organization (vcd.org.delete). To list all "
    "organizations, use vcd.org.list in the vcd-inventory group."
)

_EXAMPLE_ORG_ID = "urn:vcloud:org:<org-uuid>"

_READ_TAGS = ("read-only", "vcd", "vcloud-director", "org")
_WRITE_TAGS = ("write", "vcd", "vcloud-director", "org")


def _field(type_: str, description: str) -> dict[str, Any]:
    """Return one described schema field."""
    return {"type": type_, "description": description}


_ORG_ID_PARAM = _field(
    "string",
    "The organization id, urn:vcloud:org:<uuid>. vcd.org.list returns it in the id "
    "field. It is not the name.",
)
_DISPLAY_NAME_PARAM = {
    **_field("string", "The full name shown in the portal. At most 128 characters."),
    "minLength": 1,
    "maxLength": 128,
}
_DESCRIPTION_PARAM = {
    **_field("string", "A free text description. At most 256 characters."),
    "maxLength": 256,
}

#: The ``Org`` fields MEHO names in every organisation answer. Cloud Director
#: may add more; they pass through.
_ORG_FIELDS: dict[str, Any] = {
    "id": _field("string", "The organization id, urn:vcloud:org:<uuid>."),
    "name": _field("string", "The short name. It is part of the tenant portal URL."),
    "displayName": _field("string", "The full name shown in the portal."),
    "description": _field("string", "The description."),
    "isEnabled": _field(
        "boolean",
        "Whether the organization is enabled. Users of a disabled organization cannot log in.",
    ),
    "orgVdcCount": _field("integer", "How many organization VDCs it has."),
    "catalogCount": _field("integer", "How many catalogs it has."),
    "vappCount": _field("integer", "How many vApps it has."),
    "runningVMCount": _field("integer", "How many VMs run in it."),
    "userCount": _field("integer", "How many users it has."),
    "diskCount": _field("integer", "How many independent disks it has."),
    "canPublish": _field("boolean", "Whether it may publish catalogs to other organizations."),
    "managedBy": {
        "type": "object",
        "description": "The provider organization that manages it.",
        "properties": {
            "name": _field("string", "The name of the managing organization."),
            "id": _field("string", "The id of the managing organization."),
        },
        "additionalProperties": True,
    },
}

_ORG_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "One organization (the Cloud Director Org object).",
    "properties": _ORG_FIELDS,
    "additionalProperties": True,
}

_TASK_HREF = _field(
    "string",
    "The link to the Cloud Director task that does the work, when Cloud Director "
    "runs it in the background. Empty when it finished at once.",
)

_GET_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to read one organization by its id: its names, whether it is enabled, "
        "and how many VDCs, catalogs, vApps, running VMs, users and disks it has. "
        "Use before you change or delete an organization."
    ),
    "output_shape": (
        "One Org object: id, name, displayName, description, isEnabled, orgVdcCount, "
        "catalogCount, vappCount, runningVMCount, userCount, diskCount, canPublish "
        "and managedBy."
    ),
    "next_step": (
        "To change it, call vcd.org.update. To delete it, first disable it with "
        "vcd.org.update (is_enabled=false), make sure orgVdcCount, catalogCount "
        "and vappCount are 0, then preview and call vcd.org.delete."
    ),
}

_CREATE_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to create a new organization (a tenant). This is step 1 of tenant "
        "onboarding; an organization VDC, networks and users come after it."
    ),
    "preconditions": (
        "The name is not used by another organization (vcd.org.list). It may hold "
        "only letters, digits and the characters _ - . ! ' ( ) , $ and it becomes "
        "part of the tenant portal URL. The call waits until a second person "
        "approves it."
    ),
    "output_shape": (
        "The new Org object, with the id Cloud Director gave it (urn:vcloud:org:<uuid>), "
        "name, displayName, description and isEnabled."
    ),
    "next_step": (
        "Keep the id from the answer. Read it back with vcd.org.get, then create "
        "the organization VDC for this tenant."
    ),
}

_UPDATE_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to change an organization's display name or description, or to "
        "enable or disable it. Disabling stops all its users from logging in, and "
        "it is needed before vcd.org.delete."
    ),
    "preconditions": (
        "Give the org_id and at least one of display_name, description or "
        "is_enabled. MEHO reads the organization first and sends it back with only "
        "these fields changed. The call waits until a second person approves it; "
        "an agent needs a grant for this risk level."
    ),
    "output_shape": (
        "The changed Org object (id, name, displayName, description, isEnabled and "
        "the counts). If Cloud Director runs the change in the background, the "
        "answer also has task_href."
    ),
    "next_step": (
        "Read the organization with vcd.org.get to check the change. If the answer "
        "has task_href, follow the task with vcd.task.list."
    ),
}

_DELETE_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to delete an organization that is disabled and empty: no VDCs, no "
        "catalogs and no vApps. Its users and its settings are deleted with it."
    ),
    "preconditions": (
        "Disable the organization first (vcd.org.update with is_enabled=false), and "
        "remove its VDCs, catalogs and vApps. Then call preview_operation with the "
        "same inputs and pass its preview_hash to call_operation. MEHO refuses the "
        "call when the organization is missing, enabled or not empty. A person must "
        "run it and a second person must approve it; an agent is always refused."
    ),
    "output_shape": (
        "{org_id, name, delete_started, already_absent, task_href}. delete_started "
        "is true when Cloud Director took the delete; task_href links to its task."
    ),
    "next_step": (
        "Follow the task with vcd.task.list, then call vcd.org.get: a not-found "
        "error means the organization is gone."
    ),
}


VCD_ORG_OPS: tuple[VcloudDirectorTypedOp, ...] = (
    VcloudDirectorTypedOp(
        op_id="vcd.org.get",
        handler_attr="org_get",
        summary="Read one organization: settings and object counts.",
        description=(
            "Reads one organization by id (GET /cloudapi/1.0.0/orgs/{orgUrn}): names, "
            "enabled state, and how many VDCs, catalogs, vApps, running VMs and users "
            "it has. Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"org_id": _ORG_ID_PARAM},
            "required": ["org_id"],
            "additionalProperties": False,
            "examples": [{"org_id": _EXAMPLE_ORG_ID}],
        },
        response_schema=_ORG_RESPONSE,
        group_key=VCD_ORG_GROUP_KEY,
        tags=_READ_TAGS,
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_GET_INSTRUCTIONS,
    ),
    VcloudDirectorTypedOp(
        op_id="vcd.org.create",
        handler_attr="org_create",
        summary="Create an organization (tenant onboarding, step 1).",
        description=(
            "Creates an organization (POST /cloudapi/1.0.0/orgs) with a name, a display "
            "name, an optional description, enabled by default. Returns the new "
            "organization with its id. Waits for approval."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": {
                    **_field(
                        "string",
                        "The short name. It becomes part of the tenant portal URL. "
                        "Letters, digits and _ - . ! ' ( ) , $ only. At most 128 "
                        "characters.",
                    ),
                    "minLength": 1,
                    "maxLength": 128,
                },
                "display_name": _DISPLAY_NAME_PARAM,
                "description": _DESCRIPTION_PARAM,
                "is_enabled": {
                    **_field(
                        "boolean",
                        "Whether users can log in to the new organization. Default true.",
                    ),
                    "default": True,
                },
            },
            "required": ["name", "display_name"],
            "additionalProperties": False,
            "examples": [
                {
                    "name": "<org-name>",
                    "display_name": "<org-display-name>",
                    "description": "<description>",
                }
            ],
        },
        response_schema=_ORG_RESPONSE,
        group_key=VCD_ORG_GROUP_KEY,
        tags=_WRITE_TAGS,
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_CREATE_INSTRUCTIONS,
    ),
    VcloudDirectorTypedOp(
        op_id="vcd.org.update",
        handler_attr="org_update",
        summary="Change an organization's names or description, or disable it.",
        description=(
            "Changes the display name, description or enabled state of one "
            "organization (PUT /cloudapi/1.0.0/orgs/{orgUrn}). MEHO reads the "
            "organization first and keeps the other fields. Waits for approval."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "org_id": _ORG_ID_PARAM,
                "display_name": _DISPLAY_NAME_PARAM,
                "description": _DESCRIPTION_PARAM,
                "is_enabled": _field(
                    "boolean",
                    "true enables the organization; false disables it, so its users cannot log in.",
                ),
            },
            "required": ["org_id"],
            "anyOf": [
                {"required": ["display_name"]},
                {"required": ["description"]},
                {"required": ["is_enabled"]},
            ],
            "additionalProperties": False,
            "examples": [{"org_id": _EXAMPLE_ORG_ID, "is_enabled": False}],
        },
        response_schema={
            **_ORG_RESPONSE,
            "properties": {**_ORG_FIELDS, "task_href": _TASK_HREF},
        },
        group_key=VCD_ORG_GROUP_KEY,
        tags=_WRITE_TAGS,
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_UPDATE_INSTRUCTIONS,
    ),
    VcloudDirectorTypedOp(
        op_id="vcd.org.delete",
        handler_attr="org_delete",
        summary="Delete a disabled, empty organization.",
        description=(
            "Deletes one organization (DELETE /cloudapi/1.0.0/orgs/{orgUrn}). It must "
            "be disabled and have no VDCs, catalogs or vApps; its users go with it. "
            "Needs a preview first. Cannot be undone."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"org_id": _ORG_ID_PARAM},
            "required": ["org_id"],
            "additionalProperties": False,
            "examples": [{"org_id": _EXAMPLE_ORG_ID}],
        },
        response_schema={
            "type": "object",
            "properties": {
                "org_id": _field("string", "The organization id."),
                "name": _field("string", "The organization name."),
                "delete_started": _field(
                    "boolean", "Whether Cloud Director took the delete request."
                ),
                "already_absent": _field(
                    "boolean", "True when the organization was already gone; nothing ran."
                ),
                "task_href": _TASK_HREF,
            },
            "additionalProperties": False,
        },
        group_key=VCD_ORG_GROUP_KEY,
        tags=(*_WRITE_TAGS, "destructive"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=_DELETE_INSTRUCTIONS,
    ),
)
