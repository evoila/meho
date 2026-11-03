# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A customer's own Keycloak in front of MEHO's built-in Keycloak.

The customer keeps their people and groups in their own Keycloak. MEHO's
built-in Keycloak (the subchart's realm file plus the bootstrap Job's realm
stage, as in ``test_keycloak_role_groups_e2e.py``) brokers the login to it
as an OpenID Connect identity provider, and one "Advanced Claim to Group"
mapper per role group turns the customer's ``groups`` claim into membership
of MEHO's role groups. MEHO itself still trusts only its own Keycloak:

1. A customer person in one customer group logs in through our Keycloak and
   gets that MEHO group's ``tenant_id``, ``tenant_role`` and ``approver``,
   in a token issued by OUR realm; the backplane's JWT check accepts it.
2. Removed from the group in the customer Keycloak, the person's next login
   (sync mode FORCE) drops the MEHO group, and MEHO answers
   ``401 missing_tenant_claim``.
3. A token the customer Keycloak issues itself -- even one carrying MEHO's
   tenant claims and audience -- is refused.

The login is a scripted browser (an ``httpx`` client with cookies) running
the authorization-code flow with PKCE on ``meho-mcp`` and
``kc_idp_hint=customer-kc``. The browser reaches both Keycloaks on their
host-mapped ports; our Keycloak reaches the customer's server to server on
the customer container's address on the shared Docker network (alias ``customer-idp``).
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import pytest
from fastapi import HTTPException

from meho_backplane.auth.operator import TenantRole
from meho_backplane.core.keycloak import (
    ADMIN_SECRET_ENV,
    BOOTSTRAP_ID_ENV,
    BOOTSTRAP_SECRET_ENV,
    HttpAdmin,
    finish_realm,
)
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON
from tests.integration.test_core_bootstrap_keycloak import _FakeStore, _plan
from tests.integration.test_core_keycloak_realm_import import _subchart_image, _wait_for_realm
from tests.integration.test_keycloak_role_groups_e2e import (
    _PUBLIC_URL,
    _REALM_FILE,
    _ROLE_GROUPS,
    _TENANT_ID,
    _claims,
    _Site,
    _user_groups,
    _verify,
)

if TYPE_CHECKING:
    from testcontainers.core.network import Network

_IDP_ALIAS = "customer-kc"
_CUSTOMER_REALM = "customer"
_BROKER_CLIENT = "meho-broker"
_LOGIN_CLIENT = "meho-mcp"
_LOGIN_REDIRECT = "http://localhost:8765/callback"
# The customer Keycloak's name on the Docker network both containers share.
_CUSTOMER_HOST = "customer-idp"

# Customer group -> MEHO role group.
_GROUP_MAP = {
    "meho-readers": "MEHO-Readers",
    "meho-operators": "MEHO-Operators",
    "meho-sa-operators": "MEHO-SA-Operators",
    "meho-admins": "MEHO-Admins",
    "meho-approvers": "MEHO-Approvers",
}
_CUSTOMER_GROUP = {meho: customer for customer, meho in _GROUP_MAP.items()}


@dataclass
class _OurSite(_Site):
    """Our Keycloak, plus a master-realm admin for the identity provider.

    The bootstrap stage removes the first-start admin client, and
    ``meho-admin`` may manage only clients and users; adding an identity
    provider needs ``manage-identity-providers`` (here: a master admin).
    """

    master_admin_password: str = ""

    def master_headers(self) -> dict[str, str]:
        resp = httpx.post(
            f"{self.base_url}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "kcadmin",
                "password": self.master_admin_password,
            },
        )
        resp.raise_for_status()
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@dataclass
class _Customer:
    """The customer's Keycloak, as the browser and as our Keycloak see it."""

    base_url: str  # host-mapped, for the browser and the admin calls
    internal_url: str  # network alias, for our Keycloak's server-to-server calls
    admin_password: str

    @property
    def admin_url(self) -> str:
        return f"{self.base_url}/admin/realms/{_CUSTOMER_REALM}"

    def headers(self) -> dict[str, str]:
        resp = httpx.post(
            f"{self.base_url}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "admin",
                "password": self.admin_password,
            },
        )
        resp.raise_for_status()
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# --- Customer Keycloak ---------------------------------------------------------


