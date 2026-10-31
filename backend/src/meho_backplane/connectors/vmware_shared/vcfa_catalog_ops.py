# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The four catalog ops (content sources and sharing), written once for both connectors.

Same shape as :mod:`.vcfa_template_ops`: texts use ``{ns}`` for the op id prefix.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_shared.vcfa_catalog import (
    CATALOG_ENTITLEMENTS_PATH,
    CATALOG_SOURCES_PATH,
)
from meho_backplane.connectors.vmware_shared.vcfa_content import (
    ContentOpSpec,
    change_answer,
    field,
    flat_page,
    guidance,
    nullable,
    obj,
)
from meho_backplane.connectors.vmware_shared.vcfa_template_ops import (
    PAGING_FIELDS,
    PROJECT_ID_FIELD,
)

__all__ = ["CATALOG_OPS"]

_SOURCE_FIELDS: dict[str, Any] = {
    "id": field(["string", "null"], "The content source id."),
    "name": field(["string", "null"], "The content source name."),
    "description": field(["string", "null"], "The description."),
    "typeId": field(["string", "null"], "The source type, 'com.vmw.blueprint' for templates."),
    "projectId": field(["string", "null"], "The project that owns the source."),
    "sourceProjectId": field(["string", "null"], "The project whose templates it imports."),
    "global": field(["boolean", "null"], "True when every project may request its items."),
    "itemsFound": field(["integer", "null"], "Items found at the last import."),
    "itemsImported": field(["integer", "null"], "Items imported at the last import."),
    "lastImportStartedAt": field(["string", "null"], "When the last import started."),
    "lastImportCompletedAt": field(["string", "null"], "When the last import ended."),
    "lastImportErrors": {
        "type": "array",
        "description": "Errors of the last import.",
        "items": {"type": "string", "description": "One error."},
    },
}
_SHARE_FIELDS: dict[str, Any] = {
    "id": field(["string", "null"], "The share (entitlement) id."),
    "projectId": field(["string", "null"], "The project it is shared with."),
    "contentId": field(["string", "null"], "The shared source or item id."),
    "contentType": field(
        ["string", "null"], "CatalogSourceIdentifier (a source) or CatalogItemIdentifier."
    ),
    "contentName": field(["string", "null"], "The name of the source or item."),
    "sourceName": field(["string", "null"], "The source name, for a shared item."),
    "sourceType": field(["string", "null"], "The source type."),
}


_SOURCE_LIST = ContentOpSpec(
    name="catalog.source.list",
    handler_attr="vcfa_catalog_source_list",
    path=CATALOG_SOURCES_PATH,
    summary="List the content sources of the catalog.",
    description=(
        "Reads GET /catalog/api/admin/sources. Returns one flat row per source, with "
        "its type, the project it imports from and the last import. A long list comes "
        "back as a result handle."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "minLength": 1,
                "description": "Optional: only sources whose items this project can request.",
            },
            "search": {
                "type": "string",
                "minLength": 1,
                "description": "Optional text to find in the name or description.",
            },
            **PAGING_FIELDS,
        },
        "additionalProperties": False,
        "examples": [{}, {"search": "<source-name>"}],
    },
    response_schema=flat_page("sources", _SOURCE_FIELDS),
    group="catalog",
    tags=("read-only", "vcfa", "catalog", "content-source"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call="Call to see where the catalog items come from.",
        output_shape=(
            "{content: [{id, name, typeId, projectId, sourceProjectId, itemsFound, "
            "itemsImported, lastImportCompletedAt, lastImportErrors}], totalElements}."
        ),
        next_step=(
            "Share a source with a project with {ns}.catalog.share.set (content_type "
            "'source', content_id = the source id)."
        ),
    ),
)

