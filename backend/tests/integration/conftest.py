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
  ``pgvector/pgvector:pg16`` container (image overridable via
  ``MEHO_TEST_PGVECTOR_IMAGE``; the pgvector-bearing image is required
  because migration ``0003`` runs ``CREATE EXTENSION vector``), applies
  ``alembic upgrade head`` against the asyncpg-translated URL, and
  yields the URL string for every test in the module. Module scope
  (rather than function scope) amortises the ~3-second container boot
  across the five tests in :mod:`tests.integration.test_tenant_isolation`
  so the suite still finishes well inside the issue's "< 10s wall clock"
  acceptance criterion. Migrations land once, not five times; the
  per-test ``TRUNCATE TABLE agent_run, audit_log, documents, graph_edge,
  graph_edge_history, graph_node, graph_node_history, broadcast_override,
  tenant`` in ``pg_engine`` keeps test isolation honest even though the
  DB is shared (non-cascading multi-table TRUNCATE is required because
  every table with a ``REFERENCES tenant(id)`` FK must be listed in the
  same statement, otherwise PG rejects with ``cannot truncate a table
  referenced in a foreign key constraint``).
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

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from meho_backplane.api.v1.health import router as api_v1_health_router
from meho_backplane.api.v1.rbac_test import router as api_v1_rbac_test_router
from meho_backplane.api.v1.retrieve import router as api_v1_retrieve_router
from meho_backplane.api.well_known import router as well_known_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import (
    create_engine_for_url,
    dispose_engine,
    reset_engine_for_testing,
)
from meho_backplane.db.migrations import alembic_config
from meho_backplane.mcp import router as mcp_router
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
    from docker.errors import APIError as _DockerAPIError
    from testcontainers.postgres import PostgresContainer

    # pgvector/pgvector:pg16 — Postgres 16 with the pgvector extension
    # pre-installed. After G0.4-T1 (#258) migration 0003 runs
    # ``CREATE EXTENSION IF NOT EXISTS vector`` as part of
    # ``alembic upgrade head``, so the testcontainers image must ship
    # the extension. Stock ``postgres:16-alpine`` (the v0.1-chassis
    # choice from #268) has no ``vector.control`` and fails fast,
    # which is why this conftest's image was swapped in lock-step
    # with the matching swaps in ``test_db_engine.py`` and
    # ``test_migration_rollback.py``. Image name is overridable via
    # ``MEHO_TEST_PGVECTOR_IMAGE`` so a registry-mirror swap (GHCR
    # cache, internal Harbor) does not require a code change — same
    # env-knob shape ``test_db_engine`` / ``test_migration_rollback``
    # honour.
    image = os.environ.get("MEHO_TEST_PGVECTOR_IMAGE", "pgvector/pgvector:pg16")
    pg = PostgresContainer(image)
    try:
        pg.start()
    except _DockerAPIError as exc:
        # Docker Hub rate-limit (429 / "too many requests") surfaces here
        # when CI runners exhaust the unauthenticated pull quota. Convert
        # to a skip rather than a hard failure so the suite's pass/fail
        # signal stays meaningful. Fix: set MEHO_TEST_PGVECTOR_IMAGE to a
        # GHCR or Harbor mirror, or add Docker Hub credentials to CI.
        msg = str(exc).lower()
        if "rate limit" in msg or "too many requests" in msg or "429" in msg:
            pytest.skip(
                f"Docker Hub pull rate-limited for {image!r}; "
                "set MEHO_TEST_PGVECTOR_IMAGE to a GHCR/Harbor mirror to fix"
            )
        raise
    try:
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
    finally:
        pg.stop()


# ---------------------------------------------------------------------------
# Per-test environment, engine cache, JWKS cache, audit-table cleanup
# ---------------------------------------------------------------------------


# Single source of truth for the non-PG chassis env vars every
# integration test needs. Lives at module scope so the autouse
# ``_integration_default_env`` fixture below and the opt-in
# ``integration_env`` fixture (which adds the PG-URL override) both
# read from the same dict — preventing the copy-paste drift the
# previous shape had.
_CHASSIS_ENV: dict[str, str] = {
    "KEYCLOAK_ISSUER_URL": _ISSUER,
    "KEYCLOAK_AUDIENCE": _AUDIENCE,
    "KEYCLOAK_JWKS_CACHE_TTL_SECONDS": "300",
    "KEYCLOAK_JWT_LEEWAY_SECONDS": "30",
    "VAULT_ADDR": "https://vault.test",
    "VAULT_OIDC_ROLE": "meho-mcp",
    "VAULT_OIDC_MOUNT_PATH": "jwt",
    "VAULT_TIMEOUT_SECONDS": "5.0",
}


