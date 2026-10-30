# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The requester's policy on the Vault target of ``vcfa.provider.api_token.create`` (#3898).

The token is written through ``vault.kv.patch`` on a caller-named Vault
target. A reviewer who approves the request runs it as the reviewer, so
these tests pin that the *requester's* policy on that Vault target is still
enforced:

* the park card's ``permission_preflight`` shows ``requester_denied``;
* a reviewer's ``/decide`` approval of a denied requester's request fails
  closed before any appliance call or mint;
* a human-requested request, and an agent with no deny, still complete.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.vcf_automation import VCFA_CONNECTOR_ID, VcfAutomationConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, ApprovalRequest
from meho_backplane.main import app
from meho_backplane.operations import dispatch
from meho_backplane.settings import get_settings
from tests._vcfa_provisioning_support import BASE_URL, MINTED_TOKEN, TENANT_ID, resolved_target
from tests.test_connectors_vcf_automation_api_token import (
    _CREATE_PARAMS,
    _VAULT_TARGET,
    _capabilities,
    _mount_user_flow,
    _principal,
    _reset_state,
    _vault_target_id,
    vault_writes,
    vcfa,
)

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

__all__ = ["_reset_state", "vault_writes", "vcfa"]  # fixtures used by name

_OP_ID = "vcfa.provider.api_token.create"
_APPROVER = "op-approver"


@pytest.fixture(autouse=True)
def _oidc_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


async def _agent_permission(sub: str, op_pattern: str, verdict: str, target_scope: str) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AgentPermission(
                tenant_id=TENANT_ID,
                principal_sub=sub,
                op_pattern=op_pattern,
                target_scope=target_scope,
                verdict=verdict,
                created_by_sub="op-admin",
            )
        )
        await session.commit()


async def _deny_agent_vault_patch(sub: str) -> None:
    await _agent_permission(sub, "vault.kv.patch", "deny", str(await _vault_target_id()))


async def _park(requester: Operator) -> UUID:
    if requester.principal_kind is PrincipalKind.AGENT:
        # An agent may ask for the mint; a person must approve it.
        await _agent_permission(requester.sub, _OP_ID, "needs-approval", "*")
    parked = await dispatch(
        operator=requester,
        connector_id=VCFA_CONNECTOR_ID,
        op_id=_OP_ID,
        target=await resolved_target(),
        params=_CREATE_PARAMS,
    )
    assert parked.status == "awaiting_approval", parked
    return UUID(parked.extras["approval_request_id"])


async def _card_preflight(request_id: UUID) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == request_id))
        ).scalar_one()
    preflight: dict[str, Any] = row.proposed_effect["permission_preflight"]
    return preflight


