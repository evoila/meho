# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`meho_backplane.audit.AuditMiddleware`.

Coverage matrix (Task #28 acceptance criteria):

* **Happy path** — a ``GET /api/v1/health`` call with a valid mock
  JWT produces exactly one new ``audit_log`` row whose
  ``operator_sub`` matches the JWT's ``sub``, ``method=GET``,
  ``path=/api/v1/health``, ``status_code=200``, and ``request_id``
  matches the response ``X-Request-Id`` header.
* **Skip rule — unauthenticated** — public surfaces (``/healthz``)
  produce no row and never invoke the session factory.
* **Skip rule — 401** — protected route hit without a Bearer token
  returns 401 and produces no row (no operator to attribute).
* **Fail-closed** — when the session factory raises mid-request, the
  response converts to 500 ``{"detail": "audit_write_failed"}`` and
  the buffered handler response is discarded.

The tests drive the production ``meho_backplane.main:app`` so they
exercise the real middleware-stack ordering — ``RequestContextMiddleware``
outermost, ``AuditMiddleware`` directly inside it. Asserting on the
ordering implicitly is what proves AC #3 (middleware registered
*after* RequestContextMiddleware so ``operator_sub`` and
``request_id`` are bound by the time audit runs).

The DB layer is wired against per-test aiosqlite engines via the
``isolated_audit_engine`` fixture — same pattern as
:mod:`tests.test_alembic_probe`'s ``sqlite_engine``. Each test runs
``alembic upgrade head`` on its dedicated DB so the ``audit_log``
table exists with the production schema. Mocks for Keycloak's JWKS
and Vault's KV v2 read mirror the patterns established in
:mod:`tests.test_api_v1_health`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
import structlog
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

import meho_backplane.audit as audit_module
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

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import DEFAULT_TENANT_ID as _DEFAULT_TENANT_ID
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks
from ._vault_fakes import install_fake_vault as _install_fake_vault

# ---------------------------------------------------------------------------
# Settings + JWKS-cache fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads + a tmp-path SQLite DB.

    Overrides the autouse ``_default_database_url`` from conftest.py
    with a tmp-path DB so each test owns an isolated audit table; the
    ``isolated_audit_engine`` fixture below runs ``alembic upgrade
    head`` against that DB.
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
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    """Empty the module-level JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture
def _audit_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Resolve the per-test SQLite URL and run ``alembic upgrade head``.

    The Alembic upgrade is split into a *synchronous* fixture because
    :func:`alembic.command.upgrade` calls :func:`asyncio.run` internally
    via env.py's ``run_migrations_online`` entry point; running it
    from within an async test (or async fixture) raises
    ``RuntimeError: asyncio.run() cannot be called from a running
    event loop``. Pytest-asyncio's auto-mode lets a sync fixture feed
    its return value into an async test, which is the seam we use here.

    ``DATABASE_URL`` is set **before** ``command.upgrade`` runs so the
    inner :mod:`backend.alembic.env` (which reads
    ``os.environ.get("DATABASE_URL")`` and overrides
    ``cfg.set_main_option("sqlalchemy.url", ...)``) targets *this*
    fixture's DB rather than any value inherited from the parent
    process. The ``_settings_env`` autouse fixture already pins
    ``DATABASE_URL`` to the same path, but pinning here too keeps the
    ordering invariant local — if a future caller stops depending on
    that autouse fixture the migration still hits the right DB.
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
    """Per-test aiosqlite engine bound to the migrated audit DB.

    The schema was applied by ``_audit_db_url`` (a sync fixture) so
    this fixture only constructs the engine and injects it into the
    module-level cache — the audit middleware's
    :func:`~meho_backplane.db.engine.get_sessionmaker` returns a
    factory rooted at *this* DB. Cache resets bracket the yield so
    the next test gets a fresh engine.
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


