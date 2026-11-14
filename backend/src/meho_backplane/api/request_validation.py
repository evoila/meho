# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A 422 answer for a bad request body that can always be sent.

FastAPI's own handler puts the refused input into the 422 body. A JSON
body may carry a lone surrogate (``"\\ud800"``), which is valid JSON but
cannot be encoded as UTF-8. The stock answer then fails to encode and the
caller gets a 500. This handler builds the same body, with each text that
cannot be encoded written as backslash escapes. An error on a ticket id
field (``work_ref``) leaves out the refused input: it names the rule and the
code point only, like the header and MCP answers. Every other answer is byte
for byte the same as FastAPI's.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

from meho_backplane.work_ref import WorkRefError

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


def _is_work_ref_error(error: dict[str, Any]) -> bool:
    ctx = error.get("ctx") or {}
    if isinstance(ctx.get("error"), WorkRefError):
        return True
    loc = list(error.get("loc") or [])
    return bool(loc) and "work_ref" in str(loc[-1]).replace("-", "_")


def _without_refused_work_ref(errors: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in error.items() if key != "input"}
        if _is_work_ref_error(error)
        else error
        for error in errors
    ]


async def request_validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a :class:`RequestValidationError` with a 422 that always encodes."""
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registration contract
        raise exc
    return JSONResponse(
        status_code=422,
        content={"detail": _encodable(jsonable_encoder(_without_refused_work_ref(exc.errors())))},
    )
