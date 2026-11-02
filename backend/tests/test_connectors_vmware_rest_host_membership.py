# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler tests of the host membership changes (add, move, connect, disconnect, remove).

The handlers run against a recording fake of the connector's VI-JSON seam
(:class:`HostVim`, built on the shared teardown fake) with a stub of the
sub-op gate. Every answer is checked against the operation's response schema.

* each change: nothing is sent when the host already is as asked, the success
  path (the task is polled to its end and the host is read back), a task fault
  (the fault type and text reach the error), a timeout, and a parked gate;
* ``host.add``: the start answers ``accepted`` with the task id and the status
  read; a host already in vCenter and an add that already runs start nothing;
  a refused start and a start with no reply; the password reaches only the
  request body;
* ``host.remove``: the blast radius, the refusals, and the preview that
  refuses a host that does not exist.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.vmware_rest.composites import (
    _host_add,
    _host_membership,
    _host_remove,
    _write,
)
from meho_backplane.connectors.vmware_rest.composites._host_membership_schemas import (
    HOST_ADD_RESPONSE_SCHEMA,
    HOST_CONNECT_RESPONSE_SCHEMA,
    HOST_DISCONNECT_RESPONSE_SCHEMA,
    HOST_MOVE_RESPONSE_SCHEMA,
    HOST_REMOVE_RESPONSE_SCHEMA,
)
from meho_backplane.operations._preview import blast_radius_missing_reason
from tests._vmware_host_membership_fake import (
    CLUSTER,
    FOLDER,
    HOST,
    OTHER_CLUSTER,
    PASSWORD,
    STANDALONE,
    THUMBPRINT,
    HostVim,
    seed_host,
)
from tests._vmware_teardown_fake import (
    GateRecorder,
    http_status_error,
    moref,
    operator,
    parked,
    preview_ctx,
)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    monkeypatch.setattr(_host_membership, "_TASK_TIMEOUT_SECONDS", 0.0)
    return recorder


def _check(schema: dict[str, Any], out: Any) -> dict[str, Any]:
    assert isinstance(out, dict), out
    Draft202012Validator(schema).validate(out)
    return out


async def _move(conn: HostVim, cluster: str = OTHER_CLUSTER, target: Any = None) -> Any:
    return await _host_membership.host_move_composite(
        operator=operator(),
        target=target,
        params={"host": HOST, "cluster": cluster},
        connector=conn,
    )


async def _connect(conn: HostVim, *, connect: bool) -> Any:
    handler = (
        _host_membership.host_connect_composite
        if connect
        else _host_membership.host_disconnect_composite
    )
    return await handler(operator=operator(), target=None, params={"host": HOST}, connector=conn)


# ===========================================================================
# host.move
# ===========================================================================


async def test_move_sends_the_host_and_reads_it_back(gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn)
    out = _check(HOST_MOVE_RESPONSE_SCHEMA, await _move(conn))

    assert out["status"] == "moved"
    assert out["parent"] == OTHER_CLUSTER
    assert out["task"] == "task-1"
    [(path, body)] = conn.calls_to("MoveHostInto_Task")
    assert path == f"/ClusterComputeResource/{OTHER_CLUSTER}/MoveHostInto_Task"
    assert body == {"host": moref("HostSystem", HOST)}
    assert gate.calls[0]["op_id"] == "POST:/ClusterComputeResource/{moId}/MoveHostInto_Task"
    assert gate.calls[0]["params"] == {"host": HOST, "cluster": OTHER_CLUSTER}


async def test_move_sends_nothing_when_the_host_already_is_in_the_cluster(
    gate: GateRecorder,
) -> None:
    conn = HostVim()
    seed_host(conn)
    out = _check(HOST_MOVE_RESPONSE_SCHEMA, await _move(conn, cluster=CLUSTER))
    assert out["status"] == "unchanged"
    assert conn.calls_to("MoveHostInto_Task") == []
    assert gate.calls == []


async def test_move_refuses_a_host_outside_maintenance_mode(gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn, in_mm=False)
    out = _check(HOST_MOVE_RESPONSE_SCHEMA, await _move(conn))
    assert out["status"] == "not_in_maintenance_mode"
    assert "host.evacuate" in out["guidance"]
    assert conn.calls_to("MoveHostInto_Task") == []


