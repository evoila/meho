# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cloud templates (blueprints) on Aria Automation 8 and VCF Automation 9.

VCF Automation 9 ("VM Apps" organisations) serves the same blueprint API as
Aria Automation 8. So each handler here is written once and runs on both
connectors: ``vcfa-rest`` (op ids ``vcfa.tenant.*``) and ``vcfa-vra8`` (op ids
``vcfa.vra8.*``). Both connectors inherit the handlers through
:class:`~meho_backplane.connectors.vmware_shared.vcfa_content.VcfaContentOpsMixin`.

The handlers only use the two request methods both connectors have:
``_request_json`` (a ``GET``) and ``_post_json`` (a ``POST``, ``PUT`` or
``DELETE``). Each connector adds its own login.

Paths come from the vendor's blueprint API file (``vra-blueprint.json`` in
``vmware/vra-sdk-go`` v0.6.5). The reconcile tests check each ``*_PATH``
constant against that file.

A vendor error (4xx / 5xx) raises :exc:`httpx.HTTPStatusError`. The
dispatcher turns it into ``connector_error`` with the vendor's text. The
one exception is a ``404`` on the delete: the template is already gone, so
the op answers ``unchanged``.

``vcfa.*.blueprint.delete`` is ``destructive``, so it has a preview builder
(registered at import). It reads the template and its versions and fills
the ``blast_radius`` block. It raises when the template does not exist, so
MEHO refuses to queue the delete.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import quote

import httpx

from meho_backplane.connectors.vmware_shared.vcfa_yaml_secrets import (
    MARKER_PREFIX,
    scrub_template_yaml,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "BLUEPRINTS_PATH",
    "BLUEPRINT_INPUTS_SCHEMA_PATH",
    "BLUEPRINT_PATH",
    "BLUEPRINT_VALIDATION_PATH",
    "BLUEPRINT_VERSIONS_PATH",
    "BLUEPRINT_VERSION_RELEASE_PATH",
    "BLUEPRINT_VERSION_UNRELEASE_PATH",
    "DELETE_OP_IDS",
    "ContentTransport",
    "blueprint_create",
    "blueprint_delete",
    "blueprint_get",
    "blueprint_update",
    "blueprint_validate",
    "blueprint_version_list",
    "blueprint_version_release",
    "quote_segment",
]

BLUEPRINTS_PATH: Final[str] = "/blueprint/api/blueprints"
BLUEPRINT_PATH: Final[str] = "/blueprint/api/blueprints/{blueprintId}"
BLUEPRINT_INPUTS_SCHEMA_PATH: Final[str] = "/blueprint/api/blueprints/{blueprintId}/inputs-schema"
BLUEPRINT_VERSIONS_PATH: Final[str] = "/blueprint/api/blueprints/{blueprintId}/versions"
BLUEPRINT_VERSION_RELEASE_PATH: Final[str] = (
    "/blueprint/api/blueprints/{blueprintId}/versions/{version}/actions/release"
)
BLUEPRINT_VERSION_UNRELEASE_PATH: Final[str] = (
    "/blueprint/api/blueprints/{blueprintId}/versions/{version}/actions/unrelease"
)
BLUEPRINT_VALIDATION_PATH: Final[str] = "/blueprint/api/blueprint-validation"

#: The delete op id on each connector. Both get the same preview builder.
DELETE_OP_IDS: Final[tuple[str, ...]] = (
    "vcfa.tenant.blueprint.delete",
    "vcfa.vra8.blueprint.delete",
)

#: Page size for the lookups that find one row by name or version.
LOOKUP_PAGE_SIZE: Final[int] = 200

JSON_ACCEPT: Final[dict[str, str]] = {"Accept": "application/json"}

#: The template fields an answer shows (not the YAML, which can be long).
_SUMMARY_KEYS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "description",
    "projectId",
    "projectName",
    "status",
    "valid",
    "totalVersions",
    "totalReleasedVersions",
    "requestScopeOrg",
    "createdAt",
    "updatedAt",
)
_VERSION_KEYS: Final[tuple[str, ...]] = (
    "id",
    "version",
    "status",
    "versionDescription",
    "versionChangeLog",
    "createdAt",
    "createdBy",
)


