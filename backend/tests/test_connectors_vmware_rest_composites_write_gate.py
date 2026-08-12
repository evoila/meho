# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end governance tests for the migrated vmware write composites.

Task #2256 (Initiative #2249, Goal #2247). The hard acceptance gate: a
write composite that has migrated off ``dispatch_child`` to the direct
session must keep property 3 of #508's four guarantees -- a now-internal
write sub-op that is approval-gated (or denied) for the caller still
**queues** (or is refused) instead of silently executing.

Unlike :mod:`tests.test_connectors_vmware_rest_composites_write` (which
stubs :func:`enforce_subop_policy`), this module runs the **real**
:func:`~meho_backplane.operations.composite.enforce_subop_policy` seam
against the autouse-migrated SQLite engine, driving a composite handler
with a recording connector double so the assertion is end-to-end:

* an agent principal with a per-op ``needs_approval`` grant on the write
  sub-op -> the composite returns an ``awaiting_approval`` result, a
  durable :class:`ApprovalRequest` row exists for the sub-op, and the
  connector session never issues the write;
* an agent principal with no grant on a ``dangerous`` write -> the
  composite returns ``denied`` and the write never runs (governance not
  lowered);
* a human operator whose top-level composite was already approved ->
  the sub-op gate auto-executes (``requires_approval=False`` keeps the
  top-level composite the single approval gate), so the write proceeds.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.composites._write import (
    vm_clone_from_template_composite,
    vm_create_composite,
    vm_disk_grow_composite,
    vm_migrate_composite,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    ApprovalRequestStatus,
    PermissionVerdict,
)
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000025a6")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Open a session against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _operator(
    *,
    principal_kind: PrincipalKind = PrincipalKind.AGENT,
    sub: str = "agent-write-composite",
) -> Operator:
    return Operator(
        sub=sub,
        name="Write Composite Governance",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


async def _grant(*, principal_sub: str, op_pattern: str, verdict: PermissionVerdict) -> None:
    """Seed one AgentPermission grant for *principal_sub* on *op_pattern*."""
    async with get_sessionmaker()() as s:
        s.add(
            AgentPermission(
                tenant_id=_TENANT_ID,
                principal_sub=principal_sub,
                op_pattern=op_pattern,
                verdict=verdict.value,
                target_scope="*",
                created_by_sub="ops-admin",
            )
        )
        await s.commit()


class _RecordingConnector:
    """Minimal recording connector double for the governance tests.

    Serves canned read payloads and records writes. The governance tests
    assert on whether a *write* ``_post_json`` ever fires -- the read
    payloads only need to steer the handler to its first write.
    """

    def __init__(self, reads: dict[str, Any]) -> None:
        self._reads = reads
        self.writes: list[dict[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params("/api", query)

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        return self._reads[path]

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Any = None,
        data: Any = None,
        extra_headers: Any = None,
    ) -> Any:
        self.writes.append({"verb": verb, "path": path, "body": json})
        return {"value": "vm-should-not-exist"}


@pytest.mark.asyncio
async def test_gated_write_subop_queues_for_approval_via_composite(session: AsyncSession) -> None:
    """An agent-gated create queues for approval via the composite (hard gate).

    The migrated ``vm.create`` composite, driven by an agent principal whose
    ``POST:/vcenter/vm`` grant is ``needs_approval``, returns an
    ``awaiting_approval`` result, writes a durable pending
    :class:`ApprovalRequest` for the sub-op, and never issues the create on
    the connector session.
    """
    await _grant(
        principal_sub="agent-write-composite",
        op_pattern="POST:/vcenter/vm",
        verdict=PermissionVerdict.NEEDS_APPROVAL,
    )
    conn = _RecordingConnector({"/api/vcenter/folder": [{"folder": "folder-1", "name": "Prod"}]})

    out = await vm_create_composite(
        operator=_operator(),
        target=None,
        params={"folder_name": "Prod", "name": "web-01", "guest_os": "UBUNTU_64"},
        connector=conn,  # type: ignore[arg-type]
    )

    # The seam signalled "do not execute" -> the composite returned it verbatim.
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert out.op_id == "POST:/vcenter/vm"
    request_id = uuid.UUID(out.extras["approval_request_id"])

    # A durable pending approval row exists for the sub-op.
    row = await session.get(ApprovalRequest, request_id)
    assert row is not None
    assert row.op_id == "POST:/vcenter/vm"
    assert row.connector_id == "vmware-rest-9.0"
    assert row.status == ApprovalRequestStatus.PENDING.value
    # The create was never issued on the session.
    assert conn.writes == []


@pytest.mark.asyncio
async def test_dangerous_write_subop_denied_without_grant(session: AsyncSession) -> None:
    """An agent with no grant is denied the dangerous relocate write; it never runs."""
    conn = _RecordingConnector(
        {"/api/vcenter/cluster/c-1/drs/recommendations": [{"vm": "vm-1", "target_host": "host-A"}]}
    )
    out = await vm_migrate_composite(
        operator=_operator(sub="agent-no-grant"),
        target=None,
        params={"vm": "vm-1", "cluster": "c-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "denied"
    assert out.op_id == "POST:/vcenter/vm/{vm}?action=relocate"
    # No pending row: a deny is not a park.
    count = await session.scalar(select(func.count()).select_from(ApprovalRequest))
    assert count == 0
    # The relocate write never fired.
    assert conn.writes == []


@pytest.mark.asyncio
async def test_human_operator_subop_auto_executes(session: AsyncSession) -> None:
    """A human operator's already-approved composite auto-executes its writes.

    ``requires_approval=False`` on the sub-op keeps the top-level composite
    the single approval gate: a human/service operator (whose composite the
    dispatcher already parked + approved at the top level) clears the sub-op
    gate and the write proceeds -- no double-gate on the resume path.
    """
    conn = _RecordingConnector(
        {"/api/vcenter/cluster/c-1/drs/recommendations": [{"vm": "vm-1", "target_host": "host-A"}]}
    )
    out = await vm_migrate_composite(
        operator=_operator(principal_kind=PrincipalKind.USER, sub="human-op"),
        target=None,
        params={"vm": "vm-1", "cluster": "c-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "migrated"
    # The relocate write executed on the session.
    assert [w["path"] for w in conn.writes] == ["/api/vcenter/vm/vm-1?action=relocate"]
    # No approval row -- the sub-op auto-executed.
    count = await session.scalar(select(func.count()).select_from(ApprovalRequest))
    assert count == 0


# ===========================================================================
# vm.disk.grow — the mutating VI-JSON write flows through the same gate (#2893)
# ===========================================================================


_RECONFIG_OP_ID = "POST:/VirtualMachine/{moId}/ReconfigVM_Task"


class _DiskGrowRecordingConnector:
    """Recording double for the disk-grow governance tests.

    Serves the ``config.hardware.device`` read + the ``Task.info`` poll
    (both vmomi ``RetrievePropertiesEx``, distinguished by the request
    body's ``specSet`` object type) and records every ``ReconfigVM_Task``
    write so the tests can assert the mutating vmomi POST never fired when
    the gate parks / denies.
    """

    def __init__(self, *, capacity_bytes: int = 10 * 1024**3) -> None:
        self._capacity_bytes = capacity_bytes
        self.reconfig_writes: list[Any] = []

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        if path.endswith("/ReconfigVM_Task"):
            self.reconfig_writes.append(json)
            return {"type": "Task", "value": "task-grow-1"}
        spec_type = json["specSet"][0]["propSet"][0]["type"]
        if spec_type == "VirtualMachine":
            device = {
                "_typeName": "VirtualDisk",
                "key": 2000,
                "capacityInBytes": self._capacity_bytes,
                "backing": {
                    "_typeName": "VirtualDiskFlatVer2BackingInfo",
                    "fileName": "[ds] a.vmdk",
                },
            }
            return {
                "objects": [
                    {
                        "obj": {"type": "VirtualMachine", "value": "vm-1"},
                        "propSet": [{"name": "config.hardware.device", "val": [device]}],
                    }
                ]
            }
        return {
            "objects": [
                {
                    "obj": {"type": "Task", "value": "task-grow-1"},
                    "propSet": [{"name": "info", "val": {"state": "success"}}],
                }
            ]
        }


@pytest.mark.asyncio
async def test_disk_grow_gated_vmomi_write_queues_and_never_reaches_wire(
    session: AsyncSession,
) -> None:
    """An agent-gated ReconfigVM_Task queues for approval; the vmomi write never fires.

    The hard #2893 gate: a *mutating VI-JSON* sub-op is a write, not
    transport detail — it flows through the same real
    :func:`enforce_subop_policy` seam the REST writes do. With a
    ``needs_approval`` grant on the vim method, ``vm.disk.grow`` returns
    ``awaiting_approval``, writes a durable pending row, and issues no
    ReconfigVM_Task on the connector session.
    """
    await _grant(
        principal_sub="agent-write-composite",
        op_pattern=_RECONFIG_OP_ID,
        verdict=PermissionVerdict.NEEDS_APPROVAL,
    )
    conn = _DiskGrowRecordingConnector()

    out = await vm_disk_grow_composite(
        operator=_operator(),
        target=None,
        params={"vm": "vm-1", "disk": "2000", "capacity_bytes": 20 * 1024**3},
        connector=conn,  # type: ignore[arg-type]
    )

    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert out.op_id == _RECONFIG_OP_ID
    request_id = uuid.UUID(out.extras["approval_request_id"])
    row = await session.get(ApprovalRequest, request_id)
    assert row is not None
    assert row.op_id == _RECONFIG_OP_ID
    assert row.connector_id == "vmware-rest-9.0"
    assert row.status == ApprovalRequestStatus.PENDING.value
    # The mutating ReconfigVM_Task never reached the wire.
    assert conn.reconfig_writes == []


@pytest.mark.asyncio
async def test_disk_grow_dangerous_vmomi_write_denied_without_grant(
    session: AsyncSession,
) -> None:
    """An agent with no grant is denied the dangerous ReconfigVM_Task; it never runs."""
    conn = _DiskGrowRecordingConnector()
    out = await vm_disk_grow_composite(
        operator=_operator(sub="agent-no-grant"),
        target=None,
        params={"vm": "vm-1", "disk": "2000", "capacity_bytes": 20 * 1024**3},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "denied"
    assert out.op_id == _RECONFIG_OP_ID
    # No pending row: a deny is not a park.
    count = await session.scalar(select(func.count()).select_from(ApprovalRequest))
    assert count == 0
    assert conn.reconfig_writes == []


@pytest.mark.asyncio
async def test_disk_grow_human_operator_vmomi_write_auto_executes(
    session: AsyncSession,
) -> None:
    """A human operator's already-approved composite auto-executes the ReconfigVM_Task."""
    conn = _DiskGrowRecordingConnector()
    out = await vm_disk_grow_composite(
        operator=_operator(principal_kind=PrincipalKind.USER, sub="human-op"),
        target=None,
        params={"vm": "vm-1", "disk": "2000", "capacity_bytes": 20 * 1024**3},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "grown"
    # The reconfigure write executed on the session; the sub-op auto-executed.
    assert len(conn.reconfig_writes) == 1
    count = await session.scalar(select(func.count()).select_from(ApprovalRequest))
    assert count == 0


# ===========================================================================
# vm.clone_from_template — the mutating VI-JSON CloneVM_Task flows through the
# same gate (#2894)
# ===========================================================================


_CLONE_OP_ID = "POST:/VirtualMachine/{moId}/CloneVM_Task"


class _CloneRecordingConnector:
    """Recording double for the clone-from-template governance tests.

    Serves the template resolution (REST ``GET:/vcenter/vm``), the
    ``config.template`` assert + ``Task.info`` poll (both vmomi
    ``RetrievePropertiesEx``, keyed apart by the request body's ``specSet``
    object type) and records every ``CloneVM_Task`` write, so the tests can
    assert the mutating vmomi POST never fired when the gate parks / denies.
    """

    def __init__(self) -> None:
        self.clone_writes: list[Any] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params("/api", query)

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        return {"value": [{"vm": "vm-42", "name": "ubuntu-template"}]}

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        if path.endswith("/CloneVM_Task"):
            self.clone_writes.append(json)
            return {"type": "Task", "value": "task-clone-1"}
        spec_type = json["specSet"][0]["propSet"][0]["type"]
        if spec_type == "VirtualMachine":
            return {
                "objects": [
                    {
                        "obj": {"type": "VirtualMachine", "value": "vm-42"},
                        "propSet": [{"name": "config.template", "val": True}],
                    }
                ]
            }
        return {
            "objects": [
                {
                    "obj": {"type": "Task", "value": "task-clone-1"},
                    "propSet": [
                        {
                            "name": "info",
                            "val": {
                                "state": "success",
                                "result": {"type": "VirtualMachine", "value": "vm-99"},
                            },
                        }
                    ],
                }
            ]
        }


def _clone_gate_params() -> dict[str, Any]:
    return {
        "source_template": "ubuntu-template",
        "new_vm_name": "web-01",
        "folder": "group-v10",
        "resource_pool": "resgroup-8",
        "datastore": "datastore-15",
    }


@pytest.mark.asyncio
async def test_clone_from_template_gated_vmomi_write_queues_and_never_reaches_wire(
    session: AsyncSession,
) -> None:
    """An agent-gated CloneVM_Task queues for approval; the vmomi write never fires.

    The #2894 gate rides #2893's ``_write_vmomi_sub_op`` seam: a *mutating
    VI-JSON* CloneVM_Task is a write, not transport detail, so with a
    ``needs_approval`` grant on the vim method the composite returns
    ``awaiting_approval``, writes a durable pending row, and issues no clone.
    """
    await _grant(
        principal_sub="agent-write-composite",
        op_pattern=_CLONE_OP_ID,
        verdict=PermissionVerdict.NEEDS_APPROVAL,
    )
    conn = _CloneRecordingConnector()

    out = await vm_clone_from_template_composite(
        operator=_operator(),
        target=None,
        params=_clone_gate_params(),
        connector=conn,  # type: ignore[arg-type]
    )

    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert out.op_id == _CLONE_OP_ID
    request_id = uuid.UUID(out.extras["approval_request_id"])
    row = await session.get(ApprovalRequest, request_id)
    assert row is not None
    assert row.op_id == _CLONE_OP_ID
    assert row.connector_id == "vmware-rest-9.0"
    assert row.status == ApprovalRequestStatus.PENDING.value
    # The mutating CloneVM_Task never reached the wire.
    assert conn.clone_writes == []


@pytest.mark.asyncio
async def test_clone_from_template_dangerous_vmomi_write_denied_without_grant(
    session: AsyncSession,
) -> None:
    """An agent with no grant is denied the dangerous CloneVM_Task; it never runs."""
    conn = _CloneRecordingConnector()
    out = await vm_clone_from_template_composite(
        operator=_operator(sub="agent-no-grant"),
        target=None,
        params=_clone_gate_params(),
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "denied"
    assert out.op_id == _CLONE_OP_ID
    count = await session.scalar(select(func.count()).select_from(ApprovalRequest))
    assert count == 0
    assert conn.clone_writes == []


@pytest.mark.asyncio
async def test_clone_from_template_human_operator_vmomi_write_auto_executes(
    session: AsyncSession,
) -> None:
    """A human operator's already-approved composite auto-executes the CloneVM_Task."""
    conn = _CloneRecordingConnector()
    out = await vm_clone_from_template_composite(
        operator=_operator(principal_kind=PrincipalKind.USER, sub="human-op"),
        target=None,
        params=_clone_gate_params(),
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "cloned"
    # The CloneVM_Task executed on the session; the sub-op auto-executed.
    assert len(conn.clone_writes) == 1
    count = await session.scalar(select(func.count()).select_from(ApprovalRequest))
    assert count == 0
