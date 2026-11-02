# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the vCD organisation ops, and the delete preview builder.

Each ``vcd_org_*_impl`` function is the body a thin shim on
:class:`~meho_backplane.connectors.vcd.connector.VcloudDirectorConnector`
calls. The metadata is in :mod:`.org_ops`.

* ``vcd.org.get`` — ``GET /cloudapi/1.0.0/orgs/{orgUrn}``.
* ``vcd.org.create`` — ``POST /cloudapi/1.0.0/orgs``.
* ``vcd.org.update`` — read the org, change the given fields, then
  ``PUT /cloudapi/1.0.0/orgs/{orgUrn}`` with the whole object (the vendor PUT
  replaces the object, so a field left out would be reset).
* ``vcd.org.delete`` — ``DELETE /cloudapi/1.0.0/orgs/{orgUrn}``, without
  ``force`` or ``recursive``: only a disabled, empty org is deleted.

The writes are never retried: a 5xx on a write must reach the caller, not run
the change twice. Cloud Director answers a long write with ``202 Accepted`` and
the task link in the ``Location`` header; the result passes it on as
``task_href``.

The delete preview builder reads the org before the request waits for
approval. It refuses (raises) when the org does not exist, is enabled, or still
has VDCs, catalogs or vApps, so the request never waits for an approval that
could not succeed. After approval the handler checks the same rules again, and
checks that the org still has the name the approver saw.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vcd._paths import _ORGS_PATH, cloudapi_accept, org_path
from meho_backplane.connectors.vcd.session import VcloudDirectorTargetLike
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.operations._preview import (
    PreviewContext,
    approved_preview,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vcd.connector import VcloudDirectorConnector

__all__ = [
    "VcdOrgDeleteRefusedError",
    "vcd_org_create_impl",
    "vcd_org_delete_impl",
    "vcd_org_get_impl",
    "vcd_org_update_impl",
]

_DELETE_OP_ID = "vcd.org.delete"

#: The org counts that must be 0 before a delete. Cloud Director refuses to
#: delete an org that still holds these unless ``recursive`` is set, and this
#: op never sets it.
_MUST_BE_EMPTY: tuple[tuple[str, str], ...] = (
    ("orgVdcCount", "VDCs"),
    ("catalogCount", "catalogs"),
    ("vappCount", "vApps"),
)

#: Param name -> vendor ``Org`` field, for the fields ``vcd.org.update`` changes.
_UPDATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("display_name", "displayName"),
    ("description", "description"),
    ("is_enabled", "isEnabled"),
)


class VcdOrgDeleteRefusedError(Exception):
    """MEHO refuses to delete the organisation; the message says why and what to do."""


async def _vcd_send(
    connector: VcloudDirectorConnector,
    target: VcloudDirectorTargetLike,
    method: str,
    path: str,
    *,
    operator: Operator,
    body: dict[str, Any] | None = None,
) -> httpx.Response:
    """Send one CloudAPI write (POST / PUT / DELETE) once, and return the response.

    Uses the connector's provider Bearer header and the CloudAPI ``Accept``.
    A non-2xx answer raises :exc:`httpx.HTTPStatusError`, so the dispatcher
    shows the vendor's error text. The call is recorded as a flight-recorder
    vendor span, like the shared HTTP transport does.
    """
    client = await connector._http_client(target)
    headers = {
        **await connector.auth_headers(target, operator),
        "Accept": cloudapi_accept(connector._api_version(target)),
    }
    fr_start = flight_recorder_capture.span_start()
    try:
        resp = await client.request(
            method,
            path,
            json=body,
            headers=headers,
            extensions=connector._request_extensions(target),
        )
    except httpx.TransportError as exc:
        flight_recorder_capture.record_vendor_call_error(fr_start, method=method, exc=exc)
        raise
    flight_recorder_capture.record_vendor_call(
        fr_start,
        method=method,
        request_headers=headers,
        response=resp,
        request_body=body,
        request_content_type="application/json" if body is not None else None,
        response_body=None,
    )
    resp.raise_for_status()
    return resp


def _json_or_empty(resp: httpx.Response) -> dict[str, Any]:
    """The JSON object of *resp*, or ``{}`` for an empty or non-object body."""
    if not resp.content:
        return {}
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


async def _read_org(
    connector: VcloudDirectorConnector,
    operator: Operator,
    target: VcloudDirectorTargetLike,
    org_id: str,
) -> dict[str, Any]:
    """``GET /cloudapi/1.0.0/orgs/{orgUrn}`` — raises on 404 like any vendor error."""
    return await connector._vcd_get(target, org_path(org_id), operator=operator)


