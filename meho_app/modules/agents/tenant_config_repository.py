# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Repository for tenant agent configuration.

TASK-77: Externalize Prompts & Models
TASK-139: Extended for tenant management (Phase 4)
TASK-139 Phase 8: Added email domain lookup for tenant discovery

Provides CRUD operations for tenant-specific agent configuration,
including audit logging for configuration changes and tenant lifecycle management.
"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.agents.models import TenantAgentConfig, TenantAgentConfigAudit

logger = get_logger(__name__)


class TenantConfigRepository:
    """
    Repository for tenant agent configuration CRUD operations.

    Handles:
    - Loading tenant configuration
    - Saving/updating configuration
    - Audit logging of changes
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_config(self, tenant_id: str) -> TenantAgentConfig | None:
        """
        Get tenant configuration.

        Args:
            tenant_id: Tenant identifier

        Returns:
            TenantAgentConfig or None if not found
        """
        result = await self.session.execute(
            select(TenantAgentConfig).where(TenantAgentConfig.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def get_installation_context(self, tenant_id: str) -> str | None:
        """
        Get just the installation context for a tenant.

        This is the most common use case - getting context for prompt building.

        Args:
            tenant_id: Tenant identifier

        Returns:
            Installation context string or None
        """
        config = await self.get_config(tenant_id)
        return str(config.installation_context) if config and config.installation_context else None

    async def create_or_update(
        self,
        tenant_id: str,
        installation_context: str | None = None,
        model_override: str | None = None,
        temperature_override: float | None = None,
        features: dict[str, Any] | None = None,
        updated_by: str = "system",
    ) -> TenantAgentConfig:
        """
        Create or update tenant configuration.

        Args:
            tenant_id: Tenant identifier
            installation_context: Custom context for system prompt
            model_override: Optional model override
            temperature_override: Optional temperature override
            features: Feature flags dict
            updated_by: User making the change (for audit)

        Returns:
            Updated TenantAgentConfig
        """
        existing = await self.get_config(tenant_id)

        if existing:
            # Update existing config with audit logging
            await self._update_with_audit(
                existing,
                installation_context=installation_context,
                model_override=model_override,
                temperature_override=temperature_override,
                features=features,
                updated_by=updated_by,
            )
            return existing
        else:
            # Create new config
            config = TenantAgentConfig(
                tenant_id=tenant_id,
                installation_context=installation_context,
                model_override=model_override,
                temperature_override={"value": temperature_override}
                if temperature_override
                else None,
                features=features or {},
                updated_by=updated_by,
            )
            self.session.add(config)
            await self.session.flush()
            logger.info(f"Created tenant config for {tenant_id}")
            return config

    async def _update_with_audit(
        self,
        config: TenantAgentConfig,
        installation_context: str | None,
        model_override: str | None,
        temperature_override: float | None,
        features: dict[str, Any] | None,
        updated_by: str,
    ) -> None:
        """
        Update configuration with audit trail.

        Logs changes to the audit table for compliance/tracking.
        """
        changes_made: list[str] = []
        tenant_id_str = str(config.tenant_id)

        # Check and log each field change
        current_context = str(config.installation_context) if config.installation_context else None
        if installation_context is not None and current_context != installation_context:
            await self._log_change(
                tenant_id_str,
                "installation_context",
                current_context,
                installation_context,
                updated_by,
            )
            config.installation_context = installation_context  # type: ignore[assignment]
            changes_made.append("installation_context")

        current_model = str(config.model_override) if config.model_override else None
        if model_override is not None and current_model != model_override:
            await self._log_change(
                tenant_id_str,
                "model_override",
                current_model,
                model_override,
                updated_by,
            )
            config.model_override = model_override  # type: ignore[assignment]
            changes_made.append("model_override")

        if temperature_override is not None:
            temp_dict = config.temperature_override
            current_temp = temp_dict.get("value") if temp_dict else None
            if current_temp != temperature_override:
                await self._log_change(
                    tenant_id_str,
                    "temperature_override",
                    str(current_temp) if current_temp else None,
                    str(temperature_override),
                    updated_by,
                )
                config.temperature_override = {"value": temperature_override}  # type: ignore[assignment]
                changes_made.append("temperature_override")

        current_features = dict(config.features) if config.features else {}
        if features is not None and current_features != features:
            await self._log_change(
                tenant_id_str,
                "features",
                json.dumps(current_features),
                json.dumps(features),
                updated_by,
            )
            config.features = features  # type: ignore[assignment]
            changes_made.append("features")

        if changes_made:
            config.updated_by = updated_by  # type: ignore[assignment]
            await self.session.flush()
            logger.info(f"Updated tenant config for {tenant_id_str}: {changes_made}")

    async def _log_change(
        self,
        tenant_id: str,
        field_changed: str,
        old_value: str | None,
        new_value: str | None,
        changed_by: str,
    ) -> None:
        """Create audit log entry for a configuration change."""
        audit = TenantAgentConfigAudit(
            tenant_id=tenant_id,
            field_changed=field_changed,
            old_value=old_value,
            new_value=new_value,
            changed_by=changed_by,
        )
        self.session.add(audit)

    async def get_audit_log(
        self,
        tenant_id: str,
        limit: int = 50,
    ) -> list[TenantAgentConfigAudit]:
        """
        Get audit log for a tenant.

        Args:
            tenant_id: Tenant identifier
            limit: Maximum number of entries to return

        Returns:
            List of audit entries, most recent first
        """
        result = await self.session.execute(
            select(TenantAgentConfigAudit)
            .where(TenantAgentConfigAudit.tenant_id == tenant_id)
            .order_by(TenantAgentConfigAudit.changed_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def delete_config(self, tenant_id: str) -> bool:
        """
        Delete tenant configuration.

        Args:
            tenant_id: Tenant identifier

        Returns:
            True if deleted, False if not found
        """
        config = await self.get_config(tenant_id)
        if config:
            await self.session.delete(config)
            await self.session.flush()
            logger.info(f"Deleted tenant config for {tenant_id}")
            return True
        return False

    # =========================================================================
    # Tenant Management Methods (TASK-139 Phase 4)
    # =========================================================================

    async def list_all_tenants(
        self,
        include_inactive: bool = False,
    ) -> list[TenantAgentConfig]:
        """
        List all tenants.

        Args:
            include_inactive: Whether to include inactive tenants

        Returns:
            List of TenantAgentConfig objects
        """
        query = select(TenantAgentConfig).order_by(TenantAgentConfig.tenant_id)

        if not include_inactive:
            query = query.where(TenantAgentConfig.is_active == True)  # noqa: E712

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def find_by_email_domain(
        self,
        domain: str,
        active_only: bool = True,
    ) -> TenantAgentConfig | None:
        """
        Find tenant by email domain.

        TASK-139 Phase 8: Email-based tenant discovery.

        Args:
            domain: Email domain to search for (e.g., "acme.com")
            active_only: Whether to only return active tenants

        Returns:
            TenantAgentConfig if found, None otherwise
        """
        # Normalize domain to lowercase
        domain_lower = domain.lower().strip()

        # Query for tenant where email_domains JSON array contains the domain
        # PostgreSQL JSON containment: email_domains @> '["domain"]'
        query = select(TenantAgentConfig).where(
            TenantAgentConfig.email_domains.contains([domain_lower])
        )

        if active_only:
            query = query.where(TenantAgentConfig.is_active == True)  # noqa: E712

        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def create_tenant(
        self,
        tenant_id: str,
        display_name: str,
        subscription_tier: str = "free",
        max_connectors: int | None = None,
        max_knowledge_chunks: int | None = None,
        max_workflows_per_day: int | None = None,
        installation_context: str | None = None,
        model_override: str | None = None,
        temperature_override: float | None = None,
        features: dict[str, Any] | None = None,
        email_domains: list[str] | None = None,
        created_by: str = "system",
    ) -> TenantAgentConfig:
        """
        Create a new tenant.

        Args:
            tenant_id: Unique tenant identifier
            display_name: Human-readable display name
            subscription_tier: Subscription tier (free, pro, enterprise)
            max_connectors: Maximum connectors allowed (None=unlimited)
            max_knowledge_chunks: Maximum knowledge chunks allowed
            max_workflows_per_day: Maximum workflow executions per day
            installation_context: Custom context for system prompt
            model_override: LLM model override
            temperature_override: Temperature override
            features: Feature flags dict
            email_domains: List of email domains for tenant discovery
            created_by: User creating the tenant

        Returns:
            Created TenantAgentConfig
        """
        # Normalize email domains to lowercase
        normalized_domains = None
        if email_domains:
            normalized_domains = [d.lower().strip() for d in email_domains]

        config = TenantAgentConfig(
            tenant_id=tenant_id,
            display_name=display_name,
            is_active=True,
            subscription_tier=subscription_tier,
            email_domains=normalized_domains or [],
            max_connectors=max_connectors,
            max_knowledge_chunks=max_knowledge_chunks,
            max_workflows_per_day=max_workflows_per_day,
            installation_context=installation_context,
            model_override=model_override,
            temperature_override={"value": temperature_override} if temperature_override else None,
            features=features or {},
            updated_by=created_by,
        )
        self.session.add(config)
        await self.session.flush()
        logger.info(f"Created tenant: {tenant_id} by {created_by}")
        return config

    async def update_tenant(
        self,
        tenant_id: str,
        display_name: str | None = None,
        subscription_tier: str | None = None,
        is_active: bool | None = None,
        max_connectors: int | None = None,
        max_knowledge_chunks: int | None = None,
        max_workflows_per_day: int | None = None,
        installation_context: str | None = None,
        model_override: str | None = None,
        temperature_override: float | None = None,
        features: dict[str, Any] | None = None,
        email_domains: list[str] | None = None,
        updated_by: str = "system",
    ) -> TenantAgentConfig:
        """
        Update tenant settings.

        Only non-None values are updated.

        Args:
            tenant_id: Tenant identifier
            display_name: New display name
            subscription_tier: New subscription tier
            is_active: Whether tenant is active
            max_connectors: New max connectors limit
            max_knowledge_chunks: New max knowledge chunks limit
            max_workflows_per_day: New max workflows per day limit
            installation_context: New installation context
            model_override: New model override
            temperature_override: New temperature override
            features: New feature flags
            email_domains: New email domains for tenant discovery
            updated_by: User making the update

        Returns:
            Updated TenantAgentConfig

        Raises:
            ValueError: If tenant not found
        """
        config = await self.get_config(tenant_id)
        if not config:
            raise ValueError(f"Tenant '{tenant_id}' not found")

        changes_made: list[str] = []

        # Update fields if provided
        if display_name is not None:
            config.display_name = display_name  # type: ignore[assignment]
            changes_made.append("display_name")

        if subscription_tier is not None:
            config.subscription_tier = subscription_tier  # type: ignore[assignment]
            changes_made.append("subscription_tier")

        if is_active is not None:
            config.is_active = is_active  # type: ignore[assignment]
            changes_made.append("is_active")

        if max_connectors is not None:
            config.max_connectors = max_connectors  # type: ignore[assignment]
            changes_made.append("max_connectors")

        if max_knowledge_chunks is not None:
            config.max_knowledge_chunks = max_knowledge_chunks  # type: ignore[assignment]
            changes_made.append("max_knowledge_chunks")

        if max_workflows_per_day is not None:
            config.max_workflows_per_day = max_workflows_per_day  # type: ignore[assignment]
            changes_made.append("max_workflows_per_day")

        if installation_context is not None:
            config.installation_context = installation_context  # type: ignore[assignment]
            changes_made.append("installation_context")

        if model_override is not None:
            config.model_override = model_override  # type: ignore[assignment]
            changes_made.append("model_override")

        if temperature_override is not None:
            config.temperature_override = {"value": temperature_override}  # type: ignore[assignment]
            changes_made.append("temperature_override")

        if features is not None:
            config.features = features  # type: ignore[assignment]
            changes_made.append("features")

        if email_domains is not None:
            # Normalize email domains to lowercase
            normalized_domains = [d.lower().strip() for d in email_domains]
            config.email_domains = normalized_domains  # type: ignore[assignment]
            changes_made.append("email_domains")

        if changes_made:
            config.updated_by = updated_by  # type: ignore[assignment]
            await self.session.flush()
            logger.info(f"Updated tenant {tenant_id}: {changes_made}")

        return config

    async def disable_tenant(
        self,
        tenant_id: str,
        disabled_by: str = "system",
    ) -> TenantAgentConfig:
        """
        Disable a tenant (soft delete).

        Args:
            tenant_id: Tenant identifier
            disabled_by: User disabling the tenant

        Returns:
            Updated TenantAgentConfig

        Raises:
            ValueError: If tenant not found
        """
        config = await self.get_config(tenant_id)
        if not config:
            raise ValueError(f"Tenant '{tenant_id}' not found")

        config.is_active = False  # type: ignore[assignment]
        config.updated_by = disabled_by  # type: ignore[assignment]
        await self.session.flush()

        # Log the change
        await self._log_change(
            tenant_id,
            "is_active",
            "true",
            "false",
            disabled_by,
        )

        logger.info(f"Disabled tenant: {tenant_id} by {disabled_by}")
        return config

    async def enable_tenant(
        self,
        tenant_id: str,
        enabled_by: str = "system",
    ) -> TenantAgentConfig:
        """
        Re-enable a disabled tenant.

        Args:
            tenant_id: Tenant identifier
            enabled_by: User enabling the tenant

        Returns:
            Updated TenantAgentConfig

        Raises:
            ValueError: If tenant not found
        """
        config = await self.get_config(tenant_id)
        if not config:
            raise ValueError(f"Tenant '{tenant_id}' not found")

        config.is_active = True  # type: ignore[assignment]
        config.updated_by = enabled_by  # type: ignore[assignment]
        await self.session.flush()

        # Log the change
        await self._log_change(
            tenant_id,
            "is_active",
            "false",
            "true",
            enabled_by,
        )

        logger.info(f"Enabled tenant: {tenant_id} by {enabled_by}")
        return config