class ContentTransport(Protocol):
    """The request methods both Automation connectors have.

    ``_content_get`` is a ``GET`` whose answer may be a JSON list (the share
    list is one). :class:`~.vcfa_content.VcfaContentOpsMixin` has a default
    that uses ``_request_json``; the VCF Automation connector, whose
    ``_request_json`` refuses a list, has its own.
    """

    async def _content_get(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any: ...

    async def _request_json(
        self,
        target: Any,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: Any = ...,
    ) -> dict[str, Any]: ...


def quote_segment(value: Any) -> str:
    """Percent-encode one path segment, so an id cannot leave its segment."""
    return quote(str(value), safe="")


async def get_json(
    connector: ContentTransport,
    operator: Operator,
    target: Any,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """``GET`` with ``Accept: application/json``. May return a list."""
    return await connector._content_get(target, path, operator=operator, params=params)


async def send_json(
    connector: ContentTransport,
    operator: Operator,
    target: Any,
    path: str,
    *,
    verb: str = "POST",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A ``POST`` / ``PUT`` / ``DELETE`` with ``Accept: application/json``."""
    return await connector._post_json(
        target, path, operator=operator, verb=verb, json=body, extra_headers=dict(JSON_ACCEPT)
    )


def page_rows(payload: Any) -> list[dict[str, Any]]:
    """The rows of a ``{content: [...]}`` page, or of a bare list."""
    raw = payload.get("content") if isinstance(payload, Mapping) else payload
    return [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []


def odata_paging(params: Mapping[str, Any]) -> dict[str, Any]:
    """The caller's ``$top`` / ``$skip``, when set."""
    return {key: params[key] for key in ("$top", "$skip") if params.get(key) is not None}


def summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """The main fields of a template, without its YAML."""
    return {key: row.get(key) for key in _SUMMARY_KEYS} | {
        "validationMessages": row.get("validationMessages") or []
    }


def version_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One flat version row, without its YAML."""
    return {key: row.get(key) for key in _VERSION_KEYS}


def _blueprint_path(blueprint_id: Any) -> str:
    return BLUEPRINT_PATH.format(blueprintId=quote_segment(blueprint_id))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def blueprint_get(
    connector: ContentTransport, operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """``*.blueprint.get``: the template with its YAML, and its inputs schema."""
    blueprint_id = quote_segment(params["id"])
    row = await get_json(connector, operator, target, _blueprint_path(params["id"]))
    inputs = await get_json(
        connector,
        operator,
        target,
        BLUEPRINT_INPUTS_SCHEMA_PATH.format(blueprintId=blueprint_id),
    )
    content = row.get("content")
    replaced = 0
    if isinstance(content, str):
        content, replaced = scrub_template_yaml(content)
    return {
        "blueprint": {**summary(row), "content": content},
        "inputs_schema": inputs if isinstance(inputs, dict) else {},
        "secrets_replaced": replaced,
    }


async def blueprint_version_list(
    connector: ContentTransport, operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """``*.blueprint.version.list``: the versions of a template, as flat rows."""
    query = odata_paging(params)
    if params.get("status"):
        query["status"] = params["status"]
    path = BLUEPRINT_VERSIONS_PATH.format(blueprintId=quote_segment(params["id"]))
    payload = await get_json(connector, operator, target, path, query or None)
    rows = [version_row(row) for row in page_rows(payload)]
    total = payload.get("totalElements") if isinstance(payload, Mapping) else None
    return {"content": rows, "totalElements": total if isinstance(total, int) else len(rows)}


async def blueprint_validate(
    connector: ContentTransport, operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """``*.blueprint.validate``: check template YAML or a stored template. Changes nothing."""
    fields = {
        "project_id": "projectId",
        "content": "content",
        "blueprint_id": "blueprintId",
        "blueprint_version": "blueprintVersion",
        "inputs": "inputs",
    }
    body = {wire: params[name] for name, wire in fields.items() if params.get(name) is not None}
    answer = await send_json(connector, operator, target, BLUEPRINT_VALIDATION_PATH, body=body)
    return {
        "valid": answer.get("valid"),
        "validationMessages": answer.get("validationMessages") or [],
    }


# ---------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------


def refuse_hidden_values(params: Mapping[str, Any]) -> None:
    """Refuse template YAML that holds a marker MEHO put in place of a value.

    A template read hides secret values behind ``[REDACTED:...]``. Writing
    that YAML back would put the marker into the template in place of the
    real value, so create and update stop before any vendor call.
    """
    content = params.get("content")
    if isinstance(content, str) and MARKER_PREFIX in content:
        raise ValueError(
            "the template YAML holds a hidden value ([REDACTED:...]); MEHO does not write "
            "it back. Give the full value, or use a secret input (${secret.<name>}) in the "
            "template"
        )


async def _find_by_name(
    connector: ContentTransport, operator: Operator, target: Any, name: str, project_id: str
) -> dict[str, Any] | None:
    """The template with exactly this name in this project, or ``None``."""
    payload = await get_json(
        connector,
        operator,
        target,
        BLUEPRINTS_PATH,
        {"name": name, "projects": project_id, "$top": LOOKUP_PAGE_SIZE},
    )
    for row in page_rows(payload):
        if row.get("name") == name and row.get("projectId") == project_id:
            return row
    return None


async def blueprint_create(
    connector: ContentTransport, operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """``*.blueprint.create``: a new draft template. Safe to run twice."""
    refuse_hidden_values(params)
    name = str(params["name"])
    project_id = str(params["project_id"])
    existing = await _find_by_name(connector, operator, target, name, project_id)
    if existing is not None:
        return {
            "status": "unchanged",
            "blueprint": summary(existing),
            "guidance": (
                f"the project already has a template named {name!r}; nothing was created. "
                "Change it with the blueprint.update op of this connector"
            ),
        }
    body: dict[str, Any] = {
        "name": name,
        "projectId": project_id,
        "content": params["content"],
        "requestScopeOrg": bool(params.get("request_scope_org", False)),
    }
    if params.get("description") is not None:
        body["description"] = params["description"]
    created = await send_json(connector, operator, target, BLUEPRINTS_PATH, body=body)
    return {
        "status": "created",
        "blueprint": summary(created),
        "guidance": "the template is a draft; release a version to put it in the catalog",
    }


#: Op param -> template field, for the fields an update may change.
_UPDATE_FIELDS: Final[dict[str, str]] = {
    "name": "name",
    "description": "description",
    "content": "content",
    "request_scope_org": "requestScopeOrg",
}


async def blueprint_update(
    connector: ContentTransport, operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """``*.blueprint.update``: change the draft YAML, name or description.

    Reads the template first and sends the whole template back with the
    changes (``PUT`` replaces it). When nothing differs, nothing is sent.
    """
    refuse_hidden_values(params)
    path = _blueprint_path(params["id"])
    current = await get_json(connector, operator, target, path)
    changes = {
        wire: params[name]
        for name, wire in _UPDATE_FIELDS.items()
        if name in params and params[name] != current.get(wire)
    }
    if not changes:
        return {
            "status": "unchanged",
            "blueprint": summary(current),
            "changed_fields": [],
            "guidance": "the template already has these values; nothing was written",
        }
    body = {
        "name": current.get("name"),
        "description": current.get("description"),
        "projectId": current.get("projectId"),
        "content": current.get("content"),
        "requestScopeOrg": current.get("requestScopeOrg"),
    } | changes
    updated = await send_json(connector, operator, target, path, verb="PUT", body=body)
    return {
        "status": "updated",
        "blueprint": summary(updated),
        "changed_fields": sorted(changes),
        "guidance": (
            "this changes the draft only; released versions stay as they are. "
            "Release a new version to put the change in the catalog"
        ),
    }


async def blueprint_delete(
    connector: ContentTransport, operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """``*.blueprint.delete``: delete a template and all its versions."""
    try:
        await send_json(connector, operator, target, _blueprint_path(params["id"]), verb="DELETE")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        return {
            "status": "unchanged",
            "id": params["id"],
            "guidance": "no template has this id; nothing was deleted",
        }
    return {
        "status": "deleted",
        "id": params["id"],
        "guidance": "the template and its versions are gone; its catalog items go too",
    }


async def _find_version(
    connector: ContentTransport, operator: Operator, target: Any, blueprint_id: Any, version: str
) -> dict[str, Any] | None:
    path = BLUEPRINT_VERSIONS_PATH.format(blueprintId=quote_segment(blueprint_id))
    payload = await get_json(
        connector, operator, target, path, {"version": version, "$top": LOOKUP_PAGE_SIZE}
    )
    return next((row for row in page_rows(payload) if row.get("version") == version), None)


async def blueprint_version_release(
    connector: ContentTransport, operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """``*.blueprint.version.release``: make and release a version, or unrelease one.

    ``release``: a new version is made from the draft and released in one
    call; a version that exists is released; a released one answers
    ``unchanged``. ``unrelease``: a released version leaves the catalog; any
    other version answers ``unchanged``.
    """
    blueprint_id = params["id"]
    version = str(params["version"])
    found = await _find_version(connector, operator, target, blueprint_id, version)
    segments = {"blueprintId": quote_segment(blueprint_id), "version": quote_segment(version)}
    if params["action"] == "unrelease":
        if found is None or found.get("status") != "RELEASED":
            return _release_answer("unchanged", found, f"version {version!r} is not released")
        row = await send_json(
            connector, operator, target, BLUEPRINT_VERSION_UNRELEASE_PATH.format(**segments)
        )
        return _release_answer("unreleased", row, "the version left the catalog")
    if found is None:
        body: dict[str, Any] = {"version": version, "release": True}
        if params.get("description") is not None:
            body["description"] = params["description"]
        if params.get("change_log") is not None:
            body["changeLog"] = params["change_log"]
        path = BLUEPRINT_VERSIONS_PATH.format(blueprintId=segments["blueprintId"])
        row = await send_json(connector, operator, target, path, body=body)
        return _release_answer("created_and_released", row, _CATALOG_NOTE)
    if found.get("status") == "RELEASED":
        return _release_answer("unchanged", found, f"version {version!r} is already released")
    row = await send_json(
        connector, operator, target, BLUEPRINT_VERSION_RELEASE_PATH.format(**segments)
    )
    return _release_answer("released", row, _CATALOG_NOTE)


_CATALOG_NOTE: Final[str] = (
    "a released version reaches the catalog once a content source holds the "
    "templates of its project and that source is shared with a project"
)


def _release_answer(status: str, row: Mapping[str, Any] | None, guidance: str) -> dict[str, Any]:
    return {
        "status": status,
        "version": version_row(row) if row is not None else None,
        "guidance": guidance,
    }


# ---------------------------------------------------------------------------
# Preview for the delete (destructive)
# ---------------------------------------------------------------------------


async def blueprint_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The ``blast_radius`` of a template delete: the template and its versions.

    Returns ``None`` without a connector (the ``preview_operation`` call does
    not reach the vendor). Raises :exc:`LookupError` when the template does
    not exist: MEHO then refuses to queue the delete.
    """
    connector: Any = ctx.connector_instance
    blueprint_id = ctx.params.get("id")
    if connector is None or ctx.target is None or not isinstance(blueprint_id, str):
        return None
    try:
        row = await get_json(connector, ctx.operator, ctx.target, _blueprint_path(blueprint_id))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise LookupError(f"no cloud template has the id {blueprint_id!r}") from exc
        raise
    versions = await blueprint_version_list(
        connector, ctx.operator, ctx.target, {"id": blueprint_id, "$top": LOOKUP_PAGE_SIZE}
    )
    children = [
        {"kind": "cloud_template_version", "version": v["version"], "status": v["status"]}
        for v in versions["content"]
    ]
    released = sum(1 for child in children if child["status"] == "RELEASED")
    return {
        "blast_radius": {
            "object": {
                "kind": "cloud_template",
                "id": blueprint_id,
                "name": row.get("name"),
                "project_id": row.get("projectId"),
                "status": row.get("status"),
            },
            "children": children,
            "irreversibility": "irreversible",
            "note": (
                "the template YAML and all its versions are deleted, and "
                f"{released} released version(s) leave the catalog. Only a new template "
                "made from a saved copy of the YAML can replace it"
            ),
            "match_count": 1,
        }
    }


for _op_id in DELETE_OP_IDS:
    register_preview_builder(_op_id, blueprint_delete_preview)
