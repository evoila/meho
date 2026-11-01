# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the fleet-lcm lifecycle changes and of the task status read.

The changes (metadata in :mod:`.lifecycle_ops`):

* ``fleet-lcm.task.retry`` / ``.task.cancel`` -- ``POST /v1/tasks/{taskId}``
  with ``?action=retry`` or ``?action=cancel``.
* ``fleet-lcm.upgrade-plan.create`` -- two steps: ``POST
  /v1/upgrade-plans/validations`` (a vendor job; MEHO waits a short time for
  it), then ``POST /v1/upgrade-plans`` only when the check passed.
* ``fleet-lcm.upgrade-plan.precheck`` / ``.apply`` -- read the plan first; when
  a run of the same kind still runs, return its task id and start nothing.
  Else ``POST /v1/upgrade-plans/{planId}`` with ``?action=precheck`` or
  ``?action=apply``.
* ``fleet-lcm.depot.sync`` -- ``POST /v1/depot-metadata?action=sync``. The
  vendor itself returns the running sync when one runs.

Every vendor job answers ``accepted`` with the vendor's task id and names the
status read, ``fleet-lcm.task.info``. That read (:func:`task_status_impl`)
adds a plain state, the stage progress and the error texts to the vendor
task, and says ``not_reachable`` (never ``failed``) when the service does not
answer. MEHO keeps no job state between calls; the caller keeps the task id.

"Does not answer" (:func:`_unreachable_reason`) means a transport error, HTTP
502 / 503 / 504, or a new login that met one of those at the login service.
A refused login (401 / 403) is a real error and is raised as before. The
plan create reports the same way: a check that cannot be followed keeps its
task id, and a create with no answer says the outcome is unknown.

A ``401`` is not caught here: it reaches the dispatcher, which logs in again
and runs the call once more.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError, SessionLoginError
from meho_backplane.connectors.fleet_lcm._paths import (
    _DEPOT_SYNC_POST_PATH,
    _TASK_CANCEL_POST_PATH,
    _TASK_PATH,
    _TASK_RETRY_POST_PATH,
    _UPGRADE_PLAN_APPLY_POST_PATH,
    _UPGRADE_PLAN_PATH,
    _UPGRADE_PLAN_PRECHECK_POST_PATH,
    _UPGRADE_PLAN_VALIDATIONS_POST_PATH,
    _UPGRADE_PLANS_POST_PATH,
    encode_segment,
    fill_path,
)
from meho_backplane.connectors.fleet_lcm.session import FleetLcmTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.fleet_lcm.connector import FleetLcmConnector

__all__ = [
    "STATUS_READ_OP_ID",
    "depot_sync_impl",
    "task_cancel_impl",
    "task_retry_impl",
    "task_status_impl",
    "upgrade_plan_apply_impl",
    "upgrade_plan_create_impl",
    "upgrade_plan_precheck_impl",
]

#: The read that follows every fleet job.
STATUS_READ_OP_ID: Final[str] = "fleet-lcm.task.info"

#: Vendor task status -> MEHO's plain state.
_STATE_BY_STATUS: Final[dict[str, str]] = {
    "PENDING": "queued",
    "SCHEDULED": "queued",
    "RUNNING": "running",
    "SUCCEEDED": "succeeded",
    "FAILED": "failed",
    "CANCELED": "canceled",
}
_ACTIVE_STATES: Final[frozenset[str]] = frozenset({"queued", "running"})
_DONE_STAGE_STATUSES: Final[frozenset[str]] = frozenset({"SUCCEEDED", "SKIPPED"})
#: Answers that mean "the service is not there now" (for example during its
#: own upgrade), not "the job failed".
_UNREACHABLE_HTTP: Final[frozenset[int]] = frozenset({502, 503, 504})
#: Statuses MEHO passes on to the dispatcher (new login, missing right).
_DISPATCHER_HTTP: Final[frozenset[int]] = frozenset({401, 403})
#: The plan run type of each plan action (``UpgradePlanExecution.type``).
_EXECUTION_TYPE: Final[dict[str, str]] = {"precheck": "PRECHECK", "apply": "UPGRADE"}

