# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0036_add_approval_request_params``.

Initiative #1500 (G0.20 v0.10.1 dogfood hardening), Task #1503. Adds the
nullable ``approval_request.params`` JSON column — the dispatcher stores
the original dispatch params on the row at park time so any approval
surface (REST ``/decide``, MCP by-id approve) can re-dispatch a parked
direct operator op with the stored params instead of only recording the
decision. Soft-column discipline mirrors 0024 / 0030: nullable, no
server default, reversible. The migration uses only generic ``sa.JSON``
so PG and SQLite parity holds (the ORM pins ``JSONB`` on PG via
``with_variant``).
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


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0036.db"
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


def _approval_request_columns(sync_url: str) -> list[tuple[str, int]]:
    """Return ``(name, notnull)`` for every ``approval_request`` column."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(approval_request)")).all()
    finally:
        sync_eng.dispose()
    # PRAGMA columns: cid, name, type, notnull, dflt_value, pk.
    return [(str(row[1]), int(row[3])) for row in rows]


def _column_names(sync_url: str) -> set[str]:
    return {name for name, _ in _approval_request_columns(sync_url)}


def test_upgrade_adds_params_column_nullable(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade head`` lands the ``params`` column as nullable JSON."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    columns = _approval_request_columns(sync_url)
    by_name = dict(columns)
    assert "params" in by_name, "params column must be present after upgrade"
    # notnull flag (PRAGMA index 3): 0 == nullable.
    assert by_name["params"] == 0, "params must be nullable"


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade 0035`` drops ``params``; ``upgrade head`` restores it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert "params" in _column_names(sync_url)

    command.downgrade(cfg, "0035")
    assert "params" not in _column_names(sync_url)

    command.upgrade(cfg, "head")
    assert "params" in _column_names(sync_url)


def test_existing_columns_untouched(alembic_cfg: tuple[Config, str]) -> None:
    """Pre-0036 approval_request columns survive the ALTER."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    columns = _column_names(sync_url)
    for column in (
        "id",
        "tenant_id",
        "run_id",
        "principal_sub",
        "principal_act",
        "op_id",
        "connector_id",
        "target_id",
        "params_hash",
        "proposed_effect",
        "status",
        "reviewed_by",
        "decided_at",
        "created_at",
        "expires_at",
    ):
        assert column in columns, f"pre-0036 column {column!r} must survive"


def test_orm_field_resolves_and_defaults_none() -> None:
    """:attr:`ApprovalRequest.params` resolves and defaults to ``None``."""
    import uuid
    from datetime import UTC, datetime

    from meho_backplane.db.models import ApprovalRequest

    assert hasattr(ApprovalRequest, "params")
    row = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        principal_sub="op-smoke",
        op_id="some.op",
        connector_id="some-1.x",
        params_hash="deadbeef",
        proposed_effect={},
        status="pending",
        created_at=datetime.now(UTC),
    )
    assert row.params is None
