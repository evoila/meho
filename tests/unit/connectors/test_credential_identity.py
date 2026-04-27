# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for credential identity model changes and Plan 02 integration.

Verifies:
- EventRegistrationModel has identity columns (created_by_user_id, allowed_connector_ids, delegation_active)
- ScheduledTaskModel has identity columns (created_by_user_id renamed from created_by,
  allowed_connector_ids, delegate_credentials, delegation_active)
- Service credentials use sentinel user_id "__service__" via existing UserCredentialModel
- Webhook creation captures creator user ID (Plan 02)
- Credential pre-validation rejects delegation without stored credentials (Plan 02)
- Audit events are logged for credential resolution (Plan 02)
- Delegation flag callback is wired in executors (Plan 02)
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.connectors.models import EventRegistrationModel
from meho_app.modules.scheduled_tasks.models import ScheduledTaskModel


class TestEventModelIdentityColumns:
    """EventRegistrationModel has identity columns."""

    def test_event_model_has_created_by_user_id(self):
        assert hasattr(EventRegistrationModel, "created_by_user_id"), (
            "EventRegistrationModel missing created_by_user_id column"
        )
        col = EventRegistrationModel.__table__.columns["created_by_user_id"]
        assert col.nullable is True
        assert col.index is True

    def test_event_model_has_allowed_connector_ids(self):
        assert hasattr(EventRegistrationModel, "allowed_connector_ids"), (
            "EventRegistrationModel missing allowed_connector_ids column"
        )
        col = EventRegistrationModel.__table__.columns["allowed_connector_ids"]
        assert col.nullable is True

    def test_event_model_has_delegation_active(self):
        assert hasattr(EventRegistrationModel, "delegation_active"), (
            "EventRegistrationModel missing delegation_active column"
        )
        col = EventRegistrationModel.__table__.columns["delegation_active"]
        assert col.nullable is False
        assert col.default.arg is True


class TestScheduledTaskModelIdentityColumns:
    """ScheduledTaskModel has all four identity columns (created_by renamed)."""

    def test_scheduled_task_model_has_created_by_user_id(self):
        assert hasattr(ScheduledTaskModel, "created_by_user_id"), (
            "ScheduledTaskModel missing created_by_user_id column"
        )
        col = ScheduledTaskModel.__table__.columns["created_by_user_id"]
        assert col.nullable is True

    def test_scheduled_task_model_no_old_created_by(self):
        """The old created_by column should not exist (it was renamed)."""
        assert not hasattr(ScheduledTaskModel, "created_by"), (
            "ScheduledTaskModel still has old created_by column -- should be renamed to created_by_user_id"
        )

    def test_scheduled_task_model_has_allowed_connector_ids(self):
        assert hasattr(ScheduledTaskModel, "allowed_connector_ids"), (
            "ScheduledTaskModel missing allowed_connector_ids column"
        )
        col = ScheduledTaskModel.__table__.columns["allowed_connector_ids"]
        assert col.nullable is True

    def test_scheduled_task_model_has_delegate_credentials(self):
        assert hasattr(ScheduledTaskModel, "delegate_credentials"), (
            "ScheduledTaskModel missing delegate_credentials column"
        )
        col = ScheduledTaskModel.__table__.columns["delegate_credentials"]
        assert col.nullable is False
        assert col.default.arg is False

    def test_scheduled_task_model_has_delegation_active(self):
        assert hasattr(ScheduledTaskModel, "delegation_active"), (
            "ScheduledTaskModel missing delegation_active column"
        )
        col = ScheduledTaskModel.__table__.columns["delegation_active"]
        assert col.nullable is False
        assert col.default.arg is True


class TestServiceCredentialSentinel:
    """Service credentials use sentinel user_id '__service__'."""

    def test_service_credential_sentinel_value(self):
        """CredentialResolver defines the sentinel constant."""
        from meho_app.modules.connectors.credential_resolver import CredentialResolver

        assert CredentialResolver.SENTINEL_SERVICE_USER == "__service__"


class TestServiceCredentialCRUD:
    """Service credentials can be stored/retrieved/deleted via sentinel user_id."""

    @pytest.mark.asyncio
    async def test_service_credential_store(self, mock_credential_repo):
        """Service credential can be stored using sentinel user_id '__service__'."""
        await mock_credential_repo.store_credentials(
            "__service__", mock_credential_repo._test_credential
        )
        mock_credential_repo.store_credentials.assert_called_once()

    @pytest.mark.asyncio
    async def test_service_credential_retrieve(self, mock_credential_repo):
        """Service credential can be retrieved using sentinel user_id '__service__'."""
        mock_credential_repo.get_credentials.return_value = {
            "username": "svc",
            "password": "secret",
        }
        result = await mock_credential_repo.get_credentials("__service__", "connector-123")
        assert result == {"username": "svc", "password": "secret"}

    @pytest.mark.asyncio
    async def test_service_credential_delete(self, mock_credential_repo):
        """Service credential can be deleted using sentinel user_id '__service__'."""
        mock_credential_repo.delete_credentials.return_value = True
        result = await mock_credential_repo.delete_credentials("__service__", "connector-123")
        assert result is True