def _customer_group_id(customer: _Customer, name: str) -> str:
    groups = httpx.get(
        f"{customer.admin_url}/groups", headers=customer.headers(), params={"search": name}
    ).json()
    return str(next(g["id"] for g in groups if g["name"] == name))


def _customer_user_id(customer: _Customer, username: str) -> str:
    users = httpx.get(
        f"{customer.admin_url}/users",
        headers=customer.headers(),
        params={"username": username, "exact": "true"},
    ).json()
    assert len(users) == 1, username
    return str(users[0]["id"])


def _add_customer_person(customer: _Customer, username: str, group: str) -> tuple[str, str]:
    """A customer person with a full profile, only in *group*; return (id, password)."""
    password = secrets.token_urlsafe(18)
    httpx.post(
        f"{customer.admin_url}/users",
        headers=customer.headers(),
        json={
            "username": username,
            "enabled": True,
            "email": f"{username}@customer.example",
            "emailVerified": True,
            "firstName": "Customer",
            "lastName": username,
            "credentials": [{"type": "password", "value": password, "temporary": False}],
        },
    ).raise_for_status()
    user_id = _customer_user_id(customer, username)
    httpx.put(
        f"{customer.admin_url}/users/{user_id}/groups/{_customer_group_id(customer, group)}",
        headers=customer.headers(),
    ).raise_for_status()
    return user_id, password


def _set_up_customer_realm(customer: _Customer, broker_redirect_uri: str) -> str:
    """Realm, groups, the broker client and its groups mapper; return its secret."""
    headers = customer.headers()
    httpx.post(
        f"{customer.base_url}/admin/realms",
        headers=headers,
        json={
            "realm": _CUSTOMER_REALM,
            "enabled": True,
            "sslRequired": "none",
            # One public URL for the realm, so ``iss`` is the same whether the
            # browser or our Keycloak (on the back channel) is asking -- as for
            # a customer Keycloak with a fixed hostname.
            "attributes": {"frontendUrl": customer.base_url},
        },
    ).raise_for_status()
    for group in _GROUP_MAP:
        httpx.post(
            f"{customer.admin_url}/groups", headers=headers, json={"name": group}
        ).raise_for_status()

    broker_secret = secrets.token_hex(16)
    group_claim = {
        "name": "groups",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-group-membership-mapper",
        "config": {
            "claim.name": "groups",
            "full.path": "false",
            "id.token.claim": "true",
            "access.token.claim": "true",
            "userinfo.token.claim": "true",
            "introspection.token.claim": "true",
        },
    }
    httpx.post(
        f"{customer.admin_url}/clients",
        headers=headers,
        json={
            "clientId": _BROKER_CLIENT,
            "protocol": "openid-connect",
            "enabled": True,
            "publicClient": False,
            "clientAuthenticatorType": "client-secret",
            "secret": broker_secret,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "implicitFlowEnabled": False,
            "serviceAccountsEnabled": False,
            "redirectUris": [broker_redirect_uri],
            "attributes": {"pkce.code.challenge.method": "S256"},
            "protocolMappers": [group_claim],
        },
    ).raise_for_status()

    # A second client a customer admin could make: password grant, and
    # mappers that put MEHO's tenant claims and audience into its tokens.
    hardcoded = [
        {
            "name": name,
            "protocol": "openid-connect",
            "protocolMapper": "oidc-hardcoded-claim-mapper",
            "config": {
                "claim.name": name,
                "claim.value": value,
                "jsonType.label": "String",
                "access.token.claim": "true",
                "id.token.claim": "false",
            },
        }
        for name, value in (("tenant_id", _TENANT_ID), ("tenant_role", "tenant_admin"))
    ]
    hardcoded.append(
        {
            "name": "aud-meho-backplane",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.custom.audience": "meho-backplane",
                "access.token.claim": "true",
                "id.token.claim": "false",
            },
        }
    )
    httpx.post(
        f"{customer.admin_url}/clients",
        headers=headers,
        json={
            "clientId": "customer-cli",
            "protocol": "openid-connect",
            "enabled": True,
            "publicClient": True,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": True,
            "protocolMappers": [group_claim, *hardcoded],
        },
    ).raise_for_status()
    return broker_secret


# --- Our Keycloak: the identity provider and its group mappers ---------------


