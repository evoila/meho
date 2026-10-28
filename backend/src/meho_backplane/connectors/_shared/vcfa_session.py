# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The VCF Automation session login, shared by every VCF 9 connector that signs in through it.

A VCF 9 product such as the built-in Orchestrator signs people in through
VCF Automation. A connector gets a session token from VCF Automation and
sends it as ``Authorization: Bearer <token>`` to the product's own API.
The Orchestrator connector (``extras.login: vcfa-session``) is the first
user; other VCF 9 connectors can call the same function.

How it works
============

* ``POST /cloudapi/1.0.0/sessions`` with HTTP Basic ``<user>@<org>``. For
  the provider org ``System`` the path is
  ``/cloudapi/1.0.0/sessions/provider``.
* The token comes back in the ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` header.
* The token is a JWT. Its ``exp`` claim says when it ends. MEHO reads the
  claim without checking the signature: it only uses the time to plan the
  next login. A token without a usable ``exp`` is treated as ending after
  :data:`DEFAULT_LIFETIME_SECONDS`.
* :meth:`VcfaSessionToken.needs_renewal` is true
  :data:`RENEW_BEFORE_SECONDS` before the end, so the caller logs in again
  before the token runs out. After a ``401`` the caller drops its token and
  logs in once more (the dispatcher's ``invalidate_session`` hook).

What this module does not do
============================

* No cache. The connector keeps the token, per tenant and target.
* No retry. One login request per call.
* No trace. The login goes straight to the connector's HTTP client, not
  through the traced request path, so no trace records the token. No log
  line, error or ``repr`` holds the token or the password.

The paths and the header name are the ones the VCF Automation connector
uses (:mod:`meho_backplane.connectors.vcf_automation._paths` and
``_routing``).
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
import structlog

from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError, SessionLoginError
from meho_backplane.connectors.vcf_automation._paths import ORG_SESSION_PATH
from meho_backplane.connectors.vcf_automation._routing import (
    PROVIDER_CLOUDAPI_ACCEPT,
    PROVIDER_SESSION_PATH,
    PROVIDER_TOKEN_HEADER,
)

__all__ = [
    "DEFAULT_LIFETIME_SECONDS",
    "LOGIN_VCFA_SESSION",
    "RENEW_BEFORE_SECONDS",
    "VcfaSessionToken",
    "session_path",
    "token_expiry",
    "vcfa_session_login",
]

_log = structlog.get_logger(__name__)

#: The ``extras.login`` value that picks this login.
LOGIN_VCFA_SESSION: Final = "vcfa-session"
#: Log in again this many seconds before the token ends.
RENEW_BEFORE_SECONDS: Final = 60.0
#: Lifetime assumed when the token has no usable ``exp`` claim. Thirty
#: minutes is the VCF Automation default for an idle session.
DEFAULT_LIFETIME_SECONDS: Final = 30 * 60.0

_PROVIDER_ORG: Final = "system"


@dataclass(frozen=True)
class VcfaSessionToken:
    """One VCF Automation session token and the time it ends.

    ``value`` is left out of ``repr`` so it cannot reach a log line by
    accident. ``expires_at`` is in seconds since 1970 (UTC).
    """

    value: str = field(repr=False)
    expires_at: float

    @property
    def authorization(self) -> str:
        """The ``Authorization`` header value: ``Bearer <token>``."""
        return f"Bearer {self.value}"

    def needs_renewal(self, now: float | None = None) -> bool:
        """True from :data:`RENEW_BEFORE_SECONDS` before the end of the token."""
        moment = time.time() if now is None else now
        return moment >= self.expires_at - RENEW_BEFORE_SECONDS


def session_path(org: str | None) -> str:
    """The login path: ``/sessions/provider`` for the ``System`` org, else ``/sessions``."""
    if org is not None and org.casefold() == _PROVIDER_ORG:
        return PROVIDER_SESSION_PATH
    return ORG_SESSION_PATH


def token_expiry(token: str, *, now: float) -> float:
    """Return the end of *token* from its ``exp`` claim, or ``now + DEFAULT_LIFETIME_SECONDS``.

    The claim is read without checking the signature. MEHO uses it only to
    plan the next login; VCF Automation still checks the token on every call.
    """
    parts = token.split(".")
    if len(parts) == 3:
        payload = parts[1]
        try:
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (binascii.Error, ValueError):
            claims = None
        exp = claims.get("exp") if isinstance(claims, dict) else None
        if isinstance(exp, int | float) and not isinstance(exp, bool) and math.isfinite(exp):
            return float(exp)
    return now + DEFAULT_LIFETIME_SECONDS


def _refused(
    target: Any, *, username: str, org: str | None, path: str, status: int
) -> ConnectorAuthError:
    """The error for a login VCF Automation refused (HTTP 401 or 403)."""
    secret_ref = getattr(target, "secret_ref", None)
    if org is None:
        remediation = (
            "The target has no extras.org. Set extras.org to the VCF Automation "
            "org of the user (System for the provider org), or check that the "
            f"user name in the secret at {secret_ref!r} ends with @<org>."
        )
    else:
        remediation = (
            f"Check that user {username!r} exists and is enabled in VCF Automation "
            f"org {org!r} (extras.org), and that the secret at {secret_ref!r} holds "
            "its current password."
        )
    return ConnectorAuthError(
        f"VCF Automation refused the session login for target "
        f"{getattr(target, 'name', '?')!r}: POST {path} returned HTTP {status}. {remediation}",
        status_code=status,
        cause=f"session_establish_{status}",
        target_name=getattr(target, "name", None),
        host=getattr(target, "host", None),
        secret_ref=secret_ref,
        remediation=remediation,
    )


async def vcfa_session_login(
    client: httpx.AsyncClient,
    *,
    username: str,
    password: str,
    org: str | None,
    target: Any,
    request_extensions: dict[str, Any] | None = None,
    now: float | None = None,
) -> VcfaSessionToken:
    """Log in to VCF Automation as ``<username>@<org>`` and return the session token.

    *client* must be bound to the target's own address, so the password is
    sent only to the target's host. With no *org*, the user name is sent as
    it is (it may already end with ``@<org>``).

    Raises :class:`ConnectorAuthError` (``connector_auth_failed``) when the
    login is refused with HTTP 401 or 403; the message says what to fix.
    Raises :class:`SessionLoginError` for any other HTTP error and for an
    answer without the token header.
    """
    path = session_path(org)
    user = f"{username}@{org}" if org else username
    response = await client.post(
        path,
        auth=(user, password),
        headers={"Accept": PROVIDER_CLOUDAPI_ACCEPT},
        extensions=request_extensions or {},
    )
    if response.status_code in (401, 403):
        raise _refused(target, username=username, org=org, path=path, status=response.status_code)
    if response.is_error:
        raise SessionLoginError(
            f"VCF Automation session login for target {getattr(target, 'name', '?')!r}: "
            f"POST {path} returned HTTP {response.status_code}"
        )
    token = response.headers.get(PROVIDER_TOKEN_HEADER)
    if not token:
        raise SessionLoginError(
            f"VCF Automation session login for target {getattr(target, 'name', '?')!r}: "
            f"POST {path} returned HTTP {response.status_code} without the "
            f"{PROVIDER_TOKEN_HEADER} header"
        )
    moment = time.time() if now is None else now
    expires_at = token_expiry(token, now=moment)
    _log.info(
        "vcfa_session_established",
        target=getattr(target, "name", None),
        path=path,
        expires_in_seconds=round(expires_at - moment),
    )
    return VcfaSessionToken(value=token, expires_at=expires_at)
