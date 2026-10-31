# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``vmware.task.info`` typed read (the status read of a vendor job).

The real connector over mocked HTTP on the vCenter 8 VI-JSON path: each
vendor state, a failed task (fault type and text), a task vCenter no longer
knows, no answer from vCenter (``not_reachable``, never ``failed``), and a
login that expired between the start and the read (a new login, then the
state). Plus the operation metadata.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_task_info import VMWARE_TASK_INFO_OP
from meho_backplane.operations._validate import validate_params

_BASE = "https://vc.example.test"
_READ = "/sdk/vim25/8.0.3.0/PropertyCollector/propertyCollector/RetrievePropertiesEx"


def _operator() -> Operator:
    return Operator(
        sub="op-task-info",
        name="Task Info Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a4a6"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "<user>", "password": "<password>"}


@dataclass
class _Target:
    name: str = "vc-example"
    host: str = "vc.example.test"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


def _info_result(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "_typeName": "RetrieveResult",
        "objects": [
            {
                "_typeName": "ObjectContent",
                "obj": {"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-7"},
                "propSet": [{"_typeName": "DynamicProperty", "name": "info", "val": info}],
            }
        ],
    }


def _info(state: str, **extra: Any) -> dict[str, Any]:
    return {
        "_typeName": "TaskInfo",
        "key": "task-7",
        "state": state,
        "descriptionId": "HostSystem.enterMaintenanceMode",
        "entity": {"_typeName": "ManagedObjectReference", "type": "HostSystem", "value": "host-21"},
        "entityName": "<host-name>",
        "queueTime": "2026-01-01T10:00:00Z",
        **extra,
    }


async def _read(responses: list[httpx.Response | Exception]) -> tuple[dict[str, Any], Any]:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url=_BASE) as mock:
            session = mock.post("/api/session").mock(
                side_effect=[httpx.Response(200, json="tok-1"), httpx.Response(200, json="tok-2")]
            )
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            read = mock.post(_READ).mock(side_effect=responses)
            out = await connector.task_info(_operator(), _Target(), {"task": "task-7"})
        return out, (session, read)
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


@pytest.mark.parametrize("state", ["queued", "running"])
async def test_a_running_task_is_not_done(state: str) -> None:
    out, (_session, read) = await _read(
        [httpx.Response(200, json=_info_result(_info(state, progress=40)))]
    )
    assert out["state"] == state
    assert out["done"] is False
    assert out["reachable"] is True
    assert out["progress"] == 40
    assert out["operation"] == "HostSystem.enterMaintenanceMode"
    assert out["entity"] == "host-21"
    assert out["error_message"] is None
    body = json.loads(read.calls.last.request.content)
    assert body["specSet"][0]["propSet"][0] == {
        "_typeName": "PropertySpec",
        "type": "Task",
        "pathSet": ["info"],
    }


async def test_a_successful_task_is_done() -> None:
    out, _ = await _read(
        [httpx.Response(200, json=_info_result(_info("success", completeTime="t1")))]
    )
    assert out["state"] == "success"
    assert out["done"] is True
    assert out["complete_time"] == "t1"
    assert out["error_type"] is None


async def test_a_failed_task_gives_the_fault_type_and_text() -> None:
    error = {
        "_typeName": "LocalizedMethodFault",
        "fault": {"_typeName": "Timedout"},
        "localizedMessage": "Operation timed out.",
    }
    out, _ = await _read([httpx.Response(200, json=_info_result(_info("error", error=error)))])
    assert out["state"] == "error"
    assert out["done"] is True
    assert out["error_type"] == "Timedout"
    assert out["error_message"] == "Operation timed out."


async def test_an_unknown_task_is_not_found() -> None:
    fault = {"_typeName": "ManagedObjectNotFound", "obj": {"type": "Task", "value": "task-7"}}
    out, _ = await _read([httpx.Response(500, json=fault)])
    assert out["state"] == "not_found"
    assert out["done"] is False
    assert out["task"] == "task-7"


async def test_no_answer_is_not_reachable_with_task_and_time() -> None:
    out, _ = await _read([httpx.ConnectError("connection refused")])
    assert out["state"] == "not_reachable"
    assert out["reachable"] is False
    assert out["done"] is False
    assert out["task"] == "task-7"
    assert out["unreachable_reason"] == "connect_failed"
    assert out["checked_at"]
    assert "not a failure" in out["guidance"]


async def test_an_expired_login_logs_in_again_and_reads_the_task() -> None:
    expired = {"_typeName": "NotAuthenticated", "faultstring": "The session is not authenticated."}
    out, (session, read) = await _read(
        [
            httpx.Response(500, json=expired),
            httpx.Response(200, json=_info_result(_info("success"))),
        ]
    )
    assert out["state"] == "success"
    assert session.call_count == 2
    assert read.call_count == 2


def test_op_is_a_safe_typed_read_on_the_tasks_group() -> None:
    op = VMWARE_TASK_INFO_OP
    assert op in VMWARE_TYPED_OPS
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key is not None
    assert "vmware.task.info" in VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[op.group_key]
    assert callable(getattr(VmwareRestConnector, op.handler_attr))
    for example in op.parameter_schema["examples"]:
        validate_params(op.parameter_schema, example)
