"""
HTTP client for OpenAPI Service.

Provides methods to interact with the openapi service for connector
and endpoint management via HTTP REST APIs.
"""
# mypy: disable-error-code="no-any-return,no-untyped-def,assignment"
import httpx
import json
from typing import Dict, Any, List, Optional
from meho_api.config import get_api_config
import logging

logger = logging.getLogger(__name__)


class OpenAPIServiceClient:
    """HTTP client for OpenAPI service"""
    
    def __init__(self, base_url: Optional[str] = None):
        """
        Initialize openapi service client.
        
        Args:
            base_url: Override default service URL (useful for testing)
        """
        self.config = get_api_config()
        self.base_url = base_url or self.config.openapi_service_url
        
    def _get_client(self) -> httpx.AsyncClient:
        """Create async HTTP client with appropriate timeout"""
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(30.0, connect=5.0)
        )
    
    async def list_connectors(
        self,
        tenant_id: str,
        is_active: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        """
        List connectors for a tenant via HTTP.
        
        Args:
            tenant_id: Tenant ID
            is_active: Optional filter by active status
            
        Returns:
            List of connectors
        """
        async with self._get_client() as client:
            try:
                params = {"tenant_id": tenant_id}
                if is_active is not None:
                    params["is_active"] = is_active
                    
                response = await client.get("/connectors", params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"List connectors failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"List connectors request failed: {e}")
                raise
    
    async def get_connector(self, connector_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a connector by ID via HTTP.
        
        Args:
            connector_id: Connector ID
            
        Returns:
            Connector data or None if not found
        """
        async with self._get_client() as client:
            try:
                response = await client.get(f"/connectors/{connector_id}")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                logger.error(f"Get connector failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Get connector request failed: {e}")
                raise
    
    async def create_connector(
        self,
        name: str,
        base_url: str,
        auth_type: str,
        tenant_id: str,
        description: Optional[str] = None,
        allowed_methods: Optional[List[str]] = None,
        blocked_methods: Optional[List[str]] = None,
        default_safety_level: str = "safe"
    ) -> Dict[str, Any]:
        """
        Create a new connector via HTTP.
        
        Args:
            name: Connector name
            base_url: Base URL for the API
            auth_type: Authentication type (API_KEY, BASIC, OAUTH2, NONE)
            tenant_id: Tenant ID
            description: Optional description
            allowed_methods: Optional list of allowed HTTP methods
            blocked_methods: Optional list of blocked HTTP methods
            default_safety_level: Default safety level (safe, caution, dangerous)
            
        Returns:
            Created connector data
        """
        async with self._get_client() as client:
            try:
                response = await client.post(
                    "/connectors",
                    json={
                        "name": name,
                        "base_url": base_url,
                        "auth_type": auth_type,
                        "tenant_id": tenant_id,
                        "description": description,
                        "allowed_methods": allowed_methods or ["GET", "POST", "PUT", "PATCH", "DELETE"],
                        "blocked_methods": blocked_methods or [],
                        "default_safety_level": default_safety_level
                    }
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Create connector failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Create connector request failed: {e}")
                raise
    
    async def update_connector(
        self,
        connector_id: str,
        updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update a connector via HTTP.
        
        Args:
            connector_id: Connector ID
            updates: Dictionary of fields to update
            
        Returns:
            Updated connector data
        """
        async with self._get_client() as client:
            try:
                response = await client.patch(
                    f"/connectors/{connector_id}",
                    json=updates
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Update connector failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Update connector request failed: {e}")
                raise
    
    async def delete_connector(self, connector_id: str) -> None:
        """
        Delete a connector via HTTP.
        
        Args:
            connector_id: Connector ID
        """
        async with self._get_client() as client:
            try:
                response = await client.delete(f"/connectors/{connector_id}")
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Delete connector failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Delete connector request failed: {e}")
                raise
    
    async def list_endpoints(
        self,
        connector_id: str,
        is_enabled: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        """
        List endpoints for a connector via HTTP.
        
        Args:
            connector_id: Connector ID
            is_enabled: Optional filter by enabled status
            
        Returns:
            List of endpoints
        """
        async with self._get_client() as client:
            try:
                params = {}
                if is_enabled is not None:
                    params["is_enabled"] = is_enabled
                    
                response = await client.get(
                    f"/connectors/{connector_id}/endpoints",
                    params=params
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"List endpoints failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"List endpoints request failed: {e}")
                raise
    
    async def get_endpoint(self, endpoint_id: str) -> Optional[Dict[str, Any]]:
        """
        Get an endpoint by ID via HTTP.
        
        Args:
            endpoint_id: Endpoint ID
            
        Returns:
            Endpoint data or None if not found
        """
        async with self._get_client() as client:
            try:
                response = await client.get(f"/endpoints/{endpoint_id}")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                logger.error(f"Get endpoint failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Get endpoint request failed: {e}")
                raise
    
    async def update_endpoint(
        self,
        endpoint_id: str,
        updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update an endpoint via HTTP.
        
        Args:
            endpoint_id: Endpoint ID
            updates: Dictionary of fields to update
            
        Returns:
            Updated endpoint data
        """
        async with self._get_client() as client:
            try:
                response = await client.patch(
                    f"/endpoints/{endpoint_id}",
                    json=updates
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Update endpoint failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Update endpoint request failed: {e}")
                raise
    
    async def upload_openapi_spec(
        self,
        connector_id: str,
        spec_file: bytes,
        filename: str
    ) -> Dict[str, Any]:
        """
        Upload OpenAPI spec for a connector via HTTP.
        
        Args:
            connector_id: Connector ID
            spec_file: Spec file content as bytes
            filename: Name of the spec file
            
        Returns:
            Upload result with endpoint count
        """
        async with self._get_client() as client:
            try:
                files = {"file": (filename, spec_file)}
                response = await client.post(
                    f"/connectors/{connector_id}/spec",
                    files=files
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Upload spec failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Upload spec request failed: {e}")
                raise
    
    async def create_credential(
        self,
        connector_id: str,
        user_id: str,
        credentials: Dict[str, Any]
    ) -> None:
        """
        Create or update credentials for a connector via HTTP.
        
        Args:
            connector_id: Connector ID
            user_id: User ID
            credentials: Credential data (encrypted by service)
        """
        async with self._get_client() as client:
            try:
                response = await client.post(
                    "/credentials",
                    json={
                        "connector_id": connector_id,
                        "user_id": user_id,
                        "credentials": credentials
                    }
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Create credential failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Create credential request failed: {e}")
                raise
    
    async def get_credential(
        self,
        connector_id: str,
        user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get credentials for a connector via HTTP.
        
        Args:
            connector_id: Connector ID
            user_id: User ID
            
        Returns:
            Credential data or None if not found
        """
        async with self._get_client() as client:
            try:
                response = await client.get(f"/credentials/{connector_id}/{user_id}")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                logger.error(f"Get credential failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Get credential request failed: {e}")
                raise
    
    async def health_check(self) -> Dict[str, Any]:
        """
        Check if openapi service is healthy.
        
        Returns:
            Health status
        """
        async with self._get_client() as client:
            try:
                response = await client.get("/health")
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.error(f"Health check failed: {e}")
                raise


# Singleton instance
_openapi_client = None


def get_openapi_client() -> OpenAPIServiceClient:
    """Get openapi service client singleton"""
    global _openapi_client
    if _openapi_client is None:
        _openapi_client = OpenAPIServiceClient()
    return _openapi_client


def reset_openapi_client():
    """Reset client singleton (for testing)"""
    global _openapi_client
    _openapi_client = None