@pytest.fixture(autouse=True)
def _integration_default_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Pin chassis env vars Settings() requires at construction time.

    Autouse for every integration test so any code path that
    transitively loads :class:`Settings` (audit middleware, connector
    dispatch chain forwarding through the dispatcher, etc.) does not
    die with ``KeyError: 'KEYCLOAK_ISSUER_URL'`` at
    ``settings.py:391``'s eager ``os.environ["..."]`` access.

    Deliberately does NOT depend on ``async_pg_url`` — fixtures that
    don't need PostgreSQL (k3d / bind9 connector integration tests
    that talk to their own testcontainer) get the chassis env vars
    for free without paying the ~3-second pgvector boot. The opt-in
    ``integration_env`` fixture below stays the way to layer the
    PG-URL override on top for tests that DO need the real DB.

    Mirrors the autouse-for-invariants discipline the
    :mod:`tests.conftest` ``_default_database_url`` fixture sets at
    the unit level. The ``get_settings.cache_clear()`` /
    ``clear_jwks_cache()`` calls bracket the yield so neither cache
    bleeds between tests.
    """
    for key, value in _CHASSIS_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()

    yield

    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def integration_env(
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Override ``DATABASE_URL`` to the PG testcontainer's asyncpg URL.

    Composes with the autouse ``_integration_default_env`` fixture
    above — that one pins the non-PG chassis env vars (KEYCLOAK_* /
    VAULT_*); this one layers the testcontainer's PG URL on top for
    tests that actually need the real DB. Tests that don't need PG
    simply don't request this fixture and pay zero pgvector-boot
    cost.

    The autouse ``_default_database_url`` fixture in
    :mod:`tests.conftest` had already set ``DATABASE_URL`` at a
    tmp-path SQLite file *and* run ``alembic upgrade head`` against
    that file. This fixture overrides the env var to the
    testcontainer's asyncpg URL, clears the cached :class:`Settings`
    and engine, and yields. The autouse fixture's SQLite migration
    is harmless extra work — the PG fixture replaces it before any
    audit row is written.

    The JWKS cache is reset around the yield so the JWT mocking in
    each test can pin its own key without inheriting a sibling test's
    cached JWKS.
    """
    monkeypatch.setenv("DATABASE_URL", async_pg_url)
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
        # Truncate every chassis table in one statement: PG requires
        # that a TRUNCATE against a table referenced by a FK either
        # use ``CASCADE`` or include every referencing table in the
        # same statement. The set of real ``REFERENCES tenant(id)`` FKs
        # has grown across migrations and every one must appear in this
        # list or PG rejects with ``cannot truncate a table referenced
        # in a foreign key constraint``:
        #
        # * ``documents.tenant_id`` — migration 0003 (G0.4-T1).
        # * ``graph_node.tenant_id`` — migration 0007 (G7 topology).
        # * ``graph_edge.tenant_id`` — migration 0007 (G7 topology).
        # * ``broadcast_override.tenant_id`` — migration 0008 (G6.3
        #   PII opt-in/opt-out controls).
        # * ``graph_node_history.tenant_id`` + FK to ``graph_node`` —
        #   migration 0012 (G9.3-T1 topology history).
        # * ``graph_edge_history.tenant_id`` + FK to ``graph_edge`` —
        #   migration 0012 (G9.3-T1 topology history).
        # * ``agent_definition.tenant_id`` — migration 0016 (G11.1-T2
        #   #809 agent-definition CRUD).
        #
        # ``audit_log`` has no FK to ``tenant`` (the soft column shape
        # from 0002) but stays in the list so the per-test reset is
        # atomic. Tables whose ``tenant_id`` is a soft column with no
        # FK (``targets``, ``endpoint_descriptor``, ``operation_group``)
        # don't need to be here. ``graph_edge`` also has a FK to
        # ``graph_node`` but listing both lets the statement stay
        # non-cascading regardless of FK order. The history tables
        # likewise carry FKs to their live counterparts; PG requires
        # them in the same TRUNCATE statement (or CASCADE).
        #
        # * ``agent_run.tenant_id`` — migration 0017 (G11.1-T6 #813) is a
        #   real ``REFERENCES tenant(id)`` FK, so ``agent_run`` must be
        #   truncated in the same statement as ``tenant`` or PG raises
        #   ``cannot truncate a table referenced in a foreign key
        #   constraint``.
        # * ``agent_principal.tenant_id`` — migration 0018 (G11.2-T1 #815)
        #   is a real ``REFERENCES tenant(id)`` FK; omitting it causes PG to
        #   reject the TRUNCATE with ``cannot truncate a table referenced in
        #   a foreign key constraint``.
        # * ``scheduled_trigger`` — migration 0020 (G11.3-T1 #822) carries
        #   real FKs to ``tenant(id)`` and ``agent_definition(id)``; same rule.
        # * ``event_outbox`` — migration 0026 (G11.3-T3 #824) carries a real
        #   FK to ``tenant(id)``; omitting it causes PG to reject the
        #   TRUNCATE with ``cannot truncate a table referenced in a foreign
        #   key constraint`` (the recurring fixture gotcha #1064 / #1065 hit).
        await conn.execute(
            text(
                "TRUNCATE TABLE approval_request, agent_permission, "
                "agent_principal, scheduled_trigger, event_outbox, "
                "agent_run, audit_log, "
                "documents, graph_edge, "
                "graph_edge_history, graph_node, graph_node_history, "
                "broadcast_override, agent_definition, tenant",
            ),
        )
        # Re-seed two pinned tenant rows so the integration suite
        # actually exercises the T1 ``tenant`` table (the issue body's
        # explicit "Setup fixture" requirement). The pinned UUIDs
        # match ``TENANT_A_ID`` / ``TENANT_B_ID`` in
        # :mod:`tests.integration.test_tenant_isolation`; keeping them
        # as string literals here avoids importing test-module symbols
        # into the conftest. Test 2's "unknown tenant_id" probe gains
        # meaning from this seed — a bogus UUID now contrasts against
        # two real tenant rows in the same DB rather than against an
        # empty table.
        await conn.execute(
            text(
                "INSERT INTO tenant (id, slug, name) VALUES "
                "('11111111-1111-1111-1111-111111111111', 'tenant-a', 'Tenant A'), "
                "('22222222-2222-2222-2222-222222222222', 'tenant-b', 'Tenant B')"
            )
        )
        await conn.commit()

    try:
        yield
    finally:
        await dispose_engine()
        reset_engine_for_testing()


