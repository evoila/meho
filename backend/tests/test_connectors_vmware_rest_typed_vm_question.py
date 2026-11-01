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
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import SpillResult
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
from meho_backplane.jsonflux.query.json_pointer import parse_json_pointer, resolve_json_pointer
from meho_backplane.operations._errors import result_composite_terminal_error
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations._vendor_text import redact_error_result
from meho_backplane.operations.approval_outcome import (
    resume_outcome_from_envelope,
    with_approval_guidance,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

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


def _vm_result(props: dict[str, Any], missing: dict[str, str] | None = None) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "obj": {"type": "VirtualMachine", "value": "vm-42"},
        "propSet": [{"name": k, "val": v} for k, v in props.items()],
    }
    if missing:
        obj["missingSet"] = [
            {
                "_typeName": "MissingProperty",
                "path": path,
                "fault": {"_typeName": "LocalizedMethodFault", "fault": {"_typeName": fault}},
            }
            for path, fault in missing.items()
        ]
    return {"objects": [obj]}


def _tasks_result(
    infos: dict[str, dict[str, Any]], faults: dict[str, str] | None = None
) -> dict[str, Any]:
    """One ``Task`` object per moid: ``info`` in ``propSet``, or a ``missingSet`` fault."""
    objects: list[dict[str, Any]] = []
    for moid, info in infos.items():
        obj: dict[str, Any] = {"obj": {"type": "Task", "value": moid}}
        fault = (faults or {}).get(moid)
        if fault is None:
            obj["propSet"] = [{"name": "info", "val": info}]
        else:
            obj["propSet"] = []
            obj["missingSet"] = [
                {
                    "_typeName": "MissingProperty",
                    "path": "info",
                    "fault": {"_typeName": "LocalizedMethodFault", "fault": {"_typeName": fault}},
                }
            ]
        objects.append(obj)
    return {"objects": objects}


