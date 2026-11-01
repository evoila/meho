# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The governed flow of ``vmware.composite.vm.disk.remove`` through the real dispatcher.

The disk removal is ``destructive``. A call without a matching ``preview_hash``
is refused and nothing waits. With the hash it waits for a second person, the
approval card shows the disk, and it runs once after approval. A disk that
another disk replaced (same key, even the same file, label and size) while the
request waited is not removed: the disk UUID tells them apart. An agent is
refused. A preview of a disk that does not exist is refused too, so nothing
waits. vCenter is the shared VIM fake, extended here with ``ReconfigVM_Task``.
"""

from __future__ import annotations

import copy
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
from meho_backplane.connectors.vmware_rest.composites import register_vmware_composite_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests._vmware_teardown_fake import VimFake

_OP = "vmware.composite.vm.disk.remove"
_VM = "vm-42"
_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004335")
_TARGET_NAME = "example-vcenter"


def _disk(key: int, label: str, *, generation: int = 0) -> dict[str, Any]:
    """A test disk; *generation* gives a replacement on the same key its own UUID."""
    return {
        "_typeName": "VirtualDisk",
        "key": key,
        "deviceInfo": {"_typeName": "Description", "label": label},
        "backing": {
            "_typeName": "VirtualDiskFlatVer2BackingInfo",
            "fileName": f"[<datastore>] app-vm/app-vm_{key}.vmdk",
            "sharing": "sharingNone",
            "uuid": f"6000C290-0000-0000-{generation:04x}-{key:012x}",
        },
        "controllerKey": 1000,
        "unitNumber": key - 2000,
        "capacityInBytes": 1024**3,
    }


class _DiskVimFake(VimFake):
    """The shared VIM fake plus a ``ReconfigVM_Task`` that removes the device."""

    def __init__(self) -> None:
        super().__init__()
        self.add(
            "VirtualMachine",
            _VM,
            **{
                "name": "app-vm",
                "runtime.powerState": "poweredOff",
                "config.hardware.device": [_disk(2000, "Hard disk 1"), _disk(2001, "Hard disk 2")],
            },
        )

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
    ) -> Any:
        _mo_type, moid, method = path.strip("/").split("/")
        if method == "ReconfigVM_Task":
            self.calls.append((path, copy.deepcopy(json)))
            return self._task(method, apply=lambda: self._remove(moid, json))
        return await super()._post_vmomi_json(
            target,
            path,
            operator=operator,
            json=json,
            promote_managed_object_not_found=promote_managed_object_not_found,
        )

    def _remove(self, vm: str, body: dict[str, Any]) -> None:
        props = self.objects[("VirtualMachine", vm)]
        keys = {c["device"]["key"] for c in body["spec"]["deviceChange"]}
        props["config.hardware.device"] = [
            d for d in props["config.hardware.device"] if d["key"] not in keys
        ]

    def replace_disk(self, key: int, label: str, file_name: str | None = None) -> None:
        """Another disk takes *key*: what a remove-then-add while the request waits looks like.

        The new disk has its own UUID. Without *file_name* it keeps the file
        path of the disk it replaces.
        """
        props = self.objects[("VirtualMachine", _VM)]
        new = _disk(key, label, generation=1)
        if file_name is not None:
            new["backing"]["fileName"] = file_name
        props["config.hardware.device"] = [
            new if d["key"] == key else d for d in props["config.hardware.device"]
        ]

    def disk_keys(self) -> list[int]:
        return [d["key"] for d in self.objects[("VirtualMachine", _VM)]["config.hardware.device"]]


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
        name="Disk Remove Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _park(requester: Operator, args: dict[str, Any]) -> UUID:
    """Preview, then call with the hash: the request waits. Returns its id."""
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    return UUID(call["extras"]["approval_request_id"])


async def _approve_and_run(request_id: UUID) -> Any:
    approver = _person("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


async def _bootstrap(conn: _DiskVimFake) -> None:
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
                notes="seeded by test_connectors_vmware_rest_vm_disk_remove_dispatch",
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


def _args(disk: str) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP,
        "target": _TARGET_NAME,
        "params": {"vm": _VM, "disk": disk},
    }


@pytest.mark.usefixtures("_dispatch_env")
async def test_remove_needs_a_preview_then_a_second_person() -> None:
    conn = _DiskVimFake()
    await _bootstrap(conn)
    requester = _person("op-requester")
    args = _args("2001")

    refused = await call_operation(requester, args)
    assert refused["status"] == "denied", refused
    assert refused["extras"]["error_code"] == "preview_binding_required"
    assert await _approval_count() == 0

    request_id = await _park(requester, args)
    assert conn.calls_to("ReconfigVM_Task") == []

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"]["label"] == "Hard disk 2"
    assert effect["blast_radius"]["object"]["disk_uuid"] == _disk(2001, "")["backing"]["uuid"]
    assert effect["blast_radius"]["object"]["vm_name"] == "app-vm"
    assert effect["blast_radius"]["children"] == []
    assert effect["blast_radius"]["irreversibility"] == "reversible-file-kept"

    resume = await _approve_and_run(request_id)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "removed"
    assert len(conn.calls_to("ReconfigVM_Task")) == 1
    assert conn.disk_keys() == [2000]


@pytest.mark.usefixtures("_dispatch_env")
async def test_a_disk_replaced_while_the_request_waits_is_not_removed() -> None:
    """The approver saw one disk; another disk with the same key is there now."""
    conn = _DiskVimFake()
    await _bootstrap(conn)
    args = {**_args("2001"), "params": {"vm": _VM, "disk": "2001", "delete_file": True}}
    request_id = await _park(_person("op-requester"), args)
    conn.replace_disk(2001, "Hard disk 3", "[<datastore>] app-vm/app-vm_2001_new.vmdk")

    resume = await _approve_and_run(request_id)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "precondition_failed"
    blocker = resume.result["blockers"][0]
    assert blocker["kind"] == "disk_changed"
    assert blocker["approved"]["vmdk_file"] == "[<datastore>] app-vm/app-vm_2001.vmdk"
    assert blocker["now"]["vmdk_file"] == "[<datastore>] app-vm/app-vm_2001_new.vmdk"
    assert conn.calls_to("ReconfigVM_Task") == []
    assert conn.disk_keys() == [2000, 2001]


@pytest.mark.usefixtures("_dispatch_env")
async def test_a_replacement_with_the_same_file_label_and_size_is_not_removed() -> None:
    """Only the disk UUID tells the replacement from the disk the approver saw."""
    conn = _DiskVimFake()
    await _bootstrap(conn)
    args = {**_args("2001"), "params": {"vm": _VM, "disk": "2001", "delete_file": True}}
    request_id = await _park(_person("op-requester"), args)
    conn.replace_disk(2001, "Hard disk 2")

    resume = await _approve_and_run(request_id)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "precondition_failed"
    blocker = resume.result["blockers"][0]
    assert blocker["kind"] == "disk_changed"
    assert blocker["approved"]["vmdk_file"] == blocker["now"]["vmdk_file"]
    assert blocker["approved"]["disk_uuid"] == _disk(2001, "")["backing"]["uuid"]
    assert blocker["now"]["disk_uuid"] == _disk(2001, "", generation=1)["backing"]["uuid"]
    assert conn.calls_to("ReconfigVM_Task") == []
    assert conn.disk_keys() == [2000, 2001]


@pytest.mark.usefixtures("_dispatch_env")
async def test_a_missing_disk_never_waits_for_approval() -> None:
    conn = _DiskVimFake()
    await _bootstrap(conn)
    requester = _person("op-requester")
    args = _args("2009")
    # The preview binds the params only; the live read runs when the call is sent.
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert "has no disk" in call["extras"]["reason"]
    assert await _approval_count() == 0
    assert conn.calls_to("ReconfigVM_Task") == []


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_remove_is_refused() -> None:
    conn = _DiskVimFake()
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_FakeTarget(),
        params={"vm": _VM, "disk": "2001"},
    )
    assert result.status == "denied", result
    assert "destructive" in (result.error or "")
    assert conn.calls_to("ReconfigVM_Task") == []
    assert await _approval_count() == 0
