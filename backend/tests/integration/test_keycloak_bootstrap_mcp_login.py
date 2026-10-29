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

#4590 adds the login role groups. ``bootstrap-clients`` creates five
groups (MEHO-Readers, MEHO-Operators, MEHO-SA-Operators, MEHO-Admins,
MEHO-Approvers). Each group carries ``tenant_id`` and ``tenant_role`` as
group attributes, and MEHO-Approvers also carries ``approver=true``. The
stock User Attribute mapper reads them into the token. The tests below
prove, against a real Keycloak:

- a person in each group gets that group's tenant and role in a token
  from ``meho-cli`` and from a real ``meho-mcp`` login; only the
  MEHO-Approvers person gets ``approver``;
- a person in no group gets no ``tenant_id``, and MEHO answers
  ``401 missing_tenant_claim``;
- on a realm made by the older command (fixed-value ``tenant-id`` /
  ``tenant-role`` mappers), a run replaces the fixed mappers, and a
  second run changes nothing.

For several tenants (docs/decisions/2026-10-28-several-tenants-per-person.md)
the role groups also carry ``meho_tenant``, the ``meho-tenants`` mapper
lists them in the ``meho_tenants`` claim, and ``--add-tenant`` makes the
five groups of one more tenant. The tests prove:

- a person in ``MEHO-Readers`` and ``MEHO-lab-b-Operators`` gets both
  tenants in ``meho_tenants``;
- a person in one group gets one entry, and the same ``tenant_id``,
  ``tenant_role`` and ``approver`` as before;
- a person only in ``MEHO-lab-b-*`` groups gets no ``tenant_id``;
- the first admin is in ``MEHO-Platform-Admins`` and gets
  ``platform_admin``;
- a second run with ``--add-tenant`` changes nothing.

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
from fastapi import HTTPException

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
_GROUP_USER_PASSWORD = "group-user-pass-4590"
_UPGRADE_REALM = "meho-upgrade"
_LAB_B_ID = "5d0f7a3e-1c2b-4e8d-9f6a-7b3c2d1e0f9a"

# The five role groups and what a member must get (docs/decisions/login-role-groups.md).
_ROLE_GROUPS = {
    "MEHO-Readers": ("read_only", False),
    "MEHO-Operators": ("operator", False),
    "MEHO-SA-Operators": ("operator", False),
    "MEHO-Admins": ("tenant_admin", False),
    "MEHO-Approvers": ("read_only", True),
}


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


def _admin_headers(base_url: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_admin_token(base_url)}"}


