# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Request context variables for OTEL enrichment."""

from __future__ import annotations

from contextvars import ContextVar

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id", default=None)
tenant_id_ctx: ContextVar[str | None] = ContextVar("tenant_id", default=None)


def set_request_context(
    *,
    request_id: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """Set context for the current request.

    Called by middleware to propagate user/tenant context through async code.
    The context is automatically added to all OTEL logs via the enricher.
    """
    if request_id:
        request_id_ctx.set(request_id)
    if user_id:
        user_id_ctx.set(user_id)
    if tenant_id:
        tenant_id_ctx.set(tenant_id)


def clear_request_context() -> None:
    """Clear request context (called at end of request)."""
    request_id_ctx.set(None)
    user_id_ctx.set(None)
    tenant_id_ctx.set(None)


def get_request_context() -> dict[str, str | None]:
    """Get current request context."""
    return {
        "request_id": request_id_ctx.get(),
        "user_id": user_id_ctx.get(),
        "tenant_id": tenant_id_ctx.get(),
    }