@pytest.fixture
async def pg_engine_empty_tenant(
    integration_env: None,
    async_pg_url: str,
) -> AsyncIterator[None]:
    """Like :func:`pg_engine` but leaves the ``tenant`` table **empty**.

    The G0.8-T1 clean-room condition: a fresh v0.2 deploy whose
    ``tenant`` table has no rows. ``pg_engine`` re-seeds two pinned
    tenants so the isolation suite has rows to scope against; that
    re-seed is exactly what masks the FK wall this Task fixes. This
    fixture truncates the same chassis-table set in the same single
    non-cascading statement but **does not** re-insert any tenant —
    so the first authenticated write must rely on
    :func:`meho_backplane.tenancy.ensure_tenant` having seeded the row
    just-in-time, not on a fixture having done it out of band.
    """
    reset_engine_for_testing()
    eng = create_engine_for_url(async_pg_url, pool_size=5, pool_timeout=10.0)
    engine_module._engine = eng

    async with eng.connect() as conn:
        # Same single non-cascading TRUNCATE as ``pg_engine`` (see that
        # fixture for why every real ``REFERENCES tenant(id)`` table —
        # including ``agent_run`` from migration 0017 and
        # ``agent_principal`` from migration 0018 — must be listed
        # here). Deliberately no follow-up INSERT —
        # ``tenant`` stays empty, reproducing the clean-room deploy.
        await conn.execute(
            text(
                "TRUNCATE TABLE approval_request, agent_permission, "
                "agent_principal, scheduled_trigger, event_outbox, "
                "agent_run, audit_log, "
                "documents, graph_edge, "
                "graph_edge_history, graph_node, graph_node_history, "
                "broadcast_override, agent_definition, tenant",
            ),
        )
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
    # G0.4-T5 (#262) retrieve route; G0.4-T6 (#263) exercises it end-to-end.
    app.include_router(api_v1_retrieve_router)
    # MCP transport entrypoint + RFC 9728 protected-resource metadata —
    # mounted unconditionally so the T6 acceptance suite (G0.5-T6, #251)
    # can drive the full lifecycle (initialize → tools/list → tools/call
    # → resources/read) through the production auth chain. Routes that
    # consuming suites don't exercise are inert: the tenancy isolation
    # suite never POSTs to ``/mcp`` so its assertions are unaffected.
    app.include_router(mcp_router)
    app.include_router(well_known_router)
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


