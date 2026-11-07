# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure-ASGI middleware that caps the size of every HTTP request body.

MEHO reads a request body before it checks the login on most REST
routes, so one oversized body would be buffered in full before it is
refused. :class:`RequestBodyLimitMiddleware` is the one backstop for
every route: it answers ``413`` as soon as a body is known to exceed
:attr:`~meho_backplane.settings.Settings.http_max_request_body_bytes`.

Two arms, like the ``/mcp`` cap it mirrors:

* A ``Content-Length`` header above the cap is refused before any body
  byte is read.
* A body without a trustworthy length (chunked transfer, or a header
  that understates the size) is counted as the handler reads it. The
  moment the running total crosses the cap the read raises
  :class:`RequestBodyTooLargeError`. It is an ``HTTPException`` with
  status ``413``, so when a route reads the body (FastAPI parsing a
  declared JSON or form body, or a handler calling ``request.body()``)
  the app's own exception handler answers ``413``; FastAPI re-raises an
  ``HTTPException`` from body parsing instead of folding it into its
  ``400``. When the error escapes the app (an inner middleware read the
  body), this middleware sends the ``413`` itself if the response has
  not started yet, and re-raises otherwise.

Routes that already carry a tighter cap (``/mcp`` at 1 MiB, event
ingest at 256 KiB, the kb upload at 512 KiB) keep it: this cap is the
outer bound, not a replacement. The default of 32 MiB sits above the
largest body MEHO accepts on purpose, the 20 MiB inline spec of the
connector ingest route.
"""

from __future__ import annotations

import json
from typing import Final

import structlog
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meho_backplane.settings import get_settings

__all__ = ["RequestBodyLimitMiddleware", "RequestBodyTooLargeError"]

_log = structlog.get_logger()

_STATUS_TOO_LARGE: Final[int] = 413


def _too_large_detail(max_bytes: int) -> str:
    return f"request body exceeds {max_bytes} bytes"


class RequestBodyTooLargeError(HTTPException):
    """Raised from the wrapped ``receive`` when the body crosses the cap.

    An ``HTTPException`` so the ``413`` survives FastAPI's body parsing:
    it re-raises an ``HTTPException`` as is, while any other error from
    the read becomes its generic ``400``.
    """

    def __init__(self, max_bytes: int) -> None:
        super().__init__(status_code=_STATUS_TOO_LARGE, detail=_too_large_detail(max_bytes))
        self.max_bytes = max_bytes


def _too_large_body(max_bytes: int) -> bytes:
    return json.dumps({"detail": _too_large_detail(max_bytes)}).encode("utf-8")


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value.decode("ascii"))
            except ValueError:
                return None
    return None


class _Cap:
    """The byte cap, read from the settings only once a request carries body bytes.

    A body-less request (``GET /``, the probes) never touches the
    settings, so the app answers those before its settings exist.
    """

    def __init__(self) -> None:
        self._value: int | None = None

    @property
    def value(self) -> int:
        if self._value is None:
            self._value = get_settings().http_max_request_body_bytes
        return self._value


class _BodyGuard:
    """Counts the body bytes the app reads and notes when the response starts."""

    def __init__(self, receive: Receive, send: Send, cap: _Cap) -> None:
        self._receive = receive
        self._send = send
        self._cap = cap
        self.total = 0
        self.response_started = False

    async def receive(self) -> Message:
        message = await self._receive()
        if message["type"] == "http.request":
            body = message.get("body", b"")
            if body:
                self.total += len(body)
                if self.total > self._cap.value:
                    raise RequestBodyTooLargeError(self._cap.value)
        return message

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.response_started = True
        await self._send(message)


class RequestBodyLimitMiddleware:
    """Refuse HTTP request bodies above the configured byte cap with ``413``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        cap = _Cap()
        declared = _declared_length(scope)
        if declared is not None and declared > 0 and declared > cap.value:
            _log.warning(
                "request_body_too_large",
                path=scope.get("path"),
                declared_bytes=declared,
                max_bytes=cap.value,
            )
            await self._send_too_large(send, cap.value)
            return

        guard = _BodyGuard(receive, send, cap)
        try:
            await self.app(scope, guard.receive, guard.send)
        except RequestBodyTooLargeError as exc:
            _log.warning(
                "request_body_too_large",
                path=scope.get("path"),
                read_bytes=guard.total,
                max_bytes=exc.max_bytes,
            )
            if guard.response_started:
                raise
            await self._send_too_large(send, exc.max_bytes)

    @staticmethod
    async def _send_too_large(send: Send, max_bytes: int) -> None:
        body = _too_large_body(max_bytes)
        await send(
            {
                "type": "http.response.start",
                "status": _STATUS_TOO_LARGE,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
