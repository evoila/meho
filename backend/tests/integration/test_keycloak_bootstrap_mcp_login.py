# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""#4002 — a fresh realm set up by ``bootstrap-clients`` accepts the MCP login.

The Claude Code plugin and the Claude Desktop bundle ask Keycloak for the
scopes ``mcp:read mcp:execute offline_access``; users add ``mcp:admin`` for
the operator tools. Keycloak refuses a login that asks for a scope the
client does not offer: it answers ``invalid_scope`` and the login page never
appears. This test proves, against a real Keycloak, that a realm set up only
the way the public docs say accepts both logins:

1. Start an empty Keycloak (no realm import).
2. Create the realm and the ``meho-backplane`` client, as
   ``docs-site/install/keycloak-realm.md`` tells the reader to do first.
3. Build the ``meho`` CLI from this checkout and run
   ``meho admin keycloak bootstrap-clients`` against the realm.
4. Run the browser login (authorization code + PKCE) for ``meho-mcp`` with
   the plugin's default scopes, read from the plugin script itself, and
   again with ``mcp:admin`` added.
5. Check the token through the backplane's own JWT check, and check that
   ``mcp:admin`` is in the session's scopes only when it was asked for.

Skips when ``MEHO_TEST_KEYCLOAK_IMAGE`` is unset, when Docker is missing,
or when the Go toolchain is missing (CI provides all three).
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.settings import get_settings
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_SHIM = _REPO_ROOT / "clients" / "claude-code-plugin" / "bin" / "meho-mcp-remote"

_REALM = "meho-fresh"
_BACKPLANE_AUDIENCE = "meho-backplane"
_MCP_CLIENT_ID = "meho-mcp"
_MCP_RESOURCE_URI = "https://meho.example.test/mcp"
_TENANT_ID = "22222222-2222-2222-2222-222222222222"
_REDIRECT_URI = "http://127.0.0.1:33418/oauth/callback"
# Throwaway credentials: only ever used inside the test container.
_KC_ADMIN_USER = "test-admin"
_KC_ADMIN_PASSWORD = "test-admin-pass-4002"
_OPERATOR_USER = "operator-4002"
_OPERATOR_PASSWORD = "operator-pass-4002"


def _plugin_default_scopes() -> str:
    """The scope list the Claude Code plugin asks for by default."""
    match = re.search(r'^default_scopes="([^"]+)"$', _PLUGIN_SHIM.read_text(), re.MULTILINE)
    assert match, f"default_scopes not found in {_PLUGIN_SHIM}"
    return match.group(1)


def _admin_token(base_url: str) -> str:
    resp = httpx.post(
        f"{base_url}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": _KC_ADMIN_USER,
            "password": _KC_ADMIN_PASSWORD,
        },
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


def _create_realm_per_docs(base_url: str) -> None:
    """Create the realm and the ``meho-backplane`` client the realm guide starts from."""
    headers = {"Authorization": f"Bearer {_admin_token(base_url)}"}
    # sslRequired=none only because the container is reached over plain
    # HTTP on a non-localhost address; it changes nothing about scopes.
    httpx.post(
        f"{base_url}/admin/realms",
        headers=headers,
        json={"realm": _REALM, "enabled": True, "sslRequired": "none"},
    ).raise_for_status()
    httpx.post(
        f"{base_url}/admin/realms/{_REALM}/clients",
        headers=headers,
        json={
            "clientId": _BACKPLANE_AUDIENCE,
            "publicClient": False,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
        },
    ).raise_for_status()


def _build_meho_cli(out_dir: Path) -> Path:
    binary = out_dir / "meho"
    subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/meho"],
        cwd=_REPO_ROOT / "cli",
        check=True,
    )
    return binary