# The previous ``run_async`` helper (a thin wrapper around
# :func:`asyncio.run`) was deliberately removed. Each call would spawn
# a *fresh* event loop, but the asyncpg pool created in the
# :func:`pg_engine` async fixture is bound to the pytest-asyncio
# managed loop. Crossing loops on a pool checkout tripped
# SQLAlchemy's ``pool_pre_ping`` with
# ``RuntimeError: ... attached to a different loop`` in CI on PR #268.
# The cure is to keep PG-driven tests ``async def`` (the
# ``asyncio_mode = "auto"`` setting in ``backend/pyproject.toml`` makes
# that the default) and to ``await`` the helper coroutines directly.


# ---------------------------------------------------------------------------
# Keycloak testcontainer fixture (G11.2-T7 #1098)
# ---------------------------------------------------------------------------
#
# Closes the deferred live-IdP acceptance criterion of G11.2-T2 (#816):
# T2 shipped ``get_client_credentials_token`` with respx contract tests
# but the full JWT-validation chain has never been exercised against a
# real Keycloak realm. This fixture boots an upstream Keycloak image in
# dev mode with a pinned realm imported on startup; consuming tests
# fetch a token via the real ``client_credentials`` grant and validate
# it through :func:`meho_backplane.auth.jwt.verify_jwt_for_audience`.
#
# Image override: ``MEHO_TEST_KEYCLOAK_IMAGE``. Unlike the
# ``MEHO_TEST_PGVECTOR_IMAGE`` / ``MEHO_TEST_VAULT_IMAGE`` rows further
# up this file, the Keycloak fixture has **no docker.io default** —
# Keycloak images are ~600 MB and pulling them through the cluster's
# shared NAT egress reliably blows the Docker Hub anonymous-pull
# bucket. CI sets ``MEHO_TEST_KEYCLOAK_IMAGE`` to the Harbor mirror;
# local / agent-sandbox invocations without the env var skip cleanly
# (the acceptance criterion in #1098 names this skip-when-unset shape
# explicitly, and the consuming test asserts a non-skipped pass in CI
# rather than a vacuous skip).
#
# Realm shape: ``meho-integration`` with one confidential client
# ``agent:test-bot`` whose protocol mappers stamp ``aud`` +
# ``tenant_id`` + ``tenant_role`` + ``principal_kind`` onto every
# issued access token. The mappers are hardcoded-claim mappers (no
# backing user attribute) so the realm import is fully declarative
# and reproducible from the JSON alone. The realm file ships at
# ``backend/tests/integration/_fixtures/meho-integration-realm.json``;
# Keycloak's ``--import-realm`` requires the filename to follow the
# ``<realm-name>-realm.json`` convention, which is why the file is
# named that way rather than something more discoverable.
#
# The realm carries ``sslRequired: "none"``. Keycloak's default is
# ``external`` (HTTPS required for non-localhost requests), and a
# testcontainer's host-mapped port is reached through the daemon IP
# rather than localhost; without the override Keycloak rejects every
# token POST with 403 ``HTTPS required``. ``start-dev`` only sets
# ``sslRequired=none`` on realms it *creates* — imports inherit the
# default, so the explicit value in the JSON is load-bearing.

