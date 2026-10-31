# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vendor job behind a networking change: its state, progress and error text.

A networking change answers ``202`` with a link to a vendor task in the
``Location`` header. The start gives the caller the task id as ``job_id``.
The status read (``vcfa.provider.network.list`` with ``job_id``) then reads
the task with :func:`read_job` (``GET /api/task/{id}``, a legacy call with the
classic ``application/*+json;version=40.0`` Accept header). The vendor's own
SDK (``go-vcloud-director`` v3) follows these ``202`` starts the same way.

:func:`with_job` adds the task to the answer of the status read: the job
state, the progress, and the vendor's error text when the job failed. The
error text goes through MEHO's default redaction policy and is cut to
:data:`ERROR_TEXT_MAX` characters. When the object is gone (``not_found``) and
the job succeeded, ``state`` stays ``not_found``: that is how a finished delete
ends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_automation._provider_paths import (
    INFRA_TASK_PATH,
    bad_id_reason,
    provider_path,
)
from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.resolver import get_default_policy

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = ["ERROR_TEXT_MAX", "job_id_problem", "not_reachable", "read_job", "with_job"]

#: Vendor task state -> the job state MEHO reports.
_JOB_STATE: Final[dict[str, str]] = {
    "queued": "queued",
    "prerunning": "queued",
    "running": "running",
    "success": "succeeded",
    "error": "failed",
    "aborted": "failed",
    "canceled": "canceled",
}
#: HTTP answers that mean "the appliance does not answer now", not "the job failed".
_NOT_REACHABLE_STATUS: Final = frozenset({502, 503, 504})
#: Longest vendor error text kept in an answer.
ERROR_TEXT_MAX: Final = 500
_TEXT_WITHHELD: Final = "<vendor text withheld: redaction unavailable>"
_UNREAD_GUIDANCE: Final = (
    "MEHO could not read the vendor job with this id ({why}). The object state above still holds."
)
#: The URN form of a vendor task id; the plain id follows it.
_TASK_URN: Final = "urn:vcloud:task:"


def _uuid_of(job_id: str) -> str:
    """``urn:vcloud:task:<uuid>`` -> ``<uuid>``; a plain id passes through."""
    return job_id.removeprefix(_TASK_URN)


def job_id_problem(job_id: object) -> str | None:
    """Why *job_id* cannot go into the task path, or ``None`` when it can.

    A job id is a plain id or ``urn:vcloud:task:<id>``. Any other id with a
    colon is refused, so the task MEHO reads is always the id the answer shows.
    """
    problem = bad_id_reason(job_id, "job")
    if problem is None and isinstance(job_id, str):
        if ":" in _uuid_of(job_id):
            return f"job id {job_id!r} is neither a plain id nor of the form {_TASK_URN}<id>"
        problem = bad_id_reason(_uuid_of(job_id), "job")
    return problem


def _clean(text: object) -> str | None:
    """*text* with secrets filtered out, cut to :data:`ERROR_TEXT_MAX`; ``None`` when empty."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        cleaned = redact(text, get_default_policy()).redacted
    except Exception:  # never let the vendor's raw text through
        return _TEXT_WITHHELD
    if not isinstance(cleaned, str):
        return _TEXT_WITHHELD
    return cleaned[:ERROR_TEXT_MAX]


def _unread(job_id: str, why: str) -> dict[str, Any]:
    return {
        "id": job_id,
        "read": False,
        "state": "unknown",
        "vendor_state": None,
        "progress": None,
        "error": None,
        "operation": None,
        "started_at": None,
        "ended_at": None,
        "why_not_read": why,
    }


def not_reachable(base: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """The answer when VCF Automation did not answer; re-raises any other error."""
    if isinstance(exc, httpx.HTTPStatusError):
        silent = exc.response.status_code in _NOT_REACHABLE_STATUS
    else:
        silent = isinstance(exc, httpx.TransportError)
    if not silent:
        raise exc
    guidance = (
        "VCF Automation did not answer, for example during its own upgrade. This "
        "says nothing about the job; keep the job id and read again later."
    )
    error = f"{type(exc).__name__}: {exc}"
    return {
        **base,
        "reachable": False,
        "state": "not_reachable",
        "error": error,
        "guidance": guidance,
    }


async def read_job(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    job_id: str,
) -> dict[str, Any]:
    """Read one vendor job: state, progress and the vendor's error text.

    Check *job_id* with :func:`job_id_problem` first. A job the vendor no
    longer knows (``404``), any other ``4xx`` answer, or a body that is not a
    task answers ``read: false``; ``why_not_read`` names the HTTP status. A
    system that does not answer (also ``5xx``) raises, so the caller can say
    ``not_reachable``.
    """
    path = provider_path(target, INFRA_TASK_PATH, id=_uuid_of(job_id))
    try:
        task = await connector._request_json(target, "GET", path, operator=operator)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 404:
            return _unread(
                job_id, "HTTP 404: the vendor has no job with this id; it may have ended long ago"
            )
        if 400 <= code < 500:
            return _unread(job_id, f"HTTP {code}: the vendor refused the job read")
        raise
    except ValueError:
        return _unread(job_id, "the vendor's answer was not a task")
    if not isinstance(task, dict) or not isinstance(task.get("status"), str):
        return _unread(job_id, "the vendor's answer was not a task")
    vendor_state = task["status"]
    error = task.get("error")
    progress = task.get("progress")
    return {
        "id": job_id,
        "read": True,
        "state": _JOB_STATE.get(vendor_state.lower(), "unknown"),
        "vendor_state": vendor_state or None,
        "progress": progress if isinstance(progress, int) else None,
        "error": _clean(error.get("message")) if isinstance(error, dict) else None,
        "operation": task.get("operationName") or task.get("operation"),
        "started_at": task.get("startTime"),
        "ended_at": task.get("endTime"),
        "why_not_read": None,
    }


def with_job(answer: dict[str, Any], job: dict[str, Any] | None) -> dict[str, Any]:
    """*answer* of the status read with the vendor job *job* added.

    A job that was read sets ``state``: the job tells how far the change
    got, also while the object still shows its old state. One exception: when
    the object is gone (``not_found``) and the job succeeded, ``state`` stays
    ``not_found``, so a finished delete ends as its guidance says. A failed
    job puts the vendor's error text in ``error``.
    """
    if job is None:
        return answer
    out = {**answer, "job": job, "job_state": job["state"], "progress": job["progress"]}
    if not job["read"]:
        out["guidance"] = _UNREAD_GUIDANCE.format(why=job["why_not_read"])
        return out
    deleted = answer.get("state") == "not_found" and job["state"] == "succeeded"
    if job["state"] != "unknown" and not deleted:
        out["state"] = job["state"]
    if job["state"] == "failed":
        out["error"] = job["error"] or "the vendor job failed and gave no error text"
        out["guidance"] = (
            "The vendor job failed; 'error' has the vendor's text. Fix the cause, read the "
            "object again, then run the change again if it is still needed."
        )
    elif job["state"] in {"queued", "running"}:
        out["guidance"] = "The vendor job still runs; read again later with the same job_id."
    return out
