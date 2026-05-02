# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for Atlassian 204 No Content handling.

Jira responds with 204 No Content for transition_issue and some PUT operations.
The base _post() and _put() methods must return {} instead of calling
response.json() (which raises JSONDecodeError on empty body).
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_app.modules.connectors.atlassian.base import AtlassianHTTPConnector


class ConcreteAtlassianConnector(AtlassianHTTPConnector):
    """Minimal concrete subclass for testing (AtlassianHTTPConnector is abstract)."""

    def test_connection(self) -> bool:
        return True

    def execute(self, operation: str, params: dict[str, Any]) -> Any:
        return {}

    async def _execute_operation(self, operation_id: str, parameters: dict[str, Any]) -> Any:
        return {}

    def get_operations(self) -> list[str]:
        return []

    def get_types(self) -> list[str]:
        return []


def _make_mock_response(status_code: int, json_data: Any = None):
    """Create a mock httpx.Response with configurable status and json."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    resp.raise_for_status = MagicMock()

    if json_data is not None:
        resp.json = MagicMock(return_value=json_data)
    else:
        # Simulate 204 No Content: calling .json() would raise
        resp.json = MagicMock(side_effect=Exception("No JSON body on 204"))

    return resp


@pytest.fixture
def connector():
    """Create a connector instance with a mocked httpx client."""
    c = ConcreteAtlassianConnector(
        connector_id="test-123",
        config={"base_url": "https://test.atlassian.net"},
        credentials={"email": "test@example.com", "api_token": "fake-token"},
    )
    c._client = AsyncMock()
    c._is_connected = True
    return c


@pytest.mark.asyncio
async def test_post_returns_empty_dict_on_204(connector):
    """_post() returns {} when Jira responds with 204 No Content."""
    connector._client.post = AsyncMock(return_value=_make_mock_response(204))

    result = await connector._post(
        "/rest/api/3/issue/KEY-1/transitions", json={"transition": {"id": "31"}}
    )

    assert result == {}


@pytest.mark.asyncio
async def test_post_returns_json_on_200(connector):
    """_post() returns parsed JSON when Jira responds with 200."""
    expected = {"id": "12345", "key": "PROJ-1"}
    connector._client.post = AsyncMock(return_value=_make_mock_response(200, expected))

    result = await connector._post("/rest/api/3/issue", json={"fields": {}})

    assert result == expected


@pytest.mark.asyncio
async def test_put_returns_empty_dict_on_204(connector):
    """_put() returns {} when Jira responds with 204 No Content."""
    connector._client.put = AsyncMock(return_value=_make_mock_response(204))

    result = await connector._put("/rest/api/3/issue/KEY-1", json={"fields": {}})

    assert result == {}


@pytest.mark.asyncio
async def test_put_returns_json_on_200(connector):
    """_put() returns parsed JSON when Jira responds with 200."""
    expected = {"id": "12345", "self": "https://test.atlassian.net/rest/api/3/issue/12345"}
    connector._client.put = AsyncMock(return_value=_make_mock_response(200, expected))

    result = await connector._put("/rest/api/3/issue/KEY-1", json={"fields": {}})

    assert result == expected
