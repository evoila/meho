# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared made-up values and Graph answers for the Entra ID connector tests.

Every id, name, domain and secret here is invented. None belongs to a real
tenant, user, group or app. The login helpers (fake tokens, the token
answer, the operator, the counting secret loader) come from
``tests/_azure_fixtures.py``, because both connectors share one login.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from meho_backplane.connectors.entra.session import EntraCredentials
from tests._azure_fixtures import (
    CLIENT_A,
    MEHO_TENANT,
    OBJECT_A,
    SECRET_A,
    TENANT_A,
    CountingLoader,
    fake_jwt,
)

GRAPH_HOST = "graph.microsoft.com"
GRAPH_BASE = f"https://{GRAPH_HOST}/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
ENTRA_SECRET_REF = "tenants/example/entra-login-a"

ORGANIZATION_URL = f"{GRAPH_BASE}/organization"
USERS_URL = f"{GRAPH_BASE}/users"
GROUPS_URL = f"{GRAPH_BASE}/groups"
ROLE_ASSIGNMENTS_URL = f"{GRAPH_BASE}/roleManagement/directory/roleAssignments"
ROLE_DEFINITIONS_URL = f"{GRAPH_BASE}/roleManagement/directory/roleDefinitions"
APPLICATIONS_URL = f"{GRAPH_BASE}/applications"
SERVICE_PRINCIPALS_URL = f"{GRAPH_BASE}/servicePrincipals"

#: The Graph permissions a fully set up login carries.
ALL_ROLES = (
    "Application.Read.All",
    "Group.Read.All",
    "Organization.Read.All",
    "RoleManagement.Read.Directory",
    "User.Read.All",
)

#: Stands in for the first characters of a client secret (Graph's ``hint``),
#: so the tests can prove MEHO never returns or records it.
SECRET_HINT = "Zq9"

#: Stands in for certificate data (``key``), with the same purpose.
CERTIFICATE_DATA = "TUVIT0ZBS0VDRVJUREFUQQ=="


def guid(prefix: str, n: int) -> str:
    """A made-up GUID such as ``cccccccc-0000-0000-0000-000000000007``."""
    return f"{prefix * 8}-0000-0000-0000-{n:012d}"


def graph_token(roles: tuple[str, ...] | None = ALL_ROLES, n: int = 1) -> str:
    """A made-up Graph token of the login. *roles* go into the ``roles`` claim."""
    claims: dict[str, Any] = {"ver": "1.0", "appid": CLIENT_A, "oid": OBJECT_A, "uti": f"g{n}"}
    if roles is not None:
        claims["roles"] = list(roles)
    return fake_jwt(claims)


def entra_loader() -> CountingLoader:
    """A secret loader stub that knows the login of :class:`EntraTargetStub`."""
    return CountingLoader({ENTRA_SECRET_REF: EntraCredentials(TENANT_A, CLIENT_A, SECRET_A)})


@dataclass
class EntraTargetStub:
    """A target with the fields the connector and the resolver read."""

    name: str = "entra-tenant-a"
    host: str = GRAPH_HOST
    port: int | None = 443
    secret_ref: str | None = ENTRA_SECRET_REF
    auth_model: str | None = "shared_service_account"
    extras: dict[str, Any] = field(default_factory=lambda: {"tenant_id": TENANT_A})
    verify_tls: bool = True
    tls_ca_pin: str | None = None
    tls_server_name: str | None = None
    product: str = "entra"
    version: str | None = None
    fingerprint: Any = None
    preferred_impl_id: str | None = None
    id: UUID = field(default_factory=uuid.uuid4)
    tenant_id: UUID = MEHO_TENANT


def next_link(url: str, token: str = "page2") -> str:
    """A next-page link as Graph writes it."""
    return f"{url}?$top=999&$skiptoken={token}"


def page(items: list[dict[str, Any]], link: str | None = None) -> dict[str, Any]:
    """One page of a Graph list."""
    body: dict[str, Any] = {"@odata.context": f"{GRAPH_BASE}/$metadata#items", "value": items}
    if link is not None:
        body["@odata.nextLink"] = link
    return body


