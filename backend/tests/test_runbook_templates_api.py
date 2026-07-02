# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.runbook_templates`.

Coverage matrix (G12.2-T3 / Task #1297 acceptance criteria):

* **Route mounting** -- all six routes appear in the FastAPI app's route
  table and the OpenAPI document.
* **Draft** -- ``tenant_admin`` POST -> 201; duplicate slug -> 409;
  invalid slug -> 422; ``operator`` role -> 403.
* **List** -- ``operator`` can list its tenant's rows; ``?status=`` filter
  reaches the substrate filter.
* **Show** -- ``tenant_admin`` gets the full body; ``operator`` -> 403
  (opacity floor, NOT 404); missing slug -> 404; ``?version=`` reaches the
  service; cross-tenant probe -> 404 (anti-enumeration).
* **Edit** -- PATCH on a draft -> 200 ``forked_from=null``; PATCH on a
  published slug -> 200 with ``forked_from`` populated.
* **Publish** -- ``POST /publish`` -> 200; deprecated version -> 400;
  missing version -> 404.
* **Deprecate** -- ``POST /deprecate`` -> 200; draft version -> 400.
* **Audit op_id binding** -- each route binds the canonical
  ``audit_op_id`` + ``audit_op_class`` so the audit row classifies under
  the runbook op id rather than the HTTP-shape default.

Tests boot the FastAPI app with the production middleware stack
(``RequestContextMiddleware`` + ``AuditMiddleware``) so audit rows are
inserted into the autouse-migrated SQLite engine. The
:class:`RunbookTemplateService` is patched on the route's import site for
the per-route behavioural tests; the service's own DB-backed coverage
lives in ``tests/test_runbooks_template_service.py``.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import respx
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.runbook_templates import router as runbook_templates_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.kb.schemas import InvalidKbSlugError
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.runbooks.schemas import (
    DeprecateTemplateResponse,
    DiscardTemplateResponse,
    DraftTemplateResponse,
    EditTemplateResponse,
    ForkInfo,
    ManualStep,
    PublishTemplateResponse,
    ShowTemplateResponse,
    TemplateSummary,
)
from meho_backplane.runbooks.service import (
    DuplicateDraftError,
    TemplateNotDraftError,
    TemplateNotFoundError,
    TemplateNotPublishedError,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_ROUTE = "meho_backplane.api.v1.runbook_templates.RunbookTemplateService"
_RUN_ROUTE = "meho_backplane.api.v1.runbook_templates.RunbookRunService"

# ---------------------------------------------------------------------------
# Settings + JWKS cache fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads."""
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


# ---------------------------------------------------------------------------
# Log capture (mirrors test_api_v1_kb.py)
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
    """Return a :class:`FastAPI` mirroring prod with the runbook router mounted."""
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(runbook_templates_router)
    return app


@pytest.fixture
def client(log_buffer: io.StringIO) -> Iterator[TestClient]:
    """``TestClient`` driving a fresh app per test."""
    yield TestClient(_build_app())


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _admin_token(*, tenant_id: UUID | None = None, sub: str = "op-admin") -> tuple[Any, str]:
    key = _make_rsa_keypair("kid-admin")
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(tid),
    )
    return key, token


def _operator_token(*, tenant_id: UUID | None = None, sub: str = "op-operator") -> tuple[Any, str]:
    key = _make_rsa_keypair("kid-operator")
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tid),
    )
    return key, token


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Body / response builders
# ---------------------------------------------------------------------------


def _template_body() -> dict[str, Any]:
    """A minimal valid :class:`RunbookTemplateBody` payload (one manual step)."""
    return {
        "title": "Rotate certificate",
        "description": "Rotate the expiring TLS cert on the edge node.",
        "target_kind": "host",
        "steps": [
            {
                "id": "revoke-old-cert",
                "title": "Revoke the old certificate",
                "body": "SSH to ${run.target} and revoke the cert.",
                "type": "manual",
                "verify": {"type": "confirm", "prompt": "Is the old cert revoked?"},
            }
        ],
    }


def _show_response(
    slug: str = "rotate-cert", version: int = 1, status: str = "draft"
) -> ShowTemplateResponse:
    step = ManualStep(
        id="revoke-old-cert",
        title="Revoke the old certificate",
        body="SSH to ${run.target} and revoke the cert.",
        type="manual",
        verify={"type": "confirm", "prompt": "Is the old cert revoked?"},  # type: ignore[arg-type]
    )
    return ShowTemplateResponse(
        slug=slug,
        version=version,
        title="Rotate certificate",
        description="Rotate the expiring TLS cert on the edge node.",
        target_kind="host",
        status=status,  # type: ignore[arg-type]
        steps=[step],
        created_by="op-admin",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        edited_by="op-admin",
        edited_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


async def _audit_rows_for_path(path: str) -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == path))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Route mounting
# ---------------------------------------------------------------------------


