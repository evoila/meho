# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Slack Connector using slack-sdk.

Implements the BaseConnector interface with a flat structure (no handler mixins)
since Slack has only 6 operations. Uses AsyncWebClient for all API calls.

Credentials model:
- slack_bot_token (required): xoxb-* bot token for all operations except search
- slack_user_token (optional): xoxp-* user token for search_messages
No environment variable fallback -- credentials are explicit per connector instance.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)
from meho_app.modules.connectors.slack.handlers import (
    _handle_add_reaction,
    _handle_get_channel_history,
    _handle_get_user_info,
    _handle_list_channels,
    _handle_post_message,
    _handle_search_messages,
)
from meho_app.modules.connectors.slack.operations import SLACK_OPERATIONS
from meho_app.modules.connectors.slack.types import SLACK_TYPES

if TYPE_CHECKING:
    from slack_sdk.web.async_client import AsyncWebClient

logger = get_logger(__name__)


class SlackConnector(BaseConnector):
    """
    Slack connector for channel queries, message posting, and reactions.

    Provides 6 operations:
    - get_channel_history: Fetch channel message history
    - search_messages: Search messages (requires user token)
    - list_channels: List accessible channels
    - get_user_info: Get user profile information
    - post_message: Post messages and threaded replies
    - add_reaction: Add emoji reactions to messages

    Example:
        connector = SlackConnector(
            connector_id="abc123",
            config={},
            credentials={
                "slack_bot_token": "xoxb-...",
                "slack_user_token": "xoxp-...",  # optional, for search
            }
        )

        async with connector:
            result = await connector.execute("list_channels", {})
            print(result.data)
    """

    def __init__(self, connector_id: str, config: dict[str, Any], credentials: dict[str, Any]):
        super().__init__(connector_id, config, credentials)
        self._client: AsyncWebClient | None = None
        self._user_client: AsyncWebClient | None = None

    # =========================================================================
    # CONNECTION MANAGEMENT
    # =========================================================================

    async def connect(self) -> bool:
        """
        Connect to Slack and verify the bot token via auth.test.

        Requires explicit slack_bot_token in credentials. No fallback to
        environment variables -- this would bypass multi-tenant credential
        isolation and the CredentialResolver chain.

        Optionally initializes a user client if slack_user_token is provided,
        enabling the search_messages operation.

        Returns:
            True if connection successful.

        Raises:
            ValueError: If slack_bot_token is missing.
        """
        from slack_sdk.web.async_client import AsyncWebClient

        bot_token = self.credentials.get("slack_bot_token")
        if not bot_token:
            raise ValueError(
                "slack_bot_token credential required. "
                "MEHO is a multi-user server -- environment credentials are not used "
                "to enforce per-connector RBAC."
            )

        try:
            logger.info(f"Connecting to Slack (connector {self.connector_id})")

            self._client = AsyncWebClient(token=bot_token)
            auth_result = await self._client.auth_test()
            bot_user = auth_result.get("user", "unknown")
            team = auth_result.get("team", "unknown")

            logger.info(f"Connected to Slack as {bot_user} in workspace {team}")

            # Initialize user client for search_messages if token provided
            user_token = self.credentials.get("slack_user_token")
            if user_token:
                self._user_client = AsyncWebClient(token=user_token)
                logger.info("User token configured -- search_messages enabled")
            else:
                logger.info("No user token -- search_messages will return guidance only")

            self._is_connected = True
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Slack: {e}", exc_info=True)
            raise

    async def disconnect(self) -> None:
        """Disconnect from Slack (cleanup clients)."""
        self._client = None
        self._user_client = None
        self._is_connected = False
        logger.info("Disconnected from Slack")

    async def test_connection(self) -> bool:
        """Test if connection is alive by calling auth.test."""
        if not self._client:
            return False
        try:
            await self._client.auth_test()
            return True
        except Exception:
            return False

    # =========================================================================
    # OPERATION EXECUTION
    # =========================================================================

    async def execute(self, operation_id: str, parameters: dict[str, Any]) -> OperationResult:
        """
        Execute a Slack operation.

        Dispatches to the appropriate handler function based on operation_id.
        Handles SlackApiError specifically for structured error reporting.

        Args:
            operation_id: ID of the operation (e.g., "get_channel_history").
            parameters: Operation-specific parameters.

        Returns:
            OperationResult with success status and data/error.
        """
        if not self._is_connected or not self._client:
            return OperationResult(
                success=False,
                error="Not connected to Slack",
                operation_id=operation_id,
            )

        start_time = time.monotonic()

        try:
            result = await self._dispatch_operation(operation_id, parameters)
            duration_ms = (time.monotonic() - start_time) * 1000

            logger.info(f"{operation_id}: completed in {duration_ms:.1f}ms")

            return OperationResult(
                success=True,
                data=result,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = (time.monotonic() - start_time) * 1000
            error_info = self._parse_slack_error(e, operation_id)
            logger.error(f"{operation_id} failed: {error_info['message']}", exc_info=True)

            return OperationResult(
                success=False,
                error=error_info["message"],
                error_code=error_info.get("code"),
                error_details=error_info.get("details"),
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

    async def _dispatch_operation(
        self, operation_id: str, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Dispatch to the correct handler function.

        Args:
            operation_id: Operation to execute.
            parameters: Operation parameters.

        Returns:
            Handler result dict.

        Raises:
            ValueError: If operation_id is unknown.
        """
        assert self._client is not None  # guaranteed by execute() check  # noqa: S101

        if operation_id == "get_channel_history":
            return await _handle_get_channel_history(self._client, parameters)

        if operation_id == "search_messages":
            return await _handle_search_messages(self._client, self._user_client, parameters)

        if operation_id == "list_channels":
            return await _handle_list_channels(self._client, parameters)

        if operation_id == "get_user_info":
            return await _handle_get_user_info(self._client, parameters)

        if operation_id == "post_message":
            return await _handle_post_message(self._client, parameters)

        if operation_id == "add_reaction":
            return await _handle_add_reaction(self._client, parameters)

        raise ValueError(f"Unknown operation: {operation_id}")

    @staticmethod
    def _parse_slack_error(error: Exception, operation_id: str) -> dict[str, Any]:
        """
        Parse Slack API errors into structured error information.

        Handles SlackApiError specifically for detailed error reporting.

        Args:
            error: The exception that was raised.
            operation_id: The operation that failed.

        Returns:
            Dict with 'message', 'code', and 'details' keys.
        """
        error_str = str(error)
        error_type = type(error).__name__

        try:
            from slack_sdk.errors import SlackApiError

            if isinstance(error, SlackApiError):
                slack_error = error.response.get("error", "unknown_error")

                # Map Slack error codes to MEHO-standard codes
                code_map: dict[str, str] = {
                    "not_authed": "PERMISSION_DENIED",
                    "invalid_auth": "PERMISSION_DENIED",
                    "account_inactive": "PERMISSION_DENIED",
                    "token_revoked": "PERMISSION_DENIED",
                    "not_allowed_token_type": "PERMISSION_DENIED",
                    "missing_scope": "PERMISSION_DENIED",
                    "channel_not_found": "NOT_FOUND",
                    "user_not_found": "NOT_FOUND",
                    "ratelimited": "QUOTA_EXCEEDED",
                    "invalid_arguments": "INVALID_ARGUMENT",
                    "too_many_attachments": "INVALID_ARGUMENT",
                }
                meho_code = code_map.get(slack_error, "UNKNOWN")

                return {
                    "code": meho_code,
                    "message": (
                        f"Slack API error for operation '{operation_id}': {slack_error}"
                    ),
                    "details": {
                        "error_type": error_type,
                        "slack_error": slack_error,
                        "operation": operation_id,
                        "raw_error": error_str[:500],
                    },
                }

        except ImportError:
            pass

        return {
            "code": "UNKNOWN",
            "message": error_str,
            "details": {
                "error_type": error_type,
                "operation": operation_id,
            },
        }

    # =========================================================================
    # OPERATION & TYPE DEFINITIONS
    # =========================================================================

    def get_operations(self) -> list[OperationDefinition]:
        """Get all Slack operation definitions."""
        return SLACK_OPERATIONS

    def get_types(self) -> list[TypeDefinition]:
        """Get all Slack type definitions."""
        return SLACK_TYPES
