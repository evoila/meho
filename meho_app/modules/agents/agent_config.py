# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Agent Configuration System for MEHO.

Provides multi-layer configuration loading:
1. Inline Claude defaults (lowest priority)
2. Environment variables
3. Database (tenant-specific context)
4. Runtime injection (highest priority, for testing/evals)

Note: System prompts are built in reason_node.py, not loaded from files.
"""

import os
from typing import Any

from pydantic import BaseModel, Field

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class ModelConfig(BaseModel):
    """LLM Model configuration."""

    name: str = Field(description="Model identifier (e.g., 'anthropic:claude-opus-4-6')")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)


class DataReductionConfig(BaseModel):
    """Configuration for UnifiedExecutor data reduction."""

    auto_reduce_threshold: int = Field(default=50, description="Records threshold")
    auto_reduce_size_kb: int = Field(default=50, description="Size threshold in KB")


class AgentConfig(BaseModel):
    """
    Complete configuration for MEHO Agent.

    Supports multi-layer configuration:
    - Layer 1: Inline Claude defaults
    - Layer 2: Environment variable overrides
    - Layer 3: Database tenant context and model override
    - Layer 4: Runtime injection (for testing)

    Note: System prompts are built in reason_node.py, not loaded from files.
    """

    model: ModelConfig
    tenant_context: str | None = Field(
        default=None, description="Admin-defined installation context from database"
    )
    data_reduction: DataReductionConfig = Field(default_factory=DataReductionConfig)
    retries: int = Field(default=2, ge=0)
    instrument: bool = Field(default=True)

    @classmethod
    async def load(
        cls,
        tenant_id: str | None = None,
        runtime_overrides: dict[str, Any] | None = None,
        session_maker: Any | None = None,
    ) -> "AgentConfig":
        """
        Load configuration from all layers.

        Args:
            tenant_id: Tenant ID for loading context from database
            runtime_overrides: Optional dict with runtime overrides (for testing)
            session_maker: SQLAlchemy session maker (for DB access)

        Returns:
            AgentConfig with merged configuration from all layers
        """
        # Layer 1 + 2: Inline defaults with environment variable overrides
        model_name = os.getenv("STREAMING_AGENT_MODEL", "anthropic:claude-opus-4-6")

        temperature = float(os.getenv("MEHO_LLM_TEMPERATURE", "0.7"))

        max_tokens = int(os.getenv("MEHO_LLM_MAX_TOKENS", "4096"))

        # Layer 3: Tenant context and model override from database
        tenant_context = None
        if tenant_id and session_maker:
            tenant_context = await cls._load_tenant_context(tenant_id, session_maker)
            # Admin model selection: model_override from TenantAgentConfig
            # takes precedence over env var / inline defaults
            tenant_model = await cls._load_tenant_model_override(tenant_id, session_maker)
            if tenant_model:
                model_name = tenant_model

        # Layer 4: Runtime overrides
        if runtime_overrides:
            model_name = runtime_overrides.get("model", model_name)
            temperature = runtime_overrides.get("temperature", temperature)
            max_tokens = runtime_overrides.get("max_tokens", max_tokens)

        logger.info(f"AgentConfig loaded: model={model_name}, temp={temperature}")

        return cls(
            model=ModelConfig(
                name=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            tenant_context=tenant_context,
            data_reduction=DataReductionConfig(),
            retries=2,
            instrument=True,
        )

    @staticmethod
    async def _load_tenant_context(tenant_id: str, session_maker: Any) -> str | None:
        """
        Load tenant-specific context from database.

        Args:
            tenant_id: Tenant identifier
            session_maker: SQLAlchemy async session maker

        Returns:
            Tenant installation context or None
        """
        try:
            from sqlalchemy import select

            from meho_app.modules.agents.models import TenantAgentConfig

            async with session_maker() as session:
                result = await session.execute(
                    select(TenantAgentConfig).where(TenantAgentConfig.tenant_id == tenant_id)
                )
                config = result.scalar_one_or_none()

                if config and config.installation_context:
                    logger.info(f"Loaded tenant context for {tenant_id}")
                    return str(config.installation_context)

        except ImportError:
            logger.debug("TenantAgentConfig model not available, skipping tenant context")
        except Exception as e:
            logger.warning(f"Could not load tenant context: {e}")

        return None

    @staticmethod
    async def _load_tenant_model_override(tenant_id: str, session_maker: Any) -> str | None:
        """
        Load tenant-specific model override from database.

        Admin model selection: when TenantAgentConfig.model_override is set,
        it replaces the config default. This allows admins to switch models
        per tenant without redeployment via PUT /api/admin/config.

        Args:
            tenant_id: Tenant identifier
            session_maker: SQLAlchemy async session maker

        Returns:
            Model name string (e.g., 'anthropic:claude-opus-4-6') or None
        """
        try:
            from sqlalchemy import select

            from meho_app.modules.agents.models import TenantAgentConfig

            async with session_maker() as session:
                result = await session.execute(
                    select(TenantAgentConfig).where(TenantAgentConfig.tenant_id == tenant_id)
                )
                config = result.scalar_one_or_none()

                if config and config.model_override:
                    logger.info(f"Tenant {tenant_id} has model override: {config.model_override}")
                    return str(config.model_override)

        except ImportError:
            logger.debug("TenantAgentConfig model not available, skipping model override")
        except Exception as e:
            logger.warning(f"Could not load tenant model override: {e}")

        return None


# Convenience function for getting agent configuration
async def get_agent_config(
    tenant_id: str | None = None,
    runtime_overrides: dict[str, Any] | None = None,
) -> AgentConfig:
    """
    Get agent configuration with all layers applied.

    This is the main entry point for getting agent configuration.
    Automatically provides database session maker when a tenant_id is
    given, enabling model_override lookup from TenantAgentConfig.

    Args:
        tenant_id: Optional tenant ID for context and model override
        runtime_overrides: Optional overrides for testing

    Returns:
        AgentConfig instance
    """
    # Auto-provide session_maker for tenant DB lookups (model_override, context)
    session_maker = None
    if tenant_id:
        try:
            from meho_app.database import get_session_maker

            session_maker = get_session_maker()
        except Exception:  # noqa: S110 -- intentional silent exception handling
            pass  # DB not available (e.g., tests) -- skip tenant lookups

    return await AgentConfig.load(
        tenant_id=tenant_id,
        session_maker=session_maker,
        runtime_overrides=runtime_overrides,
    )
