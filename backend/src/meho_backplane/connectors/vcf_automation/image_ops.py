# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA ops for the image names of an org (image profiles), tenant plane.

A cloud template asks for an image by name (for example "RHEL 9"). The
org's image profile maps each name to a vCenter template (a *fabric
image*), one profile per region. These four ops read and change that
mapping:

* ``vcfa.tenant.image_profile.list`` (safe) --
  ``GET /iaas/api/image-profiles``
* ``vcfa.tenant.fabric_image.list`` (safe) -- ``GET /iaas/api/fabric-images``
* ``vcfa.tenant.cloud_account.enumerate_images`` (safe) --
  ``POST /iaas/api/cloud-accounts/{id}/private-image-enumeration``, then
  ``GET /iaas/api/request-tracker/{id}`` until the request ends
* ``vcfa.tenant.image_profile.mapping.update`` (caution, a person must
  approve) -- ``GET`` then ``PATCH /iaas/api/image-profiles/{id}``

The ``PATCH`` replaces the whole mapping, so the change op reads the
profile first and sends every other name back unchanged; it sends the
profile's own region (``_links.region.href``). Handlers live in
:mod:`._images`. Field names come from the ``vra-sdk-go`` v0.6.5 IaaS
spec (``ImageProfile``, ``ImageMapping``, ``ImageMappingDescription``,
``FabricImage``, ``RequestTracker``), the file the spec-reconcile lane
pins.

The parameter and response schemas are shared with the Aria Automation 8
ops (:mod:`meho_backplane.connectors.vra8.image_ops`), which run the same
handlers.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_automation._op_schemas import (
    field,
    guidance,
    iaas_page,
    list_of,
    obj,
)
from meho_backplane.connectors.vcf_automation._paths import (
    TENANT_FABRIC_IMAGES_PATH,
    TENANT_IMAGE_ENUMERATION_PATH,
    TENANT_IMAGE_PROFILE_PATH,
    TENANT_IMAGE_PROFILES_PATH,
)
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp, validate_typed_ops

__all__ = [
    "ENUMERATE_IMAGES_PARAMS",
    "ENUMERATE_IMAGES_RESPONSE",
    "FABRIC_IMAGE_LIST_PARAMS",
    "FABRIC_IMAGE_PAGE_RESPONSE",
    "IMAGE_PROFILE_LIST_PARAMS",
    "IMAGE_PROFILE_PAGE_RESPONSE",
    "MAPPING_UPDATE_RESPONSE",
    "VCFA_IMAGE_OPS",
    "mapping_update_params",
]

_GROUP = "vcfa-tenant-images"
_PREFIX = "vcfa.tenant"

_ID: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 256}
#: An id that goes into a URL path: '.' and '..' would not stay one segment.
_PATH_ID: dict[str, Any] = {**_ID, "not": {"enum": [".", ".."]}}

_CONSTRAINTS = list_of(
    "Placement rules for machines made from this image.",
    {
        "expression": field("string", "A tag expression, for example 'env:prod'."),
        "mandatory": field("boolean", "True when the rule must match."),
    },
)

#: One entry of a profile's mapping: the template a name gives.
_MAPPING_ENTRY_FIELDS: dict[str, Any] = {
    "id": field("string", "The fabric image id (the template Automation knows)."),
    "name": field("string", "The template name in vCenter."),
    "externalId": field("string", "The template id in vCenter."),
    "osFamily": field("string", "The operating system family of the template."),
    "isPrivate": field("boolean", "True for a vCenter template or snapshot."),
    "externalRegionId": field("string", "The vCenter region of the template."),
    "constraints": _CONSTRAINTS,
    "cloudConfig": field("string", "Cloud config added to machines made from this image."),
}

