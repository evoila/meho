# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Alertmanager connector operations.

Handles Alertmanager connector creation with ObservabilityHTTPConnector base,
configurable auth (none/basic/bearer), and test connection via
/api/v2/status or /-/ready.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.auth import get_current_user
from meho_app.api.connectors.schemas import (
    AlertmanagerConnectorResponse,
    CreateAlertmanagerConnectorRequest,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.post("/alertmanager", response_model=AlertmanagerConnectorResponse)
async def create_alertmanager_connector(
    request: CreateAlertmanagerConnectorRequest,
    user: UserContext = Depends(get_current_user),
):
    """
    Create an Alertmanager typed connector.

    Creates a typed connector using httpx for native Alertmanager v2 HTTP API access.
    Operations are pre-registered based on the Alertmanager connector implementation.
    The connector will be tested during creation to verify connectivity.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.alertmanager import (
        ALERTMANAGER_OPERATIONS,
        ALERTMANAGER_OPERATIONS_VERSION,
        AlertmanagerConnector,
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

    # Clean up base URL
    base_url = request.base_url.strip().rstrip("/")

    # Map auth_type to database auth_type enum
    auth_type_map = {
        "none": "NONE",
        "basic": "BASIC",
        "bearer": "OAUTH2",
    }
    db_auth_type = auth_type_map.get(request.auth_type, "NONE")

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            connector_service = ConnectorService(session)
            op_repo = ConnectorOperationRepository(session)

            # Build protocol config
            protocol_config = {
                "base_url": base_url,
                "skip_tls_verification": request.skip_tls_verification,
                "auth_type": request.auth_type,
                "operations_version": ALERTMANAGER_OPERATIONS_VERSION,
            }

            # Create connector record
            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description,
                routing_description=request.routing_description,
                base_url=base_url,
                auth_type=db_auth_type,
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="alertmanager",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection
            alertmanager_version = None
            logger.info(f"Testing Alertmanager connection: {base_url}")
            try:
                # Build credentials dict
                creds: dict = {}
                if request.auth_type == "basic":
                    creds = {
                        "username": request.username or "",
                        "password": request.password or "",
                    }
                elif request.auth_type == "bearer":
                    creds = {"token": request.token or ""}

                am = AlertmanagerConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials=creds,
                )
                await am.connect()
                is_connected = await am.test_connection()
                alertmanager_version = am.alertmanager_version
                await am.disconnect()

                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not connect to Alertmanager. Check URL and credentials.",
                    )

                logger.info(
                    f"Alertmanager connection verified: {base_url} "
                    f"(version: {alertmanager_version})"
                )
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"Alertmanager connection test failed: {e}")
                # Don't fail -- allow creation even if we can't test immediately

            # Store alertmanager_version in protocol_config
            if alertmanager_version:
                protocol_config["alertmanager_version"] = alertmanager_version
                import uuid

                from sqlalchemy import select

                from meho_app.modules.connectors.models import ConnectorModel

                query = select(ConnectorModel).where(ConnectorModel.id == uuid.UUID(connector_id))
                result = await session.execute(query)
                db_connector = result.scalar_one_or_none()
                if db_connector:
                    db_connector.protocol_config = protocol_config  # type: ignore[assignment]

            # Store user credentials
            cred_repo = UserCredentialRepository(session)
            if request.auth_type == "basic":
                await cred_repo.store_credentials(
                    user_id=user.user_id,
                    credential=UserCredentialProvide(
                        connector_id=connector_id,
                        credential_type="PASSWORD",
                        credentials={
                            "username": request.username or "",
                            "password": request.password or "",
                        },
                    ),
                )
            elif request.auth_type == "bearer":
                await cred_repo.store_credentials(
                    user_id=user.user_id,
                    credential=UserCredentialProvide(
                        connector_id=connector_id,
                        credential_type="OAUTH2_TOKEN",
                        credentials={
                            "access_token": request.token or "",
                        },
                    ),
                )

            # Register operations (empty initially -- Plan 02 populates ALERTMANAGER_OPERATIONS)
            op_creates = []
            for op in ALERTMANAGER_OPERATIONS:
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

            ops_count = 0
            if op_creates:
                ops_count = await op_repo.create_operations_bulk(op_creates)

            # No entity types for Alertmanager (alerts are ephemeral, not topology entities)

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

                from meho_app.modules.connectors.alertmanager.sync import (
                    _sync_alertmanager_knowledge_chunks,
                )

                chunks_created = await _sync_alertmanager_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(
                    f"Created {chunks_created} knowledge chunks for Alertmanager operations"
                )
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks: {e}")

            logger.info(
                f"Created Alertmanager connector '{request.name}' "
                f"with {ops_count} operations "
                f"and {chunks_created} knowledge chunks"
            )

            return AlertmanagerConnectorResponse(
                id=connector_id,
                name=request.name,
                base_url=base_url,
                connector_type="alertmanager",
                alertmanager_version=alertmanager_version,
                auth_type=request.auth_type,
                operations_registered=ops_count,
                message=f"Alertmanager connector created successfully. "
                f"Registered {ops_count} operations and {chunks_created} knowledge chunks.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create Alertmanager connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
