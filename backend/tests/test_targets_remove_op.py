# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``targets.remove`` typed op on ``targets-registry-1.x`` (#4623).

* **Registration** -- ``dangerous`` with ``requires_approval: true``, and
  the op id matches a delete pattern, so the rules for deletes apply.
* **Same answer as REST** -- the op and ``DELETE /api/v1/targets/{name}``
  give the same answer for a target that is removed, a target that does not
  exist, and a target of another tenant (which is never removed).
* **Topology check** -- the shared delete service refuses a target that
  ``graph_node`` rows point at unless ``force`` is set.
* **Policy** -- a person waits for a second person and cannot approve the
  own request; an agent login is refused, also in sandbox mode; a service
  principal waits and cannot get a standing grant (422); in sandbox mode a
  delete grant lets a service principal run it at once.
* **Preview** -- names the target, its product and the ``graph_node`` count.
* **Audit** -- every call writes a row with op id ``targets.remove``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.targets.ops import (
    TARGETS_REGISTRY_CONNECTOR_ID,
    TARGETS_REMOVE_OP_ID,
    register_targets_registry_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    GraphNode,
    Tenant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.delete_names import DELETE_SHAPED_OP_ID_PATTERNS
from meho_backplane.main import app
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    SelfApprovalForbiddenError,
    approve_request,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests._sandbox_mode_helpers import (
    grant_use_rows,
    seed_delete_grant,
    turn_sandbox_mode_off,
    turn_sandbox_mode_on,
)

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._targets_helpers import _admin_token, _build_app, _insert_target

_TENANT_ID = uuid.UUID(DEFAULT_TENANT_ID)
_SERVICE_SUB = "svc:test-cleaner"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()


@pytest_asyncio.fixture(autouse=True)
async def _registered_op(_settings_env: None) -> AsyncIterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_targets_registry_operations(embedding_service=service)
    yield


def _operator(
    sub: str,
    *,
    kind: PrincipalKind = PrincipalKind.USER,
    tenant_id: uuid.UUID = _TENANT_ID,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=kind,
    )


def _args(target_id: uuid.UUID | str, **params: Any) -> dict[str, Any]:
    return {
        "connector_id": TARGETS_REGISTRY_CONNECTOR_ID,
        "op_id": TARGETS_REMOVE_OP_ID,
        "params": {"target_id": str(target_id), **params},
    }


async def _seed(name: str, *, tenant_id: uuid.UUID = _TENANT_ID) -> TargetORM:
    return await _insert_target(tenant_id=tenant_id, name=name, product="ssh", host="203.0.113.5")


async def _seed_graph_node(target_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, _TENANT_ID) is None:
            session.add(Tenant(id=_TENANT_ID, slug=f"t-{_TENANT_ID}", name="test tenant"))
            await session.flush()
        session.add(
            GraphNode(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                kind="target",
                name="node-a",
                target_id=target_id,
                properties={},
                discovered_by="test",
                first_seen=datetime.now(UTC),
            )
        )
        await session.commit()


async def _is_removed(target_id: uuid.UUID) -> bool:
    async with get_sessionmaker()() as session:
        row = await session.get(TargetORM, target_id)
    assert row is not None
    return row.deleted_at is not None


async def _audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [row for row in rows if (row.payload or {}).get("op_id") == TARGETS_REMOVE_OP_ID]


async def _run_approved(operator: Operator, target_id: uuid.UUID | str, **params: Any) -> Any:
    """Run the op the way MEHO runs it after a second person approved it."""
    return await dispatch(
        operator=operator,
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=TARGETS_REMOVE_OP_ID,
        target=None,
        params={"target_id": str(target_id), **params},
        _approved=True,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_registered_as_a_dangerous_delete_that_needs_approval() -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == TARGETS_REMOVE_OP_ID)
            )
        ).scalar_one()
    assert row.source_kind == "typed"
    assert (row.product, row.version, row.impl_id) == ("targets", "1.x", "targets-registry")
    assert row.safety_level == "dangerous"
    assert row.requires_approval is True
    assert any(fnmatchcase(TARGETS_REMOVE_OP_ID, p) for p in DELETE_SHAPED_OP_ID_PATTERNS)
    assert set(row.response_schema["properties"]) == {"status", "target_id", "name"}


