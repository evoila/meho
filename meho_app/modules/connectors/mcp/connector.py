# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MCP Client Connector (Phase 93)

Connects to external MCP servers, discovers tools via list_tools(), and
proxies tool invocations via call_tool(). Unlike static connectors,
operations are dynamic -- discovered at runtime from the MCP server.

Supports both Streamable HTTP (for remote servers) and stdio (for local
subprocess servers) transports.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)
from meho_app.modules.connectors.mcp.operations import mcp_tool_to_operation
from meho_app.modules.connectors.mcp.types import MCP_TYPES

logger = get_logger(__name__)

# Retry constants
_MAX_RETRIES = 3
_RETRY_DELAYS = [1.0, 2.0, 4.0]


def _sanitize_server_name(name: str) -> str:
    """Sanitize a connector name into a valid server_name identifier.

    Lowered, spaces to underscores, strip non-alphanumeric/underscore chars.
    """
    name = name.lower().strip()
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^a-z0-9_]", "", name)
    return name or "mcp"


class MCPConnector(BaseConnector):
    """
    MCP Client connector -- consumes tools from external MCP servers.

    Config fields:
        transport_type: "streamable_http" (default) or "stdio"
        server_url: MCP server URL (required for streamable_http)
        command: Subprocess command (required for stdio)
        args: Command arguments (for stdio)
        env: Environment variables (for stdio)
        server_name: Snake_case server identifier (auto-derived from name)

    Credential fields:
        api_key: Bearer token for MCP server auth (optional)
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ) -> None:
        super().__init__(connector_id, config, credentials)
        self._transport_type: str = config.get("transport_type", "streamable_http")
        self._server_url: str | None = config.get("server_url")
        self._command: str | None = config.get("command")
        self._server_name: str = config.get(
            "server_name", _sanitize_server_name(config.get("name", "mcp"))
        )

        self._session: ClientSession | None = None
        self._discovered_tools: list[OperationDefinition] = []
        self._raw_tools: list[Any] = []
        self._transport_ctx: Any = None
        self._session_ctx: Any = None

    async def connect(self) -> bool:
        """Connect to MCP server and discover tools.

        Uses exponential backoff retry for ConnectionError and TimeoutError.
        Auth errors (401/403) are NOT retried.
        """
        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                return await self._do_connect()
            except (ConnectionError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_DELAYS[attempt]
                    logger.warning(
                        f"MCP connect attempt {attempt + 1}/{_MAX_RETRIES} failed for {self.connector_id}: {exc}. Retrying in {delay:.1f}s",
                    )
                    await asyncio.sleep(delay)
            except Exception as exc:
                # Auth errors and other non-retryable errors
                logger.error(
                    f"MCP connect failed (non-retryable) for {self.connector_id}: {exc}",
                )
                raise

        msg = f"MCP connect failed after {_MAX_RETRIES} attempts: {last_error}"
        raise ConnectionError(msg)

    async def _do_connect(self) -> bool:
        """Establish connection and discover tools (single attempt)."""
        headers: dict[str, str] = {}
        api_key = self.credentials.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        if self._transport_type == "streamable_http":
            if not self._server_url:
                msg = "server_url is required for streamable_http transport"
                raise ValueError(msg)
            self._transport_ctx = streamablehttp_client(
                self._server_url, headers=headers if headers else None
            )
        elif self._transport_type == "stdio":
            if not self._command:
                msg = "command is required for stdio transport"
                raise ValueError(msg)
            args = self.config.get("args", [])
            env = self.config.get("env")
            self._transport_ctx = stdio_client(
                StdioServerParameters(command=self._command, args=args, env=env)
            )
        else:
            msg = f"Unsupported transport type: {self._transport_type}"
            raise ValueError(msg)

        read_stream, write_stream, _ = await self._transport_ctx.__aenter__()
        self._session_ctx = ClientSession(read_stream, write_stream)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()

        # Discover tools
        response = await self._session.list_tools()
        self._raw_tools = list(response.tools)
        self._discovered_tools = [
            mcp_tool_to_operation(tool, self._server_name) for tool in response.tools
        ]

        self._is_connected = True
        logger.info(
            f"MCP connector {self.connector_id} connected to {self._server_url or self._command}, discovered {len(self._discovered_tools)} tools",
        )
        return True

    async def _execute_operation(
        self, operation_id: str, parameters: dict[str, Any]
    ) -> OperationResult:
        """Proxy operation execution to the MCP server via call_tool."""
        if not self._session or not self._is_connected:
            return OperationResult(
                success=False,
                error="Not connected to MCP server",
                operation_id=operation_id,
            )

        # Strip the mcp_{server_name}_ prefix to get original tool name
        prefix = f"mcp_{self._server_name}_"
        original_name = operation_id
        if operation_id.startswith(prefix):
            original_name = operation_id[len(prefix) :]

        start = time.monotonic()
        try:
            result = await self._session.call_tool(original_name, arguments=parameters)
        except (ConnectionError, OSError) as exc:
            # Attempt single reconnect
            logger.warning(f"MCP call_tool failed, attempting reconnect: {exc}")
            try:
                await self.disconnect()
                await self._do_connect()
                result = await self._session.call_tool(original_name, arguments=parameters)
            except Exception as reconnect_exc:
                duration_ms = (time.monotonic() - start) * 1000
                return OperationResult(
                    success=False,
                    error=f"MCP server unreachable after reconnect: {reconnect_exc}",
                    operation_id=operation_id,
                    duration_ms=duration_ms,
                )
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            return OperationResult(
                success=False,
                error=f"MCP tool call failed: {exc}",
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

        duration_ms = (time.monotonic() - start) * 1000

        # Extract text content from MCP result
        text_parts = [c.text for c in result.content if hasattr(c, "text")]
        return OperationResult(
            success=not result.isError,
            data="\n".join(text_parts) if text_parts else None,
            operation_id=operation_id,
            duration_ms=duration_ms,
        )

    def get_operations(self) -> list[OperationDefinition]:
        """Return dynamically discovered tools as OperationDefinitions."""
        return self._discovered_tools

    def get_types(self) -> list[TypeDefinition]:
        """Return MCP entity types."""
        return MCP_TYPES

    async def test_connection(self) -> bool:
        """Test connection by calling list_tools as a lightweight healthcheck."""
        if not self._session:
            return False
        try:
            response = await self._session.list_tools()
            return len(response.tools) >= 0
        except Exception as exc:
            logger.warning(f"MCP test_connection failed for {self.connector_id}: {exc}")
            return False

    async def disconnect(self) -> None:
        """Disconnect from MCP server, cleaning up context managers."""
        if self._session_ctx is not None:
            try:
                await self._session_ctx.__aexit__(None, None, None)
            except Exception as exc:
                logger.debug(f"Error closing MCP session: {exc}")
            self._session_ctx = None

        if self._transport_ctx is not None:
            try:
                await self._transport_ctx.__aexit__(None, None, None)
            except Exception as exc:
                logger.debug(f"Error closing MCP transport: {exc}")
            self._transport_ctx = None

        self._session = None
        self._is_connected = False

    @property
    def raw_tools(self) -> list[Any]:
        """Access raw MCP tool objects (for sync/operations registration)."""
        return self._raw_tools

    @property
    def server_name(self) -> str:
        """Sanitized server name used for operation prefixing."""
        return self._server_name
