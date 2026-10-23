# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for reading and answering the question a VM waits on.

Covers ``vmware.vm.question.get``, ``vmware.vm.question.answer`` (with its
approval preview), and the ``result_unknown`` answer of
``vmware.composite.vm.device.cdrom`` when the device change times out. A fake
connector records the calls, so no vCenter is needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._write import vm_device_cdrom_composite
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_question import (
    VM_QUESTION_GROUP_KEY,
    VMWARE_VM_QUESTION_ANSWER_OP,
    VMWARE_VM_QUESTION_GET_OP,
    vm_question_answer_impl,
    vm_question_answer_preview,
    vm_question_get_impl,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._validate import validate_params

_QUESTION = {
    "_typeName": "VirtualMachineQuestionInfo",
    "id": "_vmx1",
    "text": "The guest operating system has locked the CD-ROM door. Disconnect anyway?",
    "choice": {
        "_typeName": "ChoiceOption",
        "choiceInfo": [
            {"_typeName": "ElementDescription", "key": "0", "label": "Yes", "summary": "Yes"},
            {"_typeName": "ElementDescription", "key": "1", "label": "No", "summary": "No"},
        ],
        "defaultIndex": 1,
    },
    "message": [{"_typeName": "VirtualMachineMessage", "id": "msg.cdrom.locked", "text": "x"}],
}


def _operator() -> Operator:
    return Operator(
        sub="op-vm-question",
        name="VM Question Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _vm_result(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "objects": [
            {
                "obj": {"type": "VirtualMachine", "value": "vm-42"},
                "propSet": [{"name": k, "val": v} for k, v in props.items()],
            }
        ]
    }


def _tasks_result(infos: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "objects": [
            {"obj": {"type": "Task", "value": moid}, "propSet": [{"name": "info", "val": info}]}
            for moid, info in infos.items()
        ]
    }


def _fault(type_name: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://vc.test.invalid/sdk/vim25/9.0.0.0/x")
    response = httpx.Response(500, request=request, text=json.dumps({"_typeName": type_name}))
    return httpx.HTTPStatusError("500", request=request, response=response)


class _FakeConnector:
    """Answers the VM read, the task read and AnswerVM; records every call."""

    def __init__(
        self,
        *,
        question: dict[str, Any] | None = _QUESTION,
        tasks: dict[str, dict[str, Any]] | None = None,
        answer_error: Exception | None = None,
        write_error: Exception | None = None,
    ) -> None:
        self.question = question
        self.tasks = tasks or {}
        self.answer_error = answer_error
        self.write_error = write_error
        self.vmomi_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.write_calls: list[str] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(self, target: Any, query: Any, operator: Operator) -> Any:
        return query

    async def _get_json(self, target: Any, path: str, **kwargs: Any) -> Any:
        if path == "/api/vcenter/vm":
            return [{"vm": "vm-42", "name": "rhel-1"}]
        return {"backing": {"type": "ISO_FILE", "iso_file": "[ds1] a.iso"}, "state": "CONNECTED"}

    async def _post_json(self, target: Any, path: str, **kwargs: Any) -> Any:
        self.write_calls.append(path)
        if self.write_error is not None:
            raise self.write_error
        return None

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None, **kwargs: Any
    ) -> Any:
        self.vmomi_calls.append((path, json))
        if path.endswith("/AnswerVM"):
            if self.answer_error is not None:
                raise self.answer_error
            return None
        prop_spec = json["specSet"][0]["propSet"][0]
        if prop_spec["type"] == "Task":
            return _tasks_result(self.tasks)
        props: dict[str, Any] = {"name": "rhel-1"}
        if self.question is not None:
            props["runtime.question"] = self.question
        if self.tasks:
            props["recentTask"] = {
                "_typeName": "ArrayOfManagedObjectReference",
                "_value": [{"type": "Task", "value": m} for m in self.tasks],
            }
        return _vm_result(props)


_RUNNING = {"state": "running", "descriptionId": "VirtualMachine.reconfigure", "progress": 10}
_DONE = {"state": "success", "descriptionId": "VirtualMachine.powerOn"}


