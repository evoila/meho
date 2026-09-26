# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vcfa.provider.api_token.create`` / ``.revoke`` (evoila/meho#3890).

A VCFA API token is a long-lived OAuth refresh token owned by one org
user. The tenant (IaaS) plane exchanges only a **tenant-org** user's token
at ``POST /iaas/api/login``, so a governed tenant-plane session needs a
token minted *as that user*. Both ops therefore run under the user's own
session, never the connector's cached provider session:

1. ``POST /cloudapi/1.0.0/sessions`` (HTTP Basic ``<user>@<org>``) -- or
   ``/cloudapi/1.0.0/sessions/provider`` for the ``System`` org -- → the
   session JWT (``X-VMWARE-VCLOUD-ACCESS-TOKEN`` header). The password is
   read from the op's ``password_secret_ref`` under the operator's
   identity (:func:`._lookups.read_password`); it is never an op param.
2. create: ``POST /oauth/<tenant/<org>|provider>/register``
   ``{"client_name"}`` → ``client_id``; then the ``jwt-bearer`` grant at
   ``POST /oauth/.../token`` → ``refresh_token`` (``go-vcloud-director``
   ``CreateToken`` + ``GetInitialApiToken``). The token id is
   ``urn:vcloud:token:<client_id>``.
3. revoke: ``DELETE /cloudapi/1.0.0/tokens/<id>`` (``Token.Delete``).

The refresh token is **never returned**
=======================================

