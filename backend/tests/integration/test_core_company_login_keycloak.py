# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""#4592 — the company login of the realm stage against a real Keycloak.

One Keycloak runs two realms. Realm ``meho`` comes from the subchart's realm
file, as in ``test_core_bootstrap_keycloak.py``, plus the two roles that
the chart's realm ConfigMap gives ``meho-admin`` when the company login is
on. A second realm, ``company``, plays the company login system: it has a
confidential client for MEHO's Keycloak, two groups and one person. The
realm stage then sets up the company login in realm ``meho`` from the
second realm's discovery document, exactly as the Job does (the stage logs
in as ``meho-admin``, so this also proves that the two roles are enough).

The test drives a browser login with ``httpx``: realm ``meho`` sends the
person straight to the company login (auto-redirect), the person types the
password there, and Keycloak sends them back with a MEHO token. Keycloak
reads the company login's addresses inside the container
(``http://localhost:8080``); the test's browser reaches the same Keycloak at
the mapped port, so it swaps that prefix in every address it calls. Realm
``company`` has that inside address as its front-end address, so its
tokens carry one issuer, whichever port the browser used.

1. A person in a mapped company group gets ``tenant_id``, ``tenant_role``
   and ``groups`` of the mapped MEHO group.
2. After the person leaves the company group and logs in again, the fields
   are gone.
3. A second run changes nothing, and no secret is in the output.
4. A changed ``groupMap`` line changes its mapper and takes the person out
   of the old group; the next login gives the new group only.
5. A removed line removes its mapper and takes the person out of its group;
   the next login gives no MEHO group.
6. With ``autoRedirect`` off, the Keycloak login form shows again, also
   when the request for the discovery document times out.

Set ``MEHO_TEST_CORE_KEYCLOAK_IMAGE`` to use a mirror of the subchart's
image. Skips when Docker is missing or the container cannot start.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import pytest
import yaml

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.operator import TenantRole
from meho_backplane.core.company_login import (
    CLEANUP_CONFIG_KEY,
    MAPPER_PREFIX,
    ROLES,
    SECRET_ENV,
)
from meho_backplane.core.keycloak import (
    ADMIN_SECRET_ENV,
    HttpAdmin,
    Plan,
    RealmStageError,
    finish_realm,
)
from meho_backplane.settings import get_settings
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

_SUBCHART = (
    Path(__file__).resolve().parents[3] / "deploy" / "charts" / "meho" / "charts" / "keycloak"
)
_PUBLIC_URL = "https://meho.example.test"
_TENANT_ID = "6b0d7c55-41a8-4b52-9a8e-2f3c1d0e9a77"
# How Keycloak reaches itself inside the container.
_INSIDE = "http://localhost:8080/auth"
_DISCOVERY = f"{_INSIDE}/realms/company/.well-known/openid-configuration"
_BROKER_CLIENT = "meho-broker"
_CALLBACK = "http://127.0.0.1:43123/callback"
_PERSON = "jane"


@dataclass
class _Keycloak:
    base_url: str
    env: dict[str, str]
    bootstrap: tuple[str, str]
    password: str
    logs: Callable[[], str]
    output: str = ""


class _FakeStore:
    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, str]] = {}

    async def read_data(self, name: str) -> dict[str, str] | None:
        data = self.secrets.get(name)
        return None if data is None else dict(data)

    async def create(self, name: str, data: Mapping[str, str], secret_type: str) -> bool:
        if name in self.secrets:
            return False
        self.secrets[name] = dict(data)
        return True


def _subchart_image() -> str:
    image = yaml.safe_load((_SUBCHART / "values.yaml").read_text())["image"]
    return f"{image['repository']}@{image['digest']}"


