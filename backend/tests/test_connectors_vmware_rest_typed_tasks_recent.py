# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vmware.tasks.recent`` typed op (#2300).

``vmware.tasks.recent`` is a ``source_kind="typed"`` read of the recent
vCenter Task objects: it reads ``TaskManager.recentTask`` then
``Task.info`` via PropertyCollector directly on the connector session (no
``dispatch_child``, no ingested descriptor), so it works on a fresh boot
with zero catalog ingest.

The handler is exercised against a fake connector recording the two
PropertyCollector POSTs, so the assertion targets are the two-step
call-shape (recentTask -> capped info read) and the TaskInfo parse
contract, without a live httpx transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent import (
    VMWARE_TASKS_RECENT_OP,
    build_task_info_retrieve_params,
    tasks_recent_impl,
)
from meho_backplane.operations._validate import validate_params


def _make_operator() -> Operator:
    return Operator(
        sub="op-tasks-recent",
        name="Tasks Recent Test",
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


class _FakeConnector:
    """Serves the recentTask read first, then the info read, in call order."""

    def __init__(self, *, responses: list[Any], mount_prefix: str = "/api") -> None:
        self._responses = list(responses)
        self._mount_prefix = mount_prefix
        self.mount_calls: list[str] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        self.mount_calls.append(path)
        return f"{self._mount_prefix}{path}"

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> Any:
        # Both RetrievePropertiesEx reads route through the vmomi seam with
        # the spec-relative path (the /sdk/vim25 mount is the connector's
        # job, #2466).
        del target, operator
        assert json is not None
        self.post_calls.append((path, json))
        return self._responses.pop(0)


def _recent_task_result(moids: list[str]) -> dict[str, Any]:
    return {
        "objects": [
            {
                "obj": {"type": "TaskManager", "value": "TaskManager"},
                "propSet": [
                    {
                        "name": "recentTask",
                        "val": [{"type": "Task", "value": m} for m in moids],
                    }
                ],
            }
        ]
    }


def _info_result(info_by_moid: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "objects": [
            {
                "obj": {"type": "Task", "value": moid},
                "propSet": [{"name": "info", "val": info}],
            }
            for moid, info in info_by_moid.items()
        ]
    }


_TASK_INFO_SUCCESS = {
    "descriptionId": "VirtualMachine.powerOn",
    "entity": {"type": "VirtualMachine", "value": "vm-9"},
    "entityName": "web-01",
    "state": "success",
    "progress": 100,
    "cancelled": False,
    "queueTime": "2026-07-10T09:00:00Z",
    "startTime": "2026-07-10T09:00:01Z",
    "completeTime": "2026-07-10T09:00:05Z",
}
_TASK_INFO_ERROR = {
    "descriptionId": "VirtualMachine.reconfigure",
    "entity": {"type": "VirtualMachine", "value": "vm-10"},
    "entityName": "db-01",
    "state": "error",
    "progress": 40,
    "error": {"localizedMessage": "The operation is not allowed in the current state."},
}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def test_build_task_info_params_one_objectset_entry_per_task() -> None:
    body = build_task_info_retrieve_params(["task-1", "task-2"])
    (spec,) = body["specSet"]
    (prop_spec,) = spec["propSet"]
    assert prop_spec == {"type": "Task", "pathSet": ["info"]}
    assert [o["obj"]["value"] for o in spec["objectSet"]] == ["task-1", "task-2"]


# ---------------------------------------------------------------------------
# tasks_recent_impl — two-step read + parse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_recent_reads_recent_then_info() -> None:
    conn = _FakeConnector(
        responses=[
            _recent_task_result(["task-1", "task-2"]),
            _info_result({"task-1": _TASK_INFO_SUCCESS, "task-2": _TASK_INFO_ERROR}),
        ]
    )

    out = await tasks_recent_impl(conn, _make_operator(), _Target(), {})

    # Both reads route through the vmomi seam (not mount_op_path), each
    # addressed by the spec-relative RetrievePropertiesEx path (#2466).
    assert conn.mount_calls == []
    assert len(conn.post_calls) == 2
    assert {p for p, _ in conn.post_calls} == {
        "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    }

    tasks = out["tasks"]
    assert [t["task"] for t in tasks] == ["task-1", "task-2"]
    assert tasks[0] == {
        "task": "task-1",
        "operation": "VirtualMachine.powerOn",
        "entity": "vm-9",
        "entity_type": "VirtualMachine",
        "entity_name": "web-01",
        "state": "success",
        "progress": 100,
        "cancelled": False,
        "queue_time": "2026-07-10T09:00:00Z",
        "start_time": "2026-07-10T09:00:01Z",
        "complete_time": "2026-07-10T09:00:05Z",
        "error_message": None,
    }
    assert tasks[1]["state"] == "error"
    assert tasks[1]["error_message"] == "The operation is not allowed in the current state."


@pytest.mark.asyncio
async def test_tasks_recent_empty_recent_list_skips_info_read() -> None:
    conn = _FakeConnector(responses=[_recent_task_result([])])

    out = await tasks_recent_impl(conn, _make_operator(), _Target(), {})

    assert out == {"tasks": []}
    # Only the recentTask read happened; no info read on an empty list.
    assert len(conn.post_calls) == 1


@pytest.mark.asyncio
async def test_tasks_recent_max_tasks_caps_the_info_read() -> None:
    conn = _FakeConnector(
        responses=[
            _recent_task_result(["task-1", "task-2", "task-3"]),
            _info_result({"task-1": _TASK_INFO_SUCCESS}),
        ]
    )

    out = await tasks_recent_impl(conn, _make_operator(), _Target(), {"max_tasks": 1})

    # Only the first task moid flows into the info read.
    info_body = conn.post_calls[1][1]
    assert [o["obj"]["value"] for o in info_body["specSet"][0]["objectSet"]] == ["task-1"]
    assert [t["task"] for t in out["tasks"]] == ["task-1"]


@pytest.mark.asyncio
async def test_tasks_recent_tolerates_legacy_value_envelope() -> None:
    conn = _FakeConnector(
        responses=[
            {"value": _recent_task_result(["task-1"])},
            {"value": _info_result({"task-1": _TASK_INFO_SUCCESS})},
        ]
    )

    out = await tasks_recent_impl(conn, _make_operator(), _Target(), {})

    assert [t["task"] for t in out["tasks"]] == ["task-1"]


@pytest.mark.asyncio
async def test_tasks_recent_missing_info_maps_fields_to_none() -> None:
    conn = _FakeConnector(
        responses=[_recent_task_result(["task-1"]), _info_result({})]  # info read returns nothing
    )

    out = await tasks_recent_impl(conn, _make_operator(), _Target(), {})

    (task,) = out["tasks"]
    assert task["task"] == "task-1"
    assert task["state"] is None
    assert task["entity"] is None


# ---------------------------------------------------------------------------
# Parameter schema (max_tasks bound)
# ---------------------------------------------------------------------------


def test_schema_accepts_valid_max_tasks_and_empty() -> None:
    schema = VMWARE_TASKS_RECENT_OP.parameter_schema
    assert validate_params(schema, {}) == []
    assert validate_params(schema, {"max_tasks": 25}) == []


def test_schema_rejects_out_of_range_max_tasks() -> None:
    schema = VMWARE_TASKS_RECENT_OP.parameter_schema
    assert validate_params(schema, {"max_tasks": 0}) != []
    assert validate_params(schema, {"max_tasks": 5000}) != []


# ---------------------------------------------------------------------------
# Op metadata / registration contract
# ---------------------------------------------------------------------------


def test_tasks_recent_op_is_a_registered_typed_op() -> None:
    assert VMWARE_TASKS_RECENT_OP in VMWARE_TYPED_OPS
    assert VMWARE_TASKS_RECENT_OP.op_id == "vmware.tasks.recent"
    assert VMWARE_TASKS_RECENT_OP.safety_level == "safe"
    assert VMWARE_TASKS_RECENT_OP.requires_approval is False


def test_tasks_recent_handler_attr_resolves_to_a_connector_bound_method() -> None:
    handler = getattr(VmwareRestConnector, VMWARE_TASKS_RECENT_OP.handler_attr, None)
    assert handler is not None
    assert callable(handler)


def test_tasks_recent_group_has_non_empty_when_to_use() -> None:
    group_key = VMWARE_TASKS_RECENT_OP.group_key
    assert group_key is not None
    blurb = VMWARE_TYPED_WHEN_TO_USE_BY_GROUP.get(group_key)
    assert isinstance(blurb, str)
    assert blurb.strip()
