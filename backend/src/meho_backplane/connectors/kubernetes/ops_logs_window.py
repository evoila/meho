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
  :meth:`ApiClient.call_api` on the same path. The body is read as a
  stream, chunk by chunk, and the read stops as soon as the page is
  full or the window has ended.
* :func:`collect_window` -- turns that stream into one page: skips the
  lines before the start, keeps the lines inside the window until the
  byte cap is reached, and never holds more than the page plus one
  line in memory.

Kubernetes behaviour this relies on (``k8s.io/cri-client`` log reader
and the API server's pod log location):

* ``sinceTime`` is inclusive (a line at exactly that time is kept).
* The API server passes ``sinceTime`` to the node in whole seconds, so
  :func:`collect_window` drops the extra lines from the start of that
  second itself. Those lines are read and thrown away as they arrive,
  so a page can move past them however many there are.
* ``timestamps=true`` puts the time in front of each line in fixed
  nanosecond RFC 3339 form, followed by one space.
* The log is sent as a stream, so closing the connection early ends
  the read on the server side as well.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

__all__ = [
    "RFC3339_PATTERN",
    "WINDOW_CHUNK_BYTES",
    "LogLineTooLargeError",
    "LogWindow",
    "LogWindowError",
    "WindowPage",
    "collect_window",
    "parse_rfc3339",
    "read_pod_log_window",
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

#: How many bytes of the log body are read from the connection at a
#: time. Small enough to stop soon after a page is full, large enough
#: to keep the number of reads low.
WINDOW_CHUNK_BYTES = 64 * 1024

#: Exact time key: ``(epoch_seconds, nanoseconds)``.
TimeKey = tuple[int, int]


class LogWindowError(ValueError):
    """The time-window params (``since_time`` / ``until_time``) are unusable.

    Raised before any call to the cluster, so the dispatcher's
    ``connector_error`` envelope carries a plain message and
    ``extras.exception_class="LogWindowError"``.
    """


class LogLineTooLargeError(ValueError):
    """One log line inside the window is larger than one answer may hold.

    The byte cap works on whole lines, so such a line can never be
    returned, on this page or on a later one. The message names the
    line's time so the caller can continue after it.
    """

    def __init__(self, line_time: str, cap_bytes: int) -> None:
        self.line_time = line_time
        super().__init__(
            f"the log line at {line_time} is larger than {cap_bytes // (1024 * 1024)} MiB, "
            "the most one answer may hold, so it cannot be returned; set since_time "
            f"to a time after {line_time} to continue behind it, or until_time to a "
            "time before it"
        )


@dataclass(frozen=True)
class LogWindow:
    """A checked time window: inclusive start, optional inclusive end."""

    since: TimeKey
    until: TimeKey | None


@dataclass
class WindowPage:
    """The lines of one window read.

    ``stamps`` holds the raw timestamp of each kept line, in step with
    ``lines``. ``truncated`` is true when a line inside the window did
    not fit in the byte cap, so the window goes on after this page.
    """

    lines: list[str] = field(default_factory=list)
    stamps: list[str] = field(default_factory=list)
    truncated: bool = False


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
    window: LogWindow,
    cap_bytes: int,
) -> WindowPage:
    """``GET /api/v1/namespaces/{ns}/pods/{name}/log`` with extra query params.

    Builds the same request the generated ``read_namespaced_pod_log``
    does, but with the query params given here, so ``sinceTime`` can be
    sent. The body is not loaded at once: it is read in chunks of
    :data:`WINDOW_CHUNK_BYTES` and handed to :func:`collect_window`,
    which stops reading once the page is full. ``aiohttp`` closes a
    connection whose body was not read to the end instead of reusing
    it, so an early stop is safe.
    """
    response = await api_client.call_api(
        "/api/v1/namespaces/{namespace}/pods/{name}/log",
        "GET",
        path_params={"name": name, "namespace": namespace},
        query_params=list(query.items()),
        header_params={"Accept": "text/plain"},
        response_types_map={200: "str"},
        auth_settings=["BearerToken"],
        _return_http_data_only=True,
        _preload_content=False,
    )
    try:
        if not 200 <= response.status <= 299:
            # With ``_preload_content=False`` the SDK does not check the
            # status, so raise the same error it raises for the other
            # calls. The SDK turns the body into text there as well; its
            # type hints say bytes.
            raw: bytes = await response.read()
            error = ApiException(status=response.status, reason=response.reason)
            error.headers = response.headers
            error.body = raw.decode("utf-8", errors="replace")  # type: ignore[assignment]
            raise error
        return await collect_window(
            response.content.iter_chunked(WINDOW_CHUNK_BYTES), window, cap_bytes=cap_bytes
        )
    finally:
        response.release()


def window_query(
    window: LogWindow,
    *,
    container: str,
    previous: bool,
    tail_lines: int | None,
) -> dict[str, Any]:
    """Query params for a window read: ``sinceTime`` in whole seconds, UTC."""
    query: dict[str, Any] = {
        "container": container,
        "previous": previous,
        "sinceTime": datetime.fromtimestamp(window.since[0], tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timestamps": True,
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


async def collect_window(
    chunks: AsyncIterator[bytes], window: LogWindow, *, cap_bytes: int
) -> WindowPage:
    """Build one page from a streamed log body.

    Lines arrive in time order. Lines before ``window.since`` are
    thrown away as they arrive; a line after ``window.until`` ends the
    read. Lines inside the window are kept, oldest first, until the
    next one would push the page past ``cap_bytes`` (counted as the
    lines joined by newlines, the same way the newest-lines read
    counts). Then the read stops and the page is marked ``truncated``.

    Lines at exactly ``window.since`` do not count toward the cap. A
    caller pages with ``since_time`` set to the last kept time, so
    those lines are the ones the page before already had. Keeping them
    free means every page carries at least one new line, so paging
    always moves forward, also when the next line is close to the cap.

    Memory stays bounded: at most the kept page plus the line being
    read. A line that is still unfinished at ``cap_bytes`` is decided
    by its timestamp alone: before the window it is skipped without
    being stored, after the window it ends the read, and inside the
    window it raises :class:`LogLineTooLargeError`, because such a line
    could never be returned.

    A line without a timestamp (should not happen with
    ``timestamps=true``) takes the time of the line before it.
    """
    scanner = _WindowScanner(window, cap_bytes)
    async for chunk in chunks:
        if not scanner.feed(chunk):
            break
    scanner.finish()
    return scanner.page


class _WindowScanner:
    """Line-by-line state for :func:`collect_window`."""

    def __init__(self, window: LogWindow, cap_bytes: int) -> None:
        self._window = window
        self._cap_bytes = cap_bytes
        self._pending = bytearray()
        self._skipping = False
        self._done = False
        self._stamp = ""
        self._key: TimeKey | None = None
        self._kept_bytes = 0
        self.page = WindowPage()

    def feed(self, chunk: bytes) -> bool:
        """Take the next bytes; return ``False`` once the page is complete."""
        start = 0
        while start < len(chunk) and not self._done:
            newline = chunk.find(b"\n", start)
            if self._skipping:
                if newline < 0:
                    return True
                self._skipping = False
                start = newline + 1
                continue
            if newline < 0:
                self._pending += chunk[start:]
                if len(self._pending) > self._cap_bytes:
                    self._settle_unfinished_line()
                return not self._done
            self._pending += chunk[start:newline]
            start = newline + 1
            self._take_line(bytes(self._pending))
            self._pending.clear()
        return not self._done

    def finish(self) -> None:
        """Handle a last line that had no newline behind it."""
        if self._pending and not self._skipping and not self._done:
            self._take_line(bytes(self._pending))
        self._pending.clear()

    def _place(self, line: str) -> int:
        """-1 before the window, 0 inside it, 1 behind it."""
        parsed = _line_time(line)
        if parsed is not None:
            self._stamp, self._key = parsed
        if self._key is None or self._key < self._window.since:
            return -1
        if self._window.until is not None and self._key > self._window.until:
            return 1
        return 0

    def _take_line(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace")
        place = self._place(line)
        if place < 0:
            return
        if place > 0:
            self._done = True
            return
        if len(raw) > self._cap_bytes:
            raise LogLineTooLargeError(self._stamp, self._cap_bytes)
        size = 0 if self._key == self._window.since else len(raw) + (1 if self.page.lines else 0)
        if self._kept_bytes + size > self._cap_bytes:
            self.page.truncated = True
            self._done = True
            return
        self.page.lines.append(line)
        self.page.stamps.append(self._stamp)
        self._kept_bytes += size

    def _settle_unfinished_line(self) -> None:
        """Decide a line that is already too long to be returned.

        Only its timestamp is needed for that, so the bytes stored so
        far are dropped and the rest of the line is skipped as it
        arrives.
        """
        head = bytes(self._pending[:64]).decode("utf-8", errors="replace")
        place = self._place(head)
        self._pending.clear()
        if place < 0:
            self._skipping = True
            return
        self._done = True
        if place == 0:
            raise LogLineTooLargeError(self._stamp, self._cap_bytes)