#: How long ``upgrade-plan.create`` waits for the vendor's plan check, and how
#: often it reads it. Tests set both to 0.
VALIDATION_WAIT_SECONDS: float = 60.0
VALIDATION_POLL_SECONDS: float = 2.0

_ACCEPTED_NOTE = (
    "The vendor took the job. It is not done yet. Read fleet-lcm.task.info with this "
    "task_id until state is succeeded, failed or canceled."
)
_ALREADY_RUNNING_NOTE = (
    "A job of the same kind already runs on this plan. MEHO started nothing. Follow "
    "the running job with fleet-lcm.task.info."
)
_NOT_REACHABLE_NOTE = (
    "The fleet lifecycle service, or the login service, did not answer. This does not "
    "mean the job failed. Keep the task id and read again in a few minutes."
)
#: What to do next after each outcome of ``upgrade-plan.create``.
_CREATE_NOTES: Final[dict[str, str]] = {
    "created": "The plan exists. Run fleet-lcm.upgrade-plan.precheck with plan_id.",
    "validation_failed": (
        "The vendor's check refused the spec. Nothing was created. Fix the spec from the "
        "errors and call again."
    ),
    "validation_still_running": (
        "The check still runs. Nothing was created. Follow its task_id with "
        "fleet-lcm.task.info, then call again."
    ),
    "validation_not_reachable": (
        "The service stopped answering while MEHO followed the check. Nothing was created. "
        "Keep the check's task_id, read it with fleet-lcm.task.info later, then call again."
    ),
    "create_failed": "The check passed, but the vendor refused the create. Nothing was created.",
    "create_unknown": (
        "The check passed, but the create got no answer. The plan may or may not exist. "
        "Read fleet-lcm.upgrade-plan.list before you call again."
    ),
}


def _now() -> str:
    """The time now, ISO 8601 in UTC."""
    return datetime.now(UTC).isoformat()


def _message_text(entry: Any) -> str | None:
    """The text of one vendor message entry (``{level, message: {...}}``)."""
    message = entry.get("message") if isinstance(entry, dict) else None
    if not isinstance(message, dict):
        return None
    text = message.get("localizedMessage") or message.get("defaultMessage")
    return text if isinstance(text, str) and text else None


def _error_texts(entries: Any) -> list[str]:
    """The texts of the ``ERROR`` entries in a vendor message list."""
    if not isinstance(entries, list):
        return []
    found = (_message_text(e) for e in entries if isinstance(e, dict) and e.get("level") == "ERROR")
    return [text for text in found if text]


def _task_errors(task: dict[str, Any]) -> list[str]:
    """Every ``ERROR`` text of a task: its own, its stages' and its sub-tasks'."""
    errors = _error_texts(task.get("messages"))
    for stage in task.get("stages") or []:
        if isinstance(stage, dict):
            errors.extend(_error_texts(stage.get("messages")))
    for sub_task in task.get("subTasks") or []:
        if isinstance(sub_task, dict):
            errors.extend(_task_errors(sub_task))
    return list(dict.fromkeys(errors))


def _task_progress(task: dict[str, Any]) -> dict[str, Any] | None:
    """Stage progress of a task, or ``None`` when the vendor lists no stages."""
    stages = [s for s in task.get("stages") or [] if isinstance(s, dict)]
    if not stages:
        return None
    running = next((s.get("name") for s in stages if s.get("status") == "RUNNING"), None)
    return {
        "stages_total": len(stages),
        "stages_done": sum(1 for s in stages if s.get("status") in _DONE_STAGE_STATUSES),
        "current_stage": running,
    }


