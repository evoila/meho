# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus connector operations.

Handles Prometheus connector creation with ObservabilityHTTPConnector base,
configurable auth (none/basic/bearer), and test connection via buildinfo.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.connectors.schemas import (
    CreatePrometheusConnectorRequest,
    PrometheusConnectorResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


@router.post("/prometheus", response_model=PrometheusConnectorResponse)
async def create_prometheus_connector(
    request: CreatePrometheusConnectorRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_CREATE)),
):
    """
    Create a Prometheus typed connector.

    Creates a typed connector using httpx for native Prometheus HTTP API access.
    Operations are pre-registered based on the Prometheus connector implementation.
    The connector will be tested during creation to verify connectivity.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.prometheus import (
        PROMETHEUS_OPERATIONS,
        PROMETHEUS_OPERATIONS_VERSION,
        PROMETHEUS_TYPES,
        PrometheusConnector,
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
            type_repo = ConnectorTypeRepository(session)

            # Build protocol config
            protocol_config = {
                "base_url": base_url,
                "skip_tls_verification": request.skip_tls_verification,
                "auth_type": request.auth_type,
                "operations_version": PROMETHEUS_OPERATIONS_VERSION,
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
                connector_type="prometheus",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection
            prometheus_version = None
            logger.info(f"Testing Prometheus connection: {base_url}")
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

                prom = PrometheusConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials=creds,
                )
                await prom.connect()
                is_connected = await prom.test_connection()
                prometheus_version = prom.prometheus_version
                await prom.disconnect()

                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not connect to Prometheus. Check URL and credentials.",
                    )

                logger.info(
                    f"Prometheus connection verified: {base_url} (version: {prometheus_version})"
                )
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"Prometheus connection test failed: {e}")
                # Don't fail -- allow creation even if we can't test immediately

            # Store prometheus_version in protocol_config
            if prometheus_version:
                protocol_config["prometheus_version"] = prometheus_version
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

            # Register operations
            op_creates = []
            for op in PROMETHEUS_OPERATIONS:
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

            # Register entity types
            type_creates = []
            for t in PROMETHEUS_TYPES:
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

                from meho_app.modules.connectors.prometheus.sync import (
                    _sync_prometheus_knowledge_chunks,
                )

                chunks_created = await _sync_prometheus_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(f"Created {chunks_created} knowledge chunks for Prometheus operations")
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks: {e}")

            logger.info(
                f"Created Prometheus connector '{request.name}' "
                f"with {ops_count} operations, {types_count} types, "
                f"and {chunks_created} knowledge chunks"
            )

            return PrometheusConnectorResponse(
                id=connector_id,
                name=request.name,
                base_url=base_url,
                connector_type="prometheus",
                prometheus_version=prometheus_version,
                auth_type=request.auth_type,
                operations_registered=ops_count,
                types_registered=types_count,
                message=f"Prometheus connector created successfully. "
                f"Registered {ops_count} operations and {chunks_created} knowledge chunks.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create Prometheus connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