# ---------------------------------------------------------------------------
# Same answer as REST DELETE /api/v1/targets/{name}
# ---------------------------------------------------------------------------


def _rest_delete(name: str) -> int:
    key = make_rsa_keypair("kid-remove")
    client = TestClient(_build_app())
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.delete(
            f"/api/v1/targets/{name}",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    return response.status_code


@pytest.mark.parametrize(
    ("case", "rest_status", "op_status"),
    [
        ("removed", 204, "removed"),
        ("not_found", 404, "unchanged"),
        ("other_tenant", 404, "unchanged"),
    ],
)
async def test_op_and_rest_give_the_same_answer(
    case: str, rest_status: int, op_status: str
) -> None:
    tenant_id = uuid.uuid4() if case == "other_tenant" else _TENANT_ID
    rest_row = await _seed("rest-side", tenant_id=tenant_id)
    op_row = await _seed("op-side", tenant_id=tenant_id)
    rest_name = "no-such-target" if case == "not_found" else rest_row.name
    op_id_param: uuid.UUID = uuid.uuid4() if case == "not_found" else op_row.id

    assert _rest_delete(rest_name) == rest_status

    result = await _run_approved(_operator("op-person"), op_id_param)
    assert result.status == "ok", result
    assert result.result["status"] == op_status
    assert result.result["target_id"] == str(op_id_param)
    if op_status == "removed":
        assert result.result["name"] == "op-side"
    else:
        assert result.result["name"] is None

    removed = case == "removed"
    assert await _is_removed(rest_row.id) is removed
    assert await _is_removed(op_row.id) is removed


async def test_a_second_run_gives_unchanged() -> None:
    row = await _seed("twice")
    first = await _run_approved(_operator("op-person"), row.id)
    second = await _run_approved(_operator("op-person"), row.id)
    assert first.result["status"] == "removed"
    assert second.result["status"] == "unchanged"


async def test_a_bad_target_id_is_an_error() -> None:
    result = await _run_approved(_operator("op-person"), "not-a-uuid")
    assert result.status == "error", result
    assert "UUID" in str(result.extras.get("exception_message"))


# ---------------------------------------------------------------------------
# Topology check of the shared delete service
# ---------------------------------------------------------------------------


async def test_topology_records_block_the_removal_unless_force() -> None:
    row = await _seed("has-nodes")
    await _seed_graph_node(row.id)

    blocked = await _run_approved(_operator("op-person"), row.id)
    assert blocked.status == "error", blocked
    assert "graph_node" in str(blocked.extras.get("exception_message"))
    assert await _is_removed(row.id) is False

    forced = await _run_approved(_operator("op-person"), row.id, force=True)
    assert forced.result["status"] == "removed"
    assert await _is_removed(row.id) is True


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


async def test_a_person_waits_and_cannot_approve_the_own_request() -> None:
    row = await _seed("person-asks")
    asker = _operator("op-asker")

    call = await call_operation(asker, _args(row.id))
    assert call["status"] == "awaiting_approval", call
    assert await _is_removed(row.id) is False
    request_id = uuid.UUID(str(call["extras"]["approval_request_id"]))

    async with get_sessionmaker()() as session:
        with pytest.raises(SelfApprovalForbiddenError):
            await approve_request(session, request_id, operator=asker)

    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request_id, operator=_operator("op-other"))
        await session.commit()
    assert approved.status == "approved"
    assert await _audit_rows()


