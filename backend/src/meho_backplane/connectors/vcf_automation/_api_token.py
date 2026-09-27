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
   The session runs on a private, un-pooled client (own cookie jar) and
   is logged out (``DELETE /cloudapi/1.0.0/sessions/current``) when the
   op finishes.
2. create: ``POST /oauth/<tenant/<org>|provider>/register``
   ``{"client_name"}`` → ``client_id``; then the ``jwt-bearer`` grant at
   ``POST /oauth/.../token`` (session JWT as ``assertion`` and as Bearer)
   → ``refresh_token`` (``go-vcloud-director``
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
the ``k8s.secret.read_to_ref`` no-transit shape (#3496). Once the OAuth
client is registered, any failure before the Vault write lands -- a
refused grant, a failed write, a cancellation -- revokes the token
(shielded) before the error propagates, so no orphaned token is left for
a re-run to report ``unchanged``. That write is audited inside this op's
audit row (its params carry the store ref) and by Vault's own audit
device; no separate ``vault.kv.patch`` row is written.

``vcfa.provider.api_token.create`` is pinned ``credential_mint`` in
:data:`meho_backplane.broadcast.events._CREDENTIAL_MINT_OPS`, so its
broadcast collapses to aggregate-only and the flight recorder never
records its bodies (the OAuth form carries the session JWT as the
``assertion``; the token response carries the refresh token).
"""

from __future__ import annotations

import asyncio
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
    ORG_SESSION_CURRENT_PATH,
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
    """One org user's own session on a private client (not the pooled provider session).

    The client is un-pooled (:meth:`HttpConnector._ephemeral_http_client`:
    same SSRF + TLS posture, **own cookie jar**), so nothing the user's
    login sets can leak into the connector's shared provider-plane client.
    Leaving the ``async with`` block logs the session out
    (``DELETE /cloudapi/1.0.0/sessions/current``, best effort) and closes
    the client.
    """

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

    async def __aenter__(self) -> _UserSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await asyncio.shield(self._close())

    async def _close(self) -> None:
        try:
            await self.request("DELETE", ORG_SESSION_CURRENT_PATH)
        except (httpx.HTTPError, RuntimeError) as exc:
            _log.info("vcfa_user_session_logout_failed", error=type(exc).__name__)
        finally:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        accept = PROVIDER_CLOUDAPI_ACCEPT if path.startswith("/cloudapi/") else TENANT_ACCEPT
        resp = await self._client.request(
            method,
            path,
            params=dict(params) if params is not None else None,
            json=dict(json) if json is not None else None,
            data=dict(data) if data is not None else None,
            headers={"Accept": accept, "Authorization": f"Bearer {self._jwt}", **self._vhost},
            extensions=self._extensions,
        )
        resp.raise_for_status()
        payload = json_payload_or_empty(resp)
        return payload if isinstance(payload, dict) else {}


def _login_error(
    target: VcfAutomationTargetLike, params: Mapping[str, Any], path: str, status: int
) -> ConnectorAuthError:
    """The structured error for a refused (401/403) user login."""
    org, username = params["org"], params["username"]
    message = (
        f"vcf-automation user session for {username!r}@{org!r} on target "
        f"{target.name!r}: POST {path} returned HTTP {status}"
    )
    secret_ref, mount, key = password_ref(params)
    remediation = (
        f"Check that user {username!r} exists and is enabled in org {org!r} and "
        f"that password_secret_ref={secret_ref!r} (mount={mount!r}, key={key!r}) "
        "holds its current password."
    )
    return ConnectorAuthError(
        f"{message}. {remediation}",
        status_code=status,
        cause=f"session_establish_{status}",
        target_name=target.name,
        host=getattr(target, "host", None),
        secret_ref=secret_ref,
        remediation=remediation,
    )


async def _open_user_session(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    params: Mapping[str, Any],
    password: str,
) -> _UserSession:
    """Log in as ``<username>@<org>`` on a private client and return the session.

    A 401/403 raises :class:`ConnectorAuthError` naming the password secret
    ref as the remediation (the target's own secret is not involved). The
    private client is closed on any login failure.
    """
    org, username = params["org"], params["username"]
    path = PROVIDER_SESSION_PATH if _is_system(org) else ORG_SESSION_PATH
    client = await connector._ephemeral_http_client(target)
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
        if resp.status_code in (401, 403):
            raise _login_error(target, params, path, resp.status_code)
        resp.raise_for_status()
        jwt = resp.headers.get(PROVIDER_TOKEN_HEADER)
        if not jwt:
            raise RuntimeError(
                f"vcf-automation user session for {username!r}@{org!r} on target "
                f"{target.name!r}: POST {path} returned 2xx with no {PROVIDER_TOKEN_HEADER} "
                "header"
            )
    except BaseException:
        await client.aclose()
        raise
    return _UserSession(client, target, extensions, jwt)


def _owner_name(token: Mapping[str, Any]) -> str | None:
    owner = token.get("owner")
    name = owner.get("name") if isinstance(owner, dict) else None
    return name if isinstance(name, str) else None


async def _find_token(
    session: _UserSession, token_name: str, username: str
) -> dict[str, Any] | None:
    """The user's API token named *token_name*, or ``None``.

    Filters on the (pattern-restricted, FIQL-safe) token name and type only;
    the owner is matched client-side so a username carrying FIQL-reserved
    characters never reaches the filter. ``go-vcloud-director``'s
    ``GetTokenByNameAndUsername`` shape, minus the ``owner.name`` term.
    """
    payload = await session.request(
        "GET",
        PROVIDER_TOKENS_PATH,
        params={
            "filter": f"(name=={token_name};(type==PROXY,type==REFRESH))",
            "pageSize": 128,
        },
    )
    values = payload.get("values")
    for row in values if isinstance(values, list) else []:
        if not isinstance(row, dict) or row.get("name") != token_name:
            continue
        owner = _owner_name(row)
        if owner is None or owner.casefold() == username.casefold():
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


async def _register(session: _UserSession, org: str, token_name: str) -> str:
    """Register the OAuth client (the token record) → ``client_id``."""
    context = _oauth_context(org)
    registered = await session.request(
        "POST", OAUTH_REGISTER_PATH.format(context=context), json={"client_name": token_name}
    )
    client_id = registered.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise RuntimeError(
            f"vcfa.provider.api_token.create: POST /oauth/{context}/register returned no client_id"
        )
    return client_id


async def _grant(session: _UserSession, org: str, client_id: str) -> str:
    """The ``jwt-bearer`` grant → the refresh token.

    Sends the session JWT both as the ``assertion`` and as the Bearer
    header -- the request shape of the proven live provider-plane mint.
    """
    context = _oauth_context(org)
    granted = await session.request(
        "POST",
        OAUTH_TOKEN_PATH.format(context=context),
        data={"grant_type": _JWT_BEARER_GRANT, "assertion": session.jwt, "client_id": client_id},
    )
    refresh_token = granted.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError(
            f"vcfa.provider.api_token.create: POST /oauth/{context}/token returned no refresh_token"
        )
    return refresh_token


async def _revoke_quietly(session: _UserSession, client_id: str) -> bool:
    """Best-effort revoke of a just-registered token; never raises."""
    token_id = f"{_TOKEN_URN_PREFIX}{client_id}"
    try:
        await session.request("DELETE", PROVIDER_TOKEN_PATH.format(id=quote_segment(token_id)))
    except (httpx.HTTPError, RuntimeError) as exc:
        _log.warning("vcfa_api_token_cleanup_failed", client_id=client_id, error=type(exc).__name__)
        return False
    return True


class _VaultStoreError(RuntimeError):
    """The minted token could not be written to Vault (message names no value)."""


async def _mint_and_store(
    session: _UserSession,
    operator: Operator,
    params: Mapping[str, Any],
    client_id: str,
) -> dict[str, Any]:
    """Grant → Vault write → the value-free ``stored`` provenance block.

    Any failure -- a refused grant, a failed Vault write, or a cancellation
    in between -- revokes the just-registered token (shielded, so a
    cancellation cannot skip the compensation) before the error propagates,
    so a re-run never meets an orphaned token it would call ``unchanged``.
    """
    mount, store_path, field = _store_params(params)
    try:
        refresh_token = await _grant(session, params["org"], client_id)
        try:
            written = await _store_in_vault(operator, mount, store_path, field, refresh_token)
        except Exception as exc:
            raise _VaultStoreError(type(exc).__name__) from exc
    except BaseException as exc:
        revoked = await asyncio.shield(_revoke_quietly(session, client_id))
        if isinstance(exc, _VaultStoreError):
            outcome = (
                "the token was revoked"
                if revoked
                else (
                    f"the token (client_id {client_id}) could NOT be revoked -- run "
                    "vcfa.provider.api_token.revoke with token_name="
                    f"{params['token_name']!r}, username={params['username']!r}"
                )
            )
            raise RuntimeError(
                f"vcfa.provider.api_token.create: {outcome}; the minted token could not be "
                f"written to Vault at {mount}/{store_path} ({exc})"
            ) from exc.__cause__
        _log.warning("vcfa_api_token_mint_aborted", client_id=client_id, revoked=revoked)
        raise
    digest = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    stored: dict[str, Any] = {
        "mount": mount,
        "secret_ref": store_path,
        "field": field,
        "version": written.get("version"),
        "value_sha256": digest,
        "length": len(refresh_token),
    }
    return stored


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
    async with await _open_user_session(connector, target, params, password) as session:
        existing = await _find_token(session, token_name, username)
        if existing is not None:
            return {
                **envelope,
                "status": "unchanged",
                "client_id": _client_id_of(existing),
                "guidance": (
                    f"user {username!r} already has an API token named {token_name!r}; "
                    "nothing was minted (a token value is shown only once -- revoke it with "
                    "vcfa.provider.api_token.revoke to mint a new one)"
                ),
            }
        client_id = await _register(session, org, token_name)
        stored = await _mint_and_store(session, operator, params, client_id)
    return {
        **envelope,
        "status": "created",
        "client_id": client_id,
        "stored": stored,
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
    async with await _open_user_session(connector, target, params, password) as session:
        existing = await _find_token(session, token_name, username)
        if existing is None or not existing.get("id"):
            return {
                **envelope,
                "status": "unchanged",
                "guidance": f"user {username!r} has no API token named {token_name!r}",
            }
        await session.request(
            "DELETE", PROVIDER_TOKEN_PATH.format(id=quote_segment(str(existing["id"])))
        )
    return {
        **envelope,
        "status": "revoked",
        "client_id": _client_id_of(existing),
        "guidance": (
            "the token is revoked on the appliance; a Vault copy of it (if any) is now dead -- "
            "remove it with vault.kv.patch / vault.kv.delete if it should not linger"
        ),
    }
