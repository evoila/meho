# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-plane session-establish helpers for the VCF Automation connector.

Split out from :mod:`.connector` to keep that module within the
file-size budget. The helpers here take the per-target httpx client +
the resolved credentials and return the freshly-minted token; cache
ownership and lock discipline stay in the connector class so the
per-plane mutual-exclusion contract is co-located with the cache it
protects.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import structlog
from packaging.version import InvalidVersion, Version

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vcf_auth import (
    ConnectorAuthError,
    session_establish_auth_error,
)
from meho_backplane.connectors.resolver import resolve_target_version
from meho_backplane.connectors.vcf_automation._routing import (
    PROVIDER_CLOUDAPI_ACCEPT,
    PROVIDER_SESSION_PATH,
    PROVIDER_TOKEN_HEADER,
    TENANT_ACCEPT,
    TENANT_CSP_SESSION_PATH,
    TENANT_SESSION_PATH,
    vhost_header,
)
from meho_backplane.connectors.vcf_automation.session import (
    VCFA_REFRESH_TOKEN_FIELD,
    VcfAutomationCredentialsLoader,
    VcfAutomationTargetLike,
)

__all__ = [
    "load_credentials_with_override",
    "tenant_login",
    "vcfa_provider_login",
]

_log = structlog.get_logger(__name__)


async def load_credentials_with_override(
    loader: VcfAutomationCredentialsLoader,
    target: VcfAutomationTargetLike,
    operator: Operator,
    secret_ref: str | None,
) -> dict[str, str]:
    """Invoke *loader* against ``(target, operator)`` (optionally with override *secret_ref*).

    When *secret_ref* matches ``target.secret_ref`` (or is ``None``)
    the target passes through unchanged. When it differs, the loader
    receives a :class:`SimpleNamespace` proxy that mirrors the target's
    attributes with ``secret_ref`` rewritten to the override -- this
    lets the provider plane resolve a distinct Vault path
    (``provider_secret_ref``) when the provider-plane password differs
    from the SSO/tenant secret. ``operator`` is forwarded verbatim so
    the live default loader can perform the operator-context Vault
    read under the operator's identity.
    """
    if secret_ref is None or secret_ref == target.secret_ref:
        return await loader(target, operator)
    proxy = SimpleNamespace(
        name=target.name,
        host=target.host,
        port=getattr(target, "port", None),
        secret_ref=secret_ref,
        auth_model=getattr(target, "auth_model", None),
        fqdn=getattr(target, "fqdn", None),
        domain=getattr(target, "domain", None),
        provider_username=getattr(target, "provider_username", None),
        provider_secret_ref=getattr(target, "provider_secret_ref", None),
    )
    return await loader(proxy, operator)


def _require_username_password(
    creds: dict[str, str], target_name: str, plane: str
) -> tuple[str, str]:
    """Extract ``username`` + ``password`` from *creds*, raising on missing keys."""
    try:
        return creds["username"], creds["password"]
    except KeyError as exc:
        raise RuntimeError(
            f"vcf-automation {plane} credentials loader for target "
            f"{target_name!r} returned a dict missing required key "
            f"{exc.args[0]!r}; need {{'username': str, 'password': str}}"
        ) from exc


def _compose_provider_basic_user(
    creds_username: str,
    provider_username: str | None,
    domain: str | None,
) -> str:
    """Return the verbatim ``provider_username`` when set, otherwise the legacy form.

    The legacy fallback is ``f"{creds_username}@{domain or 'System'}"`` --
    the consumer wrapper carries this for targets that haven't migrated
    to the explicit ``provider_username`` field yet.
    """
    if provider_username:
        return provider_username
    return f"{creds_username}@{domain or 'System'}"


