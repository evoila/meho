# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""ORM + drift-guard tests for the Dashboard entity (#2506).

Initiative #2416 (parent goal #221), Task #2506. Covers:

* **Row round-trip** -- a ``check_dashboards`` row persists and reads back
  with the ORM defaults firing on SQLite; ``last_rollup_state`` defaults NULL
  (shipped unwritten by this Task).
* **CHECK vocabulary** -- ``ck_check_dashboards_last_rollup_state`` admits NULL
  plus the five states and rejects an unknown value.
* **Drift guards** -- the model constant and the migration's frozen literal
  both equal #2504's :data:`CheckState`, and the migration's CHECK body equals
  the ORM's. Mirrors :mod:`tests.test_db_sensor`.
* **Notification columns (#2719)** -- ``notify_email`` / ``notify_min_state``
  default to "off at ``critical``", the floor CHECK admits only the two
  actionable states, and the model / wire-Literal / ``0068`` migration copies
  of that vocabulary agree.
* **Investigator prompt (#2721)** -- ``investigator_prompt`` defaults NULL
  (the pre-#2721 briefing), round-trips operator prose verbatim, and carries
  no DB CHECK: the bound is the wire schema's, by decision.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import get_args

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from meho_backplane.checks.assertions import CheckState
from meho_backplane.checks.dashboard_schemas import NotifyMinState
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    _CHECK_DASHBOARD_NOTIFY_MIN_STATES,
    _CHECK_DASHBOARD_ROLLUP_STATES,
    CheckDashboard,
    Tenant,
)
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires for the engine."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == _TENANT))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=_TENANT, slug="tenant-a", name="Tenant A"))
            await session.commit()


@pytest.mark.asyncio
async def test_dashboard_round_trip_defaults_last_rollup_null() -> None:
    """A dashboard persists; ORM defaults fire; ``last_rollup_state`` is NULL."""
    await _seed_tenant()
    dash_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CheckDashboard(
                id=dash_id,
                tenant_id=_TENANT,
                name="prod-health",
                description="production checks",
                created_by_sub="op-admin",
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        row = await session.get(CheckDashboard, dash_id)
        assert row is not None
        assert row.name == "prod-health"
        assert row.description == "production checks"
        # The memo column ships unwritten by this Task.
        assert row.last_rollup_state is None
        assert row.created_at is not None
        assert row.updated_at is not None


@pytest.mark.asyncio
async def test_last_rollup_state_accepts_valid_state() -> None:
    """A valid five-state value persists into ``last_rollup_state``."""
    await _seed_tenant()
    dash_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CheckDashboard(
                id=dash_id,
                tenant_id=_TENANT,
                name="memo-set",
                created_by_sub="op-admin",
                last_rollup_state="critical",
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        row = await session.get(CheckDashboard, dash_id)
        assert row is not None
        assert row.last_rollup_state == "critical"


@pytest.mark.asyncio
async def test_last_rollup_state_check_rejects_unknown() -> None:
    """An out-of-vocabulary ``last_rollup_state`` is rejected by the CHECK."""
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    with pytest.raises(IntegrityError):
        async with sessionmaker() as session:
            session.add(
                CheckDashboard(
                    id=uuid.uuid4(),
                    tenant_id=_TENANT,
                    name="bad-memo",
                    created_by_sub="op-admin",
                    last_rollup_state="bogus",
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_unique_name_per_tenant_enforced() -> None:
    """Two dashboards with the same name in one tenant collide on the unique idx."""
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CheckDashboard(
                id=uuid.uuid4(), tenant_id=_TENANT, name="dupe", created_by_sub="op-admin"
            )
        )
        await session.commit()
    with pytest.raises(IntegrityError):
        async with sessionmaker() as session:
            session.add(
                CheckDashboard(
                    id=uuid.uuid4(), tenant_id=_TENANT, name="dupe", created_by_sub="op-admin"
                )
            )
            await session.commit()


# ---------------------------------------------------------------------------
# Drift guards
# ---------------------------------------------------------------------------


def _load_migration_file(filename: str, module_name: str) -> object:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration() -> object:
    return _load_migration_file("0065_create_check_dashboards.py", "_migration_0065")


def _load_notify_migration() -> object:
    return _load_migration_file("0068_add_check_dashboard_notify.py", "_migration_0068")


def _orm_check_body(name: str = "ck_check_dashboards_last_rollup_state") -> str:
    from sqlalchemy import CheckConstraint

    for c in CheckDashboard.__table__.constraints:
        if isinstance(c, CheckConstraint) and c.name == name:
            return str(c.sqltext)
    raise AssertionError(f"{name} not found on the ORM table")


def test_model_rollup_states_equal_checkstate() -> None:
    """The model's ``last_rollup_state`` vocabulary equals #2504's ``CheckState``."""
    assert set(_CHECK_DASHBOARD_ROLLUP_STATES) == set(get_args(CheckState))


def test_migration_rollup_literal_matches_checkstate() -> None:
    """The migration's frozen ``_ROLLUP_STATES`` is a snapshot of ``CheckState``."""
    migration = _load_migration()
    assert set(migration._ROLLUP_STATES) == set(get_args(CheckState))  # type: ignore[attr-defined]


def test_migration_check_body_equals_orm() -> None:
    """The migration's rendered CHECK body equals the live ORM constraint's."""
    migration = _load_migration()
    rendered = migration._check_in(  # type: ignore[attr-defined]
        "last_rollup_state",
        migration._ROLLUP_STATES,  # type: ignore[attr-defined]
    )
    assert _orm_check_body() == rendered


# ---------------------------------------------------------------------------
# Notification columns (#2719)
# ---------------------------------------------------------------------------


def test_notify_min_states_are_the_two_actionable_states() -> None:
    """The floor vocabulary is deliberately narrower than ``CheckState``.

    ``ok`` as a floor would mail on every edge, and ``skip`` / ``unknown``
    are not severities a threshold is meaningful at -- so this constant is
    declared independently rather than snapshotted from ``CheckState``.
    """
    assert set(_CHECK_DASHBOARD_NOTIFY_MIN_STATES) == {"degraded", "critical"}
    assert set(_CHECK_DASHBOARD_NOTIFY_MIN_STATES) < set(get_args(CheckState))


def test_wire_notify_min_state_literal_matches_model() -> None:
    """The wire ``NotifyMinState`` Literal equals the model's vocabulary."""
    assert set(get_args(NotifyMinState)) == set(_CHECK_DASHBOARD_NOTIFY_MIN_STATES)


def test_notify_migration_literal_matches_model() -> None:
    """The 0068 migration's frozen tuple is a snapshot of the ORM's."""
    migration = _load_notify_migration()
    assert set(migration._NOTIFY_MIN_STATES) == set(  # type: ignore[attr-defined]
        _CHECK_DASHBOARD_NOTIFY_MIN_STATES
    )


def test_notify_migration_check_body_equals_orm() -> None:
    """The 0068 migration's rendered CHECK body equals the ORM constraint's."""
    migration = _load_notify_migration()
    rendered = migration._check_in(  # type: ignore[attr-defined]
        "notify_min_state",
        migration._NOTIFY_MIN_STATES,  # type: ignore[attr-defined]
    )
    assert _orm_check_body("ck_check_dashboards_notify_min_state") == rendered


@pytest.mark.asyncio
async def test_notify_columns_default_to_silent_critical() -> None:
    """A row inserted without notification config defaults off at ``critical``."""
    await _seed_tenant()
    dashboard_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=_TENANT,
                name="defaults-notify",
                created_by_sub="op-admin",
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        row = await session.get(CheckDashboard, dashboard_id)
        assert row is not None
        assert row.notify_email is None
        assert row.notify_min_state == "critical"


@pytest.mark.asyncio
async def test_notify_min_state_check_rejects_out_of_vocabulary() -> None:
    """``ck_check_dashboards_notify_min_state`` refuses a five-state value."""
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CheckDashboard(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                name="bad-floor",
                notify_min_state="ok",
                created_by_sub="op-admin",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_investigator_prompt_defaults_to_null() -> None:
    """A row inserted without an operator prompt keeps the pre-#2721 briefing."""
    await _seed_tenant()
    dashboard_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=_TENANT,
                name="defaults-prompt",
                created_by_sub="op-admin",
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        row = await session.get(CheckDashboard, dashboard_id)
        assert row is not None
        assert row.investigator_prompt is None


@pytest.mark.asyncio
async def test_investigator_prompt_round_trips() -> None:
    """The column stores operator prose verbatim -- newlines and all.

    The briefing quotes the value inside a fence, so the ORM must not
    normalise it; the only shaping happens at render time.
    """
    await _seed_tenant()
    dashboard_id = uuid.uuid4()
    prompt = "Line one.\n\nLine two, with 'quotes' and a -- dash."
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=_TENANT,
                name="prompted",
                investigator_prompt=prompt,
                created_by_sub="op-admin",
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        row = await session.get(CheckDashboard, dashboard_id)
        assert row is not None
        assert row.investigator_prompt == prompt


def test_investigator_prompt_is_unconstrained_at_the_database() -> None:
    """No CHECK bounds the prompt -- the wire schema is the single guard.

    A drift guard for the decision recorded in ``0069``'s docstring: the
    create path is the column's only writer, so the bound lives at the
    boundary where a violation is a structured 422 naming the limit, not an
    opaque 500 from a constraint the caller cannot see.
    """
    from sqlalchemy import CheckConstraint

    bodies = [
        str(c.sqltext)
        for c in CheckDashboard.__table__.constraints
        if isinstance(c, CheckConstraint)
    ]
    assert not any("investigator_prompt" in body for body in bodies)
