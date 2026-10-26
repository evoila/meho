# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fixture tests for the migration inventory shape, the assessment result
shape, the stored-inventory loader and the Azure Migrate CSV mapping.

The fixtures are made up: a Windows VM with three disks, a Linux VM with no
utilization, the same Linux machine read again through its guest OS (same
BIOS UUID), a Kubernetes workload and an application. The golden CSV file
``tests/data/migration_azure_migrate_golden.csv`` pins the exact output; its header
was checked column by column against Microsoft's import template.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.migration import azure_migrate_csv
from meho_backplane.connectors.migration import ops as migration_ops
from meho_backplane.connectors.migration.azure_migrate_csv import inventory_to_csv
from meho_backplane.connectors.migration.contracts import (
    AssessmentResult,
    InventoryEnvelope,
    KubernetesWorkloadRecord,
    ServerRecord,
    assessment_result_json_schema,
    inventory_json_schema,
)
from meho_backplane.connectors.migration.inventory_load import (
    InventoryLoadError,
    load_inventories,
    merge_inventories,
)
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.delete_names import write_word_in_op_id
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

GOLDEN_CSV = Path(__file__).parent / "data" / "migration_azure_migrate_golden.csv"

#: The header row of Microsoft's import template (AzureMigrateimporttemplate.csv),
#: copied from the file without its byte order mark.
MICROSOFT_TEMPLATE_HEADER = (
    "*Server name,IP addresses,*Cores,*Memory (In MB),*OS name,OS version,"
    "OS architecture,Server type,Hypervisor,CPU utilization percentage,"
    "Memory utilization percentage,Network adapters,Network In throughput,"
    "Network Out throughput,Boot type,Number of disks,Storage in use (In GB),"
    "Disk 1 size (In GB),Disk 1 read throughput (MB per second),"
    "Disk 1 write throughput (MB per second),Disk 1 read ops (operations per second),"
    "Disk 1 write ops (operations per second),Disk 2 size (In GB),"
    "Disk 2 read throughput (MB per second),Disk 2 write throughput (MB per second),"
    "Disk 2 read ops (operations per second),Disk 2 write ops (operations per second)"
)

_VCENTER = "vmware:6b1f0c2a-0000-4000-8000-00000000a001"
_TIME = "2026-10-20T08:00:00Z"
_OPERATOR_SUB = "operator:fixture"


class _MemoryValkey:
    """The two calls of the async Valkey client the store uses."""

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


def _operator(tenant_id: UUID, sub: str = _OPERATOR_SUB) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="fake.jwt.value",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


def _stat(average: float, p95: float, maximum: float) -> dict[str, float]:
    return {"average": average, "p95": p95, "maximum": maximum}


def _windows_vm() -> dict[str, Any]:
    def disk(label: str, size: float, used: float, p95_ops: float) -> dict[str, Any]:
        return {
            "label": label,
            "size_gb": size,
            "used_gb": used,
            "thin": True,
            "shared": False,
            "raw_mapping": False,
            "independent": False,
            "read_ops_per_s": _stat(p95_ops / 2, p95_ops, p95_ops * 2),
            "write_ops_per_s": _stat(p95_ops / 4, p95_ops / 2, p95_ops),
            "read_mb_per_s": _stat(1.0, 2.5, 9.0),
            "write_mb_per_s": _stat(0.5, 1.25, 4.0),
        }

    return {
        "kind": "server",
        "source_id": f"{_VCENTER}:5003a1b2-0000-4000-8000-0000000000w1",
        "identity_hints": {
            "bios_uuid": "4203A1B2-1111-4000-8000-0000000000A1",
            "mac_addresses": ["00:50:56:AA:00:01"],
            "host_name": "APP-WIN-01.example.test",
        },
        "name": "app-win-01",
        "server_type": "virtual",
        "hypervisor": "VMware",
        "cores": 4,
        "memory_configured_mb": 16384,
        "memory_assigned_mb": 16384,
        "disks": [
            disk("Hard disk 1", 100, 62.5, 120),
            disk("Hard disk 2", 250, 180, 40),
            disk("Hard disk 3", 50.75, 10, 8),
        ],
        "snapshot_count": 1,
        "os": {
            "name": "Windows Server 2019",
            "version": "10.0.17763",
            "family": "windows",
            "architecture": "x86_64",
        },
        "boot_type": "uefi",
        "secure_boot": True,
        "power_state": "on",
        "nics": [{"mac_address": "00:50:56:aa:00:01", "ip_addresses": ["192.0.2.11"]}],
        "utilization": {
            "window_start": "2026-09-20T08:00:00Z",
            "window_end": _TIME,
            "samples": 8640,
            "source": "vcenter-perf",
            "cpu_percent": _stat(22.0, 61.3, 97.0),
            "memory_percent": _stat(48.0, 70.0, 88.5),
            "network_in_mb_per_s": _stat(0.8, 3.2, 12.0),
            "network_out_mb_per_s": _stat(0.4, 1.6, 7.0),
        },
        "collected_at": _TIME,
        "missing": {"nics.0.network": "not_reported_by_source"},
    }


