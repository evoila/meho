# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credentials and login for the modern ``fleet-lcm`` connector.

The VCF 9.1 fleet lifecycle API takes a Bearer token. VCF Operations hands
out that token in two steps (#4476):

1. ``POST /suite-api/api/auth/token/acquire`` with the VCF Operations user
   and password. The answer carries a VCF Operations token.
2. ``POST /suite-api/api/auth/token/exchange`` with
   ``Authorization: OpsToken <token>`` and the body
   ``{"serviceKeys": ["fleet-lcm"]}``. The answer carries ``jwtToken``,
   the Bearer token for the fleet lifecycle API.

Both paths are in the vendor's VCF Operations 9.1 API file
(``acquireToken`` and ``exchangeOpsTokenWithJwtToken``).

This module holds:

* :data:`FleetLcmTargetLike` / :data:`FleetLcmCredentialsLoader` — aliases
  of the shared VCF target shape and loader type.
* :func:`load_credentials_from_vault` — reads the target's Vault secret:
  the VCF Operations ``username`` + ``password`` (required), an optional
  ``vcf_ops_url`` (where VCF Operations answers, when that is not the
  target's own address) and an optional stored ``token`` (the old way,
  kept as a fallback).
* :func:`mint_fleet_lcm_token` — runs the two login steps on a given
  client and returns the Bearer token.

No token or password is ever written to a log line or an error text.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_vault_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors._shared.vcf_auth import (
    SessionLoginError,
    VcfCredentialsLoader,
    VcfTargetLike,
    session_establish_auth_error,
    vcf_session_login,
)

__all__ = [
    "FLEET_LCM_SERVICE_KEY",
    "FleetLcmCredentialsLoader",
    "FleetLcmTargetLike",
    "load_credentials_from_vault",
    "mint_fleet_lcm_token",
]

#: Minimum target shape :class:`FleetLcmConnector` reads (the shared
#: VCF-family Protocol).
FleetLcmTargetLike = VcfTargetLike

#: Async ``(target, operator) -> dict[str, str]`` credential loader.
FleetLcmCredentialsLoader = VcfCredentialsLoader

#: VCF Operations login paths (vendor file ``vcf-operations-openapi.json``,
#: server base ``/suite-api``). The acquire path is also in the 9.0 file; the
#: exchange path is new in 9.1. The spec-check lane reads these constants.
_TOKEN_ACQUIRE_PATH = "/suite-api/api/auth/token/acquire"
_TOKEN_EXCHANGE_PATH = "/suite-api/api/auth/token/exchange"

#: The service key that asks VCF Operations for a fleet lifecycle token.
FLEET_LCM_SERVICE_KEY = "fleet-lcm"

#: KV-v2 secret field names the loader reads.
_USERNAME_FIELD = "username"
_PASSWORD_FIELD = "password"
_TOKEN_FIELD = "token"
_VCF_OPS_URL_FIELD = "vcf_ops_url"

_JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}


