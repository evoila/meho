# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Jira connector registration endpoint.

Handles Jira Cloud connector creation with AtlassianHTTPConnector base,
email:api_token Basic Auth, and test connection via /rest/api/3/myself.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.connectors.schemas import (
    CreateJiraConnectorRequest,
    JiraConnectorResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


@router.post(
    "/jira",
    response_model=JiraConnectorResponse,
    responses={
        400: {"description": "Could not connect to Jira. Check site URL, email, and API..."},
        500: {"description": "Internal server error"},
    },
)
async def create_jira_connector(
    request: CreateJiraConnectorRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_CREATE))],
):
    """
    Create a Jira Cloud typed connector.

    Creates a typed connector using httpx for native Jira REST API v3 access.
    Operations are pre-registered based on the Jira connector implementation.
    The connector will be tested during creation to verify connectivity and
    project access.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.jira import (
        JIRA_OPERATIONS,
        JIRA_OPERATIONS_VERSION,
        WRITE_OPERATIONS,
        JiraConnector,
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

    # Clean up site URL
    site_url = request.site_url.strip().rstrip("/")

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            connector_service = ConnectorService(session)
            op_repo = ConnectorOperationRepository(session)

            # Build protocol config
            protocol_config = {
                "base_url": site_url,
                "operations_version": JIRA_OPERATIONS_VERSION,
            }

            # Create connector record
            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description,
                routing_description=request.routing_description,
                base_url=site_url,
                auth_type="BASIC",
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="jira",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection
            jira_user = None
            accessible_projects = 0
            logger.info(f"Testing Jira connection: {site_url}")
            try:
                creds = {
                    "email": request.email,
                    "api_token": request.api_token,
                }

                jira = JiraConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials=creds,
                )
                await jira.connect()
                is_connected = await jira.test_connection()
                jira_user = jira.jira_user
                accessible_projects = jira.accessible_projects
                await jira.disconnect()

                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not connect to Jira. Check site URL, email, and API token.",
                    )

                logger.info(
                    f"Jira connection verified: {site_url} "
                    f"(user: {jira_user}, projects: {accessible_projects})"
                )
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"Jira connection test failed: {e}")
                # Don't fail -- allow creation even if we can't test immediately

            # Store user credentials
            cred_repo = UserCredentialRepository(session)
            await cred_repo.store_credentials(
                user_id=user.user_id,
                credential=UserCredentialProvide(
                    connector_id=connector_id,
                    credential_type="API_KEY",
                    credentials={
                        "email": request.email,
                        "api_token": request.api_token,
                    },
                ),
            )

            # Register operations with safety_level
            op_creates = []
            for op in JIRA_OPERATIONS:
                search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
                safety_level = "write" if op.operation_id in WRITE_OPERATIONS else "read"
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

                from meho_app.modules.connectors.jira.sync import (
                    _sync_jira_knowledge_chunks,
                )

                chunks_created = await _sync_jira_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(f"Created {chunks_created} knowledge chunks for Jira operations")
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks: {e}")

            logger.info(
                f"Created Jira connector '{request.name}' "
                f"with {ops_count} operations "
                f"and {chunks_created} knowledge chunks"
            )

            return JiraConnectorResponse(
                id=connector_id,
                name=request.name,
                site_url=site_url,
                connector_type="jira",
                jira_user=jira_user,
                accessible_projects=accessible_projects,
                operations_registered=ops_count,
                message=f"Jira connector created successfully. "
                f"Registered {ops_count} operations and {chunks_created} knowledge chunks.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create Jira connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