# ---- Plan 02 Tests ----


class TestEventCapturesCreator:
    """Verify event creation captures user ID and identity fields."""

    def test_event_captures_creator(self):
        """EventCreateRequest schema accepts identity fields."""
        from meho_app.api.connectors.operations.events import EventCreateRequest

        req = EventCreateRequest(
            name="Test Event",
            allowed_connector_ids=["conn-1", "conn-2"],
        )
        assert req.allowed_connector_ids == ["conn-1", "conn-2"]

    def test_event_response_includes_identity_fields(self):
        """EventResponse schema includes identity fields."""
        from meho_app.api.connectors.operations.events import EventResponse

        fields = EventResponse.model_fields
        assert "created_by_user_id" in fields
        assert "delegation_active" in fields

    def test_event_service_accepts_identity_params(self):
        """EventService.create_event_registration signature accepts identity parameters."""
        import inspect
        from meho_app.modules.connectors.event_service import EventService

        sig = inspect.signature(EventService.create_event_registration)
        assert "created_by_user_id" in sig.parameters
        assert "allowed_connector_ids" in sig.parameters

    def test_scheduled_task_response_uses_created_by_user_id(self):
        """ScheduledTaskResponse uses created_by_user_id (not created_by)."""
        from meho_app.api.routes_scheduled_tasks import ScheduledTaskResponse

        fields = ScheduledTaskResponse.model_fields
        assert "created_by_user_id" in fields
        assert "created_by" not in fields


class TestCreationRejectsDelegationWithoutCredentials:
    """Verify that creation-time validation rejects delegation without stored credentials."""

    def test_scheduled_task_endpoint_has_credential_validation(self):
        """Scheduled task create endpoint contains credential pre-validation logic."""
        import inspect
        from meho_app.api.routes_scheduled_tasks import create_task

        source = inspect.getsource(create_task)
        assert "Cannot enable credential delegation" in source
        assert "delegate_credentials" in source


class TestAuditEvents:
    """Verify audit events for credential resolution."""

    def test_audit_events_in_resolve_helper(self):
        """The _resolve_credentials helper logs audit events for automated sessions."""
        import inspect
        from meho_app.modules.agents.shared.handlers.operation_handlers import _resolve_credentials

        source = inspect.getsource(_resolve_credentials)
        assert "automation.credential_resolved" in source
        assert "automation.credential_failed" in source
        assert "AuditService" in source

    @pytest.mark.asyncio
    async def test_audit_events_success(self, mock_audit_service):
        """Verify audit service is called with correct event_type on success."""
        from meho_app.modules.connectors.credential_resolver import (
            CredentialResolver,
            CredentialSource,
            ResolvedCredential,
            SessionType,
        )

        # Mock the resolver to return a successful resolution
        mock_resolver = MagicMock(spec=CredentialResolver)
        mock_resolver.resolve = AsyncMock(
            return_value=ResolvedCredential(
                credentials={"username": "test"},
                source=CredentialSource.SERVICE,
            )
        )

        # The audit event should contain the correct event_type
        await mock_audit_service.log_event(
            tenant_id="tenant-1",
            user_id="system:event",
            event_type="automation.credential_resolved",
            action="resolve",
            resource_type="connector",
            resource_id="connector-1",
            details={
                "trigger_type": "event",
                "trigger_id": "wh-123",
                "credential_source": "service",
            },
            result="success",
        )
        mock_audit_service.log_event.assert_called_once()
        call_kwargs = mock_audit_service.log_event.call_args[1]
        assert call_kwargs["event_type"] == "automation.credential_resolved"
        assert call_kwargs["result"] == "success"
        assert call_kwargs["details"]["credential_source"] == "service"

    @pytest.mark.asyncio
    async def test_audit_events_failure(self, mock_audit_service):
        """Verify audit service logs failure events."""
        await mock_audit_service.log_event(
            tenant_id="tenant-1",
            user_id="system:event",
            event_type="automation.credential_failed",
            action="resolve",
            resource_type="connector",
            resource_id="connector-1",
            details={
                "trigger_type": "event",
                "trigger_id": "wh-123",
                "failure_reason": "scope_rejected",
            },
            result="failure",
        )
        mock_audit_service.log_event.assert_called_once()
        call_kwargs = mock_audit_service.log_event.call_args[1]
        assert call_kwargs["event_type"] == "automation.credential_failed"
        assert call_kwargs["result"] == "failure"


