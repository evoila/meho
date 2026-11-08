# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/kb/export`` and the login that may only export knowledge (#5730).

Covers who may call the route (an operator or higher, or a
``kb_export_only`` login), that such a login reaches no other REST route
and no MCP call, how the claim is read, and the audit row.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.kb import router as kb_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import _extract_kb_export_only, clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.kb.page_export import KbExportResult
from meho_backplane.kb.service import KbService
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.middleware import KB_EXPORT_PATH, KB_IMPORT_PATH, RequestContextMiddleware
from meho_backplane.settings import get_settings
from tests._kb_embedding_stub import fake_embedding_service

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

PAGE = {
    "path": "products/vcenter/limits.md",
    "content": "---\ntype: fact\napplies_to: product-and-version\nstatus: confirmed\n---\nText.\n",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    clear_jwks_cache()
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app())


def _token(
    role: TenantRole, *, tenant_id: uuid.UUID | None = None, **claims: Any
) -> tuple[Any, str]:
    key = _make_rsa_keypair("kid-export")
    token = _mint_token(
        key,
        sub="service-account-kb-export",
        tenant_role=role.value,
        tenant_id=str(tenant_id or uuid.uuid4()),
        extra_claims=claims or None,
    )
    return key, token


def _exporter(**kwargs: Any) -> tuple[Any, str]:
    return _token(TenantRole.READ_ONLY, kb_export_only=True, **kwargs)


def _result() -> KbExportResult:
    return KbExportResult(
        page_count=0, remove_count=0, skipped_count=0, pages=[], remove=[], skipped=[]
    )


def _call(
    client: TestClient, key: Any, token: str, method: str, path: str, body: Any = None
) -> Any:
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        return client.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})


# --- who may export ----------------------------------------------------------


def test_the_route_path_is_the_one_the_cage_allows() -> None:
    from meho_backplane.main import app

    assert "post" in app.openapi()["paths"][KB_EXPORT_PATH]


def test_the_export_does_not_hide_an_entry_with_the_slug_export(client: TestClient) -> None:
    key, token = _token(TenantRole.OPERATOR)
    with patch("meho_backplane.api.v1.kb.KbService") as service:
        service.return_value.get_entry = AsyncMock(return_value=None)
        response = _call(client, key, token, "GET", "/api/v1/kb/export")
    assert response.status_code == 404
    service.return_value.get_entry.assert_awaited_once()


def test_the_export_login_may_export(client: TestClient) -> None:
    tenant = uuid.uuid4()
    fake = AsyncMock(return_value=_result())
    key, token = _exporter(tenant_id=tenant)
    with patch("meho_backplane.api.v1.kb.export_pages", fake):
        response = _call(client, key, token, "POST", KB_EXPORT_PATH)
    assert response.status_code == 200
    assert response.json()["page_count"] == 0
    assert fake.await_args.args == (tenant,)


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.TENANT_ADMIN])
def test_an_operator_may_export(client: TestClient, role: TenantRole) -> None:
    key, token = _token(role)
    with patch("meho_backplane.api.v1.kb.export_pages", AsyncMock(return_value=_result())):
        response = _call(client, key, token, "POST", KB_EXPORT_PATH)
    assert response.status_code == 200


def test_a_read_only_login_without_the_claim_is_refused(client: TestClient) -> None:
    key, token = _token(TenantRole.READ_ONLY, kb_export_only=False)
    fake = AsyncMock(return_value=_result())
    with patch("meho_backplane.api.v1.kb.export_pages", fake):
        response = _call(client, key, token, "POST", KB_EXPORT_PATH)
    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient_role"
    fake.assert_not_awaited()


def test_the_import_login_may_not_export(client: TestClient) -> None:
    key, token = _token(TenantRole.READ_ONLY, kb_import_only=True)
    fake = AsyncMock(return_value=_result())
    with patch("meho_backplane.api.v1.kb.export_pages", fake):
        response = _call(client, key, token, "POST", KB_EXPORT_PATH)
    assert response.status_code == 403
    assert response.json()["detail"] == "kb_import_only_scope_violation"
    fake.assert_not_awaited()


def test_no_token_is_401(client: TestClient) -> None:
    assert client.post(KB_EXPORT_PATH).status_code == 401