async def test_move_answers_not_found_for_an_unknown_host_or_cluster(gate: GateRecorder) -> None:
    conn = HostVim()
    assert _check(HOST_MOVE_RESPONSE_SCHEMA, await _move(conn))["status"] == "not_found"
    seed_host(conn)
    out = _check(HOST_MOVE_RESPONSE_SCHEMA, await _move(conn, cluster="domain-c99"))
    assert out["status"] == "cluster_not_found"
    assert gate.calls == []


async def test_move_fault_reaches_the_error_with_type_and_text(gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn)
    conn.task_faults["MoveHostInto_Task"] = (
        "InvalidArgument",
        "The host is not part of the same datacenter as the cluster.",
    )
    with pytest.raises(RuntimeError) as err:
        await _move(conn)
    assert "InvalidArgument" in str(err.value)
    assert "same datacenter" in str(err.value)


async def test_move_timeout_returns_the_task_and_an_error_issue(gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn)
    conn.running.add("MoveHostInto_Task")
    out = _check(HOST_MOVE_RESPONSE_SCHEMA, await _move(conn))
    assert out["status"] == "timeout"
    assert out["task"] == "task-1"
    assert out["issues"][0]["severity"] == "error"
    assert "vmware.task.info" in out["guidance"]


async def test_move_returns_a_parked_gate_and_sends_nothing() -> None:
    conn = HostVim()
    seed_host(conn)
    verdict = parked("vmware.composite.host.move")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
        out = await _move(conn)
    assert out is verdict
    assert conn.calls_to("MoveHostInto_Task") == []


async def test_move_refuses_a_single_esxi_target(gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn)
    esxi = SimpleNamespace(fingerprint={"product": "esxi", "reachable": True})
    out = _check(HOST_MOVE_RESPONSE_SCHEMA, await _move(conn, target=esxi))
    assert out["status"] == "needs_vcenter"
    assert conn.calls == []


# ===========================================================================
# host.connect / host.disconnect
# ===========================================================================


async def test_connect_reconnects_and_reads_back(gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn, state="disconnected")
    out = _check(HOST_CONNECT_RESPONSE_SCHEMA, await _connect(conn, connect=True))
    assert out["status"] == "connected"
    assert out["connection_state"] == "connected"
    [(path, body)] = conn.calls_to("ReconnectHost_Task")
    assert path == f"/HostSystem/{HOST}/ReconnectHost_Task"
    assert body == {}


async def test_connect_sends_nothing_to_a_connected_host(gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn)
    out = _check(HOST_CONNECT_RESPONSE_SCHEMA, await _connect(conn, connect=True))
    assert out["status"] == "unchanged"
    assert gate.calls == []


async def test_connect_fault_reaches_the_error(gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn, state="notResponding")
    conn.task_faults["ReconnectHost_Task"] = ("InvalidLogin", "Cannot complete login.")
    with pytest.raises(RuntimeError, match=r"InvalidLogin.*Cannot complete login"):
        await _connect(conn, connect=True)


async def test_connect_says_not_verified_when_the_host_stays_disconnected(
    gate: GateRecorder,
) -> None:
    conn = HostVim()
    seed_host(conn, state="disconnected")
    conn.apply_writes = False
    out = _check(HOST_CONNECT_RESPONSE_SCHEMA, await _connect(conn, connect=True))
    assert out["status"] == "not_verified"


async def test_disconnect_disconnects_and_reads_back(gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn, in_mm=False)
    out = _check(HOST_DISCONNECT_RESPONSE_SCHEMA, await _connect(conn, connect=False))
    assert out["status"] == "disconnected"
    assert out["connection_state"] == "disconnected"
    assert gate.calls[0]["op_id"] == "POST:/HostSystem/{moId}/DisconnectHost_Task"
    [(path, _body)] = conn.calls_to("DisconnectHost_Task")
    assert path == f"/HostSystem/{HOST}/DisconnectHost_Task"


async def test_disconnect_sends_nothing_to_a_disconnected_host(gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn, state="disconnected")
    out = _check(HOST_DISCONNECT_RESPONSE_SCHEMA, await _connect(conn, connect=False))
    assert out["status"] == "unchanged"
    assert conn.calls_to("DisconnectHost_Task") == []


async def test_disconnect_fault_reaches_the_error(gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn)
    conn.task_faults["DisconnectHost_Task"] = ("InvalidState", "The operation is not allowed.")
    with pytest.raises(RuntimeError, match=r"InvalidState.*not allowed"):
        await _connect(conn, connect=False)


