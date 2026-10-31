# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host settings read and changes over real HTTP, and through the dispatcher.

* Transport: the real connector sends every vim call to
  ``/sdk/vim25/8.0.3.0/...`` on a vCenter 8.0 U3 and to
  ``/sdk/vim25/9.0.0.0/...`` on a vCenter 9.0 (respx answers with the fake
  host of ``tests._vmware_host_config_fake``). A vim fault (HTTP 500 with the
  fault object) reaches the answer with its class and text.
* Dispatcher: a person's NTP change waits for approval, runs once after a
  second person approves, and reads the host back. Each operation is on both
  vCenter catalogs with its risk level and a clean contract.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._host_config_writes import (
    host_syslog_set_composite,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    register_vmware_composite_operations,
    register_vmware_composite_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.ingest_safety import vmware_rest_safety_floor
from meho_backplane.connectors.vmware_rest.typed_ops import (
    register_vmware_typed_operations,
    register_vmware_typed_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_config import host_config_show_impl
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor, OperationGroup
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings
from tests._vmware_host_config_fake import HOST, HostConfigVim
from tests._vmware_teardown_fake import GateRecorder
from tests.test_operation_contracts import OperationContract, check_operation

_TENANT_ID = UUID("00000000-0000-0000-0000-000000004351")
_VC_BASE = "https://vc.test.invalid"


def _operator(sub: str = "op-host-config", kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name="Host Config Test",
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

    def __init__(self, vim: HostConfigVim, release: str) -> None:
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
        if method == "RetrievePropertiesEx":
            return httpx.Response(200, json=self.vim._retrieve(body))
        if method == "QueryOptions":
            value = {"_typeName": "string", "_value": self.vim.log_host}
            option = {"_typeName": "OptionValue", "key": body["name"], "value": value}
            return httpx.Response(200, json=[option])
        self.vim.calls.append((path, body))
        self.vim._change(method, body)
        return httpx.Response(204)


def _mock(mock: respx.MockRouter, version: str, router: _VimRouter) -> None:
    mock.post("/api/session").respond(200, json="tok")
    mock.get("/api/about").respond(200, json={"version": version})
    mock.get("/api/vcenter/host").respond(200, json=[{"host": HOST, "name": "esxi-a"}])
    mock.post(url__startswith=f"{_VC_BASE}/sdk/vim25/").mock(side_effect=router)


_VERSIONS = [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")]


@pytest.mark.parametrize(("version", "release"), _VERSIONS)
async def test_show_over_vi_json(version: str, release: str) -> None:
    vim = HostConfigVim()
    router = _VimRouter(vim, release)
    connector = _connector()
    try:
        async with respx.mock(base_url=_VC_BASE) as mock:
            _mock(mock, version, router)
            out = await host_config_show_impl(
                connector, _operator(), _StubTarget(), {"host": "esxi-a"}
            )
    finally:
        await connector.aclose()
    assert out["status"] == "ok", out
    assert out["read_notes"] == []
    assert out["ntp"]["servers"] == ["old-ntp.example.invalid"]
    assert out["syslog"]["targets"] == ["udp://old-log.example.invalid:514"]
    assert f"/sdk/vim25/{release}/OptionManager/EsxHostAdvSettings-21/QueryOptions" in router.paths


@pytest.mark.parametrize(("version", "release"), _VERSIONS)
async def test_syslog_set_over_vi_json(
    monkeypatch: pytest.MonkeyPatch, version: str, release: str
) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder())
    vim = HostConfigVim()
    router = _VimRouter(vim, release)
    connector = _connector()
    targets = ["tcp://log-1.example.invalid:1514"]
    try:
        async with respx.mock(base_url=_VC_BASE) as mock:
            _mock(mock, version, router)
            out = await host_syslog_set_composite(
                operator=_operator(),
                target=_StubTarget(),
                params={"host": "esxi-a", "targets": targets},
                connector=connector,
            )
    finally:
        await connector.aclose()
    assert out["status"] == "applied", out  # type: ignore[index]
    assert out["after"]["targets"] == targets  # type: ignore[index]
    assert f"/sdk/vim25/{release}/OptionManager/EsxHostAdvSettings-21/UpdateOptions" in router.paths


@pytest.mark.parametrize(("version", "release"), _VERSIONS)
async def test_a_vim_fault_reaches_the_answer(
    monkeypatch: pytest.MonkeyPatch, version: str, release: str
) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder())
    vim = HostConfigVim()
    router = _VimRouter(vim, release)
    router.fault["UpdateOptions"] = json.dumps(
        {"_typeName": "InvalidArgument", "faultstring": "A specified parameter was not correct"}
    )
    connector = _connector()
    try:
        async with respx.mock(base_url=_VC_BASE) as mock:
            _mock(mock, version, router)
            out = await host_syslog_set_composite(
                operator=_operator(),
                target=_StubTarget(),
                params={"host": "esxi-a", "targets": []},
                connector=connector,
            )
    finally:
        await connector.aclose()
    assert out["status"] == "failed"  # type: ignore[index]
    assert "InvalidArgument" in out["fault"]  # type: ignore[index]
    assert "A specified parameter was not correct" in out["fault"]  # type: ignore[index]
    assert vim.log_host == "udp://old-log.example.invalid:514"


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


_OPS = [
    ("vmware.host.config.show", "typed", "safe", False, "vmware-host-config"),
    ("vmware.composite.host.ntp.set", "composite", "dangerous", True, "host"),
    ("vmware.composite.host.dns.set", "composite", "dangerous", True, "host"),
    ("vmware.composite.host.syslog.set", "composite", "dangerous", True, "host"),
    ("vmware.composite.host.service_control", "composite", "dangerous", True, "host"),
]


@pytest.mark.usefixtures("_db_env")
@pytest.mark.parametrize(("op_id", "source_kind", "safety", "approval", "group_key"), _OPS)
async def test_each_op_is_on_both_catalogs_with_its_risk_and_contract(
    op_id: str, source_kind: str, safety: str, approval: bool, group_key: str
) -> None:
    stub = _embedding_stub()
    await register_vmware_composite_operations(embedding_service=stub)
    await register_vmware_composite_operations_8_0(embedding_service=stub)
    await register_vmware_typed_operations(embedding_service=stub)
    await register_vmware_typed_operations_8_0(embedding_service=stub)
    async with get_sessionmaker()() as s:
        rows = (
            (await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)))
            .scalars()
            .all()
        )
        assert {row.version for row in rows} == {"9.0", "8.0"}
        for row in rows:
            assert row.source_kind == source_kind
            assert row.safety_level == safety
            assert row.requires_approval is approval
            group = (
                await s.execute(select(OperationGroup).where(OperationGroup.id == row.group_id))
            ).scalar_one()
            assert group.group_key == group_key
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
            assert check_operation(contract) == {}


