# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A standing grant for an op with a role floor needs a person with that role.

``targets.register`` and the ``topology.*`` writes need ``tenant_admin``
from the person who runs or approves them. A standing grant lets a
service principal run the op with no approval, so the person who makes
the grant needs the same role:

* REST: a person with the ``operator`` role gets 403
  ``role_floor_required`` and no grant is written. A tenant admin makes it.
* An op without a floor keeps the ``operator`` role.
* The service checks the floor on every path through its review: create
  with no role given, and the renewal of delete grants.
"""

from __future__ import annotations

from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import TenantRole

# Importing the two ops modules registers their role floors.
from meho_backplane.connectors.targets.ops import (
    TARGETS_REGISTER_OP_ID,
    TARGETS_REGISTRY_CONNECTOR_ID,
)
from meho_backplane.connectors.topology.ops import (
    TOPOLOGY_ANNOTATE_OP_ID,
    TOPOLOGY_BULK_IMPORT_OP_ID,
    TOPOLOGY_CREATE_NODE_OP_ID,
    TOPOLOGY_DELETE_NODE_OP_ID,
    TOPOLOGY_GRAPH_CONNECTOR_ID,
    TOPOLOGY_UNANNOTATE_OP_ID,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import SandboxDeleteGrant, ServicePrincipalGrant
from meho_backplane.operations.role_floors import role_floor_for
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantRoleFloorError,
    ServicePrincipalGrantService,
)
from tests._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from tests._sandbox_mode_helpers import rows_in, seed_delete_grant, soon, turn_sandbox_mode_on
from tests.test_api_v1_service_grants import (
    _BASE,
    _TENANT_A,
    _settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    _token,
    client,  # noqa: F401 — pytest-discovered fixture
)

_PRINCIPAL = "svc:deploy-bot"

_FLOORED: tuple[tuple[str, str], ...] = (
    (TARGETS_REGISTER_OP_ID, TARGETS_REGISTRY_CONNECTOR_ID),
    *(
        (op_id, TOPOLOGY_GRAPH_CONNECTOR_ID)
        for op_id in (
            TOPOLOGY_ANNOTATE_OP_ID,
            TOPOLOGY_CREATE_NODE_OP_ID,
            TOPOLOGY_DELETE_NODE_OP_ID,
            TOPOLOGY_UNANNOTATE_OP_ID,
            TOPOLOGY_BULK_IMPORT_OP_ID,
        )
    ),
)


def _body(op_id: str, connector_id: str) -> dict[str, Any]:
    return {
        "principal_sub": _PRINCIPAL,
        "op_id": op_id,
        "connector_id": connector_id,
        "reason": "unattended build",
    }


def _create(client: TestClient, role: TenantRole, body: dict[str, Any]) -> Any:  # noqa: F811
    key = make_rsa_keypair("kid-floor")
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=role)}"}
        return client.post(_BASE, headers=headers, json=body)


async def _grant_ops() -> list[str]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(ServicePrincipalGrant.op_id))
        return sorted(rows.scalars().all())


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "connector_id"), _FLOORED)
async def test_an_operator_cannot_grant_an_op_with_a_role_floor(
    client: TestClient,  # noqa: F811
    op_id: str,
    connector_id: str,
) -> None:
    response = _create(client, TenantRole.OPERATOR, _body(op_id, connector_id))

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail.startswith("role_floor_required: "), detail
    assert f"{op_id} needs the tenant_admin role; your role is operator" in detail
    assert await _grant_ops() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "connector_id"), _FLOORED)
async def test_a_tenant_admin_grants_an_op_with_a_role_floor(
    client: TestClient,  # noqa: F811
    op_id: str,
    connector_id: str,
) -> None:
    response = _create(client, TenantRole.TENANT_ADMIN, _body(op_id, connector_id))

    assert response.status_code == 201, response.text
    assert await _grant_ops() == [op_id]


@pytest.mark.asyncio
async def test_an_operator_still_grants_an_op_without_a_floor(
    client: TestClient,  # noqa: F811
) -> None:
    body = _body("vmware.composite.vm.create", "vmware-rest-9.0")
    response = _create(client, TenantRole.OPERATOR, body)

    assert response.status_code == 201, response.text
    assert await _grant_ops() == ["vmware.composite.vm.create"]


@pytest.mark.asyncio
async def test_the_service_holds_a_caller_without_a_role_to_the_floor() -> None:
    payload = ServiceGrantCreate.model_validate(
        _body(TARGETS_REGISTER_OP_ID, TARGETS_REGISTRY_CONNECTOR_ID)
    )
    with pytest.raises(GrantRoleFloorError, match="needs the tenant_admin role"):
        await ServicePrincipalGrantService().create(_TENANT_A, "person-1", payload)

    assert await _grant_ops() == []


@pytest.mark.asyncio
async def test_a_renewal_checks_the_floor_of_each_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_on(monkeypatch)
    old_id = await seed_delete_grant(
        tenant_id=_TENANT_A,
        principal_sub=_PRINCIPAL,
        op_id=TOPOLOGY_DELETE_NODE_OP_ID,
        connector_id=TOPOLOGY_GRAPH_CONNECTOR_ID,
        expires_at=soon(2),
    )

    with pytest.raises(GrantRoleFloorError, match="needs the tenant_admin role"):
        await ServicePrincipalGrantService().renew_delete_grants(
            _TENANT_A,
            _PRINCIPAL,
            "person-1",
            expires_at=soon(20),
            from_console=True,
            creator_role=TenantRole.OPERATOR,
        )

    assert await rows_in(SandboxDeleteGrant) == 1
    async with get_sessionmaker()() as session:
        old = await session.get(SandboxDeleteGrant, old_id)
    assert old is not None
    assert old.revoked_at is None


def test_floored_ops_are_registered() -> None:
    assert {role_floor_for(op_id) for op_id, _ in _FLOORED} == {TenantRole.TENANT_ADMIN}
