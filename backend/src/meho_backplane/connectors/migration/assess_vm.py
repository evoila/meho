# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``migration.assess.vm`` handler: size every server and estimate its monthly cost.

A multi-step operation. It reads the caller's stored inventories, then runs
the Azure connector's own reads as child calls (each one audited under this
operation):

1. ``azure.vm.size.list`` for the region: the VM sizes and their
   restrictions for the subscription of ``azure_target``.
2. ``azure.price.list``: the pay-as-you-go VM prices of the region, the
   reservation prices when a reserved option is asked for, and the Standard
   SSD and Premium SSD managed disk prices.

It changes nothing. A child read that fails stops the operation with a clear
message; a missing value inside the data becomes "unknown" with the reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from meho_backplane.connectors.azure.connector import AzureConnector
from meho_backplane.connectors.migration.assess_vm_figures import (
    CALCULATION_VERSION,
    HOURS_PER_MONTH,
    PRICING_OPTIONS,
    Basis,
    PricingOption,
    SizingContext,
    assess_server,
)
from meho_backplane.connectors.migration.azure_prices import (
    DISK_PRODUCTS,
    ComputePrices,
    DiskPrices,
    PriceRow,
    parse_price_rows,
)
from meho_backplane.connectors.migration.contracts import (
    AssessmentInputs,
    AssessmentItem,
    AssessmentResult,
    Assumptions,
    Figure,
    InventoryInput,
    Omission,
    ServerRecord,
    Unknown,
)
from meho_backplane.connectors.migration.inventory_load import LoadedInventory, load_inventories
from meho_backplane.connectors.migration.vm_sizing import (
    EXPECTED_SAMPLE_INTERVAL_MINUTES,
    MIN_SAMPLE_COVERAGE,
    MIN_WINDOW_DAYS,
    allocated_need,
    family_key,
    parse_sizes,
)
from meho_backplane.connectors.result_handle_store import (
    ResultRecordKindError,
    get_result_handle_store,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.schemas import OperationResult
    from meho_backplane.operations.composite import DispatchChild

__all__ = [
    "AZURE_CONNECTOR_ID",
    "PRICE_LIST_OP_ID",
    "SIZE_LIST_OP_ID",
    "AssessmentReadError",
    "migration_assess_vm",
]

AZURE_CONNECTOR_ID: Final[str] = f"{AzureConnector.impl_id}-{AzureConnector.version}"
SIZE_LIST_OP_ID: Final[str] = "azure.vm.size.list"
PRICE_LIST_OP_ID: Final[str] = "azure.price.list"

_MAX_ERROR_CHARS: Final[int] = 300
_RESERVATION_TEXT: Final[dict[PricingOption, str]] = {
    "pay_as_you_go": "none",
    "reserved_1_year": "1-year",
    "reserved_3_years": "3-year",
}


class AssessmentReadError(RuntimeError):
    """A child read failed or returned something this operation cannot use."""


async def _child_items(
    operator: Operator,
    dispatch_child: DispatchChild,
    *,
    target: str,
    op_id: str,
    params: dict[str, Any],
) -> list[Any]:
    """Run one Azure read and return its ``items``, read back from the stored result if needed."""
    result: OperationResult = await dispatch_child(
        connector_id=AZURE_CONNECTOR_ID, op_id=op_id, params=params, target=target
    )
    if result.status != "ok":
        detail = (result.error or result.status)[:_MAX_ERROR_CHARS]
        raise AssessmentReadError(f"{op_id} did not succeed ({result.status}): {detail}")
    if result.handle is None:
        payload = result.result
        items = payload.get("items") if isinstance(payload, Mapping) else payload
        if not isinstance(items, list):
            raise AssessmentReadError(f"{op_id} returned no list of items")
        return items
    try:
        stored = await get_result_handle_store().fetch_rows(
            tenant_id=operator.tenant_id,
            operator_sub=operator.sub,
            handle_id=UUID(str(result.handle.handle_id)),
        )
    except ResultRecordKindError:
        raise AssessmentReadError(f"{op_id} returned a stored result without rows") from None
    if stored is None:
        raise AssessmentReadError(f"the stored result of {op_id} was not found; run it again")
    if stored.stored_rows < stored.total_rows:
        raise AssessmentReadError(
            f"{op_id} returned {stored.total_rows} rows but only {stored.stored_rows} were "
            "stored, so a cheapest size could be missed; narrow the region or the families"
        )
    return list(stored.rows)


async def _prices(
    operator: Operator,
    dispatch_child: DispatchChild,
    *,
    target: str,
    filters: dict[str, str],
) -> list[PriceRow]:
    items = await _child_items(
        operator, dispatch_child, target=target, op_id=PRICE_LIST_OP_ID, params=filters
    )
    return parse_price_rows(items)


def _options(params: Mapping[str, Any]) -> tuple[PricingOption, ...]:
    asked = params.get("pricing") or list(PRICING_OPTIONS)
    return tuple(option for option in PRICING_OPTIONS if option in asked)


def _assumptions(
    params: Mapping[str, Any], options: tuple[PricingOption, ...], generated_at: datetime
) -> Assumptions:
    hybrid = bool(params.get("hybrid_benefit", False))
    families = params.get("size_families")
    return Assumptions(
        region=params["region"],
        currency=params.get("currency", "EUR"),
        price_date=generated_at.date(),
        hours_per_month=HOURS_PER_MONTH,
        license=(
            "Azure Hybrid Benefit: Windows servers use the Linux price"
            if hybrid
            else "Windows license included: Windows servers use the Windows price"
        ),
        reservation=", ".join(_RESERVATION_TEXT[option] for option in options),
        comfort_factor=float(params.get("comfort_factor", 1.3)),
        other={
            "pricing_options": ", ".join(options),
            "price_source": (
                "Azure Retail Prices API, read when the assessment ran; prices in a "
                "currency other than USD are reference values, not quotes"
            ),
            "percentile": "p95",
            "min_window_days": MIN_WINDOW_DAYS,
            "min_sample_coverage": MIN_SAMPLE_COVERAGE,
            "expected_sample_interval_minutes": EXPECTED_SAMPLE_INTERVAL_MINUTES,
            "os_family_other": (
                "servers with OS family 'other' use the Linux price; their own license "
                "is not counted"
            ),
            "os_disk": "the first disk is the OS disk; the others are data disks",
            "disk_type": (
                "Premium SSD when the VM size supports Premium storage, else Standard SSD; "
                "LRS; disk transactions and snapshots are not counted"
            ),
            "disk_size_unit": "disk sizes in GB are read as GiB",
            "reserved_monthly": "the reservation price is for the whole term; divided by 12 or 36",
            "windows_license_on_reserved": (
                "reservations cover compute only; the Windows license is the pay-as-you-go "
                "Windows price minus the Linux price, times the hours per month"
            ),
            "size_families": ", ".join(families) if families else "all",
        },
    )


def _servers(
    loaded: LoadedInventory,
) -> tuple[list[ServerRecord], list[tuple[ServerRecord, str]]]:
    """Servers to size, and every other record of the same machine with the one sized instead.

    Of the records of one machine (same BIOS UUID), the first that has cores
    and configured memory is sized; the others are listed as duplicates.
    """
    same_as = {d.source_id: d.same_as for d in loaded.duplicates if d.match == "bios_uuid"}
    groups: dict[str, list[ServerRecord]] = {}
    seen: set[str] = set()
    for record in loaded.records:
        if not isinstance(record, ServerRecord) or record.source_id in seen:
            continue
        seen.add(record.source_id)
        groups.setdefault(same_as.get(record.source_id, record.source_id), []).append(record)
    sized: list[ServerRecord] = []
    duplicates: list[tuple[ServerRecord, str]] = []
    for records in groups.values():
        primary = next((r for r in records if not isinstance(allocated_need(r), str)), records[0])
        sized.append(primary)
        duplicates.extend((r, primary.source_id) for r in records if r is not primary)
    return sized, duplicates


def _omissions(loaded: LoadedInventory) -> list[Omission]:
    omissions = [omission for envelope in loaded.envelopes for omission in envelope.omissions]
    skipped: dict[str, int] = {}
    for record in loaded.records:
        if not isinstance(record, ServerRecord):
            skipped[record.kind] = skipped.get(record.kind, 0) + 1
    omissions.extend(
        Omission(
            what=f"{count} {kind} records",
            reason="not_supported",
            detail="migration.assess.vm sizes servers only",
        )
        for kind, count in sorted(skipped.items())
    )
    return omissions


def _total(items: list[AssessmentItem], key: str, unit: str, basis: Basis) -> tuple[Figure, Figure]:
    """The total over all servers (unknown if any is unknown) and over the priced servers."""
    known = [item.figures[key].value for item in items if key in item.figures]
    values = [value for value in known if value is not None]
    missing = len(items) - len(values)
    inputs: dict[str, str | float | int | bool | None] = {
        "figure": key,
        "servers_priced": len(values),
        "servers_not_priced": missing,
    }
    priced = Figure(
        value=round(sum(values), 2),
        unit=unit,
        basis=basis,
        inputs=inputs,
        calculation_version=CALCULATION_VERSION,
    )
    whole = Figure(
        value=priced.value if not missing else None,
        unit=unit,
        basis=basis,
        inputs=inputs,
        calculation_version=CALCULATION_VERSION,
        unknown_reason=f"{missing} of {len(items)} servers have no price" if missing else None,
    )
    return whole, priced


def _totals(
    items: list[AssessmentItem], options: tuple[PricingOption, ...], unit: str
) -> dict[str, dict[str, dict[str, Any]]]:
    totals: dict[str, dict[str, dict[str, Any]]] = {}
    for option in options:
        allocated, allocated_priced = _total(
            items, f"monthly.allocated.{option}", unit, "allocated"
        )
        _whole, measured_priced = _total(items, f"monthly.measured.{option}", unit, "measured")
        totals[option] = {
            "allocated": allocated.model_dump(mode="json"),
            "allocated_priced_servers": allocated_priced.model_dump(mode="json"),
            "measured_priced_servers": measured_priced.model_dump(mode="json"),
        }
    return totals


async def _context(
    operator: Operator,
    dispatch_child: DispatchChild,
    params: Mapping[str, Any],
    options: tuple[PricingOption, ...],
) -> SizingContext:
    target = str(params["azure_target"])
    region = str(params["region"])
    currency = str(params.get("currency", "EUR"))
    sizes = parse_sizes(
        await _child_items(
            operator,
            dispatch_child,
            target=target,
            op_id=SIZE_LIST_OP_ID,
            params={"location": region},
        )
    )
    base = {"region": region, "currency": currency}
    vm = {**base, "service_name": "Virtual Machines"}
    rows = await _prices(
        operator, dispatch_child, target=target, filters={**vm, "price_type": "Consumption"}
    )
    if any(option != "pay_as_you_go" for option in options):
        rows += await _prices(
            operator, dispatch_child, target=target, filters={**vm, "price_type": "Reservation"}
        )
    disk_rows: list[PriceRow] = []
    for product, _letter in DISK_PRODUCTS.values():
        disk_rows += await _prices(
            operator,
            dispatch_child,
            target=target,
            filters={
                **base,
                "service_name": "Storage",
                "product_name": product,
                "price_type": "Consumption",
            },
        )
    families = params.get("size_families")
    return SizingContext(
        region=region,
        currency=currency,
        options=options,
        hybrid_benefit=bool(params.get("hybrid_benefit", False)),
        comfort_factor=float(params.get("comfort_factor", 1.3)),
        families=frozenset(family_key(f) for f in families) if families else None,
        sizes=sizes,
        compute=ComputePrices(rows),
        disks=DiskPrices(disk_rows),
    )


async def migration_assess_vm(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Size and price every server of the caller's stored inventories.

    Targetless; *target* is always ``None``. The Azure reads run against
    ``params["azure_target"]``.
    """
    del target
    loaded = await load_inventories(operator, list(params["inventory_handles"]))
    options = _options(params)
    context = await _context(operator, dispatch_child, params, options)
    generated_at = datetime.now(UTC)

    servers, duplicates = _servers(loaded)
    items: list[AssessmentItem] = []
    unknowns: list[Unknown] = []
    for server in servers:
        item, server_unknowns = assess_server(context, server)
        items.append(item)
        unknowns.extend(server_unknowns)
    sized_items = list(items)
    items.extend(
        AssessmentItem(
            source_id=record.source_id,
            name=record.name or record.source_id,
            flags=[f"duplicate_of:{primary}"],
        )
        for record, primary in duplicates
    )

    result = AssessmentResult(
        schema_version="1",
        calculation_version=CALCULATION_VERSION,
        inputs=AssessmentInputs(
            inventories=[
                InventoryInput(
                    handle_id=handle_id,
                    connector_id=envelope.source.connector_id,
                    target=envelope.source.target,
                    collected_at=envelope.source.collected_at,
                )
                for handle_id, envelope in zip(loaded.handle_ids, loaded.envelopes, strict=True)
            ],
            targets=[str(params["azure_target"])],
            generated_at=generated_at,
        ),
        assumptions=_assumptions(params, options, generated_at),
        items=items,
        unknowns=unknowns,
        omissions=_omissions(loaded),
    )
    output = result.model_dump(mode="json")
    output["totals"] = _totals(sized_items, options, f"{context.currency}/month")
    return output