_KEYCLOAK_REALM: str = "meho-integration"
_KEYCLOAK_CLIENT_ID: str = "agent:test-bot"
# This secret is generated *into* the testcontainer realm import and
# only ever held in this module + the realm JSON. It is bound to a
# throwaway per-test-run Keycloak instance that never persists, is
# never reachable off the runner, and shares zero credentials with any
# production realm — same secrets-in-fixtures discipline as
# ``_DEV_ROOT_TOKEN`` in :mod:`tests.integration.test_connectors_vault_dev_e2e`.
_KEYCLOAK_CLIENT_SECRET: str = "test-secret-do-not-use-anywhere-else-g11-2-t7"
_KEYCLOAK_AUDIENCE: str = "meho-backplane-test"
_KEYCLOAK_TENANT_ID: str = "11111111-1111-1111-1111-111111111111"
_KEYCLOAK_TENANT_ROLE: str = "tenant_admin"
_KEYCLOAK_PRINCIPAL_KIND: str = "agent"
_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME: str = "test-admin"
# Same throwaway-credential rationale as ``_KEYCLOAK_CLIENT_SECRET``:
# only ever issued into the ephemeral container, never used to grant
# admin access to anything that outlives the test process.
_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD: str = "test-admin-pass-g11-2-t7"


@dataclass(frozen=True)
class KeycloakBootstrap:
    """Connection + claim-shape bundle for the Keycloak integration realm.

    Frozen so a test can stash one on the request state, log it, or
    forward it without fear of mutation, in the same spirit as
    :class:`~meho_backplane.auth.operator.Operator`.

    Fields:

    * ``base_url`` — the container's reachable HTTP root (no path),
      e.g. ``http://127.0.0.1:38421``. Useful for direct admin probes.
    * ``realm`` — the imported realm name (``meho-integration``).
    * ``issuer_url`` — ``{base_url}/realms/{realm}``. This is what
      :func:`meho_backplane.auth.jwt.verify_jwt_for_audience` checks
      against the JWT's ``iss`` claim, and what
      :func:`~meho_backplane.auth.agent_token.get_client_credentials_token`
      uses to derive the token endpoint.
    * ``client_id`` / ``client_secret`` — the confidential client the
      ``client_credentials`` grant authenticates as. The secret is
      pinned in the realm import so tests don't need to scrape it
      back via the admin API.
    * ``audience`` — the literal value the realm's audience mapper
      stamps onto every issued access token. Passed as the
      ``expected_audience`` argument to ``verify_jwt_for_audience``.
    * ``expected_tenant_id`` / ``expected_tenant_role`` /
      ``expected_principal_kind`` — the literal values the realm's
      hardcoded-claim mappers stamp; the integration test asserts the
      resulting :class:`Operator` carries these.
    """

    base_url: str
    realm: str
    issuer_url: str
    client_id: str
    client_secret: str
    audience: str
    expected_tenant_id: str
    expected_tenant_role: str
    expected_principal_kind: str


