# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The name of the console session cookie, and the one way to read it.

The browser holds only an opaque session id in this cookie. The real
tokens stay in the ``web_session`` table.

* The name has the ``__Host-`` prefix. A browser keeps such a cookie only
  when it is set with ``Secure``, with ``Path=/`` and with no ``Domain``
  attribute. So only the MEHO host itself can set it, and no other host
  under the same parent domain.
* :func:`read_session_cookie` is the one reader. The session check, the
  CSRF check and logout all use it. A request that carries the cookie
  more than once counts as a request with no session.
"""

from __future__ import annotations

from typing import Final

from starlette.types import Scope

__all__ = ["SESSION_COOKIE_NAME", "read_session_cookie"]

#: Name of the console session cookie.
SESSION_COOKIE_NAME: Final[str] = "__Host-meho_session"

_NAME_BYTES: Final[bytes] = SESSION_COOKIE_NAME.encode("ascii")


def read_session_cookie(scope: Scope) -> str | None:
    """Return the session cookie value when the request carries it exactly once.

    Reads every ``Cookie`` header of the raw ASGI scope. Returns ``None``
    when the cookie is missing, when it appears more than once (in one
    header or across several), or when its value is not ASCII.
    """
    headers = scope.get("headers")
    if not isinstance(headers, list):
        return None
    values: list[bytes] = []
    for name, value in headers:
        if not isinstance(name, (bytes, bytearray)) or name != b"cookie":
            continue
        if not isinstance(value, (bytes, bytearray)):
            continue
        for chunk in bytes(value).split(b";"):
            cookie_name, sep, cookie_value = chunk.strip().partition(b"=")
            if sep and cookie_name.strip() == _NAME_BYTES:
                values.append(cookie_value)
    if len(values) != 1:
        return None
    try:
        return values[0].decode("ascii")
    except UnicodeDecodeError:
        return None
