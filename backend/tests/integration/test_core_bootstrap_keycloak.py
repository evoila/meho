# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""#4043 — the realm stage against a real Keycloak, then the first approval.

Keycloak starts the way the chart starts it: the chart's own realm file
imported at the first start, a temporary bootstrap service account, the
realm under ``/auth``. The realm stage then runs twice, as the post-install
and a post-upgrade hook would. The tests prove:

- the second run leaves every client secret and the first-login Secret as
  they were, and neither run prints a secret;
- the bootstrap account is gone, and each person is in exactly one group;
- the first admin logs in with the temporary password from the first-login
  Secret, must choose a new one, and MEHO's own JWT check gives
  ``tenant_admin`` with ``platform_admin`` in the first tenant;
- a credential-write operation the first admin parks for approval cannot be
  approved by the admin alone, and the second person approves it.

Skips when ``MEHO_TEST_KEYCLOAK_IMAGE`` is unset or Docker is missing (CI
provides both).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html
import io
import os
import re
import secrets
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.core.bootstrap import Plan, load_realm, reconcile
from meho_backplane.core.secrets import StoredSecret
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    SelfApprovalForbiddenError,
    approve_request,
    create_pending_request,
)
from meho_backplane.settings import get_settings
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

from .conftest import pg_engine  # noqa: F401 — pytest-discovered fixture

_REALM_DIR = (
    Path(__file__).resolve().parents[3]
    / "deploy"
    / "charts"
    / "meho"
    / "charts"
    / "keycloak"
    / "files"
)
# A tenant ``pg_engine`` seeds, so the approval rows have a tenant to belong to.
_TENANT = "22222222-2222-2222-2222-222222222222"
_SECRET_NAME = "it-core-first-login"
_REDIRECT_URI = "http://127.0.0.1:33418/oauth/callback"


def _alnum() -> str:
    return secrets.token_hex(16)


# Throwaway values, only ever used inside the test container.
_ENV = {
    "KC_BOOTSTRAP_ADMIN_CLIENT_ID": f"bootstrap{_alnum()}",
    "KC_BOOTSTRAP_ADMIN_CLIENT_SECRET": _alnum(),
    "MEHO_REALM_ADMIN_CLIENT_SECRET": _alnum(),
    "MEHO_REALM_WEB_CLIENT_SECRET": _alnum(),
    "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET": _alnum(),
    "MEHO_REALM_PUBLIC_URL": "https://meho.example.test",
}

_GROUPS = [
    {"name": "MEHO-Readers", "tenant_role": "read_only"},
    {"name": "MEHO-Operators", "tenant_role": "operator"},
    {"name": "MEHO-SA-Operators", "tenant_role": "operator"},
    {"name": "MEHO-Admins", "tenant_role": "tenant_admin"},
    {"name": "MEHO-Approvers", "tenant_role": "read_only", "approver": True},
]


class _MemoryStore:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, str]] = {}
        self.annotations: dict[str, dict[str, str]] = {}

    async def read_data(self, name: str) -> StoredSecret | None:
        if name not in self.data:
            return None
        return StoredSecret(data=dict(self.data[name]), annotations=dict(self.annotations[name]))

    async def create(self, name: str, data: Mapping[str, str], secret_type: str) -> bool:
        if name in self.data:
            return False
        self.data[name] = dict(data)
        self.annotations[name] = {}
        return True

    async def annotate(self, name: str, annotations: Mapping[str, str]) -> None:
        self.annotations[name].update(annotations)


@dataclass(frozen=True)
class Bootstrapped:
    base_url: str
    store: _MemoryStore
    outputs: tuple[str, str]
    client_secrets: tuple[dict[str, str], dict[str, str]]
    first_login: tuple[dict[str, str], dict[str, str]]


def _admin_headers(base_url: str) -> dict[str, str]:
    resp = httpx.post(
        f"{base_url}/realms/meho/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "meho-admin",
            "client_secret": _ENV["MEHO_REALM_ADMIN_CLIENT_SECRET"],
        },
    )
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _client_secrets(base_url: str) -> dict[str, str]:
    headers = _admin_headers(base_url)
    out = {}
    for client_id in ("meho-admin", "meho-web", "meho-check-runner", "meho-backplane"):
        found = httpx.get(
            f"{base_url}/admin/realms/meho/clients",
            headers=headers,
            params={"clientId": client_id},
        ).json()
        secret = httpx.get(
            f"{base_url}/admin/realms/meho/clients/{found[0]['id']}/client-secret",
            headers=headers,
        ).json()
        out[client_id] = str(secret["value"])
    return out


