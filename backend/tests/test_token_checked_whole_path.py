# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Only the REST and the MCP bind steps mark a checked token (whole-path runs).

Each test signs a real token, sends it the way a real caller does, and
reads the login the route got:

* a person's token over REST (the REST bind step): marked, a personal agent;
* a person's token over MCP (``/mcp``, the MCP bind step): marked, a
  personal agent;
* the status route of a login with several tenants and no chosen tenant
  (only the shared token check runs): not marked;
* a scheduled agent run (the agent's own login, checked by the shared
  token check): not marked, a service agent.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from meho_backplane import middleware
from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.api.v1 import health as health_module
from meho_backplane.auth.caller_kind import caller_kind
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator
from meho_backplane.main import app as main_app
from meho_backplane.mcp import server as mcp_server
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE, ISSUER, make_rsa_keypair, mint_token, public_jwks
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_jwks

_BACKPLANE_URL = "https://meho.test"
_MCP_AUDIENCE = f"{_BACKPLANE_URL}/mcp"
_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("MCP_RESOURCE_URI", "")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    clear_jwks_cache()
    get_settings.cache_clear()


@pytest.fixture
def key() -> Any:
    return make_rsa_keypair("kid-token-checked")


def test_a_person_token_over_rest_is_marked(key: Any) -> None:
    app = FastAPI()
    seen: list[Operator] = []

    @app.get("/whoami")
    async def whoami(
        operator: Operator = Depends(middleware.verify_jwt_and_bind),
    ) -> dict[str, str]:
        seen.append(operator)
        return {"sub": operator.sub}

    token = mint_token(key, sub="person-rest")
    with respx.mock as router:
        _mock_jwks(router, public_jwks(key))
        with TestClient(app) as client:
            resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    (operator,) = seen
    assert operator.token_checked is True
    assert caller_kind(operator, None) == "personal_agent"


def test_a_person_token_over_mcp_is_marked(key: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Operator] = []
    original = mcp_server._dispatch_mcp_request

    async def _spy(request: Any, operator: Operator) -> Any:
        seen.append(operator)
        return await original(request, operator)

    monkeypatch.setattr(mcp_server, "_dispatch_mcp_request", _spy)
    token = mint_token(key, sub="person-mcp", audience=_MCP_AUDIENCE)
    with respx.mock as router:
        _mock_jwks(router, public_jwks(key))
        client = TestClient(main_app)
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == {}
    (operator,) = seen
    assert operator.token_checked is True
    assert caller_kind(operator, None) == "personal_agent"


def test_the_status_route_without_a_chosen_tenant_is_not_marked(
    key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Operator] = []
    original = health_module.build_health_response

    async def _spy(operator: Operator) -> Any:
        seen.append(operator)
        return await original(operator)

    monkeypatch.setattr(health_module, "build_health_response", _spy)
    token = mint_token(
        key,
        sub="person-status",
        extra_claims={
            "meho_tenants": [f"{_TENANT_A}:lab-a:Readers", f"{_TENANT_B}:lab-b:Operators"]
        },
    )
    app = FastAPI()
    app.include_router(health_module.router)
    with respx.mock as router:
        _mock_jwks(router, public_jwks(key))
        with TestClient(app) as client:
            resp = client.get("/api/v1/health", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    (operator,) = seen
    assert operator.tenant_chosen is False
    assert operator.token_checked is False


@pytest.mark.asyncio
async def test_a_scheduled_agent_run_is_not_marked(
    key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = mint_token(
        key,
        sub="agent-sa-uuid",
        principal_kind="agent",
        extra_claims={"azp": "agent:nightly", "client_id": "agent:nightly"},
    )
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.get_client_credentials_token",
        AsyncMock(return_value=token),
    )
    invoker = AgentInvoker(runtime=MagicMock())
    monkeypatch.setattr(
        invoker,
        "_load_definition",
        AsyncMock(return_value=SimpleNamespace(name="nightly", identity_ref="agent:nightly")),
    )
    seen: list[Operator] = []

    async def _launch(name: str, inputs: str, *, operator: Operator, **_: Any) -> None:
        seen.append(operator)

    monkeypatch.setattr(invoker, "_launch_scheduled_run", _launch)
    with respx.mock as router:
        _mock_jwks(router, public_jwks(key))
        await invoker.run_scheduled(
            "nightly",
            "check the hosts",
            agent_client_id="agent:nightly",
            agent_client_secret=SecretStr("not-a-real-secret"),
        )
    (operator,) = seen
    assert operator.token_checked is False
    assert caller_kind(operator, None) == "service_agent"
