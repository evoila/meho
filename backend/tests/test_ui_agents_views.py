# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Projection tests for the agents console views (#349).

Mirrors :mod:`backend.tests.test_ui_scheduler_views`: the list-card and
detail projections are pure functions over an
:class:`~meho_backplane.agents.schemas.AgentDefinitionRead`, so they are
unit-testable without the BFF session harness that
:mod:`backend.tests.test_ui_agents` carries.

The timestamps matter here because the templates render them with a
``UTC`` suffix. ``created_at`` / ``updated_at`` are
``DateTime(timezone=True)`` columns that the SQLite driver returns
**naive**; without coercion the suffix would label an unzoned value and
the ``<time datetime="...">`` attribute would carry an offset-less
instant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from meho_backplane.agents.schemas import AgentDefinitionRead
from meho_backplane.ui.routes.agents.views import _card_context, _detail_context

_NAIVE = datetime(2026, 6, 18, 9, 30, 12, 483920)
_AWARE = datetime(2026, 6, 18, 9, 30, 12, 483920, tzinfo=UTC)


def _read(*, created_at: datetime, updated_at: datetime) -> AgentDefinitionRead:
    return AgentDefinitionRead(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="incident-triage",
        identity_ref="agent-incident-triage",
        model_tier="standard",
        system_prompt="Investigate the alert.",
        toolset={"k8s": {}},
        turn_budget=12,
        output_schema=None,
        enabled=True,
        created_by_sub="op-alice",
        created_at=created_at,
        updated_at=updated_at,
    )


def test_detail_context_coerces_naive_timestamps_to_utc() -> None:
    """Naive SQLite round-trip values reach the template tz-aware."""
    context = _detail_context(_read(created_at=_NAIVE, updated_at=_NAIVE), can_write=False)
    created = context["created_at"]
    updated = context["updated_at"]
    assert isinstance(created, datetime) and isinstance(updated, datetime)
    assert created.tzinfo is UTC
    assert updated.tzinfo is UTC
    # Coercion attaches the zone; it must not shift the wall clock.
    assert created.replace(tzinfo=None) == _NAIVE


def test_detail_context_passes_aware_timestamps_through() -> None:
    """An already-aware value (the PostgreSQL path) is unchanged."""
    context = _detail_context(_read(created_at=_AWARE, updated_at=_AWARE), can_write=False)
    assert context["created_at"] == _AWARE
    assert context["updated_at"] == _AWARE


def test_card_context_coerces_updated_at() -> None:
    """The list card renders ``updated_at`` with the same UTC suffix."""
    context = _card_context(_read(created_at=_NAIVE, updated_at=_NAIVE))
    updated = context["updated_at"]
    assert isinstance(updated, datetime)
    assert updated.tzinfo is UTC
    assert updated.strftime("%Y-%m-%d %H:%M UTC") == "2026-06-18 09:30 UTC"
