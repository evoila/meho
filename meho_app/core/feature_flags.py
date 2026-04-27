# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Feature flags for optional MEHO modules.

All flags default to True (enabled). Set MEHO_FEATURE_X=false to disable.
Flags are immutable after startup -- restart required to change.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FeatureFlags(BaseSettings):
    """Runtime feature flags loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="MEHO_FEATURE_",
        frozen=True,
    )

    knowledge: bool = Field(default=True, description="Knowledge base and search")
    topology: bool = Field(default=True, description="Topology discovery and graph")
    scheduled_tasks: bool = Field(default=True, description="Cron-based scheduled tasks")
    events: bool = Field(
        default=True, description="Event-driven triggers (webhooks, scheduled, external)"
    )
    memory: bool = Field(default=True, description="Operator memory system")
    slack: bool = Field(default=True, description="Slack connector and bot")
    network_diagnostics: bool = Field(
        default=True,
        description="Network diagnostic tools (dns_resolve, tcp_probe, http_probe, tls_check)",
    )
    mcp_client: bool = Field(
        default=True, description="MCP client connector for external tool servers"
    )
    mcp_server: bool = Field(
        default=True, description="MCP server exposing MEHO tools to external clients"
    )
    ephemeral_ingestion: bool = Field(
        default=False,
        description="Enable ephemeral ingestion worker dispatch (requires backend config)",
    )
    use_docling: bool = Field(
        default=True,
        description="Use Docling for document ingestion (requires PyTorch/GPU). "
        "Set to false for lightweight CPU-only ingestion via pymupdf4llm.",
    )


@lru_cache
def get_feature_flags() -> FeatureFlags:
    """Get cached feature flags singleton."""
    return FeatureFlags()
