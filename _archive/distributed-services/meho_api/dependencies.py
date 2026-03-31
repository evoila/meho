"""
Dependency factory for creating MEHODependencies in BFF context.

Creates proper dependencies with all required services for agents.
"""
from meho_agent.dependencies import MEHODependencies
from meho_knowledge.knowledge_store import KnowledgeStore
from meho_knowledge.repository import KnowledgeRepository
from meho_knowledge.embeddings import OpenAIEmbeddings
from meho_knowledge.hybrid_search import PostgresFTSHybridService
from meho_knowledge.database import get_session
from meho_openapi.repository import ConnectorRepository, EndpointDescriptorRepository
from meho_openapi.user_credentials import UserCredentialRepository
from meho_openapi.http_client import GenericHTTPClient
from meho_openapi.endpoint_testing import OpenAPIService
from meho_core.auth_context import UserContext
from meho_api.config import get_api_config
from meho_agent.state_store import RedisStateStore, get_redis_client
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis
import httpx
import os


async def create_agent_dependencies(
    user: UserContext,
    session: AsyncSession
) -> MEHODependencies:
    """
    Create MEHODependencies for agent execution in BFF context.
    
    This is the REAL implementation - no mocks!
    
    CRITICAL NOTE (Session 55): 
    All services now use the same DATABASE_URL, so the session can be shared.
    The previous multi-database architecture has been simplified.
    
    Args:
        user: User context (from JWT)
        session: Database session (shared across all services)
        
    Returns:
        MEHODependencies with all services connected
    """
    config = get_api_config()
    
    # Knowledge Store (real! Now using pgvector + PostgreSQL FTS hybrid search)
    repository = KnowledgeRepository(session)
    
    # Create embedding provider directly (don't use get_embedding_provider which needs full Config)
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY environment variable is required")
    embedding_provider = OpenAIEmbeddings(api_key=openai_api_key, model="text-embedding-3-small")
    
    # Create hybrid search service (combines PostgreSQL FTS + semantic)
    # No BM25 manager needed - PostgreSQL FTS uses built-in GIN indexes
    hybrid_search = PostgresFTSHybridService(repository, embedding_provider)
    
    # Create knowledge store WITH hybrid search support
    knowledge_store = KnowledgeStore(
        repository=repository,
        embedding_provider=embedding_provider,
        hybrid_search_service=hybrid_search
    )
    
    # OpenAPI Repositories (use same session - all in same database now)
    connector_repo = ConnectorRepository(session)
    endpoint_repo = EndpointDescriptorRepository(session)
    user_cred_repo = UserCredentialRepository(session)
    
    # HTTP Client (real!)
    http_client = GenericHTTPClient()
    
    # Redis Client (for BM25 caching)
    redis_client = await get_redis_client(config.redis_url)
    
    return MEHODependencies(
        knowledge_store=knowledge_store,
        connector_repo=connector_repo,
        endpoint_repo=endpoint_repo,
        user_cred_repo=user_cred_repo,
        http_client=http_client,
        user_context=user,
        redis=redis_client,  # Enable BM25 caching for 18x speedup!
        usage_limits=None  # No limits for BFF (user-facing)
    )


async def create_state_store() -> RedisStateStore:
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
    redis_client = await get_redis_client(config.redis_url)
    return RedisStateStore(redis_client)


async def create_agent_dependencies_http(user: UserContext) -> MEHODependencies:
    """
    Create MEHODependencies using HTTP calls to backend services.
    
    Alternative approach: BFF calls backend services via HTTP instead of direct DB access.
    Use this if BFF should be completely stateless.
    
    NOTE: This is an alternative architecture for future consideration.
    For current implementation, use create_agent_dependencies() which provides
    direct DB access for better performance and simpler deployment.
    
    Args:
        user: User context
        
    Returns:
        MEHODependencies with HTTP-based service access
    """
    # Not implemented - alternative architecture for future consideration
    # Current MVP uses direct DB access via create_agent_dependencies()
    raise NotImplementedError("HTTP-based dependencies - use create_agent_dependencies for current implementation")


async def get_openapi_service(
    session: AsyncSession
) -> OpenAPIService:
    """
    Get OpenAPIService instance for the current request.
    
    This is a FastAPI dependency that creates an OpenAPIService
    with the provided database session.
    
    Usage in routes:
        @router.post("/test-endpoint")
        async def test_endpoint(
            request: TestEndpointRequest,
            user: UserContext = Depends(get_current_user),
            service: OpenAPIService = Depends(get_openapi_service)
        ):
            result = await service.test_endpoint(...)
    
    Args:
        session: Database session from get_session dependency
        
    Returns:
        OpenAPIService instance
    """
    return OpenAPIService(session)