def test_all_seven_routes_mounted_on_main_app() -> None:
    """All seven routes appear in :mod:`meho_backplane.main`'s app + OpenAPI."""
    from meho_backplane.main import app

    openapi = app.openapi()
    paths = openapi["paths"]

    expected_paths = {
        "/api/v1/runbooks/templates",
        "/api/v1/runbooks/templates/{slug}",
        "/api/v1/runbooks/templates/{slug}/publish",
        "/api/v1/runbooks/templates/{slug}/deprecate",
        "/api/v1/runbooks/templates/{slug}/discard",
    }
    missing = expected_paths - paths.keys()
    assert not missing, f"missing routes: {missing}"

    assert "post" in paths["/api/v1/runbooks/templates"]
    assert "get" in paths["/api/v1/runbooks/templates"]
    assert "get" in paths["/api/v1/runbooks/templates/{slug}"]
    assert "patch" in paths["/api/v1/runbooks/templates/{slug}"]
    assert "post" in paths["/api/v1/runbooks/templates/{slug}/publish"]
    assert "post" in paths["/api/v1/runbooks/templates/{slug}/deprecate"]
    assert "post" in paths["/api/v1/runbooks/templates/{slug}/discard"]


# ---------------------------------------------------------------------------
# Unauthenticated (401)
# ---------------------------------------------------------------------------


def test_list_unauthenticated_returns_401(client: TestClient) -> None:
    assert client.get("/api/v1/runbooks/templates").status_code == 401


def test_draft_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.post(
        "/api/v1/runbooks/templates", json={"slug": "x", "body": _template_body()}
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST / -- draft
# ---------------------------------------------------------------------------


def test_draft_201(client: TestClient) -> None:
    """Tenant_admin POST with a valid body → 201 + DraftTemplateResponse shape."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a, sub="op-admin")
    fake_create = AsyncMock(
        return_value=DraftTemplateResponse(slug="rotate-cert", version=1, status="draft")
    )
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.create_draft", fake_create),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates",
            json={"slug": "rotate-cert", "body": _template_body()},
            headers=_authed(token),
        )

    assert response.status_code == 201
    body = response.json()
    assert body == {"slug": "rotate-cert", "version": 1, "status": "draft"}
    fake_create.assert_awaited_once()
    # Tenant id + operator sub are forwarded positionally.
    call_args = fake_create.await_args.args
    assert call_args[0] == tenant_a
    assert call_args[1] == "op-admin"


def test_draft_duplicate_409(client: TestClient) -> None:
    """Second draft on the same slug → 409 (DuplicateDraftError)."""
    key, token = _admin_token()
    fake_create = AsyncMock(
        side_effect=DuplicateDraftError("slug 'rotate-cert' already has a version")
    )
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.create_draft", fake_create),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates",
            json={"slug": "rotate-cert", "body": _template_body()},
            headers=_authed(token),
        )
    assert response.status_code == 409
    assert "already has a version" in response.json()["detail"]


def test_draft_invalid_slug_422(client: TestClient) -> None:
    """Slug ``Has-Caps`` fails SLUG_PATTERN at the request model → 422."""
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates",
            json={"slug": "Has-Caps", "body": _template_body()},
            headers=_authed(token),
        )
    assert response.status_code == 422


def test_draft_service_invalid_slug_422(client: TestClient) -> None:
    """Service-side InvalidKbSlugError (defense in depth) → 422 from the route.

    #1364: the body conforms to the OpenAPI ``HTTPValidationError`` LIST
    shape — ``{"detail": [{"loc": ["path", "slug"], "msg": ...,
    "type": "invalid_kb_slug"}]}`` — so a typed client deserializes it
    cleanly instead of erroring on the legacy ``{"detail": "<string>"}``.
    """
    key, token = _admin_token()
    fake_create = AsyncMock(side_effect=InvalidKbSlugError("slug does not match pattern"))
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.create_draft", fake_create),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates",
            json={"slug": "ok-slug", "body": _template_body()},
            headers=_authed(token),
        )
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "loc": ["path", "slug"],
            "msg": "slug does not match pattern",
            "type": "invalid_kb_slug",
        }
    ]


def test_draft_operator_role_403(client: TestClient) -> None:
    """Operator (non-admin) on POST → 403 (tenant_admin only)."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates",
            json={"slug": "rotate-cert", "body": _template_body()},
            headers=_authed(token),
        )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET / -- list