def _decide(request_id: UUID, mock: respx.MockRouter) -> dict[str, Any]:
    """Approve *request_id* over REST ``/decide`` as a second human."""
    key = make_rsa_keypair("kid-decider")
    mock_discovery_and_jwks(mock, public_jwks(key))
    token = mint_token(
        key, sub=_APPROVER, tenant_role=TenantRole.OPERATOR.value, tenant_id=str(TENANT_ID)
    )
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/approvals/{request_id}/decide",
            headers={"Authorization": f"Bearer {token}"},
            json={"decision": "approved"},
        )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_park_card_shows_the_requester_is_denied(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = "agent:tenant-bootstrap-denied"
    await _deny_agent_vault_patch(sub)
    _capabilities(monkeypatch, ["create", "read", "update"])
    request_id = await _park(_principal(PrincipalKind.AGENT, sub))
    preflight = await _card_preflight(request_id)
    assert preflight["will_be_denied"] is False  # the target's Vault role may write
    assert preflight["requester_denied"] is True
    assert preflight["requester_reason"].startswith("policy_denied: vault.kv.patch")
    assert _VAULT_TARGET in preflight["requester_reason"]
    assert preflight["requester"]["sub"] == sub
    assert preflight["requester"]["principal_kind"] == "agent"


async def test_reviewer_approval_of_a_denied_requester_fails_closed(
    vcfa: VcfAutomationConnector,
    vault_writes: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = "agent:tenant-bootstrap-denied"
    await _deny_agent_vault_patch(sub)
    _capabilities(monkeypatch, ["create", "read", "update"])
    request_id = await _park(_principal(PrincipalKind.AGENT, sub))
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        body = _decide(request_id, mock)
    out = body["dispatch_result"]
    assert out["status"] == "invalid_request", body
    assert out["guidance"].startswith(f"policy_denied: requester {sub!r}: vault.kv.patch")
    assert _VAULT_TARGET in out["guidance"]
    assert not any(route.called for route in routes.values())  # no appliance call, no mint
    assert vault_writes == []


@pytest.mark.parametrize(
    "requester",
    [
        _principal(PrincipalKind.USER, "op-requester"),
        _principal(PrincipalKind.AGENT, "agent:tenant-bootstrap-allowed"),
    ],
    ids=["human-requester", "agent-without-deny"],
)
async def test_reviewer_approval_still_completes_when_the_requester_may_write(
    requester: Operator,
    vcfa: VcfAutomationConnector,
    vault_writes: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capabilities(monkeypatch, ["create", "read", "update"])
    request_id = await _park(requester)
    preflight = await _card_preflight(request_id)
    assert preflight["requester_denied"] is False
    assert preflight["requester_reason"] is None
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        body = _decide(request_id, mock)
    assert body["dispatch_status"] == "ok", body
    assert body["dispatch_result"]["status"] == "created", body
    assert routes["grant"].called
    assert [kind for kind, _ in vault_writes] == ["patch"]
    assert MINTED_TOKEN not in json.dumps(body)


async def test_a_deny_added_while_the_request_waits_still_stops_it(
    vcfa: VcfAutomationConnector,
    vault_writes: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = "agent:tenant-bootstrap-later-denied"
    _capabilities(monkeypatch, ["create", "read", "update"])
    request_id = await _park(_principal(PrincipalKind.AGENT, sub))
    assert (await _card_preflight(request_id))["requester_denied"] is False
    await _deny_agent_vault_patch(sub)  # the requester's policy is checked again at run time
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        body = _decide(request_id, mock)
    assert body["dispatch_result"]["status"] == "invalid_request", body
    assert body["dispatch_result"]["guidance"].startswith(f"policy_denied: requester {sub!r}")
    assert not any(route.called for route in routes.values())
    assert vault_writes == []


async def test_reviewer_approval_fails_closed_when_the_card_has_no_requester_record(
    vcfa: VcfAutomationConnector,
    vault_writes: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meho_backplane.connectors.vault.ops as vault_ops

    async def _raise(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("probe broke")

    sub = "agent:tenant-bootstrap-no-record"
    await _deny_agent_vault_patch(sub)
    _capabilities(monkeypatch, ["create", "read", "update"])
    monkeypatch.setattr(vault_ops, "vault_kv_write_capability_preflight", _raise)
    request_id = await _park(_principal(PrincipalKind.AGENT, sub))
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == request_id))
        ).scalar_one()
    assert not (row.proposed_effect.get("permission_preflight") or {}).get("requester")
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        body = _decide(request_id, mock)
    out = body["dispatch_result"]
    assert out["status"] == "invalid_request", body
    assert "no requester record" in out["guidance"]
    assert "ask again" in out["guidance"]
    assert not any(route.called for route in routes.values())  # no appliance call, no mint
    assert vault_writes == []


async def test_reviewer_approval_fails_closed_when_the_requester_record_does_not_match(
    vcfa: VcfAutomationConnector,
    vault_writes: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = "agent:tenant-bootstrap-mismatch"
    await _deny_agent_vault_patch(sub)
    _capabilities(monkeypatch, ["create", "read", "update"])
    request_id = await _park(_principal(PrincipalKind.AGENT, sub))
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == request_id))
        ).scalar_one()
        effect = dict(row.proposed_effect)
        preflight = dict(effect["permission_preflight"])
        preflight["requester"] = {**preflight["requester"], "sub": "op-someone-else"}
        effect["permission_preflight"] = preflight
        row.proposed_effect = effect
        await session.commit()
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        body = _decide(request_id, mock)
    out = body["dispatch_result"]
    assert out["status"] == "invalid_request", body
    assert f"no requester record for {sub!r}" in out["guidance"]
    assert not any(route.called for route in routes.values())
    assert vault_writes == []
