# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared fixtures for the ``tests/integration/`` package.

The fixtures here scaffold the **integration** test surface — the one
that exercises multiple chassis subsystems end-to-end against a real
PostgreSQL container. Pieces shared with the unit suites
(JWT minter, JWKS mock, in-process Vault fake, secret-leak sweep,
default DB env var) come from :mod:`tests.conftest` and the existing
helper modules; this conftest only adds the integration-specific
pieces:

* ``_docker_socket_present`` — boolean module-level constant captured
  once at collection time. Mirrors the pattern in
  :mod:`tests.test_migration_rollback` and
  :mod:`tests.test_db_engine`. Modules in this package use it to skip
  the whole testcontainers-driven class when the agent sandbox has no
  Docker; CI runners provision Docker so the tests run there.
* ``async_pg_url`` — module-scoped fixture that boots a single
  ``postgres:16-alpine`` container, applies ``alembic upgrade head``
  against the asyncpg-translated URL, and yields the URL string for
  every test in the module. Module scope (rather than function scope)
  amortises the ~3-second container boot across the five tests in
  :mod:`tests.integration.test_tenant_isolation` so the suite still
  finishes well inside the issue's "< 10s wall clock" acceptance
  criterion. Migrations land once, not five times; the per-test
  ``audit_log`` truncation in ``integration_app`` keeps test isolation
  honest even though the DB is shared.
* ``integration_env`` — autouse fixture that pins every Settings env
  var the chassis needs at construction time, then yields. The
  conftest in ``tests/conftest.py`` already pins ``DATABASE_URL`` to a
  per-test SQLite tmp file via its own autouse fixture; this
  integration-package fixture overrides it to the testcontainer's
  asyncpg URL once the container is booted, so the audit middleware
  writes through the real Postgres.
* ``integration_app`` — function-scoped fixture that builds a fresh
  :class:`fastapi.FastAPI` mirroring the production wiring in
  :mod:`meho_backplane.main` plus the ``/api/v1/rbac-test`` stub
  routes mounted unconditionally (so Test 4 in
  :mod:`tests.integration.test_tenant_isolation` can drive the RBAC
  primitive end-to-end without the production app's
  ``MEHO_ENABLE_RBAC_TEST_ROUTE`` env-var gate). Truncates
  ``audit_log`` between tests so per-tenant row counts are exact.

Why fresh apps rather than reusing :data:`meho_backplane.main.app`:
the production singleton is constructed at module-import time before
this package's env var fixtures run, so its rbac-test mount decision
is fixed. Building inline keeps every test's middleware stack and
route table reproducible from the test source alone.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from meho_backplane.api.v1.health import router as api_v1_health_router
from meho_backplane.api.v1.rbac_test import router as api_v1_rbac_test_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import (
    create_engine_for_url,
    dispose_engine,
    reset_engine_for_testing,
)
from meho_backplane.db.migrations import alembic_config
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from tests._oidc_jwt_helpers import ISSUER as _ISSUER

# ---------------------------------------------------------------------------
# Docker-availability skip — same shape as test_migration_rollback /
# test_db_engine so the integration suites all gate on the same heuristic.
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    """Heuristic: Docker is usable if the unix socket is present.

    Matches :func:`tests.test_migration_rollback._docker_socket_present`
    so the skip condition stays uniform across the testcontainers-PG
    suites. Agent sandboxes without Docker skip; CI runners with Docker
    provisioned run.
    """
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


DOCKER_AVAILABLE: bool = _docker_socket_present()
SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


# ---------------------------------------------------------------------------
# Postgres URL translation (lifted from test_migration_rollback)
# ---------------------------------------------------------------------------


def _async_url_from(sync_url: str) -> str:
    """Translate a testcontainers sync URL to the asyncpg URL.

    ``PostgresContainer.get_connection_url`` returns
    ``postgresql+psycopg2://...`` by default. ADR 0004 pins the
    backplane to asyncpg; this helper rewrites both the default
    ``+psycopg2`` and the bare ``postgresql://`` shapes that older
    testcontainers versions emit.
    """
    return sync_url.replace(
        "postgresql+psycopg2://",
        "postgresql+asyncpg://",
    ).replace(
        "postgresql://",
        "postgresql+asyncpg://",
    )