# ---------------------------------------------------------------------------


def test_list_operator_ok(client: TestClient) -> None:
    """Operator can list; the service is called with its tenant_id."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    summary = TemplateSummary(
        slug="rotate-cert",
        version=1,
        title="Rotate certificate",
        status="draft",
        target_kind="host",
        edited_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    fake_list = AsyncMock(return_value=[summary])
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.list_templates", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/runbooks/templates", headers=_authed(token))

    assert response.status_code == 200
    body = response.json()
    assert len(body["templates"]) == 1
    assert body["templates"][0]["slug"] == "rotate-cert"
    fake_list.assert_awaited_once()
    assert fake_list.await_args.args[0] == tenant_a


def test_list_filters_by_status(client: TestClient) -> None:
    """``?status=published`` reaches the ListTemplatesFilter the service receives."""
    key, token = _operator_token()
    fake_list = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.list_templates", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/runbooks/templates?status=published&target_kind=host&limit=10",
            headers=_authed(token),
        )

    assert response.status_code == 200
    template_filter = fake_list.await_args.args[1]
    assert template_filter.status == "published"
    assert template_filter.target_kind == "host"
    assert fake_list.await_args.kwargs["limit"] == 10


def test_list_invalid_status_422(client: TestClient) -> None:
    """An out-of-vocabulary ``status`` trips the filter validator → 422."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/runbooks/templates?status=bogus",
            headers=_authed(token),
        )
    assert response.status_code == 422


def test_list_envelope_v2_unified_shape(client: TestClient) -> None:
    """``?envelope=v2`` returns ``{items, next_cursor}``; the keyed field is absent.

    G0.22-T6 (#1611): the same rows the default ``{"templates": [...]}``
    shape carries ride under ``items``; the listing is unpaged so
    ``next_cursor`` is always ``null``. The cross-endpoint contract pin
    lives in ``test_api_v1_list_envelope_v2.py``; this test owns the
    data-bearing assertion that the v2 items match the keyed payload.
    """
    key, token = _operator_token()
    summary = TemplateSummary(
        slug="rotate-cert",
        version=1,
        title="Rotate certificate",
        status="draft",
        target_kind="host",
        edited_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    fake_list = AsyncMock(return_value=[summary])
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.list_templates", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/runbooks/templates?envelope=v2",
            headers=_authed(token),
        )

    assert response.status_code == 200
    body = response.json()
    assert [item["slug"] for item in body["items"]] == ["rotate-cert"]
    assert body["items"][0]["edited_at"] == "2026-01-02T00:00:00Z"
    assert body["next_cursor"] is None
    assert "templates" not in body


# ---------------------------------------------------------------------------
# GET /{slug} -- show
# ---------------------------------------------------------------------------


def test_show_admin_ok(client: TestClient) -> None:
    """Tenant_admin gets the full template body."""
    key, token = _admin_token()
    fake_show = AsyncMock(return_value=_show_response())
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.show_template", fake_show),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/runbooks/templates/rotate-cert", headers=_authed(token))

    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "rotate-cert"
    assert body["steps"][0]["id"] == "revoke-old-cert"


def test_show_admin_unchanged(client: TestClient) -> None:
    """Regression: tenant_admin reads still 200 + full body (T4 must not break admin)."""
    key, token = _admin_token()
    fake_show = AsyncMock(return_value=_show_response())
    fake_predicate = AsyncMock(return_value=False)  # never consulted for admins
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.show_template", fake_show),
        patch(f"{_RUN_ROUTE}.can_show_template_post_completion", fake_predicate),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/runbooks/templates/rotate-cert", headers=_authed(token))
    assert response.status_code == 200
    assert response.json()["slug"] == "rotate-cert"
    # The run-state predicate is never consulted for the admin path.
    fake_predicate.assert_not_awaited()


def test_show_operator_with_no_run_gets_403(client: TestClient) -> None:
    """AC: operator with no run against (slug, latest) → 403, detail=opacity_floor."""
    key, token = _operator_token()
    fake_show = AsyncMock(return_value=_show_response(version=1))
    fake_predicate = AsyncMock(return_value=False)
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.show_template", fake_show),
        patch(f"{_RUN_ROUTE}.can_show_template_post_completion", fake_predicate),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/runbooks/templates/rotate-cert", headers=_authed(token))
    assert response.status_code == 403
    assert response.json()["detail"] == "opacity_floor"
    fake_predicate.assert_awaited_once()


