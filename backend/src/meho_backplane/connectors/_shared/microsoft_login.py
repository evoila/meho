# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Get an access token from Microsoft Entra ID with a client id and a client secret.

The Azure connector and the Entra ID connector use this module. Only the
scope (the API the token is for) differs: Azure Resource Manager or
Microsoft Graph.

How it works
============

One ``POST https://<login host>/<tenant id>/oauth2/v2.0/token`` with the form
fields ``grant_type=client_credentials``, ``client_id``, ``client_secret`` and
``scope`` (the OAuth 2.0 client credentials flow). Microsoft answers with
``access_token`` and ``expires_in`` (seconds). There is no refresh token in this
flow: when a token runs out, the caller asks for a new one.

Why not the ``azure-identity`` library
======================================

We checked ``azure-identity`` 1.27.0 (with ``azure-core`` 1.41.0 and ``msal``
1.39.0) against what MEHO needs. It keeps its own token cache inside each
credential object, so after a 401 it hands back the same rejected token unless
the object is thrown away. It retries the token request itself (up to 10 times,
with waits of up to 120 seconds) unless told not to. Every new credential object
first reads Microsoft's login settings (one more call). Its HTTP runs on
``requests``, so MEHO would need a thread for it, and the async variant needs
``aiohttp``. What is left after turning all of that off is the single request
below, which MEHO already makes the same way for Keycloak.

What this module does not do
============================

* No cache. The caller keeps the token and decides when to ask again.
* No retry. One attempt per call, so retries never stack with MEHO's own.
* No logging of the secret or the token, and no secret or token in any error.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError

__all__ = [
    "LOGIN_HOSTS",
    "ClientSecretCredentials",
    "MicrosoftAccessToken",
    "MicrosoftLoginError",
    "fetch_client_credentials_token",
]

#: The Microsoft Entra ID login host of each Azure cloud, by the cloud names the
#: Azure CLI uses. Only ``AzureCloud`` is proven on a live tenant.
LOGIN_HOSTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "AzureCloud": "login.microsoftonline.com",
        "AzureUSGovernment": "login.microsoftonline.us",
        "AzureChinaCloud": "login.chinacloudapi.cn",
    }
)

#: HTTP statuses that mean "Microsoft Entra ID refused this login". Entra ID
#: answers 401 for a wrong or expired client secret and 400 for an unknown
#: tenant, an unknown app or a wrong scope. The fix is the same for all of
#: them: correct the secret or the target.
_REFUSED_STATUSES: Final[frozenset[int]] = frozenset({400, 401, 403})

#: The OAuth 2.0 error codes (RFC 6749, section 5.2). Only these are copied
#: into an error message; any other ``error`` text from the answer is left out.
_OAUTH2_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
    }
)

#: At most this many ``AADSTS`` numbers go into an error message.
_MAX_ERROR_CODES: Final[int] = 3

#: The same time limits the shared HTTP connector base uses.
_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)


class MicrosoftLoginError(RuntimeError):
    """The login did not finish, and Microsoft did not refuse it.

    Raised when the login host cannot be reached, answers with a status that
    is not a refusal (for example 429 or 5xx), or answers without a usable
    token. A refused login raises
    :class:`~meho_backplane.connectors._shared.vcf_auth.ConnectorAuthError`
    instead. The message names the target and the login host, never the
    secret or a token.
    """


@dataclass(frozen=True)
class ClientSecretCredentials:
    """A Microsoft Entra ID login: tenant ID, client ID and client secret.

    ``client_secret`` is left out of ``repr`` so it cannot reach a log line by
    accident.
    """

    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class MicrosoftAccessToken:
    """One access token and what MEHO read from it.

    ``value`` is the token itself. It is left out of ``repr`` so it cannot
    reach a log line by accident. ``expires_in`` is the lifetime in seconds
    that Microsoft gave with the token.

    ``app_id``, ``object_id`` and ``tenant_id`` come from the token's claims
    (``appid`` or ``azp``, ``oid`` and ``tid``). Microsoft may change the token
    format at any time, so each one is ``None`` when the token cannot be read.

    ``roles`` holds the application permissions the token carries (claim
    ``roles``, for example ``User.Read.All``), sorted. It is empty when the
    token carries none, and ``None`` when the token cannot be read. A token
    keeps the permissions it was made with: a permission an admin grants
    later is only in the next token.
    """

    value: str = field(repr=False)
    expires_in: float
    app_id: str | None = None
    object_id: str | None = None
    tenant_id: str | None = None
    roles: tuple[str, ...] | None = None


