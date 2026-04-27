# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Background batch processor for topology auto-discovery.

Processes queued discovery messages in the background, storing
entities and relationships via the TopologyService.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from meho_app.core.otel import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from meho_app.modules.topology.auto_discovery.queue import DiscoveryMessage, DiscoveryQueue
    from meho_app.modules.topology.repository import TopologyRepository

logger = get_logger(__name__)


class BatchProcessor:
    """
    Background processor for topology discovery messages.

    Runs in a background loop, periodically pulling messages from the
    DiscoveryQueue and storing them via TopologyService.

    Features:
    - Configurable batch size and interval
    - Graceful shutdown
    - Error handling with continued processing
    - Statistics tracking
    - Automatic correlation detection for stored entities (Phase 2)

    Usage:
        from meho_app.database import get_session_maker
        from meho_app.modules.topology.embedding import get_topology_embedding_service

        queue = DiscoveryQueue(redis_client)
        processor = BatchProcessor(
            queue=queue,
            session_maker=get_session_maker(),
            batch_size=100,
            interval_seconds=5,
        )

        # Start processing
        processor.start()

        # Later, stop gracefully
        await processor.stop()
    """

    def __init__(
        self,
        queue: DiscoveryQueue,
        session_maker: Callable,
        batch_size: int = 100,
        interval_seconds: int = 5,
        enable_correlation: bool = True,
    ):
        """
        Initialize the batch processor.

        Args:
            queue: Discovery queue to process from
            session_maker: Async session maker for database access
            batch_size: Maximum messages to process per batch
            interval_seconds: Time between processing cycles (also used as fallback)
            enable_correlation: Whether to check for correlations after storing
        """
        self.queue = queue
        self.session_maker = session_maker
        self.batch_size = batch_size
        self.interval_seconds = interval_seconds
        self.enable_correlation = enable_correlation

        self._running = False
        self._task: asyncio.Task | None = None
        self._wakeup_event: asyncio.Event = asyncio.Event()

        # Statistics
        self._entities_processed = 0
        self._relationships_processed = 0
        self._messages_processed = 0
        self._suggestions_created = 0
        self._errors = 0

    @property
    def is_running(self) -> bool:
        """Check if processor is running."""
        return self._running

    @property
    def stats(self) -> dict:
        """Get processing statistics."""
        return {
            "entities_processed": self._entities_processed,
            "relationships_processed": self._relationships_processed,
            "messages_processed": self._messages_processed,
            "suggestions_created": self._suggestions_created,
            "errors": self._errors,
            "running": self._running,
            "correlation_enabled": self.enable_correlation,
        }

    def start(self) -> None:
        """
        Start the background processing loop.

        Creates an asyncio task that runs until stop() is called.
        """
        if self._running:
            logger.warning("BatchProcessor already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"BatchProcessor started: batch_size={self.batch_size}, "
            f"interval={self.interval_seconds}s"
        )

    async def stop(self, timeout: float = 10.0) -> None:  # noqa: ASYNC109 -- timeout handled at caller level
        """
        Stop the background processing loop.

        Waits for the current batch to complete before stopping.

        Args:
            timeout: Maximum seconds to wait for shutdown
        """
        if not self._running:
            logger.warning("BatchProcessor not running")
            return

        self._running = False

        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except TimeoutError:
                logger.warning("BatchProcessor shutdown timed out, cancelling task")
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

        logger.info(
            f"BatchProcessor stopped: processed {self._messages_processed} messages, "
            f"{self._entities_processed} entities, {self._relationships_processed} relationships"
        )

    def trigger(self) -> None:
        """
        Wake up the processor to process queue immediately.

        Called when items are enqueued to avoid waiting for the next
        polling interval. If the processor is not running, this is a no-op.
        """
        if self._running:
            self._wakeup_event.set()

    async def _run_loop(self) -> None:
        """Main processing loop."""
        while self._running:
            try:
                processed = await self._process_batch()
                if processed > 0:
                    logger.debug(f"Processed {processed} discovery messages")
            except Exception as e:
                logger.error(f"Error in batch processing loop: {e}")
                self._errors += 1

            # Wait for next cycle OR immediate trigger
            if self._running:
                self._wakeup_event.clear()
                try:
                    await asyncio.wait_for(self._wakeup_event.wait(), timeout=self.interval_seconds)
                    logger.debug("BatchProcessor woken up by trigger")
                except TimeoutError:
                    pass  # Normal timeout, proceed to next batch

    async def _process_batch(self) -> int:
        """
        Process one batch of discovery messages.

        Returns:
            Number of messages processed
        """
        # Pop messages from queue
        messages = await self.queue.pop_batch(self.batch_size)
        if not messages:
            return 0

        processed = 0

        for message in messages:
            try:
                await self._process_message(message)
                processed += 1
                self._messages_processed += 1
            except Exception as e:
                logger.error(f"Failed to process discovery message: {e}")
                self._errors += 1
                # Continue processing other messages
                continue

        return processed

    async def _process_message(self, message: DiscoveryMessage) -> None:
        """
        Process a single discovery message.

        Stores entities and relationships via TopologyService,
        then checks for correlations with connector targets.
        """
        # Import here to avoid circular imports
        from meho_app.modules.topology.schemas import (
            StoreDiscoveryInput,
            TopologyEntityCreate,
            TopologyRelationshipCreate,
        )
        from meho_app.modules.topology.service import TopologyService

        async with self.session_maker() as session:
            service = TopologyService(session)

            # Convert extracted entities to TopologyEntityCreate
            entity_creates = []
            for entity in message.entities:
                cid = None
                if entity.connector_id:
                    from uuid import UUID as _UUID

                    with contextlib.suppress(ValueError, AttributeError):
                        cid = _UUID(str(entity.connector_id))
                entity_creates.append(
                    TopologyEntityCreate(
                        name=entity.name,
                        entity_type=entity.entity_type,
                        connector_type=(
                            getattr(entity, "connector_type", None) or message.connector_type
                        ),
                        description=entity.description,
                        connector_id=cid,
                        connector_name=entity.connector_name,
                        canonical_id=getattr(entity, "canonical_id", None),
                        scope=entity.scope,
                        raw_attributes=entity.raw_attributes,
                    )
                )

            # Convert extracted relationships to TopologyRelationshipCreate
            relationship_creates = []
            for rel in message.relationships:
                relationship_creates.append(
                    TopologyRelationshipCreate(
                        from_entity_name=rel.from_entity_name,
                        to_entity_name=rel.to_entity_name,
                        relationship_type=rel.relationship_type,
                    )
                )

            # Note: We don't create "discovered_by" edges to connectors here.
            # The connector_id and connector_name metadata on entities is sufficient
            # for MEHO to know which connector to query. Explicit edges would clutter
            # the topology UI with 100s of lines for large deployments.

            # Store via TopologyService
            input_data = StoreDiscoveryInput(
                connector_type=message.connector_type,
                connector_id=None,
                entities=entity_creates,
                relationships=relationship_creates,
                same_as=[],
            )

            result = await service.store_discovery(input_data, message.tenant_id)

            if result.stored:
                self._entities_processed += result.entities_created
                self._relationships_processed += result.relationships_created

                if result.entities_created > 0 or result.relationships_created > 0:
                    logger.info(
                        f"Auto-discovery stored: {result.entities_created} entities, "
                        f"{result.relationships_created} relationships for tenant {message.tenant_id}"
                    )

                # Phase 15: Deterministic entity resolution for newly stored entities
                await self._run_deterministic_resolution(
                    session=session,
                    message=message,
                    tenant_id=message.tenant_id,
                )

                # Kubernetes deferred correlation: Service → Pod via label selectors
                k8s_rels_created = await self._correlate_k8s_services(
                    session=session,
                    message=message,
                    tenant_id=message.tenant_id,
                )
                if k8s_rels_created > 0:
                    self._relationships_processed += k8s_rels_created

                # Phase 2: Check for correlations with connector targets
                if self.enable_correlation:
                    await self._check_correlations(
                        session=session,
                        message=message,
                        tenant_id=message.tenant_id,
                    )
            else:
                logger.warning(f"Failed to store discovery: {result.message}")

    async def _correlate_k8s_services(  # NOSONAR (cognitive complexity)
        self,
        session: AsyncSession,
        message: DiscoveryMessage,
        tenant_id: str,
    ) -> int:
        """
        Correlate K8s Services to Pods via label selector matching.

        Called after storing entities to create "routes_to" relationships
        between Services and the Pods they select.

        Returns:
            Number of relationships created
        """
        from meho_app.modules.topology.repository import TopologyRepository
        from meho_app.modules.topology.schemas import (
            StoreDiscoveryInput,
            TopologyRelationshipCreate,
        )
        from meho_app.modules.topology.service import TopologyService

        repository = TopologyRepository(session)
        service = TopologyService(session)

        # Find Services with selectors in this batch
        services_with_selectors = []
        for entity in message.entities:
            attrs = entity.raw_attributes or {}
            if attrs.get("_k8s_selector") and attrs.get("_k8s_namespace"):
                services_with_selectors.append(
                    {
                        "name": entity.name,
                        "selector": attrs["_k8s_selector"],
                        "namespace": attrs["_k8s_namespace"],
                        "connector_id": entity.connector_id,
                    }
                )

        if not services_with_selectors:
            return 0

        # Find all Pods for this connector/tenant
        relationships_to_create: list[TopologyRelationshipCreate] = []

        # Query all entities from the same connector that might be pods
        for svc in services_with_selectors:
            try:
                # Find pods that match this service's selector
                # Pods have _k8s_labels and _k8s_namespace in their raw_attributes
                matching_pods = await self._find_matching_pods(
                    repository=repository,
                    selector=svc["selector"],
                    namespace=svc["namespace"],
                    connector_id=svc["connector_id"],
                    tenant_id=tenant_id,
                )

                for pod_name in matching_pods:
                    relationships_to_create.append(
                        TopologyRelationshipCreate(
                            from_entity_name=svc["name"],
                            to_entity_name=pod_name,
                            relationship_type="routes_to",
                        )
                    )

            except Exception as e:
                logger.warning(f"Error correlating service {svc['name']} to pods: {e}")
                continue

        if not relationships_to_create:
            return 0

        # Store the relationships (with connector_id for identity-quad entity lookup)
        svc_connector_id = (
            services_with_selectors[0]["connector_id"] if services_with_selectors else None
        )
        result = await service.store_discovery(
            StoreDiscoveryInput(
                connector_type=message.connector_type,
                connector_id=svc_connector_id,
                relationships=relationships_to_create,
            ),
            tenant_id=tenant_id,
        )

        if result.stored and result.relationships_created > 0:
            logger.info(
                f"K8s correlation: Created {result.relationships_created} Service→Pod routes_to relationships"
            )
            return result.relationships_created

        return 0

    async def _find_matching_pods(  # NOSONAR (cognitive complexity)
        self,
        repository: TopologyRepository,
        selector: dict[str, str],
        namespace: str,
        connector_id: str | None,
        tenant_id: str,
    ) -> list[str]:
        """
        Find pods that match a service selector within the same namespace.

        A pod matches if ALL selector labels match the pod's labels.

        Returns:
            List of pod names that match the selector
        """
        from uuid import UUID

        if not selector:
            return []

        # Use typed entity_type filter instead of text search for robustness
        connector_uuid = UUID(connector_id) if connector_id else None
        all_entities = await repository.get_entities_by_type(
            tenant_id=tenant_id,
            entity_type="K8s Pod",
            connector_id=connector_uuid,
            limit=1000,
        )

        matching_pods = []

        for entity in all_entities:
            attrs = entity.raw_attributes or {}

            # Check if this is a pod in the same namespace
            if attrs.get("_k8s_namespace") != namespace:
                continue

            # Check if it's from the same connector
            if connector_id:  # noqa: SIM102 -- readability preferred over collapse
                if entity.connector_id and str(entity.connector_id) != str(connector_id):
                    continue

            # Get pod labels
            pod_labels = attrs.get("_k8s_labels", {})
            if not pod_labels:
                continue

            # Check if all selector labels match pod labels
            matches = all(pod_labels.get(key) == value for key, value in selector.items())

            if matches:
                matching_pods.append(entity.name)

        return matching_pods

    async def _check_correlations(  # NOSONAR (cognitive complexity)
        self,
        session: AsyncSession,
        message: DiscoveryMessage,
        tenant_id: str,
    ) -> int:
        """
        Check stored entities for correlations with connector targets.

        Called after storing entities to detect matches between
        entity hostnames/IPs and connector targets.

        Returns:
            Number of suggestions created
        """
        from meho_app.modules.topology.hostname_matcher import HostnameMatcher
        from meho_app.modules.topology.repository import TopologyRepository

        matcher = HostnameMatcher(session)
        repository = TopologyRepository(session)

        suggestions_created = 0

        for entity in message.entities:
            try:
                # Build canonical_id from scope + name (matches create_entity logic)
                canonical_id = entity.name
                if entity.scope:
                    scope_parts = [str(v) for v in entity.scope.values()]
                    canonical_id = "/".join([*scope_parts, entity.name])

                # Use canonical_id lookup (unique) instead of name-only (not unique)
                from uuid import UUID as _UUID

                _connector_id = _UUID(entity.connector_id) if entity.connector_id else None
                stored_entity = await repository.get_entity_by_canonical_id(
                    tenant_id=tenant_id,
                    connector_id=_connector_id,
                    entity_type=entity.entity_type,
                    canonical_id=canonical_id,
                )

                if not stored_entity:
                    continue

                # Check if entity is correlatable
                if not matcher.is_correlatable_entity(stored_entity):
                    continue

                # Check for correlations
                suggestions = await matcher.check_entity_correlations(
                    entity=stored_entity,
                    tenant_id=tenant_id,
                )

                suggestions_created += len(suggestions)

            except Exception as e:
                logger.warning(f"Error checking correlations for entity {entity.name}: {e}")
                continue

        if suggestions_created > 0:
            self._suggestions_created += suggestions_created
            await session.commit()
            logger.info(
                f"Created {suggestions_created} correlation suggestions for tenant {tenant_id}"
            )

        return suggestions_created

    async def _run_deterministic_resolution(  # NOSONAR (cognitive complexity)
        self,
        session: AsyncSession,
        message: DiscoveryMessage,
        tenant_id: str,
    ) -> int:
        """
        Run deterministic entity resolution for newly discovered entities.

        For each entity in the discovery message, check if it matches any
        entity from a related connector using providerID, IP, or hostname.
        This runs BEFORE the existing LLM-based correlation check.

        Returns:
            Number of SAME_AS relationships or suggestions created
        """
        from uuid import UUID

        from meho_app.modules.connectors.repositories.connector_repository import (
            ConnectorRepository,
        )
        from meho_app.modules.topology.repository import TopologyRepository
        from meho_app.modules.topology.service import TopologyService

        service = TopologyService(session)
        repository = TopologyRepository(session)
        connector_repo = ConnectorRepository(session)

        matches_created = 0

        for entity in message.entities:
            try:
                if not entity.connector_id:
                    continue

                # Get the connector to find related_connector_ids
                connector = await connector_repo.get_connector(str(entity.connector_id), tenant_id)
                if not connector or not connector.related_connector_ids:
                    continue

                # Look up the stored entity (need the model with ID for SAME_AS creation)
                # Build canonical_id the same way as processor._process_message
                canonical_id = entity.name
                if entity.scope:
                    scope_parts = [str(v) for v in entity.scope.values()]
                    canonical_id = "/".join([*scope_parts, entity.name])

                stored_entity = await repository.get_entity_by_canonical_id(
                    tenant_id=tenant_id,
                    connector_id=UUID(entity.connector_id)
                    if isinstance(entity.connector_id, str)
                    else entity.connector_id,
                    entity_type=entity.entity_type,
                    canonical_id=canonical_id,
                )

                if not stored_entity:
                    continue

                # Phase 15: Remove stale SAME_AS relationships before creating new ones
                # If entity attributes changed (IP, hostname, providerID), existing
                # matches may no longer be valid
                stale_removed = await service._remove_stale_same_as(
                    entity=stored_entity,
                    tenant_id=tenant_id,
                )
                if stale_removed > 0:
                    logger.info(f"Removed {stale_removed} stale SAME_AS for {entity.name}")

                # For each related connector, find candidate entities and try resolution
                for related_id_str in connector.related_connector_ids:
                    try:
                        related_connector_id = UUID(related_id_str)
                    except (ValueError, AttributeError):
                        continue

                    # Get entities from the related connector (limited set)
                    related_entities = await repository.get_entities_by_connector(
                        connector_id=related_connector_id,
                        tenant_id=tenant_id,
                        limit=200,
                    )

                    for candidate in related_entities:
                        # Skip same connector (should not happen, but safety)
                        if candidate.connector_id == stored_entity.connector_id:
                            continue

                        result = await service.resolve_entity_pair(
                            entity_a=stored_entity,
                            entity_b=candidate,
                            tenant_id=tenant_id,
                        )
                        if result:
                            matches_created += 1

            except Exception as e:
                logger.warning(f"Error in deterministic resolution for entity {entity.name}: {e}")
                continue

        if matches_created > 0:
            await session.commit()
            logger.info(
                f"Deterministic resolution: {matches_created} matches for tenant {tenant_id}"
            )

        return matches_created

    async def process_one(self) -> bool:
        """
        Process a single message immediately (for testing).

        Returns:
            True if a message was processed, False if queue was empty
        """
        messages = await self.queue.pop_batch(1)
        if not messages:
            return False

        await self._process_message(messages[0])
        self._messages_processed += 1
        return True

    def reset_stats(self) -> None:
        """Reset processing statistics."""
        self._entities_processed = 0
        self._relationships_processed = 0
        self._messages_processed = 0
        self._suggestions_created = 0
        self._errors = 0


