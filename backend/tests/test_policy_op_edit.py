# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, a risk level / approval edit with a token waits for an Admin's click.

* A PATCH that sets ``safety_level`` or ``requires_approval`` and the
  DELETE of an override answer ``202`` and write nothing. After a click in
  the web console (``POST /ui/approvals/{id}/approve`` after a fresh
  console sign-in) the edit runs, when the card built again at the click
  equals the stored card; else nothing is written.
* Only a platform admin approves an edit that turns approval off.
* An agent login, a service login and a hosted agent run are refused
  before anything waits; an Operator is refused by the role, as today.
* An agent, a service login and a runner never approve.
* With the switch off the routes work as today and the two operations do
  not exist.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy.op_edit import (
    EDIT_NEEDS_PERSON,
    OTHER_TENANT_REFUSAL,
    POLICY_CHANGED_REFUSAL,
    POLICY_OP_EDIT_OP_ID,
    POLICY_OP_REMOVE_OVERRIDE_OP_ID,
    PolicyEditAskRefused,
    register_policy_op_edit_operations,
    request_policy_edit,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationPolicyOverride
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools.connector_admin import _edit_op_handler
from meho_backplane.operations.approval_queue import UnauthorizedApprovalError
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.operations.typed_register import (
    operation_catalog_write,
    register_typed_operation,
)
from meho_backplane.policy.fixed_rules import fixed_rule_for
from meho_backplane.settings import get_settings

from . import _install_limits_helpers as h
from ._install_limits_helpers import (
    client_fixture,  # noqa: F401
    embedding_fixture,  # noqa: F401
    switch_on_fixture,  # noqa: F401
)
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_CONNECTOR = "opedit-1.x"
_RESTART = "opedit.vm.restart"  # caution, no approval in code
_STOP = "opedit.vm.stop"  # dangerous, approval in code
_OFF = {"requires_approval": False, "reason": "read-only in practice"}
_TENANT_B = uuid.UUID("00000000-0000-4000-8000-00000000000b")


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ran": True}


async def _setup(embedding: AsyncMock) -> None:
    await register_policy_op_edit_operations(embedding_service=embedding)
    for op_id, level, approval in ((_RESTART, "caution", False), (_STOP, "dangerous", True)):
        await register_typed_operation(
            product="opedit",
            version="1.x",
            impl_id="opedit",
            op_id=op_id,
            handler=_handler,
            summary="Fixture operation.",
            description="Fixture operation.",
            parameter_schema={"type": "object", "properties": {}},
            group_key="vm",
            when_to_use="Fixture group.",
            safety_level=level,
            requires_approval=approval,
            embedding_service=embedding,
        )


def _token(key: Any, *, sub: str = "admin-a", platform_admin: bool = False, **claims: Any) -> str:
    return h.token(key, sub=sub, platform_admin=platform_admin, **claims)


def _fresh() -> dict[str, Any]:
    return {"extra_claims": {"auth_time": int(time.time())}}


def _patch(client: TestClient, bearer: str, op_id: str, body: dict[str, Any]) -> Any:
    return client.patch(
        f"/api/v1/connectors/{_CONNECTOR}/operations/{op_id}",
        json=body,
        headers={"Authorization": f"Bearer {bearer}"},
    )


def _delete(client: TestClient, bearer: str, op_id: str, **params: str) -> Any:
    return client.delete(
        f"/api/v1/connectors/{_CONNECTOR}/operation-overrides/{op_id}",
        params=params,
        headers={"Authorization": f"Bearer {bearer}"},
    )


async def _overrides(op_id: str) -> list[OperationPolicyOverride]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(OperationPolicyOverride).where(OperationPolicyOverride.op_id == op_id)
        )
        return list(rows.scalars())


async def _description(op_id: str) -> str | None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
    return row.custom_description


async def _raise_now(operator: Operator, op_id: str, level: str) -> None:
    """Write a risk level override at once, as the service does today."""
    await ReviewService(operator).edit_op(
        _CONNECTOR,
        op_id,
        tenant_id=operator.tenant_id,
        safety_level=level,  # type: ignore[arg-type]
    )


def _person_in(tenant_id: uuid.UUID, sub: str, *, platform_admin: bool) -> Operator:
    """A person with a token (no web session) who acts in *tenant_id*."""
    return Operator(
        sub=sub,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        platform_admin=platform_admin,
        raw_jwt="header.payload.signature",
        acr="1",
        auth_time=int(time.time()),
    )


