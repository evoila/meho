# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app.core.auth_context
"""

import pytest

from meho_app.core.auth_context import RequestContext, UserContext


@pytest.mark.unit
def test_user_context_minimal():
    """Test UserContext with only required fields"""
    user = UserContext(user_id="user-123")

    assert user.user_id == "user-123"
    assert user.tenant_id is None
    assert user.system_id is None
    assert user.roles == []
    assert user.groups == []


@pytest.mark.unit
def test_user_context_all_fields():
    """Test UserContext with all fields"""
    user = UserContext(
        user_id="user-123",
        tenant_id="tenant-456",
        system_id="system-789",
        roles=["admin", "user"],
        groups=["team-a", "contract:providerX"],
    )

    assert user.user_id == "user-123"
    assert user.tenant_id == "tenant-456"
    assert user.system_id == "system-789"
    assert user.roles == ["admin", "user"]
    assert user.groups == ["team-a", "contract:providerX"]


@pytest.mark.unit
def test_user_context_has_role():
    """Test UserContext.has_role() method"""
    user = UserContext(user_id="user1", roles=["admin", "user"])

    assert user.has_role("admin") is True
    assert user.has_role("user") is True
    assert user.has_role("superadmin") is False


@pytest.mark.unit
def test_user_context_has_any_role():
    """Test UserContext.has_any_role() method"""
    user = UserContext(user_id="user1", roles=["admin", "user"])

    assert user.has_any_role(["admin"]) is True
    assert user.has_any_role(["superadmin", "admin"]) is True
    assert user.has_any_role(["superadmin", "operator"]) is False


@pytest.mark.unit
def test_user_context_has_all_roles():
    """Test UserContext.has_all_roles() method"""
    user = UserContext(user_id="user1", roles=["admin", "user", "operator"])

    assert user.has_all_roles(["admin", "user"]) is True
    assert user.has_all_roles(["admin"]) is True
    assert user.has_all_roles(["admin", "superadmin"]) is False


@pytest.mark.unit
def test_user_context_has_group():
    """Test UserContext.has_group() method"""
    user = UserContext(user_id="user1", groups=["team-a", "contract:providerX"])

    assert user.has_group("team-a") is True
    assert user.has_group("contract:providerX") is True
    assert user.has_group("team-b") is False


@pytest.mark.unit
def test_user_context_has_any_group():
    """Test UserContext.has_any_group() method"""
    user = UserContext(user_id="user1", groups=["team-a", "contract:providerX"])

    assert user.has_any_group(["team-a"]) is True
    assert user.has_any_group(["team-b", "team-a"]) is True
    assert user.has_any_group(["team-b", "team-c"]) is False


@pytest.mark.unit
def test_user_context_is_admin():
    """Test UserContext.is_admin() method"""
    admin = UserContext(user_id="user1", roles=["admin"])
    user = UserContext(user_id="user2", roles=["user"])

    assert admin.is_admin() is True
    assert user.is_admin() is False


@pytest.mark.unit
def test_user_context_is_global_admin():
    """Test UserContext.is_global_admin() method"""
    # Global admin with no tenant
    global_admin = UserContext(user_id="user1", tenant_id=None, roles=["global_admin"])

    # Global admin from master realm (Keycloak)
    master_realm_admin = UserContext(
        user_id="superadmin@meho.local", tenant_id="master", roles=["global_admin"]
    )

    # Tenant admin (has global_admin role but is in a tenant)
    tenant_admin = UserContext(user_id="user2", tenant_id="tenant-123", roles=["global_admin"])

    # Regular admin without global_admin role
    regular_admin = UserContext(user_id="user3", tenant_id="master", roles=["admin"])

    assert global_admin.is_global_admin() is True
    assert master_realm_admin.is_global_admin() is True  # master realm counts as global
    assert tenant_admin.is_global_admin() is False  # tenant-123 is not global
    assert regular_admin.is_global_admin() is False  # missing global_admin role


@pytest.mark.unit
def test_user_context_json_serialization():
    """Test UserContext can be serialized to/from JSON"""
    user = UserContext(
        user_id="user-123", tenant_id="tenant-456", roles=["admin"], groups=["team-a"]
    )

    # Serialize
    json_data = user.model_dump_json()
    assert isinstance(json_data, str)

    # Deserialize
    user2 = UserContext.model_validate_json(json_data)
    assert user2.user_id == user.user_id
    assert user2.tenant_id == user.tenant_id
    assert user2.roles == user.roles


@pytest.mark.unit
def test_request_context_minimal():
    """Test RequestContext with minimal fields"""
    user = UserContext(user_id="user-123")
    req_ctx = RequestContext(user=user)

    assert req_ctx.user.user_id == "user-123"
    assert req_ctx.request_id is not None
    assert req_ctx.session_id is None
    assert req_ctx.trace_id is None


@pytest.mark.unit
def test_request_context_auto_generates_request_id():
    """Test RequestContext auto-generates request_id"""
    user = UserContext(user_id="user-123")

    req_ctx1 = RequestContext(user=user)
    req_ctx2 = RequestContext(user=user)

    # Should have different request IDs
    assert req_ctx1.request_id != req_ctx2.request_id
    assert req_ctx1.request_id is not None
    assert req_ctx2.request_id is not None


@pytest.mark.unit
def test_request_context_all_fields():
    """Test RequestContext with all fields"""
    user = UserContext(user_id="user-123", tenant_id="tenant-456")
    req_ctx = RequestContext(
        user=user, request_id="req-789", session_id="session-abc", trace_id="trace-xyz"
    )

    assert req_ctx.user.user_id == "user-123"
    assert req_ctx.request_id == "req-789"
    assert req_ctx.session_id == "session-abc"
    assert req_ctx.trace_id == "trace-xyz"


@pytest.mark.unit
def test_request_context_to_log_context():
    """Test RequestContext.to_log_context() method"""
    user = UserContext(user_id="user-123", tenant_id="tenant-456")
    req_ctx = RequestContext(user=user, request_id="req-789", session_id="session-abc")

    log_ctx = req_ctx.to_log_context()

    assert log_ctx["user_id"] == "user-123"
    assert log_ctx["tenant_id"] == "tenant-456"
    assert log_ctx["request_id"] == "req-789"
    assert log_ctx["session_id"] == "session-abc"
    assert "trace_id" in log_ctx
