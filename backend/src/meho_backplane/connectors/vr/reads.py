# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The bodies of the vSphere Replication pairing, replication, VM-check and task reads.

Each function is the body of a thin entry method on
:class:`~meho_backplane.connectors.vr.connector.VrConnector`; the op
metadata lives in :mod:`meho_backplane.connectors.vr.read_ops`.

**Flat rows.** A list read returns ``{"items": [...], "count", "more_rows",
"errors"}``. Each row is flat: a nested vendor object becomes a few named
fields (for example ``status.rpo_violation`` becomes ``rpo_violation``).
MEHO stores a long list behind a result handle; the agent reads it with
``result_query``.

**Pages.** The API pages a list with ``limit`` and ``offset``. A read asks
for :data:`PAGE_SIZE` rows at a time until the last page, at most
:data:`MAX_PAGES` pages. When rows are still left after that,
``more_rows`` is ``true``.

**Partial data.** A read that needs the remote site can come back with
part of the data and the reason in ``_meta.errors`` (for example when the
remote site is not logged in). The read returns those reasons in
``errors`` (``code``, ``message``, ``field``). The replication-server read
of ``vr.pairing.list`` runs once per vSphere Replication server of a
pairing; when one of these reads fails (often the remote server), the
read adds the failure to ``errors`` and goes on.

**Dates** stay as the API gives them: milliseconds since 1970.

**Secrets.** None of these answers carries a password or a key. The
``result`` object of a task is a free-form vendor object; a field with a
secret-like name in it comes back as :data:`REDACTED`.

The paths come from the vSphere Replication REST API v2 reference; the
reconcile lane checks every path in :data:`VENDOR_CALLS`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors.vmware_shared.dr_rest import DrTargetLike
from meho_backplane.connectors.vr.connector import API_BASE
from meho_backplane.operations._rfc6570 import has_dot_segment

if TYPE_CHECKING:
    from meho_backplane.connectors.vr.connector import VrConnector

__all__ = [
    "MAX_DISK_READS",
    "MAX_PAGES",
    "PAGE_SIZE",
    "REDACTED",
    "VENDOR_CALLS",
    "pairing_list_impl",
    "replication_issues_impl",
    "replication_list_impl",
    "task_get_impl",
    "vm_check_impl",
]

PAIRINGS_PATH: Final = f"{API_BASE}/pairings"
_VRS_PATH: Final = f"{PAIRINGS_PATH}/{{pairing_id}}/vrs"
_SERVERS_PATH: Final = f"{PAIRINGS_PATH}/{{pairing_id}}/vrs/{{vr_id}}/replication-servers"
_REPLICATIONS_PATH: Final = f"{PAIRINGS_PATH}/{{pairing_id}}/replications"
_DISKS_PATH: Final = f"{PAIRINGS_PATH}/{{pairing_id}}/replications/{{replication_id}}/disks"
_ISSUES_PATH: Final = f"{PAIRINGS_PATH}/{{pairing_id}}/replications/issues"
_CAPABILITY_PATH: Final = (
    f"{PAIRINGS_PATH}/{{pairing_id}}/vcenters/{{vcenter_id}}/vms/{{vm_id}}/capability"
)
_DATASTORES_PATH: Final = f"{PAIRINGS_PATH}/{{pairing_id}}/vcenters/{{vcenter_id}}/datastores"
_TASK_PATH: Final = f"{API_BASE}/tasks/{{task_id}}"

#: Every vendor call of these reads, as ``(method, path template)``.
VENDOR_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    ("GET", path)
    for path in (
        PAIRINGS_PATH,
        _VRS_PATH,
        _SERVERS_PATH,
        _REPLICATIONS_PATH,
        _DISKS_PATH,
        _ISSUES_PATH,
        _CAPABILITY_PATH,
        _DATASTORES_PATH,
        _TASK_PATH,
    )
)

#: Rows asked for per page.
PAGE_SIZE: Final = 100
#: Most pages one list read asks for.
MAX_PAGES: Final = 50
#: Most replications ``include_disks`` reads the disks of (one call each).
MAX_DISK_READS: Final = 50
#: The text that replaces a secret value in a task result.
REDACTED: Final = "[REDACTED]"

