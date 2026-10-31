# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared helpers of the VCFA region and quota ops: vendor jobs, steps, state.

* **Vendor jobs.** A change answers ``202`` with a link to a vendor task in
  the ``Location`` header (or a task body). :func:`job_from_response` turns
  that into ``{id, href, status_read}``; MEHO keeps no job state, the caller
  keeps the id. :func:`read_job` reads the task again (``GET /api/task/{id}``)
  and maps the vendor state to queued, running, succeeded, failed or
  canceled, with the progress and the vendor's error text.
* **Not reachable.** :func:`is_unreachable` tells a system that does not
  answer (no connection, a time-out, ``502`` / ``503`` / ``504``) from a
  real failure, so a status read can say ``not_reachable`` instead.
* **Steps.** A change runs named steps. :class:`StepLog` records each one as
  done, started (a vendor job), skipped, failed or not run, so an answer
  that fails part-way lists what ran, what failed and what did not run.
* **What the approver saw.** :func:`state_hash` hashes the fields of an
  object the change depends on. The preview stores it; the approved run
  compares it with a fresh read (:func:`approved_state_problem`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_automation._infra_paths import INFRA_TASK_PATH, safe_segment
from meho_backplane.operations._preview import approved_preview

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "Call",
    "InvalidRequestError",
    "StepLog",
    "approved_state_problem",
    "ids_of",
    "is_unreachable",
    "job_from_response",
    "match_row",
    "now_iso",
    "read_job",
    "ref",
    "state_hash",
    "uuid_of",
    "vendor_error_text",
]

#: HTTP answers of a system that is down or restarting, not of a failed call.
_UNREACHABLE_STATUS: Final = frozenset({502, 503, 504})
#: Vendor task state → the job state MEHO reports.
_JOB_STATE: Final[dict[str, str]] = {
    "queued": "queued",
    "prerunning": "queued",
    "running": "running",
    "success": "succeeded",
    "error": "failed",
    "aborted": "failed",
    "canceled": "canceled",
}
#: Longest vendor error text kept in an answer.
_ERROR_TEXT_MAX: Final = 500


@dataclass(frozen=True)
class Call:
    """The connector, target and caller of one op run."""

    connector: VcfAutomationConnector
    target: VcfAutomationTargetLike
    operator: Operator


class InvalidRequestError(Exception):
    """A name the caller gave matches nothing; nothing is written."""


def match_row(
    rows: list[dict[str, Any]], wanted: str, what: str, id_field: str = "id"
) -> dict[str, Any]:
    """The row whose id or name (case-insensitive) is *wanted*, else :exc:`InvalidRequestError`."""
    needle = wanted.casefold()
    for row in rows:
        if row.get(id_field) == wanted or str(row.get("name") or "").casefold() == needle:
            return row
    raise InvalidRequestError(f"no {what} named {wanted!r}")


def ids_of(refs: Any) -> list[str]:
    """The sorted ids of a list of ``{name, id}`` references."""
    return sorted(str(r.get("id")) for r in refs or [] if isinstance(r, dict))


def now_iso() -> str:
    """The current time, UTC, ISO 8601."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def uuid_of(urn: str) -> str:
    """``urn:vcloud:<kind>:<uuid>`` → ``<uuid>``; a bare uuid passes through."""
    return urn.rsplit(":", 1)[-1]


def ref(row: Mapping[str, Any] | None, *, id_field: str = "id") -> dict[str, Any] | None:
    """The ``{name, id}`` reference of a vendor row, or ``None``."""
    if not isinstance(row, Mapping):
        return None
    return {"name": row.get("name"), "id": row.get(id_field)}


def is_unreachable(exc: BaseException) -> bool:
    """True when *exc* means the system did not answer (not a failed call)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _UNREACHABLE_STATUS
    if isinstance(exc, httpx.TransportError):
        return True
    cause = exc.__cause__
    return cause is not None and cause is not exc and is_unreachable(cause)


def vendor_error_text(exc: BaseException, *, hide: tuple[str, ...] = ()) -> str:
    """The vendor's error text of *exc*, short, with each *hide* value removed."""
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text.strip()
        text = f"HTTP {exc.response.status_code}: {body}" if body else str(exc)
    else:
        text = str(exc) or type(exc).__name__
    for secret in hide:
        if secret:
            text = text.replace(secret, "<hidden>")
    return text[:_ERROR_TEXT_MAX]


