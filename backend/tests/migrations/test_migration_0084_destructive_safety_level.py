# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0084_widen_safety_level_destructive``.

#3183 / #3196. Widens the ``ck_endpoint_descriptor_safety_level`` CHECK
constraint from three values to four by adding the ``destructive`` tier
(``safe < caution < dangerous < destructive``).

Idempotency pinning: every forward / round-trip step targets this
migration's own revision (``0084``) and its ``down_revision`` (``0079``),
never ``head`` — so a future head migration cannot make ``upgrade("head")``
re-run this constraint recreate on a schema that already has it. SQLite is
the test driver and the migration uses only generic ``CHECK ... IN (...)``
DDL under ``batch_alter_table``, so PostgreSQL parity holds.

The ``down_revision`` is pinned to ``0079`` (the head at branch time);
the orchestrator re-points it to the then-current head at merge if the
0080-0083 queue lands first (see the migration docstring). This test
targets the literal revision ids so it stays correct under that re-point
only if the ids move in lockstep — which the orchestrator's re-point does.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
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

_REVISION = "0084"
_DOWN_REVISION = "0079"
_TABLE = "endpoint_descriptor"
_CONSTRAINT = "ck_endpoint_descriptor_safety_level"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0084.db"
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


def _insert_descriptor(sync_url: str, safety_level: str, op_id: str) -> None:
    """Insert a minimal ``endpoint_descriptor`` row at *safety_level*.

    Raises :class:`~sqlalchemy.exc.IntegrityError` when the CHECK
    constraint rejects the value — the failure surface the tests assert on.
    """
    now = datetime.now(UTC).isoformat()
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO endpoint_descriptor "
                    "(id, product, version, impl_id, op_id, source_kind, safety_level, "
                    " requires_approval, is_enabled, needs_reingest, created_at, updated_at) "
                    "VALUES (:id, :product, :version, :impl_id, :op_id, :source_kind, "
                    " :safety_level, 0, 1, 0, :created_at, :updated_at)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "product": "vault",
                    "version": "1.x",
                    "impl_id": "vault",
                    "op_id": op_id,
                    "source_kind": "typed",
                    "safety_level": safety_level,
                    "created_at": now,
                    "updated_at": now,
                },
            )
    finally:
        sync_eng.dispose()


def test_upgrade_check_accepts_destructive(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0084`` widens the CHECK so a ``destructive`` row inserts."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    # The three pre-existing values still insert...
    for level in ("safe", "caution", "dangerous"):
        _insert_descriptor(sync_url, level, f"op.{level}")
    # ...and the new tier now inserts too.
    _insert_descriptor(sync_url, "destructive", "op.destroy")


def test_upgrade_check_still_rejects_unknown_value(alembic_cfg: tuple[Config, str]) -> None:
    """A value outside the widened four-set is still rejected by the CHECK."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    with pytest.raises(IntegrityError):
        _insert_descriptor(sync_url, "catastrophic", "op.bogus")


def test_downgrade_refuses_when_destructive_rows_exist(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade 0079`` raises (not a silent DDL failure) when a
    ``destructive`` row would be orphaned by the narrowed CHECK."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    _insert_descriptor(sync_url, "destructive", "op.destroy")

    with pytest.raises(RuntimeError, match="destructive-tier"):
        command.downgrade(cfg, _DOWN_REVISION)


def test_downgrade_narrows_check_when_no_destructive_rows(alembic_cfg: tuple[Config, str]) -> None:
    """With no ``destructive`` rows, ``downgrade 0079`` narrows the CHECK
    back to three values — a subsequent ``destructive`` insert is rejected."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    _insert_descriptor(sync_url, "dangerous", "op.dangerous")

    command.downgrade(cfg, _DOWN_REVISION)

    with pytest.raises(IntegrityError):
        _insert_descriptor(sync_url, "destructive", "op.destroy")


def test_upgrade_round_trips_after_clean_downgrade(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0084`` → clean ``downgrade 0079`` → ``upgrade 0084`` again
    leaves the widened CHECK in place (idempotent constraint recreate)."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)

    _insert_descriptor(sync_url, "destructive", "op.destroy")
