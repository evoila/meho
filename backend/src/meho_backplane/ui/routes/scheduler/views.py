# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Projection + shared helpers for the scheduler UI surface (Task #1826).

The list / detail handlers project
:class:`~meho_backplane.scheduler.schemas.ScheduledTriggerRead` rows into
the flat dict shape the templates read. Keeping the projection here (not
in the route modules) means the row-to-view mapping is unit-testable
without a FastAPI request fixture and the same shape feeds both the
list rows and the detail page.

UTC coercion
------------

``ScheduledTriggerRead`` carries several ``DateTime(timezone=True)``
columns (``next_fire_at`` / ``last_fired_at`` / ``fire_at`` /
``created_at`` / ``updated_at``). On PostgreSQL the ORM round-trips
them tz-aware; on the SQLite ``aiosqlite`` test driver they come back
naive (the chassis fixture leaves ``detect_types`` unset). The list
template's ``_relative_time`` macro does ``now_utc - ts`` arithmetic,
which raises ``TypeError: can't subtract offset-naive and offset-aware
datetimes`` on a naive value. :func:`coerce_utc_aware` normalises every
timestamp before it reaches the template -- the same shape the
connectors list view applies (#873).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from meho_backplane.scheduler.schemas import ScheduledTriggerRead

__all__ = [
    "KindFilterValue",
    "StatusFilterValue",
    "coerce_utc_aware",
    "project_trigger_to_view",
    "status_badge_class",
]


class KindFilterValue(StrEnum):
    """Closed enum of the trigger ``kind`` values exposed in the filter URL.

    Mirrors the wire-level :data:`~meho_backplane.scheduler.schemas.KindFilter`
    literal (``cron`` / ``one_off`` / ``event``). The ``str`` mixin keeps
    the template's ``{{ kind_filter }}`` rendering + ``selected`` matching
    stable, and an out-of-enum value fails Pydantic validation at the HTTP
    boundary with a 422 rather than silently filtering nothing.
    """

    CRON = "cron"
    ONE_OFF = "one_off"
    EVENT = "event"


class StatusFilterValue(StrEnum):
    """Closed enum of the trigger ``status`` values exposed in the filter URL.

    Mirrors :data:`~meho_backplane.scheduler.schemas.StatusFilter`.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FIRED = "fired"


#: DaisyUI badge modifier per status -- ``active`` is the live healthy
#: state (success), ``paused`` is operator-suspended (warning), ``fired``
#: is the terminal one-off completion (info), ``cancelled`` is the
#: terminal operator stop (ghost/muted). Unknown values fall through to
#: the neutral ghost badge so a future status never renders unstyled.
_STATUS_BADGE: Final[dict[str, str]] = {
    StatusFilterValue.ACTIVE.value: "badge-success",
    StatusFilterValue.PAUSED.value: "badge-warning",
    StatusFilterValue.FIRED.value: "badge-info",
    StatusFilterValue.CANCELLED.value: "badge-ghost",
}


def coerce_utc_aware(ts: datetime | None) -> datetime | None:
    """Normalise *ts* to a tz-aware UTC :class:`datetime` (``None`` passes through).

    The SQLite test driver hands back naive datetimes for the
    ``DateTime(timezone=True)`` columns; the list template's relative-time
    macro does timedelta arithmetic against a tz-aware "now", so a naive
    value raises ``TypeError`` mid-render. Attaching :data:`datetime.UTC`
    to a naive value keeps the substrate dialect-portable without teaching
    the column shape about the dialect mismatch (same fix the connectors
    list view applies).
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def status_badge_class(status_value: str) -> str:
    """Return the DaisyUI badge modifier class for a trigger status."""
    return _STATUS_BADGE.get(status_value, "badge-ghost")


def _schedule_summary(trigger: ScheduledTriggerRead) -> str:
    """Render the one-line human schedule summary for the list column.

    ``cron`` -> the raw cron expression (operators read cron fluently;
    a humanised paraphrase would be lossy). ``one_off`` -> the ``fire_at``
    instant. ``event`` -> a static "event-driven" label (the filter JSON
    is too large for a table cell; the detail page shows it in full).
    """
    if trigger.cron_expr is not None:
        return trigger.cron_expr
    if trigger.fire_at is not None:
        coerced = coerce_utc_aware(trigger.fire_at)
        assert coerced is not None  # fire_at is not None on this branch
        return coerced.isoformat()
    return "event-driven"


def project_trigger_to_view(
    trigger: ScheduledTriggerRead,
    *,
    agent_names: dict[uuid.UUID, str],
) -> dict[str, object]:
    """Project one trigger row into the flat dict the templates read.

    *agent_names* maps ``agent_definition_id`` -> the resolved
    definition name (looked up once per page render by the handler). A
    missing id (definition deleted after the trigger was created) falls
    back to the short id prefix so the row still renders meaningfully.
    """
    agent_name = agent_names.get(trigger.agent_definition_id)
    return {
        "id": str(trigger.id),
        "kind": trigger.kind.value,
        "status": trigger.status.value,
        "status_badge": status_badge_class(trigger.status.value),
        "schedule_summary": _schedule_summary(trigger),
        "timezone": trigger.timezone,
        "next_fire_at": coerce_utc_aware(trigger.next_fire_at),
        "last_fired_at": coerce_utc_aware(trigger.last_fired_at),
        # Skip-state projection (#2327): a non-zero count drives a warning
        # badge next to the status so a silently-skipping 'active' trigger
        # no longer reads as healthy at a glance in the list.
        "skip_count": trigger.skip_count,
        "last_skip_reason": trigger.last_skip_reason,
        "agent_name": agent_name or f"{str(trigger.agent_definition_id)[:8]}…",
        "agent_definition_id": str(trigger.agent_definition_id),
        "work_ref": trigger.work_ref,
    }


def build_agent_name_map(
    agent_defs: Sequence[object],
) -> dict[uuid.UUID, str]:
    """Build the ``agent_definition_id`` -> name map from a definition list.

    Accepts any sequence whose items expose ``id`` (UUID) + ``name`` (str)
    -- the :class:`~meho_backplane.agents.schemas.AgentDefinitionRead`
    shape. Centralised so the list + detail handlers share one mapping
    builder.
    """
    mapping: dict[uuid.UUID, str] = {}
    for definition in agent_defs:
        def_id = getattr(definition, "id", None)
        def_name = getattr(definition, "name", None)
        if isinstance(def_id, uuid.UUID) and isinstance(def_name, str):
            mapping[def_id] = def_name
    return mapping
