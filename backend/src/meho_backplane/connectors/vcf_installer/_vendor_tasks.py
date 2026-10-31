# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read the Installer's own jobs (vendor ``Task``) for the settings changes.

A proxy or CEIP change returns ``202`` with a vendor ``Task``: the appliance
does the work after the call returns. Two helpers serve those changes:

* :func:`read_task` is the status read. It reads ``GET /v1/tasks/{id}`` and
  returns the vendor state, a plain ``outcome``, the progress (sub-tasks done
  of all) and the vendor error texts. It logs in again when the login has
  expired. When the appliance does not answer, it returns
  ``outcome: not_reachable`` with the job id and the time, never ``failed``.
* :func:`find_running_task` is the check before a start. It lists
  ``GET /v1/tasks`` and returns a job of the same kind that has not ended.
  The vendor does not document its task types, so the match is on whole
  words of the task ``type`` and ``name`` (for example ``PROXY`` or
  ``CEIP``). It is a help, not a lock: two callers can both check before
  either starts. When the list cannot be read, the check finds nothing.

MEHO keeps no job state between calls; the caller keeps the job id.
"""

from __future__ import annotations

import re
import ssl
from collections.abc import Collection
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx
import structlog

from meho_backplane.connectors.adapters.http import SsrfBlockedError

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
    from meho_backplane.connectors.vcf_installer.session import InstallerTargetLike

__all__ = [
    "OUTCOMES",
    "TASKS_PATH",
    "TASK_PATH",
    "find_running_task",
    "is_not_reachable",
    "read_task",
]

_log = structlog.get_logger(__name__)

#: ``GET`` — every vendor job of the appliance (``PageOfTask``).
TASKS_PATH: Final = "/v1/tasks"
#: ``GET`` — one vendor job (``Task``).
TASK_PATH: Final = "/v1/tasks/{id}"

#: Vendor task state (upper case, spaces as ``_``) → MEHO outcome. The vendor
#: sends both ``IN_PROGRESS`` and ``In Progress`` forms.
_OUTCOME_BY_STATE: Final[dict[str, str]] = {
    "PENDING": "queued",
    "QUEUED": "queued",
    "IN_PROGRESS": "running",
    "SUCCESSFUL": "succeeded",
    "COMPLETED_WITH_WARNING": "succeeded",
    "SKIPPED": "succeeded",
    "FAILED": "failed",
    "CANCELLED": "failed",
    "TIMED_OUT": "failed",
}

#: Every ``outcome`` :func:`read_task` returns.
OUTCOMES: Final[tuple[str, ...]] = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "unknown",
    "not_reachable",
)

_NOT_ENDED: Final = frozenset({"queued", "running"})
_WORD: Final = re.compile(r"[A-Z0-9]+")


def _state_key(state: Any) -> str:
    return str(state).strip().upper().replace(" ", "_") if isinstance(state, str) else ""


def _outcome(state: Any) -> str:
    return _OUTCOME_BY_STATE.get(_state_key(state), "unknown")


def is_not_reachable(exc: httpx.TransportError) -> bool:
    """True when the appliance did not answer; False for an error MEHO must report.

    An untrusted certificate means the appliance did answer, and a refused
    destination means MEHO sent nothing. Both go on to the dispatcher, so it
    reports the real reason.
    """
    if isinstance(exc, SsrfBlockedError):
        return False
    tls_failure = isinstance(exc.__cause__, ssl.SSLCertVerificationError) or (
        "CERTIFICATE_VERIFY_FAILED" in str(exc)
    )
    return not tls_failure


def _error_texts(errors: Any) -> list[str]:
    """The vendor ``message`` of each error (and of its nested errors)."""
    texts: list[str] = []
    for error in errors if isinstance(errors, list) else []:
        if not isinstance(error, dict):
            continue
        message = error.get("message") or error.get("errorCode")
        if isinstance(message, str) and message:
            texts.append(message)
        texts.extend(_error_texts(error.get("nestedErrors")))
    return texts


def _task_view(task: dict[str, Any], checked_at: str) -> dict[str, Any]:
    """The status-read answer for one vendor ``Task``."""
    view: dict[str, Any] = {
        "id": task.get("id"),
        "name": task.get("name"),
        "type": task.get("type"),
        "state": task.get("status"),
        "outcome": _outcome(task.get("status")),
        "errors": _error_texts(task.get("errors")),
        "completion_timestamp": task.get("completionTimestamp"),
        "checked_at": checked_at,
    }
    sub_tasks = task.get("subTasks")
    if isinstance(sub_tasks, list) and sub_tasks:
        done = sum(
            1
            for sub in sub_tasks
            if isinstance(sub, dict) and _outcome(sub.get("status")) == "succeeded"
        )
        view["progress"] = {"sub_tasks_done": done, "sub_tasks_total": len(sub_tasks)}
        for sub in sub_tasks:
            if isinstance(sub, dict):
                view["errors"].extend(_error_texts(sub.get("errors")))
    return view


async def read_task(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    task_id: str,
) -> dict[str, Any]:
    """Read one vendor job: state, outcome, progress and error texts.

    Logs in again once when the login has expired (the session-retry GET). A
    vendor ``404`` and other HTTP errors go on to the dispatcher.
    """
    checked_at = datetime.now(UTC).isoformat()
    try:
        task = await connector._get_json_with_session_retry(
            target, TASK_PATH.replace("{id}", quote(task_id, safe="")), operator=operator
        )
    except httpx.TransportError as exc:
        if not is_not_reachable(exc):
            raise
        _log.info("installer_task_not_reachable", target=target.name, task_id=task_id)
        return {
            "id": task_id,
            "state": None,
            "outcome": "not_reachable",
            "errors": [],
            "checked_at": checked_at,
        }
    return _task_view(task, checked_at)


def _words(task: dict[str, Any]) -> set[str]:
    text = f"{task.get('type') or ''} {task.get('name') or ''}".upper()
    return set(_WORD.findall(text))


async def find_running_task(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    *,
    word: str,
    exclude: Collection[str] = (),
) -> dict[str, Any] | None:
    """A vendor job that has not ended and whose type or name holds *word*.

    Tasks whose words include any of *exclude* do not count. A ``401`` goes
    on to the dispatcher (nothing was started yet, so a new attempt is
    safe). Any other error of the list read means "nothing found".
    """
    try:
        page = await connector._get_json(target, TASKS_PATH, operator=operator)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == httpx.codes.UNAUTHORIZED:
            raise
        _log.info(
            "installer_task_list_unreadable", target=target.name, status=exc.response.status_code
        )
        return None
    except httpx.TransportError:
        return None
    elements = page.get("elements") if isinstance(page, dict) else None
    for task in elements if isinstance(elements, list) else []:
        if not isinstance(task, dict) or _outcome(task.get("status")) not in _NOT_ENDED:
            continue
        words = _words(task)
        if word in words and not words.intersection(exclude):
            return task
    return None