def _create_realm_per_docs(base_url: str, realm: str = _REALM) -> None:
    """Create the realm and the ``meho-backplane`` client the realm guide starts from."""
    headers = _admin_headers(base_url)
    # sslRequired=none only because the container is reached over plain
    # HTTP on a non-localhost address; it changes nothing about scopes.
    httpx.post(
        f"{base_url}/admin/realms",
        headers=headers,
        json={"realm": realm, "enabled": True, "sslRequired": "none"},
    ).raise_for_status()
    httpx.post(
        f"{base_url}/admin/realms/{realm}/clients",
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


def _run_bootstrap(binary: Path, base_url: str, realm: str = _REALM, *extra: str) -> str:
    """Run ``bootstrap-clients`` against *realm*; return its stdout."""
    env = {
        **os.environ,
        "KEYCLOAK_ADMIN_PASSWORD": _KC_ADMIN_PASSWORD,
        "KEYCLOAK_ADMIN_USER_PASSWORD": _OPERATOR_PASSWORD,
    }
    proc = subprocess.run(
        [
            str(binary),
            "admin",
            "keycloak",
            "bootstrap-clients",
            "--keycloak-base-url",
            base_url,
            "--realm",
            realm,
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
            *extra,
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"bootstrap-clients failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def _client_uuid(base_url: str, realm: str, client_id: str) -> str:
    resp = httpx.get(
        f"{base_url}/admin/realms/{realm}/clients",
        headers=_admin_headers(base_url),
        params={"clientId": client_id},
    )
    resp.raise_for_status()
    return str(resp.json()[0]["id"])


def _client_mappers(base_url: str, realm: str, client_id: str) -> dict[str, dict]:
    uuid = _client_uuid(base_url, realm, client_id)
    resp = httpx.get(
        f"{base_url}/admin/realms/{realm}/clients/{uuid}/protocol-mappers/models",
        headers=_admin_headers(base_url),
    )
    resp.raise_for_status()
    return {m["name"]: m for m in resp.json()}


def _group_id(base_url: str, realm: str, name: str) -> str:
    resp = httpx.get(
        f"{base_url}/admin/realms/{realm}/groups",
        headers=_admin_headers(base_url),
        params={"search": name},
    )
    resp.raise_for_status()
    return str(next(g["id"] for g in resp.json() if g["name"] == name))


def _create_user(base_url: str, username: str, group: str | None, *more: str) -> str:
    """Create a user with a password (and a full profile); put it in *group* and *more*."""
    headers = _admin_headers(base_url)
    httpx.post(
        f"{base_url}/admin/realms/{_REALM}/users",
        headers=headers,
        json={
            "username": username,
            "email": f"{username}@example.com",
            "firstName": "Test",
            "lastName": "User",
            "enabled": True,
            "emailVerified": True,
            "credentials": [
                {"type": "password", "value": _GROUP_USER_PASSWORD, "temporary": False}
            ],
        },
    ).raise_for_status()
    found = httpx.get(
        f"{base_url}/admin/realms/{_REALM}/users",
        headers=headers,
        params={"username": username, "exact": "true"},
    )
    found.raise_for_status()
    user_id = str(found.json()[0]["id"])
    for name in ([group] if group is not None else []) + list(more):
        httpx.put(
            f"{base_url}/admin/realms/{_REALM}/users/{user_id}/groups/"
            f"{_group_id(base_url, _REALM, name)}",
            headers=headers,
        ).raise_for_status()
    return user_id


def _example_access_token(base_url: str, client_id: str, user_id: str) -> dict:
    """The access token Keycloak would issue to *user_id* on *client_id*.

    Keycloak builds it with the client's real protocol mappers (the same
    code path as a login), so it shows which claims the mappers emit.
    """
    uuid = _client_uuid(base_url, _REALM, client_id)
    resp = httpx.get(
        f"{base_url}/admin/realms/{_REALM}/clients/{uuid}"
        "/evaluate-scopes/generate-example-access-token",
        headers=_admin_headers(base_url),
        params={"userId": user_id, "scope": "openid"},
    )
    resp.raise_for_status()
    return dict(resp.json())


async def _verify(base_url: str, access_token: str, monkeypatch: pytest.MonkeyPatch):
    """Check *access_token* with the backplane's own JWT check."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{base_url}/realms/{_REALM}")
    get_settings.cache_clear()
    clear_jwks_cache()
    return await verify_jwt_for_audience(
        f"Bearer {access_token}", expected_audience=_BACKPLANE_AUDIENCE
    )


def _form_action(page: httpx.Response, form_id: str) -> str:
    match = re.search(rf'<form[^>]*id="{form_id}"[^>]*action="([^"]+)"', page.text)
    assert match, f"Keycloak page has no {form_id} form (HTTP {page.status_code})"
    return html.unescape(match.group(1))


def _browser_login(
    base_url: str,
    scope: str,
    username: str = _OPERATOR_USER,
    password: str = _OPERATOR_PASSWORD,
) -> str:
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
            data={"username": username, "password": password},
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
                    "username": username,
                    "email": f"{username}@example.com",
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
def meho_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the ``meho`` CLI from this checkout once per module."""
    if not os.environ.get("MEHO_TEST_KEYCLOAK_IMAGE"):
        pytest.skip(
            "MEHO_TEST_KEYCLOAK_IMAGE not set; runs in CI where the Keycloak image is provisioned."
        )
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)
    if shutil.which("go") is None:
        pytest.skip("Go toolchain not on PATH; needed to build the meho CLI.")
    return _build_meho_cli(tmp_path_factory.mktemp("meho-cli"))


@pytest.fixture(scope="module")
def fresh_keycloak(meho_binary: Path) -> Iterator[str]:
    """Start an empty Keycloak, set the realm up per the docs, yield its base URL."""
    image = os.environ["MEHO_TEST_KEYCLOAK_IMAGE"]

    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

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
        _run_bootstrap(meho_binary, base_url)
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
    operator = await _verify(fresh_keycloak, access_token, monkeypatch)

    assert {"mcp:read", "mcp:execute"} <= operator.scopes
    assert ("mcp:admin" in operator.scopes) is add_admin
    # The admin user bootstrap made is in MEHO-Admins and MEHO-Platform-Admins.
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role.value == "tenant_admin"
    assert operator.approver is False
    assert operator.platform_admin is True


@pytest.mark.asyncio
@pytest.mark.parametrize("group", list(_ROLE_GROUPS))
async def test_role_group_gives_tenant_and_role(
    fresh_keycloak: str,
    group: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person in one role group gets that group's tenant and role."""
    want_role, want_approver = _ROLE_GROUPS[group]
    username = f"user-{group.lower()}"
    user_id = _create_user(fresh_keycloak, username, group)

    # The token meho-cli would issue (meho-cli is device-code only, so
    # Keycloak's own token preview stands in for the device login).
    claims = _example_access_token(fresh_keycloak, "meho-cli", user_id)
    assert claims.get("tenant_id") == _TENANT_ID
    assert claims.get("tenant_role") == want_role
    # One group, one tenant in the list.
    role = group.removeprefix("MEHO-")
    assert claims.get("meho_tenants") == [f"{_TENANT_ID}:main:{role}"]
    assert "platform_admin" not in claims
    if want_approver:
        assert claims.get("approver") is True
    else:
        assert "approver" not in claims

    # A real meho-mcp login, checked by the backplane's own JWT check.
    access_token = _browser_login(
        fresh_keycloak, _plugin_default_scopes(), username, _GROUP_USER_PASSWORD
    )
    operator = await _verify(fresh_keycloak, access_token, monkeypatch)
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role.value == want_role
    assert operator.approver is want_approver


@pytest.mark.asyncio
async def test_user_in_no_group_is_refused(
    fresh_keycloak: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person in no role group gets no tenant, and MEHO refuses the token."""
    username = "user-no-group"
    user_id = _create_user(fresh_keycloak, username, None)

    claims = _example_access_token(fresh_keycloak, "meho-cli", user_id)
    for claim in ("tenant_id", "tenant_role", "approver"):
        assert claim not in claims

    access_token = _browser_login(
        fresh_keycloak, _plugin_default_scopes(), username, _GROUP_USER_PASSWORD
    )
    with pytest.raises(HTTPException) as refused:
        await _verify(fresh_keycloak, access_token, monkeypatch)
    assert refused.value.status_code == 401
    assert refused.value.detail.startswith("missing_tenant_claim: ")
    assert "role group" in refused.value.detail


def _old_fixed_mapper(name: str, claim: str, value: str) -> dict:
    """A fixed-value mapper as the older ``bootstrap-clients`` made it."""
    return {
        "name": name,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "config": {
            "claim.name": claim,
            "claim.value": value,
            "jsonType.label": "String",
            "id.token.claim": "false",
            "access.token.claim": "true",
            "userinfo.token.claim": "false",
        },
    }


def test_upgrade_replaces_fixed_mappers_and_second_run_changes_nothing(
    fresh_keycloak: str,
    meho_binary: Path,
) -> None:
    """A run on a realm made by the older command removes the fixed values."""
    _create_realm_per_docs(fresh_keycloak, _UPGRADE_REALM)
    headers = _admin_headers(fresh_keycloak)
    for client_id in ("meho-cli", "meho-mcp"):
        httpx.post(
            f"{fresh_keycloak}/admin/realms/{_UPGRADE_REALM}/clients",
            headers=headers,
            json={
                "clientId": client_id,
                "publicClient": True,
                "protocolMappers": [
                    _old_fixed_mapper("tenant-id", "tenant_id", _TENANT_ID),
                    _old_fixed_mapper("tenant-role", "tenant_role", "tenant_admin"),
                ],
            },
        ).raise_for_status()
    httpx.post(
        f"{fresh_keycloak}/admin/realms/{_UPGRADE_REALM}/groups",
        headers=headers,
        json={"name": "meho-admins"},
    ).raise_for_status()

    first = _run_bootstrap(meho_binary, fresh_keycloak, _UPGRADE_REALM)
    assert first.count("replaced oidc-hardcoded-claim-mapper") == 4, first

    for client_id in ("meho-cli", "meho-mcp"):
        mappers = _client_mappers(fresh_keycloak, _UPGRADE_REALM, client_id)
        fixed = [
            n for n, m in mappers.items() if m["protocolMapper"] == "oidc-hardcoded-claim-mapper"
        ]
        assert fixed == [], f"{client_id} still has fixed-value mappers: {fixed}"
        for name in ("tenant-id", "tenant-role", "approver"):
            assert mappers[name]["protocolMapper"] == "oidc-usermodel-attribute-mapper"

    second = _run_bootstrap(meho_binary, fresh_keycloak, _UPGRADE_REALM)
    changed = [
        line
        for line in second.splitlines()
        if re.search(r"\[(created|updated)\] (mapper|group|user|client scope)", line)
    ]
    assert changed == [], f"second run changed the realm:\n{second}"


@pytest.fixture(scope="module")
def lab_b_runs(fresh_keycloak: str, meho_binary: Path) -> tuple[str, str]:
    """Add the tenant ``lab-b`` twice; return the output of both runs."""
    extra = ("--add-tenant", "lab-b", "--add-tenant-id", _LAB_B_ID)
    first = _run_bootstrap(meho_binary, fresh_keycloak, _REALM, *extra)
    second = _run_bootstrap(meho_binary, fresh_keycloak, _REALM, *extra)
    return first, second


def test_add_tenant_twice_changes_nothing_the_second_time(lab_b_runs: tuple[str, str]) -> None:
    first, second = lab_b_runs
    assert "[created] group MEHO-lab-b-Operators" in first, first
    changed = [
        line
        for line in second.splitlines()
        if re.search(r"\[(created|updated)\] (mapper|group|user|client scope)", line)
    ]
    assert changed == [], f"second run changed the realm:\n{second}"


def test_person_in_two_tenants_gets_both_in_meho_tenants(
    fresh_keycloak: str, lab_b_runs: tuple[str, str]
) -> None:
    user_id = _create_user(
        fresh_keycloak, "user-two-tenants", "MEHO-Readers", "MEHO-lab-b-Operators"
    )
    for client_id in ("meho-cli", "meho-mcp"):
        claims = _example_access_token(fresh_keycloak, client_id, user_id)
        assert sorted(claims["meho_tenants"]) == sorted(
            [f"{_TENANT_ID}:main:Readers", f"{_LAB_B_ID}:lab-b:Operators"]
        ), client_id
        # The claims MEHO reads today still come from the first tenant's group.
        assert claims.get("tenant_id") == _TENANT_ID
        assert claims.get("tenant_role") == "read_only"


def test_person_only_in_extra_tenant_gets_no_tenant_id(
    fresh_keycloak: str, lab_b_runs: tuple[str, str]
) -> None:
    user_id = _create_user(fresh_keycloak, "user-lab-b-only", "MEHO-lab-b-Admins")
    claims = _example_access_token(fresh_keycloak, "meho-cli", user_id)
    assert claims.get("meho_tenants") == [f"{_LAB_B_ID}:lab-b:Admins"]
    for name in ("tenant_id", "tenant_role", "approver", "platform_admin"):
        assert name not in claims, name
