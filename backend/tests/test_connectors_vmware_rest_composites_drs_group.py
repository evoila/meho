# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the DRS group writes of ``vmware-rest``.

* ``vmware.composite.cluster.drs_group.members.set`` -- add or remove the
  members of an existing VM group or host group (caution, waits for approval);
* ``vmware.composite.cluster.drs_group.delete`` -- delete a group no rule uses
  (destructive, a second person approves);
* ``vmware.composite.cluster.drs_vm_host_rule.create`` with
  ``use_existing_groups=true`` -- a rule on two groups that already exist.

Handlers and previews run against the shared vim fake. The governed flow runs
through the real dispatcher: the call waits for approval, a second person
approves, and only then the reconfigure runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    _drs_group,
    _drs_group_delete,
    _write,
    register_vmware_composite_operations,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    register_vmware_composite_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.composites._teardown import LIST_CAP
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _cluster_drs_vm_host_rule_create_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_DRS_GROUP_DELETE_RESPONSE_SCHEMA,
    CLUSTER_DRS_GROUP_MEMBERS_SET_PARAMETER_SCHEMA,
    CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA,
    CLUSTER_DRS_VM_HOST_RULE_CREATE_PARAMETER_SCHEMA,
    CLUSTER_DRS_VM_HOST_RULE_CREATE_RESPONSE_SCHEMA,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import jsonflux_reducer as reducer_module
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations import result_query as rq
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import blast_radius_missing_reason
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.result_query import ResultQuerySpec, run_result_query
from meho_backplane.settings import get_settings
from tests._result_store_helpers import MemoryValkey
from tests._vmware_cluster_config_fake import cluster_config_ex
from tests._vmware_teardown_fake import (
    GateRecorder,
    VimFake,
    moref,
    operator,
    parked,
    preview_ctx,
)

_MEMBERS_OP = "vmware.composite.cluster.drs_group.members.set"
_DELETE_OP = "vmware.composite.cluster.drs_group.delete"
_CREATE_OP = "vmware.composite.cluster.drs_vm_host_rule.create"
_RECONFIGURE_OP = "POST:/ClusterComputeResource/{moId}/ReconfigureComputeResource_Task"
_CLUSTER = "domain-c7"
_RECONFIGURE_PATH = f"/ClusterComputeResource/{_CLUSTER}/ReconfigureComputeResource_Task"

_VMS = {"vm-1": "runner-01", "vm-2": "runner-02", "vm-3": "runner-03", "vm-9": "other-01"}
_HOSTS = {"host-1": "esx-01", "host-2": "esx-02"}


class DrsFake(VimFake):
    """The vim fake plus the REST name lookups (``GET /vcenter/vm`` / ``/vcenter/host``)."""

    def __init__(self) -> None:
        super().__init__()
        #: moid -> cluster, for the cluster-scoped REST name lookups.
        self.in_cluster: dict[str, str] = {}

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return path

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        return query

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        self.calls.append((path, params))
        names = set(params.get("filter.names") or [])
        clusters = set(params.get("filter.clusters") or [])
        mo_type, key = ("VirtualMachine", "vm") if path == "/vcenter/vm" else ("HostSystem", "host")
        rows = [
            {key: moid, "name": props["name"]}
            for (t, moid), props in self.objects.items()
            if t == mo_type and props["name"] in names and self.in_cluster.get(moid) in clusters
        ]
        return {"value": rows}


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _vm_group(name: str = "runners-a", vms: tuple[str, ...] = ("vm-1", "vm-2")) -> dict[str, Any]:
    return {
        "_typeName": "ClusterVmGroup",
        "name": name,
        "userCreated": True,
        "vm": [moref("VirtualMachine", v) for v in vms],
    }


def _host_group(name: str = "hosts-a", hosts: tuple[str, ...] = ("host-1", "host-2")) -> dict:
    return {
        "_typeName": "ClusterHostGroup",
        "name": name,
        "userCreated": True,
        "host": [moref("HostSystem", h) for h in hosts],
    }


def _vm_host_rule(name: str = "runners-a-on-hosts-a", key: int = 21) -> dict[str, Any]:
    return {
        "_typeName": "ClusterVmHostRuleInfo",
        "key": key,
        "name": name,
        "enabled": True,
        "mandatory": False,
        "vmGroupName": "runners-a",
        "affineHostGroupName": "hosts-a",
    }


