# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Import Operations Sync (TASK-142 Phase 9)

Syncs operations for connectors after import.
Handles typed connectors (vmware, proxmox, gcp) and dynamic connectors (rest, soap).

For typed connectors, operations are synced from code definitions.
For REST/SOAP with URL configs, operations are fetched from specs.
Network errors produce warnings, not failures.
"""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger

if TYPE_CHECKING:
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

logger = get_logger(__name__)


@dataclass
class ImportOperationsSyncResult:
    """Result of syncing operations for an imported connector."""

    operations_synced: int = 0
    types_synced: int = 0
    knowledge_chunks_created: int = 0
    warning: str | None = None
    success: bool = True


async def sync_operations_for_imported_connector(  # NOSONAR (cognitive complexity)
    session: AsyncSession,
    connector_id: str,
    connector_type: str,
    tenant_id: str,
    connector_name: str,
    protocol_config: dict[str, Any] | None = None,
) -> ImportOperationsSyncResult:
    """
    Sync operations after importing a connector.

    Dispatches to appropriate sync function based on connector_type:
    - vmware, proxmox, gcp: Sync from code definitions (always works)
    - rest with openapi_url: Fetch and parse OpenAPI spec (may fail)
    - soap with wsdl_url: Fetch and parse WSDL (may fail)
    - rest/soap without URL: Skip (user will upload manually)

    Args:
        session: AsyncSession for database operations
        connector_id: UUID of the imported connector
        connector_type: Type of connector (vmware, proxmox, gcp, rest, soap)
        tenant_id: Tenant identifier
        connector_name: Display name for logging
        protocol_config: Protocol-specific config (may contain openapi_url, wsdl_url)

    Returns:
        ImportOperationsSyncResult with sync counts and optional warning
    """
    result = ImportOperationsSyncResult()

    try:
        if connector_type == "vmware":
            result = await _sync_vmware_operations(session, connector_id, tenant_id, connector_name)
        elif connector_type == "proxmox":
            result = await _sync_proxmox_operations(
                session, connector_id, tenant_id, connector_name
            )
        elif connector_type == "gcp":
            result = await _sync_gcp_operations(session, connector_id, tenant_id, connector_name)
        elif connector_type == "rest":
            result = await _sync_rest_operations(
                session, connector_id, tenant_id, connector_name, protocol_config
            )
        elif connector_type == "soap":
            result = await _sync_soap_operations(
                session, connector_id, tenant_id, connector_name, protocol_config
            )
        elif connector_type == "kubernetes":
            result = await _sync_kubernetes_operations(
                session, connector_id, tenant_id, connector_name
            )
        elif connector_type == "prometheus":
            result = await _sync_prometheus_operations(
                session, connector_id, tenant_id, connector_name
            )
        elif connector_type == "loki":
            result = await _sync_loki_operations(session, connector_id, tenant_id, connector_name)
        elif connector_type == "tempo":
            result = await _sync_tempo_operations(session, connector_id, tenant_id, connector_name)
        elif connector_type == "alertmanager":
            result = await _sync_alertmanager_operations(
                session, connector_id, tenant_id, connector_name
            )
        elif connector_type == "jira":
            result = await _sync_jira_operations(session, connector_id, tenant_id, connector_name)
        elif connector_type == "confluence":
            result = await _sync_confluence_operations(
                session, connector_id, tenant_id, connector_name
            )
        elif connector_type == "email":
            result = await _sync_email_operations(session, connector_id, tenant_id, connector_name)
        elif connector_type == "argocd":
            result = await _sync_argocd_operations(session, connector_id, tenant_id, connector_name)
        elif connector_type == "github":
            result = await _sync_github_operations(session, connector_id, tenant_id, connector_name)
        elif connector_type == "aws":
            result = await _sync_aws_operations(session, connector_id, tenant_id, connector_name)
        elif connector_type == "azure":
            result = await _sync_azure_operations(session, connector_id, tenant_id, connector_name)
        else:
            # graphql, grpc - no auto-sync available
            logger.info(f"No operations sync available for connector type '{connector_type}'")
            result.warning = f"No automatic operations sync for '{connector_type}' connectors"

    except Exception as e:
        logger.error(f"Operations sync failed for {connector_name}: {e}", exc_info=True)
        result.success = False
        result.warning = f"Operations sync failed: {e!s}"
        return result

    # Generate skill for connectors without built-in skills.
    # Typed connectors with entries in TYPE_SKILL_MAP (kubernetes, vmware, proxmox, gcp)
    # already have hand-tuned skills that are higher quality than generated ones.
    from meho_app.modules.agents.factory import TYPE_SKILL_MAP

    if connector_type not in TYPE_SKILL_MAP:
        try:
            from meho_app.modules.connectors.skill_generation import SkillGenerator

            generator = SkillGenerator()
            skill_result = await generator.generate_skill(
                session=session,
                connector_id=connector_id,
                connector_type=connector_type,
                connector_name=connector_name,
            )
            logger.info(
                f"Generated skill for typed connector {connector_name}: "
                f"quality={skill_result.quality_score}/5"
            )
        except Exception as e:
            logger.warning(f"Failed to generate skill for {connector_name}: {e}")

    return result


def _get_knowledge_store(session: AsyncSession) -> Optional["KnowledgeStore"]:
    """Create a KnowledgeStore instance for creating knowledge chunks."""
    try:
        from meho_app.modules.knowledge.embeddings import get_embedding_provider
        from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
        from meho_app.modules.knowledge.repository import KnowledgeRepository

        knowledge_repo = KnowledgeRepository(session)
        embedding_provider = get_embedding_provider()  # Voyage AI 1024D singleton
        return KnowledgeStore(
            repository=knowledge_repo,
            embedding_provider=embedding_provider,
        )
    except Exception as e:
        logger.warning(f"Failed to create KnowledgeStore: {e}")
        return None


# =============================================================================
# Typed Connector Sync Functions
# =============================================================================


async def _sync_vmware_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync VMware operations from code definitions."""
    from meho_app.modules.connectors.repositories import ConnectorTypeRepository
    from meho_app.modules.connectors.schemas import ConnectorEntityTypeCreate
    from meho_app.modules.connectors.vmware import VMWARE_TYPES
    from meho_app.modules.connectors.vmware.sync import sync_vmware_operations_if_needed

    result = ImportOperationsSyncResult()

    try:
        # Get knowledge store for creating searchable chunks
        knowledge_store = _get_knowledge_store(session)

        # Sync operations (uses existing sync function)
        added, updated, chunks_created = await sync_vmware_operations_if_needed(
            session=session,
            connector_id=connector_id,
            tenant_id=tenant_id,
            current_version=None,  # Force full sync
            knowledge_store=knowledge_store,
            connector_name=connector_name,
        )

        result.operations_synced = added + updated
        result.knowledge_chunks_created = chunks_created

        # Also sync entity types
        type_repo = ConnectorTypeRepository(session)
        type_creates = []
        for t in VMWARE_TYPES:
            prop_names = " ".join(p.get("name", "") for p in t.properties)
            search_content = f"{t.type_name} {t.description} {t.category} {prop_names}"
            type_creates.append(
                ConnectorEntityTypeCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    type_name=t.type_name,
                    description=t.description,
                    category=t.category,
                    properties=list(t.properties),
                    search_content=search_content,
                )
            )

        if type_creates:
            result.types_synced = await type_repo.create_types_bulk(type_creates)

        logger.info(
            f"✅ Synced VMware connector '{connector_name}': "
            f"{result.operations_synced} operations, {result.types_synced} types, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"VMware operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"VMware operations sync failed: {e!s}"

    return result


async def _sync_proxmox_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync Proxmox operations from code definitions."""
    from meho_app.modules.connectors.proxmox import PROXMOX_TYPES
    from meho_app.modules.connectors.proxmox.sync import (
        sync_proxmox_operations_if_needed,
    )
    from meho_app.modules.connectors.repositories import ConnectorTypeRepository
    from meho_app.modules.connectors.schemas import ConnectorEntityTypeCreate

    result = ImportOperationsSyncResult()

    try:
        # Get knowledge store for creating searchable chunks
        knowledge_store = _get_knowledge_store(session)

        # Sync operations (uses existing sync function)
        added, updated, chunks_created = await sync_proxmox_operations_if_needed(
            session=session,
            connector_id=connector_id,
            tenant_id=tenant_id,
            current_version=None,  # Force full sync
            knowledge_store=knowledge_store,
            connector_name=connector_name,
        )

        result.operations_synced = added + updated
        result.knowledge_chunks_created = chunks_created

        # Also sync entity types
        type_repo = ConnectorTypeRepository(session)
        type_creates = []
        for t in PROXMOX_TYPES:
            prop_names = " ".join(p.get("name", "") for p in t.properties)
            search_content = f"{t.type_name} {t.description} {t.category} {prop_names}"
            type_creates.append(
                ConnectorEntityTypeCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    type_name=t.type_name,
                    description=t.description,
                    category=t.category,
                    properties=list(t.properties),
                    search_content=search_content,
                )
            )

        if type_creates:
            result.types_synced = await type_repo.create_types_bulk(type_creates)

        logger.info(
            f"✅ Synced Proxmox connector '{connector_name}': "
            f"{result.operations_synced} operations, {result.types_synced} types, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"Proxmox operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"Proxmox operations sync failed: {e!s}"

    return result


async def _sync_gcp_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync GCP operations from code definitions."""
    from meho_app.modules.connectors.gcp import GCP_TYPES
    from meho_app.modules.connectors.gcp.sync import sync_gcp_operations_if_needed
    from meho_app.modules.connectors.repositories import ConnectorTypeRepository
    from meho_app.modules.connectors.schemas import ConnectorEntityTypeCreate

    result = ImportOperationsSyncResult()

    try:
        # Get knowledge store for creating searchable chunks
        knowledge_store = _get_knowledge_store(session)

        # Sync operations (uses existing sync function)
        added, updated, chunks_created = await sync_gcp_operations_if_needed(
            session=session,
            connector_id=connector_id,
            tenant_id=tenant_id,
            current_version=None,  # Force full sync
            knowledge_store=knowledge_store,
            connector_name=connector_name,
        )

        result.operations_synced = added + updated
        result.knowledge_chunks_created = chunks_created

        # Also sync entity types
        type_repo = ConnectorTypeRepository(session)
        type_creates = []
        for t in GCP_TYPES:
            prop_names = " ".join(p.get("name", "") for p in t.properties)
            search_content = f"{t.type_name} {t.description} {t.category} {prop_names}"
            type_creates.append(
                ConnectorEntityTypeCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    type_name=t.type_name,
                    description=t.description,
                    category=t.category,
                    properties=list(t.properties),
                    search_content=search_content,
                )
            )

        if type_creates:
            result.types_synced = await type_repo.create_types_bulk(type_creates)

        logger.info(
            f"✅ Synced GCP connector '{connector_name}': "
            f"{result.operations_synced} operations, {result.types_synced} types, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"GCP operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"GCP operations sync failed: {e!s}"

    return result


async def _sync_aws_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync AWS operations from code definitions."""
    from meho_app.modules.connectors.aws import AWS_TYPES
    from meho_app.modules.connectors.aws.sync import sync_aws_operations_if_needed
    from meho_app.modules.connectors.repositories import ConnectorTypeRepository
    from meho_app.modules.connectors.schemas import ConnectorEntityTypeCreate

    result = ImportOperationsSyncResult()

    try:
        # Get knowledge store for creating searchable chunks
        knowledge_store = _get_knowledge_store(session)

        # Sync operations (uses existing sync function)
        added, updated, chunks_created = await sync_aws_operations_if_needed(
            session=session,
            connector_id=connector_id,
            tenant_id=tenant_id,
            current_version=None,  # Force full sync
            knowledge_store=knowledge_store,
            connector_name=connector_name,
        )

        result.operations_synced = added + updated
        result.knowledge_chunks_created = chunks_created

        # Also sync entity types
        type_repo = ConnectorTypeRepository(session)
        type_creates = []
        for t in AWS_TYPES:
            prop_names = " ".join(p.get("name", "") for p in t.properties)
            search_content = f"{t.type_name} {t.description} {t.category} {prop_names}"
            type_creates.append(
                ConnectorEntityTypeCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    type_name=t.type_name,
                    description=t.description,
                    category=t.category,
                    properties=list(t.properties),
                    search_content=search_content,
                )
            )

        if type_creates:
            result.types_synced = await type_repo.create_types_bulk(type_creates)

        logger.info(
            f"Synced AWS connector '{connector_name}': "
            f"{result.operations_synced} operations, {result.types_synced} types, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"AWS operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"AWS operations sync failed: {e!s}"

    return result


async def _sync_azure_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync Azure operations from code definitions."""
    from meho_app.modules.connectors.azure import AZURE_TYPES
    from meho_app.modules.connectors.azure.sync import sync_azure_operations_if_needed
    from meho_app.modules.connectors.repositories import ConnectorTypeRepository
    from meho_app.modules.connectors.schemas import ConnectorEntityTypeCreate

    result = ImportOperationsSyncResult()

    try:
        # Get knowledge store for creating searchable chunks
        knowledge_store = _get_knowledge_store(session)

        # Sync operations (uses existing sync function)
        added, updated, chunks_created = await sync_azure_operations_if_needed(
            session=session,
            connector_id=connector_id,
            tenant_id=tenant_id,
            current_version=None,  # Force full sync
            knowledge_store=knowledge_store,
            connector_name=connector_name,
        )

        result.operations_synced = added + updated
        result.knowledge_chunks_created = chunks_created

        # Also sync entity types
        type_repo = ConnectorTypeRepository(session)
        type_creates = []
        for t in AZURE_TYPES:
            prop_names = " ".join(p.get("name", "") for p in t.properties)
            search_content = f"{t.type_name} {t.description} {t.category} {prop_names}"
            type_creates.append(
                ConnectorEntityTypeCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    type_name=t.type_name,
                    description=t.description,
                    category=t.category,
                    properties=list(t.properties),
                    search_content=search_content,
                )
            )

        if type_creates:
            result.types_synced = await type_repo.create_types_bulk(type_creates)

        logger.info(
            f"Synced Azure connector '{connector_name}': "
            f"{result.operations_synced} operations, {result.types_synced} types, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"Azure operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"Azure operations sync failed: {e!s}"

    return result


async def _sync_kubernetes_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync Kubernetes operations from code definitions."""
    from meho_app.modules.connectors.kubernetes import KUBERNETES_TYPES
    from meho_app.modules.connectors.kubernetes.sync import sync_kubernetes_operations_if_needed
    from meho_app.modules.connectors.repositories import ConnectorTypeRepository
    from meho_app.modules.connectors.schemas import ConnectorEntityTypeCreate

    result = ImportOperationsSyncResult()

    try:
        knowledge_store = _get_knowledge_store(session)

        added, updated, chunks_created = await sync_kubernetes_operations_if_needed(
            session=session,
            connector_id=connector_id,
            tenant_id=tenant_id,
            current_version=None,
            knowledge_store=knowledge_store,
            connector_name=connector_name,
        )

        result.operations_synced = added + updated
        result.knowledge_chunks_created = chunks_created

        type_repo = ConnectorTypeRepository(session)
        type_creates = []
        for t in KUBERNETES_TYPES:
            prop_names = " ".join(p.get("name", "") for p in t.properties)
            search_content = f"{t.type_name} {t.description} {t.category} {prop_names}"
            type_creates.append(
                ConnectorEntityTypeCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    type_name=t.type_name,
                    description=t.description,
                    category=t.category,
                    properties=list(t.properties),
                    search_content=search_content,
                )
            )

        if type_creates:
            result.types_synced = await type_repo.create_types_bulk(type_creates)

        logger.info(
            f"Synced Kubernetes connector '{connector_name}': "
            f"{result.operations_synced} operations, {result.types_synced} types, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"Kubernetes operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"Kubernetes operations sync failed: {e!s}"

    return result


async def _sync_prometheus_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync Prometheus operations from code definitions."""
    from meho_app.modules.connectors.prometheus import PROMETHEUS_TYPES
    from meho_app.modules.connectors.repositories import (
        ConnectorOperationRepository,
        ConnectorTypeRepository,
    )
    from meho_app.modules.connectors.schemas import (
        ConnectorEntityTypeCreate,
        ConnectorOperationCreate,
    )

    result = ImportOperationsSyncResult()

    try:
        from meho_app.modules.connectors.prometheus import (
            PROMETHEUS_OPERATIONS,
        )

        # Register operations
        op_repo = ConnectorOperationRepository(session)
        op_creates = []
        for op in PROMETHEUS_OPERATIONS:
            search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
            op_creates.append(
                ConnectorOperationCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=list(op.parameters),
                    example=op.example,
                    search_content=search_content,
                )
            )

        if op_creates:
            result.operations_synced = await op_repo.create_operations_bulk(op_creates)

        # Register entity types
        type_repo = ConnectorTypeRepository(session)
        type_creates = []
        for t in PROMETHEUS_TYPES:
            prop_names = " ".join(p.get("name", "") for p in t.properties)
            search_content = f"{t.type_name} {t.description} {t.category} {prop_names}"
            type_creates.append(
                ConnectorEntityTypeCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    type_name=t.type_name,
                    description=t.description,
                    category=t.category,
                    properties=list(t.properties),
                    search_content=search_content,
                )
            )

        if type_creates:
            result.types_synced = await type_repo.create_types_bulk(type_creates)

        # Create knowledge chunks
        knowledge_store = _get_knowledge_store(session)
        if knowledge_store:
            try:
                from meho_app.modules.knowledge.schemas import (
                    ChunkMetadata,
                    KnowledgeChunkCreate,
                    KnowledgeType,
                )

                chunk_create = KnowledgeChunkCreate(
                    text=(
                        f"Prometheus connector '{connector_name}'. "
                        f"Monitors infrastructure metrics, service RED metrics, scrape targets, and alerts. "
                        f"Operations: {', '.join(op.operation_id for op in PROMETHEUS_OPERATIONS)}."
                    ),
                    tenant_id=tenant_id,
                    connector_id=str(connector_id),
                    tags=["connector", "prometheus"],
                    knowledge_type=KnowledgeType.DOCUMENTATION,
                    source_uri=f"connector://{connector_id}/operations",
                    search_metadata=ChunkMetadata.model_validate(
                        {
                            "connector_type": "prometheus",
                            "connector_name": connector_name,
                            "source_type": "connector_operations",
                        }
                    ),
                )
                await knowledge_store.add_chunk(chunk_create)
                result.knowledge_chunks_created = 1
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks for Prometheus: {e}")

        logger.info(
            f"Synced Prometheus connector '{connector_name}': "
            f"{result.operations_synced} operations, {result.types_synced} types, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"Prometheus operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"Prometheus operations sync failed: {e!s}"

    return result


async def _sync_loki_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync Loki operations from code definitions.

    Loki has no entity types (query-only connector), so only operations
    and knowledge chunks are synced.
    """
    from meho_app.modules.connectors.repositories import ConnectorOperationRepository
    from meho_app.modules.connectors.schemas import ConnectorOperationCreate

    result = ImportOperationsSyncResult()

    try:
        from meho_app.modules.connectors.loki import (
            LOKI_OPERATIONS,
        )

        # Register operations
        op_repo = ConnectorOperationRepository(session)
        op_creates = []
        for op in LOKI_OPERATIONS:
            search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
            op_creates.append(
                ConnectorOperationCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=list(op.parameters),
                    example=op.example,
                    search_content=search_content,
                )
            )

        if op_creates:
            result.operations_synced = await op_repo.create_operations_bulk(op_creates)

        # No type registration -- Loki is a query-only connector with no topology entities

        # Create knowledge chunks
        knowledge_store = _get_knowledge_store(session)
        if knowledge_store:
            try:
                from meho_app.modules.knowledge.schemas import (
                    ChunkMetadata,
                    KnowledgeChunkCreate,
                    KnowledgeType,
                )

                chunk_create = KnowledgeChunkCreate(
                    text=(
                        f"Loki connector '{connector_name}'. "
                        f"Provides log search, error investigation, volume analysis, "
                        f"pattern detection, and label discovery. "
                        f"Operations: {', '.join(op.operation_id for op in LOKI_OPERATIONS)}."
                    ),
                    tenant_id=tenant_id,
                    connector_id=str(connector_id),
                    tags=["connector", "loki"],
                    knowledge_type=KnowledgeType.DOCUMENTATION,
                    source_uri=f"connector://{connector_id}/operations",
                    search_metadata=ChunkMetadata.model_validate(
                        {
                            "connector_type": "loki",
                            "connector_name": connector_name,
                            "source_type": "connector_operations",
                        }
                    ),
                )
                await knowledge_store.add_chunk(chunk_create)
                result.knowledge_chunks_created = 1
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks for Loki: {e}")

        logger.info(
            f"Synced Loki connector '{connector_name}': "
            f"{result.operations_synced} operations, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"Loki operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"Loki operations sync failed: {e!s}"

    return result


async def _sync_tempo_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync Tempo operations from code definitions.

    Tempo has no entity types (query-only connector), so only operations
    and knowledge chunks are synced.
    """
    from meho_app.modules.connectors.repositories import ConnectorOperationRepository
    from meho_app.modules.connectors.schemas import ConnectorOperationCreate

    result = ImportOperationsSyncResult()

    try:
        from meho_app.modules.connectors.tempo import (
            TEMPO_OPERATIONS,
        )

        # Register operations
        op_repo = ConnectorOperationRepository(session)
        op_creates = []
        for op in TEMPO_OPERATIONS:
            search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
            op_creates.append(
                ConnectorOperationCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=list(op.parameters),
                    example=op.example,
                    search_content=search_content,
                )
            )

        if op_creates:
            result.operations_synced = await op_repo.create_operations_bulk(op_creates)

        # No type registration -- Tempo is a query-only connector with no topology entities

        # Create knowledge chunks
        knowledge_store = _get_knowledge_store(session)
        if knowledge_store:
            try:
                from meho_app.modules.knowledge.schemas import (
                    ChunkMetadata,
                    KnowledgeChunkCreate,
                    KnowledgeType,
                )

                chunk_create = KnowledgeChunkCreate(
                    text=(
                        f"Tempo connector '{connector_name}'. "
                        f"Provides distributed trace search, error trace analysis, "
                        f"latency investigation, service graph mapping, and tag discovery. "
                        f"Operations: {', '.join(op.operation_id for op in TEMPO_OPERATIONS)}."
                    ),
                    tenant_id=tenant_id,
                    connector_id=str(connector_id),
                    tags=["connector", "tempo"],
                    knowledge_type=KnowledgeType.DOCUMENTATION,
                    source_uri=f"connector://{connector_id}/operations",
                    search_metadata=ChunkMetadata.model_validate(
                        {
                            "connector_type": "tempo",
                            "connector_name": connector_name,
                            "source_type": "connector_operations",
                        }
                    ),
                )
                await knowledge_store.add_chunk(chunk_create)
                result.knowledge_chunks_created = 1
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks for Tempo: {e}")

        logger.info(
            f"Synced Tempo connector '{connector_name}': "
            f"{result.operations_synced} operations, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"Tempo operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"Tempo operations sync failed: {e!s}"

    return result


async def _sync_alertmanager_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync Alertmanager operations from code definitions.

    Alertmanager has no entity types (alerts are ephemeral, not topology),
    so only operations and knowledge chunks are synced.
    WRITE operations (create_silence, silence_alert, expire_silence) get
    safety_level='write' for trust model approval.
    """
    from meho_app.modules.connectors.repositories import ConnectorOperationRepository
    from meho_app.modules.connectors.schemas import ConnectorOperationCreate

    result = ImportOperationsSyncResult()

    try:
        from meho_app.modules.connectors.alertmanager import (
            ALERTMANAGER_OPERATIONS,
            WRITE_OPERATIONS,
        )

        # Register operations
        op_repo = ConnectorOperationRepository(session)
        op_creates = []
        for op in ALERTMANAGER_OPERATIONS:
            search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
            safety_level = "write" if op.operation_id in WRITE_OPERATIONS else "read"
            op_creates.append(
                ConnectorOperationCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=list(op.parameters),
                    example=op.example,
                    search_content=search_content,
                    safety_level=safety_level,  # type: ignore[arg-type]
                )
            )

        if op_creates:
            result.operations_synced = await op_repo.create_operations_bulk(op_creates)

        # No type registration -- Alertmanager has no topology entities (alerts are ephemeral)

        # Create knowledge chunks
        knowledge_store = _get_knowledge_store(session)
        if knowledge_store:
            try:
                from meho_app.modules.knowledge.schemas import (
                    ChunkMetadata,
                    KnowledgeChunkCreate,
                    KnowledgeType,
                )

                chunk_create = KnowledgeChunkCreate(
                    text=(
                        f"Alertmanager connector '{connector_name}'. "
                        f"Provides alert investigation, silence management, "
                        f"cluster status, and receiver listing. "
                        f"WRITE operations: create_silence, silence_alert, expire_silence. "
                        f"Operations: {', '.join(op.operation_id for op in ALERTMANAGER_OPERATIONS)}."
                    ),
                    tenant_id=tenant_id,
                    connector_id=str(connector_id),
                    tags=["connector", "alertmanager"],
                    knowledge_type=KnowledgeType.DOCUMENTATION,
                    source_uri=f"connector://{connector_id}/operations",
                    search_metadata=ChunkMetadata.model_validate(
                        {
                            "connector_type": "alertmanager",
                            "connector_name": connector_name,
                            "source_type": "connector_operations",
                        }
                    ),
                )
                await knowledge_store.add_chunk(chunk_create)
                result.knowledge_chunks_created = 1
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks for Alertmanager: {e}")

        logger.info(
            f"Synced Alertmanager connector '{connector_name}': "
            f"{result.operations_synced} operations, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"Alertmanager operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"Alertmanager operations sync failed: {e!s}"

    return result


async def _sync_jira_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync Jira operations from code definitions.

    Jira has no entity types (issues are not topology entities),
    so only operations and knowledge chunks are synced.
    WRITE operations (create_issue, add_comment, transition_issue,
    search_by_jql) get safety_level='write' for trust model approval.
    """
    from meho_app.modules.connectors.repositories import ConnectorOperationRepository
    from meho_app.modules.connectors.schemas import ConnectorOperationCreate

    result = ImportOperationsSyncResult()

    try:
        from meho_app.modules.connectors.jira import (
            JIRA_OPERATIONS,
            WRITE_OPERATIONS,
        )

        # Register operations
        op_repo = ConnectorOperationRepository(session)
        op_creates = []
        for op in JIRA_OPERATIONS:
            search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
            safety_level = "write" if op.operation_id in WRITE_OPERATIONS else "read"
            op_creates.append(
                ConnectorOperationCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=list(op.parameters),
                    example=op.example,
                    search_content=search_content,
                    safety_level=safety_level,  # type: ignore[arg-type]
                )
            )

        if op_creates:
            result.operations_synced = await op_repo.create_operations_bulk(op_creates)

        # No type registration -- Jira issues are not topology entities

        # Create knowledge chunks
        knowledge_store = _get_knowledge_store(session)
        if knowledge_store:
            try:
                from meho_app.modules.knowledge.schemas import (
                    ChunkMetadata,
                    KnowledgeChunkCreate,
                    KnowledgeType,
                )

                chunk_create = KnowledgeChunkCreate(
                    text=(
                        f"Jira connector '{connector_name}'. "
                        f"Provides issue search, CRUD, workflow transitions, "
                        f"project listing, and JQL query execution. "
                        f"WRITE operations: create_issue, add_comment, transition_issue, search_by_jql. "
                        f"Operations: {', '.join(op.operation_id for op in JIRA_OPERATIONS)}."
                    ),
                    tenant_id=tenant_id,
                    connector_id=str(connector_id),
                    tags=["connector", "jira"],
                    knowledge_type=KnowledgeType.DOCUMENTATION,
                    source_uri=f"connector://{connector_id}/operations",
                    search_metadata=ChunkMetadata.model_validate(
                        {
                            "connector_type": "jira",
                            "connector_name": connector_name,
                            "source_type": "connector_operations",
                        }
                    ),
                )
                await knowledge_store.add_chunk(chunk_create)
                result.knowledge_chunks_created = 1
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks for Jira: {e}")

        logger.info(
            f"Synced Jira connector '{connector_name}': "
            f"{result.operations_synced} operations, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"Jira operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"Jira operations sync failed: {e!s}"

    return result


async def _sync_confluence_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync Confluence operations from code definitions.

    Confluence has no entity types (pages are not topology entities),
    so only operations and knowledge chunks are synced.
    WRITE operations (create_page, update_page, add_comment,
    search_by_cql) get safety_level='write' for trust model approval.
    """
    from meho_app.modules.connectors.repositories import ConnectorOperationRepository
    from meho_app.modules.connectors.schemas import ConnectorOperationCreate

    result = ImportOperationsSyncResult()

    try:
        from meho_app.modules.connectors.confluence import (
            CONFLUENCE_OPERATIONS,
            WRITE_OPERATIONS,
        )

        # Register operations
        op_repo = ConnectorOperationRepository(session)
        op_creates = []
        for op in CONFLUENCE_OPERATIONS:
            search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
            safety_level = "write" if op.operation_id in WRITE_OPERATIONS else "read"
            op_creates.append(
                ConnectorOperationCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=list(op.parameters),
                    example=op.example,
                    search_content=search_content,
                    safety_level=safety_level,  # type: ignore[arg-type]
                )
            )

        if op_creates:
            result.operations_synced = await op_repo.create_operations_bulk(op_creates)

        # No type registration -- Confluence pages are not topology entities

        # Create knowledge chunks
        knowledge_store = _get_knowledge_store(session)
        if knowledge_store:
            try:
                from meho_app.modules.knowledge.schemas import (
                    ChunkMetadata,
                    KnowledgeChunkCreate,
                    KnowledgeType,
                )

                chunk_create = KnowledgeChunkCreate(
                    text=(
                        f"Confluence connector '{connector_name}'. "
                        f"Provides documentation search, page CRUD, comment management, "
                        f"space listing, and CQL query execution. "
                        f"WRITE operations: create_page, update_page, add_comment, search_by_cql. "
                        f"Operations: {', '.join(op.operation_id for op in CONFLUENCE_OPERATIONS)}."
                    ),
                    tenant_id=tenant_id,
                    connector_id=str(connector_id),
                    tags=["connector", "confluence"],
                    knowledge_type=KnowledgeType.DOCUMENTATION,
                    source_uri=f"connector://{connector_id}/operations",
                    search_metadata=ChunkMetadata.model_validate(
                        {
                            "connector_type": "confluence",
                            "connector_name": connector_name,
                            "source_type": "connector_operations",
                        }
                    ),
                )
                await knowledge_store.add_chunk(chunk_create)
                result.knowledge_chunks_created = 1
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks for Confluence: {e}")

        logger.info(
            f"Synced Confluence connector '{connector_name}': "
            f"{result.operations_synced} operations, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"Confluence operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"Confluence operations sync failed: {e!s}"

    return result


async def _sync_email_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync Email operations from code definitions.

    Email has no entity types (emails are not topology entities),
    so only operations and knowledge chunks are synced.
    WRITE operations (send_email) get safety_level='write' for trust model approval.
    """
    from meho_app.modules.connectors.repositories import ConnectorOperationRepository
    from meho_app.modules.connectors.schemas import ConnectorOperationCreate

    result = ImportOperationsSyncResult()

    try:
        from meho_app.modules.connectors.email.operations import (
            EMAIL_OPERATIONS,
            WRITE_OPERATIONS,
        )

        # Register operations
        op_repo = ConnectorOperationRepository(session)
        op_creates = []
        for op in EMAIL_OPERATIONS:
            search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
            safety_level = "write" if op.operation_id in WRITE_OPERATIONS else "read"
            op_creates.append(
                ConnectorOperationCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=list(op.parameters),
                    example=op.example,
                    search_content=search_content,
                    safety_level=safety_level,  # type: ignore[arg-type]
                )
            )

        if op_creates:
            result.operations_synced = await op_repo.create_operations_bulk(op_creates)

        # No type registration -- emails are not topology entities

        # Create knowledge chunks
        knowledge_store = _get_knowledge_store(session)
        if knowledge_store:
            try:
                from meho_app.modules.knowledge.schemas import (
                    ChunkMetadata,
                    KnowledgeChunkCreate,
                    KnowledgeType,
                )

                chunk_create = KnowledgeChunkCreate(
                    text=(
                        f"Email connector '{connector_name}'. "
                        f"Sends branded HTML email notifications and investigation reports. "
                        f"WRITE operations: send_email. "
                        f"Operations: {', '.join(op.operation_id for op in EMAIL_OPERATIONS)}."
                    ),
                    tenant_id=tenant_id,
                    connector_id=str(connector_id),
                    tags=["connector", "email"],
                    knowledge_type=KnowledgeType.DOCUMENTATION,
                    source_uri=f"connector://{connector_id}/operations",
                    search_metadata=ChunkMetadata.model_validate(
                        {
                            "connector_type": "email",
                            "connector_name": connector_name,
                            "source_type": "connector_operations",
                        }
                    ),
                )
                await knowledge_store.add_chunk(chunk_create)
                result.knowledge_chunks_created = 1
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks for Email: {e}")

        logger.info(
            f"Synced Email connector '{connector_name}': "
            f"{result.operations_synced} operations, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"Email operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"Email operations sync failed: {e!s}"

    return result


async def _sync_argocd_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync ArgoCD operations from code definitions.

    ArgoCD has no entity types (applications are not topology entities),
    so only operations and knowledge chunks are synced.
    WRITE operations (sync_application) get safety_level='write' and
    DESTRUCTIVE operations (rollback_application) get safety_level='destructive'
    for trust model approval.
    """
    from meho_app.modules.connectors.repositories import ConnectorOperationRepository
    from meho_app.modules.connectors.schemas import ConnectorOperationCreate

    result = ImportOperationsSyncResult()

    try:
        from meho_app.modules.connectors.argocd import (
            ARGOCD_OPERATIONS,
            DESTRUCTIVE_OPERATIONS,
            WRITE_OPERATIONS,
        )

        # Register operations
        op_repo = ConnectorOperationRepository(session)
        op_creates = []
        for op in ARGOCD_OPERATIONS:
            search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
            if op.operation_id in DESTRUCTIVE_OPERATIONS:
                safety_level = "destructive"
            elif op.operation_id in WRITE_OPERATIONS:
                safety_level = "write"
            else:
                safety_level = "read"
            op_creates.append(
                ConnectorOperationCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=list(op.parameters),
                    example=op.example,
                    search_content=search_content,
                    safety_level=safety_level,  # type: ignore[arg-type]
                    response_entity_type=op.response_entity_type,
                    response_identifier_field=op.response_identifier_field,
                    response_display_name_field=op.response_display_name_field,
                )
            )

        if op_creates:
            result.operations_synced = await op_repo.create_operations_bulk(op_creates)

        # No type registration -- ArgoCD applications are not topology entities

        # Create knowledge chunks
        knowledge_store = _get_knowledge_store(session)
        if knowledge_store:
            try:
                from meho_app.modules.knowledge.schemas import (
                    ChunkMetadata,
                    KnowledgeChunkCreate,
                    KnowledgeType,
                )

                chunk_create = KnowledgeChunkCreate(
                    text=(
                        f"ArgoCD connector '{connector_name}'. "
                        f"Provides GitOps application management: list/get applications, "
                        f"resource trees, sync history, managed resources, events, "
                        f"revision metadata, server diff, sync, and rollback. "
                        f"WRITE operations: sync_application. "
                        f"DESTRUCTIVE operations: rollback_application. "
                        f"Operations: {', '.join(op.operation_id for op in ARGOCD_OPERATIONS)}."
                    ),
                    tenant_id=tenant_id,
                    connector_id=str(connector_id),
                    tags=["connector", "argocd"],
                    knowledge_type=KnowledgeType.DOCUMENTATION,
                    source_uri=f"connector://{connector_id}/operations",
                    search_metadata=ChunkMetadata.model_validate(
                        {
                            "connector_type": "argocd",
                            "connector_name": connector_name,
                            "source_type": "connector_operations",
                        }
                    ),
                )
                await knowledge_store.add_chunk(chunk_create)
                result.knowledge_chunks_created = 1
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks for ArgoCD: {e}")

        logger.info(
            f"Synced ArgoCD connector '{connector_name}': "
            f"{result.operations_synced} operations, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"ArgoCD operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"ArgoCD operations sync failed: {e!s}"

    return result


async def _sync_github_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
) -> ImportOperationsSyncResult:
    """Sync GitHub operations from code definitions.

    GitHub has no entity types (repos/PRs are not topology entities),
    so only operations and knowledge chunks are synced.
    WRITE operations (rerun_failed_jobs) get safety_level='write'
    for trust model approval. No DESTRUCTIVE operations for GitHub.
    """
    from meho_app.modules.connectors.repositories import ConnectorOperationRepository
    from meho_app.modules.connectors.schemas import ConnectorOperationCreate

    result = ImportOperationsSyncResult()

    try:
        from meho_app.modules.connectors.github import (
            GITHUB_OPERATIONS,
            WRITE_OPERATIONS,
        )

        # Register operations
        op_repo = ConnectorOperationRepository(session)
        op_creates = []
        for op in GITHUB_OPERATIONS:
            search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
            safety_level = "write" if op.operation_id in WRITE_OPERATIONS else "read"
            op_creates.append(
                ConnectorOperationCreate(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=list(op.parameters),
                    example=op.example,
                    search_content=search_content,
                    safety_level=safety_level,  # type: ignore[arg-type]
                    response_entity_type=op.response_entity_type,
                    response_identifier_field=op.response_identifier_field,
                    response_display_name_field=op.response_display_name_field,
                )
            )

        if op_creates:
            result.operations_synced = await op_repo.create_operations_bulk(op_creates)

        # No type registration -- GitHub entities are not topology entities

        # Create knowledge chunks with per-operation BM25 keywords
        knowledge_store = _get_knowledge_store(session)
        if knowledge_store:
            try:
                from meho_app.modules.connectors.github.sync import (
                    _format_github_operation_as_text,
                    _get_github_bm25_keywords,
                )
                from meho_app.modules.knowledge.schemas import (
                    ChunkMetadata,
                    KnowledgeChunkCreate,
                    KnowledgeType,
                )

                for op in GITHUB_OPERATIONS:
                    text = _format_github_operation_as_text(op, connector_name)
                    bm25_keywords = _get_github_bm25_keywords(op)

                    metadata_dict = {
                        "resource_type": op.category,
                        "keywords": bm25_keywords,
                        "source_type": "connector_operation",
                        "connector_id": connector_id,
                        "connector_type": "github",
                        "operation_id": op.operation_id,
                        "operation_name": op.name,
                        "category": op.category,
                    }
                    chunk_metadata = ChunkMetadata.model_validate(metadata_dict)

                    chunk_create = KnowledgeChunkCreate(
                        text=text,
                        tenant_id=tenant_id,
                        connector_id=str(connector_id),
                        tags=["api", "operation", "github", op.category],
                        knowledge_type=KnowledgeType.DOCUMENTATION,
                        priority=5,
                        search_metadata=chunk_metadata,
                        source_uri=f"connector://{connector_id}/operation/{op.operation_id}",
                    )

                    await knowledge_store.add_chunk(chunk_create)
                    result.knowledge_chunks_created += 1

            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks for GitHub: {e}")

        logger.info(
            f"Synced GitHub connector '{connector_name}': "
            f"{result.operations_synced} operations, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.error(f"GitHub operations sync failed: {e}", exc_info=True)
        result.success = False
        result.warning = f"GitHub operations sync failed: {e!s}"

    return result


# =============================================================================
# Dynamic Connector Sync Functions (REST/SOAP)
# =============================================================================


async def _sync_rest_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
    protocol_config: dict[str, Any] | None,
) -> ImportOperationsSyncResult:
    """
    Sync REST operations from OpenAPI spec if URL is configured.

    If openapi_url is in protocol_config, fetch and parse the spec.
    Network errors produce warnings, not failures.
    """
    result = ImportOperationsSyncResult()

    # Check if openapi_url is configured
    openapi_url = None
    if protocol_config:
        openapi_url = protocol_config.get("openapi_url")

    if not openapi_url:
        # No URL configured - user will upload spec manually
        logger.info(
            f"REST connector '{connector_name}' has no openapi_url - "
            "skipping auto-sync (user can upload spec manually)"
        )
        return result

    try:
        import httpx

        from meho_app.modules.connectors.rest.repository import EndpointDescriptorRepository
        from meho_app.modules.connectors.rest.spec_parser import OpenAPIParser

        logger.info(f"🔍 Fetching OpenAPI spec from: {openapi_url}")

        # Fetch the spec
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:  # noqa: S501 -- internal service, self-signed cert
            response = await client.get(openapi_url)
            response.raise_for_status()
            spec_content = response.content

        # Parse the spec
        parser = OpenAPIParser()
        spec_dict = parser.parse(spec_content)  # type: ignore[arg-type]

        # Create endpoints from spec
        endpoint_repo = EndpointDescriptorRepository(session)
        endpoints = await endpoint_repo.create_from_spec(connector_id, spec_dict)  # type: ignore[attr-defined]
        result.operations_synced = len(endpoints)

        # Create knowledge chunks for searchable operations
        knowledge_store = _get_knowledge_store(session)
        if knowledge_store:
            from meho_app.core.auth_context import UserContext
            from meho_app.modules.connectors.rest.knowledge_ingestion import (
                ingest_openapi_to_knowledge,
            )

            # Create a minimal user context for ingestion
            user_context = UserContext(
                user_id="system",
                tenant_id=tenant_id,
                roles=["system"],
            )

            chunks = await ingest_openapi_to_knowledge(
                spec_dict=spec_dict,
                connector_id=connector_id,
                connector_name=connector_name,
                knowledge_store=knowledge_store,
                user_context=user_context,
            )
            result.knowledge_chunks_created = chunks

        logger.info(
            f"✅ Synced REST connector '{connector_name}': "
            f"{result.operations_synced} endpoints, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except httpx.HTTPError as e:
        logger.warning(f"Failed to fetch OpenAPI spec for '{connector_name}': {e}")
        result.warning = (
            f"Could not fetch OpenAPI spec from {openapi_url}: {e!s}. "
            "You can manually upload the spec later."
        )
    except Exception as e:
        logger.warning(f"Failed to parse OpenAPI spec for '{connector_name}': {e}")
        result.warning = (
            f"Failed to parse OpenAPI spec: {e!s}. You can manually upload the spec later."
        )

    return result


async def _sync_soap_operations(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    connector_name: str,
    protocol_config: dict[str, Any] | None,
) -> ImportOperationsSyncResult:
    """
    Sync SOAP operations from WSDL if URL is configured.

    If wsdl_url is in protocol_config, fetch and parse the WSDL.
    Network errors produce warnings, not failures.
    """
    result = ImportOperationsSyncResult()

    # Check if wsdl_url is configured
    wsdl_url = None
    if protocol_config:
        wsdl_url = protocol_config.get("wsdl_url")

    if not wsdl_url:
        # No URL configured - user will upload WSDL manually
        logger.info(
            f"SOAP connector '{connector_name}' has no wsdl_url - "
            "skipping auto-sync (user can upload WSDL manually)"
        )
        return result

    try:
        from uuid import UUID

        from meho_app.modules.connectors.soap.ingester import SOAPSchemaIngester
        from meho_app.modules.connectors.soap.repository import (
            SoapOperationRepository as SOAPOperationRepository,
        )
        from meho_app.modules.connectors.soap.repository import (
            SoapTypeRepository as SOAPTypeRepository,
        )

        logger.info(f"🔍 Ingesting WSDL from: {wsdl_url}")

        # Parse WSDL and extract operations
        ingester = SOAPSchemaIngester()
        operations, _metadata, type_definitions = await asyncio.to_thread(
            ingester.ingest_wsdl,
            wsdl_url=wsdl_url,
            connector_id=UUID(connector_id),
            tenant_id=tenant_id,
        )

        # Store operations in database
        op_repo = SOAPOperationRepository(session)
        for op in operations:
            await op_repo.create_operation(op)  # type: ignore[arg-type]  # SOAPOperation satisfies the create schema
        result.operations_synced = len(operations)

        # Store type definitions
        if type_definitions:
            type_repo = SOAPTypeRepository(session)
            for type_def in type_definitions:
                await type_repo.create_type(type_def)  # type: ignore[arg-type]  # SOAPTypeDefinition satisfies the create schema
            result.types_synced = len(type_definitions)

        # Create knowledge chunks for searchable operations
        knowledge_store = _get_knowledge_store(session)
        if knowledge_store:
            chunks = await ingester.create_knowledge_chunks_for_operations(
                operations=operations,
                knowledge_store=knowledge_store,
                connector_id=UUID(connector_id),
                connector_name=connector_name,
                tenant_id=tenant_id,
            )
            result.knowledge_chunks_created = chunks

        logger.info(
            f"✅ Synced SOAP connector '{connector_name}': "
            f"{result.operations_synced} operations, {result.types_synced} types, "
            f"{result.knowledge_chunks_created} knowledge chunks"
        )

    except Exception as e:
        logger.warning(f"Failed to ingest WSDL for '{connector_name}': {e}")
        result.warning = (
            f"Could not fetch/parse WSDL from {wsdl_url}: {e!s}. "
            "You can manually upload the WSDL later."
        )

    return result
