# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bounded ``query_topology`` answers with explicit completeness (#3764).

Helpers for :mod:`meho_backplane.mcp.tools.topology`. A closure or
history answer goes out in one of two ways:

* small: the rows stay inline, as before;
* large: the rows are kept behind a result handle by the shared
  :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
  (same spill, same tenant + caller ownership check on ``result_query``,
  same store caps) and the answer carries the reducer summary and the
  handle.

Every answer carries a ``completeness`` block. It names each limit that
applied, so the answer never claims a complete result after a limit cut
it. The finished answer is measured against the shared completed-call
wire ceiling from :mod:`meho_backplane.operations.delivery_budget` and
made smaller, step by step, until it fits; each smaller answer says what
it left out.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final, Literal

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.schemas import ResultHandle
from meho_backplane.operations.delivery_budget import (
    handle_is_readable,
    measure_delivered_wire_bytes,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

__all__ = ["COMPLETENESS_SCHEMA", "AnswerLimit", "bounded_answer"]

_log = structlog.get_logger(__name__)

#: A limit that kept rows out of an answer. The first four come from the
#: closure traversal (:class:`~meho_backplane.topology.query.ClosureLimit`),
#: ``row_limit`` from the history ``limit``; the last three from keeping
#: and sending the rows.
AnswerLimit = Literal[
    "hop_limit",
    "node_limit",
    "byte_limit",
    "time_budget",
    "row_limit",
    "store_cap",
    "store_unavailable",
    "delivery_cap",
]

#: Limits after which some rows exist that the answer does not hold.
_INCOMPLETE: Final[frozenset[AnswerLimit]] = frozenset(
    {"hop_limit", "node_limit", "byte_limit", "row_limit", "store_cap"}
)
#: Limits after which the answer holds no readable rows at all.
_UNAVAILABLE: Final[frozenset[AnswerLimit]] = frozenset({"time_budget", "store_unavailable"})

_NARROW: Final[str] = (
    "Narrow the query with `relation_class`, `kind_filter`, a smaller `depth` "
    "or `include_stale: false`."
)

#: Plain-language next step for each limit, in the order they apply.
_GUIDANCE: Final[dict[AnswerLimit, str]] = {
    "hop_limit": (
        "More nodes exist past the requested depth. Raise `depth`, or start a "
        "new query from a node at the last depth."
    ),
    "node_limit": f"The traversal stopped at the node limit. {_NARROW}",
    "byte_limit": f"The traversal stopped at the byte limit. {_NARROW}",
    "time_budget": (
        f"The traversal ran out of time and returned no nodes. {_NARROW} "
        "Do not read this as 'nothing depends on it'."
    ),
    "row_limit": (
        "The answer reached `limit`, so older rows may exist. Narrow the time "
        "window with `since` / `until`."
    ),
    "store_cap": (
        "The result store kept only the first rows (see `retained`). Rows after "
        "them cannot be read. Narrow the query to see them."
    ),
    "store_unavailable": (
        "The result store did not keep the rows, so they cannot be read through "
        "`result_query`. Try again later or narrow the query."
    ),
    "delivery_cap": (
        "The answer was too large to send whole. Read the rows with "
        "`result_query` and the handle when `delivery` is `partial`; when it is "
        "`unavailable`, narrow the query."
    ),
}

#: ``outputSchema`` fragment for the ``completeness`` block.
COMPLETENESS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": (
        "Present on closure and `history` answers. `state` is `complete` (every "
        "row is in the answer or behind `handle`), `incomplete` (a limit kept "
        "some rows out; the answer is the first part in its stated order) or "
        "`unavailable` (no rows can be read). `limits_hit` names each limit that "
        "applied: `hop_limit`, `node_limit`, `byte_limit`, `time_budget`, "
        "`row_limit`, `store_cap`, `store_unavailable`, `delivery_cap`. `found` "
        "is how many rows the query produced and `retained` how many can be "
        "read. `limits` shows the limits used and `guidance` the next step."
    ),
    "properties": {
        "state": {"type": "string", "enum": ["complete", "incomplete", "unavailable"]},
        "limits_hit": {"type": "array", "items": {"type": "string"}},
        "found": {"type": "integer", "minimum": 0},
        "retained": {"type": "integer", "minimum": 0},
        "limits": {"type": "object"},
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["state", "limits_hit", "found", "retained", "limits", "guidance"],
}

#: One reducer for every topology answer; it resolves the store and the
#: thresholds lazily, per call, exactly like the ``list_targets`` reducer.
_REDUCER: Final[JsonFluxReducer] = JsonFluxReducer()

#: ``op_id`` the spilled rows are stored under.
_OP_ID: Final[str] = "query_topology"


def _completeness(
    limits_hit: list[AnswerLimit],
    *,
    found: int,
    retained: int,
    limits: dict[str, int],
    readable: bool = True,
) -> dict[str, Any]:
    """Build the ``completeness`` block from the limits that applied.

    *readable* is false when the delivery step dropped the rows and there
    is no handle to read them through; the state is then ``unavailable``.
    """
    hit = set(limits_hit)
    if hit & _UNAVAILABLE or not readable:
        state = "unavailable"
    elif hit & _INCOMPLETE:
        state = "incomplete"
    else:
        state = "complete"
    guidance = " ".join(_GUIDANCE[limit] for limit in limits_hit) or None
    return {
        "state": state,
        "limits_hit": list(limits_hit),
        "found": found,
        "retained": retained,
        "limits": limits,
        "guidance": guidance,
    }


def _handle_payload(handle: ResultHandle, preview_omits: tuple[str, ...]) -> dict[str, Any]:
    """Serialize *handle*, leaving *preview_omits* keys out of its preview rows."""
    payload = handle.model_dump(mode="json")
    if preview_omits and payload.get("sample_rows"):
        payload["sample_rows"] = [
            {key: value for key, value in row.items() if key not in preview_omits}
            for row in payload["sample_rows"]
        ]
    return payload


