# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""#4590 — the login role groups work on the Keycloak MEHO ships.

The Keycloak subchart imports
``deploy/charts/meho/charts/keycloak/files/meho-realm.json`` and the realm
stage of the ``bootstrap`` Job makes the first two people (as in
``test_core_keycloak_realm_import.py`` and ``test_core_bootstrap_keycloak.py``).
Against that real Keycloak:

1. A new person in exactly one of the five role groups logs in and gets
   that group's ``tenant_id``, ``tenant_role`` and ``approver``; the
   backplane's own JWT check accepts the token with the same role.
2. Once the person leaves the group, the next login has no ``tenant_id``
   and MEHO answers ``401 missing_tenant_claim``. A refresh of the session
   from before the change loses the claim too: Keycloak runs the client's
   mappers again on every refresh, with the person's current groups.
3. The first admin is in MEHO-Admins and MEHO-Platform-Admins; the second
   person is in MEHO-SA-Operators, which is ``operator``, so the approvals
   plane lets them decide the admin's requests. (The full ask-and-approve
   path through the REST API runs on the built-in realm in
   ``test_core_bootstrap_keycloak.py``.)

The login is the password grant on ``meho-cli``, switched on by this test
only (people use the device flow); the client's mappers are untouched.

It uses the subchart's image (``MEHO_TEST_CORE_KEYCLOAK_IMAGE`` for a
mirror) and skips when Docker is missing.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_approvals_access
from meho_backplane.core.keycloak import (
    ADMIN_SECRET_ENV,
    BOOTSTRAP_ID_ENV,
    BOOTSTRAP_SECRET_ENV,
    HttpAdmin,
    finish_realm,
)
from meho_backplane.settings import get_settings
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON
from tests.integration.test_core_bootstrap_keycloak import _FakeStore, _plan
from tests.integration.test_core_keycloak_realm_import import _subchart_image, _wait_for_realm

_REALM_FILE = (
    Path(__file__).resolve().parents[3]
    / "deploy"
    / "charts"
    / "meho"
    / "charts"
    / "keycloak"
    / "files"
    / "meho-realm.json"
)
_PUBLIC_URL = "https://meho.example.test"
_TENANT_ID = "6b0d7c55-41a8-4b52-9a8e-2f3c1d0e9a77"
_BACKPLANE_AUDIENCE = "meho-backplane"
_LOGIN_CLIENT = "meho-cli"

# The five role groups and what a member must get (docs/decisions/login-role-groups.md).
_ROLE_GROUPS = {
    "MEHO-Readers": (TenantRole.READ_ONLY, False),
    "MEHO-Operators": (TenantRole.OPERATOR, False),
    "MEHO-SA-Operators": (TenantRole.OPERATOR, False),
    "MEHO-Admins": (TenantRole.TENANT_ADMIN, False),
    "MEHO-Approvers": (TenantRole.READ_ONLY, True),
}


@dataclass
class _Site:
    """One Keycloak realm set up the way MEHO sets it up."""

    base_url: str
    realm: str
    headers: Callable[[], dict[str, str]]
    admin_username: str
    admin_password: str
    approver_username: str
    approver_password: str

    @property
    def admin_url(self) -> str:
        return f"{self.base_url}/admin/realms/{self.realm}"

    @property
    def issuer(self) -> str:
        return f"{self.base_url}/realms/{self.realm}"


# --- Keycloak Admin API helpers ----------------------------------------------


def _token(site: _Site, data: dict[str, str]) -> httpx.Response:
    return httpx.post(f"{site.issuer}/protocol/openid-connect/token", data=data)


def _claims(access_token: str) -> dict[str, Any]:
    """The token's payload, unverified (the backplane check verifies it)."""
    payload = access_token.split(".")[1]
    return dict(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))))


def _group_id(site: _Site, name: str) -> str:
    groups = httpx.get(
        f"{site.admin_url}/groups", headers=site.headers(), params={"search": name}
    ).json()
    return str(next(g["id"] for g in groups if g["name"] == name))


def _user_id(site: _Site, username: str) -> str:
    users = httpx.get(
        f"{site.admin_url}/users",
        headers=site.headers(),
        params={"username": username, "exact": "true"},
    ).json()
    assert len(users) == 1, username
    return str(users[0]["id"])


def _user_groups(site: _Site, username: str) -> set[str]:
    resp = httpx.get(
        f"{site.admin_url}/users/{_user_id(site, username)}/groups", headers=site.headers()
    )
    resp.raise_for_status()
    return {g["name"] for g in resp.json()}


