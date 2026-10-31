# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The HCX service mesh changes and the interconnect task read.

``hcx.service_mesh.create``, ``hcx.service_mesh.update``,
``hcx.service_mesh.resync`` and ``hcx.service_mesh.delete`` start vendor
work and return as soon as the HCX Manager accepted it. Each answer says
``accepted`` and gives the vendor's interconnect task id. The caller
follows the task with ``hcx.task.get`` until it ends. The op metadata
lives in :mod:`meho_backplane.connectors.hcx.service_mesh_ops`.

Before a start, each change asks the Manager for queued or running
interconnect tasks (``POST /hybridity/api/interconnect/tasks/query``). If
one runs on the same service mesh (for create: on a mesh with the same
name), the change returns that task with ``status: already_running`` and
starts nothing. This check is a help, not a lock.

The paths come from the vendor reference "HCX Workload Migration APIs"
(Broadcom developer portal, no version named) and VMware's Terraform
provider for HCX. Every start call answers ``202`` with
``{"data": {"interconnectTaskId", "serviceMeshId"}, "warnings", "errors"}``.

The preview builder of ``hcx.service_mesh.delete`` reads the mesh and its
appliances first and fills the ``blast_radius`` block. It refuses when the
mesh does not exist.
"""

from __future__ import annotations

import ssl
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import SsrfBlockedError
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.connectors.hcx.typed_reads import (
    _envelope_notes,
    _redact_object,
    _segment,
    redact_secrets,
    rows_of,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = [
    "STATUS_READ",
    "VENDOR_CALLS",
    "hcx_service_mesh_create_impl",
    "hcx_service_mesh_delete_impl",
    "hcx_service_mesh_resync_impl",
    "hcx_service_mesh_update_impl",
    "hcx_task_get_impl",
]

_SERVICE_MESH_PATH: Final = "/hybridity/api/interconnect/serviceMesh"
_SERVICE_MESH_ITEM_PATH: Final = "/hybridity/api/interconnect/serviceMesh/{serviceMeshId}"
_RESYNC_PATH: Final = "/hybridity/api/interconnect/serviceMesh/{serviceMeshId}/resync"
_APPLIANCES_PATH: Final = "/hybridity/api/interconnect/appliances"
_TASK_PATH: Final = "/hybridity/api/interconnect/tasks/{interconnectTaskId}"
_TASK_QUERY_PATH: Final = "/hybridity/api/interconnect/tasks/query"

#: Every vendor call of this module, as ``(method, path template)``.
#: The reconcile lane checks each one against the pinned HCX API files.
VENDOR_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("POST", _SERVICE_MESH_PATH),
        ("GET", _SERVICE_MESH_ITEM_PATH),
        ("PUT", _SERVICE_MESH_ITEM_PATH),
        ("DELETE", _SERVICE_MESH_ITEM_PATH),
        ("POST", _RESYNC_PATH),
        ("GET", _APPLIANCES_PATH),
        ("GET", _TASK_PATH),
        ("POST", _TASK_QUERY_PATH),
    }
)

#: The read that follows every task a change starts.
STATUS_READ: Final = "hcx.task.get"

#: Vendor task states of a task that has not ended.
_ACTIVE_STATES: Final[tuple[str, ...]] = ("QUEUED", "RUNNING")
_FAILED_STATE: Final = "FAILED"
_ENDED_STATES: Final[frozenset[str]] = frozenset({"SUCCESS", _FAILED_STATE})
_NOT_REACHABLE: Final = "not_reachable"

#: The top-level fields of a vendor service mesh object that a ``PUT`` sends back.
_MESH_BODY_FIELDS: Final[tuple[str, ...]] = (
    "serviceMeshId",
    "name",
    "isLocalMesh",
    "orchestratorInfo",
    "switchPairCount",
    "wanoptConfig",
    "services",
    "computeProfiles",
    "trafficEnggCfg",
)

_DELETE_OP_ID: Final = "hcx.service_mesh.delete"

#: Switch fields of a switch pair: ``(parameter name, vendor name)``.
_SWITCH_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("id", "id"),
    ("cmp_id", "cmpId"),
    ("name", "name"),
    ("type", "type"),
)


def _compute_profile_body(profile: dict[str, Any]) -> dict[str, Any]:
    """One ``computeProfiles`` entry in vendor field names."""
    body: dict[str, Any] = {
        "endpointId": profile["endpoint_id"],
        "computeProfileId": profile["compute_profile_id"],
    }
    if profile.get("endpoint_name"):
        body["endpointName"] = profile["endpoint_name"]
    if profile.get("compute_profile_name"):
        body["computeProfileName"] = profile["compute_profile_name"]
    if profile.get("uplink_networks"):
        body["networks"] = _networks_body(profile["uplink_networks"])
        body["overriddenUplink"] = True
    return body


def _networks_body(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ``networks`` list of a compute profile entry: ``{id, name}`` per uplink."""
    return [{"id": net["id"], "name": net.get("name")} for net in networks]


