# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Edit the classification one at a time or by pattern (``POLICY_CLASSIFICATION``).

* :mod:`meho_backplane.policy.bulk`: the dry run writes nothing and lists
  every matched operation; a request makes a waiting rights change, and is
  refused before any request for no match, a conflict or the switch off.
* REST: the classification routes, and ``category`` / ``labels`` on the
  operation, group and connector PATCH routes.
* The admin MCP tools ``meho_connector_edit_op`` / ``_edit_group``: the
  same request; the fields are hidden while the switch is off.
* The dry-run operation ``policy.classify_preview``.
* The override report shows the values the classification puts in force.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.connectors_ingest import router as connectors_ingest_router
from meho_backplane.api.v1.operation_overrides import load_override_rows
from meho_backplane.api.v1.operation_overrides import router as overrides_router
from meho_backplane.api.v1.policy_classification import connector_router, router
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy.classify import (
    POLICY_CLASSIFY_OP_ID,
    register_policy_classify_operation,
)
from meho_backplane.connectors.policy.preview import (
    POLICY_CLASSIFY_PREVIEW_OP_ID,
    register_policy_classify_preview_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    EndpointDescriptor,
    OperationPolicyOverride,
    TenantPolicy,
)
from meho_backplane.mcp.registry import get_tool, tool_for_settings
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations.grant_approval import is_rights_change_op
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.role_floors import role_floor_for
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.bulk import (
    ClassificationEditError,
    ClassificationOffError,
    preview_classification_edit,
    request_classification_edit,
)
from meho_backplane.policy.store import ClassificationEdit
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000603a")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000603b")
_FIX = {"product": "clsedit", "version": "1.x", "impl_id": "clsedit"}
_CONNECTOR = "clsedit-1.x"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    clear_jwks_cache()
    get_settings.cache_clear()


def _switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "false")
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _register(
    embedding: AsyncMock,
    op_id: str,
    *,
    group_key: str = "vm",
    requires_approval: bool = False,
) -> None:
    await register_typed_operation(
        **_FIX,
        op_id=op_id,
        handler=_handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema={"type": "object", "properties": {}},
        group_key=group_key,
        when_to_use="Fixture group.",
        requires_approval=requires_approval,
        embedding_service=embedding,
    )


@pytest.fixture
async def ops(embedding: AsyncMock) -> None:
    """Three fixture operations in two groups, plus the two policy operations."""
    await _register(embedding, "clsedit.vm.start")
    await _register(embedding, "clsedit.vm.stop")
    await _register(embedding, "clsedit.net.list", group_key="net")
    await register_policy_classify_operation(embedding_service=embedding)
    await register_policy_classify_preview_operation(embedding_service=embedding)


def _admin(sub: str = "admin-a", tenant_id: uuid.UUID = _TENANT) -> Operator:
    return Operator(
        sub=sub,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        raw_jwt="header.payload.signature",
    )


def _edit(match: dict[str, str], **values: Any) -> ClassificationEdit:
    return ClassificationEdit(
        match=match,
        category=values.get("category"),
        labels=values.get("labels"),
        always_wait=values.get("always_wait"),
    )


async def _stored_documents() -> list[TenantPolicy]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(TenantPolicy))).scalars())


async def _approval_requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


# ---------------------------------------------------------------------------
# The dry run and the request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_dry_run_lists_every_match_before_and_after_and_writes_nothing() -> None:
    preview = await preview_classification_edit(
        _admin(), _edit({"op": "clsedit.vm.*"}, category="compute", labels={"team": "ops"})
    )
    body = preview.to_dict()
    assert body["matched_operations"] == 2
    assert body["changed_operations"] == 2
    assert body["conflict"] is None
    assert body["rule_ref"] == "tenant:classifications[0]"
    assert [row["op_id"] for row in body["operations"]] == ["clsedit.vm.start", "clsedit.vm.stop"]
    first = body["operations"][0]
    assert first["group_key"] == "vm"
    assert first["before"]["category"] == "other"
    assert first["after"]["category"] == "compute"
    assert first["after"]["labels"] == {"team": "ops"}
    assert first["after"]["rule_ref"] == "tenant:classifications[0]"
    assert await _stored_documents() == []
    assert await _approval_requests() == []


