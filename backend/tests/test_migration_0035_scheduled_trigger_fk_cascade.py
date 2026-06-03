# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0035_scheduled_trigger_fk_cascade``.

Issue #1480 (G0.19 v0.10.0 dogfood hardening). Migration 0035 rebuilds
the ``scheduled_trigger.agent_definition_id`` FK with ``ON DELETE
CASCADE`` so deleting an ``agent_definition`` cascade-deletes its
dependent trigger rows (including a cancelled one retained for audit)
instead of raising a foreign-key violation.

Test matrix
-----------

* **Upgrade installs the cascade.** After ``upgrade 0035`` (with SQLite
  ``PRAGMA foreign_keys=ON``), deleting the parent ``agent_definition``
  via a raw bulk ``DELETE`` succeeds and removes the dependent
  ``scheduled_trigger`` rows.
* **Reflection shows ``ON DELETE CASCADE``.** ``PRAGMA
  foreign_key_list(scheduled_trigger)`` reports ``CASCADE`` on the
  ``agent_definition`` FK at 0035.
* **Downgrade restores the blocking FK.** After ``downgrade "0034"`` the
  cascade is gone (``on_delete`` back to ``NO ACTION``) and a bulk
  parent delete raises ``IntegrityError`` again -- the 0020 behaviour.

The tests follow the synchronous pattern of
:mod:`tests.test_migration_0025_scheduled_trigger`:
``alembic.command.upgrade`` runs ``asyncio.run`` internally via env.py's
async cookbook, so each test function is sync. A fresh file-backed
SQLite DB per test isolates the migration replay.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Same shape as
    :func:`tests.test_migration_0025_scheduled_trigger.alembic_cfg`:
    sync because Alembic's env.py runs ``asyncio.run`` internally; an
    isolated SQLite DB per test; settings + engine caches reset on entry
    and exit.
    """
    db_path = tmp_path / "migration_0035.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, sync_url
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _seed_definition_with_trigger(sync_url: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert tenant + agent_definition + one cancelled scheduled_trigger.

    Returns ``(tenant_id, definition_id, trigger_id)``. The cancelled
    status is deliberate: it is the exact row #1480 reports as blocking a
    delete (``cancel()`` retains it for audit), so the cascade test
    covers the worst case.
    """
    tenant_id = uuid.uuid4()
    definition_id = uuid.uuid4()
    trigger_id = uuid.uuid4()
    slug = f"test-0035-{tenant_id.hex[:8]}"
    name = f"test-0035-agent-{definition_id.hex[:8]}"

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, :slug, :name, :now)"
                ),
                {"id": str(tenant_id), "slug": slug, "name": slug, "now": "2026-06-03T00:00:00"},
            )
            conn.execute(
                text(
                    "INSERT INTO agent_definition "
                    "(id, tenant_id, name, identity_ref, model_tier, "
                    " system_prompt, toolset, turn_budget, enabled, "
                    " created_by_sub, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :name, :identity_ref, "
                    " :model_tier, :system_prompt, :toolset, "
                    " :turn_budget, :enabled, :sub, :now, :now)"
                ),
                {
                    "id": str(definition_id),
                    "tenant_id": str(tenant_id),
                    "name": name,
                    "identity_ref": f"agent:{name}",
                    "model_tier": "standard",
                    "system_prompt": "test",
                    "toolset": "{}",
                    "turn_budget": 2,
                    "enabled": 1,
                    "sub": "seed-admin",
                    "now": "2026-06-03T00:00:00",
                },
            )
            conn.execute(
                text(
                    "INSERT INTO scheduled_trigger "
                    "(id, tenant_id, agent_definition_id, kind, cron_expr, "
                    " timezone, status, in_flight_policy, identity_sub, "
                    " created_by_sub, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :definition_id, 'cron', "
                    " '*/5 * * * *', 'UTC', 'cancelled', 'fail_into_audit', "
                    " '__scheduler__', 'seed-admin', :now, :now)"
                ),
                {
                    "id": str(trigger_id),
                    "tenant_id": str(tenant_id),
                    "definition_id": str(definition_id),
                    "now": "2026-06-03T00:00:00",
                },
            )
    finally:
        sync_eng.dispose()
    return tenant_id, definition_id, trigger_id


def _fk_on_delete(sync_url: str) -> str | None:
    """Return the ``on_delete`` action for the agent_definition FK, or None."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA foreign_key_list(scheduled_trigger)")).all()
    finally:
        sync_eng.dispose()
    for row in rows:
        # PRAGMA columns: id, seq, table, from, to, on_update, on_delete, match
        if str(row[2]) == "agent_definition":
            return str(row[6])
    return None


def _delete_definition(sync_url: str, definition_id: uuid.UUID) -> None:
    """Bulk-delete the parent definition with SQLite FK enforcement on."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys = ON"))
            conn.execute(
                text("DELETE FROM agent_definition WHERE id = :id"),
                {"id": str(definition_id)},
            )
    finally:
        sync_eng.dispose()


def _trigger_count(sync_url: str, definition_id: uuid.UUID) -> int:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text("SELECT COUNT(*) FROM scheduled_trigger WHERE agent_definition_id = :id"),
                {"id": str(definition_id)},
            ).scalar_one()
    finally:
        sync_eng.dispose()
    return int(row)


def test_upgrade_sets_on_delete_cascade(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0035`` reflects ``CASCADE`` on the agent_definition FK."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0035")
    assert _fk_on_delete(sync_url) == "CASCADE"


def test_upgrade_cascade_deletes_dependent_triggers(
    alembic_cfg: tuple[Config, str],
) -> None:
    """At 0035, a bulk parent delete cascade-deletes its triggers (no error).

    This is the acceptance-criterion proof: a **bulk** ``DELETE`` (the
    shape :meth:`AgentDefinitionService.delete` issues) triggers the
    DB-level cascade. Pre-fix the same delete raised ``IntegrityError``.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0035")

    _tenant_id, definition_id, _trigger_id = _seed_definition_with_trigger(sync_url)
    assert _trigger_count(sync_url, definition_id) == 1

    _delete_definition(sync_url, definition_id)

    assert _trigger_count(sync_url, definition_id) == 0


def test_downgrade_restores_blocking_fk(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0034"`` removes the cascade; a parent delete blocks again.

    Round-trips the migration: after the downgrade the FK is back to the
    no-``ondelete`` (``NO ACTION``) shape 0020 shipped, so deleting a
    definition that still has a trigger raises ``IntegrityError`` -- the
    exact regression #1480 fixed.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0035")
    command.downgrade(cfg, "0034")

    assert _fk_on_delete(sync_url) in (None, "NO ACTION")

    _tenant_id, definition_id, _trigger_id = _seed_definition_with_trigger(sync_url)
    with pytest.raises(IntegrityError):
        _delete_definition(sync_url, definition_id)

    # The blocking trigger is still present -- the delete was refused.
    assert _trigger_count(sync_url, definition_id) == 1
