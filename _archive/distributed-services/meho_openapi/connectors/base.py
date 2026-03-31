"""
Base Connector Interface (TASK-97)

Minimal base interface for connectors.
All connectors implement this so the router can call them uniformly.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from pydantic import BaseModel


class OperationResult(BaseModel):
    """Result from executing any operation."""
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    operation_id: Optional[str] = None
    duration_ms: Optional[float] = None

    model_config = {"arbitrary_types_allowed": True}


class OperationDefinition(BaseModel):
    """Definition of a connector operation for registration."""
    operation_id: str
    name: str
    description: str
    category: str
    parameters: List[Dict[str, Any]] = []
    example: Optional[str] = None


class TypeDefinition(BaseModel):
    """Definition of a connector entity type for registration."""
    type_name: str
    description: str
    category: str
    properties: List[Dict[str, Any]] = []


class BaseConnector(ABC):
    """
    Base class for all connector implementations.
    
    Connectors provide a uniform interface for:
    - Connecting to external systems (vCenter, Kubernetes, etc.)
    - Executing operations (list VMs, get cluster, etc.)
    - Exposing discoverable operations and types
    
    The agent uses the same tools for all connector types.
    The connector_type is an implementation detail hidden from the agent.
    """
    
    def __init__(
        self,
        connector_id: str,
        config: Dict[str, Any],
        credentials: Dict[str, Any]
    ):
        """
        Initialize connector with configuration and credentials.
        
        Args:
            connector_id: Unique connector identifier
            config: Connector-specific configuration (host, port, options)
            credentials: User credentials (username, password, etc.)
        """
        self.connector_id = connector_id
        self.config = config
        self.credentials = credentials
        self._is_connected = False
    
    @property
    def is_connected(self) -> bool:
        """Check if connector is currently connected."""
        return self._is_connected
    
    @abstractmethod
    async def connect(self) -> bool:
        """
        Establish connection to the external system.
        
        Returns:
            True if connection successful
        
        Raises:
            Exception: If connection fails
        """
        pass
    
    @abstractmethod
    async def disconnect(self) -> None:
        """
        Close connection to the external system.
        
        Should be idempotent - safe to call multiple times.
        """
        pass
    
    @abstractmethod
    async def test_connection(self) -> bool:
        """
        Test if connection is alive and working.
        
        Returns:
            True if connection is healthy
        """
        pass
    
    @abstractmethod
    async def execute(
        self,
        operation_id: str,
        parameters: Dict[str, Any]
    ) -> OperationResult:
        """
        Execute an operation on the external system.
        
        Args:
            operation_id: ID of the operation to execute
            parameters: Parameters for the operation
        
        Returns:
            OperationResult with success status and data/error
        """
        pass
    
    @abstractmethod
    def get_operations(self) -> List[OperationDefinition]:
        """
        Get operation definitions for registration.
        
        These operations are stored in the database and can be
        discovered by the agent via search_operations.
        
        Returns:
            List of operation definitions
        """
        pass
    
    @abstractmethod
    def get_types(self) -> List[TypeDefinition]:
        """
        Get type definitions for registration.
        
        These types describe entities the connector works with
        (VirtualMachine, Cluster, etc.) and can be discovered
        by the agent via search_types.
        
        Returns:
            List of type definitions
        """
        pass
    
    async def __aenter__(self) -> "BaseConnector":
        """Async context manager entry."""
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.disconnect()

