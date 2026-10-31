# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host membership changes and ``vmware.task.info`` over HTTP, on vCenter 8 and 9.

The real :class:`VmwareRestConnector` runs against ``respx``: the VI-JSON calls
go to ``/sdk/vim25/8.0.3.0/...`` on a vCenter 8.0 U3 and to
``/sdk/vim25/9.0.0.0/...`` on a vCenter 9.0. The vendor answers are written
by hand from the API reference, with made-up names and ids.

* ``host.disconnect`` and ``host.add`` send their method to the right path on
  both versions, and the disconnect polls its task to the end;
* ``vmware.task.info`` gives the vendor's state, progress, error and result;
  it logs in again when the login expired between the start and the read; it
  says ``not_reachable`` (with the task id and the time) when vCenter does not
  answer, and ``not_found`` for a task vCenter forgot.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.composites import (
    _host_add,
    _host_membership,
    _write,
)
from meho_backplane.connectors.vmware_rest.typed_ops_task_info import VMWARE_TASK_INFO_OP
from tests._vmware_teardown_fake import GateRecorder

_BASE = "https://vc.test.invalid"
_HOST = "host-1001"
_CLUSTER = "domain-c7"
_RELEASES = {"8.0.3": "8.0.3.0", "9.0.0.0": "9.0.0.0"}


@dataclass
class _Target:
    name: str = "example-vcenter"
    host: str = "vc.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _operator() -> Operator:
    return Operator(
        sub="op-host-membership-http",
        name="Host Membership HTTP Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-000000004349"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "<service-account>", "password": "<service-password>"}


def _connector() -> VmwareRestConnector:
    return VmwareRestConnector(session_loader=_loader)


async def _close(connector: VmwareRestConnector) -> None:
    for client in connector._clients.values():
        await client.aclose()
    connector._clients.clear()


def _object(mo_type: str, moid: str, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "objects": [
            {
                "obj": {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid},
                "propSet": [{"name": name, "val": val} for name, val in props.items()],
            }
        ]
    }


def _retrieve_responder(answers: dict[str, Callable[[], dict[str, Any]]]) -> Any:
    """Answer each ``RetrievePropertiesEx`` by the type it reads."""

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        mo_type = body["specSet"][0]["propSet"][0]["type"]
        return httpx.Response(200, json=answers[mo_type]())

    return respond


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


@pytest.mark.parametrize(("about", "release"), sorted(_RELEASES.items()))
async def test_disconnect_runs_on_the_vcenter_path_of_each_version(
    gate: GateRecorder, about: str, release: str
) -> None:
    state = {"connection": "connected"}

    def host() -> dict[str, Any]:
        return _object(
            "HostSystem",
            _HOST,
            {
                "name": "<esxi-host-name>",
                "parent": {
                    "_typeName": "ManagedObjectReference",
                    "type": "ClusterComputeResource",
                    "value": _CLUSTER,
                },
                "runtime.connectionState": state["connection"],
                "runtime.inMaintenanceMode": False,
                "vm": {"_typeName": "ArrayOfManagedObjectReference", "_value": []},
            },
        )

    def task() -> dict[str, Any]:
        state["connection"] = "disconnected"
        return _object("Task", "task-7", {"info": {"_typeName": "TaskInfo", "state": "success"}})

    connector = _connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.get("/api/about").respond(200, json={"version": about})
            mock.post("/api/session").respond(200, json="tok")
            base = f"/sdk/vim25/{release}"
            mock.post(f"{base}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=_retrieve_responder({"HostSystem": host, "Task": task})
            )
            disconnect = mock.post(f"{base}/HostSystem/{_HOST}/DisconnectHost_Task").respond(
                200, json={"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-7"}
            )
            out = await _host_membership.host_disconnect_composite(
                operator=_operator(), target=_Target(), params={"host": _HOST}, connector=connector
            )
        assert disconnect.called
        assert out["status"] == "disconnected"
        assert out["task"] == "task-7"
        assert out["connection_state"] == "disconnected"
    finally:
        await _close(connector)


@pytest.mark.parametrize(("about", "release"), sorted(_RELEASES.items()))
async def test_add_sends_add_host_on_the_vcenter_path_of_each_version(
    monkeypatch: pytest.MonkeyPatch, gate: GateRecorder, about: str, release: str
) -> None:
    monkeypatch.setattr(_host_add, "_list_host_moids", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        _host_add, "read_secret_param", AsyncMock(return_value="<host-password-from-vault>")
    )

    def cluster() -> dict[str, Any]:
        return _object("ClusterComputeResource", _CLUSTER, {"name": "<cluster-name>"})

    connector = _connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.get("/api/about").respond(200, json={"version": about})
            mock.post("/api/session").respond(200, json="tok")
            base = f"/sdk/vim25/{release}"
            mock.post(f"{base}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=_retrieve_responder({"ClusterComputeResource": cluster})
            )
            add = mock.post(f"{base}/ClusterComputeResource/{_CLUSTER}/AddHost_Task").respond(
                200, json={"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-9"}
            )
            out = await _host_add.host_add_composite(
                operator=_operator(),
                target=_Target(),
                params={
                    "cluster": _CLUSTER,
                    "host_name": "<esxi-host-name>",
                    "password_secret_ref": "<vault-path-of-the-host-password>",
                    "ssl_thumbprint": ":".join(["00"] * 20),
                },
                connector=connector,
            )
        assert out["status"] == "accepted"
        assert out["task"] == "task-9"
        sent = json.loads(add.calls.last.request.content)
        assert sent["asConnected"] is True
        assert sent["spec"]["_typeName"] == "HostConnectSpec"
    finally:
        await _close(connector)


