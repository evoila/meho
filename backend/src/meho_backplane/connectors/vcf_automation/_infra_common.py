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
  done, started (a vendor job), skipped, failed, uncertain (the system did
  not answer a write) or not run, so an answer that fails part-way lists
  what ran, what failed and what did not run. A step that sends more than
  one request (the storage limits of a quota) keeps one record per request,
  told apart by ``part``.
* **Vendor error text.** :func:`vendor_error_text` shortens the vendor's
  text and removes each value to hide in every form the text can carry it
  (plain, JSON-escaped, HTML-escaped, URL-encoded, form-encoded). When a
  value could still be in the text after that, the text is withheld.
* **What the approver saw.** :func:`state_hash` hashes the fields of an
  object the change depends on. The preview stores it; the approved run
  compares it with a fresh read (:func:`approved_state_problem`).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote, quote_plus, unquote, unquote_plus

import httpx

from meho_backplane.connectors.vcf_automation._provider_infra_paths import (
    INFRA_TASK_PATH,
    safe_segment,
)
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
    "part_way_guidance",
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
_TASK_URN_PREFIX: Final = "urn:vcloud:task:"
#: A JSON string escape: ``\uXXXX`` or one of ``\" \\ \/ \b \f \n \r \t``.
_JSON_ESCAPE: Final = re.compile(r'\\u([0-9a-fA-F]{4})|\\(["\\/bfnrt])')
#: The hex digits of a JSON ``\uXXXX`` escape or of a ``%XX`` URL escape.
_ESCAPE_HEX: Final = re.compile(r"(?<=\\u)[0-9a-fA-F]{4}|(?<=%)[0-9a-fA-F]{2}")
_JSON_SHORT_ESCAPES: Final[dict[str, str]] = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


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


def _json_unescaped(text: str) -> str:
    """*text* with its JSON string escapes decoded (``\\"`` to ``"``, and so on).

    A character outside the Basic Multilingual Plane (an emoji) is two
    ``\\uXXXX`` escapes in JSON, a surrogate pair. Decoding them one by one
    gives two half characters, so the pair is joined afterwards.
    """

    def _one(match: re.Match[str]) -> str:
        if match.group(1):
            return chr(int(match.group(1), 16))
        return _JSON_SHORT_ESCAPES[match.group(2)]

    decoded = _JSON_ESCAPE.sub(_one, text)
    return decoded.encode("utf-16", "surrogatepass").decode("utf-16", "surrogatepass")


def _both_hex_cases(form: str) -> set[str]:
    """*form* with the hex digits of its escapes in lower and in upper case."""
    return {
        _ESCAPE_HEX.sub(lambda m: m.group(0).lower(), form),
        _ESCAPE_HEX.sub(lambda m: m.group(0).upper(), form),
    }


def _forms_of(secret: str) -> list[str]:
    """Every form a vendor text can carry *secret* in, longest first.

    Escapes with hex digits (``\\u00e9`` in JSON, ``%26`` in a URL) come in
    both letter cases, because vendors differ.
    """
    forms = {
        secret,
        json.dumps(secret, ensure_ascii=False)[1:-1],
        html.escape(secret),
        html.escape(secret, quote=False),
    }
    for escaped in (json.dumps(secret)[1:-1], quote(secret, safe=""), quote_plus(secret)):
        forms |= _both_hex_cases(escaped)
    return sorted((form for form in forms if form), key=len, reverse=True)


def _decoded_views(text: str) -> list[str]:
    """*text* and its decoded forms (JSON, HTML, URL and form encoding), twice over."""
    views = [text]
    for _ in range(2):
        views.extend(
            [_json_unescaped(view) for view in views]
            + [html.unescape(view) for view in views]
            + [unquote(view) for view in views]
            + [unquote_plus(view) for view in views]
        )
    return views