async def _retain(
    operator: Operator,
    rows_key: str,
    rows: list[dict[str, Any]],
    limits_hit: list[AnswerLimit],
    preview_omits: tuple[str, ...],
) -> tuple[dict[str, Any], int, ResultHandle | None]:
    """Keep *rows* inline or behind a handle; return the body part and the readable count."""
    summary, handle = await _REDUCER.reduce(
        {rows_key: rows},
        None,
        {
            "op_id": _OP_ID,
            "tenant_id": str(operator.tenant_id),
            "operator_sub": operator.sub,
        },
    )
    if handle is None:
        return {rows_key: rows}, len(rows), None
    drill_in = handle.fetch_more.drill_in if handle.fetch_more is not None else None
    retained = (drill_in.stored_rows or 0) if drill_in is not None and drill_in.available else 0
    if retained == 0:
        limits_hit.append("store_unavailable")
    elif retained < len(rows):
        limits_hit.append("store_cap")
    # Rows the reducer could not profile come back as ``status:
    # unprofiled`` with a ``reason``. ``status`` means something else on
    # this tool, so the answer keeps only ``reason`` and says the rest
    # through ``completeness`` (the store kept no rows).
    body = {key: value for key, value in summary.items() if key != "status"}
    body["handle"] = _handle_payload(handle, preview_omits)
    if preview_omits:
        body["preview_omits"] = list(preview_omits)
    return body, retained, handle


def _finished(
    body: dict[str, Any],
    delivery: Literal["complete", "partial", "unavailable"],
    limits_hit: list[AnswerLimit],
    *,
    found: int,
    retained: int,
    limits: dict[str, int],
    over_size: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Return *body* as a finished answer with ``completeness`` and ``delivery``.

    *over_size* is ``(delivered_bytes, delivery_max_bytes)`` of the whole
    answer that did not fit, so a smaller answer can say why it is smaller.
    """
    answer = dict(body)
    answer["completeness"] = _completeness(
        limits_hit,
        found=found,
        retained=retained,
        limits=limits,
        readable=delivery != "unavailable",
    )
    answer["delivery"] = delivery
    if over_size is not None:
        answer["delivered_bytes"], answer["delivery_max_bytes"] = over_size
    return answer


def _smaller_answers(
    body: dict[str, Any],
    rows_key: str,
    handle: ResultHandle | None,
    limits_hit: list[AnswerLimit],
    *,
    found: int,
    retained: int,
    limits: dict[str, int],
    over_size: tuple[int, int],
) -> Iterator[dict[str, Any]]:
    """Yield ever smaller finished answers for a body over the wire ceiling.

    ``partial`` (only with a readable handle): the inline preview is
    dropped and the rows stay readable through the handle. ``unavailable``:
    the inline rows and the handle are dropped. The last resort keeps only
    ``kind``, ``completeness`` and ``delivery``. Nothing is cut without
    saying so.
    """
    if handle_is_readable(handle):
        partial = {key: value for key, value in body.items() if key != rows_key}
        partial["handle"] = {**body["handle"], "sample_rows": None}
        yield _finished(
            partial,
            "partial",
            limits_hit,
            found=found,
            retained=retained,
            limits=limits,
            over_size=over_size,
        )
    without_rows = {key: value for key, value in body.items() if key not in (rows_key, "handle")}
    receipt = {"kind": body["kind"]}
    for smaller in (without_rows, receipt):
        yield _finished(
            smaller,
            "unavailable",
            limits_hit,
            found=found,
            retained=0,
            limits=limits,
            over_size=over_size,
        )


async def bounded_answer(
    operator: Operator,
    envelope: dict[str, Any],
    rows_key: str,
    rows: list[dict[str, Any]],
    *,
    query_limit: AnswerLimit | None,
    limits: dict[str, int],
    preview_omits: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return *envelope* with *rows* delivered under the shared result contracts.

    *query_limit* is the limit the query itself hit (a closure
    :data:`~meho_backplane.topology.query.ClosureLimit` or ``row_limit``),
    or ``None``. *limits* is echoed in ``completeness.limits``.
    *preview_omits* names row keys left out of the handle's inline preview
    (history leaves out ``snapshot``; the full rows stay behind the handle).

    The finished answer, ``completeness`` and ``delivery`` included, is
    measured on the wire. Over the ceiling, each smaller answer from
    :func:`_smaller_answers` is measured in turn and the first one that
    fits is returned. The last resort fits by construction for any ceiling
    the settings accept; it is still measured, and a warning is logged if
    it is ever over, because returning it beats returning nothing.
    """
    limits_hit: list[AnswerLimit] = [query_limit] if query_limit else []
    part, retained, handle = await _retain(operator, rows_key, rows, limits_hit, preview_omits)
    body = {**envelope, **part}
    found = len(rows)
    answer = _finished(body, "complete", limits_hit, found=found, retained=retained, limits=limits)
    max_bytes = get_settings().result_complete_response_wire_max_bytes
    size = measure_delivered_wire_bytes(answer)
    if size <= max_bytes:
        return answer
    limits_hit = [*limits_hit, "delivery_cap"]
    for answer in _smaller_answers(
        body,
        rows_key,
        handle,
        limits_hit,
        found=found,
        retained=retained,
        limits=limits,
        over_size=(size, max_bytes),
    ):
        size = measure_delivered_wire_bytes(answer)
        if size <= max_bytes:
            return answer
    _log.warning("topology_answer_over_ceiling", delivered_bytes=size, delivery_max_bytes=max_bytes)
    return answer
