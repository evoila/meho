# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed Aria Automation 8 ops for image names (image profiles).

A cloud template asks for an image by name (for example "RHEL 9"). An image
profile maps each name to a template (a *fabric image*), one profile per
region. These four ops read and change that mapping on Aria Automation 8.18:

* ``vcfa.vra8.image_profile.list`` (safe) -- ``GET /iaas/api/image-profiles``
* ``vcfa.vra8.fabric_image.list`` (safe) -- ``GET /iaas/api/fabric-images``
* ``vcfa.vra8.cloud_account.enumerate_images`` (caution, approval) --
  ``POST /iaas/api/cloud-accounts/{id}/private-image-enumeration``, then
  ``GET /iaas/api/request-tracker/{id}`` until the request ends
* ``vcfa.vra8.image_profile.mapping.update`` (dangerous, approval) --
  ``GET`` then ``PATCH /iaas/api/image-profiles/{id}``

They are the VCF Automation 9 image ops
(:mod:`meho_backplane.connectors.vcf_automation.image_ops`) on this
connector: the same handlers (:mod:`meho_backplane.connectors.vcf_automation._images`,
through :class:`._images.Vra8ImageOpsMixin`) and the same parameter and
response schemas. Only the texts and the risk levels are this connector's.

The ops have their own registrar, queued in this package's ``__init__``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

from meho_backplane.connectors.vcf_automation._images import register_mapping_update_preview
from meho_backplane.connectors.vcf_automation._op_schemas import guidance
from meho_backplane.connectors.vcf_automation.image_ops import (
    ENUMERATE_IMAGES_PARAMS,
    ENUMERATE_IMAGES_RESPONSE,
    FABRIC_IMAGE_LIST_PARAMS,
    FABRIC_IMAGE_PAGE_RESPONSE,
    IMAGE_PROFILE_LIST_PARAMS,
    IMAGE_PROFILE_PAGE_RESPONSE,
    MAPPING_UPDATE_RESPONSE,
    mapping_update_params,
)
from meho_backplane.connectors.vra8._images import VRA8_IMAGE_PREFIX
from meho_backplane.connectors.vra8.typed_ops import Vra8TypedOp

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VRA8_IMAGE_OPS",
    "VRA8_IMAGE_WHEN_TO_USE_BY_GROUP",
    "register_vra8_image_operations",
]

_log = structlog.get_logger(__name__)

_GROUP = "vcfa-vra8-images"

VRA8_IMAGE_WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    _GROUP: (
        "Image names of Aria Automation 8 (image profiles). A cloud template asks for an "
        "image by name, for example 'RHEL 9'; the profile of each region maps the name to "
        "a template. Read the names (vcfa.vra8.image_profile.list) and the templates Aria "
        "Automation knows (vcfa.vra8.fabric_image.list), make it read new templates now "
        "(vcfa.vra8.cloud_account.enumerate_images; a person must approve it), and add, "
        "re-point or remove one name (vcfa.vra8.image_profile.mapping.update; a person "
        "must approve it)."
    ),
}

_READ_TAGS = ("read-only", "vcfa", "vra8", "vra", "images")
_WRITE_TAGS = ("write", "vcfa", "vra8", "vra", "images")

_IMAGE_PROFILE_LIST = Vra8TypedOp(
    op_id="vcfa.vra8.image_profile.list",
    handler_attr="image_profile_list",
    summary="List the image profiles of Aria Automation 8: each image name and its template.",
    description=(
        "Reads GET /iaas/api/image-profiles. Each profile belongs to one region and maps "
        "image names (for example 'RHEL 9') to templates. A long list comes back as a "
        "link to the stored result."
    ),
    parameter_schema=IMAGE_PROFILE_LIST_PARAMS,
    response_schema=IMAGE_PROFILE_PAGE_RESPONSE,
    group_key=_GROUP,
    tags=_READ_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call to see which image names Aria Automation 8 has and which template each "
            "name gives today, for example 'what does RHEL 9 give?'."
        ),
        output_shape=(
            "{content: [ImageProfile], numberOfElements, totalElements}. Each profile has "
            "id, name, externalRegionId, cloudAccountId and imageMappings.mapping: an "
            "object from image name to {id (fabric image id), name (template name), "
            "externalId, osFamily}."
        ),
        next_step=(
            "To change a name, find the template with vcfa.vra8.fabric_image.list, then "
            "call vcfa.vra8.image_profile.mapping.update with the profile id."
        ),
    ),
)