_SOURCE_CREATE = ContentOpSpec(
    name="catalog.source.create",
    handler_attr="vcfa_catalog_source_create",
    path=CATALOG_SOURCES_PATH,
    summary="Add a content source that imports the cloud templates of a project.",
    description=(
        "Sends POST /catalog/api/admin/sources with type com.vmw.blueprint. The "
        "catalog then imports the released templates of the project. A source with "
        "this name answers 'unchanged'."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": "The source name, unique in the catalog.",
            },
            "source_project_id": {
                "type": "string",
                "minLength": 1,
                "description": "The project whose released templates it imports.",
            },
            "description": {
                "type": "string",
                "maxLength": 1024,
                "description": "Optional source description.",
            },
        },
        "required": ["name", "source_project_id"],
        "additionalProperties": False,
        "examples": [{"name": "<source-name>", "source_project_id": "<project-id>"}],
    },
    response_schema=change_answer(
        ["created", "unchanged"], "source", obj("The content source.", _SOURCE_FIELDS)
    ),
    group="catalog",
    tags=("write", "vcfa", "catalog", "content-source"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call once per project, so its released templates reach the catalog.",
        preconditions=(
            "The project has a released template ({ns}.blueprint.version.list). A person "
            "must approve the call."
        ),
        output_shape="{status: created | unchanged, source: {id, name, itemsFound, "
        "itemsImported}, guidance}.",
        next_step="Share the source with a project with {ns}.catalog.share.set.",
    ),
)

_SHARE_LIST = ContentOpSpec(
    name="catalog.share.list",
    handler_attr="vcfa_catalog_share_list",
    path=CATALOG_ENTITLEMENTS_PATH,
    summary="List what the catalog shares with which project (entitlements).",
    description=(
        "Reads GET /catalog/api/admin/entitlements. Returns one flat row per share: "
        "the project and the shared source or item."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "minLength": 1,
                "description": "Optional: only the shares of this project.",
            },
        },
        "additionalProperties": False,
        "examples": [{"project_id": "<project-id>"}],
    },
    response_schema=flat_page("shares", _SHARE_FIELDS),
    group="catalog",
    tags=("read-only", "vcfa", "catalog", "sharing"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call="Call to see which catalog sources or items a project may request.",
        output_shape=(
            "{content: [{id, projectId, contentId, contentType, contentName, sourceName}], "
            "totalElements}."
        ),
        next_step="Change a share with {ns}.catalog.share.set.",
    ),
)

_SHARE_SET = ContentOpSpec(
    name="catalog.share.set",
    handler_attr="vcfa_catalog_share_set",
    path=CATALOG_ENTITLEMENTS_PATH,
    summary="Share a catalog source or item with a project, or stop sharing it.",
    description=(
        "share: POST /catalog/api/admin/entitlements. unshare: DELETE "
        "/catalog/api/admin/entitlements/{id}. Reads the project's shares first; a "
        "share already in that state answers 'unchanged'."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "project_id": PROJECT_ID_FIELD,
            "content_id": {
                "type": "string",
                "minLength": 1,
                "description": "The source id or item id to share.",
            },
            "content_type": {
                "type": "string",
                "enum": ["source", "item"],
                "description": "source shares all items of a source; item shares one item.",
            },
            "action": {
                "type": "string",
                "enum": ["share", "unshare"],
                "description": "share adds the share; unshare removes it.",
            },
        },
        "required": ["project_id", "content_id", "content_type", "action"],
        "additionalProperties": False,
        "examples": [
            {
                "project_id": "<project-id>",
                "content_id": "<source-id>",
                "content_type": "source",
                "action": "share",
            }
        ],
    },
    response_schema=change_answer(
        ["shared", "unshared", "unchanged"],
        "share",
        nullable(obj("The share; null when there is none.", _SHARE_FIELDS)),
    ),
    group="catalog",
    tags=("write", "vcfa", "catalog", "sharing"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to let a project request catalog items, or to stop that.",
        preconditions=(
            "Find the source with {ns}.catalog.source.list. Members of the project can "
            "build machines from what is shared. A second person must approve the call."
        ),
        output_shape="{status: shared | unshared | unchanged, share: {id, projectId, "
        "contentId, contentType}, guidance}.",
        next_step="Check with {ns}.catalog.share.list.",
    ),
)

#: The catalog ops, reads before changes.
CATALOG_OPS: Final[tuple[ContentOpSpec, ...]] = (
    _SOURCE_LIST,
    _SHARE_LIST,
    _SOURCE_CREATE,
    _SHARE_SET,
)
