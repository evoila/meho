# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The dry run of the "always wait" carry-over (``GET /api/v1/policy/carry-over/dry-run``).

* It lists each saved "always wait" of the scope: overrides with approval
  on and policy file entries with ``always_wait: true``, with the source,
  the scope, the operations it makes wait and the warnings (a step of a
  multi-step operation, sandbox mode, the preview).
* A tenant admin sees the tenant's settings, a platform admin also the
  install's. Another role is refused. Each read writes an audit row.
* It writes nothing: no policy row, override or approval request changes.
* An override with approval on keeps waiting, with ``CUSTOM_POLICY`` on
  and off.

Drives the production ``meho_backplane.main:app`` with real tokens. DB is
the autouse per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    OperationPolicyOverride,
    TenantPolicy,
)
from meho_backplane.main import app
from meho_backplane.operations import governed_subops
from meho_backplane.operations.governed_subops import GovernedSubopSurface
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.carry_over import DRY_RUN_MESSAGE
from meho_backplane.sandbox_mode import set_sandbox_mode_active
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_OTHER_TENANT = uuid.uuid4()
_CONNECTOR = "cowapi-1.x"
_ROUTE = "/api/v1/policy/carry-over/dry-run"
_AUDIT_OP = "policy.carry_over.dry_run"
_PREVIEW = "As `destructive`, a call needs a matching preview first. A call without one is refused."


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def sandbox() -> Iterator[None]:
    set_sandbox_mode_active(True)
    yield
    set_sandbox_mode_active(False)


