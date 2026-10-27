# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fixture tests for ``migration.assess.vm``: VM sizing and the monthly cost estimate.

The Azure reads (``azure.vm.size.list`` and ``azure.price.list``) are faked
with answers in their documented item shape. Sizes, prices and meter ids
are made up; the price rows copy the way Azure's Retail Prices API names
its products and meters (Windows rows, Spot, Low Priority and Cloud
Services rows, reservation rows with the price for the whole term, disk
rows named ``<tier> LRS Disk``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

import meho_backplane.operations.result_query as rq
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.migration import assess_vm, inventory_load
from meho_backplane.connectors.migration.assess_vm import (
    AZURE_CONNECTOR_ID,
    AssessmentReadError,
    migration_assess_vm,
)
from meho_backplane.connectors.migration.assess_vm_op import (
    _LLM_INSTRUCTIONS,
    MIGRATION_ASSESS_VM_OP_ID,
    MIGRATION_ASSESS_VM_PARAMETER_SCHEMA,
    MIGRATION_ASSESS_VM_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.migration.contracts import AssessmentResult
from meho_backplane.connectors.migration.vm_sizing import family_key
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.delete_names import write_word_in_op_id
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import ResultQuerySpec, run_result_query
from meho_backplane.settings import get_settings

_SUB = "operator:fixture"
_TIME = "2026-10-20T08:00:00Z"
_REGION = "westeurope"


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


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> ResultHandleStore:
    shared = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    monkeypatch.setattr(inventory_load, "get_result_handle_store", lambda: shared)
    monkeypatch.setattr(assess_vm, "get_result_handle_store", lambda: shared)
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: shared)
    return shared


def _operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub=_SUB,
        raw_jwt="fake.jwt.value",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


# --- Azure answers ------------------------------------------------------------


def _size(
    name: str,
    vcpus: int,
    memory_gb: float,
    *,
    family: str = "standardDSv5Family",
    disks: int = 8,
    premium: bool = True,
    generations: tuple[str, ...] = ("V1", "V2"),
    restrictions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "family": family,
        "vcpus": vcpus,
        "vcpus_available": vcpus,
        "memory_gb": memory_gb,
        "max_data_disks": disks,
        "premium_io": premium,
        "hyperv_generations": list(generations),
        "acus": 195,
        "restrictions": restrictions or [],
    }


SIZES: list[dict[str, Any]] = [
    _size("Standard_D2s_v5", 2, 8, disks=4),
    _size("Standard_D4s_v5", 4, 16),
    # Cheaper than D4s_v5, but not offered to this subscription.
    _size(
        "Standard_D4as_v5",
        4,
        16,
        family="standardDASv5Family",
        restrictions=[
            {"type": "Location", "reason_code": "NotAvailableForSubscription", "zones": []}
        ],
    ),
    # Only some zones are restricted: still usable without a zone.
    _size(
        "Standard_E4s_v5",
        4,
        32,
        family="standardESv5Family",
        restrictions=[
            {"type": "Zone", "reason_code": "NotAvailableForSubscription", "zones": ["1"]}
        ],
    ),
    _size("Standard_D8s_v5", 8, 32),
    _size("Standard_D64s_v5", 64, 256, disks=32),
    # The cheapest 2-vCPU size, but generation 2 only and no Premium storage.
    _size(
        "Standard_D2_v6",
        2,
        8,
        family="standardDv6Family",
        disks=4,
        premium=False,
        generations=("V2",),
    ),
]

#: Linux and Windows pay-as-you-go price per hour.
_HOURLY: dict[str, tuple[float, float]] = {
    "Standard_D2s_v5": (0.096, 0.188),
    "Standard_D4s_v5": (0.192, 0.376),
    "Standard_D4as_v5": (0.172, 0.356),
    "Standard_E4s_v5": (0.252, 0.436),
    "Standard_D8s_v5": (0.384, 0.752),
    "Standard_D64s_v5": (3.072, 5.94),
    "Standard_D2_v6": (0.090, 0.182),
}
#: Reservation price for the whole term: 1 year and 3 years.
_RESERVED: dict[str, tuple[float, float]] = {
    "Standard_D2s_v5": (500.0, 950.0),
    "Standard_D4s_v5": (1000.0, 2000.0),
    "Standard_D2_v6": (480.0, 900.0),
}
_PREMIUM = {"P6": 9.88, "P10": 19.08, "P15": 36.79}
_STANDARD = {"E6": 4.22, "E10": 8.45, "E15": 16.9}


def _row(meter: str, price: float, **fields: Any) -> dict[str, Any]:
    return {
        "service_name": "Virtual Machines",
        "product_name": "Virtual Machines DSv5 Series",
        "sku_name": "",
        "arm_sku_name": "",
        "meter_id": f"meter-{meter}",
        "meter_name": meter,
        "region": _REGION,
        "unit": "1 Hour",
        "retail_price": price,
        "unit_price": price,
        "price_type": "Consumption",
        "reservation_term": None,
        "effective_start_date": "2026-06-01T00:00:00Z",
        **fields,
    }


def _vm_rows(price_type: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if price_type == "Consumption":
        for size, (linux, windows) in _HOURLY.items():
            sku = {"arm_sku_name": size, "sku_name": size}
            rows.append(_row(f"{size}-linux", linux, **sku))
            rows.append(
                _row(
                    f"{size}-windows",
                    windows,
                    product_name="Virtual Machines DSv5 Series Windows",
                    **sku,
                )
            )
            rows.append(_row(f"{size}-spot", linux / 10, **{**sku, "sku_name": f"{size} Spot"}))
            rows.append(
                _row(f"{size}-low", linux / 5, **{**sku, "sku_name": f"{size} Low Priority"})
            )
            rows.append(
                _row(f"{size}-cloud", linux / 2, product_name="DSv5 Series Cloud Services", **sku)
            )
    else:
        for size, (one, three) in _RESERVED.items():
            for term, price in (("1 Year", one), ("3 Years", three)):
                rows.append(
                    _row(
                        f"{size}-{term}",
                        price,
                        arm_sku_name=size,
                        sku_name=size,
                        price_type="Reservation",
                        reservation_term=term,
                    )
                )
    return rows


def _disk_rows(product: str) -> list[dict[str, Any]]:
    tiers = _PREMIUM if product.startswith("Premium") else _STANDARD
    rows = [
        _row(
            f"{tier} LRS Disk",
            price,
            service_name="Storage",
            product_name=product,
            unit="1/Month",
            meter_id=f"meter-{tier}",
        )
        for tier, price in tiers.items()
    ]
    # A mount meter of the same tier, which is not the disk price.
    rows.append(
        _row(
            f"{next(iter(tiers))} LRS Disk Mount",
            0.5,
            service_name="Storage",
            product_name=product,
            unit="1/Month",
        )
    )
    return rows


class _FakeAzure:
    """Answers the child calls like the Azure connector's reads.

    The answer has the paging helper's shape: the rows under *rows_key*,
    ``truncated``, ``omission``, ``omission_kind`` and ``pages_read``. The
    read named in *fail* answers with an error; the one named in
    *truncated* says its list was cut by the row limit.
    """

    def __init__(
        self, *, fail: str | None = None, truncated: str | None = None, rows_key: str = "items"
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        self.fail = fail
        self.truncated = truncated
        self.rows_key = rows_key

    def items(self, op_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if op_id == "azure.vm.size.list":
            return SIZES
        if params["service_name"] == "Storage":
            return _disk_rows(params["product_name"])
        return _vm_rows(params["price_type"])

    async def __call__(
        self, *, connector_id: str, op_id: str, params: dict[str, Any], target: Any = None
    ) -> OperationResult:
        assert connector_id == AZURE_CONNECTOR_ID
        self.calls.append((op_id, params, target))
        if op_id == self.fail:
            return OperationResult(
                status="error", op_id=op_id, error="azure answered 403", duration_ms=1.0
            )
        cut = op_id == self.truncated
        result = {
            self.rows_key: self.items(op_id, params),
            "truncated": cut,
            "omission": "The list has more rows than one result holds (10000)." if cut else None,
            "omission_kind": "result_truncated" if cut else None,
            "pages_read": 1,
        }
        return OperationResult(status="ok", op_id=op_id, result=result, duration_ms=1.0)


# --- inventories ------------------------------------------------------------------


def _disk(label: str, size_gb: float) -> dict[str, Any]:
    return {"label": label, "size_gb": size_gb}


def _disk_missing(count: int) -> dict[str, str]:
    reasons: dict[str, str] = {}
    for index in range(count):
        for field in (
            "used_gb",
            "thin",
            "shared",
            "raw_mapping",
            "independent",
            "read_ops_per_s",
            "write_ops_per_s",
            "read_mb_per_s",
            "write_mb_per_s",
        ):
            reasons[f"disks.{index}.{field}"] = "not_reported_by_source"
    return reasons


def _stat(p95: float) -> dict[str, float]:
    return {"average": p95 / 2, "p95": p95, "maximum": min(p95 * 1.5, 100)}


def _server(
    source_id: str,
    name: str,
    *,
    cores: int,
    memory_mb: int,
    family: str | None,
    boot: str | None,
    disks: list[dict[str, Any]] | None,
    utilization: dict[str, Any] | None = None,
    bios_uuid: str | None = None,
) -> dict[str, Any]:
    missing: dict[str, str] = {
        "server_type": "not_reported_by_source",
        "hypervisor": "not_reported_by_source",
        "memory_assigned_mb": "not_reported_by_source",
        "snapshot_count": "not_reported_by_source",
        "secure_boot": "not_reported_by_source",
        "power_state": "not_reported_by_source",
        "nics": "not_reported_by_source",
        "os.version": "not_reported_by_source",
        "os.architecture": "not_reported_by_source",
        "identity_hints.host_name": "not_reported_by_source",
        **_disk_missing(len(disks or [])),
    }
    if utilization is None:
        missing["utilization"] = "not_measured"
    else:
        missing["utilization.network_in_mb_per_s"] = "not_measured"
        missing["utilization.network_out_mb_per_s"] = "not_measured"
        if utilization["sample_interval_minutes"] is None:
            missing["utilization.sample_interval_minutes"] = "not_reported_by_source"
    if family is None:
        missing["os.family"] = "not_reported_by_source"
    if boot is None:
        missing["boot_type"] = "not_reported_by_source"
    if disks is None:
        missing["disks"] = "collection_failed"
    if bios_uuid is None:
        missing["identity_hints.bios_uuid"] = "not_reported_by_source"
    return {
        "kind": "server",
        "source_id": source_id,
        "identity_hints": {"bios_uuid": bios_uuid},
        "name": name,
        "cores": cores,
        "memory_configured_mb": memory_mb,
        "disks": disks,
        "os": {"name": f"{family or 'unknown'} os", "family": family},
        "boot_type": boot,
        "utilization": utilization,
        "collected_at": _TIME,
        "missing": missing,
    }


def _utilization(
    cpu: float, memory: float, *, start: str, samples: int, interval: int | None = 120
) -> dict[str, Any]:
    return {
        "window_start": start,
        "window_end": _TIME,
        "samples": samples,
        "sample_interval_minutes": interval,
        "source": "vcenter-perf",
        "cpu_percent": _stat(cpu),
        "memory_percent": _stat(memory),
    }


WINDOWS = _server(
    "vc:win-01",
    "app-win-01",
    cores=4,
    memory_mb=16384,
    family="windows",
    boot="uefi",
    disks=[_disk("Hard disk 1", 100), _disk("Hard disk 2", 250), _disk("Hard disk 3", 50.75)],
    # 30 days, one sample every 5 minutes: 8640 samples, full coverage.
    utilization=_utilization(30.0, 35.0, start="2026-09-20T08:00:00Z", samples=8640, interval=5),
)
LINUX_UEFI = _server(
    "vc:lnx-uefi",
    "web-lnx-01",
    cores=2,
    memory_mb=8192,
    family="linux",
    boot="uefi",
    disks=[_disk("Hard disk 1", 40)],
)
LINUX_BIOS = _server(
    "vc:lnx-bios",
    "web-lnx-02",
    cores=2,
    memory_mb=8192,
    family="linux",
    boot="bios",
    disks=[_disk("Hard disk 1", 40)],
)
HUGE = _server(
    "vc:huge",
    "erp-huge-01",
    cores=128,
    memory_mb=4 * 1024 * 1024,
    family="linux",
    boot="bios",
    disks=[_disk("Hard disk 1", 100)],
)


def _envelope(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"server": 0, "application": 0, "kubernetes_workload": 0}
    for record in records:
        counts[record["kind"]] += 1
    return {
        "schema_version": "1",
        "source": {
            "connector_id": "vmware-rest-9.0",
            "target": "vc-example",
            "collected_at": _TIME,
        },
        "records": records,
        "omissions": [],
        "counts": counts,
    }


async def _store(store: ResultHandleStore, tenant: UUID, records: list[dict[str, Any]]) -> str:
    _reduced, handle = await JsonFluxReducer(byte_threshold=256, store=store).reduce(
        _envelope(records),
        None,
        context={"tenant_id": str(tenant), "operator_sub": _SUB, "op_id": "vmware.inventory"},
    )
    assert handle is not None
    return str(handle.handle_id)


async def _assess(
    store: ResultHandleStore,
    records: list[dict[str, Any]],
    azure: _FakeAzure | None = None,
    **params: Any,
) -> dict[str, Any]:
    tenant = uuid4()
    handle = await _store(store, tenant, records)
    return await migration_assess_vm(
        _operator(tenant),
        None,
        {
            "inventory_handles": [handle],
            "azure_target": "azure-example",
            "region": _REGION,
            **params,
        },
        azure or _FakeAzure(),
    )


def _item(result: dict[str, Any], source_id: str) -> dict[str, Any]:
    return next(item for item in result["items"] if item["source_id"] == source_id)


def _value(item: dict[str, Any], key: str) -> Any:
    return item["figures"][key]["value"]


# --- tests --------------------------------------------------------------------------


async def test_windows_vm_four_cores_sixteen_gb(store: ResultHandleStore) -> None:
    result = await _assess(store, [WINDOWS])
    AssessmentResult.model_validate({k: v for k, v in result.items() if k != "totals"})
    item = _item(result, "vc:win-01")
    figures = item["figures"]

    size = figures["size.allocated"]
    assert size["inputs"]["size"] == "Standard_D4s_v5"
    assert size["basis"] == "allocated"
    assert size["inputs"]["need_vcpus"] == 4
    assert size["inputs"]["need_memory_gb"] == 16
    assert size["inputs"]["need_data_disks"] == 2
    assert size["inputs"]["need_generation"] == "V2"
    assert "cheapest" in size["inputs"]["why"]

    payg = figures["compute.allocated.pay_as_you_go"]
    assert payg["value"] == 274.48  # 0.376 EUR per hour x 730 hours
    assert payg["inputs"]["meter_id"] == "meter-Standard_D4s_v5-windows"
    assert payg["inputs"]["unit"] == "1 Hour"
    assert payg["inputs"]["effective_start_date"] == "2026-06-01T00:00:00Z"
    assert payg["unit"] == "EUR/month"
    assert payg["calculation_version"] == "migration-assess-vm-1"
    assert {"region", "currency", "price_date", "hours_per_month", "license"} <= set(
        payg["assumptions"]
    )

    # Reservation: whole-term price / 12, plus the Windows license (Windows - Linux) x 730.
    reserved = figures["compute.allocated.reserved_1_year"]
    assert reserved["value"] == round(1000 / 12 + (0.376 - 0.192) * 730, 2)
    assert reserved["inputs"]["license_windows_meter_id"] == "meter-Standard_D4s_v5-windows"
    assert reserved["inputs"]["license_linux_meter_id"] == "meter-Standard_D4s_v5-linux"
    assert _value(item, "compute.allocated.reserved_3_years") == round(
        2000 / 36 + (0.376 - 0.192) * 730, 2
    )

    # Premium SSD (the size supports it): 100 GB -> P10, 250 GB -> P15, 50.75 GB -> P6.
    assert [figures[f"disk.{i}.allocated"]["inputs"]["tier"] for i in range(3)] == [
        "P10",
        "P15",
        "P6",
    ]
    assert _value(item, "disks.allocated") == round(19.08 + 36.79 + 9.88, 2)
    assert _value(item, "monthly.allocated.pay_as_you_go") == round(274.48 + 65.75, 2)

    # Measured: p95 CPU 30% and memory 35% x 1.3 -> 2 vCPUs and 7.28 GB.
    measured = figures["size.measured"]
    assert measured["basis"] == "measured"
    assert measured["inputs"]["size"] == "Standard_D2_v6"
    assert measured["inputs"]["need_vcpus"] == 2
    assert measured["inputs"]["need_memory_gb"] == 7.28
    assert {"comfort_factor", "sample_coverage", "min_window_days"} <= set(measured["assumptions"])
    # The figure shows the measurements it was made from.
    evidence = {
        "cpu_p95_percent": 30.0,
        "memory_p95_percent": 35.0,
        "comfort_factor": 1.3,
        "window_start": "2026-09-20T08:00:00+00:00",
        "window_end": "2026-10-20T08:00:00+00:00",
        "window_days": 30,
        "samples": 8640,
        "sample_interval_minutes": 5,
        "expected_samples": 8640,
        "sample_coverage": 1.0,
        "measurement_source": "vcenter-perf",
    }
    assert {key: measured["inputs"][key] for key in evidence} == evidence
    assert not (set(evidence) & set(size["inputs"]))
    # Standard_D2_v6 has no Premium storage, so the disks are Standard SSD.
    assert figures["disk.0.measured"]["inputs"]["disk_type"] == "standard_ssd"
    assert _value(item, "disks.measured") == round(8.45 + 16.9 + 4.22, 2)
    assert "not_ready" not in item["flags"]
    assert "right_sized_unknown" not in item["flags"]


async def test_restricted_size_is_skipped(store: ResultHandleStore) -> None:
    result = await _assess(store, [WINDOWS], pricing=["pay_as_you_go"])
    size = _item(result, "vc:win-01")["figures"]["size.allocated"]["inputs"]
    # Standard_D4as_v5 is cheaper but restricted for the subscription.
    assert size["size"] == "Standard_D4s_v5"
    assert size["restricted_sizes_skipped"] == 1


async def test_uefi_linux_vm_gets_a_generation_two_size(store: ResultHandleStore) -> None:
    result = await _assess(store, [LINUX_UEFI, LINUX_BIOS], pricing=["pay_as_you_go"])
    uefi = _item(result, "vc:lnx-uefi")
    bios = _item(result, "vc:lnx-bios")
    assert uefi["figures"]["size.allocated"]["inputs"]["size"] == "Standard_D2_v6"
    assert uefi["figures"]["compute.allocated.pay_as_you_go"]["inputs"]["meter_id"] == (
        "meter-Standard_D2_v6-linux"
    )
    assert _value(uefi, "compute.allocated.pay_as_you_go") == round(0.090 * 730, 2)
    # 40 GB -> E6 (64 GiB) on Standard SSD: the size has no Premium storage.
    assert uefi["figures"]["disk.0.allocated"]["inputs"]["tier"] == "E6"
    # A BIOS machine needs a generation 1 size.
    assert bios["figures"]["size.allocated"]["inputs"]["size"] == "Standard_D2s_v5"


async def test_missing_utilization_makes_right_sizing_unknown(store: ResultHandleStore) -> None:
    result = await _assess(store, [LINUX_UEFI], pricing=["pay_as_you_go"])
    item = _item(result, "vc:lnx-uefi")
    measured = item["figures"]["size.measured"]
    assert measured["value"] is None
    assert measured["unknown_reason"] == "no utilization was measured"
    assert "right_sized_unknown" in item["flags"]
    assert "monthly.measured.pay_as_you_go" not in item["figures"]
    assert {
        "source_id": "vc:lnx-uefi",
        "what": "right_sized",
        "reason": "no utilization was measured",
    } in result["unknowns"]
    # The allocated estimate is still there, and says so.
    assert item["figures"]["size.allocated"]["basis"] == "allocated"


@pytest.mark.parametrize(
    ("start", "samples", "interval", "reason"),
    [
        ("2026-10-15T08:00:00Z", 1440, 5, "shorter than 7 days"),
        ("2026-09-20T08:00:00Z", 100, 120, "cover less than 80%"),
        # 300 samples would be 83% of a 2-hour source, but a 5-minute source
        # takes 8640 samples in 30 days: 300 cover less than 4%.
        ("2026-09-20T08:00:00Z", 300, 5, "cover less than 80% of the window (3%"),
        ("2026-09-20T08:00:00Z", 8640, None, "does not say how often it sampled"),
    ],
)
async def test_too_few_measurements_make_right_sizing_unknown(
    store: ResultHandleStore, start: str, samples: int, interval: int | None, reason: str
) -> None:
    utilization = _utilization(30.0, 35.0, start=start, samples=samples, interval=interval)
    server = _server(
        "vc:win-01",
        "app-win-01",
        cores=4,
        memory_mb=16384,
        family="windows",
        boot="uefi",
        disks=[_disk("Hard disk 1", 100)],
        utilization=utilization,
    )
    result = await _assess(store, [server], pricing=["pay_as_you_go"])
    item = _item(result, "vc:win-01")
    measured = item["figures"]["size.measured"]
    assert measured["value"] is None
    assert reason in measured["unknown_reason"]
    assert "right_sized_unknown" in item["flags"]


async def test_older_inventory_without_a_sample_interval_is_sized_by_allocation(
    store: ResultHandleStore,
) -> None:
    """A version-1 inventory written before the sample interval existed still assesses."""
    utilization = dict(WINDOWS["utilization"])
    del utilization["sample_interval_minutes"]
    older = {**WINDOWS, "utilization": utilization}
    result = await _assess(store, [older], pricing=["pay_as_you_go"])
    item = _item(result, "vc:win-01")
    assert item["figures"]["size.allocated"]["inputs"]["size"] == "Standard_D4s_v5"
    assert item["figures"]["compute.allocated.pay_as_you_go"]["value"] == 274.48
    measured = item["figures"]["size.measured"]
    assert measured["value"] is None
    assert "does not say how often it sampled" in measured["unknown_reason"]
    assert "right_sized_unknown" in item["flags"]


async def test_coverage_uses_the_source_sample_interval(store: ResultHandleStore) -> None:
    """The same 300 samples over 30 days are enough for a 2-hour source, not for a 5-minute one."""
    sized: dict[int, Any] = {}
    for interval in (120, 5):
        utilization = _utilization(
            30.0, 35.0, start="2026-09-20T08:00:00Z", samples=300, interval=interval
        )
        result = await _assess(
            store, [{**WINDOWS, "utilization": utilization}], pricing=["pay_as_you_go"]
        )
        sized[interval] = _item(result, "vc:win-01")["figures"]["size.measured"]
    assert sized[120]["value"] == 2
    assert sized[120]["inputs"]["expected_samples"] == 360
    assert sized[120]["inputs"]["sample_coverage"] == 0.83
    assert sized[5]["value"] is None
    assert "8640 samples" in sized[5]["unknown_reason"]


async def test_vm_bigger_than_any_size_is_not_ready(store: ResultHandleStore) -> None:
    result = await _assess(store, [HUGE, LINUX_BIOS], pricing=["pay_as_you_go"])
    huge = _item(result, "vc:huge")
    assert huge["figures"]["size.allocated"]["value"] is None
    assert (
        "no size in westeurope offers at least 128 vCPUs"
        in (huge["figures"]["size.allocated"]["unknown_reason"])
    )
    assert "not_ready" in huge["flags"]
    assert "compute.allocated.pay_as_you_go" not in huge["figures"]
    assert any(
        u["source_id"] == "vc:huge" and u["what"] == "size.allocated" for u in result["unknowns"]
    )

    totals = result["totals"]["pay_as_you_go"]
    assert totals["allocated"]["value"] is None
    assert totals["allocated"]["unknown_reason"] == "1 of 2 servers have no price"
    bios_monthly = _value(_item(result, "vc:lnx-bios"), "monthly.allocated.pay_as_you_go")
    assert totals["allocated_priced_servers"]["value"] == bios_monthly
    assert totals["allocated_priced_servers"]["inputs"]["servers_not_priced"] == 1


async def test_totals_per_pricing_option(store: ResultHandleStore) -> None:
    result = await _assess(store, [WINDOWS, LINUX_BIOS])
    assert set(result["totals"]) == {"pay_as_you_go", "reserved_1_year", "reserved_3_years"}
    for option, total in result["totals"].items():
        expected = round(
            _value(_item(result, "vc:win-01"), f"monthly.allocated.{option}")
            + _value(_item(result, "vc:lnx-bios"), f"monthly.allocated.{option}"),
            2,
        )
        assert total["allocated"]["value"] == expected
        assert total["measured_priced_servers"]["inputs"]["servers_priced"] == 1
    assumptions = result["assumptions"]
    assert assumptions["region"] == _REGION
    assert assumptions["currency"] == "EUR"
    assert assumptions["hours_per_month"] == 730
    assert assumptions["reservation"] == "none, 1-year, 3-year"
    assert assumptions["comfort_factor"] == 1.3
    assert assumptions["price_date"]
    assert "Windows license included" in assumptions["license"]
    assert result["calculation_version"] == "migration-assess-vm-1"
    assert result["inputs"]["targets"] == ["azure-example"]


async def test_hybrid_benefit_prices_windows_at_the_linux_meter(store: ResultHandleStore) -> None:
    result = await _assess(store, [WINDOWS], hybrid_benefit=True)
    figures = _item(result, "vc:win-01")["figures"]
    assert figures["compute.allocated.pay_as_you_go"]["inputs"]["meter_id"] == (
        "meter-Standard_D4s_v5-linux"
    )
    assert figures["compute.allocated.reserved_1_year"]["value"] == round(1000 / 12, 2)
    assert "Hybrid Benefit" in result["assumptions"]["license"]


async def test_size_family_allow_list(store: ResultHandleStore) -> None:
    result = await _assess(store, [WINDOWS], pricing=["pay_as_you_go"], size_families=["ESv5"])
    figures = _item(result, "vc:win-01")["figures"]
    # Only zones are restricted for Standard_E4s_v5, so it can be used.
    assert figures["size.allocated"]["inputs"]["size"] == "Standard_E4s_v5"
    assert figures["size.measured"]["inputs"]["size"] == "Standard_E4s_v5"
    assert result["assumptions"]["other"]["size_families"] == "ESv5"
    assert family_key("standardESv5Family") == family_key("ESv5") == family_key("esv5")


async def test_unknown_os_family_and_disks_stay_unknown(store: ResultHandleStore) -> None:
    server = _server(
        "vc:unknown",
        "misc-01",
        cores=2,
        memory_mb=8192,
        family=None,
        boot=None,
        disks=None,
    )
    result = await _assess(store, [server], pricing=["pay_as_you_go"])
    item = _item(result, "vc:unknown")
    assert item["figures"]["size.allocated"]["value"] == 2
    assert item["figures"]["size.allocated"]["inputs"]["need_data_disks"] is None
    compute = item["figures"]["compute.allocated.pay_as_you_go"]
    assert compute["value"] is None
    assert "OS family is unknown" in compute["unknown_reason"]
    assert item["figures"]["disks.allocated"]["value"] is None
    assert item["figures"]["monthly.allocated.pay_as_you_go"]["value"] is None
    assert {"os_family_unknown", "boot_type_unknown_generation_not_checked"} <= set(item["flags"])
    assert "disks_unknown_data_disk_limit_not_checked" in item["flags"]


async def test_other_os_family_is_priced_as_linux_and_says_so(store: ResultHandleStore) -> None:
    other = _server(
        "vc:other",
        "bsd-01",
        cores=2,
        memory_mb=8192,
        family="other",
        boot="bios",
        disks=[_disk("Hard disk 1", 40)],
    )
    result = await _assess(store, [other], pricing=["pay_as_you_go"])
    item = _item(result, "vc:other")
    compute = item["figures"]["compute.allocated.pay_as_you_go"]
    assert compute["inputs"]["os_family"] == "other"
    assert compute["inputs"]["priced_as"] == "linux"
    assert compute["inputs"]["meter_id"] == "meter-Standard_D2s_v5-linux"
    assert "os_family_other_priced_as_linux" in item["flags"]
    assert "Linux price" in result["assumptions"]["other"]["os_family_other"]
    linux = _item(await _assess(store, [LINUX_BIOS], pricing=["pay_as_you_go"]), "vc:lnx-bios")
    assert "os_family_other_priced_as_linux" not in linux["flags"]
    assert linux["figures"]["compute.allocated.pay_as_you_go"]["inputs"]["os_family"] == "linux"


async def test_same_machine_twice_is_sized_once(store: ResultHandleStore) -> None:
    uuid = "4203c3d4-2222-4000-8000-0000000000b2"
    vcenter = {**LINUX_BIOS, "identity_hints": {"bios_uuid": uuid}}
    vcenter["missing"] = {
        k: v for k, v in vcenter["missing"].items() if k != "identity_hints.bios_uuid"
    }
    guest = {**vcenter, "source_id": "linux:web-lnx-02"}
    result = await _assess(store, [vcenter, guest], pricing=["pay_as_you_go"])
    duplicate = _item(result, "linux:web-lnx-02")
    assert duplicate["flags"] == ["duplicate_of:vc:lnx-bios"]
    assert duplicate["figures"] == {}
    assert result["totals"]["pay_as_you_go"]["allocated"]["inputs"]["servers_priced"] == 1


async def test_child_calls_use_the_azure_reads(store: ResultHandleStore) -> None:
    azure = _FakeAzure()
    await _assess(store, [LINUX_BIOS], azure, currency="EUR")
    calls = [(op, params) for op, params, _target in azure.calls]
    assert calls[0] == ("azure.vm.size.list", {"location": _REGION})
    assert (
        "azure.price.list",
        {
            "region": _REGION,
            "currency": "EUR",
            "service_name": "Virtual Machines",
            "price_type": "Reservation",
        },
    ) in calls
    assert {target for _op, _params, target in azure.calls} == {"azure-example"}
    assert len(calls) == 5

    pay_only = _FakeAzure()
    await _assess(store, [LINUX_BIOS], pay_only, pricing=["pay_as_you_go"])
    assert all(params.get("price_type") != "Reservation" for _op, params, _t in pay_only.calls)


async def test_a_failed_child_read_stops_with_a_clear_message(store: ResultHandleStore) -> None:
    with pytest.raises(AssessmentReadError, match=r"azure.vm.size.list did not succeed .*403"):
        await _assess(store, [LINUX_BIOS], _FakeAzure(fail="azure.vm.size.list"))


async def test_rows_under_value_are_read_like_items(store: ResultHandleStore) -> None:
    """The Azure paging helper keeps the rows under ``value``, as Azure's own answers do."""
    result = await _assess(
        store, [LINUX_BIOS], _FakeAzure(rows_key="value"), pricing=["pay_as_you_go"]
    )
    assert _value(_item(result, "vc:lnx-bios"), "compute.allocated.pay_as_you_go") == round(
        0.096 * 730, 2
    )


class _HandleAzure(_FakeAzure):
    """Returns every child result behind a handle, as large real results come back."""

    def __init__(
        self,
        store: ResultHandleStore,
        tenant: UUID,
        max_spill_rows: int | None,
        *,
        truncated: str | None = None,
    ) -> None:
        super().__init__(truncated=truncated)
        self.store = store
        self.tenant = tenant
        self.max_spill_rows = max_spill_rows

    async def __call__(
        self, *, connector_id: str, op_id: str, params: dict[str, Any], target: Any = None
    ) -> OperationResult:
        plain = await super().__call__(
            connector_id=connector_id, op_id=op_id, params=params, target=target
        )
        reducer = JsonFluxReducer(
            byte_threshold=256, store=self.store, max_spill_rows=self.max_spill_rows
        )
        reduced, handle = await reducer.reduce(
            plain.result,
            None,
            context={"tenant_id": str(self.tenant), "operator_sub": _SUB, "op_id": op_id},
        )
        assert handle is not None
        return OperationResult(
            status="ok", op_id=op_id, result=reduced, handle=handle, duration_ms=1.0
        )


async def _assess_with_handles(
    store: ResultHandleStore, max_spill_rows: int | None = None, *, truncated: str | None = None
) -> dict[str, Any]:
    tenant = uuid4()
    handle = await _store(store, tenant, [WINDOWS])
    return await migration_assess_vm(
        _operator(tenant),
        None,
        {"inventory_handles": [handle], "azure_target": "azure-example", "region": _REGION},
        _HandleAzure(store, tenant, max_spill_rows, truncated=truncated),
    )


async def test_child_result_stored_behind_a_handle_is_read_back(store: ResultHandleStore) -> None:
    result = await _assess_with_handles(store)
    assert _value(_item(result, "vc:win-01"), "compute.allocated.pay_as_you_go") == 274.48


async def test_child_result_stored_only_in_part_stops_the_operation(
    store: ResultHandleStore,
) -> None:
    # The store keeps only one row of each child list, so a cheaper size could be missing.
    with pytest.raises(
        AssessmentReadError, match=r"azure.vm.size.list returned \d+ rows but only 1 were stored"
    ):
        await _assess_with_handles(store, max_spill_rows=1)


@pytest.mark.parametrize("behind_handle", [False, True], ids=["inline", "behind-a-handle"])
async def test_a_child_read_cut_short_by_azure_stops_the_operation(
    store: ResultHandleStore, behind_handle: bool
) -> None:
    """Azure can stop paging before the end; the stored rows then look complete but are not."""
    with pytest.raises(
        AssessmentReadError,
        match=r"azure.price.list did not read the whole list.*more rows than one result holds",
    ):
        if behind_handle:
            await _assess_with_handles(store, truncated="azure.price.list")
        else:
            await _assess(store, [WINDOWS], _FakeAzure(truncated="azure.price.list"))


async def test_a_large_assessment_is_stored_whole_and_read_back(store: ResultHandleStore) -> None:
    """Reduced like the dispatcher does it, the result keeps every part, not only ``items``."""
    result = await _assess(store, [WINDOWS, LINUX_UEFI, HUGE])
    tenant = uuid4()
    context = {
        "tenant_id": str(tenant),
        "operator_sub": _SUB,
        "op_id": MIGRATION_ASSESS_VM_OP_ID,
        "result_document": _LLM_INSTRUCTIONS["result_document"],
    }
    summary, handle = await JsonFluxReducer(byte_threshold=256, store=store).reduce(
        result, None, context
    )
    assert handle is not None
    assert summary["result_record"] == "document"
    assert summary["calculation_version"] == "migration-assess-vm-1"

    operator = _operator(tenant)

    async def read(path: str, **paging: int) -> dict[str, Any]:
        return await run_result_query(
            operator, handle.handle_id, ResultQuerySpec(path=path), **paging
        )

    for key in ("totals", "assumptions", "inputs"):
        assert (await read(f"/{key}"))["value"] == result[key]
    assert (await read("/totals/pay_as_you_go/allocated/unknown_reason"))["value"] == (
        "1 of 3 servers have no price"
    )
    unknowns = await read("/unknowns", offset=0, limit=50)
    assert unknowns["rows"] == result["unknowns"]
    assert unknowns["total_rows"] == len(result["unknowns"]) >= 2
    assert (await read("/omissions"))["rows"] == result["omissions"] == []
    page = await read("/items", offset=1, limit=1)
    assert page["rows"] == [result["items"][1]]
    assert page["total_rows"] == 3


def test_operation_contract() -> None:
    assert write_word_in_op_id(MIGRATION_ASSESS_VM_OP_ID) is None
    for example in MIGRATION_ASSESS_VM_PARAMETER_SCHEMA["examples"]:
        assert validate_params(MIGRATION_ASSESS_VM_PARAMETER_SCHEMA, example) == []
    assert "totals" in MIGRATION_ASSESS_VM_RESPONSE_SCHEMA["properties"]
    assert "Figure" in MIGRATION_ASSESS_VM_RESPONSE_SCHEMA["$defs"]
    # The result is one document; the reducer must keep it whole.
    assert _LLM_INSTRUCTIONS["result_document"] == {"keep_whole": True}
