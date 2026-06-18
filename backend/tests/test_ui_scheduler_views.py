# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the scheduler UI view-projection helpers (Task #1826).

These cover the pure functions in
:mod:`meho_backplane.ui.routes.scheduler.views` without a FastAPI request
fixture: the UTC-coercion guard (the SQLite naive-datetime fix the issue's
acceptance criterion calls out), the status badge mapping, the schedule
summary, and the agent-name map projection.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from meho_backplane.db.models import (
    ScheduledTriggerInFlightPolicy,
    ScheduledTriggerKind,
    ScheduledTriggerStatus,
)
from meho_backplane.scheduler.schemas import ScheduledTriggerRead
from meho_backplane.ui.routes.scheduler.views import (
    build_agent_name_map,
    coerce_utc_aware,
    project_trigger_to_view,
    status_badge_class,
)

_AGENT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _read(
    *,
    kind: ScheduledTriggerKind = ScheduledTriggerKind.CRON,
    cron_expr: str | None = "*/15 * * * *",
    fire_at: datetime | None = None,
    status: ScheduledTriggerStatus = ScheduledTriggerStatus.ACTIVE,
    next_fire_at: datetime | None = None,
    work_ref: str | None = None,
) -> ScheduledTriggerRead:
    now = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    return ScheduledTriggerRead(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_definition_id=_AGENT_ID,
        kind=kind,
        cron_expr=cron_expr,
        timezone="UTC",
        fire_at=fire_at,
        event_filter=None,
        status=status,
        in_flight_policy=ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT,
        next_fire_at=next_fire_at,
        last_fired_at=None,
        inputs=None,
        identity_sub="__scheduler__",
        created_by_sub="op-admin",
        work_ref=work_ref,
        created_at=now,
        updated_at=now,
    )


def test_coerce_utc_aware_attaches_utc_to_naive() -> None:
    """A naive datetime (the SQLite round-trip shape) gets UTC attached."""
    naive = datetime(2026, 6, 18, 12, 0)
    coerced = coerce_utc_aware(naive)
    assert coerced is not None
    assert coerced.tzinfo is UTC


def test_coerce_utc_aware_passes_aware_through() -> None:
    """An already-aware datetime is returned unchanged."""
    aware = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    assert coerce_utc_aware(aware) == aware


def test_coerce_utc_aware_none_passes_through() -> None:
    """``None`` (no next_fire_at / never fired) coerces to ``None``."""
    assert coerce_utc_aware(None) is None


def test_status_badge_mapping() -> None:
    """Each status maps to a distinct DaisyUI badge; unknown falls back to ghost."""
    assert status_badge_class("active") == "badge-success"
    assert status_badge_class("paused") == "badge-warning"
    assert status_badge_class("fired") == "badge-info"
    assert status_badge_class("cancelled") == "badge-ghost"
    assert status_badge_class("future-state") == "badge-ghost"


def test_project_cron_trigger_summary() -> None:
    """A cron trigger projects its raw expression as the schedule summary."""
    view = project_trigger_to_view(
        _read(kind=ScheduledTriggerKind.CRON, cron_expr="0 9 * * *"),
        agent_names={_AGENT_ID: "nightly"},
    )
    assert view["schedule_summary"] == "0 9 * * *"
    assert view["agent_name"] == "nightly"
    assert view["status_badge"] == "badge-success"


def test_project_one_off_trigger_summary() -> None:
    """A one-off trigger projects its fire_at instant as the schedule summary."""
    fire = datetime(2026, 7, 1, 9, 30, tzinfo=UTC)
    view = project_trigger_to_view(
        _read(
            kind=ScheduledTriggerKind.ONE_OFF,
            cron_expr=None,
            fire_at=fire,
        ),
        agent_names={},
    )
    assert view["schedule_summary"] == fire.isoformat()
    # No name in the map -> short-id fallback.
    assert str(view["agent_name"]).endswith("…")


def test_project_coerces_naive_next_fire() -> None:
    """A naive next_fire_at coerces to UTC-aware so the relative-time macro is safe."""
    naive_next = datetime(2026, 6, 18, 13, 0)
    view = project_trigger_to_view(
        _read(next_fire_at=naive_next),
        agent_names={_AGENT_ID: "nightly"},
    )
    next_fire = view["next_fire_at"]
    assert isinstance(next_fire, datetime)
    assert next_fire.tzinfo is UTC


def test_build_agent_name_map_filters_bad_shapes() -> None:
    """The map builder keeps well-shaped (UUID id, str name) rows only."""
    good = SimpleNamespace(id=_AGENT_ID, name="nightly")
    bad_id = SimpleNamespace(id="not-a-uuid", name="x")
    bad_name = SimpleNamespace(id=uuid.uuid4(), name=None)
    mapping = build_agent_name_map([good, bad_id, bad_name])
    assert mapping == {_AGENT_ID: "nightly"}