async def vcd_org_get_impl(
    connector: VcloudDirectorConnector,
    operator: Operator,
    target: VcloudDirectorTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcd.org.get`` — read one organisation."""
    return await _read_org(connector, operator, target, str(params["org_id"]))


async def vcd_org_create_impl(
    connector: VcloudDirectorConnector,
    operator: Operator,
    target: VcloudDirectorTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcd.org.create`` — ``POST /cloudapi/1.0.0/orgs``; returns the new ``Org``."""
    body: dict[str, Any] = {
        "name": params["name"],
        "displayName": params["display_name"],
        "isEnabled": bool(params.get("is_enabled", True)),
    }
    if "description" in params:
        body["description"] = params["description"]
    resp = await _vcd_send(connector, target, "POST", _ORGS_PATH, operator=operator, body=body)
    return _json_or_empty(resp) or body


async def vcd_org_update_impl(
    connector: VcloudDirectorConnector,
    operator: Operator,
    target: VcloudDirectorTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcd.org.update`` — read, change the given fields, ``PUT`` the whole ``Org``."""
    org_id = str(params["org_id"])
    current = await _read_org(connector, operator, target, org_id)
    changed = dict(current)
    for param, field in _UPDATE_FIELDS:
        if param in params:
            changed[field] = params[param]
    resp = await _vcd_send(
        connector, target, "PUT", org_path(org_id), operator=operator, body=changed
    )
    result = _json_or_empty(resp) or changed
    location = resp.headers.get("location")
    if location:
        result = {**result, "task_href": location}
    return result


def _delete_refusal(org: dict[str, Any]) -> str | None:
    """Why *org* may not be deleted, or ``None`` when it may."""
    label = f"organization {org.get('name')!r} ({org.get('id')})"
    if org.get("isEnabled") is not False:
        return (
            f"{label} is enabled. Disable it first with vcd.org.update "
            "(is_enabled=false), then preview the delete again."
        )
    left = [
        f"{count} {what}"
        for field, what in _MUST_BE_EMPTY
        if isinstance(count := org.get(field), int) and count > 0
    ]
    if left:
        return (
            f"{label} is not empty: it still has {', '.join(left)}. Remove them first; "
            "vcd.org.delete deletes only an empty organization."
        )
    return None


def _blast_radius(org: dict[str, Any]) -> dict[str, Any]:
    """The approval card for deleting *org*: the object, what goes with it, undo."""
    children: list[dict[str, Any]] = []
    users = org.get("userCount")
    if isinstance(users, int) and users > 0:
        children.append({"kind": "user", "count": users})
    disks = org.get("diskCount")
    if isinstance(disks, int) and disks > 0:
        children.append({"kind": "independent_disk", "count": disks})
    return {
        "blast_radius": {
            "object": {
                "kind": "organization",
                "id": org.get("id"),
                "name": org.get("name"),
                "display_name": org.get("displayName"),
                "is_enabled": org.get("isEnabled"),
            },
            "children": children,
            "irreversibility": "permanent",
        }
    }


async def _org_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview builder for ``vcd.org.delete``. Read-only; raises to refuse.

    Declines (``None``) without a live connector or target: the egress-free
    preview path then has no card, and the destructive gate refuses the call.
    """
    # Lazy import: the operations package loads this module at connector import.
    from meho_backplane.connectors.vcd.connector import VcloudDirectorConnector

    connector = ctx.connector_instance
    if not isinstance(connector, VcloudDirectorConnector) or ctx.target is None:
        return None
    org_id = str(ctx.params.get("org_id", ""))
    try:
        org = await _read_org(connector, ctx.operator, ctx.target, org_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):
            raise VcdOrgDeleteRefusedError(
                f"Cloud Director found no organization with id {org_id!r} that this "
                f"login may read (HTTP {exc.response.status_code}). Nothing to delete; "
                "vcd.org.list shows the organizations."
            ) from exc
        raise
    refusal = _delete_refusal(org)
    if refusal is not None:
        raise VcdOrgDeleteRefusedError(refusal)
    return _blast_radius(org)


async def vcd_org_delete_impl(
    connector: VcloudDirectorConnector,
    operator: Operator,
    target: VcloudDirectorTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcd.org.delete`` — check the rules again, then ``DELETE`` the organisation."""
    org_id = str(params["org_id"])
    path = org_path(org_id)
    try:
        org = await _read_org(connector, operator, target, org_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        return {
            "org_id": org_id,
            "name": None,
            "delete_started": False,
            "already_absent": True,
            "task_href": None,
        }
    refusal = _delete_refusal(org)
    if refusal is not None:
        raise VcdOrgDeleteRefusedError(f"Nothing was deleted: {refusal}")
    seen = approved_preview(_DELETE_OP_ID)
    if seen is not None:
        seen_name = (seen.get("blast_radius") or {}).get("object", {}).get("name")
        if seen_name != org.get("name"):
            raise VcdOrgDeleteRefusedError(
                f"Nothing was deleted: the approver saw organization {seen_name!r}, but "
                f"{org_id} is now named {org.get('name')!r}. Preview and ask again."
            )
    resp = await _vcd_send(connector, target, "DELETE", path, operator=operator)
    return {
        "org_id": org_id,
        "name": org.get("name"),
        "delete_started": True,
        "already_absent": False,
        "task_href": resp.headers.get("location"),
    }


register_preview_builder(_DELETE_OP_ID, _org_delete_preview)
