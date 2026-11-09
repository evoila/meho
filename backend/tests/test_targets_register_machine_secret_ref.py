# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``targets.register``: the credential path a machine login may give a new target.

A service login whose standing grant covers ``targets.register`` runs the
call at once. Such a call may use only the new target's own credential
path (``tenants/<tenant_id>/<name>``) or no path. MEHO refuses:

* the path of another target of the tenant;
* the own path when another target, live or removed, already uses it.

A person's call, and a machine login's call that a person approved, may
use any path inside the tenant. See
:mod:`meho_backplane.connectors.targets.register_secret_ref`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.targets.ops import (
    TARGETS_REGISTER_OP_ID,
    TARGETS_REGISTRY_CONNECTOR_ID,
    targets_register,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    PermissionVerdict,
    ServicePrincipalGrant,
    Tenant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.operations._audit import policy_decision_var
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_SERVICE_SUB = "svc:onboarding"
_OTHER_REF = f"tenants/{OPERATOR_TENANT_ID}/existing-vc"


def _own_ref(name: str) -> str:
    return f"tenants/{OPERATOR_TENANT_ID}/{name}"


def _service_operator() -> Operator:
    return Operator(
        sub=_SERVICE_SUB,
        name="Onboarding",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.SERVICE,
        scopes=frozenset({"mcp:admin"}),
    )


@pytest_asyncio.fixture
async def _seeded() -> AsyncIterator[None]:
    """One tenant, one target with a stored credential, and a grant for the service login."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="Op Tenant"))
        await session.flush()
        session.add(
            TargetORM(
                tenant_id=OPERATOR_TENANT_ID,
                name="existing-vc",
                product="vault",
                host="vault-a.example",
                secret_ref=_OTHER_REF,
            )
        )
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=OPERATOR_TENANT_ID,
                principal_sub=_SERVICE_SUB,
                op_id=TARGETS_REGISTER_OP_ID,
                connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
                target_id=None,
                reason="fixture: onboarding service registers targets",
                created_by_sub="fixture-admin",
            )
        )
    yield


@pytest.fixture
def _service_client() -> Iterator[TestClient]:
    async def _fake_verify() -> Operator:
        return _service_operator()

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _register(client: TestClient, arguments: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho_targets_register", "arguments": arguments},
        },
    ).json()


async def _stored_ref(name: str) -> list[str | None]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(
            select(TargetORM.secret_ref).where(
                TargetORM.tenant_id == OPERATOR_TENANT_ID, TargetORM.name == name
            )
        )
        return list(rows)


async def _set_ref(name: str, secret_ref: str, *, removed: bool) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            TargetORM(
                tenant_id=OPERATOR_TENANT_ID,
                name=name,
                product="vault",
                host="vault-b.example",
                secret_ref=secret_ref,
                deleted_at=datetime.now(UTC) if removed else None,
            )
        )


@pytest.mark.parametrize(
    "secret_ref",
    [_OTHER_REF, f"/{_OTHER_REF}/", f"tenants/{OPERATOR_TENANT_ID}/other-name"],
    ids=["other-target", "other-target-with-slashes", "not-own-path"],
)
async def test_granted_service_login_with_another_path_is_refused(
    _service_client: TestClient, _seeded: None, secret_ref: str
) -> None:
    body = _register(
        _service_client,
        {"name": "new-vc", "product": "vault", "host": "vault-c.example", "secret_ref": secret_ref},
    )

    assert body["error"]["code"] == INVALID_PARAMS
    assert "own credential path" in body["error"]["message"]
    assert await _stored_ref("new-vc") == []


@pytest.mark.parametrize(
    "extra",
    [{}, {"secret_ref": _own_ref("new-vc")}, {"secret_ref": None}],
    ids=["omitted", "own-path", "null"],
)
async def test_granted_service_login_with_own_or_no_path_runs(
    _service_client: TestClient, _seeded: None, extra: dict[str, Any]
) -> None:
    body = _register(
        _service_client,
        {"name": "new-vc", "product": "vault", "host": "vault-c.example", **extra},
    )

    result = body["result"]
    assert result["isError"] is False, body
    assert result["structuredContent"].get("status") != "awaiting_approval"
    expected = extra.get("secret_ref", _own_ref("new-vc"))
    assert await _stored_ref("new-vc") == [expected]


@pytest.mark.parametrize("removed", [False, True], ids=["live", "removed"])
async def test_own_path_of_another_target_is_refused(
    _service_client: TestClient, _seeded: None, removed: bool
) -> None:
    await _set_ref("shared-login", _own_ref("new-vc"), removed=removed)

    body = _register(
        _service_client, {"name": "new-vc", "product": "vault", "host": "vault-c.example"}
    )

    assert body["error"]["code"] == INVALID_PARAMS
    assert "belongs to another target" in body["error"]["message"]
    assert await _stored_ref("new-vc") == []


@pytest.mark.parametrize(
    ("kind", "verdict"),
    [
        (PrincipalKind.SERVICE, PermissionVerdict.NEEDS_APPROVAL.value),
        (PrincipalKind.AGENT, PermissionVerdict.NEEDS_APPROVAL.value),
        (PrincipalKind.USER, PermissionVerdict.AUTO_EXECUTE.value),
    ],
    ids=["approved-service", "approved-agent", "person"],
)
async def test_approved_or_person_call_may_use_another_path(
    _seeded: None, kind: PrincipalKind, verdict: str
) -> None:
    operator = _service_operator().model_copy(update={"principal_kind": kind})
    token = policy_decision_var.set(verdict)
    try:
        created = await targets_register(
            operator,
            None,
            {
                "name": "new-vc",
                "product": "vault",
                "host": "vault-c.example",
                "secret_ref": _OTHER_REF,
            },
        )
    finally:
        policy_decision_var.reset(token)

    assert created["secret_ref"] == _OTHER_REF
    assert await _stored_ref("new-vc") == [_OTHER_REF]


@pytest.mark.parametrize(
    "verdict",
    [PermissionVerdict.AUTO_EXECUTE.value, None],
    ids=["auto-execute", "no-verdict"],
)
async def test_machine_call_without_approval_is_refused_at_the_handler(
    _seeded: None, verdict: str | None
) -> None:
    token = policy_decision_var.set(verdict)
    try:
        with pytest.raises(ValueError, match="own credential path"):
            await targets_register(
                _service_operator(),
                None,
                {
                    "name": "new-vc",
                    "product": "vault",
                    "host": "vault-c.example",
                    "secret_ref": _OTHER_REF,
                },
            )
    finally:
        policy_decision_var.reset(token)

    assert await _stored_ref("new-vc") == []
