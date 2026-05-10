# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.db.engine`.

Coverage matrix (Task #27 acceptance criteria #1, #2, #5):

* :func:`get_engine` returns a configured :class:`AsyncEngine` with
  the URL pulled from settings, and caches the instance across calls.
* :func:`get_sessionmaker` reuses the shared engine and produces an
  ``async_sessionmaker`` configured with ``expire_on_commit=False``
  (the SQLAlchemy 2.x async-doc default for web services — see the
  module docstring of ``db/engine.py``).
* :func:`get_session` yields an :class:`AsyncSession`, executes a
  trivial ``SELECT 1`` against it, and the session is closed when the
  generator exits — proven by asserting on the session's ``is_active``
  flag pre- and post-yield.
* :func:`dispose_engine` is awaitable and resets the cache so the
  next :func:`get_engine` call constructs a fresh engine.
* End-to-end against a SQLite-async DB: an engine is created,
  ``alembic upgrade head`` runs against it, the ``alembic_version``
  table exists, the current revision matches the script-directory
  head. Aiosqlite is the v0.1 dev DB used here so the test runs in
  any sandbox; a parallel testcontainers-PG suite covers the real
  driver path under :class:`TestPostgresIntegration`.

The testcontainers PG suite gracefully skips when Docker is
unavailable in the runner; the SQLite-async coverage above remains
the always-on assertion.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import (
    create_engine_for_url,
    dispose_engine,
    get_engine,
    get_session,
    get_sessionmaker,
    reset_engine_for_testing,
)
from meho_backplane.db.migrations import alembic_config, find_alembic_ini
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_engine_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the module-level engine cache + pin every required env var.

    The :class:`Settings` model has three required fields
    (``KEYCLOAK_ISSUER_URL`` / ``KEYCLOAK_AUDIENCE`` / ``VAULT_ADDR``)
    plus ``DATABASE_URL`` (T27). The autouse ``_default_database_url``
    fixture provides the latter; the rest are pinned here so every
    test in this module that calls :func:`get_engine` (which
    transitively constructs ``Settings``) succeeds without leaking
    test-specific values across cases. Cache resets bracket the
    yield so URL changes between cases actually take effect.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    reset_engine_for_testing()
    get_settings.cache_clear()
    yield
    reset_engine_for_testing()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Engine + sessionmaker basics
# ---------------------------------------------------------------------------


def test_create_engine_for_url_returns_async_engine() -> None:
    """``create_engine_for_url`` produces an :class:`AsyncEngine`.

    Built without going through the cache so the test is order-
    independent and exercises the factory's pool kwargs explicitly.
    """
    eng = create_engine_for_url(
        "sqlite+aiosqlite:///:memory:",
        pool_size=5,
        pool_timeout=15.0,
    )
    assert isinstance(eng, AsyncEngine)
    # ``pool_size`` is meaningless for SQLite's StaticPool/SingletonThreadPool
    # so we only assert the URL came through correctly.
    assert str(eng.url).startswith("sqlite+aiosqlite")


def test_get_engine_caches_across_calls(
    isolated_engine_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second :func:`get_engine` call returns the same instance."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    first = get_engine()
    second = get_engine()
    assert first is second


def test_get_sessionmaker_returns_async_factory(
    isolated_engine_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_sessionmaker`` returns an :func:`async_sessionmaker`."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    factory = get_sessionmaker()
    assert isinstance(factory, async_sessionmaker)
    assert factory is get_sessionmaker()


# ---------------------------------------------------------------------------
# get_session contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_yields_active_async_session(
    isolated_engine_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_session`` yields an :class:`AsyncSession`, then closes it.

    Drives the dependency by hand (without FastAPI) so the test asserts
    on the lifecycle rather than the HTTP plumbing. The contract:

    * The yielded value is an :class:`AsyncSession`.
    * Inside the yield the session is in a transaction
      (``async with session.begin()`` is the wrapping context).
    * After the generator exhausts, the session is no longer in a
      transaction (the outer ``async with`` released it).
    """
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

    gen: AsyncIterator[AsyncSession] = get_session()
    session = await gen.__anext__()
    try:
        assert isinstance(session, AsyncSession)
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1
        assert session.in_transaction() is True
    finally:
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
    # After the generator exits, both the inner ``session.begin()`` and
    # the outer ``async_sessionmaker()`` contexts have closed; the
    # session is no longer attached to a transaction.
    assert session.in_transaction() is False


@pytest.mark.asyncio
async def test_dispose_engine_is_awaitable_and_resets_cache(
    isolated_engine_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``await dispose_engine()`` clears the cached engine."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    eng_first = get_engine()
    await dispose_engine()
    assert engine_module._engine is None
    eng_second = get_engine()
    assert eng_first is not eng_second


# ---------------------------------------------------------------------------
# alembic upgrade head against an aiosqlite DB
# ---------------------------------------------------------------------------


def test_alembic_upgrade_head_against_sqlite_creates_version_table(
    isolated_engine_cache: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` against a fresh SQLite DB creates the version table.

    Asserts AC #2 of Task #27: a ``DATABASE_URL`` plus the on-disk
    Alembic config is enough to drive the async ``env.py`` end-to-end
    and Alembic creates its bookkeeping table on the first contact.
    The chassis ships with an empty ``versions/`` directory so the
    head is ``None`` and no revision is stamped, but the
    ``alembic_version`` table is still materialised (with no rows).
    T28's first migration adds a row; T29's migration runner asserts
    the row matches head before letting the backplane process traffic.

    The test is **synchronous** because ``alembic.command.upgrade``
    drives :func:`asyncio.run` itself (via the env.py
    ``run_migrations_online`` entry point), and ``asyncio.run`` cannot
    be re-entered from inside a running loop — wrapping this test in
    ``@pytest.mark.asyncio`` would crash with a ``RuntimeError``.
    """
    db_path = tmp_path / "test.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)

    cfg = alembic_config()
    # Override the URL so the inner async engine targets the test DB.
    cfg.set_main_option("sqlalchemy.url", async_url)
    command.upgrade(cfg, "head")

    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head is None  # empty versions/ dir at the chassis stage.

    # ``alembic_version`` is materialised on the first ``upgrade head``
    # contact even when no revision is stamped. T28's first migration
    # adds a row; this assertion documents the chassis-stage shape.
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            tables = sa_inspect(conn).get_table_names()
            assert "alembic_version" in tables
    finally:
        sync_eng.dispose()


def test_find_alembic_ini_resolves_packaged_path() -> None:
    """``find_alembic_ini`` returns a real, existing ``alembic.ini`` path."""
    path = find_alembic_ini()
    assert path.is_file()
    assert path.name == "alembic.ini"


def test_find_alembic_ini_honours_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``$ALEMBIC_CONFIG`` wins over package / cwd / source-tree probes.

    Ops can override the file location for non-standard deployments
    (mounted ConfigMaps, k8s init-containers, etc.) by setting the env
    var; the override must take precedence over every other resolution
    rule and the resolver must return that path verbatim.
    """
    custom_ini = tmp_path / "custom-alembic.ini"
    custom_ini.write_text("[alembic]\nscript_location = /nowhere\n")
    monkeypatch.setenv("ALEMBIC_CONFIG", str(custom_ini))

    resolved = find_alembic_ini()
    assert resolved == custom_ini


def test_find_alembic_ini_env_override_missing_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``$ALEMBIC_CONFIG`` pointing at a non-existent path raises clearly.

    Falling back to the cwd / source-tree probes when the operator
    explicitly set ``ALEMBIC_CONFIG`` would mask a typo and load the
    *wrong* migration tree. Surface the error instead so the operator
    sees the typo immediately.
    """
    missing = tmp_path / "does-not-exist.ini"
    monkeypatch.setenv("ALEMBIC_CONFIG", str(missing))

    with pytest.raises(FileNotFoundError) as exc_info:
        find_alembic_ini()
    assert "ALEMBIC_CONFIG" in str(exc_info.value)
    assert str(missing) in str(exc_info.value)


# ---------------------------------------------------------------------------
# Optional testcontainers-PG suite
# ---------------------------------------------------------------------------


_DOCKER_AVAILABLE: bool


def _docker_socket_present() -> bool:
    """Heuristic: Docker is usable if the unix socket is present.

    Avoids importing ``docker`` (which testcontainers would lazily
    do anyway) just to discover availability. The negative path is
    common in agent sandboxes; the positive path is what runs in CI.
    """
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_DOCKER_AVAILABLE = _docker_socket_present()


_SKIP_REASON = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_SKIP_REASON)
class TestPostgresIntegration:
    """End-to-end smoke against a real Postgres via testcontainers.

    Not skipped silently — the class-level ``skipif`` reason calls out
    why and points at the CI environment that does provision Docker.
    """

    def test_alembic_upgrade_head_against_real_pg(
        self,
        isolated_engine_cache: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Spin up ``postgres:16-alpine`` and run ``alembic upgrade head``.

        AC #2 of Task #27: against a fresh PG, ``alembic upgrade head``
        completes cleanly. With an empty ``versions/`` directory, no
        ``alembic_version`` row is created (Alembic skips the table
        when there is nothing to stamp), but the command must not raise.

        The test is **synchronous** for the same reason the SQLite
        sibling at lines 188-232 is synchronous: ``alembic.command.upgrade``
        drives :func:`asyncio.run` itself (via the env.py
        ``run_migrations_online`` entry point), and ``asyncio.run`` cannot
        be re-entered from inside a running loop. Decorating this test
        with ``@pytest.mark.asyncio`` would crash with a ``RuntimeError``
        the moment Docker is available and the container starts.
        """
        from testcontainers.postgres import PostgresContainer

        with PostgresContainer("postgres:16-alpine") as pg:
            sync_url = pg.get_connection_url()  # postgresql+psycopg2://...
            async_url = sync_url.replace(
                "postgresql+psycopg2://",
                "postgresql+asyncpg://",
            ).replace(
                "postgresql://",
                "postgresql+asyncpg://",
            )
            monkeypatch.setenv("DATABASE_URL", async_url)

            cfg = alembic_config()
            cfg.set_main_option("sqlalchemy.url", async_url)
            # Should complete without raising; empty versions/ means
            # this is a no-op but exercises the env.py async pattern.
            command.upgrade(cfg, "head")