# ===========================================================================
# vmware.task.info
# ===========================================================================

_RETRIEVE_80 = "/sdk/vim25/8.0.3.0/PropertyCollector/propertyCollector/RetrievePropertiesEx"


def _validate(out: dict[str, Any]) -> dict[str, Any]:
    assert VMWARE_TASK_INFO_OP.response_schema is not None
    Draft202012Validator(VMWARE_TASK_INFO_OP.response_schema).validate(out)
    return out


async def _task_info(connector: VmwareRestConnector, task: str = "task-9") -> dict[str, Any]:
    return _validate(await connector.task_info(_operator(), _Target(), {"task": task}))


async def test_task_info_gives_state_progress_and_result() -> None:
    info = {
        "_typeName": "TaskInfo",
        "state": "success",
        "progress": 100,
        "descriptionId": "ClusterComputeResource.addHost",
        "entity": {
            "_typeName": "ManagedObjectReference",
            "type": "ClusterComputeResource",
            "value": _CLUSTER,
        },
        "entityName": "<cluster-name>",
        "result": {"_typeName": "ManagedObjectReference", "type": "HostSystem", "value": _HOST},
    }
    connector = _connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post("/api/session").respond(200, json="tok")
            mock.post(_RETRIEVE_80).respond(200, json=_object("Task", "task-9", {"info": info}))
            out = await _task_info(connector)
    finally:
        await _close(connector)
    assert out["state"] == "success"
    assert out["progress"] == 100
    assert out["operation"] == "ClusterComputeResource.addHost"
    assert out["entity"] == _CLUSTER
    assert out["result"] == {"type": "HostSystem", "value": _HOST}
    assert out["reachable"] is True


async def test_task_info_gives_the_vendor_error_of_a_failed_task() -> None:
    info = {
        "_typeName": "TaskInfo",
        "state": "error",
        "error": {
            "_typeName": "LocalizedMethodFault",
            "localizedMessage": "Authenticity of the host's SSL certificate is not verified.",
            "fault": {"_typeName": "SSLVerifyFault"},
        },
    }
    connector = _connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post("/api/session").respond(200, json="tok")
            mock.post(_RETRIEVE_80).respond(200, json=_object("Task", "task-9", {"info": info}))
            out = await _task_info(connector)
    finally:
        await _close(connector)
    assert out["state"] == "error"
    assert out["error_type"] == "SSLVerifyFault"
    assert "SSL certificate" in (out["error_message"] or "")


async def test_task_info_logs_in_again_when_the_login_expired() -> None:
    info = {"_typeName": "TaskInfo", "state": "running", "progress": 40}
    expired = {"_typeName": "NotAuthenticated", "faultstring": "The session is not authenticated."}
    connector = _connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            session = mock.post("/api/session").mock(
                side_effect=[httpx.Response(200, json="tok-1"), httpx.Response(200, json="tok-2")]
            )
            read = mock.post(_RETRIEVE_80).mock(
                side_effect=[
                    httpx.Response(500, json=expired),
                    httpx.Response(200, json=_object("Task", "task-9", {"info": info})),
                ]
            )
            out = await _task_info(connector)
    finally:
        await _close(connector)
    assert session.call_count == 2
    assert read.call_count == 2
    assert read.calls.last.request.headers["vmware-api-session-id"] == "tok-2"
    assert out["state"] == "running"
    assert out["progress"] == 40


async def test_task_info_says_not_reachable_with_the_task_and_the_time() -> None:
    connector = _connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post("/api/session").respond(200, json="tok")
            mock.post(_RETRIEVE_80).mock(side_effect=httpx.ConnectError("vCenter is down"))
            out = await _task_info(connector)
    finally:
        await _close(connector)
    assert out["state"] == "not_reachable"
    assert out["reachable"] is False
    assert out["task"] == "task-9"
    assert out["checked_at"]
    assert out["unreachable_reason"] == "connect_failed"
    assert out["error_message"] is None


async def test_task_info_says_not_found_for_a_task_vcenter_forgot() -> None:
    gone = {
        "_typeName": "ManagedObjectNotFound",
        "faultstring": "The object has already been deleted.",
    }
    connector = _connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post("/api/session").respond(200, json="tok")
            mock.post(_RETRIEVE_80).respond(500, json=gone)
            out = await _task_info(connector)
    finally:
        await _close(connector)
    assert out["state"] == "not_found"
