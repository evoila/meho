# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Test the test fixtures themselves.
"""

import pytest

from tests.support.fixtures import (
    create_test_connector,
    create_test_embedding,
    create_test_knowledge_chunk_create,
    create_test_user_context,
    create_test_workflow,
    generate_test_email,
    generate_test_id,
)


@pytest.mark.unit
def test_create_test_user_context():
    """Test user context factory"""
    user_ctx = create_test_user_context()

    # Now returns actual UserContext object
    assert user_ctx.user_id is not None
    assert user_ctx.tenant_id == "test-tenant"
    assert user_ctx.roles == ["user"]
    assert user_ctx.groups == []


@pytest.mark.unit
def test_create_test_user_context_with_overrides():
    """Test user context factory with custom values"""
    user_ctx = create_test_user_context(
        user_id="custom-user", tenant_id="custom-tenant", roles=["admin", "user"], groups=["admins"]
    )

    # Now returns actual UserContext object
    assert user_ctx.user_id == "custom-user"
    assert user_ctx.tenant_id == "custom-tenant"
    assert user_ctx.roles == ["admin", "user"]
    assert user_ctx.groups == ["admins"]


@pytest.mark.unit
def test_create_test_knowledge_chunk_create():
    """Test knowledge chunk factory"""
    chunk = create_test_knowledge_chunk_create()

    assert "text" in chunk
    assert "tenant_id" in chunk
    assert chunk["tenant_id"] == "test-tenant"
    assert chunk["tags"] == ["test"]


@pytest.mark.unit
def test_create_test_connector():
    """Test connector factory"""
    connector = create_test_connector()

    assert "id" in connector
    assert "name" in connector
    assert "base_url" in connector
    assert connector["auth_type"] == "API_KEY"
    assert connector["is_active"] is True


@pytest.mark.unit
def test_create_test_workflow():
    """Test workflow factory"""
    workflow = create_test_workflow()

    assert "id" in workflow
    assert "tenant_id" in workflow
    assert "user_id" in workflow
    assert "status" in workflow
    assert workflow["status"] == "PLANNING"


@pytest.mark.unit
def test_generate_test_id():
    """Test ID generation"""
    test_id = generate_test_id()

    # Should be valid UUID
    import uuid

    uuid.UUID(test_id)  # Will raise if invalid


@pytest.mark.unit
def test_generate_test_id_with_prefix():
    """Test ID generation with prefix"""
    test_id = generate_test_id(prefix="user-")

    assert test_id.startswith("user-")


@pytest.mark.unit
def test_generate_test_email():
    """Test email generation"""
    email = generate_test_email()

    assert "@" in email
    assert email.endswith("@example.com")


@pytest.mark.unit
def test_create_test_embedding():
    """Test embedding generation"""
    embedding = create_test_embedding(dimension=128)

    assert len(embedding) == 128
    assert all(isinstance(x, float) for x in embedding)
    assert all(0 <= x <= 1 for x in embedding)


@pytest.mark.unit
def test_create_test_embedding_is_deterministic():
    """Test that embeddings are deterministic (same seed = same output)"""
    embedding1 = create_test_embedding(dimension=10)
    embedding2 = create_test_embedding(dimension=10)

    assert embedding1 == embedding2
