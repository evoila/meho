# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Time-window reads for ``k8s.logs`` (``since_time`` / ``until_time``).

Without a window, ``k8s.logs`` returns the newest part of a pod log. A
window lets a caller read an older part of a long log, one window per
call, oldest line first. This module holds the pieces that do not
depend on the op's schemas or caps:

* :func:`parse_rfc3339` -- exact ``(epoch_seconds, nanoseconds)`` keys,
  because Kubernetes writes nanosecond timestamps and :class:`datetime`
  keeps only microseconds.
* :func:`window_from_params` -- checks the params and refuses bad
  combinations with a plain :class:`LogWindowError`.
* :func:`read_pod_log_window` -- the ``/log`` request with
  ``sinceTime``. ``kubernetes_asyncio`` 36 does not accept
  ``since_time`` on ``read_namespaced_pod_log`` (the generated method
  has no such parameter), so the request goes through
  :meth:`ApiClient.call_api` on the same path.
* :func:`select_window` -- keeps the lines inside the window.

Kubernetes behaviour this relies on (``k8s.io/cri-client`` log reader
and the API server's pod log location):

* ``sinceTime`` is inclusive (a line at exactly that time is kept).
* The API server passes ``sinceTime`` to the node in whole seconds, so
  :func:`select_window` drops the extra lines from the start of that
  second itself.
* ``timestamps=true`` puts the time in front of each line in fixed
  nanosecond RFC 3339 form, followed by one space.
* ``limitBytes`` stops after that many bytes, even inside a line, and
  the timestamp prefix counts toward it.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kubernetes_asyncio import client

__all__ = [
    "RFC3339_PATTERN",
    "LogWindow",
    "LogWindowError",
    "parse_rfc3339",
    "read_pod_log_window",
    "select_window",
    "window_from_params",
    "window_query",
]

#: RFC 3339 date-time with a required offset. Shared by the parameter
#: schema and :func:`parse_rfc3339`; fractional seconds up to
#: nanoseconds (the precision Kubernetes writes with ``timestamps=true``).
RFC3339_PATTERN = (
    r"^(\d{4}-\d{2}-\d{2})[Tt](\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?([Zz]|[+-]\d{2}:\d{2})$"
)
_RFC3339_RE = re.compile(RFC3339_PATTERN)

#: Exact time key: ``(epoch_seconds, nanoseconds)``.
TimeKey = tuple[int, int]


class LogWindowError(ValueError):
    """The time-window params (``since_time`` / ``until_time``) are unusable.

    Raised before any call to the cluster, so the dispatcher's
    ``connector_error`` envelope carries a plain message and
    ``extras.exception_class="LogWindowError"``.
    """


@dataclass(frozen=True)
class LogWindow:
    """A checked time window: inclusive start, optional inclusive end."""

    since: TimeKey
    until: TimeKey | None


def parse_rfc3339(value: str) -> TimeKey:
    """Parse an RFC 3339 time into an exact ``(epoch_seconds, nanoseconds)`` key.

    The fraction is kept as a separate integer so the tuple compares
    exactly at nanosecond precision. Raises :class:`LogWindowError`
    with a plain message for anything that is not RFC 3339 with an
    offset.
    """
    match = _RFC3339_RE.match(value)
    if match is None:
        raise LogWindowError(
            f"{value!r} is not an RFC 3339 time; use a form like '2026-08-01T00:00:00Z'"
        )
    date_part, time_part, fraction, offset = match.groups()
    offset = "+00:00" if offset in ("Z", "z") else offset
    try:
        moment = datetime.fromisoformat(f"{date_part}T{time_part}{offset}")
    except ValueError as exc:
        raise LogWindowError(f"{value!r} is not a valid time: {exc}") from exc
    seconds = calendar.timegm(moment.utctimetuple())
    nanos = int((fraction or "").ljust(9, "0"))
    return seconds, nanos


def window_from_params(params: dict[str, Any]) -> LogWindow | None:
    """Check and parse ``since_time`` / ``until_time``; ``None`` when unset.

    Refuses, with a plain message: ``since`` together with
    ``since_time``; ``until_time`` without ``since_time``; an end before
    the start; and any value that is not an RFC 3339 time.
    """
    since_time: str | None = params.get("since_time")
    until_time: str | None = params.get("until_time")
    if since_time is not None and params.get("since") is not None:
        raise LogWindowError(
            "use either 'since' (a length of time like '7d') or 'since_time' "
            "(an exact start time), not both"
        )
    if until_time is None and since_time is None:
        return None
    if since_time is None:
        raise LogWindowError("'until_time' needs 'since_time': set the start of the window too")
    since_key = parse_rfc3339(since_time)
    until_key = parse_rfc3339(until_time) if until_time is not None else None
    if until_key is not None and until_key < since_key:
        raise LogWindowError("'until_time' is before 'since_time'")
    return LogWindow(since=since_key, until=until_key)


async def read_pod_log_window(
    api_client: client.ApiClient,
    *,
    name: str,
    namespace: str,
    query: dict[str, Any],
) -> str:
    """``GET /api/v1/namespaces/{ns}/pods/{name}/log`` with extra query params.

    Builds the same request the generated ``read_namespaced_pod_log``
    does, but with the query params given here, so ``sinceTime`` can be
    sent. Returns the log body as text.
    """
    body: str = await api_client.call_api(
        "/api/v1/namespaces/{namespace}/pods/{name}/log",
        "GET",
        path_params={"name": name, "namespace": namespace},
        query_params=list(query.items()),
        header_params={"Accept": "text/plain"},
        response_types_map={200: "str"},
        auth_settings=["BearerToken"],
        _return_http_data_only=True,
    )
    return body


def window_query(
    window: LogWindow,
    *,
    container: str,
    previous: bool,
    tail_lines: int | None,
    limit_bytes: int,
) -> dict[str, Any]:
    """Query params for a window read: ``sinceTime`` in whole seconds, UTC."""
    query: dict[str, Any] = {
        "container": container,
        "previous": previous,
        "sinceTime": datetime.fromtimestamp(window.since[0], tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timestamps": True,
        "limitBytes": limit_bytes,
    }
    if tail_lines is not None:
        query["tailLines"] = tail_lines
    return query


def _line_time(line: str) -> tuple[str, TimeKey] | None:
    """Return ``(raw_timestamp, key)`` for a ``timestamps=true`` line, or ``None``."""
    stamp, _, _ = line.partition(" ")
    try:
        return stamp, parse_rfc3339(stamp)
    except LogWindowError:
        return None


def select_window(lines: list[str], window: LogWindow) -> tuple[list[str], list[str], bool]:
    """Keep the lines whose time is inside ``window``.

    Returns the kept lines, the raw timestamp of each kept line (in
    step with them), and whether a line after ``window.until`` was
    seen. A line without a timestamp (should not happen with
    ``timestamps=true``) takes the time of the line before it. Lines
    are in time order, so the scan stops at the first line after the
    end.
    """
    kept: list[str] = []
    kept_stamps: list[str] = []
    stamp = ""
    key: TimeKey | None = None
    for line in lines:
        parsed = _line_time(line)
        if parsed is not None:
            stamp, key = parsed
        if key is None or key < window.since:
            continue
        if window.until is not None and key > window.until:
            return kept, kept_stamps, True
        kept.append(line)
        kept_stamps.append(stamp)
    return kept, kept_stamps, False
