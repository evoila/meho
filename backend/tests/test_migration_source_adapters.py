# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The source adapters of the migration inventory, with made-up child answers.

Each ``<product>.migration.inventory`` adapter runs its connector's own reads
through ``dispatch_child``. Here a fake ``dispatch_child`` answers those
reads from fixtures; the adapter's inventory is stored in an in-memory
result store and read back through the real loader and the CSV mapping.
Machine names are made up; addresses use the documentation range
192.0.2.0/24.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.migration import adapter_kit
from meho_backplane.connectors.migration.adapter_kit import read_child
from meho_backplane.connectors.migration.azure_migrate_csv import inventory_to_csv
from meho_backplane.connectors.migration.contracts import (
    InventoryEnvelope,
    KubernetesWorkloadRecord,
    ServerRecord,
    bios_uuid_match_key,
)
from meho_backplane.connectors.migration.inventory_load import (
    load_inventories,
    merge_inventories,
)
from meho_backplane.connectors.migration.source_hosts import (
    count_cpus,
    linux_migration_inventory,
    whole_disks,
    winsrv_migration_inventory,
)
from meho_backplane.connectors.migration.source_hyperv import hyperv_migration_inventory
from meho_backplane.connectors.migration.source_k8s import (
    k8s_migration_inventory,
    parse_quantity,
)
from meho_backplane.connectors.migration.source_proxmox import (
    memory_mb,
    proxmox_migration_inventory,
    size_gb,
)
from meho_backplane.connectors.migration.source_register import (
    SOURCE_ADAPTERS,
    _llm_instructions,
)
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.reducer import full_result_capture_var
from meho_backplane.settings import get_settings

_PLACEHOLDER_UUID = "00000000-0000-0000-0000-000000000000"

_SUB = "operator:fixture"