It is written straight to the caller-named Vault KV path/field through
the governed ``vault.kv.patch`` handler (``vault.kv.put`` when the path
does not exist yet), under the operator's own Vault identity, with the
tenant-scope guard those handlers enforce. The result carries only the
Vault ref, the written version, and a SHA-256 + length as provenance --
the ``k8s.secret.read_to_ref`` no-transit shape (#3496). If the Vault
write fails after the mint, the just-minted token is revoked before the
error propagates, so no unrecoverable credential is left behind.

``vcfa.provider.api_token.create`` is pinned ``credential_mint`` in
:data:`meho_backplane.broadcast.events._CREDENTIAL_MINT_OPS`, so its
broadcast collapses to aggregate-only and the flight recorder never
records its bodies (the OAuth form carries the session JWT as the
``assertion``; the token response carries the refresh token).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.vcf_automation._lookups import (
    password_missing_guidance,
    password_ref,
    quote_segment,
    read_password,
)
from meho_backplane.connectors.vcf_automation._paths import (
    OAUTH_REGISTER_PATH,
    OAUTH_TOKEN_PATH,
    ORG_SESSION_PATH,
    PROVIDER_TOKEN_PATH,
    PROVIDER_TOKENS_PATH,
)
from meho_backplane.connectors.vcf_automation._routing import (
    PROVIDER_CLOUDAPI_ACCEPT,
    PROVIDER_SESSION_PATH,
    PROVIDER_TOKEN_HEADER,
    TENANT_ACCEPT,
    vhost_header,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "DEFAULT_STORE_FIELD",
    "DEFAULT_STORE_MOUNT",
    "provider_api_token_create",
    "provider_api_token_revoke",
]

_log = structlog.get_logger(__name__)

#: The field the refresh token is written under by default -- the field the
#: connector's own tenant login reads (``session.VCFA_REFRESH_TOKEN_FIELD``).
DEFAULT_STORE_FIELD: Final = "refresh_token"
DEFAULT_STORE_MOUNT: Final = "secret"
_TOKEN_URN_PREFIX: Final = "urn:vcloud:token:"
_JWT_BEARER_GRANT: Final = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def _client_id_of(token: Mapping[str, Any]) -> str | None:
    """The OAuth ``client_id`` of a token row (its id is ``urn:vcloud:token:<client_id>``).

    Results carry the bare client id, not the urn: the connector-boundary
    redaction engine reads a ``token:<hex>`` value as a labelled secret and
    would mangle the urn.
    """
    raw = token.get("id")
    return str(raw).rsplit(":", 1)[-1] if raw else None


def _is_system(org: str) -> bool:
    return org.casefold() == "system"


def _oauth_context(org: str) -> str:
    """``provider`` for the System org, else ``tenant/<org>`` (``go-vcloud-director``)."""
    return "provider" if _is_system(org) else f"tenant/{quote_segment(org)}"


class _UserSession:
    """Requests under one org user's own session JWT (not the cached provider session)."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        target: VcfAutomationTargetLike,
        extensions: dict[str, Any],
        jwt: str,
    ) -> None:
        self._client = client
        self._vhost = vhost_header(getattr(target, "fqdn", None), getattr(target, "port", None))
        self._extensions = extensions
        self._jwt = jwt

    @property
    def jwt(self) -> str:
        return self._jwt

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        data: Mapping[str, str] | None = None,
        bearer: bool = True,
    ) -> dict[str, Any]:
        accept = PROVIDER_CLOUDAPI_ACCEPT if path.startswith("/cloudapi/") else TENANT_ACCEPT
        headers = {"Accept": accept, **self._vhost}
        if bearer:
            headers["Authorization"] = f"Bearer {self._jwt}"
        resp = await self._client.request(
            method,
            path,
            params=dict(params) if params is not None else None,
            json=dict(json) if json is not None else None,
            data=dict(data) if data is not None else None,
            headers=headers,
            extensions=self._extensions,
        )
        resp.raise_for_status()
        payload = json_payload_or_empty(resp)
        return payload if isinstance(payload, dict) else {}


async def _open_user_session(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    params: Mapping[str, Any],
    password: str,
) -> _UserSession:
    """Log in as ``<username>@<org>`` and return the user's session.

    A 401/403 raises :class:`ConnectorAuthError` naming the password secret
    ref as the remediation (the target's own secret is not involved).
    """
    org, username = params["org"], params["username"]
    path = PROVIDER_SESSION_PATH if _is_system(org) else ORG_SESSION_PATH
    client = await connector._http_client(target)
    extensions = connector._request_extensions(target)
    try:
        resp = await client.post(
            path,
            auth=(f"{username}@{org}", password),
            headers={
                "Accept": PROVIDER_CLOUDAPI_ACCEPT,
                **vhost_header(getattr(target, "fqdn", None), getattr(target, "port", None)),
            },
            extensions=extensions,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        message = (
            f"vcf-automation user session for {username!r}@{org!r} on target "
            f"{target.name!r}: POST {path} returned HTTP {status}"
        )
        if status in (401, 403):
            secret_ref, mount, key = password_ref(params)
            remediation = (
                f"Check that user {username!r} exists and is enabled in org {org!r} and "
                f"that password_secret_ref={secret_ref!r} (mount={mount!r}, key={key!r}) "
                "holds its current password."
            )
            raise ConnectorAuthError(
                f"{message}. {remediation}",
                status_code=status,
                cause=f"session_establish_{status}",
                target_name=target.name,
                host=getattr(target, "host", None),
                secret_ref=secret_ref,
                remediation=remediation,
            ) from exc
        raise
    jwt = resp.headers.get(PROVIDER_TOKEN_HEADER)
    if not jwt:
        raise RuntimeError(
            f"vcf-automation user session for {username!r}@{org!r} on target "
            f"{target.name!r}: POST {path} returned 2xx with no {PROVIDER_TOKEN_HEADER} header"
        )
    return _UserSession(client, target, extensions, jwt)


async def _find_token(
    session: _UserSession, token_name: str, username: str
) -> dict[str, Any] | None:
    """The user's API token named *token_name*, or ``None`` (``GetTokenByNameAndUsername``)."""
    payload = await session.request(
        "GET",
        PROVIDER_TOKENS_PATH,
        params={
            "filter": f"(name=={token_name};owner.name=={username};(type==PROXY,type==REFRESH))",
            "pageSize": 128,
        },
    )
    values = payload.get("values")
    for row in values if isinstance(values, list) else []:
        if isinstance(row, dict) and row.get("name") == token_name:
            return row
    return None


async def _store_in_vault(
    operator: Operator, mount: str, path: str, field: str, value: str
) -> dict[str, Any]:
    """Write ``{field: value}`` to Vault via the governed KV handlers (patch, else put)."""
    import hvac.exceptions

    from meho_backplane.connectors.vault.ops import vault_kv_patch, vault_kv_put

    kv_params = {"mount": mount, "path": path, "data": {field: value}}
    try:
        return await vault_kv_patch(operator, None, kv_params)
    except hvac.exceptions.InvalidPath:
        return await vault_kv_put(operator, None, kv_params)


def _store_params(params: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(params.get("store_mount") or DEFAULT_STORE_MOUNT).strip(),
        str(params["store_secret_ref"]).strip(),
        str(params.get("store_field") or DEFAULT_STORE_FIELD).strip(),
    )


async def _mint(session: _UserSession, org: str, token_name: str) -> tuple[str, str]:
    """Register the OAuth client and run the jwt-bearer grant → ``(client_id, refresh_token)``."""
    context = _oauth_context(org)
    registered = await session.request(
        "POST", OAUTH_REGISTER_PATH.format(context=context), json={"client_name": token_name}
    )
    client_id = registered.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise RuntimeError(
            f"vcfa.provider.api_token.create: POST /oauth/{context}/register returned no client_id"
        )
    granted = await session.request(
        "POST",
        OAUTH_TOKEN_PATH.format(context=context),
        data={"grant_type": _JWT_BEARER_GRANT, "assertion": session.jwt, "client_id": client_id},
        bearer=False,
    )
    refresh_token = granted.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        await _revoke_quietly(session, client_id)
        raise RuntimeError(
            f"vcfa.provider.api_token.create: POST /oauth/{context}/token returned no "
            "refresh_token; the registered client was revoked"
        )
    return client_id, refresh_token


async def _revoke_quietly(session: _UserSession, client_id: str) -> bool:
    """Best-effort revoke of a just-minted token; never raises."""
    token_id = f"{_TOKEN_URN_PREFIX}{client_id}"
    try:
        await session.request("DELETE", PROVIDER_TOKEN_PATH.format(id=quote_segment(token_id)))
    except (httpx.HTTPError, RuntimeError) as exc:
        _log.warning("vcfa_api_token_cleanup_failed", token_id=token_id, error=type(exc).__name__)
        return False
    return True


async def provider_api_token_create(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.api_token.create`` — mint an org user's API token into Vault.

    Idempotent on ``(token_name, user)``: an existing token of that name is
    reported ``unchanged`` and nothing is minted (a token's value is shown
    only once, so re-minting needs a revoke first). See the module
    docstring for the flow and the no-transit guarantee.
    """
    org, username, token_name = params["org"], params["username"], params["token_name"]
    mount, store_path, field = _store_params(params)
    envelope: dict[str, Any] = {
        "org": org,
        "username": username,
        "token_name": token_name,
        "client_id": None,
        "stored": None,
    }
    password = await read_password(operator, target, params)
    if password is None:
        return {
            **envelope,
            "status": "invalid_request",
            "guidance": password_missing_guidance(params),
        }
    session = await _open_user_session(connector, target, params, password)
    existing = await _find_token(session, token_name, username)
    if existing is not None:
        return {
            **envelope,
            "status": "unchanged",
            "client_id": _client_id_of(existing),
            "guidance": (
                f"user {username!r} already has an API token named {token_name!r}; nothing was "
                "minted (a token value is shown only once -- revoke it with "
                "vcfa.provider.api_token.revoke to mint a new one)"
            ),
        }
    client_id, refresh_token = await _mint(session, org, token_name)
    token_id = f"{_TOKEN_URN_PREFIX}{client_id}"
    try:
        written = await _store_in_vault(operator, mount, store_path, field, refresh_token)
    except Exception as exc:
        revoked = await _revoke_quietly(session, client_id)
        raise RuntimeError(
            f"vcfa.provider.api_token.create: the minted token could not be written to Vault "
            f"at {mount}/{store_path} ({type(exc).__name__}); the token "
            + ("was revoked" if revoked else f"could NOT be revoked -- revoke {token_id}")
        ) from exc
    digest = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    return {
        **envelope,
        "status": "created",
        "client_id": client_id,
        "stored": {
            "mount": mount,
            "secret_ref": store_path,
            "field": field,
            "version": written.get("version"),
            "value_sha256": digest,
            "length": len(refresh_token),
        },
        "guidance": None,
    }


async def provider_api_token_revoke(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.api_token.revoke`` — revoke an org user's API token by name.

    Runs under the user's own session (same ``password_secret_ref`` as the
    create), looks the token up by ``(token_name, user)`` and ``DELETE``\\ s
    ``/cloudapi/1.0.0/tokens/<id>``. A token that does not exist is
    ``unchanged``. The Vault copy is not touched.
    """
    org, username, token_name = params["org"], params["username"], params["token_name"]
    envelope: dict[str, Any] = {
        "org": org,
        "username": username,
        "token_name": token_name,
        "client_id": None,
    }
    password = await read_password(operator, target, params)
    if password is None:
        return {
            **envelope,
            "status": "invalid_request",
            "guidance": password_missing_guidance(params),
        }
    session = await _open_user_session(connector, target, params, password)
    existing = await _find_token(session, token_name, username)
    if existing is None or not existing.get("id"):
        return {
            **envelope,
            "status": "unchanged",
            "guidance": f"user {username!r} has no API token named {token_name!r}",
        }
    token_id = str(existing["id"])
    await session.request("DELETE", PROVIDER_TOKEN_PATH.format(id=quote_segment(token_id)))
    return {
        **envelope,
        "status": "revoked",
        "client_id": _client_id_of(existing),
        "guidance": (
            "the token is revoked on the appliance; a Vault copy of it (if any) is now dead -- "
            "remove it with vault.kv.patch / vault.kv.delete if it should not linger"
        ),
    }