_IMAGE_PROFILE_FIELDS: dict[str, Any] = {
    "id": field("string", "The image profile id."),
    "name": field("string", "The image profile name."),
    "description": field("string", "The image profile description."),
    "externalRegionId": field("string", "The vCenter region the profile is for."),
    "cloudAccountId": field("string", "The id of the cloud account the profile belongs to."),
    "orgId": field("string", "The id of the org."),
    "imageMappings": obj(
        "The image names of this profile.",
        {
            "mapping": {
                "type": "object",
                "description": (
                    "Image name (for example 'RHEL 9') to the template it gives. A cloud "
                    "template asks for one of these names."
                ),
                "additionalProperties": {
                    "type": "object",
                    "properties": _MAPPING_ENTRY_FIELDS,
                    "additionalProperties": True,
                },
            },
        },
    ),
    "_links": obj(
        "Links to related objects. '_links.region.href' names the profile's region.",
        {"region": obj("The region link.", {"href": field("string", "The region path.")})},
    ),
    "updatedAt": field("string", "When the profile was last changed (ISO 8601, UTC)."),
}

_FABRIC_IMAGE_FIELDS: dict[str, Any] = {
    "id": field("string", "The fabric image id. Use it in an image name change."),
    "name": field("string", "The template name in vCenter."),
    "externalId": field("string", "The template id in vCenter."),
    "osFamily": field("string", "The operating system family."),
    "isPrivate": field("boolean", "True for a vCenter template or snapshot."),
    "externalRegionId": field("string", "The vCenter region of the template."),
    "cloudAccountIds": {
        "type": "array",
        "description": "The ids of the cloud accounts that see this template.",
        "items": {"type": "string"},
    },
    "updatedAt": field("string", "When Automation last saw a change (ISO 8601, UTC)."),
}

_TARGET: dict[str, Any] = {
    **obj(
        "A template an image name gives, or null when the name has none "
        "(old_target of an add, new_target of a remove).",
        {
            "fabric_image_id": field(["string", "null"], "The fabric image id."),
            "name": field(["string", "null"], "The template name in vCenter."),
            "externalId": field(["string", "null"], "The template id in vCenter."),
        },
    ),
    "type": ["object", "null"],
}

IMAGE_PROFILE_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

IMAGE_PROFILE_PAGE_RESPONSE: Final[dict[str, Any]] = iaas_page(
    "image profiles", _IMAGE_PROFILE_FIELDS
)

FABRIC_IMAGE_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "$filter": {
            "type": "string",
            "minLength": 1,
            "description": "Optional OData filter, for example \"name eq '<template-name>'\".",
        },
        "$top": {
            "type": "integer",
            "minimum": 1,
            "description": "Optional number of items to return (page size).",
        },
        "$skip": {
            "type": "integer",
            "minimum": 0,
            "description": "Optional number of items to skip (offset).",
        },
    },
    "additionalProperties": False,
    "examples": [{"$filter": "name eq '<template-name>'"}],
}

FABRIC_IMAGE_PAGE_RESPONSE: Final[dict[str, Any]] = iaas_page("fabric images", _FABRIC_IMAGE_FIELDS)

ENUMERATE_IMAGES_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "cloud_account_id": {
            **_PATH_ID,
            "description": (
                "The cloud account id: 'cloudAccountId' of an image profile, or one of "
                "the 'cloudAccountIds' of a fabric image."
            ),
        },
    },
    "required": ["cloud_account_id"],
    "additionalProperties": False,
    "examples": [{"cloud_account_id": "<cloud-account-id>"}],
}

ENUMERATE_IMAGES_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": field(
            "string",
            "finished, failed, or in_progress when the wait time ended first.",
            enum=["finished", "failed", "in_progress"],
        ),
        "request": obj(
            "The last state of the request.",
            {
                "id": field("string", "The request id."),
                "status": field("string", "FINISHED, INPROGRESS or FAILED."),
                "progress": field("integer", "The progress, in percent."),
                "message": field(["string", "null"], "The status message."),
            },
        ),
        "guidance": field(["string", "null"], "What to do next, or null."),
    },
    "required": ["status", "request", "guidance"],
    "additionalProperties": False,
}