async def _fetch_audit_rows(eng: AsyncEngine) -> list[AuditLog]:
    """Return every audit_log row, ordered by occurred_at."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Happy path — authenticated request → exactly one audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticated_request_writes_one_audit_row(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /api/v1/health`` with a valid JWT writes one row before response.

    Asserts AC #4: row exists post-request, with operator_sub matching
    the JWT's ``sub``, method=GET, path=/api/v1/health, status=200,
    request_id parseable from the X-Request-Id response header.

    The TestClient is created *after* the engine fixture has populated
    the schema and injected the engine into the module cache, so the
    middleware's ``get_sessionmaker()`` resolves to the test's
    aiosqlite DB.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-100", name="Alice", email="alice@example.com")
    _install_fake_vault(monkeypatch)

    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200

    response_request_id = response.headers["x-request-id"]
    assert response_request_id  # the request-context middleware always sets it

    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == "op-100"
    assert row.method == "GET"
    assert row.path == "/api/v1/health"
    assert row.status_code == 200
    # request_id is stored as UUID; the middleware mints UUID4 hex when
    # the client doesn't send X-Request-Id, which parses cleanly.
    assert row.request_id is not None
    assert str(row.request_id).replace("-", "") == response_request_id
    assert row.duration_ms is not None
    assert row.duration_ms >= 0
    # G6.3-T2 (#379) + T3 (#380): every authenticated audit row gains
    # ``broadcast_detail_origin`` (which precedence step decided) and
    # ``broadcast_detail_effective`` (the chosen detail enum).
    # Chassis-era routes with no tenant rules + no per-call override
    # land on ``"default"`` origin; ``GET /api/v1/health`` classifies
    # as ``other`` op_class so the default detail is ``"full"``.
    assert row.payload == {
        "broadcast_detail_origin": "default",
        "broadcast_detail_effective": "full",
    }
    # G0.1-T3: tenant_id from the JWT claim lands on the audit row.
    # The default helper-minted token carries DEFAULT_TENANT_ID, which
    # ``verify_jwt_and_bind`` binds into contextvars and the audit
    # middleware reads back, parses, and writes here.
    assert row.tenant_id is not None
    assert str(row.tenant_id) == _DEFAULT_TENANT_ID


@pytest.mark.asyncio
async def test_broadcast_event_payload_omits_audit_only_origin_key(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G6.3-T2 (#379): origin lands on audit row but NOT on broadcast event.

    Regression test for the payload-mutation leak surfaced on
    [PR #683](https://github.com/evoila/meho/pull/683): the
    middleware mutates the audit ``payload`` dict with the
    resolver's origin, and pre-fix the *same* dict was passed to
    :func:`_publish_broadcast_event` whose
    :func:`redact_payload` call with ``detail="full"`` rendered
    ``payload`` as ``params`` -- leaking ``tenant_rule:<uuid>``
    identifiers into the SSE / Slack / MCP-resource feeds.

    Asserts both halves of the contract:
    * The audit row's ``payload`` carries ``broadcast_detail_origin``.
    * The published :class:`BroadcastEvent`'s ``payload`` (a
      ``redact_payload`` return value with ``detail="full"`` for a
      chassis ``GET`` route) carries no ``broadcast_detail_origin``
      key -- neither at the top level nor nested inside ``params``.
    """
    from meho_backplane.broadcast import BroadcastEvent

    captured: list[BroadcastEvent] = []

    async def _capture_publish(event: BroadcastEvent) -> None:
        captured.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture_publish)

    key = _make_rsa_keypair("kid-leak-test")
    token = _mint_token(key, sub="op-leak", name="Leak", email="leak@example.com")
    _install_fake_vault(monkeypatch)

    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200

    # Audit row carries the origin AND effective (T3 #380).
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    assert rows[0].payload == {
        "broadcast_detail_origin": "default",
        "broadcast_detail_effective": "full",
    }

    # Broadcast event must NOT carry either audit-only key -- not at
    # the top level, not inside ``params``.
    assert len(captured) == 1
    event_payload = captured[0].payload
    for forbidden in ("broadcast_detail_origin", "broadcast_detail_effective"):
        assert forbidden not in event_payload
    nested_params = event_payload.get("params")
    if isinstance(nested_params, dict):
        for forbidden in ("broadcast_detail_origin", "broadcast_detail_effective"):
            assert forbidden not in nested_params


