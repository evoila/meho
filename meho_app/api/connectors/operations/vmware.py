# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware connector operations.

Handles VMware vSphere connector creation with native pyvmomi access.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.auth import get_current_user
from meho_app.api.connectors.schemas import (
    CreateVMwareConnectorRequest,
    VMwareConnectorResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.post(
    "/vmware",
    response_model=VMwareConnectorResponse,
    responses={
        400: {"description": "Could not connect to vCenter. Check host and credentials."},
        500: {"description": "Internal server error"},
    },
)
async def create_vmware_connector(
    request: CreateVMwareConnectorRequest, user: Annotated[UserContext, Depends(get_current_user)]
):
    """
    Create a VMware vSphere connector.

    This creates a typed connector using pyvmomi for native vSphere access.
    Operations are pre-registered based on the VMware connector implementation.

    The connector will be tested during creation to verify connectivity.
    """
    from meho_app.api.database import create_openapi_session_maker
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
    from meho_app.modules.connectors.vmware import (
        VMWARE_OPERATIONS,
        VMWARE_OPERATIONS_VERSION,
        VMWARE_TYPES,
        VMwareConnector,
    )

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            connector_service = ConnectorService(session)
            op_repo = ConnectorOperationRepository(session)
            type_repo = ConnectorTypeRepository(session)

            # Clean up vcenter_host - strip protocol prefix if user included it
            vcenter_host = request.vcenter_host.strip()
            if vcenter_host.startswith("https://"):
                vcenter_host = vcenter_host[8:]
            elif vcenter_host.startswith("http://"):
                vcenter_host = vcenter_host[7:]
            vcenter_host = vcenter_host.rstrip("/")

            # Build protocol config with operations version for auto-sync
            protocol_config = {
                "vcenter_host": vcenter_host,
                "port": request.port,
                "disable_ssl_verification": request.disable_ssl_verification,
                "operations_version": VMWARE_OPERATIONS_VERSION,
            }

            # Create connector record
            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description,
                # Safe (tainted-url-host): admin-configured vCenter host (connector setup requires admin auth)
                base_url=f"https://{vcenter_host}:{request.port}",
                auth_type="SESSION",
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="vmware",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection
            logger.info(f"🔌 Testing vCenter connection: {vcenter_host}")
            try:
                vmware = VMwareConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials={
                        "username": request.username,
                        "password": request.password,
                    },
                )
                await vmware.connect()
                is_connected = await vmware.test_connection()
                await vmware.disconnect()

                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not connect to vCenter. Check host and credentials.",
                    )

                logger.info(f"✅ vCenter connection verified: {vcenter_host}")
            except ImportError:
                logger.warning("⚠️ pyvmomi not installed - skipping connection test")
            except Exception as e:
                logger.warning(f"⚠️ vCenter connection test failed: {e}")
                # Don't fail - allow creation even if we can't test immediately

            # Store user credentials
            cred_repo = UserCredentialRepository(session)
            await cred_repo.store_credentials(
                user_id=user.user_id,
                credential=UserCredentialProvide(
                    connector_id=connector_id,
                    credential_type="PASSWORD",
                    credentials={
                        "username": request.username,
                        "password": request.password,
                    },
                ),
            )

            # Register operations
            op_creates = []
            for op in VMWARE_OPERATIONS:
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
            for t in VMWARE_TYPES:
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
                from meho_app.modules.connectors.vmware.sync import _sync_vmware_knowledge_chunks
                from meho_app.modules.knowledge.embeddings import get_embedding_provider
                from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
                from meho_app.modules.knowledge.repository import KnowledgeRepository

                knowledge_repo = KnowledgeRepository(session)
                embedding_provider = get_embedding_provider()  # Voyage AI 1024D singleton
                knowledge_store = KnowledgeStore(
                    repository=knowledge_repo,
                    embedding_provider=embedding_provider,
                )

                chunks_created = await _sync_vmware_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(f"✅ Created {chunks_created} knowledge chunks for VMware operations")
            except Exception as e:
                logger.warning(f"⚠️ Failed to create knowledge chunks: {e}")
                # Don't fail connector creation if knowledge sync fails

            logger.info(
                f"✅ Created VMware connector '{request.name}' "
                f"with {ops_count} operations, {types_count} types, and {chunks_created} knowledge chunks"
            )

            return VMwareConnectorResponse(
                id=connector_id,
                name=request.name,
                vcenter_host=vcenter_host,
                connector_type="vmware",
                operations_registered=ops_count,
                types_registered=types_count,
                message=f"VMware connector created successfully. Registered {ops_count} operations and {chunks_created} knowledge chunks.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create VMware connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
