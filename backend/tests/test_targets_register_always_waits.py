# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Making a new target, and changing one, always waits for a person.

* **Each kind of caller** -- a service login with a standing grant on
  ``targets.register``, a service login inside a run with a run approval
  that lists it, an agent with a grant that allows it, and a person with a
  token: each call waits, makes one approval request and writes no target.
  After a person approves, the target exists with the values the approval
  card showed.
* **The approval card** of ``targets.register`` shows the name, product,
  host, ``fqdn``, port, the login reference (``secret_ref``, the default one
  when the call leaves it out) and every ``extras`` key and value; a value
  under a secret-like name is hidden.
* **``extras``** -- a ``targets.update`` call that changes only ``extras``
  waits too, also under a standing grant, and its card shows the old and the
  new ``extras`` with secret-like values hidden.
* **Guard** -- every operation of the target registry that makes or changes
  a target needs approval, and no standing grant, run approval or agent
  grant can cover it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.targets.ops import (
    TARGETS_REGISTER_OP_ID,
    TARGETS_REGISTRY_CONNECTOR_ID,
    register_targets_registry_operations,
)
from meho_backplane.connectors.targets.update import (
    TARGETS_UPDATE_OP_ID,
    register_targets_update_operation,
)
from meho_backplane.connectors.vault.tenant_paths import tenant_secret_ref
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    EndpointDescriptor,
    RunApproval,
    ServicePrincipalGrant,
    Tenant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.service_grants import never_covered_reason
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import DEFAULT_TENANT_ID
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._targets_helpers import _insert_target

_TENANT_ID = uuid.UUID(DEFAULT_TENANT_ID)
_SERVICE_SUB = "svc:onboarding"
_AGENT_SUB = "agent:onboarder"
_RUN = "run-onboarding"
_SECRET = "made-up-value-1"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()


@pytest_asyncio.fixture(autouse=True)
async def _registered(_settings_env: None) -> AsyncIterator[None]:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_targets_registry_operations(embedding_service=embedding)
    await register_targets_update_operation(embedding_service=embedding)
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=_TENANT_ID, slug="tenant-a", name="Tenant A"))
    yield


def _operator(
    sub: str,
    *,
    kind: PrincipalKind = PrincipalKind.USER,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    web_session: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT_ID,
        tenant_role=role,
        principal_kind=kind,
        web_session=web_session,
    )


def _service() -> Operator:
    return _operator(_SERVICE_SUB, kind=PrincipalKind.SERVICE)


def _agent() -> Operator:
    return _operator(_AGENT_SUB, kind=PrincipalKind.AGENT, role=TenantRole.OPERATOR)


async def _standing_grant(op_id: str) -> None:
    """A standing grant row, written straight to the table (as one made earlier)."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                principal_sub=_SERVICE_SUB,
                op_id=op_id,
                connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
                target_id=None,
                reason="test: onboarding service",
                created_by_sub="admin-person",
            )
        )


async def _run_approval() -> None:
    """A live run approval that lists ``targets.register``, straight to the table."""
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            RunApproval(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                principal_sub=_SERVICE_SUB,
                work_ref=_RUN,
                entries=[
                    {"connector_id": TARGETS_REGISTRY_CONNECTOR_ID, "op_id": TARGETS_REGISTER_OP_ID}
                ],
                reason="test: a run that registers targets",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="admin-person",
            )
        )


async def _agent_grant() -> None:
    """A grant that lets the agent run ``targets.register`` at once."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            AgentPermission(
                tenant_id=_TENANT_ID,
                principal_sub=_AGENT_SUB,
                op_pattern=TARGETS_REGISTER_OP_ID,
                target_scope="*",
                verdict="auto-execute",
                created_by_sub="admin-person",
            )
        )


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


async def _target(name: str) -> TargetORM | None:
    async with get_sessionmaker()() as session:
        return await session.scalar(select(TargetORM).where(TargetORM.name == name))


async def _approve_and_run(request_id: uuid.UUID) -> Any:
    approver = _operator("approver-person", web_session=True)
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=approver)
        await session.commit()
    return await resume_dispatch_after_approval(operator=approver, request=request)


def _new_target(name: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "product": "vault",
        "host": f"{name}.example",
        "fqdn": f"{name}.example",
        "port": 8200,
        "extras": {"file_hosts": ["files.example"]},
        **extra,
    }


async def _register(operator: Operator, params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=operator,
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=TARGETS_REGISTER_OP_ID,
        target=None,
        params=params,
    )


async def _service_with_grant() -> Any:
    await _standing_grant(TARGETS_REGISTER_OP_ID)
    return await _register(_service(), _new_target("svc-granted"))


async def _service_in_approved_run() -> Any:
    await _run_approval()
    token = work_ref_var.set(_RUN)
    try:
        return await _register(_service(), _new_target("svc-run"))
    finally:
        work_ref_var.reset(token)


