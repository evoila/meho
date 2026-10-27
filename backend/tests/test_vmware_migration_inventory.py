# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fixture tests for ``vmware.migration.inventory``.

A fake vCenter answers the VI-JSON reads with made-up VMs:

* ``app-win-01`` -- Windows, EFI with secure boot, three disks (thin, shared
  and independent, raw mapping), two snapshots, measured utilization;
* ``db-lin-01`` -- Linux, BIOS, running, but vCenter has no samples;
* ``db-lin-01-copy`` -- registered from copied files: the same BIOS UUID,
  powered off;
* ``tmpl-base`` -- a template, left out and listed;
* ``broken-01`` -- every read of it fails, so it is listed as an omission.

vCenter counts five VMs; three records plus two omissions match that count.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.migration.azure_migrate_csv import inventory_to_csv
from meho_backplane.connectors.migration.contracts import (
    CountCheck,
    InventoryEnvelope,
)
from meho_backplane.connectors.migration.inventory_load import load_inventories
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.vmware_rest.composites import _migration_inventory
from meho_backplane.connectors.vmware_rest.composites._migration_inventory import (
    migration_inventory_composite,
)
from meho_backplane.connectors.vmware_rest.composites._migration_inventory_map import (
    nearest_rank_stat,
)
from meho_backplane.connectors.vmware_rest.composites._migration_inventory_usage import (
    interval_for_days,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    _WHEN_TO_USE_BY_GROUP,
    COMPOSITES_NOT_ON_8_0,
)
from meho_backplane.delete_names import write_word_in_op_id
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

_VC_UUID = "6b1f0c2a-0000-4000-8000-00000000a001"
_WIN_INSTANCE = "50010000-0000-4000-8000-0000000000a1"
_LIN_INSTANCE = "50010000-0000-4000-8000-0000000000b1"
_COPY_INSTANCE = "50010000-0000-4000-8000-0000000000c1"
_LIN_BIOS = "42010000-0000-4000-8000-0000000000b0"
_GIB = 1024**3


class _Target:
    name = "vc-fixture"