def _add_person(site: _Site, username: str, group: str) -> tuple[str, str]:
    """Make a person with a full profile, only in *group*; return (id, password)."""
    password = secrets.token_urlsafe(18)
    httpx.post(
        f"{site.admin_url}/users",
        headers=site.headers(),
        json={
            "username": username,
            "enabled": True,
            "email": f"{username}@example.com",
            "emailVerified": True,
            "firstName": "Test",
            "lastName": username,
            "credentials": [{"type": "password", "value": password, "temporary": False}],
        },
    ).raise_for_status()
    user_id = _user_id(site, username)
    httpx.put(
        f"{site.admin_url}/users/{user_id}/groups/{_group_id(site, group)}",
        headers=site.headers(),
    ).raise_for_status()
    assert _user_groups(site, username) == {group}
    return user_id, password


def _allow_password_grant_on_cli(site: _Site) -> None:
    client = httpx.get(
        f"{site.admin_url}/clients", headers=site.headers(), params={"clientId": _LOGIN_CLIENT}
    ).json()[0]
    client["directAccessGrantsEnabled"] = True
    httpx.put(
        f"{site.admin_url}/clients/{client['id']}", headers=site.headers(), json=client
    ).raise_for_status()


def _finish_first_login(site: _Site, username: str) -> None:
    """What the person's first browser login does, minus a new password.

    Keycloak refuses a direct login while a required action is pending or
    the profile is incomplete ("Account is not fully set up").
    """
    user_id = _user_id(site, username)
    user = httpx.get(f"{site.admin_url}/users/{user_id}", headers=site.headers()).json()
    user["requiredActions"] = []
    user.setdefault("firstName", "Test")
    user.setdefault("lastName", username)
    user.setdefault("email", f"{username}@example.com")
    httpx.put(
        f"{site.admin_url}/users/{user_id}", headers=site.headers(), json=user
    ).raise_for_status()


def _password_login(site: _Site, username: str, password: str) -> dict[str, Any]:
    resp = _token(
        site,
        {
            "grant_type": "password",
            "client_id": _LOGIN_CLIENT,
            "username": username,
            "password": password,
            "scope": "openid",
        },
    )
    assert resp.status_code == 200, f"{username}: {resp.text}"
    return dict(resp.json())


