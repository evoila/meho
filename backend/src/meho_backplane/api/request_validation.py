# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A 422 answer for a bad request body that can always be sent.

FastAPI's own handler puts the refused input into the 422 body. A JSON
body may carry a lone surrogate (``"\\ud800"``), which is valid JSON but
cannot be encoded as UTF-8. The stock answer then fails to encode and the
caller gets a 500. This handler builds the same body, with each text that
cannot be encoded written as backslash escapes. Every other answer is byte
for byte the same as FastAPI's.
"""

from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

__all__ = ["request_validation_exception_handler"]


def _encodable(value: Any) -> Any:
    """Return ``value`` with every text made safe to encode as UTF-8."""
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, list):
        return [_encodable(item) for item in value]
    if isinstance(value, dict):
        return {_encodable(key): _encodable(item) for key, item in value.items()}
    return value


async def request_validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a :class:`RequestValidationError` with a 422 that always encodes."""
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registration contract
        raise exc
    return JSONResponse(
        status_code=422,
        content={"detail": _encodable(jsonable_encoder(exc.errors()))},
    )
