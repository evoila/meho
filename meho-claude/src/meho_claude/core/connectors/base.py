"""Abstract base connector defining the contract all connector types implement.

Every concrete connector (REST, Kubernetes, VMware, Proxmox, GCP, SOAP) must
subclass BaseConnector and implement the 4 abstract methods.
"""

from abc import ABC, abstractmethod
from typing import Any

from meho_claude.core.connectors.models import ConnectorConfig, Operation


class BaseConnector(ABC):
    """Abstract base class for all connector types.

    Subclasses must implement:
        - test_connection: Verify connectivity and auth
        - discover_operations: Parse spec/SDK into Operation list
        - execute: Run a single operation with params
        - get_trust_tier: Determine trust tier for an operation
    """

    def __init__(self, config: ConnectorConfig, credentials: dict | None = None) -> None:
        self.config = config
        self.credentials = credentials

    @abstractmethod
    async def test_connection(self) -> dict[str, Any]:
        """Test connectivity and authentication.

        Returns dict with at least {"status": "ok"} or {"status": "error", "message": "..."}.
        """
        ...

    @abstractmethod
    async def discover_operations(self) -> list[Operation]:
        """Parse spec or SDK into a list of Operation models."""
        ...

    @abstractmethod
    async def execute(self, operation: Operation, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a single operation with the given parameters."""
        ...

    @abstractmethod
    def get_trust_tier(self, operation: Operation) -> str:
        """Determine the trust tier for an operation.

        May apply trust_overrides from config or default heuristics.
        """
        ...

    def close(self) -> None:
        """Clean up resources. Override if connector holds open connections."""
        pass
