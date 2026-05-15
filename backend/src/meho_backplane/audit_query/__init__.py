# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Audit-query substrate (G8.1-T1).

The package exposes the consumer-facing surface T2 (REST), T3 (CLI), and T4
(MCP) dispatch through:

* :func:`query_audit` — tenant-scoped paginated query handler.
* :class:`AuditQueryFilters` — input filter shape (frozen Pydantic v2).
* :class:`AuditEntry` — one row of the result (frozen Pydantic v2).
* :class:`AuditQueryResult` — page of rows plus forward-only ``next_cursor``.
* :class:`InvalidCursorError` — opaque-cursor decode failure.
* :class:`UnsupportedFilterError` — filter targets a column that does not yet
  exist in v0.2 (``parent_audit_id`` waits on G0.6-T7 #398;
  ``agent_session_id`` has no current roadmap).
* :func:`parse_duration` / :class:`DurationParseError` — duration shorthand
  (``"24h"`` / ``"7d"`` / ISO-8601) → :class:`datetime` parser used by the
  T2 REST router layer (G8.1-T2 #466).

See :mod:`.schemas` for the substrate-vs-issue-body reconciliation that
documents which fields are real columns, which are computed at query time,
and which are v0.2 placeholders.
"""

from __future__ import annotations

from .cursor import CursorPosition, InvalidCursorError, decode_cursor, encode_cursor
from .duration import DurationParseError, parse_duration
from .query import UnsupportedFilterError, query_audit
from .schemas import AuditEntry, AuditQueryFilters, AuditQueryResult

__all__ = [
    "AuditEntry",
    "AuditQueryFilters",
    "AuditQueryResult",
    "CursorPosition",
    "DurationParseError",
    "InvalidCursorError",
    "UnsupportedFilterError",
    "decode_cursor",
    "encode_cursor",
    "parse_duration",
    "query_audit",
]
