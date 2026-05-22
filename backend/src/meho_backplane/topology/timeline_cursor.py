# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Opaque forward-only cursor for the ``meho topology timeline`` page walk.

Initiative #365 (G9.3), Task #861 (T5). The timeline lists every
``graph_node_history`` + ``graph_edge_history`` mutation across the
tenant in ``(valid_from DESC, history_id DESC)`` order. OFFSET-based
pagination is broken under the diff-on-write hook (T2 #857), which
inserts history rows continuously as refresh and annotate paths
mutate the live graph -- a new row landing between page 1 and page 2
shifts every subsequent row by one, and the consumer either re-reads
or skips a row. A keyset cursor over the same lex order is
correctness-preserving: page N+1 starts at the row strictly after
page N's last row.

Tie-breaker shape
=================

``valid_from`` is **not** unique. The diff-on-write hook gives every
history row in one transaction the same ``valid_from`` so a refresh
that adds 5 nodes is queryable as a single point-in-time event
(:mod:`meho_backplane.topology.history` docstring). Within one
timestamp, ``history_id`` (``BIGSERIAL`` per :class:`GraphNodeHistory`
/ :class:`GraphEdgeHistory`) gives a strict total order.

But the timeline UNIONs two tables. ``graph_node_history.history_id``
and ``graph_edge_history.history_id`` are independent counters --
both can carry ``history_id=42`` for the same ``valid_from``. The
cursor therefore encodes a third discriminator -- ``source`` is
either ``"node"`` or ``"edge"`` -- so the keyset compare can
disambiguate "same valid_from, same history_id, different table" by
sorting one source before the other (the choice of sort order is
arbitrary but must be consistent; this module picks ``"edge"``
before ``"node"`` alphabetically so the SQL ``ORDER BY ... source``
ascending lands on the right side).

Wire format
===========

``urlsafe_b64encode(json.dumps({"ts": <iso>, "id": <int>, "src": "node"|"edge"}))``.
``urlsafe`` avoids ``+`` / ``/`` so the token survives unencoded as a
query parameter and as a JSON-string value without escaping. The
token is deliberately opaque so consumers treat it as
copy-and-paste continuation, not a parseable position.

Any tampering with the bytes produces an
:class:`InvalidTimelineCursorError` at decode time rather than a
silently-wrong query -- the decoder validates structure, ISO-8601
parse, integer parse, and source enum in turn.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "InvalidTimelineCursorError",
    "TimelineCursorPosition",
    "decode_timeline_cursor",
    "encode_timeline_cursor",
]


class InvalidTimelineCursorError(ValueError):
    """Raised when an opaque timeline cursor cannot be decoded.

    The token may be malformed base64, malformed JSON, missing a
    required field, or carry an invalid ISO-8601 / integer value or
    out-of-vocabulary source. Any of these are handled identically:
    the cursor is rejected and the caller decides whether to surface
    a 400 (REST), a CLI error, or an MCP -32602.
    """


@dataclass(frozen=True, slots=True)
class TimelineCursorPosition:
    """Decoded cursor: ``(valid_from, history_id, source)`` keyset position.

    ``source`` is the discriminator between the two history tables
    the UNION combines -- ``"node"`` for :class:`GraphNodeHistory` and
    ``"edge"`` for :class:`GraphEdgeHistory`. The handler's keyset
    compare uses all three components because ``history_id`` alone
    is not unique across the two tables (each table has its own
    ``BIGSERIAL`` counter).
    """

    ts: datetime
    history_id: int
    source: str


def encode_timeline_cursor(position: TimelineCursorPosition) -> str:
    """Encode a timeline keyset position as an opaque URL-safe base64 token.

    Source is restricted to ``"node"`` / ``"edge"`` at the caller; an
    out-of-vocabulary value is a bug, not an operator-facing condition,
    so this function does not validate it. The decoder, which receives
    untrusted bytes, does.
    """
    payload = {
        "ts": position.ts.isoformat(),
        "id": position.history_id,
        "src": position.source,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_timeline_cursor(token: str) -> TimelineCursorPosition:
    """Decode an opaque timeline cursor token back into a position.

    Validates each layer (base64, JSON, dict shape, ISO-8601 ``ts``,
    integer ``id``, ``src`` ∈ ``{"node","edge"}``) and raises
    :class:`InvalidTimelineCursorError` on the first failure. The
    original decode exception is preserved as ``__cause__`` so log
    lines can carry the underlying reason without leaking it through
    the operator-facing error.
    """
    try:
        raw = base64.b64decode(
            token.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise InvalidTimelineCursorError("cursor is not valid base64") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidTimelineCursorError("cursor payload is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise InvalidTimelineCursorError("cursor payload is not a JSON object")

    ts_raw = payload.get("ts")
    id_raw = payload.get("id")
    src_raw = payload.get("src")
    if not isinstance(ts_raw, str):
        raise InvalidTimelineCursorError("cursor payload missing 'ts' string")
    if not isinstance(id_raw, int) or isinstance(id_raw, bool):
        raise InvalidTimelineCursorError("cursor payload missing 'id' integer")
    if src_raw not in ("node", "edge"):
        raise InvalidTimelineCursorError("cursor 'src' must be 'node' or 'edge'")

    try:
        ts = datetime.fromisoformat(ts_raw)
    except ValueError as exc:
        raise InvalidTimelineCursorError("cursor 'ts' is not ISO-8601") from exc

    return TimelineCursorPosition(ts=ts, history_id=id_raw, source=src_raw)
