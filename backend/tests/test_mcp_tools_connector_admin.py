# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the connector review / state-machine admin MCP tools (G0.7-T7 #407).

Covers the six ``meho.connector.*`` review / edit / enable / disable
tools. The ingest-pipeline tools (``meho.connector.ingest`` +
``meho.connector.ingest_status``) split out into their own handler
module (#1531) and are tested in
``test_mcp_tools_connector_ingest.py``.

Coverage matrix:

* ``tools/list`` exposes the right subset of admin tools per role:
  - ``read_only`` operator sees ZERO of these ``meho.connector.*`` tools.
  - ``operator`` operator sees the 2 read tools (``list`` + ``review``).
  - ``tenant_admin`` operator sees all six review / edit tools.
* Each tool's ``inputSchema`` is strict JSON-Schema 2020-12 with
  ``additionalProperties: false`` (#407 AC 4).
* Tool descriptions name when to use / when not to (AI engineering
  anchor, #407 AC 3).
* ``tools/call`` dispatch to a stubbed canonical service layer:
  - ``meho.connector.list`` returns the stubbed ConnectorListItem rows.
  - ``meho.connector.review`` returns the stubbed ConnectorReviewPayload.
  - ``meho.connector.edit_group`` writes via ReviewService and only
    forwards explicitly named fields (PATCH-semantic intent).
  - ``meho.connector.enable`` flips status via ReviewService.
* RBAC enforcement at call time: ``operator``-role calling
  ``meho.connector.enable`` returns a JSON-RPC error (not the success
  envelope) even when guessing the tool name.

Why we stub the canonical service layer rather than spin up a real
:class:`ReviewService`: the production service touches the DB; a unit
test that drives it is the canary on T8 (#408). These tests are about
the MCP handler shim — the contract under test is "the handler converts
MCP arguments into the canonical service-layer call shape correctly".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.operations.ingest import (
    ConnectorListItem,
    ConnectorReviewGroup,
    ConnectorReviewPayload,
)
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

# ---------------------------------------------------------------------------
# Canonical service-layer stubs (T6's #488 surface)
# ---------------------------------------------------------------------------


class _FakeReviewService:
    """Records every call + returns canned responses."""

    def __init__(self, operator: Operator) -> None:
        self.operator = operator
        self.review_calls: list[tuple[str, Any]] = []
        self.edit_group_calls: list[dict[str, Any]] = []
        self.edit_op_calls: list[dict[str, Any]] = []
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
    """Patch the canonical service classes the MCP handlers construct.

    The handler module resolved :class:`ReviewService` at import time
    from :mod:`meho_backplane.operations.ingest`, and
    :func:`list_ingested_connectors` similarly. Patching those names
    on the handler module's local reference is the cleanest seam —
    subsequent constructor / function calls inside the handlers
    resolve to the fakes for the duration of the fixture.
    """
    captured_review: list[_FakeReviewService] = []
    captured_list_calls: list[dict[str, Any]] = []

    def _review_factory(operator: Operator, **_kwargs: Any) -> _FakeReviewService:
        instance = _FakeReviewService(operator)
        captured_review.append(instance)
        return instance

    async def _fake_list_ingested_connectors(
        **kwargs: Any,
    ) -> list[ConnectorListItem]:
        captured_list_calls.append(kwargs)
        return [
            ConnectorListItem(
                connector_id="vmware-rest-9.0",
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                tenant_id=None,
                group_count=3,
                staged_group_count=3,
                enabled_group_count=0,
                disabled_group_count=0,
                operation_count=42,
            ),
        ]

    import meho_backplane.mcp.tools.connector_admin as ca_mod

    monkeypatch.setattr(ca_mod, "ReviewService", _review_factory)
    monkeypatch.setattr(ca_mod, "list_ingested_connectors", _fake_list_ingested_connectors)

    yield {
        "review": captured_review,
        "list_calls": captured_list_calls,
    }


# ---------------------------------------------------------------------------
# tools/list RBAC visibility tests
# ---------------------------------------------------------------------------


# The review / edit / state-machine tools this module owns. The
# ingest-pipeline tools (``meho.connector.ingest`` +
# ``meho.connector.ingest_status``) moved to
# ``test_mcp_tools_connector_ingest.py`` alongside their handler module
# (#1531); they're asserted there, not here.
_ADMIN_TOOL_NAMES = {
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
    negative_tokens = ("do not", "don't", "do-not", "avoid")
    pairing_tokens = ("before", "after", "instead", "pair with")
    for name in _ADMIN_TOOL_NAMES:
        desc = tools[name]["description"]
        desc_lower = desc.lower()
        assert "use" in desc_lower, f"{name} description missing 'use' guidance"
        assert len(desc_lower.split()) >= 30, f"{name} description is too short: {desc!r}"
        has_negative = any(token in desc_lower for token in negative_tokens)
        has_pairing = any(token in desc_lower for token in pairing_tokens)
        assert has_negative or has_pairing, (
            f"{name} description missing both negative-guidance and "
            f"pairing-nudge tokens; description was: {desc!r}"
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
def test_call_meho_connector_list_dispatches_to_list_ingested_connectors(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stubbed_services: dict[str, Any],
) -> None:
    """``tools/call meho.connector.list`` returns the stubbed connector list."""
    client, op = client_with_operator
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
    assert payload["connectors"][0]["staged_group_count"] == 3
    assert payload["connectors"][0]["operation_count"] == 42

    # The stubbed query helper recorded the status filter + operator.
    [call_kwargs] = stubbed_services["list_calls"]
    assert call_kwargs["status"] == "staged"
    assert call_kwargs["operator"] is op


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
def test_call_meho_connector_edit_group_dispatches(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stubbed_services: dict[str, Any],
) -> None:
    """``edit_group`` threads through to ReviewService with explicit field only."""
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
    # Key-presence semantics: the omitted ``name`` field is NOT in
    # the forwarded kwargs (the previous shape would have forwarded
    # name=None and conflated "omitted" with "explicit null").
    assert review.edit_group_calls == [
        {
            "connector_id": "vmware-rest-9.0",
            "group_key": "vm-lifecycle",
            "tenant_id": None,
            "when_to_use": "Use for VM CRUD.",
        },
    ]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_call_meho_connector_edit_group_distinguishes_omitted_from_null(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stubbed_services: dict[str, Any],
) -> None:
    """PATCH semantics: omitted ``name`` is NOT forwarded; explicit ``null`` IS.

    Two calls with otherwise identical bodies — one omits ``name``,
    the other passes ``name: null``. Only the second forwards ``name``
    to :meth:`ReviewService.edit_group`. This is the load-bearing
    behaviour the M1 CodeRabbit finding flagged: ``arguments.get(...)``
    would have conflated both into ``name=None``, breaking the
    operator's ability to express "leave this field alone".
    """
    client, _op = client_with_operator

    # First call: name field omitted entirely.
    post_mcp(
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
    # Second call: name=None explicit.
    post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "meho.connector.edit_group",
                "arguments": {
                    "connector_id": "vmware-rest-9.0",
                    "group_key": "vm-lifecycle",
                    "when_to_use": "Use for VM CRUD.",
                    "name": None,
                },
            },
        },
    )

    # Two MCP calls → two ReviewService instances (the handler builds
    # one per request). Concatenate their edit_group_calls in order.
    all_calls = [call for review in stubbed_services["review"] for call in review.edit_group_calls]
    omitted_call, explicit_null_call = all_calls
    assert "name" not in omitted_call, (
        f"omitted ``name`` leaked through to service layer: {omitted_call!r}"
    )
    assert "name" in explicit_null_call, (
        f"explicit ``name=None`` lost on the way to service layer: {explicit_null_call!r}"
    )
    assert explicit_null_call["name"] is None


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_call_meho_connector_edit_op_distinguishes_omitted_from_null(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stubbed_services: dict[str, Any],
) -> None:
    """Same PATCH-semantic discipline as edit_group, on the 4-field ``edit_op``."""
    client, _op = client_with_operator
    post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.connector.edit_op",
                "arguments": {
                    "connector_id": "vmware-rest-9.0",
                    "op_id": "GET:/api/vcenter/cluster",
                    "safety_level": "dangerous",
                },
            },
        },
    )
    post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "meho.connector.edit_op",
                "arguments": {
                    "connector_id": "vmware-rest-9.0",
                    "op_id": "GET:/api/vcenter/cluster",
                    "safety_level": "dangerous",
                    "custom_description": None,
                    "requires_approval": None,
                    "is_enabled": None,
                },
            },
        },
    )

    all_calls = [call for review in stubbed_services["review"] for call in review.edit_op_calls]
    omitted_call, explicit_null_call = all_calls
    assert "custom_description" not in omitted_call
    assert "requires_approval" not in omitted_call
    assert "is_enabled" not in omitted_call
    assert "custom_description" in explicit_null_call
    assert "requires_approval" in explicit_null_call
    assert "is_enabled" in explicit_null_call
    assert explicit_null_call["custom_description"] is None
    assert explicit_null_call["requires_approval"] is None
    assert explicit_null_call["is_enabled"] is None


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
