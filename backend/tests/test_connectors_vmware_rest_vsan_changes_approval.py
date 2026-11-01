# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vSAN changes through the real dispatcher: risk levels and approval.

* Every change is registered with its risk level and ``requires_approval``.
* ``vsan.cluster.configure`` by a person waits until a second person
  approves, then runs once; nothing reaches vCenter before.
* ``vsan.disk.wipe`` (destructive) is refused without a matching
  ``preview_hash``; with one it waits for approval, the approval card shows
  the disks and their partitions, and the approved call wipes them. An
  agent's call is always refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    register_vmware_composite_operations,
    register_vmware_composite_operations_8_0,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests._vmware_vsan_fake import CLUSTER, HOST, VsanFake

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004445")
_TARGET_NAME = "example-vcenter"
_DISK = "naa.0000000000000a"

#: The table of the task: op id -> risk level. Every one waits for approval.
_RISK_LEVELS: dict[str, str] = {
    "vmware.composite.vsan.cluster.configure": "dangerous",
    "vmware.composite.vsan.disk.claim": "dangerous",
    "vmware.composite.vsan.disk.wipe": "destructive",
    "vmware.composite.vsan.encryption.set": "dangerous",
    "vmware.composite.vsan.encryption.rekey": "dangerous",
}


@pytest.fixture(autouse=True)
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


def _person(sub: str, kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name="vSAN Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


def _fake() -> VsanFake:
    conn = VsanFake()
    conn.add_cluster()
    conn.add_host()
    conn.add_disk(_DISK, "ineligible", partitions=2)
    return conn


async def _bootstrap(conn: VsanFake) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = conn  # type: ignore[assignment]
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
                notes="seeded by test_connectors_vmware_rest_vsan_changes_approval",
            )
        )
        await s.commit()


class _FakeTarget:
    """The resolved-target shape ``dispatch`` reads (vCenter 9.0, vmware-rest)."""

    def __init__(self) -> None:
        self.product = "vmware"
        self.fingerprint = type("_Fingerprint", (), {"version": "9.0"})()
        self.preferred_impl_id: str | None = "vmware-rest"
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = _TARGET_NAME
        self.host = "vcenter.example.test"
        self.port = 443
        self.auth_model = "shared_service_account"


async def _approval_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


async def _approve_and_resume(request_id: UUID) -> Any:
    approver = _person("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


@pytest.mark.parametrize("register", ["9.0", "8.0"])
async def test_each_change_has_its_risk_level_and_waits_for_approval(register: str) -> None:
    registrar = (
        register_vmware_composite_operations
        if register == "9.0"
        else register_vmware_composite_operations_8_0
    )
    await registrar(embedding_service=_embedding_stub())
    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.in_(tuple(_RISK_LEVELS)),
                        EndpointDescriptor.version == register,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {row.op_id: row.safety_level for row in rows} == _RISK_LEVELS
    assert all(row.requires_approval is True for row in rows)


async def test_configure_waits_for_a_second_person_then_runs_once() -> None:
    conn = _fake()
    await _bootstrap(conn)
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": "vmware.composite.vsan.cluster.configure",
        "target": _TARGET_NAME,
        "params": {"cluster": CLUSTER, "vsan_enabled": True},
    }
    call = await call_operation(_person("op-requester"), args)
    assert call["status"] == "awaiting_approval", call
    assert conn.vsan_calls == []

    resume = await _approve_and_resume(UUID(call["extras"]["approval_request_id"]))
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "accepted"
    assert conn.vsan_methods() == ["VsanClusterReconfig"]


async def test_an_agent_configure_without_a_grant_is_refused() -> None:
    conn = _fake()
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vsan.cluster.configure",
        target=_FakeTarget(),
        params={"cluster": CLUSTER, "vsan_enabled": True},
    )
    assert result.status == "denied", result
    assert conn.vsan_calls == []


async def test_wipe_needs_a_preview_then_a_second_person() -> None:
    conn = _fake()
    await _bootstrap(conn)
    requester = _person("op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": "vmware.composite.vsan.disk.wipe",
        "target": _TARGET_NAME,
        "params": {"host": HOST, "disks": [_DISK]},
    }
    refused = await call_operation(requester, args)
    assert refused["status"] == "denied", refused
    assert refused["extras"]["error_code"] == "preview_binding_required"
    assert await _approval_count() == 0

    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    assert conn.calls_to("UpdateDiskPartitions") == []
    request_id = UUID(call["extras"]["approval_request_id"])

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    block = effect["blast_radius"]
    assert block["object"]["host_name"] == "esxi-a"
    assert block["children"][0]["name"] == _DISK
    assert len(block["children"][0]["partitions"]) == 2
    assert block["irreversibility"] == "permanent"

    resume = await _approve_and_resume(request_id)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "wiped"
    assert len(conn.calls_to("UpdateDiskPartitions")) == 1


async def test_an_agent_wipe_is_refused() -> None:
    conn = _fake()
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id="vmware.composite.vsan.disk.wipe",
        target=_FakeTarget(),
        params={"host": HOST, "disks": [_DISK]},
    )
    assert result.status == "denied", result
    assert "destructive" in (result.error or "")
    assert conn.calls_to("UpdateDiskPartitions") == []
    assert await _approval_count() == 0