_SERVER_FIELDS: Final = (
    ("id", "id"),
    ("name", "name"),
    ("url", "url"),
    ("status", "server_status"),
    ("version", "version"),
    ("build", "build_number"),
)
_PAIRING_SIDES: Final = ("local_vc_server", "remote_vc_server", "local_vrms", "remote_vrms")
_PAIRING_PREFIX: Final = {
    "local_vc_server": "local_vc",
    "remote_vc_server": "remote_vc",
    "local_vrms": "local_vrms",
    "remote_vrms": "remote_vrms",
}
_REPLICATION_SERVER_FIELDS: Final = (
    "id",
    "name",
    "hostname",
    "uuid",
    "connected",
    "decommissioned",
    "internally_managed",
    "replication_count",
    "fault",
)
_REPLICATION_SCALARS: Final = (
    "id",
    "name",
    "vm_id",
    "source_site_name",
    "source_vc_id",
    "target_site_name",
    "target_vc_id",
    "type",
    "replication_server_name",
    "target_replication_server_uuid",
    "configuration_state",
    "recovery_state",
    "recovery_error",
    "recovery_solution",
    "last_sync_time",
    "last_sync_duration",
    "last_sync_size",
    "current_rpo_violation",
    "quiescing_enabled",
    "network_compression_enabled",
    "encryption_enabled",
    "auto_replicate_new_disks_enabled",
    "mpit_enabled",
    "mpit_instances",
    "mpit_days",
    "vm_data_sets_replication_enabled",
    "enhanced_replication",
)
_ERROR_FIELDS: Final = ("code", "message", "field")
_ISSUE_FIELDS: Final = ("entity", "entity_name", "description", "status", "issue_type", "timestamp")
_CAPABILITY_FLAGS: Final = (
    "quiescing_supported",
    "network_compression_supported",
    "mpit_supported",
    "lwd_encryption_supported",
    "vm_crypt_supported",
    "vm_encrypted",
    "vm_encryption_key_provider_type",
    "auto_replicate_new_disks_supported",
    "nvme_disk_controller_supported",
    "ds_cluster_as_target_supported",
    "reconfigure_vm_disks_supported",
)
_DATASTORE_FIELDS: Final = (
    "id",
    "name",
    "type",
    "file_system_type",
    "capacity",
    "free_space",
    "overall_status",
    "accessible",
    "maintenance_mode",
    "parent",
    "parent_name",
    "path",
    "nkp_supported",
)
_TASK_SCALARS: Final = (
    "id",
    "description",
    "description_id",
    "entity",
    "entity_name",
    "status",
    "progress",
    "queued_time",
    "start_time",
    "complete_time",
)


def _segment(kind: str, value: Any) -> str:
    """Return *value* as one URL path segment, or raise ``ValueError``."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"the {kind} must be a non-empty string, got {value!r}")
    if value == "." or has_dot_segment(value):
        raise ValueError(f"the {kind} {value!r} must not be '.' or contain a '..' segment")
    return quote(value, safe="")


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pick(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    obj = _obj(source)
    return {key: obj.get(key) for key in keys}


def _meta_errors(payload: Any) -> list[dict[str, Any]]:
    errors = _obj(_obj(payload).get("_meta")).get("errors")
    if not isinstance(errors, list):
        return []
    return [_pick(error, _ERROR_FIELDS) for error in errors if isinstance(error, dict)]


async def _read_all(
    connector: VrConnector,
    operator: Operator,
    target: DrTargetLike,
    path: str,
    query: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Read every page of a list. Return ``(rows, errors, more_rows)``."""
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    offset = 0
    for _ in range(MAX_PAGES):
        payload = await connector._get_json(
            target,
            path,
            operator=operator,
            params={**(query or {}), "limit": PAGE_SIZE, "offset": offset},
        )
        page = _obj(payload).get("list")
        page_rows = [row for row in page if isinstance(row, dict)] if isinstance(page, list) else []
        rows.extend(page_rows)
        errors.extend(_meta_errors(payload))
        offset += len(page_rows)
        total = _obj(_obj(payload).get("_meta")).get("total")
        if len(page_rows) < PAGE_SIZE or (isinstance(total, int) and offset >= total):
            return rows, errors, False
    return rows, errors, True


async def _read_one(
    connector: VrConnector, operator: Operator, target: DrTargetLike, path: str
) -> dict[str, Any]:
    payload = await connector._get_json(target, path, operator=operator)
    return _obj(payload)


def _list_answer(
    rows: list[dict[str, Any]], errors: list[dict[str, Any]], more_rows: bool
) -> dict[str, Any]:
    return {"items": rows, "count": len(rows), "more_rows": more_rows, "errors": errors}


# ---------------------------------------------------------------------------
# vr.pairing.list
# ---------------------------------------------------------------------------


def _pairing_row(pairing: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"pairing_id": pairing.get("pairing_id")}
    for side in _PAIRING_SIDES:
        server = _obj(pairing.get(side))
        prefix = _PAIRING_PREFIX[side]
        for name, key in _SERVER_FIELDS:
            row[f"{prefix}_{name}"] = server.get(key)
    return row


