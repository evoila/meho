# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``vmware.composite.cluster.drs_rule.delete``.

Two layers:

* the handler and its preview against the shared vim fake: the happy path
  (gated reconfigure, task poll, new read of the rules), every refusal before
  any write, ``unchanged`` when the rule is already gone, a task fault, the
  poll timeout, a parked gate, a rule that still reads back, and the
  approval-card blast radius;
* the governed flow through the real dispatcher: the op is registered
  destructive, a call waits for approval with the rule on the approval card,
  a second person approves, and only then the reconfigure runs. An agent
  principal is refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    _drs_rule_delete,
    _write,
    register_vmware_composite_operations,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_DRS_RULE_DELETE_PARAMETER_SCHEMA,
    CLUSTER_DRS_RULE_DELETE_RESPONSE_SCHEMA,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import blast_radius_missing_reason
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests._vmware_teardown_fake import (
    GateRecorder,
    VimFake,
    moref,
    operator,
    parked,
    preview_ctx,
)

_OP_ID = "vmware.composite.cluster.drs_rule.delete"
_RECONFIGURE_OP = "POST:/ClusterComputeResource/{moId}/ReconfigureComputeResource_Task"
_CLUSTER = "domain-c7"
_RECONFIGURE_PATH = f"/ClusterComputeResource/{_CLUSTER}/ReconfigureComputeResource_Task"


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _anti_affinity_rule(
    key: int = 11, name: str = "keep-apart", vms: tuple[str, ...] = ("vm-1", "vm-2")
) -> dict[str, Any]:
    return {
        "_typeName": "ClusterAntiAffinityRuleSpec",
        "key": key,
        "name": name,
        "enabled": True,
        "mandatory": False,
        "userCreated": True,
        "vm": [moref("VirtualMachine", v) for v in vms],
    }


def _vm_host_rule(key: int = 12, name: str = "pin-to-hosts") -> dict[str, Any]:
    return {
        "_typeName": "ClusterVmHostRuleInfo",
        "key": key,
        "name": name,
        "enabled": True,
        "mandatory": True,
        "vmGroupName": "example-vms",
        "affineHostGroupName": "example-hosts",
    }


def _cluster(conn: VimFake, rules: list[dict[str, Any]], *, boxed: bool = True) -> None:
    """Seed a cluster; ``boxed`` stores the rule list the way vCenter 8.0.x sends it."""
    value: Any = {"_typeName": "ArrayOfClusterRuleInfo", "_value": rules} if boxed else rules
    conn.add(
        "ClusterComputeResource",
        _CLUSTER,
        **{"name": "example-cluster", "configurationEx.rule": value},
    )
    for vm in ("vm-1", "vm-2"):
        conn.add("VirtualMachine", vm, name=f"name-{vm}")


async def _call(conn: VimFake, **params: Any) -> Any:
    return await _drs_rule_delete.cluster_drs_rule_delete_composite(
        operator=operator(), target=object(), params=params, connector=conn
    )


def _validate(out: dict[str, Any]) -> None:
    Draft202012Validator(CLUSTER_DRS_RULE_DELETE_RESPONSE_SCHEMA).validate(out)


# ===========================================================================
# Handler
# ===========================================================================


async def test_delete_anti_affinity_rule_happy_path(gate: GateRecorder) -> None:
    conn = VimFake()
    _cluster(conn, [_anti_affinity_rule(), _vm_host_rule()])
    out = await _call(conn, cluster=_CLUSTER, rule_name="keep-apart")

    assert out["status"] == "deleted"
    assert out["task_state"] == "success"
    assert out["object"] == {
        "kind": "drs_rule",
        "cluster": _CLUSTER,
        "cluster_name": "example-cluster",
        "name": "keep-apart",
        "key": 11,
        "rule_type": "anti_affinity",
        "enabled": True,
        "mandatory": False,
        "vms": [{"vm": "vm-1", "name": "name-vm-1"}, {"vm": "vm-2", "name": "name-vm-2"}],
        "vm_count": 2,
    }
    _validate(out)
    # One gated write, naming the rule it removes.
    assert [c["op_id"] for c in gate.calls] == [_RECONFIGURE_OP]
    assert gate.calls[0]["params"] == {
        "cluster": _CLUSTER,
        "rule_name": "keep-apart",
        "rule_key": 11,
    }
    # The exact vim body: one removal by key, the key boxed as a vim int.
    [(path, body)] = conn.calls_to("ReconfigureComputeResource_Task")
    assert path == _RECONFIGURE_PATH
    assert body == {
        "spec": {
            "_typeName": "ClusterConfigSpecEx",
            "rulesSpec": [
                {
                    "_typeName": "ClusterRuleSpec",
                    "operation": "remove",
                    "removeKey": {"_typeName": "int", "_value": 11},
                }
            ],
        },
        "modify": True,
    }
    # The other rule is untouched.
    rules = conn.objects[("ClusterComputeResource", _CLUSTER)]["configurationEx.rule"]["_value"]
    assert [r["name"] for r in rules] == ["pin-to-hosts"]


