# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The HCX migration operations: validate, start, list, cancel, mobility groups, switchover window.

Each function is the body of a thin shim method on
:class:`~meho_backplane.connectors.hcx.connector.HcxConnector`; the op
metadata lives in :mod:`meho_backplane.connectors.hcx.migration_ops`.

**Vendor calls.** The paths come from the vendor reference "HCX Workload
Migration APIs" (Broadcom developer portal, no version named). The
mobility group calls (``/hybridity/api/mobility/groups...``) do the work:
a migration of VMs is a mobility group with one migration per VM. The
older calls ``/hybridity/api/migrations?action=validate|start|cancel``
are marked deprecated there, so they are not used. The migration list
uses ``POST /hybridity/api/migrations?action=query`` (not deprecated).

**Vendor jobs.** ``hcx.migration.start`` and ``hcx.migration.cancel``
return as soon as HCX accepted the work (``status: accepted``) with the
migration ids and the status read that follows them
(``hcx.migration.list``). Before a start they look for a running
migration of the same VMs or group, and before a cancel for migrations
that already ended; this is a help, not a lock. MEHO keeps no job state
between calls. When HCX does not answer, the status read returns
``reachable: false`` with the ids it was given and the time, not
"failed".

**Multi-step start.** A start of VMs (not of an existing group) makes a
mobility group first, then starts it. When the second step fails, the
answer lists the steps that ran, the step that failed with the vendor's
error, and the steps that did not run. The group stays; nothing is undone.

Every answer runs through :func:`~meho_backplane.connectors.hcx.typed_reads.redact_secrets`.
"""

from __future__ import annotations

import ssl
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import SsrfBlockedError
from meho_backplane.connectors.hcx.migration_body import (
    group_item,
    group_row,
    migration_row,
    nested,
    schedule_errors,
    schedule_window,
)
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.connectors.hcx.typed_reads import (
    _redact_object,
    _segment,
    redact_secrets,
    rows_of,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = [
    "MIGRATION_VENDOR_CALLS",
    "STATUS_READ",
    "hcx_migration_cancel_impl",
    "hcx_migration_list_impl",
    "hcx_migration_schedule_impl",
    "hcx_migration_start_impl",
    "hcx_migration_validate_impl",
    "hcx_mobility_group_list_impl",
    "hcx_mobility_group_set_impl",
]

_MIGRATIONS_PATH: Final = "/hybridity/api/migrations"
_GROUPS_PATH: Final = "/hybridity/api/mobility/groups"
_GROUPS_VALIDATE_PATH: Final = "/hybridity/api/mobility/groups/validate"
_GROUPS_START_PATH: Final = "/hybridity/api/mobility/groups/start"
_GROUP_SCHEDULE_PATH: Final = "/hybridity/api/mobility/groups/{mobilityGroupId}/schedule"
_CANCEL_PATH: Final = "/hybridity/api/mobility/migrations/cancel"
_FORCE_CANCEL_PATH: Final = "/hybridity/api/mobility/migrations/forceCancel"
_GROUPS_V2_QUERY_PATH: Final = "/hybridity/api/v2/mobility-groups/query"
_GROUP_V2_SUMMARY_PATH: Final = "/hybridity/api/v2/mobility-groups/{mobilityGroupId}/summary"

#: Every vendor call the migration ops make, as ``(method, path template)``.
#: The reconcile lane checks each one against the pinned HCX API files.
MIGRATION_VENDOR_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("POST", _MIGRATIONS_PATH),
        ("POST", _GROUPS_PATH),
        ("PUT", _GROUPS_PATH),
        ("POST", _GROUPS_VALIDATE_PATH),
        ("POST", _GROUPS_START_PATH),
        ("POST", _GROUP_SCHEDULE_PATH),
        ("POST", _CANCEL_PATH),
        ("POST", _FORCE_CANCEL_PATH),
        ("POST", _GROUPS_V2_QUERY_PATH),
        ("GET", _GROUP_V2_SUMMARY_PATH),
    }
)

#: The read that follows a started or cancelled migration.
STATUS_READ: Final = "hcx.migration.list"

_ENDED_OUTCOMES: Final[frozenset[str]] = frozenset({"succeeded", "failed", "cancelled"})

_VENDOR_TEXT_MAX: Final = 1000


def _is_not_reachable(exc: httpx.TransportError) -> bool:
    """True when HCX did not answer.

    An untrusted certificate means HCX did answer, and an
    :class:`~meho_backplane.connectors.adapters.http.SsrfBlockedError`
    means MEHO refused the address before any request. Both are raised on,
    so the dispatcher reports the real reason.
    """
    if isinstance(exc, SsrfBlockedError):
        return False
    return not (
        isinstance(exc.__cause__, ssl.SSLCertVerificationError)
        or "CERTIFICATE_VERIFY_FAILED" in str(exc)
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _notes(body: Any) -> dict[str, list[Any]]:
    """The ``errors`` and ``warnings`` lists of a vendor object, always present."""
    body = body if isinstance(body, dict) else {}
    return {"errors": body.get("errors") or [], "warnings": body.get("warnings") or []}


def _vendor_error(exc: httpx.HTTPError) -> str:
    """The vendor's error text of a failed call: the ``errors[].message`` texts, or the body."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return f"no answer from HCX: {type(exc).__name__}: {exc}"[:_VENDOR_TEXT_MAX]
    response = exc.response
    try:
        body = response.json()
    except ValueError:
        body = None
    messages: list[str] = []
    for holder in [body, *rows_of(body)]:
        for error in _notes(holder)["errors"]:
            if isinstance(error, dict) and error.get("message"):
                messages.append(str(error["message"]))
    text = "; ".join(messages) or response.text
    return f"HTTP {response.status_code}: {text}"[:_VENDOR_TEXT_MAX]


