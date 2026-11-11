# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

""" "Sign out this tool" against a real Keycloak: only that tool stops renewing.

The account page ends the logins of one (person, login client) pair with
:func:`~meho_backplane.auth.tool_sign_out.end_tool_logins`. This test runs
it against the Keycloak of :func:`keycloak_bootstrap` and then tries to
renew each login with its refresh token:

1. Two people sign in with the login clients ``meho-mcp-desktop``,
   ``meho-mcp-code`` and ``meho-cli``. Desktop and Code sign in twice:
   an ordinary login and an offline login (scope ``offline_access``). The
   command line signs in with an ordinary login only, like
   ``meho login --offline=false``.
2. Sign out Desktop of the first person. Both Desktop logins of that
   person can no longer renew. Their Code and command line logins, and
   both Desktop logins of the other person, still renew.
3. Sign out the command line of the first person. It had no offline
   login, so Keycloak's consent call finds nothing, but its ordinary login
   ends. Code still renews.

A password login makes a new Keycloak login for each sign-in, so no login
here is shared by two clients. The unit tests in
``tests/test_ui_account_tools.py`` cover a shared login.

Skips when ``MEHO_TEST_KEYCLOAK_IMAGE`` is unset or Docker is missing (CI
provides both).
"""

from __future__ import annotations

import asyncio

import httpx

from meho_backplane.auth.keycloak_admin import KeycloakAdminClient
from meho_backplane.auth.tool_sign_out import ToolLoginsEnded, end_tool_logins
from tests.integration.conftest import (
    _KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD,
    _KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME,
    KeycloakBootstrap,
)

_DESKTOP = "meho-mcp-desktop"
_CODE = "meho-mcp-code"
_CLI = "meho-cli"
# Throwaway password: only ever used inside the test container.
_PASSWORD = "tool-sign-out-pass-test"
_HTTP_TIMEOUT = httpx.Timeout(60.0)


def _master_headers(kc: KeycloakBootstrap) -> dict[str, str]:
    resp = httpx.post(
        f"{kc.base_url}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": _KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME,
            "password": _KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD,
        },
        timeout=_HTTP_TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _client_uuid(kc: KeycloakBootstrap, headers: dict[str, str], client_id: str) -> str:
    resp = httpx.get(
        f"{kc.admin_url}/clients",
        params={"clientId": client_id},
        headers=headers,
        timeout=_HTTP_TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()[0]["id"])


def _set_up_realm(kc: KeycloakBootstrap) -> dict[str, str]:
    """Create the three login clients and two people; let meho-admin manage users.

    Returns the Keycloak user id of each person.
    """
    headers = _master_headers(kc)
    for client_id in (_DESKTOP, _CODE, _CLI):
        resp = httpx.post(
            f"{kc.admin_url}/clients",
            headers=headers,
            json={
                "clientId": client_id,
                "publicClient": True,
                "directAccessGrantsEnabled": True,
                "standardFlowEnabled": False,
            },
            timeout=_HTTP_TIMEOUT,
        )
        assert resp.status_code == 201, resp.text
    user_ids: dict[str, str] = {}
    for name in ("tool-person-a", "tool-person-b"):
        resp = httpx.post(
            f"{kc.admin_url}/users",
            headers=headers,
            json={
                "username": name,
                "enabled": True,
                "email": f"{name}@example.com",
                "emailVerified": True,
                "firstName": "Tool",
                "lastName": "Person",
                "credentials": [{"type": "password", "value": _PASSWORD, "temporary": False}],
            },
            timeout=_HTTP_TIMEOUT,
        )
        assert resp.status_code == 201, resp.text
        found = httpx.get(
            f"{kc.admin_url}/users",
            params={"username": name, "exact": "true"},
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        )
        assert found.status_code == 200, found.text
        user_ids[name] = str(found.json()[0]["id"])

    # The admin client of the account page needs manage-users.
    realm_management = _client_uuid(kc, headers, "realm-management")
    role = httpx.get(
        f"{kc.admin_url}/clients/{realm_management}/roles/manage-users",
        headers=headers,
        timeout=_HTTP_TIMEOUT,
    )
    assert role.status_code == 200, role.text
    admin_client = _client_uuid(kc, headers, kc.admin_client_id)
    account = httpx.get(
        f"{kc.admin_url}/clients/{admin_client}/service-account-user",
        headers=headers,
        timeout=_HTTP_TIMEOUT,
    )
    assert account.status_code == 200, account.text
    granted = httpx.post(
        f"{kc.admin_url}/users/{account.json()['id']}/role-mappings/clients/{realm_management}",
        headers=headers,
        json=[role.json()],
        timeout=_HTTP_TIMEOUT,
    )
    assert granted.status_code == 204, granted.text
    return user_ids


