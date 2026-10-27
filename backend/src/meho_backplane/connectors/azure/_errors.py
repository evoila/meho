# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Azure error answers, turned into errors the dispatcher already knows.

Azure Resource Manager answers an error with
``{"error": {"code": "...", "message": "..."}}``. :func:`error_for_response`
reads that body and returns one of the errors below. The dispatcher turns
them into its usual result shape (``docs/codebase/error-message-shape.md``):

* :class:`AzureNotFoundError` is a ``ConnectorResourceNotFoundError``, so the
  caller gets ``not_found``.
* Every other error is an :class:`AzureApiError`, a ``ConnectorUpstreamError``.
  The caller gets ``connector_error`` with ``extras.exception_class`` (the
  kind of error), ``extras.http_status``, ``extras.upstream_message``
  (Azure's code and message) and ``extras.remediation`` (what to do).

Azure's message is untrusted data. It goes only into ``upstream_message``,
which the dispatcher cleans of secrets and cuts to a safe length. MEHO's own
text names Azure's error code only when the code looks like a code.
"""

from __future__ import annotations

import re
from typing import Any, Final

import httpx

from meho_backplane.connectors.base import ConnectorResourceNotFoundError, ConnectorUpstreamError

__all__ = [
    "AzureApiError",
    "AzureConflictError",
    "AzureForbiddenError",
    "AzureNotFoundError",
    "AzureProviderNotRegisteredError",
    "AzureRequestError",
    "AzureServerError",
    "AzureThrottledError",
    "AzureUrlRefusedError",
    "azure_error_body",
    "error_for_response",
]

#: The Azure error code of a provider that is not registered on the subscription.
_NOT_REGISTERED_CODE: Final[str] = "MissingSubscriptionRegistration"

#: What an Azure error code looks like. Anything else is not shown in MEHO's text.
_CODE_SHAPE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,99}")

#: Longest Azure message MEHO keeps before the dispatcher's own cut.
_MESSAGE_CAP: Final[int] = 2048


class AzureApiError(ConnectorUpstreamError):
    """Azure answered with an error. The message is MEHO's own text."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        azure_error_code: str | None,
        azure_message: str | None,
        remediation: str,
    ) -> None:
        upstream = ": ".join(part for part in (azure_error_code, azure_message) if part) or None
        super().__init__(message, http_status=http_status, upstream_message=upstream)
        self.azure_error_code = azure_error_code
        self.azure_message = azure_message
        self.remediation = remediation


class AzureThrottledError(AzureApiError):
    """Azure answered 429 and the retry budget does not allow another wait."""

    def __init__(self, message: str, *, retry_after_seconds: float | None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class AzureForbiddenError(AzureApiError):
    """Azure answered 403: the login has no role that allows this call."""


class AzureProviderNotRegisteredError(AzureApiError):
    """The resource provider is not registered on the subscription."""


class AzureConflictError(AzureApiError):
    """Azure answered 409: the resource is in a state that does not allow the call."""


class AzureServerError(AzureApiError):
    """Azure answered 5xx and the retry budget is used up (or the call is a write)."""


class AzureRequestError(AzureApiError):
    """Azure refused the request itself (for example 400)."""


class AzureNotFoundError(ConnectorResourceNotFoundError):
    """Azure answered 404: the subscription, group or resource does not exist."""


class AzureUrlRefusedError(ValueError):
    """MEHO refused a URL from Azure (a next page or a poll URL).

    Raised before any request is made, so no token was sent. The message
    names the rule the URL broke, never the URL itself.
    """


def _clean_code(value: object) -> str | None:
    if isinstance(value, str) and _CODE_SHAPE.fullmatch(value):
        return value
    return None


def azure_error_body(response: httpx.Response) -> tuple[str | None, str | None]:
    """Return Azure's error code and message from *response*, or ``None`` for each."""
    try:
        body = response.json()
    except ValueError:
        return None, None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None, None
    message = error.get("message")
    text = message[:_MESSAGE_CAP] if isinstance(message, str) and message else None
    return _clean_code(error.get("code")), text


def _code_note(code: str | None) -> str:
    return f" (Azure error code {code})" if code else ""


def error_for_response(
    response: httpx.Response,
    *,
    target_name: str,
    retry_after_seconds: float | None = None,
) -> Exception:
    """Return the error for an Azure error answer (any status from 400 up).

    The 401 answer is handled by the caller (a new login and one more try);
    this function is for every other error status.
    """
    status = response.status_code
    code, message = azure_error_body(response)
    note = _code_note(code)
    common: dict[str, Any] = {
        "http_status": status,
        "azure_error_code": code,
        "azure_message": message,
    }
    if code == _NOT_REGISTERED_CODE:
        remediation = (
            "Ask an Azure administrator to register the resource provider on this "
            "subscription (Subscription > Resource providers). MEHO never registers "
            "it, because that is a change to the subscription."
        )
        return AzureProviderNotRegisteredError(
            f"Azure target {target_name!r}: the resource provider this call needs is "
            f"not registered on the subscription{note}. {remediation}",
            remediation=remediation,
            **common,
        )
    if status == 404:
        path = response.request.url.path if response.request is not None else ""
        return AzureNotFoundError(
            [path],
            f"Azure target {target_name!r}: Azure has no such subscription, resource "
            f"group or resource{note}. Check the name and the subscription.",
        )
    if status == 403:
        remediation = (
            "Give the login's service principal a role on this subscription or "
            "resource group that allows this call (reads need at least Reader), "
            "then try again. azure.about shows which login MEHO uses."
        )
        return AzureForbiddenError(
            f"Azure target {target_name!r}: Azure refused the call because the login "
            f"has no role that allows it{note}. {remediation}",
            remediation=remediation,
            **common,
        )
    if status == 429:
        when = (
            f"in {retry_after_seconds:g} seconds"
            if retry_after_seconds is not None
            else "in a minute"
        )
        remediation = (
            f"Azure is limiting calls from this login. Try again {when}, or make fewer "
            "calls at the same time."
        )
        return AzureThrottledError(
            f"Azure target {target_name!r}: Azure is throttling the calls of this "
            f"login (HTTP 429){note}. MEHO waited as long as its retry budget allows. "
            f"{remediation}",
            remediation=remediation,
            retry_after_seconds=retry_after_seconds,
            **common,
        )
    if status == 409:
        remediation = "Wait until the resource's running change has finished, then try again."
        return AzureConflictError(
            f"Azure target {target_name!r}: the resource is busy or in a state that "
            f"does not allow this call (HTTP 409){note}. {remediation}",
            remediation=remediation,
            **common,
        )
    if status >= 500:
        remediation = "Azure had a problem on its side. Try again later."
        return AzureServerError(
            f"Azure target {target_name!r}: Azure answered HTTP {status}{note}. {remediation}",
            remediation=remediation,
            **common,
        )
    remediation = "Check the parameters of the call against Azure's message."
    return AzureRequestError(
        f"Azure target {target_name!r}: Azure refused the request (HTTP {status}){note}. "
        f"{remediation}",
        remediation=remediation,
        **common,
    )
