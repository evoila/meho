# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""#4040 — the built-in Keycloak's realm file gives the right claims at login.

The realm file of the Keycloak subchart
(``deploy/charts/meho/charts/keycloak/files/meho-realm.json``) is imported
into a real Keycloak with ``--import-realm``, with the same ``${...}``
environment values the subchart's StatefulSet sets. Then:

1. A person in MEHO-Readers logs in through ``meho-cli``. The backplane's
   own JWT check accepts the token as a ``read_only`` operator of the first
   tenant, without the approver flag. The token also carries the MCP
   audience.
2. A person in MEHO-Approvers gets ``read_only`` plus the approver flag.
3. The three confidential clients authenticate with the secrets from the
   environment, so Keycloak filled the placeholders. ``meho-admin`` can read
   the realm's clients; ``meho-check-runner`` tokens carry only their own
   audience, not ``meho-backplane``.

The login uses the password grant, switched on for ``meho-cli`` by this test
only (people use the device flow); the client's mappers and scopes are the
ones from the file.

The image is the subchart's own pin (``charts/keycloak/values.yaml``). Set
``MEHO_TEST_CORE_KEYCLOAK_IMAGE`` to use a mirror of the same version; the
realm file needs Keycloak 26.6 or newer. Skips when Docker is missing or the
container cannot start.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from meho_backplane.auth.agent_token import get_client_credentials_token
from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.keycloak_admin import KeycloakAdminClient
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.settings import get_settings
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

_SUBCHART = (
    Path(__file__).resolve().parents[3] / "deploy" / "charts" / "meho" / "charts" / "keycloak"
)
_PUBLIC_URL = "https://meho.example.test"
_TENANT_ID = "6b0d7c55-41a8-4b52-9a8e-2f3c1d0e9a77"


@dataclass(frozen=True)
class _Keycloak:
    base_url: str
    bootstrap_id: str
    bootstrap_secret: str
    client_secrets: dict[str, str]


def _subchart_image() -> str:
    image = yaml.safe_load((_SUBCHART / "values.yaml").read_text())["image"]
    return f"{image['repository']}@{image['digest']}"


def _wait_for_realm(base_url: str, timeout: float = 120.0) -> None:
    """Poll the realm's discovery document until Keycloak serves it."""
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


def _token(base_url: str, realm: str, data: dict[str, str]) -> httpx.Response:
    return httpx.post(f"{base_url}/realms/{realm}/protocol/openid-connect/token", data=data)


def _bootstrap_headers(kc: _Keycloak) -> dict[str, str]:
    resp = _token(
        kc.base_url,
        "master",
        {
            "grant_type": "client_credentials",
            "client_id": kc.bootstrap_id,
            "client_secret": kc.bootstrap_secret,
        },
    )
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@contextmanager
def _run_keycloak(**extra_env: str) -> Iterator[_Keycloak]:
    """Start Keycloak with the subchart's realm file imported."""
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

    image = os.environ.get("MEHO_TEST_CORE_KEYCLOAK_IMAGE") or _subchart_image()
    kc = _Keycloak(
        base_url="",
        bootstrap_id="meho-bootstrap",
        bootstrap_secret=secrets.token_hex(16),
        client_secrets={
            "meho-admin": secrets.token_hex(16),
            "meho-web": secrets.token_hex(16),
            "meho-check-runner": secrets.token_hex(16),
        },
    )
    # Same values the StatefulSet sets, except the database (the built-in
    # dev store is enough here) and, unless *extra_env* sets them, the
    # fixed public hostname.
    container = (
        DockerContainer(image)
        .with_command("start-dev --import-realm")
        .with_env("KC_HTTP_RELATIVE_PATH", "/auth")
        .with_env("KC_BOOTSTRAP_ADMIN_CLIENT_ID", kc.bootstrap_id)
        .with_env("KC_BOOTSTRAP_ADMIN_CLIENT_SECRET", kc.bootstrap_secret)
        .with_env("MEHO_REALM_PUBLIC_URL", _PUBLIC_URL)
        .with_env("MEHO_REALM_TENANT_ID", _TENANT_ID)
        .with_env("MEHO_REALM_TENANT_NAME", "main")
        .with_env("MEHO_REALM_ADMIN_CLIENT_SECRET", kc.client_secrets["meho-admin"])
        .with_env("MEHO_REALM_WEB_CLIENT_SECRET", kc.client_secrets["meho-web"])
        .with_env("MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET", kc.client_secrets["meho-check-runner"])
        .with_volume_mapping(str(_SUBCHART / "files"), "/opt/keycloak/data/import", "ro")
        .with_exposed_ports(8080)
    )
    for name, value in extra_env.items():
        container = container.with_env(name, value)
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"Keycloak container failed to start ({type(exc).__name__}): {exc}")
    try:
        wait_for_log_message(container, "Listening on:", timeout=240)
        host = container.get_container_host_ip()
        base_url = f"http://{host}:{container.get_exposed_port(8080)}/auth"
        _wait_for_realm(base_url)
        yield _Keycloak(
            base_url=base_url,
            bootstrap_id=kc.bootstrap_id,
            bootstrap_secret=kc.bootstrap_secret,
            client_secrets=kc.client_secrets,
        )
    finally:
        container.stop()