def mapping_update_params(prefix: str, *, require_expected: bool = False) -> dict[str, Any]:
    """The parameters of ``<prefix>.image_profile.mapping.update``.

    With ``require_expected`` the caller must pass
    ``expected_old_fabric_image_id`` (``null`` is allowed for a new name).
    Then every run checks the template the approver saw, so an approved
    change never overwrites a newer one.
    """
    required = ["profile_id", "image_name"]
    if require_expected:
        required.append("expected_old_fabric_image_id")
    first_word = "Required" if require_expected else "Optional"
    return {
        "type": "object",
        "properties": {
            "profile_id": {
                **_PATH_ID,
                "description": f"The image profile id (from {prefix}.image_profile.list).",
            },
            "image_name": {
                **_ID,
                "description": "The image name to change, for example 'RHEL 9'. Case counts.",
            },
            "fabric_image_id": {
                **_PATH_ID,
                "description": (
                    "The fabric image id the name should give (from "
                    f"{prefix}.fabric_image.list). Needed unless 'remove' is true."
                ),
            },
            "remove": {
                "type": "boolean",
                "description": "True to remove the name. Then leave out 'fabric_image_id'.",
            },
            "expected_old_fabric_image_id": {
                "type": ["string", "null"],
                "maxLength": 256,
                "description": (
                    f"{first_word}. The fabric image id the name gives now, from "
                    f"{prefix}.image_profile.list; null when the name does not exist yet. "
                    "When the name gives something else at run time, nothing is written."
                ),
            },
        },
        "required": required,
        "additionalProperties": False,
        "examples": [
            {
                "profile_id": "<image-profile-id>",
                "image_name": "<image-name>",
                "fabric_image_id": "<fabric-image-id>",
                "expected_old_fabric_image_id": "<old-fabric-image-id>",
            },
            {
                "profile_id": "<image-profile-id>",
                "image_name": "<image-name>",
                "remove": True,
                "expected_old_fabric_image_id": "<old-fabric-image-id>",
            },
        ],
    }


MAPPING_UPDATE_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": field(
            "string",
            "updated, unchanged (nothing to change, nothing written), "
            "invalid_request (nothing written) or precondition_failed (the name "
            "no longer gives expected_old_fabric_image_id; nothing written).",
            enum=["updated", "unchanged", "invalid_request", "precondition_failed"],
        ),
        "profile": obj(
            "The image profile.",
            {
                "id": field("string", "The image profile id."),
                "name": field(["string", "null"], "The image profile name."),
                "region_id": field(["string", "null"], "The region id sent with the change."),
            },
        ),
        "change": obj(
            "What changes for the one image name.",
            {
                "image_name": field("string", "The image name."),
                "action": field(
                    ["string", "null"],
                    "add, repoint or remove; null when nothing changes.",
                ),
                "old_target": _TARGET,
                "new_target": _TARGET,
            },
        ),
        "other_names": {
            "type": "array",
            "description": "The other image names of the profile, all sent back unchanged.",
            "items": {"type": "string"},
        },
        "guidance": field(["string", "null"], "What happened or what to fix, or null."),
    },
    "required": ["status", "guidance"],
    "additionalProperties": True,
}


_IMAGE_PROFILE_LIST = VcfaTypedOp(
    op_id="vcfa.tenant.image_profile.list",
    handler_attr="tenant_image_profile_list",
    plane="tenant",
    path=TENANT_IMAGE_PROFILES_PATH,
    summary="List the image profiles of the tenant org: each image name and the template it gives.",
    description=(
        "Reads GET /iaas/api/image-profiles on the tenant plane. Each profile belongs to "
        "one region and maps image names (for example 'RHEL 9') to vCenter templates."
    ),
    parameter_schema=IMAGE_PROFILE_LIST_PARAMS,
    response_schema=IMAGE_PROFILE_PAGE_RESPONSE,
    group_key=_GROUP,
    tags=("read-only", "vcfa", "tenant", "images"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call to see which image names the org has and which template each name "
            "gives today, for example 'what does RHEL 9 give?'."
        ),
        output_shape=(
            "{content: [ImageProfile], numberOfElements, totalElements}. Each profile has "
            "id, name, externalRegionId, cloudAccountId and imageMappings.mapping: an "
            "object from image name to {id (fabric image id), name (template name), "
            "externalId, osFamily}."
        ),
        next_step=(
            "To change a name, find the template with vcfa.tenant.fabric_image.list, then "
            "call vcfa.tenant.image_profile.mapping.update with the profile id."
        ),
    ),
)

