# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Base Connector Interface.

Minimal base interface for all connector implementations.
All connectors implement this so they can be called uniformly.
"""

import difflib
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class OperationResult(BaseModel):
    """Result from executing any connector operation."""

    success: bool
    data: Any | None = None
    error: str | None = None
    error_code: str | None = None  # Structured error code (PERMISSION_DENIED, NOT_FOUND, etc.)
    error_details: dict[str, Any] | None = None  # Additional error context
    operation_id: str | None = None
    duration_ms: float | None = None

    model_config = {"arbitrary_types_allowed": True}


class OperationDefinition(BaseModel):
    """
    Definition of a connector operation for registration.

    Operations are stored in the database and discovered by the agent
    via search_operations. The response schema fields help the Brain-Muscle
    architecture understand the structure of returned data.
    """

    operation_id: str
    name: str
    description: str
    category: str
    parameters: list[dict[str, Any]] = []
    example: str | None = None

    # Response schema for Brain-Muscle architecture (TASK-161)
    # These fields help the LLM understand the structure of returned data
    # and prevent hallucination of entity names.
    response_entity_type: str | None = None  # e.g., "Namespace", "VirtualMachine", "Pod"
    response_identifier_field: str | None = None  # e.g., "uid", "moref_id", "id"
    response_display_name_field: str | None = None  # e.g., "name", "display_name"


class TypeDefinition(BaseModel):
    """Definition of a connector entity type for registration."""

    type_name: str
    description: str
    category: str
    properties: list[dict[str, Any]] = []


class BaseConnector(ABC):
    """
    Base class for all connector implementations.

    Connectors provide a uniform interface for:
    - Connecting to external systems (vCenter, Kubernetes, REST APIs, etc.)
    - Executing operations (list VMs, get cluster, call endpoint, etc.)
    - Exposing discoverable operations and types

    The agent uses the same tools for all connector types.
    The connector_type is an implementation detail hidden from the agent.
    """

    def __init__(
        self, connector_id: str, config: dict[str, Any], credentials: dict[str, Any]
    ) -> None:
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

    async def execute(self, operation_id: str, parameters: dict[str, Any]) -> OperationResult:
        """
        Execute an operation with parameter validation (Template Method).

        Validates parameters against the operation definition before
        delegating to the connector-specific ``_execute_operation``.
        All callers continue to use this method unchanged.
        """
        validation_error = self._validate_operation_params(operation_id, parameters)
        if validation_error:
            return validation_error
        return await self._execute_operation(operation_id, parameters)

    @abstractmethod
    async def _execute_operation(
        self, operation_id: str, parameters: dict[str, Any]
    ) -> OperationResult:
        """
        Connector-specific operation execution.

        Subclasses implement this instead of ``execute()``.
        Parameter validation has already been performed by the base class.

        Args:
            operation_id: ID of the operation to execute
            parameters: Parameters for the operation

        Returns:
            OperationResult with success status and data/error
        """
        pass

    @abstractmethod
    def get_operations(self) -> list[OperationDefinition]:
        """
        Get operation definitions for registration.

        These operations are stored in the database and can be
        discovered by the agent via search_operations.

        Returns:
            List of operation definitions
        """
        pass

    @abstractmethod
    def get_types(self) -> list[TypeDefinition]:
        """
        Get type definitions for registration.

        These types describe entities the connector works with
        (VirtualMachine, Cluster, etc.) and can be discovered
        by the agent via search_types.

        Returns:
            List of type definitions
        """
        pass

    def _get_operations_by_id(self) -> dict[str, OperationDefinition]:
        """Return operation definitions keyed by operation_id (cached)."""
        if not hasattr(self, "_operations_by_id"):
            self._operations_by_id: dict[str, OperationDefinition] = {
                op.operation_id: op for op in self.get_operations()
            }
        return self._operations_by_id

    def _validate_operation_params(
        self, operation_id: str, params: dict[str, Any]
    ) -> OperationResult | None:
        """
        Validate ``params`` against the ``OperationDefinition`` for *operation_id*.

        Returns an ``OperationResult`` describing the problem when validation
        fails, or ``None`` when everything looks fine (or the operation has no
        registered definition -- graceful skip).
        """
        ops = self._get_operations_by_id()
        op_def = ops.get(operation_id)
        if op_def is None or not op_def.parameters:
            return None

        defined_names: set[str] = {p["name"] for p in op_def.parameters}
        required_names: set[str] = {p["name"] for p in op_def.parameters if p.get("required")}
        provided_names: set[str] = set(params.keys())

        missing = required_names - provided_names
        unknown = provided_names - defined_names

        if not missing and not unknown:
            return None

        parts: list[str] = []

        if missing:
            parts.append(
                f"missing required parameter{'s' if len(missing) > 1 else ''}: "
                f"{', '.join(sorted(missing))}"
            )

        if unknown:
            suggestions: list[str] = []
            for name in sorted(unknown):
                matches = difflib.get_close_matches(name, defined_names, n=1, cutoff=0.5)
                if matches:
                    suggestions.append(f"'{name}' -> did you mean '{matches[0]}'?")
                else:
                    suggestions.append(f"'{name}' is not a valid parameter")
            parts.append("; ".join(suggestions))

        available = ", ".join(
            f"{p['name']} (required)" if p.get("required") else p["name"] for p in op_def.parameters
        )

        message = (
            f"Operation '{operation_id}' parameter error: {'. '.join(parts)}. "
            f"You provided: [{', '.join(sorted(provided_names))}]. "
            f"Available parameters: {available}"
        )

        return OperationResult(
            success=False,
            error=message,
            error_code="INVALID_PARAMETERS",
            operation_id=operation_id,
        )

    async def __aenter__(self) -> "BaseConnector":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.disconnect()