# =============================================================================
# Singleton / Factory
# =============================================================================

_processor_instance: BatchProcessor | None = None


def get_batch_processor(
    queue: DiscoveryQueue | None = None,
    session_maker: Callable | None = None,
    batch_size: int = 100,
    interval_seconds: int = 5,
    enable_correlation: bool = True,
) -> BatchProcessor:
    """
    Get or create the batch processor singleton.

    Args:
        queue: Discovery queue (required for first call)
        session_maker: Database session maker (required for first call)
        batch_size: Maximum messages per batch
        interval_seconds: Processing interval
        enable_correlation: Whether to check for correlations (Phase 2)

    Returns:
        BatchProcessor instance

    Raises:
        ValueError: If queue or session_maker not provided on first call
    """
    global _processor_instance

    if _processor_instance is None:
        if queue is None:
            raise ValueError("queue is required for first batch processor initialization")
        if session_maker is None:
            raise ValueError("session_maker is required for first batch processor initialization")

        _processor_instance = BatchProcessor(
            queue=queue,
            session_maker=session_maker,
            batch_size=batch_size,
            interval_seconds=interval_seconds,
            enable_correlation=enable_correlation,
        )

    return _processor_instance


def reset_batch_processor() -> None:
    """Reset the processor singleton (for testing)."""
    global _processor_instance
    _processor_instance = None


def get_processor_instance() -> BatchProcessor | None:
    """
    Get the current processor instance if it exists.

    This allows the auto-discovery service to trigger immediate
    processing after enqueueing items, without requiring initialization
    parameters.

    Returns:
        BatchProcessor instance if initialized, None otherwise
    """
    return _processor_instance
