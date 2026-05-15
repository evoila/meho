# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Opaque forward-only cursor for ``audit_log`` pagination.

The audit-query API paginates by (``occurred_at`` DESC, ``id`` DESC) — the
natural read order for "newest first" forensic reconstruction. OFFSET-based
pagination is broken under concurrent inserts (a row written between page 1
and page 2 shifts every subsequent row by one and the consumer either re-reads
or skips a row). A keyset cursor over the same lex order is correctness-
preserving: page N+1 starts at the row strictly after page N's last row.

The encoded form is deliberately opaque (base64-encoded JSON) so consumers
treat it as a token rather than parsing it. Any tampering with the bytes
produces an :class:`InvalidCursorError` at decode time rather than a silently
wrong query — the decoder validates structure, ISO-8601 parse, and UUID parse
in turn.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "CursorPosition",
    "InvalidCursorError",
    "decode_cursor",
    "encode_cursor",
]


class InvalidCursorError(ValueError):
    """Raised when an opaque cursor token cannot be decoded.

    The token may be malformed base64, malformed JSON, missing a required
    field, or carry an invalid ISO-8601 / UUID value. Any of these are
    handled identically: the cursor is rejected and the caller decides
    whether to surface a 400 (REST), a CLI error, or an MCP -32602.
    """


@dataclass(frozen=True, slots=True)
class CursorPosition:
    """Decoded cursor: the ``(occurred_at, id)`` keyset position."""

    ts: datetime
    id: uuid.UUID


def encode_cursor(position: CursorPosition) -> str:
    """Encode a keyset position as an opaque URL-safe base64 token.

    The wire format is ``urlsafe_b64encode(json.dumps({"ts": <iso>, "id": <uuid>}))``.
    ``urlsafe`` avoids ``+`` / ``/`` so the token survives unencoded as a query
    parameter and as a JSON-string value without escaping.
    """
    payload = {"ts": position.ts.isoformat(), "id": str(position.id)}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(token: str) -> CursorPosition:
    """Decode an opaque cursor token back into a :class:`CursorPosition`.

    Validates each layer (base64, JSON, dict shape, ISO-8601 ``ts``, UUID ``id``)
    and raises :class:`InvalidCursorError` on the first failure. The original
    decode exception is preserved as ``__cause__`` so log lines can carry the
    underlying reason without leaking it through the operator-facing error.
    """
    try:
        raw = base64.b64decode(
            token.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise InvalidCursorError("cursor is not valid base64") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("cursor payload is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise InvalidCursorError("cursor payload is not a JSON object")

    ts_raw = payload.get("ts")
    id_raw = payload.get("id")
    if not isinstance(ts_raw, str) or not isinstance(id_raw, str):
        raise InvalidCursorError("cursor payload missing 'ts' or 'id' string")

    try:
        ts = datetime.fromisoformat(ts_raw)
    except ValueError as exc:
        raise InvalidCursorError("cursor 'ts' is not ISO-8601") from exc

    try:
        cursor_id = uuid.UUID(id_raw)
    except ValueError as exc:
        raise InvalidCursorError("cursor 'id' is not a UUID") from exc

    return CursorPosition(ts=ts, id=cursor_id)
