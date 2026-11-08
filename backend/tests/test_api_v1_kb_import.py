# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/kb/import`` and the login that may only import knowledge (#5728).

Covers who may call the route (a tenant admin, or a ``kb_import_only``
login), that such a login reaches no other REST route and no MCP call,
how the claim is read, the request shape, and the audit row.
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
from meho_backplane.auth.jwt import _extract_kb_import_only, clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Document
from meho_backplane.kb.page_import import MAX_PAGES, KbImportResult
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.middleware import KB_IMPORT_PATH, RequestContextMiddleware
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
    key = _make_rsa_keypair("kid-import")
    token = _mint_token(
        key,
        sub="service-account-kb-import",
        tenant_role=role.value,
        tenant_id=str(tenant_id or uuid.uuid4()),
        extra_claims=claims or None,
    )
    return key, token


def _importer(**kwargs: Any) -> tuple[Any, str]:
    return _token(TenantRole.READ_ONLY, kb_import_only=True, **kwargs)


def _result() -> KbImportResult:
    return KbImportResult(
        dry_run=False,
        created_count=1,
        updated_count=0,
        unchanged_count=0,
        skipped_count=0,
        pages=[],
        conflicts=[],
    )


def _call(
    client: TestClient, key: Any, token: str, method: str, path: str, body: Any = None
) -> Any:
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        return client.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})


# --- who may import ----------------------------------------------------------


def test_the_route_path_is_the_one_the_cage_allows() -> None:
    from meho_backplane.main import app

    assert "post" in app.openapi()["paths"][KB_IMPORT_PATH]


def test_the_import_login_may_import(client: TestClient) -> None:
    fake = AsyncMock(return_value=_result())
    key, token = _importer()
    with patch("meho_backplane.api.v1.kb.import_pages", fake):
        response = _call(client, key, token, "POST", KB_IMPORT_PATH, {"pages": [PAGE]})
    assert response.status_code == 200
    assert response.json()["created_count"] == 1
    request = fake.await_args.args[0]
    assert request.pages[0].path == PAGE["path"]
    assert fake.await_args.kwargs["actor_sub"] == "service-account-kb-import"


def test_a_tenant_admin_may_import(client: TestClient) -> None:
    key, token = _token(TenantRole.TENANT_ADMIN)
    with patch("meho_backplane.api.v1.kb.import_pages", AsyncMock(return_value=_result())):
        response = _call(client, key, token, "POST", KB_IMPORT_PATH, {"pages": [PAGE]})
    assert response.status_code == 200


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
def test_a_login_without_the_claim_below_tenant_admin_is_refused(
    client: TestClient, role: TenantRole
) -> None:
    key, token = _token(role, kb_import_only=False)
    fake = AsyncMock(return_value=_result())
    with patch("meho_backplane.api.v1.kb.import_pages", fake):
        response = _call(client, key, token, "POST", KB_IMPORT_PATH, {"pages": [PAGE]})
    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient_role"
    fake.assert_not_awaited()


def test_no_token_is_401(client: TestClient) -> None:
    assert client.post(KB_IMPORT_PATH, json={"pages": [PAGE]}).status_code == 401


# --- the import login can do nothing else ------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/kb", None),
        ("GET", "/api/v1/kb/limits", None),
        ("POST", "/api/v1/kb", {"slug": "x", "body": "y"}),
        ("DELETE", "/api/v1/kb/limits", None),
        ("POST", "/api/v1/kb/ingest", {"directory": "kb"}),
        ("GET", KB_IMPORT_PATH, None),
        ("DELETE", KB_IMPORT_PATH, None),
    ],
)
@pytest.mark.parametrize("role", [TenantRole.READ_ONLY, TenantRole.TENANT_ADMIN])
def test_the_import_login_is_refused_on_every_other_route(
    client: TestClient, method: str, path: str, body: Any, role: TenantRole
) -> None:
    key, token = _token(role, kb_import_only=True)
    with patch("meho_backplane.api.v1.kb.KbService") as service:
        response = _call(client, key, token, method, path, body)
    assert response.status_code == 403
    assert response.json()["detail"] == "kb_import_only_scope_violation"
    service.assert_not_called()


@pytest.mark.parametrize("value", [1, "yes", ["true"], {"on": True}])
def test_a_claim_meho_cannot_read_keeps_the_login_limited(client: TestClient, value: Any) -> None:
    key, token = _token(TenantRole.TENANT_ADMIN, kb_import_only=value)
    response = _call(client, key, token, "GET", "/api/v1/kb")
    assert response.status_code == 403
    assert response.json()["detail"] == "kb_import_only_scope_violation"


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({}, False),
        ({"kb_import_only": False}, False),
        ({"kb_import_only": "FALSE"}, False),
        ({"kb_import_only": True}, True),
        ({"kb_import_only": " true "}, True),
        ({"kb_import_only": 0}, True),
        ({"kb_import_only": None}, False),
    ],
)
def test_the_claim_is_read(claims: dict[str, Any], expected: bool) -> None:
    assert _extract_kb_import_only(claims) is expected


@pytest.mark.asyncio
async def test_the_import_login_has_no_mcp_surface() -> None:
    operator = Operator(
        sub="service-account-kb-import",
        raw_jwt="x",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.READ_ONLY,
        kb_import_only=True,
    )
    with pytest.raises(HTTPException) as caught:
        await verify_mcp_jwt_and_bind(operator)
    assert caught.value.status_code == 403
    assert caught.value.detail == "kb_import_only_scope_violation"


# --- request shape and audit -------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"pages": []},
        {"pages": [PAGE], "unknown": 1},
        {"pages": [{"path": "a.md"}]},
        {"pages": [{**PAGE, "content": ""}]},
        {"pages": [PAGE] * (MAX_PAGES + 1)},
    ],
)
def test_a_bad_request_is_422(client: TestClient, body: Any) -> None:
    key, token = _importer()
    fake = AsyncMock(return_value=_result())
    with patch("meho_backplane.api.v1.kb.import_pages", fake):
        response = _call(client, key, token, "POST", KB_IMPORT_PATH, body)
    assert response.status_code == 422
    fake.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_import_writes_one_audit_row_with_counters_and_no_page_text(
    client: TestClient,
) -> None:
    key, token = _importer()
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake_embedding_service(),
    ):
        response = _call(client, key, token, "POST", KB_IMPORT_PATH, {"pages": [PAGE]})
    assert response.status_code == 200
    assert response.json()["pages"][0]["action"] == "create"
    rows = await _audit_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "kb.import"
    assert payload["op_class"] == "write"
    assert payload["created_count"] == 1
    assert payload["skipped_count"] == 0
    assert "Text." not in json.dumps(payload)


@pytest.mark.asyncio
async def test_a_dry_run_is_marked_in_the_audit_row_and_writes_no_entry(
    client: TestClient,
) -> None:
    key, token = _importer()
    response = _call(client, key, token, "POST", KB_IMPORT_PATH, {"pages": [PAGE], "dry_run": True})
    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    assert response.json()["pages"][0]["action"] == "create"
    rows = await _audit_rows()
    assert [row.payload["dry_run"] for row in rows] == [True]
    async with get_sessionmaker()() as session:
        stored = await session.execute(select(Document).where(Document.source_id == "limits"))
        assert stored.first() is None


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(kb_router)
    return app


async def _audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == KB_IMPORT_PATH))
        return list(result.scalars().all())