def _fake(
    groups: list[dict[str, Any]] | None = None, rules: list[dict[str, Any]] | None = None
) -> DrsFake:
    conn = DrsFake()
    conn.add(
        "ClusterComputeResource",
        _CLUSTER,
        name="ci-cluster",
        configurationEx=cluster_config_ex(
            rules=rules, groups=[_vm_group(), _host_group()] if groups is None else groups
        ),
    )
    for moid, name in _VMS.items():
        conn.add("VirtualMachine", moid, name=name)
        conn.in_cluster[moid] = "domain-c99" if moid == "vm-9" else _CLUSTER
    for moid, name in _HOSTS.items():
        conn.add("HostSystem", moid, name=name)
        conn.in_cluster[moid] = _CLUSTER
    return conn


def _groups(conn: DrsFake) -> dict[str, dict[str, Any]]:
    config_ex = conn.objects[("ClusterComputeResource", _CLUSTER)]["configurationEx"]
    return {g["name"]: g for g in config_ex.get("group", [])}


async def _members(conn: DrsFake, **params: Any) -> Any:
    return await _drs_group.cluster_drs_group_members_set_composite(
        operator=operator(), target=object(), params={"cluster": _CLUSTER, **params}, connector=conn
    )


async def _delete(conn: DrsFake, **params: Any) -> Any:
    return await _drs_group_delete.cluster_drs_group_delete_composite(
        operator=operator(), target=object(), params={"cluster": _CLUSTER, **params}, connector=conn
    )


async def _create(conn: DrsFake, **params: Any) -> Any:
    base = {
        "cluster": _CLUSTER,
        "rule_name": "new-rule",
        "vm_group_name": "runners-a",
        "host_group_name": "hosts-a",
        "use_existing_groups": True,
    }
    return await _write.cluster_drs_vm_host_rule_create_composite(
        operator=operator(), target=object(), params={**base, **params}, connector=conn
    )


def _valid(schema: dict[str, Any], out: dict[str, Any]) -> None:
    Draft202012Validator(schema).validate(out)


# ===========================================================================
# cluster.drs_group.members.set
# ===========================================================================


async def test_add_one_vm_reads_the_group_back_by_name(gate: GateRecorder) -> None:
    conn = _fake(rules=[_vm_host_rule()])
    out = await _members(conn, group_name="runners-a", add=["runner-03"])

    assert out["status"] == "updated"
    assert out["group_kind"] == "vm"
    assert out["added"] == [{"moid": "vm-3", "name": "runner-03"}]
    assert out["removed"] == []
    assert out["members"] == [
        {"moid": "vm-1", "name": "runner-01"},
        {"moid": "vm-2", "name": "runner-02"},
        {"moid": "vm-3", "name": "runner-03"},
    ]
    assert out["rules"] == [
        {"kind": "drs_rule", "name": "runners-a-on-hosts-a", "key": 21, "rule_type": "vm_host"}
    ]
    _valid(CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA, out)
    # One gated write naming what changes.
    assert [c["op_id"] for c in gate.calls] == [_RECONFIGURE_OP]
    assert gate.calls[0]["params"] == {
        "cluster": _CLUSTER,
        "group_name": "runners-a",
        "add": ["vm-3"],
        "remove": [],
    }
    # The exact vim body: one group edit with the full new member list.
    [(path, body)] = conn.calls_to("ReconfigureComputeResource_Task")
    assert path == _RECONFIGURE_PATH
    assert body == {
        "spec": {
            "_typeName": "ClusterConfigSpecEx",
            "groupSpec": [
                {
                    "_typeName": "ClusterGroupSpec",
                    "operation": "edit",
                    "info": {
                        "_typeName": "ClusterVmGroup",
                        "name": "runners-a",
                        "vm": [moref("VirtualMachine", v) for v in ("vm-1", "vm-2", "vm-3")],
                    },
                }
            ],
        },
        "modify": True,
    }
    # The host group is untouched.
    assert _groups(conn)["hosts-a"] == _host_group()


