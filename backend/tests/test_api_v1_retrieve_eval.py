# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.retrieve_eval`.

Coverage matrix (G4.3-T2 / Task #441 acceptance criteria):

* **Happy path** — operator JWT + valid request returns 200 with the
  EvalResult shape (overall_verdict + per-surface metrics).
* **RBAC** — read_only JWT → 403; operator + tenant_admin → 200.
* **Validation** — extra fields rejected (extra=forbid); unknown
  surface rejected via Literal type → 422.
* **Audit + broadcast contract** — the audit_log row's payload
  carries ``op_id="meho.retrieval.eval"`` + ``op_class="audit_query"``
  + the enrichment fields (``eval_surface`` / ``eval_baseline``)
  + ``row_count`` reflecting the total queries evaluated.
* **Tenant scoping** — the runner is called with the operator's
  ``tenant_id``; a route accepting tenant from the body would fail.
"""

from __future__ import annotations

import io
import logging
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.retrieve_eval import router as eval_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.retrieval.eval.runner import EvalResult, SurfaceResult
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Settings + JWKS cache fixtures (mirrors test_api_v1_retrieve.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


# ---------------------------------------------------------------------------
# Log capture helper (mirrors test_api_v1_retrieve.py)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(eval_router)
    return app


@pytest.fixture
def client(log_buffer: io.StringIO) -> Iterator[TestClient]:
    yield TestClient(_build_app())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_eval_result(query_count: int = 10) -> EvalResult:
    """Build a stub EvalResult that the patched runner returns."""
    surface = SurfaceResult(
        surface="kb",
        query_count=query_count,
        precision_at_5=0.92,
        mrr=0.85,
        coverage=1.0,
        verdict="green",
    )
    return EvalResult(
        ran_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        surfaces=[surface],
        overall_verdict="green",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_eval_route_returns_200_with_result(client: TestClient) -> None:
    """``operator`` JWT + valid request → 200 with EvalResult shape."""
    key = _make_rsa_keypair("kid-A")
    tenant_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    token = _mint_token(
        key,
        sub="op-1",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tenant_id),
    )

    fake_eval = AsyncMock(return_value=_stub_eval_result())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.retrieve_eval.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/retrieve/eval",
            json={"surface": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["overall_verdict"] == "green"
    assert len(body["surfaces"]) == 1
    assert body["surfaces"][0]["surface"] == "kb"
    assert body["surfaces"][0]["precision_at_5"] == pytest.approx(0.92)

    # The runner was called with the operator's tenant_id (proves
    # tenant scoping is wired correctly).
    fake_eval.assert_awaited_once()
    call_kwargs = fake_eval.await_args.kwargs
    assert call_kwargs["tenant_id"] == tenant_id
    assert call_kwargs["surfaces"] == ["kb"]


def test_eval_route_default_surface_is_all(client: TestClient) -> None:
    """Empty body → surface defaults to 'all', runner called with surfaces=None."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)

    fake_eval = AsyncMock(return_value=_stub_eval_result())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.retrieve_eval.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/retrieve/eval",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    fake_eval.assert_awaited_once()
    # surface='all' → surfaces kwarg is None per the router's logic.
    assert fake_eval.await_args.kwargs["surfaces"] is None


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_eval_route_read_only_returns_403(client: TestClient) -> None:
    """``read_only`` JWT → 403 (gated below operator floor)."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-2", tenant_role=TenantRole.READ_ONLY.value)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/retrieve/eval",
            json={"surface": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403


def test_eval_route_tenant_admin_allowed(client: TestClient) -> None:
    """``tenant_admin`` JWT → 200 (above operator floor)."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-3", tenant_role=TenantRole.TENANT_ADMIN.value)

    fake_eval = AsyncMock(return_value=_stub_eval_result())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.retrieve_eval.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/retrieve/eval",
            json={"surface": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200


def test_eval_route_unauthenticated_returns_401(client: TestClient) -> None:
    """No Authorization header → 401."""
    response = client.post("/api/v1/retrieve/eval", json={"surface": "kb"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Validation (422)
# ---------------------------------------------------------------------------


def test_eval_route_unknown_surface_rejects_with_422(client: TestClient) -> None:
    """``surface="bogus"`` → 422 via the Literal type validation."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/retrieve/eval",
            json={"surface": "bogus"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 422


def test_eval_route_unknown_field_rejects_with_422(client: TestClient) -> None:
    """``extra=forbid`` catches a typo'd field at the framework boundary."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/retrieve/eval",
            json={"surface": "kb", "surfaces": "all"},  # typo: surfaces not surface
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Audit payload contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_route_audit_payload_carries_overrides_and_enrichment(
    client: TestClient,
) -> None:
    """The audit_log row carries op_id / op_class / surface / row_count.

    Uses the no-baseline path because v0.2 rejects explicit baseline
    values with 501 — see
    :func:`test_eval_route_baseline_request_returns_501` for the
    rejection contract. The audit-row contract is identical for the
    happy path (200) since the bindings happen before the runner
    kicks off.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-audit", tenant_role=TenantRole.OPERATOR.value)

    fake_eval = AsyncMock(return_value=_stub_eval_result(query_count=7))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.retrieve_eval.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/retrieve/eval",
            json={"surface": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200

    # Read back the audit row.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
        # There may be other rows from prior tests in the same DB; pick
        # the most recent one for op-audit.
        audit_rows = [r for r in rows if r.operator_sub == "op-audit"]
        assert audit_rows, "no audit row for op-audit"
        row = audit_rows[-1]

    payload: dict[str, Any] = row.payload
    assert payload.get("op_id") == "meho.retrieval.eval"
    assert payload.get("op_class") == "audit_query"
    assert payload.get("eval_surface") == "kb"
    assert payload.get("eval_baseline") == ""
    # row_count = total queries actually evaluated across surfaces.
    assert payload.get("row_count") == 7


def test_eval_route_baseline_request_returns_501(client: TestClient) -> None:
    """``baseline="grep"`` → 501 Not Implemented.

    Silent-drop is the worst possible posture: a caller that POSTs
    ``{"surface": "kb", "baseline": "grep"}`` reasonably expects the
    baseline to run, but the v0.2 server has no checked-in corpus
    snapshot. The CLI runs the baseline locally instead; the API
    rejects loud-and-honest so API-only consumers get a clear signal.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-501", tenant_role=TenantRole.OPERATOR.value)

    fake_eval = AsyncMock(return_value=_stub_eval_result())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.retrieve_eval.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/retrieve/eval",
            json={"surface": "kb", "baseline": "grep"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 501
    body = response.json()
    assert "baseline" in body.get("detail", "").lower()
    # The runner must not run on the rejection path.
    fake_eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_eval_route_baseline_request_still_writes_audit(
    client: TestClient,
) -> None:
    """501 rejection still produces an audit row recording operator intent.

    The audit context is bound *before* the baseline gate so a
    rejected request still feeds the audit pipeline. Operators
    querying ``audit_log`` for "who tried to run a server-side
    baseline?" can correlate via ``op_id='meho.retrieval.eval'`` +
    ``eval_baseline='grep'``.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-501-audit", tenant_role=TenantRole.OPERATOR.value)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/retrieve/eval",
            json={"surface": "kb", "baseline": "grep"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 501

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
        audit_rows = [r for r in rows if r.operator_sub == "op-501-audit"]
        assert audit_rows, "no audit row for op-501-audit"
        row = audit_rows[-1]

    payload: dict[str, Any] = row.payload
    assert payload.get("op_id") == "meho.retrieval.eval"
    assert payload.get("op_class") == "audit_query"
    assert payload.get("eval_surface") == "kb"
    assert payload.get("eval_baseline") == "grep"


@pytest.mark.asyncio
async def test_eval_route_audit_payload_has_empty_baseline_when_unset(
    client: TestClient,
) -> None:
    """``baseline`` not passed → audit payload's eval_baseline is empty string."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-no-baseline", tenant_role=TenantRole.OPERATOR.value)

    fake_eval = AsyncMock(return_value=_stub_eval_result())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.retrieve_eval.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/retrieve/eval",
            json={"surface": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
        audit_rows = [r for r in rows if r.operator_sub == "op-no-baseline"]
        assert audit_rows
        row = audit_rows[-1]

    assert row.payload.get("eval_baseline") == ""