# ---------------------------------------------------------------------------
# Skip rule — public surfaces (no operator_sub bound)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_health_check_writes_no_audit_row(
    isolated_audit_engine: AsyncEngine,
) -> None:
    """``GET /healthz`` is public — no operator, no audit row.

    The skip rule keys on the ``operator_sub`` contextvar's presence.
    ``/healthz`` is not behind ``verify_jwt_and_bind`` so the binding
    never fires; the audit middleware sees an empty ``operator_sub``
    and forwards the response unchanged.
    """
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200

    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert rows == []


@pytest.mark.asyncio
async def test_protected_route_without_token_returns_401_and_no_row(
    isolated_audit_engine: AsyncEngine,
) -> None:
    """A 401 from ``verify_jwt`` produces no audit row (no operator).

    ``verify_jwt_and_bind`` only binds ``operator_sub`` *after* the
    JWT validates; a missing-token 401 short-circuits before the
    binding fires, and the middleware sees an empty contextvar dict.
    """
    client = TestClient(app)
    response = client.get("/api/v1/health")
    assert response.status_code == 401

    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert rows == []


# ---------------------------------------------------------------------------
# Fail-closed — DB unreachable / commit raises → 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_write_failure_converts_request_to_500(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the audit insert raises, the response is replaced with 500.

    Asserts the fail-closed contract: an unaudited action is an
    unallowed action. The handler still produced its 200 response
    (the buffered messages are in the middleware's local list), but
    the audit insert raises → those messages are discarded and a
    fresh 500 ``{"detail": "audit_write_failed"}`` is sent.

    We patch ``meho_backplane.audit.get_sessionmaker`` to return a
    factory whose session raises on commit. Patching at the audit
    module's import site (rather than the engine module) is what
    keeps the ``isolated_audit_engine`` fixture's session factory
    intact for the post-test ``_fetch_audit_rows`` query — otherwise
    we'd have to introspect the failure differently because the
    audit table itself would be unreachable.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-200")
    _install_fake_vault(monkeypatch)

    class _RaisingSession:
        def __init__(self) -> None:
            pass

        async def __aenter__(self) -> _RaisingSession:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def add(self, _row: object) -> None:
            return None

        async def commit(self) -> None:
            raise RuntimeError("simulated DB outage")

    def _raising_sessionmaker() -> Any:
        return _RaisingSession

    import meho_backplane.audit as audit_module

    monkeypatch.setattr(audit_module, "get_sessionmaker", _raising_sessionmaker)

    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "audit_write_failed"}
    # Even on the failure path, RequestContextMiddleware (outermost)
    # still injects X-Request-Id. The header is the operator's only
    # crumb to correlate the failure with backplane logs.
    assert response.headers.get("x-request-id")


