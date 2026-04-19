# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub connector registration endpoint.

Handles GitHub connector creation with PAT Bearer token auth,
rate limit tracking, and test connection via /user endpoint.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.connectors.schemas import (
    CreateGitHubConnectorRequest,
    GitHubConnectorResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


@router.post(
    "/github",
    response_model=GitHubConnectorResponse,
    responses={
        400: {"description": "Could not connect to GitHub. Check PAT and organization."},
        500: {"description": "Internal server error"},
    },
)
async def create_github_connector(
    request: CreateGitHubConnectorRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_CREATE))],
):
    """
    Create a GitHub typed connector.

    Creates a typed connector using httpx for GitHub REST API access.
    Operations are pre-registered based on the GitHub connector implementation.
    The connector will be tested during creation to verify PAT and org access.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.github import (
        GITHUB_OPERATIONS,
        GITHUB_OPERATIONS_VERSION,
        WRITE_OPERATIONS,
        GitHubConnector,
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

    base_url = (request.base_url or "https://api.github.com").strip().rstrip("/")

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            connector_service = ConnectorService(session)
            op_repo = ConnectorOperationRepository(session)

            protocol_config = {
                "base_url": base_url,
                "organization": request.organization,
                "operations_version": GITHUB_OPERATIONS_VERSION,
            }

            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description,
                routing_description=request.routing_description,
                base_url=base_url,
                auth_type="API_KEY",
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="github",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection
            logger.info(f"Testing GitHub connection: {base_url} (org: {request.organization})")
            try:
                creds = {"token": request.personal_access_token}

                gh = GitHubConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials=creds,
                )
                await gh.connect()
                is_connected = await gh.test_connection()
                await gh.disconnect()

                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not connect to GitHub. Check PAT and organization.",
                    )

                logger.info(f"GitHub connection verified: {base_url} (org: {request.organization})")
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"GitHub connection test failed: {e}")

            # Store user credentials
            cred_repo = UserCredentialRepository(session)
            await cred_repo.store_credentials(
                user_id=user.user_id,
                credential=UserCredentialProvide(
                    connector_id=connector_id,
                    credential_type="API_KEY",
                    credentials={"token": request.personal_access_token},
                ),
            )

            # Register operations with safety_level
            op_creates = []
            for op in GITHUB_OPERATIONS:
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

                from meho_app.modules.connectors.github.sync import (
                    _sync_github_knowledge_chunks,
                )

                chunks_created = await _sync_github_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(f"Created {chunks_created} knowledge chunks for GitHub operations")
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks: {e}")

            logger.info(
                f"Created GitHub connector '{request.name}' "
                f"with {ops_count} operations "
                f"and {chunks_created} knowledge chunks"
            )

            return GitHubConnectorResponse(
                id=connector_id,
                name=request.name,
                base_url=base_url,
                organization=request.organization,
                connector_type="github",
                operations_registered=ops_count,
                message=f"GitHub connector created successfully. "
                f"Registered {ops_count} operations and {chunks_created} knowledge chunks.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create GitHub connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