@pytest.mark.asyncio
async def test_dry_run_never_cuts_the_list(embedding: AsyncMock) -> None:
    for index in range(55):
        await _register(embedding, f"clsedit.bulk.op{index:02d}", group_key="bulk")
    preview = await preview_classification_edit(
        _admin(), _edit({"group": "bulk"}, always_wait=True)
    )
    assert len(preview.operations) == 55
    assert preview.to_dict()["matched_operations"] == 55


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_dry_run_marks_an_operation_that_does_not_change() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TenantPolicy(
                tenant_id=_TENANT,
                document={
                    "classifications": [
                        {"match": {"op": "clsedit.vm.start"}, "category": "compute"}
                    ]
                },
                set_by_sub="fixture",
            )
        )
        await session.commit()
    preview = await preview_classification_edit(
        _admin(), _edit({"connector": _CONNECTOR}, category="network")
    )
    changes = {row.op_id: row.changes for row in preview.operations}
    assert changes == {
        "clsedit.net.list": True,
        "clsedit.vm.start": False,
        "clsedit.vm.stop": True,
    }
    assert preview.to_dict()["changed_operations"] == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_dry_run_shows_a_conflict_and_the_request_is_refused() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TenantPolicy(
                tenant_id=_TENANT,
                document={
                    "classifications": [{"match": {"op": "clsedit.vm.st*"}, "category": "compute"}]
                },
                set_by_sub="fixture",
            )
        )
        await session.commit()
    # The same level and the same literal prefix ("clsedit.vm.st") as the
    # stored entry, with another category: a conflict for start and stop.
    same = _edit({"op": "clsedit.vm.st[a-z]*"}, category="network")
    preview_same = await preview_classification_edit(_admin(), same)
    assert preview_same.conflict is not None
    assert "different category" in preview_same.conflict
    assert len(preview_same.operations) == 2
    assert all(row.after.always_wait for row in preview_same.operations)
    # A longer literal prefix is more specific: no conflict.
    longer = _edit({"op": "clsedit.vm.sta*"}, category="network")
    assert (await preview_classification_edit(_admin(), longer)).conflict is None
    with pytest.raises(ClassificationEditError, match="different category"):
        await request_classification_edit(_admin(), same)
    assert await _approval_requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_request_makes_a_waiting_rights_change() -> None:
    result = await request_classification_edit(
        _admin(), _edit({"op": "clsedit.vm.*"}, category="compute", always_wait=True)
    )
    assert result["status"] == "awaiting_approval", result
    assert result["matched_operations"] == 2
    request_id = uuid.UUID(str(result["extras"]["approval_request_id"]))
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
    assert request is not None
    assert request.rights_change is True
    assert request.op_id == POLICY_CLASSIFY_OP_ID
    assert await _stored_documents() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("edit", "message"),
    [
        pytest.param(
            _edit({"op": "nothing.*"}, category="compute"), "finds no operation", id="no-match"
        ),
        pytest.param(
            _edit({"op": "clsedit.*"}, category="unknown"), "not in the taxonomy", id="bad-category"
        ),
        pytest.param(_edit({}, category="compute"), "match", id="no-match-field"),
        pytest.param(_edit({"op": "clsedit.*"}), "at least one", id="no-value"),
        pytest.param(_edit({"op": "clsedit.*"}, labels={"": "x"}), "Label key", id="bad-label"),
    ],
)
@pytest.mark.usefixtures("ops")
async def test_request_refuses_a_bad_edit_before_asking(
    edit: ClassificationEdit, message: str
) -> None:
    with pytest.raises(ClassificationEditError, match=message):
        await request_classification_edit(_admin(), edit)
    assert await _approval_requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_the_other_tenants_entries_do_not_count() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TenantPolicy(
                tenant_id=_OTHER_TENANT,
                document={
                    "classifications": [{"match": {"op": "clsedit.*"}, "category": "storage"}]
                },
                set_by_sub="fixture",
            )
        )
        await session.commit()
    preview = await preview_classification_edit(
        _admin(), _edit({"op": "clsedit.vm.start"}, labels={"team": "ops"})
    )
    assert preview.operations[0].before.category == "other"


@pytest.mark.asyncio
async def test_switch_off_refuses_dry_run_and_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_off(monkeypatch)
    edit = _edit({"op": "clsedit.*"}, category="compute")
    with pytest.raises(ClassificationOffError):
        await preview_classification_edit(_admin(), edit)
    with pytest.raises(ClassificationOffError):
        await request_classification_edit(_admin(), edit)


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_an_operator_without_tenant_admin_is_refused() -> None:
    operator = Operator(
        sub="operator-a",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        raw_jwt="header.payload.signature",
    )
    result = await request_classification_edit(
        operator, _edit({"op": "clsedit.*"}, category="compute")
    )
    assert result["status"] != "awaiting_approval", result
    assert await _approval_requests() == []