_FABRIC_IMAGE_LIST = VcfaTypedOp(
    op_id="vcfa.tenant.fabric_image.list",
    handler_attr="tenant_fabric_image_list",
    plane="tenant",
    path=TENANT_FABRIC_IMAGES_PATH,
    summary="List the templates (fabric images) VCF Automation knows in the tenant org.",
    description=(
        "Reads GET /iaas/api/fabric-images on the tenant plane: the vCenter templates an "
        "image name can point at. Supports $filter, $top and $skip."
    ),
    parameter_schema=FABRIC_IMAGE_LIST_PARAMS,
    response_schema=FABRIC_IMAGE_PAGE_RESPONSE,
    group_key=_GROUP,
    tags=("read-only", "vcfa", "tenant", "images"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call to find the template an image name should give, and its fabric image "
            "id. Narrow with $filter on the name."
        ),
        output_shape=(
            "{content: [FabricImage], numberOfElements, totalElements}. Each FabricImage "
            "has id, name, externalId, osFamily, isPrivate, externalRegionId and "
            "cloudAccountIds."
        ),
        next_step=(
            "A new template is missing? Run vcfa.tenant.cloud_account.enumerate_images, "
            "then read again. Then change the name with "
            "vcfa.tenant.image_profile.mapping.update."
        ),
    ),
)

_ENUMERATE_IMAGES = VcfaTypedOp(
    op_id="vcfa.tenant.cloud_account.enumerate_images",
    handler_attr="tenant_cloud_account_enumerate_images",
    plane="tenant",
    path=TENANT_IMAGE_ENUMERATION_PATH,
    summary="Make VCF Automation read the templates of a cloud account now, and wait until done.",
    description=(
        "Starts POST /iaas/api/cloud-accounts/{id}/private-image-enumeration and waits "
        "for its request to end. VCF Automation then knows new vCenter templates at once, "
        "not at the next collection. It only reads vCenter."
    ),
    parameter_schema=ENUMERATE_IMAGES_PARAMS,
    response_schema=ENUMERATE_IMAGES_RESPONSE,
    group_key=_GROUP,
    tags=("vcfa", "tenant", "images"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call when a new vCenter template is missing from vcfa.tenant.fabric_image.list. "
            "Safe to run twice."
        ),
        output_shape=(
            "{status: finished | failed | in_progress, request: {id, status, progress, "
            "message}, guidance}. in_progress means the wait time ended first; run it "
            "again later."
        ),
        next_step="Read vcfa.tenant.fabric_image.list again to find the new template.",
    ),
)

_MAPPING_UPDATE = VcfaTypedOp(
    op_id="vcfa.tenant.image_profile.mapping.update",
    handler_attr="tenant_image_profile_mapping_update",
    plane="tenant",
    path=TENANT_IMAGE_PROFILE_PATH,
    summary="Add, re-point or remove one image name in an image profile; keeps all other names.",
    description=(
        "Reads the profile, changes one name, and sends the whole mapping back with "
        "PATCH /iaas/api/image-profiles/{id} and the profile's own region. All other "
        "names stay as they are."
    ),
    parameter_schema=mapping_update_params(_PREFIX),
    response_schema=MAPPING_UPDATE_RESPONSE,
    group_key=_GROUP,
    tags=("write", "vcfa", "tenant", "images"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to point an image name at another template, add a name, or remove one. "
            "Every project of the org that uses the name gets the new template."
        ),
        preconditions=(
            "Read the profile id with vcfa.tenant.image_profile.list and the fabric image "
            "id with vcfa.tenant.fabric_image.list. Pass the name's current fabric image "
            "id as expected_old_fabric_image_id. A person must approve the call; the "
            "approval card shows the old and the new template."
        ),
        output_shape=(
            "{status: updated | unchanged | invalid_request | precondition_failed, "
            "profile: {id, name, "
            "region_id}, change: {image_name, action, old_target, new_target}, "
            "other_names, guidance}."
        ),
        next_step="vcfa.tenant.image_profile.list to confirm the names.",
    ),
)

#: The image ops: reads first, then the change.
VCFA_IMAGE_OPS: Final[tuple[VcfaTypedOp, ...]] = (
    _IMAGE_PROFILE_LIST,
    _FABRIC_IMAGE_LIST,
    _ENUMERATE_IMAGES,
    _MAPPING_UPDATE,
)


validate_typed_ops(VCFA_IMAGE_OPS)
