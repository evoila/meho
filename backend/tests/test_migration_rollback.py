# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Forward-compat regression test (testcontainers).

Goal #11's Definition-of-Done bullet 3 promises that ``helm rollback``
works without DB intervention. That guarantee depends on **two**
disciplines:

* Migration-side — every ``upgrade()`` is purely additive (enforced
  in source by ``scripts/ci/check_migration_compat.py``).
* Code-side — the running backplane image must tolerate a schema
  *ahead* of it (the situation a rollback lands in: image reverted to
  revision N, schema still at revision N+1).

This module owns the unit-test-level proof of the second discipline.
The cluster-level proof — exercising real ``helm rollback`` against a
running deployment — is Goal #11's G2.8-T3, intentionally out of
scope here. Splitting the two layers means a regression in the code's
forward-compat property fails fast in CI rather than waiting on the
expensive end-to-end deploy gate.

What the test exercises
-----------------------

#. Spin up ``pgvector/pgvector:pg16`` via ``testcontainers``
   (image overridable via ``MEHO_TEST_PGVECTOR_IMAGE``).
#. Run ``alembic upgrade head`` against it. The schema is now at
   revision N (the audit-log table from
   ``0001_create_audit_log.py``).
#. Apply a synthetic *additive* migration that adds two columns to
   ``audit_log`` — ``future_field text DEFAULT 'reserved_for_v0.2'``
   and ``future_jsonb_field jsonb DEFAULT '{}'::jsonb``. The schema is
   now at revision N+1.
#. Make an authenticated ``GET /api/v1/health`` request through the
   FastAPI ``TestClient`` driving the production
   :data:`meho_backplane.main.app` (revision-N code). The audit
   middleware writes one row to ``audit_log``.
#. Read the row back. Assert (a) the response was 200, (b) the audit
   row exists, (c) revision-N's ORM-mapped fields landed correctly,
   and (d) — load-bearing — the synthetic N+1 columns hold their
   PostgreSQL-side defaults, which proves the revision-N code did
   *not* write them. The negative assertion is the load-bearing
   forward-compat property; without it, the test would pass
   vacuously even if the code reached for ``future_field``.