# ---------------------------------------------------------------------------
# The dry-run operation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_preview_operation_runs_at_once_and_writes_nothing() -> None:
    assert role_floor_for(POLICY_CLASSIFY_PREVIEW_OP_ID) == TenantRole.TENANT_ADMIN
    assert not is_rights_change_op(POLICY_CLASSIFY_PREVIEW_OP_ID)
    result = await call_operation(
        _admin(),
        {
            "connector_id": "policy-registry-1.x",
            "op_id": POLICY_CLASSIFY_PREVIEW_OP_ID,
            "params": {"match": {"group": "vm"}, "category": "compute"},
        },
    )
    assert result["status"] == "ok", result
    assert isinstance(result["result"], dict)
    assert result["result"]["matched_operations"] == 2
    assert await _stored_documents() == []
    assert await _approval_requests() == []


@pytest.mark.asyncio
async def test_preview_operation_is_not_registered_with_the_switch_off(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch_off(monkeypatch)
    await register_policy_classify_preview_operation(embedding_service=embedding)
    async with get_sessionmaker()() as session:
        found = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == POLICY_CLASSIFY_PREVIEW_OP_ID
                )
            )
        ).first()
    assert found is None


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.include_router(connector_router)
    app.include_router(connectors_ingest_router)
    app.include_router(overrides_router)
    return TestClient(app)


