# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Slack connector operations.

Handles Slack connector creation with bot token, optional app token
(Socket Mode), and optional user token (search.messages).
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.connectors.schemas import (
    CreateSlackConnectorRequest,
    SlackConnectorResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


@router.post(
    "/slack",
    response_model=SlackConnectorResponse,
    responses={
        400: {"description": "Could not connect to Slack. Check the bot token (xoxb-*)."},
        500: {"description": "Internal server error"},
    },
)
async def create_slack_connector(
    request: CreateSlackConnectorRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_CREATE))],
):
    """
    Create a Slack connector.

    This creates a typed connector using slack-bolt/slack-sdk for access to:
    - Channel history: Read messages from channels
    - Message search: Search across channels (requires user token)
    - Channel listing: Discover available channels
    - User info: Look up user profiles
    - Post message: Send messages and threaded replies
    - Add reaction: React to messages with emoji

    The connector will be tested during creation to verify the bot token.
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
    from meho_app.modules.connectors.slack import (
        SLACK_OPERATIONS,
        SLACK_OPERATIONS_VERSION,
        SLACK_TYPES,
        SlackConnector,
    )

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            connector_service = ConnectorService(session)
            op_repo = ConnectorOperationRepository(session)
            type_repo = ConnectorTypeRepository(session)

            # Build protocol config with operations version for auto-sync
            protocol_config = {
                "operations_version": SLACK_OPERATIONS_VERSION,
            }

            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description or "Slack connector",
                base_url="https://slack.com",
                auth_type="API_KEY",
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="slack",
                protocol_config=protocol_config,
            )

            connector = await connector_service.create_connector(connector_create)
            connector_id = str(connector.id)

            # Test connection if bot token provided
            credentials: dict[str, str] = {
                "slack_bot_token": request.slack_bot_token,
            }
            if request.slack_app_token:
                credentials["slack_app_token"] = request.slack_app_token
            if request.slack_user_token:
                credentials["slack_user_token"] = request.slack_user_token

            logger.info(f"Testing Slack connection for connector '{request.name}'")
            try:
                slack_connector = SlackConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials=credentials,
                )
                await slack_connector.connect()
                is_connected = await slack_connector.test_connection()
                await slack_connector.disconnect()

                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not connect to Slack. Check the bot token (xoxb-*).",
                    )

                logger.info(f"Slack connection verified for '{request.name}'")
            except ImportError:
                logger.warning("slack-bolt not installed -- skipping connection test")
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"Slack connection test failed: {e}")
                # Don't fail -- allow creation even if we can't test immediately

            # Store user credentials (bot token + optional app/user tokens)
            cred_repo = UserCredentialRepository(session)
            await cred_repo.store_credentials(
                user_id=user.user_id,
                credential=UserCredentialProvide(
                    connector_id=connector_id,
                    credential_type="API_KEY",
                    credentials=credentials,
                ),
            )

            # Register operations
            op_creates = []
            for op in SLACK_OPERATIONS:
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
            for t in SLACK_TYPES:
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
                from meho_app.modules.connectors.slack.sync import _sync_slack_knowledge_chunks
                from meho_app.modules.knowledge.embeddings import get_embedding_provider
                from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
                from meho_app.modules.knowledge.repository import KnowledgeRepository

                knowledge_repo = KnowledgeRepository(session)
                embedding_provider = get_embedding_provider()
                knowledge_store = KnowledgeStore(
                    repository=knowledge_repo,
                    embedding_provider=embedding_provider,
                )

                chunks_created = await _sync_slack_knowledge_chunks(
                    knowledge_store=knowledge_store,
                    connector_id=connector_id,
                    connector_name=request.name,
                    tenant_id=user.tenant_id,
                )
                await session.commit()
                logger.info(f"Created {chunks_created} knowledge chunks for Slack operations")
            except Exception as e:
                logger.warning(f"Failed to create knowledge chunks: {e}")
                # Don't fail connector creation if knowledge sync fails

            logger.info(
                f"Created Slack connector '{request.name}' "
                f"with {ops_count} operations, {types_count} types, "
                f"and {chunks_created} knowledge chunks"
            )

            return SlackConnectorResponse(
                id=connector_id,
                name=request.name,
                connector_type="slack",
                operations_registered=ops_count,
                types_registered=types_count,
                message=(
                    f"Slack connector created successfully. "
                    f"Registered {ops_count} operations and {chunks_created} knowledge chunks."
                ),
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create Slack connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
