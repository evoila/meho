# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Day-2 handlers shared by Aria Automation 8 and VCF Automation 9.

Both products serve the same deployment API (``/deployment/api``) and IaaS
API (``/iaas/api``), so each handler here is written once and runs on both
connectors: :class:`~meho_backplane.connectors.vcf_automation.connector.VcfAutomationConnector`
(op ids ``vcfa.tenant.*``) and :class:`~meho_backplane.connectors.vra8.connector.Vra8Connector`
(op ids ``vcfa.vra8.*``). A handler only needs the connector's
``_request_json`` (GET) and ``_post_json`` (POST, DELETE) methods; both
connectors add their own login. ``prefix`` is the op id prefix of the
calling connector, so the guidance names that connector's ops.

Metadata lives in :mod:`.day2_ops`. The machine and disk creates are in
:mod:`._day2_create`. An upstream 4xx/5xx raises
:exc:`httpx.HTTPStatusError`; the dispatcher maps it to ``connector_error``
with ``http_status`` and ``upstream_message``, so the vendor's error text
reaches the answer.

This module also registers the park-time preview of the two deployment
deletes (``vcfa.tenant.deployment.delete`` and ``vcfa.vra8.deployment.delete``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from meho_backplane.connectors.vcf_automation._day2_paths import (
    BLOCK_DEVICES_PATH,
    DEPLOYMENT_ACTIONS_PATH,
    DEPLOYMENT_REQUESTS_PATH,
    MACHINES_PATH,
    RESOURCE_ACTIONS_PATH,
    RESOURCE_REQUESTS_PATH,
)
from meho_backplane.connectors.vcf_automation._lookups import quote_segment
from meho_backplane.connectors.vcf_automation._paths import (
    TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH,
    TENANT_IAAS_API_VERSION,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__ = [
    "DEPLOYMENT_ACTION_IDS",
    "FOLLOW_UPS",
    "LEASE_ACTION_ID",
    "LEASE_INPUT",
    "LIST_ROW_LIMIT",
    "RESOURCE_ACTION_IDS",
    "Day2FollowUp",
    "IncompleteListError",
    "block_device_list",
    "deployment_action_run",
    "deployment_actions_list",
    "deployment_delete",
    "deployment_delete_preview",
    "deployment_lease_change",
    "follow_up",
    "machine_list",
    "resource_action_run",
    "row_view",
    "tracker_view",
]

#: The built-in deployment actions an agent may run. Delete and custom
#: actions (ABX actions or workflows, which can do anything) are not here;
#: a delete goes through the delete op.
DEPLOYMENT_ACTION_IDS: Final[tuple[str, ...]] = (
    "Deployment.ChangeOwner",
    "Deployment.ChangeLease",
    "Deployment.EditTags",
    "Deployment.PowerOn",
    "Deployment.PowerOff",
)
#: The built-in vSphere machine actions an agent may run. Delete,
#: remove-disk, delete-snapshot and custom actions are not here.
RESOURCE_ACTION_IDS: Final[tuple[str, ...]] = (
    "Cloud.vSphere.Machine.PowerOn",
    "Cloud.vSphere.Machine.PowerOff",
    "Cloud.vSphere.Machine.Reboot",
    "Cloud.vSphere.Machine.Resize",
    "Cloud.vSphere.Machine.Add.Disk",
    "Cloud.vSphere.Machine.Snapshot.Create",
)
LEASE_ACTION_ID: Final[str] = "Deployment.ChangeLease"
#: The input name of the lease action (the vendor's API guide uses it).
LEASE_INPUT: Final[str] = "Lease Expiration Date"

_API_VERSION: Final[dict[str, str]] = {"apiVersion": TENANT_IAAS_API_VERSION}
#: Page size and page limit of the machine and disk lists. A list with more
#: rows than :data:`LIST_ROW_LIMIT` is refused (:class:`IncompleteListError`).
_PAGE_SIZE: Final[int] = 200
_MAX_PAGES: Final[int] = 50
LIST_ROW_LIMIT: Final[int] = _PAGE_SIZE * _MAX_PAGES

_MACHINE_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "projectId",
    "deploymentId",
    "powerState",
    "address",
    "hostname",
    "externalZoneId",
    "externalRegionId",
    "provisioningStatus",
    "createdAt",
)
_DISK_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "capacityInGB",
    "type",
    "status",
    "persistent",
    "projectId",
    "deploymentId",
    "externalZoneId",
    "provisioningStatus",
    "createdAt",
)
_ACTION_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "displayName",
    "description",
    "actionType",
    "valid",
    "schema",
)
_REQUEST_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "actionId",
    "status",
    "deploymentId",
    "resourceIds",
    "requestedBy",
    "createdAt",
)
_REQUEST_STATES: Final[str] = "SUCCESSFUL, FAILED, ABORTED or APPROVAL_REJECTED"
#: When an agent stops reading for a request that does not end.
_WAIT_LIMIT: Final[str] = (
    "Most requests end within a few minutes. Stop after 10 minutes and report the request id."
)


