# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Keycloak Admin REST calls of the realm stage (#4043).

:class:`HttpAdmin` talks to Keycloak through its Service with ``httpx``.
:class:`Admin` is the narrow interface the stage in
:mod:`meho_backplane.core.keycloak` uses, so tests run it against a fake.
Errors are :class:`RealmStageError` with one plain sentence that names the
status and Keycloak's own message, never a request body or a token.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

__all__ = [
    "REALM",
    "Admin",
    "HttpAdmin",
    "RealmStageError",
    "Reply",
    "expect",
    "id_from",
]

REALM = "meho"


class RealmStageError(Exception):
    """A failure the Job reports as one plain sentence before it exits 1."""


@dataclass(frozen=True)
class Reply:
    status: int
    data: Any
    location: str = ""


class Admin(Protocol):
    """The Keycloak calls the stage needs."""

    async def realm_ready(self) -> bool: ...

    async def login(self, realm: str, client_id: str, client_secret: str) -> bool:
        """Get a token for later calls: ``True`` on success, ``False`` when refused."""
        ...

    async def check_login(self, realm: str, client_id: str, client_secret: str) -> bool:
        """Like :meth:`login`, but keep the current token."""
        ...

    async def call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> Reply: ...


def _message(reply: Reply) -> str:
    if isinstance(reply.data, dict):
        text = reply.data.get("errorMessage") or reply.data.get("error_description")
        if text:
            return f"HTTP {reply.status}: {text}"
    return f"HTTP {reply.status}"


def expect(reply: Reply, *statuses: int, what: str) -> Reply:
    if reply.status not in statuses:
        raise RealmStageError(f"{what}: {_message(reply)}")
    return reply


def id_from(reply: Reply) -> str:
    new_id = reply.location.rstrip("/").rsplit("/", 1)[-1]
    if not new_id:
        raise RealmStageError("Keycloak created an object but returned no Location header")
    return new_id


class HttpAdmin:
    """:class:`Admin` over ``httpx`` against the Keycloak Service."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._token = ""

    async def realm_ready(self) -> bool:
        try:
            reply = await self._http.get(f"/realms/{REALM}/.well-known/openid-configuration")
        except httpx.TransportError:
            return False
        return reply.is_success

    async def _grant(self, realm: str, client_id: str, client_secret: str) -> str:
        try:
            reply = await self._http.post(
                f"/realms/{realm}/protocol/openid-connect/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise RealmStageError(f"Keycloak token request failed: {type(exc).__name__}") from None
        if reply.status_code in (400, 401):
            return ""
        if not reply.is_success:
            raise RealmStageError(f"Keycloak token request failed: HTTP {reply.status_code}")
        token = str(reply.json().get("access_token") or "")
        if not token:
            raise RealmStageError("Keycloak token request returned no access token")
        return token

    async def login(self, realm: str, client_id: str, client_secret: str) -> bool:
        token = await self._grant(realm, client_id, client_secret)
        if token:
            self._token = token
        return bool(token)

    async def check_login(self, realm: str, client_id: str, client_secret: str) -> bool:
        return bool(await self._grant(realm, client_id, client_secret))

    async def call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> Reply:
        try:
            reply = await self._http.request(
                method,
                path,
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise RealmStageError(f"Keycloak request failed: {type(exc).__name__}") from None
        data: Any = None
        if reply.content and "json" in reply.headers.get("content-type", ""):
            data = reply.json()
        return Reply(reply.status_code, data, reply.headers.get("location", ""))
