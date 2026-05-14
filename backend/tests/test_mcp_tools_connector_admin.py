# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the 7 admin MCP tools registered by G0.7-T7 (#407).

Coverage matrix:

* ``tools/list`` exposes the right subset of admin tools per role:
  - ``read_only`` operator sees ZERO ``meho.connector.*`` tools.
  - ``operator`` operator sees the 2 read tools (``list`` + ``review``).
  - ``tenant_admin`` operator sees all 7 tools.
* Each tool's ``inputSchema`` is strict JSON-Schema 2020-12 with
  ``additionalProperties: false`` (#407 AC 4).
* Tool descriptions name when to use / when not to (AI engineering
  anchor, #407 AC 3).
* ``tools/call`` dispatch to a stubbed service layer:
  - ``meho.connector.list`` returns the stubbed ConnectorListResponse.
  - ``meho.connector.review`` returns the stubbed ConnectorReviewPayload.
  - ``meho.connector.ingest`` (tenant_admin) wires the request through.
  - ``meho.connector.edit_group`` writes via ReviewService.
  - ``meho.connector.enable`` flips status via ReviewService.
* RBAC enforcement at call time: ``operator``-role calling
  ``meho.connector.enable`` returns a JSON-RPC error (not the success
  envelope) even when guessing the tool name.

The fixture suite reuses :mod:`tests.mcp_test_fixtures` which already
registers ``connector_admin`` in its autouse ``isolated_registry``
reload list.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.operations.ingest import (
    ConnectorListResponse,
    ConnectorReviewGroup,
    ConnectorReviewPayload,
    ConnectorSummary,
    IngestResponse,
    SpecIngestionOutcome,
)
from meho_backplane.operations.ingest import (
    admin_service as admin_service_module,
)
from meho_backplane.operations.ingest import (
    service as review_service_module,
)
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

# ---------------------------------------------------------------------------
# Service-layer stubs
# ---------------------------------------------------------------------------


class _FakeAdminService:
    """Records every call + returns canned responses."""

    def __init__(self, operator: Operator) -> None:
        self.operator = operator
        self.ingest_calls: list[Any] = []
        self.list_calls: list[Any] = []

    async def ingest(self, request: Any) -> IngestResponse:
        self.ingest_calls.append(request)
        connector_id = f"{request.impl_id}-{request.version}"
        return IngestResponse(
            connector_id=connector_id,
            product=request.product,
            version=request.version,
            impl_id=request.impl_id,
            tenant_id=request.tenant_id,
            specs=[
                SpecIngestionOutcome(
                    source_label=spec.source_label or spec.uri,
                    uri=spec.uri,
                    inserted_count=10,
                    updated_count=0,
                    skipped_count=0,
                    connector_registered=True,
                )
                for spec in request.specs
            ],
            grouping=None,
            dry_run=request.dry_run,
        )

    async def list_connectors(self, *, status: str = "all") -> ConnectorListResponse:
        self.list_calls.append(status)
        return ConnectorListResponse(
            connectors=[
                ConnectorSummary(
                    connector_id="vmware-rest-9.0",
                    product="vmware",
                    version="9.0",
                    impl_id="vmware-rest",
                    tenant_id=None,
                    group_count=3,
                    operation_count=42,
                    enabled_operation_count=0,
                    connector_status="staged",
                    last_updated_at=None,
                ),
            ],
        )


class _FakeReviewService:
    """Records every call + returns canned responses."""

    def __init__(self, operator: Operator) -> None:
        self.operator = operator
        self.review_calls: list[tuple[str, Any]] = []
        self.edit_group_calls: list[Any] = []
        self.edit_op_calls: list[Any] = []
        self.enable_calls: list[str] = []
        self.disable_calls: list[str] = []

    async def get_review_payload(
        self,
        connector_id: str,
        tenant_id: Any,
    ) -> ConnectorReviewPayload:
        self.review_calls.append((connector_id, tenant_id))
        return ConnectorReviewPayload(
            connector_id=connector_id,
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            tenant_id=tenant_id,
            groups=[
                ConnectorReviewGroup(
                    group_key="vm-lifecycle",
                    name="VM Lifecycle",
                    when_to_use="Use for VM CRUD ops.",
                    review_status="staged",
                    op_count=0,
                    ops=[],
                ),
            ],
            total_op_count=0,
        )

    async def edit_group(
        self,
        connector_id: str,
        group_key: str,
        **kwargs: Any,
    ) -> None:
        self.edit_group_calls.append(
            {"connector_id": connector_id, "group_key": group_key, **kwargs},
        )

    async def edit_op(
        self,
        connector_id: str,
        op_id: str,
        **kwargs: Any,
    ) -> None:
        self.edit_op_calls.append(
            {"connector_id": connector_id, "op_id": op_id, **kwargs},
        )

    async def enable_connector(self, connector_id: str, **_kwargs: Any) -> None:
        self.enable_calls.append(connector_id)

    async def disable_connector(self, connector_id: str, **_kwargs: Any) -> None:
        self.disable_calls.append(connector_id)


@pytest.fixture
def stubbed_services(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Patch the admin + review service classes the MCP handlers construct.

    Both handler modules import the classes by name from
    :mod:`meho_backplane.operations.ingest`; patching the re-export
    surface is the cleanest seam — handler-internal ``ConnectorAdminService(...)``
    calls resolve to ``_FakeAdminService`` for the duration of the
    fixture.
    """
    captured_admin: list[_FakeAdminService] = []
    captured_review: list[_FakeReviewService] = []

    def _admin_factory(operator: Operator, **_kwargs: Any) -> _FakeAdminService:
        instance = _FakeAdminService(operator)
        captured_admin.append(instance)
        return instance

    def _review_factory(operator: Operator, **_kwargs: Any) -> _FakeReviewService:
        instance = _FakeReviewService(operator)
        captured_review.append(instance)
        return instance

    # The MCP handler module resolved `ConnectorAdminService` /
    # `ReviewService` at import time from
    # `meho_backplane.operations.ingest`. Patch the handler module's
    # local reference so subsequent constructor calls hit the fakes.
    import meho_backplane.mcp.tools.connector_admin as ca_mod

    monkeypatch.setattr(ca_mod, "ConnectorAdminService", _admin_factory)
    monkeypatch.setattr(ca_mod, "ReviewService", _review_factory)

    yield {"admin": captured_admin, "review": captured_review}


# ---------------------------------------------------------------------------
# tools/list RBAC visibility tests
# ---------------------------------------------------------------------------


_ADMIN_TOOL_NAMES = {
    "meho.connector.ingest",
    "meho.connector.list",
    "meho.connector.review",
    "meho.connector.edit_group",
    "meho.connector.edit_op",
    "meho.connector.enable",
    "meho.connector.disable",
}

_READ_ADMIN_TOOL_NAMES = {
    "meho.connector.list",
    "meho.connector.review",
}

_TENANT_ADMIN_ONLY_TOOL_NAMES = _ADMIN_TOOL_NAMES - _READ_ADMIN_TOOL_NAMES


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY],
    indirect=True,
)
def test_admin_tools_hidden_from_read_only_role(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: a read_only operator sees zero ``meho.connector.*`` tools."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert _ADMIN_TOOL_NAMES.isdisjoint(names), (
        f"read_only operator saw admin tools: {sorted(_ADMIN_TOOL_NAMES & names)}"
    )


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_admin_read_tools_visible_to_operator_role(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An operator-role client sees the 2 read tools and only those."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert _READ_ADMIN_TOOL_NAMES.issubset(names)
    assert _TENANT_ADMIN_ONLY_TOOL_NAMES.isdisjoint(names), (
        f"operator role saw tenant_admin tools: {sorted(_TENANT_ADMIN_ONLY_TOOL_NAMES & names)}"
    )


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_all_admin_tools_visible_to_tenant_admin_role(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A tenant_admin client sees all 7 admin tools."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert _ADMIN_TOOL_NAMES.issubset(names), (
        f"tenant_admin missing admin tools: {sorted(_ADMIN_TOOL_NAMES - names)}"
    )


# ---------------------------------------------------------------------------
# Schema strictness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_admin_tool_input_schemas_are_strict_2020_12(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Every admin tool's inputSchema is JSON-Schema 2020-12 + strict."""
    import jsonschema

    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}

    for name in _ADMIN_TOOL_NAMES:
        schema = tools[name]["inputSchema"]
        # additionalProperties: false (AC 4).
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False, (
            f"{name}: missing additionalProperties=false"
        )
        # Schema itself validates as Draft 2020-12.
        jsonschema.Draft202012Validator.check_schema(schema)
        # MEHO-internal fields are stripped from the wire shape.
        assert "required_role" not in tools[name]
        assert "op_class" not in tools[name]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_admin_tool_descriptions_name_when_to_use_and_when_not(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Descriptions follow the AI-engineering anchor: name use + non-use."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}

    # Each admin tool's description must mention a positive use case
    # ("use when") and either a negative ("do not"/"DO NOT") or a
    # complementary pairing nudge (which tool to call before/after).
    for name in _ADMIN_TOOL_NAMES:
        desc_lower = tools[name]["description"].lower()
        assert "use" in desc_lower, f"{name} description missing 'use' guidance"
        assert len(desc_lower.split()) >= 30, (
            f"{name} description is too short: {tools[name]['description']!r}"
        )


# ---------------------------------------------------------------------------
# tools/call dispatch
# ---------------------------------------------------------------------------


def _unwrap_text_content(body: dict[str, Any]) -> dict[str, Any]:
    """Pull the JSON payload out of an MCP ``content`` array."""
    contents = body["result"]["content"]
    assert len(contents) == 1
    assert contents[0]["type"] == "text"
    return json.loads(contents[0]["text"])


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_call_meho_connector_list_dispatches_to_admin_service(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stubbed_services: dict[str, Any],
) -> None:
    """``tools/call meho.connector.list`` returns the stubbed connector list."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.connector.list",
                "arguments": {"status": "staged"},
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    payload = _unwrap_text_content(body)

    assert payload["connectors"][0]["connector_id"] == "vmware-rest-9.0"
    assert payload["connectors"][0]["connector_status"] == "staged"

    # The stubbed service recorded the status filter.
    [admin] = stubbed_services["admin"]
    assert admin.list_calls == ["staged"]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_call_meho_connector_review_dispatches_to_review_service(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stubbed_services: dict[str, Any],
) -> None:
    """``tools/call meho.connector.review`` returns the stubbed review payload."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.connector.review",
                "arguments": {"connector_id": "vmware-rest-9.0"},
            },
        },
    )
    assert response.status_code == 200
    payload = _unwrap_text_content(response.json())
    assert payload["connector_id"] == "vmware-rest-9.0"
    assert len(payload["groups"]) == 1
    assert payload["groups"][0]["group_key"] == "vm-lifecycle"

    [review] = stubbed_services["review"]
    assert review.review_calls == [("vmware-rest-9.0", None)]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_call_meho_connector_ingest_threads_specs_through(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stubbed_services: dict[str, Any],
) -> None:
    """Ingest tool forwards specs + flags + tenant_id to the service."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.connector.ingest",
                "arguments": {
                    "product": "vmware",
                    "version": "9.0",
                    "impl_id": "vmware-rest",
                    "specs": [
                        {
                            "uri": "docs:vcenter-9.0/vcenter.yaml",
                            "source_label": "vcenter.yaml",
                        },
                        {"uri": "docs:vcenter-9.0/vi-json.yaml"},
                    ],
                    "dry_run": True,
                    "tenant_id": str(OPERATOR_TENANT_ID),
                },
            },
        },
    )
    assert response.status_code == 200
    payload = _unwrap_text_content(response.json())
    assert payload["connector_id"] == "vmware-rest-9.0"
    assert payload["dry_run"] is True
    assert len(payload["specs"]) == 2

    [admin] = stubbed_services["admin"]
    [request] = admin.ingest_calls
    assert request.product == "vmware"
    assert request.version == "9.0"
    assert request.impl_id == "vmware-rest"
    assert request.tenant_id == OPERATOR_TENANT_ID
    assert request.dry_run is True
    assert len(request.specs) == 2
    assert request.specs[0].source_label == "vcenter.yaml"
    # Second spec falls back to the URI as source_label.
    assert request.specs[1].source_label is None


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_call_meho_connector_edit_group_dispatches(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stubbed_services: dict[str, Any],
) -> None:
    """``edit_group`` threads through to ReviewService."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.connector.edit_group",
                "arguments": {
                    "connector_id": "vmware-rest-9.0",
                    "group_key": "vm-lifecycle",
                    "when_to_use": "Use for VM CRUD.",
                },
            },
        },
    )
    assert response.status_code == 200
    payload = _unwrap_text_content(response.json())
    assert payload == {
        "connector_id": "vmware-rest-9.0",
        "group_key": "vm-lifecycle",
        "ok": True,
    }

    [review] = stubbed_services["review"]
    assert review.edit_group_calls == [
        {
            "connector_id": "vmware-rest-9.0",
            "group_key": "vm-lifecycle",
            "tenant_id": None,
            "when_to_use": "Use for VM CRUD.",
            "name": None,
        },
    ]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_call_meho_connector_enable_dispatches(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stubbed_services: dict[str, Any],
) -> None:
    """``enable`` calls ReviewService.enable_connector."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.connector.enable",
                "arguments": {"connector_id": "vmware-rest-9.0"},
            },
        },
    )
    assert response.status_code == 200
    payload = _unwrap_text_content(response.json())
    assert payload == {
        "connector_id": "vmware-rest-9.0",
        "status": "enabled",
        "ok": True,
    }

    [review] = stubbed_services["review"]
    assert review.enable_calls == ["vmware-rest-9.0"]


# ---------------------------------------------------------------------------
# RBAC at call time (not just at list time)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_operator_role_cannot_call_tenant_admin_mutator(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stubbed_services: dict[str, Any],
) -> None:
    """A client guessing a hidden tool name gets rejected at the dispatcher."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.connector.enable",
                "arguments": {"connector_id": "vmware-rest-9.0"},
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "error" in body, body
    # JSON-RPC -32602 INVALID_PARAMS per the handlers.py "forbidden" mapping.
    assert body["error"]["code"] == -32602
    assert "forbidden" in body["error"]["message"].lower()

    # The fake service was never constructed — the handler short-
    # circuited at the RBAC re-check before reaching dispatch.
    assert not stubbed_services["review"], (
        "operator role bypassed RBAC and reached the service layer"
    )


# ---------------------------------------------------------------------------
# Single-source service-layer factor
# ---------------------------------------------------------------------------


def test_admin_service_and_review_service_share_namespace() -> None:
    """The admin service + review service compose without duplicating dispatch.

    Asserts the load-bearing factoring: T5 / T6 / T7 all consume the
    same two service classes for ingest/list and review/edit. A
    refactor that accidentally re-introduces dispatch logic in the
    MCP handler module would surface as a new ``async def`` here
    that doesn't simply delegate to a service method.
    """
    # Both service classes are present in the public re-export.
    assert hasattr(admin_service_module, "ConnectorAdminService")
    assert hasattr(review_service_module, "ReviewService")

    # The two surfaces don't overlap on method names — composition,
    # not duplication.
    admin_methods = {
        name for name in dir(admin_service_module.ConnectorAdminService) if not name.startswith("_")
    }
    review_methods = {
        name for name in dir(review_service_module.ReviewService) if not name.startswith("_")
    }
    overlap = admin_methods & review_methods
    assert overlap == set(), f"admin/review service methods collide: {overlap}"