def _call(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
) -> Any:
    clear_jwks_cache()
    key = _make_rsa_keypair(f"kid-{uuid.uuid4()}")
    token = _mint_token(key, sub="admin-a", tenant_role=role.value, tenant_id=str(_TENANT))
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        return _client().request(
            method, path, json=body, headers={"Authorization": f"Bearer {token}"}
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_rest_preview_then_request() -> None:
    body = {"match": {"op": "clsedit.vm.*"}, "category": "compute"}
    preview = _call("POST", "/api/v1/policy/classification/preview", body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["matched_operations"] == 2
    assert await _approval_requests() == []
    requested = _call("POST", "/api/v1/policy/classification", body)
    assert requested.status_code == 202, requested.text
    assert requested.json()["status"] == "awaiting_approval"
    assert requested.json()["approval_request_id"]
    assert len(await _approval_requests()) == 1
    assert await _stored_documents() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_rest_refusals() -> None:
    no_match = _call(
        "POST", "/api/v1/policy/classification", {"match": {"op": "x.*"}, "category": "compute"}
    )
    assert no_match.status_code == 400
    assert "classification_refused" in no_match.json()["detail"]
    bad = _call(
        "POST", "/api/v1/policy/classification/preview", {"match": {"op": "x"}, "category": "Bad"}
    )
    assert bad.status_code == 422
    extra = _call("POST", "/api/v1/policy/classification/preview", {"match": {"tenant": "x"}})
    assert extra.status_code == 422
    operator = _call(
        "POST",
        "/api/v1/policy/classification/preview",
        {"match": {"op": "clsedit.*"}, "category": "compute"},
        role=TenantRole.OPERATOR,
    )
    assert operator.status_code == 403


def test_rest_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_off(monkeypatch)
    body = {"match": {"op": "clsedit.*"}, "category": "compute"}
    assert _call("POST", "/api/v1/policy/classification/preview", body).status_code == 409
    assert _call("POST", "/api/v1/policy/classification", body).status_code == 409
    patch = _call(
        "PATCH",
        f"/api/v1/connectors/{_CONNECTOR}/operations/clsedit.vm.start",
        {"category": "compute"},
    )
    assert patch.status_code == 409
    listing = _call("GET", "/api/v1/policy/classification")
    assert listing.status_code == 200
    assert listing.json()["enabled"] is False
    assert listing.json()["entries"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_rest_list_shows_the_taxonomy_and_entries() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TenantPolicy(
                tenant_id=_TENANT,
                document={"classifications": [{"match": {"group": "vm"}, "category": "compute"}]},
                set_by_sub="fixture",
            )
        )
        await session.commit()
    listing = _call("GET", "/api/v1/policy/classification", role=TenantRole.OPERATOR)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["enabled"] is True
    assert "compute" in body["taxonomy"]["categories"]
    assert body["entries"] == [
        {
            "ref": "tenant:classifications[0]",
            "scope": "tenant",
            "match": {"group": "vm"},
            "category": "compute",
            "labels": {},
            "risk": None,
            "always_wait": None,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body", "match"),
    [
        pytest.param(
            f"/api/v1/connectors/{_CONNECTOR}/operations/clsedit.vm.start",
            {"category": "compute", "labels": {"team": "ops"}},
            {"connector": _CONNECTOR, "op": "clsedit.vm.start"},
            id="operation",
        ),
        pytest.param(
            f"/api/v1/connectors/{_CONNECTOR}/groups/vm",
            {"labels": {"team": "ops"}},
            {"connector": _CONNECTOR, "group": "vm"},
            id="group",
        ),
        pytest.param(
            f"/api/v1/connectors/{_CONNECTOR}/classification",
            {"category": "network", "always_wait": True},
            {"connector": _CONNECTOR},
            id="connector",
        ),
    ],
)
@pytest.mark.usefixtures("ops")
async def test_rest_patch_asks_for_a_waiting_change(
    path: str, body: dict[str, Any], match: dict[str, str]
) -> None:
    response = _call("PATCH", path, body)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "awaiting_approval"
    (request,) = await _approval_requests()
    assert request.rights_change is True
    assert request.op_id == POLICY_CLASSIFY_OP_ID
    assert await _stored_documents() == []
    assert request.params is not None
    assert request.params["match"] == match


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param(
            f"/api/v1/connectors/{_CONNECTOR}/operations/clsedit.vm.start",
            {"category": "compute", "requires_approval": True},
            id="operation",
        ),
        pytest.param(
            f"/api/v1/connectors/{_CONNECTOR}/groups/vm",
            {"category": "compute", "name": "VMs"},
            id="group",
        ),
    ],
)
@pytest.mark.usefixtures("ops")
async def test_rest_patch_refuses_a_mixed_body(path: str, body: dict[str, Any]) -> None:
    response = _call("PATCH", path, body)
    assert response.status_code == 400, response.text
    assert "classification_mixed" in response.json()["detail"]
    assert await _approval_requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_rest_patch_of_an_unknown_operation_is_refused() -> None:
    response = _call(
        "PATCH", f"/api/v1/connectors/{_CONNECTOR}/operations/clsedit.nope", {"category": "compute"}
    )
    assert response.status_code == 400
    assert await _approval_requests() == []


# ---------------------------------------------------------------------------
# The admin MCP tools
# ---------------------------------------------------------------------------


def _mcp_handler(name: str) -> Any:
    found = get_tool(name)
    assert found is not None
    return found


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_mcp_edit_op_asks_for_a_waiting_change() -> None:
    _, handler = _mcp_handler("meho_connector_edit_op")
    result = await handler(
        _admin(), {"connector_id": _CONNECTOR, "op_id": "clsedit.vm.start", "category": "compute"}
    )
    assert result["status"] == "awaiting_approval", result
    assert result["approval_request_id"]
    assert result["op"] == "clsedit.vm.start"
    assert len(await _approval_requests()) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_mcp_edit_group_refuses_a_mixed_call() -> None:
    _, handler = _mcp_handler("meho_connector_edit_group")
    with pytest.raises(McpInvalidParamsError, match="classification_mixed"):
        await handler(
            _admin(),
            {"connector_id": _CONNECTOR, "group_key": "vm", "labels": {"a": "b"}, "name": "VMs"},
        )
    with pytest.raises(McpInvalidParamsError, match="classification_refused"):
        await handler(
            _admin(), {"connector_id": _CONNECTOR, "group_key": "nope", "category": "compute"}
        )
    assert await _approval_requests() == []


@pytest.mark.parametrize("name", ["meho_connector_edit_op", "meho_connector_edit_group"])
def test_mcp_fields_exist_only_with_the_switch_on(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition, _ = _mcp_handler(name)
    shown_on = tool_for_settings(definition, _admin())
    assert {"category", "labels"} <= set(shown_on.inputSchema["properties"])
    assert "policy.classify_preview" in shown_on.description
    _switch_off(monkeypatch)
    shown_off = tool_for_settings(definition, _admin())
    assert not {"category", "labels"} & set(shown_off.inputSchema["properties"])
    assert "category" not in shown_off.description
    assert "policy.classify" not in shown_off.description


# ---------------------------------------------------------------------------
# The override report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_report_shows_the_value_in_force(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(embedding, "clsedit.vm.start", requires_approval=True)
    async with get_sessionmaker()() as session:
        session.add(
            OperationPolicyOverride(
                tenant_id=_TENANT,
                **_FIX,
                op_id="clsedit.vm.start",
                requires_approval=False,
                reason="read-only in practice",
                set_by_sub="platform-admin",
            )
        )
        await session.commit()
    admin = _admin()
    _switch_off(monkeypatch)
    (row,) = await load_override_rows(admin, reduced_approval=True)
    assert row.requires_approval is False
    assert row.rule_ref is None
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    async with get_sessionmaker()() as session:
        session.add(
            TenantPolicy(
                tenant_id=_TENANT,
                document={
                    "classifications": [
                        {
                            "match": {"op": "clsedit.vm.*"},
                            "category": "compute",
                            "always_wait": True,
                        }
                    ]
                },
                set_by_sub="fixture",
            )
        )
        await session.commit()
    assert await load_override_rows(admin, reduced_approval=True) == []
    (row,) = await load_override_rows(admin, reduced_approval=False)
    assert row.requires_approval is True
    assert row.always_wait is True
    assert row.category == "compute"
    assert row.rule_ref == "tenant:classifications[0]"
