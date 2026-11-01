# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vm.migrate_cross_vcenter`` -- the handler against a two-vCenter vim fake.

* The start sends ``RelocateVM_Task`` once, with the other vCenter's service
  locator (address, ``instanceUuid``, thumbprint, the Vault login) and the
  target objects there; the answer is ``accepted`` with the task id.
* The login never reaches the answer or the approval gate.
* A wrong thumbprint, a failed Vault read or login, an unreachable vCenter
  and a missing target object each stop the move before the start.
* A move that still runs is returned; a vendor fault on the start reaches
  the caller.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.vmware_rest.composites import (
    _vm_cross_vcenter,
    _vm_cross_vcenter_ops,
    _write,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from tests._vmware_cross_vcenter_fake import (
    CERT,
    DEST_NAME,
    DEST_UUID,
    LOGIN,
    THUMBPRINT,
    VM,
    CrossVimFake,
    dest_target,
    params,
    seed,
    source_target,
)
from tests._vmware_teardown_fake import (
    GateRecorder,
    http_status_error,
    moref,
    operator,
    parked,
    preview_ctx,
)

_OP = "vmware.composite.vm.migrate_cross_vcenter"


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


@pytest.fixture
def destination(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The other vCenter's target and the certificate it shows; tests may change both."""
    state: dict[str, Any] = {"target": dest_target(), "cert": CERT}

    async def _resolve(_operator: Any, name: str) -> Any:
        assert name == DEST_NAME
        return state["target"]

    async def _cert(_connector: Any, target: Any) -> bytes:
        assert target is state["target"]
        if isinstance(state["cert"], Exception):
            raise state["cert"]
        return bytes(state["cert"])

    monkeypatch.setattr(_vm_cross_vcenter, "resolve_destination", _resolve)
    monkeypatch.setattr(_vm_cross_vcenter, "presented_certificate", _cert)
    return state


async def _move(conn: CrossVimFake, **extra: Any) -> Any:
    return await _vm_cross_vcenter.vm_migrate_cross_vcenter_composite(
        operator=operator(), target=source_target(), params=params(**extra), connector=conn
    )


def _seeded(**kwargs: Any) -> CrossVimFake:
    conn = CrossVimFake()
    seed(conn, **kwargs)
    return conn


@pytest.mark.usefixtures("destination")
async def test_the_move_starts_once_with_the_service_locator(gate: GateRecorder) -> None:
    conn = _seeded()
    out = await _move(conn, priority="highPriority")

    assert out["status"] == "accepted", out
    assert out["task"] == "task-1"
    assert out["status_read"] == {"op_id": "vmware.task.info", "params": {"task": "task-1"}}
    assert out["vm_name"] == "<vm-name>"
    (path, body), *rest = conn.starts()
    assert rest == []
    assert path == f"/VirtualMachine/{VM}/RelocateVM_Task"
    assert body["priority"] == "highPriority"
    spec = body["spec"]
    assert spec["_typeName"] == "VirtualMachineRelocateSpec"
    assert spec["service"] == {
        "_typeName": "ServiceLocator",
        "instanceUuid": DEST_UUID.upper(),
        "url": "https://vc-b.example.test",
        "credential": {"_typeName": "ServiceLocatorNamePassword", **LOGIN},
        "sslThumbprint": THUMBPRINT,
    }
    assert spec["host"]["value"] == "host-21"
    assert spec["pool"]["value"] == "resgroup-8"
    assert spec["datastore"]["value"] == "datastore-12"
    assert spec["folder"]["value"] == "group-v3"
    (change,) = spec["deviceChange"]
    assert change["operation"] == "edit"
    assert change["device"]["key"] == 4000
    assert change["device"]["macAddress"] == "00:50:56:00:00:01"
    assert change["device"]["backing"]["port"] == {
        "_typeName": "DistributedVirtualSwitchPortConnection",
        "switchUuid": "dest-switch-uuid",
        "portgroupKey": "dvportgroup-34",
    }


@pytest.mark.usefixtures("destination")
async def test_the_login_reaches_neither_the_answer_nor_the_gate(gate: GateRecorder) -> None:
    conn = _seeded()
    out = await _move(conn)
    assert out["status"] == "accepted"
    assert LOGIN["password"] not in json.dumps(out)
    (call,) = gate.calls
    assert call["op_id"] == "POST:/VirtualMachine/{moId}/RelocateVM_Task"
    assert LOGIN["password"] not in json.dumps(call["params"], default=str)
    assert call["params"]["destination_target"] == DEST_NAME
    assert call["params"]["networks"] == {"4000": "dvportgroup-34"}


@pytest.mark.usefixtures("destination")
async def test_a_card_can_use_its_own_standard_network(gate: GateRecorder) -> None:
    conn = _seeded(nics=2)
    out = await _move(conn, nics=[{"nic_key": 4001, "network": "network-15"}])
    assert out["status"] == "accepted", out
    assert out["requested"]["networks"] == {"4000": "dvportgroup-34", "4001": "network-15"}
    changes = {
        c["device"]["key"]: c["device"]["backing"]
        for c in conn.starts()[0][1]["spec"]["deviceChange"]
    }
    assert changes[4001] == {
        "_typeName": "VirtualEthernetCardNetworkBackingInfo",
        "deviceName": "<network-name>",
        "network": moref("Network", "network-15"),
    }
    assert changes[4000]["_typeName"] == "VirtualEthernetCardDistributedVirtualPortBackingInfo"


@pytest.mark.usefixtures("destination")
async def test_a_parked_gate_is_returned_and_nothing_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(parked(_OP)))
    conn = _seeded()
    out = await _move(conn)
    assert out.status == "awaiting_approval"
    assert conn.starts() == []


async def test_a_wrong_thumbprint_stops_before_the_login(
    gate: GateRecorder, destination: dict[str, Any]
) -> None:
    destination["cert"] = b"another-certificate"
    conn = _seeded()
    out = await _move(conn)
    assert out["status"] == "certificate_mismatch", out
    assert "ssl_thumbprint" in out["guidance"]
    assert conn.loader_calls == []
    assert conn.starts() == []
    assert gate.calls == []


@pytest.mark.parametrize("thumbprint", [None, "", "AB:CD", "zz" * 20])
async def test_a_missing_or_malformed_thumbprint_is_refused(
    gate: GateRecorder, destination: dict[str, Any], thumbprint: Any
) -> None:
    destination["target"] = dest_target(ssl_thumbprint=thumbprint)
    conn = _seeded()
    out = await _move(conn)
    assert out["status"] == "invalid_request", out
    assert "ssl_thumbprint" in out["guidance"]
    assert conn.starts() == []


async def test_the_thumbprint_may_be_written_without_colons(
    gate: GateRecorder, destination: dict[str, Any]
) -> None:
    destination["target"] = dest_target(ssl_thumbprint=THUMBPRINT.replace(":", "").lower())
    conn = _seeded()
    out = await _move(conn)
    assert out["status"] == "accepted", out
    assert conn.starts()[0][1]["spec"]["service"]["sslThumbprint"] == THUMBPRINT


async def test_an_unreachable_vcenter_stops_the_move(
    gate: GateRecorder, destination: dict[str, Any]
) -> None:
    destination["cert"] = httpx.ConnectError("refused")
    conn = _seeded()
    out = await _move(conn)
    assert out["status"] == "destination_unreachable", out
    assert conn.starts() == []


@pytest.mark.usefixtures("destination")
@pytest.mark.parametrize(
    ("where", "fault"),
    [
        ("login_fault", VaultCredentialsReadError("no secret at the path")),
        ("content_fault", RuntimeError("vsphere session establish failed: HTTP 401")),
    ],
)
async def test_a_failed_login_stops_the_move(
    gate: GateRecorder, where: str, fault: Exception
) -> None:
    conn = _seeded()
    setattr(conn, where, fault)
    out = await _move(conn)
    assert out["status"] == "destination_login_failed", out
    assert conn.starts() == []
    assert gate.calls == []


@pytest.mark.usefixtures("destination")
async def test_a_transport_error_on_the_login_is_unreachable(gate: GateRecorder) -> None:
    conn = _seeded()
    conn.content_fault = httpx.ReadTimeout("timed out")
    out = await _move(conn)
    assert out["status"] == "destination_unreachable", out
    assert conn.starts() == []


@pytest.mark.usefixtures("destination")
async def test_missing_objects_in_the_other_vcenter_are_listed(gate: GateRecorder) -> None:
    conn = _seeded()
    conn.dest.objects.pop(("Datastore", "datastore-12"))
    conn.dest.objects.pop(("DistributedVirtualPortgroup", "dvportgroup-34"))
    out = await _move(conn)
    assert out["status"] == "not_found", out
    assert out["missing"] == ["datastore datastore-12", "network dvportgroup-34"]
    assert conn.starts() == []


@pytest.mark.usefixtures("destination")
async def test_an_unknown_vm_is_not_found(gate: GateRecorder) -> None:
    conn = _seeded()
    out = await _move(conn, vm="vm-99")
    assert out["status"] == "not_found"
    assert conn.loader_calls == []


@pytest.mark.usefixtures("destination")
@pytest.mark.parametrize(
    ("extra", "text"),
    [
        ({"network": None}, "need a network"),
        ({"nics": [{"nic_key": 4999, "network": "network-15"}]}, "no network card"),
        ({"host": "esx-1"}, "host"),
        ({"network": "segment-1"}, "network"),
    ],
)
async def test_bad_inputs_are_refused(gate: GateRecorder, extra: dict[str, Any], text: str) -> None:
    conn = _seeded()
    call = params(**extra)
    call = {k: v for k, v in call.items() if v is not None}
    out = await _vm_cross_vcenter.vm_migrate_cross_vcenter_composite(
        operator=operator(), target=source_target(), params=call, connector=conn
    )
    assert out["status"] == "invalid_request", out
    assert text in out["guidance"]
    assert conn.starts() == []


async def test_the_own_vcenter_is_refused(gate: GateRecorder, destination: dict[str, Any]) -> None:
    same = source_target()
    same.extras = {"ssl_thumbprint": THUMBPRINT}
    destination["target"] = same
    conn = _seeded()
    out = await _move(conn)
    assert out["status"] == "invalid_request"
    assert "vm.migrate" in out["guidance"]


async def test_a_target_that_is_not_a_vcenter_is_refused(
    gate: GateRecorder, destination: dict[str, Any]
) -> None:
    destination["target"].product = "nsx"
    out = await _move(_seeded())
    assert out["status"] == "invalid_request"


@pytest.mark.usefixtures("destination")
async def test_a_move_that_still_runs_is_returned(gate: GateRecorder) -> None:
    conn = _seeded()
    conn.objects[("VirtualMachine", VM)]["recentTask"] = [moref("Task", "task-7")]
    conn.task("task-7", state="running", descriptionId="VirtualMachine.relocate")
    out = await _move(conn)
    assert out["status"] == "already_running", out
    assert out["task"] == "task-7"
    assert out["status_read"]["params"] == {"task": "task-7"}
    assert conn.starts() == []


@pytest.mark.usefixtures("destination")
async def test_a_vendor_fault_on_the_start_reaches_the_caller(gate: GateRecorder) -> None:
    conn = _seeded()
    conn.start_fault = http_status_error(
        500, '{"_typeName": "InvalidArgument", "faultstring": "A parameter was not correct"}'
    )
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await _move(conn)
    assert "InvalidArgument" in raised.value.response.text


async def test_the_approval_card_shows_the_move_and_no_login() -> None:
    assert _PREVIEW_BUILDERS[_OP] is _vm_cross_vcenter_ops.vm_migrate_cross_vcenter_preview
    builder = _PREVIEW_BUILDERS[_OP]
    preview = await builder(preview_ctx(params(), CrossVimFake()))
    assert preview is not None
    assert preview["action"] == "migrate_vm_cross_vcenter"
    assert preview["destination_target"] == DEST_NAME
    assert preview["requested"]["datastore"] == "datastore-12"
    assert LOGIN["password"] not in json.dumps(preview)
