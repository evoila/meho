# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.audit` (G8.1-T2, G8.2-T4).

Coverage matrix (#466 acceptance criteria):

* AC1 — All routes mount + respond. Exercised implicitly by every
  happy-path test below; the OpenAPI schema check
  (:func:`test_openapi_schema_lists_all_routes`) is the explicit
  AC9 surface.
* AC2 — POST /query with empty body returns all tenant's rows up to
  ``limit=100``. Verified via the dispatched filter object on the
  mocked substrate.
* AC3 — ``since="24h"`` shorthand parses to a :class:`datetime` before
  reaching the substrate.
* AC4 — ``who-touched/{target}`` builds a ``target=<path>`` filter.
* AC5 — ``my-recent`` injects ``principal=operator.sub`` from the JWT.
* AC6 — ``show/{audit_id}`` returns 404 when the substrate yields no
  rows (the cross-tenant probe case — the substrate's tenant WHERE
  clause produces zero rows for a row in another tenant, and the
  route surfaces 404, not 403, so existence is never leaked).
* AC7 — Body-supplied ``tenant_id`` (or any unknown field) is
  rejected with 422 ``extra_forbidden`` per
  :class:`AuditQueryRequest`'s ``extra="forbid"`` config (G0.9-T2 /
  #729); the substrate is never reached on validation failure. The
  route always passes ``operator.tenant_id`` from the JWT to the
  substrate for the valid-body branch.
* AC8 — Every call binds ``audit_op_id="meho.audit.query"`` +
  ``audit_op_class="audit_query"`` contextvars before the substrate
  call, so the audit row written by :class:`AuditMiddleware` carries
  the canonical op_id and the broadcast event ships as aggregate-only.
* AC9 — OpenAPI schema lists all routes under the audit tag.
* AC10 — ruff + mypy clean: Phase 7 of the implement-issue-slim
  skill, not a test here.

G8.2-T4 replay (#1012) adds the
``GET /api/v1/audit/sessions/{session_id}/replay`` route. Its tests
seed real ``audit_log`` rows via ``get_sessionmaker()`` (the autouse
SQLite-on-disk DB from ``conftest._default_database_url``) for the
happy-path / tenant-isolation cases so the full route → ``replay_session``
wiring is exercised end-to-end, and patch ``_count_session_rows`` /
``replay_session`` for the 413-cap and RBAC cases so the count-first
short-circuit is provable (the tree builder is asserted *not* awaited).

The query substrate (:func:`query_audit`) is patched at the route's
import site so the four query-route tests don't depend on seeded audit
rows — the substrate has its own coverage in
``tests/test_audit_query_handler.py`` / ``tests/test_audit_replay.py``.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import respx
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.audit import router as audit_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.audit_query import (
    AuditEntry,
    AuditQueryResult,
    InvalidCursorError,
    UnsupportedFilterError,
)
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    AUDIENCE as _AUDIENCE,
)
from ._oidc_jwt_helpers import (
    ISSUER as _ISSUER,
)
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads, around every test.

    Mirrors :mod:`tests.test_api_v1_retrieve`'s autouse fixture so the
    chassis ``verify_jwt`` dependency resolves cleanly without a real
    Keycloak / Vault on the network.
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
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def _configure_capture(buf: io.StringIO) -> None:
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )


@pytest.fixture
def log_buffer() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    _configure_capture(buf)
    yield buf
    structlog.reset_defaults()


def _read_log_lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _build_app() -> FastAPI:
    """FastAPI mirroring prod with the audit router + chassis middleware."""
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(audit_router)
    return app


@pytest.fixture
def client(log_buffer: io.StringIO) -> Iterator[TestClient]:
    """``TestClient`` driving a fresh app + log capture rebound first.

    ``log_buffer`` is requested so the structlog processors stay
    pinned for the lifetime of the request — without it the audit
    middleware's logger would render with the default config and the
    captured log lines disappear.
    """
    yield TestClient(_build_app())


_TENANT_A = UUID("33333333-3333-3333-3333-333333333333")
_TENANT_B = UUID("44444444-4444-4444-4444-444444444444")
_DEFAULT_AUDIT_ID = UUID("11111111-1111-1111-1111-111111111111")


def _empty_result() -> AuditQueryResult:
    return AuditQueryResult(rows=[], next_cursor=None)