async def test_remove_a_host_from_a_host_group(gate: GateRecorder) -> None:
    conn = _fake()
    out = await _members(conn, group_name="hosts-a", remove=["esx-02"])

    assert out["status"] == "updated"
    assert out["group_kind"] == "host"
    assert out["removed"] == [{"moid": "host-2", "name": "esx-02"}]
    assert out["members"] == [{"moid": "host-1", "name": "esx-01"}]
    [(_path, body)] = conn.calls_to("ReconfigureComputeResource_Task")
    info = body["spec"]["groupSpec"][0]["info"]
    assert info == {
        "_typeName": "ClusterHostGroup",
        "name": "hosts-a",
        "host": [moref("HostSystem", "host-1")],
    }
    _valid(CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA, out)


async def test_remove_a_vm_and_run_twice(gate: GateRecorder) -> None:
    conn = _fake()
    first = await _members(conn, group_name="runners-a", remove=["runner-02"])
    second = await _members(conn, group_name="runners-a", remove=["runner-02"])
    assert (first["status"], second["status"]) == ("updated", "unchanged")
    assert second["not_members"] == ["runner-02"]
    assert len(conn.calls_to("ReconfigureComputeResource_Task")) == 1


async def test_add_a_vm_that_is_already_in_the_group_is_unchanged(gate: GateRecorder) -> None:
    conn = _fake()
    out = await _members(conn, group_name="runners-a", add=["runner-01"])
    assert out["status"] == "unchanged"
    assert out["already_members"] == ["runner-01"]
    assert gate.calls == []
    _valid(CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA, out)


async def test_a_name_outside_the_cluster_is_refused_before_any_write(
    gate: GateRecorder,
) -> None:
    conn = _fake()
    out = await _members(conn, group_name="runners-a", add=["runner-03", "other-01", "nope"])
    assert out["status"] == "names_not_found"
    assert out["not_found"] == ["other-01", "nope"]
    assert "nothing was changed" in out["guidance"]
    assert gate.calls == []
    assert "ReconfigureComputeResource_Task" not in conn.methods()
    _valid(CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA, out)


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"group_name": "missing", "add": ["runner-03"]}, "group_not_found"),
        ({"group_name": "runners-a", "add": ["runner-03"], "remove": ["runner-03"]}, None),
        ({"cluster": "domain-c404", "group_name": "runners-a", "add": ["runner-03"]}, None),
        ({"cluster": "../HostSystem/host-1", "group_name": "runners-a", "add": ["x"]}, None),
    ],
    ids=["no-such-group", "name-in-both-lists", "unknown-cluster", "bad-cluster-moid"],
)
async def test_refusals_write_nothing(
    gate: GateRecorder, params: dict[str, Any], status: str | None
) -> None:
    conn = _fake()
    out = await _members(conn, **params)
    assert out["status"] == (status or "invalid_request")
    assert gate.calls == []
    assert "ReconfigureComputeResource_Task" not in conn.methods()
    _valid(CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA, out)


def test_members_parameter_schema_needs_add_or_remove() -> None:
    schema = CLUSTER_DRS_GROUP_MEMBERS_SET_PARAMETER_SCHEMA
    ok = {"cluster": _CLUSTER, "group_name": "runners-a", "add": ["runner-03"]}
    assert validate_params(schema, ok) == []
    assert validate_params(schema, {"cluster": _CLUSTER, "group_name": "runners-a"}) != []
    assert validate_params(schema, {**ok, "cluster": "ci-cluster"}) != []


async def test_members_task_fault_raises(gate: GateRecorder) -> None:
    conn = _fake()
    conn.task_faults["ReconfigureComputeResource_Task"] = (
        "InvalidArgument",
        "A specified parameter was not correct: groupSpec",
    )
    with pytest.raises(RuntimeError, match="groupSpec"):
        await _members(conn, group_name="runners-a", add=["runner-03"])