async def _verify(site: _Site, access_token: str, monkeypatch: pytest.MonkeyPatch) -> Operator:
    """Check *access_token* with the backplane's own JWT check."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", site.issuer)
    get_settings.cache_clear()
    clear_jwks_cache()
    return await verify_jwt_for_audience(
        f"Bearer {access_token}", expected_audience=_BACKPLANE_AUDIENCE
    )


# --- The site ------------------------------------------------------------------


@pytest.fixture(scope="module")
def site() -> Iterator[_Site]:
    """The subchart's realm file imported, then the bootstrap Job's realm stage."""
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    import asyncio

    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

    env = {
        BOOTSTRAP_ID_ENV: "meho-bootstrap",
        BOOTSTRAP_SECRET_ENV: secrets.token_hex(16),
        "MEHO_REALM_PUBLIC_URL": _PUBLIC_URL,
        "MEHO_REALM_TENANT_ID": _TENANT_ID,
        ADMIN_SECRET_ENV: secrets.token_hex(16),
        "MEHO_REALM_WEB_CLIENT_SECRET": secrets.token_hex(16),
        "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET": secrets.token_hex(16),
    }
    image = os.environ.get("MEHO_TEST_CORE_KEYCLOAK_IMAGE") or _subchart_image()
    container = (
        DockerContainer(image)
        .with_command("start-dev --import-realm")
        .with_env("KC_HTTP_RELATIVE_PATH", "/auth")
        .with_env("KC_BOOTSTRAP_ADMIN_CLIENT_ID", env[BOOTSTRAP_ID_ENV])
        .with_env("KC_BOOTSTRAP_ADMIN_CLIENT_SECRET", env[BOOTSTRAP_SECRET_ENV])
        .with_env("MEHO_REALM_PUBLIC_URL", _PUBLIC_URL)
        .with_env("MEHO_REALM_TENANT_ID", _TENANT_ID)
        .with_env("MEHO_REALM_ADMIN_CLIENT_SECRET", env[ADMIN_SECRET_ENV])
        .with_env("MEHO_REALM_WEB_CLIENT_SECRET", env["MEHO_REALM_WEB_CLIENT_SECRET"])
        .with_env(
            "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET", env["MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET"]
        )
        .with_volume_mapping(str(_REALM_FILE.parent), "/opt/keycloak/data/import", "ro")
        .with_exposed_ports(8080)
    )
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"Keycloak container failed to start ({type(exc).__name__}): {exc}")
    try:
        wait_for_log_message(container, "Listening on:", timeout=240)
        host = container.get_container_host_ip()
        base_url = f"http://{host}:{container.get_exposed_port(8080)}/auth"
        _wait_for_realm(base_url)

        store = _FakeStore()

        async def run_stage() -> None:
            async with httpx.AsyncClient(base_url=base_url, timeout=30) as http:
                await finish_realm(_plan(), env, HttpAdmin(http), store)

        asyncio.run(run_stage())
        first_login = store.secrets["rel-core-first-login"]

        def headers() -> dict[str, str]:
            resp = httpx.post(
                f"{base_url}/realms/meho/protocol/openid-connect/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": "meho-admin",
                    "client_secret": env[ADMIN_SECRET_ENV],
                },
            )
            resp.raise_for_status()
            return {"Authorization": f"Bearer {resp.json()['access_token']}"}

        site = _Site(
            base_url=base_url,
            realm="meho",
            headers=headers,
            admin_username=first_login["admin_username"],
            admin_password=first_login["admin_password"],
            approver_username=first_login["approver_username"],
            approver_password=first_login["approver_password"],
        )
        _allow_password_grant_on_cli(site)
        yield site
    finally:
        container.stop()


# --- 1. Each group gives its tenant and role ---------------------------------


@pytest.mark.parametrize("group", list(_ROLE_GROUPS))
async def test_member_of_one_group_gets_its_tenant_and_role(
    site: _Site, group: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    want_role, want_approver = _ROLE_GROUPS[group]
    username = f"member-{group.lower()}"
    _, password = _add_person(site, username, group)

    access_token = _password_login(site, username, password)["access_token"]
    claims = _claims(access_token)
    assert claims.get("tenant_id") == _TENANT_ID
    assert claims.get("tenant_role") == want_role.value
    if want_approver:
        assert claims.get("approver") is True
    else:
        assert "approver" not in claims
    assert "platform_admin" not in claims

    operator = await _verify(site, access_token, monkeypatch)
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role is want_role
    assert operator.approver is want_approver
    assert operator.platform_admin is False


# --- 2. Leaving the group takes the access away -------------------------------


async def test_leaving_the_group_removes_the_tenant_from_new_logins_and_refreshes(
    site: _Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    username = "leaver"
    user_id, password = _add_person(site, username, "MEHO-Operators")

    before = _password_login(site, username, password)
    operator = await _verify(site, before["access_token"], monkeypatch)
    assert operator.tenant_role is TenantRole.OPERATOR

    httpx.delete(
        f"{site.admin_url}/users/{user_id}/groups/{_group_id(site, 'MEHO-Operators')}",
        headers=site.headers(),
    ).raise_for_status()
    assert _user_groups(site, username) == set()

    # A new login has no tenant, and MEHO refuses it.
    after = _password_login(site, username, password)
    claims = _claims(after["access_token"])
    for claim in ("tenant_id", "tenant_role", "approver"):
        assert claim not in claims, claim
    with pytest.raises(HTTPException) as refused:
        await _verify(site, after["access_token"], monkeypatch)
    assert refused.value.status_code == 401
    assert refused.value.detail == "missing_tenant_claim"

    # The session from before the change: Keycloak accepts the refresh (the
    # session is still valid) but builds the new access token from the
    # person's current groups, so the tenant is gone there too. The access
    # token issued before the change stays valid until it expires.
    refreshed = _token(
        site,
        {
            "grant_type": "refresh_token",
            "client_id": _LOGIN_CLIENT,
            "refresh_token": before["refresh_token"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    claims = _claims(refreshed.json()["access_token"])
    for claim in ("tenant_id", "tenant_role", "approver"):
        assert claim not in claims, claim
    with pytest.raises(HTTPException) as refused:
        await _verify(site, refreshed.json()["access_token"], monkeypatch)
    assert refused.value.detail == "missing_tenant_claim"


# --- 3. The two people the setup makes ---------------------------------------


async def test_first_admin_and_second_person_get_their_groups_and_roles(
    site: _Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _user_groups(site, site.admin_username) == {"MEHO-Admins", "MEHO-Platform-Admins"}
    assert _user_groups(site, site.approver_username) == {"MEHO-SA-Operators"}

    _finish_first_login(site, site.admin_username)
    _finish_first_login(site, site.approver_username)

    admin_token = _password_login(site, site.admin_username, site.admin_password)
    admin = await _verify(site, admin_token["access_token"], monkeypatch)
    assert str(admin.tenant_id) == _TENANT_ID
    assert admin.tenant_role is TenantRole.TENANT_ADMIN
    assert admin.platform_admin is True
    assert admin.approver is False

    approver_token = _password_login(site, site.approver_username, site.approver_password)
    approver = await _verify(site, approver_token["access_token"], monkeypatch)
    assert str(approver.tenant_id) == _TENANT_ID
    assert approver.tenant_role is TenantRole.OPERATOR
    assert approver.platform_admin is False
    assert approver.approver is False
    assert approver.sub != admin.sub, "two people"
    # ``operator`` clears the approvals plane, so the second person can decide
    # the first admin's requests (the self-approval refusal is per ``sub``).
    assert require_approvals_access(approver) is approver
