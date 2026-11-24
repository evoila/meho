# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.logs.read`` -- the governed log read of Kubernetes (group ``logs``).

The same parameters and the same answer as the log read of every other
connector (:mod:`meho_backplane.connectors.log_read`). It sits on top of
``k8s.logs``: it maps ``source`` to the pod, the namespace and the
container, maps ``since`` / ``until`` to the time window of ``k8s.logs``,
runs it, and adds the text filter and the shared answer shape.
``k8s.logs`` keeps its own parameters and answer.

``source`` is ``<namespace>/<pod>`` or ``<namespace>/<pod>/<container>``.
The pod may be a unique prefix, as in ``k8s.logs``. With ``since`` the
rows carry their time, oldest first, and ``continuation`` is the time to
give as ``since`` to read on, with no repeat and no gap: the exact
nanosecond times go to ``k8s.logs`` as they are, and a page ends only
between two times. Without ``since`` the newest lines are read and have
no time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_logs import (
    MAX_TAIL_LINES,
    LogWindowError,
    k8s_logs,
    parse_rfc3339,
)
from meho_backplane.connectors.log_read import (
    DEFAULT_LOG_LINES,
    LOG_READ_RESPONSE_SCHEMA,
    REDACTION_CONTEXT_LINES,
    LogEntry,
    LogReadError,
    build_log_answer,
    is_span,
    log_read_parameter_schema,
    parse_since,
    redact_entries,
    take_page,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = ["OPS", "k8s_logs_read"]

OP_ID = "k8s.logs.read"


def _exact_since(value: str) -> str:
    """Return the start as an RFC 3339 time; an exact time stays as it is."""
    start = parse_since(value) if is_span(value) else None
    return value if start is None else start.strftime("%Y-%m-%dT%H:%M:%SZ")


def _split_source(source: str) -> tuple[str, str, str | None]:
    parts = source.split("/")
    if len(parts) not in (2, 3) or not all(parts):
        raise LogReadError(
            f"source {source!r} must be '<namespace>/<pod>' or '<namespace>/<pod>/<container>'"
        )
    return parts[0], parts[1], parts[2] if len(parts) == 3 else None


def _timed_entries(lines: list[str]) -> list[LogEntry]:
    """Split each line into its time and its text.

    A line without a time (should not happen) belongs to the time before it.
    """
    entries: list[LogEntry] = []
    group: tuple[int, int] | None = None
    for text in lines:
        stamp, _, rest = text.partition(" ")
        try:
            group = parse_rfc3339(stamp)
        except LogWindowError:
            entries.append(LogEntry(time=None, line=text, group=group))
        else:
            entries.append(LogEntry(time=stamp, line=rest, group=group))
    return entries


async def k8s_logs_read(
    connector: Any,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.logs.read``: read the log of one container."""
    namespace, pod, container = _split_source(params["source"])
    lines = int(params.get("lines", DEFAULT_LOG_LINES))
    needle = params.get("filter")
    since, until = params.get("since"), params.get("until")
    if until is not None and since is None:
        raise LogReadError("'until' needs 'since'")

    inner: dict[str, Any] = {"pod_name": pod, "namespace": namespace}
    if container is not None:
        inner["container"] = container
    if since is not None:
        inner["since_time"] = _exact_since(since)
        if until is not None:
            inner["until_time"] = until
    else:
        wide = MAX_TAIL_LINES if needle else lines + REDACTION_CONTEXT_LINES
        inner["tail"] = min(wide, MAX_TAIL_LINES)

    answer = await k8s_logs(connector, target, operator, inner)
    raw: list[str] = answer["lines"]
    source_cut = bool(answer["truncated"])
    if since is not None:
        entries = _timed_entries(raw)
    else:
        entries = [LogEntry(time=None, line=text) for text in raw]
        source_cut = source_cut or len(raw) >= inner["tail"]
    entries = await redact_entries(entries, connector=connector, operator=operator, op_id=OP_ID)
    page = await take_page(
        entries, lines=lines, needle=needle, keep="newest" if since is None else "oldest"
    )
    continuation: str | None = answer.get("next_since_time")
    if since is not None and page.rest:
        continuation = page.rest[0].time
    return build_log_answer(page, source_cut=source_cut, continuation=continuation)


_LOGS_READ_OP = KubernetesOp(
    op_id=OP_ID,
    handler_attr="logs_read",
    summary="Read log lines of one container, in the shared log read shape.",
    description=(
        "Reads log lines of one container, with since, until, lines and a text filter. "
        "Returns {rows: [{time, line, line_truncated}], total, truncated, covered, "
        "continuation}. With since, rows carry times and continuation pages forward. "
        "Read-only. Secrets in lines are hidden. k8s.logs stays."
    ),
    parameter_schema={
        **log_read_parameter_schema(
            "Which container log: '<namespace>/<pod>' or '<namespace>/<pod>/<container>'."
        ),
        "examples": [{"source": "prod/web-1/app", "since": "15m", "filter": "error"}],
    },
    response_schema=LOG_READ_RESPONSE_SCHEMA,
    group_key="logs",
    tags=("read-only", "pod", "logs"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read what a container logged, with an optional time window and text "
            "match. It takes the same parameters as the log read of any other connector."
        ),
        "parameter_hints": {
            "source": "'<namespace>/<pod>/<container>'; leave the container out for a "
            "single-container pod.",
            "since": "'15m' or an RFC 3339 time. With it, rows have times and 'continuation' "
            "pages forward.",
            "filter": "Plain text, case counts.",
        },
        "output_shape": (
            "{rows: [{time, line, line_truncated}], total, truncated, covered, continuation}. "
            "Large answers become a result_query handle. 'covered' is the time span that was "
            "searched, also when no line matches; it is null without 'since'. "
            "'line_truncated' marks a line that was cut at 4096 characters; the filter looks "
            "at the whole line. Kubernetes keeps only part of a log: check 'covered' and "
            "'truncated' before you conclude that something is absent."
        ),
        "next_step": (
            "To read on, call again with 'since' set to 'continuation'. For the events of "
            "the pod, call k8s.event.list."
        ),
    },
)

#: Collected by :func:`~meho_backplane.connectors.kubernetes.ops.collect_ops`.
OPS: tuple[KubernetesOp, ...] = (_LOGS_READ_OP,)
