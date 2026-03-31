# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes connector operations.

Handles Kubernetes cluster connector creation with native kubernetes-asyncio access.
Uses pre-defined operations (TASK-159) instead of OpenAPI spec parsing.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.connectors.schemas import (
    CreateKubernetesConnectorRequest,
    KubernetesConnectorResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


@router.post("/kubernetes", response_model=KubernetesConnectorResponse)
async def create_kubernetes_connector(
    request: CreateKubernetesConnectorRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_CREATE)),
):
    """
    Create a Kubernetes typed connector.

    This creates a typed connector using kubernetes-asyncio for native K8s access.
    Operations are pre-registered based on the Kubernetes connector implementation.

    The connector will be tested during creation to verify connectivity.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.kubernetes import (
        KUBERNETES_OPERATIONS,
        KUBERNETES_OPERATIONS_VERSION,
        KUBERNETES_TYPES,
        KubernetesConnector,
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

    # Clean up server URL
    server_url = request.server_url.strip().rstrip("/")
    if not server_url.startswith("https://"):
        if server_url.startswith("http://"):
            # Allow http for local dev clusters (minikube, kind)
            logger.warning("⚠️ Using HTTP for Kubernetes API (not recommended for production)")
        else:
            # nosemgrep: tainted-url-host -- admin-configured K8s API server URL (connector setup requires admin auth)
            server_url = f"https://{server_url}"

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            connector_service = ConnectorService(session)
            op_repo = ConnectorOperationRepository(session)
            type_repo = ConnectorTypeRepository(session)

            # Build protocol config with operations version for auto-sync
            protocol_config = {
                "server_url": server_url,
                "skip_tls_verification": request.skip_tls_verification,
                "operations_version": KUBERNETES_OPERATIONS_VERSION,
            }
            if request.ca_certificate:
                protocol_config["ca_certificate"] = request.ca_certificate

            # Create connector record
            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description,
                routing_description=request.routing_description,
                base_url=server_url,
                auth_type="OAUTH2",  # Uses Authorization: Bearer <token>
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="kubernetes",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection and get K8s version
            kubernetes_version = None
            logger.info(f"🔌 Testing Kubernetes connection: {server_url}")
            try:
                k8s = KubernetesConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials={
                        "token": request.token,
                    },
                )
                await k8s.connect()
                is_connected = await k8s.test_connection()
                kubernetes_version = k8s.kubernetes_version
                await k8s.disconnect()

                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not connect to Kubernetes cluster. Check server URL and token.",
                    )

                logger.info(
                    f"✅ Kubernetes connection verified: {server_url} (version: {kubernetes_version})"
                )
            except ImportError:
                logger.warning("⚠️ kubernetes-asyncio not installed - skipping connection test")
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"⚠️ Kubernetes connection test failed: {e}")
                # Don't fail - allow creation even if we can't test immediately

            # Store kubernetes_version in protocol_config if we got it
            if kubernetes_version:
                protocol_config["kubernetes_version"] = kubernetes_version
                # Update connector's protocol_config
                import uuid

                from sqlalchemy import select

                from meho_app.modules.connectors.models import ConnectorModel

                query = select(ConnectorModel).where(ConnectorModel.id == uuid.UUID(connector_id))
                result = await session.execute(query)
                db_connector = result.scalar_one_or_none()
                if db_connector:
                    db_connector.protocol_config = protocol_config  # type: ignore[assignment]

            # Store user credentials (Bearer token)
            cred_repo = UserCredentialRepository(session)
            await cred_repo.store_credentials(
                user_id=user.user_id,
                credential=UserCredentialProvide(
                    connector_id=connector_id,
                    credential_type="OAUTH2_TOKEN",
                    credentials={
                        "access_token": request.token,
                    },
                ),
            )

            # Register operations
            op_creates = []
            for op in KUBERNETES_OPERATIONS:
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
            for t in KUBERNETES_TYPES:
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
            # This enables the agent to find operations via hybrid search
            # NOTE: After the connector-scoped migration (08-01), operation-related
            # knowledge chunks are purged. These chunks targeted knowledge_chunk for
            # hybrid search but operations now live exclusively in ConnectorOperationModel.
            # The embedding fix is retained for safety in case this code path is
            # re-enabled for a different purpose.
            chunks_created = 0
            try:
                from meho_app.modules.connectors.kubernetes.sync import (
                    _sync_kubernetes_knowledge_chunks,
                )
                from meho_app.modules.knowledge.embeddings import get_embedding_provider
                from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
                from meho_app.modules.knowledge.repository import KnowledgeRepository

                knowledge_repo = KnowledgeRepository(session)
                embedding_provider = get_embedding_provider()  # Voyage AI 1024D singleton
                knowledge_store = KnowledgeStore(
                    repository=knowledge_repo,
                    embedding_provider=embedding_provider,
                )

                chunks_created = await _sync_kubernetes_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(
                    f"✅ Created {chunks_created} knowledge chunks for Kubernetes operations"
                )
            except Exception as e:
                logger.warning(f"⚠️ Failed to create knowledge chunks: {e}")
                # Don't fail connector creation if knowledge sync fails

            logger.info(
                f"✅ Created Kubernetes connector '{request.name}' "
                f"with {ops_count} operations, {types_count} types, and {chunks_created} knowledge chunks"
            )

            return KubernetesConnectorResponse(
                id=connector_id,
                name=request.name,
                server_url=server_url,
                connector_type="kubernetes",
                kubernetes_version=kubernetes_version,
                operations_registered=ops_count,
                types_registered=types_count,
                message=f"Kubernetes connector created successfully. Registered {ops_count} operations and {chunks_created} knowledge chunks.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create Kubernetes connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
