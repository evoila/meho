# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``auto`` for the console's approval client, and what health shows for it (#6282).

``APPROVAL_SIGN_IN_LEVEL=auto`` reads the binding of the console's approval
client ``meho-web-approve``, as it reads ``meho-cli-approve`` for the
command line. A failed read never turns the level off: ``auto`` then
requires it, and ``required`` never reads at all. ``/api/v1/health``
shows both clients and warns when either does not require the level.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.api.v1.health import VaultStatus, build_health_response
from meho_backplane.auth import approval_sign_in_flow
from meho_backplane.auth.approval_sign_in_flow import SignInLevel, sign_in_level_in_force
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.settings import get_settings
from tests.test_auth_approval_sign_in_flow import _ADMIN, _KC, _TOKEN, _client, _mount

_WEB_CLIENT = "meho-web-approve"


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
    monkeypatch.delenv("UI_KEYCLOAK_APPROVE_CLIENT_ID", raising=False)
    get_settings.cache_clear()
    approval_sign_in_flow._kept.clear()
    yield
    get_settings.cache_clear()
    approval_sign_in_flow._kept.clear()


def _web_client(binding: str | None = "flow-approval") -> dict[str, Any]:
    return _client(binding, id="uuid-web-approve", clientId=_WEB_CLIENT)


def _mount_both(mock: respx.MockRouter, *, cli: Any = None, web: Any = None, **realm: Any) -> None:
    """The realm, with both approval clients; ``None`` keeps a bound client."""
    _mount(mock, clients=cli, web_clients=web, **realm)


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", mode)
    get_settings.cache_clear()


# --- auto for the console ----------------------------------------------------


@pytest.mark.parametrize(
    ("web", "expected"),
    [
        ([], SignInLevel("observe")),
        ([_web_client(None)], SignInLevel("observe")),
        ([_web_client("flow-other")], SignInLevel("observe")),
        ([_web_client()], SignInLevel("required")),
    ],
    ids=["client missing", "client not bound", "client bound to another flow", "client bound"],
)
async def test_auto_for_the_console_follows_the_console_clients_binding(
    monkeypatch: pytest.MonkeyPatch, web: list[dict[str, Any]], expected: SignInLevel
) -> None:
    _set_mode(monkeypatch, "auto")
    with respx.mock(assert_all_called=False) as mock:
        _mount_both(mock, web=web)
        level = await sign_in_level_in_force(get_settings(), web=True)

    assert level == expected


async def test_auto_for_the_console_does_not_follow_the_command_line_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, "auto")
    with respx.mock(assert_all_called=False) as mock:
        _mount_both(mock, cli=[_client(None)], web=[_web_client()])
        web = await sign_in_level_in_force(get_settings(), web=True)
        cli = await sign_in_level_in_force(get_settings())

    assert web == SignInLevel("required")
    assert cli == SignInLevel("observe")


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"token": httpx.ConnectError("refused")}, id="keycloak not reachable"),
        pytest.param({"web": httpx.Response(500)}, id="client read fails"),
    ],
)
async def test_auto_for_the_console_requires_the_level_when_keycloak_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, failure: dict[str, Any]
) -> None:
    _set_mode(monkeypatch, "auto")
    with respx.mock(assert_all_called=False) as mock:
        _mount_both(mock, web=failure.get("web"))
        if "token" in failure:
            mock.post(_TOKEN).mock(side_effect=failure["token"])
        level = await sign_in_level_in_force(get_settings(), web=True)

    assert level == SignInLevel("required", check_failed=True)


async def test_required_for_the_console_never_reads_keycloak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, "required")
    with respx.mock(assert_all_called=False) as mock:
        _mount_both(mock, web=[_web_client(None)])
        mock.post(_TOKEN).mock(side_effect=httpx.ConnectError("refused"))
        level = await sign_in_level_in_force(get_settings(), web=True)

    assert level == SignInLevel("required")
    assert not mock.calls


# --- /api/v1/health shows both clients -----------------------------------------


async def _health(monkeypatch: pytest.MonkeyPatch, **realm: Any) -> Any:
    async def vault(*_args: object, **_kwargs: object) -> VaultStatus:
        return VaultStatus(reachable=True, read_ok=True, detail=None)

    monkeypatch.setattr("meho_backplane.api.v1.health._probe_federation", vault)
    operator = Operator(
        sub="op-6282",
        raw_jwt="<jwt>",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000006282"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    with respx.mock(assert_all_called=False) as mock:
        _mount_both(mock, **realm)
        response = await build_health_response(operator)
    assert response.approvals is not None
    return response.approvals


async def test_health_command_line_required_and_console_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, "auto")

    approvals = await _health(monkeypatch, web=[_web_client(None)])

    assert approvals.sign_in.state == "ok"
    assert approvals.sign_in_level_in_force == "required"
    assert approvals.web_sign_in.state == "not_bound"
    assert approvals.web_sign_in.client == _WEB_CLIENT
    assert approvals.web_sign_in_level_in_force == "observe"
    assert approvals.sign_in_level_warning == (
        "The console does not require the approval level: APPROVAL_SIGN_IN_LEVEL is auto and "
        "approvals.web_sign_in is not_bound, not ok. Fix the approval sign-in in Keycloak."
    )


async def test_health_console_required_and_command_line_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, "auto")

    approvals = await _health(monkeypatch, cli=[_client(None)])

    assert approvals.sign_in_level_in_force == "observe"
    assert approvals.web_sign_in.state == "ok"
    assert approvals.web_sign_in_level_in_force == "required"
    warning = approvals.sign_in_level_warning
    assert warning is not None
    assert warning.startswith("MEHO does not require the approval level")
    assert "approvals.web_sign_in" not in warning


async def test_health_both_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_mode(monkeypatch, "auto")

    approvals = await _health(monkeypatch)

    assert approvals.sign_in_level_in_force == "required"
    assert approvals.web_sign_in_level_in_force == "required"
    assert approvals.sign_in_level_warning is None


async def test_health_both_not_required_warns_for_both(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_mode(monkeypatch, "auto")

    approvals = await _health(monkeypatch, cli=[_client(None)], web=[])

    warning = approvals.sign_in_level_warning
    assert warning is not None
    assert warning.startswith("MEHO does not require the approval level")
    assert "approvals.web_sign_in is not_bound, not ok." in warning


async def test_health_warns_when_the_console_client_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, "auto")

    approvals = await _health(monkeypatch, web=httpx.Response(500))

    assert approvals.web_sign_in.state == "not_checked"
    assert approvals.web_sign_in_level_in_force == "required"
    assert approvals.sign_in_level_warning.startswith(
        "MEHO could not check the console approval client"
    )


async def test_health_names_an_empty_console_client_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UI_KEYCLOAK_APPROVE_CLIENT_ID", "")
    _set_mode(monkeypatch, "auto")

    approvals = await _health(monkeypatch)

    assert approvals.web_sign_in.state == "not_bound"
    assert approvals.web_sign_in_level_in_force == "observe"
    assert approvals.sign_in_level_warning.endswith("Set UI_KEYCLOAK_APPROVE_CLIENT_ID.")


@pytest.mark.parametrize("mode", ["required", "observe", "off"])
async def test_health_shows_the_setting_for_both_clients_outside_auto(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _set_mode(monkeypatch, mode)

    approvals = await _health(monkeypatch, web=[_web_client(None)])

    assert approvals.sign_in_level_in_force == mode
    assert approvals.web_sign_in_level_in_force == mode
    warning = approvals.sign_in_level_warning
    assert (warning is None) is (mode == "required")
    assert warning is None or "approvals.web_sign_in" not in warning