def _state(status: Any) -> str:
    """MEHO's plain state for a vendor task status."""
    return _STATE_BY_STATUS.get(str(status).upper(), "unknown") if status else "unknown"


def _status_read(task_id: str | None) -> dict[str, Any]:
    """How the caller follows a job."""
    return {"op_id": STATUS_READ_OP_ID, "params": {"taskId": task_id}}


async def _read_task(
    connector: FleetLcmConnector, operator: Operator, target: FleetLcmTargetLike, task_id: str
) -> dict[str, Any]:
    path = fill_path(_TASK_PATH, {"taskId": encode_segment(task_id)})
    return await connector._get_json(target, path, operator=operator)


def _unreachable_reason(exc: BaseException) -> str | None:
    """Why the service gave no answer, or ``None`` when *exc* is a real error.

    No answer: a transport error, HTTP 502 / 503 / 504, or a new login that
    met one of those at the login service (a :class:`SessionLoginError` whose
    cause is such an HTTP error). A refused login (401 / 403,
    :class:`ConnectorAuthError`) is a real error, as is every other status.
    """
    if isinstance(exc, httpx.TransportError):
        return type(exc).__name__
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return f"HTTP {code}" if code in _UNREACHABLE_HTTP else None
    if isinstance(exc, SessionLoginError) and not isinstance(exc, ConnectorAuthError):
        cause = exc.__cause__
        if isinstance(cause, httpx.HTTPStatusError):
            code = cause.response.status_code
            return f"login HTTP {code}" if code in _UNREACHABLE_HTTP else None
    return None


def _not_reachable(task_id: str, reason: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "state": "not_reachable",
        "progress": None,
        "errors": [],
        "reachable": False,
        "unreachable_reason": reason,
        "checked_at": _now(),
        "note": _NOT_REACHABLE_NOTE,
    }


async def task_status_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.task.info`` -- the vendor task plus state, progress and errors.

    No answer (see :func:`_unreachable_reason`: a transport error, HTTP 502 /
    503 / 504, or a login service that answers so when the token is renewed)
    gives ``state: not_reachable`` with the task id and the time. Other errors
    raise, so the dispatcher reports them (and logs in again on a ``401``).
    """
    task_id = str(params["taskId"])
    try:
        task = await _read_task(connector, operator, target, task_id)
    except (httpx.HTTPError, SessionLoginError) as exc:
        reason = _unreachable_reason(exc)
        if reason is None:
            raise
        return _not_reachable(task_id, reason)
    return {
        **task,
        "task_id": task.get("id") or task_id,
        "state": _state(task.get("status")),
        "progress": _task_progress(task),
        "errors": _task_errors(task),
        "reachable": True,
        "unreachable_reason": None,
        "checked_at": _now(),
    }


def _job_answer(
    task: dict[str, Any],
    *,
    action: str,
    fallback_task_id: str | None = None,
    started: bool = True,
) -> dict[str, Any]:
    """The answer of a start: ``accepted`` (or ``already_running``), never done."""
    task_id = task.get("id") or task.get("taskId") or fallback_task_id
    return {
        "status": "accepted" if started else "already_running",
        "started": started,
        "action": action,
        "task_id": task_id,
        "vendor_status": task.get("status"),
        "status_read": _status_read(task_id),
        "note": _ACCEPTED_NOTE if started else _ALREADY_RUNNING_NOTE,
    }


async def _post(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await connector._post_json(target, path, operator=operator, json=body)


async def task_retry_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.task.retry`` -- retry a failed task from the stage that failed."""
    task_id = str(params["taskId"])
    path = fill_path(_TASK_RETRY_POST_PATH, {"taskId": encode_segment(task_id)})
    task = await _post(connector, operator, target, path)
    return _job_answer(task, action="retry", fallback_task_id=task_id)