def _run_stage(base_url: str, store: _MemoryStore) -> str:
    plan = Plan.model_validate(
        {
            "keycloak_url": base_url,
            "tenant_id": _TENANT,
            "groups": _GROUPS,
            "first_login_secret": _SECRET_NAME,
            "admin": {"username": "admin", "group": "MEHO-Admins"},
            "approver": {"username": "approver", "group": "MEHO-SA-Operators"},
        }
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(reconcile(plan, load_realm(_REALM_DIR / "meho-realm.json", _ENV), _ENV, store))
    return out.getvalue()


def _chart_keycloak_image(image: str) -> str:
    """*image*'s registry and repository with the tag the chart pins.

    The chart's realm file needs the chart's Keycloak: on 26.0 (the
    suite-wide ``MEHO_TEST_KEYCLOAK_IMAGE``) the import makes no ``basic``
    scope, so tokens carry no ``sub``.
    """
    values = yaml.safe_load((_REALM_DIR.parent / "values.yaml").read_text())
    name, _, tag = image.rpartition(":")
    repository = name if "/" not in tag else image
    return f"{repository}:{values['image']['tag']}"


@pytest.fixture(scope="module")
def bootstrapped() -> Iterator[Bootstrapped]:
    """Start Keycloak like the chart does, run the realm stage twice."""
    image = os.environ.get("MEHO_TEST_KEYCLOAK_IMAGE")
    if not image:
        pytest.skip(
            "MEHO_TEST_KEYCLOAK_IMAGE not set; runs in CI where the Keycloak image is provisioned."
        )
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

    container = DockerContainer(_chart_keycloak_image(image)).with_command(
        "start-dev --import-realm"
    )
    for name, value in _ENV.items():
        container = container.with_env(name, value)
    container = (
        container.with_env("KC_HTTP_RELATIVE_PATH", "/auth")
        .with_volume_mapping(str(_REALM_DIR), "/opt/keycloak/data/import", "ro")
        .with_exposed_ports(8080)
    )
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"Keycloak container failed to start ({type(exc).__name__}): {exc}")
    try:
        wait_for_log_message(container, "Listening on:", timeout=180)
        host, port = container.get_container_host_ip(), container.get_exposed_port(8080)
        base_url = f"http://{host}:{port}/auth"
        store = _MemoryStore()
        first_out = _run_stage(base_url, store)
        first = (_client_secrets(base_url), dict(store.data[_SECRET_NAME]))
        second_out = _run_stage(base_url, store)
        second = (_client_secrets(base_url), dict(store.data[_SECRET_NAME]))
        yield Bootstrapped(
            base_url=base_url,
            store=store,
            outputs=(first_out, second_out),
            client_secrets=(first[0], second[0]),
            first_login=(first[1], second[1]),
        )
    finally:
        container.stop()


def _form(page: httpx.Response, form_id: str) -> str | None:
    match = re.search(rf'<form[^>]*id="{form_id}"[^>]*action="([^"]+)"', page.text)
    return html.unescape(match.group(1)) if match else None


