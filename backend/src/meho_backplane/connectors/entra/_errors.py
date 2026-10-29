# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Microsoft Graph error answers, turned into errors the dispatcher already knows.

Graph answers an error with ``{"error": {"code": "...", "message": "..."}}``,
the same shape as Azure Resource Manager. So the Azure helper
:func:`~meho_backplane.connectors.azure._errors.azure_error_body` reads it: it
keeps Graph's code only when it looks like a code, and it filters secrets out
of Graph's message and cuts it. Graph's message is untrusted data; it goes only
into ``upstream_message``.

:func:`error_for_response` returns one of the errors below. Each one is a
``ConnectorUpstreamError``, so the dispatcher answers ``connector_error`` with
``extras.exception_class`` (the kind of error), ``extras.http_status``,
``extras.upstream_message`` (Graph's code and message) and
``extras.remediation`` (what to do).

* :class:`EntraPermissionError` (403): the login lacks the application
  permission the call needs, or an admin has not granted consent for it. The
  message names the permission; the remediation names the permissions the
  token carries and what an admin must do. The dispatcher keeps only the
  start of a message, so the facts come first.
* :class:`EntraThrottledError` (429): Graph limits the calls of this login,
  and the retry budget is used up.
* :class:`EntraApiError`: every other error answer.

:class:`EntraUrlRefusedError` is raised when MEHO refuses a URL from Graph (a
next-page link or a redirect). No token went to that URL.
"""

from __future__ import annotations

from typing import Any

import httpx

from meho_backplane.connectors.azure._errors import azure_error_body
from meho_backplane.connectors.base import ConnectorUpstreamError
from meho_backplane.connectors.entra._paths import WIDE_PERMISSION

__all__ = [
    "EntraApiError",
    "EntraPermissionError",
    "EntraThrottledError",
    "EntraUrlRefusedError",
    "error_for_response",
]


class EntraApiError(ConnectorUpstreamError):
    """Microsoft Graph answered with an error. The message is MEHO's own text."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        graph_error_code: str | None,
        graph_message: str | None,
        remediation: str,
    ) -> None:
        upstream = ": ".join(part for part in (graph_error_code, graph_message) if part) or None
        super().__init__(message, http_status=http_status, upstream_message=upstream)
        self.graph_error_code = graph_error_code
        self.graph_message = graph_message
        self.remediation = remediation


class EntraPermissionError(EntraApiError):
    """Graph answered 403: the login may not make this call."""


class EntraThrottledError(EntraApiError):
    """Graph answered 429 and the retry budget does not allow another wait."""

    def __init__(self, message: str, *, retry_after_seconds: float | None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class EntraUrlRefusedError(ValueError):
    """MEHO refused a URL from Graph (a next-page link or a redirect).

    Raised before any request is made, so no token was sent. The message
    names the rule the URL broke, never the URL itself.
    """


def _code_note(code: str | None) -> str:
    return f" (Graph error code {code})" if code else ""


def _granted_text(granted: tuple[str, ...] | None) -> str:
    """Say which application permissions the login's token carries, or ``""``."""
    if granted is None:
        return ""
    if not granted:
        return "The login's access token carries no Microsoft Graph application permission. "
    return f"The login's access token carries: {', '.join(granted)}. "


def _permission_error(
    *,
    target_name: str,
    permission: str,
    granted: tuple[str, ...] | None,
    common: dict[str, Any],
) -> EntraPermissionError:
    """Build the error for a 403 answer.

    The message holds the facts first (the dispatcher keeps only its start):
    which permission the call needs. The remediation holds what the token
    carries and what an admin must do.
    """
    held = _granted_text(granted)
    lead = f"Entra ID target {target_name!r}: Microsoft Graph refused the call (HTTP 403)"
    if granted is not None and (permission in granted or WIDE_PERMISSION in granted):
        carried = permission if permission in granted else WIDE_PERMISSION
        message = f"{lead}, although the login's token carries {carried}."
        remediation = (
            f"{held}So a missing permission is not the cause. Graph's message says why it "
            "refused; a Conditional Access policy for app logins can also block the call. Ask a "
            "Microsoft Entra admin."
        )
    else:
        message = (
            f"{lead}. The login needs the application permission {permission} with admin consent."
        )
        remediation = (
            f"{held}Ask a Microsoft Entra admin with the Privileged Role Administrator role to "
            f"add the Microsoft Graph application permission {permission} to the login's app "
            "registration and to grant admin consent (Microsoft Entra admin center: App "
            "registrations > the app > API permissions > Grant admin consent). "
            f"{WIDE_PERMISSION} also works but allows much more. MEHO asks for a new token "
            "on the next call, so no restart is needed."
        )
    return EntraPermissionError(message, remediation=remediation, **common)


def error_for_response(
    response: httpx.Response,
    *,
    target_name: str,
    permission: str,
    granted: tuple[str, ...] | None,
    retry_after_seconds: float | None = None,
) -> EntraApiError:
    """Return the error for a Graph error answer (any status from 400 up, except 401).

    *permission* is the application permission the call needs; *granted* is
    what the login's token carries (``None`` when the token cannot be read).
    A 401 answer is handled by the caller (a new login and one more try).
    """
    status = response.status_code
    code, message = azure_error_body(response)
    note = _code_note(code)
    common: dict[str, Any] = {
        "http_status": status,
        "graph_error_code": code,
        "graph_message": message,
    }
    if status == 403:
        return _permission_error(
            target_name=target_name, permission=permission, granted=granted, common=common
        )
    if status == 429:
        when = (
            f"in {retry_after_seconds:g} seconds"
            if retry_after_seconds is not None
            else "in a minute"
        )
        remediation = (
            f"Microsoft Graph is limiting the calls of this login. Try again {when}, or make "
            "fewer calls at the same time."
        )
        return EntraThrottledError(
            f"Entra ID target {target_name!r}: Microsoft Graph is throttling the calls of this "
            f"login (HTTP 429){note}. MEHO waited as long as its retry budget allows. "
            f"{remediation}",
            remediation=remediation,
            retry_after_seconds=retry_after_seconds,
            **common,
        )
    if status >= 500:
        remediation = "Microsoft Graph had a problem on its side. Try again later."
    else:
        remediation = "Check the parameters of the call against Graph's message."
    return EntraApiError(
        f"Entra ID target {target_name!r}: Microsoft Graph answered HTTP {status}{note}. "
        f"{remediation}",
        remediation=remediation,
        **common,
    )
