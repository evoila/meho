# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware Operations Auto-Sync

Automatically syncs VMware operations to the database when the operations
version changes. This ensures existing connectors get new operations
(like PerformanceManager metrics) without manual intervention.

TASK-126: Also creates knowledge_chunk entries with embeddings for hybrid search.
This enables semantic search (BM25 + embeddings) across all VMware operations.

Usage:
    On API startup, call sync_all_vmware_connectors() to update any
    connectors that have an outdated operations version.
"""

from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import OperationDefinition
from meho_app.modules.connectors.schemas import ConnectorOperationCreate
from meho_app.modules.connectors.vmware.operations import (
    VMWARE_OPERATIONS,
    VMWARE_OPERATIONS_VERSION,
)

if TYPE_CHECKING:
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

logger = get_logger(__name__)


def _format_vmware_operation_as_text(
    op: OperationDefinition, connector_name: str = "VMware"
) -> str:
    """
    Format VMware operation as rich searchable text.

    TASK-126: Creates text optimized for BM25 + semantic search.
    Similar to _format_endpoint_as_text() for REST endpoints.

    Args:
        op: VMware operation definition
        connector_name: Name of the connector for context

    Returns:
        Formatted text for embedding and BM25 indexing
    """
    parts = []

    # Header: Operation ID and name
    parts.append(f"{op.operation_id}")
    parts.append("")

    # Name and connector context
    parts.append(f"Name: {op.name}")
    parts.append(f"Connector: {connector_name}")
    parts.append("")

    # Description
    parts.append(f"Description: {op.description}")
    parts.append("")

    # Category
    parts.append(f"Category: {op.category}")
    parts.append("")

    # Parameters
    if op.parameters:
        parts.append("Parameters:")
        for param in op.parameters:
            param_name = param.get("name", "unknown") if isinstance(param, dict) else str(param)
            param_desc = param.get("description", "") if isinstance(param, dict) else ""
            parts.append(f"  - {param_name}: {param_desc}")
        parts.append("")

    # Example
    if op.example:
        parts.append(f"Example: {op.example}")
        parts.append("")

    # Search keywords for better BM25 matching
    # Add common variations and abbreviations
    keywords = _generate_vmware_search_keywords(op)
    if keywords:
        parts.append(f"Search: {keywords}")

    return "\n".join(parts)


def _generate_vmware_search_keywords(op: OperationDefinition) -> str:
    """
    Generate search keywords for better BM25 matching.

    Adds abbreviations, synonyms, and variations that users might search for.

    Args:
        op: VMware operation definition

    Returns:
        Space-separated search keywords
    """
    keywords = set()

    # Add operation ID parts
    for part in op.operation_id.split("_"):
        keywords.add(part)

    # Add category
    keywords.add(op.category)

    # VMware-specific abbreviations
    vmware_abbrevs = {
        "virtual_machine": ["vm", "vms", "virtual machine", "virtualmachine"],
        "datastore": ["ds", "storage"],
        "resource_pool": ["rp", "pool"],
        "virtual_switch": ["vswitch", "switch"],
        "distributed_port_group": ["dpg", "portgroup"],
        "host": ["esxi", "server"],
        "cluster": ["compute cluster"],
        "datacenter": ["dc"],
        "folder": ["inventory"],
        "network": ["vlan", "net"],
        "performance": ["perf", "metrics", "stats"],
    }

    # Add relevant abbreviations based on operation name/description
    combined_text = f"{op.operation_id} {op.name} {op.description}".lower()
    for key, abbrevs in vmware_abbrevs.items():
        if key in combined_text:
            keywords.update(abbrevs)

    # Common action words
    if "list" in combined_text:
        keywords.update(["list", "get", "show", "all"])
    if "get" in combined_text and "list" not in combined_text:
        keywords.update(["get", "show", "details"])
    if "create" in combined_text:
        keywords.update(["create", "new", "add"])
    if "delete" in combined_text:
        keywords.update(["delete", "remove", "destroy"])
    if "power" in combined_text:
        keywords.update(["power", "on", "off", "start", "stop"])

    return " ".join(sorted(keywords))


async def sync_vmware_operations_if_needed(
    session: AsyncSession,
    connector_id: str,
    tenant_id: str,
    current_version: str | None,
    knowledge_store: Optional["KnowledgeStore"] = None,
    connector_name: str = "VMware vCenter",
) -> tuple[int, int, int]:
    """
    Sync VMware operations if the version is outdated.

    TASK-126: Also creates knowledge_chunk entries with embeddings for hybrid search.

    Args:
        session: Database session
        connector_id: UUID of the connector
        tenant_id: Tenant ID for the connector
        current_version: Current operations version stored in connector
        knowledge_store: Optional KnowledgeStore for creating searchable chunks with embeddings
        connector_name: Display name of the connector for text formatting

    Returns:
        Tuple of (operations_added, operations_updated, knowledge_chunks_created)
    """
    from meho_app.modules.connectors.repositories import ConnectorOperationRepository

    # Check if sync is needed
    if current_version == VMWARE_OPERATIONS_VERSION:
        logger.debug(f"Connector {connector_id} already at version {current_version}")
        return (0, 0, 0)

    logger.info(
        f"Syncing VMware operations for connector {connector_id}: "
        f"{current_version or 'none'} -> {VMWARE_OPERATIONS_VERSION}"
    )

    op_repo = ConnectorOperationRepository(session)

    # Get existing operations
    existing_ops = await op_repo.list_operations(connector_id=connector_id, limit=1000)
    existing_op_ids = {op.operation_id for op in existing_ops}

    added = 0
    updated = 0
    chunks_created = 0

    for op in VMWARE_OPERATIONS:
        search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"

        if op.operation_id not in existing_op_ids:
            # Add new operation
            await op_repo.create_operation(
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
                    # Response schema for Brain-Muscle architecture (TASK-161)
                    response_entity_type=op.response_entity_type,
                    response_identifier_field=op.response_identifier_field,
                    response_display_name_field=op.response_display_name_field,
                )
            )
            added += 1
            logger.debug(f"  Added operation: {op.operation_id}")
        else:
            # Update existing operation (description, parameters may have changed)
            await op_repo.update_operation(
                connector_id=connector_id,
                operation_id=op.operation_id,
                name=op.name,
                description=op.description,
                category=op.category,
                parameters=list(op.parameters),
                example=op.example,
                search_content=search_content,
                # Response schema for Brain-Muscle architecture (TASK-161)
                response_entity_type=op.response_entity_type,
                response_identifier_field=op.response_identifier_field,
                response_display_name_field=op.response_display_name_field,
            )
            updated += 1

    # TASK-126: Create knowledge_chunk entries for hybrid search
    if knowledge_store is not None:
        chunks_created = await _sync_vmware_knowledge_chunks(
            knowledge_store=knowledge_store,
            connector_id=connector_id,
            connector_name=connector_name,
            tenant_id=tenant_id,
        )

    logger.info(
        f"Synced connector {connector_id}: "
        f"{added} added, {updated} updated, {len(VMWARE_OPERATIONS)} total, "
        f"{chunks_created} knowledge chunks created"
    )

    return (added, updated, chunks_created)


async def _sync_vmware_knowledge_chunks(
    knowledge_store: "KnowledgeStore",
    connector_id: str,
    connector_name: str,
    tenant_id: str,
) -> int:
    """
    Create or update knowledge_chunk entries for VMware operations.

    TASK-126: Enables hybrid search (BM25 + semantic) for VMware operations.

    Args:
        knowledge_store: KnowledgeStore for creating chunks with embeddings
        connector_id: UUID of the connector
        connector_name: Display name for formatting
        tenant_id: Tenant ID

    Returns:
        Number of knowledge chunks created
    """
    from meho_app.modules.knowledge.models import KnowledgeChunkModel
    from meho_app.modules.knowledge.schemas import KnowledgeChunkCreate, KnowledgeType

    # First, delete existing knowledge chunks for this connector
    # This ensures we don't have stale data
    try:
        stmt = delete(KnowledgeChunkModel).where(
            KnowledgeChunkModel.tenant_id == tenant_id,
            KnowledgeChunkModel.search_metadata["connector_id"].astext == connector_id,
            KnowledgeChunkModel.search_metadata["source_type"].astext == "connector_operation",
            KnowledgeChunkModel.search_metadata["connector_type"].astext == "vmware",
        )
        await knowledge_store.repository.session.execute(stmt)
        logger.debug(f"Deleted existing VMware knowledge chunks for connector {connector_id}")
    except Exception as e:
        logger.warning(f"Failed to delete existing chunks (may not exist): {e}")

    chunks_created = 0

    for op in VMWARE_OPERATIONS:
        try:
            # Format operation as rich searchable text
            text = _format_vmware_operation_as_text(op, connector_name)

            # Create knowledge chunk with metadata for filtering
            # Note: search_metadata accepts ChunkMetadata with extra fields (extra="allow")
            from meho_app.modules.knowledge.schemas import ChunkMetadata

            # Build metadata dict and validate (ChunkMetadata has extra="allow")
            metadata_dict = {
                "resource_type": op.category,
                "keywords": [op.operation_id, op.name, op.category],
                "source_type": "connector_operation",
                "connector_id": connector_id,
                "connector_type": "vmware",
                "operation_id": op.operation_id,
                "operation_name": op.name,
                "category": op.category,
            }
            chunk_metadata = ChunkMetadata.model_validate(metadata_dict)

            chunk_create = KnowledgeChunkCreate(
                text=text,
                tenant_id=tenant_id,
                connector_id=str(connector_id),
                tags=["api", "operation", "vmware", op.category],
                knowledge_type=KnowledgeType.DOCUMENTATION,
                priority=5,
                search_metadata=chunk_metadata,
                source_uri=f"connector://{connector_id}/operation/{op.operation_id}",
            )

            # Add chunk (generates embedding automatically)
            await knowledge_store.add_chunk(chunk_create)
            chunks_created += 1
            logger.debug(f"  Created knowledge chunk for operation: {op.operation_id}")

        except Exception as e:
            logger.error(f"Failed to create knowledge chunk for {op.operation_id}: {e}")
            continue

    logger.info(f"Created {chunks_created} knowledge chunks for VMware connector {connector_id}")
    return chunks_created


async def update_connector_operations_version(
    session: AsyncSession,
    connector_id: str,
) -> None:
    """
    Update the operations_version in connector's protocol_config.

    This marks the connector as synced to the current version.
    """
    import uuid

    from sqlalchemy import select

    from meho_app.modules.connectors.models import ConnectorModel

    query = select(ConnectorModel).where(ConnectorModel.id == uuid.UUID(connector_id))
    result = await session.execute(query)
    connector = result.scalar_one_or_none()

    if connector:
        # Update protocol_config with new version
        config: dict[str, Any] = dict(connector.protocol_config or {})
        config["operations_version"] = VMWARE_OPERATIONS_VERSION
        connector.protocol_config = config  # type: ignore[assignment]  # SQLAlchemy ORM attribute assignment
        await session.flush()
        logger.debug(f"Updated connector {connector_id} to version {VMWARE_OPERATIONS_VERSION}")


async def sync_all_vmware_connectors(
    session: AsyncSession,
    knowledge_store: Optional["KnowledgeStore"] = None,
) -> dict:
    """
    Sync all VMware connectors to the latest operations version.

    Called on API startup to ensure all existing connectors have
    the latest operations available.

    TASK-126: Also creates knowledge_chunk entries with embeddings for hybrid search
    if knowledge_store is provided.

    Args:
        session: Database session
        knowledge_store: Optional KnowledgeStore for creating searchable chunks

    Returns:
        Summary dict with counts of synced connectors and operations
    """
    from sqlalchemy import select

    from meho_app.modules.connectors.models import ConnectorModel

    # Find all VMware connectors
    query = select(ConnectorModel).where(ConnectorModel.connector_type == "vmware")
    result = await session.execute(query)
    connectors = result.scalars().all()

    if not connectors:
        logger.info("No VMware connectors found to sync")
        return {"connectors_checked": 0, "connectors_synced": 0}

    logger.info(f"Checking {len(connectors)} VMware connector(s) for operation sync")

    total_added = 0
    total_updated = 0
    total_chunks = 0
    connectors_synced = 0

    for connector in connectors:
        config: dict[str, Any] = dict(connector.protocol_config or {})
        current_version = config.get("operations_version")

        if current_version == VMWARE_OPERATIONS_VERSION:
            continue  # Already up to date

        # Sync operations (with optional knowledge store for hybrid search)
        added, updated, chunks = await sync_vmware_operations_if_needed(
            session=session,
            connector_id=str(connector.id),
            tenant_id=str(connector.tenant_id),
            current_version=current_version,
            knowledge_store=knowledge_store,
            connector_name=str(connector.name) if connector.name else "VMware vCenter",
        )

        # Update version in connector
        await update_connector_operations_version(session, str(connector.id))

        total_added += added
        total_updated += updated
        total_chunks += chunks
        connectors_synced += 1

    await session.commit()

    summary = {
        "connectors_checked": len(connectors),
        "connectors_synced": connectors_synced,
        "operations_added": total_added,
        "operations_updated": total_updated,
        "knowledge_chunks_created": total_chunks,
        "current_version": VMWARE_OPERATIONS_VERSION,
    }

    if connectors_synced > 0:
        logger.info(
            f"VMware operation sync complete: "
            f"{connectors_synced} connector(s) updated, "
            f"{total_added} operations added, {total_updated} updated, "
            f"{total_chunks} knowledge chunks created"
        )
    else:
        logger.info("All VMware connectors already at latest version")

    return summary
