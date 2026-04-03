# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""HTTP probe tool - Probe a URL with full response details.

Uses httpx for async HTTP requests with redirect following, latency measurement,
header capture, redirect chain tracking, and body preview. Emits ExternalURL
topology entities for cross-connector correlation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter

logger = logging.getLogger(__name__)


class HttpProbeInput(BaseModel):
    """Input for http_probe tool."""

    url: str = Field(description="URL to probe (e.g., 'https://example.com')")
    method: str = Field(
        default="GET",
        description="HTTP method (GET or HEAD only)",
        pattern="^(GET|HEAD)$",
    )
    timeout_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        description="Request timeout in seconds",
    )
    follow_redirects: bool = Field(
        default=True,
        description="Whether to follow HTTP redirects",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Additional HTTP headers to send",
    )


class HttpProbeOutput(BaseModel):
    """Output from http_probe tool."""

    url: str = Field(description="The original requested URL")
    final_url: str = Field(default="", description="Final URL after redirects")
    status_code: int = Field(default=0, description="HTTP status code")
    latency_ms: float = Field(default=0.0, description="Request latency in milliseconds")
    headers: dict[str, str] = Field(default_factory=dict, description="Response headers")
    content_type: str = Field(default="", description="Content-Type header value")
    redirect_chain: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Redirect chain: [{url, status_code}]",
    )
    body_preview: str | None = Field(
        default=None,
        description="First 500 chars of body (text/json content only)",
    )
    error: str | None = Field(default=None, description="Error message if request failed")
    success: bool = Field(default=False, description="True if HTTP request completed")


@dataclass
class HttpProbeTool(BaseTool[HttpProbeInput, HttpProbeOutput]):
    """Probe a URL and return full HTTP response details.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "http_probe"
    TOOL_DESCRIPTION: ClassVar[str] = """Probe a URL with HTTP GET or HEAD.
Returns status code, latency, headers, redirect chain, content type, and body preview.
Use to check if a web endpoint is reachable and what it returns."""
    InputSchema: ClassVar[type[BaseModel]] = HttpProbeInput
    OutputSchema: ClassVar[type[BaseModel]] = HttpProbeOutput

    async def execute(
        self,
        tool_input: HttpProbeInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> HttpProbeOutput:
        """Execute HTTP probe against the specified URL."""
        await emitter.tool_start(self.TOOL_NAME)

        try:
            async with httpx.AsyncClient(
                follow_redirects=tool_input.follow_redirects,
                timeout=httpx.Timeout(tool_input.timeout_seconds),
                verify=True,
            ) as client:
                start = time.perf_counter()
                response = await client.request(
                    method=tool_input.method,
                    url=tool_input.url,
                    headers=tool_input.headers,
                )
                latency_ms = (time.perf_counter() - start) * 1000

            # Extract redirect chain from history
            redirect_chain = [
                {"url": str(r.url), "status_code": r.status_code} for r in response.history
            ]

            # Response headers as plain dict
            resp_headers = dict(response.headers)
            content_type = response.headers.get("content-type", "")

            # Body preview: first 500 chars for text/json content
            body_preview: str | None = None
            if tool_input.method == "GET" and ("text" in content_type or "json" in content_type):
                body_preview = response.text[:500] if response.text else None

            final_url = str(response.url)
            status_code = response.status_code

            output = HttpProbeOutput(
                url=tool_input.url,
                final_url=final_url,
                status_code=status_code,
                latency_ms=round(latency_ms, 2),
                headers=resp_headers,
                content_type=content_type,
                redirect_chain=redirect_chain,
                body_preview=body_preview,
                success=True,
            )

            # Fire-and-forget topology emission
            try:
                await _emit_topology(
                    final_url=final_url,
                    status_code=status_code,
                    latency_ms=round(latency_ms, 2),
                    content_type=content_type,
                    deps=deps,
                )
            except Exception:
                logger.debug(
                    "Topology emission failed for http_probe(%s), continuing",
                    tool_input.url,
                    exc_info=True,
                )

            await emitter.tool_complete(self.TOOL_NAME, success=True)
            return output

        except httpx.TimeoutException as e:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            return HttpProbeOutput(
                url=tool_input.url,
                error=f"Request timed out after {tool_input.timeout_seconds}s: {e}",
                success=False,
            )
        except httpx.HTTPError as e:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            return HttpProbeOutput(
                url=tool_input.url,
                error=f"HTTP error: {e}",
                success=False,
            )
        except Exception as e:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            return HttpProbeOutput(
                url=tool_input.url,
                error=f"Unexpected error: {e}",
                success=False,
            )


async def _emit_topology(
    *,
    final_url: str,
    status_code: int,
    latency_ms: float,
    content_type: str,
    deps: Any,
) -> None:
    """Emit ExternalURL topology entity (fire-and-forget)."""
    meho_deps = getattr(deps, "meho_deps", None)
    if not meho_deps:
        return

    session = getattr(meho_deps, "db_session", None)
    if not session:
        return

    tenant_id = getattr(meho_deps, "tenant_id", "default")

    from meho_app.modules.topology.schemas import (
        StoreDiscoveryInput,
        TopologyEntityCreate,
    )
    from meho_app.modules.topology.service import TopologyService

    now_iso = datetime.now(UTC).isoformat()

    entity = TopologyEntityCreate(
        name=final_url,
        entity_type="ExternalURL",
        connector_type="network_diagnostics",
        canonical_id=final_url,
        description=f"HTTP probe of {final_url} returned {status_code} in {latency_ms:.0f}ms",
        raw_attributes={
            "url": final_url,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "content_type": content_type,
            "last_probed": now_iso,
        },
    )

    service = TopologyService(session)
    await service.store_discovery(
        input=StoreDiscoveryInput(
            connector_type="network_diagnostics",
            entities=[entity],
        ),
        tenant_id=tenant_id,
    )