def _make_entry(
    audit_id: UUID = _DEFAULT_AUDIT_ID,
    tenant_id: UUID = _TENANT_A,
    principal_sub: str = "op-1",
    policy_decision: str | None = None,
) -> AuditEntry:
    return AuditEntry(
        id=audit_id,
        ts=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        tenant_id=tenant_id,
        principal_sub=principal_sub,
        principal_name=None,
        target_id=None,
        target_name=None,
        method="POST",
        path="/api/v1/audit/query",
        status_code=200,
        request_id=None,
        duration_ms=None,
        payload={},
        op_id="meho.audit.query",
        op_class="audit_query",
        result_status="ok",
        parent_audit_id=None,
        agent_session_id=None,
        work_ref=None,
        policy_decision=policy_decision,
        broadcast_event_id=None,
    )


def _token(
    key: Any,
    *,
    sub: str = "op-1",
    role: TenantRole = TenantRole.OPERATOR,
    tenant_id: UUID = _TENANT_A,
) -> str:
    return mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=str(tenant_id),
    )


# ---------------------------------------------------------------------------
# POST /api/v1/audit/query
# ---------------------------------------------------------------------------


def test_post_query_empty_body_returns_200_with_no_filters(client: TestClient) -> None:
    """AC2: empty body dispatches a no-filter query with the default limit."""
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    mock_query = AsyncMock(return_value=_empty_result())
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/audit/query",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"rows": [], "next_cursor": None}
    mock_query.assert_awaited_once()
    filters = mock_query.await_args.args[0]
    kwargs = mock_query.await_args.kwargs
    assert kwargs["tenant_id"] == _TENANT_A
    assert filters.since is None
    assert filters.until is None
    assert filters.target is None
    assert filters.principal is None
    assert filters.limit == 100  # AC2: default limit


def test_post_query_since_24h_parses_to_datetime(client: TestClient) -> None:
    """AC3: ``since="24h"`` reaches the substrate as a tz-aware datetime."""
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    mock_query = AsyncMock(return_value=_empty_result())
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        before = datetime.now(UTC)
        resp = client.post(
            "/api/v1/audit/query",
            json={"since": "24h"},
            headers={"Authorization": f"Bearer {token}"},
        )
        after = datetime.now(UTC)
    assert resp.status_code == 200
    filters = mock_query.await_args.args[0]
    assert filters.since is not None
    assert filters.since.tzinfo is not None
    # The router subtracts 24h from "now" — verify the value lands in
    # the [before - 24h, after - 24h] band.
    assert (before - timedelta(hours=24, seconds=1)) <= filters.since
    assert filters.since <= (after - timedelta(hours=24) + timedelta(seconds=1))


def test_post_query_body_tenant_id_is_rejected_with_extra_forbidden(
    client: TestClient,
) -> None:
    """AC7 (G0.9-T2 / #729): ``tenant_id`` in the body is rejected at 422.

    Pre-#729 the value was silently dropped (Pydantic v2's default
    ``extra="ignore"``) and the route still ran under
    ``operator.tenant_id`` from the JWT — the test asserted "JWT
    wins". With ``extra="forbid"`` on :class:`AuditQueryRequest`,
    the framework now rejects the field at 422 with
    ``extra_forbidden``, making the cross-tenant-attempt visible to
    the caller and to the audit log instead of silently mapping it
    to own-tenant. The tenant boundary is still enforced — by
    construction the substrate is never reached for a body that
    fails validation.
    """
    key = make_rsa_keypair("kid-A")
    token = _token(key, tenant_id=_TENANT_A)
    mock_query = AsyncMock(return_value=_empty_result())
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/audit/query",
            json={"tenant_id": str(_TENANT_B)},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(
        d.get("type") == "extra_forbidden" and tuple(d.get("loc", ())) == ("body", "tenant_id")
        for d in detail
    ), detail
    mock_query.assert_not_awaited()  # Substrate never reached.


