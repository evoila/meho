# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the SRM pairing, recovery plan, protection group, issue and history reads.

Seven reads on SRM 8.8, Live Site Recovery 9.0 and VCF 9.1 Protection and
Recovery, all through the SRM REST API v2:

* ``srm.pairing.list`` -- ``GET /pairings``;
* ``srm.remote_login`` -- ``POST /pairings/{pairing_id}/remote-session``
  with the remote site's account from Vault, then
  ``GET /pairings/{pairing_id}/remote-session``;
* ``srm.plan.list`` -- ``GET .../recovery-management/plans``, then per
  plan its protection groups and its newest run;
* ``srm.plan.get`` -- one plan, its protection groups, its recovery steps
  in one view mode, its VMs and (optional) one VM's recovery settings;
* ``srm.group.list`` -- ``GET .../protection-management/groups``, or the
  VMs of one group;
* ``srm.issue.list`` -- the issues of the pairing, of one plan, of one
  group or of one SRM server;
* ``srm.history.list`` -- the run history of a plan (the vendor builds the
  list in a task, which this read waits for), or one report.

The entry methods live on
:class:`~meho_backplane.connectors.srm.connector.SrmConnector` and call the
functions here, so an ``srm.*`` operation only binds to an SRM connector.
A list comes back as ``items`` (flat rows) next to a few scalars, so MEHO
stores a long list behind a result handle.

