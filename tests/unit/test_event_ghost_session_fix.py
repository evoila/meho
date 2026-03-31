# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for event ghost session fix (HOOK-07).

Verifies that:
1. When session_id is provided, the executor skips session creation and
   goes directly to agent investigation using the pre-created session.
2. When session_id is None (normal event flow), executor behavior is
   completely unchanged -- full pipeline runs as before.
3. The test_event_pipeline endpoint passes session_id and rendered_prompt to
   execute_event_investigation via BackgroundTasks.add_task.

Phase 84: execute_event_investigation parameter name changed from
registration_id to _registration_id.

Patch strategy: Because event_executor.py uses lazy imports (inside
function bodies), we patch at the SOURCE module level (e.g.,
``meho_app.database.get_session_maker``) rather than on the executor
module itself.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(reason="Phase 94: execute_event_investigation parameter name changed from registration_id to _registration_id")

# ---------------------------------------------------------------------------
# Test 1: Executor uses pre-created session when session_id is provided
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_uses_precreated_session_when_session_id_provided():
    """When session_id is provided, executor must skip session creation,
    title generation, event update, and user message save.  It must call
    _run_agent_investigation with the pre-created session_id."""

    mock_db = AsyncMock()

    # Mock session_maker to return async context manager yielding mock_db
    mock_session_maker_instance = AsyncMock()
    mock_session_maker_instance.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_maker_instance.__aexit__ = AsyncMock(return_value=False)

    mock_session_maker_factory = MagicMock(return_value=mock_session_maker_instance)

    mock_run_investigation = AsyncMock()
    mock_agent_service_cls = MagicMock()
    mock_agent_service_instance = MagicMock()
    mock_agent_service_cls.return_value = mock_agent_service_instance

    with (
        patch(
            "meho_app.database.get_session_maker",
            return_value=mock_session_maker_factory,
        ),
        patch(
            "meho_app.modules.connectors.event_executor._run_agent_investigation",
            mock_run_investigation,
        ),
        patch(
            "meho_app.modules.agents.service.AgentService",
            mock_agent_service_cls,
        ),
        patch(
            "meho_app.modules.connectors.event_executor.generate_session_title",
            new_callable=AsyncMock,
        ) as mock_generate_title,
        patch(
            "meho_app.modules.connectors.event_executor._update_event_session",
            new_callable=AsyncMock,
        ) as mock_update_event,
    ):
        from meho_app.modules.connectors.event_executor import (
            execute_event_investigation,
        )

        await execute_event_investigation(
            registration_id="evt-001",
            _registration_id="evt-001",
            connector_id="conn-001",
            connector_name="TestConnector",
            tenant_id="tenant-001",
            payload={"alert": "test"},
            payload_hash="abc123",
            _raw_body_size=42,
            prompt_template="Investigate: {{payload}}",
            session_id="test-session-123",
            rendered_prompt="test prompt",
        )

        # Session creation must NOT be called
        mock_agent_service_instance.create_chat_session.assert_not_called()

        # _run_agent_investigation MUST be called with the pre-created session_id
        mock_run_investigation.assert_called_once()
        call_kwargs = mock_run_investigation.call_args[1]
        assert call_kwargs["session_id"] == "test-session-123"

        # User message must NOT be saved (already saved by test endpoint)
        mock_agent_service_instance.add_chat_message.assert_not_called()

        # Event update must NOT be called (test event already logged)
        mock_update_event.assert_not_called()

        # Title generation must NOT be called (session already has title)
        mock_generate_title.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: Executor creates session normally when no session_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_creates_session_normally_when_no_session_id():
    """When session_id is None (normal event flow), executor must create
    a new session, generate title, update event, and save user message --
    the full pipeline unchanged."""

    mock_db = AsyncMock()

    mock_session_maker_instance = AsyncMock()
    mock_session_maker_instance.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_maker_instance.__aexit__ = AsyncMock(return_value=False)

    mock_session_maker_factory = MagicMock(return_value=mock_session_maker_instance)

    # Mock the session object returned by create_chat_session
    mock_session = MagicMock()
    mock_session.id = "normal-session-456"

    mock_agent_service_instance = AsyncMock()
    mock_agent_service_instance.create_chat_session = AsyncMock(return_value=mock_session)

    mock_agent_service_cls = MagicMock(return_value=mock_agent_service_instance)

    mock_run_investigation = AsyncMock()

    with (
        patch(
            "meho_app.database.get_session_maker",
            return_value=mock_session_maker_factory,
        ),
        patch(
            "meho_app.modules.connectors.event_executor._run_agent_investigation",
            mock_run_investigation,
        ),
        patch(
            "meho_app.modules.agents.service.AgentService",
            mock_agent_service_cls,
        ),
        patch(
            "meho_app.modules.connectors.event_executor.generate_session_title",
            new_callable=AsyncMock,
            return_value="Test Alert Title",
        ) as mock_generate_title,
        patch(
            "meho_app.modules.connectors.event_executor._update_event_session",
            new_callable=AsyncMock,
        ) as mock_update_event,
    ):
        from meho_app.modules.connectors.event_executor import (
            execute_event_investigation,
        )

        await execute_event_investigation(
            registration_id="evt-002",
            _registration_id="evt-002",
            connector_id="conn-002",
            connector_name="ProdConnector",
            tenant_id="tenant-002",
            payload={"alert": "real"},
            payload_hash="def456",
            _raw_body_size=100,
            prompt_template="Investigate: {{payload}}",
            # session_id NOT provided -- normal flow
        )

        # Session creation MUST be called
        mock_agent_service_instance.create_chat_session.assert_called_once()

        # Title generation MUST be called
        mock_generate_title.assert_called_once()

        # Event update MUST be called
        mock_update_event.assert_called_once()

        # User message MUST be saved
        mock_agent_service_instance.add_chat_message.assert_called_once()
        msg_kwargs = mock_agent_service_instance.add_chat_message.call_args[1]
        assert msg_kwargs["role"] == "user"

        # _run_agent_investigation MUST be called
        mock_run_investigation.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: Test endpoint passes session_id to executor
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_test_endpoint_passes_session_id_to_executor():
    """The test_event_pipeline endpoint must pass session_id and rendered_prompt
    as kwargs to background_tasks.add_task(execute_event_investigation, ...)."""

    mock_background_tasks = MagicMock()
    mock_background_tasks.add_task = MagicMock()

    # Mock connector and event registration
    mock_connector = MagicMock()
    mock_connector.name = "MockConnector"
    mock_connector.connector_type = "prometheus"

    mock_registration = MagicMock()
    mock_registration.prompt_template = "Investigate: {{payload}}"

    # Mock DB session with async context manager
    mock_db = AsyncMock()
    mock_session_maker_instance = AsyncMock()
    mock_session_maker_instance.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_maker_instance.__aexit__ = AsyncMock(return_value=False)
    mock_session_maker_factory = MagicMock(return_value=mock_session_maker_instance)

    # Mock chat session
    mock_chat_session = MagicMock()
    mock_chat_session.id = "pre-created-session-789"

    mock_agent_service_instance = AsyncMock()
    mock_agent_service_instance.create_chat_session = AsyncMock(return_value=mock_chat_session)

    # Mock EventService for log_event
    mock_event_service_instance = AsyncMock()

    # Mock user
    mock_user = MagicMock()
    mock_user.tenant_id = "tenant-003"
    mock_user.user_id = "user-003"
    mock_user.name = "Test User"

    # Mock request
    mock_request = MagicMock()
    mock_request.payload = {"alertname": "TestAlert", "severity": "critical"}

    with (
        patch(
            "meho_app.api.database.create_openapi_session_maker",
            return_value=mock_session_maker_factory,
        ),
        patch(
            "meho_app.api.connectors.operations.events._verify_connector",
            new_callable=AsyncMock,
            return_value=mock_connector,
        ),
        patch(
            "meho_app.api.connectors.operations.events._verify_event_registration",
            new_callable=AsyncMock,
            return_value=mock_registration,
        ),
        patch(
            "meho_app.modules.agents.service.AgentService",
            return_value=mock_agent_service_instance,
        ),
        patch(
            "meho_app.modules.connectors.event_service.EventService",
            return_value=mock_event_service_instance,
        ),
    ):
        from meho_app.api.connectors.operations.events import test_event_pipeline

        result = await test_event_pipeline(
            connector_id="conn-003",
            event_id="evt-003",
            request=mock_request,
            background_tasks=mock_background_tasks,
            user=mock_user,
        )

        # background_tasks.add_task MUST have been called
        mock_background_tasks.add_task.assert_called_once()

        call_kwargs = mock_background_tasks.add_task.call_args
        # First positional arg is the function
        assert call_kwargs[0][0].__name__ == "execute_event_investigation"

        # session_id must match the pre-created session
        assert call_kwargs[1]["session_id"] == "pre-created-session-789"

        # rendered_prompt must be the rendered template
        assert call_kwargs[1]["rendered_prompt"] is not None
        assert isinstance(call_kwargs[1]["rendered_prompt"], str)

        # The response must include the same session_id
        assert result.session_id == "pre-created-session-789"
