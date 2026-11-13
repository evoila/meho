# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval sign-in state that ``/api/v1/health`` shows (#6280).

Runs :func:`read_approval_sign_in` against a small fake Keycloak (respx):
each state, and every way the read can fail. A failed read is never ``ok``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import HTTPException

from meho_backplane.api.v1.health import VaultStatus, build_health_response
from meho_backplane.auth import approval_sign_in_flow
from meho_backplane.auth.approval_sign_in import (
    LEVEL_MISSING_REMEDIATION,
    LEVEL_UNCHECKED_REMEDIATION,
    ApprovalAuth,
    check_fresh_approval_sign_in,
)
from meho_backplane.auth.approval_sign_in_flow import (
    ApprovalSignInStatus,
    read_approval_sign_in,
    sign_in_level_in_force,
    sign_in_level_warning,
)
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.settings import get_settings

_KC = "https://keycloak.test"
_ADMIN = f"{_KC}/admin/realms/meho"
_TOKEN = f"{_KC}/realms/meho/protocol/openid-connect/token"
_FLOW = "meho approval sign-in level"
_LEVEL = "meho approval sign-in level - level 2"
_SUB = "meho approval sign-in level - one-time code"
_STEPS_URL = f"{_ADMIN}/authentication/flows/meho%20approval%20sign-in%20level/executions"
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
    approval_sign_in_flow._kept.clear()
    yield
    get_settings.cache_clear()
    approval_sign_in_flow._kept.clear()


def _step(level: int, name: str, requirement: str, *, flow: bool = False) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "level": level,
        "requirement": requirement,
        "authenticationFlow": flow,
        "displayName": name if flow else name.replace("-", " ").title(),
        "providerId": "basic-flow" if flow else name,
    }


def _good_steps(*, settings: str | None = "cfg-level") -> list[dict[str, Any]]:
    level_step = _step(1, "conditional-level-of-authentication", "REQUIRED")
    if settings:
        level_step["authenticationConfig"] = settings
    return [
        _step(0, _LEVEL, "CONDITIONAL", flow=True),
        level_step,
        _step(1, "auth-username-password-form", "REQUIRED"),
        _step(1, _SUB, "CONDITIONAL", flow=True),
        _step(2, "conditional-user-configured", "REQUIRED"),
        _step(2, "auth-otp-form", "REQUIRED"),
    ]


_GOOD_CONFIG = {"loa-condition-level": "2", "loa-max-age": "0"}


def _client(binding: str | None = _FLOW_ID, **fields: Any) -> dict[str, Any]:
    client: dict[str, Any] = {
        "id": "uuid-approve",
        "clientId": "meho-cli-approve",
        "directAccessGrantsEnabled": False,
        "attributes": {"oauth2.device.authorization.grant.enabled": "false"},
        **fields,
    }
    if binding is not None:
        client["authenticationFlowBindingOverrides"] = {"browser": binding}
    return client


def _mount(
    mock: respx.MockRouter,
    *,
    flows: Any = None,
    steps: Any = None,
    clients: Any = None,
    config: Any = None,
    levels: Any = '{"meho-approve":2,"meho-touch":3}',
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
    mock.get(f"{_ADMIN}/authentication/config/cfg-level").mock(
        return_value=config
        if isinstance(config, httpx.Response)
        else httpx.Response(200, json={"config": config if config is not None else _GOOD_CONFIG})
    )
    mock.get(_ADMIN).mock(
        return_value=levels
        if isinstance(levels, httpx.Response)
        else httpx.Response(
            200,
            json={"realm": "meho", "attributes": {} if levels is None else {"acr.loa.map": levels}},
        )
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
            "the flow has 7 steps; wanted 6",
        ),
        (
            [*_good_steps()[:2], _step(1, "identity-provider-redirector", "ALTERNATIVE")],
            "step 3 is 'identity-provider-redirector'",
        ),
        (
            [
                *_good_steps()[:2],
                _step(1, "auth-username-password-form", "ALTERNATIVE"),
                *_good_steps()[3:],
            ],
            "step 3 is 'auth-username-password-form' (ALTERNATIVE",
        ),
        (_good_steps()[:4], "step 5 ('conditional-user-configured') is missing"),
        ([], "step 1 ('meho approval sign-in level - level 2') is missing"),
        (_good_steps()[2:], "step 1 is 'auth-username-password-form'"),
    ],
    ids=[
        "extra session step",
        "other sign-in system",
        "password optional",
        "no code",
        "empty",
        "no level step",
    ],
)
async def test_a_flow_with_other_steps_is_flow_differs(
    steps: list[dict[str, Any]], difference: str
) -> None:
    """Also when the client is bound to it: such a flow never counts as ok."""
    status = await _read(steps=steps)
    assert status.state == "flow_differs"
    assert status.detail is not None and status.detail.startswith(difference)


