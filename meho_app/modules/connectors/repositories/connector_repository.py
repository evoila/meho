# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Repository for Connector database operations.
"""

# mypy: disable-error-code="arg-type,assignment"
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.modules.connectors.models import ConnectorModel
from meho_app.modules.connectors.schemas import Connector, ConnectorCreate, ConnectorUpdate
from meho_app.modules.connectors.utils import extract_target_host


class ConnectorRepository:
    """Repository for connector operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_connector(self, connector: ConnectorCreate) -> Connector:
        """Create a new connector."""
        db_connector = ConnectorModel(id=uuid.uuid4(), **connector.model_dump())
        self.session.add(db_connector)
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
        await self.session.refresh(db_connector)

        return Connector(
            id=str(db_connector.id),
            **connector.model_dump(),
            is_active=db_connector.is_active,
            created_at=db_connector.created_at,
            updated_at=db_connector.updated_at,
        )

    async def get_connector(
        self, connector_id: str, tenant_id: str | None = None
    ) -> Connector | None:
        """Get connector by ID."""
        try:
            query = select(ConnectorModel).where(ConnectorModel.id == uuid.UUID(connector_id))
            if tenant_id:
                query = query.where(ConnectorModel.tenant_id == tenant_id)

            result = await self.session.execute(query)
            db_connector = result.scalar_one_or_none()

            if not db_connector:
                return None

            return Connector(
                id=str(db_connector.id),
                tenant_id=db_connector.tenant_id,
                name=db_connector.name,
                description=db_connector.description,
                routing_description=db_connector.routing_description,
                base_url=db_connector.base_url,
                auth_type=db_connector.auth_type,
                auth_config=db_connector.auth_config,
                credential_strategy=db_connector.credential_strategy,
                # Connector type - single source of truth
                connector_type=getattr(db_connector, "connector_type", "rest") or "rest",
                protocol_config=db_connector.protocol_config,
                # SESSION auth fields
                login_url=db_connector.login_url,
                login_method=db_connector.login_method,
                login_config=db_connector.login_config,
                # Safety policies
                allowed_methods=db_connector.allowed_methods,
                blocked_methods=db_connector.blocked_methods,
                default_safety_level=db_connector.default_safety_level,
                # Related connectors for topology correlation
                related_connector_ids=db_connector.related_connector_ids or [],
                is_active=db_connector.is_active,
                created_at=db_connector.created_at,
                updated_at=db_connector.updated_at,
                # Topology entity reference
                topology_entity_id=str(db_connector.topology_entity_id)
                if db_connector.topology_entity_id
                else None,
                # Skill fields (Phase 7 - Skill Editor UI)
                generated_skill=db_connector.generated_skill,
                custom_skill=db_connector.custom_skill,
                skill_quality_score=db_connector.skill_quality_score,
                skill_name=db_connector.skill_name,
            )
        except ValueError:
            return None

    async def list_connectors(self, tenant_id: str, active_only: bool = True) -> list[Connector]:
        """List connectors for a tenant."""
        query = select(ConnectorModel).where(ConnectorModel.tenant_id == tenant_id)
        if active_only:
            query = query.where(ConnectorModel.is_active)
        query = query.order_by(ConnectorModel.created_at.desc())

        result = await self.session.execute(query)
        db_connectors = result.scalars().all()

        # Convert database models to Pydantic schemas
        connectors = []
        for c in db_connectors:
            connector = Connector(
                id=str(c.id),
                tenant_id=c.tenant_id,
                name=c.name,
                description=c.description,
                routing_description=c.routing_description,
                base_url=c.base_url,
                auth_type=c.auth_type,
                auth_config=c.auth_config or {},
                credential_strategy=c.credential_strategy or "SYSTEM",
                connector_type=getattr(c, "connector_type", "rest") or "rest",
                protocol_config=c.protocol_config,
                login_url=c.login_url,
                login_method=c.login_method,
                login_config=c.login_config,
                allowed_methods=c.allowed_methods or ["GET", "POST", "PUT", "PATCH", "DELETE"],
                blocked_methods=c.blocked_methods or [],
                default_safety_level=c.default_safety_level or "safe",
                # Related connectors for topology correlation
                related_connector_ids=c.related_connector_ids or [],
                is_active=c.is_active,
                created_at=c.created_at,
                updated_at=c.updated_at,
                # Topology entity reference
                topology_entity_id=str(c.topology_entity_id) if c.topology_entity_id else None,
                # Skill fields (Phase 7 - Skill Editor UI)
                generated_skill=c.generated_skill,
                custom_skill=c.custom_skill,
                skill_quality_score=c.skill_quality_score,
                skill_name=c.skill_name,
            )
            connectors.append(connector)

        return connectors

    async def update_connector(
        self, connector_id: str, update: ConnectorUpdate, tenant_id: str | None = None
    ) -> Connector | None:
        """Update connector configuration."""
        try:
            query = select(ConnectorModel).where(ConnectorModel.id == uuid.UUID(connector_id))
            if tenant_id:
                query = query.where(ConnectorModel.tenant_id == tenant_id)

            result = await self.session.execute(query)
            db_connector = result.scalar_one_or_none()

            if not db_connector:
                return None

            # Update fields
            update_data = update.model_dump(exclude_unset=True)
            for key, value in update_data.items():
                setattr(db_connector, key, value)

            db_connector.updated_at = datetime.now(tz=UTC)

            await self.session.flush()
            await self.session.refresh(db_connector)

            return Connector.model_validate(db_connector)
        except ValueError:
            return None

    async def delete_connector(self, connector_id: str, tenant_id: str | None = None) -> bool:
        """
        Delete a connector.

        Returns True if deleted, False if not found.

        Note: This will cascade delete related records (specs, endpoints, credentials)
        due to foreign key constraints with ON DELETE CASCADE.
        """
        try:
            query = select(ConnectorModel).where(ConnectorModel.id == uuid.UUID(connector_id))
            if tenant_id:
                query = query.where(ConnectorModel.tenant_id == tenant_id)

            result = await self.session.execute(query)
            db_connector = result.scalar_one_or_none()

            if not db_connector:
                return False

            await self.session.delete(db_connector)
            await self.session.flush()

            return True
        except ValueError:
            return False

    async def find_by_target_host(
        self,
        hostname: str,
        tenant_id: str,
        active_only: bool = True,
    ) -> list[ConnectorModel]:
        """
        Find connectors whose base_url targets the given hostname.

        Used for automatic cross-connector correlation when a K8s Ingress
        or VMware VM is discovered with an IP/hostname that matches a
        REST/SOAP connector's target.

        Args:
            hostname: The hostname to search for (e.g., "api.myapp.com")
            tenant_id: Tenant ID to filter by
            active_only: Only return active connectors

        Returns:
            List of connectors whose base_url matches the hostname

        Note:
            This performs an in-memory filter since extract_target_host
            cannot be done purely in SQL. For large numbers of connectors,
            consider adding a computed/indexed column for target_host.
        """
        # Build query
        query = select(ConnectorModel).where(ConnectorModel.tenant_id == tenant_id)
        if active_only:
            query = query.where(ConnectorModel.is_active)

        result = await self.session.execute(query)
        all_connectors = result.scalars().all()

        # Filter in Python using extract_target_host
        matching = []
        hostname_lower = hostname.lower()

        for connector in all_connectors:
            if connector.base_url:
                target = extract_target_host(connector.base_url)
                if target and target.lower() == hostname_lower:
                    matching.append(connector)

        return matching

    async def get_connector_model(
        self,
        connector_id: str,
        tenant_id: str | None = None,
    ) -> ConnectorModel | None:
        """
        Get the raw ConnectorModel (not converted to Pydantic schema).

        Used when you need the SQLAlchemy model for relationships
        or foreign key references.
        """
        try:
            query = select(ConnectorModel).where(ConnectorModel.id == uuid.UUID(connector_id))
            if tenant_id:
                query = query.where(ConnectorModel.tenant_id == tenant_id)

            result = await self.session.execute(query)
            return result.scalar_one_or_none()
        except ValueError:
            return None