async def task_cancel_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.task.cancel`` -- the running stage ends, later stages do not run."""
    task_id = str(params["taskId"])
    path = fill_path(_TASK_CANCEL_POST_PATH, {"taskId": encode_segment(task_id)})
    task = await _post(connector, operator, target, path)
    return _job_answer(task, action="cancel", fallback_task_id=task_id)


def _running_execution(plan: dict[str, Any], execution_type: str) -> dict[str, Any] | None:
    """A run of *execution_type* on the plan that has not ended, if any."""
    for execution in plan.get("executions") or []:
        if not isinstance(execution, dict):
            continue
        if str(execution.get("type", "")).upper() != execution_type:
            continue
        if _state(execution.get("status")) in _ACTIVE_STATES:
            return execution
    return None


async def _plan_action(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
    action: Literal["precheck", "apply"],
) -> dict[str, Any]:
    plan_segment = {"planId": encode_segment(params["planId"])}
    plan = await connector._get_json(
        target, fill_path(_UPGRADE_PLAN_PATH, plan_segment), operator=operator
    )
    running = _running_execution(plan, _EXECUTION_TYPE[action])
    if running is not None:
        return _job_answer(running, action=action, started=False)
    template = (
        _UPGRADE_PLAN_PRECHECK_POST_PATH if action == "precheck" else _UPGRADE_PLAN_APPLY_POST_PATH
    )
    body: dict[str, Any] = {}
    if params.get("componentsFilter"):
        body["componentsFilter"] = params["componentsFilter"]
    task = await _post(connector, operator, target, fill_path(template, plan_segment), body)
    return _job_answer(task, action=action)


async def upgrade_plan_precheck_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.upgrade-plan.precheck`` -- start the pre-check of a plan."""
    return await _plan_action(connector, operator, target, params, "precheck")


async def upgrade_plan_apply_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.upgrade-plan.apply`` -- start the upgrades of a plan."""
    return await _plan_action(connector, operator, target, params, "apply")


async def depot_sync_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.depot.sync`` -- sync the depot metadata now.

    The vendor runs one sync at a time and returns the running sync's task
    when one runs, so a second call starts nothing new.
    """
    del params  # schema declares the param object empty
    task = await _post(connector, operator, target, _DEPOT_SYNC_POST_PATH)
    return _job_answer(task, action="sync")


# ---------------------------------------------------------------------------
# upgrade-plan.create: check the plan, then create it
# ---------------------------------------------------------------------------


def _vendor_error(exc: httpx.HTTPStatusError) -> str:
    """The vendor's error text of a failed call (``ErrorResponse.message``)."""
    response = exc.response
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        text = _message_text(payload)
        if text:
            return f"HTTP {response.status_code}: {text}"
    return f"HTTP {response.status_code}: {response.text[:500]}"


def _validation_findings(task: dict[str, Any], severity: str) -> list[str]:
    """The ``failedValidations`` texts of *severity* in a plan-check task."""
    result = task.get("result")
    found = result.get("failedValidations") if isinstance(result, dict) else None
    texts: list[str] = []
    for item in found if isinstance(found, list) else []:
        if isinstance(item, dict) and item.get("severity") == severity:
            texts.append(_message_text(item) or str(item.get("errorCode") or "unnamed check"))
    return texts


