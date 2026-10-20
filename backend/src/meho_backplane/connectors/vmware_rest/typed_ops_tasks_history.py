# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.tasks.history`` typed op (#3701).

The time-window sibling of ``vmware.tasks.recent``. ``recentTask`` keeps
only the last few minutes of tasks, and classic vim tasks (an OVF deploy
from a content library, a clone, a relocate) are not CIS task providers,
so ``/cis/tasks`` cannot see them either. The only source for an older
task is a ``TaskHistoryCollector``.

A collector is server-side state, so the op owns its whole life inside one
dispatch, directly on the connector session (no ``dispatch_child``, no
ingested descriptor, zero catalog ingest):

1. ``TaskManager.CreateCollectorForTasks`` with a ``TaskFilterSpec`` built
   from the params (queued-time window, optional entity).
2. ``TaskHistoryCollector.ReadNextTasks`` page by page. A new collector
   starts at the oldest item, so pages run oldest to newest.
3. ``TaskHistoryCollector.DestroyCollector`` on the success path and on
   the error path.

The op returns only when the collector is confirmed gone. A failed
destroy is retried (``_DESTROY_ATTEMPTS`` tries in all). When it keeps
failing, the op closes the vCenter session that owns the collector
(:meth:`VmwareRestConnector.close_session`): a collector dies with its
session, and the next call logs in again on a clean session, so leaked
collectors cannot pile up on the cached session. ``collector_cleanup``
reports which path ran. When neither path is confirmed, the call fails
with :class:`CollectorCleanupError` (a read error that happened first is
raised instead, and the cleanup failure is only logged).

``TaskFilterSpec`` has no operation filter, so ``description_ids`` is
applied here, on the rows read. The scan is bounded by
``_MAX_PAGES`` pages.

Rows use the same shape as ``vmware.tasks.recent`` (one shared parser).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent import (
    _moref_value,
    _parse_task_info,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    method_fault_type,
    unwrap_vim_value,
    vim_moref,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "TASKS_HISTORY_GROUP_KEY",
    "TASKS_HISTORY_WHEN_TO_USE",
    "VMWARE_TASKS_HISTORY_OP",
    "CollectorCleanupError",
    "build_task_filter_spec",
    "tasks_history_impl",
]

_log = structlog.get_logger(__name__)

# The TaskManager is a singleton whose moId is 'TaskManager'.
_CREATE_COLLECTOR_PATH = "/TaskManager/TaskManager/CreateCollectorForTasks"
_COLLECTOR_MO_TYPE = "TaskHistoryCollector"

#: Rows asked for per ``ReadNextTasks`` call.
_PAGE_SIZE = 100
#: Upper bound on pages read in one dispatch (5,000 rows scanned at most).
_MAX_PAGES = 50
_DEFAULT_MAX_TASKS = 200
_MAX_TASKS_CAP = 1000
#: ``DestroyCollector`` tries before the op falls back to closing the session.
_DESTROY_ATTEMPTS = 3
#: Pause between two ``DestroyCollector`` tries.
_DESTROY_RETRY_DELAY_SECONDS = 0.5

CollectorCleanup = Literal["destroyed", "session_closed"]

TASKS_HISTORY_GROUP_KEY = "vmware-tasks-history"


class CollectorCleanupError(RuntimeError):
    """The temporary task history collector could not be removed.

    Raised after every ``DestroyCollector`` try failed and the vCenter
    session that owns the collector could not be closed either. The
    collector then lives until vCenter times out that session.
    """