class _MemoryValkey:
    """The two calls of the async Valkey client the store uses."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


class _Target:
    def __init__(self, name: str) -> None:
        self.name = name


Answer = dict[str, Any] | list[Any] | str | Callable[[dict[str, Any]], Any]


class FakeChildren:
    """Answers child reads by ``op_id`` and ``params``; records every call.

    A value of ``"denied"`` or ``"error"`` answers with that status.
    """

    def __init__(self, answers: dict[tuple[str, str], Answer]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    @staticmethod
    def key(op_id: str, params: dict[str, Any] | None = None) -> tuple[str, str]:
        return op_id, json.dumps(params or {}, sort_keys=True)

    async def __call__(
        self, *, connector_id: str, op_id: str, params: dict[str, Any], target: Any = None
    ) -> OperationResult:
        del target
        self.calls.append((connector_id, op_id, params))
        answer = self.answers.get(self.key(op_id, params), "error")
        if callable(answer):
            answer = answer(params)
        if answer in ("denied", "error"):
            return OperationResult(status=str(answer), op_id=op_id, error="no", duration_ms=1.0)
        return OperationResult(status="ok", op_id=op_id, result=answer, duration_ms=1.0)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> ResultHandleStore:
    memory = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    monkeypatch.setattr(adapter_kit, "get_result_handle_store", lambda: memory)
    return memory


def _operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub=_SUB,
        raw_jwt="fake.jwt.value",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


async def _run(handler: Any, children: FakeChildren, target: str, store: ResultHandleStore) -> Any:
    operator = _operator(uuid4())
    answer = await handler(
        operator=operator, target=_Target(target), params={}, dispatch_child=children
    )
    loaded = await load_inventories(operator, [answer["inventory_handle"]], store=store)
    return answer, loaded.envelopes[0]


def _k(op_id: str, **params: Any) -> tuple[str, str]:
    return FakeChildren.key(op_id, params)


# --- shared parts ------------------------------------------------------------


async def test_read_child_returns_the_full_answer_of_a_shortened_result() -> None:
    full = {"rows": [{"Id": str(n)} for n in range(80)], "total": 80}

    async def child(**kwargs: Any) -> OperationResult:
        capture = full_result_capture_var.get()
        assert capture is not None
        capture.payload, capture.captured = full, True
        return OperationResult(
            status="ok", op_id=kwargs["op_id"], result={"row_count": 80}, duration_ms=1.0
        )

    answer = await read_child(child, connector_id="hyperv-ssh-2022.x", op_id="hyperv.vms.list")  # type: ignore[arg-type]

    assert answer == full
    assert full_result_capture_var.get() is None


async def test_read_child_names_why_a_child_did_not_answer() -> None:
    denied = FakeChildren({_k("linux.about"): "denied"})
    with pytest.raises(adapter_kit.ChildReadError) as raised:
        await read_child(denied, connector_id="linux-ssh-1.x", op_id="linux.about")  # type: ignore[arg-type]
    assert raised.value.reason == "no_permission"
    failed = FakeChildren({})
    with pytest.raises(adapter_kit.ChildReadError) as raised:
        await read_child(failed, connector_id="linux-ssh-1.x", op_id="linux.about")  # type: ignore[arg-type]
    assert raised.value.reason == "collection_failed"


def test_usable_bios_uuid_drops_placeholders() -> None:
    usable = adapter_kit.usable_bios_uuid
    assert (
        usable("{1B2C3D4E-5F60-4718-8293-A4B5C6D7E8F9}") == "1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9"
    )
    assert usable(_PLACEHOLDER_UUID) is None
    assert usable("FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF") is None
    assert usable("not a uuid") is None
    assert usable(None) is None


def test_bios_uuid_match_key_is_the_same_for_both_byte_orders() -> None:
    """A hypervisor's form and the guest's form (first three groups reversed) give one key."""
    as_vcenter = "4e3d2c1b-605f-1847-8293-a4b5c6d7e8f9"
    as_guest = "1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9"
    assert bios_uuid_match_key(as_vcenter) == bios_uuid_match_key(as_guest) == as_guest
    assert bios_uuid_match_key(as_guest.upper()) == as_guest
    # The last two groups are never reversed: a change there is another machine.
    assert bios_uuid_match_key("4e3d2c1b-605f-1847-8293-a4b5c6d7e8fa") != as_guest
    assert bios_uuid_match_key("not a uuid") == "not a uuid"


async def test_counts_and_left_out_items_survive_a_long_omission_list(
    store: ResultHandleStore,
) -> None:
    """The count dicts stay in the answer and in the handle when the answer spills.

    A source with many failed items has an ``omissions`` list larger than
    the inline limit. The reducer then stores the omissions as rows. The
    ``result_objects`` hint keeps ``record_counts``, ``source_totals`` and
    ``left_out_items`` inline and with the rows; without it they are lost.
    """
    children = _hyperv_children()
    rows = [
        {"Name": f"vm-{i:03}", "Id": str(uuid4()), "State": "Off", "Generation": 2}
        for i in range(120)
    ]
    children.answers[_k("hyperv.vms.list")] = {"rows": rows, "total": len(rows)}
    operator = _operator(uuid4())
    answer = await hyperv_migration_inventory(
        operator=operator, target=_Target("hv-1"), params={}, dispatch_child=children
    )
    assert len(json.dumps(answer)) > 4096 and answer["omission_count"] == 120
    hyperv = next(a for a in SOURCE_ADAPTERS if a.product == "hyperv")
    hints = _llm_instructions(hyperv)
    context = {
        "op_id": hyperv.op_id,
        "operator_sub": operator.sub,
        "tenant_id": str(operator.tenant_id),
        "result_scalars": hints["result_scalars"],
        "result_objects": hints["result_objects"],
    }
    reducer = JsonFluxReducer(sample_size=5, store=store, max_spill_rows=10000)

    reduced, handle = await reducer.reduce(answer, None, context)

    assert handle is not None and reduced["source_key"] == "omissions"
    assert reduced["inventory_handle"] == answer["inventory_handle"]
    assert reduced["counts_match"] is True
    assert reduced["record_counts"] == {"server": 120, "application": 0, "kubernetes_workload": 0}
    # The inline copy lists every kind; a kind without a value is null there.
    assert reduced["source_totals"] == {
        "server": 120,
        "application": None,
        "kubernetes_workload": None,
    }
    assert reduced["left_out_items"] == {
        "server": None,
        "application": None,
        "kubernetes_workload": None,
    }
    stored = await store.fetch_document(
        tenant_id=operator.tenant_id, operator_sub=operator.sub, handle_id=handle.handle_id
    )
    assert stored is not None
    assert stored.document["record_counts"] == answer["record_counts"]
    assert stored.document["source_totals"] == {"server": 120}
    assert stored.document["left_out_items"] == {}


def test_unit_parsers() -> None:
    assert parse_quantity("250m") == pytest.approx(0.25)
    assert parse_quantity("512Mi") == 512 * 1024**2
    assert parse_quantity("1e9") == 1e9
    assert parse_quantity("2") == 2
    assert parse_quantity("lots") is None
    assert size_gb("32G") == 32
    assert size_gb("512M") == 0.5
    assert size_gb("1T") == 1024
    assert memory_mb("4096") == 4096
    assert memory_mb("current=8192,shares=1000") == 8192
    assert count_cpus("0-3,6") == 5
    assert whole_disks(
        "major minor  #blocks  name\n\n"
        "   8        0   52428800 sda\n   8        1   52427776 sda1\n"
        " 259        0  104857600 nvme0n1\n 259        1  104856576 nvme0n1p1\n"
        "   7        0      65536 loop0\n 253        0   52426752 dm-0\n"
    ) == [("sda", 50.0), ("nvme0n1", 100.0)]


# --- Hyper-V -------------------------------------------------------------------

_WIN_ID = "6f9c2d1e-0a4b-4c3d-9e8f-1a2b3c4d5e6f"
_LNX_ID = "0b1c2d3e-4f50-4617-8283-94a5b6c7d8e9"
_DUP_IDS = ("aaaaaaaa-0000-4000-8000-000000000001", "aaaaaaaa-0000-4000-8000-000000000002")
_GIB = 1024**3


def _hyperv_children() -> FakeChildren:
    vms = [
        {
            "Name": "app-win-01",
            "Id": _WIN_ID,
            "State": "Running",
            "Generation": 2,
            "ProcessorCount": 4,
            "MemoryAssigned": 6 * _GIB,
        },
        {
            "Name": "db-lnx-01",
            "Id": _LNX_ID,
            "State": "Off",
            "Generation": 1,
            "ProcessorCount": 2,
            "MemoryAssigned": 0,
        },
        {"Name": "dup", "Id": _DUP_IDS[0], "State": "Off", "Generation": 2, "ProcessorCount": 1},
        {"Name": "dup", "Id": _DUP_IDS[1], "State": "Off", "Generation": 2, "ProcessorCount": 1},
    ]
    guests = [
        {
            "Id": _WIN_ID.upper(),
            "BIOSGUID": "{1B2C3D4E-5F60-4718-8293-A4B5C6D7E8F9}",
            "OSName": "Windows Server 2022 Datacenter",
            "OSVersion": "10.0.20348",
        },
        {"Id": _LNX_ID, "BIOSGUID": "{0F1E2D3C-4B5A-4978-8695-A4B3C2D1E0F9}"},
    ]
    win_disks = [
        {"Path": "C:\\VMs\\app-win-01\\os.vhdx", "SupportPersistentReservations": False},
        {"Path": "C:\\ClusterStorage\\data.vhdx", "SupportPersistentReservations": True},
    ]
    return FakeChildren(
        {
            _k("hyperv.vms.list"): {"rows": vms, "total": 4},
            _k("hyperv.vms.guest.list"): {"rows": guests, "total": 2},
            _k("hyperv.vms.config", vm_name="app-win-01"): {
                "ProcessorCount": 4,
                "MemoryStartup": 4 * _GIB,
                "DynamicMemoryEnabled": True,
                "SecureBoot": "On",
            },
            _k("hyperv.vms.config", vm_name="db-lnx-01"): {
                "ProcessorCount": 2,
                "MemoryStartup": 2 * _GIB,
                "SecureBoot": None,
            },
            _k("hyperv.disks.vm.list", vm_name="app-win-01"): {"rows": win_disks, "total": 2},
            _k("hyperv.disks.vm.list", vm_name="db-lnx-01"): {
                "rows": [{"Path": None, "DiskNumber": 3}],
                "total": 1,
            },
            _k("hyperv.disks.vhd.get", path="C:\\VMs\\app-win-01\\os.vhdx"): {
                "Size": 127 * _GIB,
                "FileSize": 40 * _GIB,
                "VhdType": "Dynamic",
            },
            _k("hyperv.checkpoints.list", vm_name="app-win-01"): {"rows": [{}, {}], "total": 2},
            _k("hyperv.checkpoints.list", vm_name="db-lnx-01"): {"rows": [], "total": 0},
        }
    )


async def test_hyperv_inventory(store: ResultHandleStore) -> None:
    children = _hyperv_children()
    answer, envelope = await _run(hyperv_migration_inventory, children, "hv-node-1", store)

    assert answer["source_totals"] == {"server": 4}
    assert answer["record_counts"]["server"] == 4
    assert answer["counts_match"] is True
    assert all(call[0] == "hyperv-ssh-2022.x" for call in children.calls)
    # A name two VMs share is never used for the per-VM reads.
    assert not any(c[2].get("vm_name") == "dup" for c in children.calls)
    whats = [o.what for o in envelope.omissions]
    assert whats.count("details of VM dup") == 2
    assert "some details of VM app-win-01" in whats  # the second disk's vhd read failed

    records = {r.source_id: r for r in envelope.records}
    win = records[f"hyperv:{_WIN_ID}"]
    assert isinstance(win, ServerRecord)
    assert (win.cores, win.memory_configured_mb, win.memory_assigned_mb) == (4, 4096, 6144)
    assert (win.boot_type, win.secure_boot, win.power_state) == ("uefi", True, "on")
    assert win.identity_hints.bios_uuid == "1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9"
    assert win.os is not None and win.os.family == "windows"
    assert win.snapshot_count == 2
    assert win.disks is not None
    assert (win.disks[0].size_gb, win.disks[0].used_gb, win.disks[0].thin) == (127, 40, True)
    assert win.disks[1].shared is True and win.missing["disks.1"] == "collection_failed"
    # A checkpoint always takes every virtual hard disk, so no disk is independent.
    assert [d.independent for d in win.disks] == [False, False]

    lnx = records[f"hyperv:{_LNX_ID}"]
    assert isinstance(lnx, ServerRecord)
    assert (lnx.boot_type, lnx.secure_boot, lnx.power_state) == ("bios", False, "off")
    assert lnx.memory_assigned_mb is None and lnx.missing["memory_assigned_mb"] == "powered_off"
    assert lnx.os is None and lnx.missing["os"] == "powered_off"
    assert lnx.disks is not None and lnx.disks[0].raw_mapping is True
    assert lnx.disks[0].independent is False

    dup = records[f"hyperv:{_DUP_IDS[0]}"]
    assert isinstance(dup, ServerRecord)
    assert dup.disks is None and dup.missing["disks"] == "not_supported"

    export = inventory_to_csv(envelope.records, [])
    assert export.row_count == 1  # only the Windows VM has an OS name
    assert "app-win-01" in export.csv_lines[1]


async def test_hyperv_inventory_without_guest_data(store: ResultHandleStore) -> None:
    children = _hyperv_children()
    children.answers[_k("hyperv.vms.guest.list")] = "denied"
    answer, envelope = await _run(hyperv_migration_inventory, children, "hv-node-1", store)

    assert answer["counts_match"] is True
    assert envelope.omissions[0].reason == "no_permission"
    win = next(r for r in envelope.records if r.source_id == f"hyperv:{_WIN_ID}")
    assert isinstance(win, ServerRecord)
    assert win.os is None and win.identity_hints.bios_uuid is None


async def test_hyperv_vms_with_a_placeholder_bios_uuid_stay_separate(
    store: ResultHandleStore,
) -> None:
    children = _hyperv_children()
    children.answers[_k("hyperv.vms.guest.list")] = {
        "rows": [
            {"Id": _WIN_ID, "BIOSGUID": "{" + _PLACEHOLDER_UUID.upper() + "}", "OSName": "W"},
            {"Id": _LNX_ID, "BIOSGUID": _PLACEHOLDER_UUID, "OSName": "L"},
        ],
        "total": 2,
    }
    _answer, envelope = await _run(hyperv_migration_inventory, children, "hv-node-1", store)

    records = {r.source_id: r for r in envelope.records}
    for vm_id in (_WIN_ID, _LNX_ID):
        record = records[f"hyperv:{vm_id}"]
        assert isinstance(record, ServerRecord)
        assert record.identity_hints.bios_uuid is None
        assert record.missing["identity_hints.bios_uuid"] == "not_reported_by_source"
    _records, duplicates = merge_inventories([envelope])
    assert duplicates == []


# --- Proxmox ---------------------------------------------------------------------


def _proxmox_children() -> FakeChildren:
    get = "proxmox.api.get"
    return FakeChildren(
        {
            _k(get, path="nodes"): {
                "data": [
                    {"node": "pve1", "status": "online"},
                    {"node": "pve2", "status": "offline"},
                ]
            },
            _k(get, path="nodes/pve1/qemu"): {
                "data": [
                    {"vmid": 100, "name": "web-01", "status": "running"},
                    {"vmid": 101, "name": "batch-01", "status": "stopped"},
                    {"vmid": 9000, "name": "tmpl", "status": "stopped", "template": 1},
                ]
            },
            _k(get, path="nodes/pve1/qemu/100/config"): {
                "data": {
                    "cores": 2,
                    "sockets": 2,
                    "memory": "8192",
                    "bios": "ovmf",
                    "efidisk0": "local-lvm:vm-100-disk-1,efitype=4m,pre-enrolled-keys=1,size=4M",
                    "scsi0": "local-lvm:vm-100-disk-0,iothread=1,size=32G",
                    "scsi1": "/dev/disk/by-id/ata-DISK1,size=1T",
                    "ide2": "local:iso/install.iso,media=cdrom,size=1G",
                    "net0": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0",
                    "smbios1": "uuid=5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f",
                    "ostype": "l26",
                    "agent": "enabled=1",
                }
            },
            _k(get, path="nodes/pve1/qemu/100/snapshot"): {
                "data": [{"name": "before-upgrade"}, {"name": "current"}]
            },
            _k(get, path="nodes/pve1/qemu/100/agent/get-osinfo"): {
                "data": {
                    "result": {
                        "pretty-name": "Debian GNU/Linux 12 (bookworm)",
                        "version-id": "12",
                        "machine": "x86_64",
                    }
                }
            },
            _k(get, path="nodes/pve1/qemu/101/config"): {
                "data": {
                    "memory": "current=2048",
                    "ostype": "win11",
                    "sata0": "local:101/a.qcow2,size=64G",
                }
            },
            _k(get, path="nodes/pve1/qemu/101/snapshot"): "error",
        }
    )


async def test_proxmox_inventory(store: ResultHandleStore) -> None:
    answer, envelope = await _run(proxmox_migration_inventory, _proxmox_children(), "pve-a", store)

    # One node is offline, so the source's own VM count is unknown.
    assert answer["source_totals"] == {}
    assert answer["left_out_items"] == {"server": 1}
    assert answer["counts_match"] is False
    whats = {o.what: o.reason for o in envelope.omissions}
    assert whats["VM template 9000"] == "not_supported"
    assert whats["VMs on node pve2"] == "collection_failed"
    assert whats["some details of VM 101"] == "collection_failed"

    records = {r.source_id: r for r in envelope.records}
    web = records["proxmox:pve-a:100"]
    assert isinstance(web, ServerRecord)
    assert (web.cores, web.memory_configured_mb, web.boot_type, web.secure_boot) == (
        4,
        8192,
        "uefi",
        True,
    )
    assert web.disks is not None and [d.label for d in web.disks] == ["scsi0", "scsi1"]
    assert (web.disks[0].size_gb, web.disks[1].raw_mapping) == (32, True)
    # A Proxmox snapshot always takes every disk, so no disk is independent.
    assert [d.independent for d in web.disks] == [False, False]
    assert web.snapshot_count == 1
    assert web.identity_hints.bios_uuid == "5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f"
    assert web.identity_hints.mac_addresses == ["bc:24:11:aa:bb:cc"]
    assert web.os is not None and web.os.name == "Debian GNU/Linux 12 (bookworm)"

    batch = records["proxmox:pve-a:101"]
    assert isinstance(batch, ServerRecord)
    assert (batch.cores, batch.memory_configured_mb, batch.boot_type) == (1, 2048, "bios")
    assert batch.os is not None and batch.os.family == "windows" and batch.os.name is None
    assert batch.missing["os.name"] == "powered_off"
    assert batch.snapshot_count is None and batch.missing["snapshot_count"] == "collection_failed"


async def test_proxmox_counts_match_only_when_every_node_was_read(
    store: ResultHandleStore,
) -> None:
    children = _proxmox_children()
    get = "proxmox.api.get"
    children.answers[_k(get, path="nodes")] = {
        "data": [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"}]
    }
    children.answers[_k(get, path="nodes/pve2/qemu")] = {"data": []}
    answer, _envelope = await _run(proxmox_migration_inventory, children, "pve-a", store)
    assert answer["source_totals"] == {"server": 3} and answer["counts_match"] is True

    # The VM list of one node is denied: the total is not the source's own.
    children.answers[_k(get, path="nodes/pve2/qemu")] = "denied"
    answer, envelope = await _run(proxmox_migration_inventory, children, "pve-a", store)
    assert answer["source_totals"] == {} and answer["counts_match"] is False
    assert {o.what: o.reason for o in envelope.omissions}["VMs on node pve2"] == "no_permission"


async def test_proxmox_vm_with_a_placeholder_smbios_uuid(store: ResultHandleStore) -> None:
    children = _proxmox_children()
    config = children.answers[_k("proxmox.api.get", path="nodes/pve1/qemu/100/config")]
    assert isinstance(config, dict)
    config["data"]["smbios1"] = f"uuid={_PLACEHOLDER_UUID}"
    _answer, envelope = await _run(proxmox_migration_inventory, children, "pve-a", store)

    web = next(r for r in envelope.records if r.source_id == "proxmox:pve-a:100")
    assert isinstance(web, ServerRecord)
    assert web.identity_hints.bios_uuid is None
    assert web.missing["identity_hints.bios_uuid"] == "not_reported_by_source"


# --- Linux and Windows hosts --------------------------------------------------------


def _file(content: str, *, exists: bool = True) -> dict[str, Any]:
    return {"content": content, "exists": exists, "truncated": False}


def _linux_children() -> FakeChildren:
    read = "linux.file.read"
    return FakeChildren(
        {
            _k("linux.about"): {
                "hostname": "files-01",
                "os_pretty": "Ubuntu 22.04.4 LTS",
                "version": "22.04",
            },
            _k(read, path="/sys/devices/system/cpu/online"): _file("0-7\n"),
            _k(read, path="/proc/meminfo"): _file("MemTotal:       16384000 kB\nMemFree: 1 kB\n"),
            _k(read, path="/proc/partitions"): _file(
                "major minor  #blocks  name\n   8 0 104857600 sda\n   8 1 104856576 sda1\n"
            ),
            _k(read, path="/sys/firmware/efi"): _file("", exists=True),
            _k(read, path="/sys/class/dmi/id/product_uuid"): _file(""),
            _k(read, path="/sys/class/dmi/id/sys_vendor"): _file("Dell Inc.\n"),
            _k(read, path="/sys/class/dmi/id/product_name"): _file("PowerEdge R650\n"),
            _k(read, path="/etc/machine-id"): _file("0f1e2d3c4b5a69788796a5b4c3d2e1f0\n"),
        }
    )


async def test_linux_host_inventory(store: ResultHandleStore) -> None:
    answer, envelope = await _run(linux_migration_inventory, _linux_children(), "files-01", store)

    assert answer["counts_match"] is True and answer["omission_count"] == 0
    (record,) = envelope.records
    assert isinstance(record, ServerRecord)
    assert record.source_id == "linux:0f1e2d3c4b5a69788796a5b4c3d2e1f0"
    assert (record.cores, record.memory_configured_mb, record.boot_type) == (8, 16000, "uefi")
    assert record.secure_boot is None and record.missing["secure_boot"] == "not_supported"
    assert record.disks is not None and [(d.label, d.size_gb) for d in record.disks] == [
        ("sda", 100.0)
    ]
    assert record.identity_hints.bios_uuid is None
    assert record.missing["identity_hints.bios_uuid"] == "no_permission"
    assert record.server_type is None  # a Dell server is not a known hypervisor
    assert record.os is not None and record.os.family == "linux"
    assert inventory_to_csv(envelope.records, []).row_count == 1


async def test_linux_host_on_a_hypervisor_with_root(store: ResultHandleStore) -> None:
    children = _linux_children()
    read = "linux.file.read"
    children.answers[_k(read, path="/sys/class/dmi/id/product_uuid")] = _file(
        "1B2C3D4E-5F60-4718-8293-A4B5C6D7E8F9\n"
    )
    children.answers[_k(read, path="/sys/class/dmi/id/sys_vendor")] = _file("VMware, Inc.\n")
    children.answers[_k(read, path="/sys/firmware/efi")] = _file("", exists=False)
    _answer, envelope = await _run(linux_migration_inventory, children, "files-01", store)

    (record,) = envelope.records
    assert isinstance(record, ServerRecord)
    assert record.identity_hints.bios_uuid == "1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9"
    assert (record.server_type, record.hypervisor, record.boot_type) == (
        "virtual",
        "VMware",
        "bios",
    )
    assert record.secure_boot is False and "secure_boot" not in record.missing


def _windows_children() -> FakeChildren:
    return FakeChildren(
        {
            _k("winsrv.system.os-info"): {
                "Hostname": "SQL-01",
                "Caption": "Microsoft Windows Server 2019 Standard",
                "Version": "10.0.17763",
                "OSArchitecture": "64-bit",
            },
            _k("winsrv.system.hardware"): {
                "Hostname": "SQL-01",
                "Manufacturer": "Microsoft Corporation",
                "Model": "Virtual Machine",
                "LogicalProcessors": 8,
                "TotalPhysicalMemory": 32 * _GIB,
                "FirmwareType": "Uefi",
                "SecureBoot": True,
                "ProductUuid": "0F1E2D3C-4B5A-4978-8695-A4B3C2D1E0F9",
            },
            _k("winsrv.storage.disk.list"): {
                "rows": [
                    {"Number": 0, "FriendlyName": "Msft Virtual Disk", "Size": 127 * _GIB},
                    {"Number": 1, "FriendlyName": "Msft Virtual Disk", "Size": 512 * _GIB},
                ],
                "total": 2,
            },
        }
    )


async def test_windows_host_inventory(store: ResultHandleStore) -> None:
    _answer, envelope = await _run(winsrv_migration_inventory, _windows_children(), "sql-01", store)

    (record,) = envelope.records
    assert isinstance(record, ServerRecord)
    assert record.source_id == "winsrv:0f1e2d3c-4b5a-4978-8695-a4b3c2d1e0f9"
    assert (record.cores, record.memory_configured_mb) == (8, 32768)
    assert (record.boot_type, record.secure_boot) == ("uefi", True)
    assert (record.server_type, record.hypervisor) == ("virtual", "Hyper-V")
    assert record.disks is not None and [d.size_gb for d in record.disks] == [127, 512]
    assert record.os is not None and record.os.family == "windows"
    assert record.identity_hints.host_name == "sql-01"


async def test_windows_host_when_the_hardware_read_fails(store: ResultHandleStore) -> None:
    children = _windows_children()
    children.answers[_k("winsrv.system.hardware")] = "error"
    answer, envelope = await _run(winsrv_migration_inventory, children, "sql-01", store)

    assert answer["omissions"][0]["reason"] == "collection_failed"
    (record,) = envelope.records
    assert isinstance(record, ServerRecord)
    assert record.source_id == "winsrv:sql-01"
    assert record.cores is None and record.missing["cores"] == "collection_failed"


@pytest.mark.parametrize(
    ("firmware", "secure_boot", "reason"),
    [
        ("Bios", False, None),
        ("Uefi", None, "not_reported_by_source"),
    ],
)
async def test_windows_host_secure_boot_when_the_read_gives_no_value(
    store: ResultHandleStore, firmware: str, secure_boot: bool | None, reason: str | None
) -> None:
    """BIOS firmware cannot have Secure Boot, so it is false; on UEFI the state is unknown."""
    children = _windows_children()
    hardware = children.answers[_k("winsrv.system.hardware")]
    assert isinstance(hardware, dict)
    hardware["FirmwareType"] = firmware
    hardware["SecureBoot"] = None
    _answer, envelope = await _run(winsrv_migration_inventory, children, "sql-01", store)

    (record,) = envelope.records
    assert isinstance(record, ServerRecord)
    assert record.boot_type == firmware.lower()
    assert record.secure_boot is secure_boot
    assert record.missing.get("secure_boot") == reason


async def test_hosts_with_a_placeholder_uuid_stay_separate(store: ResultHandleStore) -> None:
    """Two hosts whose firmware reports a placeholder UUID are two machines."""
    linux = _linux_children()
    linux.answers[_k("linux.file.read", path="/sys/class/dmi/id/product_uuid")] = _file(
        _PLACEHOLDER_UUID.upper() + "\n"
    )
    _answer, linux_envelope = await _run(linux_migration_inventory, linux, "files-01", store)
    (linux_record,) = linux_envelope.records
    assert isinstance(linux_record, ServerRecord)
    assert linux_record.identity_hints.bios_uuid is None
    assert linux_record.missing["identity_hints.bios_uuid"] == "not_reported_by_source"
    assert linux_record.source_id == "linux:0f1e2d3c4b5a69788796a5b4c3d2e1f0"

    envelopes = [linux_envelope]
    for host in ("sql-01", "sql-02"):
        windows = _windows_children()
        hardware = windows.answers[_k("winsrv.system.hardware")]
        assert isinstance(hardware, dict)
        hardware["ProductUuid"] = "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF"
        os_info = windows.answers[_k("winsrv.system.os-info")]
        assert isinstance(os_info, dict)
        os_info["Hostname"] = hardware["Hostname"] = host.upper()
        _answer, envelope = await _run(winsrv_migration_inventory, windows, host, store)
        (record,) = envelope.records
        assert isinstance(record, ServerRecord)
        assert record.identity_hints.bios_uuid is None
        assert record.source_id == f"winsrv:{host}"
        envelopes.append(envelope)

    records, duplicates = merge_inventories(envelopes)
    assert len(records) == 3 and duplicates == []


# --- Kubernetes ----------------------------------------------------------------------

_NODE_UUID = "1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9"


def _deployment_page(params: dict[str, Any]) -> dict[str, Any]:
    if "continue_token" not in params:
        row = {
            "kind": "Deployment",
            "name": "web",
            "namespace": "shop",
            "replicas": 3,
            "containers": [
                {
                    "name": "app",
                    "image": "registry.example/web:2.1",
                    "requests": {"cpu": "250m", "memory": "256Mi"},
                    "limits": {"memory": "512Mi"},
                },
                {
                    "name": "proxy",
                    "image": "registry.example/proxy:1.0",
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                    "limits": {},
                },
            ],
            "volumes": [{"name": "cache", "type": "emptyDir", "claim_name": None}],
            "volume_claim_templates": [],
            "pod_selector": "app=web",
        }
        return {"kind": "Deployment", "rows": [row], "total": 1, "next_continue": "page-2"}
    row = {
        "kind": "Deployment",
        "name": "api",
        "namespace": "shop",
        "replicas": 2,
        "containers": [
            {"name": "api", "image": "registry.example/api:3", "requests": {}, "limits": {}}
        ],
        "volumes": [{"name": "data", "type": "persistentVolumeClaim", "claim_name": "api-data"}],
        "volume_claim_templates": [],
        "pod_selector": "app=api",
    }
    return {"kind": "Deployment", "rows": [row], "total": 1}


def _k8s_children() -> FakeChildren:
    listing = "k8s.workload.list"
    statefulset = {
        "kind": "StatefulSet",
        "name": "db",
        "namespace": "shop",
        "replicas": 1,
        "containers": [
            {
                "name": "pg",
                "image": "registry.example/pg:16",
                "requests": {"cpu": "1", "memory": "2Gi"},
                "limits": {"cpu": "2"},
            }
        ],
        "volumes": [],
        "volume_claim_templates": [
            {"name": "pgdata", "storage_request": "20Gi", "storage_class": "fast"}
        ],
        "pod_selector": "app=db",
    }
    pods = "k8s.pod.list"
    return FakeChildren(
        {
            _k("k8s.node.list"): {
                "rows": [
                    {
                        "name": "worker-1",
                        "status": "Ready",
                        "os": "linux",
                        "os_image": "Ubuntu 22.04.4 LTS",
                        "architecture": "amd64",
                        "system_uuid": _NODE_UUID.upper(),
                        "cpu_capacity": "4",
                        "memory_capacity": "16393216Ki",
                    },
                    {
                        "name": "worker-2",
                        "status": "NotReady",
                        "os": "linux",
                        "system_uuid": None,
                        "cpu_capacity": None,
                        "memory_capacity": None,
                    },
                ],
                "total": 2,
            },
            _k(listing, kind="Deployment", all_namespaces=True, limit=500): _deployment_page,
            _k(
                listing, kind="Deployment", all_namespaces=True, limit=500, continue_token="page-2"
            ): (_deployment_page),
            _k(listing, kind="StatefulSet", all_namespaces=True, limit=500): {
                "kind": "StatefulSet",
                "rows": [statefulset],
                "total": 1,
            },
            _k(listing, kind="DaemonSet", all_namespaces=True, limit=500): {
                "kind": "DaemonSet",
                "rows": [],
                "total": 0,
            },
            _k("k8s.persistentvolumeclaim.list", all_namespaces=True): {
                "rows": [
                    {
                        "name": "api-data",
                        "namespace": "shop",
                        "capacity": "10Gi",
                        "storage_class": "standard",
                    }
                ],
                "total": 1,
            },
            _k(pods, namespace="shop", label_selector="app=web", limit=500): {
                "rows": [{"node": "worker-1"}, {"node": "worker-2"}, {"node": "worker-1"}],
            },
            _k(pods, namespace="shop", label_selector="app=api", limit=500): {"rows": []},
            _k(pods, namespace="shop", label_selector="app=db", limit=500): "denied",
        }
    )


async def test_k8s_inventory(store: ResultHandleStore) -> None:
    answer, envelope = await _run(k8s_migration_inventory, _k8s_children(), "prod-cluster", store)

    assert answer["source_totals"] == {"server": 2, "kubernetes_workload": 3}
    assert answer["record_counts"] == {"server": 2, "application": 0, "kubernetes_workload": 3}
    assert answer["counts_match"] is True
    records = {r.source_id: r for r in envelope.records}

    node = records["k8s:prod-cluster:node:worker-1"]
    assert isinstance(node, ServerRecord)
    assert node.identity_hints.bios_uuid == _NODE_UUID
    assert (node.cores, node.memory_configured_mb) == (4, 16009)
    assert node.os is not None and node.os.name == "Ubuntu 22.04.4 LTS"

    web = records["k8s:prod-cluster:shop:Deployment:web"]
    assert isinstance(web, KubernetesWorkloadRecord)
    assert (web.replicas, web.cpu_request_millicores, web.memory_request_mb) == (3, 300, 320)
    assert web.memory_limit_mb is None  # the proxy container sets no memory limit
    assert web.nodes == ["k8s:prod-cluster:node:worker-1", "k8s:prod-cluster:node:worker-2"]
    assert web.images == ["registry.example/proxy:1.0", "registry.example/web:2.1"]

    api = records["k8s:prod-cluster:shop:Deployment:api"]
    assert isinstance(api, KubernetesWorkloadRecord)
    assert api.volumes is not None and (api.volumes[0].size_gb, api.volumes[0].storage_class) == (
        10,
        "standard",
    )
    assert api.cpu_request_millicores is None and api.nodes == []

    db = records["k8s:prod-cluster:shop:StatefulSet:db"]
    assert isinstance(db, KubernetesWorkloadRecord)
    assert db.volumes is not None and db.volumes[0].type == "volumeClaimTemplate"
    assert db.volumes[0].size_gb == 20
    assert db.nodes is None and db.missing["nodes"] == "no_permission"
    assert [o.what for o in envelope.omissions] == ["pods of StatefulSet shop/db"]


async def test_k8s_node_matches_the_vm_record_of_the_same_machine(
    store: ResultHandleStore,
) -> None:
    _answer, k8s = await _run(k8s_migration_inventory, _k8s_children(), "prod-cluster", store)
    _answer, hyperv = await _run(hyperv_migration_inventory, _hyperv_children(), "hv-1", store)

    _records, duplicates = merge_inventories([hyperv, k8s])

    assert [(d.source_id, d.same_as, d.match) for d in duplicates] == [
        ("k8s:prod-cluster:node:worker-1", f"hyperv:{_WIN_ID}", "bios_uuid")
    ]


async def test_k8s_node_matches_its_vmware_vm_when_the_uuid_bytes_are_reversed(
    store: ResultHandleStore,
) -> None:
    """A VMware VM record keeps vCenter's UUID; the node reports the guest's form."""
    _answer, k8s = await _run(k8s_migration_inventory, _k8s_children(), "prod-cluster", store)
    node = next(r for r in k8s.records if r.source_id == "k8s:prod-cluster:node:worker-1")
    assert isinstance(node, ServerRecord) and node.identity_hints.bios_uuid == _NODE_UUID
    as_vcenter = "4e3d2c1b-605f-1847-8293-a4b5c6d7e8f9"
    vm = adapter_kit.record_with_reasons(
        ServerRecord,
        {
            "source_id": "vmware:vc-1:vm-7",
            "collected_at": k8s.source.collected_at,
            "identity_hints": {"bios_uuid": as_vcenter, "host_name": "worker-1"},
            "name": "worker-1",
            "server_type": "virtual",
            "hypervisor": "VMware",
            "cores": 4,
            "memory_configured_mb": 16384,
            "power_state": "on",
        },
    )
    vmware = InventoryEnvelope.model_validate(
        {
            "schema_version": "1",
            "source": {
                "connector_id": "vmware-rest-9.0",
                "target": "vc-1",
                "collected_at": k8s.source.collected_at,
            },
            "records": [vm.model_dump(mode="json")],
            "counts": {"server": 1, "application": 0, "kubernetes_workload": 0},
        }
    )

    _records, duplicates = merge_inventories([vmware, k8s])

    assert [(d.source_id, d.same_as, d.match) for d in duplicates] == [
        ("k8s:prod-cluster:node:worker-1", "vmware:vc-1:vm-7", "bios_uuid")
    ]


async def test_k8s_inventory_when_a_kind_cannot_be_listed(store: ResultHandleStore) -> None:
    children = _k8s_children()
    children.answers[_k("k8s.workload.list", kind="DaemonSet", all_namespaces=True, limit=500)] = (
        "denied"
    )
    answer, _envelope = await _run(k8s_migration_inventory, children, "prod-cluster", store)

    assert answer["source_totals"] == {"server": 2}
    assert answer["counts_match"] is False
    assert answer["omissions"][0] == {
        "what": "all DaemonSet workloads",
        "reason": "no_permission",
        "detail": "k8s.workload.list",
    }


async def test_k8s_counts_do_not_match_when_nothing_could_be_listed(
    store: ResultHandleStore,
) -> None:
    """An inventory with no source total at all is not a checked inventory."""
    children = FakeChildren({_k("k8s.node.list"): "denied"})
    answer, envelope = await _run(k8s_migration_inventory, children, "prod-cluster", store)

    assert envelope.records == []
    assert answer["source_totals"] == {} and answer["counts_match"] is False
    assert [o.what for o in envelope.omissions] == [
        "cluster nodes",
        "all Deployment workloads",
        "all StatefulSet workloads",
        "all DaemonSet workloads",
        "persistent volume claim sizes",
    ]


async def test_k8s_second_page_failure_keeps_the_first_page(store: ResultHandleStore) -> None:
    children = _k8s_children()
    children.answers[
        _k(
            "k8s.workload.list",
            kind="Deployment",
            all_namespaces=True,
            limit=500,
            continue_token="page-2",
        )
    ] = "error"
    answer, envelope = await _run(k8s_migration_inventory, children, "prod-cluster", store)

    ids = {r.source_id for r in envelope.records}
    assert "k8s:prod-cluster:shop:Deployment:web" in ids  # from the first page
    assert "k8s:prod-cluster:shop:Deployment:api" not in ids  # was on the failed page
    assert answer["record_counts"]["kubernetes_workload"] == 2
    assert "kubernetes_workload" not in answer["source_totals"]
    assert answer["counts_match"] is False
    assert answer["omissions"][0] == {
        "what": "Deployment workloads after the first 500",
        "reason": "collection_failed",
        "detail": "k8s.workload.list",
    }


async def test_k8s_pod_placement_follows_the_pages(store: ResultHandleStore) -> None:
    children = _k8s_children()
    pods = "k8s.pod.list"
    children.answers[_k(pods, namespace="shop", label_selector="app=web", limit=500)] = {
        "rows": [{"node": "worker-1"}],
        "next_continue": "more",
    }
    children.answers[
        _k(pods, namespace="shop", label_selector="app=web", limit=500, continue_token="more")
    ] = {"rows": [{"node": "worker-3"}]}
    children.answers[_k(pods, namespace="shop", label_selector="app=api", limit=500)] = {
        "rows": [{"node": "worker-2"}],
        "next_continue": "more",
    }
    children.answers[
        _k(pods, namespace="shop", label_selector="app=api", limit=500, continue_token="more")
    ] = "error"
    _answer, envelope = await _run(k8s_migration_inventory, children, "prod-cluster", store)

    records = {r.source_id: r for r in envelope.records}
    web = records["k8s:prod-cluster:shop:Deployment:web"]
    assert isinstance(web, KubernetesWorkloadRecord)
    # worker-3 appears only on the second page of pods.
    assert web.nodes == ["k8s:prod-cluster:node:worker-1", "k8s:prod-cluster:node:worker-3"]
    api = records["k8s:prod-cluster:shop:Deployment:api"]
    assert isinstance(api, KubernetesWorkloadRecord)
    # The first page of pods is kept when the second one fails, and the omission says so.
    assert api.nodes == ["k8s:prod-cluster:node:worker-2"] and "nodes" not in api.missing
    omissions = {o.what: o for o in envelope.omissions}
    assert omissions["pods of Deployment shop/api"].reason == "collection_failed"
    assert omissions["pods of Deployment shop/api"].detail == (
        "pods after the first 500 were not read"
    )


async def test_k8s_nodes_with_placeholder_or_shared_uuids_stay_separate(
    store: ResultHandleStore,
) -> None:
    children = _k8s_children()
    shared = "5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f"
    nodes = children.answers[_k("k8s.node.list")]
    assert isinstance(nodes, dict)
    nodes["rows"] = [
        {"name": "zero-1", "status": "Ready", "os": "linux", "system_uuid": _PLACEHOLDER_UUID},
        {"name": "zero-2", "status": "Ready", "os": "linux", "system_uuid": _PLACEHOLDER_UUID},
        {"name": "twin-1", "status": "Ready", "os": "linux", "system_uuid": shared},
        {"name": "twin-2", "status": "Ready", "os": "linux", "system_uuid": shared.upper()},
        {"name": "worker-1", "status": "Ready", "os": "linux", "system_uuid": _NODE_UUID},
    ]
    nodes["total"] = 5
    answer, envelope = await _run(k8s_migration_inventory, children, "prod-cluster", store)

    assert answer["record_counts"]["server"] == 5 and answer["counts_match"] is True
    records = {r.source_id: r for r in envelope.records if isinstance(r, ServerRecord)}
    assert len(records) == 5
    for name in ("zero-1", "zero-2", "twin-1", "twin-2"):
        node = records[f"k8s:prod-cluster:node:{name}"]
        assert node.identity_hints.bios_uuid is None
        assert node.missing["identity_hints.bios_uuid"] == "not_reported_by_source"
    assert records["k8s:prod-cluster:node:worker-1"].identity_hints.bios_uuid == _NODE_UUID
    assert envelope.omissions[0].what == "BIOS UUID of nodes twin-1, twin-2"
    assert envelope.omissions[0].reason == "not_reported_by_source"

    _records, duplicates = merge_inventories([envelope])
    assert duplicates == []