async def _install_approval_off(op_id: str) -> None:
    """A platform admin turns approval off for the whole install, at once."""
    await ReviewService(h.person("platform-c")).edit_op(
        _CONNECTOR, op_id, tenant_id=None, person_present=True, **_OFF
    )


async def _tenant_copy(op_id: str) -> None:
    """Give the tenant its own copy of the built-in row of *op_id*."""
    async with get_sessionmaker()() as session, session.begin():
        built_in = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
        columns = {
            attr.key: getattr(built_in, attr.key)
            for attr in EndpointDescriptor.__mapper__.column_attrs
            if attr.key not in {"id", "tenant_id"}
        }
        session.add(EndpointDescriptor(**columns, tenant_id=h.TENANT))


async def _drop_tenant_copy(op_id: str) -> None:
    async with get_sessionmaker()() as session, session.begin():
        copy = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == op_id, EndpointDescriptor.tenant_id == h.TENANT
                )
            )
        ).scalar_one()
        await session.delete(copy)


async def _enabled_rows(op_id: str) -> list[tuple[uuid.UUID | None, bool]]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(EndpointDescriptor.tenant_id, EndpointDescriptor.is_enabled).where(
                EndpointDescriptor.op_id == op_id
            )
        )
        return [(tenant_id, enabled) for tenant_id, enabled in rows.all()]


def _refused_as_changed(request: Any) -> None:
    assert POLICY_CHANGED_REFUSAL in json.dumps(request.resume_result)


def _waits(response: Any) -> uuid.UUID:
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"status", "approval_request_id", "message"}
    assert body["status"] == "awaiting_approval"
    return uuid.UUID(body["approval_request_id"])


