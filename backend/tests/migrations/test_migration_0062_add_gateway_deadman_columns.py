# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0062_add_gateway_deadman_columns``.

Initiative #2415, Task #2501. Adds the dead-man-switch liveness columns:
``runner_principal.last_seen_at`` (``NOT NULL``, server-default ``now()``,
+ a b-tree index) and ``runner_assignments.stale_at`` (``NULL``). The
**fifth** migration in the initiative's serialized chain; renumbered
0061->0062 at drain time to sit after the sibling #2500 ``gateway_command``
capability migration, so it now extends the then-current single head
``0061`` (``gateway_command`` capability-binding columns).

**Idempotency pinning (0049/0050/0053/0055/0057/0058/0060/0061 footgun).** Every
forward / round-trip / stamp-replay step targets this migration's **own**
revision (``0062``) and its ``down_revision`` (``0061``), never ``head`` —
so a future head migration cannot make ``upgrade("head")`` re-run these
``add_column`` calls on a schema that already has them. SQLite drives the
test; the migration branches the ``last_seen_at`` default per dialect
(``now()`` on PG, a constant literal on SQLite, which forbids
``CURRENT_TIMESTAMP`` as an ADD COLUMN default).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0062"
_DOWN_REVISION = "0061"

_PRINCIPAL_TABLE = "runner_principal"
_ASSIGNMENTS_TABLE = "runner_assignments"
_LAST_SEEN_COLUMN = "last_seen_at"
_STALE_COLUMN = "stale_at"
_LAST_SEEN_INDEX = "runner_principal_last_seen_at_idx"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0062.db"
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


def _columns(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _column_row(sync_url: str, table: str, column: str) -> tuple[object, ...]:
    """Return the ``PRAGMA table_info`` row for *column* (cid, name, type, notnull, dflt, pk)."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    for row in rows:
        if str(row[1]) == column:
            return tuple(row)
    raise AssertionError(f"column {column!r} not present on {table!r}")


def _index_names(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def test_upgrade_adds_both_columns(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0062`` adds ``last_seen_at`` + ``stale_at`` to the gateway tables."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _LAST_SEEN_COLUMN in _columns(sync_url, _PRINCIPAL_TABLE)
    assert _STALE_COLUMN in _columns(sync_url, _ASSIGNMENTS_TABLE)


def test_last_seen_at_not_null_with_default(alembic_cfg: tuple[Config, str]) -> None:
    """``last_seen_at`` lands ``NOT NULL`` with a non-NULL default (initialises rows)."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk).
    _cid, _name, _type, notnull, dflt_value, _pk = _column_row(
        sync_url, _PRINCIPAL_TABLE, _LAST_SEEN_COLUMN
    )
    assert notnull == 1, "last_seen_at must be NOT NULL"
    assert dflt_value is not None, "last_seen_at needs a server default to initialise existing rows"


def test_stale_at_is_nullable(alembic_cfg: tuple[Config, str]) -> None:
    """``stale_at`` is nullable — ``NULL`` = fresh, the un-flipped default."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    _cid, _name, _type, notnull, _dflt, _pk = _column_row(
        sync_url, _ASSIGNMENTS_TABLE, _STALE_COLUMN
    )
    assert notnull == 0, "stale_at must be nullable (NULL = fresh)"


def test_upgrade_creates_last_seen_index(alembic_cfg: tuple[Config, str]) -> None:
    """The sweeper's ``last_seen_at`` scan index is created."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _LAST_SEEN_INDEX in _index_names(sync_url, _PRINCIPAL_TABLE)


def test_stamp_down_revision_then_upgrade_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Stamp ``0061`` then upgrade to ``0062`` — pinned to own revisions, never ``head``.

    Builds the schema through the parent revision, stamps the version table
    to ``0061``, then replays **only** this migration to its own revision. A
    future head migration cannot make this step re-run the ``add_column`` on
    an already-migrated schema, since no leg targets ``"head"``.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    command.stamp(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)

    assert _LAST_SEEN_COLUMN in _columns(sync_url, _PRINCIPAL_TABLE)
    assert _STALE_COLUMN in _columns(sync_url, _ASSIGNMENTS_TABLE)


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0061"`` drops the columns + index; ``upgrade "0062"`` re-adds them.

    Pinned to this migration's own revision on both legs (never ``head``).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _LAST_SEEN_COLUMN in _columns(sync_url, _PRINCIPAL_TABLE)

    command.downgrade(cfg, _DOWN_REVISION)
    assert _LAST_SEEN_COLUMN not in _columns(sync_url, _PRINCIPAL_TABLE)
    assert _STALE_COLUMN not in _columns(sync_url, _ASSIGNMENTS_TABLE)
    assert _LAST_SEEN_INDEX not in _index_names(sync_url, _PRINCIPAL_TABLE)

    command.upgrade(cfg, _REVISION)
    assert _LAST_SEEN_COLUMN in _columns(sync_url, _PRINCIPAL_TABLE)
    assert _STALE_COLUMN in _columns(sync_url, _ASSIGNMENTS_TABLE)
