# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator overrides of risk levels and approval flags (#4711).

What these tests prove:

* A tenant admin raises a typed operation and requires approval; after a
  restart (typed registration runs again) the operation is still stricter.
  The same holds for an ingested operation after its spec is read again.
* A tenant admin's change does not reach another tenant.
* An edit can never go below the classification, on any surface.
* Turning approval off needs ``platform_admin``, a reason and a fresh
  sign-in, and is never allowed for a destructive operation.
* The MCP edit tool can only make an operation stricter.
* A REST call with an old sign-in, a service principal or an agent token
  cannot turn approval off (``401`` with the RFC 9470 challenge).
* Every edit's audit row holds the old and new values, the scope and the
  reason; the report lists the operations with approval turned off.
"""

from __future__ import annotations

import time
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
from meho_backplane.api.v1.operation_overrides import router as overrides_router
from meho_backplane.auth.fresh_sign_in import has_fresh_sign_in
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationPolicyOverride
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.ingest import (
    EndpointDescriptorProto,
    ReviewService,
    register_ingested_operations,
)
from meho_backplane.operations.op_policy import OpPolicyEditError
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_TYPED = {"product": "typedpets", "version": "1.x", "impl_id": "typedpets"}
_TYPED_CONNECTOR = "typedpets-1.x"
_EDIT_OP = "meho_connector_edit_op"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    clear_jwks_cache()
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


def _operator(
    tenant_id: uuid.UUID,
    *,
    platform_admin: bool = False,
    kind: PrincipalKind = PrincipalKind.USER,
    auth_time: int | None = None,
) -> Operator:
    return Operator(
        sub=f"sub-{uuid.uuid4()}",
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=platform_admin,
        principal_kind=kind,
        auth_time=auth_time,
    )


async def _typed_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _register_typed(
    embedding: AsyncMock,
    *,
    op_id: str = "typedpets.pet.update",
    safety_level: Any = "caution",
    requires_approval: bool = False,
) -> None:
    """Register one typed op, the way every start does."""
    await register_typed_operation(
        **_TYPED,
        op_id=op_id,
        handler=_typed_handler,
        summary="Change a pet.",
        description="Change a pet.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level=safety_level,
        requires_approval=requires_approval,
        embedding_service=embedding,
    )


async def _effective(tenant_id: uuid.UUID, op_id: str = "typedpets.pet.update") -> tuple[str, bool]:
    row = await lookup_descriptor(tenant_id=tenant_id, op_id=op_id, **_TYPED)
    assert row is not None
    return row.safety_level, row.requires_approval


async def _stored_row(op_id: str = "typedpets.pet.update") -> EndpointDescriptor:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.tenant_id.is_(None), EndpointDescriptor.op_id == op_id
                )
            )
        ).scalar_one()


async def _last_edit_audit() -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == _EDIT_OP)))
            .scalars()
            .all()
        )
    assert rows, "no edit audit row"
    payload: dict[str, Any] = sorted(rows, key=lambda r: r.occurred_at)[-1].payload
    return payload


# ---------------------------------------------------------------------------
# Tightening sticks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typed_tightening_survives_restart_and_stays_in_tenant(
    embedding: AsyncMock,
) -> None:
    """A tenant admin's stricter typed op stays strict after a restart, for that tenant only."""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    await _register_typed(embedding)

    await ReviewService(_operator(tenant_a)).edit_op(
        _TYPED_CONNECTOR,
        "typedpets.pet.update",
        tenant_id=tenant_a,
        safety_level="dangerous",
        requires_approval=True,
        reason="touches billing",
    )
    await _register_typed(embedding)  # the next start writes the code values again

    assert await _effective(tenant_a) == ("dangerous", True)
    assert await _effective(tenant_b) == ("caution", False)
    stored = await _stored_row()
    assert (stored.safety_level, stored.requires_approval) == ("caution", False)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_registry")
async def test_ingested_tightening_survives_reingest(embedding: AsyncMock) -> None:
    """A stricter ingested op stays strict when its spec is read again, changed or not."""
    tenant_id = uuid.uuid4()

    async def ingest(summary: str) -> None:
        await register_ingested_operations(
            product="petstore",
            version="1.0",
            impl_id="petstore-rest",
            spec_source="petstore.yaml",
            operations=[
                EndpointDescriptorProto(
                    op_id="POST:/pets",
                    method="POST",
                    path="/pets",
                    summary=summary,
                    description=summary,
                    tags=["pets"],
                    parameter_schema={"type": "object", "properties": {}},
                    response_schema={"type": "object"},
                    safety_level="caution",
                    requires_approval=False,
                )
            ],
            tenant_id=tenant_id,
            embedding_service=embedding,
        )

    await ingest("Add a pet")
    service = ReviewService(_operator(tenant_id))
    await service.edit_op("petstore-rest-1.0", "POST:/pets", tenant_id=tenant_id, is_enabled=True)
    await service.edit_op(
        "petstore-rest-1.0",
        "POST:/pets",
        tenant_id=tenant_id,
        safety_level="dangerous",
        requires_approval=True,
    )
    await ingest("Add a pet")
    await ingest("Add a new pet to the shop")

    row = await lookup_descriptor(
        tenant_id=tenant_id,
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        op_id="POST:/pets",
    )
    assert row is not None
    assert (row.safety_level, row.requires_approval) == ("dangerous", True)