@pytest.mark.parametrize(
    ("realm", "difference"),
    [
        ({"steps": _good_steps(settings=None)}, "the level step has no settings"),
        (
            {"config": {"loa-condition-level": "1", "loa-max-age": "0"}},
            "the level step has the settings",
        ),
        (
            {"config": {"loa-condition-level": "2", "loa-max-age": "36000"}},
            "the level step has the settings",
        ),
        ({"levels": None}, "the realm attribute acr.loa.map does not name level 2 meho-approve"),
        (
            {"levels": '{"meho-approve":3}'},
            "the realm attribute acr.loa.map does not name level 2 meho-approve",
        ),
        ({"levels": "[2]"}, "the realm attribute acr.loa.map is not a JSON object"),
        (
            {"levels": '{"meho-approve":2,"gold":2}'},
            "the name 'gold' already has level 2 in acr.loa.map",
        ),
        (
            {"clients": [_client(attributes={"acr.loa.map": '{"gold":2}'})]},
            "the client meho-cli-approve has its own acr.loa.map",
        ),
    ],
    ids=[
        "level step without settings",
        "wrong level",
        "level kept for a time",
        "no level map",
        "approval name on another level",
        "level map not an object",
        "another name on level 2",
        "client has its own level map",
    ],
)
async def test_a_realm_that_does_not_give_the_level_is_flow_differs(
    realm: dict[str, Any], difference: str
) -> None:
    """``ok`` means the realm really gives the approval level; anything less is not ok."""
    status = await _read(**realm)
    assert status.state == "flow_differs"
    assert status.detail is not None and status.detail.startswith(difference)


@pytest.mark.parametrize(
    ("client", "detail"),
    [
        (_client(directAccessGrantsEnabled=True), "allows direct grant"),
        (
            _client(attributes={"oauth2.device.authorization.grant.enabled": "true"}),
            "allows device grant",
        ),
    ],
    ids=["direct grant", "device grant"],
)
async def test_a_client_with_another_grant_is_flow_differs(
    client: dict[str, Any], detail: str
) -> None:
    status = await _read(clients=[client])
    assert (status.state, status.detail) == (
        "flow_differs",
        f"the client meho-cli-approve {detail}",
    )


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
        ({"config": httpx.Response(403)}, "no_right_to_read_login_flows"),
        ({"levels": httpx.Response(403)}, "no_right_to_read_realm"),
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
        "level settings 403",
        "realm 403",
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


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="<html>sign in</html>"),
        httpx.Response(200, json=["not", "a", "token"]),
    ],
    ids=["html", "not an object"],
)
async def test_a_bad_token_answer_is_not_checked(answer: httpx.Response) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock)
        mock.post(_TOKEN).mock(return_value=answer)
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


async def test_the_answer_is_kept_for_a_short_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Health is called often: a second call within the time reads Keycloak no more."""
    now = [1000.0]
    monkeypatch.setattr(approval_sign_in_flow.time, "monotonic", lambda: now[0])
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock)
        first = await read_approval_sign_in()
        calls = mock.calls.call_count
        now[0] += approval_sign_in_flow._KEEP_S - 1
        assert await read_approval_sign_in() == first
        assert mock.calls.call_count == calls

        _mount(mock, clients=[_client(None)])
        now[0] += 2
        again = await read_approval_sign_in()
    # After the time, the check reads Keycloak again and sees the change.
    assert (first.state, again.state) == ("ok", "not_bound")


async def test_a_failed_read_is_kept_for_a_few_seconds_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One short error must not hold approvals back for a minute."""
    now = [1000.0]
    monkeypatch.setattr(approval_sign_in_flow.time, "monotonic", lambda: now[0])
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, flows=httpx.Response(500))
        failed = await read_approval_sign_in()
        _mount(mock)
        now[0] += approval_sign_in_flow._KEEP_FAILED_S - 1
        assert await read_approval_sign_in() == failed
        now[0] += 2
        again = await read_approval_sign_in()
    assert approval_sign_in_flow._KEEP_FAILED_S < approval_sign_in_flow._KEEP_S
    assert (failed.state, again.state) == ("not_checked", "ok")


async def test_an_error_nobody_named_is_a_failed_read(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_args: object) -> object:
        raise ValueError("odd")

    monkeypatch.setattr(approval_sign_in_flow, "_state", broken)
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock)
        status = await read_approval_sign_in()
    assert (status.state, status.detail) == ("not_checked", "keycloak_check_error")


# --- auto: a failed read means "refuse" -------------------------------------------


def _approval_sign_in(acr: str) -> Operator:
    return Operator(
        sub="op-6281",
        raw_jwt="<jwt>",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000006281"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        azp="meho-cli-approve",
        acr=acr,
        auth_time=int(time.time()) - 5,
        sid="s-6281",
    )


async def _approve(acr: str) -> ApprovalAuth:
    settings = get_settings()
    level = await sign_in_level_in_force(settings)
    return check_fresh_approval_sign_in(
        _approval_sign_in(acr),
        settings,
        level_in_force=level.in_force,
        level_check_failed=level.check_failed,
    )