async def _query_migrations(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    migration_filter: dict[str, Any],
) -> list[dict[str, Any]]:
    """``POST /hybridity/api/migrations?action=query`` -> flat migration rows."""
    payload = await connector._post_json(
        target,
        _MIGRATIONS_PATH,
        operator=operator,
        params={"action": "query"},
        json={"filter": migration_filter},
    )
    rows = rows_of(payload)
    if not rows and isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        rows = [row for row in payload["rows"] if isinstance(row, dict)]
    return [migration_row(row) for row in redact_secrets(rows)]


async def hcx_migration_list_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.migration.list`` -- the migrations with their state and progress.

    When HCX does not answer, returns ``reachable: false`` with the ids it
    was given and ``checked_at``, and no rows.
    """
    migration_filter: dict[str, Any] = {"skipDrafts": not params.get("include_drafts", False)}
    for name, vendor in (
        ("migration_ids", "migrationId"),
        ("vm_ids", "entityId"),
        ("state", "state"),
    ):
        if params.get(name):
            migration_filter[vendor] = list(params[name])
    if params.get("mobility_group_id"):
        migration_filter["migrationGroupId"] = [params["mobility_group_id"]]
    checked_at = _now()
    try:
        rows = await _query_migrations(connector, operator, target, migration_filter)
    except httpx.TransportError as exc:
        if not _is_not_reachable(exc):
            raise
        return {
            "items": [],
            "reachable": False,
            "checked_at": checked_at,
            "migration_ids": list(params.get("migration_ids") or []),
            "mobility_group_id": params.get("mobility_group_id"),
        }
    return {
        "items": rows,
        "reachable": True,
        "checked_at": checked_at,
        "migration_ids": list(params.get("migration_ids") or []),
        "mobility_group_id": params.get("mobility_group_id"),
    }


def _migration_results(item: dict[str, Any]) -> list[dict[str, Any]]:
    """The per-VM rows of a mobility group answer item."""
    return [
        {
            "migration_id": migration.get("migrationId"),
            "vm_id": migration.get("entityId") or nested(migration, "entity", "entityId"),
            **_notes(migration),
        }
        for migration in item.get("migrations") or []
        if isinstance(migration, dict)
    ]


def _first_item(payload: Any) -> dict[str, Any]:
    rows = rows_of(payload)
    return rows[0] if rows else {}


def _status_read_params(
    group_id: Any, running: list[dict[str, Any]], vm_ids: list[str] | None
) -> dict[str, Any]:
    """The ``hcx.migration.list`` parameters that follow a start; never a null value.

    The group id when it is known; the ids of the running migrations for
    ``already_running``; else the VM ids, so a start whose group was not
    made can still be followed. An empty object when nothing is known.
    """
    read_params: dict[str, Any] = {}
    if group_id:
        read_params["mobility_group_id"] = group_id
    running_ids = [row["migration_id"] for row in running if row.get("migration_id")]
    if running_ids:
        read_params["migration_ids"] = running_ids
    elif not group_id and vm_ids:
        read_params["vm_ids"] = list(vm_ids)
    return read_params


def _job_answer(
    status: str,
    group_id: Any,
    *,
    item: dict[str, Any] | None = None,
    running: list[dict[str, Any]] | None = None,
    vm_ids: list[str] | None = None,
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    item = item or {}
    group_id = item.get("migrationGroupId") or group_id
    running = running or []
    return _redact_object(
        {
            "status": status,
            "mobility_group_id": group_id,
            "migrations": _migration_results(item),
            "running": [
                {key: row.get(key) for key in ("migration_id", "vm_id", "state", "outcome")}
                for row in running
            ],
            **_notes(item),
            "steps": steps,
            "status_read": STATUS_READ,
            "status_read_params": _status_read_params(group_id, running, vm_ids),
        }
    )


def _step(name: str, state: str, error: str | None = None) -> dict[str, Any]:
    return {"step": name, "state": state, "error": error}


async def _running(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    migration_filter: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = await _query_migrations(
        connector, operator, target, {"skipDrafts": True, **migration_filter}
    )
    return [row for row in rows if row["outcome"] == "running"]


async def _start_group(
    connector: HcxConnector, operator: Operator, target: HcxTargetLike, group_id: str
) -> dict[str, Any]:
    payload = await connector._post_json(
        target,
        _GROUPS_START_PATH,
        operator=operator,
        json={"items": [{"migrationGroupId": group_id}]},
    )
    return _first_item(payload)


async def hcx_migration_start_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.migration.start`` -- start a mobility group, or make one for VMs and start it.

    Returns ``status: already_running`` (and starts nothing) when a
    migration of the same group or VMs still runs; ``accepted`` when HCX
    took the start; ``failed`` or ``no_answer`` when the start step of a
    new group failed after the group was made.
    """
    group_id = params.get("mobility_group_id")
    if group_id:
        running = await _running(connector, operator, target, {"migrationGroupId": [group_id]})
        if running:
            return _job_answer("already_running", group_id, running=running, steps=[])
        item = await _start_group(connector, operator, target, group_id)
        return _job_answer("accepted", group_id, item=item, steps=[_step("start_group", "done")])
    group = params["group"]
    vm_ids = [vm["vm_id"] for vm in group["vms"]]
    running = await _running(connector, operator, target, {"entityId": vm_ids})
    if running:
        return _job_answer("already_running", None, running=running, vm_ids=vm_ids, steps=[])
    created = _first_item(
        await connector._post_json(
            target, _GROUPS_PATH, operator=operator, json={"items": [group_item(group)]}
        )
    )
    new_id = created.get("migrationGroupId")
    if not new_id:
        errors = "; ".join(
            str(error.get("message"))
            for error in _notes(created)["errors"]
            if isinstance(error, dict)
        )
        steps = [
            _step("create_group", "failed", errors or "HCX returned no mobility group id"),
            _step("start_group", "not_run"),
        ]
        return _job_answer("failed", None, item=created, vm_ids=vm_ids, steps=steps)
    try:
        item = await _start_group(connector, operator, target, new_id)
    except (httpx.HTTPStatusError, httpx.TransportError) as exc:
        status = "no_answer" if isinstance(exc, httpx.TransportError) else "failed"
        steps = [_step("create_group", "done"), _step("start_group", "failed", _vendor_error(exc))]
        return _job_answer(status, new_id, item=created, steps=steps)
    steps = [_step("create_group", "done"), _step("start_group", "done")]
    return _job_answer("accepted", new_id, item=item, steps=steps)


