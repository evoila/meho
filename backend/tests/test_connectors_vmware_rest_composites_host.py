# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler + governance tests for the host-domain write composites (#3182).

Covers the three ``vmware.composite.host.*`` write composites shipped in
:mod:`~meho_backplane.connectors.vmware_rest.composites._host`:
``datastore_mount_nfs`` / ``disk_mark_flash`` / ``service_control``.

Two tiers, mirroring the disk-grow write composite's proof
(:mod:`tests.test_connectors_vmware_rest_composites_write` for handler
behaviour +
:mod:`tests.test_connectors_vmware_rest_composites_write_gate` for the
real ``enforce_subop_policy`` gate):

* **Handler behaviour** with a stubbed gate (``_GateRecorder``) and a
  recording connector double -- the happy paths, the allowlist refusal,
  the host-resolution refusals, and the per-disk partial-failure capture.
* **Real-gate governance** against the autouse-migrated SQLite engine:
  an agent-gated write **queues** (durable ``ApprovalRequest``, no wire
  write), a dangerous write with no grant is **denied**, and a human
  operator's already-approved composite **auto-executes** -- the #3182
  proof that each host write rides the same governed VI-JSON seam the
  #2893 disk-grow write established (the mutating vim method never
  reaches the wire when the gate parks / denies).
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
from meho_backplane.connectors.vmware_rest.composites import _host, _write
from meho_backplane.connectors.vmware_rest.composites._host import (
    datastore_mount_nfs_composite,
    disk_mark_flash_composite,
    service_control_composite,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    ApprovalRequestStatus,
    PermissionVerdict,
)
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000031a6")

# Governance op_ids the host writes gate on (the vim ``METHOD:/path`` keys).
_CREATE_NAS_OP_ID = "POST:/HostDatastoreSystem/{moId}/CreateNasDatastore"
_MARK_SSD_OP_ID = "POST:/HostStorageSystem/{moId}/MarkAsSsd_Task"
_START_SERVICE_OP_ID = "POST:/HostServiceSystem/{moId}/StartService"

