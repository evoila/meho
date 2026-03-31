# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD connector registration endpoint.

Handles ArgoCD connector creation with Bearer token auth (PAT),
configurable SSL verification, and test connection via /api/v1/applications.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.auth import get_current_user
from meho_app.api.connectors.schemas import (
    ArgoConnectorResponse,
    CreateArgoConnectorRequest,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.post("/argocd", response_model=ArgoConnectorResponse)
async def create_argocd_connector(
    request: CreateArgoConnectorRequest,
    user: UserContext = Depends(get_current_user),
):
    """
    Create an ArgoCD typed connector.

    Creates a typed connector using httpx for ArgoCD REST API access.
    Operations are pre-registered based on the ArgoCD connector implementation.
    The connector will be tested during creation to verify connectivity.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.argocd import (
        ARGOCD_OPERATIONS,
        ARGOCD_OPERATIONS_VERSION,
        DESTRUCTIVE_OPERATIONS,
        WRITE_OPERATIONS,
        ArgoConnector,
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

    server_url = request.server_url.strip().rstrip("/")

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            connector_service = ConnectorService(session)
            op_repo = ConnectorOperationRepository(session)

            protocol_config = {
                "base_url": server_url,
                "verify_ssl": not request.skip_tls_verification,
                "operations_version": ARGOCD_OPERATIONS_VERSION,
            }

            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description,
                routing_description=request.routing_description,
                base_url=server_url,
                auth_type="API_KEY",
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="argocd",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection
            logger.info(f"Testing ArgoCD connection: {server_url}")
            try:
                creds = {"api_token": request.api_token}

                argo = ArgoConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials=creds,
                )
                await argo.connect()
                is_connected = await argo.test_connection()
                await argo.disconnect()

                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not connect to ArgoCD. Check server URL and API token.",
                    )

                logger.info(f"ArgoCD connection verified: {server_url}")
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"ArgoCD connection test failed: {e}")

            # Store user credentials
            cred_repo = UserCredentialRepository(session)
            await cred_repo.store_credentials(
                user_id=user.user_id,
                credential=UserCredentialProvide(
                    connector_id=connector_id,
                    credential_type="API_KEY",
                    credentials={"api_token": request.api_token},
                ),
            )

            # Register operations with safety_level
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
                        tenant_id=user.tenant_id,
                        operation_id=op.operation_id,
                        name=op.name,
                        description=op.description,
                        category=op.category,
                        parameters=list(op.parameters),
                        example=op.example,
                        search_content=search_content,
                        safety_level=safety_level,
                    )
                )

            ops_count = 0
            if op_creates:
                ops_count = await op_repo.create_operations_bulk(op_creates)

            await session.commit()

            # Create knowledge chunks for hybrid search (BM25 + semantic)
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

                from meho_app.modules.connectors.argocd.sync import (
                    _sync_argocd_knowledge_chunks,
                )

                chunks_created = await _sync_argocd_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(f"Created {chunks_created} knowledge chunks for ArgoCD operations")
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks: {e}")

            logger.info(
                f"Created ArgoCD connector '{request.name}' "
                f"with {ops_count} operations "
                f"and {chunks_created} knowledge chunks"
            )

            return ArgoConnectorResponse(
                id=connector_id,
                name=request.name,
                server_url=server_url,
                connector_type="argocd",
                operations_registered=ops_count,
                message=f"ArgoCD connector created successfully. "
                f"Registered {ops_count} operations and {chunks_created} knowledge chunks.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create ArgoCD connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