def test_show_operator_with_in_progress_run_gets_403(client: TestClient) -> None:
    """AC: in_progress run does NOT lift the gate — predicate returns False, route 403s."""
    # The predicate (T3, #1308) only returns True for completed/abandoned; for
    # an in_progress run it returns False — the route's role-conditional handler
    # is the same shape regardless of *why* False was returned.
    key, token = _operator_token()
    fake_show = AsyncMock(return_value=_show_response(version=1))
    fake_predicate = AsyncMock(return_value=False)
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.show_template", fake_show),
        patch(f"{_RUN_ROUTE}.can_show_template_post_completion", fake_predicate),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/runbooks/templates/rotate-cert", headers=_authed(token))
    assert response.status_code == 403
    assert response.json()["detail"] == "opacity_floor"


def test_show_operator_with_completed_run_gets_200(client: TestClient) -> None:
    """AC: completed run against (slug, v1) → predicate True → 200 + full body."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a, sub="op-operator")
    fake_show = AsyncMock(return_value=_show_response(version=1))
    fake_predicate = AsyncMock(return_value=True)
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.show_template", fake_show),
        patch(f"{_RUN_ROUTE}.can_show_template_post_completion", fake_predicate),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/runbooks/templates/rotate-cert?version=1",
            headers=_authed(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "rotate-cert"
    assert body["version"] == 1
    assert body["steps"][0]["id"] == "revoke-old-cert"
    # Predicate called with the operator's tenant_id + sub + the resolved (slug, version).
    args = fake_predicate.await_args.args
    assert args[0] == tenant_a
    assert args[1] == "op-operator"
    assert args[2] == "rotate-cert"
    assert args[3] == 1


def test_show_operator_with_abandoned_run_gets_200(client: TestClient) -> None:
    """AC: abandoned counts the same as completed (post-mortem use case)."""
    # The predicate's body returns True for state ∈ {completed, abandoned}; the
    # route does not distinguish — both yield 200.
    key, token = _operator_token()
    fake_show = AsyncMock(return_value=_show_response(version=1))
    fake_predicate = AsyncMock(return_value=True)
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.show_template", fake_show),
        patch(f"{_RUN_ROUTE}.can_show_template_post_completion", fake_predicate),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/runbooks/templates/rotate-cert?version=1",
            headers=_authed(token),
        )
    assert response.status_code == 200


def test_show_operator_no_version_resolves_latest_then_authorizes(client: TestClient) -> None:
    """AC: operator with no ?version: latest is resolved first, then predicate checked.

    Service is called once with version=None to resolve latest (returns v2);
    the predicate is then called with the *resolved* version (v2), not None,
    so the authorization check uses the same version the response returns.
    """
    key, token = _operator_token()
    fake_show = AsyncMock(return_value=_show_response(version=2))
    fake_predicate = AsyncMock(return_value=True)
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.show_template", fake_show),
        patch(f"{_RUN_ROUTE}.can_show_template_post_completion", fake_predicate),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/runbooks/templates/rotate-cert", headers=_authed(token))
    assert response.status_code == 200
    assert response.json()["version"] == 2
    # The service was asked for version=None (resolve-latest);
    # the predicate was asked for the resolved version (2), not None.
    show_kwargs = fake_show.await_args.kwargs
    assert show_kwargs["version"] is None
    assert fake_predicate.await_args.args[3] == 2


def test_show_operator_completed_v1_asks_for_v2_gets_403(client: TestClient) -> None:
    """AC: version-specific authorization — completed v1 does NOT authorize v2 reads."""
    # Caller asks ?version=2 explicitly; predicate is asked about v2 (not v1)
    # and returns False (operator only has a v1 run). Route 403s.
    key, token = _operator_token()
    fake_show = AsyncMock(return_value=_show_response(version=2))
    fake_predicate = AsyncMock(return_value=False)
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.show_template", fake_show),
        patch(f"{_RUN_ROUTE}.can_show_template_post_completion", fake_predicate),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/runbooks/templates/rotate-cert?version=2",
            headers=_authed(token),
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "opacity_floor"
    # Predicate was asked about v2 (the version the operator requested).
    assert fake_predicate.await_args.args[3] == 2
    # And on the predicate-first branch the service is never called.
    fake_show.assert_not_awaited()


def test_show_cross_tenant_operator_gets_403(client: TestClient) -> None:
    """AC: operator in tenant A asking for tenant B's slug → 403 (predicate is tenant-scoped).

    The predicate receives ``operator.tenant_id`` (tenant A); a row matching
    tenant B is invisible to the query, so the predicate returns False and
    the route surfaces 403 with the anti-enumeration ``opacity_floor`` detail
    rather than leaking template-existence via a differential status.
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    # The service's tenant-scoped query makes tenant B's slug invisible →
    # TemplateNotFoundError. The route's operator branch maps that to 403
    # opacity_floor (NOT 404) so an operator cannot enumerate other tenants'
    # slugs via status-code differential.
    fake_show = AsyncMock(side_effect=TemplateNotFoundError("not found for tenant"))
    fake_predicate = AsyncMock(return_value=False)
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.show_template", fake_show),
        patch(f"{_RUN_ROUTE}.can_show_template_post_completion", fake_predicate),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/runbooks/templates/other-tenant-slug",
            headers=_authed(token),
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "opacity_floor"