def _fault(type_name: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://vc.test.invalid/sdk/vim25/9.0.0.0/x")
    response = httpx.Response(500, request=request, text=json.dumps({"_typeName": type_name}))
    return httpx.HTTPStatusError("500", request=request, response=response)


class _FakeConnector:
    """Answers the VM read, the task read and AnswerVM; records every call."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(
        self,
        *,
        question: dict[str, Any] | None = _QUESTION,
        tasks: dict[str, dict[str, Any]] | None = None,
        answer_error: Exception | None = None,
        write_error: Exception | None = None,
        task_read_error: Exception | None = None,
        missing: dict[str, str] | None = None,
        task_faults: dict[str, str] | None = None,
    ) -> None:
        self.question = question
        self.tasks = tasks or {}
        self.answer_error = answer_error
        self.write_error = write_error
        self.task_read_error = task_read_error
        self.missing = missing
        self.task_faults = task_faults
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
            if self.task_read_error is not None:
                raise self.task_read_error
            return _tasks_result(self.tasks, self.task_faults)
        props: dict[str, Any] = {"name": "rhel-1"}
        if self.question is not None:
            props["runtime.question"] = self.question
        if self.tasks:
            props["recentTask"] = {
                "_typeName": "ArrayOfManagedObjectReference",
                "_value": [{"type": "Task", "value": m} for m in self.tasks],
            }
        return _vm_result(props, self.missing)


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
        "tasks_error": None,
    }
    assert len(conn.vmomi_calls) == 1


@pytest.fixture
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The reducer reads its size limits from settings, which need these env vars."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_settings_env")
async def test_get_over_4kb_keeps_the_whole_question_inline_through_the_reducer() -> None:
    """A long task list reduces the result to a handle; the question survives.

    Runs the real JSONFlux reducer with the op's ``result_objects`` hint, as
    the dispatcher does. Without the hint the reducer keeps only the scalar
    siblings of ``running_tasks`` and drops ``question``.
    """
    tasks = {f"task-{i}": {**_RUNNING, "startTime": "2026-10-24T10:00:00Z"} for i in range(40)}
    conn = _FakeConnector(tasks=tasks)
    out = await vm_question_get_impl(conn, _operator(), _Target(), {"vm": "vm-42"})  # type: ignore[arg-type]
    assert len(json.dumps(out)) > 4096
    assert len(out["running_tasks"]) == 40

    context = {
        "op_id": VMWARE_VM_QUESTION_GET_OP.op_id,
        "result_objects": VMWARE_VM_QUESTION_GET_OP.llm_instructions["result_objects"],  # type: ignore[index]
    }
    reduced, handle = await JsonFluxReducer(sample_size=5).reduce(out, None, context)

    assert handle is not None
    assert reduced["row_count"] == 40
    assert reduced["question"] == out["question"]
    assert reduced["pending"] is True
    assert reduced["vm"] == "vm-42"
    assert reduced["tasks_error"] is None
    assert "result_object_truncations" not in reduced


class _RecordingStore:
    """Stand-in for the handle store: records what the reducer keeps with the rows."""

    def __init__(self) -> None:
        self.documents: list[dict[str, Any] | None] = []

    async def spill(self, *, rows: list[dict[str, Any]], **kwargs: Any) -> SpillResult:
        self.documents.append(kwargs.get("document"))
        return SpillResult(
            stored_rows=len(rows), document_stored=kwargs.get("document") is not None
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_settings_env")
async def test_get_with_a_long_question_keeps_the_whole_question_in_the_handle() -> None:
    """Choices the inline projection cuts are still there for ``result_query``.

    A question with many long choice summaries does not fit the reducer's
    inline budget for a hinted object. The inline copy is cut and says so;
    the complete question is stored with the rows, so a ``path`` read of
    ``/question`` (or ``/question/choices``) returns every choice.
    """
    choices = [
        {
            "_typeName": "ElementDescription",
            "key": str(i),
            "label": f"option {i}",
            "summary": "s" * 120,
        }
        for i in range(30)
    ]
    question = {**_QUESTION, "choice": {"choiceInfo": choices, "defaultIndex": 0}}
    tasks = {f"task-{i}": _RUNNING for i in range(60)}
    conn = _FakeConnector(question=question, tasks=tasks)
    out = await vm_question_get_impl(conn, _operator(), _Target(), {"vm": "vm-42"})  # type: ignore[arg-type]
    assert len(out["question"]["choices"]) == 30

    store = _RecordingStore()
    context = {
        "op_id": VMWARE_VM_QUESTION_GET_OP.op_id,
        "operator_sub": "op-vm-question",
        "tenant_id": "00000000-0000-0000-0000-00000000a0a0",
        "result_objects": VMWARE_VM_QUESTION_GET_OP.llm_instructions["result_objects"],  # type: ignore[index]
    }
    reduced, handle = await JsonFluxReducer(
        sample_size=5,
        store=store,  # type: ignore[arg-type]
        max_spill_rows=10000,
    ).reduce(out, None, context)

    assert handle is not None
    assert reduced["question"]["id"] == "_vmx1"
    inline_choices = reduced["question"]["choices"]
    assert 0 < len(inline_choices) < 30
    assert reduced["result_object_truncations"] == {"question": ["choices"]}
    assert 'query={"path": "/question"}' in reduced["result_object_note"]
    document = store.documents[0]
    assert document is not None
    assert document["question"] == out["question"]
    omitted = resolve_json_pointer(document, parse_json_pointer("/question/choices"))[
        len(inline_choices) :
    ]
    assert omitted
    assert omitted[-1] == {"key": "29", "label": "option 29", "summary": "s" * 120}


@pytest.mark.asyncio
async def test_get_keeps_the_question_when_only_the_task_read_fails() -> None:
    conn = _FakeConnector(tasks={"task-7": _RUNNING}, task_read_error=httpx.ConnectError("down"))
    out = await vm_question_get_impl(conn, _operator(), _Target(), {"vm": "vm-42"})  # type: ignore[arg-type]

    assert out["pending"] is True
    assert out["question"]["id"] == "_vmx1"
    assert out["running_tasks"] == []
    assert out["tasks_error"] is not None
    assert "ConnectError" in out["tasks_error"]
    assert len(conn.vmomi_calls) == 2


@pytest.mark.asyncio
async def test_get_reports_a_task_whose_info_vcenter_could_not_read() -> None:
    """The HTTP read succeeds, but one ``Task.info`` comes back as a ``missingSet`` fault.

    That task may still run, so the result must not look like "no running
    task": ``running_tasks`` is empty and ``tasks_error`` names the fault. The
    question is kept.
    """
    conn = _FakeConnector(
        tasks={"task-7": _RUNNING, "task-6": _DONE}, task_faults={"task-7": "NoPermission"}
    )
    out = await vm_question_get_impl(conn, _operator(), _Target(), {"vm": "vm-42"})  # type: ignore[arg-type]

    assert out["pending"] is True
    assert out["question"]["id"] == "_vmx1"
    assert out["running_tasks"] == []
    assert out["tasks_error"] == (
        "vCenter could not read info of 1 of 2 task(s) (NoPermission); "
        "whether they still run is unknown"
    )
    assert len(conn.vmomi_calls) == 2


@pytest.mark.asyncio
async def test_get_reports_a_task_vcenter_left_out_of_its_answer() -> None:
    """A task with neither ``info`` nor a fault in the answer is unknown, not finished."""
    conn = _FakeConnector(tasks={"task-7": _RUNNING, "task-6": _DONE})
    full_result = conn._post_vmomi_json

    async def _drop_one(target: Any, path: str, **kwargs: Any) -> Any:
        result = await full_result(target, path, **kwargs)
        if kwargs["json"]["specSet"][0]["propSet"][0]["type"] == "Task":
            result["objects"] = [o for o in result["objects"] if o["obj"]["value"] != "task-7"]
        return result

    conn._post_vmomi_json = _drop_one  # type: ignore[method-assign]
    out = await vm_question_get_impl(conn, _operator(), _Target(), {"vm": "vm-42"})  # type: ignore[arg-type]

    assert out["question"]["id"] == "_vmx1"
    assert out["running_tasks"] == []
    assert out["tasks_error"] == (
        "vCenter could not read info of 1 of 2 task(s) (no answer); "
        "whether they still run is unknown"
    )


@pytest.mark.asyncio
async def test_get_reports_an_unreadable_task_list_without_a_second_read() -> None:
    conn = _FakeConnector(missing={"recentTask": "NoPermission"})
    out = await vm_question_get_impl(conn, _operator(), _Target(), {"vm": "vm-42"})  # type: ignore[arg-type]

    assert out["question"]["id"] == "_vmx1"
    assert out["running_tasks"] == []
    assert out["tasks_error"] == "vCenter could not read recentTask (NoPermission)"
    assert len(conn.vmomi_calls) == 1


@pytest.mark.asyncio
async def test_get_fails_when_vcenter_cannot_read_the_question() -> None:
    """An unreadable ``runtime.question`` is a failure, not ``pending=false``."""
    conn = _FakeConnector(question=None, missing={"runtime.question": "NoPermission"})
    with pytest.raises(RuntimeError, match=r"runtime\.question .*NoPermission"):
        await vm_question_get_impl(conn, _operator(), _Target(), {"vm": "vm-42"})  # type: ignore[arg-type]


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
async def test_answer_still_answers_when_only_the_task_read_fails() -> None:
    conn = _FakeConnector(tasks={"task-7": _RUNNING}, task_read_error=httpx.ConnectError("down"))
    out = await vm_question_answer_impl(
        conn,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"vm": "vm-42", "question_id": "_vmx1", "answer": "0"},
    )

    assert out["status"] == "answered"
    assert conn.vmomi_calls[-1][0] == "/VirtualMachine/vm-42/AnswerVM"


@pytest.mark.asyncio
async def test_answer_sends_nothing_when_vcenter_cannot_read_the_question() -> None:
    conn = _FakeConnector(question=None, missing={"runtime.question": "NoPermission"})
    with pytest.raises(RuntimeError, match=r"runtime\.question"):
        await vm_question_answer_impl(
            conn,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"vm": "vm-42", "question_id": "_vmx1", "answer": "0"},
        )
    assert not any(path.endswith("/AnswerVM") for path, _ in conn.vmomi_calls)


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
    assert preview["tasks_error"] is None
    assert _PREVIEW_BUILDERS[VMWARE_VM_QUESTION_ANSWER_OP.op_id] is vm_question_answer_preview


@pytest.mark.asyncio
async def test_preview_still_shows_the_question_when_the_task_read_fails() -> None:
    conn = _FakeConnector(tasks={"task-7": _RUNNING}, task_read_error=httpx.ConnectError("down"))
    preview = await vm_question_answer_preview(
        _preview_ctx(conn, {"vm": "vm-42", "question_id": "_vmx1", "answer": "0"})
    )
    assert preview is not None
    assert preview["question_still_open"] is True
    assert preview["running_tasks"] == []
    assert "ConnectError" in preview["tasks_error"]


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
    assert out["tasks_error"] is None
    assert out["vm_question"]["id"] == "_vmx1"
    assert "task-7" in out["guidance"]
    assert "vmware.vm.question.answer" in out["guidance"]
    assert [i["severity"] for i in out["issues"]] == ["error"]
    assert out["current_backing"] == {"type": "ISO_FILE", "iso_file": "[ds1] a.iso"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_gate")
async def test_cdrom_timeout_keeps_the_question_when_only_the_task_read_fails() -> None:
    conn = _FakeConnector(
        tasks={"task-7": _RUNNING},
        write_error=httpx.ReadTimeout("timed out"),
        task_read_error=httpx.ConnectError("down"),
    )
    out = await vm_device_cdrom_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "cdrom": "16000", "action": "remove"},
        connector=conn,  # type: ignore[arg-type]
    )

    assert out["status"] == "result_unknown"
    assert out["vm_question"]["id"] == "_vmx1"
    assert out["running_tasks"] == []
    assert "ConnectError" in out["tasks_error"]
    assert "unknown" in out["guidance"]
    assert "vmware.vm.question.answer" in out["guidance"]


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
    assert "ConnectError" in out["tasks_error"]
    assert "unknown" in out["guidance"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_gate")
async def test_approved_cdrom_timeout_run_summary_names_the_question() -> None:
    """The approval run summary carries the result word and the guidance (#5377).

    The composite's answer goes the way the dispatcher sends it: it becomes the
    error result of the run (#3809), passes the secret cleanup, and is stored
    on the approval request with the guidance stamp. The run summary then
    names the question id.
    """
    conn = _FakeConnector(tasks={"task-7": _RUNNING}, write_error=httpx.ReadTimeout("timed out"))
    out = await vm_device_cdrom_composite(
        operator=_operator(),
        target=_Target(),
        params={"vm": "vm-42", "cdrom": "16000", "action": "disconnect"},
        connector=conn,  # type: ignore[arg-type]
    )
    result = redact_error_result(
        result_composite_terminal_error(
            "vmware.composite.vm.device.cdrom",
            error_code=out["status"],
            envelope=out,
            error_summary="result_unknown: the device change timed out",
            duration_ms=30175.0,
        )
    )

    stored = with_approval_guidance(
        result.model_dump(mode="json"), parked_op_id="vmware.composite.vm.device.cdrom"
    )
    outcome = resume_outcome_from_envelope(stored)

    assert outcome.result_status == "error"
    assert outcome.error_code == "result_unknown"
    assert outcome.guidance is not None
    assert "The VM waits on question _vmx1" in outcome.guidance
    assert "task-7" in outcome.guidance
    assert "vmware.vm.question.answer" in outcome.guidance
    # MEHO's own words only: the question text from vCenter is not in it.
    assert "locked the CD-ROM door" not in outcome.guidance


def test_both_ops_are_registered_with_the_right_safety() -> None:
    assert VMWARE_VM_QUESTION_GET_OP in VMWARE_TYPED_OPS
    assert VMWARE_VM_QUESTION_ANSWER_OP in VMWARE_TYPED_OPS
    assert VMWARE_VM_QUESTION_GET_OP.safety_level == "safe"
    assert VMWARE_VM_QUESTION_GET_OP.requires_approval is False
    assert VMWARE_VM_QUESTION_ANSWER_OP.safety_level == "dangerous"
    assert VMWARE_VM_QUESTION_ANSWER_OP.requires_approval is True
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[VM_QUESTION_GROUP_KEY]