def _parse_time(value: Any, name: str) -> datetime:
    """Parse an ISO-8601 time; a time without a zone is read as UTC."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"vmware.tasks.history: {name!r} must be an ISO-8601 date-time string")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(
            f"vmware.tasks.history: {name!r} is not an ISO-8601 date-time: {value!r}"
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _wire_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_task_filter_spec(params: dict[str, Any]) -> dict[str, Any]:
    """Build the ``TaskFilterSpec`` for ``CreateCollectorForTasks``.

    The window filters on ``queuedTime`` (when the task was created). An
    entity filter needs both ``entity`` (moid) and ``entity_type``.
    Raises :exc:`ValueError` on a bad time or a window that ends before it
    starts.
    """
    since = _parse_time(params.get("since"), "since")
    time_spec: dict[str, Any] = {
        VIM_TYPE_NAME_KEY: "TaskFilterSpecByTime",
        "timeType": "queuedTime",
        "beginTime": _wire_time(since),
    }
    if params.get("until") is not None:
        until = _parse_time(params.get("until"), "until")
        if until <= since:
            raise ValueError("vmware.tasks.history: 'until' must be later than 'since'")
        time_spec["endTime"] = _wire_time(until)
    spec: dict[str, Any] = {VIM_TYPE_NAME_KEY: "TaskFilterSpec", "time": time_spec}
    entity = params.get("entity")
    if entity:
        entity_type = params.get("entity_type")
        if not isinstance(entity_type, str) or not entity_type:
            raise ValueError("vmware.tasks.history: 'entity' needs 'entity_type'")
        spec["entity"] = {
            VIM_TYPE_NAME_KEY: "TaskFilterSpecByEntity",
            "entity": vim_moref(entity_type, str(entity)),
            "recursion": "all" if params.get("include_children") is True else "self",
        }
    return spec


def _page_rows(page: Any) -> list[Any]:
    """The ``TaskInfo`` list of one ``ReadNextTasks`` answer (``null`` is empty)."""
    rows = unwrap_vim_value(_unwrap_value(page))
    return rows if isinstance(rows, list) else []


def _history_row(info: Any) -> dict[str, Any]:
    ti = info if isinstance(info, dict) else {}
    task_moid = _moref_value(ti.get("task")) or ti.get("key")
    return _parse_task_info(str(task_moid) if task_moid is not None else "", ti)


async def _read_pages(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    read_path: str,
    *,
    max_tasks: int,
    description_ids: set[str] | None,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Page the collector; return ``(rows, truncated, scanned)``."""
    rows: list[dict[str, Any]] = []
    scanned = 0
    for _ in range(_MAX_PAGES):
        page = _page_rows(
            await connector._post_vmomi_json(
                target, read_path, operator=operator, json={"maxCount": _PAGE_SIZE}
            )
        )
        scanned += len(page)
        for info in page:
            row = _history_row(info)
            if description_ids is not None and row["operation"] not in description_ids:
                continue
            if len(rows) == max_tasks:
                return rows, True, scanned
            rows.append(row)
        if len(page) < _PAGE_SIZE:
            return rows, False, scanned
    # Page budget used up with full pages: more rows may exist.
    return rows, True, scanned


def _collector_already_gone(exc: Exception) -> bool:
    """``DestroyCollector`` faulted because the collector no longer exists."""
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == 500
        and method_fault_type(exc.response.text) == "ManagedObjectNotFound"
    )


async def _destroy_collector(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    collector_moid: str,
) -> bool:
    """Call ``DestroyCollector`` up to ``_DESTROY_ATTEMPTS`` times."""
    path = f"/{_COLLECTOR_MO_TYPE}/{collector_moid}/DestroyCollector"
    for attempt in range(1, _DESTROY_ATTEMPTS + 1):
        try:
            await connector._post_vmomi_json(target, path, operator=operator)
            return True
        except Exception as exc:
            if _collector_already_gone(exc):
                return True
            _log.warning(
                "vmware_tasks_history_destroy_failed",
                target=target.name,
                collector=collector_moid,
                attempt=attempt,
                attempts=_DESTROY_ATTEMPTS,
                error_type=type(exc).__name__,
            )
        if attempt < _DESTROY_ATTEMPTS:
            await asyncio.sleep(_DESTROY_RETRY_DELAY_SECONDS)
    return False


async def _remove_collector(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    collector_moid: str,
) -> CollectorCleanup | None:
    """Make sure the collector is gone; ``None`` when that cannot be confirmed.

    First ``DestroyCollector`` (with retries). When that keeps failing,
    close the vCenter session that owns the collector, so vCenter drops it
    now and the next call on the target starts on a clean session.
    """
    if await _destroy_collector(connector, operator, target, collector_moid):
        return "destroyed"
    if await connector.close_session(target):
        _log.warning(
            "vmware_tasks_history_session_closed",
            target=target.name,
            collector=collector_moid,
        )
        return "session_closed"
    _log.error(
        "vmware_tasks_history_collector_leaked",
        target=target.name,
        collector=collector_moid,
    )
    return None