def test_show_missing_404(client: TestClient) -> None:
    """Admin asking for a missing slug → 404."""
    key, token = _admin_token()
    fake_show = AsyncMock(side_effect=TemplateNotFoundError("no template for slug 'nope'"))
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.show_template", fake_show),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/runbooks/templates/nope", headers=_authed(token))
    assert response.status_code == 404


def test_show_specific_version(client: TestClient) -> None:
    """``?version=1`` reaches the service as the version argument."""
    key, token = _admin_token()
    fake_show = AsyncMock(return_value=_show_response(version=1))
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.show_template", fake_show),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/runbooks/templates/rotate-cert?version=1",
            headers=_authed(token),
        )
    assert response.status_code == 200
    assert fake_show.await_args.kwargs["version"] == 1


def test_cross_tenant_show_returns_404(client: TestClient) -> None:
    """Tenant A's admin asking for tenant B's template → 404 (anti-enumeration).

    The service's tenant-scoped query makes the other tenant's row
    invisible, so it raises TemplateNotFoundError exactly as it would for
    a genuinely absent slug -- the route maps that to 404, never 403 and
    never the other tenant's body.
    """
    key, token = _admin_token(tenant_id=uuid.uuid4())
    fake_show = AsyncMock(
        side_effect=TemplateNotFoundError("no template for slug 'other-tenant-runbook'")
    )
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.show_template", fake_show),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/runbooks/templates/other-tenant-runbook",
            headers=_authed(token),
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /{slug} -- edit
# ---------------------------------------------------------------------------


def test_edit_draft_in_place_200(client: TestClient) -> None:
    """PATCH on a draft → 200, ``forked_from=null``."""
    key, token = _admin_token()
    fake_edit = AsyncMock(
        return_value=EditTemplateResponse(
            slug="rotate-cert", version=1, status="draft", forked_from=None
        )
    )
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.update_or_fork", fake_edit),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/runbooks/templates/rotate-cert",
            json=_template_body(),
            headers=_authed(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert body["forked_from"] is None
    # The path slug is the authoritative target.
    assert fake_edit.await_args.args[2].slug == "rotate-cert"


def test_edit_published_forks_200(client: TestClient) -> None:
    """PATCH on a published slug → 200 with ``forked_from`` populated."""
    key, token = _admin_token()
    fake_edit = AsyncMock(
        return_value=EditTemplateResponse(
            slug="rotate-cert",
            version=2,
            status="draft",
            forked_from=ForkInfo(slug="rotate-cert", version=1, in_flight_run_count=3),
        )
    )
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.update_or_fork", fake_edit),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/runbooks/templates/rotate-cert",
            json=_template_body(),
            headers=_authed(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 2
    assert body["forked_from"]["version"] == 1
    assert body["forked_from"]["in_flight_run_count"] == 3


def test_edit_missing_404(client: TestClient) -> None:
    """PATCH on a slug with no versions → 404."""
    key, token = _admin_token()
    fake_edit = AsyncMock(side_effect=TemplateNotFoundError("nothing to edit or fork"))
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.update_or_fork", fake_edit),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/runbooks/templates/nope",
            json=_template_body(),
            headers=_authed(token),
        )
    assert response.status_code == 404


def test_edit_operator_role_403(client: TestClient) -> None:
    """Operator on PATCH → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/runbooks/templates/rotate-cert",
            json=_template_body(),
            headers=_authed(token),
        )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# POST /{slug}/publish
# ---------------------------------------------------------------------------


def test_publish_200(client: TestClient) -> None:
    """POST /publish flips draft → published."""
    key, token = _admin_token()
    fake_publish = AsyncMock(
        return_value=PublishTemplateResponse(slug="rotate-cert", version=1, status="published")
    )
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.publish", fake_publish),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates/rotate-cert/publish",
            json={"version": 1},
            headers=_authed(token),
        )
    assert response.status_code == 200
    assert response.json()["status"] == "published"
    publish_req = fake_publish.await_args.args[1]
    assert publish_req.slug == "rotate-cert"
    assert publish_req.version == 1


def test_publish_deprecated_400(client: TestClient) -> None:
    """POST /publish on a deprecated version → 400 (TemplateNotDraftError)."""
    key, token = _admin_token()
    fake_publish = AsyncMock(side_effect=TemplateNotDraftError("is 'deprecated', not draft"))
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.publish", fake_publish),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates/rotate-cert/publish",
            json={"version": 1},
            headers=_authed(token),
        )
    assert response.status_code == 400


def test_publish_missing_404(client: TestClient) -> None:
    """POST /publish on a nonexistent version → 404."""
    key, token = _admin_token()
    fake_publish = AsyncMock(side_effect=TemplateNotFoundError("no template 'rotate-cert' v9"))
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.publish", fake_publish),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates/rotate-cert/publish",
            json={"version": 9},
            headers=_authed(token),
        )
    assert response.status_code == 404


def test_publish_operator_role_403(client: TestClient) -> None:
    """Operator on POST /publish → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates/rotate-cert/publish",
            json={"version": 1},
            headers=_authed(token),
        )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# POST /{slug}/deprecate