def organization_answer() -> dict[str, Any]:
    """A List organizations answer, shaped like Microsoft's example."""
    return {
        "value": [
            {
                "id": TENANT_A,
                "displayName": "Example Tenant",
                "verifiedDomains": [
                    {
                        "capabilities": "Email, OfficeCommunicationsOnline",
                        "isDefault": False,
                        "isInitial": True,
                        "name": "exampletenant.onmicrosoft.com",
                        "type": "Managed",
                    },
                    {
                        "capabilities": "Email",
                        "isDefault": True,
                        "isInitial": False,
                        "name": "example.com",
                        "type": "Managed",
                    },
                ],
            }
        ]
    }


def user_item(n: int, *, guest: bool = False, enabled: bool = True) -> dict[str, Any]:
    """One user, with the fields entra.user.list selects."""
    return {
        "id": guid("1", n),
        "displayName": f"Example Person {n}",
        "userPrincipalName": f"person{n}@example.com",
        "accountEnabled": enabled,
        "userType": "Guest" if guest else "Member",
        "createdDateTime": "2026-01-02T03:04:05Z",
    }


def group_item(n: int, kind: str = "security", *, dynamic: bool = False) -> dict[str, Any]:
    """One group of the given kind, with the fields entra.group.list selects."""
    group_types = ["Unified"] if kind == "microsoft_365" else []
    if dynamic:
        group_types.append("DynamicMembership")
    mail, security = {
        "microsoft_365": (True, False),
        "security": (False, True),
        "mail_enabled_security": (True, True),
        "distribution": (True, False),
        "other": (False, False),
    }[kind]
    return {
        "id": guid("2", n),
        "displayName": f"Example Group {n}",
        "groupTypes": group_types,
        "mailEnabled": mail,
        "securityEnabled": security,
    }


def role_definition_item(definition_id: str, name: str) -> dict[str, Any]:
    """One Entra ID role definition."""
    return {
        "id": definition_id,
        "displayName": name,
        "isBuiltIn": True,
        "templateId": definition_id,
        "rolePermissions": [{"allowedResourceActions": ["microsoft.directory/example/read"]}],
    }


def role_assignment_item(n: int, definition_id: str, kind: str = "user") -> dict[str, Any]:
    """One role assignment with its principal expanded, as Graph sends it."""
    principal_id = guid("3", n)
    return {
        "id": f"example-assignment-{n}",
        "principalId": principal_id,
        "resourceScope": "/",
        "directoryScopeId": "/",
        "roleDefinitionId": definition_id,
        "principal": {
            "@odata.type": f"#microsoft.graph.{kind}",
            "id": principal_id,
            "displayName": f"Example Principal {n}",
        },
    }


def password_credential(end: str | None, n: int = 1) -> dict[str, Any]:
    """One client secret as Graph lists it: with a hint, never a value."""
    return {
        "customKeyIdentifier": None,
        "displayName": f"example secret {n}",
        "endDateTime": end,
        "hint": SECRET_HINT,
        "keyId": guid("4", n),
        "secretText": None,
        "startDateTime": "2026-01-01T00:00:00Z",
    }


def key_credential(end: str | None, n: int = 1) -> dict[str, Any]:
    """One certificate as Graph lists it, with certificate data."""
    return {
        "customKeyIdentifier": "RVhBTVBMRVRIVU1CUFJJTlQ=",
        "displayName": f"CN=example certificate {n}",
        "endDateTime": end,
        "key": CERTIFICATE_DATA,
        "keyId": guid("5", n),
        "startDateTime": "2026-01-01T00:00:00Z",
        "type": "AsymmetricX509Cert",
        "usage": "Verify",
    }


def app_item(
    n: int,
    *,
    secret_ends: tuple[str | None, ...] = (),
    certificate_ends: tuple[str | None, ...] = (),
) -> dict[str, Any]:
    """One app registration with client secrets and certificates ending at the given times."""
    return {
        "id": guid("6", n),
        "appId": guid("7", n),
        "displayName": f"Example App {n}",
        "passwordCredentials": [
            password_credential(end, i) for i, end in enumerate(secret_ends, start=1)
        ],
        "keyCredentials": [
            key_credential(end, i) for i, end in enumerate(certificate_ends, start=1)
        ],
    }


def service_principal_item(
    n: int,
    *,
    kind: str = "Application",
    secret_ends: tuple[str | None, ...] = (),
    certificate_ends: tuple[str | None, ...] = (),
) -> dict[str, Any]:
    """One enterprise app (service principal) with credentials ending at the given times."""
    item = app_item(n, secret_ends=secret_ends, certificate_ends=certificate_ends)
    item["id"] = guid("8", n)
    item["servicePrincipalType"] = kind
    item["accountEnabled"] = True
    return item
