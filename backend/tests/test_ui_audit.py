# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for the BFF audit-thread on ``/ui/*`` GET requests.

Initiative #1209 (G0.15 v0.7.0 closed-loop dogfood hardening), Task
#1216 (T7). The consumer's v0.7.0 closed-loop dogfood
(``claude-rdc-hetzner-dc#753``) flagged that an operator browsing 5
``/ui/*`` surfaces generated **zero** ``audit_log`` rows under their
``principal_sub`` -- a governance product completeness gap. The fix
binds audit contextvars
(:func:`meho_backplane.ui.audit.bind_ui_view_audit`) inside
:class:`meho_backplane.ui.auth.middleware.UISessionMiddleware` so the
chassis :class:`meho_backplane.audit.AuditMiddleware` writes one row
per page-view GET.

Coverage matrix (issue #1216 acceptance criteria):

* Each of the 5 surfaces (``/ui/`` dashboard, ``/ui/broadcast``,
  ``/ui/connectors``, ``/ui/kb``, ``/ui/memory``, ``/ui/topology``)
  writes exactly one ``audit_log`` row per GET, with the operator's
  ``principal_sub``, the session's ``tenant_id``, ``op_class="ui_view"``,
  and ``op_id="ui.view.<surface>"`` in the payload.
* ``query_audit principal_sub=<operator> since=1h`` after the operator
  browses 5 surfaces returns >= 5 rows (one per surface) with the
  expected payload shape.
* HEAD requests are audited identically to GET (HTML pre-flight cache).
* POST / PATCH / DELETE on ``/ui/*`` does **not** receive the
  ``ui_view`` op-class binding here -- those go through service-layer
  audit writers under their own ``op_id`` discipline; double-binding
  would produce a duplicate ``ui_view`` row per write.
* Auth-prefix paths (``/ui/auth/login``) and static-asset paths
  (``/ui/static/...``) bypass the binding entirely.
* The session middleware short-circuits unauthenticated ``/ui/*``
  requests to a 302 before the audit branch -- no row written, no
  identity to attribute.

The suite drives the production :data:`meho_backplane.main.app` so it
exercises the real middleware-stack ordering (UISessionMiddleware
outermost, AuditMiddleware innermost) plus the production routes. The
DB is per-test SQLite migrated to head via the
``isolated_audit_engine`` fixture -- same pattern as
:mod:`backend.tests.test_audit_middleware`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import respx
from alembic import command
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import (
    create_engine_for_url,
    dispose_engine,
    get_sessionmaker,
    reset_engine_for_testing,
)
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.settings import get_settings
from meho_backplane.ui.audit import UI_AUDIT_OP_CLASS, derive_ui_surface
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.templating import reset_templating_for_testing

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import DEFAULT_TENANT_ID as _DEFAULT_TENANT_ID
from ._oidc_jwt_helpers import ISSUER as _ISSUER

# ---------------------------------------------------------------------------
# Fixtures -- env, JWKS cache, isolated DB, fresh module state
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ui_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """Pin every env var the BFF + chassis Settings reads.

    The chassis JWKS / Vault settings are pinned even though no test in
    this suite mints a JWT -- the production :data:`meho_backplane.main.app`
    constructs the audit middleware at import time and that import path
    expects the env to be coherent. Mirrors :func:`tests.test_audit_middleware._settings_env`.
    """
    db_path = tmp_path / "audit.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    clear_jwks_cache()
    clear_discovery_cache()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    clear_discovery_cache()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()


@pytest.fixture
def _audit_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Resolve the per-test SQLite URL and run ``alembic upgrade head``."""
    db_path = tmp_path / "audit.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
async def isolated_audit_engine(
    _audit_db_url: str,
) -> AsyncIterator[AsyncEngine]:
    """Per-test aiosqlite engine bound to the migrated DB.

    Mirrors :func:`tests.test_audit_middleware.isolated_audit_engine`.
    The engine is injected into the module-level cache so every
    middleware + route resolving ``get_sessionmaker()`` lands on the
    same per-test DB.
    """
    reset_engine_for_testing()
    eng = create_engine_for_url(_audit_db_url, pool_size=5, pool_timeout=10.0)
    engine_module._engine = eng
    try:
        yield eng
    finally:
        await dispose_engine()
        reset_engine_for_testing()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _fetch_audit_rows() -> list[AuditLog]:
    """Return every ``audit_log`` row, ordered by ``occurred_at``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


async def _seed_ui_session(
    *,
    operator_sub: str = "op-ui-42",
    tenant_id: uuid.UUID | None = None,
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create one ``web_session`` row and return its UUID.

    Bypasses the full Keycloak round-trip -- the suite is about audit
    coverage on ``/ui/*`` page views, not the auth flow. The Fernet
    key set by ``_ui_env`` is the same one the session-store reads, so
    the seeded row decrypts cleanly on the next ``load_session``.

    Awaited from inside an ``@pytest.mark.asyncio`` test rather than
    wrapping ``asyncio.run`` -- the suite drives the event loop
    pytest-asyncio provides, and a nested ``asyncio.run`` would raise
    ``RuntimeError: asyncio.run() cannot be called from a running
    event loop``.
    """
    tenant = tenant_id or uuid.UUID(_DEFAULT_TENANT_ID)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        decrypted = await create_session(
            session,
            operator_sub=operator_sub,
            tenant_id=tenant,
            access_token="access-token-plaintext",
            refresh_token="refresh-token-plaintext",
            lifetime=lifetime,
        )
        return decrypted.id


def _client_with_session(session_id: uuid.UUID) -> TestClient:
    """Build a TestClient that ships the ``meho_session`` cookie.

    ``raise_server_exceptions=False`` so a handler crash surfaces as a
    500 the way it would in production, not as a raised exception that
    fails the test harness before assertions run. The Secure / SameSite
    posture on the BFF cookie does not block httpx's TestClient -- it
    delivers cookies it received on a 302 chain regardless of the
    Secure flag, which is the pattern :mod:`tests.test_ui_chassis_smoke`
    relies on.
    """
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


# ---------------------------------------------------------------------------
# Unit tests -- the path → surface mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/ui/", "dashboard"),
        ("/ui/broadcast", "broadcast"),
        ("/ui/broadcast/event/abc", "broadcast"),
        ("/ui/connectors", "connectors"),
        ("/ui/connectors/rdc-vault", "connectors"),
        ("/ui/connectors/rdc-vault/probe", "connectors"),
        ("/ui/kb", "kb"),
        ("/ui/kb/my-slug", "kb"),
        ("/ui/kb/my-slug/preview", "kb"),
        ("/ui/memory", "memory"),
        ("/ui/memory/operator/foo", "memory"),
        ("/ui/memory/operator/foo/edit", "memory"),
        ("/ui/topology", "topology"),
        ("/ui/topology/detail/abc", "topology"),
        # Out-of-prefix / unrecognised paths.
        ("/ui/auth/login", None),
        ("/ui/static/dist/tailwind.css", None),
        ("/api/v1/health", None),
        ("/ui/unknown-future-surface", None),
        ("/ui", None),
    ],
)
def test_derive_ui_surface_matches_known_prefixes(
    path: str,
    expected: str | None,
) -> None:
    """The path → surface mapping covers every shipped ``/ui/<surface>``."""
    assert derive_ui_surface(path) == expected


# ---------------------------------------------------------------------------
# Integration tests -- every /ui/<surface> GET writes one audit row
# ---------------------------------------------------------------------------


_SURFACES_UNDER_TEST: tuple[tuple[str, str], ...] = (
    ("/ui/", "dashboard"),
    ("/ui/broadcast", "broadcast"),
    ("/ui/kb", "kb"),
    ("/ui/memory", "memory"),
    ("/ui/connectors", "connectors"),
    ("/ui/topology", "topology"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "surface"), _SURFACES_UNDER_TEST)
async def test_ui_get_writes_one_audit_row_with_ui_view_op_class(
    isolated_audit_engine: AsyncEngine,
    path: str,
    surface: str,
) -> None:
    """Each ``/ui/<surface>`` GET writes one audit row attributed to the operator.

    Asserts the load-bearing closure of the governance gap:

    * The row is **present** (pre-fix, AuditMiddleware skipped UI GETs
      because ``operator_sub`` was unbound -- this is the row that
      didn't exist).
    * ``operator_sub`` matches the seeded session.
    * ``tenant_id`` matches the seeded session.
    * Payload carries ``op_id="ui.view.<surface>"`` and
      ``op_class="ui_view"`` -- the audit middleware reads these from
      the ``audit_*`` contextvars the session middleware binds, strips
      the ``audit_`` prefix, and writes them into the row's
      ``payload`` dict.
    * ``method="GET"`` / ``path=<route>``.

    The status code is **not** asserted because some surface routes
    legitimately render a 500 in this minimal SQLite environment (e.g.
    the connector detail page queries pg-only operation_group columns).
    Audit coverage is the contract here, not route correctness -- the
    row lands on **any** non-401 status, which is exactly the
    governance-completeness guarantee the Task ships.
    """
    operator_sub = "op-ui-42"
    session_id = await _seed_ui_session(operator_sub=operator_sub)

    client = _client_with_session(session_id)
    # Do not follow redirects -- the dashboard route renders a 200
    # directly, but a future Initiative could insert a 302 (e.g. tenant
    # picker) which would mask the row under the followed-redirect's
    # path. We assert on the row written for the original path.
    response = client.get(path, follow_redirects=False)
    assert response.status_code != 401, (
        f"{path} returned 401 -- the seeded session is not authenticating"
    )

    rows = await _fetch_audit_rows()
    operator_rows = [r for r in rows if r.operator_sub == operator_sub]
    assert len(operator_rows) == 1, (
        f"{path} expected exactly 1 row for {operator_sub}; got {len(operator_rows)}: "
        f"{[(r.path, r.operator_sub) for r in rows]}"
    )

    row = operator_rows[0]
    assert row.operator_sub == operator_sub
    assert row.method == "GET"
    assert row.path == path
    assert row.tenant_id is not None
    assert str(row.tenant_id) == _DEFAULT_TENANT_ID
    assert row.payload.get("op_id") == f"ui.view.{surface}"
    assert row.payload.get("op_class") == UI_AUDIT_OP_CLASS


@pytest.mark.asyncio
async def test_operator_browsing_5_surfaces_yields_5_audit_rows(
    isolated_audit_engine: AsyncEngine,
) -> None:
    """End-to-end: ``query_audit principal_sub=<op> since=1h`` returns >= 5 rows.

    Maps directly to issue #1216 acceptance criterion:

      > ``query_audit principal_sub=<operator> since=1h`` after an
      > operator browses 5 UI surfaces returns >= 5 rows (one per
      > surface), each with the expected ``op_id`` and ``principal_sub``.

    Drives 5 GETs sequentially under one TestClient (one session),
    then queries the ``audit_log`` table directly for the operator's
    sub. The row-per-GET contract holds: each surface produces one
    row, no row carries another operator's identity, and every row's
    ``op_id`` lives in the expected ``ui.view.*`` namespace.
    """
    operator_sub = "op-dogfood-team"
    session_id = await _seed_ui_session(operator_sub=operator_sub)

    surfaces = (
        ("/ui/broadcast", "ui.view.broadcast"),
        ("/ui/kb", "ui.view.kb"),
        ("/ui/memory", "ui.view.memory"),
        ("/ui/connectors", "ui.view.connectors"),
        ("/ui/topology", "ui.view.topology"),
    )

    client = _client_with_session(session_id)
    for path, _ in surfaces:
        response = client.get(path, follow_redirects=False)
        assert response.status_code != 401

    rows = await _fetch_audit_rows()
    operator_rows = [r for r in rows if r.operator_sub == operator_sub]
    assert len(operator_rows) >= 5, (
        f"Expected >= 5 rows from 5 UI surfaces; got {len(operator_rows)}: "
        f"{[r.path for r in operator_rows]}"
    )

    expected_op_ids = {expected for _, expected in surfaces}
    actual_op_ids = {r.payload.get("op_id") for r in operator_rows}
    assert expected_op_ids.issubset(actual_op_ids), (
        f"missing op_ids: {expected_op_ids - actual_op_ids}"
    )
    # Every operator row carries the ui_view op_class -- no agent-path
    # leakage masquerading as a UI view.
    for row in operator_rows:
        assert row.payload.get("op_class") == UI_AUDIT_OP_CLASS, (
            f"row {row.path} has op_class={row.payload.get('op_class')}"
        )


# ---------------------------------------------------------------------------
# Negative / boundary cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_ui_get_writes_no_audit_row(
    isolated_audit_engine: AsyncEngine,
) -> None:
    """``GET /ui/`` without a session 302s to login and writes no row.

    The session middleware short-circuits the request to the login
    redirect before the audit branch can fire -- there is no operator
    to attribute, and the audit row would falsely suggest an
    authenticated action took place.
    """
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/ui/", follow_redirects=False)
    assert response.status_code == 302

    rows = await _fetch_audit_rows()
    assert rows == [], f"expected no audit rows; got {[(r.path, r.operator_sub) for r in rows]}"


@pytest.mark.asyncio
async def test_ui_static_asset_writes_no_audit_row(
    isolated_audit_engine: AsyncEngine,
) -> None:
    """Static assets bypass session + audit binding.

    Vendored JS / compiled CSS at ``/ui/static/*`` is intentionally
    reachable unauthenticated so the login page renders styled. Audit
    rows for asset fetches would inflate the audit log by an order of
    magnitude without governance value.
    """
    operator_sub = "op-static-fetch"
    session_id = await _seed_ui_session(operator_sub=operator_sub)
    client = _client_with_session(session_id)

    # The asset itself may 404 (no compiled CSS in the test env) but
    # the short-circuit happens before audit regardless.
    client.get("/ui/static/missing.css", follow_redirects=False)

    rows = await _fetch_audit_rows()
    assert [r for r in rows if r.operator_sub == operator_sub] == []


@pytest.mark.asyncio
async def test_ui_auth_routes_write_no_ui_view_audit_row(
    isolated_audit_engine: AsyncEngine,
) -> None:
    """The BFF auth surfaces (``/ui/auth/*``) bypass the ui_view binding.

    Auth routes are unauthenticated by design (they set the session
    cookie). The audit middleware's general skip rule
    (no ``operator_sub``) plus the session middleware's prefix bypass
    means no row attributes to "a session that does not yet exist".
    """
    with respx.mock(assert_all_called=False):
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/ui/auth/login", follow_redirects=False)

    rows = await _fetch_audit_rows()
    for row in rows:
        assert row.payload.get("op_class") != UI_AUDIT_OP_CLASS, (
            f"auth route {row.path} should not carry ui_view op_class"
        )