async def test_delete_vm_host_rule_shows_groups_and_keeps_them(gate: GateRecorder) -> None:
    conn = VimFake()
    _cluster(conn, [_vm_host_rule()], boxed=False)
    out = await _call(conn, cluster=_CLUSTER, rule_name="pin-to-hosts")

    assert out["status"] == "deleted"
    obj = out["object"]
    assert obj["rule_type"] == "vm_host"
    assert obj["mandatory"] is True
    assert obj["vm_group"] == "example-vms"
    assert obj["affine_host_group"] == "example-hosts"
    assert obj["anti_affine_host_group"] is None
    assert "vms" not in obj
    # Only a rule removal: no groupSpec, so the VM and host groups stay.
    [(_path, body)] = conn.calls_to("ReconfigureComputeResource_Task")
    assert set(body["spec"]) == {"_typeName", "rulesSpec"}
    _validate(out)


async def test_rule_already_gone_is_unchanged(gate: GateRecorder) -> None:
    conn = VimFake()
    _cluster(conn, [_vm_host_rule()])
    out = await _call(conn, cluster=_CLUSTER, rule_name="keep-apart")

    assert out["status"] == "unchanged"
    assert "nothing was deleted" in out["guidance"]
    assert out["object"] == {
        "kind": "drs_rule",
        "cluster": _CLUSTER,
        "cluster_name": "example-cluster",
        "name": "keep-apart",
    }
    assert gate.calls == []
    assert "ReconfigureComputeResource_Task" not in conn.methods()
    _validate(out)


async def test_cluster_without_any_rule_is_unchanged(gate: GateRecorder) -> None:
    """vCenter leaves an empty rule list out of the answer: that means no rules."""
    conn = VimFake()
    conn.add("ClusterComputeResource", _CLUSTER, name="example-cluster")
    out = await _call(conn, cluster=_CLUSTER, rule_name="keep-apart")
    assert out["status"] == "unchanged"
    assert gate.calls == []


async def test_run_twice_deletes_once_then_unchanged(gate: GateRecorder) -> None:
    conn = VimFake()
    _cluster(conn, [_anti_affinity_rule()])
    first = await _call(conn, cluster=_CLUSTER, rule_name="keep-apart")
    second = await _call(conn, cluster=_CLUSTER, rule_name="keep-apart")
    assert (first["status"], second["status"]) == ("deleted", "unchanged")
    assert len(conn.calls_to("ReconfigureComputeResource_Task")) == 1


async def test_duplicate_rule_names_are_refused(gate: GateRecorder) -> None:
    conn = VimFake()
    _cluster(conn, [_anti_affinity_rule(key=11), _anti_affinity_rule(key=14)])
    out = await _call(conn, cluster=_CLUSTER, rule_name="keep-apart")

    assert out["status"] == "precondition_failed"
    assert out["blockers"] == [
        {"kind": "drs_rule", "key": 11, "rule_type": "anti_affinity"},
        {"kind": "drs_rule", "key": 14, "rule_type": "anti_affinity"},
    ]
    assert "2 DRS rules" in out["guidance"]
    assert gate.calls == []
    assert "ReconfigureComputeResource_Task" not in conn.methods()
    _validate(out)


async def test_unknown_cluster_is_invalid_request(gate: GateRecorder) -> None:
    conn = VimFake()
    out = await _call(conn, cluster="domain-c404", rule_name="keep-apart")
    assert out["status"] == "invalid_request"
    assert "not found" in out["guidance"]
    assert gate.calls == []
    _validate(out)