async def test_disconnect_answers_not_found_for_an_unknown_host(gate: GateRecorder) -> None:
    out = _check(HOST_DISCONNECT_RESPONSE_SCHEMA, await _connect(HostVim(), connect=False))
    assert out["status"] == "not_found"


# ===========================================================================
# host.add (a vendor job)
# ===========================================================================


def _add_params(**extra: Any) -> dict[str, Any]:
    return {
        "cluster": CLUSTER,
        "host_name": "<esxi-host-name>",
        "password_secret_ref": "<vault-path-of-the-host-password>",
        "ssl_thumbprint": THUMBPRINT,
        **extra,
    }


@pytest.fixture
def add_env(monkeypatch: pytest.MonkeyPatch, gate: GateRecorder) -> AsyncMock:
    """No host in vCenter yet; the Vault read gives the test password."""
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(_host_add, "_list_host_moids", listing)
    monkeypatch.setattr(_host_add, "read_secret_param", AsyncMock(return_value=PASSWORD))
    return listing


async def _add(conn: HostVim, params: dict[str, Any]) -> Any:
    return await _host_add.host_add_composite(
        operator=operator(), target=None, params=params, connector=conn
    )


async def test_add_to_a_cluster_answers_accepted_with_the_task(
    add_env: AsyncMock, gate: GateRecorder
) -> None:
    conn = HostVim()
    conn.add("ClusterComputeResource", CLUSTER, name="<cluster-name>", recentTask=[])
    out = _check(HOST_ADD_RESPONSE_SCHEMA, await _add(conn, _add_params()))

    assert out["status"] == "accepted"
    assert out["task"] == "task-1"
    assert out["status_read"] == {"op_id": "vmware.task.info", "params": {"task": "task-1"}}
    assert out["parent_name"] == "<cluster-name>"
    [(path, body)] = conn.calls_to("AddHost_Task")
    assert path == f"/ClusterComputeResource/{CLUSTER}/AddHost_Task"
    assert body == {
        "spec": {
            "_typeName": "HostConnectSpec",
            "hostName": "<esxi-host-name>",
            "sslThumbprint": THUMBPRINT,
            "userName": "root",
            "password": PASSWORD,
            "force": False,
        },
        "asConnected": True,
    }
    # The start is not polled: the answer comes back while the task runs.
    read_types = [
        b["specSet"][0]["propSet"][0]["type"] for _p, b in conn.calls_to("RetrievePropertiesEx")
    ]
    assert "Task" not in read_types
    # The password is in the vCenter body only: not in the gate, not in the answer.
    assert PASSWORD not in json.dumps(gate.calls, default=str)
    assert PASSWORD not in json.dumps(out)
    assert gate.calls[0]["op_id"] == "POST:/ClusterComputeResource/{moId}/AddHost_Task"


async def test_add_alone_to_a_folder_uses_add_standalone_host(add_env: AsyncMock) -> None:
    conn = HostVim()
    conn.add("Folder", FOLDER, name="<host-folder>", recentTask=[])
    params = _add_params(port=8443, user_name="<admin-user>")
    del params["cluster"]
    out = _check(HOST_ADD_RESPONSE_SCHEMA, await _add(conn, {**params, "folder": FOLDER}))

    assert out["status"] == "accepted"
    assert out["parent_type"] == "folder"
    [(path, body)] = conn.calls_to("AddStandaloneHost_Task")
    assert path == f"/Folder/{FOLDER}/AddStandaloneHost_Task"
    assert body["addConnected"] is True
    assert body["spec"]["port"] == 8443
    assert body["spec"]["userName"] == "<admin-user>"


async def test_add_starts_nothing_when_the_host_is_in_vcenter(add_env: AsyncMock) -> None:
    conn = HostVim()
    conn.add("ClusterComputeResource", CLUSTER, name="<cluster-name>", recentTask=[])
    add_env.return_value = [HOST]
    out = _check(HOST_ADD_RESPONSE_SCHEMA, await _add(conn, _add_params()))
    assert out["status"] == "already_added"
    assert out["host"] == HOST
    assert conn.calls_to("AddHost_Task") == []
    assert add_env.await_args.args[3] == {"filter.names": ["<esxi-host-name>"]}


