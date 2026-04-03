# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ConnectorService - Business logic for connector management.

This service handles:
- Connector CRUD operations
- Credential management
- Connection testing
- Auto-ingestion of specs (OpenAPI, WSDL)
- Topology entity registration (TASK-144)
"""

# Import protocols for type hints
from datetime import UTC
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.repositories import (
    ConnectorRepository,
    CredentialRepository,
)
from meho_app.modules.connectors.schemas import (
    Connector,
    ConnectorCreate,
    ConnectorUpdate,
    UserCredentialProvide,
)
from meho_app.modules.connectors.utils import extract_target_host

if TYPE_CHECKING:
    from meho_app.protocols.openapi import IConnectorRepository

logger = get_logger(__name__)


def _build_connector_entity_description(
    name: str,
    connector_type: str,
    target_host: str,
    description: str | None = None,
) -> str:
    """
    Build a rich description for a connector topology entity.

    Used for embedding generation to enable similarity-based matching
    against discovered infrastructure (K8s Ingresses, VMware VMs, etc.).
    """
    desc = f"{connector_type.upper()} connector '{name}' targeting {target_host}"
    if description:
        desc += f". {description}"
    return desc


class ConnectorService:
    """
    Public API for connector management.

    Supports two construction patterns:

    1. Session-based (standard):
        service = ConnectorService(session)

    2. Protocol-based (for dependency injection):
        service = ConnectorService.from_protocols(
            connector_repo=mock_connector_repo,
            credential_repo=mock_credential_repo,
        )
    """

    def __init__(
        self,
        session: AsyncSession | None = None,
        *,
        connector_repo: Optional["IConnectorRepository"] = None,
        credential_repo: CredentialRepository | None = None,
    ) -> None:
        """
        Initialize ConnectorService.

        Args:
            session: AsyncSession (creates concrete implementations)
            connector_repo: Optional connector repository (for DI)
            credential_repo: Optional credential repository (for DI)
        """
        if session is not None:
            # Standard: build from session
            self.session = session
            self.connector_repo = connector_repo or ConnectorRepository(session)
            self.credential_repo = credential_repo or CredentialRepository(session)
        elif connector_repo is not None:
            # Protocol-based construction
            self.session = None  # type: ignore[assignment]  # session is Optional at runtime but typed as required
            self.connector_repo = connector_repo
            self.credential_repo = credential_repo  # type: ignore[assignment]
        else:
            raise ValueError(
                "ConnectorService requires either 'session' or 'connector_repo' argument"
            )

    @classmethod
    def from_protocols(
        cls,
        connector_repo: "IConnectorRepository",
        credential_repo: CredentialRepository | None = None,
    ) -> "ConnectorService":
        """
        Create ConnectorService from protocol implementations.

        Useful for testing with mocks.
        """
        return cls(
            session=None,
            connector_repo=connector_repo,
            credential_repo=credential_repo,
        )

    # =========================================================================
    # Connector CRUD
    # =========================================================================

    async def create_connector(self, data: ConnectorCreate) -> Connector:
        """
        Create a new connector and register it as a topology entity.

        The topology entity enables cross-connector correlation:
        - REST connector targeting api.myapp.com can be linked to
        - K8s Ingress for api.myapp.com via SAME_AS relationship
        """
        # 1. Create the connector
        connector = await self.connector_repo.create_connector(data)

        # 2. Register as topology entity (requires session)
        if self.session is not None:
            try:
                topology_entity_id = await self._create_connector_topology_entity(
                    connector_id=connector.id,
                    name=data.name,
                    connector_type=data.connector_type,
                    base_url=data.base_url,
                    description=data.description,
                    tenant_id=data.tenant_id,
                )

                if topology_entity_id:
                    # Update connector with topology entity reference
                    connector = (
                        await self.connector_repo.update_connector(
                            connector.id,
                            ConnectorUpdate(topology_entity_id=str(topology_entity_id)),
                            data.tenant_id,
                        )
                        or connector
                    )

                    logger.info(
                        f"Registered connector '{data.name}' ({data.connector_type}) as topology entity {topology_entity_id}"
                    )

                    # 3. Create relationships to related connectors
                    if data.related_connector_ids:
                        await self._create_connector_relationships(
                            topology_entity_id=topology_entity_id,
                            related_connector_ids=data.related_connector_ids,
                            tenant_id=data.tenant_id,
                        )
            except Exception as e:
                # Log but don't fail - topology registration is best-effort
                logger.warning(
                    f"Failed to register connector '{data.name}' as topology entity: {e}"
                )

        return connector

    async def _create_connector_topology_entity(
        self,
        connector_id: str,
        name: str,
        connector_type: str,
        base_url: str,
        description: str | None,
        tenant_id: str,
    ) -> UUID | None:
        """
        Create a topology entity representing this connector.

        Returns the topology entity ID or None if creation fails.
        """
        from meho_app.modules.topology.schemas import (
            StoreDiscoveryInput,
            TopologyEntityCreate,
        )
        from meho_app.modules.topology.service import TopologyService

        target_host = extract_target_host(base_url)
        entity_description = _build_connector_entity_description(
            name=name,
            connector_type=connector_type,
            target_host=target_host,
            description=description,
        )

        topology_service = TopologyService(self.session)

        # Create the entity via store_discovery
        entity_create = TopologyEntityCreate(
            name=name,
            entity_type="Connector",
            connector_type=connector_type,
            connector_id=UUID(connector_id),
            connector_name=name,
            description=entity_description,
            raw_attributes={
                "connector_type": connector_type,
                "base_url": base_url,
                "target_host": target_host,
                "is_connector_entity": True,
            },
        )

        result = await topology_service.store_discovery(
            StoreDiscoveryInput(
                connector_type=connector_type,
                connector_id=UUID(connector_id),
                entities=[entity_create],
            ),
            tenant_id=tenant_id,
        )

        if result.stored and result.entities_created > 0:
            # Retrieve the created entity to get its ID
            from meho_app.modules.topology.repository import TopologyRepository

            repo = TopologyRepository(self.session)
            entity = await repo.get_entity_by_name(
                name=name,
                tenant_id=tenant_id,
                connector_id=UUID(connector_id),
            )
            return entity.id if entity else None

        return None

    async def _create_connector_relationships(
        self,
        topology_entity_id: UUID,
        related_connector_ids: list[str],
        tenant_id: str,
    ) -> int:
        """
        Create topology relationships between this connector and related connectors.

        Creates `related_to` relationships for cross-connector topology correlation.
        E.g., GKE connector related_to GCP connector.

        Skips relationships that already exist (idempotent).

        Returns the number of relationships created.
        """
        from meho_app.modules.topology.repository import TopologyRepository

        if self.session is None:
            return 0

        repo = TopologyRepository(self.session)
        relationships_created = 0

        for related_id in related_connector_ids:
            try:
                # Get the related connector
                related_connector = await self.connector_repo.get_connector(related_id, tenant_id)

                if not related_connector:
                    logger.warning(
                        f"Related connector {related_id} not found, skipping relationship"
                    )
                    continue

                if not related_connector.topology_entity_id:
                    logger.warning(
                        f"Related connector '{related_connector.name}' has no topology entity, skipping relationship"
                    )
                    continue

                to_entity_id = UUID(related_connector.topology_entity_id)

                # Check if relationship already exists
                existing = await repo.get_relationship(
                    from_entity_id=topology_entity_id,
                    to_entity_id=to_entity_id,
                    relationship_type="related_to",
                )

                if existing:
                    logger.debug(
                        f"Relationship already exists: {topology_entity_id} --related_to--> {related_connector.name}"
                    )
                    continue

                # Create the relationship: this connector → related_to → related connector
                await repo.create_relationship(
                    from_entity_id=topology_entity_id,
                    to_entity_id=to_entity_id,
                    relationship_type="related_to",
                )
                relationships_created += 1

                logger.info(
                    f"Created connector relationship: topology_entity {topology_entity_id} "
                    f"--related_to--> {related_connector.name}"
                )

            except Exception as e:
                logger.warning(f"Failed to create relationship to connector {related_id}: {e}")
                continue

        return relationships_created

    async def get_connector(
        self, connector_id: str, tenant_id: str | None = None
    ) -> Connector | None:
        """Get connector by ID."""
        return await self.connector_repo.get_connector(connector_id, tenant_id)

    async def list_connectors(
        self, tenant_id: str, active_only: bool = True
    ) -> list[Connector]:  # NOSONAR (cognitive complexity)
        """List connectors for a tenant."""
        return await self.connector_repo.list_connectors(tenant_id, active_only)

    async def update_connector(
        self, connector_id: str, data: ConnectorUpdate, tenant_id: str | None = None
    ) -> Connector | None:
        """
        Update connector configuration and sync topology entity if needed.

        If base_url or name changes, the topology entity description is updated
        to maintain accurate cross-connector correlation.
        """
        # Get existing connector to check for changes
        existing = await self.connector_repo.get_connector(connector_id, tenant_id)
        if not existing:
            return None

        # Update the connector
        updated = await self.connector_repo.update_connector(connector_id, data, tenant_id)
        if not updated:
            return None

        # Sync topology entity if base_url or name changed
        if self.session is not None and existing.topology_entity_id:
            base_url_changed = data.base_url is not None and data.base_url != existing.base_url
            name_changed = data.name is not None and data.name != existing.name
            desc_changed = data.description is not None and data.description != existing.description

            if base_url_changed or name_changed or desc_changed:
                try:
                    await self._update_connector_topology_entity(
                        topology_entity_id=UUID(existing.topology_entity_id),
                        name=updated.name,
                        connector_type=updated.connector_type,
                        base_url=updated.base_url,
                        description=updated.description,
                        tenant_id=updated.tenant_id,
                    )
                    logger.info(f"Updated topology entity for connector '{updated.name}'")
                except Exception as e:
                    logger.warning(
                        f"Failed to update topology entity for connector '{updated.name}': {e}"
                    )

            # Handle related_connector_ids changes
            # Note: This adds new relationships but doesn't remove old ones
            # (relationship cleanup would require tracking which were created by this feature)
            if data.related_connector_ids is not None:
                try:
                    await self._create_connector_relationships(
                        topology_entity_id=UUID(existing.topology_entity_id),
                        related_connector_ids=data.related_connector_ids,
                        tenant_id=updated.tenant_id,
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to update connector relationships for '{updated.name}': {e}"
                    )

                # Phase 15: Trigger batch entity resolution for newly linked connectors
                try:
                    await self._run_batch_entity_resolution(
                        connector_id=connector_id,
                        related_connector_ids=data.related_connector_ids,
                        tenant_id=updated.tenant_id,
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to run batch entity resolution for '{updated.name}': {e}"
                    )

        return updated

    async def _update_connector_topology_entity(
        self,
        topology_entity_id: UUID,
        name: str,
        connector_type: str,
        base_url: str,
        description: str | None,
        tenant_id: str,
    ) -> None:
        """
        Update the topology entity for a connector.

        Called when connector base_url, name, or description changes.
        """
        from datetime import datetime

        from meho_app.modules.topology.repository import TopologyRepository

        target_host = extract_target_host(base_url)
        entity_description = _build_connector_entity_description(
            name=name,
            connector_type=connector_type,
            target_host=target_host,
            description=description,
        )

        repo = TopologyRepository(self.session)
        entity = await repo.get_entity_by_id(topology_entity_id)

        if entity:
            # Update entity fields directly
            entity.name = name
            entity.description = entity_description
            entity.raw_attributes = {
                "connector_type": connector_type,
                "base_url": base_url,
                "target_host": target_host,
                "is_connector_entity": True,
            }
            entity.last_verified_at = datetime.now(tz=UTC)
            await self.session.flush()

    async def _run_batch_entity_resolution(
        self,
        connector_id: str,
        related_connector_ids: list[str],
        tenant_id: str,
    ) -> None:
        """
        Run batch entity resolution when connectors are linked.

        Loads all entities from the updated connector and each newly linked
        connector, then runs deterministic resolution across all pairs.
        Results appear immediately (synchronous, no background queue).

        Args:
            connector_id: The connector being updated
            related_connector_ids: New list of related connector IDs
            tenant_id: Tenant ID for scoping
        """
        if not self.session:
            logger.warning("Cannot run batch entity resolution without database session")
            return

        from uuid import UUID as _UUID

        from meho_app.modules.topology.service import TopologyService

        topology_service = TopologyService(self.session)

        result = await topology_service.batch_resolve(
            connector_id=_UUID(connector_id),
            related_connector_ids=related_connector_ids,
            tenant_id=tenant_id,
        )

        if result.get("matches_found", 0) > 0:
            logger.info(
                f"Batch entity resolution for connector '{connector_id}': "
                f"{result['matches_found']} matches, "
                f"{result.get('same_as_created', 0)} SAME_AS, "
                f"{result.get('suggestions_created', 0)} suggestions"
            )

    async def delete_connector(self, connector_id: str, tenant_id: str | None = None) -> bool:
        """
        Delete a connector and all associated topology entities.

        This ensures that when a connector is deleted:
        1. All topology entities learned for this connector are removed
        2. The connector itself is deleted (with cascade to specs, endpoints, credentials)
        """
        # First, delete associated topology entities
        # This must happen before connector deletion to avoid orphans
        if self.session is not None:
            try:
                from meho_app.modules.topology.service import TopologyService

                topology_service = TopologyService(self.session)
                count = await topology_service.delete_entities_for_connector(UUID(connector_id))
                if count > 0:
                    logger.info(f"Deleted {count} topology entities for connector {connector_id}")
            except Exception as e:
                # Log but don't fail - topology cleanup is best-effort
                # The connector can still be deleted even if topology cleanup fails
                logger.warning(f"Failed to cleanup topology for connector {connector_id}: {e}")

        # Then delete the connector itself
        return await self.connector_repo.delete_connector(connector_id, tenant_id)

    # =========================================================================
    # Credential Management
    # =========================================================================

    async def store_credentials(self, user_id: str, credential: UserCredentialProvide) -> None:
        """Store user credentials for a connector."""
        if self.credential_repo is None:
            raise ValueError("Credential repository not available")
        await self.credential_repo.store_credentials(user_id, credential)

    async def get_credentials(self, user_id: str, connector_id: str) -> dict | None:
        """Get decrypted credentials for a user-connector pair."""
        if self.credential_repo is None:
            return None
        return await self.credential_repo.get_credentials(user_id, connector_id)

    async def delete_credentials(self, user_id: str, connector_id: str) -> bool:
        """Delete user credentials for a connector."""
        if self.credential_repo is None:
            return False
        return await self.credential_repo.delete_credentials(user_id, connector_id)
