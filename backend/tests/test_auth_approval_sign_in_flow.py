# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval sign-in state that ``/api/v1/health`` shows (#6280).

Runs :func:`read_approval_sign_in` against a small fake Keycloak (respx):
each state, and every way the read can fail. A failed read is never ``ok``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.api.v1.health import VaultStatus, build_health_response
from meho_backplane.auth import approval_sign_in_flow
from meho_backplane.auth.approval_sign_in_flow import (
    ApprovalSignInStatus,
    read_approval_sign_in,
)
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.settings import get_settings

_KC = "https://keycloak.test"
_ADMIN = f"{_KC}/admin/realms/meho"
_TOKEN = f"{_KC}/realms/meho/protocol/openid-connect/token"
_FLOW = "meho approval sign-in"
_SUB = "meho approval sign-in - one-time code"
_STEPS_URL = f"{_ADMIN}/authentication/flows/meho%20approval%20sign-in/executions"
_FLOW_ID = "flow-approval"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{_KC}/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", _ADMIN)
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "made-up-admin-secret")
    monkeypatch.delenv("KEYCLOAK_DISCOVERY_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_CLI_APPROVE_CLIENT_ID", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _step(level: int, name: str, requirement: str, *, flow: bool = False) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "level": level,
        "requirement": requirement,
        "authenticationFlow": flow,
        "displayName": name if flow else name.replace("-", " ").title(),
        "providerId": "basic-flow" if flow else name,
    }


def _good_steps() -> list[dict[str, Any]]:
    return [
        _step(0, "auth-username-password-form", "REQUIRED"),
        _step(0, _SUB, "CONDITIONAL", flow=True),
        _step(1, "conditional-user-configured", "REQUIRED"),
        _step(1, "auth-otp-form", "REQUIRED"),
    ]


def _client(binding: str | None = _FLOW_ID) -> dict[str, Any]:
    client: dict[str, Any] = {"id": "uuid-approve", "clientId": "meho-cli-approve"}
    if binding is not None:
        client["authenticationFlowBindingOverrides"] = {"browser": binding}
    return client


def _mount(
    mock: respx.MockRouter,
    *,
    flows: Any = None,
    steps: Any = None,
    clients: Any = None,
) -> None:
    """The realm: by default the flow of the realm file, bound to the client."""
    mock.post(_TOKEN).respond(200, json={"access_token": "made-up-token"})
    mock.get(f"{_ADMIN}/authentication/flows").mock(
        return_value=flows
        if isinstance(flows, httpx.Response)
        else httpx.Response(
            200,
            json=flows
            if flows is not None
            else [{"id": "flow-browser", "alias": "browser"}, {"id": _FLOW_ID, "alias": _FLOW}],
        )
    )
    mock.get(_STEPS_URL).mock(
        return_value=steps
        if isinstance(steps, httpx.Response)
        else httpx.Response(200, json=steps if steps is not None else _good_steps())
    )
    mock.get(f"{_ADMIN}/clients", params={"clientId": "meho-cli-approve"}).mock(
        return_value=clients
        if isinstance(clients, httpx.Response)
        else httpx.Response(200, json=clients if clients is not None else [_client()])
    )


async def _read(**realm: Any) -> ApprovalSignInStatus:
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, **realm)
        return await read_approval_sign_in()


# --- The states --------------------------------------------------------------


async def test_the_flow_of_the_realm_file_bound_to_the_client_is_ok() -> None:
    status = await _read()
    assert status == ApprovalSignInStatus(state="ok", client="meho-cli-approve")
    assert status.flow == _FLOW


async def test_a_realm_without_the_flow_is_flow_missing() -> None:
    status = await _read(
        flows=[{"id": "flow-browser", "alias": "browser"}], clients=[_client(None)]
    )
    assert status.state == "flow_missing"


async def test_a_client_without_the_binding_is_not_bound() -> None:
    """A customer's own Keycloak where nobody bound the client to the flow."""
    status = await _read(clients=[_client(None)])
    assert status.state == "not_bound"
    assert status.detail == "the client meho-cli-approve has no own browser flow"


async def test_a_client_bound_to_another_flow_is_not_bound() -> None:
    status = await _read(clients=[_client("flow-browser")])
    assert status.state == "not_bound"
    assert status.detail == "the client meho-cli-approve is bound to another flow"


async def test_a_missing_client_is_not_bound() -> None:
    status = await _read(clients=[])
    assert status.state == "not_bound"
    assert status.detail == "the client meho-cli-approve does not exist"


async def test_the_client_name_comes_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_CLI_APPROVE_CLIENT_ID", "site-approve")
    get_settings.cache_clear()
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock)
        mock.get(f"{_ADMIN}/clients", params={"clientId": "site-approve"}).respond(
            200, json=[{"clientId": "site-approve", "authenticationFlowBindingOverrides": {}}]
        )
        status = await read_approval_sign_in()
    assert (status.state, status.client) == ("not_bound", "site-approve")


