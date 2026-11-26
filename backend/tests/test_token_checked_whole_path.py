# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Only the four REST and MCP token checks mark a checked token (whole-path runs).

Each test signs a real token with a made-up key, sends it the way a real
caller does through the real app, and reads the login that arrives:

* a person's token on ``POST /api/v1/operations/call``, read at the
  dispatcher: marked, a personal agent;
* a person's token on ``/mcp`` with ``tools/call`` ``call_operation``,
  read at the dispatcher: marked, a personal agent;
* the status route ``GET /api/v1/health``, with and without a chosen
  tenant, and for an agent and a service token: marked;
* a scheduled agent run (the agent's own login, checked by the shared
  token check): not marked, a service agent;
* a web console login through the web session check: not marked, a person;
* a login built in code: not marked.

The two bind steps give back the same login object they get.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.requests import Request

from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.api.v1 import health as health_module
from meho_backplane.auth.caller_kind import caller_kind
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.main import app as main_app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.middleware import _bind_operator
from meho_backplane.operations._errors import result_connector_not_found
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.refresh import verify_access_token_with_refresh
from meho_backplane.ui.auth.session_store import DecryptedSession

from ._oidc_jwt_helpers import (
    AUDIENCE,
    DEFAULT_TENANT_ID,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    public_jwks,
)
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_jwks

_BACKPLANE_URL = "https://meho.test"
_MCP_AUDIENCE = f"{_BACKPLANE_URL}/mcp"
_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_DISPATCH = "meho_backplane.operations.meta_tools.dispatch"


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


def _spy_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[Operator]:
    """Replace the dispatcher; return the list of logins that reach it."""
    seen: list[Operator] = []

    async def _dispatch(*, operator: Operator, op_id: str, **_: Any) -> Any:
        seen.append(operator)
        missing = ConnectorResourceNotFoundError(["x"], "stub: not found")
        return result_connector_not_found(op_id, missing, duration_ms=1.0)

    monkeypatch.setattr(_DISPATCH, _dispatch)
    return seen


_CALL_ARGUMENTS: dict[str, Any] = {
    "connector_id": "vmware-rest-9.0",
    "op_id": "vmware.vm.info",
    "target": None,
    "params": {"name": "x"},
}


def test_a_person_token_on_the_rest_call_is_marked_at_dispatch(
    key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_dispatch(monkeypatch)
    token = mint_token(key, sub="person-rest")
    with respx.mock as router:
        _mock_jwks(router, public_jwks(key))
        resp = TestClient(main_app).post(
            "/api/v1/operations/call",
            json=_CALL_ARGUMENTS,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    (operator,) = seen
    assert operator.token_checked is True
    assert caller_kind(operator, None) == "personal_agent"


def test_a_person_token_on_the_mcp_call_operation_is_marked_at_dispatch(
    key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_dispatch(monkeypatch)
    token = mint_token(key, sub="person-mcp", audience=_MCP_AUDIENCE)
    with respx.mock as router:
        _mock_jwks(router, public_jwks(key))
        resp = TestClient(main_app).post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "call_operation", "arguments": _CALL_ARGUMENTS},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    (operator,) = seen
    assert operator.token_checked is True
    assert caller_kind(operator, None) == "personal_agent"


def _status_login(key: Any, monkeypatch: pytest.MonkeyPatch, token: str) -> Operator:
    """Send *token* to the status route of the real app; return the login it got."""
    seen: list[Operator] = []

    async def _spy(operator: Operator) -> Any:
        seen.append(operator)
        raise HTTPException(status_code=418, detail="read")

    monkeypatch.setattr(health_module, "build_health_response", _spy)
    with respx.mock as router:
        _mock_jwks(router, public_jwks(key))
        resp = TestClient(main_app).get(
            "/api/v1/health", headers={"Authorization": f"Bearer {token}"}
        )
    assert resp.status_code == 418, resp.text
    (operator,) = seen
    return operator


def test_the_status_route_with_a_chosen_tenant_is_marked(
    key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = _status_login(key, monkeypatch, mint_token(key, sub="person-status-one"))
    assert operator.tenant_chosen is True
    assert operator.token_checked is True


def test_the_status_route_without_a_chosen_tenant_is_marked(
    key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = mint_token(
        key,
        sub="person-status-two",
        extra_claims={
            "meho_tenants": [f"{_TENANT_A}:lab-a:Readers", f"{_TENANT_B}:lab-b:Operators"]
        },
    )
    operator = _status_login(key, monkeypatch, token)
    assert operator.tenant_chosen is False
    assert operator.token_checked is True


@pytest.mark.parametrize("kind", ["agent", "service"])
def test_the_status_route_marks_an_agent_and_a_service_token(
    key: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    token = mint_token(
        key,
        sub=f"{kind}-status",
        principal_kind=kind,
        extra_claims={"azp": f"{kind}:status", "client_id": f"{kind}:status"},
    )
    operator = _status_login(key, monkeypatch, token)
    assert operator.principal_kind is PrincipalKind(kind)
    assert operator.token_checked is True
    assert caller_kind(operator, None) == "service_agent"


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


@pytest.mark.asyncio
async def test_a_web_console_login_is_not_marked(key: Any) -> None:
    now = datetime.now(UTC)
    session = DecryptedSession(
        id=uuid.uuid4(),
        operator_sub="person-web",
        tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
        created_at=now,
        expires_at=now + timedelta(hours=1),
        access_token=mint_token(key, sub="person-web"),
        refresh_token="not-a-real-refresh-token",
        last_seen_at=now,
    )
    with respx.mock as router:
        _mock_jwks(router, public_jwks(key))
        _, operator = await verify_access_token_with_refresh(session, expected_audience=AUDIENCE)
    assert operator.web_session is True
    assert operator.token_checked is False
    assert caller_kind(operator, None) == "person"


def _in_code(principal_kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub="system:example",
        raw_jwt="",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
        in_code_kind="system",
    )


def test_a_login_built_in_code_is_not_marked() -> None:
    assert _in_code().token_checked is False


@pytest.mark.asyncio
@pytest.mark.parametrize("principal_kind", [PrincipalKind.USER, PrincipalKind.AGENT])
async def test_the_mcp_bind_step_returns_the_login_it_gets(
    principal_kind: PrincipalKind,
) -> None:
    operator = _in_code(principal_kind).model_copy(update={"token_checked": True})
    assert await verify_mcp_jwt_and_bind(operator) is operator


@pytest.mark.asyncio
async def test_the_rest_bind_step_returns_the_login_it_gets() -> None:
    operator = _in_code().model_copy(update={"token_checked": True})
    request = Request({"type": "http", "method": "GET", "path": "/api/v1/x", "headers": []})
    assert await _bind_operator(request, operator) is operator