class _MemoryValkey:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(tenant_id: UUID | None = None) -> Operator:
    return Operator(
        sub="operator:fixture",
        raw_jwt="fake.jwt.value",
        tenant_id=tenant_id or uuid4(),
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


def _moref(mo_type: str, moid: str) -> dict[str, str]:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


def _disk(key: int, label: str, size_gib: int, backing: dict[str, Any]) -> dict[str, Any]:
    return {
        "_typeName": "VirtualDisk",
        "key": key,
        "deviceInfo": {"label": label},
        "capacityInBytes": size_gib * _GIB,
        "backing": backing,
    }


def _nic(mac: str) -> dict[str, Any]:
    return {
        "_typeName": "VirtualVmxnet3",
        "key": 4000,
        "macAddress": mac,
        "addressType": "assigned",
        "backing": {
            "_typeName": "VirtualEthernetCardNetworkBackingInfo",
            "deviceName": "VM Network",
        },
    }


def _flat(*, thin: bool, mode: str = "persistent", sharing: str = "sharingNone") -> dict[str, Any]:
    backing = {"_typeName": "VirtualDiskFlatVer2BackingInfo", "diskMode": mode, "sharing": sharing}
    if thin:
        backing["thinProvisioned"] = True
    return backing


_VMS: dict[str, dict[str, Any]] = {
    "vm-101": {
        "name": "app-win-01",
        "config.template": False,
        "config.uuid": "4201AAAA-0000-4000-8000-0000000000A0",
        "config.instanceUuid": _WIN_INSTANCE,
        "config.guestFullName": "Microsoft Windows Server 2022 (64-bit)",
        "config.guestId": "windows2019srvNext_64Guest",
        "config.firmware": "efi",
        "config.bootOptions.efiSecureBootEnabled": True,
        "config.hardware.numCPU": 4,
        "config.hardware.memoryMB": 16384,
        "config.hardware.device": [
            _disk(2000, "Hard disk 1", 80, _flat(thin=True)),
            _disk(
                2001,
                "Hard disk 2",
                200,
                _flat(thin=False, mode="independent_persistent", sharing="sharingMultiWriter"),
            ),
            _disk(
                2002,
                "Hard disk 3",
                500,
                {
                    "_typeName": "VirtualDiskRawDiskMappingVer1BackingInfo",
                    "diskMode": "persistent",
                    "sharing": "sharingNone",
                },
            ),
            _nic("00:50:56:AA:00:01"),
            {"_typeName": "VirtualCdrom", "key": 3000},
        ],
        "guest.guestFullName": "Microsoft Windows Server 2022 (64-bit)",
        "guest.guestFamily": "windowsGuest",
        "guest.hostName": "app-win-01.example.test",
        "guest.net": [
            {
                "macAddress": "00:50:56:aa:00:01",
                "ipAddress": ["192.0.2.11"],
                "network": "VM Network",
            }
        ],
        "runtime.powerState": "poweredOn",
        "runtime.host": _moref("HostSystem", "host-10"),
        "snapshot": {
            "rootSnapshotList": [{"name": "before-patch", "childSnapshotList": [{"name": "after"}]}]
        },
    },
    "vm-102": {
        "name": "db-lin-01",
        "config.template": False,
        "config.uuid": _LIN_BIOS,
        "config.instanceUuid": _LIN_INSTANCE,
        "config.guestFullName": "Ubuntu Linux (64-bit)",
        "config.guestId": "ubuntu64Guest",
        "config.firmware": "bios",
        "config.hardware.numCPU": 2,
        "config.hardware.memoryMB": 8192,
        "config.hardware.device": [
            _disk(2000, "Hard disk 1", 40, _flat(thin=True)),
            _nic("00:50:56:aa:00:02"),
        ],
        "guest.guestFullName": "Ubuntu Linux (64-bit)",
        "guest.guestFamily": "linuxGuest",
        "guest.hostName": "db-lin-01",
        "runtime.powerState": "poweredOn",
        "runtime.host": _moref("HostSystem", "host-11"),
    },
    "vm-103": {
        "name": "db-lin-01-copy",
        "config.template": False,
        "config.uuid": _LIN_BIOS,
        "config.instanceUuid": _COPY_INSTANCE,
        "config.guestFullName": "Ubuntu Linux (64-bit)",
        "config.guestId": "ubuntu64Guest",
        "config.firmware": "bios",
        "config.hardware.numCPU": 2,
        "config.hardware.memoryMB": 8192,
        "config.hardware.device": [_disk(2000, "Hard disk 1", 40, _flat(thin=True))],
        "runtime.powerState": "poweredOff",
        "runtime.host": _moref("HostSystem", "host-11"),
    },
    "vm-104": {"name": "tmpl-base", "config.template": True},
    "vm-105": {"name": "broken-01", "config.template": False},
}

_COUNTERS = [
    {"key": 2, "groupInfo": {"key": "cpu"}, "nameInfo": {"key": "usage"}, "rollupType": "average"},
    {"key": 24, "groupInfo": {"key": "mem"}, "nameInfo": {"key": "usage"}, "rollupType": "average"},
    {
        "key": 150,
        "groupInfo": {"key": "net"},
        "nameInfo": {"key": "received"},
        "rollupType": "average",
    },
    {"key": 6, "groupInfo": {"key": "cpu"}, "nameInfo": {"key": "usagemhz"}, "rollupType": "none"},
]


class _Fault:
    """A property vCenter could not read: it lands in ``missingSet`` with this fault."""

    def __init__(self, type_name: str) -> None:
        self.type_name = type_name


def _objects(mo_type: str, rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "objects": [
            {
                "obj": _moref(mo_type, moid),
                "propSet": [
                    {"name": name, "val": value}
                    for name, value in props.items()
                    if not isinstance(value, _Fault)
                ],
                "missingSet": [
                    {"path": name, "fault": {"_typeName": value.type_name}}
                    for name, value in props.items()
                    if isinstance(value, _Fault)
                ],
            }
            for moid, props in rows.items()
        ]
    }


class _FakeVcenter:
    """Answers the VI-JSON calls the operation makes, from :data:`_VMS`."""

    version = "9.0"

    def __init__(
        self,
        *,
        failing: frozenset[str] = frozenset({"vm-105"}),
        vms: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.failing = failing
        self.vms = vms if vms is not None else _VMS
        self.scopes: list[tuple[str, str]] = []
        self.perf_queries: list[dict[str, Any]] = []

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Any, json: Any = None, **_: Any
    ) -> Any:
        del target, operator
        if path.endswith("RetrieveServiceContent"):
            return {
                "rootFolder": _moref("Folder", "group-d1"),
                "perfManager": _moref("PerformanceManager", "PerfMgr"),
                "about": {"instanceUuid": _VC_UUID.upper()},
            }
        if path.endswith("QueryPerf"):
            self.perf_queries.append(json)
            return self._perf(json)
        return self._retrieve(json)

    def _retrieve(self, body: dict[str, Any]) -> dict[str, Any]:
        spec = body["specSet"][0]
        mo_type = spec["propSet"][0]["type"]
        paths = spec["propSet"][0]["pathSet"]
        objs = spec["objectSet"]
        if objs[0].get("selectSet"):
            self.scopes.append((objs[0]["obj"]["type"], objs[0]["obj"]["value"]))
            rows = {m: {p: v[p] for p in paths if p in v} for m, v in self.vms.items()}
            return _objects("VirtualMachine", rows)
        moids = [o["obj"]["value"] for o in objs]
        if mo_type == "VirtualMachine":
            if self.failing & set(moids):
                raise httpx.ConnectError("vCenter did not answer")
            return _objects(
                "VirtualMachine",
                {m: {p: self.vms[m][p] for p in paths if p in self.vms[m]} for m in moids},
            )
        if mo_type == "HostSystem":
            hosts = {
                "host-10": {
                    "name": "esx-01.example.test",
                    "parent": _moref("ClusterComputeResource", "domain-c7"),
                },
                "host-11": {
                    "name": "esx-02.example.test",
                    "parent": _moref("ComputeResource", "domain-s9"),
                },
            }
            return _objects("HostSystem", {m: hosts[m] for m in moids})
        if mo_type == "ClusterComputeResource":
            return _objects("ClusterComputeResource", {"domain-c7": {"name": "cl-prod"}})
        if mo_type == "PerformanceManager":
            return _objects("PerformanceManager", {"PerfMgr": {"perfCounter": _COUNTERS}})
        raise AssertionError(f"unexpected read of {mo_type}")

    def _perf(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        answer = []
        for spec in body["querySpec"]:
            if spec["entity"]["value"] != "vm-101":
                continue
            answer.append(
                {
                    "_typeName": "PerfEntityMetric",
                    "entity": spec["entity"],
                    "sampleInfo": [{"timestamp": "t", "interval": 7200}] * 4,
                    "value": [
                        {"id": {"counterId": 2, "instance": ""}, "value": [1000, 2000, 3000, -1]},
                        {
                            "id": {"counterId": 24, "instance": ""},
                            "value": [5000, 5000, 6000, 7000],
                        },
                        {
                            "id": {"counterId": 150, "instance": ""},
                            "value": [1024, 2048, 3072, 4096],
                        },
                        {
                            "id": {"counterId": 2, "instance": "0"},
                            "value": [9999, 9999, 9999, 9999],
                        },
                    ],
                }
            )
        return answer


async def _no_children(**kwargs: Any) -> OperationResult:
    raise AssertionError(f"no child operation expected: {kwargs}")


async def _run(params: dict[str, Any] | None = None, vcenter: _FakeVcenter | None = None) -> Any:
    return await migration_inventory_composite(
        operator=_operator(),
        target=_Target(),
        params=params or {},
        connector=vcenter or _FakeVcenter(),  # type: ignore[arg-type]
        dispatch_child=_no_children,  # type: ignore[arg-type]
    )


def _reduce_context(operator: Operator) -> dict[str, Any]:
    return {
        "tenant_id": str(operator.tenant_id),
        "operator_sub": operator.sub,
        "op_id": "vmware.migration.inventory",
    }


def _by_name(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {record["name"]: record for record in result["records"]}


async def test_counts_match_vcenter_and_templates_and_failures_are_listed() -> None:
    vcenter = _FakeVcenter()
    result = await _run(vcenter=vcenter)

    envelope = InventoryEnvelope.model_validate(result)
    assert vcenter.scopes == [("Folder", "group-d1")]
    assert envelope.counts.source_totals == {"server": 5}
    assert envelope.counts.server == 3
    check = envelope.counts.reconciliation[0]
    assert (check.source_total, check.records, check.omitted) == (5, 3, 2)
    assert check.matches is True
    assert check.difference == 0
    omitted = {o.source_id: (o.reason, o.what) for o in envelope.omissions if o.kind == "server"}
    assert omitted == {
        f"vmware:{_VC_UUID}:vm-104": ("not_supported", "VM 'tmpl-base' (vm-104)"),
        f"vmware:{_VC_UUID}:vm-105": ("collection_failed", "VM 'broken-01' (vm-105)"),
    }


async def test_windows_vm_matches_vcenter_values() -> None:
    record = _by_name(await _run())["app-win-01"]

    assert record["source_id"] == f"vmware:{_VC_UUID}:{_WIN_INSTANCE}"
    assert record["identity_hints"] == {
        "bios_uuid": "4201aaaa-0000-4000-8000-0000000000a0",
        "mac_addresses": ["00:50:56:aa:00:01"],
        "host_name": "app-win-01.example.test",
    }
    assert (record["cores"], record["memory_configured_mb"], record["memory_assigned_mb"]) == (
        4,
        16384,
        16384,
    )
    disks = [
        (d["label"], d["size_gb"], d["thin"], d["shared"], d["raw_mapping"], d["independent"])
        for d in record["disks"]
    ]
    assert disks == [
        ("Hard disk 1", 80.0, True, False, False, False),
        ("Hard disk 2", 200.0, False, True, False, True),
        ("Hard disk 3", 500.0, None, False, True, False),
    ]
    assert record["missing"]["disks.2.thin"] == "not_reported_by_source"
    assert record["snapshot_count"] == 2
    assert record["os"]["family"] == "windows"
    assert record["os"]["architecture"] == "x64"
    assert (record["boot_type"], record["secure_boot"], record["power_state"]) == (
        "uefi",
        True,
        "on",
    )
    assert record["nics"] == [
        {
            "mac_address": "00:50:56:AA:00:01",
            "ip_addresses": ["192.0.2.11"],
            "network": "VM Network",
        }
    ]
    assert record["source_attributes"] == {
        "vm_id": "vm-101",
        "instance_uuid": _WIN_INSTANCE,
        "esxi_host": "esx-01.example.test",
        "cluster": "cl-prod",
    }


async def test_utilization_comes_from_vcenter_history_with_its_window() -> None:
    vcenter = _FakeVcenter()
    record = _by_name(await _run({"utilization_days": 30}, vcenter))["app-win-01"]

    usage = record["utilization"]
    assert usage["source"] == "vcenter-performance"
    assert usage["samples"] == 4
    assert usage["cpu_percent"] == {"average": 20.0, "p95": 30.0, "maximum": 30.0}
    assert usage["memory_percent"] == {"average": 57.5, "p95": 70.0, "maximum": 70.0}
    assert usage["network_in_mb_per_s"] == {"average": 2.5, "p95": 4.0, "maximum": 4.0}
    assert usage["network_out_mb_per_s"] is None
    assert record["missing"]["utilization.network_out_mb_per_s"] == "not_measured"
    spec = vcenter.perf_queries[0]["querySpec"][0]
    assert spec["intervalId"] == 7200
    assert sorted(m["counterId"] for m in spec["metricId"]) == [2, 24, 150]


async def test_missing_utilization_is_unknown_with_a_reason_never_zero() -> None:
    records = _by_name(await _run())

    running = records["db-lin-01"]
    assert running["utilization"] is None
    assert running["missing"]["utilization"] == "not_measured"
    stopped = records["db-lin-01-copy"]
    assert stopped["utilization"] is None
    assert stopped["missing"]["utilization"] == "powered_off"
    assert stopped["memory_assigned_mb"] is None
    assert stopped["missing"]["memory_assigned_mb"] == "powered_off"
    assert stopped["os"]["name"] == "Ubuntu Linux (64-bit)"
    assert stopped["source_attributes"]["esxi_host"] == "esx-02.example.test"
    assert "cluster" not in stopped["source_attributes"]


async def test_same_bios_uuid_is_flagged_and_not_counted_twice_in_the_csv() -> None:
    result = await _run()
    assert result["duplicates"] == [
        {
            "source_id": f"vmware:{_VC_UUID}:{_COPY_INSTANCE}",
            "same_as": f"vmware:{_VC_UUID}:{_LIN_INSTANCE}",
            "match": "bios_uuid",
        }
    ]

    store = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    operator = _operator()
    _reduced, handle = await JsonFluxReducer(byte_threshold=256, store=store).reduce(
        result, None, context=_reduce_context(operator)
    )
    assert handle is not None
    loaded = await load_inventories(operator, [str(handle.handle_id)], store=store)
    export = inventory_to_csv(loaded.records, loaded.duplicates)
    assert export.row_count == 2
    assert [(row.name, row.reason) for row in export.left_out] == [
        ("db-lin-01-copy", "same_machine_as")
    ]


async def test_repeated_instance_uuid_gets_a_vm_id_and_is_flagged() -> None:
    vms = {moid: dict(props) for moid, props in _VMS.items()}
    vms["vm-103"]["config.instanceUuid"] = _LIN_INSTANCE
    vms["vm-103"]["config.uuid"] = "42010000-0000-4000-8000-0000000000cc"

    result = await _run(vcenter=_FakeVcenter(vms=vms))

    assert result["duplicates"] == [
        {
            "source_id": f"vmware:{_VC_UUID}:vm-103",
            "same_as": f"vmware:{_VC_UUID}:{_LIN_INSTANCE}",
            "match": "instance_uuid",
        }
    ]


async def test_linked_duplicates_name_one_machine_and_the_csv_counts_it_once() -> None:
    """vm-103 shares vm-102's BIOS UUID; vm-106 shares vm-103's instance UUID."""
    vms = {moid: dict(props) for moid, props in _VMS.items()}
    vms["vm-106"] = {
        **vms["vm-103"],
        "name": "db-lin-01-again",
        "config.uuid": "42010000-0000-4000-8000-0000000000cc",
        "config.instanceUuid": _COPY_INSTANCE.upper(),
    }
    linux = f"vmware:{_VC_UUID}:{_LIN_INSTANCE}"

    result = await _run(vcenter=_FakeVcenter(vms=vms))

    assert result["counts"]["reconciliation"][0]["matches"] is True
    assert result["duplicates"] == [
        {
            "source_id": f"vmware:{_VC_UUID}:{_COPY_INSTANCE}",
            "same_as": linux,
            "match": "bios_uuid",
        },
        {"source_id": f"vmware:{_VC_UUID}:vm-106", "same_as": linux, "match": "instance_uuid"},
    ]

    store = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    operator = _operator()
    _reduced, handle = await JsonFluxReducer(byte_threshold=256, store=store).reduce(
        result, None, context=_reduce_context(operator)
    )
    assert handle is not None
    loaded = await load_inventories(operator, [str(handle.handle_id)], store=store)
    assert [d.match for d in loaded.duplicates] == ["bios_uuid", "instance_uuid"]
    export = inventory_to_csv(loaded.records, loaded.duplicates)
    assert export.row_count == 2
    assert [(row.name, row.reason, row.same_as) for row in export.left_out] == [
        ("db-lin-01-copy", "same_machine_as", linux),
        ("db-lin-01-again", "same_machine_as", linux),
    ]


async def test_a_refused_secure_boot_read_is_unknown_not_off() -> None:
    vms = {moid: dict(props) for moid, props in _VMS.items()}
    vms["vm-101"]["config.bootOptions.efiSecureBootEnabled"] = _Fault("NoPermission")

    record = _by_name(await _run(vcenter=_FakeVcenter(vms=vms)))["app-win-01"]

    assert record["boot_type"] == "uefi"
    assert record["secure_boot"] is None
    assert record["missing"]["secure_boot"] == "no_permission"


async def test_an_efi_vm_without_the_secure_boot_flag_has_it_off() -> None:
    vms = {moid: dict(props) for moid, props in _VMS.items()}
    del vms["vm-101"]["config.bootOptions.efiSecureBootEnabled"]

    record = _by_name(await _run(vcenter=_FakeVcenter(vms=vms)))["app-win-01"]

    assert (record["boot_type"], record["secure_boot"]) == ("uefi", False)
    assert "secure_boot" not in record["missing"]


async def test_a_failed_batch_is_read_again_one_vm_at_a_time() -> None:
    vcenter = _FakeVcenter(failing=frozenset({"vm-102"}))
    result = await _run(vcenter=vcenter)

    names = sorted(_by_name(result))
    assert names == ["app-win-01", "broken-01", "db-lin-01-copy"]
    assert result["counts"]["reconciliation"][0]["matches"] is True


async def test_scope_narrows_to_a_cluster() -> None:
    vcenter = _FakeVcenter()
    await _run({"cluster": "domain-c7"}, vcenter)
    assert vcenter.scopes == [("ClusterComputeResource", "domain-c7")]


async def test_vcenter_performance_failure_leaves_utilization_unknown() -> None:
    class _NoPerf(_FakeVcenter):
        def _perf(self, body: dict[str, Any]) -> list[dict[str, Any]]:
            raise httpx.ReadTimeout("slow")

    result = await _run(vcenter=_NoPerf())

    record = _by_name(result)["app-win-01"]
    assert record["utilization"] is None
    assert record["missing"]["utilization"] == "collection_failed"
    problems = [o for o in result["omissions"] if o["kind"] is None]
    assert [o["reason"] for o in problems] == ["collection_failed"]
    assert result["counts"]["reconciliation"][0]["matches"] is True


# --- VCF Operations ----------------------------------------------------------


class _VropsTarget:
    name = "vrops-fixture"
    product = "vrops"


def _vrops_child(status: str = "ok") -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def dispatch_child(
        *, connector_id: str, op_id: str, params: dict[str, Any], target: Any = None
    ) -> OperationResult:
        assert connector_id == "vrops-rest-9.0"
        assert isinstance(target, _VropsTarget)
        calls.append((op_id, params))
        if status != "ok":
            return OperationResult(status=status, op_id=op_id, duration_ms=1.0)
        if op_id == "vrops.resource.query":
            name = params["name"][0]
            moid = {"app-win-01": "vm-101", "db-lin-01": "vm-102"}.get(name)
            if moid is None:
                return OperationResult(
                    status="ok", op_id=op_id, result={"resourceList": []}, duration_ms=1.0
                )
            identifiers = [
                {"identifierType": {"name": "VMEntityObjectID"}, "value": moid},
                {"identifierType": {"name": "VMEntityVCID"}, "value": _VC_UUID},
            ]
            resource = {
                "identifier": f"res-{moid}",
                "resourceKey": {"name": name, "resourceIdentifiers": identifiers},
            }
            return OperationResult(
                status="ok", op_id=op_id, result={"resourceList": [resource]}, duration_ms=1.0
            )
        stamp = int(datetime(2026, 10, 20, 8, tzinfo=UTC).timestamp() * 1000)
        stats = [
            {"statKey": {"key": "cpu|usage_average"}, "timestamps": [stamp], "data": [12.5]},
            {"statKey": {"key": "mem|usage_average"}, "timestamps": [stamp], "data": [40.0]},
        ]
        return OperationResult(
            status="ok",
            op_id=op_id,
            result={
                "values": [{"resourceId": params["resourceId"][0], "stat-list": {"stat": stats}}]
            },
            duration_ms=1.0,
        )

    return calls, dispatch_child


async def _run_vrops(monkeypatch: pytest.MonkeyPatch, status: str = "ok") -> tuple[Any, list[Any]]:
    async def _resolve(operator: Any, name: str) -> _VropsTarget:
        assert name == "vrops-fixture"
        return _VropsTarget()

    monkeypatch.setattr(_migration_inventory, "_resolve_vrops_target", _resolve)
    calls, dispatch_child = _vrops_child(status)
    vcenter = _FakeVcenter()
    result = await migration_inventory_composite(
        operator=_operator(),
        target=_Target(),
        params={"vcf_operations_target": "vrops-fixture"},
        connector=vcenter,  # type: ignore[arg-type]
        dispatch_child=dispatch_child,
    )
    assert vcenter.perf_queries == []
    return result, calls


async def test_utilization_comes_from_vcf_operations_when_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _calls = await _run_vrops(monkeypatch)

    records = _by_name(result)
    usage = records["app-win-01"]["utilization"]
    assert usage["source"] == "vcf-operations-latest"
    assert usage["samples"] == 1
    assert usage["cpu_percent"] == {"average": 12.5, "p95": 12.5, "maximum": 12.5}
    assert usage["window_start"] == usage["window_end"]
    assert records["app-win-01"]["missing"]["utilization.network_in_mb_per_s"] == "not_measured"
    assert records["db-lin-01-copy"]["missing"]["utilization"] == "not_measured"


async def test_a_denied_vcf_operations_read_stops_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = await _run_vrops(monkeypatch, status="denied")

    assert len(calls) == 1
    for record in result["records"]:
        assert record["utilization"] is None
        assert record["missing"]["utilization"] == "no_permission"
    problems = [o for o in result["omissions"] if o["kind"] is None]
    assert [(o["what"], o["reason"]) for o in problems] == [
        ("utilization from VCF Operations", "no_permission")
    ]


# --- helpers, contract and registration ---------------------------------------


def test_stat_uses_nearest_rank_and_skips_missing_samples() -> None:
    stat = nearest_rank_stat([float(v) for v in range(1, 21)] + [-1.0])
    assert stat is not None
    assert (stat.average, stat.p95, stat.maximum) == (10.5, 19.0, 20.0)
    assert nearest_rank_stat([-1.0]) is None


@pytest.mark.parametrize(("days", "interval"), [(1, 300), (7, 1800), (30, 7200), (90, 86400)])
def test_interval_follows_the_window(days: int, interval: int) -> None:
    assert interval_for_days(days) == interval


def test_count_check_must_add_up() -> None:
    with pytest.raises(ValidationError):
        CountCheck(kind="server", source_total=5, records=3, omitted=1, difference=0, matches=True)
    with pytest.raises(ValidationError):
        CountCheck(kind="server", source_total=5, records=3, omitted=1, difference=1, matches=True)
    check = CountCheck(
        kind="server", source_total=5, records=3, omitted=1, difference=1, matches=False
    )
    assert check.difference == 1


async def test_envelope_refuses_a_wrong_reconciliation_or_unknown_duplicate() -> None:
    result = await _run()
    wrong_count = {**result, "counts": {**result["counts"]}}
    wrong_count["counts"]["reconciliation"] = [
        {**result["counts"]["reconciliation"][0], "records": 2, "difference": 1, "matches": False}
    ]
    with pytest.raises(ValidationError, match="reconciliation"):
        InventoryEnvelope.model_validate(wrong_count)
    unknown = {**result, "duplicates": [{"source_id": "x", "same_as": "y", "match": "bios_uuid"}]}
    with pytest.raises(ValidationError, match="duplicates"):
        InventoryEnvelope.model_validate(unknown)


def test_registered_safe_read_on_both_vcenter_catalogs() -> None:
    spec = next(s for s in _COMPOSITES if s.op_id == "vmware.migration.inventory")
    assert spec.op_id not in COMPOSITES_NOT_ON_8_0
    assert (spec.safety_level, spec.requires_approval) == ("safe", False)
    assert spec.group_key == "migration"
    assert _WHEN_TO_USE_BY_GROUP["migration"]
    assert write_word_in_op_id(spec.op_id) is None
    for example in spec.parameter_schema["examples"]:
        assert validate_params(spec.parameter_schema, example) == []
    both = {"cluster": "domain-c7", "folder": "group-v3"}
    assert validate_params(spec.parameter_schema, both) != []
