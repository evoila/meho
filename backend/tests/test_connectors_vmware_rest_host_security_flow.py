# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host security reads and changes over real HTTP, and through the dispatcher.

* Transport: the real connector sends every vim call to
  ``/sdk/vim25/8.0.3.0/...`` on a vCenter 8.0 U3 and to
  ``/sdk/vim25/9.0.0.0/...`` on a vCenter 9.0 (respx answers with the fake
  host of ``tests._vmware_host_security_fake``). A vim fault (HTTP 500 with
  the fault object) reaches the answer with its class and text.
* Dispatcher: a person's lockdown change waits for approval, runs once after a
  second person approves, and reads the host back; an agent without a grant
  is refused. Each operation is on both vCenter catalogs with its risk level,
  its group and a clean contract.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._host_security_writes import (
    host_firewall_ruleset_set_composite,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    register_vmware_composite_operations,
    register_vmware_composite_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.typed_ops import (
    register_vmware_typed_operations,
    register_vmware_typed_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_security import (
    advanced_settings_show_impl,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor, OperationGroup
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings
from tests._vmware_host_security_fake import HOST, HostSecurityVim
from tests._vmware_teardown_fake import GateRecorder
from tests.test_operation_contracts import OperationContract, check_operation

_TENANT_ID = UUID("00000000-0000-0000-0000-000000004352")
_VC_BASE = "https://vc.test.invalid"


def _operator(sub: str = "op-host-security", kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name="Host Security Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


# ---------------------------------------------------------------------------
# Transport: real connector, respx, vCenter 8.0 U3 and 9.0 paths
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-test"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid.uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


async def _stub_loader(_target: Any, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def _connector() -> VmwareRestConnector:
    connector = VmwareRestConnector(session_loader=_stub_loader)

    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]
    return connector


class _VimRouter:
    """respx side effect: answers each ``/sdk/vim25/<release>/...`` call from the fake."""

    def __init__(self, vim: HostSecurityVim, release: str) -> None:
        self.vim = vim
        self.prefix = f"/sdk/vim25/{release}/"
        self.paths: list[str] = []
        self.fault: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert path.startswith(self.prefix), path
        self.paths.append(path)
        method = path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"null")
        if method in self.fault:
            return httpx.Response(500, text=self.fault[method])
        self.vim.calls.append((path, body))
        answer = self.vim.answer(path, body)
        return httpx.Response(204) if answer is None else httpx.Response(200, json=answer)


def _mock(mock: respx.MockRouter, version: str, router: _VimRouter) -> None:
    mock.post("/api/session").respond(200, json="tok")
    mock.get("/api/about").respond(200, json={"version": version})
    mock.get("/api/vcenter/host").respond(200, json=[{"host": HOST, "name": "esxi-a"}])
    mock.post(url__startswith=f"{_VC_BASE}/sdk/vim25/").mock(side_effect=router)


_VERSIONS = [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")]
_RULESET_PARAMS = {"host": "esxi-a", "ruleset": "sshServer", "enabled": True}


@pytest.mark.parametrize(("version", "release"), _VERSIONS)
async def test_advanced_settings_show_over_vi_json(version: str, release: str) -> None:
    vim = HostSecurityVim()
    router = _VimRouter(vim, release)
    connector = _connector()
    try:
        async with respx.mock(base_url=_VC_BASE) as mock:
            _mock(mock, version, router)
            out = await advanced_settings_show_impl(
                connector, _operator(), _StubTarget(), {"host": "esxi-a", "prefix": "Net."}
            )
    finally:
        await connector.aclose()
    assert out["status"] == "ok", out
    assert out["settings"][0]["key"] == "Net.BlockGuestBPDU"
    assert out["settings"][0]["is_default"] is False
    assert f"/sdk/vim25/{release}/OptionManager/EsxHostAdvSettings-21/QueryOptions" in router.paths


@pytest.mark.parametrize(("version", "release"), _VERSIONS)
async def test_firewall_change_over_vi_json(
    monkeypatch: pytest.MonkeyPatch, version: str, release: str
) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder())
    vim = HostSecurityVim()
    router = _VimRouter(vim, release)
    connector = _connector()
    try:
        async with respx.mock(base_url=_VC_BASE) as mock:
            _mock(mock, version, router)
            out = await host_firewall_ruleset_set_composite(
                operator=_operator(),
                target=_StubTarget(),
                params=_RULESET_PARAMS,
                connector=connector,
            )
    finally:
        await connector.aclose()
    assert out["status"] == "applied", out  # type: ignore[index]
    assert out["after"]["enabled"] is True  # type: ignore[index]
    assert f"/sdk/vim25/{release}/HostFirewallSystem/firewallSystem-21/EnableRuleset" in (
        router.paths
    )


@pytest.mark.parametrize(("version", "release"), _VERSIONS)
async def test_a_vim_fault_reaches_the_answer(
    monkeypatch: pytest.MonkeyPatch, version: str, release: str
) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder())
    vim = HostSecurityVim()
    router = _VimRouter(vim, release)
    router.fault["EnableRuleset"] = json.dumps(
        {"_typeName": "HostConfigFault", "faultstring": "Ruleset could not be changed"}
    )
    connector = _connector()
    try:
        async with respx.mock(base_url=_VC_BASE) as mock:
            _mock(mock, version, router)
            out = await host_firewall_ruleset_set_composite(
                operator=_operator(),
                target=_StubTarget(),
                params=_RULESET_PARAMS,
                connector=connector,
            )
    finally:
        await connector.aclose()
    assert out["status"] == "failed"  # type: ignore[index]
    assert "HostConfigFault" in out["fault"]  # type: ignore[index]
    assert "Ruleset could not be changed" in out["fault"]  # type: ignore[index]
    assert vim.rulesets[0]["enabled"] is False


# ---------------------------------------------------------------------------
# Through the dispatcher
# ---------------------------------------------------------------------------

_TARGET_NAME = "example-vcenter"


@pytest.fixture
def _db_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


def _embedding_stub() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


_GROUP = "vmware-host-security"
_OPS = [
    ("vmware.host.advanced_settings.show", "typed", "safe", False),
    ("vmware.host.firewall.show", "typed", "safe", False),
    ("vmware.composite.host.advanced_settings.set", "composite", "dangerous", True),
    ("vmware.composite.host.firewall.ruleset.set", "composite", "dangerous", True),
    ("vmware.composite.host.lockdown.set", "composite", "dangerous", True),
    ("vmware.composite.host.lockdown.exceptions.set", "composite", "dangerous", True),
    ("vmware.composite.host.snmp.set", "composite", "dangerous", True),
    ("vmware.composite.host.coredump.set", "composite", "dangerous", True),
]


@pytest.mark.usefixtures("_db_env")
async def test_each_op_is_on_both_catalogs_with_its_risk_and_contract() -> None:
    stub = _embedding_stub()
    await register_vmware_composite_operations(embedding_service=stub)
    await register_vmware_composite_operations_8_0(embedding_service=stub)
    await register_vmware_typed_operations(embedding_service=stub)
    await register_vmware_typed_operations_8_0(embedding_service=stub)
    async with get_sessionmaker()() as s:
        for op_id, source_kind, safety, approval in _OPS:
            rows = (
                (
                    await s.execute(
                        select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
                    )
                )
                .scalars()
                .all()
            )
            assert {row.version for row in rows} == {"9.0", "8.0"}, op_id
            for row in rows:
                assert row.source_kind == source_kind
                assert row.safety_level == safety
                assert row.requires_approval is approval
                group = (
                    await s.execute(select(OperationGroup).where(OperationGroup.id == row.group_id))
                ).scalar_one()
                assert group.group_key == _GROUP
                contract = OperationContract(
                    connector="vmware-rest",
                    op_id=row.op_id,
                    summary=row.summary,
                    description=row.description,
                    parameter_schema=dict(row.parameter_schema),
                    response_schema=row.response_schema,
                    llm_instructions=row.llm_instructions,
                    safety_level=row.safety_level,
                    requires_approval=row.requires_approval,
                    group_text=group.when_to_use,
                )
                assert check_operation(contract) == {}, op_id


async def _bootstrap(vim: HostSecurityVim) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = vim  # type: ignore[assignment]
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="vmware",
                host="vcenter.example.test",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.0"},
                preferred_impl_id="vmware-rest",
                notes="seeded by test_connectors_vmware_rest_host_security_flow",
            )
        )
        await s.commit()