@dataclass(frozen=True)
class Day2FollowUp:
    """How an agent follows a day-2 request on one connector, with the reads it has.

    The two connectors read a deployment from different APIs. The vRA 8 read
    (``vcfa.vra8.deployment.get``, ``/deployment/api``) shows ``lastRequest``
    and ``leaseExpireAt``. The VCF Automation read (``vcfa.tenant.deployment.get``,
    ``/iaas/api``) shows neither, and MEHO has no read of a request there yet.
    So the guidance of the action and lease ops is written per connector and
    names only a read that shows the field.
    """

    request_end: str
    """How to see that the request ended, and when to stop reading."""
    lease_end: str
    """How to read the lease end, or that MEHO cannot read it."""


FOLLOW_UPS: Final[dict[str, Day2FollowUp]] = {
    "vcfa.tenant": Day2FollowUp(
        request_end=(
            "MEHO has no read of a VCF Automation request yet, and vcfa.tenant.deployment.get "
            "(the IaaS read) does not show it. Wait a minute, then read vcfa.tenant.machine.list "
            "with $filter \"deploymentId eq '<deployment-id>'\" to see the new power state of "
            f"the machines. {_WAIT_LIMIT} A person can check the request in the vendor UI."
        ),
        lease_end=(
            "MEHO cannot read the lease end of a VCF Automation deployment yet: "
            "vcfa.tenant.deployment.get reads the IaaS API, which has no lease field."
        ),
    ),
    "vcfa.vra8": Day2FollowUp(
        request_end=(
            "Read vcfa.vra8.deployment.get with deployment_id until lastRequest.status is "
            f"{_REQUEST_STATES}; inprogressRequests is then empty. {_WAIT_LIMIT}"
        ),
        lease_end="vcfa.vra8.deployment.get shows the lease end as leaseExpireAt.",
    ),
}


def follow_up(prefix: str) -> Day2FollowUp:
    """The follow-up texts of the connector whose op ids start with *prefix*."""
    return FOLLOW_UPS[prefix]


class IncompleteListError(RuntimeError):
    """An IaaS list did not end within :data:`_MAX_PAGES` pages.

    The rows read so far are not returned: a list that stops without a word
    looks complete, and ``result_query`` cannot bring the missing rows back.
    ``remediation`` reaches the answer as ``extras.remediation``.
    """

    def __init__(self, path: str, rows: int, total: Any) -> None:
        of_total = f" of {total}" if isinstance(total, int) else ""
        super().__init__(
            f"{path} has more rows than this op reads ({rows}{of_total} read in {_MAX_PAGES} "
            "pages); the list would be incomplete, so none of it is returned"
        )
        self.remediation = (
            "Narrow the list with $filter, for example \"deploymentId eq '<deployment-id>'\" "
            f"or \"projectId eq '<project-id>'\", so it has at most {LIST_ROW_LIMIT:,} rows."
        )


def row_view(row: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: row.get(key) for key in keys}


def _linked_ids(row: Mapping[str, Any], prefix: str) -> list[str]:
    """The ids of the objects ``_links`` points to under *prefix* (an IaaS path)."""
    found: list[str] = []
    for link in (row.get("_links") or {}).values():
        if not isinstance(link, Mapping):
            continue
        hrefs = [link.get("href"), *(link.get("hrefs") or [])]
        for href in hrefs:
            if isinstance(href, str) and href.startswith(prefix):
                tail = href[len(prefix) :].split("/", 1)[0]
                if tail and tail not in found:
                    found.append(tail)
    return found


