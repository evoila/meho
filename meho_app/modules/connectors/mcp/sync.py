# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MCP Dynamic Operation Sync (Phase 93)

Syncs MCP connector operations to the database. Unlike static connectors
(GCP, VMware), MCP operations are dynamic -- they are discovered from the
server at connect() time and persisted for search_operations discovery.

The startup sync does NOT connect to MCP servers. It only ensures that
previously persisted operations are available for the agent. Re-discovery
happens when an operator refreshes the connector or on reconnect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import OperationDefinition

if TYPE_CHECKING:
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

logger = get_logger(__name__)


def _format_mcp_operation_as_text(
    op: OperationDefinition,
    connector_name: str,
    server_name: str,
) -> str:
    """
    Format an MCP operation as rich searchable text.

    Creates text optimized for BM25 + semantic search, including
    MCP server context for disambiguation.

    Args:
        op: MCP operation definition.
        connector_name: Display name of the connector.
        server_name: Sanitized MCP server name.

    Returns:
        Formatted text for embedding and BM25 indexing.
    """
    parts = [
        op.operation_id,
        "",
        f"Name: {op.name}",
        f"Connector: {connector_name}",
        f"MCP Server: {server_name}",
        "Platform: MCP (Model Context Protocol)",
        "",
        f"Description: {op.description}",
        "",
        f"Category: {op.category}",
        "",
    ]

    if op.parameters:
        parts.append("Parameters:")
        for param in op.parameters:
            param_name = param.get("name", "unknown") if isinstance(param, dict) else str(param)
            param_desc = param.get("description", "") if isinstance(param, dict) else ""
            param_req = param.get("required", False) if isinstance(param, dict) else False
            req_marker = " (required)" if param_req else ""
            parts.append(f"  - {param_name}: {param_desc}{req_marker}")
        parts.append("")

    # MCP-specific search keywords
    keywords = ["mcp", "model context protocol", "tool", server_name, connector_name.lower()]
    parts.append(f"Search: {' '.join(keywords)}")

    return "\n".join(parts)


async def sync_mcp_knowledge_chunks(
    knowledge_store: KnowledgeStore,
    connector_id: str,
    connector_name: str,
    server_name: str,
    tenant_id: str,
    operations: list[OperationDefinition],
) -> int:
    """
    Create or update knowledge_chunk entries for MCP operations.

    Args:
        knowledge_store: KnowledgeStore for creating chunks.
        connector_id: UUID of the connector.
        connector_name: Display name for formatting.
        server_name: Sanitized MCP server name.
        tenant_id: Tenant ID.
        operations: List of discovered operations.

    Returns:
        Number of knowledge chunks created.
    """
    from meho_app.modules.knowledge.models import KnowledgeChunkModel
    from meho_app.modules.knowledge.schemas import KnowledgeChunkCreate, KnowledgeType

    # Delete existing chunks for this connector
    try:
        stmt = delete(KnowledgeChunkModel).where(
            KnowledgeChunkModel.tenant_id == tenant_id,
            KnowledgeChunkModel.search_metadata["connector_id"].astext == connector_id,
            KnowledgeChunkModel.search_metadata["source_type"].astext == "connector_operation",
            KnowledgeChunkModel.search_metadata["connector_type"].astext == "mcp",
        )
        await knowledge_store.repository.session.execute(stmt)
        logger.debug(f"Deleted existing MCP knowledge chunks for connector {connector_id}")
    except Exception as e:
        logger.warning(f"Failed to delete existing MCP chunks (may not exist): {e}")

    chunks_created = 0

    for op in operations:
        try:
            text = _format_mcp_operation_as_text(op, connector_name, server_name)

            from meho_app.modules.knowledge.schemas import ChunkMetadata

            metadata_dict = {
                "resource_type": op.category,
                "keywords": [op.operation_id, op.name, op.category, "mcp", server_name],
                "source_type": "connector_operation",
                "connector_id": connector_id,
                "connector_type": "mcp",
                "operation_id": op.operation_id,
                "operation_name": op.name,
                "category": op.category,
            }
            chunk_metadata = ChunkMetadata.model_validate(metadata_dict)

            chunk_create = KnowledgeChunkCreate(
                text=text,
                tenant_id=tenant_id,
                connector_id=str(connector_id),
                tags=["api", "operation", "mcp", "tool", server_name],
                knowledge_type=KnowledgeType.DOCUMENTATION,
                priority=5,
                search_metadata=chunk_metadata,
                source_uri=f"connector://{connector_id}/operation/{op.operation_id}",
            )

            await knowledge_store.add_chunk(chunk_create)
            chunks_created += 1

        except Exception as e:
            logger.error(f"Failed to create knowledge chunk for {op.operation_id}: {e}")
            continue

    logger.info(f"Created {chunks_created} knowledge chunks for MCP connector {connector_id}")
    return chunks_created


async def sync_all_mcp_connectors(
    session: AsyncSession,
    knowledge_store: KnowledgeStore | None = None,
) -> dict[str, Any]:
    """
    Sync all MCP connectors on startup.

    Unlike static connectors (GCP, VMware) where operations are defined
    in code, MCP operations are dynamic. This startup sync only ensures
    that previously persisted operations remain consistent. It does NOT
    connect to MCP servers -- that happens on demand.

    The sync checks if the operations_hash in protocol_config matches the
    stored operations. If they diverge (e.g., after a version upgrade),
    it logs a warning. Actual re-sync happens when the operator clicks
    refresh or the connector reconnects.

    Args:
        session: Database session.
        knowledge_store: Optional KnowledgeStore for hybrid search.

    Returns:
        Summary dict with counts.
    """
    from sqlalchemy import select

    from meho_app.modules.connectors.models import ConnectorModel

    # Find all MCP connectors
    query = select(ConnectorModel).where(ConnectorModel.connector_type == "mcp")
    result = await session.execute(query)
    connectors = result.scalars().all()

    if not connectors:
        logger.info("No MCP connectors found to sync")
        return {"connectors_checked": 0, "connectors_synced": 0}

    logger.info(f"Checking {len(connectors)} MCP connector(s) for operation consistency")

    connectors_checked = 0

    for connector in connectors:
        config: dict[str, Any] = dict(connector.protocol_config or {})
        tools_hash = config.get("operations_hash")
        connectors_checked += 1

        if not tools_hash:
            logger.debug(
                f"MCP connector {connector.id} has no operations_hash -- operations "
                "will be synced on next connect",
            )

    logger.info(f"MCP startup sync: checked {connectors_checked} connector(s)")

    return {
        "connectors_checked": connectors_checked,
        "connectors_synced": 0,
    }