_LINUX_BIOS_UUID = "4203c3d4-2222-4000-8000-0000000000b2"


def _linux_vm() -> dict[str, Any]:
    return {
        "kind": "server",
        "source_id": f"{_VCENTER}:5003c3d4-0000-4000-8000-0000000000l1",
        "identity_hints": {"bios_uuid": _LINUX_BIOS_UUID, "host_name": "db-lnx-01"},
        "name": "db-lnx-01",
        "server_type": "virtual",
        "hypervisor": "VMware",
        "cores": 2,
        "memory_configured_mb": 8192,
        "memory_assigned_mb": 8192,
        "disks": [{"label": "Hard disk 1", "size_gb": 40, "thin": False}],
        "snapshot_count": 0,
        "os": {"name": "Ubuntu Linux", "version": "22.04", "family": "linux"},
        "boot_type": "bios",
        "secure_boot": False,
        "power_state": "on",
        "nics": [{"mac_address": "00:50:56:aa:00:02", "ip_addresses": ["192.0.2.12"]}],
        "utilization": None,
        "collected_at": _TIME,
        "missing": {"utilization": "not_measured", "os.architecture": "not_reported_by_source"},
    }


def _linux_guest_view() -> dict[str, Any]:
    """The same Linux machine, read through SSH on its guest OS."""
    return {
        "kind": "server",
        "source_id": "linux:db-lnx-01:machine-id-0f1e2d3c",
        "identity_hints": {"bios_uuid": _LINUX_BIOS_UUID.upper(), "host_name": "db-lnx-01"},
        "name": "db-lnx-01",
        "server_type": None,
        "hypervisor": None,
        "cores": 2,
        "memory_configured_mb": 7976,
        "memory_assigned_mb": None,
        "disks": [{"label": "sda", "size_gb": 40, "used_gb": 12.5}],
        "snapshot_count": None,
        "os": {"name": "Ubuntu Linux", "version": "22.04.4", "family": "linux"},
        "boot_type": "bios",
        "secure_boot": None,
        "power_state": "on",
        "nics": [{"mac_address": "00:50:56:aa:00:02", "ip_addresses": ["192.0.2.12"]}],
        "utilization": None,
        "collected_at": _TIME,
        "missing": {
            "server_type": "not_reported_by_source",
            "hypervisor": "not_reported_by_source",
            "memory_assigned_mb": "not_supported",
            "snapshot_count": "not_supported",
            "secure_boot": "no_permission",
            "utilization": "not_measured",
        },
    }


def _application() -> dict[str, Any]:
    return {
        "kind": "application",
        "source_id": f"{_VCENTER}:5003a1b2-0000-4000-8000-0000000000w1:app:mssql",
        "name": "Microsoft SQL Server",
        "version": "15.0",
        "vendor": "Microsoft",
        "category": "database",
        "runs_on": [f"{_VCENTER}:5003a1b2-0000-4000-8000-0000000000w1"],
        "collected_at": _TIME,
    }