async def _replication_servers(
    connector: VrConnector,
    operator: Operator,
    target: DrTargetLike,
    pairing_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The replication servers of every vSphere Replication server in one pairing."""
    pairing_seg = _segment("pairing id", pairing_id)
    vrs_payload = await _read_one(
        connector, operator, target, _VRS_PATH.format(pairing_id=pairing_seg)
    )
    errors = _meta_errors(vrs_payload)
    vrs_list = vrs_payload.get("list")
    rows: list[dict[str, Any]] = []
    for vrms in vrs_list if isinstance(vrs_list, list) else []:
        vr_id = _obj(vrms).get("id")
        if not isinstance(vr_id, str) or not vr_id:
            continue
        path = _SERVERS_PATH.format(pairing_id=pairing_seg, vr_id=_segment("server id", vr_id))
        try:
            servers, server_errors, _ = await _read_all(connector, operator, target, path)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                # An expired session must reach the dispatcher, which logs
                # in again and runs the whole read once more.
                raise
            errors.append(
                {
                    "code": str(exc.response.status_code),
                    "message": (
                        f"reading the replication servers of vSphere Replication server "
                        f"{vr_id} failed: HTTP {exc.response.status_code} {exc.response.text[:300]}"
                    ),
                    "field": vr_id,
                }
            )
            continue
        errors.extend(server_errors)
        for server in servers:
            rows.append(
                {
                    "pairing_id": pairing_id,
                    "vrms_id": vr_id,
                    "vrms_name": _obj(vrms).get("name"),
                    **_pick(server, _REPLICATION_SERVER_FIELDS),
                }
            )
    return rows, errors


async def pairing_list_impl(
    connector: VrConnector,
    operator: Operator,
    target: DrTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vr.pairing.list``: the pairings, and the replication servers of each one."""
    del params  # the schema declares no parameters
    pairings, errors, more_rows = await _read_all(connector, operator, target, PAIRINGS_PATH)
    servers: list[dict[str, Any]] = []
    for pairing in pairings:
        pairing_id = pairing.get("pairing_id")
        if not isinstance(pairing_id, str) or not pairing_id:
            continue
        rows, server_errors = await _replication_servers(connector, operator, target, pairing_id)
        servers.extend(rows)
        errors.extend(server_errors)
    answer = _list_answer([_pairing_row(p) for p in pairings], errors, more_rows)
    answer["replication_servers"] = servers
    return answer


# ---------------------------------------------------------------------------
# vr.replication.list and vr.replication.issues
# ---------------------------------------------------------------------------


def _replication_row(replication: dict[str, Any], pairing_id: str) -> dict[str, Any]:
    status = _obj(replication.get("status"))
    rpo_violation = status.get("rpo_violation")
    row: dict[str, Any] = {"pairing_id": pairing_id, **_pick(replication, _REPLICATION_SCALARS)}
    row["status"] = status.get("status")
    row["rpo_minutes"] = replication.get("rpo")
    row["rpo_violation"] = rpo_violation
    row["rpo_met"] = (not rpo_violation) if isinstance(rpo_violation, bool) else None
    row["sync_progress"] = _obj(replication.get("sync_progress")).get("progress")
    for key in ("configuration_error", "last_group_error"):
        error = _obj(replication.get(key))
        row[key] = error.get("message")
        row[f"{key}_details"] = error.get("details")
    return row


def _path_fields(prefix: str, data: Any) -> dict[str, Any]:
    obj = _obj(data)
    return {
        f"{prefix}_datastore_id": obj.get("datastore_id"),
        f"{prefix}_datastore_name": obj.get("datastore_name"),
        f"{prefix}_path": obj.get("path"),
        f"{prefix}_file": obj.get("filename"),
    }


def _disk_row(replication_id: str, disk: dict[str, Any]) -> dict[str, Any]:
    vm_disk = _obj(disk.get("vm_disk"))
    return {
        "replication_id": replication_id,
        "device_key": vm_disk.get("device_key"),
        "label": vm_disk.get("label"),
        "is_vm_home": vm_disk.get("is_vm_home"),
        "replicated": disk.get("replicated"),
        "capacity_kb": vm_disk.get("capacity"),
        "encrypted": vm_disk.get("encrypted"),
        "supported_for_replication": vm_disk.get("supported_for_replication"),
        "source_disk_format": vm_disk.get("source_disk_format"),
        **_path_fields("source", vm_disk.get("source_path")),
        "destination_disk_format": disk.get("destination_disk_format"),
        "destination_storage_policy_name": disk.get("destination_storage_policy_name"),
        **_path_fields("destination", disk.get("destination_path")),
        "replica_disk_usage": disk.get("replica_disk_usage"),
    }


def _replication_query(params: dict[str, Any]) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if params.get("source_vc_guid"):
        query["source_vc_guid"] = params["source_vc_guid"]
    return query


async def replication_list_impl(
    connector: VrConnector,
    operator: Operator,
    target: DrTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vr.replication.list``: the replications of one pairing, with RPO state."""
    pairing_id = params.get("pairing_id")
    pairing_seg = _segment("pairing id", pairing_id)
    query = {**_replication_query(params), "extended_info": "true"}
    if params.get("name"):
        query["filter_property"] = "name"
        query["filter"] = [params["name"]]
    path = _REPLICATIONS_PATH.format(pairing_id=pairing_seg)
    replications, errors, more_rows = await _read_all(connector, operator, target, path, query)
    rows = [_replication_row(r, str(pairing_id)) for r in replications]
    answer = _list_answer(rows, errors, more_rows)
    if not params.get("include_disks"):
        return answer
    if len(rows) > MAX_DISK_READS:
        raise ValueError(
            f"include_disks reads one call per replication, at most {MAX_DISK_READS}; "
            f"pairing {pairing_id} has {len(rows)} replications. Set name to read the "
            "disks of one replication."
        )
    disks: list[dict[str, Any]] = []
    for row in rows:
        replication_id = row.get("id")
        if not isinstance(replication_id, str) or not replication_id:
            continue
        disk_path = _DISKS_PATH.format(
            pairing_id=pairing_seg, replication_id=_segment("replication id", replication_id)
        )
        disk_rows, disk_errors, _ = await _read_all(connector, operator, target, disk_path)
        disks.extend(_disk_row(replication_id, disk) for disk in disk_rows)
        errors.extend(disk_errors)
    answer["disks"] = disks
    return answer


async def replication_issues_impl(
    connector: VrConnector,
    operator: Operator,
    target: DrTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vr.replication.issues``: the current issues of the replications of one pairing."""
    pairing_id = params.get("pairing_id")
    path = _ISSUES_PATH.format(pairing_id=_segment("pairing id", pairing_id))
    issues, errors, more_rows = await _read_all(
        connector, operator, target, path, _replication_query(params)
    )
    return _list_answer([_pick(issue, _ISSUE_FIELDS) for issue in issues], errors, more_rows)


# ---------------------------------------------------------------------------
# vr.vm.check
# ---------------------------------------------------------------------------


async def vm_check_impl(
    connector: VrConnector,
    operator: Operator,
    target: DrTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vr.vm.check``: can this VM be replicated, and which target datastores fit."""
    pairing_id = params.get("pairing_id")
    vcenter_id = params.get("vcenter_id")
    vm_id = params.get("vm_id")
    target_vcenter_id = params.get("target_vcenter_id") or vcenter_id
    pairing_seg = _segment("pairing id", pairing_id)
    capability_path = _CAPABILITY_PATH.format(
        pairing_id=pairing_seg,
        vcenter_id=_segment("vCenter id", vcenter_id),
        vm_id=_segment("VM id", vm_id),
    )
    datastores_path = _DATASTORES_PATH.format(
        pairing_id=pairing_seg, vcenter_id=_segment("target vCenter id", target_vcenter_id)
    )
    capability = await _read_one(connector, operator, target, capability_path)
    datastores, errors, more_rows = await _read_all(connector, operator, target, datastores_path)
    validation_status = capability.get("vm_validation_status")
    reasons = capability.get("replication_not_supported_reasons")
    reasons = [reason for reason in reasons if reason] if isinstance(reasons, list) else []
    answer = _list_answer(
        [_pick(datastore, _DATASTORE_FIELDS) for datastore in datastores],
        _meta_errors(capability) + errors,
        more_rows,
    )
    return {
        "vm_id": vm_id,
        "vcenter_id": vcenter_id,
        "target_vcenter_id": target_vcenter_id,
        "vm_validation_status": validation_status,
        "replication_possible": validation_status == "OK" and not reasons,
        "replication_not_supported_reasons": reasons,
        "min_rpo_mins": capability.get("min_rpo_mins"),
        "capability": _pick(capability, _CAPABILITY_FLAGS),
        **answer,
    }


# ---------------------------------------------------------------------------
# vr.task.get
# ---------------------------------------------------------------------------


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(key, str) and is_secret_param_name(key) and item is not None
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


async def task_get_impl(
    connector: VrConnector,
    operator: Operator,
    target: DrTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vr.task.get``: the state of one task."""
    task_id = params.get("task_id")
    task = await _read_one(
        connector, operator, target, _TASK_PATH.format(task_id=_segment("task id", task_id))
    )
    error = _obj(task.get("error"))
    return {
        **_pick(task, _TASK_SCALARS),
        "error": error.get("message"),
        "error_details": error.get("details"),
        "result": _redact(task.get("result")),
    }