# ---------------------------------------------------------------------------


def test_deprecate_200(client: TestClient) -> None:
    """POST /deprecate flips published → deprecated."""
    key, token = _admin_token()
    fake_deprecate = AsyncMock(
        return_value=DeprecateTemplateResponse(slug="rotate-cert", version=1, status="deprecated")
    )
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.deprecate", fake_deprecate),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates/rotate-cert/deprecate",
            json={"version": 1},
            headers=_authed(token),
        )
    assert response.status_code == 200
    assert response.json()["status"] == "deprecated"


def test_deprecate_draft_400(client: TestClient) -> None:
    """POST /deprecate on a draft → 400 (TemplateNotPublishedError)."""
    key, token = _admin_token()
    fake_deprecate = AsyncMock(side_effect=TemplateNotPublishedError("is 'draft', not published"))
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.deprecate", fake_deprecate),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates/rotate-cert/deprecate",
            json={"version": 1},
            headers=_authed(token),
        )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /{slug}/discard  (#135)
# ---------------------------------------------------------------------------


def test_discard_200(client: TestClient) -> None:
    """POST /discard deletes a draft → 200 with status="discarded"."""
    key, token = _admin_token()
    fake_discard = AsyncMock(
        return_value=DiscardTemplateResponse(slug="rotate-cert", version=1, status="discarded")
    )
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.discard", fake_discard),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates/rotate-cert/discard",
            json={"version": 1},
            headers=_authed(token),
        )
    assert response.status_code == 200
    assert response.json()["status"] == "discarded"


def test_discard_published_400(client: TestClient) -> None:
    """POST /discard on a published version → 400 (TemplateNotDraftError)."""
    key, token = _admin_token()
    fake_discard = AsyncMock(
        side_effect=TemplateNotDraftError(
            "is 'published', not draft; cannot discard (use deprecate)"
        )
    )
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.discard", fake_discard),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates/rotate-cert/discard",
            json={"version": 1},
            headers=_authed(token),
        )
    assert response.status_code == 400
    assert "deprecate" in response.json()["detail"]


def test_discard_missing_404(client: TestClient) -> None:
    """POST /discard on a missing (slug, version) → 404 (TemplateNotFoundError)."""
    key, token = _admin_token()
    fake_discard = AsyncMock(side_effect=TemplateNotFoundError("no template 'ghost' v1 for tenant"))
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.discard", fake_discard),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates/ghost/discard",
            json={"version": 1},
            headers=_authed(token),
        )
    assert response.status_code == 404