def _k8s_workload() -> dict[str, Any]:
    return {
        "kind": "kubernetes_workload",
        "source_id": "k8s:cluster-a:shop:Deployment:web",
        "cluster": "cluster-a",
        "namespace": "shop",
        "workload_kind": "Deployment",
        "name": "web",
        "replicas": 3,
        "cpu_request_millicores": 500,
        "cpu_limit_millicores": 1000,
        "memory_request_mb": 512,
        "memory_limit_mb": 1024,
        "images": ["registry.example.test/shop/web:1.4.2"],
        "volumes": [
            {"name": "cache", "type": "emptyDir"},
            {
                "name": "uploads",
                "type": "persistentVolumeClaim",
                "size_gb": 20,
                "storage_class": "standard",
            },
        ],
        "nodes": ["worker-1", "worker-2"],
        "collected_at": _TIME,
        "missing": {"volumes.0.size_gb": "not_supported"},
    }


def _envelope(
    records: list[dict[str, Any]], connector_id: str, target: str, **extra: Any
) -> dict[str, Any]:
    counts = {"server": 0, "application": 0, "kubernetes_workload": 0}
    for record in records:
        counts[record["kind"]] += 1
    return {
        "schema_version": "1",
        "source": {"connector_id": connector_id, "target": target, "collected_at": _TIME},
        "records": records,
        "omissions": extra.pop("omissions", []),
        "counts": {**counts, **extra.pop("counts", {})},
    }


def _vcenter_inventory() -> dict[str, Any]:
    return _envelope(
        [_windows_vm(), _linux_vm(), _application()],
        "vmware-rest-9.0",
        "vc-lab-a",
        counts={"source_totals": {"server": 3}},
        omissions=[
            {
                "what": "VM 'tmpl-base' (a template)",
                "reason": "not_supported",
                "detail": "templates are not servers",
            }
        ],
    )


def _guest_inventory() -> dict[str, Any]:
    return _envelope([_linux_guest_view()], "linux-ssh-1.0", "db-lnx-01")


# --- inventory shape --------------------------------------------------------


def test_windows_vm_with_three_disks_round_trips() -> None:
    record = ServerRecord.model_validate(_windows_vm())
    assert [disk.size_gb for disk in record.disks or []] == [100, 250, 50.75]
    assert record.identity_hints.bios_uuid == "4203a1b2-1111-4000-8000-0000000000a1"
    assert record.identity_hints.mac_addresses == ["00:50:56:aa:00:01"]
    again = ServerRecord.model_validate_json(record.model_dump_json())
    assert again == record


def test_linux_vm_without_utilization_needs_a_reason() -> None:
    record = ServerRecord.model_validate(_linux_vm())
    assert record.utilization is None
    assert record.missing["utilization"] == "not_measured"

    no_reason = _linux_vm()
    del no_reason["missing"]["utilization"]
    with pytest.raises(ValidationError, match=r"\['utilization'\] are null"):
        ServerRecord.model_validate(no_reason)


def test_unknown_values_are_never_zero_by_default() -> None:
    bare = {"source_id": "x:1", "name": "bare", "collected_at": _TIME}
    with pytest.raises(ValidationError, match="have no reason"):
        ServerRecord.model_validate(bare)


def test_kubernetes_workload_validates() -> None:
    record = KubernetesWorkloadRecord.model_validate(_k8s_workload())
    assert record.replicas == 3
    assert record.nodes == ["worker-1", "worker-2"]
    assert record.volumes is not None
    assert record.volumes[1].size_gb == 20


def test_envelope_counts_must_match_records() -> None:
    envelope = InventoryEnvelope.model_validate(_vcenter_inventory())
    assert envelope.counts.server == 2
    assert envelope.counts.source_totals == {"server": 3}

    wrong = _vcenter_inventory()
    wrong["counts"]["server"] = 3
    with pytest.raises(ValidationError, match="do not match the records"):
        InventoryEnvelope.model_validate(wrong)


