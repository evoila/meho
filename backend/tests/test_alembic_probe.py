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

Plus the #1607 rollback-tolerance matrix: the DB *ahead* of the
image's head (the post-``helm rollback`` state — the pre-upgrade
migration Job's commit survives a manifest-only rollback) is healthy,
while the DB *behind* head stays unhealthy, and the pgvector gate
still applies on the ahead path.

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
from alembic.util.exc import CommandError
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
# #1607 rollback tolerance — DB ahead of the image's head
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_healthy_when_db_ahead_of_head(
    sqlite_engine: AsyncEngine,
) -> None:
    """DB stamped with a revision this image doesn't ship → ``ok=True``.

    The post-auto-rollback state (#1607): the ``pre-upgrade``
    migration Job committed ``0037``, the new release failed
    readiness, and Helm rolled the Deployment back to the prior image
    whose ``versions/`` ends at ``0036``. Helm reverts manifests only
    — the schema stays at ``0037``. The additive-only ``upgrade()``
    contract (``scripts/ci/check_migration_compat.py``) makes the
    newer schema readable by the older code, so the rolled-back pod
    must report Ready; the strict ``current == head`` equality this
    test replaces is what made the 2026-06-08 auto-rollback dead on
    arrival. ``get_revision`` raising ``CommandError`` mirrors
    Alembic's real unknown-revision behaviour (verified on 1.18.4).
    """
    await _create_alembic_version_table(sqlite_engine, "0037")

    with patch(
        "meho_backplane.db.migrations.ScriptDirectory.from_config",
    ) as fake_from_config:
        fake_script = fake_from_config.return_value
        fake_script.get_current_head.return_value = "0036"
        fake_script.get_revision.side_effect = CommandError(
            "Can't locate revision identified by '0037'",
        )
        result = await db_migration_probe()

    assert result.ok is True
    assert result.detail == "current=0037 head=0036 db_ahead=true"
    fake_script.get_revision.assert_called_once_with("0037")


@pytest.mark.asyncio
async def test_probe_unhealthy_when_db_behind_head(
    sqlite_engine: AsyncEngine,
) -> None:
    """DB stamped with an *older* revision this image knows → ``ok=False``.

    The dangerous direction: the code expects schema objects migration
    ``0036`` creates and the DB is still at ``0035`` (a forward-deploy
    missed its ``alembic upgrade head``). ``0035`` resolves in this
    image's script directory, so this is not the db-ahead rollback
    state — the fail-closed contract is preserved.
    """
    await _create_alembic_version_table(sqlite_engine, "0035")

    with patch(
        "meho_backplane.db.migrations.ScriptDirectory.from_config",
    ) as fake_from_config:
        fake_script = fake_from_config.return_value
        fake_script.get_current_head.return_value = "0036"
        # Default Mock behaviour: ``get_revision("0035")`` returns a
        # Mock Script — the revision is *known* to this image.
        result = await db_migration_probe()

    assert result.ok is False
    assert result.detail == "current=0035 head=0036"
    fake_script.get_revision.assert_called_once_with("0035")


