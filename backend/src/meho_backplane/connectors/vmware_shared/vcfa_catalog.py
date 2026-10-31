# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Catalog content sources and sharing on Aria Automation 8 and VCF Automation 9.

Written once for both connectors, like
:mod:`~meho_backplane.connectors.vmware_shared.vcfa_templates`. A content
source imports items into the catalog (here: the released cloud templates of
one project). Sharing (the vendor calls it an *entitlement*) lets a project
request the items of a source, or one item.

Paths come from the vendor's catalog API file
(``vra-catalog-deployment.json`` in ``vmware/vra-sdk-go`` v0.6.5). The
reconcile tests check each ``*_PATH`` constant against that file.

The type id ``com.vmw.blueprint`` and its ``sourceProjectId`` setting, and
the share types ``CatalogSourceIdentifier`` / ``CatalogItemIdentifier``, are
the values the vendor's Terraform provider and API guide use.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_shared.vcfa_templates import (
    LOOKUP_PAGE_SIZE,
    ContentTransport,
    get_json,
    odata_paging,
    page_rows,
    quote_segment,
    send_json,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "BLUEPRINT_SOURCE_TYPE",
    "CATALOG_ENTITLEMENTS_PATH",
    "CATALOG_ENTITLEMENT_PATH",
    "CATALOG_SOURCES_PATH",
    "SHARE_TYPES",
    "catalog_share_list",
    "catalog_share_set",
    "catalog_source_create",
    "catalog_source_list",
]

CATALOG_SOURCES_PATH: Final[str] = "/catalog/api/admin/sources"
CATALOG_ENTITLEMENTS_PATH: Final[str] = "/catalog/api/admin/entitlements"
CATALOG_ENTITLEMENT_PATH: Final[str] = "/catalog/api/admin/entitlements/{id}"

#: The content source type that imports the released cloud templates of a project.
BLUEPRINT_SOURCE_TYPE: Final[str] = "com.vmw.blueprint"

#: Op value -> the vendor's share (``definition.type``) value.
SHARE_TYPES: Final[dict[str, str]] = {
    "source": "CatalogSourceIdentifier",
    "item": "CatalogItemIdentifier",
}

_SOURCE_KEYS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "description",
    "typeId",
    "projectId",
    "global",
    "itemsFound",
    "itemsImported",
    "lastImportStartedAt",
    "lastImportCompletedAt",
)


def source_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One flat content source row."""
    config = row.get("config")
    errors = row.get("lastImportErrors")
    return {key: row.get(key) for key in _SOURCE_KEYS} | {
        "sourceProjectId": config.get("sourceProjectId") if isinstance(config, Mapping) else None,
        "lastImportErrors": errors if isinstance(errors, list) else [],
    }


def share_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One flat sharing row: the project and the shared source or item."""
    definition = row.get("definition")
    content = definition if isinstance(definition, Mapping) else {}
    return {
        "id": row.get("id"),
        "projectId": row.get("projectId"),
        "contentId": content.get("id"),
        "contentType": content.get("type"),
        "contentName": content.get("name"),
        "sourceName": content.get("sourceName"),
        "sourceType": content.get("sourceType"),
    }


# ---------------------------------------------------------------------------
# Content sources
# ---------------------------------------------------------------------------


async def catalog_source_list(
    connector: ContentTransport, operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """``*.catalog.source.list``: the content sources, as flat rows."""
    query = odata_paging(params)
    if params.get("project_id"):
        query["projectId"] = params["project_id"]
    if params.get("search"):
        query["search"] = params["search"]
    payload = await get_json(connector, operator, target, CATALOG_SOURCES_PATH, query or None)
    rows = [source_row(row) for row in page_rows(payload)]
    total = payload.get("totalElements") if isinstance(payload, Mapping) else None
    return {"content": rows, "totalElements": total if isinstance(total, int) else len(rows)}


async def catalog_source_create(
    connector: ContentTransport, operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """``*.catalog.source.create``: a source for the cloud templates of a project.

    Safe to run twice: a source with exactly this name answers ``unchanged``.
    """
    name = str(params["name"])
    payload = await get_json(
        connector,
        operator,
        target,
        CATALOG_SOURCES_PATH,
        {"search": name, "$top": LOOKUP_PAGE_SIZE},
    )
    existing = next((row for row in page_rows(payload) if row.get("name") == name), None)
    if existing is not None:
        return {
            "status": "unchanged",
            "source": source_row(existing),
            "guidance": f"a content source named {name!r} already exists; nothing was created",
        }
    body: dict[str, Any] = {
        "name": name,
        "typeId": BLUEPRINT_SOURCE_TYPE,
        "config": {"sourceProjectId": params["source_project_id"]},
    }
    if params.get("description") is not None:
        body["description"] = params["description"]
    created = await send_json(connector, operator, target, CATALOG_SOURCES_PATH, body=body)
    return {
        "status": "created",
        "source": source_row(created),
        "guidance": (
            "the source imports the released templates of the project; share it with a "
            "project so its members can request the items"
        ),
    }


# ---------------------------------------------------------------------------
# Sharing (entitlements)
# ---------------------------------------------------------------------------


async def _shares(
    connector: ContentTransport, operator: Operator, target: Any, project_id: str | None
) -> list[dict[str, Any]]:
    query = {"projectId": project_id} if project_id else None
    payload = await get_json(connector, operator, target, CATALOG_ENTITLEMENTS_PATH, query)
    return page_rows(payload)


async def catalog_share_list(
    connector: ContentTransport, operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """``*.catalog.share.list``: what is shared with which project, as flat rows."""
    rows = [
        share_row(row)
        for row in await _shares(connector, operator, target, params.get("project_id"))
    ]
    return {"content": rows, "totalElements": len(rows)}


async def catalog_share_set(
    connector: ContentTransport, operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """``*.catalog.share.set``: share a source or item with a project, or stop.

    Reads the project's sharing first, so both actions are safe to run twice.
    """
    project_id = str(params["project_id"])
    content_id = str(params["content_id"])
    rows = await _shares(connector, operator, target, project_id)
    existing = next(
        (
            row
            for row in rows
            if row.get("projectId") == project_id
            and isinstance(row.get("definition"), Mapping)
            and row["definition"].get("id") == content_id
        ),
        None,
    )
    if params["action"] == "unshare":
        if existing is None:
            return _share_answer("unchanged", None, "the project has no such share")
        path = CATALOG_ENTITLEMENT_PATH.format(id=quote_segment(existing["id"]))
        await send_json(connector, operator, target, path, verb="DELETE")
        return _share_answer(
            "unshared", existing, "members of the project can no longer request it"
        )
    if existing is not None:
        return _share_answer("unchanged", existing, "the project already has this share")
    body = {
        "projectId": project_id,
        "definition": {"id": content_id, "type": SHARE_TYPES[params["content_type"]]},
    }
    created = await send_json(connector, operator, target, CATALOG_ENTITLEMENTS_PATH, body=body)
    return _share_answer("shared", created, "members of the project can request it now")


def _share_answer(status: str, row: Mapping[str, Any] | None, guidance: str) -> dict[str, Any]:
    return {
        "status": status,
        "share": share_row(row) if row is not None else None,
        "guidance": guidance,
    }