async def vcfa_provider_login(
    client: httpx.AsyncClient,
    creds: dict[str, str],
    target: VcfAutomationTargetLike,
    *,
    request_extensions: dict[str, Any] | None = None,
) -> str:
    """POST the provider session-create endpoint and return the JWT.

    Issues ``POST /cloudapi/1.0.0/sessions/provider`` with HTTP Basic
    auth and ``Accept: application/json;version=9.0.0``. A 2xx response
    carries ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` as a response header --
    the JWT is returned to the caller (which then writes the cache
    under the per-plane lock). Absence of the header on a 2xx response
    raises :exc:`RuntimeError` rather than caching an empty token.

    ``request_extensions`` (evoila/meho#2398) carries the caller's
    ``HttpConnector._request_extensions(target)`` so the login handshake
    honours a target's ``tls_server_name`` / ``fqdn`` SNI + cert-verify
    name; ``None`` normalises to an empty dict (byte-identical when
    unset). The login also presents ``target.fqdn`` as the ``Host:``
    header (:func:`._routing.vhost_header`) so VCFA's strict vhost
    routing accepts the login POST when the transport dials the host IP
    (evoila/meho#2863).
    """
    username, password = _require_username_password(creds, target.name, "provider")
    provider_username = getattr(target, "provider_username", None)
    domain = getattr(target, "domain", None)
    basic_user = _compose_provider_basic_user(username, provider_username, domain)
    try:
        resp = await client.post(
            PROVIDER_SESSION_PATH,
            auth=(basic_user, password),
            headers={
                "Accept": PROVIDER_CLOUDAPI_ACCEPT,
                **vhost_header(getattr(target, "fqdn", None), getattr(target, "port", None)),
            },
            extensions=request_extensions or {},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        message = (
            f"vcf-automation provider session establish failed for target "
            f"{target.name!r}: POST {PROVIDER_SESSION_PATH} returned "
            f"HTTP {exc.response.status_code}"
        )
        # #2329: a 401/403 on the provider-plane login is an auth-class
        # establish failure -> structured ``connector_auth_failed``.
        raise (
            session_establish_auth_error(exc, message=message, target=target)
            or RuntimeError(message)
        ) from exc
    jwt: str | None = resp.headers.get(PROVIDER_TOKEN_HEADER)
    if not jwt:
        raise RuntimeError(
            f"vcf-automation provider session establish for target "
            f"{target.name!r}: POST {PROVIDER_SESSION_PATH} returned "
            f"2xx with no {PROVIDER_TOKEN_HEADER} response header"
        )
    _log.info(
        "vcf_automation_provider_session_established",
        target=target.name,
        host=target.host,
    )
    return jwt


#: First VCF Automation release whose ``POST /iaas/api/login`` accepts only
#: ``{"refreshToken": ...}`` (evoila/meho#3865). A target whose resolved
#: version (:func:`~meho_backplane.connectors.resolver.resolve_target_version`)
#: parses at or above this goes straight to the token exchange; anything else
#: (older, unset, or a non-PEP-440 label such as the ``2021-07-15`` API-date the
#: fingerprint records) tries the legacy username/password body first and
#: falls back on a 400.
_TOKEN_EXCHANGE_MIN_VERSION = Version("9.1")
#: Upper guard so a date-shaped label that happens to parse (``2021.07``) is not
#: mistaken for a product release.
_PRODUCT_VERSION_CEILING = Version("100")

#: The valueless ``?access_token`` flag the CSP login expects.
_CSP_ACCESS_TOKEN_FLAG = {"access_token": ""}

#: Upstream error bodies are quoted into the establish message capped at this
#: length. Login error bodies carry a validation message, never a credential.
_UPSTREAM_SNIPPET_MAX = 200


def _prefers_token_exchange(target: VcfAutomationTargetLike) -> bool:
    """Return ``True`` when *target*'s version says the legacy login cannot work."""
    raw = resolve_target_version(target)
    if raw is None:
        return False
    try:
        version = Version(raw)
    except InvalidVersion:
        return False
    return _TOKEN_EXCHANGE_MIN_VERSION <= version < _PRODUCT_VERSION_CEILING


def _tenant_domain(target: VcfAutomationTargetLike) -> str | None:
    """Return the tenant login ``domain``: ``target.domain``, else ``extras["domain"]``.

    The persisted Target model has no ``domain`` column, so ``extras`` is
    the only operator-settable source on a DB-backed target (same
    projection the ``vra8`` connector reads).
    """
    domain = getattr(target, "domain", None)
    if domain:
        return str(domain)
    extras = getattr(target, "extras", None)
    value = extras.get("domain") if isinstance(extras, dict) else None
    return str(value) if value else None


def _upstream_snippet(response: httpx.Response) -> str:
    """Return the upstream body's ``message`` (JSON) or capped raw text, for diagnostics."""
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        text = payload["message"]
    else:
        text = response.text
    text = " ".join(text.split())
    return text[:_UPSTREAM_SNIPPET_MAX]


def _tenant_headers(target: VcfAutomationTargetLike) -> dict[str, str]:
    """JSON request headers + the vhost ``Host:`` header for a tenant login POST."""
    return {
        "Accept": TENANT_ACCEPT,
        "Content-Type": TENANT_ACCEPT,
        **vhost_header(getattr(target, "fqdn", None), getattr(target, "port", None)),
    }


def _token_from_body(
    resp: httpx.Response, field: str, target: VcfAutomationTargetLike, path: str
) -> str:
    """Pull the non-empty string *field* out of a 2xx JSON login body, or raise."""
    payload: Any = resp.json()
    value = payload.get(field) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        raise RuntimeError(
            f"vcf-automation tenant session establish for target "
            f"{target.name!r}: POST {path} returned 2xx with no {field!r} "
            "field in the response body"
        )
    return value


def _refresh_token_remediation(target: VcfAutomationTargetLike) -> str:
    """The operator hint for a target whose tenant login needs an API token."""
    return (
        f"store a VCF Automation API token (refresh token) as the "
        f"{VCFA_REFRESH_TOKEN_FIELD!r} field of the target's secret "
        f"(secret_ref={getattr(target, 'secret_ref', None)!r})"
    )


async def _legacy_password_login(
    client: httpx.AsyncClient,
    username: str,
    password: str,
    target: VcfAutomationTargetLike,
    *,
    request_extensions: dict[str, Any],
) -> httpx.Response:
    """POST ``{username, password, domain?}`` to ``/iaas/api/login`` (VCFA 9.0 shape).

    Returns the raw response so the caller can branch on a 400 (the 9.1
    "only ``refreshToken`` is accepted" reject) without raising.
    """
    body: dict[str, str] = {"username": username, "password": password}
    domain = _tenant_domain(target)
    if domain:
        body["domain"] = domain
    return await client.post(
        TENANT_SESSION_PATH,
        json=body,
        headers=_tenant_headers(target),
        extensions=request_extensions,
    )


async def _csp_refresh_token(
    client: httpx.AsyncClient,
    username: str,
    password: str,
    target: VcfAutomationTargetLike,
    *,
    request_extensions: dict[str, Any],
    legacy_reject: str | None,
) -> str:
    """Step 1 of the token exchange: CSP ``username``/``password`` → ``refresh_token``.

    A 400/404 here means the appliance serves no usable password→token
    exchange for this account (VCF Automation 9 appliances can answer the CSP
    login with 404), so it raises the structured :class:`ConnectorAuthError`
    naming the ``refresh_token`` secret field as the remediation.
    *legacy_reject* is the upstream message of the preceding legacy 400, if
    any, quoted so the operator sees why the password body was refused.
    """
    body: dict[str, str] = {"username": username, "password": password}
    domain = _tenant_domain(target)
    if domain:
        body["domain"] = domain
    try:
        resp = await client.post(
            TENANT_CSP_SESSION_PATH,
            params=_CSP_ACCESS_TOKEN_FLAG,
            json=body,
            headers=_tenant_headers(target),
            extensions=request_extensions,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        message = (
            f"vcf-automation tenant session establish failed for target "
            f"{target.name!r}: POST {TENANT_CSP_SESSION_PATH} returned HTTP {status}"
        )
        if legacy_reject is not None:
            message += (
                f" after POST {TENANT_SESSION_PATH} rejected the password body "
                f"(HTTP 400: {legacy_reject})"
            )
        auth_error = session_establish_auth_error(exc, message=message, target=target)
        if auth_error is not None:
            raise auth_error from exc
        if status in (400, 404):
            raise ConnectorAuthError(
                f"{message}; {_refresh_token_remediation(target)}",
                status_code=status,
                cause=f"session_establish_{status}",
                target_name=target.name,
                host=getattr(target, "host", None),
                secret_ref=getattr(target, "secret_ref", None),
            ) from exc
        raise RuntimeError(message) from exc
    return _token_from_body(resp, "refresh_token", target, TENANT_CSP_SESSION_PATH)


async def _iaas_token_exchange(
    client: httpx.AsyncClient,
    refresh_token: str,
    target: VcfAutomationTargetLike,
    *,
    request_extensions: dict[str, Any],
) -> str:
    """Step 2: POST ``{"refreshToken": ...}`` to ``/iaas/api/login`` → bearer ``token``.

    400/401/403 mean the refresh token itself was refused (expired, revoked,
    or not a VCFA API token) → structured :class:`ConnectorAuthError`.
    """
    try:
        resp = await client.post(
            TENANT_SESSION_PATH,
            json={"refreshToken": refresh_token},
            headers=_tenant_headers(target),
            extensions=request_extensions,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        message = (
            f"vcf-automation tenant token exchange failed for target "
            f"{target.name!r}: POST {TENANT_SESSION_PATH} with a refresh token "
            f"returned HTTP {status}"
        )
        if status in (400, 401, 403):
            raise ConnectorAuthError(
                f"{message}; the refresh token was refused -- {_refresh_token_remediation(target)}",
                status_code=status,
                cause=f"session_establish_{status}",
                target_name=target.name,
                host=getattr(target, "host", None),
                secret_ref=getattr(target, "secret_ref", None),
            ) from exc
        raise RuntimeError(message) from exc
    return _token_from_body(resp, "token", target, TENANT_SESSION_PATH)


async def tenant_login(
    client: httpx.AsyncClient,
    creds: dict[str, str],
    target: VcfAutomationTargetLike,
    *,
    request_extensions: dict[str, Any] | None = None,
) -> str:
    """Establish a tenant-plane session and return the bearer token.

    Two wire shapes exist for ``POST /iaas/api/login`` (evoila/meho#3865):

    * **Legacy (VCFA 9.0)** -- JSON ``{"username", "password"[, "domain"]}``
      → ``{"token": ...}``.
    * **Token exchange (VCFA 9.1+ / Aria Automation 8.16+)** -- the endpoint
      validates only ``{"refreshToken": ...}`` and 400s any other body. The
      refresh token is the secret's optional ``refresh_token`` field (a VCFA
      API token) when present; otherwise it is minted from the CSP identity
      service (``POST /csp/gateway/am/api/login?access_token`` with the
      username/password).

    Selection, first match wins:

    1. ``creds["refresh_token"]`` set → token exchange with it (any version).
    2. The target's resolved version is ``>= 9.1`` → CSP mint + exchange.
    3. Otherwise → legacy body; on HTTP 400 fall back to CSP mint + exchange.

    A 401/403 on the legacy body is an auth failure (stale password), not a
    shape mismatch, so it raises :class:`ConnectorAuthError` without falling
    back. The caller caches the returned bearer per ``(tenant_id, target.id)``
    and re-runs this whole chain after a data-path 401.

    ``request_extensions`` (evoila/meho#2398) carries the caller's
    ``HttpConnector._request_extensions(target)`` so every login POST
    honours ``tls_server_name`` / ``fqdn`` SNI; each POST also presents
    ``target.fqdn`` as the ``Host:`` header (evoila/meho#2863).
    """
    ext = request_extensions or {}
    refresh_token = creds.get(VCFA_REFRESH_TOKEN_FIELD)
    if refresh_token:
        token = await _iaas_token_exchange(client, refresh_token, target, request_extensions=ext)
        _log_tenant_session(target, "refresh_token")
        return token

    username, password = _require_username_password(creds, target.name, "tenant")
    legacy_reject: str | None = None
    if not _prefers_token_exchange(target):
        resp = await _legacy_password_login(
            client, username, password, target, request_extensions=ext
        )
        if resp.status_code != 400:
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                message = (
                    f"vcf-automation tenant session establish failed for target "
                    f"{target.name!r}: POST {TENANT_SESSION_PATH} returned "
                    f"HTTP {exc.response.status_code}"
                )
                # #2329: 401/403 -> structured ``connector_auth_failed``.
                raise (
                    session_establish_auth_error(exc, message=message, target=target)
                    or RuntimeError(message)
                ) from exc
            token = _token_from_body(resp, "token", target, TENANT_SESSION_PATH)
            _log_tenant_session(target, "password")
            return token
        legacy_reject = _upstream_snippet(resp)
        _log.info(
            "vcf_automation_tenant_login_legacy_rejected",
            target=target.name,
            host=target.host,
            fallback="csp_token_exchange",
        )

    minted = await _csp_refresh_token(
        client,
        username,
        password,
        target,
        request_extensions=ext,
        legacy_reject=legacy_reject,
    )
    token = await _iaas_token_exchange(client, minted, target, request_extensions=ext)
    _log_tenant_session(target, "csp_token_exchange")
    return token


def _log_tenant_session(target: VcfAutomationTargetLike, login_flow: str) -> None:
    """Log a tenant session establish (attribution + flow name only, never a token)."""
    _log.info(
        "vcf_automation_tenant_session_established",
        target=target.name,
        host=target.host,
        login_flow=login_flow,
    )