def job_from_response(resp: httpx.Response, *, status_read: str) -> dict[str, Any]:
    """The vendor job a ``202`` answer started: ``{id, href, status_read}``.

    The id comes from a task body (``id`` / ``href``) or else from the
    ``Location`` header. An answer with neither (an empty ``202``) gives
    ``id: null``; the caller then reads the object with *status_read*.
    """
    href: str | None = resp.headers.get("Location") or None
    task_id: str | None = None
    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = None
    if isinstance(body, dict):
        href = str(body.get("href") or href or "") or None
        raw = body.get("id")
        task_id = uuid_of(str(raw)) if raw else None
    if task_id is None and href and "/task/" in href:
        task_id = href.rstrip("/").rsplit("/", 1)[-1] or None
    return {"id": task_id, "href": href, "status_read": status_read}


async def read_job(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    job_id: str,
) -> dict[str, Any]:
    """Read one vendor job: state, progress and the vendor's error text.

    A job the vendor no longer knows (``404``) answers ``state: unknown``.
    A system that does not answer raises, so the caller can say
    ``not_reachable``.
    """
    path = INFRA_TASK_PATH.format(id=safe_segment(uuid_of(job_id), "job_id"))
    try:
        task = await connector._request_json(target, "GET", path, operator=operator)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        return {
            "id": job_id,
            "state": "unknown",
            "vendor_state": None,
            "progress": None,
            "error": "the vendor has no job with this id; it may have ended long ago",
            "operation": None,
            "started_at": None,
            "ended_at": None,
        }
    vendor_state = str(task.get("status") or "")
    error = task.get("error")
    return {
        "id": job_id,
        "state": _JOB_STATE.get(vendor_state.lower(), "unknown"),
        "vendor_state": vendor_state or None,
        "progress": task.get("progress"),
        "error": (error.get("message") if isinstance(error, dict) else None) or None,
        "operation": task.get("operationName") or task.get("operation"),
        "started_at": task.get("startTime"),
        "ended_at": task.get("endTime"),
    }


def state_hash(state: Any) -> str:
    """A stable SHA-256 of *state* (sorted JSON), for the approver's view."""
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def approved_state_problem(op_id: str, current_hash: str) -> str | None:
    """Why the approved run must stop, or ``None`` when it may go on.

    ``None`` also on a dispatch that is not an approved re-run (nothing to
    compare). After an approval, the run stops when the park stored no
    state (the preview failed) or when the object changed since.
    """
    seen = approved_preview(op_id)
    if seen is None:
        return None
    seen_hash = seen.get("state_hash")
    if not seen_hash:
        return (
            "the approver saw no state of the object (the preview could not read it), "
            "so nothing was written. Preview the call again and ask for a new approval"
        )
    if seen_hash != current_hash:
        return (
            "the object changed after the approver saw it, so nothing was written. "
            "Preview the call again and ask for a new approval"
        )
    return None


@dataclass
class StepLog:
    """The steps of one change, in order, with what happened to each."""

    names: tuple[str, ...]
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)

    def mark(self, step: str, result: str, detail: str | None = None, **extra: Any) -> None:
        """Record *result* (done, started, skipped, failed) for *step*."""
        self.rows[step] = {"step": step, "result": result, "detail": detail, **extra}

    def fail(self, step: str, error: str, http_status: int | None) -> None:
        """Record that *step* failed with the vendor's *error*."""
        self.mark(step, "failed", None, error=error, http_status=http_status)

    def any_ran(self) -> bool:
        """True when a step already changed something (done or started)."""
        return any(row["result"] in {"done", "started"} for row in self.rows.values())

    def as_list(self) -> list[dict[str, Any]]:
        """Every step in order; a step not recorded is ``not_run``."""
        out: list[dict[str, Any]] = []
        for name in self.names:
            row = self.rows.get(name) or {"step": name, "result": "not_run", "detail": None}
            out.append({"error": None, "http_status": None, "job_id": None, **row})
        return out
