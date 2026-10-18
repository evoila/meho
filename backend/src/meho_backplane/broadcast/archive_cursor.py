# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Opaque cursors over the announcement archive (#3748).

Two cursor kinds, kept apart on purpose:

* A **watch checkpoint** (``w1.<data>``) says "I have seen every
  announcement up to sequence number N". ``meho_broadcast_watch`` takes
  it and returns the next one. ``meho_broadcast_recent`` returns one for
  the moment it read, and the announce ack returns one for the new
  announcement.
* A **history page cursor** (``h1.<data>``) continues one
  ``meho_broadcast_recent`` read: the fixed upper end of that read, the
  last sequence number already scanned, and the start of the time
  window.

Both are bound to a tenant. A cursor from another tenant is refused, so
a cursor copied between tenants cannot silently read from the wrong
place. The ``1`` in the prefix is the format version; a later format
gets a new prefix and old cursors are refused with a clear message.

The data part is URL-safe base64 of fixed-size big-endian integers. It
is opaque to callers but not secret: it holds only the tenant id and
sequence numbers, and every read is scoped to the caller's own tenant
anyway.
"""

from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

__all__ = [
    "HistoryPosition",
    "InvalidArchiveCursorError",
    "decode_history_cursor",
    "decode_watch_checkpoint",
    "encode_history_cursor",
    "encode_watch_checkpoint",
]

_WATCH_PREFIX: Final[str] = "w1."
_HISTORY_PREFIX: Final[str] = "h1."

#: tenant UUID bytes + checkpoint sequence number.
_WATCH_LAYOUT: Final[struct.Struct] = struct.Struct(">16sQ")
#: tenant UUID bytes + read upper end + last scanned seq + window start (ms).
_HISTORY_LAYOUT: Final[struct.Struct] = struct.Struct(">16sQQq")


class InvalidArchiveCursorError(ValueError):
    """The cursor is malformed, from another tenant, or the wrong kind."""


@dataclass(frozen=True)
class HistoryPosition:
    """Where one ``meho_broadcast_recent`` read continues.

    * ``watermark`` -- the highest sequence number the read covers. Fixed
      for every page of one read, so the pages form one consistent view.
    * ``after_seq`` -- the last sequence number already scanned.
    * ``since`` -- the start of the time window.
    """

    watermark: int
    after_seq: int
    since: datetime


def _pack(prefix: str, data: bytes) -> str:
    return prefix + base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unpack(
    raw: str, tenant_id: UUID, prefix: str, layout: struct.Struct, what: str
) -> tuple[Any, ...]:
    """Decode *raw* and check its tenant; return the fields after the tenant."""
    if not raw.startswith(prefix):
        raise InvalidArchiveCursorError(f"cursor: not a {what}")
    body = raw[len(prefix) :]
    try:
        fields = layout.unpack(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (binascii.Error, ValueError, struct.error) as exc:
        raise InvalidArchiveCursorError(f"cursor: malformed {what}") from exc
    if fields[0] != tenant_id.bytes:
        raise InvalidArchiveCursorError(f"cursor: this {what} belongs to another tenant")
    return tuple(fields[1:])


def encode_watch_checkpoint(tenant_id: UUID, seq: int) -> str:
    """Encode "seen everything up to *seq*" for *tenant_id*."""
    return _pack(_WATCH_PREFIX, _WATCH_LAYOUT.pack(tenant_id.bytes, seq))


def decode_watch_checkpoint(raw: str, tenant_id: UUID) -> int:
    """Return the sequence number in *raw*; refuse another tenant's checkpoint."""
    (seq,) = _unpack(raw, tenant_id, _WATCH_PREFIX, _WATCH_LAYOUT, "watch checkpoint")
    return int(seq)


def encode_history_cursor(tenant_id: UUID, position: HistoryPosition) -> str:
    """Encode where the next page of one recent read starts."""
    since_ms = int(position.since.timestamp() * 1000)
    data = _HISTORY_LAYOUT.pack(tenant_id.bytes, position.watermark, position.after_seq, since_ms)
    return _pack(_HISTORY_PREFIX, data)


def decode_history_cursor(raw: str, tenant_id: UUID) -> HistoryPosition:
    """Return the page position in *raw*; refuse another tenant's cursor."""
    watermark, after_seq, since_ms = _unpack(
        raw, tenant_id, _HISTORY_PREFIX, _HISTORY_LAYOUT, "history page cursor"
    )
    return HistoryPosition(
        watermark=int(watermark),
        after_seq=int(after_seq),
        since=datetime.fromtimestamp(int(since_ms) / 1000, UTC),
    )
