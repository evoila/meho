# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MCP connector registration endpoint (Phase 93).

Handles MCP connector creation: validates transport config, tests connection
by discovering tools, registers operations with mcp_{server_name}_ prefixing,
and creates knowledge chunks for search discovery.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
import re

from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.connectors.schemas import (
    CreateMCPConnectorRequest,
    MCPConnectorResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.feature_flags import get_feature_flags
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


def _sanitize_server_name(name: str) -> str:
    """Derive a snake_case server_name from the connector display name."""
    name = name.lower().strip()
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^a-z0-9_]", "", name)
    return name or "mcp"


@router.post("/mcp", response_model=MCPConnectorResponse)
async def create_mcp_connector(
    request: CreateMCPConnectorRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_CREATE)),
):
    """
    Create an MCP client connector.

    Connects to an external MCP server, discovers tools via list_tools(),
    and registers them as connector operations. Supports Streamable HTTP
    and stdio transports.
    """
    # Check feature flag
    flags = get_feature_flags()
    if not flags.mcp_client:
        raise HTTPException(
            status_code=400,
            detail="MCP client connector is disabled (MEHO_FEATURE_MCP_CLIENT=false)",
        )

    # Validate transport-specific requirements
    if request.transport_type == "streamable_http" and not request.server_url:
        raise HTTPException(
            status_code=400,
            detail="server_url is required for streamable_http transport",
        )
    if request.transport_type == "stdio" and not request.command:
        raise HTTPException(
            status_code=400,
            detail="command is required for stdio transport",
        )

    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.mcp import MCPConnector
    from meho_app.modules.connectors.mcp.operations import (
        compute_safety_level,
        compute_tools_hash,
    )
    from meho_app.modules.connectors.repositories import (
        ConnectorOperationRepository,
    )
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )
    from meho_app.modules.connectors.schemas import (
        ConnectorCreate,
        ConnectorOperationCreate,
        UserCredentialProvide,
    )
    from meho_app.modules.connectors.service import ConnectorService

    server_name = _sanitize_server_name(request.name)

    # Determine base_url for the connector record
    if request.transport_type == "streamable_http":
        # nosemgrep: tainted-url-host -- admin-configured MCP server URL (connector setup requires admin auth)
        base_url = request.server_url or ""
    else:
        base_url = f"stdio://{request.command}"

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            connector_service = ConnectorService(session)
            op_repo = ConnectorOperationRepository(session)

            protocol_config = {
                "transport_type": request.transport_type,
                "server_url": request.server_url,
                "command": request.command,
                "args": request.args,
                "server_name": server_name,
            }

            auth_type = "API_KEY" if request.api_key else "NONE"

            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description,
                base_url=base_url,
                auth_type=auth_type,
                credential_strategy="USER_PROVIDED" if request.api_key else "SYSTEM",
                tenant_id=user.tenant_id,
                connector_type="mcp",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection and discover tools
            logger.info(
                "Testing MCP connection: %s (transport: %s)",
                request.server_url or request.command,
                request.transport_type,
            )
            credentials = {"api_key": request.api_key} if request.api_key else {}
            tools_discovered = 0
            raw_tools = []

            try:
                mcp_connector = MCPConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials=credentials,
                )
                await mcp_connector.connect()

                is_connected = await mcp_connector.test_connection()
                if not is_connected:
                    await mcp_connector.disconnect()
                    raise HTTPException(
                        status_code=400,
                        detail="Could not verify MCP server connection. Server may be unreachable.",
                    )

                tools_discovered = len(mcp_connector.get_operations())
                raw_tools = mcp_connector.raw_tools
                discovered_ops = mcp_connector.get_operations()

                # Compute and store tools hash for change detection
                tools_hash = compute_tools_hash(raw_tools)
                protocol_config["operations_hash"] = tools_hash

                await mcp_connector.disconnect()

                logger.info(
                    "MCP connection verified: %s (%d tools discovered)",
                    request.server_url or request.command,
                    tools_discovered,
                )
            except HTTPException:
                raise
            except Exception as e:
                logger.warning("MCP connection test failed: %s", e)
                raise HTTPException(
                    status_code=400,
                    detail=f"MCP connection failed: {e}",
                ) from e

            # Update protocol_config with operations_hash
            from sqlalchemy import select

            from meho_app.modules.connectors.models import ConnectorModel

            query = select(ConnectorModel).where(
                ConnectorModel.id == connector.id
            )
            result = await session.execute(query)
            db_connector = result.scalar_one()
            db_connector.protocol_config = protocol_config  # type: ignore[assignment]

            # Store credentials if api_key provided
            if request.api_key:
                cred_repo = UserCredentialRepository(session)
                await cred_repo.store_credentials(
                    user_id=user.user_id,
                    credential=UserCredentialProvide(
                        connector_id=connector_id,
                        credential_type="API_KEY",
                        credentials={"api_key": request.api_key},
                    ),
                )

            # Register operations from discovered tools
            op_creates = []
            for i, op in enumerate(discovered_ops):
                search_content = f"{op.name} {op.operation_id} {op.description} {op.category} mcp {server_name}"
                safety_level = compute_safety_level(raw_tools[i]) if i < len(raw_tools) else "safe"
                op_creates.append(
                    ConnectorOperationCreate(
                        connector_id=connector_id,
                        tenant_id=user.tenant_id,
                        operation_id=op.operation_id,
                        name=op.name,
                        description=op.description,
                        category=op.category,
                        parameters=list(op.parameters),
                        search_content=search_content,
                        safety_level=safety_level,
                    )
                )

            ops_count = 0
            if op_creates:
                ops_count = await op_repo.create_operations_bulk(op_creates)

            # Register entity types
            try:
                from meho_app.modules.connectors.mcp.types import MCP_TYPES
                from meho_app.modules.connectors.schemas import ConnectorEntityTypeCreate

                for type_def in MCP_TYPES:
                    from meho_app.modules.connectors.repositories import (
                        ConnectorEntityTypeRepository,
                    )

                    type_repo = ConnectorEntityTypeRepository(session)
                    await type_repo.create_entity_type(
                        ConnectorEntityTypeCreate(
                            connector_id=connector_id,
                            tenant_id=user.tenant_id,
                            type_name=type_def.type_name,
                            description=type_def.description,
                            category=type_def.category,
                            properties=list(type_def.properties),
                            search_content=f"{type_def.type_name} {type_def.description} mcp",
                        )
                    )
            except Exception as e:
                logger.warning("Failed to register MCP entity types: %s", e)

            await session.commit()

            # Create knowledge chunks for hybrid search
            chunks_created = 0
            try:
                from meho_app.modules.knowledge.embeddings import get_embedding_provider
                from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
                from meho_app.modules.knowledge.repository import KnowledgeRepository

                knowledge_repo = KnowledgeRepository(session)
                embedding_provider = get_embedding_provider()
                knowledge_store = KnowledgeStore(
                    repository=knowledge_repo,
                    embedding_provider=embedding_provider,
                )

                from meho_app.modules.connectors.mcp.sync import sync_mcp_knowledge_chunks

                chunks_created = await sync_mcp_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    server_name=server_name,
                    tenant_id=user.tenant_id,
                    operations=discovered_ops,
                )
                await session.commit()
                logger.info("Created %d knowledge chunks for MCP operations", chunks_created)
            except Exception as e:
                logger.warning("Failed to create knowledge chunks: %s", e)

            logger.info(
                "Created MCP connector '%s' with %d operations and %d knowledge chunks",
                request.name,
                ops_count,
                chunks_created,
            )

            return MCPConnectorResponse(
                id=connector_id,
                name=request.name,
                server_url=request.server_url,
                transport_type=request.transport_type,
                connector_type="mcp",
                tools_discovered=tools_discovered,
                operations_registered=ops_count,
                message=(
                    f"MCP connector created successfully. "
                    f"Discovered {tools_discovered} tools, "
                    f"registered {ops_count} operations and "
                    f"{chunks_created} knowledge chunks."
                ),
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Create MCP connector failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