# --- the export login can do nothing else ------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/kb", None),
        ("GET", "/api/v1/kb/limits", None),
        ("GET", KB_EXPORT_PATH, None),
        ("POST", "/api/v1/kb", {"slug": "x", "body": "y"}),
        ("DELETE", "/api/v1/kb/limits", None),
        ("POST", "/api/v1/kb/ingest", {"directory": "kb"}),
        ("POST", KB_IMPORT_PATH, {"pages": [PAGE]}),
    ],
)
@pytest.mark.parametrize("role", [TenantRole.READ_ONLY, TenantRole.TENANT_ADMIN])
def test_the_export_login_is_refused_on_every_other_route(
    client: TestClient, method: str, path: str, body: Any, role: TenantRole
) -> None:
    key, token = _token(role, kb_export_only=True)
    with (
        patch("meho_backplane.api.v1.kb.KbService") as service,
        patch("meho_backplane.api.v1.kb.import_pages") as importer,
    ):
        response = _call(client, key, token, method, path, body)
    assert response.status_code == 403
    assert response.json()["detail"] == "kb_export_only_scope_violation"
    service.assert_not_called()
    importer.assert_not_called()


def test_a_login_with_both_claims_reaches_neither_route(client: TestClient) -> None:
    key, token = _token(TenantRole.TENANT_ADMIN, kb_export_only=True, kb_import_only=True)
    fake = AsyncMock(return_value=_result())
    with patch("meho_backplane.api.v1.kb.export_pages", fake):
        export = _call(client, key, token, "POST", KB_EXPORT_PATH)
        imported = _call(client, key, token, "POST", KB_IMPORT_PATH, {"pages": [PAGE]})
    assert (export.status_code, imported.status_code) == (403, 403)
    fake.assert_not_awaited()


@pytest.mark.parametrize("value", [1, "yes", ["true"], {"on": True}])
def test_a_claim_meho_cannot_read_keeps_the_login_limited(client: TestClient, value: Any) -> None:
    key, token = _token(TenantRole.TENANT_ADMIN, kb_export_only=value)
    response = _call(client, key, token, "GET", "/api/v1/kb")
    assert response.status_code == 403
    assert response.json()["detail"] == "kb_export_only_scope_violation"


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({}, False),
        ({"kb_export_only": False}, False),
        ({"kb_export_only": "FALSE"}, False),
        ({"kb_export_only": True}, True),
        ({"kb_export_only": " true "}, True),
        ({"kb_export_only": 0}, True),
        ({"kb_export_only": None}, False),
        ({"kb_import_only": True}, False),
    ],
)
def test_the_claim_is_read(claims: dict[str, Any], expected: bool) -> None:
    assert _extract_kb_export_only(claims) is expected


@pytest.mark.asyncio
async def test_the_export_login_has_no_mcp_surface() -> None:
    operator = Operator(
        sub="service-account-kb-export",
        raw_jwt="x",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.READ_ONLY,
        kb_export_only=True,
    )
    with pytest.raises(HTTPException) as caught:
        await verify_mcp_jwt_and_bind(operator)
    assert caught.value.status_code == 403
    assert caught.value.detail == "kb_export_only_scope_violation"


# --- answer and audit --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_export_writes_one_read_audit_row_with_counters_and_no_page_text(
    client: TestClient,
) -> None:
    tenant = uuid.uuid4()
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake_embedding_service(),
    ):
        await KbService().create_entry(
            tenant,
            "limits",
            "Keep at most 32 snapshots.",
            {"type": "fact", "applies_to": "product-and-version", "status": "confirmed"},
        )
    key, token = _exporter(tenant_id=tenant)
    response = _call(client, key, token, "POST", KB_EXPORT_PATH)
    assert response.status_code == 200
    assert [page["path"] for page in response.json()["pages"]] == ["limits.md"]
    async with get_sessionmaker()() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == KB_EXPORT_PATH))
        rows = list(result.scalars().all())
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "kb.export"
    assert payload["op_class"] == "read"
    assert (payload["page_count"], payload["remove_count"], payload["skipped_count"]) == (1, 0, 0)
    assert "32 snapshots" not in json.dumps(payload)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(kb_router)
    return app