@pytest.mark.asyncio
async def test_handler_exception_writes_audit_row_with_status_500(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler exception still produces an audit row with ``status=500``.

    The pre-fix middleware called ``await self.app(...)`` outside any
    try/except, so an unhandled handler exception propagated to the
    outer :class:`starlette.middleware.errors.ServerErrorMiddleware`
    *before* the audit branch ran — the row was never written and the
    operator's failed action left no trace. The fix wraps the inner
    call in ``try/except Exception`` (CancelledError still propagates),
    forces ``status_code=500``, writes the row, then re-raises so
    ServerErrorMiddleware builds the canonical 500 response.

    Patches ``_probe_vault_federation`` at the
    :mod:`meho_backplane.api.v1.health` import site so the inner
    handler body raises *after* ``verify_jwt_and_bind`` has bound
    ``operator_sub``. Without that binding the skip rule would short-
    circuit before the audit write — the test would prove the wrong
    branch.

    ``TestClient`` is constructed with ``raise_server_exceptions=False``
    so the re-raised handler exception surfaces as the 500 the
    operator would actually see in production rather than failing the
    test with a propagated exception.
    """
    import meho_backplane.api.v1.health as health_module

    key = _make_rsa_keypair("kid-D")
    token = _mint_token(key, sub="op-500")
    _install_fake_vault(monkeypatch)

    async def _raising_probe(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("simulated upstream failure")

    monkeypatch.setattr(health_module, "_probe_vault_federation", _raising_probe)

    client = TestClient(app, raise_server_exceptions=False)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    # Outer ServerErrorMiddleware converts the re-raised handler
    # exception into a 500 response with Starlette's generic body —
    # which is exactly what an unaudited handler crash should produce
    # in production.
    assert response.status_code == 500

    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == "op-500"
    assert row.method == "GET"
    assert row.path == "/api/v1/health"
    assert row.status_code == 500


@pytest.mark.asyncio
async def test_request_id_passes_through_when_uuid_shaped(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller-supplied UUID-shaped X-Request-Id is preserved on the audit row."""
    key = _make_rsa_keypair("kid-B")
    token = _mint_token(key, sub="op-300")
    _install_fake_vault(monkeypatch)

    caller_request_id = str(uuid.uuid4())
    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-Id": caller_request_id,
            },
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    assert rows[0].request_id is not None
    assert str(rows[0].request_id) == caller_request_id


