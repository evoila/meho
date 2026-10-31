# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The reads of the Cloud Builder operations.

* ``installer.cb.about`` — ``GET /v1/sddcs/about``: the bring-up service and
  its version.
* ``installer.cb.validation.status`` — ``GET /v1/sddcs/validations/{id}``
  (one validation, check by check) or ``GET /v1/sddcs/validations`` (all
  validations, when no id is given).
* ``installer.cb.sddc.status`` — ``GET /v1/sddcs/{id}`` (one bring-up, task by
  task) or ``GET /v1/sddcs`` (all bring-ups, when no id is given).
* ``installer.cb.sddc.report`` — ``GET /v1/sddcs/{id}/detail-report`` (PDF or
  CSV) or ``GET /v1/sddcs/validations/{validationId}/report`` (PDF). The file
  comes back base64-encoded; the operation asks MEHO to store the whole answer
  behind a result handle, so the caller gets a link to the file.

A status read that gets no answer returns ``state: not_reachable`` with the id
and the time, never ``failed``. MEHO keeps no job state between calls.

A vendor error text of a check or a task can echo a value of the spec an
earlier start sent. The status reads hide the secret values the connector
remembers for the target, and every licence key by its shape (see
:class:`~meho_backplane.connectors.vcf_cloud_builder._jobs.SecretMemory`).
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_cloud_builder._jobs import (
    error_list_text,
    hide_secrets_everywhere,
    not_reachable,
    now_iso,
    sddc_state,
    validation_state,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_cloud_builder.connector import CloudBuilderConnector
    from meho_backplane.connectors.vcf_installer.session import InstallerTargetLike

__all__ = [
    "ABOUT_PATH",
    "READ_CALLS",
    "SDDC_LIST_PATH",
    "SDDC_PATH",
    "SUBTASK_FAILED",
    "SUBTASK_RUNNING",
    "VALIDATIONS_PATH",
    "VALIDATION_PATH",
    "about_impl",
    "page_elements",
    "sddc_report_impl",
    "sddc_rows",
    "sddc_status_impl",
    "sddc_view",
    "validation_rows",
    "validation_status_impl",
    "validation_view",
]

ABOUT_PATH: Final = "/v1/sddcs/about"
VALIDATIONS_PATH: Final = "/v1/sddcs/validations"
VALIDATION_PATH: Final = "/v1/sddcs/validations/{id}"
VALIDATION_REPORT_PATH: Final = "/v1/sddcs/validations/{validationId}/report"
SDDC_LIST_PATH: Final = "/v1/sddcs"
SDDC_PATH: Final = "/v1/sddcs/{id}"
DETAIL_REPORT_PATH: Final = "/v1/sddcs/{id}/detail-report"

#: Every ``METHOD:/path`` the reads send. The reconcile lane checks each one.
READ_CALLS: Final[frozenset[str]] = frozenset(
    {
        f"GET:{ABOUT_PATH}",
        f"GET:{VALIDATIONS_PATH}",
        f"GET:{VALIDATION_PATH}",
        f"GET:{VALIDATION_REPORT_PATH}",
        f"GET:{SDDC_LIST_PATH}",
        f"GET:{SDDC_PATH}",
        f"GET:{DETAIL_REPORT_PATH}",
    }
)

#: The sub-task states that count as finished well, running and failed.
_SUBTASK_DONE: Final = frozenset(
    {
        "COMPLETED_WITH_SUCCESS",
        "PREVALIDATION_COMPLETED_WITH_SUCCESS",
        "POSTVALIDATION_COMPLETED_WITH_SUCCESS",
    }
)
SUBTASK_RUNNING: Final = frozenset(
    {"IN_PROGRESS", "PREVALIDATION_IN_PROGRESS", "POSTVALIDATION_IN_PROGRESS"}
)
SUBTASK_FAILED: Final = frozenset(
    {
        "COMPLETED_WITH_FAILURE",
        "PREVALIDATION_COMPLETED_WITH_FAILURE",
        "POSTVALIDATION_COMPLETED_WITH_FAILURE",
        "INTERNAL_ERROR",
    }
)

#: Report media types by format.
_MEDIA_TYPES: Final = {"pdf": "application/pdf", "csv": "text/csv"}


def page_elements(payload: Any) -> list[dict[str, Any]]:
    """The ``elements`` of a vendor page, or ``[]``."""
    rows = payload.get("elements") if isinstance(payload, dict) else None
    return [row for row in rows or () if isinstance(row, dict)]


async def about_impl(
    connector: CloudBuilderConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.cb.about`` — the ``VcfService`` of the bring-up app."""
    del params
    payload = await connector.send(target, "GET", ABOUT_PATH, operator=operator)
    return {key: payload.get(key) for key in ("id", "name", "version", "status")}


def _check_row(check: dict[str, Any]) -> dict[str, Any]:
    error = check.get("errorResponse")
    return {
        "description": check.get("description"),
        "severity": check.get("severity"),
        "result": check.get("resultStatus"),
        "error": error_list_text([error]) if isinstance(error, dict) else None,
    }


def validation_view(payload: dict[str, Any]) -> dict[str, Any]:
    """A flat view of one vendor ``Validation``: scalars and one list of checks."""
    checks = [_check_row(c) for c in payload.get("validationChecks") or () if isinstance(c, dict)]
    failed = [c for c in checks if c["result"] == "FAILED"]
    errors = "; ".join(f"{c['description']}: {c['error']}" for c in failed if c["error"])
    return {
        "id": payload.get("id"),
        "description": payload.get("description"),
        "state": validation_state(payload.get("executionStatus"), payload.get("resultStatus")),
        "vendor_state": payload.get("executionStatus"),
        "result": payload.get("resultStatus"),
        "checks_total": len(checks),
        "checks_finished": sum(1 for c in checks if c["result"] not in (None, "IN_PROGRESS")),
        "checks_failed": len(failed),
        "error": errors or None,
        "checks": checks,
    }


def validation_rows(payload: Any) -> list[dict[str, Any]]:
    """One flat row per validation of a ``PageOfValidation``."""
    keys = ("id", "description", "state", "vendor_state", "result")
    return [
        {key: view[key] for key in keys} for view in map(validation_view, page_elements(payload))
    ]


async def validation_status_impl(
    connector: CloudBuilderConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.cb.validation.status`` — one validation, or all when no id is given."""
    validation_id = params.get("id")
    secrets = connector.known_secrets(target)
    try:
        if not validation_id:
            payload = await connector.send(target, "GET", VALIDATIONS_PATH, operator=operator)
            rows = validation_rows(payload)
            return {"checked_at": now_iso(), "validations": hide_secrets_everywhere(rows, secrets)}
        path = VALIDATION_PATH.replace("{id}", str(validation_id))
        payload = await connector.send(target, "GET", path, operator=operator)
    except httpx.TransportError as exc:
        return not_reachable(exc, id_key="id", job_id=validation_id, target=target)
    view = hide_secrets_everywhere(validation_view(payload), secrets)
    return {**view, "checked_at": now_iso()}


def _task_row(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": task.get("name"),
        "description": task.get("description"),
        "status": task.get("status"),
        "updated_at": task.get("updateTimestamp"),
        "error": error_list_text(task.get("errors")),
    }


def sddc_view(payload: dict[str, Any]) -> dict[str, Any]:
    """A flat view of one vendor ``SddcTask`` (a bring-up): scalars and one list of tasks."""
    tasks = [_task_row(t) for t in payload.get("sddcSubTasks") or () if isinstance(t, dict)]
    failed = [t for t in tasks if t["status"] in SUBTASK_FAILED]
    return {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "state": sddc_state(payload.get("status")),
        "vendor_state": payload.get("status"),
        "created_at": payload.get("creationTimestamp"),
        "tasks_total": len(tasks),
        "tasks_done": sum(1 for t in tasks if t["status"] in _SUBTASK_DONE),
        "tasks_running": sum(1 for t in tasks if t["status"] in SUBTASK_RUNNING),
        "tasks_failed": len(failed),
        "failed_task": failed[0]["name"] if failed else None,
        "error": failed[0]["error"] if failed else None,
        "tasks": tasks,
    }


def sddc_rows(payload: Any) -> list[dict[str, Any]]:
    """One flat row per bring-up of a ``PageOfSddcTask``."""
    return [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "state": sddc_state(item.get("status")),
            "vendor_state": item.get("status"),
            "created_at": item.get("creationTimestamp"),
        }
        for item in page_elements(payload)
    ]


async def sddc_status_impl(
    connector: CloudBuilderConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.cb.sddc.status`` — one bring-up, or all when no id is given."""
    sddc_id = params.get("id")
    secrets = connector.known_secrets(target)
    try:
        if not sddc_id:
            payload = await connector.send(target, "GET", SDDC_LIST_PATH, operator=operator)
            rows = sddc_rows(payload)
            return {"checked_at": now_iso(), "bringups": hide_secrets_everywhere(rows, secrets)}
        path = SDDC_PATH.replace("{id}", str(sddc_id))
        payload = await connector.send(target, "GET", path, operator=operator)
    except httpx.TransportError as exc:
        return not_reachable(exc, id_key="id", job_id=sddc_id, target=target)
    view = hide_secrets_everywhere(sddc_view(payload), secrets)
    return {**view, "checked_at": now_iso()}


async def sddc_report_impl(
    connector: CloudBuilderConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.cb.sddc.report`` — the bring-up detail report or a validation report."""
    if params.get("validation_id"):
        report, report_id, fmt = "validation", str(params["validation_id"]), "pdf"
        path = VALIDATION_REPORT_PATH.replace("{validationId}", report_id)
        query = None
    else:
        report, report_id = "bringup_detail", str(params["sddc_id"])
        fmt = str(params.get("format") or "pdf")
        path = DETAIL_REPORT_PATH.replace("{id}", report_id)
        query = {"format": fmt}
    response = await connector.get_file(target, path, operator=operator, params=query)
    content = response.content
    return {
        "report": report,
        "id": report_id,
        "format": fmt,
        "content_type": response.headers.get("content-type") or _MEDIA_TYPES[fmt],
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "encoding": "base64",
        "content": base64.b64encode(content).decode("ascii"),
    }