# Default config-manager sub-manager moids the double resolves per prop.
_DEFAULT_CONFIG_MANAGERS: dict[str, tuple[str, str]] = {
    "configManager.datastoreSystem": ("HostDatastoreSystem", "datastoreSystem-15"),
    "configManager.storageSystem": ("HostStorageSystem", "storageSystem-15"),
    "configManager.serviceSystem": ("HostServiceSystem", "serviceSystem-15"),
}


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
    sub: str = "agent-host-composite",
) -> Operator:
    return Operator(
        sub=sub,
        name="Host Composite Governance",
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


class _GateRecorder:
    """Recording stub for :func:`enforce_subop_policy` (auto-execute by default)."""

    def __init__(self, gate_for: dict[str, OperationResult] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._gate_for = gate_for or {}

    async def __call__(
        self,
        *,
        operator: Operator,
        connector_id: str,
        op_id: str,
        safety_level: str,
        requires_approval: bool,
        target: Any,
        params: dict[str, Any],
    ) -> OperationResult | None:
        self.calls.append(
            {
                "op_id": op_id,
                "connector_id": connector_id,
                "safety_level": safety_level,
                "requires_approval": requires_approval,
                "params": dict(params),
            }
        )
        return self._gate_for.get(op_id)

    @property
    def gated_op_ids(self) -> list[str]:
        return [c["op_id"] for c in self.calls]


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    """Install a default (auto-execute) gate recorder on the ``_write`` module.

    The host handlers route their mutating writes through
    ``_write._write_vmomi_sub_op``, which resolves ``enforce_subop_policy`` in
    the ``_write`` module namespace -- so patching it there gates the host
    writes too.
    """
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


class _HostRecordingConnector:
    """Recording connector double for the host-domain composite tests.

    Serves the ``GET:/vcenter/host`` resolution listing (keyed off the
    ``names`` / ``hosts`` filter), the ``configManager.<sub>``
    PropertyCollector read, and the ``Task.info`` poll, and records every
    mutating vmomi write so the tests can assert what did / did not reach
    the wire.
    """

    def __init__(
        self,
        *,
        hosts: list[dict[str, str]] | None = None,
        config_managers: dict[str, tuple[str, str]] | None = None,
        fault_tasks: set[str] | None = None,
    ) -> None:
        self.hosts = hosts if hosts is not None else [{"host": "host-15", "name": "esxi-01"}]
        # None → the default all-resolvable map; {} → nothing resolves (unreadable).
        self.config_managers = (
            _DEFAULT_CONFIG_MANAGERS if config_managers is None else config_managers
        )
        self._fault_tasks = fault_tasks or set()
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
        params = params or {}
        names = params.get("names") or params.get("filter.names")
        if names:
            return [h for h in self.hosts if h["name"] in names]
        wanted = params.get("hosts") or params.get("filter.hosts")
        if wanted:
            return [h for h in self.hosts if h["host"] in wanted]
        return list(self.hosts)

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        if path.endswith("/RetrievePropertiesEx"):
            return self._serve_retrieve(json)
        self.writes.append({"path": path, "json": json})
        if path.endswith("/CreateNasDatastore"):
            return {
                "_typeName": "ManagedObjectReference",
                "type": "Datastore",
                "value": "datastore-99",
            }
        if path.endswith("_Task"):
            task = f"task-{json['scsiDiskUuid']}"
            return {"_typeName": "ManagedObjectReference", "type": "Task", "value": task}
        return None  # 204-No-Content service methods

    def _serve_retrieve(self, body: Any) -> dict[str, Any]:
        spec = body["specSet"][0]
        spec_type = spec["propSet"][0]["type"]
        if spec_type == "HostSystem":
            prop = spec["propSet"][0]["pathSet"][0]
            resolved = self.config_managers.get(prop)
            prop_set = (
                []
                if resolved is None
                else [
                    {
                        "name": prop,
                        "val": {
                            "_typeName": "ManagedObjectReference",
                            "type": resolved[0],
                            "value": resolved[1],
                        },
                    }
                ]
            )
            return {
                "objects": [
                    {"obj": {"type": "HostSystem", "value": "host-15"}, "propSet": prop_set}
                ]
            }
        # Task.info poll.
        task_moid = spec["objectSet"][0]["obj"]["value"]
        info: dict[str, Any] = (
            {"state": "error"} if task_moid in self._fault_tasks else {"state": "success"}
        )
        if task_moid in self._fault_tasks:
            info["error"] = {"localizedMessage": "disk not found"}
        return {
            "objects": [
                {
                    "obj": {"type": "Task", "value": task_moid},
                    "propSet": [{"name": "info", "val": info}],
                }
            ]
        }


# ===========================================================================
# datastore_mount_nfs — handler behaviour
# ===========================================================================


@pytest.mark.asyncio
async def test_datastore_mount_nfs_mounts_and_returns_summary(gate: _GateRecorder) -> None:
    conn = _HostRecordingConnector()
    out = await datastore_mount_nfs_composite(
        operator=_operator(),
        target=None,
        params={
            "host": "esxi-01",
            "nfs_server": "10.0.0.5",
            "remote_path": "/export/base",
            "datastore_name": "base-nfs",
            "access_mode": "readOnly",
            "nfs_type": "NFS41",
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "mounted"
    assert out["host"] == "host-15"
    assert out["datastore"] == "datastore-99"
    assert out["summary"] == {
        "datastore": "datastore-99",
        "name": "base-nfs",
        "nfs_server": "10.0.0.5",
        "remote_path": "/export/base",
        "access_mode": "readOnly",
        "type": "NFS41",
    }
    # The mount landed on the host's HostDatastoreSystem sub-manager, with the
    # HostNasVolumeSpec fully tagged and populated.
    assert len(conn.writes) == 1
    write = conn.writes[0]
    assert write["path"] == "/HostDatastoreSystem/datastoreSystem-15/CreateNasDatastore"
    spec = write["json"]["spec"]
    assert spec["_typeName"] == "HostNasVolumeSpec"
    assert spec["remoteHost"] == "10.0.0.5"
    assert spec["localPath"] == "base-nfs"
    assert spec["accessMode"] == "readOnly"
    assert spec["type"] == "NFS41"
    # The gate saw the create op with the entity in its params.
    assert gate.gated_op_ids == [_CREATE_NAS_OP_ID]


@pytest.mark.asyncio
async def test_datastore_mount_nfs_defaults_access_mode_and_type(gate: _GateRecorder) -> None:
    conn = _HostRecordingConnector()
    out = await datastore_mount_nfs_composite(
        operator=_operator(),
        target=None,
        params={
            "host": "host-15",
            "nfs_server": "nfs.local",
            "remote_path": "/vol/iso",
            "datastore_name": "iso",
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "mounted"  # type: ignore[index]
    spec = conn.writes[0]["json"]["spec"]
    assert spec["accessMode"] == "readWrite"
    assert spec["type"] == "NFS"


@pytest.mark.asyncio
async def test_datastore_mount_nfs_refuses_ambiguous_host(gate: _GateRecorder) -> None:
    conn = _HostRecordingConnector(
        hosts=[{"host": "host-1", "name": "dup"}, {"host": "host-2", "name": "dup"}]
    )
    out = await datastore_mount_nfs_composite(
        operator=_operator(),
        target=None,
        params={
            "host": "dup",
            "nfs_server": "n",
            "remote_path": "/p",
            "datastore_name": "d",
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "ambiguous_host"
    assert sorted(out["candidate_hosts"]) == ["host-1", "host-2"]
    assert out["datastore"] is None
    assert conn.writes == []  # refused before any write
    assert gate.calls == []  # never reached the gate


@pytest.mark.asyncio
async def test_datastore_mount_nfs_refuses_unknown_host(gate: _GateRecorder) -> None:
    conn = _HostRecordingConnector(hosts=[])
    out = await datastore_mount_nfs_composite(
        operator=_operator(),
        target=None,
        params={"host": "ghost", "nfs_server": "n", "remote_path": "/p", "datastore_name": "d"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "host_not_found"  # type: ignore[index]
    assert conn.writes == []


@pytest.mark.asyncio
async def test_datastore_mount_nfs_refuses_when_config_manager_unreadable(
    gate: _GateRecorder,
) -> None:
    conn = _HostRecordingConnector(config_managers={})
    out = await datastore_mount_nfs_composite(
        operator=_operator(),
        target=None,
        params={"host": "esxi-01", "nfs_server": "n", "remote_path": "/p", "datastore_name": "d"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "config_manager_unreadable"  # type: ignore[index]
    assert conn.writes == []


@pytest.mark.asyncio
async def test_datastore_mount_nfs_resolves_host_by_moref(gate: _GateRecorder) -> None:
    """A moref that is not a display name resolves via the filter.hosts fallback."""
    conn = _HostRecordingConnector(hosts=[{"host": "host-42", "name": "esxi-nine"}])
    out = await datastore_mount_nfs_composite(
        operator=_operator(),
        target=None,
        params={"host": "host-42", "nfs_server": "n", "remote_path": "/p", "datastore_name": "d"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "mounted"  # type: ignore[index]
    assert out["host"] == "host-42"  # type: ignore[index]


# ===========================================================================
# disk_mark_flash — handler behaviour
# ===========================================================================


@pytest.mark.asyncio
async def test_disk_mark_flash_marks_every_disk(gate: _GateRecorder) -> None:
    conn = _HostRecordingConnector()
    out = await disk_mark_flash_composite(
        operator=_operator(),
        target=None,
        params={"host": "esxi-01", "disk_uuids": ["uuid-a", "uuid-b"]},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "marked"
    assert out["mode"] == "flash"
    assert out["summary"] == {"marked": 2, "failed": 0}
    assert [row["disk_uuid"] for row in out["results"]] == ["uuid-a", "uuid-b"]
    assert all(row["status"] == "marked" for row in out["results"])
    # One MarkAsSsd_Task per disk on the storage sub-manager.
    mark_writes = [w for w in conn.writes if w["path"].endswith("MarkAsSsd_Task")]
    assert len(mark_writes) == 2
    assert mark_writes[0]["path"] == "/HostStorageSystem/storageSystem-15/MarkAsSsd_Task"
    assert mark_writes[0]["json"] == {"scsiDiskUuid": "uuid-a"}


@pytest.mark.asyncio
async def test_disk_mark_flash_non_flash_uses_mark_as_non_ssd(gate: _GateRecorder) -> None:
    conn = _HostRecordingConnector()
    out = await disk_mark_flash_composite(
        operator=_operator(),
        target=None,
        params={"host": "esxi-01", "disk_uuids": ["uuid-a"], "mode": "non_flash"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "marked"  # type: ignore[index]
    assert out["mode"] == "non_flash"  # type: ignore[index]
    assert conn.writes[0]["path"] == "/HostStorageSystem/storageSystem-15/MarkAsNonSsd_Task"
    # The gate governs on the MarkAsNonSsd op_id (the inverse is the same op).
    assert gate.gated_op_ids == ["POST:/HostStorageSystem/{moId}/MarkAsNonSsd_Task"]


@pytest.mark.asyncio
async def test_disk_mark_flash_partial_on_faulted_disk(gate: _GateRecorder) -> None:
    conn = _HostRecordingConnector(fault_tasks={"task-uuid-b"})
    out = await disk_mark_flash_composite(
        operator=_operator(),
        target=None,
        params={"host": "esxi-01", "disk_uuids": ["uuid-a", "uuid-b"]},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "partial"  # type: ignore[index]
    assert out["summary"] == {"marked": 1, "failed": 1}  # type: ignore[index]
    rows = {row["disk_uuid"]: row for row in out["results"]}  # type: ignore[index]
    assert rows["uuid-a"]["status"] == "marked"
    assert rows["uuid-b"]["status"] == "faulted"
    assert rows["uuid-b"]["error"] == "disk not found"


@pytest.mark.asyncio
async def test_disk_mark_flash_refuses_unknown_host(gate: _GateRecorder) -> None:
    conn = _HostRecordingConnector(hosts=[])
    out = await disk_mark_flash_composite(
        operator=_operator(),
        target=None,
        params={"host": "ghost", "disk_uuids": ["uuid-a"]},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "host_not_found"  # type: ignore[index]
    assert out["results"] == []  # type: ignore[index]
    assert conn.writes == []


# ===========================================================================
# service_control — handler behaviour + allowlist
# ===========================================================================


@pytest.mark.asyncio
async def test_service_control_applies_action_and_policy(gate: _GateRecorder) -> None:
    conn = _HostRecordingConnector()
    out = await service_control_composite(
        operator=_operator(),
        target=None,
        params={"host": "esxi-01", "service": "TSM-SSH", "action": "start", "policy": "on"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "applied"
    assert out["policy_updated"] is True
    paths = [w["path"] for w in conn.writes]
    assert paths == [
        "/HostServiceSystem/serviceSystem-15/StartService",
        "/HostServiceSystem/serviceSystem-15/UpdateServicePolicy",
    ]
    assert conn.writes[0]["json"] == {"id": "TSM-SSH"}
    assert conn.writes[1]["json"] == {"id": "TSM-SSH", "policy": "on"}


@pytest.mark.asyncio
async def test_service_control_without_policy_skips_policy_update(gate: _GateRecorder) -> None:
    conn = _HostRecordingConnector()
    out = await service_control_composite(
        operator=_operator(),
        target=None,
        params={"host": "esxi-01", "service": "ntpd", "action": "restart"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "applied"  # type: ignore[index]
    assert out["policy_updated"] is False  # type: ignore[index]
    assert [w["path"] for w in conn.writes] == [
        "/HostServiceSystem/serviceSystem-15/RestartService"
    ]


@pytest.mark.asyncio
async def test_service_control_refuses_out_of_allowlist_service(gate: _GateRecorder) -> None:
    """An out-of-list service is refused before any resolution or write."""
    conn = _HostRecordingConnector()
    out = await service_control_composite(
        operator=_operator(),
        target=None,
        params={"host": "esxi-01", "service": "vpxa", "action": "stop"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "service_not_allowed"
    assert out["service"] == "vpxa"
    assert out["allowed_services"] == ["TSM", "TSM-SSH", "ntpd", "ptpd"]
    assert conn.writes == []
    assert gate.calls == []  # never resolved a host, never gated


@pytest.mark.asyncio
async def test_service_control_refuses_unknown_host(gate: _GateRecorder) -> None:
    conn = _HostRecordingConnector(hosts=[])
    out = await service_control_composite(
        operator=_operator(),
        target=None,
        params={"host": "ghost", "service": "TSM-SSH", "action": "start"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "host_not_found"  # type: ignore[index]
    assert conn.writes == []


# ===========================================================================
# Real-gate governance — the mutating vim write never reaches the wire when
# the gate parks / denies (#3182, mirroring the disk-grow trio)
# ===========================================================================


@pytest.mark.asyncio
async def test_datastore_mount_gated_write_queues_and_never_reaches_wire(
    session: AsyncSession,
) -> None:
    await _grant(
        principal_sub="agent-host-composite",
        op_pattern=_CREATE_NAS_OP_ID,
        verdict=PermissionVerdict.NEEDS_APPROVAL,
    )
    conn = _HostRecordingConnector()
    out = await datastore_mount_nfs_composite(
        operator=_operator(),
        target=None,
        params={"host": "esxi-01", "nfs_server": "n", "remote_path": "/p", "datastore_name": "d"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert out.op_id == _CREATE_NAS_OP_ID
    request_id = uuid.UUID(out.extras["approval_request_id"])
    row = await session.get(ApprovalRequest, request_id)
    assert row is not None
    assert row.connector_id == "vmware-rest-9.0"
    assert row.status == ApprovalRequestStatus.PENDING.value
    assert conn.writes == []  # CreateNasDatastore never reached the wire


@pytest.mark.asyncio
async def test_datastore_mount_denied_without_grant(session: AsyncSession) -> None:
    conn = _HostRecordingConnector()
    out = await datastore_mount_nfs_composite(
        operator=_operator(sub="agent-no-grant"),
        target=None,
        params={"host": "esxi-01", "nfs_server": "n", "remote_path": "/p", "datastore_name": "d"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "denied"
    assert out.op_id == _CREATE_NAS_OP_ID
    count = await session.scalar(select(func.count()).select_from(ApprovalRequest))
    assert count == 0
    assert conn.writes == []


@pytest.mark.asyncio
async def test_datastore_mount_human_operator_auto_executes(session: AsyncSession) -> None:
    conn = _HostRecordingConnector()
    out = await datastore_mount_nfs_composite(
        operator=_operator(principal_kind=PrincipalKind.USER, sub="human-op"),
        target=None,
        params={"host": "esxi-01", "nfs_server": "n", "remote_path": "/p", "datastore_name": "d"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "mounted"
    assert len(conn.writes) == 1
    count = await session.scalar(select(func.count()).select_from(ApprovalRequest))
    assert count == 0


@pytest.mark.asyncio
async def test_disk_mark_flash_gated_write_queues_and_never_reaches_wire(
    session: AsyncSession,
) -> None:
    await _grant(
        principal_sub="agent-host-composite",
        op_pattern=_MARK_SSD_OP_ID,
        verdict=PermissionVerdict.NEEDS_APPROVAL,
    )
    conn = _HostRecordingConnector()
    out = await disk_mark_flash_composite(
        operator=_operator(),
        target=None,
        params={"host": "esxi-01", "disk_uuids": ["uuid-a", "uuid-b"]},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert out.op_id == _MARK_SSD_OP_ID
    # Parked on the first disk — no MarkAs*_Task reached the wire for any disk.
    assert conn.writes == []


@pytest.mark.asyncio
async def test_disk_mark_flash_denied_without_grant(session: AsyncSession) -> None:
    conn = _HostRecordingConnector()
    out = await disk_mark_flash_composite(
        operator=_operator(sub="agent-no-grant"),
        target=None,
        params={"host": "esxi-01", "disk_uuids": ["uuid-a"]},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "denied"
    assert out.op_id == _MARK_SSD_OP_ID
    assert conn.writes == []


@pytest.mark.asyncio
async def test_disk_mark_flash_human_operator_auto_executes(session: AsyncSession) -> None:
    conn = _HostRecordingConnector()
    out = await disk_mark_flash_composite(
        operator=_operator(principal_kind=PrincipalKind.USER, sub="human-op"),
        target=None,
        params={"host": "esxi-01", "disk_uuids": ["uuid-a"]},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "marked"
    assert len([w for w in conn.writes if w["path"].endswith("MarkAsSsd_Task")]) == 1


@pytest.mark.asyncio
async def test_service_control_gated_write_queues_and_never_reaches_wire(
    session: AsyncSession,
) -> None:
    await _grant(
        principal_sub="agent-host-composite",
        op_pattern=_START_SERVICE_OP_ID,
        verdict=PermissionVerdict.NEEDS_APPROVAL,
    )
    conn = _HostRecordingConnector()
    out = await service_control_composite(
        operator=_operator(),
        target=None,
        params={"host": "esxi-01", "service": "TSM-SSH", "action": "start", "policy": "on"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert out.op_id == _START_SERVICE_OP_ID
    # Parked on the action — neither StartService nor UpdateServicePolicy fired.
    assert conn.writes == []


@pytest.mark.asyncio
async def test_service_control_denied_without_grant(session: AsyncSession) -> None:
    conn = _HostRecordingConnector()
    out = await service_control_composite(
        operator=_operator(sub="agent-no-grant"),
        target=None,
        params={"host": "esxi-01", "service": "TSM-SSH", "action": "start"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "denied"
    assert out.op_id == _START_SERVICE_OP_ID
    assert conn.writes == []


@pytest.mark.asyncio
async def test_service_control_human_operator_auto_executes(session: AsyncSession) -> None:
    conn = _HostRecordingConnector()
    out = await service_control_composite(
        operator=_operator(principal_kind=PrincipalKind.USER, sub="human-op"),
        target=None,
        params={"host": "esxi-01", "service": "TSM-SSH", "action": "start"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "applied"
    assert [w["path"] for w in conn.writes] == ["/HostServiceSystem/serviceSystem-15/StartService"]


@pytest.mark.asyncio
async def test_service_control_allowlist_is_the_expected_bounded_set() -> None:
    """Pin the curated allowlist so an accidental widening is caught in review."""
    assert frozenset({"TSM-SSH", "TSM", "ntpd", "ptpd"}) == _host._SERVICE_ALLOWLIST