async def hcx_migration_validate_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.migration.validate`` -- ``POST /hybridity/api/mobility/groups/validate``.

    Validates an existing mobility group (by id) or a planned one (the same
    ``group`` input as ``hcx.migration.start``). HCX answers a failed check
    with HTTP 400 and the same body; that is a result, ``valid: false``.
    """
    group_id = params.get("mobility_group_id")
    item = {"migrationGroupId": group_id} if group_id else group_item(params["group"])
    try:
        payload = await connector._post_json(
            target, _GROUPS_VALIDATE_PATH, operator=operator, json={"items": [item]}
        )
        checked = True
    except httpx.HTTPStatusError as exc:
        payload = _json_or_none(exc.response)
        if exc.response.status_code != httpx.codes.BAD_REQUEST or not rows_of(payload):
            raise
        checked = False
    result = _first_item(payload)
    migrations = _migration_results(result)
    notes = _notes(result)
    valid = checked and not notes["errors"] and not any(m["errors"] for m in migrations)
    return _redact_object(
        {
            "valid": valid,
            "mobility_group_id": result.get("migrationGroupId") or group_id,
            **notes,
            "migrations": migrations,
        }
    )


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


async def hcx_migration_cancel_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.migration.cancel`` -- cancel, or force-cancel, migrations.

    Reads the migrations first; one that already ended is not sent and is
    listed under ``already_ended``. ``POST
    /hybridity/api/mobility/migrations/cancel`` (or ``forceCancel``) with
    ``{"items": [{"migrationId", "migrationGroupId"}]}``.
    """
    wanted = params["migrations"]
    ids = [entry["migration_id"] for entry in wanted]
    rows = await _query_migrations(connector, operator, target, {"migrationId": ids})
    ended = {
        row["migration_id"]: row["outcome"] for row in rows if row["outcome"] in _ENDED_OUTCOMES
    }
    to_cancel = [entry for entry in wanted if entry["migration_id"] not in ended]
    answer: dict[str, Any] = {
        "force": bool(params.get("force")),
        "already_ended": [
            {"migration_id": migration_id, "outcome": outcome}
            for migration_id, outcome in ended.items()
        ],
        "status_read": STATUS_READ,
        "status_read_params": {"migration_ids": ids},
    }
    if not to_cancel:
        return {"status": "already_ended", "migrations": [], "errors": [], "warnings": [], **answer}
    body = {
        "items": [
            {
                key: value
                for key, value in (
                    ("migrationId", entry["migration_id"]),
                    ("migrationGroupId", entry.get("mobility_group_id")),
                )
                if value
            }
            for entry in to_cancel
        ]
    }
    path = _FORCE_CANCEL_PATH if params.get("force") else _CANCEL_PATH
    payload = await connector._post_json(target, path, operator=operator, json=body)
    migrations = [
        {
            "migration_id": row.get("migrationId"),
            "mobility_group_id": row.get("migrationGroupId"),
            "vm_id": row.get("entityId"),
            **_notes(row),
        }
        for row in rows_of(payload)
    ]
    return _redact_object(
        {"status": "accepted", "migrations": migrations, **_notes(payload), **answer}
    )