@pytest.mark.asyncio
async def test_request_id_null_when_caller_sends_opaque_string(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-UUID X-Request-Id values store as NULL — never fail the audit insert."""
    key = _make_rsa_keypair("kid-C")
    token = _mint_token(key, sub="op-400")
    _install_fake_vault(monkeypatch)

    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-Id": "k8s-correlation-12345",
            },
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    assert rows[0].request_id is None
    # The other fields land normally — failing the insert on a request
    # shape mismatch would convert a benign client into a 5xx.
    assert rows[0].operator_sub == "op-400"


# ---------------------------------------------------------------------------
# G0.1-T3 — tenant_id contextvar binding + AuditMiddleware persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_row_tenant_id_matches_jwt_claim(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a per-test ``tenant_id`` claim flows JWT → contextvar → row.

    Pinning a non-default tenant id (rather than re-asserting on
    DEFAULT_TENANT_ID like the happy-path test does) proves the value
    actually rides through the pipeline — a regression that hard-coded
    the default would still pass the happy-path assertion.
    """
    custom_tenant = "11111111-2222-3333-4444-555555555555"
    key = _make_rsa_keypair("kid-T")
    token = _mint_token(key, sub="op-tenant", tenant_id=custom_tenant)
    _install_fake_vault(monkeypatch)

    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    assert str(rows[0].tenant_id) == custom_tenant


def _patch_get_contextvars(
    monkeypatch: pytest.MonkeyPatch,
    *,
    transform: Any,
) -> None:
    """Patch ``audit_module.structlog.contextvars.get_contextvars`` in place.

    The audit middleware reads tenant_id off contextvars via that
    indirection; substituting a transformed copy lets a test simulate
    a programming bug (binding cleared mid-request, or the value bound
    to garbage) without touching the production code path.
    """
    real_get = structlog.contextvars.get_contextvars

    def _wrapped() -> dict[str, Any]:
        return transform(dict(real_get()))

    monkeypatch.setattr(
        audit_module.structlog.contextvars,
        "get_contextvars",
        _wrapped,
    )


@pytest.mark.asyncio
async def test_missing_tenant_contextvar_logs_error_and_writes_null(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hand-crafted bug: tenant_id contextvar cleared mid-request.

    Asserts the design contract spelt out in the issue body: "If the
    request reached AuditMiddleware past the auth dependency,
    ``tenant_id`` MUST be bound — its absence is a programming bug, not
    a runtime condition. Surface it loudly: log
    ``audit_missing_tenant_id`` at error level, write ``tenant_id=None``
    to the row, proceed."

    The bug is simulated by intercepting
    :func:`structlog.contextvars.get_contextvars` inside the audit
    module so the read returns an ``operator_sub``-bearing dict but no
    ``tenant_id`` key — exactly the shape a future contributor would
    produce by routing a protected handler through
    ``Depends(verify_jwt)`` directly instead of
    ``Depends(verify_jwt_and_bind)``. The route still resolves cleanly
    (handler-side identity comes from the typed :class:`Operator`
    dependency, which is unaffected by contextvar state), so the
    response is 200 and the audit row is written — only the
    ``tenant_id`` column is NULL.

    Captured stdlib :mod:`logging` records are inspected via
    :class:`pytest.LogCaptureFixture` rather than parsing the structlog
    JSON buffer, because the chassis test config ships structlog
    forwarding into the stdlib logger; the ``caplog`` route is the
    smallest seam for asserting on the event name.
    """
    key = _make_rsa_keypair("kid-E")
    token = _mint_token(key, sub="op-clear")
    _install_fake_vault(monkeypatch)

    def _drop_tenant(ctx: dict[str, Any]) -> dict[str, Any]:
        ctx.pop("tenant_id", None)
        return ctx

    _patch_get_contextvars(monkeypatch, transform=_drop_tenant)

    captured: list[dict[str, Any]] = []

    real_resolve = audit_module._resolve_tenant_id

    def _wrapped_resolve(*, log: Any, **kwargs: Any) -> Any:
        class _Recorder:
            def __getattr__(self, name: str) -> Any:
                return getattr(log, name)

            def error(self, event: str, **fields: Any) -> None:
                captured.append({"event": event, **fields})
                log.error(event, **fields)

        return real_resolve(log=_Recorder(), **kwargs)

    monkeypatch.setattr(audit_module, "_resolve_tenant_id", _wrapped_resolve)

    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    # The row is committed; only the tenant_id column is NULL —
    # T1's nullable column is the schema-level escape hatch for
    # exactly this programming-bug surface.
    assert rows[0].tenant_id is None
    assert rows[0].operator_sub == "op-clear"

    matched = [e for e in captured if e["event"] == "audit_missing_tenant_id"]
    assert matched, f"expected audit_missing_tenant_id in {captured!r}"
    assert matched[0]["operator_sub"] == "op-clear"
    assert matched[0]["path"] == "/api/v1/health"


@pytest.mark.asyncio
async def test_malformed_tenant_contextvar_logs_error_and_writes_null(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound ``tenant_id`` is not a UUID-shaped string → row gets NULL + error log.

    Same fail-soft contract as the missing-binding test, distinguished
    by a different log event (``audit_malformed_tenant_id``) so on-call
    can tell at a glance whether a contributor forgot to bind at all
    or bound something the wrong shape.
    """
    key = _make_rsa_keypair("kid-F")
    token = _mint_token(key, sub="op-malformed")
    _install_fake_vault(monkeypatch)

    def _garbage_tenant(ctx: dict[str, Any]) -> dict[str, Any]:
        ctx["tenant_id"] = "not-a-uuid"
        return ctx

    _patch_get_contextvars(monkeypatch, transform=_garbage_tenant)

    captured: list[dict[str, Any]] = []
    real_resolve = audit_module._resolve_tenant_id

    def _wrapped_resolve(*, log: Any, **kwargs: Any) -> Any:
        class _Recorder:
            def __getattr__(self, name: str) -> Any:
                return getattr(log, name)

            def error(self, event: str, **fields: Any) -> None:
                captured.append({"event": event, **fields})
                log.error(event, **fields)

        return real_resolve(log=_Recorder(), **kwargs)

    monkeypatch.setattr(audit_module, "_resolve_tenant_id", _wrapped_resolve)

    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    assert rows[0].tenant_id is None

    matched = [e for e in captured if e["event"] == "audit_malformed_tenant_id"]
    assert matched, f"expected audit_malformed_tenant_id in {captured!r}"
    assert matched[0]["value"] == "not-a-uuid"
