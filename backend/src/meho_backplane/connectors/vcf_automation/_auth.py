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

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vcf_auth import session_establish_auth_error
from meho_backplane.connectors.vcf_automation._routing import (
    PROVIDER_CLOUDAPI_ACCEPT,
    PROVIDER_SESSION_PATH,
    PROVIDER_TOKEN_HEADER,
    TENANT_ACCEPT,
    TENANT_SESSION_PATH,
)
from meho_backplane.connectors.vcf_automation.session import (
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
    honours a target's ``tls_server_name`` SNI / cert-verify override;
    ``None`` normalises to an empty dict (byte-identical when unset).
    """
    username, password = _require_username_password(creds, target.name, "provider")
    provider_username = getattr(target, "provider_username", None)
    domain = getattr(target, "domain", None)
    basic_user = _compose_provider_basic_user(username, provider_username, domain)
    try:
        resp = await client.post(
            PROVIDER_SESSION_PATH,
            auth=(basic_user, password),
            headers={"Accept": PROVIDER_CLOUDAPI_ACCEPT},
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


async def tenant_login(
    client: httpx.AsyncClient,
    creds: dict[str, str],
    target: VcfAutomationTargetLike,
    *,
    request_extensions: dict[str, Any] | None = None,
) -> str:
    """POST the tenant login endpoint and return the bearer token.

    Issues ``POST /iaas/api/login`` with JSON body
    ``{"username": ..., "password": ...}`` plus an optional ``domain``
    field when ``target.domain`` is set. The response body is
    ``{"token": "..."}``. Missing / empty ``token`` field on a 2xx
    response raises :exc:`RuntimeError`.

    ``request_extensions`` (evoila/meho#2398) carries the caller's
    ``HttpConnector._request_extensions(target)`` so the login handshake
    honours a target's ``tls_server_name`` SNI / cert-verify override;
    ``None`` normalises to an empty dict (byte-identical when unset).
    """
    username, password = _require_username_password(creds, target.name, "tenant")
    body: dict[str, str] = {"username": username, "password": password}
    domain = getattr(target, "domain", None)
    if domain:
        body["domain"] = domain
    try:
        resp = await client.post(
            TENANT_SESSION_PATH,
            json=body,
            headers={"Accept": TENANT_ACCEPT, "Content-Type": TENANT_ACCEPT},
            extensions=request_extensions or {},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        message = (
            f"vcf-automation tenant session establish failed for target "
            f"{target.name!r}: POST {TENANT_SESSION_PATH} returned "
            f"HTTP {exc.response.status_code}"
        )
        # #2329: a 401/403 on the tenant-plane login is an auth-class
        # establish failure -> structured ``connector_auth_failed``.
        raise (
            session_establish_auth_error(exc, message=message, target=target)
            or RuntimeError(message)
        ) from exc
    payload: Any = resp.json()
    raw_token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(raw_token, str) or not raw_token:
        raise RuntimeError(
            f"vcf-automation tenant session establish for target "
            f"{target.name!r}: POST {TENANT_SESSION_PATH} returned "
            "2xx with no 'token' field in the response body"
        )
    _log.info(
        "vcf_automation_tenant_session_established",
        target=target.name,
        host=target.host,
    )
    return raw_token