def test_envelope_rejects_a_repeated_source_id() -> None:
    twice = _envelope([_linux_vm(), _linux_vm()], "vmware-rest-9.0", "vc-lab-a")
    with pytest.raises(ValidationError, match="source_id values repeat"):
        InventoryEnvelope.model_validate(twice)


def test_envelope_rejects_unknown_fields_and_versions() -> None:
    extra = _vcenter_inventory()
    extra["records"][0]["cpu_count"] = 4
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        InventoryEnvelope.model_validate(extra)
    newer = _vcenter_inventory()
    newer["schema_version"] = "2"
    with pytest.raises(ValidationError):
        InventoryEnvelope.model_validate(newer)


def test_json_schemas_export() -> None:
    inventory = inventory_json_schema()
    assert set(inventory["properties"]) == {
        "schema_version",
        "source",
        "records",
        "omissions",
        "counts",
    }
    assert {"ServerRecord", "ApplicationRecord", "KubernetesWorkloadRecord"} <= set(
        inventory["$defs"]
    )
    assessment = assessment_result_json_schema()
    assert "calculation_version" in assessment["properties"]


# --- assessment result shape ------------------------------------------------


def _assessment(figure: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "calculation_version": "sizing-0.1",
        "inputs": {
            "inventories": [
                {
                    "handle_id": str(uuid4()),
                    "connector_id": "vmware-rest-9.0",
                    "target": "vc-lab-a",
                    "collected_at": _TIME,
                }
            ],
            "generated_at": _TIME,
        },
        "assumptions": {
            "region": "westeurope",
            "currency": "EUR",
            "price_date": "2026-10-01",
            "hours_per_month": 730,
            "comfort_factor": 1.3,
        },
        "items": [
            {
                "source_id": "vmware:x:vm-1",
                "name": "app-win-01",
                "figures": {"monthly_compute_cost": figure},
            }
        ],
        "unknowns": [{"what": "disk throughput of db-lnx-01", "reason": "not_measured"}],
    }


def test_assessment_figure_carries_inputs_assumptions_and_version() -> None:
    figure = {
        "value": 140.16,
        "unit": "EUR/month",
        "basis": "allocated",
        "inputs": {"vm_size": "Standard_D4s_v5", "price_per_hour": 0.192},
        "assumptions": ["region", "currency", "hours_per_month"],
        "calculation_version": "sizing-0.1",
    }
    result = AssessmentResult.model_validate(_assessment(figure))
    stored = result.items[0].figures["monthly_compute_cost"]
    assert stored.inputs["vm_size"] == "Standard_D4s_v5"
    assert stored.calculation_version == "sizing-0.1"


def test_assessment_unknown_needs_a_reason_and_known_assumptions() -> None:
    unknown = {"value": None, "unit": "EUR/month", "calculation_version": "sizing-0.1"}
    with pytest.raises(ValidationError, match="unknown value needs an unknown_reason"):
        AssessmentResult.model_validate(_assessment(unknown))
    unknown["unknown_reason"] = "no price for the region"
    AssessmentResult.model_validate(_assessment(unknown))

    unset = {
        "value": 1.0,
        "unit": "EUR/month",
        "assumptions": ["reservation"],
        "calculation_version": "sizing-0.1",
    }
    with pytest.raises(ValidationError, match=r"cites assumptions \['reservation'\]"):
        AssessmentResult.model_validate(_assessment(unset))


# --- stored inventories ----------------------------------------------------


async def _store_through_reducer(
    store: ResultHandleStore, tenant_id: UUID, envelope: dict[str, Any]
) -> str:
    """Store *envelope* the way a dispatch does: through the JSONFlux reducer."""
    _reduced, handle = await JsonFluxReducer(byte_threshold=256, store=store).reduce(
        envelope,
        None,
        context={
            "tenant_id": str(tenant_id),
            "operator_sub": _OPERATOR_SUB,
            "op_id": "vmware.inventory.read",
        },
    )
    assert handle is not None
    assert handle.fetch_more is not None
    assert handle.fetch_more.drill_in.available
    return str(handle.handle_id)


