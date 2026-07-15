# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0059_create_gateway_command``.

Initiative #2415 (Remote execution gateway), Task #2498. Creates the
``gateway_command`` queue table — the durable transport state the outbound
long-poll command plane claims from and reports onto.

Asserts the table + its columns land, the claim index exists, the
``status`` CHECK constraint rejects an out-of-vocabulary value, the
migration round-trips (downgrade drops it, re-upgrade recreates it), and
the migration's recorded status vocabulary agrees with the model enum
(:class:`~meho_backplane.db.models.GatewayCommandStatus`) — the drift
guard.

**Idempotency pinning (0049/0050/0053/0055 footgun).** Every forward /
round-trip step targets this migration's **own** revision (``0059``) and
its ``down_revision`` (``0058``), never ``head`` — so a future head
migration cannot make ``upgrade("head")`` re-run this ``create_table`` on
a schema that already has it. SQLite is the test driver and the migration
uses only generic DDL, so PG parity holds.
"""

from __future__ import annotations

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
from meho_backplane.db.models import GatewayCommandStatus
from meho_backplane.settings import get_settings

_REVISION = "0059"
_DOWN_REVISION = "0058"
_TABLE = "gateway_command"
_EXPECTED_COLUMNS = {
    "id",
    "tenant_id",
    "runner_id",
    "op_id",
    "params",
    "target_descriptor",
    "status",
    "result",
    "error",
    "enqueued_by_sub",
    "enqueued_at",
    "delivered_at",
    "completed_at",
}
_EXPECTED_INDEXES = {"gateway_command_claim_idx"}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0059.db"
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


def _table_names(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def _columns(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({_TABLE})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _index_names(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def test_upgrade_creates_table_columns_and_index(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0059`` creates ``gateway_command`` with its columns + claim index."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _TABLE in _table_names(sync_url)
    assert _columns(sync_url) >= _EXPECTED_COLUMNS
    assert _index_names(sync_url) >= _EXPECTED_INDEXES


def test_status_check_constraint_rejects_unknown_value(alembic_cfg: tuple[Config, str]) -> None:
    """The ``status`` CHECK constraint bounds the enum to the four known values."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        # FK enforcement stays off (SQLite default) so the bogus tenant_id
        # does not mask the status CHECK we are exercising.
        with sync_eng.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO gateway_command "
                    "(id, tenant_id, runner_id, op_id, params, status, "
                    "enqueued_by_sub, enqueued_at) "
                    "VALUES ('cmd-1', 'ten-1', 'runner-a', 'net.ping', '{}', "
                    "'bogus', 'sub-1', '2026-07-15')"
                )
            )
    finally:
        sync_eng.dispose()


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0058"`` drops the table; ``upgrade "0059"`` recreates it.

    Pinned to this migration's own revision on both legs (never ``head``)
    so a future head migration cannot break the round-trip.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _TABLE in _table_names(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert _TABLE not in _table_names(sync_url), "downgrade must drop gateway_command"
    assert not (_EXPECTED_INDEXES & _index_names(sync_url)), (
        "downgrade must drop the claim index too"
    )

    command.upgrade(cfg, _REVISION)
    assert _TABLE in _table_names(sync_url)


def _load_migration_0059() -> object:
    """Load migration ``0059`` as a module via its file path.

    Alembic version files are digit-prefixed and not importable as normal
    dotted modules; loading by file path with :mod:`importlib.util` is the
    robust way to reach the migration's recorded literal tuple.
    """
    import importlib.util

    path = (
        Path(__file__).resolve().parent.parent.parent
        / "alembic"
        / "versions"
        / "0059_create_gateway_command.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_0059", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_status_check_matches_enum() -> None:
    """The migration's status vocabulary matches :class:`GatewayCommandStatus`.

    Drift guard: if a new member is added to the enum without a migration
    update (or vice versa), this test catches the mismatch — the same
    lock-step discipline as ``test_migration_0023_approval_request``.
    """
    migration = _load_migration_0059()

    model_values = {s.value for s in GatewayCommandStatus}
    migration_values = set(migration._GATEWAY_COMMAND_STATUSES)  # type: ignore[attr-defined]
    assert model_values == migration_values