async def test_members_timeout_and_mismatch(
    gate: GateRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _fake()
    conn.apply_writes = False
    out = await _members(conn, group_name="runners-a", add=["runner-03"])
    assert out["status"] == "mismatch"
    assert len(out["members"]) == 2
    _valid(CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA, out)

    monkeypatch.setattr(_drs_group, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn.running.add("ReconfigureComputeResource_Task")
    out = await _members(conn, group_name="runners-a", add=["runner-03"])
    assert out["status"] == "timeout"
    _valid(CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA, out)


async def test_members_parked_gate_keeps_the_write_off_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(parked(_RECONFIGURE_OP)))
    conn = _fake()
    out = await _members(conn, group_name="runners-a", add=["runner-03"])
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert "ReconfigureComputeResource_Task" not in conn.methods()


async def test_members_preview_names_the_change_and_the_rules() -> None:
    conn = _fake(rules=[_vm_host_rule()])
    effect = await _drs_group.cluster_drs_group_members_set_preview(
        preview_ctx({"cluster": _CLUSTER, "group_name": "runners-a", "add": ["runner-03"]}, conn)
    )
    assert effect is not None
    assert effect["cluster_name"] == "ci-cluster"
    assert effect["members_before"] == [
        {"moid": "vm-1", "name": "runner-01"},
        {"moid": "vm-2", "name": "runner-02"},
    ]
    assert effect["added"] == [{"moid": "vm-3", "name": "runner-03"}]
    assert [r["name"] for r in effect["rules"]] == ["runners-a-on-hosts-a"]
    assert "refusal" not in effect
    assert "ReconfigureComputeResource_Task" not in conn.methods()


async def test_members_preview_shows_the_refusal_and_declines_bad_input() -> None:
    conn = _fake()
    effect = await _drs_group.cluster_drs_group_members_set_preview(
        preview_ctx({"cluster": _CLUSTER, "group_name": "runners-a", "add": ["nope"]}, conn)
    )
    assert effect is not None
    assert effect["refusal"]["status"] == "names_not_found"
    assert (
        await _drs_group.cluster_drs_group_members_set_preview(
            preview_ctx({"cluster": _CLUSTER, "group_name": "runners-a"}, None)
        )
        is None
    )


# ===========================================================================
# cluster.drs_group.delete
# ===========================================================================


async def test_delete_an_unused_group(gate: GateRecorder) -> None:
    conn = _fake()
    out = await _delete(conn, group_name="runners-a")

    assert out["status"] == "deleted"
    assert out["object"] == {
        "kind": "drs_group",
        "cluster": _CLUSTER,
        "cluster_name": "ci-cluster",
        "name": "runners-a",
        "group_kind": "vm",
        "members": [{"moid": "vm-1", "name": "runner-01"}, {"moid": "vm-2", "name": "runner-02"}],
        "member_count": 2,
    }
    _valid(CLUSTER_DRS_GROUP_DELETE_RESPONSE_SCHEMA, out)
    assert gate.calls[0]["params"] == {"cluster": _CLUSTER, "group_name": "runners-a"}
    [(path, body)] = conn.calls_to("ReconfigureComputeResource_Task")
    assert path == _RECONFIGURE_PATH
    assert body == {
        "spec": {
            "_typeName": "ClusterConfigSpecEx",
            "groupSpec": [
                {
                    "_typeName": "ClusterGroupSpec",
                    "operation": "remove",
                    "removeKey": {"_typeName": "string", "_value": "runners-a"},
                }
            ],
        },
        "modify": True,
    }
    assert set(_groups(conn)) == {"hosts-a"}
    # The VMs stay.
    assert ("VirtualMachine", "vm-1") in conn.objects


@pytest.mark.parametrize(
    "rule",
    [
        _vm_host_rule(),
        {
            "_typeName": "ClusterDependencyRuleInfo",
            "key": 22,
            "name": "start-order",
            "vmGroup": "web",
            "dependsOnVmGroup": "runners-a",
        },
    ],
    ids=["vm-host-rule", "dependency-rule"],
)
async def test_delete_of_a_group_a_rule_uses_is_refused_and_names_the_rule(
    gate: GateRecorder, rule: dict[str, Any]
) -> None:
    conn = _fake(rules=[rule])
    out = await _delete(conn, group_name="runners-a")
    assert out["status"] == "precondition_failed"
    assert [b["name"] for b in out["blockers"]] == [rule["name"]]
    assert rule["name"] in out["guidance"]
    assert gate.calls == []
    assert "ReconfigureComputeResource_Task" not in conn.methods()
    _valid(CLUSTER_DRS_GROUP_DELETE_RESPONSE_SCHEMA, out)


async def test_delete_of_a_missing_group_is_unchanged(gate: GateRecorder) -> None:
    conn = _fake(groups=[])
    out = await _delete(conn, group_name="runners-a")
    assert out["status"] == "unchanged"
    assert gate.calls == []
    _valid(CLUSTER_DRS_GROUP_DELETE_RESPONSE_SCHEMA, out)


async def test_delete_task_fault_raises_and_still_present(gate: GateRecorder) -> None:
    conn = _fake()
    conn.task_faults["ReconfigureComputeResource_Task"] = ("NotFound", "group not found: x")
    with pytest.raises(RuntimeError, match="group not found"):
        await _delete(conn, group_name="runners-a")

    conn = _fake()
    conn.apply_writes = False
    out = await _delete(conn, group_name="runners-a")
    assert out["status"] == "still_present"
    _valid(CLUSTER_DRS_GROUP_DELETE_RESPONSE_SCHEMA, out)


async def test_delete_preview_is_the_blast_radius() -> None:
    conn = _fake(rules=[_vm_host_rule()])
    effect = await _drs_group_delete.cluster_drs_group_delete_preview(
        preview_ctx({"cluster": _CLUSTER, "group_name": "hosts-a"}, conn)
    )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    block = effect["blast_radius"]
    assert block["object"]["members"] == [
        {"moid": "host-1", "name": "esx-01"},
        {"moid": "host-2", "name": "esx-02"},
    ]
    assert block["blockers"][0]["name"] == "runners-a-on-hosts-a"
    assert block["refusal"]["status"] == "precondition_failed"
    assert "ReconfigureComputeResource_Task" not in conn.methods()


# ===========================================================================
# cluster.drs_vm_host_rule.create with use_existing_groups=true
# ===========================================================================


async def test_create_rule_on_existing_groups_makes_no_group(gate: GateRecorder) -> None:
    conn = _fake()
    out = await _create(conn, affine=False, mandatory=True)

    assert out["status"] == "created"
    assert out["resolved_vms"] == [
        {"vm": "vm-1", "name": "runner-01"},
        {"vm": "vm-2", "name": "runner-02"},
    ]
    assert out["resolved_hosts"] == [
        {"host": "host-1", "name": "esx-01"},
        {"host": "host-2", "name": "esx-02"},
    ]
    _valid(CLUSTER_DRS_VM_HOST_RULE_CREATE_RESPONSE_SCHEMA, out)
    [(_path, body)] = conn.calls_to("ReconfigureComputeResource_Task")
    assert body == {
        "spec": {
            "_typeName": "ClusterConfigSpecEx",
            "rulesSpec": [
                {
                    "_typeName": "ClusterRuleSpec",
                    "operation": "add",
                    "info": {
                        "_typeName": "ClusterVmHostRuleInfo",
                        "name": "new-rule",
                        "enabled": True,
                        "mandatory": True,
                        "vmGroupName": "runners-a",
                        "antiAffineHostGroupName": "hosts-a",
                    },
                }
            ],
        },
        "modify": True,
    }
    # No name lookups: the groups are used as they are.
    assert not any(path.startswith("/vcenter/") for path, _ in conn.calls)


@pytest.mark.parametrize(
    ("params", "status", "word"),
    [
        ({"vm_group_name": "missing"}, "group_not_found", "VM group 'missing'"),
        ({"vm_group_name": "hosts-a"}, "group_not_found", "VM group 'hosts-a'"),
        ({"host_group_name": "runners-a"}, "group_not_found", "host group 'runners-a'"),
        ({"rule_name": "runners-a-on-hosts-a"}, "rule_exists", "already exists"),
    ],
    ids=["missing-vm-group", "host-group-as-vm-group", "vm-group-as-host-group", "rule-taken"],
)
async def test_create_on_existing_groups_refusals(
    gate: GateRecorder, params: dict[str, Any], status: str, word: str
) -> None:
    conn = _fake(rules=[_vm_host_rule()])
    out = await _create(conn, **params)
    assert out["status"] == status
    assert word in out["guidance"]
    assert gate.calls == []
    _valid(CLUSTER_DRS_VM_HOST_RULE_CREATE_RESPONSE_SCHEMA, out)


async def test_create_on_existing_groups_task_fault_raises(gate: GateRecorder) -> None:
    conn = _fake()
    conn.task_faults["ReconfigureComputeResource_Task"] = ("InvalidArgument", "bad rule spec")
    with pytest.raises(RuntimeError, match="bad rule spec"):
        await _create(conn)


async def test_create_preview_shows_the_existing_group_members() -> None:
    conn = _fake()
    params = {
        "cluster": _CLUSTER,
        "rule_name": "new-rule",
        "vm_group_name": "runners-a",
        "host_group_name": "missing",
        "use_existing_groups": True,
    }
    effect = await _cluster_drs_vm_host_rule_create_preview(preview_ctx(params, conn))
    assert effect is not None
    assert effect["resolved_vms"] == [
        {"vm": "vm-1", "name": "runner-01"},
        {"vm": "vm-2", "name": "runner-02"},
    ]
    assert effect["total_vms"] == 2
    assert effect["resolved_hosts"] is None
    assert effect["total_hosts"] is None
    assert effect["refusal"]["status"] == "group_not_found"


@pytest.mark.parametrize(
    ("extra", "accepted"),
    [
        ({"vms": ["a"], "hosts": ["b"]}, True),
        ({"vms": ["a"], "hosts": ["b"], "use_existing_groups": False}, True),
        ({"use_existing_groups": True}, True),
        ({"use_existing_groups": True, "vms": ["a"]}, False),
        ({}, False),
        ({"vms": ["a"]}, False),
    ],
    ids=["new-groups", "new-groups-explicit", "existing", "existing-with-vms", "none", "vms-only"],
)
def test_create_schema_new_or_existing_groups(extra: dict[str, Any], accepted: bool) -> None:
    params = {
        "cluster": _CLUSTER,
        "rule_name": "r",
        "vm_group_name": "vg",
        "host_group_name": "hg",
        **extra,
    }
    errors = validate_params(CLUSTER_DRS_VM_HOST_RULE_CREATE_PARAMETER_SCHEMA, params)
    assert (errors == []) is accepted, errors


# ===========================================================================
# Groups with more than LIST_CAP members: results are whole, cards are cut
# ===========================================================================

_BIG = LIST_CAP + 10


def _big_fake(rules: list[dict[str, Any]] | None = None) -> DrsFake:
    """A VM group of ``_BIG`` VMs (``vm-1`` .. ``vm-60``) plus one VM outside the group."""
    conn = _fake(
        groups=[_vm_group(vms=tuple(f"vm-{i}" for i in range(1, _BIG + 1))), _host_group()]
    )
    for i in range(1, _BIG + 2):
        conn.add("VirtualMachine", f"vm-{i}", name=f"runner-{i:02d}")
        conn.in_cluster[f"vm-{i}"] = _CLUSTER
    return conn


def _names(rows: list[dict[str, Any]]) -> list[str]:
    return [row["name"] for row in rows]


async def test_members_result_carries_every_member_of_a_large_group(gate: GateRecorder) -> None:
    conn = _big_fake()
    remove = [f"runner-{i:02d}" for i in range(1, LIST_CAP + 6)]
    out = await _members(conn, group_name="runners-a", add=[f"runner-{_BIG + 1}"], remove=remove)

    assert out["status"] == "updated"
    assert len(out["members_before"]) == _BIG
    assert out["member_count_before"] == _BIG
    assert len(out["removed"]) == LIST_CAP + 5
    assert _names(out["added"]) == [f"runner-{_BIG + 1}"]
    assert out["member_count_after"] == _BIG - (LIST_CAP + 5) + 1
    kept = _names(out["members_before"])[LIST_CAP + 5 :]
    assert _names(out["members"]) == [*kept, f"runner-{_BIG + 1}"]
    assert len(_groups(conn)["runners-a"]["vm"]) == out["member_count_after"]
    _valid(CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA, out)


async def test_members_result_reads_back_a_large_group_whole(gate: GateRecorder) -> None:
    conn = _big_fake()
    out = await _members(conn, group_name="runners-a", add=[f"runner-{_BIG + 1}"])
    assert out["status"] == "updated"
    assert len(out["members"]) == _BIG + 1
    assert out["members"][-1] == {"moid": f"vm-{_BIG + 1}", "name": f"runner-{_BIG + 1}"}


@pytest.fixture
def result_store(monkeypatch: pytest.MonkeyPatch) -> ResultHandleStore:
    """The real result store on an in-memory stand-in for Valkey."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    store = ResultHandleStore(MemoryValkey())  # type: ignore[arg-type]
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(reducer_module, "get_result_handle_store", lambda: store)
    return store


@pytest.mark.usefixtures("result_store")
async def test_a_large_member_list_is_stored_and_read_back_whole(gate: GateRecorder) -> None:
    """JSONFlux keeps the whole result; ``result_query`` pages ``members`` past row 50.

    The result carries several lists (``members``, ``members_before``, ...),
    so the reducer stores it whole as one document and the status stays
    inline. A ``path`` read then pages any of the lists, nothing is cut.
    """
    conn = _big_fake()
    out = await _members(conn, group_name="runners-a", add=[f"runner-{_BIG + 1}"])
    op = operator()
    context = {"op_id": _MEMBERS_OP, "tenant_id": str(op.tenant_id), "operator_sub": op.sub}

    reduced, handle = await JsonFluxReducer().reduce(
        out, CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA, context
    )

    assert handle is not None
    assert reduced["status"] == "updated"
    assert reduced["member_count_after"] == _BIG + 1

    read = await run_result_query(
        op, handle.handle_id, ResultQuerySpec(path="/members"), offset=LIST_CAP, limit=LIST_CAP
    )
    assert read["coverage"] == "complete"
    assert read["total_rows"] == _BIG + 1
    assert _names(read["rows"]) == [f"runner-{i:02d}" for i in range(LIST_CAP + 1, _BIG + 1)] + [
        f"runner-{_BIG + 1}"
    ]
    before = await run_result_query(op, handle.handle_id, ResultQuerySpec(path="/members_before"))
    assert before["total_rows"] == _BIG


async def test_members_preview_cuts_the_lists_and_keeps_the_counts() -> None:
    conn = _big_fake()
    remove = [f"runner-{i:02d}" for i in range(1, LIST_CAP + 6)]
    params = {
        "cluster": _CLUSTER,
        "group_name": "runners-a",
        "add": [f"runner-{_BIG + 1}"],
        "remove": remove,
    }
    effect = await _drs_group.cluster_drs_group_members_set_preview(preview_ctx(params, conn))
    assert effect is not None
    assert len(effect["members_before"]) == LIST_CAP
    assert effect["member_count_before"] == _BIG
    assert len(effect["removed"]) == LIST_CAP
    assert effect["removed_count"] == LIST_CAP + 5
    assert _names(effect["added"]) == [f"runner-{_BIG + 1}"]
    assert effect["added_count"] == 1
    assert effect["member_count_after"] == _BIG - (LIST_CAP + 5) + 1
    assert "ReconfigureComputeResource_Task" not in conn.methods()


async def test_delete_result_carries_every_member_and_the_card_is_cut(
    gate: GateRecorder,
) -> None:
    conn = _big_fake()
    effect = await _drs_group_delete.cluster_drs_group_delete_preview(
        preview_ctx({"cluster": _CLUSTER, "group_name": "runners-a"}, conn)
    )
    assert effect is not None
    card = effect["blast_radius"]["object"]
    assert len(card["members"]) == LIST_CAP
    assert card["member_count"] == _BIG

    out = await _delete(conn, group_name="runners-a")
    assert out["status"] == "deleted"
    assert len(out["object"]["members"]) == _BIG
    assert out["object"]["member_count"] == _BIG
    _valid(CLUSTER_DRS_GROUP_DELETE_RESPONSE_SCHEMA, out)


async def test_create_on_existing_groups_lists_every_member_and_the_card_is_cut(
    gate: GateRecorder,
) -> None:
    conn = _big_fake()
    params = {
        "cluster": _CLUSTER,
        "rule_name": "new-rule",
        "vm_group_name": "runners-a",
        "host_group_name": "hosts-a",
        "use_existing_groups": True,
    }
    effect = await _cluster_drs_vm_host_rule_create_preview(preview_ctx(params, conn))
    assert effect is not None
    assert len(effect["resolved_vms"]) == LIST_CAP
    assert effect["total_vms"] == _BIG
    assert _names(effect["resolved_hosts"]) == ["esx-01", "esx-02"]
    assert effect["total_hosts"] == 2

    out = await _create(conn)
    assert out["status"] == "created"
    assert len(out["resolved_vms"]) == _BIG
    assert out["resolved_vms"][-1] == {"vm": f"vm-{_BIG}", "name": f"runner-{_BIG}"}


# ===========================================================================
# Registration and the governed flow through the dispatcher
# ===========================================================================

_TENANT_ID = UUID("00000000-0000-0000-0000-000000005344")
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


def _person(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name="DRS Group Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _bootstrap(conn: DrsFake) -> None:
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
                notes="seeded by test_connectors_vmware_rest_composites_drs_group",
            )
        )
        await s.commit()


@pytest.mark.usefixtures("_dispatch_env")
async def test_registered_on_8_0_and_9_0_with_approval() -> None:
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
    await register_vmware_composite_operations_8_0(embedding_service=_embedding_stub())
    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.in_([_MEMBERS_OP, _DELETE_OP])
                    )
                )
            )
            .scalars()
            .all()
        )
    levels = {(row.op_id, row.version): (row.safety_level, row.requires_approval) for row in rows}
    assert levels == {
        (_MEMBERS_OP, "9.0"): ("caution", True),
        (_MEMBERS_OP, "8.0"): ("caution", True),
        (_DELETE_OP, "9.0"): ("destructive", True),
        (_DELETE_OP, "8.0"): ("destructive", True),
    }


async def _approve_and_resume(call: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    request_id = UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    approver = _person("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    return effect, resume


async def _call(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    requester = _person("op-requester")
    args = {
        "connector_id": "vmware-rest-9.0",
        "op_id": op_id,
        "target": _TARGET_NAME,
        "params": params,
    }
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    return await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})


@pytest.mark.usefixtures("_dispatch_env")
async def test_members_set_waits_for_approval_then_adds_the_vm() -> None:
    conn = _fake(rules=[_vm_host_rule()])
    await _bootstrap(conn)
    call = await _call(
        _MEMBERS_OP, {"cluster": _CLUSTER, "group_name": "runners-a", "add": ["runner-03"]}
    )
    assert call["status"] == "awaiting_approval", call
    assert "ReconfigureComputeResource_Task" not in conn.methods()

    effect, resume = await _approve_and_resume(call)
    # The approval card names the change and the rules that use the group.
    preview = effect["preview"]
    assert preview["added"] == [{"moid": "vm-3", "name": "runner-03"}]
    assert [r["name"] for r in preview["rules"]] == ["runners-a-on-hosts-a"]
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "updated"
    assert [m["name"] for m in resume.result["members"]] == ["runner-01", "runner-02", "runner-03"]


@pytest.mark.usefixtures("_dispatch_env")
async def test_group_delete_waits_for_a_second_person_then_deletes() -> None:
    conn = _fake()
    await _bootstrap(conn)
    call = await _call(_DELETE_OP, {"cluster": _CLUSTER, "group_name": "runners-a"})
    assert call["status"] == "awaiting_approval", call
    assert "ReconfigureComputeResource_Task" not in conn.methods()

    effect, resume = await _approve_and_resume(call)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"]["member_count"] == 2
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "deleted"
    assert set(_groups(conn)) == {"hosts-a"}


@pytest.mark.usefixtures("_dispatch_env")
async def test_create_on_existing_groups_waits_for_approval() -> None:
    conn = _fake()
    await _bootstrap(conn)
    params = {
        "cluster": _CLUSTER,
        "rule_name": "new-rule",
        "vm_group_name": "runners-a",
        "host_group_name": "hosts-a",
        "use_existing_groups": True,
    }
    call = await _call(_CREATE_OP, params)
    assert call["status"] == "awaiting_approval", call
    effect, resume = await _approve_and_resume(call)
    assert [v["name"] for v in effect["preview"]["resolved_vms"]] == ["runner-01", "runner-02"]
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "created"
    assert set(_groups(conn)) == {"runners-a", "hosts-a"}
