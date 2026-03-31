"""
Protocol Router

Routes API calls to the appropriate protocol client based on connector type.
Supports REST, GraphQL, gRPC, and SOAP protocols with a unified interface.

TASK-75: Multi-Protocol Support
"""

import logging
from typing import Any, Dict, Optional, Tuple
from datetime import datetime

from meho_openapi.schemas import Connector, EndpointDescriptor
from meho_openapi.http_client import GenericHTTPClient
from meho_openapi.soap.client import SOAPClient, VMwareSOAPClient
from meho_openapi.soap.models import (
    SOAPConnectorConfig,
    SOAPAuthType,
    SOAPOperation,
    SOAPResponse,
)

logger = logging.getLogger(__name__)


class ProtocolRouter:
    """Routes API calls to the appropriate protocol client
    
    This router provides a unified interface for calling APIs regardless
    of their underlying protocol (REST, GraphQL, gRPC, SOAP).
    
    Example:
        router = ProtocolRouter()
        
        # REST call
        status, data = await router.call(
            connector=rest_connector,
            endpoint=rest_endpoint,
            params={"query_params": {"limit": 10}}
        )
        
        # SOAP call
        status, data = await router.call(
            connector=soap_connector,
            operation=soap_operation,
            params={"userName": "admin", "password": "***"}
        )
    """
    
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        
        # REST client (existing)
        self.rest_client = GenericHTTPClient(timeout=timeout)
        
        # SOAP clients (cached per connector)
        self._soap_clients: Dict[str, SOAPClient] = {}
        
        # GraphQL and gRPC clients will be added in future phases
        # self._graphql_clients: Dict[str, GraphQLClient] = {}
        # self._grpc_clients: Dict[str, GRPCClient] = {}
    
    async def call(
        self,
        connector: Connector,
        endpoint: Optional[EndpointDescriptor] = None,
        operation: Optional[SOAPOperation] = None,
        path_params: Optional[Dict[str, Any]] = None,
        query_params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,  # SOAP/GraphQL params
        user_credentials: Optional[Dict[str, str]] = None,
        session_token: Optional[str] = None,
        session_expires_at: Optional[datetime] = None,
        refresh_token: Optional[str] = None,
        refresh_expires_at: Optional[datetime] = None,
        on_session_update: Optional[Any] = None,
    ) -> Tuple[int, Any]:
        """Call an API endpoint using the appropriate protocol
        
        Args:
            connector: Connector configuration (contains protocol type)
            endpoint: REST endpoint descriptor (for REST protocol)
            operation: SOAP operation (for SOAP protocol)
            path_params: Path parameters (REST)
            query_params: Query parameters (REST)
            body: Request body (REST)
            params: Operation parameters (SOAP/GraphQL)
            user_credentials: User credentials
            session_token: Session token (for session-based auth)
            session_expires_at: Session expiry
            refresh_token: Refresh token
            refresh_expires_at: Refresh token expiry
            on_session_update: Callback for session updates
            
        Returns:
            Tuple of (status_code, response_data)
        """
        protocol = getattr(connector, 'protocol', 'rest') or 'rest'
        
        logger.info(f"🔀 ProtocolRouter: Routing call via {protocol.upper()} protocol")
        
        if protocol == "rest":
            return await self._call_rest(
                connector=connector,
                endpoint=endpoint,
                path_params=path_params,
                query_params=query_params,
                body=body,
                user_credentials=user_credentials,
                session_token=session_token,
                session_expires_at=session_expires_at,
                refresh_token=refresh_token,
                refresh_expires_at=refresh_expires_at,
                on_session_update=on_session_update,
            )
        
        elif protocol == "soap":
            return await self._call_soap(
                connector=connector,
                operation=operation,
                params=params or {},
                user_credentials=user_credentials,
            )
        
        elif protocol == "graphql":
            # Future: GraphQL support
            raise NotImplementedError(
                "GraphQL protocol support is planned for Phase A of TASK-75"
            )
        
        elif protocol == "grpc":
            # Future: gRPC support
            raise NotImplementedError(
                "gRPC protocol support is planned for Phase B of TASK-75"
            )
        
        else:
            raise ValueError(f"Unknown protocol: {protocol}")
    
    async def _call_rest(
        self,
        connector: Connector,
        endpoint: Optional[EndpointDescriptor],
        path_params: Optional[Dict[str, Any]],
        query_params: Optional[Dict[str, Any]],
        body: Optional[Dict[str, Any]],
        user_credentials: Optional[Dict[str, str]],
        session_token: Optional[str],
        session_expires_at: Optional[datetime],
        refresh_token: Optional[str],
        refresh_expires_at: Optional[datetime],
        on_session_update: Optional[Any],
    ) -> Tuple[int, Any]:
        """Route to REST client"""
        if endpoint is None:
            raise ValueError("REST protocol requires an endpoint")
        
        return await self.rest_client.call_endpoint(
            connector=connector,
            endpoint=endpoint,
            path_params=path_params,
            query_params=query_params,
            body=body,
            user_credentials=user_credentials,
            session_token=session_token,
            session_expires_at=session_expires_at,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires_at,
            on_session_update=on_session_update,
        )
    
    async def _call_soap(
        self,
        connector: Connector,
        operation: Optional[SOAPOperation],
        params: Dict[str, Any],
        user_credentials: Optional[Dict[str, str]],
    ) -> Tuple[int, Any]:
        """Route to SOAP client"""
        if operation is None:
            # If no operation object, try to use params directly
            # This supports calling by operation name
            operation_name = params.pop("operation_name", None)
            if not operation_name:
                raise ValueError("SOAP protocol requires an operation or operation_name")
        else:
            operation_name = operation.operation_name
        
        # Get or create SOAP client for this connector
        client = await self._get_soap_client(connector, user_credentials)
        
        # Call the operation
        if operation:
            response = await client.call_operation(operation, params)
        else:
            response = await client.call(operation_name, params)
        
        return response.status_code, response.body
    
    async def _get_soap_client(
        self,
        connector: Connector,
        user_credentials: Optional[Dict[str, str]],
    ) -> SOAPClient:
        """Get or create a SOAP client for a connector"""
        connector_id = str(connector.id)
        
        # Check cache
        if connector_id in self._soap_clients:
            client = self._soap_clients[connector_id]
            if client.is_connected:
                return client
        
        # Get WSDL URL from protocol_config
        protocol_config = getattr(connector, 'protocol_config', {}) or {}
        wsdl_url = protocol_config.get('wsdl_url')
        
        if not wsdl_url:
            # Fallback: try base_url with /wsdl suffix
            wsdl_url = f"{connector.base_url.rstrip('/')}/wsdl"
            logger.warning(
                f"⚠️ No wsdl_url in protocol_config, trying: {wsdl_url}"
            )
        
        # Determine auth type
        auth_type = SOAPAuthType.NONE
        if connector.auth_type == "BASIC":
            auth_type = SOAPAuthType.BASIC
        elif connector.auth_type == "SESSION":
            auth_type = SOAPAuthType.SESSION
        
        # Build config
        config = SOAPConnectorConfig(
            wsdl_url=wsdl_url,
            auth_type=auth_type,
            username=user_credentials.get("username") if user_credentials else None,
            password=user_credentials.get("password") if user_credentials else None,
            login_operation=protocol_config.get("login_operation"),
            logout_operation=protocol_config.get("logout_operation"),
            verify_ssl=protocol_config.get("verify_ssl", True),
            timeout=int(self.timeout),
        )
        
        # Use VMware client if this looks like a VMware connector
        is_vmware = (
            "vmware" in connector.name.lower() or
            "vim" in wsdl_url.lower() or
            "vsphere" in connector.name.lower()
        )
        
        if is_vmware:
            logger.info("🏢 ProtocolRouter: Using VMware-optimized SOAP client")
            client = VMwareSOAPClient(config)
        else:
            client = SOAPClient(config)
        
        # Connect
        await client.connect()
        
        # Cache
        self._soap_clients[connector_id] = client
        
        return client
    
    async def close(self) -> None:
        """Close all clients and cleanup"""
        # Close SOAP clients
        for client in self._soap_clients.values():
            try:
                await client.disconnect()
            except Exception as e:
                logger.warning(f"⚠️ Error closing SOAP client: {e}")
        
        self._soap_clients.clear()
        
        logger.info("🔌 ProtocolRouter: All clients closed")
    
    async def __aenter__(self) -> "ProtocolRouter":
        return self
    
    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()


# Singleton instance for shared use
_router_instance: Optional[ProtocolRouter] = None


def get_protocol_router(timeout: float = 30.0) -> ProtocolRouter:
    """Get the shared ProtocolRouter instance"""
    global _router_instance
    if _router_instance is None:
        _router_instance = ProtocolRouter(timeout=timeout)
    return _router_instance

