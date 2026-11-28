# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How ``k8s.logs.read`` reads one container log.

A window is read with the window reader of ``k8s.logs``
(:mod:`.ops_logs_window`). Two reads are new here:

* :func:`read_newest` -- the newest lines. ``k8s.logs`` stops at 5000
  lines, so it cannot say whether a 5001st exists.
* :func:`read_before` -- the lines before a window. They are for the
  redaction only. Kubernetes cannot read backwards from a time, so this starts a while before the
  window and starts earlier again when that gave too few lines.

Both read the body as a stream and keep only what they return. A line over
1 MiB is never held whole (:class:`_LineBuffer`).
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.ops_logs import (
    MAX_BODY_BYTES,
    parse_rfc3339,
    resolve_pod_and_container,
)
from meho_backplane.connectors.kubernetes.ops_logs_window import (
    LogWindow,
    LogWindowError,
    WindowPage,
    open_pod_log,
    read_pod_log_window,
    window_query,
)
from meho_backplane.connectors.log_read import (
    MAX_CONTEXT_LINE_CHARS,
    REDACTION_CONTEXT_LINES,
    LogReadError,
    bound_context_line,
    has_key_mark,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = ["ContainerLog", "NewestLines", "read_before", "read_newest", "read_window"]

#: How many seconds before the window :func:`read_before` starts, one after
#: the other. ``None`` is the start of the log.
_LOOK_BACK_SECONDS: tuple[int | None, ...] = (300, 86400, None)

#: The most bytes one look back reads.
_MAX_LOOK_BACK_BYTES = 64 * 1024 * 1024

#: The end of the first and of the last line of a private key, and more
#: bytes than one of these lines has.
_KEY_MARK_END = b"PRIVATE KEY-----"
_KEY_MARK_BYTES = 136


@dataclass(frozen=True)
class ContainerLog:
    """One container of one pod, found by its name or a unique prefix."""

    api_client: client.ApiClient
    pod: str
    namespace: str
    container: str

    @classmethod
    async def find(
        cls,
        connector: Any,
        target: KubernetesTargetLike,
        operator: Operator,
        namespace: str,
        pod: str,
        container: str | None,
    ) -> ContainerLog:
        """Find the container, with the rules and the errors of ``k8s.logs``."""
        api_client = await connector._get_api_client(target, operator)
        pod, container = await resolve_pod_and_container(
            client.CoreV1Api(api_client), namespace, pod, container, include_init_containers=True
        )
        return cls(api_client, pod, namespace, container)


@dataclass(frozen=True)
class _Line:
    """One line of the log body. ``cut`` is true when it is too large to be kept whole."""

    text: str
    cut: bool


class _LineBuffer:
    """The line that is being read, in bounded memory.

    Of a line over :data:`MAX_BODY_BYTES` only the first characters and the
    last mark of a private key are kept, the same as in
    :func:`~meho_backplane.connectors.log_read.bound_context_line`.
    """

    def __init__(self) -> None:
        self._kept = bytearray()
        self._cut = False
        self._mark = b""
        self._end = b""

    def add(self, part: bytes) -> None:
        if self._cut:
            self._watch(part)
            return
        self._kept += part
        if len(self._kept) > MAX_BODY_BYTES:
            self._cut = True
            self._watch(bytes(self._kept))
            del self._kept[MAX_CONTEXT_LINE_CHARS:]

    def _watch(self, part: bytes) -> None:
        """Remember the last key mark; it can be spread over two parts."""
        data = self._end + part
        at = data.rfind(_KEY_MARK_END)
        if at >= 0:
            stop = at + len(_KEY_MARK_END)
            self._mark = data[max(0, stop - _KEY_MARK_BYTES) : stop]
        self._end = data[-_KEY_MARK_BYTES:]

    def __bool__(self) -> bool:
        return bool(self._kept)

    def take(self) -> _Line:
        """Return the line and start the next one."""
        mark = b" " + self._mark if self._mark else b""
        line = _Line((bytes(self._kept) + mark).decode("utf-8", errors="replace"), self._cut)
        self._kept.clear()
        self._cut, self._mark, self._end = False, b"", b""
        return line


@dataclass
class _Tail:
    """The last lines of a stream that fit, and the lines right before them."""

    rows: int
    max_bytes: int
    lines: deque[str] = field(default_factory=deque)
    context: deque[str] = field(default_factory=lambda: deque(maxlen=REDACTION_CONTEXT_LINES))
    size: int = 0

    def add(self, line: _Line) -> None:
        self.lines.append(line.text)
        self.size += len(line.text.encode()) + 1
        while self.lines and (
            line.cut
            or len(self.lines) > self.rows
            or (len(self.lines) > 1 and self.size - 1 > self.max_bytes)
        ):
            old = self.lines.popleft()
            self.size -= len(old.encode()) + 1
            self.context.append(bound_context_line(old))


@dataclass(frozen=True)
class NewestLines:
    """The newest lines, oldest first, and whether the log holds older ones."""

    lines: list[str]
    context: list[str]
    truncated: bool


async def _lines(
    log: ContainerLog, query: dict[str, Any], max_bytes: int | None = None
) -> AsyncGenerator[_Line | None]:
    """Yield the lines of the log body; ``None`` and the end after *max_bytes*."""
    async with open_pod_log(
        log.api_client, name=log.pod, namespace=log.namespace, query=query
    ) as chunks:
        pending = _LineBuffer()
        total = 0
        async for chunk in chunks:
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                yield None
                return
            *done, rest = chunk.split(b"\n")
            for part in done:
                pending.add(part)
                yield pending.take()
            pending.add(rest)
        if pending:
            yield pending.take()


async def read_newest(log: ContainerLog, rows: int) -> NewestLines:
    """Read the newest *rows* lines, at most 1 MiB, and what is before them.

    A line over 1 MiB ends the page like any line that does not fit. When
    it is the newest line, no page can be built and the read fails.
    """
    tail = _Tail(rows, MAX_BODY_BYTES)
    query = {
        "container": log.container,
        "previous": False,
        "tailLines": rows + REDACTION_CONTEXT_LINES,
    }
    async for line in _lines(log, query):
        if line is not None:
            tail.add(line)
    if tail.context and not tail.lines:
        raise LogReadError(
            f"the newest log line is larger than {MAX_BODY_BYTES // (1024 * 1024)} MiB, the "
            "most one answer may hold; read other lines with 'since' and 'until'"
        )
    return NewestLines(list(tail.lines), list(tail.context), bool(tail.context))


async def read_window(log: ContainerLog, window: LogWindow) -> WindowPage:
    """Read one page of *window*, oldest line first."""
    query = window_query(window, container=log.container, previous=False, tail_lines=None)
    return await read_pod_log_window(
        log.api_client,
        name=log.pod,
        namespace=log.namespace,
        query=query,
        window=window,
        cap_bytes=MAX_BODY_BYTES,
    )


def _text_before(line: str, window: LogWindow) -> str | None:
    """Return the text of a line with its time, or ``None`` when the window starts."""
    stamp, _, text = line.partition(" ")
    try:
        return None if parse_rfc3339(stamp) >= window.since else text
    except LogWindowError:
        return line


async def _look_back(log: ContainerLog, window: LogWindow, seconds: int | None) -> list[str] | None:
    """Return the lines from *seconds* before the window, or ``None`` when they are too many."""
    query: dict[str, Any] = {"container": log.container, "previous": False, "timestamps": True}
    if seconds is not None:
        start = datetime.fromtimestamp(max(0, window.since[0] - seconds), tz=UTC)
        query["sinceTime"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    before: deque[str] = deque(maxlen=REDACTION_CONTEXT_LINES)
    async with aclosing(_lines(log, query, _MAX_LOOK_BACK_BYTES)) as lines:
        async for line in lines:
            if line is None:
                return None
            text = _text_before(line.text, window)
            if text is None:
                break
            before.append(bound_context_line(text))
    return list(before)


async def read_before(log: ContainerLog, window: LogWindow) -> list[str]:
    """Read the lines right before *window*.

    It is called before the window is read: when the node drops old lines
    in between, they are missing in the window and not here.
    """
    context: list[str] = []
    for seconds in _LOOK_BACK_SECONDS:
        longer = await _look_back(log, window, seconds)
        if longer is None:
            break
        context = longer
        if len(context) == REDACTION_CONTEXT_LINES or any(map(has_key_mark, context)):
            break
    return context