@pytest.mark.parametrize(
    ("steps", "difference"),
    [
        (
            [*_good_steps(), _step(0, "auth-cookie", "ALTERNATIVE")],
            "the flow has 5 steps; wanted 4",
        ),
        (
            [_step(0, "identity-provider-redirector", "ALTERNATIVE"), *_good_steps()[1:]],
            "step 1 is 'identity-provider-redirector'",
        ),
        (
            [_step(0, "auth-username-password-form", "ALTERNATIVE"), *_good_steps()[1:]],
            "step 1 is 'auth-username-password-form' (ALTERNATIVE",
        ),
        (_good_steps()[:2], "step 3 ('conditional-user-configured') is missing"),
        ([], "step 1 ('auth-username-password-form') is missing"),
    ],
    ids=["extra session step", "other sign-in system", "password optional", "no code", "empty"],
)
async def test_a_flow_with_other_steps_is_flow_differs(
    steps: list[dict[str, Any]], difference: str
) -> None:
    """Also when the client is bound to it: such a flow never counts as ok."""
    status = await _read(steps=steps)
    assert status.state == "flow_differs"
    assert status.detail is not None and status.detail.startswith(difference)


# --- A failed read is "not_checked", never "ok" ----------------------------------


async def test_without_the_admin_client_it_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYCLOAK_ADMIN_URL")
    get_settings.cache_clear()
    with respx.mock(assert_all_called=False) as mock:
        status = await read_approval_sign_in()
        assert not mock.calls
    assert (status.state, status.detail) == ("not_checked", "keycloak_admin_not_configured")


@pytest.mark.parametrize(
    ("realm", "reason"),
    [
        ({"flows": httpx.Response(403)}, "no_right_to_read_login_flows"),
        ({"steps": httpx.Response(403)}, "no_right_to_read_login_flows"),
        ({"clients": httpx.Response(403)}, "no_right_to_read_clients"),
        ({"flows": httpx.Response(500)}, "keycloak_unexpected_answer"),
        ({"steps": httpx.Response(404)}, "keycloak_unexpected_answer"),
        ({"flows": httpx.Response(200, json={"alias": _FLOW})}, "keycloak_unexpected_answer"),
        ({"flows": httpx.Response(200, text="<html>")}, "keycloak_unexpected_answer"),
        ({"steps": httpx.Response(200, json=["not a step"])}, "keycloak_unexpected_answer"),
        ({"clients": httpx.Response(200, json=None)}, "keycloak_unexpected_answer"),
    ],
    ids=[
        "flows 403",
        "steps 403",
        "clients 403",
        "flows 500",
        "steps 404",
        "flows not a list",
        "flows not JSON",
        "step not an object",
        "clients null",
    ],
)
async def test_a_failed_read_is_not_checked(realm: dict[str, Any], reason: str) -> None:
    status = await _read(**realm)
    assert (status.state, status.detail) == ("not_checked", reason)


async def test_a_refused_admin_login_is_not_checked() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock)
        mock.post(_TOKEN).respond(401, json={"error": "unauthorized_client"})
        status = await read_approval_sign_in()
    assert (status.state, status.detail) == ("not_checked", "keycloak_unreachable")


async def test_an_unreachable_keycloak_is_not_checked() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock)
        mock.get(_STEPS_URL).mock(side_effect=httpx.ConnectError("refused"))
        status = await read_approval_sign_in()
    assert (status.state, status.detail) == ("not_checked", "keycloak_unreachable")


async def test_a_slow_keycloak_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approval_sign_in_flow, "_READ_TIMEOUT_S", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=[])

    with respx.mock(assert_all_called=False) as mock:
        _mount(mock)
        mock.get(f"{_ADMIN}/authentication/flows").mock(side_effect=slow)
        status = await read_approval_sign_in()
    assert (status.state, status.detail) == ("not_checked", "keycloak_timeout")


async def test_the_status_never_holds_the_admin_secret_or_token() -> None:
    for realm in ({}, {"flows": httpx.Response(403)}, {"clients": [_client(None)]}):
        dumped = (await _read(**realm)).model_dump_json()
        assert "made-up-admin-secret" not in dumped
        assert "made-up-token" not in dumped


# --- /api/v1/health carries the state --------------------------------------------


async def test_health_shows_the_state_in_the_approvals_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def vault(*_args: object, **_kwargs: object) -> VaultStatus:
        return VaultStatus(reachable=True, read_ok=True, detail=None)

    monkeypatch.setattr("meho_backplane.api.v1.health._probe_federation", vault)
    operator = Operator(
        sub="op-6280",
        raw_jwt="<jwt>",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000006280"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, clients=[_client(None)])
        response = await build_health_response(operator)

    assert response.approvals is not None
    assert response.approvals.sign_in == ApprovalSignInStatus(
        state="not_bound",
        client="meho-cli-approve",
        detail="the client meho-cli-approve has no own browser flow",
    )
