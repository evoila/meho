# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Error hierarchy for MEHO.

All MEHO-specific exceptions inherit from MehoError.
Classified errors add source/type/severity metadata for structured handling.
"""

from typing import Any

from opentelemetry import trace


def get_current_trace_id() -> str | None:
    """Get the current OTEL trace ID as a hex string."""
    try:
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, "032x")
    except Exception:  # noqa: S110 -- intentional silent exception handling
        pass
    return None


class ClassifiedError:
    """Mixin that adds error classification metadata.

    Three-tier classification:
      Source (WHERE): llm, connector, internal
      Type (WHAT): timeout, auth, rate_limit, connection, context_overflow, data_error, unknown
      Severity (HOW BAD): transient (retry-worthy), permanent (don't retry)
    """

    source: str = "internal"
    error_type: str = "unknown"
    severity: str = "permanent"
    connector_name: str | None = None
    remediation: str | None = None

    @property
    def trace_id(self) -> str | None:
        return get_current_trace_id()

    @property
    def is_transient(self) -> bool:
        return self.severity == "transient"

    def classification_dict(self) -> dict[str, Any]:
        """Return classification metadata as a dict for serialization."""
        return {
            "error_source": self.source,
            "error_type": self.error_type,
            "severity": self.severity,
            "transient": self.is_transient,
            "connector_name": self.connector_name,
            "remediation": self.remediation,
            "trace_id": self.trace_id,
        }


class MehoError(Exception):
    """Base exception for all MEHO errors"""

    code: str = "MEHO_ERROR"
    message: str = "An error occurred"

    def __init__(self, message: str | None = None, **details: Any):
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Serialize error for API responses"""
        result = {"code": self.code, "message": self.message, "details": self.details}
        if isinstance(self, ClassifiedError):
            result.update(self.classification_dict())
        return result

    def __repr__(self) -> str:
        details_str = f", details={self.details}" if self.details else ""
        return f"{self.__class__.__name__}('{self.message}'{details_str})"


class ConfigError(MehoError):
    """Configuration error"""

    code = "CONFIG_ERROR"
    message = "Configuration error"


class AuthError(MehoError):
    """Authentication or authorization error"""

    code = "AUTH_ERROR"
    message = "Authentication failed"


class NotFoundError(MehoError):
    """Resource not found"""

    code = "NOT_FOUND"
    message = "Resource not found"


class ValidationError(MehoError):
    """Data validation error"""

    code = "VALIDATION_ERROR"
    message = "Validation failed"


class UpstreamApiError(MehoError):
    """Error calling upstream API"""

    code = "UPSTREAM_API_ERROR"
    message = "Upstream API call failed"

    def __init__(
        self,
        status_code: int,
        url: str,
        payload: Any = None,
        message: str | None = None,
        **details: Any,
    ):
        self.status_code = status_code
        self.url = url
        self.payload = payload
        super().__init__(
            message or f"API call to {url} failed with status {status_code}",
            status_code=status_code,
            url=url,
            payload=payload,
            **details,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize with additional fields"""
        base = super().to_dict()
        base.update({"status_code": self.status_code, "url": self.url})
        return base


class VectorStoreError(MehoError):
    """Vector store operation error"""

    code = "VECTOR_STORE_ERROR"
    message = "Vector store operation failed"


class WorkflowError(MehoError):
    """Workflow execution error"""

    code = "WORKFLOW_ERROR"
    message = "Workflow execution failed"


class IngestionError(MehoError):
    """Knowledge ingestion error"""

    code = "INGESTION_ERROR"
    message = "Knowledge ingestion failed"


class CredentialError(MehoError):
    """User credential error"""

    code = "CREDENTIAL_ERROR"
    message = "Credential operation failed"


# --- Classified error subclasses (Phase 23: error classification) ---


class LLMError(MehoError, ClassifiedError):
    """LLM-related errors (rate limit, auth, context overflow, connection)."""

    code = "LLM_ERROR"
    source = "llm"

    def __init__(
        self,
        error_type: str,
        severity: str,
        message: str,
        remediation: str | None = None,
        **details: Any,
    ):
        self.error_type = error_type
        self.severity = severity
        self.remediation = remediation
        super().__init__(message=message, **details)


class ConnectorError(MehoError, ClassifiedError):
    """Connector-related errors (timeout, auth, unreachable, data)."""

    code = "CONNECTOR_ERROR"
    source = "connector"

    def __init__(
        self,
        error_type: str,
        severity: str,
        message: str,
        connector_name: str | None = None,
        remediation: str | None = None,
        **details: Any,
    ):
        self.error_type = error_type
        self.severity = severity
        self.connector_name = connector_name
        self.remediation = remediation
        super().__init__(message=message, **details)


class InternalError(MehoError, ClassifiedError):
    """Internal MEHO errors (bugs, config issues, unexpected failures)."""

    code = "INTERNAL_ERROR"
    source = "internal"

    def __init__(
        self,
        error_type: str = "unknown",
        severity: str = "permanent",
        message: str = "An internal error occurred",
        remediation: str | None = None,
        **details: Any,
    ):
        self.error_type = error_type
        self.severity = severity
        self.remediation = remediation
        super().__init__(message=message, **details)
