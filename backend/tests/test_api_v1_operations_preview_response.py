# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The typed response of ``POST /api/v1/operations/preview`` (#4923).

Two contracts:

* The OpenAPI document names ``PreviewOperationResponse`` for the route and
  the schema lists the risk-level and policy fields, with the closed set of
  ``policy_verdict`` values, so a generated client can discover them. The
  committed ``cli/api/openapi.json`` snapshot carries the same fields.
* The wire shape of a preview is unchanged by the model: only the keys the
  envelope carries are sent (no ``null`` padding for absent fields) and the
  extra keys of a rate-limit or target-resolution envelope pass through.
  Key order is not part of the contract; the CLI decodes into a struct.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1 import operations as operations_api
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.middleware import verify_jwt_and_bind

_GOVERNANCE_FIELDS = ("safety_level", "requires_approval", "policy_verdict", "policy_reason")
_VERDICTS = ["auto-execute", "needs-approval", "deny"]
_SNAPSHOT = Path(__file__).resolve().parents[2] / "cli" / "api" / "openapi.json"


def _router_app() -> FastAPI:
    app = FastAPI()
    app.include_router(operations_api.router)
    return app


def _resolve_ref(document: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Follow a ``$ref`` (directly or inside an ``anyOf``) to its component."""
    candidates = [node, *node.get("anyOf", [])]
    for candidate in candidates:
        ref = candidate.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            return dict(document["components"]["schemas"][name])
    return node


def _assert_preview_schema(document: dict[str, Any]) -> None:
    route = document["paths"]["/api/v1/operations/preview"]["post"]
    schema = route["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/PreviewOperationResponse"}
    component = document["components"]["schemas"]["PreviewOperationResponse"]
    properties = component["properties"]
    for field in _GOVERNANCE_FIELDS:
        assert field in properties, field
    assert component["required"] == ["status"]
    verdict = _resolve_ref(document, properties["policy_verdict"])
    assert verdict["enum"] == _VERDICTS
    assert properties["requires_approval"].get("type") == "boolean" or any(
        branch.get("type") == "boolean" for branch in properties["requires_approval"]["anyOf"]
    )


def test_openapi_names_the_preview_response_model_with_the_policy_fields() -> None:
    _assert_preview_schema(_router_app().openapi())


def test_committed_openapi_snapshot_carries_the_policy_fields() -> None:
    document = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    _assert_preview_schema(document)


@pytest.fixture
def preview_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AsyncMock]:
    """The operations router with a fixed operator and a stubbed preview funnel."""
    funnel = AsyncMock()
    monkeypatch.setattr(operations_api, "preview_operation", funnel)
    app = _router_app()

    async def _operator() -> Operator:
        return Operator(
            sub="op-rest",
            name="Rest Caller",
            email=None,
            raw_jwt="<test-jwt>",
            tenant_id=uuid.uuid4(),
            tenant_role=TenantRole.OPERATOR,
        )

    app.dependency_overrides[verify_jwt_and_bind] = _operator
    return TestClient(app), funnel


_BODY = {"connector_id": "gh-rest-3", "op_id": "POST:/repos/{owner}/{repo}/issues", "params": {}}


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param(
            {
                "status": "ok",
                "op_id": "POST:/repos/{owner}/{repo}/issues",
                "connector_id": "gh-rest-3",
                "source_kind": "ingested",
                "method": "POST",
                "resolved_path": "/repos/evoila/meho/issues",
                "query": None,
                "redacted_body": {"title": "diagnose me"},
                "preview_hash": "a" * 64,
                "safety_level": "caution",
                "requires_approval": False,
                "policy_verdict": "auto-execute",
                "policy_reason": None,
            },
            id="ingested-ok",
        ),
        pytest.param(
            {
                "status": "unavailable",
                "op_id": "vault.kv.put",
                "connector_id": "vault-kv-2",
                "source_kind": "typed",
                "error": "preview_unavailable: ...",
                "extras": {"error_code": "preview_unavailable", "reason": "not_ingested"},
                "safety_level": "dangerous",
                "requires_approval": True,
                "policy_verdict": "needs-approval",
                "policy_reason": "requires_approval",
            },
            id="typed-unavailable",
        ),
        pytest.param(
            {
                "status": "error",
                "op_id": "GET:/nowhere",
                "connector_id": "gh-rest-3",
                "error": "unknown_op: ...",
                "extras": {"error_code": "unknown_op"},
            },
            id="unknown-op-without-policy-fields",
        ),
        pytest.param(
            {
                "status": "error",
                "op_id": "GET:/x",
                "result": None,
                "error": "target_required: ...",
                "duration_ms": 0.0,
                "handle": None,
                "audit_id": None,
                "delivery": None,
                "extras": {"error_code": "target_required"},
            },
            id="target-required-operation-result",
        ),
        pytest.param(
            {
                "status": "rate_limited",
                "error_code": "rate_limited",
                "kind": "rate",
                "limit": 60,
                "retry_after_seconds": 7,
                "error": "rate_limited: 60 dispatches per minute for this principal",
            },
            id="rate-limited",
        ),
    ],
)
def test_preview_route_sends_the_envelope_unchanged(
    preview_client: tuple[TestClient, AsyncMock], envelope: dict[str, Any]
) -> None:
    client, funnel = preview_client
    funnel.return_value = dict(envelope)

    response = client.post("/api/v1/operations/preview", json=_BODY)

    assert response.status_code == 200, response.text
    assert response.json() == envelope