def _first_login(base_url: str, username: str, password: str, new_password: str) -> str:
    """Browser login (code + PKCE) for ``meho-mcp`` with a temporary password.

    Keycloak asks for a new password (the temporary one), and for the profile
    fields the default user profile needs. Returns the access token.
    """
    realm_url = f"{base_url}/realms/meho/protocol/openid-connect"
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    # Keycloak marks its login cookies Secure; httpx does not send them over HTTP.
    jar: dict[str, str] = {}

    def send(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
        cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
        resp = client.request(method, url, headers={"Cookie": cookie}, **kwargs)
        jar.update(resp.cookies.items())
        return resp

    with httpx.Client(follow_redirects=False) as client:
        page = send(
            client,
            "GET",
            f"{realm_url}/auth",
            params={
                "client_id": "meho-mcp",
                "response_type": "code",
                "redirect_uri": _REDIRECT_URI,
                "scope": "openid",
                "state": "s4043",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        action = _form(page, "kc-form-login")
        assert action, f"no login form (HTTP {page.status_code})"
        resp = send(client, "POST", action, data={"username": username, "password": password})
        asked_for_password = False
        for _ in range(4):
            location = resp.headers.get("location", "")
            if "code=" in location:
                break
            page = send(client, "GET", location) if location else resp
            if action := _form(page, "kc-passwd-update-form"):
                asked_for_password = True
                data = {"password-new": new_password, "password-confirm": new_password}
            elif action := _form(page, "kc-update-profile-form"):
                data = {
                    "username": username,
                    "email": f"{username}@example.com",
                    "firstName": "First",
                    "lastName": "Login",
                }
            else:
                pytest.fail(f"unexpected Keycloak page (HTTP {page.status_code}): {location}")
            resp = send(client, "POST", action, data=data)
        assert asked_for_password, "Keycloak did not ask to change the temporary password"
        query = parse_qs(urlparse(resp.headers["location"]).query)
        token = client.post(
            f"{realm_url}/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "meho-mcp",
                "code": query["code"][0],
                "redirect_uri": _REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
        token.raise_for_status()
        return str(token.json()["access_token"])


async def _verify(base_url: str, token: str, monkeypatch: pytest.MonkeyPatch) -> Operator:
    """Check *token* with the backplane's own JWT check, wired like the chart."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{base_url}/realms/meho")
    monkeypatch.setenv("JWT_PLATFORM_ADMIN_ROLE_NAME", "platform_admin")
    get_settings.cache_clear()
    clear_jwks_cache()
    return await verify_jwt_for_audience(f"Bearer {token}", expected_audience="meho-backplane")


def test_second_run_keeps_every_client_secret_and_the_first_login_secret(
    bootstrapped: Bootstrapped,
) -> None:
    assert bootstrapped.client_secrets[0] == bootstrapped.client_secrets[1]
    assert bootstrapped.first_login[0] == bootstrapped.first_login[1]
    secrets_now = bootstrapped.client_secrets[1]
    assert secrets_now["meho-admin"] == _ENV["MEHO_REALM_ADMIN_CLIENT_SECRET"]
    assert secrets_now["meho-web"] == _ENV["MEHO_REALM_WEB_CLIENT_SECRET"]
    assert secrets_now["meho-check-runner"] == _ENV["MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET"]
    second = bootstrapped.outputs[1]
    assert "created" not in second
    assert "secret set" not in second


def test_job_output_holds_no_secret(bootstrapped: Bootstrapped) -> None:
    values = [v for k, v in _ENV.items() if k != "MEHO_REALM_PUBLIC_URL"]
    values += [v for k, v in bootstrapped.first_login[1].items() if k.endswith("_password")]
    values.append(bootstrapped.client_secrets[1]["meho-backplane"])
    for output in bootstrapped.outputs:
        for value in values:
            assert value not in output
    assert "user admin: created in MEHO-Admins" in bootstrapped.outputs[0]
    assert "bootstrap account: deleted" in bootstrapped.outputs[0]
    assert "bootstrap account: gone" in bootstrapped.outputs[1]


def test_bootstrap_account_is_gone_and_each_person_is_in_one_group(
    bootstrapped: Bootstrapped,
) -> None:
    refused = httpx.post(
        f"{bootstrapped.base_url}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _ENV["KC_BOOTSTRAP_ADMIN_CLIENT_ID"],
            "client_secret": _ENV["KC_BOOTSTRAP_ADMIN_CLIENT_SECRET"],
        },
    )
    assert refused.status_code in (400, 401)
    headers = _admin_headers(bootstrapped.base_url)
    for username, group in (("admin", "MEHO-Admins"), ("approver", "MEHO-SA-Operators")):
        user = httpx.get(
            f"{bootstrapped.base_url}/admin/realms/meho/users",
            headers=headers,
            params={"username": username, "exact": "true"},
        ).json()[0]
        groups = httpx.get(
            f"{bootstrapped.base_url}/admin/realms/meho/users/{user['id']}/groups",
            headers=headers,
        ).json()
        assert [g["name"] for g in groups] == [group]
        assert user["requiredActions"] == ["UPDATE_PASSWORD"]


@pytest.fixture(scope="module")
def first_logins(bootstrapped: Bootstrapped) -> dict[str, str]:
    """Both people log in once with their temporary passwords; their tokens."""
    stored = bootstrapped.first_login[1]
    return {
        who: _first_login(
            bootstrapped.base_url,
            stored[f"{who}_username"],
            stored[f"{who}_password"],
            f"New-{secrets.token_hex(12)}",
        )
        for who in ("admin", "approver")
    }


@pytest.mark.asyncio
async def test_first_admin_logs_in_as_tenant_admin_with_platform_admin(
    bootstrapped: Bootstrapped,
    first_logins: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = await _verify(bootstrapped.base_url, first_logins["admin"], monkeypatch)
    assert str(admin.tenant_id) == _TENANT
    assert admin.tenant_role is TenantRole.TENANT_ADMIN
    assert admin.platform_admin is True

    approver = await _verify(bootstrapped.base_url, first_logins["approver"], monkeypatch)
    assert str(approver.tenant_id) == _TENANT
    assert approver.tenant_role is TenantRole.OPERATOR
    assert approver.platform_admin is False


@pytest.mark.asyncio
async def test_second_person_approves_the_first_admins_credential_write(
    bootstrapped: Bootstrapped,
    first_logins: dict[str, str],
    pg_engine: None,  # noqa: F811 — fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", Fernet.generate_key().decode())
    admin = await _verify(bootstrapped.base_url, first_logins["admin"], monkeypatch)
    approver = await _verify(bootstrapped.base_url, first_logins["approver"], monkeypatch)
    assert admin.sub != approver.sub

    params: dict[str, object] = {"path": "secret/meho/day-one", "data": {"token": "value"}}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=admin,
            connector_id="vault-1.x",
            op_id="vault.kv.put",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        await session.commit()
    # A credential write waits with its input encrypted, not in the row.
    assert request.params is None
    request_id: uuid.UUID = request.id

    # One admin alone cannot approve their own change.
    async with get_sessionmaker()() as session:
        with pytest.raises(SelfApprovalForbiddenError):
            await approve_request(session, request_id, operator=admin)

    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request_id, operator=approver)
        await session.commit()
    assert approved.status == ApprovalRequestStatus.APPROVED.value
