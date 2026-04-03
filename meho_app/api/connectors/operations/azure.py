# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Azure connector operations.

Handles Microsoft Azure connector creation with native async SDK access.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.auth import get_current_user
from meho_app.api.connectors.schemas import (
    AzureConnectorResponse,
    CreateAzureConnectorRequest,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.post(
    "/azure",
    response_model=AzureConnectorResponse,
    responses={
        400: {"description": "Could not connect to Azure. Check tenant ID, client ID, c..."},
        500: {"description": "Internal server error"},
    },
)
async def create_azure_connector(
    request: CreateAzureConnectorRequest, user: Annotated[UserContext, Depends(get_current_user)]
):
    """
    Create a Microsoft Azure connector.

    This creates a typed connector using native Azure async SDKs for access to:
    - Compute: VMs, managed disks
    - Monitor: Metrics, alerts, activity log
    - AKS: Kubernetes clusters, node pools
    - Networking: VNets, subnets, NSGs, load balancers
    - Storage: Storage accounts, containers
    - Web: App Service, Function Apps

    The connector will be tested during creation to verify connectivity.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.azure import (
        AZURE_OPERATIONS,
        AZURE_OPERATIONS_VERSION,
        AZURE_TYPES,
        AzureConnector,
    )
    from meho_app.modules.connectors.repositories import (
        ConnectorOperationRepository,
        ConnectorTypeRepository,
    )
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )
    from meho_app.modules.connectors.schemas import (
        ConnectorCreate,
        ConnectorEntityTypeCreate,
        ConnectorOperationCreate,
        UserCredentialProvide,
    )
    from meho_app.modules.connectors.service import ConnectorService

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            connector_service = ConnectorService(session)
            op_repo = ConnectorOperationRepository(session)
            type_repo = ConnectorTypeRepository(session)

            # Build protocol config with operations version for auto-sync
            protocol_config = {
                "subscription_id": request.subscription_id,
                "resource_group_filter": request.resource_group_filter,
                "operations_version": AZURE_OPERATIONS_VERSION,
            }

            # Build Azure Portal URL for the connector base_url
            base_url = (
                f"https://portal.azure.com/#@{request.tenant_id}"
                f"/resource/subscriptions/{request.subscription_id}"
            )

            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description,
                base_url=base_url,
                auth_type="API_KEY",  # Service principal credentials stored as credential
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="azure",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection
            logger.info(f"Testing Azure connection: subscription={request.subscription_id}")
            try:
                azure = AzureConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials={
                        "tenant_id": request.tenant_id,
                        "client_id": request.client_id,
                        "client_secret": request.client_secret,
                        "subscription_id": request.subscription_id,
                    },
                )
                await azure.connect()
                is_connected = await azure.test_connection()
                await azure.disconnect()

                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not connect to Azure. Check tenant ID, client ID, client secret, and subscription ID.",
                    )

                logger.info(f"Azure connection verified: subscription={request.subscription_id}")
            except ImportError:
                logger.warning("Azure SDKs not installed - skipping connection test")
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"Azure connection test failed: {e}")
                # Don't fail - allow creation even if we can't test immediately

            # Store user credentials (service principal)
            cred_repo = UserCredentialRepository(session)
            await cred_repo.store_credentials(
                user_id=user.user_id,
                credential=UserCredentialProvide(
                    connector_id=connector_id,
                    credential_type="API_KEY",
                    credentials={
                        "tenant_id": request.tenant_id,
                        "client_id": request.client_id,
                        "client_secret": request.client_secret,
                        "subscription_id": request.subscription_id,
                    },
                ),
            )

            # Register operations
            op_creates = []
            for op in AZURE_OPERATIONS:
                search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
                op_creates.append(
                    ConnectorOperationCreate(
                        connector_id=connector_id,
                        tenant_id=user.tenant_id,
                        operation_id=op.operation_id,
                        name=op.name,
                        description=op.description,
                        category=op.category,
                        parameters=list(op.parameters),
                        example=op.example,
                        search_content=search_content,
                    )
                )

            ops_count = await op_repo.create_operations_bulk(op_creates)

            # Register entity types
            type_creates = []
            for t in AZURE_TYPES:
                prop_names = " ".join(p.get("name", "") for p in t.properties)
                search_content = f"{t.type_name} {t.description} {t.category} {prop_names}"
                type_creates.append(
                    ConnectorEntityTypeCreate(
                        connector_id=connector_id,
                        tenant_id=user.tenant_id,
                        type_name=t.type_name,
                        description=t.description,
                        category=t.category,
                        properties=list(t.properties),
                        search_content=search_content,
                    )
                )

            types_count = await type_repo.create_types_bulk(type_creates)

            await session.commit()

            # Create knowledge chunks for searchable operations
            chunks_created = 0
            try:
                from meho_app.modules.connectors.azure.sync import _sync_azure_knowledge_chunks
                from meho_app.modules.knowledge.embeddings import get_embedding_provider
                from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
                from meho_app.modules.knowledge.repository import KnowledgeRepository

                knowledge_repo = KnowledgeRepository(session)
                embedding_provider = get_embedding_provider()
                knowledge_store = KnowledgeStore(
                    repository=knowledge_repo,
                    embedding_provider=embedding_provider,
                )

                chunks_created = await _sync_azure_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(f"Created {chunks_created} knowledge chunks for Azure operations")
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks: {e}")
                # Don't fail connector creation if knowledge sync fails

            logger.info(
                f"Created Azure connector '{request.name}' "
                f"with {ops_count} operations, {types_count} types, and {chunks_created} knowledge chunks"
            )

            return AzureConnectorResponse(
                id=connector_id,
                name=request.name,
                subscription_id=request.subscription_id,
                connector_type="azure",
                operations_registered=ops_count,
                types_registered=types_count,
                message=f"Azure connector created successfully. Registered {ops_count} operations and {chunks_created} knowledge chunks.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create Azure connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
