"""
HTTP client for Knowledge Service.

Provides methods to interact with the knowledge service via HTTP REST APIs
instead of direct Python imports.
"""
# mypy: disable-error-code="no-any-return,no-untyped-def,assignment,no-redef"
import httpx
import json
from typing import Dict, Any, List, Optional
from meho_api.config import get_api_config
from meho_core.auth_context import UserContext
import logging

logger = logging.getLogger(__name__)


class KnowledgeServiceClient:
    """HTTP client for Knowledge service"""
    
    def __init__(self, base_url: Optional[str] = None):
        """
        Initialize knowledge service client.
        
        Args:
            base_url: Override default service URL (useful for testing)
        """
        self.config = get_api_config()
        self.base_url = base_url or self.config.knowledge_service_url
        # Don't create client in __init__ - create per-request to avoid connection pooling issues
        
    def _get_client(self) -> httpx.AsyncClient:
        """Create async HTTP client with appropriate timeout"""
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(30.0, connect=5.0)
        )
    
    async def search(
        self,
        query: str,
        user_context: UserContext,
        top_k: int = 10,
        metadata_filters: Optional[Dict[str, Any]] = None,
        search_mode: str = "semantic"
    ) -> Dict[str, Any]:
        """
        Search knowledge base via HTTP.
        
        Args:
            query: Search query
            user_context: User context for ACL filtering
            top_k: Number of results to return
            metadata_filters: Optional metadata filters
            search_mode: "semantic", "bm25", or "hybrid"
            
        Returns:
            Search response with chunks and scores
        """
        async with self._get_client() as client:
            try:
                response = await client.post(
                    "/knowledge/search",
                    json={
                        "query": query,
                        "tenant_id": user_context.tenant_id,
                        "user_id": user_context.user_id,
                        "system_id": user_context.system_id,
                        "roles": user_context.roles,
                        "groups": user_context.groups,
                        "top_k": top_k,
                        "metadata_filters": metadata_filters or {},
                        "search_mode": search_mode
                    }
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Knowledge search failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Knowledge search request failed: {e}")
                raise
    
    async def hybrid_search(
        self,
        query: str,
        user_context: UserContext,
        top_k: int = 10,
        metadata_filters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Hybrid search (BM25 + semantic) via HTTP.
        
        Args:
            query: Search query
            user_context: User context for ACL filtering
            top_k: Number of results to return
            metadata_filters: Optional metadata filters
            
        Returns:
            Search response with chunks and scores
        """
        async with self._get_client() as client:
            try:
                response = await client.post(
                    "/knowledge/search/hybrid",
                    json={
                        "query": query,
                        "tenant_id": user_context.tenant_id,
                        "user_id": user_context.user_id,
                        "system_id": user_context.system_id,
                        "roles": user_context.roles,
                        "groups": user_context.groups,
                        "top_k": top_k,
                        "metadata_filters": metadata_filters or {}
                    }
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Hybrid search failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Hybrid search request failed: {e}")
                raise
    
    async def ingest_text(
        self,
        text: str,
        tenant_id: str,
        user_id: Optional[str] = None,
        system_id: Optional[str] = None,
        roles: Optional[List[str]] = None,
        groups: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        source_uri: Optional[str] = None,
        knowledge_type: str = "documentation",
        priority: int = 0,
        expires_at: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Ingest raw text via HTTP.
        
        Args:
            text: Text content to ingest
            tenant_id: Tenant ID
            user_id: Optional user ID for private knowledge
            system_id: Optional system ID
            roles: Optional roles for role-based access
            groups: Optional groups for group-based access
            tags: Optional tags
            source_uri: Optional source URI
            knowledge_type: Type of knowledge (documentation, procedure, event)
            priority: Priority (0-100)
            expires_at: Optional expiration timestamp (for events)
            
        Returns:
            Ingestion response with chunk IDs
        """
        async with self._get_client() as client:
            try:
                response = await client.post(
                    "/knowledge/ingest/text",
                    json={
                        "text": text,
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "system_id": system_id,
                        "roles": roles or [],
                        "groups": groups or [],
                        "tags": tags or [],
                        "source_uri": source_uri,
                        "knowledge_type": knowledge_type,
                        "priority": priority,
                        "expires_at": expires_at
                    }
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Text ingestion failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Text ingestion request failed: {e}")
                raise
    
    async def ingest_document(
        self,
        file_bytes: bytes,
        filename: str,
        tenant_id: str,
        user_id: Optional[str] = None,
        system_id: Optional[str] = None,
        roles: Optional[List[str]] = None,
        groups: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        knowledge_type: str = "documentation",
        priority: int = 0
    ) -> Dict[str, Any]:
        """
        Ingest a document via HTTP.
        
        Args:
            file_bytes: File content as bytes
            filename: Name of the file
            tenant_id: Tenant ID
            user_id: Optional user ID for private knowledge
            system_id: Optional system ID
            roles: Optional roles for role-based access
            groups: Optional groups for group-based access
            tags: Optional tags
            knowledge_type: Type of knowledge (documentation, procedure, event)
            priority: Priority (0-100)
            
        Returns:
            Ingestion response with chunk IDs and document URI
        """
        async with self._get_client() as client:
            try:
                # Prepare multipart form data
                files = {"file": (filename, file_bytes)}
                metadata = {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "system_id": system_id,
                    "roles": roles or [],
                    "groups": groups or [],
                    "tags": tags or [],
                    "knowledge_type": knowledge_type,
                    "priority": priority
                }
                data = {"metadata": json.dumps(metadata)}
                
                response = await client.post(
                    "/knowledge/ingest/document",
                    files=files,
                    data=data
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Document ingestion failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Document ingestion request failed: {e}")
                raise
    
    async def get_chunk(self, chunk_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a single chunk by ID via HTTP.
        
        Args:
            chunk_id: Chunk ID
            
        Returns:
            Chunk data or None if not found
        """
        async with self._get_client() as client:
            try:
                response = await client.get(f"/knowledge/chunks/{chunk_id}")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                logger.error(f"Get chunk failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Get chunk request failed: {e}")
                raise
    
    async def delete_chunk(self, chunk_id: str) -> None:
        """
        Delete a chunk via HTTP.
        
        Args:
            chunk_id: Chunk ID
        """
        async with self._get_client() as client:
            try:
                response = await client.delete(f"/knowledge/chunks/{chunk_id}")
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Delete chunk failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Delete chunk request failed: {e}")
                raise
    
    async def list_active_jobs(self, tenant_id: str) -> List[Dict[str, Any]]:
        """
        List active ingestion jobs for a tenant via HTTP.
        
        Args:
            tenant_id: Tenant ID
            
        Returns:
            List of active jobs
        """
        async with self._get_client() as client:
            try:
                response = await client.get(
                    "/knowledge/jobs/active",
                    params={"tenant_id": tenant_id}
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"List active jobs failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"List active jobs request failed: {e}")
                raise
    
    async def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific ingestion job by ID via HTTP.
        
        Args:
            job_id: Job ID
            
        Returns:
            Job data or None if not found
        """
        async with self._get_client() as client:
            try:
                response = await client.get(f"/knowledge/jobs/{job_id}")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                logger.error(f"Get job failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Get job request failed: {e}")
                raise
    
    async def list_chunks(
        self,
        tenant_id: Optional[str] = None,
        system_id: Optional[str] = None,
        knowledge_type: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        List knowledge chunks with filters via HTTP.
        
        Args:
            tenant_id: Optional tenant filter
            system_id: Optional system filter
            knowledge_type: Optional type filter
            limit: Max results
            
        Returns:
            List of chunks
        """
        async with self._get_client() as client:
            try:
                params = {"limit": limit}
                if tenant_id:
                    params["tenant_id"] = tenant_id
                if system_id:
                    params["system_id"] = system_id
                if knowledge_type:
                    params["knowledge_type"] = knowledge_type
                    
                response = await client.get("/knowledge/chunks", params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"List chunks failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"List chunks request failed: {e}")
                raise
    
    async def list_documents(
        self,
        tenant_id: Optional[str] = None,
        status_filter: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        List documents (ingestion jobs) with filters via HTTP.
        
        Args:
            tenant_id: Optional tenant filter
            status_filter: Optional status filter
            limit: Max results
            
        Returns:
            List of documents/jobs
        """
        async with self._get_client() as client:
            try:
                params = {"limit": limit}
                if tenant_id:
                    params["tenant_id"] = tenant_id
                if status_filter:
                    params["status_filter"] = status_filter
                    
                response = await client.get("/knowledge/documents", params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"List documents failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"List documents request failed: {e}")
                raise
    
    async def delete_document(self, document_id: str) -> None:
        """
        Delete a document and all its chunks via HTTP.
        
        Args:
            document_id: Document/job ID to delete
        """
        async with self._get_client() as client:
            try:
                response = await client.delete(f"/knowledge/documents/{document_id}")
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Delete document failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Delete document request failed: {e}")
                raise
    
    async def delete_chunk(self, chunk_id: str) -> None:
        """
        Delete a knowledge chunk via HTTP.
        
        Args:
            chunk_id: Chunk ID to delete
        """
        async with self._get_client() as client:
            try:
                response = await client.delete(f"/knowledge/chunks/{chunk_id}")
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Delete chunk failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Delete chunk request failed: {e}")
                raise
    
    async def health_check(self) -> Dict[str, Any]:
        """
        Check if knowledge service is healthy.
        
        Returns:
            Health status
        """
        async with self._get_client() as client:
            try:
                response = await client.get("/knowledge/ping")
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.error(f"Health check failed: {e}")
                raise


# Singleton instance
_knowledge_client = None


def get_knowledge_client() -> KnowledgeServiceClient:
    """Get knowledge service client singleton"""
    global _knowledge_client
    if _knowledge_client is None:
        _knowledge_client = KnowledgeServiceClient()
    return _knowledge_client


def reset_knowledge_client():
    """Reset client singleton (for testing)"""
    global _knowledge_client
    _knowledge_client = None

