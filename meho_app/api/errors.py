# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unified error handling for MEHO API.

Provides consistent error responses and exception handlers.
Error responses include classification metadata (source/type/severity)
when the exception carries it (Phase 23: error classification).
"""

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from meho_app.core.errors import ClassifiedError, get_current_trace_id
from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class MEHOAPIError(Exception):
    """Base exception for MEHO API errors"""

    def __init__(self, message: str, status_code: int = 500) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ServiceUnavailableError(MEHOAPIError):
    """Backend service unavailable"""

    def __init__(self, service: str) -> None:
        super().__init__(
            f"Service {service} is currently unavailable. Please try again later.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class UnauthorizedError(MEHOAPIError):
    """Authentication failed"""

    def __init__(self, message: str = "Authentication required") -> None:
        super().__init__(message, status_code=status.HTTP_401_UNAUTHORIZED)


async def meho_api_error_handler(_request: Request, exc: MEHOAPIError) -> JSONResponse:
    """Handle MEHO API errors with classification metadata."""
    logger.error(f"MEHO API Error: {exc.message}", exc_info=True)

    response = {
        "error": {
            "message": exc.message,
            "type": type(exc).__name__,
            "status_code": exc.status_code,
        }
    }
    # Add classification if the error carries it
    if isinstance(exc, ClassifiedError):
        response["error"].update(exc.classification_dict())
    else:
        # Provide minimal classification for non-classified MEHOAPIError
        response["error"]["trace_id"] = get_current_trace_id()

    return JSONResponse(status_code=exc.status_code, content=response)


async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Handle validation errors"""
    errors = exc.errors()
    logger.warning(f"Validation error: {errors}")

    # Convert error details to ensure JSON serializability
    # (bytes objects in error input need to be decoded)
    serializable_errors = []
    for error in errors:
        serializable_error = dict(error)
        if "input" in serializable_error and isinstance(serializable_error["input"], bytes):
            serializable_error["input"] = serializable_error["input"].decode(
                "utf-8", errors="replace"
            )

        # Convert ValueError objects in ctx to strings
        if "ctx" in serializable_error and "error" in serializable_error["ctx"]:
            ctx_error = serializable_error["ctx"]["error"]
            if isinstance(ctx_error, (ValueError, Exception)):
                serializable_error["ctx"]["error"] = str(ctx_error)

        serializable_errors.append(serializable_error)

    # Use FastAPI's standard format
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": serializable_errors},
    )


async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Handle HTTP exceptions"""
    logger.error(f"HTTP {exc.status_code}: {exc.detail}")

    # Use FastAPI's standard format for consistency
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


async def general_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Catch-all exception handler with trace ID."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)

    error_response: dict = {
        "message": "An unexpected error occurred. Please try again or contact support.",
        "type": "InternalServerError",
        "status_code": 500,
        "trace_id": get_current_trace_id(),
    }

    # If a classified MehoError escaped to here, include its classification
    if isinstance(exc, ClassifiedError):
        error_response.update(exc.classification_dict())
        error_response["message"] = exc.message if hasattr(exc, "message") else str(exc)

    return JSONResponse(status_code=500, content={"error": error_response})