async def fetch_client_credentials_token(
    credentials: ClientSecretCredentials,
    *,
    login_host: str,
    scope: str,
    target_name: str,
    secret_ref: str | None,
) -> MicrosoftAccessToken:
    """Ask Microsoft Entra ID for an access token for *scope* with *credentials*.

    Sends one request and never retries. *target_name* and *secret_ref* only
    go into error messages, so the operator knows which target and which
    secret to check.

    Raises :class:`ConnectorAuthError` when Microsoft refuses the login (HTTP
    400, 401 or 403). The dispatcher then drops the cached secret, so a
    corrected secret is read on the next call. Raises
    :class:`MicrosoftLoginError` for every other failure.
    """
    url = f"https://{login_host}/{quote(credentials.tenant_id, safe='')}/oauth2/v2.0/token"
    form = {
        "grant_type": "client_credentials",
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scope": scope,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, data=form, headers={"Accept": "application/json"})
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in _REFUSED_STATUSES:
            raise _login_refused(
                exc.response,
                login_host=login_host,
                target_name=target_name,
                secret_ref=secret_ref,
            ) from exc
        raise MicrosoftLoginError(
            f"The Microsoft login service at {login_host} answered HTTP {status} "
            f"for target {target_name!r}. Try again later."
        ) from exc
    except httpx.HTTPError as exc:
        raise MicrosoftLoginError(
            f"MEHO could not reach the Microsoft login service at {login_host} for "
            f"target {target_name!r}: {type(exc).__name__}: {exc}"
        ) from exc
    return _parse_token_answer(response, login_host=login_host, target_name=target_name)


def _login_refused(
    response: httpx.Response,
    *,
    login_host: str,
    target_name: str,
    secret_ref: str | None,
) -> ConnectorAuthError:
    """Build the error for a login that Microsoft Entra ID refused."""
    status = response.status_code
    remediation = (
        f"Check tenant_id, client_id and client_secret in the secret at {secret_ref!r}. "
        "A client secret has an end date; make a new one in Microsoft Entra ID when it "
        "has run out."
    )
    message = (
        f"Microsoft Entra ID refused the login for target {target_name!r} "
        f"(HTTP {status}{_error_detail(response)}). {remediation}"
    )
    return ConnectorAuthError(
        message,
        status_code=status,
        cause=f"session_establish_{status}",
        target_name=target_name,
        host=login_host,
        secret_ref=secret_ref,
        remediation=remediation,
    )


def _error_detail(response: httpx.Response) -> str:
    """Return ``", invalid_client, AADSTS7000215"`` style detail, or ``""``.

    Only the OAuth 2.0 error code and the ``AADSTS`` numbers are copied. They
    name the kind of failure (wrong secret, unknown app, unknown tenant) and
    hold no secret. The free text of the answer is left out.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    parts: list[str] = []
    error = body.get("error")
    if isinstance(error, str) and error in _OAUTH2_ERROR_CODES:
        parts.append(error)
    codes = body.get("error_codes")
    if isinstance(codes, list):
        numbers = [code for code in codes if isinstance(code, int) and not isinstance(code, bool)]
        parts.extend(f"AADSTS{number}" for number in numbers[:_MAX_ERROR_CODES])
    return "".join(f", {part}" for part in parts)


def _parse_token_answer(
    response: httpx.Response, *, login_host: str, target_name: str
) -> MicrosoftAccessToken:
    """Read the token and its lifetime from a successful answer."""
    try:
        body = response.json()
    except ValueError as exc:
        raise MicrosoftLoginError(
            f"The Microsoft login service at {login_host} sent an answer that is not "
            f"JSON for target {target_name!r}."
        ) from exc
    if not isinstance(body, dict):
        body = {}
    value = body.get("access_token")
    expires_in = _seconds(body.get("expires_in"))
    if not isinstance(value, str) or not value or expires_in is None:
        raise MicrosoftLoginError(
            f"The Microsoft login service at {login_host} sent no usable access_token "
            f"and expires_in for target {target_name!r}."
        )
    claims = _read_claims(value)
    return MicrosoftAccessToken(
        value=value,
        expires_in=expires_in,
        app_id=_text_claim(claims, "appid") or _text_claim(claims, "azp"),
        object_id=_text_claim(claims, "oid"),
        tenant_id=_text_claim(claims, "tid"),
        roles=_roles_claim(claims),
    )


def _seconds(value: object) -> float | None:
    """Return *value* as a positive number of seconds, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str):
        try:
            seconds = float(value)
        except ValueError:
            return None
    else:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _read_claims(token: str) -> dict[str, Any]:
    """Return the claims of a JWT without checking its signature, or ``{}``.

    MEHO does not trust these claims for any decision. It only shows the
    login's ids to the operator. The token came straight from Microsoft over
    a checked TLS connection.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
    except (binascii.Error, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _text_claim(claims: Mapping[str, Any], name: str) -> str | None:
    """Return the claim *name* when it is a non-empty string."""
    value = claims.get(name)
    return value if isinstance(value, str) and value else None


def _roles_claim(claims: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Return the ``roles`` claim (the application permissions), sorted.

    ``None`` when the token could not be read (no claims at all) or the claim
    is not a list. A readable token without the claim carries no application
    permission, so the answer is empty then.
    """
    if not claims:
        return None
    value = claims.get("roles", [])
    if not isinstance(value, list):
        return None
    return tuple(sorted({role for role in value if isinstance(role, str) and role}))
