# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Transport tests for the VM device changes, with real httpx (respx).

The changes run through the real :class:`VmwareRestConnector`:

* ``vmware.composite.vm.cpu_memory.options.set`` sends its VIM calls as
  VI-JSON to ``/sdk/vim25/{release}``: ``8.0.3.0`` for a vCenter 8.0 U3 and
  ``9.0.0.0`` for a vCenter 9.0. The task is polled to its end.
* ``vmware.composite.vm.nic.connect`` and ``vmware.composite.vm.serial_port.add``
  send their REST calls to ``/api``.

The policy check of the write seam is replaced by a recorder that lets the
write go on. Names and ids are made up.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.composites import _vm_cpu_memory_options as opts
from meho_backplane.connectors.vmware_rest.composites import _vm_nic_serial as nic_serial
from meho_backplane.connectors.vmware_rest.composites import _write

_BASE_URL = "https://vc.test.invalid"
_VM = "vm-42"


def _operator() -> Operator:
    return Operator(
        sub="op-vm-devices-transport",
        name="VM Devices Transport Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0d5"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    """Structural :class:`VsphereTargetLike` for a vCenter."""

    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-test"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-test", "password": "<test-password>"}


@pytest.fixture
async def connector() -> AsyncIterator[VmwareRestConnector]:
    conn = VmwareRestConnector(session_loader=_stub_loader)

    async def _aclose() -> None:
        conn._session_tokens.clear()
        for client in conn._clients.values():
            await client.aclose()
        conn._clients.clear()

    conn.aclose = _aclose  # type: ignore[method-assign]
    yield conn
    await conn.aclose()


@pytest.fixture(autouse=True)
def _open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _allow(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(_write, "enforce_subop_policy", _allow)


def _vm_props(cpu_hot_add: bool) -> dict[str, Any]:
    shares = {"_typeName": "SharesInfo", "level": "normal", "shares": 1000}
    alloc = {"_typeName": "ResourceAllocationInfo", "shares": shares}
    props = {
        "name": "vm-a",
        "runtime.powerState": "poweredOff",
        "config.cpuHotAddEnabled": cpu_hot_add,
        "config.memoryHotAddEnabled": False,
        "config.cpuAllocation": alloc,
        "config.memoryAllocation": alloc,
    }
    return {
        "objects": [
            {
                "obj": {
                    "_typeName": "ManagedObjectReference",
                    "type": "VirtualMachine",
                    "value": _VM,
                },
                "propSet": [{"name": k, "val": v} for k, v in props.items()],
            }
        ]
    }


_TASK_DONE = {
    "objects": [
        {
            "obj": {"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-9"},
            "propSet": [{"name": "info", "val": {"_typeName": "TaskInfo", "state": "success"}}],
        }
    ]
}


def _json(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


@pytest.mark.parametrize(("about", "release"), [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")])
async def test_options_use_the_vi_json_path_of_the_vcenter_release(
    connector: VmwareRestConnector, about: str, release: str
) -> None:
    sdk = f"/sdk/vim25/{release}"
    reads = iter([_vm_props(False), _TASK_DONE, _vm_props(True)])
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post("/api/session").respond(200, json="tok")
        mock.get("/api/about").respond(200, json={"version": about})
        retrieve = mock.post(
            f"{sdk}/PropertyCollector/propertyCollector/RetrievePropertiesEx"
        ).mock(side_effect=lambda _req: _json(next(reads)))
        reconfig = mock.post(f"{sdk}/VirtualMachine/{_VM}/ReconfigVM_Task").respond(
            200, json={"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-9"}
        )
        out = await opts.vm_cpu_memory_options_set_composite(
            operator=_operator(),
            target=_Target(),
            params={"vm": _VM, "cpu_hot_add_enabled": True},
            connector=connector,
        )
    assert reconfig.call_count == 1
    assert json.loads(reconfig.calls[0].request.content) == {
        "spec": {"_typeName": "VirtualMachineConfigSpec", "cpuHotAddEnabled": True}
    }
    assert retrieve.call_count == 3
    assert out["status"] == "set"
    assert out["after"]["cpu_hot_add_enabled"] is True


async def test_nic_connect_posts_the_connect_action_on_api(
    connector: VmwareRestConnector,
) -> None:
    nic_path = f"/api/vcenter/vm/{_VM}/hardware/ethernet/4000"
    states = iter(["NOT_CONNECTED", "CONNECTED"])
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post("/api/session").respond(200, json="tok")
        mock.get(nic_path).mock(
            side_effect=lambda _req: _json({"label": "Network adapter 1", "state": next(states)})
        )
        connect = mock.post(nic_path, params={"action": "connect"}).respond(204)
        out = await nic_serial.vm_nic_connect_composite(
            operator=_operator(),
            target=_Target(),
            params={"vm": _VM, "nic": "4000"},
            connector=connector,
        )
    assert connect.call_count == 1
    assert out["status"] == "connected"
    assert out["after"]["state"] == "CONNECTED"


async def test_serial_add_posts_a_flat_create_spec_on_api(
    connector: VmwareRestConnector,
) -> None:
    serial_path = f"/api/vcenter/vm/{_VM}/hardware/serial"
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post("/api/session").respond(200, json="tok")
        create = mock.post(serial_path).respond(201, json="9000")
        mock.get(f"{serial_path}/9000").respond(
            200, json={"label": "Serial port 1", "backing": {"type": "FILE", "file": "[ds] a.log"}}
        )
        out = await nic_serial.vm_serial_port_add_composite(
            operator=_operator(),
            target=_Target(),
            params={"vm": _VM, "file": "[ds] a.log"},
            connector=connector,
        )
    assert json.loads(create.calls[0].request.content) == {
        "backing": {"type": "FILE", "file": "[ds] a.log"},
        "start_connected": True,
    }
    assert out["status"] == "added"
    assert out["port"] == "9000"
    assert out["port_info"]["file"] == "[ds] a.log"
