# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``linux.logs.read`` -- the governed log read of ``linux-ssh`` (group ``logs``).

One read over the two places a Linux host keeps logs: an allow-listed log
file, and the systemd journal. It takes the shared parameters of
:mod:`meho_backplane.connectors.log_read` and answers with the shared
shape. It reuses the confined file read of ``linux.log.tail`` and the
journal command of ``linux.journal.tail``, so the same path checks, the
same command quoting and the same byte caps apply. Both older ops stay.

``source`` is an absolute path (a file), ``journal`` (the whole journal)
or a systemd unit name. A file has no times, so ``since`` and ``until``
are refused for a file instead of being ignored. The text filter runs on
the host's lines here, before the answer is built.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.linux.ops import (
    MAX_TAIL_LINES,
    SSH_TRANSPORT_NOTE,
    LinuxOp,
    confine_read_path,
)
from meho_backplane.connectors.linux.ops_file import _read_opened_file, build_log_tail_command
from meho_backplane.connectors.linux.ops_inspect import LinuxInspectError, linux_journal_tail
from meho_backplane.connectors.log_read import (
    DEFAULT_LOG_LINES,
    build_log_answer,
    log_read_parameter_schema,
    parse_since,
    parse_until,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = ["LOGS_OPS", "linux_logs_read"]

JOURNAL_SOURCE = "journal"


def _journal_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


async def _read_file(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None,
) -> tuple[list[dict[str, Any]], bool]:
    if params.get("since") is not None or params.get("until") is not None:
        raise LinuxInspectError(
            "a log file has no times, so 'since' and 'until' cannot be used with a file; "
            "use the journal, or leave them out"
        )
    window = MAX_TAIL_LINES if params.get("filter") else int(params.get("lines", DEFAULT_LOG_LINES))
    confined = confine_read_path(params["source"])
    cmd = build_log_tail_command(confined, window)
    output = await _read_opened_file(connector, target, cmd, confined, operator)
    raw = output.content.splitlines()
    return [{"time": None, "line": line} for line in raw], len(raw) >= window


async def _read_journal(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None,
) -> tuple[list[dict[str, Any]], bool]:
    window = MAX_TAIL_LINES if params.get("filter") else int(params.get("lines", DEFAULT_LOG_LINES))
    journal_params: dict[str, Any] = {"lines": window}
    if params["source"] != JOURNAL_SOURCE:
        journal_params["unit"] = params["source"]
    since = parse_since(params.get("since"))
    until = parse_until(params.get("until"))
    if since is not None:
        journal_params["since"] = _journal_time(since)
    if until is not None:
        journal_params["until"] = _journal_time(until)
    answer = await linux_journal_tail(connector, target, journal_params, operator)
    entries = answer["rows"]
    rows = [{"time": entry["ts"], "line": entry["message"] or ""} for entry in entries]
    return rows, bool(answer.get("truncated")) or len(entries) >= window


async def linux_logs_read(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.logs.read``: read a log file or the systemd journal."""
    if params["source"].startswith("/"):
        rows, source_cut = await _read_file(connector, target, params, operator)
    else:
        rows, source_cut = await _read_journal(connector, target, params, operator)
    needle = params.get("filter")
    if needle:
        rows = [row for row in rows if needle in row["line"]]
    return build_log_answer(
        rows,
        lines=int(params.get("lines", DEFAULT_LOG_LINES)),
        keep="newest",
        source_cut=source_cut,
    )


_LOGS_READ_OP = LinuxOp(
    op_id="linux.logs.read",
    handler_attr="logs_read",
    summary="Read log lines of a host: an allow-listed log file or the systemd journal.",
    description=(
        "Reads log lines from an allow-listed file, a systemd unit or the whole journal, with "
        "since, until, lines and a text filter. Returns {rows: [{time, line}], total, "
        "truncated, covered, continuation}. A file has no times and refuses since and until. "
        "Read-only. Secrets in lines are hidden."
    ),
    parameter_schema={
        **log_read_parameter_schema(
            "What to read: an absolute log file path (for example /var/log/messages), "
            "'journal' for the whole systemd journal, or a systemd unit name (for example "
            "nfs-server.service)."
        ),
        "examples": [{"source": "nfs-server.service", "since": "15m", "filter": "error"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "time": {"type": ["string", "null"]},
                        "line": {"type": "string"},
                    },
                    "required": ["line"],
                },
            },
            "total": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "covered": {
                "type": ["object", "null"],
                "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
            },
            "continuation": {"type": ["string", "null"]},
        },
        "required": ["rows", "total", "truncated"],
        "additionalProperties": True,
    },
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
            "since": "'15m' or an RFC 3339 time. Journal and unit only.",
            "filter": "Plain text, case counts.",
        },
        "output_shape": (
            "{rows: [{time, line}], total, truncated, covered, continuation}. Rows are "
            "oldest first. Large answers become a result_query handle. Check 'covered' "
            "and 'truncated' before you conclude that something is absent from the log. "
            "The journal gives no continuation marker, so 'continuation' is null: read an "
            "earlier part with 'until'. Older lines are left out when 'truncated' is true."
        ),
        "next_step": (
            "If a unit name shows in the lines, call linux.service.status for it. To read "
            "an earlier part of the journal, call again with 'until'."
        ),
    },
)

#: The ops of this module, composed onto ``LINUX_OPS``.
LOGS_OPS: tuple[LinuxOp, ...] = (_LOGS_READ_OP,)
