# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A person approves each call of the Keycloak writes that give rights.

* The Keycloak connector marks these writes for per-call approval when
  it is imported.
* Creating a standing grant for any of them is refused.
* A grant row stored some other way (for example an older one) does not
  clear a call of a service login: the call still waits for a person.
* A run approval cannot list them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import PermissionVerdict, ServicePrincipalGrant
from meho_backplane.op_classes import needs_per_call_approval
from meho_backplane.operations import reset_dispatcher_caches
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

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006238")
_CONNECTOR = "keycloak-admin-26.x"
_SERVICE_SUB = "svc:keycloak-account-writer"
_OPS = (
    "keycloak.role_mapping.assign",
    "keycloak.role_mapping.client.assign",
    "keycloak.role_mapping.client.remove",
    "keycloak.user.update",
    "keycloak.user.credential.delete",
    "keycloak.group.member.add",
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    get_settings.cache_clear()


@pytest.mark.parametrize("op_id", _OPS)
def test_account_write_is_marked_at_import(op_id: str) -> None:
    assert needs_per_call_approval(op_id)


@pytest.mark.parametrize("op_id", _OPS)
async def test_no_new_grant_and_no_run_approval(op_id: str) -> None:
    await KeycloakConnector.register_operations()

    with pytest.raises(GrantValidationError, match="a person approves each call"):
        await ServicePrincipalGrantService().create(
            _TENANT,
            "fixture-admin",
            ServiceGrantCreate(
                principal_sub=_SERVICE_SUB,
                op_id=op_id,
                connector_id=_CONNECTOR,
                target_id=None,
                reason=f"fixture: try to grant {op_id}",
                expires_at=None,
            ),
        )
    refusal = await _entry_refusal(_TENANT, RunApprovalEntry(connector_id=_CONNECTOR, op_id=op_id))
    assert refusal is not None


@pytest.mark.parametrize("op_id", _OPS)
async def test_an_older_grant_row_does_not_clear_a_call(op_id: str) -> None:
    await KeycloakConnector.register_operations()
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE_SUB,
                op_id=op_id,
                connector_id=_CONNECTOR,
                target_id=None,
                reason="fixture: made before the per-call mark",
                created_by_sub="fixture-admin",
            )
        )
        await session.commit()

    descriptor = await lookup_descriptor(
        tenant_id=_TENANT, product="keycloak", version="26.x", impl_id="keycloak-admin", op_id=op_id
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