async def test_cluster_moid_shape_is_checked_before_any_io(gate: GateRecorder) -> None:
    conn = VimFake()
    out = await _call(conn, cluster="../VirtualMachine/vm-1", rule_name="keep-apart")
    assert out["status"] == "invalid_request"
    assert conn.calls == []
    assert gate.calls == []


def test_parameter_schema_requires_a_cluster_moid_and_a_name() -> None:
    validator = Draft202012Validator(CLUSTER_DRS_RULE_DELETE_PARAMETER_SCHEMA)
    validator.validate({"cluster": _CLUSTER, "rule_name": "keep-apart"})
    for bad in (
        {"cluster": "example-cluster", "rule_name": "keep-apart"},
        {"cluster": _CLUSTER, "rule_name": ""},
        {"cluster": _CLUSTER},
        {"cluster": _CLUSTER, "rule_name": "keep-apart", "force": True},
    ):
        with pytest.raises(ValidationError):
            validator.validate(bad)


async def test_unreadable_rule_list_fails_before_any_write(gate: GateRecorder) -> None:
    """vCenter could not read the rules: never read that as "no rule"."""
    conn = VimFake()
    _cluster(conn, [_anti_affinity_rule()])
    conn.unreadable[("ClusterComputeResource", _CLUSTER)] = {"configurationEx.rule"}
    with pytest.raises(Exception, match=r"could not read configurationEx\.rule"):
        await _call(conn, cluster=_CLUSTER, rule_name="keep-apart")
    assert gate.calls == []


async def test_parked_gate_keeps_the_write_off_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = GateRecorder(verdict=parked(_RECONFIGURE_OP))
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    conn = VimFake()
    _cluster(conn, [_anti_affinity_rule()])
    out = await _call(conn, cluster=_CLUSTER, rule_name="keep-apart")
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert "ReconfigureComputeResource_Task" not in conn.methods()


async def test_task_fault_raises(gate: GateRecorder) -> None:
    conn = VimFake()
    _cluster(conn, [_anti_affinity_rule()])
    conn.task_faults["ReconfigureComputeResource_Task"] = (
        "InvalidArgument",
        "A specified parameter was not correct: removeKey",
    )
    with pytest.raises(RuntimeError, match="removeKey"):
        await _call(conn, cluster=_CLUSTER, rule_name="keep-apart")


