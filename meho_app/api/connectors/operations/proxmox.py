# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox connector operations.

Handles Proxmox VE connector creation with native proxmoxer access.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.connectors.schemas import (
    CreateProxmoxConnectorRequest,
    ProxmoxConnectorResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


@router.post("/proxmox", response_model=ProxmoxConnectorResponse)
async def create_proxmox_connector(  # NOSONAR (cognitive complexity)
    request: CreateProxmoxConnectorRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_CREATE)),
):
    """
    Create a Proxmox VE connector.

    This creates a typed connector using proxmoxer for native Proxmox API access.
    Operations are pre-registered based on the Proxmox connector implementation.

    Supports both API token authentication (recommended) and username/password.
    The connector will be tested during creation to verify connectivity.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.proxmox import (
        PROXMOX_OPERATIONS,
        PROXMOX_OPERATIONS_VERSION,
        PROXMOX_TYPES,
        ProxmoxConnector,
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

    # Validate authentication - need either password or API token
    has_password_auth = request.username and request.password
    has_token_auth = request.api_token_id and request.api_token_secret

    if not has_password_auth and not has_token_auth:
        raise HTTPException(
            status_code=400,
            detail="Either (username + password) or (api_token_id + api_token_secret) must be provided",
        )

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            connector_service = ConnectorService(session)
            op_repo = ConnectorOperationRepository(session)
            type_repo = ConnectorTypeRepository(session)

            # Clean up host - strip protocol prefix if user included it
            host = request.host.strip()
            if host.startswith("https://"):
                host = host[8:]
            elif host.startswith("http://"):
                host = host[7:]
            host = host.rstrip("/")

            # Build protocol config with operations version for auto-sync
            protocol_config = {
                "host": host,
                "port": request.port,
                "disable_ssl_verification": request.disable_ssl_verification,
                "operations_version": PROXMOX_OPERATIONS_VERSION,
            }

            # Determine auth type
            auth_type = "API_KEY" if has_token_auth else "SESSION"

            # Create connector record
            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description,
                # nosemgrep: tainted-url-host -- admin-configured Proxmox host (connector setup requires admin auth)
                base_url=f"https://{host}:{request.port}",
                auth_type=auth_type,
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="proxmox",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Build credentials dict
            credentials = {}
            if has_token_auth:
                credentials["api_token_id"] = request.api_token_id
                credentials["api_token_secret"] = request.api_token_secret
            else:
                credentials["username"] = request.username
                credentials["password"] = request.password

            # Test connection
            logger.info(f"🔌 Testing Proxmox connection: {host}")
            try:
                proxmox = ProxmoxConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials=credentials,
                )
                await proxmox.connect()
                is_connected = await proxmox.test_connection()
                await proxmox.disconnect()

                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not connect to Proxmox. Check host and credentials.",
                    )

                logger.info(f"✅ Proxmox connection verified: {host}")
            except ImportError:
                logger.warning("⚠️ proxmoxer not installed - skipping connection test")
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"⚠️ Proxmox connection test failed: {e}")
                # Don't fail - allow creation even if we can't test immediately

            # Store user credentials
            cred_repo = UserCredentialRepository(session)
            credential_type = "API_KEY" if has_token_auth else "PASSWORD"
            await cred_repo.store_credentials(
                user_id=user.user_id,
                credential=UserCredentialProvide(
                    connector_id=connector_id,
                    credential_type=credential_type,
                    credentials=credentials,
                ),
            )

            # Register operations
            op_creates = []
            for op in PROXMOX_OPERATIONS:
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
            for t in PROXMOX_TYPES:
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
            # NOTE: After the connector-scoped migration (08-01), operation-related
            # knowledge chunks are purged. These chunks targeted knowledge_chunk for
            # hybrid search but operations now live exclusively in ConnectorOperationModel.
            # The embedding fix is retained for safety in case this code path is
            # re-enabled for a different purpose.
            chunks_created = 0
            try:
                from meho_app.modules.connectors.proxmox.sync import _sync_proxmox_knowledge_chunks
                from meho_app.modules.knowledge.embeddings import get_embedding_provider
                from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
                from meho_app.modules.knowledge.repository import KnowledgeRepository

                knowledge_repo = KnowledgeRepository(session)
                embedding_provider = get_embedding_provider()  # Voyage AI 1024D singleton
                knowledge_store = KnowledgeStore(
                    repository=knowledge_repo,
                    embedding_provider=embedding_provider,
                )

                chunks_created = await _sync_proxmox_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(f"✅ Created {chunks_created} knowledge chunks for Proxmox operations")
            except Exception as e:
                logger.warning(f"⚠️ Failed to create knowledge chunks: {e}")
                # Don't fail connector creation if knowledge sync fails

            logger.info(
                f"✅ Created Proxmox connector '{request.name}' "
                f"with {ops_count} operations, {types_count} types, and {chunks_created} knowledge chunks"
            )

            return ProxmoxConnectorResponse(
                id=connector_id,
                name=request.name,
                host=host,
                connector_type="proxmox",
                operations_registered=ops_count,
                types_registered=types_count,
                message=f"Proxmox connector created successfully. Registered {ops_count} operations and {chunks_created} knowledge chunks.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create Proxmox connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