def tracker_view(tracker: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": tracker.get("id"),
        "name": tracker.get("name"),
        "status": tracker.get("status"),
        "progress": tracker.get("progress"),
        "message": tracker.get("message"),
        "resources": list(tracker.get("resources") or []),
    }


def _action_body(params: Mapping[str, Any], action_id: str, inputs: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"actionId": action_id, "inputs": dict(inputs or {})}
    if params.get("reason"):
        body["reason"] = params["reason"]
    return body


def _request_answer(request: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    return {
        "status": "submitted",
        "request": row_view(request, _REQUEST_FIELDS),
        "guidance": f"The action runs in the background. {follow_up(prefix).request_end}",
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _get_json(connector: Any, operator: Any, target: Any, path: str) -> Any:
    """GET *path* and return the JSON body, also when it is a list.

    The action lists answer a JSON array. The VCF Automation connector's
    ``_request_json`` accepts only an object, so this reads the raw response
    there (``_send_with_retry``, same login and 401 retry). The vRA 8
    connector's ``_request_json`` passes an array through.
    """
    send = getattr(connector, "_send_with_retry", None)
    if send is None:
        return await connector._request_json(target, "GET", path, operator=operator)
    response = await send(target, "GET", path, operator=operator)
    return response.json() if response.content else []


async def deployment_actions_list(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """The day-2 actions of a deployment, or of one of its resources.

    ``GET /deployment/api/deployments/{deploymentId}/actions`` or
    ``GET /deployment/api/resources/{resourceId}/actions``. The schema asks
    for exactly one of ``deployment_id`` and ``resource_id``.
    """
    del prefix
    if params.get("resource_id"):
        path = RESOURCE_ACTIONS_PATH.format(resourceId=quote_segment(params["resource_id"]))
    else:
        path = DEPLOYMENT_ACTIONS_PATH.format(deploymentId=quote_segment(params["deployment_id"]))
    rows = await _get_json(connector, operator, target, path)
    if isinstance(rows, Mapping):
        rows = rows.get("content") or []
    return {"items": [row_view(row, _ACTION_FIELDS) for row in rows if isinstance(row, Mapping)]}


async def _iaas_list(
    connector: Any, operator: Any, target: Any, path: str, params: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Every row of an IaaS list, page by page.

    Raises :class:`IncompleteListError` when the list has not ended after
    ``_MAX_PAGES`` full pages, instead of returning a part of it.
    """
    query: dict[str, Any] = {**_API_VERSION, "$top": _PAGE_SIZE}
    if params.get("$filter"):
        query["$filter"] = params["$filter"]
    rows: list[dict[str, Any]] = []
    total: Any = None
    for page in range(_MAX_PAGES):
        answer = await connector._request_json(
            target, "GET", path, operator=operator, params={**query, "$skip": page * _PAGE_SIZE}
        )
        content = [row for row in answer.get("content") or [] if isinstance(row, Mapping)]
        rows.extend(dict(row) for row in content)
        total = answer.get("totalElements")
        if len(content) < _PAGE_SIZE or (isinstance(total, int) and len(rows) >= total):
            return rows
    raise IncompleteListError(path, len(rows), total)


async def machine_list(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``GET /iaas/api/machines`` -- every machine, as a flat list of rows."""
    del prefix
    rows = await _iaas_list(connector, operator, target, MACHINES_PATH, params)
    return {
        "items": [
            {
                **row_view(row, _MACHINE_FIELDS),
                "zoneIds": _linked_ids(row, "/iaas/api/zones/"),
                "diskIds": _linked_ids(row, f"{BLOCK_DEVICES_PATH}/"),
            }
            for row in rows
        ]
    }


async def block_device_list(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``GET /iaas/api/block-devices`` -- every disk with its machines, as a flat list."""
    del prefix
    rows = await _iaas_list(connector, operator, target, BLOCK_DEVICES_PATH, params)
    return {
        "items": [
            {**row_view(row, _DISK_FIELDS), "machineIds": _linked_ids(row, f"{MACHINES_PATH}/")}
            for row in rows
        ]
    }


# ---------------------------------------------------------------------------
# Day-2 actions
# ---------------------------------------------------------------------------


async def deployment_action_run(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``POST /deployment/api/deployments/{deploymentId}/requests`` with a built-in action.

    The schema allows only :data:`DEPLOYMENT_ACTION_IDS`; the check here
    stops a call that skipped the schema (defence in depth).
    """
    action_id = params["action_id"]
    if action_id not in DEPLOYMENT_ACTION_IDS:
        raise ValueError(f"action {action_id!r} is not an allowed deployment action")
    path = DEPLOYMENT_REQUESTS_PATH.format(deploymentId=quote_segment(params["deployment_id"]))
    request = await connector._post_json(
        target,
        path,
        operator=operator,
        json=_action_body(params, action_id, params.get("inputs")),
    )
    return _request_answer(request, prefix)


async def resource_action_run(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``POST /deployment/api/resources/{resourceId}/requests`` with a built-in machine action."""
    action_id = params["action_id"]
    if action_id not in RESOURCE_ACTION_IDS:
        raise ValueError(f"action {action_id!r} is not an allowed machine action")
    path = RESOURCE_REQUESTS_PATH.format(resourceId=quote_segment(params["resource_id"]))
    request = await connector._post_json(
        target,
        path,
        operator=operator,
        json=_action_body(params, action_id, params.get("inputs")),
    )
    return _request_answer(request, prefix)


async def deployment_lease_change(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``Deployment.ChangeLease`` on one deployment, with the new lease end."""
    path = DEPLOYMENT_REQUESTS_PATH.format(deploymentId=quote_segment(params["deployment_id"]))
    inputs = {LEASE_INPUT: params["lease_expiration_date"]}
    request = await connector._post_json(
        target, path, operator=operator, json=_action_body(params, LEASE_ACTION_ID, inputs)
    )
    return _request_answer(request, prefix)


# ---------------------------------------------------------------------------
# Delete a deployment
# ---------------------------------------------------------------------------


async def deployment_delete(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``DELETE /deployment/api/deployments/{deploymentId}``.

    Starts the delete of the deployment and all its resources. A ``404`` (no
    such deployment) answers ``unchanged``, so a second run is safe.
    """
    path = TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH.format(deploymentId=quote_segment(params["id"]))
    try:
        request = await connector._post_json(target, path, operator=operator, verb="DELETE")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        return {
            "status": "unchanged",
            "request": None,
            "guidance": "no deployment has this id; nothing was deleted",
        }
    return {
        "status": "submitted",
        "request": {key: request.get(key) for key in ("id", "name", "status", "deploymentId")},
        "guidance": (
            f"the delete runs in the background; read {prefix}.deployment.get with the "
            "same id until it answers not found"
        ),
    }


async def deployment_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time preview of a deployment delete, with the blast-radius block.

    Reads ``GET /deployment/api/deployments/{deploymentId}?expand=resources``
    so the approver sees the deployment and every resource that goes with
    it. A deployment that does not exist (``404``) raises, so the delete is
    refused before it waits for approval. Without a connector (the
    ``preview_operation`` call) it only names the id.
    """
    deployment_id = ctx.params.get("id")
    if not isinstance(deployment_id, str) or not deployment_id:
        return None
    preview: dict[str, Any] = {"action": "delete_deployment", "id": deployment_id}
    connector: Any = ctx.connector_instance
    if connector is None:
        return preview
    path = TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH.format(deploymentId=quote_segment(deployment_id))
    try:
        row = await connector._request_json(
            ctx.target, "GET", path, operator=ctx.operator, params={"expand": "resources"}
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise LookupError(f"no deployment has the id {deployment_id!r}") from exc
        raise
    resources = [r for r in row.get("resources") or [] if isinstance(r, Mapping)]
    return {
        **preview,
        "name": row.get("name"),
        "project_id": row.get("projectId"),
        "status": row.get("status"),
        "blast_radius": {
            "object": {
                "kind": "deployment",
                "id": row.get("id") or deployment_id,
                "name": row.get("name"),
                "project_id": row.get("projectId"),
            },
            "children": [
                {"kind": r.get("type"), "id": r.get("id"), "name": r.get("name")} for r in resources
            ],
            "irreversibility": "permanent",
            "detail": (
                "every machine, disk and network of the deployment is deleted; only a "
                "disk marked persistent stays. A deleted machine cannot be brought back."
            ),
        },
    }


register_preview_builder("vcfa.tenant.deployment.delete", deployment_delete_preview)
register_preview_builder("vcfa.vra8.deployment.delete", deployment_delete_preview)
