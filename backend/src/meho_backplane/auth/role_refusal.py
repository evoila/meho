# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A 403 role refusal with a plain sentence next to its code.

The REST and console doors refuse a caller with the Reader role who asks
for a change. The body keeps the short code in ``detail``, so clients that
read the code keep working, and adds the sentence in ``message``::

    {"detail": "insufficient_role", "message": "You have the Reader role. ..."}

The code and the sentence come from
:func:`~meho_backplane.operations.role_floors.reader_change_refusal`.
:func:`role_refused_handler` is registered on the app for
:class:`RoleRefusedError`; Starlette picks the handler of the closest class
of the exception, so every other ``HTTPException`` keeps its own handler.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

__all__ = ["RoleRefusedError", "role_refused_handler"]


class RoleRefusedError(HTTPException):
    """A 403 whose body has a plain *message* next to the *detail* code."""

    def __init__(self, detail: str, message: str) -> None:
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
        self.message = message


async def role_refused_handler(request: Request, exc: Exception) -> Response:
    """Answer a :class:`RoleRefusedError` with ``detail`` and ``message``."""
    del request
    if not isinstance(exc, RoleRefusedError):  # pragma: no cover - registration contract
        raise exc
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "message": exc.message},
        headers=exc.headers,
    )