def _set_up_identity_provider(ours: _OurSite, customer: _Customer, broker_secret: str) -> None:
    headers = ours.master_headers()
    front = f"{customer.base_url}/realms/{_CUSTOMER_REALM}/protocol/openid-connect"
    back = f"{customer.internal_url}/realms/{_CUSTOMER_REALM}/protocol/openid-connect"
    httpx.post(
        f"{ours.admin_url}/identity-provider/instances",
        headers=headers,
        json={
            "alias": _IDP_ALIAS,
            "displayName": "Customer SSO",
            "providerId": "oidc",
            "enabled": True,
            "trustEmail": True,
            "storeToken": False,
            "linkOnly": False,
            "firstBrokerLoginFlowAlias": "first broker login",
            "config": {
                "clientId": _BROKER_CLIENT,
                "clientSecret": broker_secret,
                "clientAuthMethod": "client_secret_post",
                # The browser is sent here, so it is the customer's public URL.
                "authorizationUrl": f"{front}/auth",
                # Our Keycloak calls these itself.
                "tokenUrl": f"{back}/token",
                "userInfoUrl": f"{back}/userinfo",
                "jwksUrl": f"{back}/certs",
                "useJwksUrl": "true",
                "validateSignature": "true",
                # The customer realm's public issuer. Our Keycloak checks it
                # against both the ``iss`` of the authorization response (front
                # channel) and of the ID token (back channel); a mismatch fails
                # with "Wrong issuer from token".
                "issuer": f"{customer.base_url}/realms/{_CUSTOMER_REALM}",
                "defaultScope": "openid profile email",
                "pkceEnabled": "true",
                "pkceMethod": "S256",
                "syncMode": "FORCE",
            },
        },
    ).raise_for_status()

    for customer_group, meho_group in _GROUP_MAP.items():
        httpx.post(
            f"{ours.admin_url}/identity-provider/instances/{_IDP_ALIAS}/mappers",
            headers=headers,
            json={
                "name": f"{customer_group}-to-{meho_group}",
                "identityProviderAlias": _IDP_ALIAS,
                "identityProviderMapper": "oidc-advanced-group-idp-mapper",
                "config": {
                    "syncMode": "FORCE",
                    "claims": json.dumps([{"key": "groups", "value": customer_group}]),
                    "are.claim.values.regex": "false",
                    "group": f"/{meho_group}",
                },
            },
        ).raise_for_status()


# --- The scripted browser -------------------------------------------------------