async def _bootstrap(vim: HostConfigVim) -> None:
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
                notes="seeded by test_connectors_vmware_rest_host_config_flow",
            )
        )
        await s.commit()


#: The generic (ingested) rows of the vim calls ``host.ntp.set`` makes.
_NTP_GENERIC_OPS = (
    "POST:/HostDateTimeSystem/{moId}/UpdateDateTimeConfig",
    "POST:/HostServiceSystem/{moId}/UpdateServicePolicy",
    "POST:/HostServiceSystem/{moId}/RestartService",
    "POST:/HostServiceSystem/{moId}/StartService",
)


async def _store_generic_rows() -> None:
    """Store the generic rows with the level the VMware floor gives them."""
    now = datetime.now(UTC)
    async with get_sessionmaker()() as s:
        for op_id in _NTP_GENERIC_OPS:
            method, path = op_id.split(":", 1)
            proto = vmware_rest_safety_floor(
                "9.0",
                EndpointDescriptorProto(
                    op_id=op_id,
                    method=method,
                    path=path,
                    parameter_schema={"type": "object", "properties": {}},
                    safety_level="caution",
                    requires_approval=False,
                ),
            )
            assert proto.requires_approval is True, op_id
            s.add(
                EndpointDescriptor(
                    id=uuid.uuid4(),
                    tenant_id=None,
                    product="vmware",
                    version="9.0",
                    impl_id="vmware-rest",
                    op_id=op_id,
                    source_kind="ingested",
                    method=method,
                    path=path,
                    handler_ref=None,
                    summary="Generic host call.",
                    tags=[],
                    parameter_schema=proto.parameter_schema,
                    safety_level=proto.safety_level,
                    requires_approval=proto.requires_approval,
                    is_enabled=True,
                    embedding=[0.1] * 384,
                    created_at=now,
                    updated_at=now,
                )
            )
        await s.commit()


@pytest.mark.usefixtures("_db_env")
async def test_an_ntp_change_waits_for_approval_then_runs_once() -> None:
    """One approval runs all NTP steps, also when the generic rows wait for approval."""
    vim = HostConfigVim()
    await _bootstrap(vim)
    await _store_generic_rows()
    servers = ["ntp-1.example.invalid"]
    call = await call_operation(
        _operator("op-requester"),
        {
            "connector_id": "vmware-rest-9.0",
            "op_id": "vmware.composite.host.ntp.set",
            "target": _TARGET_NAME,
            "params": {"host": "esxi-a", "servers": servers},
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
    assert effect["preview"]["current"]["servers"] == ["old-ntp.example.invalid"]

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
    assert resume.result["after"]["servers"] == servers
    assert vim.writes() == ["UpdateDateTimeConfig", "UpdateServicePolicy", "RestartService"]
    async with get_sessionmaker()() as s:
        count = (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
    assert count == 1


@pytest.mark.usefixtures("_db_env")
async def test_an_agent_without_a_grant_cannot_change_dns() -> None:
    vim = HostConfigVim()
    await _bootstrap(vim)
    call = await call_operation(
        _operator("agent-1", PrincipalKind.AGENT),
        {
            "connector_id": "vmware-rest-9.0",
            "op_id": "vmware.composite.host.dns.set",
            "target": _TARGET_NAME,
            "params": {"host": "esxi-a", "host_name": "esxi-b"},
        },
    )
    assert call["status"] == "denied", call
    assert vim.writes() == []
