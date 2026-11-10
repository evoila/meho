# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0142_add_approval_request_outcome_check`` (#5026).

The migration adds six nullable columns that hold what a person found when
they checked the target of an approved run with an unknown outcome, one
nullable column for the start of the latest re-run, and CHECKs on the
verdict and the reason. Every step targets this revision and its
``down_revision``, never ``head``.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.operations.approval_outcome_check import (
    OutcomeCheckVerdict,
    OutcomeUnknownReason,
)
from meho_backplane.settings import get_settings

_REVISION = "0142"
_DOWN_REVISION = "0141"
_TABLE = "approval_request"
_COLUMNS = (
    "outcome_checked_at",
    "outcome_checked_by",
    "outcome_checked_by_name",
    "outcome_check_verdict",
    "outcome_check_note",
    "outcome_check_reason",
    "rerun_started_at",
)
_VERSIONS = Path(__file__).resolve().parent.parent.parent / "alembic" / "versions"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0142.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, f"sqlite:///{db_path}"
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _columns(sync_url: str) -> dict[str, dict[str, object]]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return {c["name"]: dict(c) for c in inspect(conn).get_columns(_TABLE)}
    finally:
        eng.dispose()


def _insert(sync_url: str, *, row_id: str, verdict: str | None, reason: str | None = None) -> None:
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO approval_request (id, tenant_id, principal_sub, op_id, "
                    "connector_id, params_hash, proposed_effect, status, created_at, "
                    "outcome_check_verdict, outcome_check_reason) VALUES (:id, "
                    "'00000000-0000-0000-0000-000000005026', 'agent', 'op', 'c-1', 'h', "
                    "'{}', 'approved', '2026-11-07 00:00:00', :verdict, :reason)"
                ),
                {"id": row_id, "verdict": verdict, "reason": reason},
            )
    finally:
        eng.dispose()


def test_verdicts_and_reasons_match_the_enums() -> None:
    """The migration's value tuples equal the enums (drift guard)."""
    spec = importlib.util.spec_from_file_location(
        "_migration_0142", _VERSIONS / "0142_add_approval_request_outcome_check.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module._VERDICTS) == {v.value for v in OutcomeCheckVerdict}
    assert set(module._REASONS) == {r.value for r in OutcomeUnknownReason}


def test_upgrade_adds_nullable_columns_and_the_checks(
    alembic_cfg: tuple[Config, str],
) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    columns = _columns(sync_url)
    for name in _COLUMNS:
        assert columns[name]["nullable"] is True
    _insert(sync_url, row_id="00000000-0000-0000-0000-00000000c001", verdict=None)
    _insert(sync_url, row_id="00000000-0000-0000-0000-00000000c002", verdict="happened")
    _insert(sync_url, row_id="00000000-0000-0000-0000-00000000c003", verdict="did_not_happen")
    with pytest.raises(sa.exc.IntegrityError):
        _insert(sync_url, row_id="00000000-0000-0000-0000-00000000c004", verdict="maybe")
    _insert(
        sync_url,
        row_id="00000000-0000-0000-0000-00000000c006",
        verdict="happened",
        reason="started_no_result",
    )
    with pytest.raises(sa.exc.IntegrityError):
        _insert(
            sync_url,
            row_id="00000000-0000-0000-0000-00000000c007",
            verdict="happened",
            reason="other",
        )


def test_downgrade_drops_the_columns(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    _insert(sync_url, row_id="00000000-0000-0000-0000-00000000c005", verdict="happened")
    command.downgrade(cfg, _DOWN_REVISION)

    columns = _columns(sync_url)
    for name in _COLUMNS:
        assert name not in columns