@pytest.mark.asyncio
async def test_get_reads_the_question_and_the_running_tasks() -> None:
    conn = _FakeConnector(tasks={"task-7": _RUNNING, "task-6": _DONE})
    out = await vm_question_get_impl(conn, _operator(), _Target(), {"vm": "vm-42"})  # type: ignore[arg-type]

    assert out["pending"] is True
    assert out["name"] == "rhel-1"
    question = out["question"]
    assert question["id"] == "_vmx1"
    assert question["choices"] == [
        {"key": "0", "label": "Yes", "summary": "Yes"},
        {"key": "1", "label": "No", "summary": "No"},
    ]
    assert question["default_choice"] == "1"
    assert question["messages"] == [{"id": "msg.cdrom.locked", "text": "x"}]
    assert out["running_tasks"] == [
        {
            "task": "task-7",
            "operation": "VirtualMachine.reconfigure",
            "state": "running",
            "progress": 10,
            "start_time": None,
        }
    ]
    vm_read = conn.vmomi_calls[0]
    assert vm_read[0] == "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    assert vm_read[1]["specSet"][0]["propSet"][0]["pathSet"] == [
        "name",
        "runtime.question",
        "recentTask",
    ]


@pytest.mark.asyncio
async def test_get_without_question_or_tasks_makes_one_read() -> None:
    conn = _FakeConnector(question=None)
    out = await vm_question_get_impl(conn, _operator(), _Target(), {"name": "rhel-1"})  # type: ignore[arg-type]

    assert out == {
        "vm": "vm-42",
        "name": "rhel-1",
        "pending": False,
        "question": None,
        "running_tasks": [],
    }
    assert len(conn.vmomi_calls) == 1


@pytest.mark.asyncio
async def test_answer_sends_answervm_on_the_vijson_path() -> None:
    conn = _FakeConnector()
    out = await vm_question_answer_impl(
        conn,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"vm": "vm-42", "question_id": "_vmx1", "answer": "0"},
    )

    assert out["status"] == "answered"
    assert out["answer_label"] == "Yes"
    assert conn.vmomi_calls[-1] == (
        "/VirtualMachine/vm-42/AnswerVM",
        {"questionId": "_vmx1", "answerChoice": "0"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [None, {**_QUESTION, "id": "_vmx2"}])
async def test_answer_sends_nothing_when_the_question_changed(
    question: dict[str, Any] | None,
) -> None:
    conn = _FakeConnector(question=question)
    out = await vm_question_answer_impl(
        conn,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"vm": "vm-42", "question_id": "_vmx1", "answer": "0"},
    )

    assert out["status"] == "question_gone"
    assert not any(path.endswith("/AnswerVM") for path, _ in conn.vmomi_calls)


@pytest.mark.asyncio
async def test_answer_sends_nothing_for_a_key_that_is_not_a_choice() -> None:
    conn = _FakeConnector()
    out = await vm_question_answer_impl(
        conn,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"vm": "vm-42", "question_id": "_vmx1", "answer": "Yes"},
    )

    assert out["status"] == "invalid_answer"
    assert [c["key"] for c in out["choices"]] == ["0", "1"]
    assert not any(path.endswith("/AnswerVM") for path, _ in conn.vmomi_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["ConcurrentAccess", "InvalidArgument"])
async def test_answer_fault_for_a_closed_question_is_question_gone(fault: str) -> None:
    conn = _FakeConnector(answer_error=_fault(fault))
    out = await vm_question_answer_impl(
        conn,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"vm": "vm-42", "question_id": "_vmx1", "answer": "0"},
    )
    assert out["status"] == "question_gone"


@pytest.mark.asyncio
async def test_answer_other_fault_is_raised() -> None:
    conn = _FakeConnector(answer_error=_fault("NoPermission"))
    with pytest.raises(httpx.HTTPStatusError):
        await vm_question_answer_impl(
            conn,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"vm": "vm-42", "question_id": "_vmx1", "answer": "0"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("vm", ["vm-42/../x", "vm-42\n", "vm 42"])
async def test_answer_refuses_a_vm_id_that_is_not_a_moid(vm: str) -> None:
    conn = _FakeConnector()
    with pytest.raises(ValueError, match="managed-object id"):
        await vm_question_answer_impl(
            conn,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"vm": vm, "question_id": "_vmx1", "answer": "0"},
        )
    assert conn.vmomi_calls == []
    errors = validate_params(
        VMWARE_VM_QUESTION_ANSWER_OP.parameter_schema,
        {"vm": vm, "question_id": "_vmx1", "answer": "0"},
    )
    assert errors