def _sign_in(kc: KeycloakBootstrap, username: str, client_id: str, *, offline: bool) -> str:
    """Sign in with a password; return the refresh token."""
    data = {
        "grant_type": "password",
        "client_id": client_id,
        "username": username,
        "password": _PASSWORD,
        "scope": "openid offline_access" if offline else "openid",
    }
    resp = httpx.post(
        f"{kc.issuer_url}/protocol/openid-connect/token", data=data, timeout=_HTTP_TIMEOUT
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["refresh_token"])


def _renew(kc: KeycloakBootstrap, client_id: str, refresh_token: str) -> httpx.Response:
    return httpx.post(
        f"{kc.issuer_url}/protocol/openid-connect/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        timeout=_HTTP_TIMEOUT,
    )


def _end(kc: KeycloakBootstrap, user_id: str, client_id: str) -> ToolLoginsEnded:
    async def _do() -> ToolLoginsEnded:
        admin = KeycloakAdminClient(
            admin_url=kc.admin_url,
            token_url=f"{kc.issuer_url}/protocol/openid-connect/token",
            client_id=kc.admin_client_id,
            client_secret=kc.admin_client_secret,
        )
        async with admin:
            return await end_tool_logins(admin, user_id=user_id, client_id=client_id)

    return asyncio.run(_do())


def test_sign_out_one_tool_stops_only_that_tool(keycloak_bootstrap: KeycloakBootstrap) -> None:
    kc = keycloak_bootstrap
    users = _set_up_realm(kc)
    a, b = "tool-person-a", "tool-person-b"
    tokens = {
        (a, _DESKTOP, "ordinary"): _sign_in(kc, a, _DESKTOP, offline=False),
        (a, _DESKTOP, "offline"): _sign_in(kc, a, _DESKTOP, offline=True),
        (a, _CODE, "ordinary"): _sign_in(kc, a, _CODE, offline=False),
        (a, _CODE, "offline"): _sign_in(kc, a, _CODE, offline=True),
        (a, _CLI, "ordinary"): _sign_in(kc, a, _CLI, offline=False),
        (b, _DESKTOP, "ordinary"): _sign_in(kc, b, _DESKTOP, offline=False),
        (b, _DESKTOP, "offline"): _sign_in(kc, b, _DESKTOP, offline=True),
    }
    for (_person, client_id, _kind), token in tokens.items():
        resp = _renew(kc, client_id, token)
        assert resp.status_code == 200, resp.text

    ended = _end(kc, users[a], _DESKTOP)
    assert ended.offline_login_ended is True
    assert ended.sessions_ended >= 1
    assert ended.shared_with == ()

    for (person, client_id, kind), token in tokens.items():
        resp = _renew(kc, client_id, token)
        if (person, client_id) == (a, _DESKTOP):
            assert resp.status_code == 400, f"{kind} Desktop login still renews: {resp.text}"
        else:
            assert resp.status_code == 200, f"{person} {client_id} {kind}: {resp.text}"

    ended = _end(kc, users[a], _CLI)
    assert ended.offline_login_ended is False
    assert ended.sessions_ended == 1
    resp = _renew(kc, _CLI, tokens[(a, _CLI, "ordinary")])
    assert resp.status_code == 400, resp.text
    resp = _renew(kc, _CODE, tokens[(a, _CODE, "ordinary")])
    assert resp.status_code == 200, resp.text
