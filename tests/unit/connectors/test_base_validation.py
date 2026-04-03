# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for BaseConnector parameter validation (Template Method).

Verifies that execute() validates parameters against OperationDefinition
before delegating to _execute_operation(), producing clear actionable
error messages when the LLM sends wrong parameter names.
"""

from typing import Any

import pytest

from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)

# ---------------------------------------------------------------------------
# Concrete stub so we can instantiate BaseConnector (which is abstract)
# ---------------------------------------------------------------------------

_STUB_OPERATIONS = [
    OperationDefinition(
        operation_id="get_instance",
        name="Get Instance",
        description="Get a compute instance by name",
        category="compute",
        parameters=[
            {
                "name": "instance_name",
                "type": "string",
                "required": True,
                "description": "Name of the instance",
            },
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": "Zone of the instance",
            },
        ],
    ),
    OperationDefinition(
        operation_id="list_instances",
        name="List Instances",
        description="List all instances",
        category="compute",
        parameters=[
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": "Filter by zone",
            },
        ],
    ),
    OperationDefinition(
        operation_id="get_metrics",
        name="Get Metrics",
        description="Get instance metrics",
        category="monitoring",
        parameters=[
            {
                "name": "instance_name",
                "type": "string",
                "required": True,
                "description": "Name of the instance",
            },
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": "Zone",
            },
            {
                "name": "minutes",
                "type": "integer",
                "required": False,
                "description": "Time range",
            },
        ],
    ),
    OperationDefinition(
        operation_id="no_params_op",
        name="No Params",
        description="Operation with no parameters",
        category="misc",
        parameters=[],
    ),
]

_EXECUTE_CALLED = OperationResult(success=True, data={"stub": True}, operation_id="stub")


class StubConnector(BaseConnector):
    """Minimal concrete connector for testing."""

    async def connect(self) -> bool:
        self._is_connected = True
        return True

    async def disconnect(self) -> None:
        self._is_connected = False

    async def test_connection(self) -> bool:
        return self._is_connected

    async def _execute_operation(
        self, operation_id: str, parameters: dict[str, Any]
    ) -> OperationResult:
        return _EXECUTE_CALLED

    def get_operations(self) -> list[OperationDefinition]:
        return _STUB_OPERATIONS

    def get_types(self) -> list[TypeDefinition]:
        return []


@pytest.fixture
def connector() -> StubConnector:
    return StubConnector(connector_id="test", config={}, credentials={})


# ---------------------------------------------------------------------------
# _validate_operation_params unit tests
# ---------------------------------------------------------------------------


class TestValidateOperationParams:
    """Direct tests of the validation helper."""

    def test_valid_params_returns_none(self, connector: StubConnector) -> None:
        result = connector._validate_operation_params(
            "get_instance", {"instance_name": "web-1", "zone": "us-east1-b"}
        )
        assert result is None

    def test_valid_required_only(self, connector: StubConnector) -> None:
        result = connector._validate_operation_params("get_instance", {"instance_name": "web-1"})
        assert result is None

    def test_missing_required_param(self, connector: StubConnector) -> None:
        result = connector._validate_operation_params("get_instance", {"zone": "us-east1-b"})
        assert result is not None
        assert result.success is False
        assert result.error_code == "INVALID_PARAMETERS"
        assert "instance_name" in result.error

    def test_unknown_param_with_close_match(self, connector: StubConnector) -> None:
        """'name' is close to 'instance_name' -- should suggest correction."""
        result = connector._validate_operation_params(
            "get_instance", {"name": "web-1", "zone": "us-east1-b"}
        )
        assert result is not None
        assert result.error_code == "INVALID_PARAMETERS"
        assert "instance_name" in result.error
        assert "did you mean" in result.error.lower()

    def test_unknown_param_no_close_match(self, connector: StubConnector) -> None:
        result = connector._validate_operation_params(
            "get_instance", {"xyzzy": "web-1", "zone": "us-east1-b"}
        )
        assert result is not None
        assert result.error_code == "INVALID_PARAMETERS"
        assert "instance_name" in result.error
        assert "not a valid parameter" in result.error

    def test_operation_not_in_definitions_skips(self, connector: StubConnector) -> None:
        result = connector._validate_operation_params("totally_unknown_op", {"whatever": 1})
        assert result is None

    def test_empty_params_all_optional(self, connector: StubConnector) -> None:
        result = connector._validate_operation_params("list_instances", {})
        assert result is None

    def test_empty_params_with_required(self, connector: StubConnector) -> None:
        result = connector._validate_operation_params("get_instance", {})
        assert result is not None
        assert "instance_name" in result.error

    def test_no_params_operation(self, connector: StubConnector) -> None:
        result = connector._validate_operation_params("no_params_op", {})
        assert result is None

    def test_mixed_correct_and_wrong(self, connector: StubConnector) -> None:
        """Some correct, some wrong -- error lists both missing and unknown."""
        result = connector._validate_operation_params(
            "get_metrics", {"name": "web-1", "minutes": 30}
        )
        assert result is not None
        assert result.error_code == "INVALID_PARAMETERS"
        assert "instance_name" in result.error
        assert "name" in result.error

    def test_available_params_listed(self, connector: StubConnector) -> None:
        result = connector._validate_operation_params("get_metrics", {})
        assert result is not None
        assert "instance_name (required)" in result.error
        assert "zone" in result.error
        assert "minutes" in result.error

    def test_cache_populated_once(self, connector: StubConnector) -> None:
        connector._validate_operation_params("get_instance", {"instance_name": "x"})
        cached = connector._get_operations_by_id()
        assert "get_instance" in cached
        assert "list_instances" in cached
        assert cached is connector._get_operations_by_id()


# ---------------------------------------------------------------------------
# Template Method integration: execute() -> validation -> _execute_operation
# ---------------------------------------------------------------------------


class TestExecuteTemplateMethod:
    """Verify the execute() Template Method wiring."""

    @pytest.mark.asyncio
    async def test_valid_params_reaches_execute_operation(self, connector: StubConnector) -> None:
        result = await connector.execute("get_instance", {"instance_name": "web-1"})
        assert result is _EXECUTE_CALLED

    @pytest.mark.asyncio
    async def test_invalid_params_short_circuits(self, connector: StubConnector) -> None:
        result = await connector.execute("get_instance", {"name": "web-1"})
        assert result.success is False
        assert result.error_code == "INVALID_PARAMETERS"
        assert result is not _EXECUTE_CALLED

    @pytest.mark.asyncio
    async def test_unknown_operation_passes_through(self, connector: StubConnector) -> None:
        result = await connector.execute("unknown_op", {"any": "thing"})
        assert result is _EXECUTE_CALLED
