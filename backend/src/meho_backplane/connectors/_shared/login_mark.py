# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The mark that names a request as a login.

The pooled client of a connector picks its redirect rule by the kind of
request (``adapters/http.py``). A read and a login follow a redirect on the
same origin. Every other request gets the change rule. The code names a
login in one of two ways:

* it sends the login inside :func:`login_request`, or
* it sends the login while the core asks the connector for its auth headers
  (``HttpConnector.auth_headers``).

No rule guesses a login from its path or its address.

This module imports nothing of MEHO, so every login helper can import it.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from typing import Any

__all__ = ["in_login_request", "is_login", "login_request", "mark_as_login"]

#: The request extension that the pooled client sets on each login it sends.
_LOGIN_EXTENSION = "meho_login"

_login_request: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "meho_login_request", default=False
)


@contextlib.contextmanager
def login_request() -> Iterator[None]:
    """Name the requests sent inside as a login."""
    token = _login_request.set(True)
    try:
        yield
    finally:
        _login_request.reset(token)


def in_login_request() -> bool:
    """Whether the request sent now is named as a login."""
    return _login_request.get()


def mark_as_login(request: Any) -> None:
    """Mark *request* (an ``httpx.Request``) as a login, for the error handling later."""
    request.extensions[_LOGIN_EXTENSION] = True


def is_login(request: Any) -> bool:
    """Whether the pooled client sent *request* (an ``httpx.Request``) as a login."""
    return request.extensions.get(_LOGIN_EXTENSION) is True
