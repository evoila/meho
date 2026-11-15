# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A person approves each ``secret.move``.

* The secret broker marks ``secret.move`` for per-call approval when it is
  imported.
* Creating a standing grant for it is refused.
* A grant row stored some other way (for example an older one) does not
  clear a call of a service login: the call still waits for a person.
* A run approval cannot list it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest

import meho_backplane.connectors.secret  # noqa: F401 -- declares the per-call mark
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import PermissionVerdict, ServicePrincipalGrant
from meho_backplane.op_classes import needs_per_call_approval
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.run_approval_schemas import RunApprovalEntry
from meho_backplane.operations.run_approvals import _entry_refusal
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006235")
_CONNECTOR = "secret-broker-1.x"
_OP = "secret.move"
_SERVICE_SUB = "svc:secret-mover"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def test_secret_move_is_marked_at_import() -> None:
    assert needs_per_call_approval(_OP)


@pytest.mark.asyncio
async def test_no_new_grant_and_no_run_approval_for_secret_move(embedding: AsyncMock) -> None:
    await register_secret_broker_operations(embedding_service=embedding)

    with pytest.raises(GrantValidationError, match="a person approves each call"):
        await ServicePrincipalGrantService().create(
            _TENANT,
            "fixture-admin",
            ServiceGrantCreate(
                principal_sub=_SERVICE_SUB,
                op_id=_OP,
                connector_id=_CONNECTOR,
                target_id=None,
                reason="fixture: try to grant secret.move",
                expires_at=None,
            ),
        )
    refusal = await _entry_refusal(_TENANT, RunApprovalEntry(connector_id=_CONNECTOR, op_id=_OP))
    assert refusal is not None


@pytest.mark.asyncio
async def test_an_older_grant_row_does_not_clear_a_move(embedding: AsyncMock) -> None:
    await register_secret_broker_operations(embedding_service=embedding)
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE_SUB,
                op_id=_OP,
                connector_id=_CONNECTOR,
                target_id=None,
                reason="fixture: made before the per-call mark",
                created_by_sub="fixture-admin",
            )
        )
        await session.commit()

    descriptor = await lookup_descriptor(
        tenant_id=_TENANT, product="secret", version="1.x", impl_id="secret-broker", op_id=_OP
    )
    assert descriptor is not None
    service = Operator(
        sub=_SERVICE_SUB,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )
    verdict, _reason = await policy_gate(
        operator=service, descriptor=descriptor, target=None, connector_id=_CONNECTOR
    )
    assert verdict is PermissionVerdict.NEEDS_APPROVAL
