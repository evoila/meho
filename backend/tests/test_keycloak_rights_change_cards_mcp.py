# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The MCP approval tools and the Keycloak rights-change cards.

The approval request a person reads always holds the cards of
``keycloak.client.update``, ``keycloak.protocol_mapper.create`` and
``keycloak.realm.update``. ``meho_approvals_get`` and
``meho_approvals_list`` give the caller what ``preview_operation`` gives:
with ``KEYCLOAK_RIGHTS_CHANGE_CARDS`` off (the default), the generic params
echo for client update and mapper create, and the realm card without the
values now for realm update. With the switch on, the full cards.

Fixture values are made up.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from meho_backplane.api.v1.approvals import _view
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import (  # loading the package registers the ops
    KeycloakConnector,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.mcp.tools.approvals import _get_handler, _list_handler
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    proposed_effect_for_agent,
)
from meho_backplane.settings import get_settings
from tests.test_ui_approvals import (
    _REVIEWER_SUB,
    _TENANT_A,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _operator,
    _seed_request,
    _seed_tenant,
)

_CONNECTOR_ID = "keycloak-admin-26.x"
_CLIENT_UUID = "33333333-3333-3333-3333-333333333333"
_CLIENT_NOW = {
    "id": _CLIENT_UUID,
    "clientId": "example-app",
    "fullScopeAllowed": False,
    "protocolMappers": [{"name": "groups", "protocolMapper": "oidc-group-membership-mapper"}],
}
_REALM_NOW = {"realm": "example", "registrationAllowed": False}

_CLIENT_UPDATE = (
    "keycloak.client.update",
    {"id": _CLIENT_UUID, "representation": {"fullScopeAllowed": True}},
    _CLIENT_NOW,
)
_MAPPER_CREATE = (
    "keycloak.protocol_mapper.create",
    {"id": _CLIENT_UUID, "representation": {"name": "role", "config": {"claim.name": "x"}}},
    _CLIENT_NOW,
)
_REALM_UPDATE = (
    "keycloak.realm.update",
    {"representation": {"registrationAllowed": True}},
    _REALM_NOW,
)


@dataclass
class _FakeDescriptor:
    op_id: str


@dataclass
class _FakeTarget:
    extras: dict[str, Any] = field(default_factory=lambda: {"managed_realm": "example"})


@pytest.fixture(autouse=True)
def _tenant() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    if on:
        monkeypatch.setenv("KEYCLOAK_RIGHTS_CHANGE_CARDS", "true")
    else:
        monkeypatch.delenv("KEYCLOAK_RIGHTS_CHANGE_CARDS", raising=False)
    get_settings.cache_clear()


def _effect(op_id: str, params: dict[str, Any], now: dict[str, Any], *, for_agent: bool) -> Any:
    connector = KeycloakConnector()
    connector._get_admin_json = AsyncMock(return_value=now)  # type: ignore[method-assign]
    connector._find_client_uuid = AsyncMock(return_value=_CLIENT_UUID)  # type: ignore[method-assign]
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id=op_id),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=Operator(
            sub="person-requester",
            raw_jwt="<test-raw-jwt>",
            tenant_id=_TENANT_A,
            tenant_role=TenantRole.TENANT_ADMIN,
        ),
        target=_FakeTarget(),
        params=params,
        connector_id=_CONNECTOR_ID,
        agent_preview=for_agent,
    )
    return asyncio.run(build_proposed_effect(ctx))


def _identity(op_id: str) -> dict[str, Any]:
    """The fields the dispatcher adds to every stored request."""
    return {
        "op_id": op_id,
        "connector_id": _CONNECTOR_ID,
        "target_id": None,
        "safety_level": "dangerous",
        "preview_populated": True,
    }


def _park(op_id: str, params: dict[str, Any], now: dict[str, Any]) -> uuid.UUID:
    """A waiting request that holds the card a person reads, as the dispatcher stores it."""
    stored = {**_effect(op_id, params, now, for_agent=False), **_identity(op_id)}
    rid = _seed_request(
        tenant_id=_TENANT_A, op_id=op_id, connector_id=_CONNECTOR_ID, proposed_effect=stored
    )

    async def _store_params() -> None:
        async with get_sessionmaker()() as session, session.begin():
            await session.execute(
                update(ApprovalRequest).where(ApprovalRequest.id == rid).values(params=params)
            )

    asyncio.run(_store_params())
    return rid


def _mcp_effects(rid: uuid.UUID) -> tuple[dict[str, Any], dict[str, Any]]:
    """``proposed_effect`` of *rid* from ``meho_approvals_get`` and ``meho_approvals_list``."""
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)
    got = asyncio.run(_get_handler(operator, {"approval_request_id": str(rid)}))
    listed = asyncio.run(_list_handler(operator, {"status": "pending"}))
    by_id = {item["id"]: item["proposed_effect"] for item in listed["items"]}
    return got["proposed_effect"], by_id[str(rid)]


def _rest_effect(rid: uuid.UUID) -> dict[str, Any]:
    async def _load() -> ApprovalRequest:
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, rid)
            assert row is not None
            return row

    return dict(_view(asyncio.run(_load())).proposed_effect or {})


@pytest.mark.parametrize("case", [_CLIENT_UPDATE, _MAPPER_CREATE], ids=["client", "mapper"])
def test_switch_off_mcp_tools_echo_the_params_of_client_update_and_mapper_create(
    case: tuple[str, dict[str, Any], dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    op_id, params, now = case
    _switch(monkeypatch, on=False)
    rid = _park(op_id, params, now)
    agent_preview = _effect(op_id, params, now, for_agent=True)

    got, listed = _mcp_effects(rid)

    assert got == listed == {**agent_preview, **_identity(op_id)}
    assert "preview" not in got
    assert got["params_echo"]["representation"] == params["representation"]
    # The person still reads the full card.
    assert "preview" in _rest_effect(rid)


def test_switch_off_mcp_tools_show_the_realm_card_without_the_values_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, params, now = _REALM_UPDATE
    _switch(monkeypatch, on=False)
    rid = _park(op_id, params, now)
    agent_preview = _effect(op_id, params, now, for_agent=True)

    got, listed = _mcp_effects(rid)

    assert got == listed
    assert got["preview"] == agent_preview["preview"]
    assert "current_values" not in got["preview"]
    assert "current_values_available" not in got["preview"]
    assert _rest_effect(rid)["preview"]["current_values"] == {"registrationAllowed": False}


@pytest.mark.parametrize(
    "case", [_CLIENT_UPDATE, _MAPPER_CREATE, _REALM_UPDATE], ids=["client", "mapper", "realm"]
)
def test_switch_on_mcp_tools_show_the_full_card(
    case: tuple[str, dict[str, Any], dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    op_id, params, now = case
    _switch(monkeypatch, on=True)
    rid = _park(op_id, params, now)

    got, listed = _mcp_effects(rid)

    assert got == listed == _rest_effect(rid)
    assert got["preview"] == _effect(op_id, params, now, for_agent=True)["preview"]


def test_switch_off_mcp_tools_leave_other_requests_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, on=False)
    stored = {"op_id": "vsphere.vm.create", "preview": {"name": "vm-1"}}
    rid = _seed_request(tenant_id=_TENANT_A, proposed_effect=stored)

    got, listed = _mcp_effects(rid)

    assert got == listed == stored


def test_a_request_without_a_stored_effect_reads_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, on=False)

    shown = proposed_effect_for_agent(
        "keycloak.client.update",
        None,
        params={"id": _CLIENT_UUID},
        connector_id=_CONNECTOR_ID,
        tenant=str(_TENANT_A),
    )

    assert shown is None