@pytest.mark.asyncio
async def test_probe_db_ahead_still_fails_when_pgvector_missing(
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The db-ahead tolerance must not bypass the pgvector gate.

    A rolled-back pod on PostgreSQL with the ``vector`` extension
    dropped out-of-band is still unready — the revision tolerance and
    the extension check are orthogonal, and the ahead path joins the
    pgvector gate rather than short-circuiting around it. The detail
    reports the DB's actual revision (``0037``), not the image head.
    """
    await _create_alembic_version_table(sqlite_engine, "0037")

    with patch(
        "meho_backplane.db.migrations.ScriptDirectory.from_config",
    ) as fake_from_config:
        fake_script = fake_from_config.return_value
        fake_script.get_current_head.return_value = "0036"
        fake_script.get_revision.side_effect = CommandError(
            "Can't locate revision identified by '0037'",
        )
        monkeypatch.setattr(sqlite_engine.dialect, "name", "postgresql", raising=False)
        with patch(
            "meho_backplane.db.migrations._check_pgvector_extension",
            return_value=False,
        ):
            result = await db_migration_probe()

    assert result.ok is False
    assert result.detail == "revision=0037 pgvector=missing"


@pytest.mark.asyncio
async def test_probe_db_ahead_healthy_on_postgres_with_pgvector(
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rolled-back pod on PostgreSQL with pgvector intact → ``ok=True``.

    The full production rollback shape: PG dialect, schema ahead,
    extension present. Pins that the ahead path emits the
    ``db_ahead=true`` detail after the pgvector gate passes.
    """
    await _create_alembic_version_table(sqlite_engine, "0037")

    with patch(
        "meho_backplane.db.migrations.ScriptDirectory.from_config",
    ) as fake_from_config:
        fake_script = fake_from_config.return_value
        fake_script.get_current_head.return_value = "0036"
        fake_script.get_revision.side_effect = CommandError(
            "Can't locate revision identified by '0037'",
        )
        monkeypatch.setattr(sqlite_engine.dialect, "name", "postgresql", raising=False)
        with patch(
            "meho_backplane.db.migrations._check_pgvector_extension",
            return_value=True,
        ):
            result = await db_migration_probe()

    assert result.ok is True
    assert result.detail == "current=0037 head=0036 db_ahead=true"


# ---------------------------------------------------------------------------
# G0.4-T6 (#263) pgvector extension probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_skips_pgvector_check_on_sqlite(
    sqlite_engine: AsyncEngine,
) -> None:
    """SQLite path: probe healthy without ever querying pg_extension.

    The ``vector`` extension is a PostgreSQL concept; SQLite has no
    ``pg_extension`` catalog. The probe gates on
    ``engine.dialect.name == "postgresql"`` so the SQLite dev/test
    driver never sees the extension check. Regression-locks the
    dialect gate: a refactor that called the check unconditionally
    would crash with ``no such table: pg_extension`` on SQLite.
    """
    await _create_alembic_version_table(sqlite_engine, "headcafe1111")
    with patch(
        "meho_backplane.db.migrations.ScriptDirectory.from_config",
    ) as fake_from_config:
        fake_from_config.return_value.get_current_head.return_value = "headcafe1111"
        with patch(
            "meho_backplane.db.migrations._check_pgvector_extension",
        ) as fake_pgvector_check:
            result = await db_migration_probe()

    # Healthy on SQLite without invoking the pgvector helper.
    assert result.ok is True
    assert result.detail == "revision=headcafe1111"
    fake_pgvector_check.assert_not_called()


@pytest.mark.asyncio
async def test_probe_unhealthy_when_pgvector_missing(
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revision matches but pgvector absent → ``ok=False`` with explicit detail.

    Simulates the post-deploy drift mode: schema is correct
    (alembic head matches DB revision) but the extension was dropped
    out-of-band (operator ran ``DROP EXTENSION vector CASCADE`` or a
    backup restored the table without the catalog entry). The probe
    must flip ``/ready`` red rather than silently allowing retrieval
    writes against a column whose type the extension defined.

    Patches the dialect to ``postgresql`` (the SQLite engine is the
    test driver, but the probe gate is on dialect name not the
    actual driver) and stubs ``_check_pgvector_extension`` to return
    False -- mirrors the runtime shape without needing a real PG
    container.
    """
    await _create_alembic_version_table(sqlite_engine, "headcafe1111")

    with patch(
        "meho_backplane.db.migrations.ScriptDirectory.from_config",
    ) as fake_from_config:
        fake_from_config.return_value.get_current_head.return_value = "headcafe1111"
        # Force the dialect-gate to take the PG branch even though the
        # engine is SQLite -- the helper itself is patched away below.
        monkeypatch.setattr(sqlite_engine.dialect, "name", "postgresql", raising=False)
        with patch(
            "meho_backplane.db.migrations._check_pgvector_extension",
            return_value=False,
        ) as fake_pgvector_check:
            result = await db_migration_probe()

    assert result.ok is False
    assert result.detail == "revision=headcafe1111 pgvector=missing"
    fake_pgvector_check.assert_called_once()


@pytest.mark.asyncio
async def test_probe_healthy_when_pgvector_present(
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revision matches AND pgvector present → ``ok=True`` with revision detail.

    Mirrors the production happy path: the migration Job ran, the
    extension is loaded, retrieval is ready. Detail stays the same
    shape (``revision=<sha>``) as the SQLite happy path so existing
    operator tooling parsing the detail field doesn't need to
    branch on dialect.
    """
    await _create_alembic_version_table(sqlite_engine, "headcafe1111")

    with patch(
        "meho_backplane.db.migrations.ScriptDirectory.from_config",
    ) as fake_from_config:
        fake_from_config.return_value.get_current_head.return_value = "headcafe1111"
        monkeypatch.setattr(sqlite_engine.dialect, "name", "postgresql", raising=False)
        with patch(
            "meho_backplane.db.migrations._check_pgvector_extension",
            return_value=True,
        ):
            result = await db_migration_probe()

    assert result.ok is True
    assert result.detail == "revision=headcafe1111"


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
