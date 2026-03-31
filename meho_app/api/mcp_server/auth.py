# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ASGI auth middleware for MCP server requests.

Validates Keycloak JWT tokens on all MCP HTTP requests.
FastAPI's Depends(get_current_user) does NOT apply to mounted sub-apps,
so auth must be enforced at the ASGI middleware level.
"""

import logging
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from meho_app.core.auth_context import UserContext

logger = logging.getLogger(__name__)


class MCPAuthMiddleware:
    """ASGI middleware that validates JWT on MCP requests.

    Wraps the MCP ASGI app and intercepts HTTP requests to validate
    the Authorization: Bearer header. On success, injects a UserContext
    into scope["state"]["user_context"] for tool handlers.

    API key support is not yet implemented (no API key model exists).
    JWT-only for Phase 93. API key attempts are logged with a warning.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode()

            user_context: UserContext | None = None

            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
                # Try JWT validation (Keycloak)
                user_context = await self._validate_jwt(token)
                if not user_context:
                    # API key infrastructure does not exist yet -- log and reject
                    logger.warning(
                        "MCP auth: token failed JWT validation. "
                        "API key auth is not yet implemented."
                    )

            if not user_context:
                response = JSONResponse(
                    status_code=401,
                    content={
                        "error": "Authentication required. Provide a valid Bearer JWT token."
                    },
                )
                await response(scope, receive, send)
                return

            # Inject user context into ASGI scope for tool handlers
            scope.setdefault("state", {})
            scope["state"]["user_context"] = user_context

        await self.app(scope, receive, send)

    async def _validate_jwt(self, token: str) -> UserContext | None:
        """Validate a Keycloak JWT token and return UserContext.

        Reuses the existing Keycloak JWKS validation infrastructure.

        Returns:
            UserContext on success, None on any failure.
        """
        try:
            from meho_app.api.auth import get_keycloak_validator

            validator = get_keycloak_validator()
            token_data = await validator.validate_token(token)

            return UserContext(
                user_id=token_data.user_id,
                name=token_data.name,
                tenant_id=token_data.tenant_id,
                roles=token_data.roles,
                groups=token_data.groups,
            )
        except Exception:
            # Any validation failure -> return None (let caller handle 401)
            logger.debug("MCP JWT validation failed", exc_info=True)
            return None