def test_post_query_bad_duration_returns_400(client: TestClient) -> None:
    """A garbage ``since`` value surfaces as 400, not 500."""
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    mock_query = AsyncMock(return_value=_empty_result())
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/audit/query",
            json={"since": "twentyfour-hours"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 400
    assert "duration" in resp.json()["detail"].lower()
    mock_query.assert_not_awaited()  # Substrate never reached.


def test_post_query_invalid_cursor_returns_400(client: TestClient) -> None:
    """Substrate-raised :class:`InvalidCursorError` maps to 400."""
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    mock_query = AsyncMock(side_effect=InvalidCursorError("cursor is not valid base64"))
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/audit/query",
            json={"cursor": "not-base64"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 400
    assert "cursor" in resp.json()["detail"].lower()


def test_post_query_parent_audit_id_returns_400(client: TestClient) -> None:
    """Substrate-raised :class:`UnsupportedFilterError` maps to 400."""
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    mock_query = AsyncMock(
        side_effect=UnsupportedFilterError(
            "parent_audit_id filter not supported in v0.2 — column lands with G0.6-T7 (#398)",
        ),
    )
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/audit/query",
            json={"parent_audit_id": "55555555-5555-5555-5555-555555555555"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 400
    assert "parent_audit_id" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/v1/audit/who-touched/{target}
# ---------------------------------------------------------------------------


def test_who_touched_builds_target_filter(client: TestClient) -> None:
    """AC4: the path param becomes the substrate filter's ``target`` field."""
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    mock_query = AsyncMock(return_value=_empty_result())
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/audit/who-touched/rdc-vcenter",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    filters = mock_query.await_args.args[0]
    assert filters.target == "rdc-vcenter"
    assert filters.since is not None  # default "24h" → datetime
    assert filters.limit == 100


# ---------------------------------------------------------------------------
# GET /api/v1/audit/by-work-ref/{ref}
# ---------------------------------------------------------------------------


def test_by_work_ref_builds_exact_work_ref_filter(client: TestClient) -> None:
    """work_ref I1-T3 #1658: the path param becomes the substrate ``work_ref`` filter.

    Unlike ``who-touched``, the route binds **no** default ``since`` window — a
    change-ticket lookup wants the whole governed history of the ref, not just
    the last 24h — so ``since`` stays None unless the caller passes one.
    """
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    mock_query = AsyncMock(return_value=_empty_result())
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/audit/by-work-ref/gh:evoila/meho%231",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    filters = mock_query.await_args.args[0]
    assert filters.work_ref == "gh:evoila/meho#1"
    assert filters.since is None  # no default window for a change-ticket lookup
    assert filters.limit == 100


# ---------------------------------------------------------------------------
# GET /api/v1/audit/my-recent
# ---------------------------------------------------------------------------


def test_my_recent_injects_principal_from_jwt(client: TestClient) -> None:
    """AC5: ``principal`` filter is the operator's JWT subject — not from the URL."""
    key = make_rsa_keypair("kid-A")
    token = _token(key, sub="op-42-jwt-sub")
    mock_query = AsyncMock(return_value=_empty_result())
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/audit/my-recent",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    filters = mock_query.await_args.args[0]
    assert filters.principal == "op-42-jwt-sub"


def test_audit_query_row_includes_policy_decision(client: TestClient) -> None:
    """#130 AC2: the ``/audit/query`` row shape carries ``policy_decision``."""
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    entry = _make_entry(policy_decision="deny")
    mock_query = AsyncMock(return_value=AuditQueryResult(rows=[entry], next_cursor=None))
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/audit/query",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert rows[0]["policy_decision"] == "deny"


def test_audit_my_recent_row_includes_policy_decision(client: TestClient) -> None:
    """#130 AC2: the ``/audit/my-recent`` row shape carries ``policy_decision``."""
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    entry = _make_entry(policy_decision="needs-approval")
    mock_query = AsyncMock(return_value=AuditQueryResult(rows=[entry], next_cursor=None))
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/audit/my-recent",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["rows"][0]["policy_decision"] == "needs-approval"


# ---------------------------------------------------------------------------
# GET /api/v1/audit/show/{audit_id}
# ---------------------------------------------------------------------------


def test_show_returns_200_with_entry_when_found(client: TestClient) -> None:
    """``show`` returns 200 + the row when the substrate yields one."""
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    audit_id = UUID("11111111-1111-1111-1111-111111111111")
    entry = _make_entry(audit_id=audit_id)
    mock_query = AsyncMock(return_value=AuditQueryResult(rows=[entry], next_cursor=None))
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/show/{audit_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["id"] == str(audit_id)
    filters = mock_query.await_args.args[0]
    assert filters.audit_id == audit_id
    assert filters.limit == 1


def test_show_returns_404_when_substrate_yields_no_rows(client: TestClient) -> None:
    """AC6: cross-tenant probe surfaces 404, not 403 — existence never leaks."""
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    cross_tenant_audit_id = UUID("66666666-6666-6666-6666-666666666666")
    mock_query = AsyncMock(return_value=_empty_result())
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/show/{cross_tenant_audit_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Audit-on-audit-query (AC8)
# ---------------------------------------------------------------------------


def test_audit_overrides_bound_for_post_query(
    client: TestClient,
    log_buffer: io.StringIO,
) -> None:
    """AC8: ``audit_op_id="meho.audit.query"`` reaches the audit log row.

    The override is bound via :func:`structlog.contextvars.bind_contextvars`
    before the substrate call. The audit middleware reads these contextvars
    when constructing the audit row's ``payload``; the audit row's
    ``payload->>'op_id'`` is what operators filter on. We verify the
    override is observable by checking the captured structlog output —
    every log line during the request inherits the bound contextvars,
    so any line emitted between the bind and the response carries the
    override key.
    """
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    mock_query = AsyncMock(return_value=_empty_result())
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/audit/query",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200

    lines = _read_log_lines(log_buffer)
    overrides_observed = [
        line
        for line in lines
        if line.get("audit_op_id") == "meho.audit.query"
        and line.get("audit_op_class") == "audit_query"
    ]
    assert overrides_observed, (
        "expected at least one structlog line carrying audit_op_id="
        "'meho.audit.query' + audit_op_class='audit_query' bound by the route"
    )


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_unauthenticated_returns_401(client: TestClient) -> None:
    """No Authorization header → 401 on every route."""
    resp = client.post("/api/v1/audit/query", json={})
    assert resp.status_code == 401


def test_read_only_role_returns_403(client: TestClient) -> None:
    """``read_only`` JWT is below the operator gate — 403."""
    key = make_rsa_keypair("kid-A")
    token = _token(key, role=TenantRole.READ_ONLY)
    mock_query = AsyncMock(return_value=_empty_result())
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/audit/query",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403
    mock_query.assert_not_awaited()


def test_tenant_admin_role_returns_200(client: TestClient) -> None:
    """``tenant_admin`` JWT clears the operator gate."""
    key = make_rsa_keypair("kid-A")
    token = _token(key, role=TenantRole.TENANT_ADMIN)
    mock_query = AsyncMock(return_value=_empty_result())
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.query_audit", new=mock_query),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/audit/query",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# OpenAPI mount surface (AC1 + AC9)
# ---------------------------------------------------------------------------


def test_openapi_schema_lists_all_routes(client: TestClient) -> None:
    """AC9: ``/openapi.json`` advertises every audit route under the audit tag."""
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/audit/query" in paths
    assert "/api/v1/audit/who-touched/{target}" in paths
    assert "/api/v1/audit/by-work-ref/{ref}" in paths
    assert "/api/v1/audit/my-recent" in paths
    assert "/api/v1/audit/show/{audit_id}" in paths
    assert "/api/v1/audit/sessions/{session_id}/replay" in paths
    # Every route is tagged "audit" so operators filtering /docs by tag
    # find them grouped under one section.
    assert "post" in paths["/api/v1/audit/query"]
    assert "audit" in paths["/api/v1/audit/query"]["post"]["tags"]
    assert "audit" in paths["/api/v1/audit/who-touched/{target}"]["get"]["tags"]
    assert "audit" in paths["/api/v1/audit/by-work-ref/{ref}"]["get"]["tags"]
    assert "audit" in paths["/api/v1/audit/my-recent"]["get"]["tags"]
    assert "audit" in paths["/api/v1/audit/show/{audit_id}"]["get"]["tags"]
    assert "audit" in paths["/api/v1/audit/sessions/{session_id}/replay"]["get"]["tags"]
    # The replay 200 body is the dedicated envelope, not the query result.
    replay_get = paths["/api/v1/audit/sessions/{session_id}/replay"]["get"]
    ok_schema = replay_get["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok_schema["$ref"].endswith("/AuditReplayResult")


# ---------------------------------------------------------------------------
# GET /api/v1/audit/sessions/{session_id}/replay (G8.2-T4)
# ---------------------------------------------------------------------------


async def _seed_audit_row(
    s: AsyncSession,
    *,
    tenant_id: UUID,
    second: int,
    agent_session_id: UUID | None = None,
    parent_audit_id: UUID | None = None,
    row_id: UUID | None = None,
) -> UUID:
    """Insert one :class:`AuditLog` row at a fixed base + ``second`` offset."""
    row_id = row_id or uuid.uuid4()
    base = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    s.add(
        AuditLog(
            id=row_id,
            occurred_at=base + timedelta(seconds=second),
            operator_sub="op-1",
            tenant_id=tenant_id,
            method="POST",
            path="/mcp",
            status_code=200,
            duration_ms=Decimal("1.0"),
            payload={"op_id": "vsphere.vm.list", "op_class": "read"},
            agent_session_id=agent_session_id,
            parent_audit_id=parent_audit_id,
        ),
    )
    return row_id


async def _seed_session_tree(tenant_id: UUID, session_id: UUID) -> tuple[UUID, UUID, UUID]:
    """Seed a root → child → grandchild tree under ``session_id``; return ids."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        root = await _seed_audit_row(s, tenant_id=tenant_id, second=0, agent_session_id=session_id)
        child = await _seed_audit_row(
            s, tenant_id=tenant_id, second=1, agent_session_id=session_id, parent_audit_id=root
        )
        grandchild = await _seed_audit_row(
            s, tenant_id=tenant_id, second=2, agent_session_id=session_id, parent_audit_id=child
        )
        await s.commit()
    return root, child, grandchild


@pytest.mark.asyncio
async def test_replay_returns_seeded_multi_level_tree(client: TestClient) -> None:
    """AC1: a seeded multi-level session replays to its tree; ids echo correctly."""
    session_id = uuid.uuid4()
    root, child, grandchild = await _seed_session_tree(_TENANT_A, session_id)

    key = make_rsa_keypair("kid-A")
    # #1843: cross-session replay is tenant_admin-gated.
    token = _token(key, role=TenantRole.TENANT_ADMIN, tenant_id=_TENANT_A)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/sessions/{session_id}/replay",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == str(session_id)
    assert body["tenant_id"] == str(_TENANT_A)
    assert body["row_count"] == 3  # three anchor rows in the session
    assert len(body["root"]) == 1
    node = body["root"][0]
    assert node["id"] == str(root)
    assert node["depth"] == 0
    assert [c["id"] for c in node["children"]] == [str(child)]
    assert node["children"][0]["depth"] == 1
    assert [g["id"] for g in node["children"][0]["children"]] == [str(grandchild)]


@pytest.mark.asyncio
async def test_replay_tenant_isolation_foreign_session_is_empty(client: TestClient) -> None:
    """AC2: tenant B requesting tenant A's session id gets empty — never A's rows."""
    session_id = uuid.uuid4()
    await _seed_session_tree(_TENANT_A, session_id)

    key = make_rsa_keypair("kid-A")
    # Caller is tenant B requesting the session id seeded under tenant A.
    # #1843: cross-session replay is tenant_admin-gated; the cross-tenant
    # isolation guard is orthogonal and must still return empty (not A's rows).
    token = _token(key, role=TenantRole.TENANT_ADMIN, tenant_id=_TENANT_B)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/sessions/{session_id}/replay",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200  # not 404 — foreign == empty, existence never leaks
    body = resp.json()
    assert body["root"] == []
    assert body["row_count"] == 0
    assert body["tenant_id"] == str(_TENANT_B)


@pytest.mark.asyncio
async def test_replay_unknown_session_returns_empty_not_404(client: TestClient) -> None:
    """An unknown session id yields ``root=[]`` / ``row_count=0`` — not 404."""
    session_id = uuid.uuid4()
    key = make_rsa_keypair("kid-A")
    # #1843: cross-session replay is tenant_admin-gated.
    token = _token(key, role=TenantRole.TENANT_ADMIN, tenant_id=_TENANT_A)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/sessions/{session_id}/replay",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "root": [],
        "session_id": str(session_id),
        "tenant_id": str(_TENANT_A),
        "row_count": 0,
    }


def test_replay_over_cap_returns_413_without_building_tree(client: TestClient) -> None:
    """AC3: > 10k rows → 413; the recursive tree build is never reached.

    The count-first guard is proven by patching ``replay_session`` as a
    spy and asserting it was never awaited — the route rejected on the
    count alone, before materializing the tree.
    """
    session_id = uuid.uuid4()
    key = make_rsa_keypair("kid-A")
    # #1843: cross-session replay is tenant_admin-gated.
    token = _token(key, role=TenantRole.TENANT_ADMIN, tenant_id=_TENANT_A)
    spy_replay = AsyncMock(return_value=[])
    over_cap_count = AsyncMock(return_value=10_001)
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.replay_session", new=spy_replay),
        patch("meho_backplane.api.v1.audit._count_session_rows", new=over_cap_count),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/sessions/{session_id}/replay",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 413
    assert resp.json()["detail"] == {"detail": "session_too_large", "row_count": 10_001}
    spy_replay.assert_not_awaited()  # count-first: tree never built


def test_replay_at_cap_boundary_returns_200(client: TestClient) -> None:
    """A session of exactly 10k rows is allowed — the cap is strictly ``>``."""
    session_id = uuid.uuid4()
    key = make_rsa_keypair("kid-A")
    # #1843: cross-session replay is tenant_admin-gated.
    token = _token(key, role=TenantRole.TENANT_ADMIN, tenant_id=_TENANT_A)
    spy_replay = AsyncMock(return_value=[])
    at_cap_count = AsyncMock(return_value=10_000)
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.replay_session", new=spy_replay),
        patch("meho_backplane.api.v1.audit._count_session_rows", new=at_cap_count),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/sessions/{session_id}/replay",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["row_count"] == 10_000
    spy_replay.assert_awaited_once()


def test_replay_read_only_role_returns_403(client: TestClient) -> None:
    """AC4: ``read_only`` is below the cross-session replay gate — 403; substrate untouched.

    #1843 lifted this route to ``tenant_admin``, so ``read_only`` remains
    403 (now two ranks below the gate rather than one).
    """
    key = make_rsa_keypair("kid-A")
    token = _token(key, role=TenantRole.READ_ONLY)
    spy_replay = AsyncMock(return_value=[])
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.replay_session", new=spy_replay),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/sessions/{uuid.uuid4()}/replay",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403
    spy_replay.assert_not_awaited()


def test_replay_operator_role_returns_403(client: TestClient) -> None:
    """#1843: cross-session replay is ``tenant_admin``-only — ``operator`` → 403.

    The replay route takes an *arbitrary* ``session_id`` and reconstructs
    another principal's full session trace, so it gates one rank above the
    flat / self-scoped routes (which stay ``operator``). This matches the
    MCP ``meho.audit.replay`` tool and ``docs/cross-repo/audit-replay.md``.
    The substrate is never reached — RBAC rejects before dispatch.
    """
    key = make_rsa_keypair("kid-A")
    token = _token(key, role=TenantRole.OPERATOR, tenant_id=_TENANT_A)
    spy_replay = AsyncMock(return_value=[])
    spy_count = AsyncMock(return_value=0)
    with (
        respx.mock as r,
        patch("meho_backplane.api.v1.audit.replay_session", new=spy_replay),
        patch("meho_backplane.api.v1.audit._count_session_rows", new=spy_count),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/sessions/{uuid.uuid4()}/replay",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403
    spy_replay.assert_not_awaited()
    spy_count.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_tenant_admin_role_returns_200(client: TestClient) -> None:
    """#1843: ``tenant_admin`` clears the cross-session replay gate (operator no longer does)."""
    key = make_rsa_keypair("kid-A")
    token = _token(key, role=TenantRole.TENANT_ADMIN, tenant_id=_TENANT_A)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/sessions/{uuid.uuid4()}/replay",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200


def test_replay_binds_replay_op_id_and_aggregate_only_class(
    client: TestClient,
    log_buffer: io.StringIO,
) -> None:
    """AC5: replay binds ``audit_op_id='meho.audit.replay'`` + aggregate-only class.

    The route's own audit-on-replay broadcast must be aggregate-only
    (``op_class='audit_query'``) and tagged with the distinct replay
    op_id. We assert the contextvars are bound by checking the captured
    structlog lines (full aggregate-only payload assertion is T7).
    """
    key = make_rsa_keypair("kid-A")
    # #1843: cross-session replay is tenant_admin-gated.
    token = _token(key, role=TenantRole.TENANT_ADMIN, tenant_id=_TENANT_A)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/sessions/{uuid.uuid4()}/replay",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200

    lines = _read_log_lines(log_buffer)
    observed = [
        line
        for line in lines
        if line.get("audit_op_id") == "meho.audit.replay"
        and line.get("audit_op_class") == "audit_query"
    ]
    assert observed, (
        "expected a structlog line carrying audit_op_id='meho.audit.replay' "
        "+ audit_op_class='audit_query' bound by the replay route"
    )