# ---------------------------------------------------------------------------
# Module-scoped Postgres container — boots once, runs migrations once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def async_pg_url() -> Iterator[str]:
    """Boot a Postgres container, migrate it to head, yield the asyncpg URL.

    Module scope means the ~3-second container start cost is amortised
    across every test in the consuming module. The five tests in
    :mod:`tests.integration.test_tenant_isolation` together must fit
    inside the issue's "< 10s wall clock" budget; per-test container
    boots would blow that budget on the first test alone.

    The container is started inside this fixture rather than as a
    session-scoped resource so a failed boot only fails the consuming
    module, and the cleanup (``__exit__`` on ``PostgresContainer``)
    runs deterministically when the module finishes its tests rather
    than at interpreter shutdown.

    Migrations are applied via Alembic against the asyncpg URL — same
    path :mod:`meho_backplane.alembic.env` uses, so the schema the
    tests see is byte-identical to a fresh production deploy.
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    # Local import: testcontainers transitively imports the ``docker``
    # SDK, which probes the socket on import. Keeping the import inside
    # the fixture body means modules that inherit this conftest don't
    # crash at collection time on a no-Docker sandbox — they collect
    # fine and skip the consuming tests.
    from testcontainers.postgres import PostgresContainer

    # mirror.gcr.io: same registry pin as test_db_engine /
    # test_migration_rollback so the supply-chain audit (Goal #11
    # acceptance criteria) covers a single image source.
    with PostgresContainer("mirror.gcr.io/library/postgres:16-alpine") as pg:
        sync_url = pg.get_connection_url()
        async_url = _async_url_from(sync_url)

        # Apply the migration tree (0001 → 0002) against the container.
        # ``DATABASE_URL`` must be set before ``command.upgrade`` so
        # backend/alembic/env.py's ``os.environ.get("DATABASE_URL")``
        # override picks up the container URL rather than whatever
        # the parent process inherited (the autouse SQLite default
        # from :mod:`tests.conftest`).
        previous_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = async_url
        try:
            cfg = alembic_config()
            cfg.set_main_option("sqlalchemy.url", async_url)
            command.upgrade(cfg, "head")
            yield async_url
        finally:
            if previous_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous_url


# ---------------------------------------------------------------------------
# Per-test environment, engine cache, JWKS cache, audit-table cleanup
# ---------------------------------------------------------------------------


@pytest.fixture
def integration_env(
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Pin every chassis env var + redirect ``DATABASE_URL`` at the PG container.

    The autouse ``_default_database_url`` fixture in
    :mod:`tests.conftest` set ``DATABASE_URL`` at a tmp-path SQLite
    file *and* ran ``alembic upgrade head`` against that file. This
    fixture overrides the env var to the testcontainer's asyncpg URL,
    clears the cached :class:`Settings` and engine, and yields. The
    autouse fixture's SQLite migration is harmless extra work — the
    PG fixture replaces it before any audit row is written.

    The JWKS cache is reset around the yield so the JWT mocking in
    each test can pin its own key without inheriting a sibling test's
    cached JWKS.
    """
    monkeypatch.setenv("DATABASE_URL", async_pg_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()

    yield

    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
async def pg_engine(integration_env: None, async_pg_url: str) -> AsyncIterator[None]:
    """Inject the testcontainer engine into the module-level cache.

    The audit middleware resolves its sessionmaker via
    :func:`meho_backplane.db.engine.get_sessionmaker`, which reads the
    module-level ``_engine``. Tests that drive the production app must
    point that cache at the testcontainer or audit writes hit the
    SQLite default the autouse conftest fixture wired up.

    Truncates ``audit_log`` and ``tenant`` on entry so each test starts
    from an empty state even though ``async_pg_url`` is module-scoped
    (one container, multiple tests). Truncation is preferred over
    ``drop_all`` + ``upgrade`` because it skips the migration replay
    cost (Alembic's ``upgrade head`` against a freshly-truncated DB
    would still take ~150 ms; the truncate is sub-millisecond).
    """
    reset_engine_for_testing()
    eng = create_engine_for_url(async_pg_url, pool_size=5, pool_timeout=10.0)
    engine_module._engine = eng

    async with eng.connect() as conn:
        # ``RESTART IDENTITY CASCADE`` is unnecessary — neither table
        # has serial columns or FK relationships in v0.2 — but the
        # explicit ``TRUNCATE`` is what guarantees a stale row from a
        # prior test cannot leak into the next test's per-tenant
        # row-count assertions.
        await conn.execute(text("TRUNCATE TABLE audit_log"))
        await conn.execute(text("TRUNCATE TABLE tenant"))
        await conn.commit()

    try:
        yield
    finally:
        await dispose_engine()
        reset_engine_for_testing()


# ---------------------------------------------------------------------------
# Integration app builder
# ---------------------------------------------------------------------------


def build_integration_app() -> FastAPI:
    """Return a fresh :class:`FastAPI` with the production middleware stack.

    Mirrors :mod:`meho_backplane.main`'s wiring (audit middleware
    inside, request-context middleware outside, both registered before
    routers) plus the ``/api/v1/rbac-test`` stub routes mounted
    unconditionally so the integration test can drive RBAC end-to-end
    without flipping the production env-var gate.

    The function deliberately does **not** install the ``lifespan``
    hook from production — the lifespan calls ``configure_logging``,
    which would clobber any test-scoped structlog capture, and eagerly
    constructs an engine that the ``pg_engine`` fixture has already
    overridden. The audit middleware reads the engine cache lazily on
    first request, so skipping the lifespan is safe for these tests.
    """
    app = FastAPI()
    # add_middleware wraps existing app — last-added is outermost.
    # AuditMiddleware first → innermost; RequestContextMiddleware
    # second → outermost. Same ordering as production main.py.
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(api_v1_health_router)
    app.include_router(api_v1_rbac_test_router)
    return app


@pytest.fixture
def integration_app(pg_engine: None) -> FastAPI:
    """Return a fresh integration app with the PG engine cache primed."""
    return build_integration_app()


# ---------------------------------------------------------------------------
# DB read-back helpers
# ---------------------------------------------------------------------------


async def fetch_audit_rows_for_tenant(
    async_url: str,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Read every audit row scoped to *tenant_id*.

    Forward-compat probe for the future G8 audit-query API: today the
    helper is the smallest possible per-tenant SELECT, with no joins,
    no filtering beyond the tenant scope, no operator filter. G8 will
    replace it with a full query API; T6 only proves that the
    boundary holds.

    Issued through a dedicated short-lived :class:`AsyncEngine` so the
    query is independent of the engine the audit middleware just used,
    matching the read-back pattern in :mod:`tests.test_migration_rollback`.
    """
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT id::text, operator_sub, tenant_id::text, "
                    "method, path, status_code "
                    "FROM audit_log "
                    "WHERE tenant_id = :tid "
                    "ORDER BY occurred_at"
                ),
                {"tid": tenant_id},
            )
            rows = list(result.mappings().all())
    finally:
        await engine.dispose()
    return [dict(r) for r in rows]


async def count_audit_rows(async_url: str) -> int:
    """Total row count across all tenants — for cross-pollination checks."""
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT COUNT(*) FROM audit_log"))
            count = result.scalar_one()
    finally:
        await engine.dispose()
    return int(count)


def run_async(coro: Any) -> Any:
    """Run *coro* in a fresh event loop, returning its value.

    Wrapper around :func:`asyncio.run` that exists so the test bodies
    can stay synchronous (the testcontainers PG class form mirrors
    :class:`tests.test_migration_rollback.TestForwardCompatRollback`,
    where ``alembic.command.upgrade`` calls :func:`asyncio.run`
    internally and the outer test cannot itself be ``async``).
    """
    return asyncio.run(coro)
