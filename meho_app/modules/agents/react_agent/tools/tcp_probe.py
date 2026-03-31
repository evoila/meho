# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""TCP probe tool - Test TCP connectivity to a host:port.

Uses asyncio.open_connection for async TCP handshake with configurable timeout.
Returns connection status (connected/refused/timeout/error) and latency.
No topology emission -- TCP probes alone don't discover entities rich enough
for topology; the higher-level tools (dns_resolve, http_probe) handle that.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter


class TcpProbeInput(BaseModel):
    """Input for tcp_probe tool."""

    host: str = Field(description="Hostname or IP address to probe")
    port: int = Field(ge=1, le=65535, description="TCP port number")
    timeout_seconds: float = Field(
        default=5.0,
        ge=0.5,
        le=30.0,
        description="Connection timeout in seconds",
    )


class TcpProbeOutput(BaseModel):
    """Output from tcp_probe tool."""

    host: str = Field(description="The probed host")
    port: int = Field(description="The probed port")
    status: str = Field(description="Connection status: connected, refused, timeout, or error")
    latency_ms: float | None = Field(default=None, description="Round-trip latency in milliseconds")
    error: str | None = Field(default=None, description="Error message if connection failed")
    success: bool = Field(default=False, description="True if TCP connection succeeded")


@dataclass
class TcpProbeTool(BaseTool[TcpProbeInput, TcpProbeOutput]):
    """Test TCP connectivity to a host:port.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "tcp_probe"
    TOOL_DESCRIPTION: ClassVar[str] = """Test TCP connectivity to host:port.
Returns connected/refused/timeout status with latency.
Use to check if a service port is reachable and responsive."""
    InputSchema: ClassVar[type[BaseModel]] = TcpProbeInput
    OutputSchema: ClassVar[type[BaseModel]] = TcpProbeOutput

    async def execute(
        self,
        tool_input: TcpProbeInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> TcpProbeOutput:
        """Execute TCP probe against the specified host:port."""
        await emitter.tool_start(self.TOOL_NAME)

        status = "error"
        latency_ms: float | None = None
        error: str | None = None

        start = time.perf_counter()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(tool_input.host, tool_input.port),
                timeout=tool_input.timeout_seconds,
            )
            latency_ms = (time.perf_counter() - start) * 1000
            status = "connected"

            # Clean shutdown
            writer.close()
            await writer.wait_closed()

        except TimeoutError:
            latency_ms = (time.perf_counter() - start) * 1000
            status = "timeout"
            error = f"Connection timed out after {tool_input.timeout_seconds}s"

        except ConnectionRefusedError:
            latency_ms = (time.perf_counter() - start) * 1000
            status = "refused"
            error = f"Connection refused on {tool_input.host}:{tool_input.port}"

        except OSError as e:
            latency_ms = (time.perf_counter() - start) * 1000
            status = "error"
            error = str(e)

        success = status == "connected"
        await emitter.tool_complete(self.TOOL_NAME, success=success)

        return TcpProbeOutput(
            host=tool_input.host,
            port=tool_input.port,
            status=status,
            latency_ms=round(latency_ms, 2) if latency_ms is not None else None,
            error=error,
            success=success,
        )
