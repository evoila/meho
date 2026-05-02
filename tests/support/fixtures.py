# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Test fixtures and factory functions for creating test data.

Usage:
    from tests.support.fixtures import create_test_user_context

    user_ctx = create_test_user_context(tenant_id="my-tenant")
"""

import uuid
from datetime import UTC, datetime
from typing import Any

# Import actual models when available
try:
    from meho_app.core.auth_context import RequestContext, UserContext

    HAS_AUTH_CONTEXT = True
except ImportError:
    HAS_AUTH_CONTEXT = False


# ============================================================================
# User Context Factories
# ============================================================================


def create_test_user_context(
    user_id: str | None = None,
    tenant_id: str | None = None,
    system_id: str | None = None,
    roles: list[str] | None = None,
    groups: list[str] | None = None,
    **kwargs,
):
    """
    Factory for test user contexts.

    Returns actual UserContext if available, otherwise dict.

    Args:
        user_id: User ID (auto-generated if not provided)
        tenant_id: Tenant ID (defaults to "test-tenant")
        system_id: Optional system ID
        roles: List of roles (defaults to ["user"])
        groups: List of groups (defaults to [])
        **kwargs: Additional fields

    Returns:
        UserContext instance or dict
    """
    data = {
        "user_id": user_id or f"test-user-{uuid.uuid4()}",
        "tenant_id": tenant_id or "test-tenant",
        "system_id": system_id,
        "roles": roles or ["user"],
        "groups": groups or [],
    }

    if HAS_AUTH_CONTEXT:
        return UserContext(**data, **kwargs)
    else:
        return {**data, **kwargs}


def create_test_request_context(user=None, request_id: str | None = None, **kwargs):
    """
    Factory for test request contexts.

    Args:
        user: User context (auto-generated if not provided)
        request_id: Request ID (auto-generated if not provided)
        **kwargs: Additional fields

    Returns:
        RequestContext instance or dict
    """
    if user is None:
        user = create_test_user_context()

    data = {
        "user": user,
        "request_id": request_id or f"req-{uuid.uuid4()}",
        "session_id": kwargs.pop("session_id", None),
        "trace_id": kwargs.pop("trace_id", None),
    }

    if HAS_AUTH_CONTEXT:
        return RequestContext(**data, **kwargs)
    else:
        return {**data, **kwargs}


# ============================================================================
# Knowledge Factories
# ============================================================================


def create_test_knowledge_chunk_create(
    text: str | None = None,
    tenant_id: str | None = None,
    system_id: str | None = None,
    user_id: str | None = None,
    roles: list[str] | None = None,
    groups: list[str] | None = None,
    tags: list[str] | None = None,
    source_uri: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """
    Factory for test knowledge chunk creation objects.

    Returns dict that can be used to create KnowledgeChunkCreate.
    """
    return {
        "text": text or f"Test knowledge text {uuid.uuid4()}",
        "tenant_id": tenant_id or "test-tenant",
        "system_id": system_id,
        "user_id": user_id,
        "roles": roles or [],
        "groups": groups or [],
        "tags": tags or ["test"],
        "source_uri": source_uri,
        **kwargs,
    }


def create_test_knowledge_chunk(
    chunk_id: str | None = None, text: str | None = None, **kwargs
) -> dict[str, Any]:
    """
    Factory for test knowledge chunks (with ID and timestamps).
    """
    chunk_data = create_test_knowledge_chunk_create(text=text, **kwargs)
    return {
        "id": chunk_id or str(uuid.uuid4()),
        "created_at": datetime.now(tz=UTC),
        "updated_at": datetime.now(tz=UTC),
        **chunk_data,
    }


# ============================================================================
# Connector Factories
# ============================================================================


def create_test_connector_create(
    name: str | None = None,
    base_url: str | None = None,
    auth_type: str = "API_KEY",
    tenant_id: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """
    Factory for test connector creation objects.
    """
    return {
        "tenant_id": tenant_id or "test-tenant",
        "name": name or f"Test Connector {uuid.uuid4()}",
        "description": kwargs.pop("description", "Test connector"),
        "base_url": base_url or "https://api.example.com",
        "auth_type": auth_type,
        "auth_config": kwargs.pop("auth_config", {"api_key": "test-key"}),
        "credential_strategy": kwargs.pop("credential_strategy", "SYSTEM"),
        **kwargs,
    }


def create_test_connector(
    connector_id: str | None = None, name: str | None = None, **kwargs
) -> dict[str, Any]:
    """
    Factory for test connectors (with ID and timestamps).
    """
    connector_data = create_test_connector_create(name=name, **kwargs)
    return {
        "id": connector_id or str(uuid.uuid4()),
        "is_active": True,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        **connector_data,
    }


def create_test_endpoint_descriptor(
    endpoint_id: str | None = None,
    connector_id: str | None = None,
    method: str = "GET",
    path: str = "/api/resource",
    **kwargs,
) -> dict[str, Any]:
    """
    Factory for test endpoint descriptors.
    """
    return {
        "id": endpoint_id or str(uuid.uuid4()),
        "connector_id": connector_id or str(uuid.uuid4()),
        "method": method.upper(),
        "path": path,
        "operation_id": kwargs.pop("operation_id", None),
        "summary": kwargs.pop("summary", f"{method} {path}"),
        "description": kwargs.pop("description", "Test endpoint"),
        "tags": kwargs.pop("tags", ["test"]),
        "required_params": kwargs.pop("required_params", []),
        "path_params_schema": kwargs.pop("path_params_schema", {}),
        "query_params_schema": kwargs.pop("query_params_schema", {}),
        "body_schema": kwargs.pop("body_schema", {}),
        "response_schema": kwargs.pop("response_schema", {}),
        "created_at": datetime.now(tz=UTC),
        **kwargs,
    }


# ============================================================================
# Workflow Factories
# ============================================================================


def create_test_workflow(
    workflow_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    goal: str | None = None,
    status: str = "PLANNING",
    **kwargs,
) -> dict[str, Any]:
    """
    Factory for test workflows.
    """
    return {
        "id": workflow_id or str(uuid.uuid4()),
        "tenant_id": tenant_id or "test-tenant",
        "user_id": user_id or "test-user",
        "status": status,
        "goal": goal or "Test workflow goal",
        "plan_json": kwargs.pop("plan_json", None),
        "current_step_index": kwargs.pop("current_step_index", 0),
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        **kwargs,
    }


def create_test_workflow_step(
    step_id: str | None = None,
    workflow_id: str | None = None,
    index: int = 0,
    tool_name: str = "search_knowledge",
    status: str = "PENDING",
    **kwargs,
) -> dict[str, Any]:
    """
    Factory for test workflow steps.
    """
    return {
        "id": step_id or str(uuid.uuid4()),
        "workflow_id": workflow_id or str(uuid.uuid4()),
        "index": index,
        "tool_name": tool_name,
        "input_json": kwargs.pop("input_json", {}),
        "output_json": kwargs.pop("output_json", None),
        "status": status,
        "error_message": kwargs.pop("error_message", None),
        "started_at": kwargs.pop("started_at", None),
        "finished_at": kwargs.pop("finished_at", None),
        **kwargs,
    }


# ============================================================================
# Plan Factories
# ============================================================================


def create_test_plan(
    goal: str | None = None, steps: list[dict] | None = None, **kwargs
) -> dict[str, Any]:
    """
    Factory for test plans.
    """
    if steps is None:
        steps = [
            {
                "id": "step-1",
                "description": "Search knowledge",
                "tool_name": "search_knowledge",
                "tool_args": {"query": "test query"},
                "depends_on": [],
            },
            {
                "id": "step-2",
                "description": "Call API",
                "tool_name": "call_endpoint",
                "tool_args": {},
                "depends_on": ["step-1"],
            },
        ]

    return {
        "goal": goal or "Test plan goal",
        "steps": steps,
        "notes": kwargs.pop("notes", None),
        **kwargs,
    }


# ============================================================================
# Helper Functions
# ============================================================================


def generate_test_id(prefix: str = "") -> str:
    """Generate a test ID with optional prefix"""
    return f"{prefix}{uuid.uuid4()}" if prefix else str(uuid.uuid4())


def generate_test_email(username: str | None = None) -> str:
    """Generate a test email address"""
    user = username or f"test-{uuid.uuid4()}"
    return f"{user}@example.com"


def generate_test_url(path: str = "") -> str:
    """Generate a test URL"""
    return f"https://api.example.com{path}"


def create_test_embedding(dimension: int = 1536) -> list[float]:
    """Create a test embedding vector"""
    import random

    random.seed(12345)  # Deterministic
    return [random.random() for _ in range(dimension)]