async def test_add_returns_the_running_add_and_starts_nothing(add_env: AsyncMock) -> None:
    conn = HostVim()
    conn.add(
        "ClusterComputeResource",
        CLUSTER,
        name="<cluster-name>",
        recentTask=[moref("Task", "task-50"), moref("Task", "task-51")],
    )
    conn._tasks["task-50"] = {"state": "success", "descriptionId": "ClusterComputeResource.addHost"}
    conn._tasks["task-51"] = {"state": "running", "descriptionId": "ClusterComputeResource.addHost"}
    out = _check(HOST_ADD_RESPONSE_SCHEMA, await _add(conn, _add_params()))
    assert out["status"] == "already_running"
    assert out["task"] == "task-51"
    assert out["status_read"]["params"] == {"task": "task-51"}
    assert conn.calls_to("AddHost_Task") == []


async def test_add_finds_a_running_add_on_a_later_page(add_env: AsyncMock) -> None:
    """vCenter may split the task read into pages; the running add is on page two."""
    conn = HostVim()
    conn.page_size = 1
    conn.add(
        "ClusterComputeResource",
        CLUSTER,
        name="<cluster-name>",
        recentTask=[moref("Task", "task-50"), moref("Task", "task-51")],
    )
    conn._tasks["task-50"] = {"state": "success", "descriptionId": "ClusterComputeResource.addHost"}
    conn._tasks["task-51"] = {"state": "queued", "descriptionId": "ClusterComputeResource.addHost"}
    out = _check(HOST_ADD_RESPONSE_SCHEMA, await _add(conn, _add_params()))
    assert out["status"] == "already_running"
    assert out["task"] == "task-51"
    assert conn.calls_to("AddHost_Task") == []
    # The second page was read with the token, on the session of the first call.
    assert conn.continue_calls == [("<token-1>", "<session-1>")]
    assert conn._pending == {}


async def test_add_answers_parent_not_found(add_env: AsyncMock) -> None:
    out = _check(HOST_ADD_RESPONSE_SCHEMA, await _add(HostVim(), _add_params()))
    assert out["status"] == "parent_not_found"
    assert out["task"] is None


async def test_a_refused_start_gives_the_fault_without_the_password(add_env: AsyncMock) -> None:
    conn = HostVim()
    conn.add("ClusterComputeResource", CLUSTER, name="<cluster-name>", recentTask=[])
    fault = {"_typeName": "InvalidLogin", "faultstring": f"login failed for {PASSWORD}"}

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise http_status_error(500, json.dumps(fault))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_host_add, "_write_vmomi_sub_op", refuse)
        out = _check(HOST_ADD_RESPONSE_SCHEMA, await _add(conn, _add_params()))
    assert out["status"] == "start_failed"
    assert out["error_type"] == "InvalidLogin"
    assert PASSWORD not in json.dumps(out)


async def test_a_start_with_no_reply_says_start_unknown(add_env: AsyncMock) -> None:
    conn = HostVim()
    conn.add("ClusterComputeResource", CLUSTER, name="<cluster-name>", recentTask=[])

    async def lost(*_args: Any, **_kwargs: Any) -> Any:
        raise httpx.ReadTimeout("no reply")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_host_add, "_write_vmomi_sub_op", lost)
        out = _check(HOST_ADD_RESPONSE_SCHEMA, await _add(conn, _add_params()))
    assert out["status"] == "start_unknown"
    assert "Do not start again blindly" in out["guidance"]


@pytest.mark.parametrize("status_code", [502, 503, 504])
async def test_a_gateway_answer_to_the_start_says_start_unknown(
    add_env: AsyncMock, status_code: int
) -> None:
    """A gateway can answer after vCenter took the job, so the start is not 'failed'."""
    conn = HostVim()
    conn.add("ClusterComputeResource", CLUSTER, name="<cluster-name>", recentTask=[])

    async def gateway(*_args: Any, **_kwargs: Any) -> Any:
        raise http_status_error(status_code, "<html>gateway</html>")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_host_add, "_write_vmomi_sub_op", gateway)
        out = _check(HOST_ADD_RESPONSE_SCHEMA, await _add(conn, _add_params()))
    assert out["status"] == "start_unknown"
    assert out["error_message"] == f"HTTP {status_code}"
    assert out["error_type"] is None
    assert "Do not start again blindly" in out["guidance"]


async def test_add_returns_a_parked_gate(add_env: AsyncMock) -> None:
    conn = HostVim()
    conn.add("ClusterComputeResource", CLUSTER, name="<cluster-name>", recentTask=[])
    verdict = parked("vmware.composite.host.add")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
        out = await _add(conn, _add_params())
    assert out is verdict
    assert conn.calls_to("AddHost_Task") == []


