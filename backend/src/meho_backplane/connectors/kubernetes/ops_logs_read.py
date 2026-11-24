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
give as ``since`` to read on, with no repeat and no gap. Without
``since`` the newest lines are read and have no time.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_logs import MAX_TAIL_LINES, k8s_logs
from meho_backplane.connectors.log_read import (
    DEFAULT_LOG_LINES,
    LogReadError,
    build_log_answer,
    log_read_parameter_schema,
    parse_since,
    parse_until,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = ["OPS", "k8s_logs_read"]

_TIME_TOKEN = re.compile(r"^\d{4}-\d{2}-\d{2}T\S+Z$")


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split_source(source: str) -> tuple[str, str, str | None]:
    parts = source.split("/")
    if len(parts) not in (2, 3) or not all(parts):
        raise LogReadError(
            f"source {source!r} must be '<namespace>/<pod>' or '<namespace>/<pod>/<container>'"
        )
    return parts[0], parts[1], parts[2] if len(parts) == 3 else None


def _row(text: str, *, timed: bool) -> dict[str, Any]:
    if timed:
        stamp, _, rest = text.partition(" ")
        if _TIME_TOKEN.match(stamp):
            return {"time": stamp, "line": rest}
    return {"time": None, "line": text}


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
    since = parse_since(params.get("since"))
    until = parse_until(params.get("until"))
    if until is not None and since is None:
        raise LogReadError("'until' needs 'since'")

    inner: dict[str, Any] = {"pod_name": pod, "namespace": namespace}
    if container is not None:
        inner["container"] = container
    timed = since is not None
    if since is not None:
        inner["since_time"] = _rfc3339(since)
        if until is not None:
            inner["until_time"] = _rfc3339(until)
        keep = "oldest"
    else:
        inner["tail"] = MAX_TAIL_LINES if needle else lines
        keep = "newest"

    page = await k8s_logs(connector, target, operator, inner)
    raw: list[str] = page["lines"]
    rows = [_row(text, timed=timed) for text in raw]
    if needle:
        rows = [row for row in rows if needle in row["line"]]
    source_cut = bool(page["truncated"]) or (not timed and len(raw) >= inner["tail"])
    continuation: str | None = page.get("next_since_time")
    if timed and len(rows) > lines:
        continuation = rows[lines]["time"] or continuation
    return build_log_answer(
        rows, lines=lines, keep=keep, source_cut=source_cut, continuation=continuation
    )


_LOGS_READ_OP = KubernetesOp(
    op_id="k8s.logs.read",
    handler_attr="logs_read",
    summary="Read log lines of one container, in the shared log read shape.",
    description=(
        "Reads log lines of one container, with since, until, lines and a text filter. "
        "Returns {rows: [{time, line}], total, truncated, covered, continuation}. With since, "
        "rows carry times and continuation pages forward. Read-only. Secrets in lines are "
        "hidden. k8s.logs stays."
    ),
    parameter_schema={
        **log_read_parameter_schema(
            "Which container log: '<namespace>/<pod>' or '<namespace>/<pod>/<container>'."
        ),
        "examples": [{"source": "prod/web-1/app", "since": "15m", "filter": "error"}],
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
            "{rows: [{time, line}], total, truncated, covered, continuation}. Large answers "
            "become a result_query handle. Kubernetes keeps only part of a log: check "
            "'covered' and 'truncated' before you conclude that something is absent."
        ),
        "next_step": (
            "To read on, call again with 'since' set to 'continuation'. For the events of "
            "the pod, call k8s.event.list."
        ),
    },
)

#: Collected by :func:`~meho_backplane.connectors.kubernetes.ops.collect_ops`.
OPS: tuple[KubernetesOp, ...] = (_LOGS_READ_OP,)