@pytest.fixture
def a_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cowapi.vm.update`` is a step of one multi-step operation."""
    monkeypatch.setitem(
        governed_subops._REGISTRY,
        "cowapi.composite.rebuild",
        GovernedSubopSurface(connector_id=_CONNECTOR, sub_op_ids=("cowapi.vm.update",)),
    )


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _register(embedding: AsyncMock, op_id: str, group: str, level: Any) -> None:
    await register_typed_operation(
        product="cowapi",
        version="1.x",
        impl_id="cowapi",
        op_id=op_id,
        handler=_handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema={"type": "object", "properties": {}},
        group_key=group,
        when_to_use="Fixture group.",
        safety_level=level,
        embedding_service=embedding,
    )


async def _override(op_id: str, tenant_id: uuid.UUID | None, *, approval: bool | None) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            OperationPolicyOverride(
                tenant_id=tenant_id,
                product="cowapi",
                version="1.x",
                impl_id="cowapi",
                op_id=op_id,
                requires_approval=approval,
                category=None if approval is not None else "compute",
                set_by_sub="admin-a",
            )
        )
        await session.commit()


async def _policy(tenant_id: uuid.UUID | None, classifications: list[dict[str, Any]]) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TenantPolicy(
                tenant_id=tenant_id,
                document={"classifications": classifications},
                set_by_sub="admin-a",
            )
        )
        await session.commit()


async def _setup(embedding: AsyncMock) -> None:
    """Three operations, overrides and policy files in three scopes."""
    await _register(embedding, "cowapi.vm.list", "compute", "safe")
    await _register(embedding, "cowapi.vm.update", "compute", "caution")
    await _register(embedding, "cowapi.disk.list", "storage", "safe")
    await _override("cowapi.vm.update", _TENANT, approval=True)
    await _override("cowapi.disk.list", _TENANT, approval=None)
    await _override("cowapi.vm.list", None, approval=True)
    await _override("cowapi.disk.list", _OTHER_TENANT, approval=True)
    await _policy(
        _TENANT,
        [
            {"match": {"connector": "cowapi-*", "group": "compute"}, "always_wait": True},
            {"match": {"op": "cowapi.vm.list"}, "always_wait": False},
            {"match": {"op": "nothing.*"}, "always_wait": True},
            {"match": {"op": "cowapi.disk.list"}, "category": "storage"},
        ],
    )
    await _policy(None, [{"match": {"group": "storage"}, "always_wait": True}])
    await _policy(_OTHER_TENANT, [{"match": {"op": "cowapi.*"}, "always_wait": True}])


def _token(key: Any, *, sub: str = "admin-a", **claims: Any) -> str:
    claims.setdefault("tenant_role", TenantRole.TENANT_ADMIN.value)
    return mint_token(key, sub=sub, tenant_id=str(_TENANT), **claims)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _dry_run(client: TestClient, key: Any, scope: str | None = None, **claims: Any) -> Any:
    params = {"scope": scope} if scope else None
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        return client.get(_ROUTE, params=params, headers=_auth(_token(key, **claims)))


def _op(op_id: str, *step_of: str) -> dict[str, Any]:
    return {"connector_id": _CONNECTOR, "op_id": op_id, "step_of": list(step_of)}


async def _audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        stmt = select(AuditLog).where(AuditLog.path == _ROUTE).order_by(AuditLog.occurred_at)
        return list((await session.execute(stmt)).scalars())


async def _snapshot() -> list[tuple[Any, ...]]:
    async with get_sessionmaker()() as session:
        overrides = (await session.execute(select(OperationPolicyOverride))).scalars()
        policies = (await session.execute(select(TenantPolicy))).scalars()
        requests = (await session.execute(select(ApprovalRequest))).scalars()
        return sorted(
            [
                *(
                    ("override", o.id, o.requires_approval, o.safety_level, o.set_at)
                    for o in overrides
                ),
                *(("policy", p.id, repr(p.document), p.set_at) for p in policies),
                *(("request", r.id, r.status) for r in requests),
            ],
            key=repr,
        )


# ---------------------------------------------------------------------------
# What the dry run lists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox", "a_step")
async def test_the_dry_run_lists_each_saved_always_wait_of_the_tenant(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    resp = _dry_run(client, make_rsa_keypair("kid-co-list"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["scope"], body["sandbox_mode"], body["message"]) == (
        "tenant",
        True,
        DRY_RUN_MESSAGE,
    )
    override, group_entry, no_match = body["settings"]

    assert override["source"] == "override" and override["scope"] == "tenant"
    assert override["ref"] == "tenant:override"
    assert override["match"] == {"connector": _CONNECTOR, "op": "cowapi.vm.update"}
    assert override["operations"] == [_op("cowapi.vm.update", "cowapi.composite.rebuild")]
    step, in_sandbox, preview = override["warnings"]
    assert "step of `cowapi.composite.rebuild`" in step and "refused" in step
    assert "sandbox mode" in in_sandbox
    assert preview == _PREVIEW

    # The group entry: vm.list has a more specific `always_wait: false`.
    assert group_entry["source"] == "policy_file" and group_entry["scope"] == "tenant"
    assert group_entry["ref"] == "tenant:classifications[0]"
    assert group_entry["match"] == {"connector": "cowapi-*", "group": "compute"}
    assert group_entry["operations"] == [_op("cowapi.vm.update", "cowapi.composite.rebuild")]
    assert len(group_entry["warnings"]) == 3

    assert no_match["ref"] == "tenant:classifications[2]"
    assert no_match["operations"] == []
    assert no_match["warnings"][0] == "Today this entry makes no operation wait."
    assert no_match["warnings"][-1] == _PREVIEW


@pytest.mark.asyncio
async def test_outside_sandbox_mode_and_without_steps_only_the_preview_warns(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    body = _dry_run(client, make_rsa_keypair("kid-co-plain")).json()
    assert body["sandbox_mode"] is False
    override = body["settings"][0]
    assert override["operations"] == [_op("cowapi.vm.update")]
    assert override["warnings"] == [_PREVIEW]


@pytest.mark.asyncio
async def test_a_tenant_without_saved_settings_gets_an_empty_list(client: TestClient) -> None:
    body = _dry_run(client, make_rsa_keypair("kid-co-empty")).json()
    assert body["settings"] == []
    assert body["message"] == DRY_RUN_MESSAGE


@pytest.mark.asyncio
async def test_a_stored_policy_that_is_not_valid_is_409(client: TestClient) -> None:
    await _policy(_TENANT, [{"match": "everything", "always_wait": True}])
    resp = _dry_run(client, make_rsa_keypair("kid-co-bad"))
    assert resp.status_code == 409
    assert "Import a fixed policy file." in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Who may read it (whole path: token, gate, audit row)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_who_may_read_it_is_the_rule_of_the_export(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-co-who")

    tenant = _dry_run(client, key).json()
    refs = {(s["scope"], s["ref"], s["match"].get("op")) for s in tenant["settings"]}
    assert all(scope == "tenant" for scope, _, _ in refs)
    assert ("tenant", "tenant:override", "cowapi.disk.list") not in refs

    assert _dry_run(client, key, "install").status_code == 403
    operator = _dry_run(client, key, tenant_role=TenantRole.OPERATOR.value)
    assert operator.status_code == 403
    assert operator.json()["detail"] == "insufficient_role"
    machine = _dry_run(client, key, "install", platform_admin=True, principal_kind="service")
    assert machine.status_code == 403

    install = _dry_run(client, key, "install", sub="root-a", platform_admin=True).json()
    assert [(s["source"], s["scope"], s["ref"], s["match"]) for s in install["settings"]] == [
        (
            "override",
            "install",
            "install:override",
            {"connector": _CONNECTOR, "op": "cowapi.vm.list"},
        ),
        ("policy_file", "install", "install:classifications[0]", {"group": "storage"}),
    ]
    assert install["settings"][1]["operations"] == [_op("cowapi.disk.list")]

    rows = await _audit_rows()
    assert [(row.operator_sub, row.status_code) for row in rows] == [
        ("admin-a", 200),
        ("admin-a", 403),
        ("admin-a", 403),
        ("admin-a", 403),
        ("root-a", 200),
    ]
    assert {row.method for row in rows} == {"GET"}
    # The role gate refuses the operator before the route names the read.
    assert [row.payload.get("op_id") for row in rows if row.status_code == 200] == [
        _AUDIT_OP,
        _AUDIT_OP,
    ]


def test_the_dry_run_needs_a_token(client: TestClient) -> None:
    assert client.get(_ROUTE).status_code == 401


# ---------------------------------------------------------------------------
# It writes nothing, and an override with approval on keeps waiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox", "a_step")
async def test_the_dry_run_changes_no_row(client: TestClient, embedding: AsyncMock) -> None:
    await _setup(embedding)
    before = await _snapshot()
    key = make_rsa_keypair("kid-co-nowrite")
    assert _dry_run(client, key).status_code == 200
    assert _dry_run(client, key, "install", platform_admin=True).status_code == 200
    assert await _snapshot() == before


def _call(client: TestClient, key: Any) -> dict[str, Any]:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/operations/call",
            json={"connector_id": _CONNECTOR, "op_id": "cowapi.vm.update", "params": {}},
            headers=_auth(_token(key, sub="op-a", tenant_role=TenantRole.OPERATOR.value)),
        )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("custom_policy", ["false", "true"])
async def test_an_override_with_approval_on_keeps_waiting(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    custom_policy: str,
) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", custom_policy)
    get_settings.cache_clear()
    await _register(embedding, "cowapi.vm.update", "compute", "caution")
    await _override("cowapi.vm.update", _TENANT, approval=True)
    key = make_rsa_keypair("kid-co-wait")

    first = _call(client, key)
    assert _dry_run(client, key).status_code == 200
    second = _call(client, key)

    assert first["status"] == second["status"] == "awaiting_approval"
    async with get_sessionmaker()() as session:
        requests = list((await session.execute(select(ApprovalRequest))).scalars())
        calls = [
            row
            for row in (await session.execute(select(AuditLog))).scalars()
            if (row.payload or {}).get("op_id") == "cowapi.vm.update"
        ]
    assert [r.op_id for r in requests] == ["cowapi.vm.update", "cowapi.vm.update"]
    assert {r.status for r in requests} == {"pending"}
    assert len(calls) == 2
    assert {row.operator_sub for row in calls} == {"op-a"}
    assert {(row.path, row.status_code) for row in calls} == {("approval.request", 202)}
    assert {row.payload["approval_request_id"] for row in calls} == {str(r.id) for r in requests}
