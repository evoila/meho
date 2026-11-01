# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA ops that change an organization's settings (provider plane).

* ``vcfa.provider.org.update`` (dangerous, approval): ``GET``, then ``PUT``
  ``/cloudapi/1.0.0/orgs/{orgUrn}``.
* ``vcfa.provider.org.networking.set`` (dangerous, approval): ``GET``, then
  ``PUT /cloudapi/1.0.0/orgs/{orgUrn}/networkingSettings``.

Both exist on every VCF Automation 9.x version (9.0, 9.1, 9.1.1) with the same
path. Handlers and approval cards live in :mod:`._provider_org_settings`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_automation._op_schemas import field, guidance, obj
from meho_backplane.connectors.vcf_automation._provider_paths import (
    PROVIDER_ORG_NETWORKING_PATH,
    PROVIDER_ORG_PATH,
)
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp, validate_typed_ops

__all__ = ["ORG_PARAM", "VCFA_PROVIDER_ORG_SETTINGS_OPS"]

#: The ``org`` parameter both ops (and the networking ops) share.
ORG_PARAM: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 256,
    "description": (
        "The organization: its name, or its id ('urn:vcloud:org:...'). "
        "vcfa.provider.org.list shows both."
    ),
}

_ORG_STATE: Final[dict[str, Any]] = {
    "id": field(["string", "null"], "The organization id (a URN)."),
    "name": field(["string", "null"], "The organization name; it never changes."),
    "displayName": field(["string", "null"], "The display name."),
    "description": field(["string", "null"], "The description."),
    "isEnabled": field(["boolean", "null"], "True when the organization is on."),
    "canPublish": field(["boolean", "null"], "True when it may publish catalogs to others."),
}

_NETWORKING_VIEW: Final[dict[str, Any]] = {
    "networking_enabled": field(["boolean", "null"], "True when networking is on."),
    "org_name_for_logs": field(
        ["string", "null"], "The short name for logs; it is the id of the NSX project."
    ),
}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {**schema, "type": ["object", "null"]}


_ORG_UPDATE = VcfaTypedOp(
    op_id="vcfa.provider.org.update",
    handler_attr="provider_org_update",
    plane="provider",
    path=PROVIDER_ORG_PATH,
    summary="Change an organization's display name, description, on/off state or publish rights.",
    description=(
        "Reads the organization with GET /cloudapi/1.0.0/orgs/{orgUrn} and sends PUT with the "
        "changed fields. Its name and kind never change. Turning it off stops its users from "
        "signing in. A person must approve the call."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "org": ORG_PARAM,
            "display_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": "The new display name.",
            },
            "description": {
                "type": "string",
                "maxLength": 1024,
                "description": "The new description. An empty string clears it.",
            },
            "is_enabled": {
                "type": "boolean",
                "description": (
                    "True to turn the organization on, false to turn it off. Off stops its "
                    "users from signing in."
                ),
            },
            "can_publish": {
                "type": "boolean",
                "description": "True to let the organization publish catalogs to others.",
            },
        },
        "required": ["org"],
        "anyOf": [
            {"required": ["display_name"]},
            {"required": ["description"]},
            {"required": ["is_enabled"]},
            {"required": ["can_publish"]},
        ],
        "additionalProperties": False,
        "examples": [{"org": "<org-name>", "is_enabled": False}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string",
                "What the op did.",
                enum=["changed", "accepted", "unchanged", "invalid_request", "precondition_failed"],
            ),
            "org": _nullable(obj("The organization after the change.", dict(_ORG_STATE))),
            "previous": _nullable(obj("The organization before the change.", dict(_ORG_STATE))),
            "job_id": field(["string", "null"], "The vendor task id when the answer was 202."),
            "guidance": field(["string", "null"], "A short note on what to do next, or null."),
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group_key="vcfa-provider-networking",
    tags=("write", "vcfa", "provider", "org"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to rename an organization's display name, change its description, turn it "
            "on or off, or let it publish catalogs."
        ),
        preconditions=(
            "Find the organization with vcfa.provider.org.list. Turning it off stops its users "
            "from signing in; say so to the person. A person must approve the call. The run "
            "stops with precondition_failed when the organization changed after the approval "
            "card was made."
        ),
        output_shape=(
            "{status: changed | accepted | unchanged | invalid_request | precondition_failed, "
            "org: {id, name, displayName, description, isEnabled, canPublish}, previous, "
            "job_id, guidance}. 'accepted' means VCF Automation finishes the change on its own."
        ),
        next_step="Read the organization with vcfa.provider.org.list to see the result.",
        parameter_hints={"org": "The name or the id from vcfa.provider.org.list."},
    ),
)

_ORG_NETWORKING_SET = VcfaTypedOp(
    op_id="vcfa.provider.org.networking.set",
    handler_attr="provider_org_networking_set",
    plane="provider",
    path=PROVIDER_ORG_NETWORKING_PATH,
    summary="Turn networking on or off for an organization and set its short name for logs.",
    description=(
        "Reads and changes /cloudapi/1.0.0/orgs/{orgUrn}/networkingSettings. The short name "
        "for logs becomes the id of the organization's NSX project, so it is chosen once: MEHO "
        "refuses to change it. A person must approve the call."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "org": ORG_PARAM,
            "networking_enabled": {
                "type": "boolean",
                "description": (
                    "True to turn networking on. Off works only after the organization's "
                    "networking objects are gone."
                ),
            },
            "org_name_for_logs": {
                "type": "string",
                "minLength": 1,
                "maxLength": 8,
                "description": (
                    "The short name for logs, at most 8 characters. Needed to turn networking "
                    "on when none is set; it cannot change later."
                ),
            },
        },
        "required": ["org", "networking_enabled"],
        "additionalProperties": False,
        "examples": [
            {"org": "<org-name>", "networking_enabled": True, "org_name_for_logs": "<short>"}
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string",
                "What the op did.",
                enum=["changed", "unchanged", "invalid_request", "precondition_failed"],
            ),
            "org_id": field(["string", "null"], "The organization id."),
            "settings": _nullable(obj("The networking settings after the op.", _NETWORKING_VIEW)),
            "previous": _nullable(obj("The networking settings before the op.", _NETWORKING_VIEW)),
            "guidance": field(["string", "null"], "A short note on what to do next, or null."),
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group_key="vcfa-provider-networking",
    tags=("write", "vcfa", "provider", "org", "network"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call before vcfa.provider.regional_networking.set, which VCF Automation refuses "
            "while the organization has no short name for logs ('Short Log Name is not set')."
        ),
        preconditions=(
            "Read the current settings with vcfa.provider.network.list (kind org_networking). "
            "Pick the short name with care: it becomes the NSX project id and cannot change. "
            "A person must approve the call."
        ),
        output_shape=(
            "{status: changed | unchanged | invalid_request | precondition_failed, org_id, "
            "settings: {networking_enabled, org_name_for_logs}, previous, guidance}."
        ),
        next_step=(
            "Give the organization networking in a region with "
            "vcfa.provider.regional_networking.set."
        ),
    ),
)

#: The two organization settings ops.
VCFA_PROVIDER_ORG_SETTINGS_OPS: Final[tuple[VcfaTypedOp, ...]] = (
    _ORG_UPDATE,
    _ORG_NETWORKING_SET,
)

validate_typed_ops(VCFA_PROVIDER_ORG_SETTINGS_OPS)
