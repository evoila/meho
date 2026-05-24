# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Validation tests for the audit-query Pydantic schemas (G8.1-T1).

Covers the contract of :class:`~meho_backplane.audit_query.AuditQueryFilters`
and :class:`~meho_backplane.audit_query.AuditEntry`:

* ``frozen=True`` blocks runtime field reassignment on every model.
* ``limit`` validation range (1-1000) — boundary tests prove the bounds
  bite at the Pydantic layer so the SQL handler never sees an out-of-range
  ``LIMIT`` value.
* Defaults: every filter is optional, ``limit`` defaults to 100, ``cursor``
  defaults to None. Empty :class:`AuditQueryFilters` is a valid construction
  ("show me the most recent 100 rows scoped to the tenant").
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from meho_backplane.audit_query import AuditEntry, AuditQueryFilters, AuditQueryResult

# ---------------------------------------------------------------------------
# AuditQueryFilters
# ---------------------------------------------------------------------------


def test_filters_empty_construction_uses_defaults() -> None:
    """Empty filter set is valid; ``limit=100``, every other field None."""
    f = AuditQueryFilters()

    assert f.limit == 100
    assert f.cursor is None
    assert f.target is None
    assert f.principal is None
    assert f.op_id is None
    assert f.op_class is None
    assert f.result_status is None
    assert f.since is None
    assert f.until is None
    assert f.audit_id is None
    assert f.parent_audit_id is None
    assert f.agent_session_id is None


def test_filters_limit_lower_bound_rejected() -> None:
    """``limit=0`` raises a ``ValidationError`` — must be >= 1."""
    with pytest.raises(ValidationError):
        AuditQueryFilters(limit=0)


def test_filters_limit_upper_bound_rejected() -> None:
    """``limit=1001`` raises a ``ValidationError`` — must be <= 1000."""
    with pytest.raises(ValidationError):
        AuditQueryFilters(limit=1001)


def test_filters_limit_bounds_accepted() -> None:
    """Both ends of the inclusive range accept cleanly."""
    assert AuditQueryFilters(limit=1).limit == 1
    assert AuditQueryFilters(limit=1000).limit == 1000


def test_filters_frozen_blocks_reassignment() -> None:
    """``frozen=True`` is load-bearing — a filter is a value, not a builder."""
    f = AuditQueryFilters(limit=50)
    with pytest.raises(ValidationError):
        f.limit = 100  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AuditEntry
# ---------------------------------------------------------------------------


def _make_entry(**overrides: object) -> AuditEntry:
    """Build a populated :class:`AuditEntry` with every field set.

    Every field on the model is required (none carries a Pydantic default), so
    the helper spells out all of them — including the two v0.2 placeholders
    that the handler always sets to None (``principal_name`` /
    ``broadcast_event_id``) and the two lineage columns the handler now
    populates from the row (``parent_audit_id`` / ``agent_session_id``).
    """
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "ts": datetime.now(UTC),
        "tenant_id": uuid.uuid4(),
        "principal_sub": "operator-1",
        "principal_name": None,
        "target_id": None,
        "target_name": None,
        "method": "GET",
        "path": "/api/v1/healthz",
        "status_code": 200,
        "request_id": None,
        "duration_ms": Decimal("3.14"),
        "payload": {},
        "op_id": "http.get:/api/v1/healthz",
        "op_class": "other",
        "result_status": "ok",
        "parent_audit_id": None,
        "agent_session_id": None,
        "broadcast_event_id": None,
    }
    defaults.update(overrides)
    return AuditEntry(**defaults)  # type: ignore[arg-type]


def test_entry_round_trip_preserves_every_field() -> None:
    """Construction + read-back of an :class:`AuditEntry` is lossless."""
    payload = {"op_id": "vsphere.vm.list", "op_class": "read"}
    target_id = uuid.uuid4()
    entry = _make_entry(
        target_id=target_id,
        target_name="rdc-vcenter",
        payload=payload,
        op_id="vsphere.vm.list",
        op_class="read",
    )

    assert entry.target_id == target_id
    assert entry.target_name == "rdc-vcenter"
    assert entry.payload == payload
    assert entry.op_id == "vsphere.vm.list"
    assert entry.op_class == "read"


def test_entry_frozen_blocks_reassignment() -> None:
    """``frozen=True`` so a row handed to the caller cannot mutate."""
    entry = _make_entry()
    with pytest.raises(ValidationError):
        entry.status_code = 500  # type: ignore[misc]


def test_entry_placeholders_default_to_none() -> None:
    """The two remaining v0.2 placeholder fields stay None.

    ``parent_audit_id`` (#398) and ``agent_session_id`` (#1009) are now real
    columns the handler reads off the row; only ``principal_name`` and
    ``broadcast_event_id`` are still unconditionally None in v0.2.
    """
    entry = _make_entry()
    assert entry.principal_name is None
    assert entry.broadcast_event_id is None


# ---------------------------------------------------------------------------
# AuditQueryResult
# ---------------------------------------------------------------------------


def test_result_with_cursor_round_trip() -> None:
    """``AuditQueryResult`` carries rows + optional ``next_cursor``."""
    rows = [_make_entry() for _ in range(3)]
    result = AuditQueryResult(rows=rows, next_cursor="opaque-token-bytes")
    assert len(result.rows) == 3
    assert result.next_cursor == "opaque-token-bytes"


def test_result_empty_terminal_page() -> None:
    """A terminal page has ``next_cursor=None`` and possibly an empty row list."""
    result = AuditQueryResult(rows=[], next_cursor=None)
    assert result.rows == []
    assert result.next_cursor is None