def _wait_for_realm(base_url: str, logs: Callable[[], str], timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        with contextlib.suppress(httpx.TransportError):
            if httpx.get(f"{base_url}/realms/meho/.well-known/openid-configuration").is_success:
                return
        if time.monotonic() > deadline:
            pytest.fail(f"Keycloak did not serve realm meho in time. Keycloak log:\n{logs()}")
        time.sleep(2)


def _master_headers(kc: _Keycloak) -> dict[str, str]:
    client_id, client_secret = kc.bootstrap
    resp = httpx.post(
        f"{kc.base_url}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _company_realm(password: str, broker_secret: str) -> dict[str, Any]:
    return {
        "realm": "company",
        "enabled": True,
        "sslRequired": "none",
        "attributes": {"frontendUrl": _INSIDE},
        "groups": [{"name": "acme-ops"}, {"name": "acme-readers"}],
        "clients": [
            {
                "clientId": _BROKER_CLIENT,
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
                "clientAuthenticatorType": "client-secret",
                "secret": broker_secret,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "redirectUris": ["*"],
                "protocolMappers": [
                    {
                        "name": "groups",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-group-membership-mapper",
                        "config": {
                            "claim.name": "groups",
                            "full.path": "false",
                            "id.token.claim": "true",
                            "access.token.claim": "true",
                            "userinfo.token.claim": "true",
                        },
                    }
                ],
            }
        ],
        "users": [
            {
                "username": _PERSON,
                "enabled": True,
                "email": "jane@example.com",
                "emailVerified": True,
                "firstName": "Jane",
                "lastName": "Doe",
                "credentials": [{"type": "password", "value": password, "temporary": False}],
                "groups": ["/acme-ops"],
            }
        ],
    }


def _give_meho_admin_the_company_login_roles(kc: _Keycloak) -> None:
    """What the chart's realm ConfigMap does with the company login on.

    The realm file here is the one of an install without the company login,
    so the two roles are given after the import, with the first-start
    account, which the Job's account cannot do for itself.
    """
    headers = _master_headers(kc)
    admin = f"{kc.base_url}/admin/realms/meho"

    def client_uuid(client_id: str) -> str:
        found = httpx.get(f"{admin}/clients", headers=headers, params={"clientId": client_id})
        found.raise_for_status()
        (client,) = found.json()
        return str(client["id"])

    management = client_uuid("realm-management")
    roles = []
    for role in ROLES:
        rep = httpx.get(f"{admin}/clients/{management}/roles/{role}", headers=headers)
        rep.raise_for_status()
        roles.append(rep.json())
    account = httpx.get(
        f"{admin}/clients/{client_uuid('meho-admin')}/service-account-user", headers=headers
    )
    account.raise_for_status()
    httpx.post(
        f"{admin}/users/{account.json()['id']}/role-mappings/clients/{management}",
        headers=headers,
        json=roles,
    ).raise_for_status()


@pytest.fixture(scope="module")
def keycloak() -> Iterator[_Keycloak]:
    """Keycloak with realm ``meho`` imported and realm ``company`` added."""
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

    bootstrap = ("meho-bootstrap", secrets.token_hex(16))
    env = {
        "MEHO_REALM_PUBLIC_URL": _PUBLIC_URL,
        "MEHO_REALM_TENANT_ID": _TENANT_ID,
        "MEHO_REALM_TENANT_NAME": "main",
        ADMIN_SECRET_ENV: secrets.token_hex(16),
        "MEHO_REALM_WEB_CLIENT_SECRET": secrets.token_hex(16),
        "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET": secrets.token_hex(16),
        SECRET_ENV: secrets.token_hex(16),
    }
    image = os.environ.get("MEHO_TEST_CORE_KEYCLOAK_IMAGE") or _subchart_image()
    container = (
        DockerContainer(image)
        .with_command("start-dev --import-realm")
        .with_env("KC_HTTP_RELATIVE_PATH", "/auth")
        .with_env("KC_BOOTSTRAP_ADMIN_CLIENT_ID", bootstrap[0])
        .with_env("KC_BOOTSTRAP_ADMIN_CLIENT_SECRET", bootstrap[1])
        .with_volume_mapping(str(_SUBCHART / "files"), "/opt/keycloak/data/import", "ro")
        .with_exposed_ports(8080)
    )
    for name in (
        "MEHO_REALM_PUBLIC_URL",
        "MEHO_REALM_TENANT_ID",
        "MEHO_REALM_TENANT_NAME",
        ADMIN_SECRET_ENV,
        "MEHO_REALM_WEB_CLIENT_SECRET",
        "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET",
    ):
        container = container.with_env(name, env[name])
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"Keycloak container failed to start ({type(exc).__name__}): {exc}")
    try:

        def logs() -> str:
            out, err = container.get_logs()
            lines = (out + err).decode(errors="replace").splitlines()
            return "\n".join(line for line in lines if "WARN" in line or "ERROR" in line)[-4000:]

        wait_for_log_message(container, "Listening on:", timeout=240)
        host = container.get_container_host_ip()
        base_url = f"http://{host}:{container.get_exposed_port(8080)}/auth"
        _wait_for_realm(base_url, logs)

        kc = _Keycloak(
            base_url=base_url,
            env=env,
            bootstrap=bootstrap,
            password=secrets.token_hex(12),
            logs=logs,
        )
        _give_meho_admin_the_company_login_roles(kc)
        httpx.post(
            f"{base_url}/admin/realms",
            headers=_master_headers(kc),
            json=_company_realm(kc.password, env[SECRET_ENV]),
        ).raise_for_status()
        yield kc
    finally:
        container.stop()


@pytest.fixture(scope="module")
def store() -> _FakeStore:
    return _FakeStore()


def _plan(**company: Any) -> Plan:
    return Plan.model_validate(
        {
            "keycloak_url": "unused-here",
            "realm_file": str(_SUBCHART / "files" / "meho-realm.json"),
            "first_login_secret": "rel-core-first-login",
            "admin_username": "admin",
            "approver_username": "approver",
            "company_login": {
                "enabled": True,
                "discovery_url": _DISCOVERY,
                "client_id": _BROKER_CLIENT,
                "group_map": {"acme-ops": "MEHO-Operators", "acme-readers": "MEHO-Readers"},
                **company,
            },
        }
    )


async def _run_stage(kc: _Keycloak, store: _FakeStore, **company: Any) -> None:
    # No first-start account in the env: the stage logs in as meho-admin, as
    # every run after the first one does.
    async with httpx.AsyncClient(base_url=kc.base_url, timeout=30) as http:
        await finish_realm(_plan(**company), kc.env, HttpAdmin(http), store)


class _Browser:
    """Just enough browser for Keycloak's login pages.

    It keeps its own cookies by name and path: Keycloak marks its cookies
    ``Secure`` when the host is ``localhost``, and httpx would then not send
    them over plain HTTP.
    """

    def __init__(self, kc: _Keycloak) -> None:
        self.kc = kc
        self.http = httpx.Client(timeout=30)
        self.cookies: dict[tuple[str, str], str] = {}

    def close(self) -> None:
        self.http.close()

    def send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        url = url.replace(_INSIDE, self.kc.base_url)
        path = urlsplit(url).path
        cookie = "; ".join(f"{n}={v}" for (n, p), v in self.cookies.items() if path.startswith(p))
        headers = {"Cookie": cookie} if cookie else {}
        response = self.http.request(method, url, headers=headers, **kwargs)
        self.http.cookies.clear()
        for raw in response.headers.get_list("set-cookie"):
            parsed: SimpleCookie = SimpleCookie()
            parsed.load(raw)
            for name, morsel in parsed.items():
                key = (name, morsel["path"] or "/")
                gone = morsel["max-age"] == "0" or "1970" in morsel["expires"]
                if gone or not morsel.value:
                    self.cookies.pop(key, None)
                else:
                    self.cookies[key] = morsel.value
        return response


def _page_text(page: str) -> str:
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    text = " ".join(re.sub(r"<[^>]+>", " ", page).split())
    return f"{title.group(1).strip() if title else '?'}: {text[:1500]}"


def _form(page: str, form_id: str) -> str:
    tag = re.search(rf'<form\b[^>]*\bid="{form_id}"[^>]*>', page)
    action = re.search(r'\baction="([^"]+)"', tag.group(0)) if tag else None
    if action is None:
        pytest.fail(f"no form {form_id} on the page {_page_text(page)}")
    return html.unescape(action.group(1))


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _start_login(browser: _Browser, challenge: str) -> httpx.Response:
    return browser.send(
        "GET",
        f"{browser.kc.base_url}/realms/meho/protocol/openid-connect/auth",
        params={
            "client_id": "meho-mcp",
            "response_type": "code",
            "redirect_uri": _CALLBACK,
            "scope": "openid",
            "state": secrets.token_hex(8),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )


def _company_login(kc: _Keycloak) -> dict[str, Any]:
    """A browser login through the company login; returns the MEHO token's claims."""
    verifier, challenge = _pkce()
    browser = _Browser(kc)
    seen: list[str] = []
    try:
        response = _start_login(browser, challenge)
        code = ""
        for _ in range(25):
            if response.is_redirect:
                location = urljoin(str(response.url), response.headers["location"])
                seen.append(urlsplit(location).path)
                if location.startswith(_CALLBACK):
                    query = parse_qs(urlsplit(location).query)
                    assert "code" in query, location
                    code = query["code"][0]
                    break
                response = browser.send("GET", location)
                continue
            if response.status_code != 200 or "/realms/company/" not in str(response.url):
                # Only the company login may show a page: its password form.
                pytest.fail(
                    f"{response.status_code} after {seen}: {_page_text(response.text)}\n"
                    f"Keycloak log:\n{kc.logs()}"
                )
            response = browser.send(
                "POST",
                _form(response.text, "kc-form-login"),
                data={"username": _PERSON, "password": kc.password, "credentialId": ""},
            )
        assert code, seen
        assert any("/realms/meho/broker/company/" in path for path in seen), seen
    finally:
        browser.close()
    token = httpx.post(
        f"{kc.base_url}/realms/meho/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "client_id": "meho-mcp",
            "code": code,
            "redirect_uri": _CALLBACK,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    access = token.json()["access_token"]
    payload = access.split(".")[1]
    claims: dict[str, Any] = json.loads(
        base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    )
    claims["_raw"] = access
    return claims


def _company_groups(kc: _Keycloak, names: list[str]) -> None:
    """Put the person into exactly these groups of realm ``company``."""
    headers = _master_headers(kc)
    company = f"{kc.base_url}/admin/realms/company"
    (person,) = httpx.get(
        f"{company}/users", headers=headers, params={"username": _PERSON, "exact": "true"}
    ).json()
    for group in httpx.get(f"{company}/users/{person['id']}/groups", headers=headers).json():
        httpx.delete(
            f"{company}/users/{person['id']}/groups/{group['id']}", headers=headers
        ).raise_for_status()
    groups = {g["name"]: g["id"] for g in httpx.get(f"{company}/groups", headers=headers).json()}
    for name in names:
        httpx.put(
            f"{company}/users/{person['id']}/groups/{groups[name]}", headers=headers
        ).raise_for_status()


def _meho_admin_headers(kc: _Keycloak) -> dict[str, str]:
    resp = httpx.post(
        f"{kc.base_url}/realms/meho/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "meho-admin",
            "client_secret": kc.env[ADMIN_SECRET_ENV],
        },
    )
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _meho_groups(kc: _Keycloak) -> list[str]:
    """The person's groups in realm ``meho``, read with the Job's account."""
    headers = _meho_admin_headers(kc)
    admin = f"{kc.base_url}/admin/realms/meho"
    (person,) = httpx.get(
        f"{admin}/users", headers=headers, params={"username": _PERSON, "exact": "true"}
    ).json()
    groups = httpx.get(f"{admin}/users/{person['id']}/groups", headers=headers)
    groups.raise_for_status()
    return sorted(g["name"] for g in groups.json())


def _mappers(kc: _Keycloak) -> dict[str, dict[str, Any]]:
    resp = httpx.get(
        f"{kc.base_url}/admin/realms/meho/identity-provider/instances/company/mappers",
        headers=_meho_admin_headers(kc),
    )
    resp.raise_for_status()
    return {m["name"]: m for m in resp.json()}


def _snapshot(kc: _Keycloak) -> dict[str, Any]:
    headers = _meho_admin_headers(kc)
    admin = f"{kc.base_url}/admin/realms/meho"
    executions = httpx.get(f"{admin}/authentication/flows/browser/executions", headers=headers)
    executions.raise_for_status()
    redirector = next(
        e for e in executions.json() if e["providerId"] == "identity-provider-redirector"
    )
    config = None
    if redirector.get("authenticationConfig"):
        config = httpx.get(
            f"{admin}/authentication/config/{redirector['authenticationConfig']}",
            headers=headers,
        ).json()
    provider = httpx.get(f"{admin}/identity-provider/instances/company", headers=headers)
    provider.raise_for_status()
    return {"provider": provider.json(), "mappers": _mappers(kc), "redirect": config}


async def test_first_run_sets_up_the_company_login(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    await _run_stage(keycloak, store)
    keycloak.output = capfd.readouterr().out

    assert "login: meho-admin" in keycloak.output
    assert "identity provider company: created" in keycloak.output
    assert "auto-redirect: set to company" in keycloak.output
    state = _snapshot(keycloak)
    config = state["provider"]["config"]
    assert config["issuer"] == f"{_INSIDE}/realms/company"
    assert (config["pkceEnabled"], config["pkceMethod"]) == ("true", "S256")
    assert (config["validateSignature"], config["useJwksUrl"]) == ("true", "true")
    assert config["syncMode"] == "FORCE"
    assert state["provider"]["trustEmail"] is False
    assert set(state["mappers"]) == {f"{MAPPER_PREFIX}acme-ops", f"{MAPPER_PREFIX}acme-readers"}
    assert state["redirect"]["config"]["defaultProvider"] == "company"


async def test_a_person_in_a_mapped_group_gets_the_meho_group(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = _company_login(keycloak)

    assert claims["tenant_id"] == _TENANT_ID
    assert claims["tenant_role"] == "operator"
    assert claims["groups"] == ["MEHO-Operators"]
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{keycloak.base_url}/realms/meho")
    get_settings.cache_clear()
    clear_jwks_cache()
    operator = await verify_jwt_for_audience(
        f"Bearer {claims['_raw']}", expected_audience="meho-backplane"
    )
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role is TenantRole.OPERATOR


def test_after_leaving_the_company_group_the_fields_are_gone(keycloak: _Keycloak) -> None:
    _company_groups(keycloak, [])

    claims = _company_login(keycloak)

    assert "tenant_id" not in claims
    assert "tenant_role" not in claims
    assert not claims.get("groups")


async def test_second_run_changes_nothing_and_prints_no_secret(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    before = _snapshot(keycloak)
    secrets_before = {k: dict(v) for k, v in store.secrets.items()}
    capfd.readouterr()

    await _run_stage(keycloak, store)

    output = capfd.readouterr().out
    for word in ("created", "updated", "removed", "set to"):
        assert word not in output, output
    assert "identity provider company: kept (secret set)" in output
    assert _snapshot(keycloak) == before
    assert store.secrets == secrets_before
    for value in [*keycloak.env.values(), keycloak.password]:
        assert value not in output + keycloak.output
    # The company login's client secret still works from Keycloak's side.
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


async def test_a_changed_line_changes_its_mapper_and_takes_the_old_group_away(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    _company_groups(keycloak, ["acme-ops"])
    assert _company_login(keycloak)["groups"] == ["MEHO-Operators"]
    ops_id = _mappers(keycloak)[f"{MAPPER_PREFIX}acme-ops"]["id"]
    capfd.readouterr()

    await _run_stage(
        keycloak, store, group_map={"acme-ops": "MEHO-Admins", "acme-readers": "MEHO-Readers"}
    )

    output = capfd.readouterr().out
    assert f"mapper {MAPPER_PREFIX}acme-ops: updated" in output
    assert "group /MEHO-Operators: 1 company login member(s) removed" in output
    mappers = _mappers(keycloak)
    assert mappers[f"{MAPPER_PREFIX}acme-ops"]["id"] == ops_id
    assert mappers[f"{MAPPER_PREFIX}acme-ops"]["config"]["group"] == "/MEHO-Admins"
    assert _meho_groups(keycloak) == []
    claims = _company_login(keycloak)
    assert claims["groups"] == ["MEHO-Admins"]
    assert claims["tenant_role"] == "tenant_admin"


async def test_a_removed_line_removes_its_mapper_and_takes_its_group_away(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    _company_groups(keycloak, ["acme-readers"])
    assert _company_login(keycloak)["groups"] == ["MEHO-Readers"]
    capfd.readouterr()

    await _run_stage(keycloak, store, group_map={"acme-ops": "MEHO-Admins"})

    output = capfd.readouterr().out
    assert f"mapper {MAPPER_PREFIX}acme-readers: removed" in output
    assert "group /MEHO-Readers: 1 company login member(s) removed" in output
    assert set(_mappers(keycloak)) == {f"{MAPPER_PREFIX}acme-ops"}
    assert _meho_groups(keycloak) == []
    claims = _company_login(keycloak)
    assert "tenant_id" not in claims
    assert "tenant_role" not in claims
    assert not claims.get("groups")


class _LeavingGroupsTimesOut(httpx.AsyncHTTPTransport):
    """Keycloak answers, except that the requests that take a person out of
    a group time out: the Job stops in the middle of the group cleanup."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "DELETE" and "/users/" in path and "/groups/" in path:
            raise httpx.ReadTimeout("Keycloak does not answer", request=request)
        return await super().handle_async_request(request)


async def test_a_cleanup_that_times_out_is_finished_at_the_next_run(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    _company_groups(keycloak, ["acme-ops"])
    assert _company_login(keycloak)["groups"] == ["MEHO-Admins"]
    capfd.readouterr()

    async with httpx.AsyncClient(
        base_url=keycloak.base_url, timeout=30, transport=_LeavingGroupsTimesOut()
    ) as http:
        with pytest.raises(RealmStageError, match="ReadTimeout"):
            await finish_realm(
                _plan(group_map={"acme-ops": "MEHO-Operators"}),
                keycloak.env,
                HttpAdmin(http),
                store,
            )

    # The mapper already points at the new group, so it no longer says which
    # group it granted; the note on the provider does.
    assert _mappers(keycloak)[f"{MAPPER_PREFIX}acme-ops"]["config"]["group"] == "/MEHO-Operators"
    assert _meho_groups(keycloak) == ["MEHO-Admins"]
    note = _snapshot(keycloak)["provider"]["config"][CLEANUP_CONFIG_KEY]
    assert json.loads(note) == ["/MEHO-Admins"]
    capfd.readouterr()

    await _run_stage(keycloak, store, group_map={"acme-ops": "MEHO-Operators"})

    output = capfd.readouterr().out
    assert "identity provider company: finishing the group cleanup of an earlier run" in output
    assert "group /MEHO-Admins: 1 company login member(s) removed" in output
    assert _meho_groups(keycloak) == []
    assert CLEANUP_CONFIG_KEY not in _snapshot(keycloak)["provider"]["config"]
    claims = _company_login(keycloak)
    assert claims["groups"] == ["MEHO-Operators"]
    assert claims["tenant_role"] == "operator"


def _login_form_shows(kc: _Keycloak) -> None:
    browser = _Browser(kc)
    try:
        response = _start_login(browser, _pkce()[1])
    finally:
        browser.close()
    assert response.status_code == 200, response.headers.get("location")
    assert "/realms/meho/" in _form(response.text, "kc-form-login")


async def test_auto_redirect_off_shows_the_keycloak_login_form_again(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    await _run_stage(keycloak, store, group_map={"acme-ops": "MEHO-Admins"}, auto_redirect=False)
    assert "auto-redirect: removed" in capfd.readouterr().out
    _login_form_shows(keycloak)


class _DiscoveryTimesOut(httpx.AsyncHTTPTransport):
    """Keycloak answers, except that the request for the discovery document
    times out, as it does when Keycloak waits for a company login that is
    down longer than the Job waits for Keycloak."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/identity-provider/import-config"):
            raise httpx.ReadTimeout("the company login does not answer", request=request)
        return await super().handle_async_request(request)


async def test_auto_redirect_off_works_while_the_company_login_does_not_answer(
    keycloak: _Keycloak, store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    await _run_stage(keycloak, store, group_map={"acme-ops": "MEHO-Admins"})
    assert "auto-redirect: set to company" in capfd.readouterr().out
    before = _snapshot(keycloak)

    async with httpx.AsyncClient(
        base_url=keycloak.base_url, timeout=30, transport=_DiscoveryTimesOut()
    ) as http:
        await finish_realm(
            _plan(group_map={"acme-ops": "MEHO-Admins"}, auto_redirect=False),
            keycloak.env,
            HttpAdmin(http),
            store,
        )

    output = capfd.readouterr().out
    assert "discovery document not readable, kept the saved addresses" in output
    assert "auto-redirect: removed" in output
    after = _snapshot(keycloak)
    assert after["provider"] == before["provider"]
    assert after["redirect"] is None
    _login_form_shows(keycloak)
