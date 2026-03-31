# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP connector operations.

Handles Google Cloud Platform connector creation with native SDK access.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.connectors.schemas import (
    CreateGCPConnectorRequest,
    GCPConnectorResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


@router.post("/gcp", response_model=GCPConnectorResponse)
async def create_gcp_connector(
    request: CreateGCPConnectorRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_CREATE)),
):
    """
    Create a Google Cloud Platform connector.

    This creates a typed connector using native Google Cloud SDKs for access to:
    - Compute Engine: VMs, disks, snapshots
    - GKE: Kubernetes clusters, node pools
    - Networking: VPCs, subnets, firewalls
    - Cloud Monitoring: Metrics, alerts

    The connector will be tested during creation to verify connectivity.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.gcp import (
        GCP_OPERATIONS,
        GCP_OPERATIONS_VERSION,
        GCP_TYPES,
        GCPConnector,
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
                "project_id": request.project_id,
                "default_region": request.default_region,
                "default_zone": request.default_zone,
                "operations_version": GCP_OPERATIONS_VERSION,
            }

            # Create connector record
            # Use a placeholder base_url since GCP SDK doesn't use HTTP directly
            base_url = (
                f"https://console.cloud.google.com/home/dashboard?project={request.project_id}"
            )

            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description,
                base_url=base_url,
                auth_type="API_KEY",  # Service account JSON is stored as credential
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="gcp",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection
            logger.info(f"🔌 Testing GCP connection: project={request.project_id}")
            try:
                gcp = GCPConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials={
                        "service_account_json": request.service_account_json,
                    },
                )
                await gcp.connect()
                is_connected = await gcp.test_connection()
                await gcp.disconnect()

                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not connect to GCP. Check project ID and service account credentials.",
                    )

                logger.info(f"✅ GCP connection verified: project={request.project_id}")
            except ImportError:
                logger.warning("⚠️ google-cloud SDKs not installed - skipping connection test")
            except Exception as e:
                logger.warning(f"⚠️ GCP connection test failed: {e}")
                # Don't fail - allow creation even if we can't test immediately

            # Store user credentials (service account JSON)
            cred_repo = UserCredentialRepository(session)
            await cred_repo.store_credentials(
                user_id=user.user_id,
                credential=UserCredentialProvide(
                    connector_id=connector_id,
                    credential_type="API_KEY",
                    credentials={
                        "service_account_json": request.service_account_json,
                    },
                ),
            )

            # Register operations
            op_creates = []
            for op in GCP_OPERATIONS:
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
            for t in GCP_TYPES:
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
                from meho_app.modules.connectors.gcp.sync import _sync_gcp_knowledge_chunks
                from meho_app.modules.knowledge.embeddings import get_embedding_provider
                from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
                from meho_app.modules.knowledge.repository import KnowledgeRepository

                knowledge_repo = KnowledgeRepository(session)
                embedding_provider = get_embedding_provider()  # Voyage AI 1024D singleton
                knowledge_store = KnowledgeStore(
                    repository=knowledge_repo,
                    embedding_provider=embedding_provider,
                )

                chunks_created = await _sync_gcp_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(f"✅ Created {chunks_created} knowledge chunks for GCP operations")
            except Exception as e:
                logger.warning(f"⚠️ Failed to create knowledge chunks: {e}")
                # Don't fail connector creation if knowledge sync fails

            logger.info(
                f"✅ Created GCP connector '{request.name}' "
                f"with {ops_count} operations, {types_count} types, and {chunks_created} knowledge chunks"
            )

            return GCPConnectorResponse(
                id=connector_id,
                name=request.name,
                project_id=request.project_id,
                connector_type="gcp",
                operations_registered=ops_count,
                types_registered=types_count,
                message=f"GCP connector created successfully. Registered {ops_count} operations and {chunks_created} knowledge chunks.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create GCP connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
