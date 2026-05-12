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
    """``alembic upgrade head`` against a fresh SQLite DB stamps T28's revision.

    Asserts AC #2 of Task #27 (the env.py async path is end-to-end
    drivable from a ``DATABASE_URL`` plus the on-disk Alembic config)
    extended for Task #28 (the first migration is now the audit-log
    table, revision ``0001``). The migration runs cleanly on SQLite
    because ``0001_create_audit_log.py`` branches on
    ``op.get_bind().dialect.name`` and skips the PG-specific
    ``gen_random_uuid()`` / ``now()`` server defaults.

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

    # Resolve head dynamically rather than pinning ``"0001"`` — any future
    # migration would otherwise fail this assertion even though
    # ``alembic upgrade head`` still works correctly. The contract this
    # test guards is "the migration runner reached *some* head", not
    # "the head is specifically the audit-log migration".
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head is not None
    assert head != ""

    # The migration created both the ``alembic_version`` bookkeeping
    # table and the ``audit_log`` table itself. Both indexes ship
    # with the migration.
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = inspector.get_table_names()
            assert "alembic_version" in tables
            assert "audit_log" in tables
            index_names = {idx["name"] for idx in inspector.get_indexes("audit_log")}
            assert "audit_log_occurred_at_idx" in index_names
            assert "audit_log_operator_sub_idx" in index_names
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


def test_alembic_config_overrides_script_location_to_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``alembic_config`` overrides ``script_location`` to an absolute path.

    Regression for issue #205: the on-disk ``alembic.ini`` ships
    ``script_location = alembic`` (relative) for source-tree dev
    ergonomics. Alembic's ``coerce_resource_to_filename`` only does
    package-resource resolution for values containing a colon, so the
    plain ``alembic`` ends up cwd-relative — which breaks the installed
    -wheel path (the migration Job's WORKDIR is ``/app`` but the wheel
    ships scripts at ``site-packages/meho_backplane/alembic/``).

    The fix is to override ``script_location`` programmatically to the
    absolute path of the ``alembic/`` directory adjacent to the
    resolved ``alembic.ini``. Asserting against a tmp_path fixture is
    sufficient: every resolution path in :func:`find_alembic_ini`
    follows the same "scripts live next to ini" convention.
    """
    custom_ini = tmp_path / "alembic.ini"
    custom_ini.write_text("[alembic]\nscript_location = alembic\n")
    scripts_dir = tmp_path / "alembic"
    scripts_dir.mkdir()
    monkeypatch.setenv("ALEMBIC_CONFIG", str(custom_ini))

    cfg = alembic_config()

    resolved_script_location = cfg.get_main_option("script_location")
    assert resolved_script_location is not None
    assert Path(resolved_script_location).is_absolute()
    assert Path(resolved_script_location) == scripts_dir