async def test_load_merges_stored_inventories_and_finds_the_same_machine() -> None:
    store = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    tenant = uuid4()
    vcenter = await _store_through_reducer(store, tenant, _vcenter_inventory())
    guest = await _store_through_reducer(store, tenant, _guest_inventory())

    loaded = await load_inventories(_operator(tenant), [vcenter, guest, vcenter], store=store)

    assert loaded.handle_ids == [vcenter, guest]
    assert [e.source.target for e in loaded.envelopes] == ["vc-lab-a", "db-lnx-01"]
    assert len(loaded.records) == 4
    assert [(d.source_id, d.same_as, d.match) for d in loaded.duplicates] == [
        ("linux:db-lnx-01:machine-id-0f1e2d3c", _linux_vm()["source_id"], "bios_uuid")
    ]


async def test_load_fails_clearly_for_a_missing_or_foreign_handle() -> None:
    store = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    tenant = uuid4()
    handle = await _store_through_reducer(store, tenant, _vcenter_inventory())

    with pytest.raises(InventoryLoadError, match="not found or has expired"):
        await load_inventories(_operator(tenant), [str(uuid4())], store=store)
    with pytest.raises(InventoryLoadError, match="not found or has expired"):
        await load_inventories(_operator(uuid4()), [handle], store=store)
    with pytest.raises(InventoryLoadError, match="not found or has expired"):
        await load_inventories(_operator(tenant, sub="operator:other"), [handle], store=store)
    with pytest.raises(InventoryLoadError, match="is not a handle id"):
        await load_inventories(_operator(tenant), ["vc-lab-a"], store=store)


async def test_load_checks_schema_version_and_shape() -> None:
    store = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    tenant = uuid4()
    newer = _vcenter_inventory()
    newer["schema_version"] = "2"
    newer_handle = await _store_through_reducer(store, tenant, newer)
    with pytest.raises(InventoryLoadError, match="schema version '2'"):
        await load_inventories(_operator(tenant), [newer_handle], store=store)

    broken = _vcenter_inventory()
    broken["counts"]["server"] = 9
    broken_handle = await _store_through_reducer(store, tenant, broken)
    with pytest.raises(InventoryLoadError, match="does not hold a valid inventory"):
        await load_inventories(_operator(tenant), [broken_handle], store=store)


async def test_load_refuses_a_rows_handle() -> None:
    store = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    tenant, handle_id = uuid4(), uuid4()
    await store.spill(
        tenant_id=tenant,
        operator_sub=_OPERATOR_SUB,
        handle_id=handle_id,
        op_id="vmware.vm.list",
        rows=[{"name": "app-win-01"}],
        total_rows=1,
        ttl_seconds=60,
        max_rows=10,
    )
    with pytest.raises(InventoryLoadError, match="holds rows, not one inventory"):
        await load_inventories(_operator(tenant), [str(handle_id)], store=store)


# --- Azure Migrate CSV ------------------------------------------------------


def _golden_export() -> azure_migrate_csv.CsvExport:
    vcenter = InventoryEnvelope.model_validate(_vcenter_inventory())
    guest = InventoryEnvelope.model_validate(_guest_inventory())
    k8s = InventoryEnvelope.model_validate(_envelope([_k8s_workload()], "k8s-1.30", "cluster-a"))
    records, duplicates = merge_inventories([vcenter, guest, k8s])
    return inventory_to_csv(records, duplicates)


