# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Content checks for the MEHO realm file of the built-in Keycloak (#4040).

The file ``deploy/charts/meho/charts/keycloak/files/meho-realm.json`` is what
Keycloak imports at its first start. These checks pin the realm the product
owner decided (``docs/decisions/login-role-groups.md``): five role groups that
carry ``tenant_id`` and ``tenant_role`` (plus ``approver`` on MEHO-Approvers),
the platform group that carries only ``platform_admin`` (#4043), the
``meho_tenant`` attribute and ``meho-tenants`` mapper for several tenants, the six
clients, the mappers ``meho admin keycloak bootstrap-clients`` puts on the
clients people log in with, and no secret or host name written into the
file.
Every value that differs per install is a ``${...}`` placeholder that
Keycloak fills from the pod's environment during the import.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

_REALM_FILE = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "charts"
    / "meho"
    / "charts"
    / "keycloak"
    / "files"
    / "meho-realm.json"
)

_HUMAN_CLIENTS = ("meho-cli", "meho-mcp", "meho-web")
_PLACEHOLDERS = {
    "MEHO_REALM_PUBLIC_URL",
    "MEHO_REALM_TENANT_ID",
    "MEHO_REALM_TENANT_NAME",
    "MEHO_REALM_ADMIN_CLIENT_SECRET",
    "MEHO_REALM_WEB_CLIENT_SECRET",
    "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET",
}


@pytest.fixture(scope="module")
def realm() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_REALM_FILE.read_text())
    return data


def _client(realm: dict[str, Any], client_id: str) -> dict[str, Any]:
    matches = [c for c in realm["clients"] if c["clientId"] == client_id]
    assert len(matches) == 1, client_id
    return matches[0]


def test_realm_name_and_session_limits(realm: dict[str, Any]) -> None:
    assert realm["realm"] == "meho"
    assert realm["enabled"] is True
    assert realm["registrationAllowed"] is False
    # An offline login (the MCP clients' long login) ends after 7 days at most.
    assert realm["offlineSessionMaxLifespanEnabled"] is True
    assert realm["offlineSessionMaxLifespan"] == 7 * 24 * 3600


def test_browser_session_limits_match_the_console(realm: dict[str, Any]) -> None:
    # The login server keeps a browser session for 1 hour without use and
    # 10 hours at most: the same as the console's own session (#5518).
    assert realm["ssoSessionIdleTimeout"] == 3600
    assert realm["ssoSessionMaxLifespan"] == 10 * 3600


def test_no_company_login_yet(realm: dict[str, Any]) -> None:
    # A later Task adds the company login (Active Directory, Entra ID).
    assert realm["identityProviders"] == []
    assert realm["identityProviderMappers"] == []


def _meho_tenant(role: str) -> list[str]:
    return [f"${{MEHO_REALM_TENANT_ID}}:${{MEHO_REALM_TENANT_NAME}}:{role}"]


def test_role_groups_with_tenant_attributes_and_the_platform_group(realm: dict[str, Any]) -> None:
    groups = {g["name"]: g["attributes"] for g in realm["groups"]}
    assert groups == {
        "MEHO-Readers": {
            "tenant_id": ["${MEHO_REALM_TENANT_ID}"],
            "tenant_role": ["read_only"],
            "meho_tenant": _meho_tenant("Readers"),
        },
        "MEHO-Operators": {
            "tenant_id": ["${MEHO_REALM_TENANT_ID}"],
            "tenant_role": ["operator"],
            "meho_tenant": _meho_tenant("Operators"),
        },
        "MEHO-SA-Operators": {
            "tenant_id": ["${MEHO_REALM_TENANT_ID}"],
            "tenant_role": ["operator"],
            "meho_tenant": _meho_tenant("SA-Operators"),
        },
        "MEHO-Admins": {
            "tenant_id": ["${MEHO_REALM_TENANT_ID}"],
            "tenant_role": ["tenant_admin"],
            "meho_tenant": _meho_tenant("Admins"),
        },
        "MEHO-Approvers": {
            "tenant_id": ["${MEHO_REALM_TENANT_ID}"],
            "tenant_role": ["read_only"],
            "approver": ["true"],
            "meho_tenant": _meho_tenant("Approvers"),
        },
        # Not a role group: it carries no tenant and no role, only the right
        # to act across tenants. The bootstrap Job puts the first admin here.
        "MEHO-Platform-Admins": {"platform_admin": ["true"]},
    }


