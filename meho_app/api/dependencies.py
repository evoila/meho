# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
API layer dependencies for MEHO unified application.

Provides FastAPI dependencies for:
- Database sessions
- User authentication/authorization
- Service instances (Knowledge, OpenAPI, Agent, Ingestion)
- Agent dependencies for tool execution
- State persistence
"""

from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.api.auth import get_current_user as get_user_from_jwt
from meho_app.api.config import get_api_config
from meho_app.core.auth_context import UserContext
from meho_app.core.redis import get_redis_client
from meho_app.database import get_db_session
from meho_app.modules.agents import AgentService, get_agent_service

# Agent-specific dependencies
from meho_app.modules.agents.dependencies import MEHODependencies
from meho_app.modules.agents.persistence.state_store import AgentStateStore
from meho_app.modules.agents.session_state import AgentSessionState
from meho_app.modules.agents.state_store import RedisStateStore
from meho_app.modules.connectors.repositories import ConnectorRepository
from meho_app.modules.connectors.repositories.credential_repository import UserCredentialRepository
from meho_app.modules.connectors.rest.http_client import GenericHTTPClient
from meho_app.modules.connectors.rest.repository import EndpointDescriptorRepository
from meho_app.modules.connectors.rest.service import OpenAPIService, get_openapi_service
from meho_app.modules.ingestion import IngestionService, get_ingestion_service

# Module service imports
from meho_app.modules.knowledge import KnowledgeService, get_knowledge_service
from meho_app.modules.knowledge.embeddings import get_embedding_provider
from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.repository import KnowledgeRepository

# ============================================================================
# Basic Dependencies
# ============================================================================

# Database session dependency
DbSession = Annotated[AsyncSession, Depends(get_db_session)]

# Current user dependency
CurrentUser = Annotated[UserContext, Depends(get_user_from_jwt)]


# ============================================================================
# Service Dependencies
# ============================================================================


def get_knowledge_service_dep(session: DbSession) -> KnowledgeService:
    """Get KnowledgeService instance for the current request."""
    return get_knowledge_service(session)


def get_openapi_service_dep(session: DbSession) -> OpenAPIService:
    """Get OpenAPIService instance for the current request."""
    return get_openapi_service(session)


def get_agent_service_dep(session: DbSession) -> AgentService:
    """Get AgentService instance for the current request."""
    return get_agent_service(session)


def get_ingestion_service_dep(session: DbSession) -> IngestionService:
    """Get IngestionService instance for the current request."""
    return get_ingestion_service(session)


# Annotated service types for route handlers
KnowledgeServiceDep = Annotated[KnowledgeService, Depends(get_knowledge_service_dep)]
OpenAPIServiceDep = Annotated[OpenAPIService, Depends(get_openapi_service_dep)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service_dep)]
IngestionServiceDep = Annotated[IngestionService, Depends(get_ingestion_service_dep)]


# ============================================================================
# Agent Execution Dependencies
# ============================================================================


def create_agent_dependencies(
    user: CurrentUser,
    session: DbSession,
    current_question: str = "",
    session_state: AgentSessionState | None = None,
    # Phase 74: Automation identity context
    session_type: str = "interactive",
    created_by_user_id: str | None = None,
    allowed_connector_ids: list[str] | None = None,
    trigger_type: str | None = None,
    trigger_id: str | None = None,
    delegation_active: bool = True,
    delegation_flag_callback: Any = None,
    # Phase 75: notification targets for approval alerts
    notification_targets: list[dict[str, str]] | None = None,
) -> MEHODependencies:
    """
    Create MEHODependencies for agent execution.

    This provides the agent with direct access to all module services
    without HTTP overhead.

    Args:
        user: User context from JWT
        session: Database session (shared across all modules)
        current_question: Current user question (for context)
        session_state: Optional pre-loaded session state
        session_type: Session type for credential resolution (Phase 74)
        created_by_user_id: Original creator's user_id for automated sessions (Phase 74)
        allowed_connector_ids: Connector scope for automated sessions (Phase 74)
        trigger_type: "event" or "scheduler" for audit/flagging (Phase 74)
        trigger_id: UUID of event registration/task row for audit/flagging (Phase 74)
        delegation_active: Current delegation_active flag from trigger model (Phase 74)
        delegation_flag_callback: Callback to write delegation_active back to trigger model (Phase 74)

    Returns:
        MEHODependencies with all services connected
    """
    config = get_api_config()

    # Create knowledge store with all required components
    knowledge_repo = KnowledgeRepository(session)

    # Create embedding provider (Voyage AI 1024D singleton)
    embedding_provider = get_embedding_provider()

    # Create hybrid search service
    hybrid_search = PostgresFTSHybridService(knowledge_repo, embedding_provider)

    # Create knowledge store
    knowledge_store = KnowledgeStore(
        repository=knowledge_repo,
        embedding_provider=embedding_provider,
        hybrid_search_service=hybrid_search,
    )

    # OpenAPI repositories
    connector_repo = ConnectorRepository(session)
    endpoint_repo = EndpointDescriptorRepository(session)
    user_cred_repo = UserCredentialRepository(session)

    # HTTP client for external API calls
    http_client = GenericHTTPClient()

    # Redis client for caching
    redis_client = get_redis_client(config.redis_url)

    # Create session state if not provided
    if session_state is None:
        session_state = AgentSessionState()

    # Create and return MEHODependencies
    return MEHODependencies(
        knowledge_store=knowledge_store,
        connector_repo=connector_repo,
        endpoint_repo=endpoint_repo,
        user_cred_repo=user_cred_repo,
        http_client=http_client,
        user_context=user,
        db_session=session,  # Pass through for topology and other direct operations
        session_state=session_state,
        current_question=current_question,
        redis=redis_client,
        # Phase 74: Automation identity
        session_type=session_type,
        created_by_user_id=created_by_user_id,
        allowed_connector_ids=allowed_connector_ids,
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        delegation_active=delegation_active,
        delegation_flag_callback=delegation_flag_callback,
        # Phase 75: notification targets
        notification_targets=notification_targets,
    )


def create_state_store() -> RedisStateStore:
    """
    Create RedisStateStore for agent state persistence.

    This provides Redis-backed state storage with automatic TTL cleanup.
    State persists across requests, enabling:
    - No redundant connector discovery
    - Auto-filled connector_id from previous turns
    - Cached endpoints reused
    - Entity context preserved

    Returns:
        RedisStateStore instance connected to Redis
    """
    config = get_api_config()
    redis_client = get_redis_client(config.redis_url)
    return RedisStateStore(redis_client)


def create_agent_state_store() -> AgentStateStore:
    """
    Create AgentStateStore for orchestrator agent state persistence.

    This provides Redis-backed state storage for the new agent architecture
    (meho_app/modules/agents/). It enables multi-turn conversations where:
    - Connector memory persists across turns
    - Operation context is preserved
    - Cached data references are tracked
    - Recent errors are remembered

    Uses a different Redis key prefix from the legacy state store to avoid
    collisions: "meho:agents:state" vs "meho:state".

    Returns:
        AgentStateStore instance connected to Redis
    """
    config = get_api_config()
    redis_client = get_redis_client(config.redis_url)
    return AgentStateStore(redis_client)


# ============================================================================
# Exports
# ============================================================================

__all__ = [
    "AgentServiceDep",
    "CurrentUser",
    # Basic dependencies
    "DbSession",
    "IngestionServiceDep",
    # Service dependencies
    "KnowledgeServiceDep",
    "OpenAPIServiceDep",
    # Agent-specific
    "create_agent_dependencies",
    "create_agent_state_store",
    "create_state_store",
    "get_agent_service_dep",
    "get_ingestion_service_dep",
    # Service factory functions
    "get_knowledge_service_dep",
    "get_openapi_service_dep",
]