def test_discard_operator_role_403(client: TestClient) -> None:
    """Operator on POST /discard → 403 (tenant_admin-only, matches sibling verbs)."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/templates/rotate-cert/discard",
            json={"version": 1},
            headers=_authed(token),
        )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Malformed path slug on the model-rebuilding routes (regression: #1336 B1)
#
# PATCH /{slug}, POST /{slug}/publish, and POST /{slug}/deprecate rebuild a
# SLUG_PATTERN-validated request model from the raw path param. A malformed
# slug must surface as 422 (the same status draft/show-by-pattern use), not
# a 500 from an unhandled in-handler ValidationError -- and the rejection
# must short-circuit before the service is touched.
# ---------------------------------------------------------------------------

_MALFORMED_SLUG = "Bad-Caps"  # uppercase -> fails SLUG_PATTERN


def _assert_invalid_kb_slug_shape(detail: Any) -> None:
    """Assert *detail* is the conformant invalid-slug 422 list entry (#1364).

    The four template write routes map ``InvalidKbSlugError`` through the
    shared ``http_for`` emitter, which produces the Pydantic-list shape
    ``{"detail": [{"loc": ["path", "slug"], "msg": ...,
    "type": "invalid_kb_slug"}]}`` so a typed client deserializes the body
    and keys on ``detail[0].type``. The ``msg`` carries the full
    SLUG_PATTERN explanation verbatim, so we structurally pin ``loc`` /
    ``type`` and substring-check the message.
    """
    assert isinstance(detail, list)
    assert len(detail) == 1
    entry = detail[0]
    assert entry["loc"] == ["path", "slug"]
    assert entry["type"] == "invalid_kb_slug"
    assert "does not match" in entry["msg"]


def test_edit_malformed_slug_422_no_service_call(client: TestClient) -> None:
    """PATCH on a malformed path slug → 422, service never invoked."""
    key, token = _admin_token()
    fake_edit = AsyncMock()
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.update_or_fork", fake_edit),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            f"/api/v1/runbooks/templates/{_MALFORMED_SLUG}",
            json=_template_body(),
            headers=_authed(token),
        )
    assert response.status_code == 422
    _assert_invalid_kb_slug_shape(response.json()["detail"])
    fake_edit.assert_not_awaited()


def test_publish_malformed_slug_422_no_service_call(client: TestClient) -> None:
    """POST /publish on a malformed path slug → 422, service never invoked."""
    key, token = _admin_token()
    fake_publish = AsyncMock()
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.publish", fake_publish),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/templates/{_MALFORMED_SLUG}/publish",
            json={"version": 1},
            headers=_authed(token),
        )
    assert response.status_code == 422
    _assert_invalid_kb_slug_shape(response.json()["detail"])
    fake_publish.assert_not_awaited()


def test_deprecate_malformed_slug_422_no_service_call(client: TestClient) -> None:
    """POST /deprecate on a malformed path slug → 422, service never invoked."""
    key, token = _admin_token()
    fake_deprecate = AsyncMock()
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.deprecate", fake_deprecate),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/templates/{_MALFORMED_SLUG}/deprecate",
            json={"version": 1},
            headers=_authed(token),
        )
    assert response.status_code == 422
    _assert_invalid_kb_slug_shape(response.json()["detail"])
    fake_deprecate.assert_not_awaited()


def test_discard_malformed_slug_422_no_service_call(client: TestClient) -> None:
    """POST /discard on a malformed path slug → 422, service never invoked."""
    key, token = _admin_token()
    fake_discard = AsyncMock()
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.discard", fake_discard),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/templates/{_MALFORMED_SLUG}/discard",
            json={"version": 1},
            headers=_authed(token),
        )
    assert response.status_code == 422
    _assert_invalid_kb_slug_shape(response.json()["detail"])
    fake_discard.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_malformed_slug_no_write_audit_row() -> None:
    """A rejected malformed slug must not bind the write-classified op_id.

    The 422 fires before ``bind_contextvars``, so the audit row for the
    rejected request must not carry ``op_id="runbook.publish_template"``
    / ``op_class="write"`` -- a rejected input never produces a
    write-classified audit/broadcast side-effect.
    """
    key, token = _admin_token()
    fake_publish = AsyncMock()
    path = f"/api/v1/runbooks/templates/{_MALFORMED_SLUG}/publish"
    test_client = TestClient(_build_app())
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.publish", fake_publish),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = test_client.post(path, json={"version": 1}, headers=_authed(token))
    assert response.status_code == 422
    fake_publish.assert_not_awaited()

    rows = await _audit_rows_for_path(path)
    for row in rows:
        payload = row.payload
        assert payload.get("op_id") != "runbook.publish_template"
        assert payload.get("op_class") != "write"


# ---------------------------------------------------------------------------
# Audit op_id binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_writes_audit_row_with_draft_op_id() -> None:
    """POST → audit row carries ``op_id="runbook.draft_template"`` + ``op_class="write"`` + slug."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    fake_create = AsyncMock(
        return_value=DraftTemplateResponse(slug="rotate-cert", version=1, status="draft")
    )
    test_client = TestClient(_build_app())
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.create_draft", fake_create),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = test_client.post(
            "/api/v1/runbooks/templates",
            json={"slug": "rotate-cert", "body": _template_body()},
            headers=_authed(token),
        )
    assert response.status_code == 201

    rows = await _audit_rows_for_path("/api/v1/runbooks/templates")
    post_rows = [r for r in rows if r.method == "POST"]
    assert len(post_rows) == 1
    payload = post_rows[0].payload
    assert payload["op_id"] == "runbook.draft_template"
    assert payload["op_class"] == "write"
    assert payload["slug"] == "rotate-cert"