async def hcx_mobility_group_list_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.mobility_group.list`` -- the mobility groups, or the summary of one group."""
    group_id = params.get("mobility_group_id")
    if group_id:
        path = _GROUP_V2_SUMMARY_PATH.format(mobilityGroupId=_segment("mobility group", group_id))
        payload = await connector._get_json(target, path, operator=operator)
        item = payload.get("item") if isinstance(payload, dict) else None
        rows = [group_row(item)] if isinstance(item, dict) else []
        return _redact_object({"items": rows, "page": None, "errors": []})
    group_filter: dict[str, Any] = {}
    if params.get("name"):
        group_filter["mobilityGroupName"] = params["name"]
    if params.get("state"):
        group_filter["mobilityGroupState"] = list(params["state"])
    body: dict[str, Any] = {"filter": group_filter}
    page = {
        vendor: params[name]
        for name, vendor in (("page_size", "pageSize"), ("page_number", "pageNumber"))
        if params.get(name) is not None
    }
    if page:
        body["pageParameters"] = page
    payload = await connector._post_json(
        target, _GROUPS_V2_QUERY_PATH, operator=operator, json=body
    )
    page_info = payload.get("pageParameters") if isinstance(payload, dict) else None
    return _redact_object(
        {
            "items": [group_row(item) for item in rows_of(payload)],
            "page": page_info if isinstance(page_info, dict) else None,
            "errors": _notes(payload)["errors"],
        }
    )


async def hcx_mobility_group_set_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.mobility_group.set`` -- make a mobility group, or replace the settings of one.

    ``POST /hybridity/api/mobility/groups`` without ``mobility_group_id``;
    ``PUT /hybridity/api/mobility/groups`` with the id in the body
    otherwise. Starts no migration.
    """
    item = group_item(params["group"])
    group_id = params.get("mobility_group_id")
    if group_id:
        item["migrationGroupId"] = group_id
    payload = await connector._post_json(
        target,
        _GROUPS_PATH,
        operator=operator,
        verb="PUT" if group_id else "POST",
        json={"items": [item]},
    )
    result = _first_item(payload)
    return _redact_object(
        {
            "created": not group_id,
            "mobility_group_id": result.get("migrationGroupId") or group_id,
            "name": result.get("name"),
            "state": result.get("state"),
            **_notes(result),
            "migrations": _migration_results(result),
        }
    )


async def hcx_migration_schedule_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.migration.schedule`` -- set or clear the switchover window of a mobility group.

    ``POST /hybridity/api/mobility/groups/{mobilityGroupId}/schedule`` with
    ``{"mobilityGroupDefaults": {"switchover": {...}, "transfer": {...}}}``;
    the times go as epoch milliseconds.
    """
    group_id = params["mobility_group_id"]
    defaults: dict[str, Any] = {"switchover": schedule_window("switchover", params["switchover"])}
    if params.get("transfer"):
        defaults["transfer"] = schedule_window("transfer", params["transfer"])
    path = _GROUP_SCHEDULE_PATH.format(mobilityGroupId=_segment("mobility group", group_id))
    payload = await connector._post_json(
        target, path, operator=operator, json={"mobilityGroupDefaults": defaults}
    )
    body = payload if isinstance(payload, dict) else {}
    return _redact_object(
        {
            "mobility_group_id": group_id,
            "switchover": defaults["switchover"],
            "transfer": defaults.get("transfer"),
            "affected_transfer_schedules": body.get("affectedTransferScheduleCount"),
            "affected_switchover_schedules": body.get("affectedSwitchoverScheduleCount"),
            "errors": schedule_errors(body.get("errors")),
        }
    )