def test_golden_csv() -> None:
    export = _golden_export()
    # The file uses CRLF line ends (RFC 4180); the golden copy is kept with LF.
    assert export.csv.count("\r\n") == export.row_count + 1
    assert export.csv.replace("\r\n", "\n") == GOLDEN_CSV.read_text(encoding="utf-8")
    assert export.row_count == 2
    assert export.skipped_records == {"application": 1, "kubernetes_workload": 1}
    assert [(s.source_id, s.reason, s.same_as) for s in export.left_out] == [
        ("linux:db-lnx-01:machine-id-0f1e2d3c", "same_machine_as", _linux_vm()["source_id"])
    ]


def test_golden_csv_columns_follow_microsoft_template() -> None:
    header = next(csv.reader(io.StringIO(GOLDEN_CSV.read_text(encoding="utf-8"))))
    template = MICROSOFT_TEMPLATE_HEADER.split(",")
    assert header[: len(template)] == template
    # The third disk group repeats the template's disk columns with number 3.
    assert header[len(template) :] == [c.replace("Disk 2", "Disk 3") for c in template[-5:]]


def test_csv_leaves_out_servers_without_required_values() -> None:
    no_cores = _linux_vm()
    no_cores["cores"] = None
    no_cores["missing"]["cores"] = "no_permission"
    no_os = _windows_vm()
    no_os["os"] = None
    no_os["missing"]["os"] = "powered_off"
    export = inventory_to_csv(
        [ServerRecord.model_validate(no_cores), ServerRecord.model_validate(no_os)]
    )
    assert export.row_count == 0
    assert [s.missing for s in export.left_out] == [
        {"cores": "no_permission"},
        {"os.name": "powered_off"},
    ]
    assert export.csv.splitlines()[0].startswith("*Server name,")


def test_csv_makes_repeated_names_unique_and_cleans_text() -> None:
    first = _linux_vm()
    second = _windows_vm()
    second["name"] = "DB-LNX-01"
    tricky = _windows_vm()
    tricky["source_id"] = "vmware:x:tricky"
    tricky["identity_hints"]["bios_uuid"] = None
    tricky["name"] = "=cmd|'/c calc'!A1\nnext"
    export = inventory_to_csv([ServerRecord.model_validate(r) for r in (first, second, tricky)])

    names = [row[0] for row in csv.reader(io.StringIO(export.csv))][1:]
    assert names == [
        f"db-lnx-01 ({first['source_id']})",
        f"DB-LNX-01 ({second['source_id']})",
        "'=cmd|'/c calc'!A1 next",
    ]
    assert [r.reasons for r in export.renamed] == [
        ["repeated_name"],
        ["repeated_name"],
        ["cleaned_text"],
    ]


def test_csv_stops_at_the_file_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(azure_migrate_csv, "MAX_SERVERS_PER_FILE", 1)
    export = inventory_to_csv(
        [ServerRecord.model_validate(_windows_vm()), ServerRecord.model_validate(_linux_vm())]
    )
    assert export.row_count == 1
    assert [(s.name, s.reason) for s in export.left_out] == [("db-lnx-01", "over_file_limit")]


# --- the operation ----------------------------------------------------------


async def test_inventory_csv_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    tenant = uuid4()
    handle = await _store_through_reducer(store, tenant, _vcenter_inventory())
    monkeypatch.setattr(
        "meho_backplane.connectors.migration.inventory_load.get_result_handle_store",
        lambda: store,
    )

    result = await migration_ops.migration_inventory_csv(
        _operator(tenant), None, {"inventory_handles": [handle]}
    )

    assert result["row_count"] == 2
    assert result["csv"].startswith("*Server name,IP addresses,*Cores,")
    assert result["sources"] == [
        {
            "handle_id": handle,
            "connector_id": "vmware-rest-9.0",
            "target": "vc-lab-a",
            "collected_at": "2026-10-20T08:00:00+00:00",
        }
    ]
    assert set(migration_ops.MIGRATION_INVENTORY_CSV_RESPONSE_SCHEMA["properties"]) >= set(result)


def test_operation_id_has_no_write_word() -> None:
    assert write_word_in_op_id(migration_ops.MIGRATION_INVENTORY_CSV_OP_ID) is None