The synthetic migration lives at
``tests/fixtures/synthetic_n_plus_1.py`` rather than under
``backend/alembic/versions/`` precisely so the production migration
sequence and the CI guard's path filter
(``backend/alembic/versions/**``) remain undisturbed.

Skipping in sandbox
-------------------

testcontainers needs Docker. The ``_docker_socket_present()`` heuristic
mirrors the one used in :class:`tests.test_db_engine.TestPostgresIntegration`
and skips the test on agent sandboxes that have no Docker; CI provisions
Docker via the runner pool, so the test runs there.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from meho_backplane.auth import vault as vault_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import (
    create_engine_for_url,
    dispose_engine,
    reset_engine_for_testing,
)
from meho_backplane.db.migrations import alembic_config
from meho_backplane.main import app
from meho_backplane.settings import get_settings
from tests.fixtures.synthetic_n_plus_1 import (
    FUTURE_JSONB_FIELD_DEFAULT,
    FUTURE_TEXT_FIELD_DEFAULT,
    SYNTHETIC_N_PLUS_1_COLUMNS,
    apply_synthetic_n_plus_1_migration,
)

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Docker-availability skip — same shape as test_db_engine.TestPostgresIntegration
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    """Heuristic: Docker is usable if the unix socket is present.

    Lifted from :mod:`tests.test_db_engine` so the skip condition
    stays consistent across the testcontainers-PG suites — agent
    sandboxes (no Docker) skip; CI runners (Docker provisioned) run.
    """
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_DOCKER_AVAILABLE: bool = _docker_socket_present()
_SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


class _FakeJWTAuth:
    """Minimal hvac jwt-auth shim — replays
    :class:`tests.test_audit_middleware._FakeJWTAuth` without the
    dataclass machinery (the test only ever needs ``jwt_login``)."""

    def __init__(self) -> None:
        self.issued_token: str = "fake-vault-token"
        self.parent: _FakeClient | None = None

    def jwt_login(
        self,
        role: str,
        jwt: str,
        path: str | None = None,
    ) -> dict[str, Any]:
        if self.parent is not None:
            self.parent.token = self.issued_token
        return {"auth": {"client_token": self.issued_token}}


class _FakeTokenAuth:
    def revoke_self(self, mount_point: str = "token") -> None:
        return None


class _FakeAuth:
    def __init__(self) -> None:
        self.jwt = _FakeJWTAuth()
        self.token = _FakeTokenAuth()


class _FakeKVv2:
    def __init__(self) -> None:
        self.secret: dict[str, Any] = {"username": "demo"}
        self.version: int = 11

    def read_secret_version(self, path: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "data": {
                "data": self.secret,
                "metadata": {"version": self.version, "path": path},
            }
        }


class _FakeKV:
    def __init__(self) -> None:
        self.v2 = _FakeKVv2()


class _FakeSecrets:
    def __init__(self) -> None:
        self.kv = _FakeKV()


class _FakeSysBackend:
    def read_health_status(self, *, method: str = "HEAD", **_kwargs: Any) -> Any:
        return None


class _FakeClient:
    def __init__(self) -> None:
        self.url: str = "https://vault.test"
        self.timeout: float = 5.0
        self.namespace: str | None = None
        self.token: str | None = None
        self.auth = _FakeAuth()
        self.sys = _FakeSysBackend()
        self.secrets = _FakeSecrets()
        self.auth.jwt.parent = self


def _install_fake_vault(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    """Replace ``meho_backplane.auth.vault._build_client`` with a fake.

    The forward-compat test does not exercise the Vault federation
    chain — it only needs the ``/api/v1/health`` route to reach the
    audit-write middleware with a valid operator binding. The fake
    keeps the route's Vault-status branch on the success path so the
    response is 200 rather than 502/500.
    """
    fake = _FakeClient()

    def _fake_build_client(_settings: Any, *, token: str | None = None) -> _FakeClient:
        fake.token = token
        return fake

    monkeypatch.setattr(vault_module, "_build_client", _fake_build_client)
    return fake


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _async_url_from(sync_url: str) -> str:
    """Translate a testcontainers-issued sync URL to the asyncpg URL.

    ``PostgresContainer.get_connection_url`` returns
    ``postgresql+psycopg2://...`` by default. The backplane's engine
    factory (and Alembic's env.py) target asyncpg; ADR 0004 pins the
    driver. This helper handles both the default psycopg2 prefix and
    the bare ``postgresql://`` shape that older testcontainers
    versions emitted.
    """
    return sync_url.replace(
        "postgresql+psycopg2://",
        "postgresql+asyncpg://",
    ).replace(
        "postgresql://",
        "postgresql+asyncpg://",
    )


@pytest.fixture
def env_overrides(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires for module construction.

    The autouse ``_default_database_url`` fixture in :mod:`tests.conftest`
    points ``DATABASE_URL`` at a per-test SQLite tmp file *and* runs
    ``alembic upgrade head`` against it. This fixture deliberately
    overrides ``DATABASE_URL`` later (after the testcontainers PG
    starts up) so the audit middleware sees the PG schema instead.
    The conftest-time SQLite migration is harmless extra work — the
    PG migration replaces it before any request fires.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    clear_jwks_cache()
    yield
    clear_jwks_cache()


async def _fetch_audit_state(async_url: str) -> dict[str, Any]:
    """Read back the single audit row plus the synthetic columns.

    Issued through a dedicated short-lived :class:`AsyncEngine` so the
    query is independent of the engine the audit middleware just
    used (which the test must dispose-then-rebuild between phases).
    Selecting the synthetic columns explicitly is what proves the
    forward-compat property: if the revision-N code wrote anything
    to ``future_field`` / ``future_jsonb_field``, the row would carry
    those values; otherwise the columns hold their PostgreSQL-side
    defaults. Either outcome is observable here.

    Returns the row as a dict so the caller can assert on each
    field by name; using positional indices into a tuple was the
    earlier shape and made assertion failures harder to read.
    """
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT "
                    "operator_sub, method, path, status_code, "
                    "future_field, future_jsonb_field "
                    "FROM audit_log "
                    "ORDER BY occurred_at DESC "
                    "LIMIT 1"
                )
            )
            row = result.first()
            row_count_result = await conn.execute(text("SELECT COUNT(*) FROM audit_log"))
            count = row_count_result.scalar_one()
    finally:
        await engine.dispose()

    if row is None:
        return {"count": count, "row": None}
    return {
        "count": count,
        "row": {
            "operator_sub": row[0],
            "method": row[1],
            "path": row[2],
            "status_code": row[3],
            "future_field": row[4],
            "future_jsonb_field": row[5],
        },
    }


# ---------------------------------------------------------------------------
# The forward-compat regression test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_SKIP_REASON)
class TestForwardCompatRollback:
    """Backplane image at revision N runs cleanly against schema at revision N+1.

    The test class form mirrors :class:`tests.test_db_engine.TestPostgresIntegration`
    so the skip annotation applies once at the class level and the
    intent — "this class is the testcontainers-PG slice; skipped
    when Docker is absent" — is visible on a single line.
    """

    def test_n_image_runs_cleanly_against_n_plus_1_schema(
        self,
        env_overrides: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Full forward-compat round-trip — see module docstring.

        The test is **synchronous** for the same reason every other
        ``alembic upgrade head`` driving test in this suite is sync:
        ``alembic.command.upgrade`` invokes :func:`asyncio.run`
        internally via the env.py async cookbook, and ``asyncio.run``
        cannot be re-entered from a running loop. Decorating with
        ``@pytest.mark.asyncio`` would crash. Async work that the
        test needs to do directly (the audit-row read-back) is
        wrapped in its own ``asyncio.run`` boundary, mirroring how
        Alembic itself does it.
        """
        from testcontainers.postgres import PostgresContainer

        operator_sub = "op-rollback"
        key = _make_rsa_keypair("kid-rollback")
        token = _mint_token(
            key,
            sub=operator_sub,
            name="Forward Compat",
            email="fc@example.com",
        )
        _install_fake_vault(monkeypatch)

        # pgvector/pgvector:pg16: after G0.4-T1 (#258) migration 0003 runs
        # ``CREATE EXTENSION IF NOT EXISTS vector`` as part of ``upgrade head``,
        # so the testcontainers image must have pgvector pre-installed.
        # ``postgres:16-alpine`` (the v0.1-chassis choice) lacks the .control
        # file and fails fast; the pgvector image is a drop-in replacement.
        # See the matching swap in tests/test_db_engine.py. Image name is
        # env-overridable via ``MEHO_TEST_PGVECTOR_IMAGE`` so a registry-
        # mirror swap (GHCR cache, internal Harbor) does not require a
        # code change — same knob the integration conftest honours.
        image = os.environ.get("MEHO_TEST_PGVECTOR_IMAGE", "pgvector/pgvector:pg16")
        with PostgresContainer(image) as pg:
            sync_url = pg.get_connection_url()
            async_url = _async_url_from(sync_url)

            # Step 1 — migrate the PG schema to revision N (current head).
            # ``DATABASE_URL`` must be set before ``command.upgrade`` so the
            # alembic env.py picks up the test container's URL rather than
            # the conftest-installed SQLite default.
            monkeypatch.setenv("DATABASE_URL", async_url)
            get_settings.cache_clear()
            reset_engine_for_testing()

            cfg = alembic_config()
            cfg.set_main_option("sqlalchemy.url", async_url)
            command.upgrade(cfg, "head")

            # Step 2 — apply the synthetic N+1 additive migration.
            # The helper drives its own ``asyncio.run`` so the outer
            # test stays synchronous.
            apply_synthetic_n_plus_1_migration(async_url)

            # Step 3 — wire the production engine cache at the PG URL
            # so the audit middleware writes through the test container,
            # not through the conftest-installed SQLite engine that the
            # autouse fixture cached. The cache reset *plus* explicit
            # injection is what makes this safe to interleave with the
            # autouse fixture's own engine teardown.
            reset_engine_for_testing()
            engine_module._engine = create_engine_for_url(async_url, pool_size=5, pool_timeout=10.0)

            try:
                # Step 4 — drive a real authenticated request through
                # the production app. The audit middleware writes to
                # ``audit_log`` against the N+1 schema; the revision-N
                # code only knows the original column set.
                client = TestClient(app)
                with respx.mock as mock_router:
                    _mock_discovery_and_jwks(mock_router, _public_jwks(key))
                    response = client.get(
                        "/api/v1/health",
                        headers={"Authorization": f"Bearer {token}"},
                    )

                # Assertion (a) — backplane handled the request cleanly.
                # No 5xx is the explicit forward-compat acceptance
                # criterion; the schema being ahead must not poison
                # the request hot path.
                assert response.status_code == 200, (
                    "revision-N image must serve a 200 against revision-N+1 schema; "
                    f"got {response.status_code}: {response.text!r}"
                )

                # Assertion (b) + (c) — the audit row landed with the
                # revision-N column set populated correctly.
                state = asyncio.run(_fetch_audit_state(async_url))
                assert state["count"] == 1, f"expected exactly one audit row, got {state['count']}"
                row = state["row"]
                assert row is not None
                assert row["operator_sub"] == operator_sub
                assert row["method"] == "GET"
                assert row["path"] == "/api/v1/health"
                assert row["status_code"] == 200

                # Assertion (d) — the load-bearing negative test. The
                # synthetic N+1 columns were added with PG-side
                # defaults; if the revision-N code had written to
                # them (e.g. via a ``SELECT *``-shaped reflection or
                # an explicit column list that included future
                # fields), the values would differ. Asserting the
                # defaults landed verbatim proves the revision-N
                # ORM never mentioned the new columns on insert.
                assert row["future_field"] == FUTURE_TEXT_FIELD_DEFAULT, (
                    "future_field on the audit row must hold the PG-side default; "
                    "any other value would mean the revision-N code wrote to a "
                    "column it should not know about, falsifying the forward-compat "
                    f"property. Saw: {row['future_field']!r}"
                )
                assert row["future_jsonb_field"] == FUTURE_JSONB_FIELD_DEFAULT, (
                    "future_jsonb_field must hold the PG-side jsonb default; "
                    f"saw {row['future_jsonb_field']!r}"
                )

                # Sanity — the columns we asserted on cover every
                # synthetic column the migration added. Drift between
                # the migration's column list and the test's
                # assertions would silently shrink coverage; the
                # explicit subset check is the lock against that.
                assert set(SYNTHETIC_N_PLUS_1_COLUMNS) == {
                    "future_field",
                    "future_jsonb_field",
                }
            finally:
                # Tear down the engine cache before the container goes
                # away — leaving an asyncpg pool pointing at a stopped
                # container would leak warnings on event-loop shutdown.
                asyncio.run(dispose_engine())
                reset_engine_for_testing()


# ---------------------------------------------------------------------------
# G7.1-T5 #317 — seed migration 0018 PG-side idempotency replay
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_SKIP_REASON)
class TestSeedRdcInternalConventionsPgIdempotency:
    """Migration ``0018``'s upsert + ON CONFLICT shape is idempotent on real PG.

    The always-on suite in :mod:`tests.test_alembic_seed_rdc_conventions`
    proves the contract against SQLite (the dialect the per-test schema
    template uses). PG has its own conflict-resolution semantics --
    specifically the spec-conformant ``ON CONFLICT DO UPDATE`` /
    ``DO NOTHING`` arbiter precedence -- so the same shape must replay
    against an actual PG to lock in the cross-dialect guarantee.
    Without this test the migration's "safe to re-run" promise rests
    only on what SQLite tolerates.

    Mirrors the testcontainers + ``alembic upgrade`` discipline
    :class:`TestForwardCompatRollback` follows: spin up a PG container,
    upgrade to ``head``, stamp back to ``0017``, upgrade to ``0018``
    again, assert row counts are unchanged.
    """

    def test_seed_replay_against_pg_leaves_row_counts_unchanged(
        self,
        env_overrides: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stamp 0017 + upgrade 0018 twice on real PG: tenant + 8 conventions, no dupes.

        Test shape:

        1. Spin up ``pgvector/pgvector:pg16`` and ``alembic upgrade
           head`` -- this is the migration's first pass.
        2. Read the post-first-pass counts:
           ``tenant WHERE slug = 'rdc-internal'`` (must be 1), the
           seeded ``tenant_conventions`` (must be 8), and the seeded
           ``tenant_convention_history`` rows (must be 8 -- one
           CREATE-shape row per convention).
        3. Stamp back to ``0017`` (no data change), then ``upgrade
           0018`` again -- this is the replay.
        4. Re-read the same counts and assert they are unchanged.
           A failing arbiter or a forgotten ``ON CONFLICT`` clause
           would surface here as either an :class:`IntegrityError` on
           the re-insert or as duplicated rows.

        The test is synchronous for the same reason
        :meth:`TestForwardCompatRollback.test_n_image_runs_cleanly_against_n_plus_1_schema`
        is sync: ``alembic.command.upgrade`` invokes ``asyncio.run``
        internally via the env.py async cookbook, so a
        :func:`pytest.mark.asyncio` decoration would crash on re-entry.
        Async work (the row-count read-backs) is wrapped in its own
        ``asyncio.run`` boundary, the same idiom Alembic itself uses.
        """
        from testcontainers.postgres import PostgresContainer

        image = os.environ.get("MEHO_TEST_PGVECTOR_IMAGE", "pgvector/pgvector:pg16")
        with PostgresContainer(image) as pg:
            sync_url = pg.get_connection_url()
            async_url = _async_url_from(sync_url)

            # Step 1 -- first ``upgrade 0018``. The seed migration
            # runs as the last step of the chain.
            #
            # We intentionally stop at ``0018`` (not ``head``) because
            # G0.13-T7 (#1137) shipped migration ``0028`` on top of
            # ``0018`` that cleans up the rdc-internal seed and
            # replaces it with a generic ``default`` tenant for OSS
            # commercialization-readiness. The original 0018-only
            # idempotency contract this test pins predates that
            # supersede; head-state idempotency for 0028 is covered
            # by :class:`TestSupersedeDefaultConventionsPgIdempotency`
            # below.
            monkeypatch.setenv("DATABASE_URL", async_url)
            get_settings.cache_clear()
            reset_engine_for_testing()

            cfg = alembic_config()
            cfg.set_main_option("sqlalchemy.url", async_url)
            command.upgrade(cfg, "0018")

            first = asyncio.run(_fetch_seed_state(async_url))
            assert first["tenant_count"] == 1, (
                "first upgrade must land exactly one rdc-internal tenant row; "
                f"got {first['tenant_count']}"
            )
            assert first["convention_count"] == 8, (
                f"first upgrade must land 8 seeded conventions; got {first['convention_count']}"
            )
            assert first["history_count"] == 8, (
                "first upgrade must land one CREATE-shape history row per "
                f"seeded convention (8 total); got {first['history_count']}"
            )

            # Step 2 -- stamp back to 0017 so the seed migration's
            # data path replays (a plain ``upgrade 0018`` against a
            # DB already at 0018 would be a no-op via Alembic's
            # revision gate; stamping rewinds the revision pointer
            # without touching data, then ``upgrade 0018`` re-runs
            # the migration's actual ``upgrade()`` body).
            command.stamp(cfg, "0017")
            command.upgrade(cfg, "0018")

            second = asyncio.run(_fetch_seed_state(async_url))
            assert second["tenant_count"] == first["tenant_count"], (
                "re-running 0018 must not duplicate the rdc-internal tenant -- "
                "the ON CONFLICT (slug) DO UPDATE shape filters the second "
                f"insert. Saw {first['tenant_count']} → {second['tenant_count']}."
            )
            assert second["convention_count"] == first["convention_count"], (
                "re-running 0018 must not duplicate seeded conventions -- "
                "the ON CONFLICT (tenant_id, slug) DO NOTHING shape filters "
                f"the second pass. Saw {first['convention_count']} → "
                f"{second['convention_count']}."
            )
            assert second["history_count"] == first["history_count"], (
                "re-running 0018 must not duplicate history rows -- the "
                "RETURNING-id gate in upgrade() skips the history write "
                "when the convention insert was a no-op. Saw "
                f"{first['history_count']} → {second['history_count']}."
            )

            asyncio.run(dispose_engine())
            reset_engine_for_testing()


# ---------------------------------------------------------------------------
# G0.13-T7 #1137 — supersede migration 0028 PG-side idempotency replay
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_SKIP_REASON)
class TestSupersedeDefaultConventionsPgIdempotency:
    """Migration ``0028``'s cleanup + seed shape is idempotent on real PG.

    Twin to :class:`TestSeedRdcInternalConventionsPgIdempotency` -- the
    SQLite-side coverage lives in
    :mod:`tests.test_alembic_seed_0028_supersede`; this class locks in
    the cross-dialect guarantee on real PG.

    Asserts:

    * ``upgrade head`` (one shot) reaches the post-0028 state: no
      rdc-internal seeded rows survive, 2 default conventions + 2
      history rows are in place.
    * Stamp 0027 + upgrade 0028 again is a no-op (the cleanup matches
      zero rows on the replay, the seed's ON CONFLICT DO NOTHING shape
      filters duplicate inserts).
    """

    def test_supersede_replay_against_pg_leaves_row_counts_unchanged(
        self,
        env_overrides: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Upgrade head then stamp 0027 + upgrade 0028 again: counts stable."""
        from testcontainers.postgres import PostgresContainer

        image = os.environ.get("MEHO_TEST_PGVECTOR_IMAGE", "pgvector/pgvector:pg16")
        with PostgresContainer(image) as pg:
            sync_url = pg.get_connection_url()
            async_url = _async_url_from(sync_url)

            monkeypatch.setenv("DATABASE_URL", async_url)
            get_settings.cache_clear()
            reset_engine_for_testing()

            cfg = alembic_config()
            cfg.set_main_option("sqlalchemy.url", async_url)
            command.upgrade(cfg, "head")

            first = asyncio.run(_fetch_default_seed_state(async_url))
            # Post-0028 state: rdc-internal seeded rows cleaned, default
            # tenant + 2 conventions + 2 history rows in place.
            assert first["legacy_convention_count"] == 0, (
                "0028 must remove every convention 0018 authored; "
                f"got {first['legacy_convention_count']} survivors"
            )
            assert first["legacy_history_count"] == 0, (
                "0028 must remove every seed-authored history row 0018 wrote; "
                f"got {first['legacy_history_count']} survivors"
            )
            assert first["default_tenant_count"] == 1, (
                "0028 must seed exactly one default tenant row; "
                f"got {first['default_tenant_count']}"
            )
            assert first["default_convention_count"] == 2, (
                "0028 must seed 2 illustrative default conventions; "
                f"got {first['default_convention_count']}"
            )
            assert first["default_history_count"] == 2, (
                "0028 must land one CREATE history row per seeded default "
                f"convention; got {first['default_history_count']}"
            )

            # Replay 0028.
            command.stamp(cfg, "0027")
            command.upgrade(cfg, "0028")

            second = asyncio.run(_fetch_default_seed_state(async_url))
            assert second == first, (
                f"re-running 0028 must be a complete no-op on PG -- saw {first} -> {second}"
            )

            asyncio.run(dispose_engine())
            reset_engine_for_testing()


async def _fetch_default_seed_state(async_url: str) -> dict[str, int]:
    """Read post-0028 seed state counts back through a short-lived engine."""
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as conn:
            legacy_convention_count = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM tenant_conventions "
                        "WHERE created_by_sub = 'migration:seed-rdc-conventions'",
                    ),
                )
            ).scalar_one()
            legacy_history_count = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM tenant_convention_history "
                        "WHERE actor_sub = 'migration:seed-rdc-conventions'",
                    ),
                )
            ).scalar_one()
            default_tenant_count = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM tenant WHERE slug = 'default'"),
                )
            ).scalar_one()
            default_convention_count = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM tenant_conventions "
                        "WHERE created_by_sub = 'migration:seed-default-conventions'",
                    ),
                )
            ).scalar_one()
            default_history_count = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM tenant_convention_history "
                        "WHERE actor_sub = 'migration:seed-default-conventions'",
                    ),
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    return {
        "legacy_convention_count": int(legacy_convention_count),
        "legacy_history_count": int(legacy_history_count),
        "default_tenant_count": int(default_tenant_count),
        "default_convention_count": int(default_convention_count),
        "default_history_count": int(default_history_count),
    }


async def _fetch_seed_state(async_url: str) -> dict[str, int]:
    """Read the seeded row counts back through a short-lived engine.

    Used by :class:`TestSeedRdcInternalConventionsPgIdempotency` -- a
    fresh engine per call so the read is independent of any pool the
    test may have already disposed.
    """
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as conn:
            tenant_count = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM tenant WHERE slug = 'rdc-internal'"),
                )
            ).scalar_one()
            convention_count = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM tenant_conventions "
                        "WHERE created_by_sub = 'migration:seed-rdc-conventions'",
                    ),
                )
            ).scalar_one()
            history_count = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM tenant_convention_history "
                        "WHERE actor_sub = 'migration:seed-rdc-conventions'",
                    ),
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    return {
        "tenant_count": int(tenant_count),
        "convention_count": int(convention_count),
        "history_count": int(history_count),
    }


# ---------------------------------------------------------------------------
# Always-on smoke — proves the synthetic-migration helper imports cleanly
# even on no-Docker sandboxes, so a typo / syntax error surfaces fast in CI
# without the full PG suite running.
# ---------------------------------------------------------------------------


def test_synthetic_migration_helper_exposes_documented_constants() -> None:
    """Module-level smoke: the fixture's public surface stays stable.

    The test itself is cheap — no Docker, no PG, no event loop. It
    catches the failure mode "someone renamed a constant in
    ``synthetic_n_plus_1.py`` and the testcontainers test fails to
    import on the runner that *does* have Docker", which would
    otherwise only surface in CI at the bottom of a 5-minute matrix.
    """
    assert FUTURE_TEXT_FIELD_DEFAULT == "reserved_for_v0.2"
    assert FUTURE_JSONB_FIELD_DEFAULT == {}
    assert tuple(SYNTHETIC_N_PLUS_1_COLUMNS) == (
        "future_field",
        "future_jsonb_field",
    )
    # Helper must be importable and callable; we don't invoke it (no
    # PG to point at), but referencing the symbol catches a removed
    # export.
    assert callable(apply_synthetic_n_plus_1_migration)


def test_docker_skip_reason_explains_ci_path() -> None:
    """Soft contract: the skip message points at CI.

    A future agent debugging "why is this test not running locally"
    needs the breadcrumb. Asserting on the skip-reason text keeps
    the breadcrumb readable.
    """
    assert "CI" in _SKIP_REASON
    assert "Docker" in _SKIP_REASON
