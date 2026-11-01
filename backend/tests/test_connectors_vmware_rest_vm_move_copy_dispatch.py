# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vm.relocate`` and ``vm.clone_from_vm`` through the dispatcher and over HTTP.

* Both register on the ``vmware-rest-8.0`` and ``vmware-rest-9.0`` catalogs as
  ``dangerous`` changes that wait for approval.
* A person's call waits until a second person approves; then the task starts
  once. An agent without a grant is refused.
* The real connector sends the VIM calls on the vCenter 8 path
  (``/sdk/vim25/8.0.3.0/...``) and the vCenter 9 path (``/sdk/vim25/9.0.0.0/...``).
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
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.composites import (
    _vm_clone_from_vm,
    _vm_relocate,
    _write,
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
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings
from tests._vmware_move_copy_fake import VM, MoveCopyVimFake, seed_vm
from tests._vmware_teardown_fake import GateRecorder

_RELOCATE_OP = "vmware.composite.vm.relocate"
_CLONE_OP = "vmware.composite.vm.clone_from_vm"
_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004346")
_TARGET_NAME = "example-vcenter"


@pytest.fixture
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
        name="Move Copy Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _bootstrap(conn: MoveCopyVimFake) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = conn  # type: ignore[assignment]
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
    await register_vmware_composite_operations_8_0(embedding_service=_embedding_stub())
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
                notes="seeded by test_connectors_vmware_rest_vm_move_copy_dispatch",
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


@pytest.mark.usefixtures("_dispatch_env")
async def test_both_register_as_dangerous_changes_on_both_catalogs() -> None:
    await _bootstrap(MoveCopyVimFake())
    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.in_((_RELOCATE_OP, _CLONE_OP))
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 4
    for row in rows:
        assert row.safety_level == "dangerous"
        assert row.requires_approval is True


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.parametrize(
    ("op_id", "params", "method"),
    [
        (_RELOCATE_OP, {"vm": VM, "datastore": "datastore-12"}, "RelocateVM_Task"),
        (_CLONE_OP, {"source_vm": VM, "name": "<new-vm-name>"}, "CloneVM_Task"),
    ],
)
async def test_a_person_waits_for_a_second_person_then_the_task_starts_once(
    op_id: str, params: dict[str, Any], method: str
) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    await _bootstrap(conn)
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": op_id,
        "target": _TARGET_NAME,
        "params": params,
    }
    call = await call_operation(_person("op-requester"), args)
    assert call["status"] == "awaiting_approval", call
    assert conn.calls_to(method) == []

    resume = await _approve_and_resume(UUID(call["extras"]["approval_request_id"]))
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "accepted"
    assert resume.result["status_read"]["op_id"] == "vmware.task.info"
    assert len(conn.calls_to(method)) == 1


@pytest.mark.usefixtures("_dispatch_env")
async def test_the_approval_card_shows_the_move() -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    await _bootstrap(conn)
    call = await call_operation(
        _person("op-requester"),
        {
            "connector_id": _CONNECTOR_ID,
            "op_id": _RELOCATE_OP,
            "target": _TARGET_NAME,
            "params": {"vm": VM, "resource_pool": "resgroup-9"},
        },
    )
    assert call["status"] == "awaiting_approval", call
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, UUID(call["extras"]["approval_request_id"]))
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "dangerous"
    assert effect["preview"]["action"] == "relocate_vm"
    assert effect["preview"]["requested"] == {"resource_pool": "resgroup-9"}


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_without_a_grant_is_refused() -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_CLONE_OP,
        target=_FakeTarget(),
        params={"source_vm": VM, "name": "<new-vm-name>"},
    )
    assert result.status == "denied", result
    assert conn.starts() == []
    assert await _approval_count() == 0


# ===========================================================================
# Over HTTP: the vCenter 8 and the vCenter 9 VI-JSON paths
# ===========================================================================

_BASE = "https://vc.example.test"


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "<user>", "password": "<password>"}


@dataclass
class _Target:
    name: str = "vc-example"
    host: str = "vc.example.test"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid.uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


def _vm_result() -> dict[str, Any]:
    props = {
        "name": "<vm-name>",
        "runtime.powerState": "poweredOff",
        "parent": {"_typeName": "ManagedObjectReference", "type": "Folder", "value": "group-v3"},
        "config.template": False,
        "datastore": [],
        "config.hardware.device": [],
        "recentTask": [],
    }
    return {
        "_typeName": "RetrieveResult",
        "objects": [
            {
                "_typeName": "ObjectContent",
                "obj": {
                    "_typeName": "ManagedObjectReference",
                    "type": "VirtualMachine",
                    "value": VM,
                },
                "propSet": [
                    {"_typeName": "DynamicProperty", "name": k, "val": v} for k, v in props.items()
                ],
            }
        ],
    }


def _folder_result() -> dict[str, Any]:
    return {
        "_typeName": "RetrieveResult",
        "objects": [
            {
                "_typeName": "ObjectContent",
                "obj": {
                    "_typeName": "ManagedObjectReference",
                    "type": "Folder",
                    "value": "group-v3",
                },
                "propSet": [{"_typeName": "DynamicProperty", "name": "childEntity", "val": []}],
            }
        ],
    }


def _task_ref(moid: str) -> dict[str, str]:
    return {"_typeName": "ManagedObjectReference", "type": "Task", "value": moid}


async def _over_http(about: str, release: str, op: str) -> tuple[dict[str, Any], Any]:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    read_path = f"/sdk/vim25/{release}/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.post("/api/session").respond(200, json="tok-1")
            mock.get("/api/about").respond(200, json={"version": about})
            mock.post(read_path).mock(
                side_effect=[
                    httpx.Response(200, json=_vm_result()),
                    httpx.Response(200, json=_folder_result()),
                ]
            )
            if op == _RELOCATE_OP:
                start = mock.post(f"/sdk/vim25/{release}/VirtualMachine/{VM}/RelocateVM_Task")
                start.respond(200, json=_task_ref("task-31"))
                out = await _vm_relocate.vm_relocate_composite(
                    operator=_person("op-http"),
                    target=_Target(),
                    params={"vm": VM, "datastore": "datastore-12"},
                    connector=connector,
                )
            else:
                start = mock.post(f"/sdk/vim25/{release}/VirtualMachine/{VM}/CloneVM_Task")
                start.respond(200, json=_task_ref("task-31"))
                out = await _vm_clone_from_vm.vm_clone_from_vm_composite(
                    operator=_person("op-http"),
                    target=_Target(),
                    params={"source_vm": VM, "name": "<new-vm-name>"},
                    connector=connector,
                )
        return out, start
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


@pytest.mark.parametrize(("about", "release"), [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")])
@pytest.mark.parametrize("op", [_RELOCATE_OP, _CLONE_OP])
async def test_the_task_starts_on_the_vi_json_path_of_each_version(
    monkeypatch: pytest.MonkeyPatch, about: str, release: str, op: str
) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder())
    out, start = await _over_http(about, release, op)
    assert out["status"] == "accepted", out
    assert out["task"] == "task-31"
    assert start.call_count == 1
    body = json.loads(start.calls.last.request.content)
    assert body["spec"]["_typeName"] in ("VirtualMachineRelocateSpec", "VirtualMachineCloneSpec")
