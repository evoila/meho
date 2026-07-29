# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Projection + shared helpers for the ``/ui/checks`` console surface (#2506).

The list / detail handlers project
:class:`~meho_backplane.checks.dashboard_schemas.DashboardRead` /
:class:`~meho_backplane.checks.dashboard_schemas.DashboardDetail` into the
flat dict shape the templates read. Keeping the projection here (not in the
route modules) makes the row-to-view mapping unit-testable without a FastAPI
request fixture and shares one badge vocabulary across the list + detail
pages.

UTC coercion mirrors the scheduler surface: ``DateTime(timezone=True)``
columns round-trip naive on the SQLite test driver and aware on PG, and the
templates do ``now_utc - ts`` arithmetic, so every timestamp is coerced to
tz-aware UTC before it reaches a template.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from meho_backplane.checks.dashboard_schemas import (
    DashboardDetail,
    DashboardMemberView,
    DashboardRead,
)

__all__ = [
    "coerce_utc_aware",
    "project_dashboard_to_row",
    "project_detail",
    "state_badge_class",
]


#: DaisyUI badge modifier per five-state value. ``ok`` is healthy (success),
#: ``degraded`` is the middle warning, ``critical`` is the worst (error);
#: ``unknown`` (stale / never-read) and ``skip`` (unreachable-by-design) are
#: muted so a first-class non-failing state never reads as an alarm. Unknown
#: values fall through to the neutral ghost badge so a future state never
#: renders unstyled.
_STATE_BADGE: Final[dict[str, str]] = {
    "ok": "badge-success",
    "degraded": "badge-warning",
    "critical": "badge-error",
    "unknown": "badge-ghost",
    "skip": "badge-outline",
}


def coerce_utc_aware(ts: datetime | None) -> datetime | None:
    """Normalise *ts* to a tz-aware UTC :class:`datetime` (``None`` passes through).

    The SQLite test driver hands back naive datetimes for the
    ``DateTime(timezone=True)`` columns; the templates do timedelta
    arithmetic against a tz-aware "now", so a naive value would raise
    ``TypeError`` mid-render.
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def state_badge_class(state: str) -> str:
    """Return the DaisyUI badge modifier class for a five-state value."""
    return _STATE_BADGE.get(state, "badge-ghost")


def project_dashboard_to_row(dashboard: DashboardRead) -> dict[str, object]:
    """Project one Dashboard list row into the flat dict the template reads."""
    return {
        "id": str(dashboard.id),
        "name": dashboard.name,
        "description": dashboard.description,
        "member_count": dashboard.member_count,
        "state": dashboard.state,
        "state_badge": state_badge_class(dashboard.state),
        "updated_at": coerce_utc_aware(dashboard.updated_at),
    }


def _project_member(member: DashboardMemberView) -> dict[str, object]:
    """Project one member row for the detail table."""
    return {
        "sensor_id": str(member.sensor_id),
        "name": member.name,
        "connector_id": member.connector_id,
        "op_id": member.op_id,
        "raw_state": member.raw_state,
        "raw_state_badge": state_badge_class(member.raw_state),
        "effective_state": member.effective_state,
        "effective_state_badge": state_badge_class(member.effective_state),
        "pending": member.pending,
        "severity": member.severity.value,
        "for_seconds": member.for_seconds,
        "status": member.status.value,
        "state_since": coerce_utc_aware(member.state_since),
        "last_value": member.last_value,
        "last_evidence": member.last_evidence,
        "last_evaluated_at": coerce_utc_aware(member.last_evaluated_at),
        "next_fire_at": coerce_utc_aware(member.next_fire_at),
    }


def project_detail(detail: DashboardDetail) -> dict[str, object]:
    """Project a Dashboard detail (+ its members) into the template context."""
    return {
        "id": str(detail.id),
        "name": detail.name,
        "description": detail.description,
        "member_count": detail.member_count,
        "state": detail.state,
        "state_badge": state_badge_class(detail.state),
        "created_by_sub": detail.created_by_sub,
        "created_at": coerce_utc_aware(detail.created_at),
        "updated_at": coerce_utc_aware(detail.updated_at),
        "members": [_project_member(m) for m in detail.members],
    }
