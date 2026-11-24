# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``linux.logs.read`` -- the governed log read of ``linux-ssh`` (group ``logs``).

One read over the two places a Linux host keeps logs: an allow-listed log
file, and the systemd journal. It takes the shared parameters of
:mod:`meho_backplane.connectors.log_read` and answers with the shared
shape. A file goes through the confined read of ``linux.log.tail``; the
journal is read by :mod:`.logs_journal`. Both older ops stay.

``source`` is an absolute path (a file), ``journal`` (the whole journal)
or a systemd unit name. A file has no times, so ``since`` and ``until``
are refused for a file instead of being ignored. The text filter runs on
the host's lines here, after the secrets in them are hidden.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.linux.logs_journal import (
    JOURNAL_MARKER_PATTERN,
    journal_query,
    read_journal,
)
from meho_backplane.connectors.linux.ops import (
    MAX_TAIL_LINES,
    SSH_TRANSPORT_NOTE,
    LinuxOp,
    confine_read_path,
)
from meho_backplane.connectors.linux.ops_file import (
    ReadOutput,
    _read_opened_file,
    build_log_tail_command,
)
from meho_backplane.connectors.linux.ops_inspect import LinuxInspectError
from meho_backplane.connectors.log_read import (
    DEFAULT_LOG_LINES,
    LOG_READ_RESPONSE_SCHEMA,
    REDACTION_CONTEXT_LINES,
    LogEntry,
    build_log_answer,
    log_read_parameter_schema,
    redact_entries,
    take_page,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = ["LOGS_OPS", "linux_logs_read"]

OP_ID = "linux.logs.read"


def _window(params: dict[str, Any]) -> int:
    """Return how many entries to read from the host."""
    if params.get("filter"):
        return MAX_TAIL_LINES
    return min(
        int(params.get("lines", DEFAULT_LOG_LINES)) + REDACTION_CONTEXT_LINES, MAX_TAIL_LINES
    )


def _check_file_was_read(output: ReadOutput, path: str) -> None:
    """Raise when the file gave no content because it was not read."""
    if not output.exists:
        raise LinuxInspectError(f"log file {path!r} does not exist; nothing was read")
    if output.real_path is None:
        raise LinuxInspectError(f"log file {path!r} could not be opened; nothing was read")
    if not output.content and output.size_bytes != 0:
        raise LinuxInspectError(f"log file {path!r} gave no content and is not an empty file")


async def _read_file(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None,
) -> dict[str, Any]:
    if params.get("since") is not None or params.get("until") is not None:
        raise LinuxInspectError(
            "a log file has no times, so 'since' and 'until' cannot be used with a file; "
            "use the journal, or leave them out"
        )
    window = _window(params)
    confined = confine_read_path(params["source"])
    cmd = build_log_tail_command(confined, window)
    output = await _read_opened_file(connector, target, cmd, confined, operator)
    _check_file_was_read(output, confined)
    raw = output.content.splitlines()
    entries = await redact_entries(
        [LogEntry(time=None, line=line) for line in raw],
        connector=connector,
        operator=operator,
        op_id=OP_ID,
    )
    page = await take_page(
        entries,
        lines=int(params.get("lines", DEFAULT_LOG_LINES)),
        needle=params.get("filter"),
        keep="newest",
    )
    return build_log_answer(page, source_cut=len(raw) >= window)


async def _read_journal(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None,
) -> dict[str, Any]:
    query = journal_query(params)
    read = await read_journal(connector, target, query, _window(params), operator)
    entries = await redact_entries(
        read.entries, connector=connector, operator=operator, op_id=OP_ID
    )
    page = await take_page(
        entries,
        lines=int(params.get("lines", DEFAULT_LOG_LINES)),
        needle=params.get("filter"),
        keep="oldest" if query.forward else "newest",
    )
    markers = [entry.marker for entry in page.examined if entry.marker]
    boundary = query.marker
    if markers:
        boundary = markers[-1] if query.forward else markers[0]
    answer = build_log_answer(page, source_cut=read.cut, continuation=boundary)
    if read.warning:
        answer["warning"] = read.warning
    return answer


async def linux_logs_read(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.logs.read``: read a log file or the systemd journal."""
    read = _read_file if params["source"].startswith("/") else _read_journal
    return await read(connector, target, params, operator)


_LOGS_READ_OP = LinuxOp(
    op_id=OP_ID,
    handler_attr="logs_read",
    summary="Read log lines of a host: an allow-listed log file or the systemd journal.",
    description=(
        "Reads log lines from an allow-listed file, a systemd unit or the whole journal, with "
        "since, until, lines and a filter. Returns {rows: [{time, line, line_truncated}], "
        "total, truncated, covered, continuation}. A file has no times and refuses since and "
        "until. Read-only. Secrets in lines are hidden."
    ),
    parameter_schema={
        **log_read_parameter_schema(
            "What to read: an absolute log file path (for example /var/log/messages), "
            "'journal' for the whole systemd journal, or a systemd unit name (for example "
            "nfs-server.service).",
            marker_pattern=JOURNAL_MARKER_PATTERN,
        ),
        "examples": [{"source": "nfs-server.service", "since": "15m", "filter": "error"}],
    },
    response_schema=LOG_READ_RESPONSE_SCHEMA,
    group_key="logs",
    tags=("read-only", "log", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read what a host logged: a log file, one systemd unit, or the whole "
            "journal, with an optional time window and text match. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {
            "source": "A path starting with '/', 'journal', or a unit name.",
            "since": "'15m', an RFC 3339 time, or the 'continuation' of a read with 'since'. "
            "Journal and unit only.",
            "until": "An RFC 3339 time, or the 'continuation' of a read without 'since'. "
            "Journal and unit only.",
            "filter": "Plain text, case counts.",
        },
        "output_shape": (
            "{rows: [{time, line, line_truncated}], total, truncated, covered, continuation}. "
            "Rows are oldest first. Large answers become a result_query handle. 'covered' is "
            "the time span that was searched, also when no line matches. 'line_truncated' "
            "marks a line that was cut at 4096 characters; the filter looks at the whole "
            "line. With 'since' the read goes forward and keeps the oldest lines; without it, "
            "the newest. Check 'covered' and 'truncated' before you conclude that something "
            "is absent from the log. 'continuation' is the journal's own marker at the end "
            "of the page; a file has none."
        ),
        "next_step": (
            "While 'truncated' is true, read on in the journal with the same source: after a "
            "read with 'since', set 'since' to 'continuation'; after a read without it, set "
            "'until' to 'continuation' to read the part before. If a unit name shows in the "
            "lines, call linux.service.status for it."
        ),
    },
)

#: The ops of this module, composed onto ``LINUX_OPS``.
LOGS_OPS: tuple[LinuxOp, ...] = (_LOGS_READ_OP,)
