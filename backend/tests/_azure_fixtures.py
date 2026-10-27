# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared made-up values and stubs for the Azure connector tests.

Every id, name and secret here is invented. None belongs to a real tenant,
subscription or login.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.azure.session import AzureCredentials

TENANT_A = "00000000-0000-0000-0000-00000000a001"
TENANT_B = "00000000-0000-0000-0000-00000000a002"
CLIENT_A = "aaaaaaaa-0000-0000-0000-00000000c001"
CLIENT_B = "aaaaaaaa-0000-0000-0000-00000000c002"
OBJECT_A = "bbbbbbbb-0000-0000-0000-00000000d001"
OBJECT_B = "bbbbbbbb-0000-0000-0000-00000000d002"
SUBSCRIPTION_A = "11111111-0000-0000-0000-00000000e001"
SUBSCRIPTION_B = "11111111-0000-0000-0000-00000000e002"
SECRET_A = "fake-client-secret-for-tests-a"
SECRET_B = "fake-client-secret-for-tests-b"

ARM_HOST = "management.azure.com"
ARM_BASE = f"https://{ARM_HOST}"
ARM_SCOPE = "https://management.azure.com/.default"
MEHO_TENANT = UUID("00000000-0000-0000-0000-0000000000f1")


def token_url(tenant_id: str = TENANT_A, host: str = "login.microsoftonline.com") -> str:
    """The token endpoint of *tenant_id* on *host*."""
    return f"https://{host}/{tenant_id}/oauth2/v2.0/token"


def subscription_url(subscription_id: str = SUBSCRIPTION_A) -> str:
    """The Resource Manager URL of one subscription, without the query."""
    return f"{ARM_BASE}/subscriptions/{subscription_id}"


def locations_url(subscription_id: str = SUBSCRIPTION_A) -> str:
    """The Resource Manager URL of the regions of one subscription, without the query."""
    return f"{ARM_BASE}/subscriptions/{subscription_id}/locations"


def location_answer(name: str, paired: tuple[str, ...] = ()) -> dict[str, Any]:
    """One ``Location`` as Azure returns it. The ids are made up."""
    return {
        "id": f"/subscriptions/{SUBSCRIPTION_A}/locations/{name}",
        "name": name,
        "type": "Region",
        "displayName": name.title(),
        "regionalDisplayName": f"(Example) {name.title()}",
        "metadata": {
            "regionType": "Physical",
            "regionCategory": "Recommended",
            "geography": "Example Geography",
            "geographyGroup": "Example Group",
            "longitude": "1.0",
            "latitude": "2.0",
            "pairedRegion": [
                {"name": other, "id": f"/subscriptions/{SUBSCRIPTION_A}/locations/{other}"}
                for other in paired
            ],
        },
    }


def fake_jwt(claims: dict[str, Any]) -> str:
    """Build a token shaped like a JWT. The signature part is not real."""

    def part(value: dict[str, Any]) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims)}.not-signed"


def login_token(client_id: str = CLIENT_A, object_id: str = OBJECT_A, n: int = 1) -> str:
    """A made-up access token of one login. *n* makes each token different."""
    return fake_jwt({"ver": "1.0", "appid": client_id, "oid": object_id, "uti": f"t{n}"})


def token_answer(token: str, expires_in: int = 3599) -> dict[str, Any]:
    """The JSON body of a successful token answer."""
    return {"token_type": "Bearer", "expires_in": expires_in, "access_token": token}


def subscription_answer(subscription_id: str = SUBSCRIPTION_A) -> dict[str, Any]:
    """The JSON body of a Subscriptions - Get answer."""
    return {
        "id": f"/subscriptions/{subscription_id}",
        "subscriptionId": subscription_id,
        "tenantId": TENANT_A,
        "displayName": "Example Subscription",
        "state": "Enabled",
        "authorizationSource": "RoleBased",
        "subscriptionPolicies": {"spendingLimit": "Off"},
    }


@dataclass
class AzureTargetStub:
    """A target with the fields the connector and the resolver read."""

    name: str = "azure-sub-a"
    host: str = ARM_HOST
    port: int | None = 443
    secret_ref: str | None = "tenants/example/azure-login-a"
    auth_model: str | None = "shared_service_account"
    extras: dict[str, Any] = field(default_factory=lambda: {"subscription_id": SUBSCRIPTION_A})
    verify_tls: bool = True
    tls_ca_pin: str | None = None
    tls_server_name: str | None = None
    product: str = "azure"
    version: str | None = None
    fingerprint: Any = None
    preferred_impl_id: str | None = None
    id: UUID = field(default_factory=uuid.uuid4)
    tenant_id: UUID = MEHO_TENANT


def operator(raw_jwt: str = "person.jwt.value") -> Operator:
    """An operator with a person's token, unless *raw_jwt* is empty."""
    return Operator(
        sub="azure-test-operator",
        name="Azure Test Operator",
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=MEHO_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


class CountingLoader:
    """A credentials loader stub that counts its calls.

    It answers by ``secret_ref``, so two targets can hold two logins.
    """

    def __init__(self, logins: dict[str | None, AzureCredentials] | None = None) -> None:
        self.calls = 0
        self.logins = logins or {
            "tenants/example/azure-login-a": AzureCredentials(TENANT_A, CLIENT_A, SECRET_A)
        }

    async def __call__(self, target: Any, _operator: Operator) -> AzureCredentials:
        self.calls += 1
        return self.logins[target.secret_ref]
