# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two-step session-establish helper for the ``vra8`` connector.

Split out from :mod:`.connector` to keep that module lean (the ``vcf-automation``
/ ``vcd`` precedent). The helper takes the per-target httpx client + resolved
credentials and returns the freshly-minted bearer token; cache ownership and lock
discipline stay in the connector class so the mutual-exclusion contract is
co-located with the cache it protects.

vRA 8's token acquisition is a **two-step exchange** (confirmed against the
Broadcom vRA 8.x API Programming Guide + the ``vmware/vra-sdk-go`` ``vra-iaas.json``
Swagger 2.0 spec):

1. **CSP identity** — ``POST /csp/gateway/am/api/login?access_token`` with a JSON
   ``{username, password, domain?}`` body returns a long-lived (~90-day)
   ``refresh_token`` (snake_case) in the response body. ``domain`` is optional
   (read from ``target.extras["domain"]`` — the Target model has no dedicated
   column): omitted for a local/basic vRA user, set to the directory/AD domain (or
   the literal ``"System Domain"`` for local system users) otherwise. This CSP
   identity endpoint is **not** part of ``vra-sdk-go``.
2. **IaaS token exchange** — ``POST /iaas/api/login`` with a ``{refreshToken}``
   (camelCase — note the case flip from step 1's ``refresh_token``) body returns a
   short-lived (~8h) bearer ``token`` in a ``{tokenType, token}`` response body
   (spec-confirmed).

The one bearer authenticates every vRA 8 service (``/iaas``, ``/deployment``,
``/blueprint``, ``/catalog``). A 401/403 at *either* step is an auth-class establish
failure, mapped to the structured
:class:`~meho_backplane.connectors._shared.vcf_auth.ConnectorAuthError`
(``connector_auth_failed``) via
:func:`~meho_backplane.connectors._shared.vcf_auth.session_establish_auth_error`
(the ``vcf-automation`` / #2329 precedent).
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from meho_backplane.connectors._shared.vcf_auth import session_establish_auth_error
from meho_backplane.connectors.vra8._paths import _CSP_LOGIN_PATH, _IAAS_LOGIN_PATH
from meho_backplane.connectors.vra8.session import Vra8TargetLike

__all__ = ["vra8_login"]

_log = structlog.get_logger(__name__)

_JSON_ACCEPT = "application/json"
#: The literal valueless ``?access_token`` query flag the CSP login requires
#: (per the vRA 8 Programming Guide). httpx renders ``{"access_token": ""}`` as
#: ``?access_token=`` — accepted equivalently by the appliance; the exact bare-flag
#: wire form is a documented live-verify tail (connector docstring).
_CSP_ACCESS_TOKEN_FLAG = {"access_token": ""}


def _require_username_password(creds: dict[str, str], target_name: str) -> tuple[str, str]:
    """Extract ``username`` + ``password`` from *creds*, raising on a missing key."""
    try:
        return creds["username"], creds["password"]
    except KeyError as exc:
        raise RuntimeError(
            f"vra8 credentials loader for target {target_name!r} returned a dict "
            f"missing required key {exc.args[0]!r}; need "
            f"{{'username': str, 'password': str}}"
        ) from exc


async def _csp_refresh_token(
    client: httpx.AsyncClient,
    creds: dict[str, str],
    target: Vra8TargetLike,
    *,
    request_extensions: dict[str, Any],
) -> str:
    """Step 1 — POST the CSP login and return the ``refresh_token``.

    ``POST /csp/gateway/am/api/login?access_token`` with ``{username, password,
    domain?}``. A 2xx carries ``{"refresh_token": ...}`` (snake_case); a missing /
    empty field raises :exc:`RuntimeError`. 401/403 → structured auth error.
    """
    username, password = _require_username_password(creds, target.name)
    body: dict[str, str] = {"username": username, "password": password}
    # The optional CSP domain selector comes from the target's per-product
    # ``extras`` (the Target model has no dedicated column): set ``extras["domain"]``
    # to the AD/directory domain, or "System Domain" for a local vRA system user;
    # omit it entirely for a plain local user. Reading a phantom ``target.domain``
    # attribute would silently omit it for every real target (the model has none).
    extras = getattr(target, "extras", None)
    domain = extras.get("domain") if isinstance(extras, dict) else None
    if domain:
        body["domain"] = domain
    try:
        resp = await client.post(
            _CSP_LOGIN_PATH,
            params=_CSP_ACCESS_TOKEN_FLAG,
            json=body,
            headers={"Accept": _JSON_ACCEPT},
            extensions=request_extensions,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        message = (
            f"vra8 CSP identity login failed for target {target.name!r}: "
            f"POST {_CSP_LOGIN_PATH} returned HTTP {exc.response.status_code}"
        )
        raise (
            session_establish_auth_error(exc, message=message, target=target)
            or RuntimeError(message)
        ) from exc
    payload: Any = resp.json()
    refresh_token = payload.get("refresh_token") if isinstance(payload, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError(
            f"vra8 CSP identity login for target {target.name!r}: "
            f"POST {_CSP_LOGIN_PATH} returned 2xx with no 'refresh_token' "
            "field in the response body"
        )
    return refresh_token


async def _iaas_bearer_token(
    client: httpx.AsyncClient,
    refresh_token: str,
    target: Vra8TargetLike,
    *,
    request_extensions: dict[str, Any],
) -> str:
    """Step 2 — exchange the refresh token for the IaaS bearer access token.

    ``POST /iaas/api/login`` with ``{"refreshToken": ...}`` (camelCase). A 2xx
    carries ``{"tokenType": "Bearer", "token": ...}``; a missing / empty ``token``
    raises :exc:`RuntimeError`. 401/403 → structured auth error.
    """
    try:
        resp = await client.post(
            _IAAS_LOGIN_PATH,
            json={"refreshToken": refresh_token},
            headers={"Accept": _JSON_ACCEPT},
            extensions=request_extensions,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        message = (
            f"vra8 IaaS token exchange failed for target {target.name!r}: "
            f"POST {_IAAS_LOGIN_PATH} returned HTTP {exc.response.status_code}"
        )
        raise (
            session_establish_auth_error(exc, message=message, target=target)
            or RuntimeError(message)
        ) from exc
    payload: Any = resp.json()
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError(
            f"vra8 IaaS token exchange for target {target.name!r}: "
            f"POST {_IAAS_LOGIN_PATH} returned 2xx with no 'token' field in the "
            "response body"
        )
    return token


async def vra8_login(
    client: httpx.AsyncClient,
    creds: dict[str, str],
    target: Vra8TargetLike,
    *,
    request_extensions: dict[str, Any] | None = None,
) -> str:
    """Run the vRA 8 two-step token exchange and return the bearer access token.

    Step 1 mints a ``refresh_token`` from the CSP identity service; step 2
    exchanges it for the short-lived bearer the connector caches and sends as
    ``Authorization: Bearer <token>`` on every subsequent read. The refresh token
    is *not* cached separately — a cache eviction (401 recovery) re-runs both steps,
    which is rare (only on the ~8h bearer's expiry) and keeps the connector state to
    the single minted bearer.

    ``request_extensions`` (evoila/meho#2398) carries the caller's
    :meth:`HttpConnector._request_extensions` so both login POSTs honour a target's
    ``tls_server_name`` SNI / cert-verify name; ``None`` normalises to ``{}``
    (byte-identical when unset).
    """
    ext = request_extensions or {}
    refresh_token = await _csp_refresh_token(client, creds, target, request_extensions=ext)
    token = await _iaas_bearer_token(client, refresh_token, target, request_extensions=ext)
    _log.info("vra8_session_established", target=target.name, host=target.host)
    return token