@pytest.fixture(scope="module")
def keycloak() -> Iterator[_Keycloak]:
    """Keycloak without a fixed hostname: the issuer follows the URL the test calls."""
    with _run_keycloak() as kc:
        yield kc


@pytest.fixture(scope="module")
def keycloak_behind_ingress() -> Iterator[_Keycloak]:
    """Keycloak as the subchart starts it: a fixed public address, back channel dynamic.

    The test reaches it on the container port, as a pod reaches the Service.
    """
    with _run_keycloak(
        KC_HOSTNAME=f"{_PUBLIC_URL}/auth",
        KC_HOSTNAME_BACKCHANNEL_DYNAMIC="true",
        KC_PROXY_HEADERS="xforwarded",
    ) as kc:
        yield kc


def _add_person(kc: _Keycloak, username: str, group: str) -> str:
    headers = _bootstrap_headers(kc)
    admin = f"{kc.base_url}/admin/realms/meho"
    password = secrets.token_urlsafe(18)
    httpx.post(
        f"{admin}/users",
        headers=headers,
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
    user_id = httpx.get(
        f"{admin}/users", headers=headers, params={"username": username, "exact": "true"}
    ).json()[0]["id"]
    group_id = next(
        g["id"]
        for g in httpx.get(f"{admin}/groups", headers=headers, params={"search": group}).json()
        if g["name"] == group
    )
    httpx.put(f"{admin}/users/{user_id}/groups/{group_id}", headers=headers).raise_for_status()
    return password


def _allow_password_grant_on_cli(kc: _Keycloak) -> None:
    headers = _bootstrap_headers(kc)
    admin = f"{kc.base_url}/admin/realms/meho"
    client = httpx.get(f"{admin}/clients", headers=headers, params={"clientId": "meho-cli"}).json()[
        0
    ]
    client["directAccessGrantsEnabled"] = True
    httpx.put(f"{admin}/clients/{client['id']}", headers=headers, json=client).raise_for_status()


async def _login(
    kc: _Keycloak, username: str, group: str, monkeypatch: pytest.MonkeyPatch, audience: str
) -> Operator:
    password = _add_person(kc, username, group)
    _allow_password_grant_on_cli(kc)
    resp = _token(
        kc.base_url,
        "meho",
        {
            "grant_type": "password",
            "client_id": "meho-cli",
            "username": username,
            "password": password,
            "scope": "openid",
        },
    )
    resp.raise_for_status()
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{kc.base_url}/realms/meho")
    get_settings.cache_clear()
    clear_jwks_cache()
    return await verify_jwt_for_audience(
        f"Bearer {resp.json()['access_token']}", expected_audience=audience
    )


async def test_reader_gets_tenant_and_role_from_the_group(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = await _login(keycloak, "reader", "MEHO-Readers", monkeypatch, "meho-backplane")
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role is TenantRole.READ_ONLY
    assert operator.approver is False


async def test_approver_gets_the_approver_flag(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = await _login(
        keycloak, "approver", "MEHO-Approvers", monkeypatch, f"{_PUBLIC_URL}/mcp"
    )
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role is TenantRole.READ_ONLY
    assert operator.approver is True


def _token_claims(access_token: str) -> dict[str, Any]:
    payload = access_token.split(".")[1]
    claims: dict[str, Any] = json.loads(
        base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    )
    return claims


def test_one_tenant_login_lists_one_tenant(keycloak: _Keycloak) -> None:
    """The ``meho-tenants`` mapper lists the one tenant; the old claims stay."""
    password = _add_person(keycloak, "operator-one", "MEHO-Operators")
    _allow_password_grant_on_cli(keycloak)
    resp = _token(
        keycloak.base_url,
        "meho",
        {
            "grant_type": "password",
            "client_id": "meho-cli",
            "username": "operator-one",
            "password": password,
            "scope": "openid",
        },
    )
    resp.raise_for_status()
    claims = _token_claims(resp.json()["access_token"])
    assert claims["meho_tenants"] == [f"{_TENANT_ID}:main:Operators"]
    assert claims["tenant_id"] == _TENANT_ID
    assert claims["tenant_role"] == "operator"
    assert "approver" not in claims


def test_confidential_clients_use_the_secrets_from_the_environment(keycloak: _Keycloak) -> None:
    introspect = f"{keycloak.base_url}/realms/meho/protocol/openid-connect/token/introspect"

    def client_check(client_id: str, secret: str) -> int:
        # The introspection endpoint checks the client's secret first.
        return httpx.post(
            introspect, data={"token": "x", "client_id": client_id, "client_secret": secret}
        ).status_code

    for client_id, secret in keycloak.client_secrets.items():
        assert client_check(client_id, secret) == 200, client_id
        assert client_check(client_id, "wrong-secret") == 401, client_id

    def grant(client_id: str) -> httpx.Response:
        return _token(
            keycloak.base_url,
            "meho",
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": keycloak.client_secrets[client_id],
            },
        )

    admin = grant("meho-admin")
    assert admin.status_code == 200, admin.text
    clients = httpx.get(
        f"{keycloak.base_url}/admin/realms/meho/clients",
        headers={"Authorization": f"Bearer {admin.json()['access_token']}"},
        params={"clientId": "meho-cli"},
    )
    assert clients.status_code == 200
    assert clients.json()[0]["clientId"] == "meho-cli"

    runner = grant("meho-check-runner")
    assert runner.status_code == 200, runner.text
    claims = httpx.post(
        introspect,
        data={
            "token": runner.json()["access_token"],
            "client_id": "meho-check-runner",
            "client_secret": keycloak.client_secrets["meho-check-runner"],
        },
    ).json()
    audience = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    assert "meho-check-runner" in audience
    assert "meho-backplane" not in audience


async def test_backplane_reaches_keycloak_through_the_service_address(
    keycloak_behind_ingress: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4591: discovery and tokens over the Service address, the public issuer in tokens.

    The backplane's settings are the ones the chart derives for the built-in
    Keycloak; the test talks plain HTTP to the container port, as a pod does
    to the Service.
    """
    kc = keycloak_behind_ingress
    internal = f"{kc.base_url}/realms/meho"
    issuer = f"{_PUBLIC_URL}/auth/realms/meho"

    discovery = httpx.get(f"{internal}/.well-known/openid-configuration").json()
    assert discovery["issuer"] == issuer
    assert discovery["authorization_endpoint"].startswith(issuer)
    assert discovery["jwks_uri"].startswith(internal)
    assert discovery["token_endpoint"].startswith(internal)

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", issuer)
    monkeypatch.setenv("KEYCLOAK_DISCOVERY_URL", internal)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", f"{kc.base_url}/admin/realms/meho")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", kc.client_secrets["meho-admin"])
    get_settings.cache_clear()
    clear_jwks_cache()
    settings = get_settings()

    # The Admin REST API with the meho-admin client: register an agent
    # principal, then log in as it through the token endpoint the agent
    # runs use, and check its token with the backplane's own JWT check.
    async with KeycloakAdminClient.from_settings(settings) as admin:
        internal_id = await admin.create_client(
            client_id="agent:service-url-check",
            name="service-url-check",
            tenant_id=_TENANT_ID,
            owner_sub="test",
            audience="meho-backplane",
            tenant_role="operator",
        )
        secret = await admin.get_client_secret(internal_id)
    token = await get_client_credentials_token(
        issuer_url=settings.keycloak_discovery_base,
        client_id="agent:service-url-check",
        client_secret=secret,
        audience="meho-backplane",
    )
    operator = await verify_jwt_for_audience(f"Bearer {token}", expected_audience="meho-backplane")
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role is TenantRole.OPERATOR


def _set_ssl_required(kc: _Keycloak, value: str) -> None:
    httpx.put(
        f"{kc.base_url}/admin/realms/meho",
        headers=_bootstrap_headers(kc),
        json={"sslRequired": value},
    ).raise_for_status()


def _admin_grant_status(kc: _Keycloak) -> int:
    return _token(
        kc.base_url,
        "meho",
        {
            "grant_type": "client_credentials",
            "client_id": "meho-admin",
            "client_secret": kc.client_secrets["meho-admin"],
        },
    ).status_code


def test_fixed_https_address_lets_pods_call_over_plain_http(
    keycloak_behind_ingress: _Keycloak,
) -> None:
    """#4591: the realm's ``sslRequired`` never blocks the Service address.

    Keycloak checks the scheme of the frontend address, which
    ``KC_HOSTNAME=https://<host>/auth`` fixes to https. So a pod's plain HTTP
    call through the Service passes even with ``sslRequired: all``, whatever
    the pod's own address.
    """
    _set_ssl_required(keycloak_behind_ingress, "all")
    try:
        assert _admin_grant_status(keycloak_behind_ingress) == 200
    finally:
        _set_ssl_required(keycloak_behind_ingress, "external")


def test_without_a_fixed_address_the_check_does_apply(keycloak: _Keycloak) -> None:
    """The control for the test above: without ``KC_HOSTNAME`` the request decides."""
    _set_ssl_required(keycloak, "all")
    try:
        assert _admin_grant_status(keycloak) == 403
    finally:
        _set_ssl_required(keycloak, "external")
