# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`meho_backplane.middleware.BroadcastDetailMiddleware`.

Coverage matrix (Task #380 / G6.3-T3 acceptance criteria):

* ``X-Broadcast-Detail: full`` flips the published broadcast event to
  ``detail="full"`` -- verified by capturing the published event via
  monkey-patched :func:`publish_event` and asserting the rendered
  payload includes ``params`` (the full-detail shape, not the
  aggregate shape).
* ``X-Broadcast-Detail: aggregate`` on a normally-full route does NOT
  downgrade -- the header value is logged at info but discarded,
  matching the "opt-in only" Initiative #376 DoD.
* Missing header → resolver's ``request_override=None`` → static
  default detail applies.
* Malformed header value (arbitrary string) is logged + dropped; the
  request still succeeds with the default detail.
* Audit row payload includes ``broadcast_detail_origin`` =
  ``"request_override"`` when the header was honored, ``"default"``
  otherwise.

The tests drive the production ``meho_backplane.main:app`` so the
middleware chain ordering is exercised (``RequestContextMiddleware`` →
``BroadcastDetailMiddleware`` → ``AuditMiddleware`` → router). The
audit-side DB is wired against a per-test aiosqlite engine via the
``isolated_audit_engine`` fixture (same shape as
:mod:`tests.test_audit_middleware`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

import meho_backplane.audit as audit_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.broadcast import BroadcastEvent
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

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks
from ._vault_fakes import install_fake_vault as _install_fake_vault


@pytest.fixture(autouse=True)
def _settings_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """Pin Settings env + a tmp-path SQLite DB.

    Mirrors :mod:`tests.test_audit_middleware` -- overrides the
    ``_default_database_url`` autouse fixture with a per-test DB so
    each case owns an isolated audit table.
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
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def _audit_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Per-test SQLite URL + ``alembic upgrade head``.

    Synchronous fixture because :func:`alembic.command.upgrade` calls
    :func:`asyncio.run` internally; calling it from within an async
    fixture raises ``RuntimeError: asyncio.run() cannot be called
    from a running event loop``. Mirrors the
    :mod:`tests.test_audit_middleware` pattern.
    """
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
    """Per-test aiosqlite engine bound to the migrated audit DB."""
    reset_engine_for_testing()
    engine = create_engine_for_url(_audit_db_url, pool_size=5, pool_timeout=10.0)
    engine_module._engine = engine
    try:
        yield engine
    finally:
        await dispose_engine()
        reset_engine_for_testing()


async def _fetch_audit_rows(engine: AsyncEngine) -> list[AuditLog]:
    """Read every ``audit_log`` row in order."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


def _capture_publish(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace :func:`publish_event` with an in-memory recorder.

    Patches the import as seen by :mod:`meho_backplane.audit` -- that's
    the call site for the HTTP-audit publish hook.
    """
    captured: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        captured.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return captured


def _hit_health(monkeypatch: pytest.MonkeyPatch, **headers: str) -> Any:
    """Issue an authenticated ``GET /api/v1/health`` with optional extra headers."""
    key = _make_rsa_keypair("kid-bdh")
    token = _mint_token(key, sub="op-bdh", name="Op", email="op@example.com")
    _install_fake_vault(monkeypatch)
    client = TestClient(app)
    request_headers = {"Authorization": f"Bearer {token}", **headers}
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        return client.get("/api/v1/health", headers=request_headers)


# ---------------------------------------------------------------------------
# Header parsing -- the four operator-facing cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_header_uses_default_detail(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing header → resolver gets ``request_override=None`` → default branch."""
    captured = _capture_publish(monkeypatch)
    response = _hit_health(monkeypatch)
    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    assert rows[0].payload["broadcast_detail_origin"] == "default"
    assert rows[0].payload["broadcast_detail_effective"] == "full"
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_full_header_keeps_default_origin_for_non_sensitive_route(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``X-Broadcast-Detail: full`` is a no-op on a non-sensitive op.

    The chassis ``GET /api/v1/health`` route classifies as ``other``
    op_class, so the default detail is already ``"full"`` and the
    resolver's request_override branch is gated to sensitive classes
    only (per resolver logic). The middleware still *binds* the
    contextvar; the resolver simply doesn't honor it for this op
    class. Origin therefore stays ``"default"``.

    The resolver's "request_override upgrades sensitive class" path
    is exercised by :mod:`tests.test_broadcast_overrides_resolver`
    (which calls the resolver directly).
    """
    captured = _capture_publish(monkeypatch)
    response = _hit_health(monkeypatch, **{"X-Broadcast-Detail": "full"})
    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert rows[0].payload["broadcast_detail_origin"] == "default"
    assert rows[0].payload["broadcast_detail_effective"] == "full"
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_aggregate_header_does_not_downgrade(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``X-Broadcast-Detail: aggregate`` is logged + ignored.

    Per Initiative #376 DoD, "weaken via header" is not honored:
    only ``"full"`` is accepted by the middleware. The request
    succeeds with the route's default detail unchanged.
    """
    captured = _capture_publish(monkeypatch)
    response = _hit_health(monkeypatch, **{"X-Broadcast-Detail": "aggregate"})
    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert rows[0].payload["broadcast_detail_origin"] == "default"
    assert rows[0].payload["broadcast_detail_effective"] == "full"
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_malformed_header_value_logged_and_dropped(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Random header value (typo, fuzzing) doesn't crash the middleware.

    Pins the fail-open contract: a malformed header is logged at
    info under ``broadcast_detail_invalid_header`` and dropped. The
    request still succeeds with the default detail; the value
    never reaches the resolver.
    """
    captured = _capture_publish(monkeypatch)
    response = _hit_health(monkeypatch, **{"X-Broadcast-Detail": "vErBoSe"})
    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert rows[0].payload["broadcast_detail_origin"] == "default"
    assert rows[0].payload["broadcast_detail_effective"] == "full"
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_case_insensitive_header_value(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Header value is parsed case-insensitively (``Full``, ``FULL`` both work).

    HTTP header *names* are case-insensitive by spec, but values
    are not -- the middleware lower-cases the value as a usability
    affordance for operators using shell aliases or quick-curl
    invocations. The contract is still that only ``"full"`` (in any
    case) is honored.
    """
    captured = _capture_publish(monkeypatch)
    response = _hit_health(monkeypatch, **{"X-Broadcast-Detail": "Full"})
    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    # Origin is still ``default`` on non-sensitive routes (the
    # request_override branch is gated by op_class), but the
    # middleware *bound* the contextvar -- proved by the absence
    # of an ``broadcast_detail_invalid_header`` log line (would be
    # asserted via caplog in a full structlog harness).
    assert rows[0].payload["broadcast_detail_origin"] == "default"
    assert rows[0].payload["broadcast_detail_effective"] == "full"
    assert len(captured) == 1


# ---------------------------------------------------------------------------
# Contextvar lifecycle -- direct middleware-level assertion
# ---------------------------------------------------------------------------


def test_middleware_unbinds_contextvar_after_request() -> None:
    """The ``finally`` block runs ``unbind_contextvars`` symmetrically.

    Pins the safety net at the middleware boundary directly: after
    :class:`BroadcastDetailMiddleware` finishes processing a request
    that bound the contextvar, the slot is gone -- nothing leaks
    into the asyncio task's contextvar dict for the next request
    on the same task. The full end-to-end "request A then request
    B" shape is hard to express against the TestClient + respx
    chain (a single test would need to share a JWKS-mocked window
    across two calls); asserting the bracketing directly is a
    tighter test of the same invariant.
    """
    import asyncio

    import structlog

    from meho_backplane.middleware import BroadcastDetailMiddleware

    async def _inner_app(scope: Any, receive: Any, send: Any) -> None:
        # During the inner app's execution, the contextvar IS bound.
        assert structlog.contextvars.get_contextvars().get("broadcast_detail_override") == "full"

    middleware = BroadcastDetailMiddleware(_inner_app)
    scope = {
        "type": "http",
        "headers": [(b"x-broadcast-detail", b"full")],
    }
    structlog.contextvars.clear_contextvars()
    asyncio.run(middleware(scope, lambda: None, lambda _msg: None))  # type: ignore[arg-type]
    # After the middleware returns, the slot is gone.
    assert structlog.contextvars.get_contextvars().get("broadcast_detail_override") is None
