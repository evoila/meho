# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Provider session-establish helper for the ``vcd`` connector.

Split out from :mod:`.connector` to keep that module lean, the
:mod:`~meho_backplane.connectors.vcf_automation._auth` precedent. The helper
takes the per-target httpx client + resolved credentials and returns the
freshly-minted Bearer JWT; cache ownership and lock discipline stay in the
connector class so the mutual-exclusion contract is co-located with the cache
it protects.

vCD's provider login is the vCloud-Director-derived Basic→Bearer flow the
``vcf-automation`` provider plane also speaks (they share the surface — VCFA
absorbed vCD's provider control plane): ``POST /cloudapi/1.0.0/sessions/provider``
with HTTP Basic (``<user>@System``) returns the JWT in the
``X-VMWARE-VCLOUD-ACCESS-TOKEN`` **response header**, which every subsequent
call sends as ``Authorization: Bearer <jwt>``.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from meho_backplane.connectors._shared.vcf_auth import session_establish_auth_error
from meho_backplane.connectors.vcd._paths import (
    _SESSIONS_PROVIDER_PATH,
    VCD_TOKEN_HEADER,
    cloudapi_accept,
)
from meho_backplane.connectors.vcd.session import VcloudDirectorTargetLike

__all__ = ["compose_provider_basic_user", "vcd_provider_login"]

_log = structlog.get_logger(__name__)


def _require_username_password(creds: dict[str, str], target_name: str) -> tuple[str, str]:
    """Extract ``username`` + ``password`` from *creds*, raising on a missing key."""
    try:
        return creds["username"], creds["password"]
    except KeyError as exc:
        raise RuntimeError(
            f"vcd provider credentials loader for target {target_name!r} returned a "
            f"dict missing required key {exc.args[0]!r}; need "
            f"{{'username': str, 'password': str}}"
        ) from exc


def compose_provider_basic_user(username: str) -> str:
    """Return the vCD provider Basic username — ``"<user>@System"``.

    vCD provider (administrator) accounts always live in the built-in
    ``System`` organization, so the Basic username is the credential
    username suffixed with ``@System``. A non-System / SSO provider
    identity (an explicit ``provider_username`` override) is a documented
    follow-up; the System-org form covers the provider-account login every
    vCD deployment ships with.
    """
    return f"{username}@System"


async def vcd_provider_login(
    client: httpx.AsyncClient,
    creds: dict[str, str],
    target: VcloudDirectorTargetLike,
    *,
    api_version: str,
    request_extensions: dict[str, Any] | None = None,
) -> str:
    """POST the provider session-create endpoint and return the minted JWT.

    Issues ``POST /cloudapi/1.0.0/sessions/provider`` with HTTP Basic
    (``<user>@System``) and ``Accept: application/json;version=<api_version>``.
    A 2xx response carries the JWT in the ``X-VMWARE-VCLOUD-ACCESS-TOKEN``
    response header — it is returned to the caller (which writes the cache
    under the session lock). Absence of the header on a 2xx response raises
    :exc:`RuntimeError` rather than caching an empty token.

    A 401/403 at login is an auth-class establish failure, mapped to the
    structured :class:`~meho_backplane.connectors._shared.vcf_auth.ConnectorAuthError`
    (``connector_auth_failed``) via
    :func:`~meho_backplane.connectors._shared.vcf_auth.session_establish_auth_error`,
    the ``vcf-automation`` / #2329 precedent. ``request_extensions`` carries
    the caller's :meth:`HttpConnector._request_extensions` so the login
    handshake honours a target's ``tls_server_name`` SNI / cert-verify name
    (``None`` normalises to ``{}``, byte-identical when unset).
    """
    username, password = _require_username_password(creds, target.name)
    basic_user = compose_provider_basic_user(username)
    try:
        resp = await client.post(
            _SESSIONS_PROVIDER_PATH,
            auth=(basic_user, password),
            headers={"Accept": cloudapi_accept(api_version)},
            extensions=request_extensions or {},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        message = (
            f"vcd provider session establish failed for target {target.name!r}: "
            f"POST {_SESSIONS_PROVIDER_PATH} returned HTTP {exc.response.status_code}"
        )
        raise (
            session_establish_auth_error(exc, message=message, target=target)
            or RuntimeError(message)
        ) from exc
    jwt: str | None = resp.headers.get(VCD_TOKEN_HEADER)
    if not jwt:
        raise RuntimeError(
            f"vcd provider session establish for target {target.name!r}: "
            f"POST {_SESSIONS_PROVIDER_PATH} returned 2xx with no "
            f"{VCD_TOKEN_HEADER} response header"
        )
    _log.info("vcd_provider_session_established", target=target.name, host=target.host)
    return jwt