def test_clients(realm: dict[str, Any]) -> None:
    assert {c["clientId"] for c in realm["clients"]} == {
        "meho-backplane",
        "meho-cli",
        "meho-mcp",
        "meho-web",
        "meho-admin",
        "meho-check-runner",
    }


@pytest.mark.parametrize("client_id", _HUMAN_CLIENTS)
def test_human_clients_read_claims_from_groups(realm: dict[str, Any], client_id: str) -> None:
    mappers = {m["name"]: m for m in _client(realm, client_id)["protocolMappers"]}
    assert set(mappers) == {
        "audience-meho-backplane",
        "meho-mcp-audience",
        "tenant-id",
        "tenant-role",
        "approver",
        "platform-admin",
        "meho-tenants",
        "groups-claim",
    }
    for name, attribute, json_type in (
        ("tenant-id", "tenant_id", "String"),
        ("tenant-role", "tenant_role", "String"),
        ("approver", "approver", "boolean"),
        ("platform-admin", "platform_admin", "boolean"),
    ):
        mapper = mappers[name]
        assert mapper["protocolMapper"] == "oidc-usermodel-attribute-mapper"
        config = mapper["config"]
        assert config["user.attribute"] == attribute
        assert config["claim.name"] == attribute
        assert config["jsonType.label"] == json_type
        # The first group that carries the attribute wins, which is why a
        # person is in exactly one group.
        assert config["aggregate.attrs"] == "false"
        assert config["multivalued"] == "false"
        assert config["access.token.claim"] == "true"
        assert config["id.token.claim"] == "false"
        assert config["userinfo.token.claim"] == "true"
    # One person may be in role groups of several tenants: this mapper lists
    # the meho_tenant value of every group of the person.
    tenants = mappers["meho-tenants"]
    assert tenants["protocolMapper"] == "oidc-usermodel-attribute-mapper"
    assert tenants["config"] == {
        "user.attribute": "meho_tenant",
        "claim.name": "meho_tenants",
        "jsonType.label": "String",
        "aggregate.attrs": "true",
        "multivalued": "true",
        "id.token.claim": "false",
        "access.token.claim": "true",
        "introspection.token.claim": "true",
        "userinfo.token.claim": "true",
    }
    assert mappers["groups-claim"]["protocolMapper"] == "oidc-group-membership-mapper"
    assert mappers["audience-meho-backplane"]["config"]["included.client.audience"] == (
        "meho-backplane"
    )
    assert mappers["meho-mcp-audience"]["config"]["included.custom.audience"] == (
        "${MEHO_REALM_PUBLIC_URL}/mcp"
    )


def test_no_fixed_tenant_or_role_on_any_client(realm: dict[str, Any]) -> None:
    for client in realm["clients"]:
        for mapper in client.get("protocolMappers", []):
            assert mapper["protocolMapper"] != "oidc-hardcoded-claim-mapper", client["clientId"]


@pytest.mark.parametrize("client_id", _HUMAN_CLIENTS)
def test_human_clients_default_scopes(realm: dict[str, Any], client_id: str) -> None:
    client = _client(realm, client_id)
    assert set(client["defaultClientScopes"]) == {
        "basic",
        "roles",
        "web-origins",
        "acr",
        "profile",
        "email",
    }


def test_cli_client_uses_device_flow(realm: dict[str, Any]) -> None:
    client = _client(realm, "meho-cli")
    assert client["publicClient"] is True
    assert client["directAccessGrantsEnabled"] is False
    assert client["attributes"]["oauth2.device.authorization.grant.enabled"] == "true"
    assert client["optionalClientScopes"] == []