def _run_bootstrap(binary: Path, base_url: str) -> None:
    env = {
        **os.environ,
        "KEYCLOAK_ADMIN_PASSWORD": _KC_ADMIN_PASSWORD,
        "KEYCLOAK_ADMIN_USER_PASSWORD": _OPERATOR_PASSWORD,
    }
    subprocess.run(
        [
            str(binary),
            "admin",
            "keycloak",
            "bootstrap-clients",
            "--keycloak-base-url",
            base_url,
            "--realm",
            _REALM,
            "--admin-username",
            _KC_ADMIN_USER,
            "--mcp-resource-uri",
            _MCP_RESOURCE_URI,
            "--tenant-id",
            _TENANT_ID,
            "--admin-user-username",
            _OPERATOR_USER,
            "--admin-user-email",
            f"{_OPERATOR_USER}@example.com",
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        check=True,
    )


def _form_action(page: httpx.Response, form_id: str) -> str:
    match = re.search(rf'<form[^>]*id="{form_id}"[^>]*action="([^"]+)"', page.text)
    assert match, f"Keycloak page has no {form_id} form (HTTP {page.status_code})"
    return html.unescape(match.group(1))


def _browser_login(base_url: str, scope: str) -> str:
    """Run the authorization-code + PKCE login for ``meho-mcp``; return the access token.

    Fails with Keycloak's own error text when the login is refused (for
    example ``invalid_scope``), so a regression reads plainly.
    """
    realm_url = f"{base_url}/realms/{_REALM}/protocol/openid-connect"
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    # Keycloak marks its login cookies Secure, and httpx does not send
    # Secure cookies over plain HTTP, so carry them by hand.
    jar: dict[str, str] = {}

    def send(
        client: httpx.Client,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        cookie = "; ".join(f"{name}={value}" for name, value in jar.items())
        resp = client.request(method, url, params=params, data=data, headers={"Cookie": cookie})
        jar.update(resp.cookies.items())
        return resp

    with httpx.Client(follow_redirects=False) as client:
        page = send(
            client,
            "GET",
            f"{realm_url}/auth",
            params={
                "client_id": _MCP_CLIENT_ID,
                "response_type": "code",
                "redirect_uri": _REDIRECT_URI,
                "scope": scope,
                "state": "s4002",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        if page.status_code != 200:
            pytest.fail(
                f"login refused for scope {scope!r}: HTTP {page.status_code} "
                f"Location={page.headers.get('location')}"
            )
        resp = send(
            client,
            "POST",
            _form_action(page, "kc-form-login"),
            data={"username": _OPERATOR_USER, "password": _OPERATOR_PASSWORD},
        )
        location = resp.headers.get("location", "")
        if "required-action" in location:
            # First login of the user bootstrap created: Keycloak's default
            # user profile asks for first and last name. Fill it in, as a
            # person at the browser would.
            page = send(client, "GET", location)
            resp = send(
                client,
                "POST",
                _form_action(page, "kc-update-profile-form"),
                data={
                    "username": _OPERATOR_USER,
                    "email": f"{_OPERATOR_USER}@example.com",
                    "firstName": "Test",
                    "lastName": "Operator",
                },
            )
            location = resp.headers.get("location", "")
        query = parse_qs(urlparse(location).query)
        assert "code" in query, f"no code after login (HTTP {resp.status_code}): {location}"
        token = client.post(
            f"{realm_url}/token",
            data={
                "grant_type": "authorization_code",
                "client_id": _MCP_CLIENT_ID,
                "code": query["code"][0],
                "redirect_uri": _REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
        token.raise_for_status()
        return str(token.json()["access_token"])


@pytest.fixture(scope="module")
def fresh_keycloak(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start an empty Keycloak, set the realm up per the docs, yield its base URL."""
    image = os.environ.get("MEHO_TEST_KEYCLOAK_IMAGE")
    if not image:
        pytest.skip(
            "MEHO_TEST_KEYCLOAK_IMAGE not set; runs in CI where the Keycloak image is provisioned."
        )
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)
    if shutil.which("go") is None:
        pytest.skip("Go toolchain not on PATH; needed to build the meho CLI.")

    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

    binary = _build_meho_cli(tmp_path_factory.mktemp("meho-cli"))
    container = (
        DockerContainer(image)
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", _KC_ADMIN_USER)
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", _KC_ADMIN_PASSWORD)
        .with_command("start-dev")
        .with_exposed_ports(8080)
    )
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"Keycloak container failed to start ({type(exc).__name__}): {exc}")
    try:
        wait_for_log_message(container, "Listening on:", timeout=120)
        base_url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
        _create_realm_per_docs(base_url)
        _run_bootstrap(binary, base_url)
        yield base_url
    finally:
        container.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("add_admin", [False, True], ids=["default-scopes", "with-mcp-admin"])
async def test_plugin_login_succeeds_on_bootstrapped_realm(
    fresh_keycloak: str,
    add_admin: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _plugin_default_scopes()
    if add_admin:
        scope += " mcp:admin"

    access_token = _browser_login(fresh_keycloak, scope)

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{fresh_keycloak}/realms/{_REALM}")
    get_settings.cache_clear()
    clear_jwks_cache()
    operator = await verify_jwt_for_audience(
        f"Bearer {access_token}", expected_audience=_BACKPLANE_AUDIENCE
    )

    assert {"mcp:read", "mcp:execute"} <= operator.scopes
    assert ("mcp:admin" in operator.scopes) is add_admin
    assert str(operator.tenant_id) == _TENANT_ID