@pytest.mark.usefixtures("_db_env")
async def test_a_lockdown_change_waits_for_approval_then_runs_once() -> None:
    vim = HostSecurityVim()
    await _bootstrap(vim)
    call = await call_operation(
        _operator("op-requester"),
        {
            "connector_id": "vmware-rest-9.0",
            "op_id": "vmware.composite.host.lockdown.set",
            "target": _TARGET_NAME,
            "params": {"host": "esxi-a", "mode": "normal"},
        },
    )
    assert call["status"] == "awaiting_approval", call
    assert vim.writes() == []
    request_id = UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "dangerous"
    assert effect["preview"]["current"] == {"mode": "disabled"}

    approver = _operator("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "applied"
    assert resume.result["after"] == {"mode": "normal"}
    assert vim.writes() == ["ChangeLockdownMode"]


@pytest.mark.usefixtures("_db_env")
async def test_an_agent_without_a_grant_cannot_change_the_core_dump_partition() -> None:
    vim = HostSecurityVim()
    await _bootstrap(vim)
    call = await call_operation(
        _operator("agent-1", PrincipalKind.AGENT),
        {
            "connector_id": "vmware-rest-9.0",
            "op_id": "vmware.composite.host.coredump.set",
            "target": _TARGET_NAME,
            "params": {"host": "esxi-a", "disk_name": "disk-b", "partition": 9},
        },
    )
    assert call["status"] == "denied", call
    assert vim.writes() == []
