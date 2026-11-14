# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""#4043 — the realm stage of the ``bootstrap`` Job against a real Keycloak.

Keycloak starts with the subchart's realm file imported, as in
``test_core_keycloak_realm_import.py``, with the first-start account the
chart gives it. Then the realm stage runs twice against it, exactly as the
Job runs it, with a fake Secret store in place of the Kubernetes API:

1. The first run logs in with the first-start account, makes the first admin
   and the second person, sets the client secrets and removes the
   first-start account.
2. Keycloak asks the first admin for a new password before any login. Once
   that required action is done, the password from the Secret is the one
   Keycloak holds, and a login through ``meho-cli`` gives a token the
   backplane accepts as a ``tenant_admin`` of the first tenant with
   ``platform_admin``. The second person is an ``operator``.
3. With the two logins, the first admin asks for a credential write through
   the backplane's REST API. It waits for approval, the first admin cannot
   approve it alone, and the second person approves it through the
   approvals API, which runs the write. The second person's password-grant
   token alone is refused: it shows no sign-in time (#3979).
4. The plan names one extra tenant. Its five groups carry only
   ``meho_tenant``: a person in ``MEHO-Readers`` and
   ``MEHO-lab-b-Operators`` gets both tenants in ``meho_tenants`` and the
   first tenant's ``tenant_id`` as before; a person only in the extra
   tenant's groups gets no ``tenant_id``.
5. The first run also made the console admin: a user of realm ``master``
   with every role of the master client ``meho-realm``. With a password it
   can add an identity provider and an LDAP user federation to realm
   ``meho`` (what the company-login recipes need), MEHO refuses its token,
   and ``meho-admin`` can neither find it nor reset its password.
6. The second run logs in as ``meho-admin`` and changes nothing: the realm
   and the Secrets are the same, and the output holds no secret.
7. On a realm whose ``meho-cli`` has no long login (as before #5516), the
   next run adds ``offline_access`` and the 48-hour idle limit, and a login
   that asks for ``offline_access`` then gets an offline token. When an
   admin revokes it (the person's Consents tab), its renewal is refused.
8. On an install without the console admin's Secret, a run as
   ``meho-admin`` makes nothing in realm ``master``, says how to add the
   console admin by hand and does not fail.

The container also has a master admin user of its own (``_INSPECTOR``) so
the tests can look at realm ``master`` after the first-start account is
gone; the stage never uses it.

Set ``MEHO_TEST_CORE_KEYCLOAK_IMAGE`` to use a mirror of the subchart's
image. Skips when Docker is missing or the container cannot start.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi import HTTPException
from httpx import ASGITransport

from meho_backplane.auth.approval_sign_in import ApprovalAuth
from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.core.keycloak import (
    ADMIN_SECRET_ENV,
    BOOTSTRAP_ID_ENV,
    BOOTSTRAP_SECRET_ENV,
    HttpAdmin,
    Plan,
    RealmStageError,
    TenantPlan,
    finish_realm,
)
from meho_backplane.core.keycloak_recovery import ADD_RECOVERY_CODES, RECOVERY_STEP
from meho_backplane.core.keycloak_role_check import check as check_role
from meho_backplane.operations import (
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.settings import get_settings
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

_SUBCHART = (
    Path(__file__).resolve().parents[3] / "deploy" / "charts" / "meho" / "charts" / "keycloak"
)
_PUBLIC_URL = "https://meho.example.test"
_TENANT_ID = "6b0d7c55-41a8-4b52-9a8e-2f3c1d0e9a77"
_LAB_B_ID = "5d0f7a3e-1c2b-4e8d-9f6a-7b3c2d1e0f9a"
_INSPECTOR = "test-inspector"
_INSPECTOR_PASSWORD = secrets.token_urlsafe(24)


@dataclass
class _Keycloak:
    base_url: str
    env: dict[str, str]
    first_login: dict[str, str] = field(default_factory=dict)
    output: str = ""
    # The secret of each person's code app, and the last code sent for it:
    # Keycloak takes a code only once.
    code_secrets: dict[str, str] = field(default_factory=dict)
    last_code: dict[str, int] = field(default_factory=dict)


class _FakeStore:
    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, str]] = {}
        self.created: list[str] = []

    async def read_data(self, name: str) -> dict[str, str] | None:
        data = self.secrets.get(name)
        return None if data is None else dict(data)

    async def create(self, name: str, data: Mapping[str, str], secret_type: str) -> bool:
        if name in self.secrets:
            return False
        self.secrets[name] = dict(data)
        self.created.append(name)
        return True


def _subchart_image() -> str:
    image = yaml.safe_load((_SUBCHART / "values.yaml").read_text())["image"]
    return f"{image['repository']}@{image['digest']}"


def _wait_for_realm(base_url: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if httpx.get(f"{base_url}/realms/meho/.well-known/openid-configuration").is_success:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            pytest.fail("Keycloak did not serve realm meho in time")
        time.sleep(2)


def _plan() -> Plan:
    return Plan(
        keycloak_url="unused-here",
        realm_file=str(_SUBCHART / "files" / "meho-realm.json"),
        first_login_secret="rel-core-first-login",
        admin_username="admin",
        approver_username="approver",
        tenants=(TenantPlan(name="lab-b", id=UUID(_LAB_B_ID)),),
        console_admin_secret="rel-core-console-admin",
        console_admin_username="console-admin",
    )


@pytest.fixture(scope="module")
def keycloak() -> Iterator[_Keycloak]:
    """Start Keycloak with the realm file imported and the first-start account."""
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

    env = {
        BOOTSTRAP_ID_ENV: "meho-bootstrap",
        BOOTSTRAP_SECRET_ENV: secrets.token_hex(16),
        "MEHO_REALM_PUBLIC_URL": _PUBLIC_URL,
        "MEHO_REALM_TENANT_ID": _TENANT_ID,
        "MEHO_REALM_TENANT_NAME": "main",
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
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", _INSPECTOR)
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", _INSPECTOR_PASSWORD)
        .with_env("MEHO_REALM_PUBLIC_URL", _PUBLIC_URL)
        .with_env("MEHO_REALM_TENANT_ID", _TENANT_ID)
        .with_env("MEHO_REALM_TENANT_NAME", "main")
        .with_env("MEHO_REALM_ADMIN_CLIENT_SECRET", env[ADMIN_SECRET_ENV])
        .with_env("MEHO_REALM_WEB_CLIENT_SECRET", env["MEHO_REALM_WEB_CLIENT_SECRET"])
        .with_env(
            "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET", env["MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET"]
        )
        .with_volume_mapping(str(_SUBCHART / "files"), "/opt/keycloak/data/import", "ro")
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
        yield _Keycloak(base_url=base_url, env=env)
    finally:
        container.stop()


@pytest.fixture(scope="module")
def store() -> _FakeStore:
    return _FakeStore()


async def _run_stage(kc: _Keycloak, store: _FakeStore) -> None:
    async with httpx.AsyncClient(base_url=kc.base_url, timeout=30) as http:
        await finish_realm(_plan(), kc.env, HttpAdmin(http), store)


def _token(kc: _Keycloak, realm: str, data: dict[str, str]) -> httpx.Response:
    return httpx.post(f"{kc.base_url}/realms/{realm}/protocol/openid-connect/token", data=data)


def _admin_headers(kc: _Keycloak) -> dict[str, str]:
    resp = _token(
        kc,
        "meho",
        {
            "grant_type": "client_credentials",
            "client_id": "meho-admin",
            "client_secret": kc.env[ADMIN_SECRET_ENV],
        },
    )
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _master_headers(kc: _Keycloak) -> dict[str, str]:
    """The test's own master admin, to look at realm master."""
    resp = _token(
        kc,
        "master",
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": _INSPECTOR,
            "password": _INSPECTOR_PASSWORD,
        },
    )
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _master_users(kc: _Keycloak, username: str | None = None) -> list[dict[str, Any]]:
    params = {"username": username, "exact": "true"} if username else {}
    users: list[dict[str, Any]] = httpx.get(
        f"{kc.base_url}/admin/realms/master/users", headers=_master_headers(kc), params=params
    ).json()
    return users


def _user(kc: _Keycloak, username: str) -> dict[str, Any]:
    admin = f"{kc.base_url}/admin/realms/meho"
    users = httpx.get(
        f"{admin}/users",
        headers=_admin_headers(kc),
        params={"username": username, "exact": "true"},
    ).json()
    assert len(users) == 1, username
    user: dict[str, Any] = users[0]
    user["groups"] = [
        g["name"]
        for g in httpx.get(f"{admin}/users/{user['id']}/groups", headers=_admin_headers(kc)).json()
    ]
    return user


async def test_first_run_makes_the_people_and_removes_the_first_start_account(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    await _run_stage(keycloak, store)
    keycloak.output = capfd.readouterr().out
    keycloak.first_login = dict(store.secrets["rel-core-first-login"])

    assert "login: first-start account" in keycloak.output
    assert "first-start account: removed" in keycloak.output
    refused = _token(
        keycloak,
        "master",
        {
            "grant_type": "client_credentials",
            "client_id": keycloak.env[BOOTSTRAP_ID_ENV],
            "client_secret": keycloak.env[BOOTSTRAP_SECRET_ENV],
        },
    )
    assert refused.status_code in (400, 401), refused.text

    admin = _user(keycloak, "admin")
    assert admin["enabled"] is True
    assert sorted(admin["requiredActions"]) == ["CONFIGURE_TOTP", "UPDATE_PASSWORD"]
    assert set(admin["groups"]) == {"MEHO-Admins", "MEHO-Platform-Admins"}
    approver = _user(keycloak, "approver")
    assert approver["groups"] == ["MEHO-SA-Operators"]
    assert sorted(approver["requiredActions"]) == ["CONFIGURE_TOTP", "UPDATE_PASSWORD"]
    assert "recovery codes: set" in keycloak.output
    assert _recovery_settings(keycloak) == ("true", "ALTERNATIVE")


_AUTHENTICATION = "/admin/realms/meho/authentication"


def _recovery_settings(kc: _Keycloak) -> tuple[str | None, str]:
    """The "Add recovery codes" option of "Configure OTP" and the recovery step's requirement."""
    headers = _master_headers(kc)
    action = httpx.get(
        f"{kc.base_url}{_AUTHENTICATION}/required-actions/CONFIGURE_TOTP", headers=headers
    ).json()
    steps = httpx.get(f"{kc.base_url}{_AUTHENTICATION}/flows/browser/executions", headers=headers)
    step = next(s for s in steps.json() if s.get("providerId") == RECOVERY_STEP)
    return (action.get("config") or {}).get(ADD_RECOVERY_CODES), step["requirement"]


def _allow_password_grant_on_cli(kc: _Keycloak) -> None:
    admin = f"{kc.base_url}/admin/realms/meho"
    headers = _admin_headers(kc)
    client = httpx.get(f"{admin}/clients", headers=headers, params={"clientId": "meho-cli"}).json()[
        0
    ]
    client["directAccessGrantsEnabled"] = True
    httpx.put(f"{admin}/clients/{client['id']}", headers=headers, json=client).raise_for_status()


def _code_at(secret: str, counter: int) -> str:
    """The code an authenticator app shows in 30-second step *counter* (TOTP,
    SHA-1, 6 digits; Keycloak takes the secret's bytes as the key)."""
    digest = hmac.new(secret.encode(), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def _one_time_code(kc: _Keycloak, username: str) -> str:
    """The person's current code; waits for the next one when this one was sent already."""
    counter = int(time.time()) // 30
    if kc.last_code.get(username) == counter:
        time.sleep(30 - time.time() % 30 + 1)
        counter = int(time.time()) // 30
    kc.last_code[username] = counter
    return _code_at(kc.code_secrets[username], counter)


def _password_login(
    kc: _Keycloak, username: str, password: str, code: str | None = None
) -> httpx.Response:
    data = {
        "grant_type": "password",
        "client_id": "meho-cli",
        "username": username,
        "password": password,
        "scope": "openid",
    }
    if code is not None:
        data["totp"] = code
    return _token(kc, "meho", data)


def _login_with_code(kc: _Keycloak, username: str, password: str) -> httpx.Response:
    return _password_login(kc, username, password, _one_time_code(kc, username))


def _finish_first_login(kc: _Keycloak, username: str) -> dict[str, Any]:
    """What the browser's first login does, minus the new password.

    Keycloak refuses a direct login while a required action is pending or
    the profile is incomplete ("Account is not fully set up"): the realm's
    user profile wants a first name, a last name and an email, which the
    person types in at the first browser login. The person also sets up a
    code app: here a code secret made up by the test, kept in *kc*.
    """
    admin = f"{kc.base_url}/admin/realms/meho"
    headers = _admin_headers(kc)
    user = _user(kc, username)
    code_secret = secrets.token_urlsafe(20)
    user_rep = httpx.get(f"{admin}/users/{user['id']}", headers=headers).json()
    user_rep["requiredActions"] = []
    user_rep.update({"firstName": "Test", "lastName": username, "email": f"{username}@example.com"})
    user_rep["credentials"] = [
        {
            "type": "otp",
            "userLabel": "authenticator app",
            "secretData": json.dumps({"value": code_secret}),
            "credentialData": json.dumps(
                {
                    "subType": "totp",
                    "digits": 6,
                    "counter": 0,
                    "period": 30,
                    "algorithm": "HmacSHA1",
                }
            ),
        }
    ]
    httpx.put(f"{admin}/users/{user['id']}", headers=headers, json=user_rep).raise_for_status()
    kc.code_secrets[username] = code_secret
    return user


def _check_the_code_is_needed(kc: _Keycloak, username: str, password: str) -> None:
    """The password alone, or with a wrong code, does not log the person in.

    The pauses keep two failed logins more than a second apart: closer ones
    make Keycloak's brute force guard lock the account for a minute.
    """
    old_code = _code_at(kc.code_secrets[username], int(time.time()) // 30 - 120)
    for code in (None, old_code):
        time.sleep(1.5)
        refused = _password_login(kc, username, password, code)
        assert refused.status_code in (400, 401), refused.text
        assert refused.json()["error"] == "invalid_grant", refused.text


async def _operator_for(
    kc: _Keycloak, username: str, password: str, monkeypatch: pytest.MonkeyPatch
) -> Any:
    resp = _login_with_code(kc, username, password)
    assert resp.status_code == 200, resp.text
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{kc.base_url}/realms/meho")
    get_settings.cache_clear()
    clear_jwks_cache()
    return await verify_jwt_for_audience(
        f"Bearer {resp.json()['access_token']}", expected_audience="meho-backplane"
    )


async def test_first_admin_must_change_the_password_then_is_a_platform_admin(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_login = keycloak.first_login
    assert first_login["admin_username"] == "admin"
    _allow_password_grant_on_cli(keycloak)

    # Keycloak wants a new password (and the profile) before any login.
    pending = _password_login(keycloak, "admin", first_login["admin_password"])
    assert pending.status_code == 400
    assert "not fully set up" in pending.json()["error_description"]

    # Once the first login's steps are done, the password from the Secret is
    # the one Keycloak holds, and the login carries the first admin's claims.
    _finish_first_login(keycloak, "admin")
    _check_the_code_is_needed(keycloak, "admin", first_login["admin_password"])
    operator = await _operator_for(keycloak, "admin", first_login["admin_password"], monkeypatch)
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role is TenantRole.TENANT_ADMIN
    assert operator.platform_admin is True
    assert operator.approver is False


async def test_second_person_is_an_operator(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_login = keycloak.first_login
    assert first_login["approver_username"] == "approver"
    _finish_first_login(keycloak, "approver")
    _check_the_code_is_needed(keycloak, "approver", first_login["approver_password"])

    operator = await _operator_for(
        keycloak, "approver", first_login["approver_password"], monkeypatch
    )
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role is TenantRole.OPERATOR
    assert operator.platform_admin is False


def _person_in(kc: _Keycloak, username: str, groups: list[str]) -> str:
    """Make a person with a full profile in *groups*; return the password."""
    admin = f"{kc.base_url}/admin/realms/meho"
    password = secrets.token_urlsafe(18)
    httpx.post(
        f"{admin}/users",
        headers=_admin_headers(kc),
        json={
            "username": username,
            "enabled": True,
            "email": f"{username}@example.com",
            "emailVerified": True,
            "firstName": "Test",
            "lastName": username,
            "credentials": [{"type": "password", "value": password, "temporary": False}],
            "groups": [f"/{group}" for group in groups],
        },
    ).raise_for_status()
    return password


def _claims(kc: _Keycloak, username: str, password: str) -> dict[str, Any]:
    resp = _password_login(kc, username, password)
    assert resp.status_code == 200, resp.text
    payload = resp.json()["access_token"].split(".")[1]
    claims: dict[str, Any] = json.loads(
        base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    )
    return claims


def test_extra_tenant_groups_carry_only_meho_tenant(keycloak: _Keycloak) -> None:
    admin = f"{keycloak.base_url}/admin/realms/meho"
    headers = _admin_headers(keycloak)
    for role in ("Readers", "Operators", "SA-Operators", "Admins", "Approvers"):
        name = f"MEHO-lab-b-{role}"
        found = httpx.get(
            f"{admin}/groups", headers=headers, params={"search": name, "exact": "true"}
        ).json()
        group = httpx.get(f"{admin}/groups/{found[0]['id']}", headers=headers).json()
        assert group["attributes"] == {"meho_tenant": [f"{_LAB_B_ID}:lab-b:{role}"]}, name


def test_a_person_in_two_tenants_gets_both_in_meho_tenants(keycloak: _Keycloak) -> None:
    _allow_password_grant_on_cli(keycloak)
    password = _person_in(keycloak, "two-tenants", ["MEHO-Readers", "MEHO-lab-b-Operators"])
    claims = _claims(keycloak, "two-tenants", password)
    assert sorted(claims["meho_tenants"]) == sorted(
        [f"{_TENANT_ID}:main:Readers", f"{_LAB_B_ID}:lab-b:Operators"]
    )
    # The claims MEHO reads today still come from the first tenant's group.
    assert claims["tenant_id"] == _TENANT_ID
    assert claims["tenant_role"] == "read_only"


def test_a_person_only_in_the_extra_tenant_gets_no_tenant_id(keycloak: _Keycloak) -> None:
    _allow_password_grant_on_cli(keycloak)
    password = _person_in(keycloak, "lab-b-only", ["MEHO-lab-b-Admins"])
    claims = _claims(keycloak, "lab-b-only", password)
    assert claims["meho_tenants"] == [f"{_LAB_B_ID}:lab-b:Admins"]
    for name in ("tenant_id", "tenant_role", "approver", "platform_admin"):
        assert name not in claims, name


def test_a_create_with_an_unknown_group_makes_no_user(keycloak: _Keycloak) -> None:
    """The stage relies on this: a person and their groups are one request."""
    admin = f"{keycloak.base_url}/admin/realms/meho"
    headers = _admin_headers(keycloak)
    resp = httpx.post(
        f"{admin}/users",
        headers=headers,
        json={"username": "half-made", "enabled": True, "groups": ["/MEHO-Admins", "/No-Such"]},
    )
    assert resp.status_code >= 400, resp.text
    found = httpx.get(
        f"{admin}/users", headers=headers, params={"username": "half-made", "exact": "true"}
    ).json()
    assert found == []


# --- The Keycloak admin of realm meho ----------------------------------------


def _meho_realm_roles(kc: _Keycloak, user_id: str) -> tuple[set[str], set[str]]:
    """``(every role of meho-realm, the ones the user holds in effect)``."""
    master = f"{kc.base_url}/admin/realms/master"
    headers = _master_headers(kc)
    client = httpx.get(
        f"{master}/clients", headers=headers, params={"clientId": "meho-realm"}
    ).json()[0]
    every = {
        r["name"]
        for r in httpx.get(f"{master}/clients/{client['id']}/roles", headers=headers).json()
    }
    held = httpx.get(
        f"{master}/users/{user_id}/role-mappings/clients/{client['id']}/composite", headers=headers
    ).json()
    return every, {r["name"] for r in held}


async def test_console_admin_administers_realm_meho_from_realm_master(
    keycloak: _Keycloak, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    console = store.secrets["rel-core-console-admin"]
    assert console["username"] == "console-admin"
    assert "user console-admin: created (in no group)" in keycloak.output
    assert "meho-realm roles granted" in keycloak.output
    assert (
        httpx.get(
            f"{keycloak.base_url}/admin/realms/meho/users",
            headers=_admin_headers(keycloak),
            params={"username": "console-admin", "exact": "true"},
        ).json()
        == []
    ), "not a user of realm meho"
    (user,) = _master_users(keycloak, "console-admin")
    assert sorted(user["requiredActions"]) == ["CONFIGURE_TOTP", "UPDATE_PASSWORD"]
    every, held = _meho_realm_roles(keycloak, user["id"])
    assert "impersonation" in every
    assert held == every - {"impersonation"}

    # What the person does at the first console login: a new password (set
    # here through the Admin API) and nothing else pending.
    master = f"{keycloak.base_url}/admin/realms/master"
    headers = _master_headers(keycloak)
    password = secrets.token_urlsafe(24)
    httpx.put(
        f"{master}/users/{user['id']}/reset-password",
        headers=headers,
        json={"type": "password", "value": password, "temporary": False},
    ).raise_for_status()
    rep = httpx.get(f"{master}/users/{user['id']}", headers=headers).json()
    rep.update(
        {
            "requiredActions": [],
            "firstName": "Console",
            "lastName": "Admin",
            "email": "console-admin@example.com",
        }
    )
    httpx.put(f"{master}/users/{user['id']}", headers=headers, json=rep).raise_for_status()

    token = _token(
        keycloak,
        "master",
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "console-admin",
            "password": password,
        },
    )
    assert token.status_code == 200, token.text
    access = token.json()["access_token"]
    as_console = {"Authorization": f"Bearer {access}"}

    admin = f"{keycloak.base_url}/admin/realms/meho"
    idp = httpx.post(
        f"{admin}/identity-provider/instances",
        headers=as_console,
        json={
            "alias": "company-oidc",
            "providerId": "oidc",
            "enabled": True,
            "config": {
                "authorizationUrl": "https://idp.example.test/auth",
                "tokenUrl": "https://idp.example.test/token",
                "clientId": "meho",
                "clientSecret": "not-a-real-secret",
                "clientAuthMethod": "client_secret_post",
            },
        },
    )
    assert idp.status_code == 201, idp.text
    realm_id = httpx.get(admin, headers=as_console).json()["id"]
    ldap = httpx.post(
        f"{admin}/components",
        headers=as_console,
        json={
            "name": "company-ldap",
            "providerId": "ldap",
            "providerType": "org.keycloak.storage.UserStorageProvider",
            "parentId": realm_id,
            "config": {
                "enabled": ["false"],
                "vendor": ["other"],
                "connectionUrl": ["ldap://ldap.example.test:389"],
                "usersDn": ["ou=people,dc=example,dc=test"],
                "usernameLDAPAttribute": ["uid"],
                "rdnLDAPAttribute": ["uid"],
                "uuidLDAPAttribute": ["entryUUID"],
                "userObjectClasses": ["inetOrgPerson"],
                "authType": ["none"],
                "editMode": ["READ_ONLY"],
            },
        },
    )
    assert ldap.status_code == 201, ldap.text

    # No impersonation: it cannot log in as a person of realm meho.
    (person,) = httpx.get(
        f"{admin}/users", headers=as_console, params={"username": "admin", "exact": "true"}
    ).json()
    impersonate = httpx.post(f"{admin}/users/{person['id']}/impersonation", headers=as_console)
    assert impersonate.status_code == 403, impersonate.text
    components = httpx.get(f"{admin}/components", headers=as_console)
    assert components.status_code == 200
    assert "company-ldap" in {c["name"] for c in components.json()}
    # Leave the realm as the stage left it for the second run.
    httpx.delete(ldap.headers["location"], headers=as_console).raise_for_status()
    httpx.delete(
        f"{admin}/identity-provider/instances/company-oidc", headers=as_console
    ).raise_for_status()

    # A token of realm master: MEHO, which trusts realm meho, refuses it.
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{keycloak.base_url}/realms/meho")
    get_settings.cache_clear()
    clear_jwks_cache()
    with pytest.raises(HTTPException) as refused:
        await verify_jwt_for_audience(f"Bearer {access}", expected_audience="meho-backplane")
    assert refused.value.status_code == 401


def test_meho_admin_cannot_find_or_take_over_the_console_admin(keycloak: _Keycloak) -> None:
    """The takeover a user of realm meho was open to: meho-admin resets its password."""
    (user,) = _master_users(keycloak, "console-admin")
    master = f"{keycloak.base_url}/admin/realms/master"
    as_meho_admin = _admin_headers(keycloak)

    found = httpx.get(
        f"{master}/users",
        headers=as_meho_admin,
        params={"username": "console-admin", "exact": "true"},
    )
    reset = httpx.put(
        f"{master}/users/{user['id']}/reset-password",
        headers=as_meho_admin,
        json={"type": "password", "value": secrets.token_urlsafe(24), "temporary": False},
    )

    assert found.status_code in (401, 403), found.text
    assert reset.status_code in (401, 403), reset.text


# --- The admin's credential write and the second person's approval ----------

_WRITE_OP = "k8s.secret.create"  # classified credential_write by the backplane
_SENTINEL = "s3cret-sentinel-value"


class _WriteConnector(Connector):
    product = "credwrite"
    version = "1.x"
    impl_id = "credwrite"
    priority = 10

    async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
        return FingerprintResult(
            probe=ProbeResult(reachable=True, probe_method="none"),
            product="credwrite",
            version="1.x",
        )

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:
        raise NotImplementedError


#: What the write handler saw: one entry per run (the dispatcher wants a
#: module-level handler, so this cannot be a closure).
_WRITES: list[dict[str, Any]] = []


async def _write_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    _WRITES.append({"by": operator.sub, "params": params})
    return {"written": True}


async def _register_write_op() -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    register_connector_v2(product="credwrite", version="", impl_id="", cls=_WriteConnector)
    await register_typed_operation(
        product="credwrite",
        version="1.x",
        impl_id="credwrite",
        op_id=_WRITE_OP,
        handler=_write_handler,
        summary="A write whose params carry a secret.",
        description="Test.",
        parameter_schema={"type": "object"},
        requires_approval=True,
        when_to_use=None,
        embedding_service=embedding,
    )


async def test_the_second_person_approves_the_first_admins_credential_write(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance path with the two people the Job made, over the REST API."""
    from meho_backplane.api.v1 import approvals as approvals_module
    from meho_backplane.main import app

    first_login = keycloak.first_login
    admin_token = _login_with_code(keycloak, "admin", first_login["admin_password"]).json()[
        "access_token"
    ]
    approver_token = _login_with_code(
        keycloak, "approver", first_login["approver_password"]
    ).json()["access_token"]

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{keycloak.base_url}/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", Fernet.generate_key().decode())
    # No broadcast store here: a closed port makes every publish fail fast,
    # and a failed publish is a warning by contract. The audit rows are the
    # record.
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://127.0.0.1:9")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_broadcast_client_for_testing()
    reset_dispatcher_caches()
    clear_registry()
    _WRITES.clear()
    try:
        await _register_write_op()
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://backplane.test"
        ) as api:
            as_admin = {"Authorization": f"Bearer {admin_token}"}
            as_approver = {"Authorization": f"Bearer {approver_token}"}

            # The first admin asks for the write; it waits for approval.
            asked = await api.post(
                "/api/v1/operations/call",
                headers=as_admin,
                json={
                    "connector_id": "credwrite-1.x",
                    "op_id": _WRITE_OP,
                    "params": {"name": "db", "data": {"password": _SENTINEL}},
                },
            )
            assert asked.status_code == 200, asked.text
            assert asked.json()["status"] == "awaiting_approval", asked.json()
            request_id = asked.json()["extras"]["approval_request_id"]
            assert _WRITES == []

            # A password-grant token is a plain API token: it shows no sign-in
            # time, so it never approves over REST (#3979).
            plain = await api.post(
                f"/api/v1/approvals/{request_id}/decide",
                headers=as_approver,
                json={"decision": "approved"},
            )
            assert plain.status_code == 401, plain.text
            assert plain.json()["detail"].startswith("fresh_sign_in_required:")
            assert "insufficient_user_authentication" in plain.headers["www-authenticate"]
            assert _WRITES == []

            # From here on the people sign in fresh in the browser, which a
            # test cannot do. Stand in for that proof; the group rules below
            # are the real Keycloak tokens.
            monkeypatch.setattr(
                approvals_module,
                "check_fresh_approval_sign_in",
                lambda operator, settings, **_kwargs: ApprovalAuth(
                    channel="rest",
                    method="fresh_sign_in",
                    auth_time=int(time.time()),
                    sid=secrets.token_hex(8),
                ),
            )

            # The first admin may approve their own request too (#5969); the
            # tests of the approval groups check that. Here the second person
            # approves it, and the write runs.
            decided = await api.post(
                f"/api/v1/approvals/{request_id}/decide",
                headers=as_approver,
                json={"decision": "approved"},
            )
            assert decided.status_code == 200, decided.text
            body = decided.json()
            assert body["decision"] == "approved"
            assert body["decided_by_name"] == "Test approver"
            assert body["dispatch_status"] == "ok", body
            assert body["dispatch_result"] == {"written": True}

            shown = await api.get(f"/api/v1/approvals/{request_id}", headers=as_approver)
            assert shown.status_code == 200, shown.text
            assert shown.json()["status"] == "approved"
            assert shown.json()["principal_name"] == "Test admin"
            assert shown.json()["principal_sub"] != body["decided_by"], "two people"
    finally:
        reset_dispatcher_caches()
        clear_registry()
        reset_broadcast_client_for_testing()

    assert len(_WRITES) == 1
    assert _WRITES[0]["params"]["data"]["password"] == _SENTINEL
    assert _WRITES[0]["by"] == body["decided_by"], "the approved write runs under the approver"


async def test_second_run_logs_in_as_meho_admin_and_changes_nothing(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    admin = f"{keycloak.base_url}/admin/realms/meho"
    headers = _admin_headers(keycloak)

    def snapshot() -> dict[str, Any]:
        clients = httpx.get(f"{admin}/clients", headers=headers).json()
        return {
            "clients": {c["clientId"]: c for c in clients},
            "mappers": {
                c["clientId"]: sorted(
                    m["id"]
                    for m in httpx.get(
                        f"{admin}/clients/{c['id']}/protocol-mappers/models", headers=headers
                    ).json()
                )
                for c in clients
            },
            "groups": sorted(
                g["name"]
                for g in httpx.get(
                    f"{admin}/groups", headers=headers, params={"briefRepresentation": "true"}
                ).json()
            ),
            "users": sorted(u["id"] for u in httpx.get(f"{admin}/users", headers=headers).json()),
        }

    before = snapshot()
    secrets_before = {k: dict(v) for k, v in store.secrets.items()}
    capfd.readouterr()

    await _run_stage(keycloak, store)

    output = capfd.readouterr().out
    assert "login: meho-admin" in output
    assert "created" not in output
    assert snapshot() == before
    assert store.secrets == secrets_before
    for value in list(keycloak.env.values()) + list(keycloak.first_login.values()):
        if "@" not in value and len(value) >= 16:
            assert value not in output + keycloak.output
    # The client secrets are the ones from the environment, after both runs.
    introspect = f"{keycloak.base_url}/realms/meho/protocol/openid-connect/token/introspect"
    for client_id, key in (
        ("meho-admin", ADMIN_SECRET_ENV),
        ("meho-web", "MEHO_REALM_WEB_CLIENT_SECRET"),
        ("meho-check-runner", "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET"),
    ):
        status = httpx.post(
            introspect,
            data={"token": "x", "client_id": client_id, "client_secret": keycloak.env[key]},
        ).status_code
        assert status == 200, client_id


async def test_the_stage_gives_an_older_cli_client_a_long_login(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    """A realm from before the long CLI login gets it from the next run of the stage."""
    admin = f"{keycloak.base_url}/admin/realms/meho"
    headers = _admin_headers(keycloak)
    idle = "client.offline.session.idle.timeout"
    cli = httpx.get(f"{admin}/clients", headers=headers, params={"clientId": "meho-cli"}).json()[0]
    scopes = httpx.get(f"{admin}/client-scopes", headers=headers).json()
    offline_id = next(s["id"] for s in scopes if s["name"] == "offline_access")
    httpx.delete(
        f"{admin}/clients/{cli['id']}/optional-client-scopes/{offline_id}", headers=headers
    ).raise_for_status()
    cli["attributes"][idle] = ""
    httpx.put(f"{admin}/clients/{cli['id']}", headers=headers, json=cli).raise_for_status()
    password = _person_in(keycloak, "long-login", ["MEHO-Readers"])
    long_login = {
        "grant_type": "password",
        "client_id": "meho-cli",
        "username": "long-login",
        "password": password,
        "scope": "openid offline_access",
    }
    refused = _token(keycloak, "meho", long_login)
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"] == "invalid_scope"
    capfd.readouterr()

    await _run_stage(keycloak, store)

    assert "client meho-cli: kept (optional scope offline_access added" in capfd.readouterr().out
    cli = httpx.get(f"{admin}/clients/{cli['id']}", headers=headers).json()
    assert cli["attributes"][idle] == "172800"
    granted = _token(keycloak, "meho", long_login)
    assert granted.status_code == 200, granted.text
    payload = granted.json()["refresh_token"].split(".")[1]
    refresh = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert refresh["typ"] == "Offline"

    # An admin ends the long login (the Consents tab of the person); the
    # next renewal is refused, so the CLI asks for a new login.
    person = _user(keycloak, "long-login")
    httpx.delete(
        f"{admin}/users/{person['id']}/consents/meho-cli", headers=headers
    ).raise_for_status()
    renewal = _token(
        keycloak,
        "meho",
        {
            "grant_type": "refresh_token",
            "client_id": "meho-cli",
            "refresh_token": granted.json()["refresh_token"],
        },
    )
    assert renewal.status_code == 400, renewal.text
    assert renewal.json()["error"] == "invalid_grant"


async def test_a_run_as_meho_admin_without_the_console_admin_does_not_fail(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    """An install older than the console admin: its Secret is missing.

    Runs on a copy of the store, so the shared Keycloak and store are left
    as they were and the order of the tests does not matter.
    """
    older = _FakeStore()
    older.secrets = {k: dict(v) for k, v in store.secrets.items() if k != "rel-core-console-admin"}
    master_before = sorted(u["id"] for u in _master_users(keycloak))
    capfd.readouterr()

    await _run_stage(keycloak, older)

    output = capfd.readouterr().out
    assert "login: meho-admin" in output
    assert "console admin: none, and meho-admin cannot make users in realm master" in output
    assert "created" not in output
    assert older.created == []
    assert sorted(u["id"] for u in _master_users(keycloak)) == master_before


def _turn_recovery_codes_off(kc: _Keycloak) -> None:
    """Put the two settings back as a realm made before recovery codes has them."""
    headers = _master_headers(kc)
    url = f"{kc.base_url}{_AUTHENTICATION}/required-actions/CONFIGURE_TOTP"
    action = httpx.get(url, headers=headers).json()
    action["config"] = {**(action.get("config") or {}), ADD_RECOVERY_CODES: "false"}
    httpx.put(url, headers=headers, json=action).raise_for_status()
    url = f"{kc.base_url}{_AUTHENTICATION}/flows/browser/executions"
    step = next(
        s for s in httpx.get(url, headers=headers).json() if s.get("providerId") == RECOVERY_STEP
    )
    httpx.put(url, headers=headers, json={**step, "requirement": "DISABLED"}).raise_for_status()


def _manage_realm_of_meho_admin(kc: _Keycloak) -> tuple[str, list[dict[str, Any]]]:
    """The role-mapping URL of meho-admin's service account and the ``manage-realm`` role."""
    admin = f"{kc.base_url}/admin/realms/meho"
    headers = _master_headers(kc)

    def client_id(name: str) -> str:
        found = httpx.get(f"{admin}/clients", headers=headers, params={"clientId": name}).json()
        return str(found[0]["id"])

    account = httpx.get(
        f"{admin}/clients/{client_id('meho-admin')}/service-account-user", headers=headers
    ).json()
    management = client_id("realm-management")
    role = httpx.get(f"{admin}/clients/{management}/roles/manage-realm", headers=headers).json()
    return f"{admin}/users/{account['id']}/role-mappings/clients/{management}", [role]


async def test_an_older_install_gets_recovery_codes_once_meho_admin_may_change_login_settings(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    """An older install: ``meho-admin`` without ``manage-realm``. The Job stops (#6280).

    Then the fix of the upgrade notes: give the role back, run again.
    """
    url, roles = _manage_realm_of_meho_admin(keycloak)
    httpx.request("DELETE", url, headers=_master_headers(keycloak), json=roles).raise_for_status()
    try:
        _turn_recovery_codes_off(keycloak)
        capfd.readouterr()

        with pytest.raises(RealmStageError, match="manage-realm"):
            await _run_stage(keycloak, store)

        output = capfd.readouterr().out
        assert "login: meho-admin" in output
        assert "no right to read" not in output
        assert _recovery_settings(keycloak) == ("false", "DISABLED")
    finally:
        httpx.post(url, headers=_master_headers(keycloak), json=roles).raise_for_status()

    await _run_stage(keycloak, store)
    assert "recovery codes: set" in capfd.readouterr().out
    assert _recovery_settings(keycloak) == ("true", "ALTERNATIVE")


async def test_the_check_before_an_upgrade_stops_only_without_manage_realm(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    """The pre-upgrade check (#6336) reads meho-admin's real roles: no role, no upgrade."""
    await _run_stage(keycloak, store)  # the first-start account is gone; meho-admin is used
    capfd.readouterr()

    async def check() -> None:
        async with httpx.AsyncClient(base_url=keycloak.base_url, timeout=30) as http:
            await check_role(HttpAdmin(http), keycloak.env)

    await check()
    assert "role check: meho-admin has manage-realm" in capfd.readouterr().out

    url, roles = _manage_realm_of_meho_admin(keycloak)
    httpx.request("DELETE", url, headers=_master_headers(keycloak), json=roles).raise_for_status()
    try:
        with pytest.raises(RealmStageError, match="lacks the realm-management role manage-realm"):
            await check()
    finally:
        httpx.post(url, headers=_master_headers(keycloak), json=roles).raise_for_status()
    assert keycloak.env[ADMIN_SECRET_ENV] not in capfd.readouterr().out

    await check()
    assert "has manage-realm" in capfd.readouterr().out


_APPROVAL_FLOW = "meho approval sign-in level"


def _approval_flow_and_binding(kc: _Keycloak) -> tuple[str | None, str | None]:
    """The id of the approval flow and the browser flow bound to ``meho-cli-approve``."""
    admin = f"{kc.base_url}/admin/realms/meho"
    headers = _master_headers(kc)
    flows = httpx.get(f"{admin}/authentication/flows", headers=headers).json()
    flow_id = next((f["id"] for f in flows if f["alias"] == _APPROVAL_FLOW), None)
    (client,) = httpx.get(
        f"{admin}/clients", headers=headers, params={"clientId": "meho-cli-approve"}
    ).json()
    return flow_id, (client.get("authenticationFlowBindingOverrides") or {}).get("browser")


async def test_an_upgrade_makes_the_approval_flow_and_binds_it_again(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    """A realm without the approval flow and binding gets both back from the Job (#6280)."""
    admin = f"{keycloak.base_url}/admin/realms/meho"
    headers = _master_headers(keycloak)
    flow_id, bound = _approval_flow_and_binding(keycloak)
    assert flow_id and bound == flow_id
    (client,) = httpx.get(
        f"{admin}/clients", headers=headers, params={"clientId": "meho-cli-approve"}
    ).json()
    # Keycloak refuses to delete a flow a client is bound to: unbind first.
    client["authenticationFlowBindingOverrides"] = {"browser": ""}
    httpx.put(f"{admin}/clients/{client['id']}", headers=headers, json=client).raise_for_status()
    httpx.delete(f"{admin}/authentication/flows/{flow_id}", headers=headers).raise_for_status()
    assert _approval_flow_and_binding(keycloak) == (None, None)
    capfd.readouterr()

    await _run_stage(keycloak, store)

    output = capfd.readouterr().out
    assert "login: meho-admin" in output
    assert f"login flow {_APPROVAL_FLOW}: created" in output
    flow_id, bound = _approval_flow_and_binding(keycloak)
    assert flow_id and bound == flow_id
    steps = httpx.get(
        f"{admin}/authentication/flows/meho%20approval%20sign-in%20level/executions",
        headers=headers,
    ).json()
    assert [
        (
            s["level"],
            # Keycloak leaves "authenticationFlow" out for a plain step.
            s["displayName"] if s.get("authenticationFlow") else s["providerId"],
            s["requirement"],
        )
        for s in steps
    ] == [
        (0, "meho approval sign-in level - level 2", "CONDITIONAL"),
        (1, "conditional-level-of-authentication", "REQUIRED"),
        (1, "auth-username-password-form", "REQUIRED"),
        (1, "meho approval sign-in level - one-time code", "CONDITIONAL"),
        (2, "conditional-user-configured", "REQUIRED"),
        (2, "auth-otp-form", "REQUIRED"),
    ]
    # The level step got its settings back, and the realm names the level.
    level_step = next(
        s for s in steps if s.get("providerId") == "conditional-level-of-authentication"
    )
    settings = httpx.get(
        f"{admin}/authentication/config/{level_step['authenticationConfig']}", headers=headers
    ).json()
    assert settings["config"] == {"loa-condition-level": "2", "loa-max-age": "0"}
    realm = httpx.get(admin, headers=headers).json()
    assert json.loads(realm["attributes"]["acr.loa.map"])["meho-approve"] == 2


#: Realm settings the write of the level map must leave alone, with values
#: that differ from the realm file and from Keycloak's defaults.
_REALM_SETTINGS = {
    "bruteForceProtected": True,
    "failureFactor": 7,
    "waitIncrementSeconds": 120,
    "maxFailureWaitSeconds": 900,
    "maxDeltaTimeSeconds": 3600,
    "offlineSessionMaxLifespanEnabled": True,
    "offlineSessionMaxLifespan": 86400,
    "displayName": "MEHO level test",
    "webAuthnPolicyPasswordlessUserVerificationRequirement": "preferred",
}


def _kept_settings(realm: Mapping[str, Any]) -> dict[str, Any]:
    """The realm settings the level write must keep: the ones above and the passkey policy."""
    return {
        key: value
        for key, value in realm.items()
        if key in _REALM_SETTINGS or key.startswith("webAuthnPolicyPasswordless")
    }


async def test_the_level_write_keeps_the_other_realm_settings(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    """A realm without ``meho-approve`` gets it; nothing else of the realm changes (#6281).

    The stage reads the realm attributes, changes only ``acr.loa.map`` and
    sends the attributes back. Some realm settings live in realm attributes
    inside Keycloak, so this checks them after a real write.
    """
    admin = f"{keycloak.base_url}/admin/realms/meho"
    headers = _master_headers(keycloak)
    realm = httpx.get(admin, headers=headers).json()
    realm.update(_REALM_SETTINGS)
    realm["attributes"] = {
        **realm["attributes"],
        "acr.loa.map": json.dumps({"meho-touch": 3}),
        "meho-test-extra": "kept",
    }
    httpx.put(admin, headers=headers, json=realm).raise_for_status()
    before = httpx.get(admin, headers=headers).json()
    assert json.loads(before["attributes"]["acr.loa.map"]) == {"meho-touch": 3}
    assert {key: before[key] for key in _REALM_SETTINGS} == _REALM_SETTINGS
    capfd.readouterr()

    await _run_stage(keycloak, store)

    assert "sign-in levels: set (meho-approve)" in capfd.readouterr().out
    after = httpx.get(admin, headers=headers).json()
    assert json.loads(after["attributes"]["acr.loa.map"]) == {"meho-touch": 3, "meho-approve": 2}
    assert _kept_settings(after) == _kept_settings(before)
    others = {k: v for k, v in before["attributes"].items() if k != "acr.loa.map"}
    assert {k: after["attributes"].get(k) for k in others} == others