async def _refused_detail(acr: str) -> str:
    with pytest.raises(HTTPException) as refused:
        await _approve(acr)
    assert refused.value.status_code == 401
    return str(refused.value.detail)


async def test_auto_refuses_after_a_failed_read_and_follows_the_next_good_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout, unreachable, then ``flow_missing``, then ``ok``, each read in turn."""
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "auto")
    monkeypatch.setattr(approval_sign_in_flow, "_READ_TIMEOUT_S", 0.05)
    get_settings.cache_clear()

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=[])

    def next_read() -> None:
        # The time a kept answer is kept is over (a frozen clock would also stop the event loop).
        approval_sign_in_flow._kept.clear()

    with respx.mock(assert_all_called=False) as mock:
        _mount(mock)
        mock.get(f"{_ADMIN}/authentication/flows").mock(side_effect=slow)
        assert (await _refused_detail("1")).endswith(LEVEL_UNCHECKED_REMEDIATION)

        next_read()
        mock.get(f"{_ADMIN}/authentication/flows").mock(side_effect=httpx.ConnectError("refused"))
        assert (await _refused_detail("1")).endswith(LEVEL_UNCHECKED_REMEDIATION)
        assert (await _approve("meho-approve")).level_in_force == "required"

        next_read()
        _mount(mock, flows=[{"id": "flow-browser", "alias": "browser"}])
        recorded = await _approve("1")
        assert (recorded.level_missing, recorded.level_in_force) == (True, "observe")

        next_read()
        _mount(mock)
        assert (await _refused_detail("1")).endswith(LEVEL_MISSING_REMEDIATION)
        accepted = await _approve("meho-approve")
    assert (accepted.level_missing, accepted.level_in_force) == (False, "required")


def test_an_unresolved_auto_is_recorded_as_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller that passes no *level_in_force* under ``auto``: the audit row says ``required``."""
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "auto")
    get_settings.cache_clear()
    settings = get_settings()

    accepted = check_fresh_approval_sign_in(_approval_sign_in("meho-approve"), settings)

    assert accepted.level_in_force == "required"
    assert accepted.audit_payload("s")["level_in_force"] == "required"
    with pytest.raises(HTTPException):
        check_fresh_approval_sign_in(_approval_sign_in("1"), settings)


@pytest.mark.parametrize(
    ("detail", "fix"),
    [
        ("keycloak_admin_not_configured", "Give MEHO its Keycloak admin client"),
        ("no_right_to_read_login_flows", "the realm-management role view-realm"),
        ("no_right_to_read_realm", "the realm-management role view-realm"),
        ("no_right_to_read_clients", "the realm-management role view-clients"),
    ],
)
def test_the_warning_names_the_fix_when_meho_may_not_read(detail: str, fix: str) -> None:
    """No admin client, or no right to read: the health warning names that fix."""
    status = ApprovalSignInStatus(state="not_checked", client="meho-cli-approve", detail=detail)

    warning = sign_in_level_warning("auto", status)

    assert warning is not None
    assert fix in warning
    assert "Fix the approval sign-in in Keycloak" not in warning


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


@pytest.mark.parametrize(
    ("mode", "realm", "in_force", "warned"),
    [
        ("auto", {}, "required", False),
        ("auto", {"clients": [_client(None)]}, "observe", True),
        ("auto", {"levels": None}, "observe", True),
        ("auto", {"flows": httpx.Response(500)}, "required", True),
        ("auto", {"flows": httpx.Response(403)}, "observe", True),
        ("required", {"clients": [_client(None)]}, "required", False),
        ("observe", {}, "observe", True),
        ("off", {}, "off", True),
    ],
    ids=[
        "auto, realm gives the level",
        "auto, client not bound",
        "auto, no level name",
        "auto, read failed",
        "auto, no right to read",
        "required",
        "observe",
        "off",
    ],
)
async def test_health_warns_whenever_the_level_is_not_required_in_practice(
    mode: str,
    realm: dict[str, Any],
    in_force: str,
    warned: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def vault(*_args: object, **_kwargs: object) -> VaultStatus:
        return VaultStatus(reachable=True, read_ok=True, detail=None)

    monkeypatch.setattr("meho_backplane.api.v1.health._probe_federation", vault)
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", mode)
    get_settings.cache_clear()
    operator = Operator(
        sub="op-6281",
        raw_jwt="<jwt>",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000006281"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, **realm)
        response = await build_health_response(operator)

    assert response.approvals is not None
    assert response.approvals.sign_in_level == mode
    assert response.approvals.sign_in_level_in_force == in_force
    warning = response.approvals.sign_in_level_warning
    assert (warning is not None) is warned
    if warning is not None and in_force == "required":
        assert warning.startswith("MEHO could not check the approval sign-in of the realm")
        assert response.approvals.sign_in is not None
        assert response.approvals.sign_in.state == "not_checked"
    elif warning is not None:
        assert warning.startswith("MEHO does not require the approval level")