@pytest.mark.asyncio
async def test_raise_to_destructive_requires_approval(embedding: AsyncMock) -> None:
    """Raising works for all four levels; destructive always waits for approval."""
    tenant_id = uuid.uuid4()
    await _register_typed(embedding, safety_level="safe")
    await ReviewService(_operator(tenant_id)).edit_op(
        _TYPED_CONNECTOR, "typedpets.pet.update", tenant_id=tenant_id, safety_level="destructive"
    )
    assert await _effective(tenant_id) == ("destructive", True)


@pytest.mark.asyncio
async def test_platform_admin_change_applies_to_whole_install(embedding: AsyncMock) -> None:
    """A platform admin's change to a built-in op applies to every tenant."""
    await _register_typed(embedding)
    await ReviewService(_operator(uuid.uuid4(), platform_admin=True)).edit_op(
        _TYPED_CONNECTOR, "typedpets.pet.update", tenant_id=None, safety_level="dangerous"
    )
    assert (await _effective(uuid.uuid4()))[0] == "dangerous"
    assert (await _last_edit_audit())["scope"] == "install"


# ---------------------------------------------------------------------------
# Lowering is refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lowering_below_classification_is_refused(embedding: AsyncMock) -> None:
    """An edit below the classification is refused with the pull-request message."""
    tenant_id = uuid.uuid4()
    await _register_typed(embedding, safety_level="dangerous", requires_approval=True)
    for operator in (_operator(tenant_id), _operator(tenant_id, platform_admin=True)):
        with pytest.raises(OpPolicyEditError) as caught:
            await ReviewService(operator).edit_op(
                _TYPED_CONNECTOR, "typedpets.pet.update", tenant_id=tenant_id, safety_level="safe"
            )
        assert caught.value.code == "risk_level_below_classification"
        assert "pull request" in str(caught.value)
    assert await _effective(tenant_id) == ("dangerous", True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_registry")
async def test_ingested_delete_cannot_go_below_its_floor(embedding: AsyncMock) -> None:
    """The #3604 case: an ingested delete keeps its minimum level against an edit."""
    tenant_id = uuid.uuid4()
    await register_ingested_operations(
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        spec_source="petstore.yaml",
        operations=[
            EndpointDescriptorProto(
                op_id="DELETE:/pets/{id}",
                method="DELETE",
                path="/pets/{id}",
                summary="Delete a pet",
                description="Delete a pet",
                tags=["pets"],
                parameter_schema={"type": "object", "properties": {}},
                response_schema={"type": "object"},
                safety_level="destructive",
                requires_approval=True,
            )
        ],
        tenant_id=tenant_id,
        embedding_service=embedding,
    )
    with pytest.raises(OpPolicyEditError) as caught:
        await ReviewService(_operator(tenant_id, platform_admin=True)).edit_op(
            "petstore-rest-1.0", "DELETE:/pets/{id}", tenant_id=tenant_id, safety_level="caution"
        )
    assert caught.value.code == "risk_level_below_classification"


@pytest.mark.asyncio
async def test_removing_an_override_brings_back_the_code_value(embedding: AsyncMock) -> None:
    """Setting the classification again removes the override; the audit row shows it."""
    tenant_id = uuid.uuid4()
    await _register_typed(embedding)
    service = ReviewService(_operator(tenant_id))
    await service.edit_op(
        _TYPED_CONNECTOR, "typedpets.pet.update", tenant_id=tenant_id, safety_level="dangerous"
    )
    await service.edit_op(
        _TYPED_CONNECTOR, "typedpets.pet.update", tenant_id=tenant_id, safety_level="caution"
    )

    assert await _effective(tenant_id) == ("caution", False)
    async with get_sessionmaker()() as session:
        left = (await session.execute(select(OperationPolicyOverride))).scalars().all()
    assert list(left) == []
    payload = await _last_edit_audit()
    assert payload["changes"]["safety_level"] == {"old": "dangerous", "new": "caution"}


# ---------------------------------------------------------------------------
# Turning approval off
# ---------------------------------------------------------------------------


async def _turn_off(operator: Operator, **kwargs: Any) -> None:
    await ReviewService(operator).edit_op(
        _TYPED_CONNECTOR,
        "typedpets.pet.update",
        tenant_id=None if operator.platform_admin else operator.tenant_id,
        requires_approval=False,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_turning_approval_off_rules(embedding: AsyncMock) -> None:
    """Approval off needs platform_admin, a reason and a person; then it is audited and listed."""
    tenant_id = uuid.uuid4()
    await _register_typed(embedding, requires_approval=True)
    admin = _operator(tenant_id, platform_admin=True)

    cases: list[tuple[Operator, dict[str, Any], str]] = [
        (_operator(tenant_id), {"reason": "x", "person_present": True}, "platform_admin"),
        (admin, {"person_present": True}, "reason"),
        (admin, {"reason": "  ", "person_present": True}, "reason"),
        (admin, {"reason": "read-only in practice"}, "fresh_sign_in"),
    ]
    for operator, kwargs, code_part in cases:
        with pytest.raises(OpPolicyEditError) as caught:
            await _turn_off(operator, **kwargs)
        assert code_part in caught.value.code
    assert await _effective(tenant_id) == ("caution", True)

    await _turn_off(admin, reason="read-only in practice", person_present=True)
    assert await _effective(tenant_id) == ("caution", False)
    assert await _effective(uuid.uuid4()) == ("caution", False)

    payload = await _last_edit_audit()
    assert payload["changes"]["requires_approval"] == {"old": True, "new": False}
    assert payload["scope"] == "install"
    assert payload["reason"] == "read-only in practice"

    # A tenant admin may still require approval again for their own tenant.
    await ReviewService(_operator(tenant_id)).edit_op(
        _TYPED_CONNECTOR, "typedpets.pet.update", tenant_id=tenant_id, requires_approval=True
    )
    assert await _effective(tenant_id) == ("caution", True)
    assert await _effective(uuid.uuid4()) == ("caution", False)


@pytest.mark.asyncio
async def test_destructive_approval_is_never_turned_off(embedding: AsyncMock) -> None:
    """Even a platform admin with a reason and a fresh sign-in cannot do it."""
    await _register_typed(embedding, safety_level="destructive", requires_approval=True)
    with pytest.raises(OpPolicyEditError) as caught:
        await _turn_off(
            _operator(uuid.uuid4(), platform_admin=True), reason="why", person_present=True
        )
    assert caught.value.code == "approval_off_destructive"


@pytest.mark.asyncio
async def test_tighten_only_refuses_every_loosening(embedding: AsyncMock) -> None:
    """The MCP mode refuses approval off and any level lower than the current one."""
    tenant_id = uuid.uuid4()
    await _register_typed(embedding, requires_approval=True)
    service = ReviewService(_operator(tenant_id, platform_admin=True))
    await service.edit_op(
        _TYPED_CONNECTOR,
        "typedpets.pet.update",
        tenant_id=None,
        safety_level="dangerous",
        tighten_only=True,
    )
    for kwargs in ({"requires_approval": False, "reason": "x"}, {"safety_level": "caution"}):
        with pytest.raises(OpPolicyEditError) as caught:
            await service.edit_op(
                _TYPED_CONNECTOR,
                "typedpets.pet.update",
                tenant_id=None,
                tighten_only=True,
                person_present=True,
                **kwargs,
            )
        assert caught.value.code == "edit_tool_cannot_loosen"
        assert "console or the CLI" in str(caught.value)
    assert await _effective(tenant_id) == ("dangerous", True)


@pytest.mark.asyncio
async def test_review_payload_shows_effective_and_classified_values(
    embedding: AsyncMock,
) -> None:
    tenant_id = uuid.uuid4()
    await _register_typed(embedding)
    service = ReviewService(_operator(tenant_id))
    await service.edit_op(
        _TYPED_CONNECTOR, "typedpets.pet.update", tenant_id=tenant_id, safety_level="dangerous"
    )
    payload = await service.get_review_payload(_TYPED_CONNECTOR, tenant_id)
    ops = [*payload.ungrouped_ops, *(op for group in payload.groups for op in group.ops)]
    (op,) = [op for op in ops if op.op_id == "typedpets.pet.update"]
    assert (op.safety_level, op.classified_safety_level) == ("dangerous", "caution")


# ---------------------------------------------------------------------------
# Fresh sign-in check
# ---------------------------------------------------------------------------


def test_has_fresh_sign_in() -> None:
    now = time.time()
    tenant_id = uuid.uuid4()
    assert has_fresh_sign_in(_operator(tenant_id, auth_time=int(now) - 10), now=now)
    assert not has_fresh_sign_in(_operator(tenant_id, auth_time=int(now) - 3600), now=now)
    assert not has_fresh_sign_in(_operator(tenant_id), now=now)
    for kind in (PrincipalKind.SERVICE, PrincipalKind.AGENT):
        assert not has_fresh_sign_in(_operator(tenant_id, kind=kind, auth_time=int(now)), now=now)


# ---------------------------------------------------------------------------
# REST and MCP surfaces
# ---------------------------------------------------------------------------


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(overrides_router)
    app.include_router(connectors_ingest_router)
    return TestClient(app)


def _patch(token_kwargs: dict[str, Any], body: dict[str, Any]) -> Any:
    clear_jwks_cache()  # each call signs with a new key
    key = _make_rsa_keypair(f"kid-{uuid.uuid4()}")
    token = _mint_token(
        key,
        sub="admin-sub",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(uuid.uuid4()),
        **token_kwargs,
    )
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        client = _client()
        response = client.patch(
            f"/api/v1/connectors/{_TYPED_CONNECTOR}/operations/typedpets.pet.update",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        report = client.get(
            "/api/v1/connectors/operation-overrides?reduced_approval=true",
            headers={"Authorization": f"Bearer {token}"},
        )
    return response, report


_OFF = {"requires_approval": False, "reason": "read-only in practice"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"platform_admin": True, "extra_claims": {"auth_time": int(time.time()) - 3600}},
        {"platform_admin": True},
        {"platform_admin": True, "principal_kind": "service", "extra_claims": {"auth_time": 1}},
        {"platform_admin": True, "principal_kind": "agent"},
    ],
    ids=["old-sign-in", "no-auth-time", "service-principal", "agent"],
)
async def test_rest_without_fresh_sign_in_cannot_turn_approval_off(
    embedding: AsyncMock, token_kwargs: dict[str, Any]
) -> None:
    await _register_typed(embedding, requires_approval=True)
    response, report = _patch(token_kwargs, _OFF)
    assert response.status_code == 401, response.text
    assert 'error="insufficient_user_authentication"' in response.headers["www-authenticate"]
    assert "approval_off_needs_fresh_sign_in" in response.json()["detail"]
    assert report.json() == {"overrides": []}


@pytest.mark.asyncio
async def test_rest_fresh_sign_in_turns_approval_off_and_report_lists_it(
    embedding: AsyncMock,
) -> None:
    await _register_typed(embedding, requires_approval=True)
    response, report = _patch(
        {"platform_admin": True, "extra_claims": {"auth_time": int(time.time()) - 5}}, _OFF
    )
    assert response.status_code == 200, response.text
    (row,) = report.json()["overrides"]
    assert row["connector_id"] == _TYPED_CONNECTOR
    assert row["op_id"] == "typedpets.pet.update"
    assert row["requires_approval"] is False
    assert row["scope"] == "install"
    assert row["reason"] == "read-only in practice"
    assert row["set_by_sub"] == "admin-sub"


@pytest.mark.asyncio
async def test_rest_refusals_map_to_400_and_403(embedding: AsyncMock) -> None:
    await _register_typed(embedding, requires_approval=True)
    fresh = {"extra_claims": {"auth_time": int(time.time())}}
    lower, _ = _patch({**fresh, "platform_admin": True}, {"safety_level": "safe"})
    assert lower.status_code == 400
    assert "pull request" in lower.json()["detail"]
    tenant_admin, _ = _patch(fresh, _OFF)
    assert tenant_admin.status_code == 403
    assert "approval_off_needs_platform_admin" in tenant_admin.json()["detail"]
    raised, _ = _patch(fresh, {"safety_level": "destructive"})
    assert raised.status_code == 200, raised.text


@pytest.mark.asyncio
async def test_mcp_edit_tool_refuses_to_loosen(embedding: AsyncMock) -> None:
    import meho_backplane.mcp.tools.connector_admin as ca_mod

    await _register_typed(embedding, requires_approval=True)
    admin = _operator(uuid.uuid4(), platform_admin=True, auth_time=int(time.time()))
    base = {"connector_id": _TYPED_CONNECTOR, "op_id": "typedpets.pet.update", "tenant_id": None}
    for patch in ({"requires_approval": False, "reason": "x"}, {"safety_level": "safe"}):
        with pytest.raises(McpInvalidParamsError) as caught:
            await ca_mod._edit_op_handler(admin, {**base, **patch})
        assert "pull request" in str(caught.value)
    result = await ca_mod._edit_op_handler(admin, {**base, "safety_level": "dangerous"})
    assert result["ok"] is True
    assert await _effective(uuid.uuid4()) == ("dangerous", True)