async def _wait_for_check(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    task: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Read the plan-check task until it ends or the short wait is over.

    Returns the last task read and, when the service stopped answering
    during the wait, why (else ``None``). The task keeps its id either way.
    """
    task_id = task.get("id")
    deadline = time.monotonic() + VALIDATION_WAIT_SECONDS
    while task_id and _state(task.get("status")) in _ACTIVE_STATES:
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(VALIDATION_POLL_SECONDS)
        try:
            task = await _read_task(connector, operator, target, str(task_id))
        except (httpx.HTTPError, SessionLoginError) as exc:
            reason = _unreachable_reason(exc)
            if reason is None:
                raise
            return task, reason
    return task, None


async def _create_step(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    spec: dict[str, Any],
    step: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Step 2, the create. Returns the outcome, the step report and the plan.

    No answer gives ``unknown``: the plan may or may not exist. A vendor
    refusal gives ``failed`` with the vendor's text. A ``401`` / ``403`` and a
    refused login raise, so the dispatcher handles them.
    """
    try:
        plan = await _post(connector, operator, target, _UPGRADE_PLANS_POST_PATH, {"spec": spec})
    except (httpx.HTTPError, SessionLoginError) as exc:
        reason = _unreachable_reason(exc)
        if reason is not None:
            return (
                "create_unknown",
                {**step, "result": "unknown", "unreachable_reason": reason},
                None,
            )
        if (
            not isinstance(exc, httpx.HTTPStatusError)
            or exc.response.status_code in _DISPATCHER_HTTP
        ):
            raise
        return "create_failed", {**step, "result": "failed", "errors": [_vendor_error(exc)]}, None
    return "created", {**step, "result": "succeeded"}, plan


def _create_answer(
    outcome: str, check: dict[str, Any], create: dict[str, Any], plan: dict[str, Any] | None
) -> dict[str, Any]:
    steps = [check, create]
    not_ok = ("failed", "still_running", "not_reachable", "unknown")
    return {
        "outcome": outcome,
        "plan_id": plan.get("id") if plan else None,
        "plan": plan,
        "steps": steps,
        "steps_ran": [s["name"] for s in steps if s["result"] == "succeeded"],
        "step_failed": next((s for s in steps if s["result"] in not_ok), None),
        "steps_not_run": [s["name"] for s in steps if s["result"] == "not_run"],
        "note": _CREATE_NOTES[outcome],
    }


async def upgrade_plan_create_impl(
    connector: FleetLcmConnector,
    operator: Operator,
    target: FleetLcmTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet-lcm.upgrade-plan.create`` -- check the plan, then create it.

    Step 1 posts the spec to the vendor's plan check and reads that job until
    it ends (at most :data:`VALIDATION_WAIT_SECONDS`). Step 2 creates the plan
    only when the check passed. A check that fails, or still runs after the
    wait, creates nothing. The vendor's create updates a matching plan, so
    running this twice does not make a second plan.

    The answer always lists both steps. When the service stops answering
    while MEHO follows the check, the check step keeps the check's task id
    (``not_reachable``). When the create gets no answer, the create step says
    ``unknown``: the plan may or may not exist.
    """
    spec: dict[str, Any] = {"desiredSoftware": params["desiredSoftware"]}
    if params.get("componentsFilter"):
        spec["componentsFilter"] = params["componentsFilter"]
    not_run: dict[str, Any] = {
        "name": "create",
        "result": "not_run",
        "task_id": None,
        "errors": [],
        "unreachable_reason": None,
    }

    started = await _post(connector, operator, target, _UPGRADE_PLAN_VALIDATIONS_POST_PATH, spec)
    task, unreachable = await _wait_for_check(connector, operator, target, started)
    state = _state(task.get("status"))
    errors = _task_errors(task) + _validation_findings(task, "ERROR")
    check: dict[str, Any] = {
        "name": "validate",
        "result": "succeeded",
        "task_id": task.get("id") or started.get("id"),
        "vendor_status": task.get("status"),
        "errors": errors,
        "warnings": _validation_findings(task, "WARNING"),
        "unreachable_reason": unreachable,
    }
    if unreachable is not None:
        check["result"] = "not_reachable"
        return _create_answer("validation_not_reachable", check, not_run, None)
    if state in _ACTIVE_STATES:
        check["result"] = "still_running"
        return _create_answer("validation_still_running", check, not_run, None)
    if state != "succeeded" or errors:
        check["result"] = "failed"
        return _create_answer("validation_failed", check, not_run, None)

    outcome, create, plan = await _create_step(connector, operator, target, spec, not_run)
    return _create_answer(outcome, check, create, plan)
