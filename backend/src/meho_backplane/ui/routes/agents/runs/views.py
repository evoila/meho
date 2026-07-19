# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Projection + shared helpers for the agent-runs UI surface (Task #1830).

The list / detail handlers project the invoker's read dataclasses
(:class:`~meho_backplane.agent.invocation.AgentRunSummary` for a list row,
:class:`~meho_backplane.agent.invocation.AgentRunStatusView` for the detail
poll) into the flat dict shape the templates read. Keeping the projection
here (not in the route modules) means the row-to-view mapping is
unit-testable without a FastAPI request fixture, and the list + detail
renders share one status-badge + UTC-coercion vocabulary.

UTC coercion
------------

``AgentRun`` carries ``DateTime(timezone=True)`` columns (``created_at`` /
``started_at`` / ``ended_at``). On PostgreSQL the ORM round-trips them
tz-aware; on the SQLite ``aiosqlite`` test driver they come back naive
(the chassis fixture leaves ``detect_types`` unset). The list template's
relative-time macro does ``now_utc - ts`` arithmetic, which raises
``TypeError: can't subtract offset-naive and offset-aware datetimes`` on a
naive value. :func:`coerce_utc_aware` normalises every timestamp before it
reaches the template -- the same shape the connectors / scheduler list
views apply.

The status filter
-----------------

The list ``?status=`` filter uses the runtime
:class:`~meho_backplane.db.models.AgentRunStatus` enum directly as the
FastAPI ``Query`` type (it is a ``StrEnum``, so an out-of-enum value 422s at
the HTTP boundary rather than silently matching nothing, and the
``<select>`` options are built from the same source enum). Reusing the
source enum -- rather than mirroring a parallel one -- removes any drift
risk between the filter vocabulary and the runtime states.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from meho_backplane.agent.invocation import AgentRunStatusView, AgentRunSummary
from meho_backplane.db.models import AgentRunStatus

__all__ = [
    "coerce_utc_aware",
    "is_terminal_status",
    "project_detail_to_view",
    "project_run_to_view",
    "status_badge_class",
]


#: Lifecycle states from which a run never transitions again -- the detail
#: view stops polling once a run reaches one. Mirrors the terminal set the
#: runtime's state machine treats as final (``succeeded`` / ``failed`` /
#: ``cancelled``).
_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {
        AgentRunStatus.SUCCEEDED.value,
        AgentRunStatus.FAILED.value,
        AgentRunStatus.CANCELLED.value,
    }
)

#: DaisyUI badge modifier per status. ``succeeded`` is the healthy terminal
#: (success); ``failed`` is the error terminal (error); ``running`` is live
#: (info); ``awaiting_approval`` is the policy-gated pause (warning);
#: ``cancelled`` is the operator stop (ghost/muted); ``pending`` is the
#: not-yet-started state (neutral). An unknown value falls through to the
#: ghost badge so a future status never renders unstyled.
_STATUS_BADGE: Final[dict[str, str]] = {
    AgentRunStatus.PENDING.value: "badge-neutral",
    AgentRunStatus.RUNNING.value: "badge-info",
    AgentRunStatus.AWAITING_APPROVAL.value: "badge-warning",
    AgentRunStatus.SUCCEEDED.value: "badge-success",
    AgentRunStatus.FAILED.value: "badge-error",
    AgentRunStatus.CANCELLED.value: "badge-ghost",
}


def coerce_utc_aware(ts: datetime | None) -> datetime | None:
    """Normalise *ts* to a tz-aware UTC :class:`datetime` (``None`` passes through).

    The SQLite test driver hands back naive datetimes for the
    ``DateTime(timezone=True)`` columns; the list template's relative-time
    macro does timedelta arithmetic against a tz-aware "now", so a naive
    value raises ``TypeError`` mid-render. Attaching :data:`datetime.UTC`
    to a naive value keeps the substrate dialect-portable without teaching
    the column shape about the dialect mismatch (same fix the connectors /
    scheduler list views apply).
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def status_badge_class(status_value: str) -> str:
    """Return the DaisyUI badge modifier class for a run status."""
    return _STATUS_BADGE.get(status_value, "badge-ghost")


def is_terminal_status(status_value: str) -> bool:
    """Return ``True`` when a run in *status_value* will never change again.

    The detail view polls only while a run is non-terminal; a terminal run
    renders statically. Centralised here so the route handler and the
    template-context builder agree on what "terminal" means.
    """
    return status_value in _TERMINAL_STATUSES


def project_run_to_view(summary: AgentRunSummary) -> dict[str, object]:
    """Project one :class:`AgentRunSummary` into the list-row dict shape.

    The summary deliberately omits the run's ``output`` blob (the list is a
    scannable index); the detail poll carries it. The ``trigger`` field is
    surfaced as the run's provenance label (``direct`` / ``scheduled`` /
    ``event`` / ``agent-invoked``). The summary now carries the per-run
    agent back-link -- ``agent_name`` (resolved read-time from the row's
    ``agent_definition_id`` soft-FK, #2472) and ``agent_definition_id``
    itself -- so the list renders which agent produced each run;
    ``agent_name`` is ``None`` for an ad-hoc run or a definition deleted
    after the run.
    """
    return {
        "run_id": str(summary.run_id),
        "status": summary.status.value,
        "status_badge": status_badge_class(summary.status.value),
        "trigger": summary.trigger,
        "agent_name": summary.agent_name,
        "agent_definition_id": (
            str(summary.agent_definition_id) if summary.agent_definition_id is not None else None
        ),
        "model_tier": summary.model_tier,
        "provider": summary.provider,
        "model": summary.model,
        "turns": summary.turns,
        "work_ref": summary.work_ref,
        "created_at": coerce_utc_aware(summary.created_at),
        "started_at": coerce_utc_aware(summary.started_at),
        "ended_at": coerce_utc_aware(summary.ended_at),
        "is_awaiting_approval": (summary.status == AgentRunStatus.AWAITING_APPROVAL),
    }


def project_detail_to_view(view: AgentRunStatusView) -> dict[str, object]:
    """Project an :class:`AgentRunStatusView` into the detail-template shape.

    ``output`` / ``error`` are populated only once the run is terminal; the
    template renders the JSON ``output`` block and the ``error`` reason when
    present. ``is_terminal`` drives whether the status panel keeps polling.
    """
    return {
        "run_id": str(view.run_id),
        "status": view.status.value,
        "status_badge": status_badge_class(view.status.value),
        "turns": view.turns,
        "provider": view.provider,
        "model": view.model,
        "output": view.output,
        "error": view.error,
        "agent_name": view.agent_name,
        "agent_definition_id": (
            str(view.agent_definition_id) if view.agent_definition_id is not None else None
        ),
        "is_terminal": is_terminal_status(view.status.value),
        "is_awaiting_approval": view.status == AgentRunStatus.AWAITING_APPROVAL,
    }