def vendor_error_text(exc: BaseException, *, hide: tuple[str, ...] = ()) -> str:
    """The vendor's error text of *exc*, short, with each *hide* value removed.

    A value is removed in every form the text can carry it: as it is, JSON-
    escaped (a quote or a backslash in the value becomes ``\\"`` or ``\\\\`` in a
    JSON body), HTML-escaped, URL-encoded and form-encoded (a space becomes
    ``+``). When a value is still found in a decoded view of the text after
    that, the text is withheld and only the HTTP status stays.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status: int | None = exc.response.status_code
        body = exc.response.text.strip()
        text = f"HTTP {status}: {body}" if body else str(exc)
    else:
        status = None
        text = str(exc) or type(exc).__name__
    secrets = [secret for secret in hide if secret]
    for secret in secrets:
        for form in _forms_of(secret):
            text = text.replace(form, "<hidden>")
    if any(secret in view for secret in secrets for view in _decoded_views(text)):
        prefix = f"HTTP {status}: " if status is not None else ""
        return f"{prefix}the vendor's error text is withheld because it may hold the password"
    return text[:_ERROR_TEXT_MAX]


def job_from_response(resp: httpx.Response, *, status_read: str) -> dict[str, Any]:
    """The vendor job a ``202`` answer started: ``{id, href, status_read}``.

    The id comes from a task body (an ``id`` of the form
    ``urn:vcloud:task:...`` or an ``href`` with ``/task/``) or else from the
    ``Location`` header. Any other body (for example the object the call
    created) gives no job id. An answer with no task (an empty ``202``)
    gives ``id: null``; the caller then reads the object with *status_read*.
    """
    href: str | None = resp.headers.get("Location") or None
    task_id: str | None = None
    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = None
    if isinstance(body, dict):
        body_href = str(body.get("href") or "")
        if "/task/" in body_href:
            href = body_href
        raw = str(body.get("id") or "")
        if raw.startswith(_TASK_URN_PREFIX):
            task_id = uuid_of(raw) or None
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
    """The steps of one change, in order, with what happened to each.

    A step that sends one request has one record. A step that sends several
    (the storage limits of a quota, one request per policy) has one record
    per request, each with its own ``part`` (the policy name), result and
    job id. A record is replaced when the same step and part is marked
    again.
    """

    names: tuple[str, ...]
    rows: list[dict[str, Any]] = field(default_factory=list)

    def mark(
        self,
        step: str,
        result: str,
        detail: str | None = None,
        *,
        part: str | None = None,
        **extra: Any,
    ) -> None:
        """Record *result* (done, started, skipped, failed, uncertain) for *step* / *part*."""
        row = {"step": step, "part": part, "result": result, "detail": detail, **extra}
        for index, existing in enumerate(self.rows):
            if existing["step"] == step and existing["part"] == part:
                self.rows[index] = row
                return
        self.rows.append(row)

    def fail(
        self, step: str, error: str, http_status: int | None, *, part: str | None = None
    ) -> None:
        """Record that *step* failed with the vendor's *error*."""
        self.mark(step, "failed", None, part=part, error=error, http_status=http_status)

    def uncertain(
        self, step: str, error: str, http_status: int | None, *, part: str | None = None
    ) -> None:
        """Record that the system did not answer the request of *step*.

        The change may or may not have happened; the caller must read the
        object before it calls again.
        """
        self.mark(step, "uncertain", None, part=part, error=error, http_status=http_status)

    def any_ran(self) -> bool:
        """True when a step already changed something (done or started)."""
        return any(row["result"] in {"done", "started"} for row in self.rows)

    def any_uncertain(self) -> bool:
        """True when a request got no answer, so its change may have happened."""
        return any(row["result"] == "uncertain" for row in self.rows)

    def as_list(self) -> list[dict[str, Any]]:
        """Every record in step order; a step without a record is ``not_run``."""
        out: list[dict[str, Any]] = []
        for name in self.names:
            rows = [row for row in self.rows if row["step"] == name] or [
                {"step": name, "part": None, "result": "not_run", "detail": None}
            ]
            for row in rows:
                out.append({"error": None, "http_status": None, "job_id": None, **row})
        return out


def part_way_guidance(log: StepLog, status_read: str) -> str:
    """What the caller does after a ``failed_part_way`` answer.

    When a request got no answer (an ``uncertain`` record), the system may
    have made that change, so the caller reads the object with *status_read*
    before it calls again.
    """
    if log.any_uncertain():
        return (
            "A request got no answer after an earlier step ran, so VCF Automation may or may "
            f"not have made that change. The steps that ran are not undone. Read {status_read} "
            "first to see the state and the jobs, then call again with the same inputs; done "
            "steps are skipped."
        )
    return (
        "A step failed after an earlier step ran. The steps that ran are not undone. Read "
        f"{status_read}, fix the cause, then call again with the same inputs; done steps are "
        "skipped."
    )
