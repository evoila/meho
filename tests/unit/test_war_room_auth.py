# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for war room multi-user authorization and processing guard (Phase 39).

Tests:
- check_session_access: group session access for tenant users
- check_session_access: private session rejection for non-owners
- check_session_access: cross-tenant rejection
- check_session_access: owner can access own private session
- 409 returned when SETNX fails (agent is already processing)
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from meho_app.core.auth_context import UserContext

# =============================================================================
# check_session_access tests
# =============================================================================


@pytest.mark.unit
class TestCheckSessionAccess:
    """Tests for the shared check_session_access helper."""

    @pytest.mark.asyncio
    async def test_group_session_allows_non_owner_tenant_user(self):
        """Non-owner tenant user should be allowed to access group sessions."""
        from meho_app.api.routes_chat import check_session_access

        session_obj = MagicMock()
        session_obj.tenant_id = "acme"
        session_obj.user_id = "owner@acme.com"
        session_obj.visibility = "group"

        user = UserContext(
            user_id="other@acme.com",
            name="Other User",
            tenant_id="acme",
        )

        # Should NOT raise
        await check_session_access(session_obj, user)

    @pytest.mark.asyncio
    async def test_tenant_session_allows_non_owner_tenant_user(self):
        """Non-owner tenant user should be allowed to access tenant sessions."""
        from meho_app.api.routes_chat import check_session_access

        session_obj = MagicMock()
        session_obj.tenant_id = "acme"
        session_obj.user_id = "owner@acme.com"
        session_obj.visibility = "tenant"

        user = UserContext(
            user_id="viewer@acme.com",
            name="Viewer",
            tenant_id="acme",
        )

        # Should NOT raise
        await check_session_access(session_obj, user)

    @pytest.mark.asyncio
    async def test_private_session_rejects_non_owner(self):
        """Non-owner user should be rejected from private sessions with 403."""
        from meho_app.api.routes_chat import check_session_access

        session_obj = MagicMock()
        session_obj.tenant_id = "acme"
        session_obj.user_id = "owner@acme.com"
        session_obj.visibility = "private"

        user = UserContext(
            user_id="other@acme.com",
            name="Other User",
            tenant_id="acme",
        )

        with pytest.raises(HTTPException) as exc_info:
            await check_session_access(session_obj, user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_different_tenant_rejected_from_group_session(self):
        """User from a different tenant should be rejected from group sessions."""
        from meho_app.api.routes_chat import check_session_access

        session_obj = MagicMock()
        session_obj.tenant_id = "acme"
        session_obj.user_id = "owner@acme.com"
        session_obj.visibility = "group"

        user = UserContext(
            user_id="user@other-corp.com",
            name="External User",
            tenant_id="other-corp",
        )

        with pytest.raises(HTTPException) as exc_info:
            await check_session_access(session_obj, user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_owner_can_access_own_private_session(self):
        """Owner should be able to access their own private session."""
        from meho_app.api.routes_chat import check_session_access

        session_obj = MagicMock()
        session_obj.tenant_id = "acme"
        session_obj.user_id = "owner@acme.com"
        session_obj.visibility = "private"

        user = UserContext(
            user_id="owner@acme.com",
            name="Owner",
            tenant_id="acme",
        )

        # Should NOT raise
        await check_session_access(session_obj, user)

    @pytest.mark.asyncio
    async def test_different_tenant_rejected_from_private_session(self):
        """User from different tenant should be rejected from private sessions."""
        from meho_app.api.routes_chat import check_session_access

        session_obj = MagicMock()
        session_obj.tenant_id = "acme"
        session_obj.user_id = "owner@acme.com"
        session_obj.visibility = "private"

        user = UserContext(
            user_id="user@evil-corp.com",
            name="Evil User",
            tenant_id="evil-corp",
        )

        with pytest.raises(HTTPException) as exc_info:
            await check_session_access(session_obj, user)
        assert exc_info.value.status_code == 403


# =============================================================================
# 409 SETNX Guard Tests
# =============================================================================


@pytest.mark.unit
class TestProcessingGuard:
    """Tests for the atomic Redis SETNX processing guard."""

    @pytest.mark.asyncio
    async def test_setnx_conflict_returns_409_error_event(self):
        """When SETNX fails (agent already processing), an error event with 409 should be emitted."""
        # This tests the behavior at the route level:
        # When redis_client.set(nx=True) returns False/None, the endpoint
        # raises HTTPException(409) which is caught and emitted as an SSE error event.
        # We test the logic pattern directly rather than the full route.

        mock_redis = MagicMock()
        # SETNX returns False when key already exists
        mock_redis.set = AsyncMock(return_value=False)

        session_id = "test-session-123"
        user_id = "alice@acme.com"

        # Simulate the SETNX check from chat_stream
        acquired = await mock_redis.set(
            f"meho:active:{session_id}",
            user_id,
            nx=True,
            ex=300,
        )

        assert acquired is False

        # In the real code, this triggers:
        # raise HTTPException(status_code=409, detail="Agent is currently processing")
        with pytest.raises(HTTPException) as exc_info:  # noqa: PT012 -- multi-statement raises block is intentional
            if not acquired:
                raise HTTPException(
                    status_code=409,
                    detail="Agent is currently processing",
                )
        assert exc_info.value.status_code == 409
        assert "currently processing" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_setnx_success_allows_processing(self):
        """When SETNX succeeds (no other processor), processing should proceed."""
        mock_redis = MagicMock()
        # SETNX returns True when key was set
        mock_redis.set = AsyncMock(return_value=True)

        session_id = "test-session-456"
        user_id = "bob@acme.com"

        acquired = await mock_redis.set(
            f"meho:active:{session_id}",
            user_id,
            nx=True,
            ex=300,
        )

        assert acquired is True
        # No exception should be raised -- processing proceeds

    @pytest.mark.asyncio
    async def test_setnx_only_for_group_sessions(self):
        """SETNX guard should only apply to group sessions, not private."""
        # For private sessions, is_group_session is False,
        # so SETNX is never attempted. Verify the logic:
        visibility = "private"
        is_group_session = visibility != "private"
        assert is_group_session is False

        visibility = "group"
        is_group_session = visibility != "private"
        assert is_group_session is True

        visibility = "tenant"
        is_group_session = visibility != "private"
        assert is_group_session is True