def _switch_pairs_body(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ``switchPairCount`` list in vendor field names."""
    body = []
    for pair in pairs:
        switches = [
            {key: switch[param] for param, key in _SWITCH_FIELDS if switch.get(param) is not None}
            for switch in pair["switches"]
        ]
        body.append(
            {"switches": switches, "l2cApplianceCount": pair.get("network_extension_appliances", 1)}
        )
    return body


def _apply_options(body: dict[str, Any], params: dict[str, Any]) -> None:
    """Write the optional settings of *params* into the vendor *body* (in place)."""
    if params.get("services"):
        body["services"] = [{"name": name} for name in params["services"]]
    if params.get("compute_profiles"):
        body["computeProfiles"] = [_compute_profile_body(p) for p in params["compute_profiles"]]
    if params.get("switch_pairs"):
        body["switchPairCount"] = _switch_pairs_body(params["switch_pairs"])
    if params.get("uplink_max_bandwidth") is not None:
        body["wanoptConfig"] = {"uplinkMaxBandwidth": params["uplink_max_bandwidth"]}
    traffic = {
        vendor: params[param]
        for param, vendor in (
            ("app_path_resiliency", "isAppPathResiliencyEnabled"),
            ("tcp_flow_conditioning", "isTcpFlowConditioningEnabled"),
        )
        if params.get(param) is not None
    }
    if traffic:
        body["trafficEnggCfg"] = {**(body.get("trafficEnggCfg") or {}), **traffic}


def _apply_uplinks(body: dict[str, Any], uplinks: list[dict[str, Any]]) -> None:
    """Set the uplink networks of the named compute profiles in *body* (in place)."""
    profiles = body.get("computeProfiles") or []
    for change in uplinks:
        matches = [p for p in profiles if p.get("computeProfileId") == change["compute_profile_id"]]
        if not matches:
            raise ValueError(
                f"the service mesh uses no compute profile {change['compute_profile_id']!r}; "
                "the uplink change names a profile the mesh does not use"
            )
        for profile in matches:
            profile["networks"] = _networks_body(change["networks"])
            profile["overriddenUplink"] = True


async def _running_task(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    *,
    mesh_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any] | None:
    """Return a queued or running task on the mesh (by id, or by name for create), else ``None``."""
    task_filter: dict[str, Any] = {"status": list(_ACTIVE_STATES)}
    if mesh_id is not None:
        task_filter["entityIds"] = [mesh_id]
    payload = await connector._post_json(
        target, _TASK_QUERY_PATH, operator=operator, json={"filter": task_filter}
    )
    for task in rows_of(payload):
        if str(task.get("status") or "").upper() not in _ACTIVE_STATES:
            continue
        entity = task.get("entityDetails")
        entity = entity if isinstance(entity, dict) else {}
        if mesh_id is not None and entity.get("id") == mesh_id:
            return task
        if name is not None and entity.get("name") == name:
            return task
    return None


def _already_running(task: dict[str, Any], mesh_id: str | None) -> dict[str, Any]:
    """The answer of a change that found a task already running and started nothing."""
    entity = task.get("entityDetails")
    entity = entity if isinstance(entity, dict) else {}
    return _redact_object(
        {
            "status": "already_running",
            "service_mesh_id": entity.get("id") or mesh_id,
            "interconnect_task_id": task.get("interconnectTaskId"),
            "task_type": task.get("type"),
            "task_status": task.get("status"),
            "status_read": STATUS_READ,
            "warnings": [],
            "errors": [],
        }
    )


def _accepted(payload: Any, mesh_id: str | None) -> dict[str, Any]:
    """The answer of a start the Manager accepted; raises when it named no task."""
    body = payload if isinstance(payload, dict) else {}
    data = body.get("data")
    data = data if isinstance(data, dict) else {}
    notes = _envelope_notes(payload)
    task_id = data.get("interconnectTaskId")
    if not task_id:
        messages = "; ".join(str(e.get("message")) for e in notes["errors"] if isinstance(e, dict))
        raise ValueError(
            "the HCX Manager answered without an interconnect task id"
            + (f": {messages}" if messages else "")
        )
    return _redact_object(
        {
            "status": "accepted",
            "service_mesh_id": data.get("serviceMeshId") or mesh_id,
            "interconnect_task_id": task_id,
            "task_type": None,
            "task_status": None,
            "status_read": STATUS_READ,
            **notes,
        }
    )


async def hcx_service_mesh_create_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.service_mesh.create`` -- ``POST /hybridity/api/interconnect/serviceMesh``."""
    name = params["name"]
    running = await _running_task(connector, operator, target, name=name)
    if running is not None:
        return _already_running(running, None)
    body: dict[str, Any] = {"name": name}
    _apply_options(body, params)
    payload = await connector._post_json(target, _SERVICE_MESH_PATH, operator=operator, json=body)
    return _accepted(payload, None)


async def _read_mesh(
    connector: HcxConnector, operator: Operator, target: HcxTargetLike, mesh_id: str
) -> dict[str, Any]:
    """``GET /hybridity/api/interconnect/serviceMesh/{serviceMeshId}``: the mesh object."""
    path = _SERVICE_MESH_ITEM_PATH.format(serviceMeshId=_segment("service mesh", mesh_id))
    payload = await connector._get_json(target, path, operator=operator)
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and "serviceMeshId" in data:
        return data
    return payload if isinstance(payload, dict) else {}


async def hcx_service_mesh_update_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.service_mesh.update`` -- read the mesh, merge the changes, ``PUT`` it back.

    The ``PUT`` needs the whole mesh object, so the change reads it first
    (``GET .../serviceMesh/{serviceMeshId}``), replaces the fields the
    caller set and sends the vendor fields of the object back.
    """
    mesh_id = params["service_mesh_id"]
    running = await _running_task(connector, operator, target, mesh_id=mesh_id)
    if running is not None:
        return _already_running(running, mesh_id)
    current = await _read_mesh(connector, operator, target, mesh_id)
    body = {key: current[key] for key in _MESH_BODY_FIELDS if key in current}
    body["serviceMeshId"] = mesh_id
    _apply_options(body, params)
    if params.get("uplinks"):
        _apply_uplinks(body, params["uplinks"])
    path = _SERVICE_MESH_ITEM_PATH.format(serviceMeshId=_segment("service mesh", mesh_id))
    payload = await connector._post_json(target, path, operator=operator, verb="PUT", json=body)
    return _accepted(payload, mesh_id)


async def hcx_service_mesh_resync_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.service_mesh.resync`` -- ``POST .../serviceMesh/{serviceMeshId}/resync``.

    The Manager computes the difference between the mesh and its compute
    profiles and applies it; it can redeploy appliances.
    """
    mesh_id = params["service_mesh_id"]
    path = _RESYNC_PATH.format(serviceMeshId=_segment("service mesh", mesh_id))
    running = await _running_task(connector, operator, target, mesh_id=mesh_id)
    if running is not None:
        return _already_running(running, mesh_id)
    payload = await connector._post_json(target, path, operator=operator)
    return _accepted(payload, mesh_id)


async def hcx_service_mesh_delete_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.service_mesh.delete`` -- ``DELETE .../serviceMesh/{serviceMeshId}?force=...``."""
    mesh_id = params["service_mesh_id"]
    path = _SERVICE_MESH_ITEM_PATH.format(serviceMeshId=_segment("service mesh", mesh_id))
    running = await _running_task(connector, operator, target, mesh_id=mesh_id)
    if running is not None:
        return _already_running(running, mesh_id)
    force = "true" if params.get("force") else "false"
    payload = await connector._post_json(
        target, path, operator=operator, verb="DELETE", params={"force": force}
    )
    return _accepted(payload, mesh_id)


def _is_not_reachable(exc: httpx.TransportError) -> bool:
    """True when the Manager did not answer.

    An untrusted certificate means the Manager did answer, and an
    :class:`SsrfBlockedError` means MEHO refused the address before any
    request. Both go on to the dispatcher, which names the real reason.
    """
    if isinstance(exc, SsrfBlockedError):
        return False
    tls = isinstance(exc.__cause__, ssl.SSLCertVerificationError) or (
        "CERTIFICATE_VERIFY_FAILED" in str(exc)
    )
    return not tls


def _task_error(task: dict[str, Any]) -> str | None:
    """The vendor's error text of a failed task: its message and the step errors."""
    if str(task.get("status") or "").upper() != _FAILED_STATE:
        return None
    texts = [str(task["message"])] if task.get("message") else []
    for step in task.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for error in step.get("errors") or []:
            if isinstance(error, dict) and error.get("message"):
                texts.append(str(error["message"]))
    return "; ".join(texts) or "the task failed; the Manager gave no error text"


def _step_row(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": step.get("title"),
        "type": step.get("type"),
        "status": step.get("status"),
        "progress": step.get("progress"),
        "message": step.get("message"),
        "job_id": step.get("jobId"),
        "errors": step.get("errors") or [],
    }


def _task_answer(task: dict[str, Any], task_id: str, checked_at: str) -> dict[str, Any]:
    """The flat answer of ``hcx.task.get`` for one vendor task object."""
    entity = task.get("entityDetails")
    entity = entity if isinstance(entity, dict) else {}
    status = task.get("status")
    steps = [_step_row(s) for s in task.get("steps") or [] if isinstance(s, dict)]
    return {
        "interconnect_task_id": task.get("interconnectTaskId") or task_id,
        "status": status,
        "done": str(status or "").upper() in _ENDED_STATES,
        "progress": task.get("progress"),
        "message": task.get("message"),
        "error": _task_error(task),
        "type": task.get("type"),
        "entity_id": entity.get("id"),
        "entity_type": entity.get("type"),
        "entity_name": entity.get("name"),
        "start_time": task.get("startTime"),
        "end_time": task.get("endTime"),
        "steps": redact_secrets(steps),
        "checked_at": checked_at,
    }


async def hcx_task_get_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.task.get`` -- ``GET /hybridity/api/interconnect/tasks/{interconnectTaskId}``.

    When the Manager does not answer (a connection or time-out error after
    the retries), returns ``status: not_reachable`` with the task id and
    ``checked_at``. A ``401`` is raised, so the dispatcher logs in again.
    """
    task_id = params["task_id"]
    path = _TASK_PATH.format(interconnectTaskId=_segment("task", task_id))
    checked_at = datetime.now(UTC).isoformat()
    try:
        payload = await connector._get_json(target, path, operator=operator)
    except httpx.TransportError as exc:
        if not _is_not_reachable(exc):
            raise
        return {**_task_answer({}, task_id, checked_at), "status": _NOT_REACHABLE}
    task = payload if isinstance(payload, dict) else {}
    return _redact_object(_task_answer(task, task_id, checked_at))


def _appliance_child(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "appliance",
        "id": row.get("applianceId"),
        "name": row.get("applianceName"),
        "type": row.get("applianceType"),
    }


async def _service_mesh_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The ``blast_radius`` of ``hcx.service_mesh.delete``: the mesh, its appliances and services.

    Read-only. Raises when the mesh does not exist, so the request is
    refused before it waits for approval. Declines (``None``) without a
    live HCX connector or target.
    """
    from meho_backplane.connectors.hcx.connector import HcxConnector

    connector = ctx.connector_instance
    mesh_id = ctx.params.get("service_mesh_id")
    if not isinstance(connector, HcxConnector) or not isinstance(mesh_id, str):
        return None
    if ctx.target is None:
        return None
    try:
        mesh = await _read_mesh(connector, ctx.operator, ctx.target, mesh_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == httpx.codes.NOT_FOUND:
            raise ValueError(f"the service mesh {mesh_id!r} does not exist") from exc
        raise
    appliances = await connector._get_json(ctx.target, _APPLIANCES_PATH, operator=ctx.operator)
    children = sorted(
        (
            _appliance_child(row)
            for row in rows_of(appliances)
            if row.get("serviceMeshId") == mesh_id
        ),
        key=lambda child: str(child["id"]),
    )
    services = [s.get("name") for s in mesh.get("services") or [] if isinstance(s, dict)]
    return {
        "blast_radius": {
            "object": {
                "kind": "service_mesh",
                "id": mesh_id,
                "name": mesh.get("name"),
                "services": services,
                "force": bool(ctx.params.get("force")),
            },
            "children": children,
            "irreversibility": (
                "The appliances of the mesh are removed. Migrations and network "
                "extensions that use the mesh stop working. A new mesh needs a new "
                "create and a new deployment of every appliance."
            ),
            "match_count": len(children),
        }
    }


register_preview_builder(_DELETE_OP_ID, _service_mesh_delete_preview)