async def _agent_with_grant() -> Any:
    await _agent_grant()
    return await _register(_agent(), _new_target("agent-granted"))


async def _person_with_token() -> Any:
    return await _register(_operator("person-token"), _new_target("person-token"))


@pytest.mark.parametrize(
    ("call", "name"),
    [
        (_service_with_grant, "svc-granted"),
        (_service_in_approved_run, "svc-run"),
        (_agent_with_grant, "agent-granted"),
        (_person_with_token, "person-token"),
    ],
    ids=["service-standing-grant", "service-run-approval", "agent-grant", "person-token"],
)
async def test_every_caller_waits_and_the_approved_target_has_the_card_values(
    call: Any, name: str
) -> None:
    result = await call()

    assert result.status == "awaiting_approval", result
    (request,) = await _requests()
    assert request.op_id == TARGETS_REGISTER_OP_ID
    assert await _target(name) is None
    assert request.proposed_effect is not None
    card = request.proposed_effect["preview"]["target"]
    assert card == {**_new_target(name), "secret_ref": tenant_secret_ref(_TENANT_ID, name)}
    assert request.proposed_effect["preview"]["secret_ref_derived"] is True

    ran = await _approve_and_run(request.id)

    assert ran.status == "ok", ran
    row = await _target(name)
    assert row is not None
    stored = {
        key: getattr(row, key) for key in ("name", "product", "host", "fqdn", "port", "secret_ref")
    }
    assert stored == {key: card[key] for key in stored}
    assert row.extras == card["extras"]


async def test_the_card_shows_a_given_secret_ref_as_sent() -> None:
    ref = tenant_secret_ref(_TENANT_ID, "shared-login")

    result = await _register(_operator("person-token"), _new_target("given-ref", secret_ref=ref))

    assert result.status == "awaiting_approval", result
    (request,) = await _requests()
    assert request.proposed_effect is not None
    assert request.proposed_effect["preview"]["target"]["secret_ref"] == ref
    assert request.proposed_effect["preview"]["secret_ref_derived"] is False


async def test_the_card_hides_a_secret_like_extras_value() -> None:
    params = _new_target(
        "with-secret", extras={"file_hosts": ["files.example"], "password": _SECRET}
    )

    result = await _register(_operator("person-token"), params)

    assert result.status == "awaiting_approval", result
    (request,) = await _requests()
    assert request.proposed_effect is not None
    card = request.proposed_effect["preview"]
    assert card["target"]["extras"]["file_hosts"] == ["files.example"]
    assert card["target"]["extras"]["password"] != _SECRET
    assert _SECRET not in str(request.proposed_effect)
    assert card["result_if_run"] == "created"


async def test_the_card_says_a_taken_name_fails() -> None:
    await _insert_target(tenant_id=_TENANT_ID, name="taken", host="taken.example")

    result = await _register(_operator("person-token"), _new_target("taken"))

    assert result.status == "awaiting_approval", result
    (request,) = await _requests()
    assert request.proposed_effect is not None
    assert request.proposed_effect["preview"]["result_if_run"] == "fails"


async def test_an_extras_only_change_waits_under_a_standing_grant() -> None:
    row = await _insert_target(
        tenant_id=_TENANT_ID,
        name="avi-01",
        host="avi-01.example",
        extras={"file_hosts": ["old.example"], "token": _SECRET},
    )
    await _standing_grant(TARGETS_UPDATE_OP_ID)
    new_extras = {"file_hosts": ["new.example"], "token": "made-up-value-2"}

    result = await dispatch(
        operator=_service(),
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=TARGETS_UPDATE_OP_ID,
        target=None,
        params={"target_id": str(row.id), "extras": new_extras},
    )

    assert result.status == "awaiting_approval", result
    (request,) = await _requests()
    stored = await _target("avi-01")
    assert stored is not None and stored.extras == row.extras
    assert request.proposed_effect is not None
    change = request.proposed_effect["preview"]["changes"]["extras"]
    assert change["from"]["file_hosts"] == ["old.example"]
    assert change["to"]["file_hosts"] == ["new.example"]
    assert _SECRET not in str(request.proposed_effect)
    assert "made-up-value-2" not in str(request.proposed_effect)


async def _target_writes() -> list[EndpointDescriptor]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.product == "targets")
            )
        ).scalars()
        return [row for row in rows if "write" in (row.tags or [])]


async def test_no_grant_or_run_approval_covers_an_op_that_makes_or_changes_a_target() -> None:
    writes = await _target_writes()

    assert {TARGETS_REGISTER_OP_ID, TARGETS_UPDATE_OP_ID} <= {row.op_id for row in writes}
    for row in writes:
        # requires_approval keeps an agent grant from running the call at once.
        assert row.requires_approval is True, row.op_id
        # never_covered_reason keeps standing grants and run approvals off it.
        assert never_covered_reason(row) is not None, row.op_id
