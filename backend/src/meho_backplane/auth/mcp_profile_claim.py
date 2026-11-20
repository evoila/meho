# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``mcp_profile`` claim: a login token made for one MCP tool profile (#6099).

An admin can make a login client for one profile. A fixed claim mapper on
that client writes ``mcp_profile: <name>`` into every token of the client,
so the session cannot get a token without it. MEHO takes such a token only
at ``/mcp?profile=<the same name>``:

* :func:`read_mcp_profile_claim` reads the claim when the token is checked.
  A value MEHO cannot read, or a profile name MEHO does not know, is
  refused at every entrance.
* :func:`refuse_profile_token` refuses the token at every entrance that
  does not allow it on purpose. The one function every entrance uses
  (:func:`~meho_backplane.auth.jwt.verify_jwt_for_audience`) calls it, so a
  new entrance refuses a profile token by default. Only the MCP route
  allows it; it then checks that the URL names the same profile
  (:func:`~meho_backplane.mcp.server._apply_url_options`).

Every refusal is a 403, not a 401: a 401 makes an MCP client start a new
login, which gives the same token again.

A token without the claim works as before, with or without ``?profile=``.

Limit: this stops the reuse of the profile's token. It does not stop the
same person from signing in again through another login client.
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from fastapi import HTTPException, status

from meho_backplane.auth.operator import Operator

__all__ = [
    "MALFORMED_CLAIM",
    "MCP_PROFILE_CLAIM",
    "SCOPE_VIOLATION",
    "UNKNOWN_PROFILE",
    "read_mcp_profile_claim",
    "refuse_profile_token",
]

#: The claim that ties a login token to one MCP tool profile.
MCP_PROFILE_CLAIM: Final[str] = "mcp_profile"

#: 403 details. They never repeat the claim value.
MALFORMED_CLAIM: Final[str] = "malformed_mcp_profile_claim"
UNKNOWN_PROFILE: Final[str] = "unknown_mcp_profile_claim"
SCOPE_VIOLATION: Final[str] = "mcp_profile_scope_violation"


def _http_403(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def read_mcp_profile_claim(claims: Any) -> str | None:
    """Return the profile name of the ``mcp_profile`` claim, or ``None``.

    No claim gives ``None``. A value that is not a non-blank text (also
    ``null``), or a profile name MEHO does not know, raises 403: the claim
    takes rights away, so a value MEHO cannot use must never free the token.
    """
    if MCP_PROFILE_CLAIM not in claims:
        return None
    raw = claims[MCP_PROFILE_CLAIM]
    log = structlog.get_logger(__name__)
    if not isinstance(raw, str) or not raw.strip():
        log.warning(MALFORMED_CLAIM, claim_name=MCP_PROFILE_CLAIM)
        raise _http_403(MALFORMED_CLAIM)
    name = raw.strip().lower()
    # Imported here: the ``mcp`` package imports the token check.
    from meho_backplane.mcp.profiles import is_known_profile

    if not is_known_profile(name):
        log.warning(UNKNOWN_PROFILE, claim_name=MCP_PROFILE_CLAIM)
        raise _http_403(UNKNOWN_PROFILE)
    return name


def refuse_profile_token(operator: Operator) -> None:
    """Raise 403 when *operator*'s token is tied to an MCP tool profile."""
    if operator.mcp_profile_claim is None:
        return
    structlog.get_logger(__name__).warning(
        SCOPE_VIOLATION, operator_sub=operator.sub, profile=operator.mcp_profile_claim
    )
    raise _http_403(SCOPE_VIOLATION)
