# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A small fake Workspace ONE Access 3.3 and VCF Identity Broker 9.x for the ``idb`` tests.

The answers are written by hand from the vendor references (see
``connectors/idb/_paths.py``). Made-up names and ids only.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

HOST = "idb.test.invalid"
BASE_URL = f"https://{HOST}"
TENANT = "CUSTOMER"


@dataclass
class FakeIdb:
    """Answers both products' paths; ``product`` picks which one is "installed"."""

    product: str = "wsa"  # "wsa" or "idb"
    client_id: str = "meho-client"
    client_secret: str = "client-secret-test"  # gitleaks:allow NOSONAR -- test value
    token_prefix: str = "access-token"  # gitleaks:allow NOSONAR -- test value
    version: str | None = "9.1.0.0"
    properties_status: int = 200
    users: list[dict[str, Any]] = field(default_factory=list)
    groups: list[dict[str, Any]] = field(default_factory=list)
    directories: list[dict[str, Any]] = field(default_factory=list)
    identity_providers: list[dict[str, Any]] = field(default_factory=list)
    sync_status: int = 200
    refuse_next_calls: int = 0
    logins: int = 0
    calls: list[httpx.Request] = field(default_factory=list)
    _valid_tokens: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------

    def current_token(self) -> str:
        return f"{self.token_prefix}-{self.logins}"

    def calls_of(self, method: str, path: str | None = None) -> list[httpx.Request]:
        return [
            c for c in self.calls if c.method == method and (path is None or c.url.path == path)
        ]

    def expire_tokens(self) -> None:
        self._valid_tokens.clear()

    # ------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path in ("/SAAS/auth/oauthtoken", f"/acs/t/{TENANT}/token"):
            return self._login(request, path)
        if path == "/SAAS/API/1.0/REST/system/health" and request.method == "GET":
            if self.product != "wsa":
                return httpx.Response(404)
            return httpx.Response(200, json=self._health())
        if path == f"/acs/t/{TENANT}/.well-known/openid-configuration":
            if self.product != "idb":
                return httpx.Response(404)
            return httpx.Response(200, json={"issuer": f"{BASE_URL}/acs/t/{TENANT}"})
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ")
        if not auth.startswith("Bearer ") or token not in self._valid_tokens:
            return httpx.Response(401, json={"error": "invalid_token"})
        if self.refuse_next_calls > 0:
            self.refuse_next_calls -= 1
            return httpx.Response(401, json={"error": "invalid_token"})
        return self._route(request, path)

    def _login(self, request: httpx.Request, path: str) -> httpx.Response:
        if (self.product == "wsa") != path.startswith("/SAAS"):
            return httpx.Response(404)
        expected = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        form = parse_qs(request.content.decode())
        if request.headers.get("Authorization") != f"Basic {expected}":
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") != ["client_credentials"]:
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        self.logins += 1
        token = self.current_token()
        self._valid_tokens.add(token)
        return httpx.Response(
            200, json={"access_token": token, "token_type": "Bearer", "expires_in": 3600}
        )

    def _health(self) -> dict[str, Any]:
        return {
            "AllOk": "true",
            "DatabaseConnectionOk": "true",
            "MessagingConnectionOk": "false",
            "AnalyticsConnectionOk": "true",
            "BuildVersion": "3.3.7.0 Build 12345678",
            "AuditPollInterval": "1000",
        }

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        if self.product == "wsa":
            return self._route_wsa(request, path)
        return self._route_idb(request, path)

    def _route_wsa(self, request: httpx.Request, path: str) -> httpx.Response:
        prefix = "/SAAS/jersey/manager/api"
        if path == f"{prefix}/identityProviders" and request.method == "GET":
            return httpx.Response(200, json={"items": self.identity_providers})
        if path == f"{prefix}/connectormanagement/directoryconfigs" and request.method == "GET":
            return httpx.Response(200, json={"items": self.directories})
        if path.endswith("/syncprofile/sync") and request.method == "POST":
            directory_id = path.split("/")[-3]
            known = {d.get("directoryId") for d in self.directories}
            if directory_id not in known:
                return httpx.Response(
                    404, json={"errors": [{"code": "directory.not.found", "message": "no dir"}]}
                )
            return httpx.Response(self.sync_status, json={"syncStatus": "STARTED"})
        if path == f"{prefix}/scim/Users/.search" and request.method == "POST":
            return self._search(request, self.users, "userName", "Resources")
        if path == f"{prefix}/scim/Groups/.search" and request.method == "POST":
            return self._search(request, self.groups, "displayName", "Resources")
        return httpx.Response(404, json={"message": "not found"})

    def _route_idb(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == f"/acs/t/{TENANT}/properties" and request.method == "GET":
            if self.properties_status != 200:
                return httpx.Response(self.properties_status)
            return httpx.Response(
                200,
                json={
                    "version": self.version,
                    "deployment_type": "VCF",
                    "instance_id": "instance-1",
                    "_links": {"self": {"href": f"{BASE_URL}/acs/t/{TENANT}/properties"}},
                },
            )
        if path == f"/usergroup/t/{TENANT}/scim/v2/Users/.search":
            return self._search(request, self.users, "userName", "resources")
        if path == f"/usergroup/t/{TENANT}/scim/v2/Groups/.search":
            return self._search(request, self.groups, "displayName", "resources")
        return httpx.Response(404, json={"message": "not found"})

    def _search(
        self, request: httpx.Request, rows: list[dict[str, Any]], attribute: str, key: str
    ) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        filt = str(body.get("filter", ""))
        if not filt.startswith(f"{attribute} "):
            return httpx.Response(400, json={"detail": f"bad filter {filt}"})
        _, op, raw = filt.split(" ", 2)
        value = json.loads(raw)
        found = [
            r
            for r in rows
            if (op == "eq" and r.get(attribute) == value)
            or (op == "co" and value in str(r.get(attribute, "")))
            or (op == "sw" and str(r.get(attribute, "")).startswith(value))
        ]
        count = int(body.get("count", 20))
        return httpx.Response(
            200,
            json={
                "totalResults": len(found),
                "itemsPerPage": min(count, len(found)),
                "startIndex": 1,
                key: found[:count],
            },
        )


def scim_user(name: str, *, groups: int = 1, active: bool = True) -> dict[str, Any]:
    return {
        "id": f"id-{name}",
        "userName": name,
        "displayName": name.title(),
        "active": active,
        "password": "user-password-must-not-show",  # gitleaks:allow NOSONAR -- planted
        "emails": [{"value": f"{name}@example.test", "primary": True}],
        "groups": [{"value": f"g{i}", "display": f"group-{i}"} for i in range(groups)],
        "urn:scim:schemas:extension:workspace:1.0": {
            "domain": "example.test",
            "internalUserType": "PROVISIONED",
            "userStatus": "1",
        },
    }


def scim_group(name: str, members: int = 2) -> dict[str, Any]:
    return {
        "id": f"id-{name}",
        "displayName": name,
        "members": [{"value": f"u{i}", "display": f"user-{i}"} for i in range(members)],
        "urn:scim:schemas:extension:workspace:1.0": {
            "domain": "example.test",
            "internalGroupType": "EXTERNAL",
            "description": "made-up group",
        },
    }
