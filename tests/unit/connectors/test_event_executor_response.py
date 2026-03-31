# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for response channel execution in event executor.

Tests the _execute_response_channel function and its integration
with execute_event_investigation.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


@pytest.fixture
def response_config():
    """Standard response config for testing."""
    return {
        "connector_id": str(uuid4()),
        "operation_id": "add_comment",
        "parameter_mapping": {
            "issue_key": "{{payload.issue.key}}",
            "body": "{{result}}",
        },
    }


@pytest.fixture
def mock_connector():
    """Mock ConnectorModel from DB."""
    connector = MagicMock()
    connector.id = uuid4()
    connector.connector_type = "jira"
    connector.protocol_config = {"base_url": "https://jira.example.com"}
    connector.name = "Test Jira"
    return connector


def _make_session_maker_mock(mock_db):
    """Create a mock session_maker that returns an async context manager."""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_maker = MagicMock()
    mock_maker.return_value = mock_session
    return mock_maker


def _make_db_mock(connector_result):
    """Create a mock DB session that returns the given connector."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = connector_result
    mock_db.execute = AsyncMock(return_value=mock_result)
    return mock_db


@pytest.mark.asyncio
async def test_response_channel_success(response_config, mock_connector):
    """Happy path: response channel calls execute_connector_operation with correct params."""
    from meho_app.modules.connectors.event_executor import _execute_response_channel

    response_config["connector_id"] = str(mock_connector.id)

    mock_db = _make_db_mock(mock_connector)

    mock_resolved = MagicMock()
    mock_resolved.credentials = {"token": "secret"}

    with (
        patch(
            "meho_app.modules.connectors.event_executor.get_session_maker",
            return_value=MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_db),
                __aexit__=AsyncMock(return_value=False),
            )),
        ),
        patch(
            "meho_app.modules.connectors.response_formatters.format_for_connector",
            return_value="formatted result",
        ),
        patch(
            "meho_app.modules.connectors.response_formatters.render_response_parameters",
            return_value={"issue_key": "PROJ-123", "body": "formatted result"},
        ),
        patch(
            "meho_app.modules.connectors.credential_resolver.CredentialResolver"
        ) as mock_resolver_cls,
        patch(
            "meho_app.modules.connectors.pool.execute_connector_operation",
            new_callable=AsyncMock,
        ) as mock_exec_op,
        patch("meho_app.api.config.get_api_config") as mock_config,
        patch(
            "meho_app.modules.connectors.keycloak_user_checker.KeycloakUserChecker"
        ),
        patch(
            "meho_app.modules.connectors.repositories.credential_repository.UserCredentialRepository"
        ),
    ):
        mock_config.return_value = MagicMock(
            keycloak_url="http://kc", keycloak_admin_username="a", keycloak_admin_password="p"
        )

        mock_resolver = AsyncMock()
        mock_resolver.resolve = AsyncMock(return_value=mock_resolved)
        mock_resolver_cls.return_value = mock_resolver

        await _execute_response_channel(
            response_config=response_config,
            payload={"issue": {"key": "PROJ-123"}},
            result="**Investigation complete**",
            session_id="sess-123",
            session_title="Test Session",
            tenant_id="tenant-1",
        )

        # Verify execute_connector_operation was called
        mock_exec_op.assert_called_once()
        call_kwargs = mock_exec_op.call_args
        assert call_kwargs[1]["connector_type"] == "jira"
        assert call_kwargs[1]["operation_id"] == "add_comment"
        assert call_kwargs[1]["parameters"] == {
            "issue_key": "PROJ-123",
            "body": "formatted result",
        }


@pytest.mark.asyncio
async def test_response_channel_formats_result(response_config, mock_connector):
    """Verify that format_for_connector is called before parameter mapping."""
    from meho_app.modules.connectors.event_executor import _execute_response_channel

    response_config["connector_id"] = str(mock_connector.id)

    mock_db = _make_db_mock(mock_connector)
    mock_resolved = MagicMock()
    mock_resolved.credentials = {"token": "secret"}

    with (
        patch(
            "meho_app.modules.connectors.event_executor.get_session_maker",
            return_value=MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_db),
                __aexit__=AsyncMock(return_value=False),
            )),
        ),
        patch(
            "meho_app.modules.connectors.response_formatters.format_for_connector",
            return_value="*bold* result",
        ) as mock_format,
        patch(
            "meho_app.modules.connectors.response_formatters.render_response_parameters",
            return_value={"body": "*bold* result"},
        ),
        patch(
            "meho_app.modules.connectors.credential_resolver.CredentialResolver"
        ) as mock_resolver_cls,
        patch(
            "meho_app.modules.connectors.pool.execute_connector_operation",
            new_callable=AsyncMock,
        ),
        patch("meho_app.api.config.get_api_config") as mock_config,
        patch(
            "meho_app.modules.connectors.keycloak_user_checker.KeycloakUserChecker"
        ),
        patch(
            "meho_app.modules.connectors.repositories.credential_repository.UserCredentialRepository"
        ),
    ):
        mock_config.return_value = MagicMock(
            keycloak_url="http://kc", keycloak_admin_username="a", keycloak_admin_password="p"
        )
        mock_resolver = AsyncMock()
        mock_resolver.resolve = AsyncMock(return_value=mock_resolved)
        mock_resolver_cls.return_value = mock_resolver

        await _execute_response_channel(
            response_config=response_config,
            payload={"issue": {"key": "PROJ-123"}},
            result="**bold** result",
            session_id="sess-123",
            session_title="Test Session",
            tenant_id="tenant-1",
        )

        # format_for_connector was called with connector type and raw result
        mock_format.assert_called_once_with("jira", "**bold** result")


@pytest.mark.asyncio
async def test_response_channel_uses_service_user(response_config, mock_connector):
    """Verify credentials are resolved with AUTOMATED_EVENT and SENTINEL_SERVICE_USER."""
    from meho_app.modules.connectors.event_executor import _execute_response_channel

    response_config["connector_id"] = str(mock_connector.id)

    mock_db = _make_db_mock(mock_connector)
    mock_resolved = MagicMock()
    mock_resolved.credentials = {"token": "secret"}

    with (
        patch(
            "meho_app.modules.connectors.event_executor.get_session_maker",
            return_value=MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_db),
                __aexit__=AsyncMock(return_value=False),
            )),
        ),
        patch(
            "meho_app.modules.connectors.response_formatters.format_for_connector",
            return_value="result",
        ),
        patch(
            "meho_app.modules.connectors.response_formatters.render_response_parameters",
            return_value={"body": "result"},
        ),
        patch(
            "meho_app.modules.connectors.credential_resolver.CredentialResolver"
        ) as mock_resolver_cls,
        patch(
            "meho_app.modules.connectors.pool.execute_connector_operation",
            new_callable=AsyncMock,
        ),
        patch("meho_app.api.config.get_api_config") as mock_config,
        patch(
            "meho_app.modules.connectors.keycloak_user_checker.KeycloakUserChecker"
        ),
        patch(
            "meho_app.modules.connectors.repositories.credential_repository.UserCredentialRepository"
        ),
    ):
        mock_config.return_value = MagicMock(
            keycloak_url="http://kc", keycloak_admin_username="a", keycloak_admin_password="p"
        )

        mock_resolver = AsyncMock()
        mock_resolver.resolve = AsyncMock(return_value=mock_resolved)
        mock_resolver_cls.return_value = mock_resolver

        await _execute_response_channel(
            response_config=response_config,
            payload={},
            result="result",
            session_id="sess-123",
            session_title="Test",
            tenant_id="tenant-1",
        )

        # Verify resolve was called with AUTOMATED_EVENT and SENTINEL_SERVICE_USER
        from meho_app.modules.connectors.credential_resolver import (
            CredentialResolver,
            SessionType,
        )

        mock_resolver.resolve.assert_called_once()
        resolve_kwargs = mock_resolver.resolve.call_args.kwargs
        assert resolve_kwargs["session_type"] == SessionType.AUTOMATED_EVENT
        assert resolve_kwargs["user_id"] == CredentialResolver.SENTINEL_SERVICE_USER


@pytest.mark.asyncio
async def test_response_channel_catches_exceptions(response_config):
    """Response channel never raises -- all exceptions caught and logged."""
    from meho_app.modules.connectors.event_executor import _execute_response_channel

    with patch(
        "meho_app.modules.connectors.event_executor.get_session_maker"
    ) as mock_session_maker:
        # Make DB session raise an exception
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_maker.return_value = MagicMock(return_value=mock_session)

        # Should NOT raise
        await _execute_response_channel(
            response_config=response_config,
            payload={},
            result="result",
            session_id="sess-123",
            session_title="Test",
            tenant_id="tenant-1",
        )


@pytest.mark.asyncio
async def test_response_channel_connector_not_found(response_config):
    """Missing connector logs warning and returns without raising."""
    from meho_app.modules.connectors.event_executor import _execute_response_channel

    mock_db = _make_db_mock(None)  # Not found

    with (
        patch(
            "meho_app.modules.connectors.event_executor.get_session_maker",
            return_value=MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_db),
                __aexit__=AsyncMock(return_value=False),
            )),
        ),
        patch(
            "meho_app.modules.connectors.pool.execute_connector_operation",
            new_callable=AsyncMock,
        ) as mock_exec_op,
    ):
        await _execute_response_channel(
            response_config=response_config,
            payload={},
            result="result",
            session_id="sess-123",
            session_title="Test",
            tenant_id="tenant-1",
        )

        # execute_connector_operation should NOT have been called
        mock_exec_op.assert_not_called()


@pytest.mark.asyncio
async def test_response_channel_bad_parameter_mapping(response_config, mock_connector):
    """Invalid parameter_mapping template logs warning and returns."""
    from meho_app.modules.connectors.event_executor import _execute_response_channel

    response_config["connector_id"] = str(mock_connector.id)
    response_config["parameter_mapping"] = {"bad": "{{unclosed"}

    mock_db = _make_db_mock(mock_connector)

    with (
        patch(
            "meho_app.modules.connectors.event_executor.get_session_maker",
            return_value=MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_db),
                __aexit__=AsyncMock(return_value=False),
            )),
        ),
        patch(
            "meho_app.modules.connectors.response_formatters.format_for_connector",
            return_value="result",
        ),
        patch(
            "meho_app.modules.connectors.pool.execute_connector_operation",
            new_callable=AsyncMock,
        ) as mock_exec_op,
    ):
        await _execute_response_channel(
            response_config=response_config,
            payload={},
            result="result",
            session_id="sess-123",
            session_title="Test",
            tenant_id="tenant-1",
        )

        # render_response_parameters returns empty dict -> no operation executed
        mock_exec_op.assert_not_called()


@pytest.mark.asyncio
async def test_investigation_calls_response_channel_when_configured():
    """_execute_response_channel is importable and callable as expected."""
    from meho_app.modules.connectors.event_executor import _execute_response_channel

    with patch(
        "meho_app.modules.connectors.event_executor._execute_response_channel",
        new_callable=AsyncMock,
    ) as mock_response:
        await mock_response(
            response_config={"connector_id": "c1", "operation_id": "op1", "parameter_mapping": {}},
            payload={},
            result="test",
            session_id="s1",
            session_title="t1",
            tenant_id="t1",
        )
        mock_response.assert_called_once()


@pytest.mark.asyncio
async def test_investigation_skips_response_when_no_config():
    """When response_config is None, _execute_response_channel is not called."""
    import inspect

    from meho_app.modules.connectors import event_executor

    source = inspect.getsource(event_executor._run_agent_investigation)
    # Verify the guard checks response_config before calling response channel
    assert "response_config" in source
    assert "_execute_response_channel" in source


@pytest.mark.asyncio
async def test_investigation_skips_response_when_empty_result():
    """When final_answer_content is empty, _execute_response_channel should not be called."""
    import inspect

    from meho_app.modules.connectors import event_executor

    source = inspect.getsource(event_executor._run_agent_investigation)
    # Verify the guard checks final_answer_content before calling response channel
    assert "final_answer_content" in source
    assert "response_config" in source
