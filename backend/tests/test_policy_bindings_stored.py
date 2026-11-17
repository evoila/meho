# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Stored bindings: after an import, and together with approver routing.

* ``meho policy export`` with the switch on writes the shipped bindings
  into a tenant's file. After an import they are stored bindings: the
  group alone then gives the kind, also for a login with a tenant list.
* A binding sets the kind that the routing checks at the click.

See :mod:`meho_backplane.policy.bindings` and
:mod:`meho_backplane.policy.routing`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import delete

from meho_backplane.auth.operator import Operator, RoleGroup, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus, TenantPolicy
from meho_backplane.operations.approval_queue import ApprovalRoutingError
from meho_backplane.operations.approval_rights import ApproveRightReason, approve_right
from meho_backplane.policy.bindings import bind_current_tenant
from meho_backplane.policy.document import document_of, parse_policy_file
from meho_backplane.policy.service import export_policy
from meho_backplane.settings import get_settings
from tests.test_api_v1_approvals_fresh_sign_in import (
    _TENANT_A,
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_group_rules import _OTHER, _person
from tests.test_policy_routing import _approve, _load, _park

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006483")


@pytest.fixture(autouse=True)
def _switch_on(_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.delenv("JWT_ROLE_GROUPS", raising=False)
    monkeypatch.delenv("JWT_SELF_APPROVER_GROUP", raising=False)
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true")
    get_settings.cache_clear()


async def _store(tenant_id: uuid.UUID, document: dict[str, Any] | None) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(delete(TenantPolicy).where(TenantPolicy.tenant_id == tenant_id))
        if document is not None:
            session.add(TenantPolicy(tenant_id=tenant_id, document=document, set_by_sub="admin-a"))


def _listed_operator() -> Operator:
    return Operator(
        sub="person-1",
        raw_jwt="raw.jwt.token",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.mark.asyncio
async def test_exported_shipped_bindings_count_for_a_listed_login_after_the_import() -> None:
    """A login with a tenant list, entry ``Operators``, in the shipped self-approval group."""
    claims = {
        "meho_tenants": [f"{_TENANT}:tenant-a:Operators"],
        "groups": ["/MEHO-SA-Operators"],
    }
    await _store(_TENANT, None)
    try:
        before = await bind_current_tenant(_listed_operator(), claims)
        assert before.bound_kind is None
        assert before.self_approver is False

        async with get_sessionmaker()() as session:
            exported = await export_policy(session, scope="tenant", tenant_id=_TENANT)
        assert "group: /MEHO-SA-Operators" in exported
        parsed = parse_policy_file(exported.encode())
        assert parsed.mistakes == [] and parsed.model is not None
        await _store(_TENANT, document_of(parsed.model))

        after = await bind_current_tenant(_listed_operator(), claims)
        assert after.bound_kind is RoleGroup.SA_OPERATORS
        assert after.self_approver is True
    finally:
        await _store(_TENANT, None)


_BOUND_AND_ROUTED: dict[str, Any] = {
    "classifications": [],
    "bindings": [
        {
            "match": {"group": "/MEHO-Operators/Network"},
            "kind": "SA-Operators",
            "categories": ["network"],
        }
    ],
    "routing": [{"match": {"category": "network"}, "approvers": ["SA-Operators"]}],
}


@pytest.mark.asyncio
async def test_a_binding_sets_the_kind_that_the_routing_checks() -> None:
    await _store(_TENANT_A, _BOUND_AND_ROUTED)
    try:
        bound = await bind_current_tenant(_person(_OTHER), {"groups": ["/MEHO-Operators/Network"]})
        assert bound.bound_kind is RoleGroup.SA_OPERATORS
        not_bound = await bind_current_tenant(
            _person("person-third"), {"groups": ["/MEHO-Operators"]}
        )
        assert not_bound.bound_kind is None

        request_id = await _park("caution")
        row = await _load(request_id)
        assert row.approver_roles == ["SA-Operators"]
        right = approve_right(not_bound, row)
        assert (right.may_approve, right.reason) == (False, ApproveRightReason.NOT_ROUTED)
        with pytest.raises(ApprovalRoutingError):
            await _approve(request_id, not_bound)

        assert approve_right(bound, row).may_approve is True
        approved = await _approve(request_id, bound)
        assert approved.status == ApprovalRequestStatus.APPROVED.value
    finally:
        await _store(_TENANT_A, None)
