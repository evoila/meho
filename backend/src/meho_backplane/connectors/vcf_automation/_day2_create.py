# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create a machine or a disk through the IaaS API (shared day-2 handlers).

Written once for Aria Automation 8 and VCF Automation 9, like :mod:`._day2`
(see there for the connector contract and ``prefix``). Metadata lives in
:mod:`._day2_specs_iaas`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any, Final

from meho_backplane.connectors.vcf_automation._day2 import row_view, tracker_view
from meho_backplane.connectors.vcf_automation._day2_paths import (
    BLOCK_DEVICES_PATH,
    MACHINE_DISKS_PATH,
    MACHINES_PATH,
)
from meho_backplane.connectors.vcf_automation._lookups import quote_segment
from meho_backplane.connectors.vcf_automation._paths import (
    TENANT_IAAS_API_VERSION,
    TENANT_REQUEST_TRACKER_PATH,
)

__all__ = ["block_device_create", "machine_create"]

_API_VERSION: Final[dict[str, str]] = {"apiVersion": TENANT_IAAS_API_VERSION}

#: How long the disk create waits for its request, and how often it looks.
DISK_WAIT_SECONDS: float = 60.0
DISK_POLL_SECONDS: float = 2.0


def _odata_text(value: str) -> str:
    """One OData string literal: single quotes doubled."""
    return "'" + value.replace("'", "''") + "'"


def _tags(params: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {key: tag[key] for key in ("key", "value") if tag.get(key) is not None}
        for tag in params.get("tags") or []
    ]


def _constraints(params: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"expression": c["expression"], "mandatory": bool(c.get("mandatory", True))}
        for c in params.get("constraints") or []
    ]


async def _find_in_project(
    connector: Any, operator: Any, target: Any, path: str, name: str, project_id: str
) -> dict[str, Any] | None:
    query = {
        **_API_VERSION,
        "$filter": f"name eq {_odata_text(name)} and projectId eq {_odata_text(project_id)}",
    }
    answer = await connector._request_json(target, "GET", path, operator=operator, params=query)
    for row in answer.get("content") or []:
        if row.get("name") == name and row.get("projectId") == project_id:
            return dict(row)
    return None


async def machine_create(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """``POST /iaas/api/machines`` -- a machine outside a template.

    A machine of the same name in the same project answers ``unchanged`` and
    creates nothing, so a second run is safe.
    """
    name, project_id = params["name"], params["project_id"]
    existing = await _find_in_project(connector, operator, target, MACHINES_PATH, name, project_id)
    if existing is not None:
        return {
            "status": "unchanged",
            "request": None,
            "machine": row_view(existing, ("id", "name", "projectId", "powerState")),
            "guidance": f"a machine named {name!r} already exists in this project",
        }
    body: dict[str, Any] = {
        "name": name,
        "projectId": project_id,
        "flavor": params["flavor"],
        "image": params["image"],
    }
    for param, wire in (("description", "description"), ("deployment_id", "deploymentId")):
        if params.get(param):
            body[wire] = params[param]
    if params.get("tags"):
        body["tags"] = _tags(params)
    if params.get("constraints"):
        body["constraints"] = _constraints(params)
    tracker = await connector._post_json(
        target, MACHINES_PATH, operator=operator, params=_API_VERSION, json=body
    )
    return {
        "status": "submitted",
        "request": tracker_view(tracker),
        "machine": None,
        "guidance": (
            f"the machine is built in the background; read {prefix}.machine.list with "
            f"$filter \"name eq '{name}'\" until it shows up with a powerState"
        ),
    }


async def _wait_for_tracker(
    connector: Any, operator: Any, target: Any, tracker: dict[str, Any]
) -> dict[str, Any]:
    """Read the request tracker until it ends or :data:`DISK_WAIT_SECONDS` pass."""
    tracker_id = tracker.get("id")
    if not tracker_id:
        return tracker
    path = TENANT_REQUEST_TRACKER_PATH.format(id=quote_segment(tracker_id))
    deadline = time.monotonic() + DISK_WAIT_SECONDS
    while str(tracker.get("status")).upper() == "INPROGRESS" and time.monotonic() < deadline:
        await asyncio.sleep(DISK_POLL_SECONDS)
        tracker = await connector._request_json(
            target, "GET", path, operator=operator, params=_API_VERSION
        )
    return tracker


def _new_disk_id(tracker: Mapping[str, Any]) -> str | None:
    prefix = f"{BLOCK_DEVICES_PATH}/"
    for href in tracker.get("resources") or []:
        if isinstance(href, str) and href.startswith(prefix):
            return href[len(prefix) :].split("/", 1)[0] or None
    return None


async def block_device_create(
    connector: Any, operator: Any, target: Any, params: dict[str, Any], *, prefix: str
) -> dict[str, Any]:
    """Create a disk (``POST /iaas/api/block-devices``) and attach it to a machine.

    With ``block_device_id`` the disk exists and is only attached
    (``POST /iaas/api/machines/{id}/disks``). Otherwise the disk is created
    first; the op waits up to :data:`DISK_WAIT_SECONDS` for the create
    request, then attaches the new disk. A create still running then is the
    answer ``in_progress`` (attach it later with ``block_device_id``); a
    failed create is the answer ``failed``.
    """
    disk_id = params.get("block_device_id")
    created: dict[str, Any] | None = None
    if not disk_id:
        body: dict[str, Any] = {
            "name": params["name"],
            "projectId": params["project_id"],
            "capacityInGB": params["capacity_gb"],
            "persistent": bool(params.get("persistent", False)),
        }
        if params.get("description"):
            body["description"] = params["description"]
        tracker = await connector._post_json(
            target, BLOCK_DEVICES_PATH, operator=operator, params=_API_VERSION, json=body
        )
        tracker = await _wait_for_tracker(connector, operator, target, tracker)
        created = tracker_view(tracker)
        state = str(tracker.get("status")).upper()
        disk_id = _new_disk_id(tracker) if state == "FINISHED" else None
        if state == "FAILED":
            return {
                "status": "failed",
                "create_request": created,
                "attach_request": None,
                "block_device_id": None,
                "guidance": "the disk create failed; see create_request.message",
            }
        if disk_id is None:
            return {
                "status": "in_progress",
                "create_request": created,
                "attach_request": None,
                "block_device_id": None,
                "guidance": (
                    f"the disk is still being created; find it with {prefix}.block_device.list "
                    "and run this op again with block_device_id to attach it"
                ),
            }
    path = MACHINE_DISKS_PATH.format(id=quote_segment(params["machine_id"]))
    attach = await connector._post_json(
        target, path, operator=operator, params=_API_VERSION, json={"blockDeviceId": disk_id}
    )
    return {
        "status": "submitted",
        "create_request": created,
        "attach_request": tracker_view(attach),
        "block_device_id": disk_id,
        "guidance": (
            f"the attach runs in the background; read {prefix}.block_device.list until the "
            "disk shows the machine in machineIds"
        ),
    }