@pytest.mark.parametrize("sandbox", [False, True])
async def test_an_agent_login_is_refused(monkeypatch: pytest.MonkeyPatch, sandbox: bool) -> None:
    row = await _seed("agent-asks")
    if sandbox:
        turn_sandbox_mode_on(monkeypatch)
        await seed_delete_grant(
            tenant_id=_TENANT_ID,
            principal_sub="agent-1",
            op_id=TARGETS_REMOVE_OP_ID,
            connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        )
    else:
        turn_sandbox_mode_off(monkeypatch)

    call = await call_operation(_operator("agent-1", kind=PrincipalKind.AGENT), _args(row.id))
    assert call["status"] == "denied", call
    assert await _is_removed(row.id) is False
    async with get_sessionmaker()() as session:
        pending = (await session.execute(select(ApprovalRequest))).scalars().all()
    assert pending == []
    assert await grant_use_rows() == []
    assert await _audit_rows()


async def test_a_service_principal_waits_for_a_person() -> None:
    row = await _seed("service-asks")
    call = await call_operation(_operator(_SERVICE_SUB, kind=PrincipalKind.SERVICE), _args(row.id))
    assert call["status"] == "awaiting_approval", call
    assert await _is_removed(row.id) is False
    assert await _audit_rows()


def test_a_standing_grant_for_the_op_is_refused() -> None:
    key = make_rsa_keypair("kid-grant")
    token = mint_token(
        key, sub="op-admin", tenant_role=TenantRole.OPERATOR.value, tenant_id=DEFAULT_TENANT_ID
    )
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = TestClient(app).post(
            "/api/v1/service-principals/grants",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "principal_sub": _SERVICE_SUB,
                "op_id": TARGETS_REMOVE_OP_ID,
                "connector_id": TARGETS_REGISTRY_CONNECTOR_ID,
                "reason": "clean up test targets",
            },
        )
    assert response.status_code == 422, response.text
    assert "delete-shaped" in response.json()["detail"]


@pytest.mark.parametrize("with_grant", [True, False])
async def test_sandbox_mode_runs_a_service_call_only_under_a_delete_grant(
    monkeypatch: pytest.MonkeyPatch, with_grant: bool
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    row = await _seed("sandbox-target")
    grant_id = None
    if with_grant:
        grant_id = await seed_delete_grant(
            tenant_id=_TENANT_ID,
            principal_sub=_SERVICE_SUB,
            op_id=TARGETS_REMOVE_OP_ID,
            connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        )

    call = await call_operation(_operator(_SERVICE_SUB, kind=PrincipalKind.SERVICE), _args(row.id))
    if with_grant:
        assert call["status"] == "ok", call
        assert call["result"]["status"] == "removed"
        assert await _is_removed(row.id) is True
        assert len(await grant_use_rows(grant_id)) == 1
    else:
        assert call["status"] == "awaiting_approval", call
        assert await _is_removed(row.id) is False
        assert await grant_use_rows() == []
    assert await _audit_rows()


# ---------------------------------------------------------------------------
# Preview and audit
# ---------------------------------------------------------------------------


async def test_preview_names_the_target_its_product_and_the_topology_count() -> None:
    row = await _seed("preview-me")
    await _seed_graph_node(row.id)

    preview = await preview_operation(_operator("op-person"), _args(row.id))
    assert preview["status"] == "ok", preview
    effect = preview["proposed_effect"]["preview"]
    assert effect["target"] == {"id": str(row.id), "name": "preview-me", "host": row.host}
    assert effect["connector"]["product"] == "ssh"
    assert effect["graph_node_refs"] == 1
    assert effect["result_if_run"] == "fails"
    assert await _is_removed(row.id) is False


async def test_preview_of_a_missing_target_says_unchanged() -> None:
    preview = await preview_operation(_operator("op-person"), _args(uuid.uuid4()))
    assert preview["status"] == "ok", preview
    effect = preview["proposed_effect"]["preview"]
    assert effect["target"] is None
    assert effect["result_if_run"] == "unchanged"


async def test_an_executed_call_writes_an_audit_row() -> None:
    row = await _seed("audited")
    result = await _run_approved(_operator("op-person"), row.id)
    assert result.result["status"] == "removed"
    (audit,) = await _audit_rows()
    assert audit.method == "DISPATCH"
    assert audit.path == TARGETS_REMOVE_OP_ID
