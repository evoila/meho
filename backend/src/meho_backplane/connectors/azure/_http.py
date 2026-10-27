# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every Azure Resource Manager call goes through :func:`send`.

:func:`send` owns the three things every Azure call needs:

* **One retry budget per call.** At most :data:`MAX_ATTEMPTS` tries and at
  most :data:`MAX_WAIT_SECONDS` of waiting between them. This is the only
  retry loop of an Azure call: the Azure connector does not use the retry of
  the shared HTTP base, and it handles a 401 itself (one new login, one more
  try, counted in the same budget). So the dispatcher's own retry after a 401
  never runs on top, and no layer multiplies the tries of another.
* **Waiting when Azure throttles.** A 429 answer waits ``Retry-After`` (or a
  short back-off when it is missing) and tries again if the budget allows;
  otherwise the call fails with :class:`AzureThrottledError`, which says when
  to try again. A write (POST, PUT, PATCH, DELETE) is repeated only after a
  429 or a 401, because then Azure did not run it. It is never repeated after
  a timeout, a lost connection or a 5xx, because Azure may have run it.
* **A limit per Azure login.** At most :data:`MAX_CALLS_IN_FLIGHT_PER_LOGIN`
  calls of one login (Azure tenant ID and client ID) run at the same time,
  and Resource Graph queries are paced to
  :data:`RESOURCE_GRAPH_CALLS_PER_WINDOW` per
  :data:`RESOURCE_GRAPH_WINDOW_SECONDS`. The limit holds per MEHO process:
  two MEHO processes each have their own.

Before a token goes to a URL that came from Azure (a next page or a poll
URL), :func:`check_azure_url` checks it. Any other URL is refused, and no
request is sent. The Azure client follows no redirects at all
(:class:`NoRedirectClient`): a 3xx answer fails the call, so a redirect on
the same host can never carry the token to another subscription.