async def load_credentials_from_vault(
    target: FleetLcmTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Read the fleet-lcm credentials from Vault, under the operator's identity.

    Returns ``username`` + ``password`` (the VCF Operations account — always
    required) and, when set:

    * ``vcf_ops_url`` — the ``https://host[:port]`` address of VCF
      Operations. Without it, MEHO logs in at the target's own address.
    * ``token`` — a stored fleet lifecycle token. When set, MEHO sends it as
      is and does not log in (the old way, kept as a fallback).

    A missing pair or a bad ``vcf_ops_url`` raises
    :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
    naming the target, so a half-set secret fails closed.
    """
    secret_data = await load_vault_secret_data(target, operator)

    username = _optional_field(secret_data, _USERNAME_FIELD)
    password = _optional_field(secret_data, _PASSWORD_FIELD)
    if not (username and password):
        raise VaultCredentialsReadError(
            f"fleet-lcm target {target.name!r}: secret at secret_ref must carry a "
            f"{_USERNAME_FIELD!r} + {_PASSWORD_FIELD!r} pair (the VCF Operations "
            f"account MEHO logs in with). Optionally add {_VCF_OPS_URL_FIELD!r} when "
            f"VCF Operations has a different address than the target."
        )

    credentials = {_USERNAME_FIELD: username, _PASSWORD_FIELD: password}
    ops_url = _optional_field(secret_data, _VCF_OPS_URL_FIELD)
    if ops_url:
        credentials[_VCF_OPS_URL_FIELD] = _check_ops_url(ops_url, target.name)
    token = _optional_field(secret_data, _TOKEN_FIELD)
    if token:
        credentials[_TOKEN_FIELD] = token
    return credentials


def _check_ops_url(value: str, target_name: str) -> str:
    """Return *value* if it is a plain ``https://host[:port]`` address, else raise.

    Only https is allowed: the VCF Operations password travels in the login
    body. A path, query, fragment or user part is refused so the value
    cannot point the login anywhere but the root of one host.
    """
    if not _is_plain_https_origin(value):
        raise VaultCredentialsReadError(
            f"fleet-lcm target {target_name!r}: {_VCF_OPS_URL_FIELD!r} must be an "
            f"https address with no path, for example https://vcf-ops.example.com"
        )
    return value


def _is_plain_https_origin(value: str) -> bool:
    """True when *value* is ``https://host`` or ``https://host:port`` (one ``/`` allowed)."""
    parts = urlsplit(value)
    try:
        _ = parts.port  # a non-numeric or out-of-range port raises ValueError
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.path in ("", "/")
        and not parts.query
        and not parts.fragment
    )


def _optional_field(secret_data: dict[str, object], field: str) -> str | None:
    """Return *field* from the KV-v2 payload as a stripped str, or ``None``.

    A missing key, an empty string or a whitespace-only value all count as
    "not set".
    """
    raw = secret_data.get(field)
    if raw is None:
        return None
    value = strip_credential_value(raw)
    return value or None


async def mint_fleet_lcm_token(
    client: httpx.AsyncClient,
    *,
    username: str,
    password: str,
    target_name: str,
    request_extensions: dict[str, Any] | None = None,
) -> str:
    """Log in to VCF Operations on *client* and return a fleet lifecycle token.

    Step 1 (acquire) goes through the shared
    :func:`~meho_backplane.connectors._shared.vcf_auth.vcf_session_login`.
    Step 2 (exchange) sends the VCF Operations token as
    ``Authorization: OpsToken <token>`` and asks for the ``fleet-lcm``
    service key.

    A ``401`` / ``403`` at either step raises
    :class:`~meho_backplane.connectors._shared.vcf_auth.ConnectorAuthError`
    (the dispatcher turns it into ``connector_auth_failed``). Any other
    failure raises :class:`SessionLoginError`. The error text names the
    target, the path and the HTTP status — never a token.
    """
    ops_token = await vcf_session_login(
        client,
        _TOKEN_ACQUIRE_PATH,
        username=username,
        password=password,
        target_name=target_name,
        token_extractor=lambda resp: _body_string(resp, "token"),
        request_headers=_JSON_HEADERS,
        request_extensions=request_extensions,
    )
    try:
        resp = await client.post(
            _TOKEN_EXCHANGE_PATH,
            json={"serviceKeys": [FLEET_LCM_SERVICE_KEY]},
            headers={**_JSON_HEADERS, "Authorization": f"OpsToken {ops_token}"},
            extensions=request_extensions or {},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        message = (
            f"fleet-lcm token exchange failed for target {target_name!r}: "
            f"POST {_TOKEN_EXCHANGE_PATH} returned HTTP {exc.response.status_code}"
        )
        raise (
            session_establish_auth_error(exc, message=message, target_name=target_name)
            or SessionLoginError(message)
        ) from exc
    token = _body_string(resp, "jwtToken")
    if not token:
        raise SessionLoginError(
            f"fleet-lcm token exchange for target {target_name!r}: "
            f"POST {_TOKEN_EXCHANGE_PATH} returned {resp.status_code} with no jwtToken"
        )
    return token


def _body_string(resp: httpx.Response, key: str) -> str | None:
    """Return the non-empty string *key* of a JSON object body, else ``None``."""
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return value if isinstance(value, str) and value else None
