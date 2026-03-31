"""
Repository for tenant agent configuration.

TASK-77: Externalize Prompts & Models

Provides CRUD operations for tenant-specific agent configuration,
including audit logging for configuration changes.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, Dict, Any
import logging
import json

from meho_agent.models import TenantAgentConfig, TenantAgentConfigAudit

logger = logging.getLogger(__name__)


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
    
    async def get_config(self, tenant_id: str) -> Optional[TenantAgentConfig]:
        """
        Get tenant configuration.
        
        Args:
            tenant_id: Tenant identifier
            
        Returns:
            TenantAgentConfig or None if not found
        """
        result = await self.session.execute(
            select(TenantAgentConfig).where(
                TenantAgentConfig.tenant_id == tenant_id
            )
        )
        return result.scalar_one_or_none()
    
    async def get_installation_context(self, tenant_id: str) -> Optional[str]:
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
        installation_context: Optional[str] = None,
        model_override: Optional[str] = None,
        temperature_override: Optional[float] = None,
        features: Optional[Dict[str, Any]] = None,
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
                temperature_override={"value": temperature_override} if temperature_override else None,
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
        installation_context: Optional[str],
        model_override: Optional[str],
        temperature_override: Optional[float],
        features: Optional[Dict[str, Any]],
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
        old_value: Optional[str],
        new_value: Optional[str],
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

