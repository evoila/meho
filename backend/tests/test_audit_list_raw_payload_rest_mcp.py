# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The REST and MCP audit lists answer the same without ``raw_payload`` (#6818).

The audit list stopped loading the stored raw answer of each row. These
tests call ``POST /api/v1/audit/query`` and the MCP tool ``query_audit``
over rows from :mod:`tests._audit_large_raw_seed` (one holds a raw answer
of about 5 MB). Each answer must equal the one built from the fully
loaded rows, the way the list built it before.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.audit import router as audit_router
from meho_backplane.audit_query.query import _build_audit_entry
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import MCP_ADMIN_SCOPE
from meho_backplane.middleware import RequestContextMiddleware
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

from ._audit_large_raw_seed import (
    OPERATOR_SUB,
    TARGET_NAME,
    TENANT_NAME,
    Seeded,
    full_rows,
    seed,
)
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_KEY = make_rsa_keypair("kid-large-raw")
_FILTER: dict[str, Any] = {"target": TARGET_NAME}


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture
async def seeded() -> Seeded:
    return await seed(OPERATOR_TENANT_ID)


async def _expected(seeded: Seeded) -> list[dict[str, Any]]:
    """The answer rows, built from the fully loaded rows, newest first."""
    full = await full_rows(seeded.ids)
    return [
        _build_audit_entry(full[row_id], TARGET_NAME, tenant_name=TENANT_NAME).model_dump(
            mode="json"
        )
        for row_id in reversed(seeded.ids)
    ]


async def test_rest_audit_query_answer_is_unchanged(seeded: Seeded) -> None:
    rest_app = FastAPI()
    rest_app.add_middleware(RequestContextMiddleware)
    rest_app.include_router(audit_router)
    token = mint_token(
        _KEY,
        sub=OPERATOR_SUB,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(OPERATOR_TENANT_ID),
    )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEY))
        response = TestClient(rest_app).post(
            "/api/v1/audit/query",
            json=_FILTER,
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"rows": await _expected(seeded), "next_cursor": None}


async def test_mcp_query_audit_answer_is_unchanged(seeded: Seeded) -> None:
    operator = Operator(
        sub=OPERATOR_SUB,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        scopes=frozenset({MCP_ADMIN_SCOPE}),
    )

    async def _fake_verify() -> Operator:
        return operator

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            response = post_mcp(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "query_audit", "arguments": _FILTER},
                },
            )
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)

    body = response.json()
    assert body["result"]["isError"] is False, body
    answer = json.loads(body["result"]["content"][0]["text"])
    assert answer == {"rows": await _expected(seeded), "next_cursor": None}