@pytest.fixture(scope="module")
def keycloak_bootstrap() -> Iterator[KeycloakBootstrap]:
    """Boot Keycloak with the integration realm imported, yield the bootstrap.

    Module scope amortises the ~10-second Keycloak startup across every
    test in the consuming module — the suite is small today (a single
    end-to-end test in :mod:`tests.integration.test_auth_keycloak_client_credentials`)
    but the fixture is the durable seam other auth integration tests
    (JWKS rotation, admin client, future agent-principal lifecycle)
    will attach to once they land. The cost of the container start
    dwarfs every single-request test so amortising is mandatory.

    Skip conditions, in priority order:

    1. ``MEHO_TEST_KEYCLOAK_IMAGE`` unset → skip with the explicit
       remediation. The image is required (no docker.io default) so
       CI is the only environment that runs the suite by default,
       matching the issue body's explicit "no fall back to a
       300-megabyte Docker Hub pull" framing.
    2. Docker socket not reachable in the sandbox → skip with the
       same reason as every other testcontainers-driven suite in this
       directory (uniform skip heuristic).
    3. Container failed to start (privileged denied, image pull
       rate-limit, cgroup refusal) → skip with the exception class
       name, same pattern :func:`vault_dev_addr` in
       :mod:`tests.integration.test_connectors_vault_dev_e2e` uses.

    Bootstrapping shape: a realm-import JSON committed under
    ``tests/integration/_fixtures/`` is bind-mounted read-only at
    ``/opt/keycloak/data/import/`` and Keycloak is launched with
    ``start-dev --import-realm``. Keycloak 26.x requires the file to
    be named ``<realm-name>-realm.json`` (the file's name is
    ``meho-integration-realm.json`` because of this convention).

    Bootstrap admin credentials are set via
    ``KC_BOOTSTRAP_ADMIN_USERNAME`` / ``KC_BOOTSTRAP_ADMIN_PASSWORD``
    (the 26.x replacement for ``KEYCLOAK_ADMIN`` /
    ``KEYCLOAK_ADMIN_PASSWORD``). The integration test does not use
    admin credentials — it authenticates as the imported
    confidential client — but Keycloak refuses to start without
    bootstrap admin creds on a fresh database.
    """
    image = os.environ.get("MEHO_TEST_KEYCLOAK_IMAGE")
    if not image:
        pytest.skip(
            "MEHO_TEST_KEYCLOAK_IMAGE not set; the live-Keycloak "
            "integration test runs in CI where the Harbor-proxied "
            "image is provisioned. To run locally, set the env var "
            "to a Keycloak 26.x image tag (e.g. quay.io/keycloak/keycloak:26.0)."
        )
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    # Same local-import discipline as the Postgres + Vault fixtures
    # above: testcontainers transitively imports the ``docker`` SDK
    # which probes the socket on import, so keeping the import inside
    # the fixture lets the module collect on a no-Docker sandbox.
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    realm_file = Path(__file__).parent / "_fixtures" / f"{_KEYCLOAK_REALM}-realm.json"
    if not realm_file.is_file():
        # Defensive: the realm-import JSON is committed alongside this
        # conftest; an absent file is a packaging error, not a test
        # environment one — surface a clear failure rather than letting
        # Keycloak silently start with no realms imported.
        pytest.fail(
            f"Keycloak realm-import fixture missing at {realm_file}; "
            "expected committed under tests/integration/_fixtures/."
        )

    container = (
        DockerContainer(image)
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", _KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME)
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", _KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD)
        .with_command("start-dev --import-realm")
        # ``ro`` mount: the realm file is read at startup and never
        # written back, so denying writes hardens against an accidental
        # in-container overwrite (and matches the principle the test
        # fixtures committed to: deterministic, reproducible inputs).
        .with_volume_mapping(
            str(realm_file.parent),
            "/opt/keycloak/data/import",
            "ro",
        )
        .with_exposed_ports(8080)
    )

    try:
        container.start()
    except Exception as exc:
        # Broad catch is intentional: testcontainers wraps the docker
        # SDK's varied failure modes (privileged denied, cgroup
        # refusal, registry rate-limit, manifest-not-found, daemon
        # socket missing) under a heterogeneous set of exceptions; the
        # only useful response is "skip with the class name and
        # message" so the suite stays diagnosable without enumerating
        # every transitive docker.errors subtype. Mirrors the same
        # pattern in :func:`vault_dev_addr` of
        # :mod:`tests.integration.test_connectors_vault_dev_e2e`.
        pytest.skip(f"Keycloak container failed to start ({type(exc).__name__}): {exc}")

    try:
        # Quarkus logs ``started in <N>.<N>s. Listening on: http://...:8080``
        # once the server is up and serving. Wait on the unambiguous
        # "Listening on" prefix — the realm-import line that precedes
        # it is itself a useful but slightly Keycloak-version-specific
        # signal, so anchor on the Quarkus boot line instead.
        wait_for_logs(container, "Listening on:", timeout=120)
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8080)
        base_url = f"http://{host}:{port}"
        bootstrap = KeycloakBootstrap(
            base_url=base_url,
            realm=_KEYCLOAK_REALM,
            issuer_url=f"{base_url}/realms/{_KEYCLOAK_REALM}",
            client_id=_KEYCLOAK_CLIENT_ID,
            client_secret=_KEYCLOAK_CLIENT_SECRET,
            audience=_KEYCLOAK_AUDIENCE,
            expected_tenant_id=_KEYCLOAK_TENANT_ID,
            expected_tenant_role=_KEYCLOAK_TENANT_ROLE,
            expected_principal_kind=_KEYCLOAK_PRINCIPAL_KIND,
        )
        yield bootstrap
    finally:
        container.stop()