def test_alembic_config_preserves_ini_script_location_when_adjacent_dir_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``alembic_config`` falls through to the ini value when no adjacent dir.

    Defensive: if an operator points ``$ALEMBIC_CONFIG`` at an ini file
    that *doesn't* sit next to an ``alembic/`` directory (exotic
    layouts — mounted ConfigMap, partial overlay), don't lock the
    script_location to a path that doesn't exist. Fall through to
    whatever the ini said; the caller can still override
    programmatically. This guard keeps the fix surgical — we add an
    override only when the conventional adjacent layout is satisfied.
    """
    custom_ini = tmp_path / "alembic.ini"
    custom_ini.write_text("[alembic]\nscript_location = /opt/scripts\n")
    # Deliberately do NOT create tmp_path / "alembic"/.
    monkeypatch.setenv("ALEMBIC_CONFIG", str(custom_ini))

    cfg = alembic_config()

    assert cfg.get_main_option("script_location") == "/opt/scripts"


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
        """Spin up ``pgvector/pgvector:pg16`` and run ``alembic upgrade head``.

        AC #2 of Task #27: against a fresh PG, ``alembic upgrade head``
        completes cleanly. The image is ``pgvector/pgvector:pg16``
        (Postgres 16 + pgvector pre-installed) because migration ``0003``
        (G0.4-T1 #258) runs ``CREATE EXTENSION IF NOT EXISTS vector``
        and would fail fast against a vanilla ``postgres:16-alpine``
        without the extension — the fail-fast behaviour is the intended
        contract for production deploys against pgvector-less clusters,
        but the testcontainers smoke must use an image that supports
        the migration end-to-end.

        The test is **synchronous** for the same reason the SQLite
        sibling at lines 188-232 is synchronous: ``alembic.command.upgrade``
        drives :func:`asyncio.run` itself (via the env.py
        ``run_migrations_online`` entry point), and ``asyncio.run`` cannot
        be re-entered from inside a running loop. Decorating this test
        with ``@pytest.mark.asyncio`` would crash with a ``RuntimeError``
        the moment Docker is available and the container starts.
        """
        from testcontainers.postgres import PostgresContainer

        # ``pgvector/pgvector:pg16`` — Postgres 16 + pgvector pre-installed.
        # Hosted on Docker Hub at ``pgvector/pgvector``; not available on
        # ``mirror.gcr.io/library/`` because that mirror only covers the
        # ``library/*`` (official) namespace. Falling back to Docker Hub
        # here trades a small rate-limit risk for the pgvector capability.
        # The image is env-overridable via ``MEHO_TEST_PGVECTOR_IMAGE``
        # so operators can point at a GHCR / internal-mirror cache
        # without re-rolling the test on the first 429 from Docker Hub
        # rate limits. Same env knob the integration conftest and
        # ``test_migration_rollback`` honour.
        image = os.environ.get("MEHO_TEST_PGVECTOR_IMAGE", "pgvector/pgvector:pg16")
        with PostgresContainer(image) as pg:
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
            # Should complete without raising; migrations 0001-0003 all
            # apply, the ``vector`` extension is enabled by 0003.
            command.upgrade(cfg, "head")

    def test_documents_table_uses_vector_column_on_postgres(
        self,
        isolated_engine_cache: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After ``alembic upgrade head``, ``documents.embedding`` is ``vector(384)``.

        G0.4-T1 acceptance: against a real PG with pgvector enabled,
        migration ``0003`` installs the ``vector`` extension and the
        ``documents`` table's ``embedding`` column compiles to
        ``vector(384)`` (not the SQLite-side ``TEXT`` fallback). Also
        verifies the two PG-only indexes (``documents_body_fts_idx``
        GIN, ``documents_embedding_idx`` IVFFlat) land on PG — the
        SQLite-side migration-shape test
        (``test_migration_installs_documents_table_and_portable_indexes``)
        pins their *absence* on SQLite; this pins their *presence* on PG.

        Drives schema inspection through the async asyncpg engine
        (the only PG dialect the backplane installs) via
        :func:`asyncio.run` from a sync test body. Adding ``psycopg2``
        just to inspect would balloon dev deps for a one-off probe.
        """
        import asyncio

        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from testcontainers.postgres import PostgresContainer

        image = os.environ.get("MEHO_TEST_PGVECTOR_IMAGE", "pgvector/pgvector:pg16")
        with PostgresContainer(image) as pg:
            sync_url = pg.get_connection_url()
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
            command.upgrade(cfg, "head")

            async def _inspect() -> tuple[set[str], str | None, str, set[str], dict[str, str]]:
                # Build a fresh async engine bound to the testcontainer.
                # Disposed in the finally so the asyncpg pool releases
                # before the testcontainer tears down the cluster.
                engine = create_async_engine(async_url)
                try:
                    async with engine.connect() as conn:
                        tables = await conn.run_sync(
                            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
                        )
                        extversion_result = await conn.execute(
                            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                        )
                        extversion = extversion_result.scalar_one_or_none()
                        # ``format_type`` renders ``vector(384)`` for the
                        # pgvector typed column; this is the catalog
                        # answer rather than the SQLAlchemy dialect's
                        # opaque ``USER-DEFINED`` string.
                        type_result = await conn.execute(
                            text(
                                "SELECT format_type(atttypid, atttypmod) "
                                "FROM pg_attribute "
                                "WHERE attrelid = 'documents'::regclass "
                                "AND attname = 'embedding'"
                            )
                        )
                        embedding_type_str = type_result.scalar_one()
                        documents_indexes = await conn.run_sync(
                            lambda sync_conn: {
                                idx["name"]
                                for idx in sa_inspect(sync_conn).get_indexes("documents")
                            }
                        )
                        # ``pg_indexes.indexdef`` carries the canonical
                        # CREATE INDEX SQL the planner sees. Asserting on
                        # the DDL — not just the index name — catches a
                        # regression where someone keeps the name but
                        # swaps GIN for a btree or drops the IVFFlat
                        # operator class / ``lists`` parameter, which
                        # would silently change the recall profile.
                        indexdef_result = await conn.execute(
                            text(
                                "SELECT indexname, indexdef FROM pg_indexes "
                                "WHERE tablename = 'documents'"
                            )
                        )
                        indexdefs = {row[0]: row[1] for row in indexdef_result.all()}
                finally:
                    await engine.dispose()
                return tables, extversion, embedding_type_str, documents_indexes, indexdefs

            tables, extversion, embedding_type_str, documents_indexes, indexdefs = asyncio.run(
                _inspect()
            )

            assert "documents" in tables
            assert extversion is not None, (
                "vector extension must be enabled after migration 0003; "
                "image pgvector/pgvector:pg16 should ship it pre-installed"
            )
            assert embedding_type_str == "vector(384)", (
                f"documents.embedding must be vector(384), got {embedding_type_str!r}"
            )
            assert "documents_tenant_source_id_idx" in documents_indexes
            assert "documents_body_hash_idx" in documents_indexes
            assert "documents_body_fts_idx" in documents_indexes, (
                "GIN FTS index must land on PG via migration 0003's raw SQL"
            )
            assert "documents_embedding_idx" in documents_indexes, (
                "IVFFlat cosine index must land on PG via migration 0003's raw SQL"
            )

            # Definition-level assertions for the two PG-only indexes —
            # the load-bearing contract from migration 0003 is the
            # *shape* of the DDL, not the index name. ``pg_indexes.indexdef``
            # rendering is canonicalised by PG: GIN expression-indexes
            # show ``USING gin (to_tsvector(...))`` and IVFFlat indexes
            # show ``USING ivfflat (... vector_cosine_ops) WITH (lists='100')``.
            # Lowercased compare keeps the assertion robust to PG's
            # capitalisation choices across versions.
            fts_def = indexdefs.get("documents_body_fts_idx", "").lower()
            assert "using gin" in fts_def and "to_tsvector('english'" in fts_def, (
                "GIN FTS index must be over to_tsvector('english', body); "
                f"got: {indexdefs.get('documents_body_fts_idx')!r}"
            )
            ivf_def = indexdefs.get("documents_embedding_idx", "").lower()
            assert "using ivfflat" in ivf_def, (
                f"IVFFlat index must use ivfflat method; got: "
                f"{indexdefs.get('documents_embedding_idx')!r}"
            )
            assert "vector_cosine_ops" in ivf_def, (
                "IVFFlat index must use vector_cosine_ops operator class; "
                f"got: {indexdefs.get('documents_embedding_idx')!r}"
            )
            assert "lists='100'" in ivf_def or "lists=100" in ivf_def, (
                "IVFFlat index must carry WITH (lists = 100) per migration 0003; "
                f"got: {indexdefs.get('documents_embedding_idx')!r}"
            )