async def test_poll_timeout(gate: GateRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_drs_rule_delete, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = VimFake()
    _cluster(conn, [_anti_affinity_rule()])
    conn.running.add("ReconfigureComputeResource_Task")
    out = await _call(conn, cluster=_CLUSTER, rule_name="keep-apart")
    assert out["status"] == "timeout"
    assert out["task_state"] == "timeout"
    _validate(out)


async def test_deleted_only_after_the_new_read_misses_the_rule(gate: GateRecorder) -> None:
    """The task says success but the rule still reads back: still_present, not deleted."""
    conn = VimFake()
    _cluster(conn, [_anti_affinity_rule()])
    conn.apply_writes = False
    out = await _call(conn, cluster=_CLUSTER, rule_name="keep-apart")
    assert out["status"] == "still_present"
    assert out["task_state"] == "success"
    assert "still reads back" in out["guidance"]
    # The plan read, then the task poll, then the new read of the rules.
    cluster_reads = [
        body
        for path, body in conn.calls_to("RetrievePropertiesEx")
        if body["specSet"][0]["propSet"][0]["type"] == "ClusterComputeResource"
    ]
    assert len(cluster_reads) == 2
    _validate(out)


# ===========================================================================
# Preview (the approval card)
# ===========================================================================


async def test_preview_shows_the_rule_on_the_approval_card() -> None:
    conn = VimFake()
    _cluster(conn, [_anti_affinity_rule()])
    effect = await _drs_rule_delete.cluster_drs_rule_delete_preview(
        preview_ctx({"cluster": _CLUSTER, "rule_name": "keep-apart"}, conn)
    )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    block = effect["blast_radius"]
    assert block["object"]["present"] is True
    assert block["object"]["cluster_name"] == "example-cluster"
    assert block["object"]["rule_type"] == "anti_affinity"
    assert block["object"]["enabled"] is True
    assert block["object"]["mandatory"] is False
    assert block["object"]["vms"] == [
        {"vm": "vm-1", "name": "name-vm-1"},
        {"vm": "vm-2", "name": "name-vm-2"},
    ]
    assert block["children"] == []
    assert block["irreversibility"] == "permanent"
    assert "refusal" not in block
    # The preview never writes.
    assert "ReconfigureComputeResource_Task" not in conn.methods()


async def test_preview_for_a_rule_already_gone_still_parks() -> None:
    conn = VimFake()
    _cluster(conn, [])
    effect = await _drs_rule_delete.cluster_drs_rule_delete_preview(
        preview_ctx({"cluster": _CLUSTER, "rule_name": "keep-apart"}, conn)
    )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    assert effect["blast_radius"]["object"]["present"] is False
    assert effect["blast_radius"]["irreversibility"] == "none-object-already-absent"


async def test_preview_shows_the_duplicate_name_refusal() -> None:
    conn = VimFake()
    _cluster(conn, [_anti_affinity_rule(key=11), _vm_host_rule(key=14, name="keep-apart")])
    effect = await _drs_rule_delete.cluster_drs_rule_delete_preview(
        preview_ctx({"cluster": _CLUSTER, "rule_name": "keep-apart"}, conn)
    )
    assert effect is not None
    assert effect["blast_radius"]["refusal"]["status"] == "precondition_failed"
    assert {b["rule_type"] for b in effect["blast_radius"]["blockers"]} == {
        "anti_affinity",
        "vm_host",
    }


async def test_preview_declines_without_connector_or_with_bad_params() -> None:
    no_connector = await _drs_rule_delete.cluster_drs_rule_delete_preview(
        preview_ctx({"cluster": _CLUSTER, "rule_name": "keep-apart"}, None)
    )
    bad_params = await _drs_rule_delete.cluster_drs_rule_delete_preview(
        preview_ctx({"cluster": _CLUSTER}, VimFake())
    )
    assert no_connector is None
    assert bad_params is None


# ===========================================================================
# The governed flow through the dispatcher (approval gate)
# ===========================================================================

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004005")
_TARGET_NAME = "example-vcenter"
_PARAMS: dict[str, Any] = {"cluster": _CLUSTER, "rule_name": "keep-apart"}


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
        name="DRS Rule Delete Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _bootstrap(conn: VimFake) -> None:
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
                notes="seeded by test_connectors_vmware_rest_composites_drs_rule_delete",
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


@pytest.mark.usefixtures("_dispatch_env")
async def test_registered_destructive_and_needs_approval() -> None:
    await _bootstrap(VimFake())
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP_ID))
        ).scalar_one()
    assert row.safety_level == "destructive"
    assert row.requires_approval is True
    assert row.source_kind == "composite"


@pytest.mark.usefixtures("_dispatch_env")
async def test_waits_for_a_second_person_then_deletes() -> None:
    conn = VimFake()
    _cluster(conn, [_anti_affinity_rule()])
    await _bootstrap(conn)

    requester = _person("op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": _TARGET_NAME,
        "params": _PARAMS,
    }
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview

    # The call waits for approval; nothing is written yet.
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    assert "ReconfigureComputeResource_Task" not in conn.methods()
    request_id = UUID(call["extras"]["approval_request_id"])

    # The approval card shows the rule.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["op_id"] == _OP_ID
    assert effect["safety_level"] == "destructive"
    blast = effect["blast_radius"]
    assert blast["object"]["name"] == "keep-apart"
    assert blast["object"]["rule_type"] == "anti_affinity"
    assert blast["object"]["vm_count"] == 2
    assert blast["irreversibility"] == "permanent"

    # A second person approves; then the delete runs once.
    approver = _person("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "deleted"
    assert [p for p, _ in conn.calls_to("ReconfigureComputeResource_Task")] == [_RECONFIGURE_PATH]


@pytest.mark.usefixtures("_dispatch_env")
async def test_agent_principal_is_refused() -> None:
    conn = VimFake()
    _cluster(conn, [_anti_affinity_rule()])
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeTarget(),
        params=_PARAMS,
    )
    assert result.status == "denied", result
    assert "destructive" in (result.error or "")
    assert "ReconfigureComputeResource_Task" not in conn.methods()
    assert await _approval_count() == 0