_FABRIC_IMAGE_LIST = Vra8TypedOp(
    op_id="vcfa.vra8.fabric_image.list",
    handler_attr="fabric_image_list",
    summary="List the templates (fabric images) Aria Automation 8 knows.",
    description=(
        "Reads GET /iaas/api/fabric-images: the templates an image name can point at, "
        "with their region and cloud accounts. Supports $filter, $top and $skip."
    ),
    parameter_schema=FABRIC_IMAGE_LIST_PARAMS,
    response_schema=FABRIC_IMAGE_PAGE_RESPONSE,
    group_key=_GROUP,
    tags=_READ_TAGS,
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
            "A new template is missing? Run vcfa.vra8.cloud_account.enumerate_images, then "
            "read again. Then change the name with vcfa.vra8.image_profile.mapping.update."
        ),
    ),
)

_ENUMERATE_IMAGES = Vra8TypedOp(
    op_id="vcfa.vra8.cloud_account.enumerate_images",
    handler_attr="cloud_account_enumerate_images",
    summary="Make Aria Automation 8 read the templates of a cloud account now; wait until done.",
    description=(
        "Starts POST /iaas/api/cloud-accounts/{id}/private-image-enumeration and follows "
        "GET /iaas/api/request-tracker/{id} until the request ends. Aria Automation then "
        "knows new templates at once, not at the next collection."
    ),
    parameter_schema=ENUMERATE_IMAGES_PARAMS,
    response_schema=ENUMERATE_IMAGES_RESPONSE,
    group_key=_GROUP,
    tags=_WRITE_TAGS,
    safety_level="caution",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call when a new template is missing from vcfa.vra8.fabric_image.list. Safe to "
            "run twice."
        ),
        preconditions=(
            "Read the cloud account id from vcfa.vra8.image_profile.list (cloudAccountId) "
            "or vcfa.vra8.fabric_image.list (cloudAccountIds). A person must approve the "
            "call."
        ),
        output_shape=(
            "{status: finished | failed | in_progress, request: {id, status, progress, "
            "message}, guidance}. in_progress means the wait time ended first; run it "
            "again later."
        ),
        next_step="Read vcfa.vra8.fabric_image.list again to find the new template.",
    ),
)

_MAPPING_UPDATE = Vra8TypedOp(
    op_id="vcfa.vra8.image_profile.mapping.update",
    handler_attr="image_profile_mapping_update",
    summary="Add, re-point or remove one image name in an image profile; keeps all other names.",
    description=(
        "Reads the profile, changes one name, and sends the whole mapping back with "
        "PATCH /iaas/api/image-profiles/{id} and the profile's own region. All other "
        "names stay as they are."
    ),
    parameter_schema=mapping_update_params(VRA8_IMAGE_PREFIX),
    response_schema=MAPPING_UPDATE_RESPONSE,
    group_key=_GROUP,
    tags=_WRITE_TAGS,
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to point an image name at another template, add a name, or remove one. "
            "Every project that uses the name gets the new template."
        ),
        preconditions=(
            "Read the profile id with vcfa.vra8.image_profile.list and the fabric image "
            "id with vcfa.vra8.fabric_image.list. Pass the name's current fabric image "
            "id as expected_old_fabric_image_id. A person must approve the call; the "
            "approval card shows the old and the new template."
        ),
        output_shape=(
            "{status: updated | unchanged | invalid_request | precondition_failed, "
            "profile: {id, name, region_id}, change: {image_name, action, old_target, "
            "new_target}, other_names, guidance}."
        ),
        next_step="vcfa.vra8.image_profile.list to confirm the names.",
    ),
)

#: The image ops: reads first, then the changes.
VRA8_IMAGE_OPS: Final[tuple[Vra8TypedOp, ...]] = (
    _IMAGE_PROFILE_LIST,
    _FABRIC_IMAGE_LIST,
    _ENUMERATE_IMAGES,
    _MAPPING_UPDATE,
)

register_mapping_update_preview(VRA8_IMAGE_PREFIX)


async def register_vra8_image_operations(
    *, embedding_service: EmbeddingService | None = None
) -> None:
    """Upsert the four image ops as typed descriptor rows of the vRA 8 connector."""
    from meho_backplane.connectors.vra8.connector import Vra8Connector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in VRA8_IMAGE_OPS:
        handler = getattr(Vra8Connector, op.handler_attr)
        await register_typed_operation(
            product=Vra8Connector.product,
            version=Vra8Connector.version,
            impl_id=Vra8Connector.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=op.group_key,
            when_to_use=VRA8_IMAGE_WHEN_TO_USE_BY_GROUP[op.group_key],
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
    _log.info("vra8_image_operations_registered", count=len(VRA8_IMAGE_OPS))
