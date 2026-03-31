"""
Unified error handling for MEHO API.

Provides consistent error responses and exception handlers.
"""
from fastapi import Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import logging

logger = logging.getLogger(__name__)


class MEHOAPIError(Exception):
    """Base exception for MEHO API errors"""
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ServiceUnavailableError(MEHOAPIError):
    """Backend service unavailable"""
    def __init__(self, service: str):
        super().__init__(
            f"Service {service} is currently unavailable. Please try again later.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        )


class UnauthorizedError(MEHOAPIError):
    """Authentication failed"""
    def __init__(self, message: str = "Authentication required"):
        super().__init__(message, status_code=status.HTTP_401_UNAUTHORIZED)


async def meho_api_error_handler(request: Request, exc: MEHOAPIError) -> JSONResponse:
    """Handle MEHO API errors"""
    logger.error(f"MEHO API Error: {exc.message}", exc_info=True)
    
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "type": type(exc).__name__,
                "status_code": exc.status_code
            }
        }
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Handle validation errors"""
    errors = exc.errors()
    logger.warning(f"Validation error: {errors}")
    
    # Convert error details to ensure JSON serializability
    # (bytes objects in error input need to be decoded)
    serializable_errors = []
    for error in errors:
        serializable_error = dict(error)
        if 'input' in serializable_error and isinstance(serializable_error['input'], bytes):
            serializable_error['input'] = serializable_error['input'].decode('utf-8', errors='replace')
        
        # Convert ValueError objects in ctx to strings
        if 'ctx' in serializable_error and 'error' in serializable_error['ctx']:
            ctx_error = serializable_error['ctx']['error']
            if isinstance(ctx_error, (ValueError, Exception)):
                serializable_error['ctx']['error'] = str(ctx_error)
        
        serializable_errors.append(serializable_error)
    
    # Use FastAPI's standard format
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": serializable_errors}
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Handle HTTP exceptions"""
    logger.error(f"HTTP {exc.status_code}: {exc.detail}")
    
    # Use FastAPI's standard format for consistency
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all exception handler"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "message": "An unexpected error occurred. Please try again or contact support.",
                "type": "InternalServerError",
                "status_code": 500
            }
        }
    )

