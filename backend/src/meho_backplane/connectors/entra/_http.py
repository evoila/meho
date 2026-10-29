# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every Microsoft Graph call of the Entra ID connector goes through :func:`send`.

The retry rules are the Azure connector's (``connectors/azure/_http.py``), with
the same numbers, imported from there:

* **One retry budget per call.** At most ``MAX_ATTEMPTS`` tries and at most
  ``MAX_WAIT_SECONDS`` of waiting between them. This is the only retry loop of
  a Graph call: the retry of the shared HTTP base is not used, and a 401 is
  handled here (one new login, one more try, counted in the same budget).
* **Waiting when Graph throttles.** A 429 answer waits ``Retry-After`` (or a
  short back-off when it is missing) and tries again if the budget allows;
  otherwise the call fails with ``EntraThrottledError``.
* A 5xx answer or a lost connection is tried again after a short wait. Every
  Graph call here is a read (GET), so repeating it is safe.
* **No redirects.** The client follows none (``NoRedirectClient``); a 3xx
  answer fails the call, so no token goes to another address.

Two rules are Graph's own:

* **A 403 answer drops the token** before the call fails with
  ``EntraPermissionError``. A token keeps the permissions it was made with. So
  after an admin grants a missing permission, the next call logs in again and
  its new token carries the permission. No restart is needed.
* **Next-page links are checked.** Graph puts the next page in
  ``@odata.nextLink``, a full URL. :func:`check_next_link` checks it before
  the token goes to it.

The Azure ``send`` is not reused as it is: its URL check needs a subscription
in the path, and its errors speak of Azure roles. The retry rules are the same.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.microsoft_login import MicrosoftAccessToken
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.adapters.http import (
    SsrfBlockedError,
    _read_capped_json_response,
)
from meho_backplane.connectors.azure._http import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    MAX_WAIT_SECONDS,
    THROTTLE_BACKOFF_SECONDS,
    NoRedirectClient,
    retry_after_seconds,
)
from meho_backplane.connectors.entra._errors import EntraUrlRefusedError, error_for_response
from meho_backplane.connectors.entra.session import EntraTargetLike, resolve_target_config
from meho_backplane.flight_recorder import capture as flight_recorder_capture

if TYPE_CHECKING:
    from meho_backplane.connectors.entra.connector import EntraConnector

__all__ = ["MAX_NEXT_LINK_LENGTH", "NoRedirectClient", "check_next_link", "send"]

#: Longest next-page link from Graph that MEHO follows. Graph puts the query of
#: the first page and a page token into the link, which stays well below this.
MAX_NEXT_LINK_LENGTH: Final[int] = 8192

#: Transport errors after which a read is tried again (the shared HTTP base's set).
_RETRYABLE_TRANSPORT_ERRORS: Final[tuple[type[httpx.TransportError], ...]] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
)

#: Sleep. Tests replace it to move time forward without waiting.
_sleep = asyncio.sleep


class _Budget:
    """The tries and the waiting one call has used."""

    def __init__(self) -> None:
        self.attempts = 0
        self.waited = 0.0

    def allows(self, delay: float) -> bool:
        return self.attempts < MAX_ATTEMPTS and self.waited + delay <= MAX_WAIT_SECONDS

    def backoff(self, steps: tuple[float, ...]) -> float:
        return steps[min(self.attempts, len(steps)) - 1]


def check_next_link(target: EntraTargetLike, url: str, *, path: str) -> str:
    """Check a next-page link from Graph before MEHO sends a token to it.

    The link must use https on port 443, have no user name or password, be at
    most :data:`MAX_NEXT_LINK_LENGTH` characters long, point at the target's
    Microsoft Graph host, and continue the same list: its path must be *path*
    (the hand-written path of the first page), compared without regard to
    case, with no ``.`` or ``..`` part. Returns *url* unchanged. Raises
    :class:`EntraUrlRefusedError` otherwise; the error never repeats the URL.
    """
    config = resolve_target_config(target)
    name = target.name
    if len(url) > MAX_NEXT_LINK_LENGTH:
        raise EntraUrlRefusedError(
            f"Entra ID target {name!r}: a next-page link from Microsoft Graph is longer "
            f"than {MAX_NEXT_LINK_LENGTH} characters. MEHO did not follow it."
        )
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError, TypeError) as exc:
        raise EntraUrlRefusedError(
            f"Entra ID target {name!r}: a next-page link from Microsoft Graph is not a "
            "valid URL. MEHO did not follow it."
        ) from exc
    if parsed.scheme != "https" or parsed.userinfo or parsed.port not in (None, 443):
        raise EntraUrlRefusedError(
            f"Entra ID target {name!r}: a next-page link from Microsoft Graph does not use "
            "plain https on port 443. MEHO sent no token to it."
        )
    if parsed.host.lower() != config.cloud.graph_host:
        raise EntraUrlRefusedError(
            f"Entra ID target {name!r}: a next-page link from Microsoft Graph points to "
            f"another host than {config.cloud.graph_host}. MEHO sent no token to it."
        )
    segments = parsed.path.split("/")
    if any(part in (".", "..") for part in segments) or parsed.path.lower() != path.lower():
        raise EntraUrlRefusedError(
            f"Entra ID target {name!r}: a next-page link from Microsoft Graph does not "
            "continue the same list. MEHO sent no token to it."
        )
    return url