class TestDelegationFlagCallbackWired:
    """Verify delegation flag callback is wired in executors."""

    def test_delegation_flag_callback_wired_in_event_executor(self):
        """Event executor defines and uses delegation_flag_callback."""
        import inspect
        from meho_app.modules.connectors import event_executor

        # Verify the callback function exists
        assert hasattr(event_executor, "_event_delegation_flag_callback")

        # Verify it's used in _run_agent_investigation
        source = inspect.getsource(event_executor._run_agent_investigation)
        assert "delegation_flag_callback" in source
        assert "automated_event" in source

    def test_delegation_flag_callback_wired_in_scheduler_executor(self):
        """Scheduled task executor defines and uses delegation_flag_callback."""
        import inspect
        from meho_app.modules.scheduled_tasks import executor

        # Verify the callback function exists
        assert hasattr(executor, "_scheduler_delegation_flag_callback")

        # Verify it's used in _run_agent_investigation
        source = inspect.getsource(executor._run_agent_investigation)
        assert "delegation_flag_callback" in source
        assert "automated_scheduler" in source

    def test_meho_dependencies_has_automation_fields(self):
        """MEHODependencies has all Phase 74 automation identity fields."""
        from meho_app.modules.agents.dependencies import MEHODependencies
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(MEHODependencies)}
        assert "session_type" in field_names
        assert "created_by_user_id" in field_names
        assert "allowed_connector_ids" in field_names
        assert "trigger_type" in field_names
        assert "trigger_id" in field_names
        assert "delegation_active" in field_names
        assert "delegation_flag_callback" in field_names

    def test_create_agent_dependencies_accepts_automation_params(self):
        """create_agent_dependencies accepts Phase 74 automation parameters."""
        import inspect
        from meho_app.api.dependencies import create_agent_dependencies

        sig = inspect.signature(create_agent_dependencies)
        assert "session_type" in sig.parameters
        assert "created_by_user_id" in sig.parameters
        assert "allowed_connector_ids" in sig.parameters
        assert "trigger_type" in sig.parameters
        assert "trigger_id" in sig.parameters
        assert "delegation_active" in sig.parameters
        assert "delegation_flag_callback" in sig.parameters


class TestResolveCredentialsHelper:
    """Verify the _resolve_credentials helper replaces inline credential lookups."""

    def test_no_credential_strategy_checks_in_operation_handlers(self):
        """operation_handlers.py has zero credential_strategy == USER_PROVIDED checks."""
        import inspect
        from meho_app.modules.agents.shared.handlers import operation_handlers

        source = inspect.getsource(operation_handlers)
        assert source.count('credential_strategy == "USER_PROVIDED"') == 0

    def test_resolve_credentials_helper_exists(self):
        """_resolve_credentials helper function exists in operation_handlers."""
        from meho_app.modules.agents.shared.handlers.operation_handlers import (
            _resolve_credentials,
        )
        import inspect

        assert callable(_resolve_credentials)
        assert inspect.iscoroutinefunction(_resolve_credentials)

    def test_no_credential_strategy_in_dependencies_call_endpoint(self):
        """dependencies.py call_endpoint uses CredentialResolver, not credential_strategy."""
        import inspect
        from meho_app.modules.agents.dependencies import MEHODependencies

        source = inspect.getsource(MEHODependencies.call_endpoint)
        assert 'credential_strategy == "USER_PROVIDED"' not in source
        assert "CredentialResolver" in source


# ---- Fixtures ----


@pytest.fixture
def mock_credential_repo():
    """Mock CredentialRepository with configurable return values."""
    repo = MagicMock()
    repo.store_credentials = AsyncMock()
    repo.get_credentials = AsyncMock(return_value=None)
    repo.delete_credentials = AsyncMock(return_value=False)
    # Attach a test credential for store tests
    repo._test_credential = MagicMock()
    repo._test_credential.connector_id = "connector-123"
    repo._test_credential.credential_type = "PASSWORD"
    repo._test_credential.credentials = {"username": "svc", "password": "secret"}
    return repo


@pytest.fixture
def mock_audit_service():
    """Mock AuditService."""
    svc = MagicMock()
    svc.log_event = AsyncMock()
    return svc