def test_mcp_client_browser_login(realm: dict[str, Any]) -> None:
    client = _client(realm, "meho-mcp")
    assert client["publicClient"] is True
    assert client["standardFlowEnabled"] is True
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["attributes"]["client.offline.session.idle.timeout"] == str(48 * 3600)
    assert client["redirectUris"] == ["http://localhost:*", "http://127.0.0.1:*"]
    assert set(client["optionalClientScopes"]) == {
        "offline_access",
        "mcp:read",
        "mcp:execute",
        "mcp:admin",
    }


def test_mcp_scopes_exist_and_keep_default_scopes(realm: dict[str, Any]) -> None:
    scopes = {s["name"]: s for s in realm["clientScopes"]}
    assert set(scopes) == {"mcp:read", "mcp:execute", "mcp:admin"}
    for scope in scopes.values():
        assert scope["attributes"]["include.in.token.scope"] == "true"
    # Without this attribute, a realm file that lists its own client scopes
    # gets none of Keycloak's built-in scopes (basic, profile, roles, ...).
    assert realm["attributes"]["CreateDefaultClientScopes"] == "true"


def test_web_client(realm: dict[str, Any]) -> None:
    client = _client(realm, "meho-web")
    assert client["publicClient"] is False
    assert client["secret"] == "${MEHO_REALM_WEB_CLIENT_SECRET}"
    assert client["standardFlowEnabled"] is True
    assert client["directAccessGrantsEnabled"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["redirectUris"] == ["${MEHO_REALM_PUBLIC_URL}/ui/auth/callback"]
    assert client["attributes"]["post.logout.redirect.uris"] == (
        "${MEHO_REALM_PUBLIC_URL}/ui/auth/login"
    )


def test_service_clients(realm: dict[str, Any]) -> None:
    admin = _client(realm, "meho-admin")
    assert admin["serviceAccountsEnabled"] is True
    assert admin["secret"] == "${MEHO_REALM_ADMIN_CLIENT_SECRET}"
    runner = _client(realm, "meho-check-runner")
    assert runner["serviceAccountsEnabled"] is True
    assert runner["secret"] == "${MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET}"
    # The runner's tokens carry their own audience, so a secrets-store role
    # bound to meho-backplane does not accept them.
    audiences = [m["config"]["included.custom.audience"] for m in runner["protocolMappers"]]
    assert audiences == ["meho-check-runner"]
    backplane = _client(realm, "meho-backplane")
    assert backplane["publicClient"] is False
    assert "secret" not in backplane
    for flag in (
        "standardFlowEnabled",
        "implicitFlowEnabled",
        "directAccessGrantsEnabled",
        "serviceAccountsEnabled",
    ):
        assert backplane[flag] is False, flag


def test_admin_service_account_roles(realm: dict[str, Any]) -> None:
    (user,) = realm["users"]
    assert user["serviceAccountClientId"] == "meho-admin"
    assert user["clientRoles"] == {"realm-management": ["manage-clients", "manage-users"]}


def test_only_known_placeholders(realm: dict[str, Any]) -> None:
    text = _REALM_FILE.read_text()
    assert set(re.findall(r"\$\{([^}]*)\}", text)) == _PLACEHOLDERS


def test_no_secret_or_host_written_in_the_file(realm: dict[str, Any]) -> None:
    for client in realm["clients"]:
        secret = client.get("secret")
        assert secret is None or re.fullmatch(r"\$\{[A-Z_]+\}", secret), client["clientId"]
    # Every absolute URL is either a loopback redirect or built from the
    # public-URL placeholder.
    for url in re.findall(r"https?://[^\"\s]+", _REALM_FILE.read_text()):
        assert url.startswith(("http://localhost:", "http://127.0.0.1:")), url


def test_cli_client_allows_the_fresh_sign_in_for_an_approval(realm: dict[str, Any]) -> None:
    """``meho approvals approve`` signs in again in the browser (#3979).

    The browser flow is on with PKCE and a 127.0.0.1 loopback redirect only;
    ``meho login`` keeps the device flow. Same as the bootstrap recipe.
    """
    client = _client(realm, "meho-cli")
    assert client["standardFlowEnabled"] is True
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["attributes"]["oauth2.device.authorization.grant.enabled"] == "true"
    assert client["redirectUris"] == ["http://127.0.0.1:*"]
    assert client["webOrigins"] == []