async def tasks_history_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.tasks.history``: create, page, destroy.

    The collector is removed on the success path and on the error path.
    Returns ``{"tasks": [...], "truncated", "scanned", "collector_cleanup"}``,
    oldest task first. Raises :class:`CollectorCleanupError` when the read
    worked but the collector could not be confirmed gone; a read error wins
    over a cleanup error.
    """
    filter_spec = build_task_filter_spec(params)
    max_tasks = params.get("max_tasks")
    if not isinstance(max_tasks, int) or isinstance(max_tasks, bool) or max_tasks < 1:
        max_tasks = _DEFAULT_MAX_TASKS
    raw_ids = params.get("description_ids")
    description_ids = {str(d) for d in raw_ids} if isinstance(raw_ids, list) and raw_ids else None

    created = await connector._post_vmomi_json(
        target, _CREATE_COLLECTOR_PATH, operator=operator, json={"filter": filter_spec}
    )
    collector_moid = _moref_value(unwrap_vim_value(_unwrap_value(created)))
    if collector_moid is None:
        raise RuntimeError(
            "vmware.tasks.history: CreateCollectorForTasks returned no collector reference"
        )

    try:
        rows, truncated, scanned = await _read_pages(
            connector,
            operator,
            target,
            f"/{_COLLECTOR_MO_TYPE}/{collector_moid}/ReadNextTasks",
            max_tasks=max_tasks,
            description_ids=description_ids,
        )
    except Exception:
        # The read error is the one to report; the cleanup logs its own outcome.
        await _remove_collector(connector, operator, target, collector_moid)
        raise
    cleanup = await _remove_collector(connector, operator, target, collector_moid)
    if cleanup is None:
        raise CollectorCleanupError(
            "vmware.tasks.history: the temporary task history collector could not "
            f"be removed: DestroyCollector failed {_DESTROY_ATTEMPTS} times and the "
            "vCenter session that owns it could not be closed. vCenter drops the "
            "collector when that session times out. Try again later."
        )

    _log.info(
        "vmware_tasks_history_read",
        target=target.name,
        task_count=len(rows),
        scanned=scanned,
        truncated=truncated,
        collector_cleanup=cleanup,
    )
    return {
        "tasks": rows,
        "truncated": truncated,
        "scanned": scanned,
        "collector_cleanup": cleanup,
    }


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "since": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Start of the time window, ISO-8601 (e.g. '2026-10-20T08:00:00Z'). "
                "Matches on when the task was queued. A time without a zone is UTC."
            ),
        },
        "until": {
            "type": "string",
            "minLength": 1,
            "description": (
                "End of the time window, ISO-8601. Omit to read up to now. Must be "
                "later than 'since'."
            ),
        },
        "entity": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional moid of the object the task ran on (e.g. 'vm-42', "
                "'resgroup-8'). Needs 'entity_type'."
            ),
        },
        "entity_type": {
            "type": "string",
            "minLength": 1,
            "description": (
                "vim type of 'entity' (e.g. 'VirtualMachine', 'ResourcePool', "
                "'HostSystem', 'ClusterComputeResource', 'Datacenter')."
            ),
        },
        "include_children": {
            "type": "boolean",
            "description": (
                "With 'entity': also return tasks on its child objects. Default false "
                "(the entity itself only)."
            ),
        },
        "description_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "description": (
                "Optional operation ids to keep (TaskInfo.descriptionId, e.g. "
                "'ResourcePool.ImportVApp', 'VirtualMachine.clone'). Filtered by "
                "MEHO after the read, not by vCenter."
            ),
        },
        "max_tasks": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_TASKS_CAP,
            "description": (
                f"Maximum rows returned (1-{_MAX_TASKS_CAP}; default {_DEFAULT_MAX_TASKS})."
            ),
        },
    },
    "required": ["since"],
    "dependentRequired": {"entity": ["entity_type"]},
    "additionalProperties": False,
    "examples": [
        {"since": "2026-10-20T08:00:00Z", "until": "2026-10-20T10:00:00Z"},
        {
            "since": "2026-10-20T08:00:00Z",
            "entity": "resgroup-8",
            "entity_type": "ResourcePool",
            "description_ids": ["ResourcePool.ImportVApp"],
        },
    ],
}

_TASK_ROW_PROPERTIES: dict[str, Any] = {
    "task": {"type": "string", "description": "Task moid, or the task key."},
    "operation": {
        "type": ["string", "null"],
        "description": "Operation identifier (descriptionId).",
    },
    "entity": {"type": ["string", "null"], "description": "Target entity moid."},
    "entity_type": {"type": ["string", "null"], "description": "Target entity type."},
    "entity_name": {"type": ["string", "null"], "description": "Target entity name."},
    "state": {
        "type": ["string", "null"],
        "description": "Task state: queued / running / success / error.",
    },
    "progress": {"type": ["integer", "null"], "description": "Percent complete 0-100."},
    "cancelled": {
        "type": ["boolean", "null"],
        "description": "Whether cancellation was requested.",
    },
    "queue_time": {"type": ["string", "null"], "description": "When the task was queued."},
    "start_time": {"type": ["string", "null"], "description": "When the task started."},
    "complete_time": {"type": ["string", "null"], "description": "When the task completed."},
    "error_message": {
        "type": ["string", "null"],
        "description": "Fault message when state is error.",
    },
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _TASK_ROW_PROPERTIES,
                "required": ["task"],
            },
            "description": "Tasks in the window, oldest first.",
        },
        "truncated": {
            "type": "boolean",
            "description": "True when more tasks may exist than were returned.",
        },
        "scanned": {
            "type": "integer",
            "description": "Task rows read from vCenter before the operation filter.",
        },
        "collector_cleanup": {
            "type": "string",
            "enum": ["destroyed", "session_closed"],
            "description": (
                "How the temporary history collector was removed: 'destroyed' "
                "(DestroyCollector accepted) or 'session_closed' (destroy kept "
                "failing, so MEHO closed the vCenter session that owned it)."
            ),
        },
    },
    "required": ["tasks", "truncated", "scanned", "collector_cleanup"],
}

#: Curated ``when_to_use`` blurb for the tasks-history group.
TASKS_HISTORY_WHEN_TO_USE = (
    "Use to find vCenter tasks in a past time window: tasks that already "
    "dropped out of the recent list (tasks.recent shows only the last few "
    "minutes). Covers classic tasks such as OVF deploys, clones and "
    "relocates, which the CIS task API cannot see. Reads through a "
    "temporary task history collector that MEHO removes before it "
    "returns. Read-only."
)

VMWARE_TASKS_HISTORY_OP = VmwareTypedOp(
    op_id="vmware.tasks.history",
    handler_attr="tasks_history",
    summary="vCenter tasks in a past time window, with state, timing and error text.",
    description=(
        "Returns vCenter tasks queued in a time window, oldest first, with "
        "state, progress, timing and error text. For tasks older than the "
        "recent list. Uses a temporary task history collector and removes it "
        "before returning. vCenter only. safety_level=safe, read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=TASKS_HISTORY_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "tasks", "history", "rca"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when you need a task that finished more than a few minutes "
            "ago, for example to find why an OVF deploy, clone or relocate "
            "failed. For tasks from the last few minutes, tasks.recent is "
            "cheaper. Keep the window narrow: vCenter keeps task history only "
            "as long as its own settings allow, and a wide window can hit the "
            "row limit (truncated=true)."
        ),
        "parameter_hints": {
            "since": "ISO-8601 start of the window (when the task was queued).",
            "until": "ISO-8601 end of the window; omit for now.",
            "entity": "Moid of the object the task ran on; give entity_type too.",
            "description_ids": "Operation ids to keep, e.g. ResourcePool.ImportVApp.",
            "max_tasks": f"Row cap (1-{_MAX_TASKS_CAP}; default {_DEFAULT_MAX_TASKS}).",
        },
        "output_shape": (
            "{tasks: [{task, operation, entity, entity_type, entity_name, "
            "state, progress, cancelled, queue_time, start_time, "
            "complete_time, error_message}, ...], truncated, scanned, "
            "collector_cleanup}. A long list comes back as a result handle; "
            "read it with result_query."
        ),
        "next_step": (
            "If truncated is true, narrow the window or add entity / "
            "description_ids and call again. For a failed task, read "
            "error_message, then inspect the entity (for example with "
            "object.collect or vm.info)."
        ),
    },
)
