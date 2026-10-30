# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""phpIPAM error answers, turned into errors the dispatcher already knows.

phpIPAM answers an error with ``{"code": 404, "success": false, "message":
"..."}`` and the same HTTP status. The message is phpIPAM's text: untrusted
data. It goes only into ``upstream_message`` (the dispatcher filters secrets
out of it and cuts it), never into MEHO's own message.

* :class:`PhpipamApiError` -- every error answer MEHO does not handle itself.
* :class:`PhpipamAddressTakenError` -- ``phpipam.address.reserve`` found the
  address reserved for another host name. Nothing was changed. That host name
  is phpIPAM data too, so it is the ``upstream_message``.
* A refused login or token (401, 403) becomes
  :class:`~meho_backplane.connectors._shared.vcf_auth.ConnectorAuthError`, so
  the dispatcher answers ``connector_auth_failed`` and drops the cached secret.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.base import ConnectorUpstreamError

__all__ = [
    "AUTH_REMEDIATION",
    "PhpipamAddressTakenError",
    "PhpipamApiError",
    "auth_error",
    "phpipam_message",
]

#: phpIPAM's own text is cut to this many characters before MEHO keeps it.
_MESSAGE_CAP: Final[int] = 200

#: Host names from phpIPAM are cut to this many characters in MEHO's texts.
HOSTNAME_CAP: Final[int] = 255

AUTH_REMEDIATION: Final[str] = (
    "Check the secret of the target: 'token' must be the app code of an API app with "
    "'SSL with App code token', or 'username' and 'password' must be a phpIPAM user for an "
    "app with 'SSL with User token'. Check extras.app_id. A change (reserve, release) also "
    "needs the API app permission 'Read / Write'."
)


class PhpipamApiError(ConnectorUpstreamError):
    """phpIPAM answered with an error. The message is MEHO's own text."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        upstream_message: str | None,
        remediation: str | None = None,
    ) -> None:
        super().__init__(message, http_status=http_status, upstream_message=upstream_message)
        self.remediation = remediation


class PhpipamAddressTakenError(ConnectorUpstreamError):
    """The address is reserved for another host name. Nothing was changed."""

    def __init__(self, *, ip: str, subnet: str, held_by: str | None) -> None:
        # The host name is phpIPAM data, so it goes only into upstream_message.
        holder = "another host name" if held_by else "an entry with no host name"
        super().__init__(
            f"{ip} in subnet {subnet} is already reserved for {holder}. Nothing was changed.",
            http_status=409,
            upstream_message=held_by[:HOSTNAME_CAP] if held_by else None,
        )
        self.held_by = held_by
        self.remediation = (
            "Pick another address (phpipam.subnet.free_addresses), or release this one first "
            "if it is really free (phpipam.address.release)."
        )


def phpipam_message(response: httpx.Response) -> str | None:
    """Return phpIPAM's ``message`` from an error answer, cut, or ``None``."""
    try:
        body: Any = response.json()
    except ValueError:
        return None
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str) or not message.strip():
        return None
    return message.strip()[:_MESSAGE_CAP]


def auth_error(
    response: httpx.Response,
    *,
    target: Any,
    stage: str,
    after_relogin: bool = False,
) -> ConnectorAuthError:
    """Return the error for a 401 or 403 answer from phpIPAM.

    *stage* is ``login`` (the user login was refused) or ``call`` (a call
    with the token was refused).
    """
    status = response.status_code
    where = "the user login" if stage == "login" else "the call"
    return ConnectorAuthError(
        f"phpIPAM refused {where} for target {getattr(target, 'name', None)!r} with HTTP {status}.",
        status_code=status,
        cause=f"phpipam_{stage}_{status}",
        target_name=getattr(target, "name", None),
        host=getattr(target, "host", None),
        secret_ref=getattr(target, "secret_ref", None),
        remediation=AUTH_REMEDIATION,
        stage="after_relogin" if after_relogin else ("establish" if stage == "login" else "call"),
    )