_FORM_RE = re.compile(r"<form\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w[\w-]*)\s*=\s*"([^"]*)"')


def _page_text(page: str) -> str:
    """The visible text of a Keycloak page, for failure messages."""
    body = re.sub(r"(?s)<(script|style|head)\b.*?</\1>", " ", page)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())[:1500]


def _login_form_action(page: str) -> str:
    for tag in _FORM_RE.findall(page):
        attrs = dict(_ATTR_RE.findall(tag))
        if attrs.get("id") == "kc-form-login":
            return html.unescape(attrs["action"])
    raise AssertionError(f"no login form on the page:\n{page[:2000]}")


def _broker_login(ours: _Site, username: str, password: str) -> dict[str, Any]:
    """Log in on ``meho-mcp`` through the customer Keycloak; return our tokens."""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(12)
    with httpx.Client(follow_redirects=False, timeout=30) as browser:
        resp = browser.get(
            f"{ours.issuer}/protocol/openid-connect/auth",
            params={
                "client_id": _LOGIN_CLIENT,
                "redirect_uri": _LOGIN_REDIRECT,
                "response_type": "code",
                "scope": "openid",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "kc_idp_hint": _IDP_ALIAS,
            },
        )
        submitted = False
        for _ in range(20):
            # Keycloak marks its cookies ``Secure``; a browser still sends
            # them to http://localhost (a secure context), httpx does not.
            for cookie in browser.cookies.jar:
                cookie.secure = False
            if resp.is_redirect:
                location = urljoin(str(resp.url), resp.headers["location"])
                if location.startswith(_LOGIN_REDIRECT):
                    break
                resp = browser.get(location)
                continue
            assert resp.status_code == 200, (
                f"{resp.status_code} at {resp.url}: {_page_text(resp.text)}; "
                f"cookies: {[(c.domain, c.path, c.name) for c in browser.cookies.jar]}; "
                f"set-cookie: {resp.headers.get_list('set-cookie')}"
            )
            assert not submitted, f"an extra page after the login at {resp.url}:\n{resp.text}"
            resp = browser.post(
                _login_form_action(resp.text),
                data={"username": username, "password": password, "credentialId": ""},
            )
            submitted = True
        else:
            raise AssertionError("the login did not come back to the redirect URI")

    query = parse_qs(urlparse(location).query)
    assert query.get("state") == [state]
    assert "code" in query, location
    tokens = httpx.post(
        f"{ours.issuer}/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "client_id": _LOGIN_CLIENT,
            "code": query["code"][0],
            "redirect_uri": _LOGIN_REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert tokens.status_code == 200, tokens.text
    return dict(tokens.json())


# --- The two Keycloaks on one Docker network -------------------------------------


@pytest.fixture(scope="module")
def network() -> Iterator[Network]:
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)
    from testcontainers.core.network import Network

    with Network() as net:
        yield net


@pytest.fixture(scope="module")
def site(network: Network) -> Iterator[_OurSite]:
    """Our Keycloak as ``test_keycloak_role_groups_e2e.site`` sets it up.

    Two differences: it is on the shared network, and it also gets a
    master-realm admin user (the bootstrap stage removes only the
    first-start admin *client*).
    """
    import asyncio

    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

    master_admin_password = secrets.token_urlsafe(18)
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
        .with_network(network)
        .with_env("KC_HTTP_RELATIVE_PATH", "/auth")
        .with_env("KC_BOOTSTRAP_ADMIN_CLIENT_ID", env[BOOTSTRAP_ID_ENV])
        .with_env("KC_BOOTSTRAP_ADMIN_CLIENT_SECRET", env[BOOTSTRAP_SECRET_ENV])
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", "kcadmin")
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", master_admin_password)
        .with_env("MEHO_REALM_PUBLIC_URL", _PUBLIC_URL)
        .with_env("MEHO_REALM_TENANT_ID", _TENANT_ID)
        .with_env("MEHO_REALM_TENANT_NAME", "main")
        .with_env("MEHO_REALM_ADMIN_CLIENT_SECRET", env[ADMIN_SECRET_ENV])
        .with_env("MEHO_REALM_WEB_CLIENT_SECRET", env["MEHO_REALM_WEB_CLIENT_SECRET"])
        .with_env(
            "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET", env["MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET"]
        )
        .with_volume_mapping(str(Path(_REALM_FILE).parent), "/opt/keycloak/data/import", "ro")
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

        yield _OurSite(
            base_url=base_url,
            realm="meho",
            headers=headers,
            admin_username=first_login["admin_username"],
            admin_password=first_login["admin_password"],
            approver_username=first_login["approver_username"],
            approver_password=first_login["approver_password"],
            master_admin_password=master_admin_password,
        )
    finally:
        container.stop()


@pytest.fixture(scope="module")
def customer(site: _OurSite, network: Network) -> Iterator[_Customer]:
    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

    admin_password = secrets.token_urlsafe(18)
    image = os.environ.get("MEHO_TEST_CORE_KEYCLOAK_IMAGE") or _subchart_image()
    container = (
        DockerContainer(image)
        .with_command("start-dev")
        .with_network(network)
        .with_network_aliases(_CUSTOMER_HOST)
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", "admin")
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", admin_password)
        .with_exposed_ports(8080)
    )
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"customer Keycloak container failed to start ({type(exc).__name__}): {exc}")
    try:
        wait_for_log_message(container, "Listening on:", timeout=240)
        base_url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
        customer = _Customer(
            base_url=base_url,
            internal_url=f"http://{_CUSTOMER_HOST}:8080",
            admin_password=admin_password,
        )
        deadline = time.monotonic() + 120
        while True:
            try:
                customer.headers()
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline:
                    pytest.fail("the customer Keycloak did not come up in time")
                time.sleep(2)

        broker_secret = _set_up_customer_realm(
            customer, f"{site.issuer}/broker/{_IDP_ALIAS}/endpoint"
        )
        _set_up_identity_provider(site, customer, broker_secret)
        yield customer
    finally:
        container.stop()


# --- 1. Each customer group gives its MEHO tenant and role -------------------


@pytest.mark.parametrize("meho_group", list(_ROLE_GROUPS))
async def test_customer_group_member_gets_the_meho_role_through_the_broker(
    site: _OurSite,
    customer: _Customer,
    meho_group: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    want_role, want_approver = _ROLE_GROUPS[meho_group]
    customer_group = _CUSTOMER_GROUP[meho_group]
    username = f"cust-{customer_group}"
    _, password = _add_customer_person(customer, username, customer_group)

    access_token = _broker_login(site, username, password)["access_token"]
    claims = _claims(access_token)
    assert claims["iss"] == site.issuer
    assert claims.get("tenant_id") == _TENANT_ID
    assert claims.get("tenant_role") == want_role.value
    if want_approver:
        assert claims.get("approver") is True
    else:
        assert "approver" not in claims
    assert "platform_admin" not in claims
    assert _user_groups(site, username) == {meho_group}

    # Our Keycloak made the person on first login, without asking for anything:
    # same username, the customer's e-mail trusted, linked to the customer.
    user = httpx.get(
        f"{site.admin_url}/users",
        headers=site.headers(),
        params={"username": username, "exact": "true"},
    ).json()[0]
    assert user["email"] == f"{username}@customer.example"
    assert user["emailVerified"] is True
    assert not user.get("requiredActions")
    links = httpx.get(
        f"{site.admin_url}/users/{user['id']}/federated-identity", headers=site.headers()
    ).json()
    assert [(link["identityProvider"], link["userName"]) for link in links] == [
        (_IDP_ALIAS, username)
    ]

    operator = await _verify(site, access_token, monkeypatch)
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role is want_role
    assert operator.approver is want_approver
    assert operator.platform_admin is False


# --- 2. Leaving the customer group takes the access away ----------------------


async def test_leaving_the_customer_group_removes_the_tenant_at_the_next_login(
    site: _OurSite,
    customer: _Customer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    username = "cust-leaver"
    customer_user_id, password = _add_customer_person(customer, username, "meho-operators")

    before = _broker_login(site, username, password)
    operator = await _verify(site, before["access_token"], monkeypatch)
    assert operator.tenant_role is TenantRole.OPERATOR
    assert _user_groups(site, username) == {"MEHO-Operators"}

    httpx.delete(
        f"{customer.admin_url}/users/{customer_user_id}/groups/"
        f"{_customer_group_id(customer, 'meho-operators')}",
        headers=customer.headers(),
    ).raise_for_status()
    # Nothing changed in our Keycloak yet: it learns at the next login.
    assert _user_groups(site, username) == {"MEHO-Operators"}

    after = _broker_login(site, username, password)
    assert _user_groups(site, username) == set()
    claims = _claims(after["access_token"])
    assert claims["iss"] == site.issuer
    for claim in ("tenant_id", "tenant_role", "approver"):
        assert claim not in claims, claim
    with pytest.raises(HTTPException) as refused:
        await _verify(site, after["access_token"], monkeypatch)
    assert refused.value.status_code == 401
    assert str(refused.value.detail).startswith("missing_tenant_claim"), refused.value.detail


# --- 3. MEHO does not take the customer's own tokens -------------------------


async def test_token_from_the_customer_keycloak_is_refused(
    site: _OurSite,
    customer: _Customer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    username = "cust-direct"
    _, password = _add_customer_person(customer, username, "meho-admins")
    resp = httpx.post(
        f"{customer.base_url}/realms/{_CUSTOMER_REALM}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "customer-cli",
            "username": username,
            "password": password,
            "scope": "openid",
        },
    )
    assert resp.status_code == 200, resp.text
    access_token = resp.json()["access_token"]
    claims = _claims(access_token)
    # It carries everything MEHO looks for, except our issuer and our key.
    assert claims["iss"] != site.issuer
    assert claims.get("tenant_id") == _TENANT_ID
    assert claims.get("tenant_role") == "tenant_admin"
    assert "meho-backplane" in (
        claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    )
    assert "meho-admins" in claims.get("groups", [])

    with pytest.raises(HTTPException) as refused:
        await _verify(site, access_token, monkeypatch)
    assert refused.value.status_code == 401
    # Signed with a key our realm's JWKS does not have.
    assert refused.value.detail == "invalid_token"
