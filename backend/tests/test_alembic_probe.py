# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :func:`meho_backplane.db.migrations.db_migration_probe`.

Coverage matrix (Task #27 acceptance criteria #3, #4):

* Probe healthy when the database revision matches the on-disk head.
* Probe unhealthy when the database revision diverges from head.
* Probe unhealthy when the database is unreachable / driver fails.
* Probe unhealthy with a stable detail string when the
  ``alembic_version`` table is absent (fresh DB before ``upgrade head``).
* ``/api/v1/health.db.migrated`` reflects the probe outcome — the
  response field is no longer hardcoded ``None``.

The aiosqlite tests stay always-on; they exercise the probe's
revision comparison machinery without needing Docker. The probe's
async-engine code path is the same one a PG deployment hits — the
only DB-specific surface is the SQL the ``MigrationContext`` issues,
and Alembic's own contract is that the SQL is portable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meho_backplane.db.engine import (
    create_engine_for_url,
    dispose_engine,
    reset_engine_for_testing,
)
from meho_backplane.db.migrations import alembic_config, db_migration_probe
from meho_backplane.health import (
    ProbeResult,
    clear_probes,
    register_probe,
    run_probes_async,
)
from meho_backplane.main import app
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def sqlite_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[AsyncEngine]:
    """Create a per-test aiosqlite engine and wire it as the process engine.

    Resets the module-level cache before and after so the probe sees
    *this* engine when it calls :func:`get_engine`. The DB file lives
    under pytest's ``tmp_path`` so each test gets an isolated DB.
    """
    db_path = tmp_path / "probe.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    eng = create_engine_for_url(url, pool_size=5, pool_timeout=10.0)
    # Inject as the cached engine so db_migration_probe's get_engine() returns it.
    from meho_backplane.db import engine as engine_module

    engine_module._engine = eng
    try:
        yield eng
    finally:
        await dispose_engine()
        get_settings.cache_clear()
        reset_engine_for_testing()


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    """Empty the readiness-probe registry around every test."""
    clear_probes()
    yield
    clear_probes()


async def _create_alembic_version_table(eng: AsyncEngine, revision: str | None) -> None:
    """Create ``alembic_version`` and stamp it with *revision*.

    Used to manufacture the "applied revision" half of the head/current
    comparison. Mirrors what ``alembic stamp <rev>`` would do at the
    SQL layer; we issue the DDL directly so the test does not depend
    on the alembic CLI at runtime.
    """
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)",
            ),
        )
        if revision is not None:
            await conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:r)"),
                {"r": revision},
            )


# ---------------------------------------------------------------------------
# db_migration_probe outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_unhealthy_when_versions_present_but_db_unstamped(
    sqlite_engine: AsyncEngine,
) -> None:
    """Migrations on disk + fresh DB (no alembic_version) → diverged-style detail.

    Pre-T28 the chassis shipped with an empty ``versions/`` directory,
    so head and current were both ``None`` and the probe returned the
    ``no_migrations`` flavour of unhealthy. Once T28's first migration
    lands, head is ``0001`` and a fresh DB still has no
    ``alembic_version`` table; the probe's failure is now expressed
    as ``current=None head=0001`` (the ``current == head`` branch
    fails for a different reason than the empty case it once
    described). The fail-closed contract — ``ok=False`` with a
    structured detail — is preserved.
    """
    result = await db_migration_probe()
    assert result.name == "db"
    assert result.ok is False
    assert result.detail is not None
    # Resolve head dynamically from the script directory so the
    # assertion survives future migrations replacing 0001 as head.
    current_head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert f"head={current_head}" in result.detail
    assert "current=None" in result.detail


@pytest.mark.asyncio
async def test_probe_unhealthy_when_revisions_diverge(
    sqlite_engine: AsyncEngine,
) -> None:
    """DB stamped with one revision, head reports another → unhealthy.

    Patches :class:`ScriptDirectory` so head returns a synthetic
    revision; manufactures the ``alembic_version`` row with a
    different revision; asserts the probe returns ``ok=False`` with
    the diverged detail string.
    """
    await _create_alembic_version_table(sqlite_engine, "deadbeef0000")

    with patch(
        "meho_backplane.db.migrations.ScriptDirectory.from_config",
    ) as fake_from_config:
        fake_from_config.return_value.get_current_head.return_value = "headcafe1111"
        result = await db_migration_probe()

    assert result.ok is False
    assert result.detail == "current=deadbeef0000 head=headcafe1111"


@pytest.mark.asyncio
async def test_probe_healthy_when_revisions_match(
    sqlite_engine: AsyncEngine,
) -> None:
    """DB stamped with head's revision → ok=True, detail carries revision."""
    await _create_alembic_version_table(sqlite_engine, "headcafe1111")

    with patch(
        "meho_backplane.db.migrations.ScriptDirectory.from_config",
    ) as fake_from_config:
        fake_from_config.return_value.get_current_head.return_value = "headcafe1111"
        result = await db_migration_probe()

    assert result.ok is True
    assert result.detail == "revision=headcafe1111"


@pytest.mark.asyncio
async def test_probe_unhealthy_when_db_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB connection failure → ok=False with redacted detail.

    Resets the engine cache and points DATABASE_URL at an asyncpg URL
    that cannot connect (port 1 on localhost). The probe must not
    raise; AC #3 says it converts every failure to a structured
    ``ProbeResult``.
    """
    get_settings.cache_clear()
    reset_engine_for_testing()
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none",
    )
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    try:
        result = await db_migration_probe()
    finally:
        await dispose_engine()
        get_settings.cache_clear()
        reset_engine_for_testing()

    assert result.name == "db"
    assert result.ok is False
    assert result.detail is not None
    assert result.detail.startswith("check_failed: ")
    # Detail must not echo operator-controllable substrings (URL, host,
    # creds). The check_failed prefix carries only an exception class
    # name — anything else is a leak.
    assert "127.0.0.1" not in result.detail
    assert "nobody" not in result.detail


@pytest.mark.asyncio
async def test_probe_registers_with_async_runner(
    sqlite_engine: AsyncEngine,
) -> None:
    """``run_probes_async`` awaits :func:`db_migration_probe` correctly."""
    register_probe("db", db_migration_probe)
    results = await run_probes_async()
    assert len(results) == 1
    assert results[0].name == "db"
    assert isinstance(results[0], ProbeResult)


# ---------------------------------------------------------------------------
# /ready integration
# ---------------------------------------------------------------------------


def test_ready_reflects_db_probe_state(
    sqlite_engine: AsyncEngine,
) -> None:
    """``/ready`` payload includes the DB probe verdict.

    Registers only the DB probe (clearing Keycloak/Vault from the
    autouse fixture); asserts the response carries the structured
    diverged-revision detail when migrations exist on disk (T28's
    ``0001``) but the DB is fresh (no ``alembic_version`` table).
    """
    register_probe("db", db_migration_probe)

    client = TestClient(app)
    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    db_check = next(c for c in body["checks"] if c["name"] == "db")
    assert db_check["ok"] is False
    # Dynamic head — see other ``head=`` assertion in this module for
    # rationale; pinning the literal would tie every future migration
    # to also fixing this test.
    current_head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert f"head={current_head}" in (db_check["detail"] or "")