async def test_add_preview_shows_the_plan_without_the_password() -> None:
    preview = await _host_add._host_add_preview(preview_ctx(_add_params(), HostVim()))
    assert preview is not None
    assert preview["parent"] == CLUSTER
    assert preview["ssl_thumbprint"] == THUMBPRINT
    assert preview["password_source"] == "vault"


# ===========================================================================
# host.remove (destructive)
# ===========================================================================


async def _remove(conn: HostVim) -> Any:
    return await _host_remove.host_remove_composite(
        operator=operator(), target=None, params={"host": HOST}, connector=conn
    )


@pytest.fixture
def remove_gate(monkeypatch: pytest.MonkeyPatch, gate: GateRecorder) -> GateRecorder:
    from meho_backplane.connectors.vmware_rest.composites import _teardown

    monkeypatch.setattr(_teardown, "_DESTROY_TASK_TIMEOUT_SECONDS", 0.0)
    return gate


async def test_remove_destroys_a_cluster_host_and_reads_it_back(remove_gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn)
    out = _check(HOST_REMOVE_RESPONSE_SCHEMA, await _remove(conn))
    assert out["status"] == "removed"
    assert [p for p, _ in conn.calls_to("Destroy_Task")] == [f"/HostSystem/{HOST}/Destroy_Task"]
    assert remove_gate.calls[0]["op_id"] == "POST:/HostSystem/{moId}/Destroy_Task"


async def test_remove_of_a_host_alone_destroys_its_compute_resource(
    remove_gate: GateRecorder,
) -> None:
    conn = HostVim()
    seed_host(conn, parent=moref("ComputeResource", STANDALONE))
    conn.add("ComputeResource", STANDALONE, name="<esxi-host-name>")
    out = _check(HOST_REMOVE_RESPONSE_SCHEMA, await _remove(conn))
    assert out["status"] == "removed"
    assert [p for p, _ in conn.calls_to("Destroy_Task")] == [
        f"/ComputeResource/{STANDALONE}/Destroy_Task"
    ]


async def test_remove_refuses_a_connected_host_outside_maintenance_mode(
    remove_gate: GateRecorder,
) -> None:
    conn = HostVim()
    seed_host(conn, in_mm=False)
    out = _check(HOST_REMOVE_RESPONSE_SCHEMA, await _remove(conn))
    assert out["status"] == "precondition_failed"
    assert conn.calls_to("Destroy_Task") == []


async def test_remove_of_a_disconnected_host_runs_without_maintenance_mode(
    remove_gate: GateRecorder,
) -> None:
    conn = HostVim()
    seed_host(conn, state="notResponding", in_mm=False)
    out = _check(HOST_REMOVE_RESPONSE_SCHEMA, await _remove(conn))
    assert out["status"] == "removed"


async def test_remove_of_a_gone_host_is_unchanged(remove_gate: GateRecorder) -> None:
    out = _check(HOST_REMOVE_RESPONSE_SCHEMA, await _remove(HostVim()))
    assert out["status"] == "unchanged"


async def test_remove_fault_reaches_the_error(remove_gate: GateRecorder) -> None:
    conn = HostVim()
    seed_host(conn)
    conn.task_faults["Destroy_Task"] = ("InvalidState", "The host is not in maintenance mode.")
    with pytest.raises(RuntimeError, match="not in maintenance mode"):
        await _remove(conn)


async def test_remove_preview_lists_the_host_and_its_vms() -> None:
    conn = HostVim()
    seed_host(conn, vms=("vm-1", "vm-2"))
    preview = await _host_remove.host_remove_preview(preview_ctx({"host": HOST}, conn))
    assert preview is not None
    assert blast_radius_missing_reason(preview) is None
    block = preview["blast_radius"]
    assert block["object"]["moid"] == HOST
    assert block["object"]["vm_count"] == 2
    assert [child["moid"] for child in block["children"]] == ["vm-1", "vm-2"]
    assert block["irreversibility"] == "permanent"
    assert "refusal" not in block


async def test_remove_preview_carries_the_refusal() -> None:
    conn = HostVim()
    seed_host(conn, in_mm=False)
    preview = await _host_remove.host_remove_preview(preview_ctx({"host": HOST}, conn))
    assert preview is not None
    assert preview["blast_radius"]["refusal"]["status"] == "precondition_failed"


async def test_remove_preview_refuses_a_host_that_does_not_exist() -> None:
    with pytest.raises(LookupError, match="nothing to remove"):
        await _host_remove.host_remove_preview(preview_ctx({"host": HOST}, HostVim()))