# ---------------------------------------------------------------------------
# A token asks, an Admin clicks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_tenant_admins_raise_with_a_token_waits_then_runs_after_a_click(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-opedit-raise")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        asked = _patch(client, _token(key), _RESTART, {"safety_level": "dangerous"})
        request_id = _waits(asked)
        assert asked.json()["message"].endswith("An Admin approves it in the web console.")
        assert await _overrides(_RESTART) == []
        (request,) = await h.requests()
        assert request.rights_change is True
        card = request.proposed_effect["preview"]
        assert card["scope"] == "tenant"
        assert card["ends_approval"] is False
        assert card["changes"] == {
            "safety_level": {"old": "caution", "new": "dangerous"},
            "requires_approval": {"old": False, "new": False},
        }
        assert card["row_tenant_id"] is None

        clicked = await h.console_approve(
            client, key, request_id, sub="admin-b", platform_admin=False
        )
        assert clicked.status_code == 200, clicked.text
    (override,) = await _overrides(_RESTART)
    assert (override.tenant_id, override.safety_level) == (h.TENANT, "dangerous")
    (run_row,) = await h.audit_rows("DISPATCH", POLICY_OP_EDIT_OP_ID)
    assert run_row.operator_sub == "admin-b"
    async with get_sessionmaker()() as session:
        descriptor = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == POLICY_OP_EDIT_OP_ID)
            )
        ).scalar_one()
    assert fixed_rule_for(descriptor, h.person("admin-a", web=False)) == "policy_change_waits"


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_platform_admins_click_keeps_a_tenant_admins_raise_in_the_tenant(
    client: TestClient, embedding: AsyncMock
) -> None:
    """The override goes to the scope on the card, not to the approver's scope."""
    await _setup(embedding)
    key = make_rsa_keypair("kid-opedit-scope")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id = _waits(_patch(client, _token(key), _RESTART, {"safety_level": "dangerous"}))
        clicked = await h.console_approve(client, key, request_id, sub="platform-b")
        assert clicked.status_code == 200, clicked.text
    (override,) = await _overrides(_RESTART)
    assert (override.tenant_id, override.safety_level) == (h.TENANT, "dangerous")


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_platform_admins_install_raise_needs_a_platform_admins_click(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-opedit-install")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = _token(key, sub="platform-a", platform_admin=True)
        request_id = _waits(_patch(client, bearer, _RESTART, {"safety_level": "dangerous"}))
        (request,) = await h.requests()
        assert request.proposed_effect["preview"]["scope"] == "install"
        refused = await h.console_approve(
            client, key, request_id, sub="admin-b", platform_admin=False
        )
        assert "Only a platform admin may approve it." in refused.text
        assert await _overrides(_RESTART) == []
        clicked = await h.console_approve(client, key, request_id, sub="platform-b")
        assert clicked.status_code == 200, clicked.text
    (override,) = await _overrides(_RESTART)
    assert (override.tenant_id, override.safety_level) == (None, "dangerous")


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_raise_from_the_mcp_tool_waits_and_a_looser_edit_is_refused(
    embedding: AsyncMock,
) -> None:
    await _setup(embedding)
    person = h.person("admin-a", platform_admin=False, web=False)
    answer = await _edit_op_handler(
        person, {"connector_id": _CONNECTOR, "op_id": _RESTART, "safety_level": "dangerous"}
    )
    assert answer["status"] == "awaiting_approval"
    assert answer["approval_request_id"]
    assert await _overrides(_RESTART) == []
    with pytest.raises(McpInvalidParamsError, match="can only make an operation stricter"):
        await _edit_op_handler(
            person, {"connector_id": _CONNECTOR, "op_id": _STOP, "requires_approval": False}
        )
    with pytest.raises(McpInvalidParamsError, match="policy_edit_mixed_clear"):
        await _edit_op_handler(
            person,
            {
                "connector_id": _CONNECTOR,
                "op_id": _RESTART,
                "safety_level": "destructive",
                "custom_description": None,
            },
        )
    assert len(await h.requests()) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_platform_admins_approval_off_from_the_cli_waits_for_a_platform_admin(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-opedit-off")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = _token(key, sub="platform-a", platform_admin=True, **_fresh())
        asked = _patch(client, bearer, _STOP, _OFF)
        request_id = _waits(asked)
        assert asked.json()["message"].endswith("A platform admin approves it in the web console.")
        assert await _overrides(_STOP) == []
        (request,) = await h.requests()
        assert request.proposed_effect["preview"]["ends_approval"] is True

        clicked = await h.console_approve(client, key, request_id, sub="platform-b")
        assert clicked.status_code == 200, clicked.text
    (override,) = await _overrides(_STOP)
    assert (override.tenant_id, override.requires_approval) == (None, False)


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_tenant_admins_approve_of_approval_off_is_refused(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-opedit-off-tenant")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = _token(key, sub="platform-a", platform_admin=True, **_fresh())
        request_id = _waits(_patch(client, bearer, _STOP, _OFF))
        clicked = await h.console_approve(
            client, key, request_id, sub="admin-b", platform_admin=False
        )
    assert "Only a platform admin may approve it." in clicked.text
    assert [r.id for r in await h.pending_requests()] == [request_id]
    assert await _overrides(_STOP) == []
    with pytest.raises(UnauthorizedApprovalError, match="Only a platform admin"):
        await h.approve(request_id, h.person("admin-b", platform_admin=False))
    assert await _overrides(_STOP) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_removal_with_a_token_waits_then_the_override_is_gone(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    await _raise_now(h.person("admin-a", platform_admin=False), _RESTART, "dangerous")
    key = make_rsa_keypair("kid-opedit-remove")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        asked = _delete(client, _token(key), _RESTART, tenant_id=str(h.TENANT))
        request_id = _waits(asked)
        assert len(await _overrides(_RESTART)) == 1
        (request,) = await h.requests()
        assert request.proposed_effect["preview"]["override_removed"] is True
        clicked = await h.console_approve(
            client, key, request_id, sub="admin-b", platform_admin=False
        )
        assert clicked.status_code == 200, clicked.text
    assert await _overrides(_RESTART) == []
    (run_row,) = await h.audit_rows("DISPATCH", POLICY_OP_REMOVE_OVERRIDE_OP_ID)
    assert run_row.operator_sub == "admin-b"


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize("kind", ["agent", "service"])
async def test_an_agent_or_service_login_gets_403_and_no_request(
    client: TestClient, embedding: AsyncMock, kind: str
) -> None:
    await _setup(embedding)
    await _raise_now(h.person("admin-a", platform_admin=False), _RESTART, "dangerous")
    key = make_rsa_keypair(f"kid-opedit-{kind}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = _token(key, principal_kind=kind)
        edited = _patch(client, bearer, _RESTART, {"safety_level": "destructive"})
        removed = _delete(client, bearer, _RESTART, tenant_id=str(h.TENANT))
    assert edited.status_code == 403, edited.text
    assert removed.status_code == 403, removed.text
    assert await h.requests() == []
    (override,) = await _overrides(_RESTART)
    assert override.safety_level == "dangerous"


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_hosted_agent_run_is_refused_and_makes_no_request(embedding: AsyncMock) -> None:
    await _setup(embedding)
    person = h.person("admin-a", platform_admin=False, web=False)
    with actor_delegation("agent:example"):
        with pytest.raises(McpInvalidParamsError, match="policy_edit_needs_person"):
            await _edit_op_handler(
                person, {"connector_id": _CONNECTOR, "op_id": _RESTART, "safety_level": "dangerous"}
            )
        with pytest.raises(PolicyEditAskRefused, match="Only a person"):
            await request_policy_edit(
                person,
                POLICY_OP_REMOVE_OVERRIDE_OP_ID,
                {"connector_id": _CONNECTOR, "op_id": _RESTART, "override_tenant_id": None},
            )
    assert await h.requests() == []
    assert EDIT_NEEDS_PERSON.startswith("Only a person")


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_an_operators_edit_is_refused_as_today(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-opedit-operator")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = _token(key, tenant_role=TenantRole.OPERATOR.value)
        edited = _patch(client, bearer, _RESTART, {"safety_level": "dangerous"})
    assert edited.status_code == 403, edited.text
    assert await h.requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_description_alone_runs_at_once_and_with_a_level_it_waits_as_a_whole(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-opedit-description")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = _token(key, platform_admin=True)
        alone = _patch(client, bearer, _RESTART, {"custom_description": "First text."})
        assert alone.status_code == 200, alone.text
        assert await _description(_RESTART) == "First text."
        assert await h.requests() == []

        both = _patch(
            client,
            bearer,
            _RESTART,
            {"custom_description": "Second text.", "safety_level": "dangerous"},
        )
        request_id = _waits(both)
        assert await _description(_RESTART) == "First text."
        assert await _overrides(_RESTART) == []
        clicked = await h.console_approve(client, key, request_id, sub="platform-b")
        assert clicked.status_code == 200, clicked.text
    assert await _description(_RESTART) == "Second text."
    (override,) = await _overrides(_RESTART)
    assert override.safety_level == "dangerous"


# ---------------------------------------------------------------------------
# Who may approve, and the approved run
# ---------------------------------------------------------------------------


def _machine(kind: PrincipalKind) -> Operator:
    return Operator(
        sub=f"{kind.value}-x",
        tenant_id=h.TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=kind,
        platform_admin=True,
        raw_jwt="header.payload.signature",
        runner_id=uuid.uuid4() if kind is PrincipalKind.RUNNER else None,
        acr="1",
        auth_time=int(time.time()),
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_an_agent_a_service_login_and_a_runner_cannot_approve(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-opedit-machines")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id = _waits(_patch(client, _token(key), _RESTART, {"safety_level": "dangerous"}))
    for kind in (PrincipalKind.AGENT, PrincipalKind.SERVICE, PrincipalKind.RUNNER):
        with pytest.raises(UnauthorizedApprovalError):
            await h.approve(request_id, _machine(kind))
    assert [r.id for r in await h.pending_requests()] == [request_id]
    assert await _overrides(_RESTART) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_the_approved_run_refuses_when_the_policy_changed(
    client: TestClient, embedding: AsyncMock
) -> None:
    """Approval goes on after the request: the click writes nothing."""
    await _setup(embedding)
    key = make_rsa_keypair("kid-opedit-changed")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id = _waits(_patch(client, _token(key), _RESTART, {"safety_level": "dangerous"}))
        await ReviewService(h.person("admin-c", platform_admin=False)).edit_op(
            _CONNECTOR, _RESTART, tenant_id=h.TENANT, requires_approval=True
        )
        await h.console_approve(client, key, request_id, sub="admin-b", platform_admin=False)
    (override,) = await _overrides(_RESTART)
    assert (override.safety_level, override.requires_approval) == (None, True)
    (request,) = await h.requests()
    _refused_as_changed(request)


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_second_waiting_request_is_refused_after_the_first_is_approved(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-opedit-second")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        first = _waits(_patch(client, _token(key), _RESTART, {"safety_level": "dangerous"}))
        second = _waits(_patch(client, _token(key), _RESTART, {"safety_level": "destructive"}))
        clicked = await h.console_approve(client, key, first, sub="admin-b", platform_admin=False)
        assert clicked.status_code == 200, clicked.text
        await h.console_approve(client, key, second, sub="admin-b", platform_admin=False)
    (override,) = await _overrides(_RESTART)
    assert override.safety_level == "dangerous"
    _refused_as_changed(next(r for r in await h.requests() if r.id == second))


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_an_approval_off_kept_by_an_install_override_is_refused_when_it_goes(
    client: TestClient, embedding: AsyncMock
) -> None:
    """The card says approval stays off; the install override goes; the click writes nothing."""
    await _setup(embedding)
    await _install_approval_off(_STOP)
    key = make_rsa_keypair("kid-opedit-install-gone")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id = _waits(_patch(client, _token(key, **_fresh()), _STOP, _OFF))
        (request,) = await h.requests()
        card = request.proposed_effect["preview"]
        assert (card["scope"], card["ends_approval"]) == ("tenant", False)
        await ReviewService(h.person("platform-c")).remove_op_override(
            _CONNECTOR, _STOP, override_tenant_id=None, reason="back to code", person_present=True
        )
        await h.console_approve(client, key, request_id, sub="platform-b")
    assert await _overrides(_STOP) == []
    (request,) = await h.requests()
    _refused_as_changed(request)


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_an_edit_of_a_tenant_copy_is_refused_when_the_copy_goes(
    client: TestClient, embedding: AsyncMock
) -> None:
    """The click does not turn off the install's row, which the card never named."""
    await _setup(embedding)
    await _tenant_copy(_RESTART)
    key = make_rsa_keypair("kid-opedit-copy-gone")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        body = {"safety_level": "dangerous", "is_enabled": False}
        request_id = _waits(_patch(client, _token(key), _RESTART, body))
        (request,) = await h.requests()
        card = request.proposed_effect["preview"]
        assert (card["scope"], card["row_tenant_id"]) == ("tenant", str(h.TENANT))
        await _drop_tenant_copy(_RESTART)
        await h.console_approve(client, key, request_id, sub="platform-b")
    assert await _enabled_rows(_RESTART) == [(None, True)]
    assert await _overrides(_RESTART) == []
    (request,) = await h.requests()
    _refused_as_changed(request)


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_removal_of_another_tenants_override_is_refused_before_it_waits(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    await ReviewService(_person_in(_TENANT_B, "admin-b", platform_admin=False)).edit_op(
        _CONNECTOR, _RESTART, tenant_id=_TENANT_B, safety_level="dangerous"
    )
    key = make_rsa_keypair("kid-opedit-other-tenant")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = _token(key, sub="platform-a", platform_admin=True)
        refused = _delete(client, bearer, _RESTART, tenant_id=str(_TENANT_B))
    assert refused.status_code == 403, refused.text
    assert OTHER_TENANT_REFUSAL in refused.text
    assert await h.requests() == []
    (override,) = await _overrides(_RESTART)
    assert (override.tenant_id, override.safety_level) == (_TENANT_B, "dangerous")

    params = {"connector_id": _CONNECTOR, "op_id": _RESTART, "override_tenant_id": str(_TENANT_B)}
    with pytest.raises(PolicyEditAskRefused, match="another tenant"):
        await request_policy_edit(h.person("platform-a"), POLICY_OP_REMOVE_OVERRIDE_OP_ID, params)
    result, _ = await request_policy_edit(
        _person_in(_TENANT_B, "platform-a", platform_admin=True),
        POLICY_OP_REMOVE_OVERRIDE_OP_ID,
        params,
    )
    assert result["status"] == "awaiting_approval", result
    (request,) = await h.requests()
    assert request.tenant_id == _TENANT_B
    assert len(await _overrides(_RESTART)) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_the_switch_off_at_the_click_writes_nothing(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch goes off and the registrar runs before the click: the run is refused."""
    await _setup(embedding)
    key = make_rsa_keypair("kid-opedit-switch-off")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id = _waits(_patch(client, _token(key), _RESTART, {"safety_level": "dangerous"}))
        monkeypatch.setenv("CUSTOM_POLICY", "false")
        get_settings.cache_clear()
        async with operation_catalog_write(embedding_service=embedding):
            pass
        await h.console_approve(client, key, request_id, sub="admin-b", platform_admin=False)
    assert await _overrides(_RESTART) == []
    (request,) = await h.requests()
    assert request.resume_result["status"] == "error", request.resume_result


# ---------------------------------------------------------------------------
# The switch off: as today
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_the_switch_off_a_token_raise_runs_at_once_and_the_ops_do_not_exist(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    async with operation_catalog_write(embedding_service=embedding):
        pass
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(EndpointDescriptor.op_id).where(
                EndpointDescriptor.op_id.in_(
                    (POLICY_OP_EDIT_OP_ID, POLICY_OP_REMOVE_OVERRIDE_OP_ID)
                )
            )
        )
        assert list(rows.scalars()) == []
    key = make_rsa_keypair("kid-opedit-off-switch")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        edited = _patch(client, _token(key), _RESTART, {"safety_level": "dangerous"})
        assert edited.status_code == 200, edited.text
        removed = _delete(client, _token(key), _RESTART, tenant_id=str(h.TENANT))
        assert removed.status_code == 204, removed.text
    assert await h.requests() == []
    assert await _overrides(_RESTART) == []