@pytest.mark.asyncio
async def test_list_writes_audit_row_with_list_op_id() -> None:
    """GET → audit row carries ``op_id="runbook.list_templates"`` + ``op_class="read"``."""
    key, token = _operator_token()
    fake_list = AsyncMock(return_value=[])
    test_client = TestClient(_build_app())
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.list_templates", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = test_client.get("/api/v1/runbooks/templates", headers=_authed(token))
    assert response.status_code == 200

    rows = await _audit_rows_for_path("/api/v1/runbooks/templates")
    get_rows = [r for r in rows if r.method == "GET"]
    assert len(get_rows) == 1
    payload = get_rows[0].payload
    assert payload["op_id"] == "runbook.list_templates"
    assert payload["op_class"] == "read"


@pytest.mark.asyncio
async def test_publish_writes_audit_row_with_publish_op_id_and_version() -> None:
    """POST /publish → audit row ``op_id="runbook.publish_template"`` + slug + version."""
    key, token = _admin_token()
    fake_publish = AsyncMock(
        return_value=PublishTemplateResponse(slug="rotate-cert", version=2, status="published")
    )
    test_client = TestClient(_build_app())
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.publish", fake_publish),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = test_client.post(
            "/api/v1/runbooks/templates/rotate-cert/publish",
            json={"version": 2},
            headers=_authed(token),
        )
    assert response.status_code == 200

    rows = await _audit_rows_for_path("/api/v1/runbooks/templates/rotate-cert/publish")
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "runbook.publish_template"
    assert payload["op_class"] == "write"
    assert payload["slug"] == "rotate-cert"
    assert payload["version"] == 2
    # The template body / step contents never reach the audit payload.
    serialised = json.dumps(payload)
    assert "revoke-old-cert" not in serialised


@pytest.mark.asyncio
async def test_discard_writes_audit_row_with_discard_op_id_and_version() -> None:
    """POST /discard → audit row ``op_id="runbook.discard_template"`` + slug + version (AC4)."""
    key, token = _admin_token()
    fake_discard = AsyncMock(
        return_value=DiscardTemplateResponse(slug="rotate-cert", version=2, status="discarded")
    )
    test_client = TestClient(_build_app())
    with (
        respx.mock as mock_router,
        patch(f"{_ROUTE}.discard", fake_discard),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = test_client.post(
            "/api/v1/runbooks/templates/rotate-cert/discard",
            json={"version": 2},
            headers=_authed(token),
        )
    assert response.status_code == 200

    rows = await _audit_rows_for_path("/api/v1/runbooks/templates/rotate-cert/discard")
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "runbook.discard_template"
    assert payload["op_class"] == "write"
    assert payload["slug"] == "rotate-cert"
    assert payload["version"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("blank_body", ["", "   ", "\t\n  "])
async def test_draft_rejects_blank_step_body_422(blank_body: str) -> None:
    """A step with an empty or whitespace-only body is rejected at request validation (#2117 D3).

    ``ManualStep.body`` / ``OperationCallStep.body`` carry
    ``StringConstraints(strip_whitespace=True, min_length=1)``; a blank body
    (empty *or* whitespace-only) is a non-functional step (the operator sees a
    blank verify prompt), so it must not be silently accepted. Pydantic v2
    strips before the min_length check, so a whitespace-only body collapses to
    ``""`` and fails.
    """
    key, token = _admin_token()
    payload = _template_body()
    payload["steps"][0]["body"] = blank_body  # blank body -> min_length violation after strip
    test_client = TestClient(_build_app())
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = test_client.post(
            "/api/v1/runbooks/templates",
            json={"slug": "rotate-cert", "body": payload},
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
