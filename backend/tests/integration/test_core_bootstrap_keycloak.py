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
3. The second run logs in as ``meho-admin`` and changes nothing: the realm
   and the Secrets are the same, and the output holds no secret.

Set ``MEHO_TEST_CORE_KEYCLOAK_IMAGE`` to use a mirror of the subchart's
image. Skips when Docker is missing or the container cannot start.
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.operator import TenantRole
from meho_backplane.core.keycloak import (
    ADMIN_SECRET_ENV,
    BOOTSTRAP_ID_ENV,
    BOOTSTRAP_SECRET_ENV,
    HttpAdmin,
    Plan,
    finish_realm,
)
from meho_backplane.settings import get_settings
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

_SUBCHART = (
    Path(__file__).resolve().parents[3] / "deploy" / "charts" / "meho" / "charts" / "keycloak"
)
_PUBLIC_URL = "https://meho.example.test"
_TENANT_ID = "6b0d7c55-41a8-4b52-9a8e-2f3c1d0e9a77"


@dataclass
class _Keycloak:
    base_url: str
    env: dict[str, str]
    first_login: dict[str, str] = field(default_factory=dict)
    output: str = ""


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
    assert admin["requiredActions"] == ["UPDATE_PASSWORD"]
    assert set(admin["groups"]) == {"MEHO-Admins", "MEHO-Platform-Admins"}
    approver = _user(keycloak, "approver")
    assert approver["groups"] == ["MEHO-SA-Operators"]


def _allow_password_grant_on_cli(kc: _Keycloak) -> None:
    admin = f"{kc.base_url}/admin/realms/meho"
    headers = _admin_headers(kc)
    client = httpx.get(f"{admin}/clients", headers=headers, params={"clientId": "meho-cli"}).json()[
        0
    ]
    client["directAccessGrantsEnabled"] = True
    httpx.put(f"{admin}/clients/{client['id']}", headers=headers, json=client).raise_for_status()


def _password_login(kc: _Keycloak, username: str, password: str) -> httpx.Response:
    return _token(
        kc,
        "meho",
        {
            "grant_type": "password",
            "client_id": "meho-cli",
            "username": username,
            "password": password,
            "scope": "openid",
        },
    )


def _finish_first_login(kc: _Keycloak, username: str) -> dict[str, Any]:
    """What the browser's first login does, minus the new password.

    Keycloak refuses a direct login while a required action is pending or
    the profile is incomplete ("Account is not fully set up"): the realm's
    user profile wants a first name, a last name and an email, which the
    person types in at the first browser login.
    """
    admin = f"{kc.base_url}/admin/realms/meho"
    headers = _admin_headers(kc)
    user = _user(kc, username)
    user_rep = httpx.get(f"{admin}/users/{user['id']}", headers=headers).json()
    user_rep["requiredActions"] = []
    user_rep.update({"firstName": "Test", "lastName": username, "email": f"{username}@example.com"})
    httpx.put(f"{admin}/users/{user['id']}", headers=headers, json=user_rep).raise_for_status()
    return user


async def _operator_for(
    kc: _Keycloak, username: str, password: str, monkeypatch: pytest.MonkeyPatch
) -> Any:
    resp = _password_login(kc, username, password)
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

    operator = await _operator_for(
        keycloak, "approver", first_login["approver_password"], monkeypatch
    )
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role is TenantRole.OPERATOR
    assert operator.platform_admin is False


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