def _preview_ctx(conn: _FakeConnector, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=conn,  # type: ignore[arg-type]
        operator=_operator(),
        target=_Target(),
        params=params,
    )


@pytest.mark.asyncio
async def test_preview_shows_the_question_and_the_answer_label() -> None:
    conn = _FakeConnector(tasks={"task-7": _RUNNING})
    preview = await vm_question_answer_preview(
        _preview_ctx(conn, {"vm": "vm-42", "question_id": "_vmx1", "answer": "0"})
    )

    assert preview is not None
    assert preview["question_still_open"] is True
    assert preview["answer_label"] == "Yes"
    assert "Yes" in preview["summary"] and "CD-ROM door" in preview["summary"]
    assert [t["task"] for t in preview["running_tasks"]] == ["task-7"]
    assert _PREVIEW_BUILDERS[VMWARE_VM_QUESTION_ANSWER_OP.op_id] is vm_question_answer_preview


@pytest.mark.asyncio
async def test_preview_says_when_the_question_is_closed() -> None:
    conn = _FakeConnector(question=None)
    preview = await vm_question_answer_preview(
        _preview_ctx(conn, {"vm": "vm-42", "question_id": "_vmx1", "answer": "0"})
    )
    assert preview is not None
    assert preview["question_still_open"] is False
    assert "no longer waits" in preview["summary"]


@pytest.fixture
def _no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _clear(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(_write, "enforce_subop_policy", _clear)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_gate")
async def test_cdrom_change_that_times_out_says_the_result_is_unknown() -> None:
    conn = _FakeConnector(
        tasks={"task-7": _RUNNING},
        write_error=httpx.ReadTimeout("timed out"),
    )
    out = await vm_device_cdrom_composite(
        operator=_operator(),
        target=_Target(),
        params={
            "vm": "vm-42",
            "cdrom": "16000",
            "action": "update",
            "backing": {"type": "CLIENT_DEVICE"},
        },
        connector=conn,  # type: ignore[arg-type]
    )

    assert out["status"] == "result_unknown"
    assert [t["task"] for t in out["running_tasks"]] == ["task-7"]
    assert out["vm_question"]["id"] == "_vmx1"
    assert "task-7" in out["guidance"]
    assert "vmware.vm.question.answer" in out["guidance"]
    assert [i["severity"] for i in out["issues"]] == ["error"]
    assert out["current_backing"] == {"type": "ISO_FILE", "iso_file": "[ds1] a.iso"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_gate")
async def test_cdrom_timeout_still_says_unknown_when_the_follow_up_read_fails() -> None:
    conn = _FakeConnector(write_error=httpx.ReadTimeout("timed out"))

    async def _broken(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("down")

    conn._post_vmomi_json = _broken  # type: ignore[method-assign]
    out = await vm_device_cdrom_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "cdrom": "16000", "action": "remove"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "result_unknown"
    assert out["running_tasks"] == []
    assert out["vm_question"] is None
    assert "none found" in out["guidance"]


def test_both_ops_are_registered_with_the_right_safety() -> None:
    assert VMWARE_VM_QUESTION_GET_OP in VMWARE_TYPED_OPS
    assert VMWARE_VM_QUESTION_ANSWER_OP in VMWARE_TYPED_OPS
    assert VMWARE_VM_QUESTION_GET_OP.safety_level == "safe"
    assert VMWARE_VM_QUESTION_GET_OP.requires_approval is False
    assert VMWARE_VM_QUESTION_ANSWER_OP.safety_level == "dangerous"
    assert VMWARE_VM_QUESTION_ANSWER_OP.requires_approval is True
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[VM_QUESTION_GROUP_KEY]