async def send(
    connector: EntraConnector,
    target: EntraTargetLike,
    url: str,
    *,
    operator: Operator,
    permission: str,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    list_path: str | None = None,
) -> httpx.Response:
    """Send one GET to Microsoft Graph inside one retry budget and return the answer.

    *url* is one of the hand-written paths (it starts with ``/``) or a
    next-page link from Graph, which must continue the list at *list_path*
    and is checked with :func:`check_next_link` first. *permission* is the
    application permission the call needs; a refusal names it.

    Returns the answer for any status below 300. Raises
    :class:`EntraUrlRefusedError` for a refused link or a redirect, an error
    from :mod:`._errors` for an error answer, ``ConnectorAuthError`` when
    Graph still answers 401 after a new login, or the ``httpx`` error of a
    lost connection or a timeout.
    """
    if not url.startswith("/"):
        check_next_link(target, url, path=list_path or "")
    budget = _Budget()
    logged_in_again = False
    while True:
        budget.attempts += 1
        try:
            response, token = await _send_once(connector, target, url, operator, params, headers)
        except httpx.TransportError as exc:
            delay = _transport_delay(exc, budget)
            if delay is None:
                raise
            await _wait(budget, delay)
            continue
        status = response.status_code
        if status < 300:
            return response
        if status < 400:
            raise _redirect_refused(target, status)
        if status == 401:
            if logged_in_again or budget.attempts >= MAX_ATTEMPTS:
                raise _still_unauthorized(target, logged_in_again)
            logged_in_again = True
            await connector.invalidate_session(target)
            continue
        wait_hint = retry_after_seconds(response)
        delay = _status_delay(status, wait_hint, budget)
        if delay is not None:
            await _wait(budget, delay)
            continue
        if status == 403:
            # The next call logs in again, so a permission granted since this
            # token was made takes effect at once.
            await connector.invalidate_session(target)
        raise error_for_response(
            response,
            target_name=target.name,
            permission=permission,
            granted=token.roles,
            retry_after_seconds=wait_hint,
        )


async def _wait(budget: _Budget, delay: float) -> None:
    budget.waited += delay
    await _sleep(delay)


def _transport_delay(exc: httpx.TransportError, budget: _Budget) -> float | None:
    """The wait before trying again after a lost connection, or ``None`` to stop."""
    if isinstance(exc, SsrfBlockedError) or not isinstance(exc, _RETRYABLE_TRANSPORT_ERRORS):
        return None
    delay = budget.backoff(BACKOFF_SECONDS)
    return delay if budget.allows(delay) else None


def _status_delay(status: int, wait_hint: float | None, budget: _Budget) -> float | None:
    """The wait before trying again after an error status, or ``None`` to stop."""
    if status == 429:
        delay = wait_hint if wait_hint is not None else budget.backoff(THROTTLE_BACKOFF_SECONDS)
    elif status >= 500:
        delay = wait_hint if wait_hint is not None else budget.backoff(BACKOFF_SECONDS)
    else:
        return None
    return delay if budget.allows(delay) else None


def _redirect_refused(target: EntraTargetLike, status: int) -> EntraUrlRefusedError:
    """The error for a 3xx answer. MEHO follows no redirect of a Graph call."""
    return EntraUrlRefusedError(
        f"Entra ID target {target.name!r}: Microsoft Graph answered {status} with a "
        "redirect. MEHO does not follow redirects of Graph calls, so no token went to the "
        "new address. Check the target's host and run entra.about."
    )


def _still_unauthorized(target: EntraTargetLike, logged_in_again: bool) -> ConnectorAuthError:
    """The error when Graph keeps answering 401."""
    remediation = (
        "Microsoft Graph did not accept a new token of this login. Check that the login's "
        "app registration still exists in the target's tenant and that its secret is "
        "valid, then run entra.about."
    )
    return ConnectorAuthError(
        f"Entra ID target {target.name!r}: Microsoft Graph answered 401 again after a new "
        f"login. {remediation}",
        status_code=401,
        cause="session_dispatch_401_after_relogin" if logged_in_again else "dispatch_401",
        target_name=target.name,
        host=target.host,
        secret_ref=target.secret_ref,
        remediation=remediation,
        stage="after_relogin" if logged_in_again else "call",
    )


async def _send_once(
    connector: EntraConnector,
    target: EntraTargetLike,
    url: str,
    operator: Operator,
    params: dict[str, str] | None,
    headers: dict[str, str] | None,
) -> tuple[httpx.Response, MicrosoftAccessToken]:
    """Send the request once, record it in the trace, and return it with its token."""
    auth, token = await connector.authorize(target, operator)
    request_headers: dict[str, str] = {**(headers or {}), **auth}
    client = await connector._http_client(target)
    span = flight_recorder_capture.span_start()
    try:
        response = await _read_capped_json_response(
            client,
            "GET",
            url,
            params=params,
            headers=request_headers,
            extensions=connector._request_extensions(target),
        )
    except httpx.TransportError as exc:
        flight_recorder_capture.record_vendor_call_error(span, method="GET", exc=exc)
        raise
    flight_recorder_capture.record_vendor_call(
        span,
        method="GET",
        request_headers=request_headers,
        response=response,
        request_body=None,
        request_content_type=None,
        response_body=connector._trace_response_body(span, response),
    )
    return response, token
