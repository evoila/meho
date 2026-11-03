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


class RequestBodyLimitMiddleware:
    """Refuse HTTP request bodies above the configured byte cap with ``413``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = get_settings().http_max_request_body_bytes
        declared = _declared_length(scope)
        if declared is not None and declared > max_bytes:
            _log.warning(
                "request_body_too_large",
                path=scope.get("path"),
                declared_bytes=declared,
                max_bytes=max_bytes,
            )
            await self._send_too_large(send, max_bytes)
            return

        response_started = False
        total = 0

        async def receive_capped() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > max_bytes:
                    raise RequestBodyTooLargeError(max_bytes)
            return message

        async def send_tracking(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_capped, send_tracking)
        except RequestBodyTooLargeError:
            _log.warning(
                "request_body_too_large",
                path=scope.get("path"),
                read_bytes=total,
                max_bytes=max_bytes,
            )
            if response_started:
                raise
            await self._send_too_large(send, max_bytes)

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