A vendor list is paged with ``limit`` and ``offset``. A read fetches every
page until the last one (a short page, or ``_meta.total`` rows). There is no
page cap: a vendor that ignores ``offset`` and sends the same page twice
stops the read with an error, so no row goes missing in silence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.connectors._shared.vault_creds import (
    load_param_secret_data,
    secret_ref_prefix,
    strip_credential_value,
)
from meho_backplane.connectors._shared.vcf_auth import basic_auth_header
from meho_backplane.connectors.srm.recovery_paths import (
    GROUP_ISSUES,
    GROUP_VMS,
    GROUPS,
    HISTORY_REPORT,
    HISTORY_REPORTS,
    PAIRING_ISSUES,
    PAIRINGS,
    PLAN,
    PLAN_GROUPS,
    PLAN_ISSUES,
    PLAN_STEPS,
    PLAN_VM_SETTINGS,
    PLAN_VMS,
    PLANS,
    REMOTE_SESSION,
    SRM_ISSUES,
    TASK,
)
from meho_backplane.connectors.vmware_rest.composites._guest_secret_ref import (
    check_guest_secret_ref,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.srm.connector import SrmConnector
    from meho_backplane.connectors.vmware_shared.dr_rest import DrTargetLike

__all__ = [
    "HISTORY_POLL_SECONDS",
    "HISTORY_WAIT_SECONDS",
    "NEEDS_CLEANUP_STATUSES",
    "REMOTE_SECRET_PREFIX_EXTRA",
    "group_list",
    "history_list",
    "issue_list",
    "pairing_list",
    "plan_get",
    "plan_list",
    "remote_login",
]

#: Per-target ``extras`` key that replaces the default allowed prefix of the
#: ``secret_ref`` that ``srm.remote_login`` takes. The default is the
#: namespace of the target's own ``secret_ref``.
REMOTE_SECRET_PREFIX_EXTRA: Final = "remote_secret_prefix"

#: Rows asked for per page.
PAGE_SIZE: Final = 100

#: How many plans ``srm.plan.list`` reads the groups and the newest run of at once.
PLAN_DETAIL_CONCURRENCY: Final = 4

#: How long ``srm.history.list`` waits for the vendor task that builds the
#: list, and how often it checks the task.
HISTORY_WAIT_SECONDS: Final = 60.0
HISTORY_POLL_SECONDS: Final = 1.0

#: Plan states after a test that still need a cleanup run.
NEEDS_CLEANUP_STATUSES: Final = frozenset(
    {"TEST_COMPLETE", "TEST_INTERRUPTED", "CLEANUP_INCOMPLETE", "CLEANUP_INTERRUPTED"}
)

_PLAN_FIELDS: Final = (
    "id",
    "name",
    "status",
    "is_running",
    "progress",
    "protected_site_name",
    "recovery_site_name",
    "location_name",
    "description",
)
_GROUP_FIELDS: Final = (
    "id",
    "name",
    "replication_type",
    "status",
    "recovery_status",
    "total_vms",
    "protected_site_name",
    "recovery_site_name",
    "location_name",
    "description",
)
_SERVER_FIELDS: Final = ("id", "name", "url", "server_status", "version", "build_number")
_SERVER_ROLES: Final = (
    ("local_srm", "local_srm_server"),
    ("remote_srm", "remote_srm_server"),
    ("local_vc", "local_vc_server"),
    ("remote_vc", "remote_vc_server"),
)
_ISSUE_SCOPES: Final = ("plan_id", "group_id", "srm_id")


def _seg(value: Any) -> str:
    """One path segment from an id the caller gave; never a ``/``."""
    return quote(str(value), safe="")


def _path(conn: SrmConnector, template: str, **ids: Any) -> str:
    """Full vendor path: the API base plus *template* with each id as one path segment."""
    return conn.api_base + template.format(**{name: _seg(value) for name, value in ids.items()})


def _iso(ms: Any) -> str | None:
    """Milliseconds since 1970 as an ISO 8601 UTC time; ``None`` when not a number."""
    if isinstance(ms, bool) or not isinstance(ms, int | float) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _ms(value: str, field: str) -> str:
    """An ISO 8601 time from the caller as milliseconds since 1970 (vendor query form)."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO 8601 time: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return str(int(moment.timestamp() * 1000))


def _pick(source: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    data = source if isinstance(source, dict) else {}
    return {field: data.get(field) for field in fields}


def _total(payload: Any) -> int | None:
    """``_meta.total`` of a vendor list payload; ``None`` when it is not given."""
    meta = payload.get("_meta") if isinstance(payload, dict) else None
    total = meta.get("total") if isinstance(meta, dict) else None
    return total if isinstance(total, int) and not isinstance(total, bool) else None


def _list_page(payload: Any) -> list[dict[str, Any]]:
    """The rows of one vendor list page (``{"list": [...], "_meta": {...}}``)."""
    page = payload.get("list") if isinstance(payload, dict) else None
    return [row for row in page if isinstance(row, dict)] if isinstance(page, list) else []


async def _all_pages(
    fetch: Callable[[int], Awaitable[Any]],
    rows_of: Callable[[Any], list[dict[str, Any]]],
    what: str,
) -> list[dict[str, Any]]:
    """Every row of a paged vendor list.

    *fetch* gets the offset of the next page and returns its payload;
    *rows_of* takes the rows out of a payload. The read ends at a short
    page, or when it holds ``_meta.total`` rows. A page equal to the one
    before it means the vendor ignored the offset; the read then stops with
    an error instead of returning part of the list as if it were complete.
    """
    rows: list[dict[str, Any]] = []
    previous: list[dict[str, Any]] | None = None
    while True:
        payload = await fetch(len(rows))
        page = rows_of(payload)
        if not page:
            return rows
        if page == previous:
            raise RuntimeError(
                f"SRM sent the same page of {what} twice (offset {len(rows)}); it ignored "
                "the offset, so the list cannot be read in full."
            )
        previous = page
        rows.extend(page)
        total = _total(payload)
        if len(page) < PAGE_SIZE or (total is not None and len(rows) >= total):
            return rows


async def _list_all(
    conn: SrmConnector, operator: Operator, target: DrTargetLike, path: str
) -> list[dict[str, Any]]:
    """Every row of a vendor list (``{"list": [...], "_meta": {"total"}}``), page by page."""

    async def fetch(offset: int) -> Any:
        return await conn._get_json(
            target,
            path,
            operator=operator,
            params={"limit": str(PAGE_SIZE), "offset": str(offset)},
        )

    return await _all_pages(fetch, _list_page, path.rsplit("/", 1)[-1])


# ---------------------------------------------------------------------------
# Pairings and the remote login
# ---------------------------------------------------------------------------


def _pairing_row(pairing: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"pairing_id": pairing.get("pairing_id")}
    for prefix, key in _SERVER_ROLES:
        server = pairing.get(key)
        for field, value in _pick(server, _SERVER_FIELDS).items():
            row[f"{prefix}_{'status' if field == 'server_status' else field}"] = value
    return row


async def pairing_list(
    conn: SrmConnector, operator: Operator, target: DrTargetLike
) -> dict[str, Any]:
    """``srm.pairing.list`` -- one row per pairing with both sites' servers and their status."""
    rows = await _list_all(conn, operator, target, _path(conn, PAIRINGS))
    items = [_pairing_row(row) for row in rows]
    return {"total": len(items), "items": items}


@dataclass(slots=True)
class _SecretRefTarget:
    """Points the shared Vault read at the path the call names (not the target's login)."""

    name: str
    host: str
    secret_ref: str | None


async def _remote_credentials(
    operator: Operator, target: DrTargetLike, secret_ref: str
) -> tuple[str, str]:
    """Read the remote site's account from *secret_ref*.

    The reference must fall under the target's allowed prefix: the
    namespace of the target's own ``secret_ref``, or
    ``extras["remote_secret_prefix"]`` when an operator set it. Any other
    reference is refused here, before any Vault read or request.
    """
    check_guest_secret_ref(
        secret_ref, target, param="secret_ref", extra_key=REMOTE_SECRET_PREFIX_EXTRA
    )
    data = await load_param_secret_data(
        _SecretRefTarget(name=target.name, host=target.host, secret_ref=secret_ref),
        operator,
        param="secret_ref",
    )
    username = strip_credential_value(data.get("username") or "")
    password = strip_credential_value(data.get("password") or "")
    if not username or not password:
        raise ValueError(
            f"the Vault secret at secret_ref={secret_ref_prefix(secret_ref)!r} needs the "
            "keys 'username' and 'password' with text. Nothing was sent."
        )
    return username, password


async def remote_login(
    conn: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.remote_login`` -- log in to the remote site of a pairing.

    The remote session belongs to this target's SRM session. A ``401`` may
    mean that the SRM session ended or that the remote site refused the
    account, so the read drops the SRM session, logs in again and tries
    once more. A second ``401`` is reported as a refused remote login.
    """
    pairing_id = params["pairing_id"]
    secret_ref = str(params["secret_ref"]).strip()
    username, password = await _remote_credentials(operator, target, secret_ref)
    path = _path(conn, REMOTE_SESSION, pairing_id=pairing_id)
    authorization = {"Authorization": basic_auth_header(username, password)}
    for attempt in range(2):
        try:
            await conn._post_json(
                target,
                path,
                operator=operator,
                extra_headers=authorization,
            )
            break
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != httpx.codes.UNAUTHORIZED:
                raise
            await conn.invalidate_session(target, rejected=exc.request)
            if attempt == 1:
                raise RuntimeError(
                    f"the remote site of pairing {pairing_id!r} refused the login (HTTP 401). "
                    "Check the account stored at secret_ref: it must be a vCenter single "
                    "sign-on account of the remote site."
                ) from None
    session = await conn._get_json(target, path, operator=operator)
    remote_user = session.get("username") if isinstance(session, dict) else None
    return {"pairing_id": pairing_id, "logged_in": True, "remote_username": remote_user}


# ---------------------------------------------------------------------------
# Recovery plans
# ---------------------------------------------------------------------------


def _plan_row(plan: dict[str, Any]) -> dict[str, Any]:
    row = _pick(plan, _PLAN_FIELDS)
    row["needs_cleanup"] = row["status"] in NEEDS_CLEANUP_STATUSES
    return row


def _last_run_columns(report: dict[str, Any] | None, error: str | None) -> dict[str, Any]:
    row = _report_row(report or {}, detail=False)
    return {
        "last_run_report_id": row["report_id"],
        "last_run_type": row["operation_type"],
        "last_run_result": row["result_state"],
        "last_run_start_time": row["start_time"],
        "last_run_errors": row["error_count"],
        "last_run_warnings": row["warning_count"],
        "last_run_error": error,
    }


async def _plan_details(
    conn: SrmConnector,
    operator: Operator,
    target: DrTargetLike,
    ids: dict[str, Any],
    gate: asyncio.Semaphore,
) -> dict[str, Any]:
    """The group names and ids of one plan, and the columns of its newest run.

    The newest run comes from the first page of the plan's history, asked
    for newest first; the read then takes the newest row of that page, so
    it is right even when SRM does not sort. A history task that fails or
    takes too long fills ``last_run_error`` instead of stopping the list.
    """
    async with gate:
        groups = await _list_all(conn, operator, target, _path(conn, PLAN_GROUPS, **ids))
        query = {
            "sort_by": "start_time_ms",
            "order_by": "desc",
            "limit": str(PAGE_SIZE),
            "offset": "0",
        }
        reports: list[dict[str, Any]] = []
        error: str | None = None
        try:
            task = await conn._get_json(
                target, _path(conn, HISTORY_REPORTS, **ids), operator=operator, params=query
            )
            task = await _wait_for_task(conn, operator, target, task)
            reports = _task_reports(task.get("result"))
        except RuntimeError as exc:
            error = str(exc)
    newest = max(reports, key=_report_start, default=None)
    return {
        "protection_group_ids": [group.get("id") for group in groups],
        "protection_group_names": [group.get("name") for group in groups],
        **_last_run_columns(newest, error),
    }


def _report_start(report: dict[str, Any]) -> float:
    start = report.get("start_time_ms")
    return float(start) if isinstance(start, int | float) and not isinstance(start, bool) else 0.0


async def plan_list(
    conn: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.plan.list`` -- one row per recovery plan with its state, groups and newest run."""
    pairing_id = params["pairing_id"]
    plans = await _list_all(conn, operator, target, _path(conn, PLANS, pairing_id=pairing_id))
    gate = asyncio.Semaphore(PLAN_DETAIL_CONCURRENCY)
    details = await asyncio.gather(
        *(
            _plan_details(
                conn, operator, target, {"pairing_id": pairing_id, "plan_id": plan["id"]}, gate
            )
            for plan in plans
        )
    )
    items = [{**_plan_row(plan), **detail} for plan, detail in zip(plans, details, strict=True)]
    return {"pairing_id": pairing_id, "total": len(items), "items": items}


def _step_row(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": step.get("id"),
        "step_number": step.get("step_number"),
        "title": step.get("title"),
        "status": step.get("status"),
        "depth": step.get("depth"),
        "child_count": step.get("child_count"),
        "progress": step.get("progress"),
        "start_time": _iso(step.get("start_time")),
        "finish_time": _iso(step.get("finish_time")),
        "warnings": step.get("warnings") or [],
        "errors": step.get("errors") or [],
    }


def _plan_vm_row(vm: dict[str, Any]) -> dict[str, Any]:
    settings = vm.get("recovery_settings")
    settings = settings if isinstance(settings, dict) else {}
    return {
        "id": vm.get("id"),
        "name": vm.get("name"),
        "status": vm.get("status"),
        "protection_group": vm.get("protection_group"),
        "protection_group_name": vm.get("protection_group_name"),
        "priority": settings.get("priority"),
        "startup_action": settings.get("startup_action"),
        "allow_migration": settings.get("allow_migration"),
        "dependent_vms": [
            dep.get("name") for dep in settings.get("dependent_vms") or [] if isinstance(dep, dict)
        ],
    }


async def _plan_steps(
    conn: SrmConnector, operator: Operator, target: DrTargetLike, ids: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None]:
    """The recovery steps in one view mode; the vendor refuses some modes in some plan states."""
    try:
        rows = await _list_all(conn, operator, target, _path(conn, PLAN_STEPS, **ids))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == httpx.codes.UNAUTHORIZED:
            raise
        return [], f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
    return [_step_row(row) for row in rows], None


async def plan_get(
    conn: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.plan.get`` -- one plan with its groups, steps, VMs and (optional) one VM's settings."""
    pairing_id = params["pairing_id"]
    plan_id = params["plan_id"]
    view_mode = params.get("view_mode") or "recovery"
    ids = {"pairing_id": pairing_id, "plan_id": plan_id}
    plan = await conn._get_json(target, _path(conn, PLAN, **ids), operator=operator)
    groups = await _list_all(conn, operator, target, _path(conn, PLAN_GROUPS, **ids))
    steps, steps_error = await _plan_steps(conn, operator, target, {**ids, "view_mode": view_mode})
    vms = await _list_all(conn, operator, target, _path(conn, PLAN_VMS, **ids))
    result: dict[str, Any] = {
        "pairing_id": pairing_id,
        "plan": _plan_row(plan),
        "protection_groups": [_pick(group, ("id", "name", "status")) for group in groups],
        "view_mode": view_mode,
        "steps": steps,
        "steps_error": steps_error,
        "vms": [_plan_vm_row(vm) for vm in vms],
        "vm_recovery_settings": None,
    }
    vm_id = params.get("vm_id")
    if vm_id:
        settings = await conn._get_json(
            target,
            _path(conn, PLAN_VM_SETTINGS, **ids, vm_id=vm_id),
            operator=operator,
        )
        settings = settings if isinstance(settings, dict) else {}
        result["vm_recovery_settings"] = {"vm_id": vm_id, **settings}
        result["vm_recovery_settings"].pop("_meta", None)
    return result


# ---------------------------------------------------------------------------
# Protection groups
# ---------------------------------------------------------------------------


def _group_vm_row(vm: dict[str, Any]) -> dict[str, Any]:
    info = vm.get("protection_info")
    info = info if isinstance(info, dict) else {}
    details = vm.get("protection_status_details")
    details = details if isinstance(details, dict) else {}
    folder = info.get("recovery_folder")
    pool = info.get("recovery_resource_pool")
    return {
        "id": vm.get("id"),
        "name": vm.get("name"),
        "protection_status": details.get("status"),
        "protection_errors": details.get("errors") or [],
        "recovery_folder": folder.get("name") if isinstance(folder, dict) else None,
        "recovery_resource_pool": pool.get("name") if isinstance(pool, dict) else None,
        "recovery_networks": [
            net.get("name") for net in info.get("recovery_networks") or [] if isinstance(net, dict)
        ],
        "vmotion_eligible": info.get("vmotion_eligible"),
    }


async def group_list(
    conn: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.group.list`` -- the protection groups, or the VMs of one group."""
    pairing_id = params["pairing_id"]
    group_id = params.get("group_id")
    if group_id:
        rows = await _list_all(
            conn, operator, target, _path(conn, GROUP_VMS, pairing_id=pairing_id, group_id=group_id)
        )
        items = [_group_vm_row(row) for row in rows]
        kind = "vms"
    else:
        items = [
            _pick(row, _GROUP_FIELDS)
            for row in await _list_all(
                conn, operator, target, _path(conn, GROUPS, pairing_id=pairing_id)
            )
        ]
        kind = "groups"
    return {
        "pairing_id": pairing_id,
        "group_id": group_id,
        "kind": kind,
        "total": len(items),
        "items": items,
    }


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


def _issue_path(conn: SrmConnector, params: dict[str, Any]) -> tuple[str, str]:
    """The vendor path of the issue list the call asks for, and its scope name."""
    given = [name for name in _ISSUE_SCOPES if params.get(name)]
    if len(given) > 1:
        raise ValueError(f"give at most one of plan_id, group_id, srm_id; got {given}")
    pairing_id = params["pairing_id"]
    if not given:
        return _path(conn, PAIRING_ISSUES, pairing_id=pairing_id), "pairing"
    scope = given[0]
    template, name = {
        "plan_id": (PLAN_ISSUES, "plan"),
        "group_id": (GROUP_ISSUES, "group"),
        "srm_id": (SRM_ISSUES, "srm"),
    }[scope]
    return _path(conn, template, pairing_id=pairing_id, **{scope: params[scope]}), name


async def issue_list(
    conn: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.issue.list`` -- health issues of a pairing, a plan, a group or an SRM server."""
    path, scope = _issue_path(conn, params)
    rows = await _list_all(conn, operator, target, path)
    items = [
        {
            "severity": row.get("status"),
            "issue_type": row.get("issue_type"),
            "entity": row.get("entity"),
            "entity_name": row.get("entity_name"),
            "description": row.get("description"),
            "time": _iso(row.get("timestamp")),
        }
        for row in rows
    ]
    return {
        "pairing_id": params["pairing_id"],
        "scope": scope,
        "total": len(items),
        "errors": sum(1 for item in items if item["severity"] == "ERROR"),
        "warnings": sum(1 for item in items if item["severity"] == "WARNING"),
        "items": items,
    }


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------


def _report_row(report: dict[str, Any], *, detail: bool) -> dict[str, Any]:
    run_key = report.get("run_key")
    row: dict[str, Any] = {
        "report_id": None if run_key is None else str(run_key),
        "plan_name": report.get("plan_name"),
        "operation_type": report.get("operation_type"),
        "result_state": report.get("result_state"),
        "username": report.get("username"),
        "start_time": _iso(report.get("start_time_ms")),
        "duration_ms": report.get("duration_ms"),
        "duration": report.get("human_friendly_duration"),
        "error_count": report.get("error_count"),
        "warning_count": report.get("warning_count"),
    }
    if detail:
        row["errors"] = report.get("errors_list") or []
        row["warnings"] = report.get("warnings_list") or []
    return row


def _task_reports(result: Any) -> list[dict[str, Any]]:
    """The history reports a finished task holds: a list, ``{"list": [...]}`` or one report."""
    if isinstance(result, dict) and isinstance(result.get("list"), list):
        result = result["list"]
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return []
    return [row for row in result if isinstance(row, dict)]


async def _wait_for_task(
    conn: SrmConnector, operator: Operator, target: DrTargetLike, task: dict[str, Any]
) -> dict[str, Any]:
    """Check ``GET /tasks/{id}`` until the task ends; raise when it fails or takes too long."""
    task_id = task.get("id") if isinstance(task, dict) else None
    if not task_id:
        raise RuntimeError("SRM answered the history read with no task id")
    waited = 0.0
    while task.get("status") in ("QUEUED", "RUNNING", None):
        if waited >= HISTORY_WAIT_SECONDS:
            raise RuntimeError(
                f"the SRM task {task_id!r} that builds the history list did not end within "
                f"{HISTORY_WAIT_SECONDS:.0f} s. Call srm.history.list again, with a shorter "
                "time range."
            )
        await asyncio.sleep(HISTORY_POLL_SECONDS)
        waited += HISTORY_POLL_SECONDS
        task = await conn._get_json(
            target,
            _path(conn, TASK, taskId=task_id),
            operator=operator,
        )
    if task.get("status") != "SUCCESS":
        error = task.get("error")
        error = error if isinstance(error, dict) else {}
        raise RuntimeError(
            f"the SRM task {task_id!r} that builds the history list ended with status "
            f"{task.get('status')!r}: {error.get('message') or 'no message'}"
        )
    return task


async def _history_reports(
    conn: SrmConnector,
    operator: Operator,
    target: DrTargetLike,
    ids: dict[str, Any],
    query: dict[str, str],
) -> list[dict[str, Any]]:
    """Every report of a plan's history. Each page is a vendor task the read waits for."""
    path = _path(conn, HISTORY_REPORTS, **ids)

    async def fetch(offset: int) -> Any:
        page_query = {**query, "limit": str(PAGE_SIZE), "offset": str(offset)}
        task = await conn._get_json(target, path, operator=operator, params=page_query)
        return (await _wait_for_task(conn, operator, target, task)).get("result")

    return await _all_pages(fetch, _task_reports, "history reports")


async def history_list(
    conn: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.history.list`` -- the run history of a plan, or one report with its errors."""
    pairing_id = params["pairing_id"]
    plan_id = params["plan_id"]
    ids = {"pairing_id": pairing_id, "plan_id": plan_id}
    report_id = params.get("report_id")
    if report_id:
        report = await conn._get_json(
            target,
            _path(conn, HISTORY_REPORT, **ids, report_id=report_id),
            operator=operator,
        )
        items = [_report_row(report, detail=True)]
    else:
        query: dict[str, str] = {}
        if params.get("start_time"):
            query["start_date"] = _ms(str(params["start_time"]), "start_time")
        if params.get("end_time"):
            query["end_date"] = _ms(str(params["end_time"]), "end_time")
        reports = await _history_reports(conn, operator, target, ids, query)
        reports.sort(key=_report_start, reverse=True)
        items = [_report_row(report, detail=False) for report in reports]
    return {
        "pairing_id": pairing_id,
        "plan_id": plan_id,
        "total": len(items),
        "items": items,
    }