``docs/codebase/connectors-azure.md`` describes the numbers and why they were
chosen.
"""

from __future__ import annotations

import asyncio
import math
import time
import weakref
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.adapters.http import (
    SsrfBlockedError,
    _read_capped_json_response,
)
from meho_backplane.connectors.adapters.http import (
    _SameOriginRedirectClient as _SharedClient,
)
from meho_backplane.connectors.azure._errors import AzureUrlRefusedError, error_for_response
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config
from meho_backplane.flight_recorder import capture as flight_recorder_capture

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "MAX_ATTEMPTS",
    "MAX_AZURE_URL_LENGTH",
    "MAX_CALLS_IN_FLIGHT_PER_LOGIN",
    "MAX_WAIT_SECONDS",
    "RESOURCE_GRAPH_CALLS_PER_WINDOW",
    "RESOURCE_GRAPH_WINDOW_SECONDS",
    "NoRedirectClient",
    "check_azure_url",
    "retry_after_seconds",
    "send",
]

#: Most tries of one call, the first one included.
MAX_ATTEMPTS: Final[int] = 4

#: Most seconds one call may wait between its tries, all waits together.
MAX_WAIT_SECONDS: Final[float] = 60.0

#: Waits before the 2nd, 3rd and 4th try after a 5xx or a lost connection.
#: The same as the shared HTTP base.
BACKOFF_SECONDS: Final[tuple[float, ...]] = (0.5, 1.0, 2.0)

#: Waits before the 2nd, 3rd and 4th try after a 429 without ``Retry-After``.
THROTTLE_BACKOFF_SECONDS: Final[tuple[float, ...]] = (2.0, 4.0, 8.0)

#: Most calls of one Azure login that run at the same time, per MEHO process.
MAX_CALLS_IN_FLIGHT_PER_LOGIN: Final[int] = 8

#: Resource Graph allows about 15 queries per 5 seconds per login.
RESOURCE_GRAPH_CALLS_PER_WINDOW: Final[int] = 15
RESOURCE_GRAPH_WINDOW_SECONDS: Final[float] = 5.0

#: Longest URL from Azure that MEHO follows. Azure promises poll URLs of up to 4 KB.
MAX_AZURE_URL_LENGTH: Final[int] = 4096

_REPEATABLE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

#: Transport errors after which a read is tried again (the shared HTTP base's set).
_RETRYABLE_TRANSPORT_ERRORS: Final[tuple[type[httpx.TransportError], ...]] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
)

#: Clock and sleep. Tests replace them to move time forward without waiting.
_monotonic = time.monotonic
_sleep = asyncio.sleep

Login = tuple[str, str]


class NoRedirectClient(_SharedClient):
    """The shared pooled client, but it follows no redirect at all.

    The shared client follows a redirect on the same origin. On Azure the
    origin is one host for every subscription, so such a redirect could carry
    the token to another subscription. Azure Resource Manager never redirects
    a call, so the answer is returned as it is and :func:`send` fails on it.
    """

    async def send(
        self,
        request: httpx.Request,
        *,
        stream: bool = False,
        auth: Any = httpx.USE_CLIENT_DEFAULT,
        follow_redirects: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        return await httpx.AsyncClient.send(
            self, request, stream=stream, auth=auth, follow_redirects=False
        )


# ----------------------------------------------------------------------
# Limit per Azure login
# ----------------------------------------------------------------------


class _LoginLimiter:
    """The calls in flight and the Resource Graph pace of one Azure login."""

    def __init__(self) -> None:
        self.in_flight = asyncio.Semaphore(MAX_CALLS_IN_FLIGHT_PER_LOGIN)
        self._graph_calls: deque[float] = deque()
        self._graph_lock = asyncio.Lock()

    async def pace_resource_graph(self) -> None:
        """Wait until one more Resource Graph query fits in the window."""
        async with self._graph_lock:
            while True:
                now = _monotonic()
                while self._graph_calls and now - self._graph_calls[0] >= (
                    RESOURCE_GRAPH_WINDOW_SECONDS
                ):
                    self._graph_calls.popleft()
                if len(self._graph_calls) < RESOURCE_GRAPH_CALLS_PER_WINDOW:
                    self._graph_calls.append(now)
                    return
                await _sleep(self._graph_calls[0] + RESOURCE_GRAPH_WINDOW_SECONDS - now)


#: One set of limiters per event loop, because asyncio locks belong to one loop.
_limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Login, _LoginLimiter]] = (
    weakref.WeakKeyDictionary()
)


def _limiter_for(login: Login) -> _LoginLimiter:
    per_loop = _limiters.setdefault(asyncio.get_running_loop(), {})
    limiter = per_loop.get(login)
    if limiter is None:
        limiter = per_loop[login] = _LoginLimiter()
    return limiter


# ----------------------------------------------------------------------
# URL check
# ----------------------------------------------------------------------


def check_azure_url(
    target: AzureTargetLike, url: str, *, extra_hosts: Iterable[str] = ()
) -> httpx.URL:
    """Check a URL that came from Azure before MEHO sends a token to it.

    The URL must use https on port 443, have no user name or password, and
    be at most :data:`MAX_AZURE_URL_LENGTH` long. Its host must be the
    target's Resource Manager host, and then the path must start with the
    target's own subscription (``/subscriptions/<id>``); or the host must be
    one of *extra_hosts* (a later read adds its own, for example a storage
    data host). Raises :class:`AzureUrlRefusedError` otherwise. The error
    never repeats the URL.
    """
    config = resolve_target_config(target)
    name = target.name
    if len(url) > MAX_AZURE_URL_LENGTH:
        raise AzureUrlRefusedError(
            f"Azure target {name!r}: a URL from Azure is longer than "
            f"{MAX_AZURE_URL_LENGTH} characters. MEHO did not follow it."
        )
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError, TypeError) as exc:
        raise AzureUrlRefusedError(
            f"Azure target {name!r}: a URL from Azure is not a valid URL. MEHO did not follow it."
        ) from exc
    if parsed.scheme != "https" or parsed.userinfo or parsed.port not in (None, 443):
        raise AzureUrlRefusedError(
            f"Azure target {name!r}: a URL from Azure does not use plain https on port "
            "443. MEHO sent no token to it."
        )
    host = parsed.host.lower()
    if host == config.cloud.management_host:
        segments = parsed.path.split("/")
        inside = (
            len(segments) >= 3
            and segments[1].lower() == "subscriptions"
            and segments[2].lower() == config.subscription_id
            and not any(part in (".", "..") for part in segments)
        )
        if not inside:
            raise AzureUrlRefusedError(
                f"Azure target {name!r}: a URL from Azure points outside the target's "
                "subscription. MEHO sent no token to it."
            )
        return parsed
    if host not in {extra.lower() for extra in extra_hosts}:
        raise AzureUrlRefusedError(
            f"Azure target {name!r}: a URL from Azure points to another host than the "
            f"target's ({config.cloud.management_host}). MEHO sent no token to it."
        )
    return parsed


# ----------------------------------------------------------------------
# Retry-After
# ----------------------------------------------------------------------


def _seconds_from_clock_text(raw: str) -> float | None:
    """Read ``hh:mm:ss`` (Resource Graph's ``x-ms-user-quota-resets-after``)."""
    parts = raw.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (float(part) for part in parts)
    except ValueError:
        return None
    total = hours * 3600 + minutes * 60 + seconds
    return total if math.isfinite(total) and total >= 0 else None


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Return how many seconds Azure asks MEHO to wait, or ``None``.

    Reads ``Retry-After`` as seconds or as an HTTP date. When it is missing,
    reads Resource Graph's ``x-ms-user-quota-resets-after``.
    """
    raw = response.headers.get("retry-after")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            try:
                when = parsedate_to_datetime(raw)
            except (TypeError, ValueError, IndexError):
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(0.0, (when - datetime.now(UTC)).total_seconds())
        return max(0.0, value) if math.isfinite(value) else None
    resets = response.headers.get("x-ms-user-quota-resets-after")
    return _seconds_from_clock_text(resets) if resets else None


# ----------------------------------------------------------------------
# The call
# ----------------------------------------------------------------------


class _Budget:
    """The tries and the waiting one call has used."""

    def __init__(self) -> None:
        self.attempts = 0
        self.waited = 0.0

    def allows(self, delay: float) -> bool:
        return self.attempts < MAX_ATTEMPTS and self.waited + delay <= MAX_WAIT_SECONDS

    def backoff(self, steps: tuple[float, ...]) -> float:
        return steps[min(self.attempts, len(steps)) - 1]


async def send(
    connector: AzureConnector,
    target: AzureTargetLike,
    method: str,
    url: str,
    *,
    operator: Operator,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    repeatable: bool | None = None,
    resource_graph: bool = False,
    extra_hosts: Iterable[str] = (),
) -> httpx.Response:
    """Send one Azure call inside one retry budget and return the answer.

    *url* is either a path on the target's Resource Manager host (one of the
    hand-written paths, it starts with ``/``) or a URL that came from Azure,
    which is checked with :func:`check_azure_url` first. A caller that
    follows a link from Azure checks it itself too, because a link can look
    like a path. *repeatable* says whether the call
    may be sent again after a 5xx or a lost connection; it defaults to
    ``True`` for GET, HEAD and OPTIONS only. A read-only POST (a Resource
    Graph query) passes ``True``. *resource_graph* paces the call to
    Resource Graph's limit.

    Returns the answer for any status below 300. Raises
    :class:`AzureUrlRefusedError` for a redirect (MEHO follows none), an
    error from :mod:`._errors` for an error answer, ``ConnectorAuthError``
    when Azure still answers 401 after a new login, or the ``httpx`` error
    of a lost connection or a timeout.
    """
    method = method.upper()
    may_repeat = method in _REPEATABLE_METHODS if repeatable is None else repeatable
    if not url.startswith("/"):
        check_azure_url(target, url, extra_hosts=extra_hosts)
    budget = _Budget()
    logged_in_again = False
    while True:
        budget.attempts += 1
        try:
            response = await _send_once(
                connector, target, method, url, operator, params, json, resource_graph
            )
        except httpx.TransportError as exc:
            delay = _transport_delay(exc, may_repeat, budget)
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
        delay = _status_delay(status, wait_hint, may_repeat, budget)
        if delay is None:
            raise error_for_response(
                response, target_name=target.name, retry_after_seconds=wait_hint
            )
        await _wait(budget, delay)


async def _wait(budget: _Budget, delay: float) -> None:
    budget.waited += delay
    await _sleep(delay)


def _transport_delay(exc: httpx.TransportError, may_repeat: bool, budget: _Budget) -> float | None:
    """The wait before trying again after a lost connection, or ``None`` to stop."""
    if not may_repeat or isinstance(exc, SsrfBlockedError):
        return None
    if not isinstance(exc, _RETRYABLE_TRANSPORT_ERRORS):
        return None
    delay = budget.backoff(BACKOFF_SECONDS)
    return delay if budget.allows(delay) else None


def _status_delay(
    status: int, wait_hint: float | None, may_repeat: bool, budget: _Budget
) -> float | None:
    """The wait before trying again after an error status, or ``None`` to stop."""
    if status == 429:
        delay = wait_hint if wait_hint is not None else budget.backoff(THROTTLE_BACKOFF_SECONDS)
    elif status >= 500 and may_repeat:
        delay = wait_hint if wait_hint is not None else budget.backoff(BACKOFF_SECONDS)
    else:
        return None
    return delay if budget.allows(delay) else None


def _redirect_refused(target: AzureTargetLike, status: int) -> AzureUrlRefusedError:
    """The error for a 3xx answer. MEHO follows no redirect of an Azure call."""
    return AzureUrlRefusedError(
        f"Azure target {target.name!r}: Azure answered {status} with a redirect. MEHO "
        "does not follow redirects of Azure calls, so no token went to the new address. "
        "Check the target's host and run azure.about."
    )


def _still_unauthorized(target: AzureTargetLike, logged_in_again: bool) -> ConnectorAuthError:
    """The error when Azure keeps answering 401."""
    remediation = (
        "Azure did not accept a new token of this login. Check that the login's "
        "service principal still exists in the target's tenant and that the "
        "subscription belongs to that tenant, then run azure.about."
    )
    return ConnectorAuthError(
        f"Azure target {target.name!r}: Azure answered 401 again after a new login. {remediation}",
        status_code=401,
        cause="session_dispatch_401_after_relogin" if logged_in_again else "dispatch_401",
        target_name=target.name,
        host=target.host,
        secret_ref=target.secret_ref,
        remediation=remediation,
        stage="after_relogin" if logged_in_again else "call",
    )


async def _send_once(
    connector: AzureConnector,
    target: AzureTargetLike,
    method: str,
    url: str,
    operator: Operator,
    params: dict[str, Any] | None,
    json: dict[str, Any] | None,
    resource_graph: bool,
) -> httpx.Response:
    """Send the request once, inside the login's limit, and record it in the trace."""
    headers, login = await connector.authorize(target, operator)
    limiter = _limiter_for(login)
    client = await connector._http_client(target)
    async with limiter.in_flight:
        if resource_graph:
            await limiter.pace_resource_graph()
        span = flight_recorder_capture.span_start()
        try:
            response = await _read_capped_json_response(
                client,
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                extensions=connector._request_extensions(target),
            )
        except httpx.TransportError as exc:
            flight_recorder_capture.record_vendor_call_error(span, method=method, exc=exc)
            raise
    flight_recorder_capture.record_vendor_call(
        span,
        method=method,
        request_headers=headers,
        response=response,
        request_body=json,
        request_content_type="application/json" if json is not None else None,
        response_body=connector._trace_response_body(span, response),
    )
    return response
