# Error classification in MEHO

## Overview

MEHO uses a two-layer error taxonomy: a base hierarchy for human-readable messages (`MehoError`), and a mixin for machine-readable metadata (`ClassifiedError`). Every unhandled exception that escapes a route boundary is caught by a single app-level handler that produces a structured JSON 500 response.

## Key types

| Class | Inherits | `source` | Purpose |
|-------|----------|----------|---------|
| `MehoError` | `Exception` | — | Base for all MEHO-specific errors. Carries `code`, `message`, `details`. |
| `ClassifiedError` | mixin | varies | Adds `source`, `error_type`, `severity`, `remediation`, and an OTEL `trace_id`. |
| `InternalError` | `MehoError`, `ClassifiedError` | `"internal"` | Unexpected failures inside route handlers. |
| `ConnectorError` | `MehoError`, `ClassifiedError` | `"connector"` | Failures in connector adapters (timeout, auth, unreachable). |
| `LLMError` | `MehoError`, `ClassifiedError` | `"llm"` | LLM provider failures (rate limit, auth, context overflow). |

All classes live in `meho_app/core/errors.py`.

## App-level handler

`meho_app/api/errors.py` registers `general_exception_handler` on `Exception`. The response shape depends on whether the exception is a `ClassifiedError`.

**Unclassified exception** (plain `Exception`, `RuntimeError`, etc.):

```json
{
  "error": {
    "message": "An unexpected error occurred. Please try again or contact support.",
    "type": "InternalServerError",
    "status_code": 500,
    "trace_id": "<otel-trace-id>"
  }
}
```

**`ClassifiedError` (e.g. `InternalError`)** — additional fields from `classification_dict()` are merged in:

```json
{
  "error": {
    "message": "Human-readable description of what failed",
    "type": "InternalServerError",
    "status_code": 500,
    "trace_id": "<otel-trace-id>",
    "error_source": "internal",
    "error_type": "unknown",
    "severity": "permanent",
    "transient": false
  }
}
```

The `error_source`, `error_type`, `severity`, `transient`, `connector_name`, and `remediation` fields are only present when the exception is a `ClassifiedError`.

## Route boundary pattern

Route handlers should catch only the narrow set of concrete exceptions that the handler's code can actually throw, then re-raise as `InternalError` so the app-level handler produces the structured response:

```python
from meho_app.core.errors import InternalError
from sqlalchemy.exc import SQLAlchemyError

try:
    # ... handler body ...
except HTTPException:
    raise  # 4xx — client errors pass through unchanged
except (SQLAlchemyError, RuntimeError, OSError, ValueError) as e:
    logger.error(f"...: {e}", exc_info=True)
    raise InternalError(message="Human-readable description of what failed") from e
```

**Why `InternalError` instead of `HTTPException(500)`:**
- Produces a consistent `{"error": {...}}` envelope rather than `{"detail": "..."}`.
- Automatically attaches the current OTEL trace ID so errors can be correlated in Jaeger/Tempo.
- Carries `severity` and `transient` flags that upstream clients (frontend, orchestrator) can use to decide whether to retry.

## Background task pattern

Background tasks (document ingestion, URL fetch, deletion) span multiple heterogeneous subsystems. Their outer catch must stay broad and is annotated with `# noqa: BLE001`:

```python
except Exception as e:  # noqa: BLE001 -- pipeline spans X, Y, Z
    logger.error(f"Background task failed: {e}", exc_info=True)
```

Inner checkpoints within the same background function narrow the catch type as normal.

## Audit side-effect pattern

Audit logging is always a side-effect that must not fail the primary operation. These catches are kept broad and annotated:

```python
except Exception as audit_err:  # noqa: BLE001 -- audit side-effect must not fail the main response
    logger.warning(f"Audit logging failed: {audit_err}")
```

## Dependencies

- `opentelemetry-sdk` — `get_current_trace_id()` reads the active span context.
- `fastapi.exception_handlers` — the app-level handler is wired in `create_app()` in `meho_app/main.py`.

## References

- `meho_app/core/errors.py` — taxonomy
- `meho_app/api/errors.py` — app-level handler
- `meho_app/main.py` — handler registration (`create_app`)
- Issue #286 — adoption of `ClassifiedError` across API and bootstrap hotspots
