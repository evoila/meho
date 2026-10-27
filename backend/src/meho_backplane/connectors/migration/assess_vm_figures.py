# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The figures of ``migration.assess.vm`` for one server.

For each way to size (``allocated``, and ``measured`` when there are enough
measurements) one server gets these figures, all per month in the chosen
currency unless the key says otherwise:

* ``size.<basis>``: the chosen size (value: its vCPUs) and why it was chosen.
* ``compute.<basis>.<pricing>``: the VM price for each pricing option.
* ``disk.<n>.<basis>``: one managed disk; ``disks.<basis>``: all disks.
* ``monthly.<basis>.<pricing>``: compute plus disks.

Every figure names the price rows it used (meter id, price, unit,
effective date) in ``inputs``. A value MEHO cannot work out is ``null``
with the reason, never ``0``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

from meho_backplane.connectors.migration.azure_prices import (
    DISK_PRODUCTS,
    DISK_TIERS_GIB,
    ComputePrices,
    DiskPrices,
    DiskType,
    OsFamily,
    PriceRow,
    disk_tier,
)
from meho_backplane.connectors.migration.contracts import (
    AssessmentItem,
    Figure,
    ServerRecord,
    Unknown,
)
from meho_backplane.connectors.migration.vm_sizing import (
    Need,
    VmSize,
    allocated_need,
    measured_need,
    pick_size,
)

__all__ = [
    "CALCULATION_VERSION",
    "HOURS_PER_MONTH",
    "PRICING_OPTIONS",
    "Basis",
    "PricingOption",
    "SizingContext",
    "assess_server",
]

CALCULATION_VERSION: Final[str] = "migration-assess-vm-1"
HOURS_PER_MONTH: Final[float] = 730.0

PricingOption = Literal["pay_as_you_go", "reserved_1_year", "reserved_3_years"]
Basis = Literal["allocated", "measured"]
ServerOs = Literal["windows", "linux", "other"]

PRICING_OPTIONS: Final[tuple[PricingOption, ...]] = (
    "pay_as_you_go",
    "reserved_1_year",
    "reserved_3_years",
)
#: Reservation term as Azure writes it, and its length in months.
RESERVATION_TERMS: Final[dict[PricingOption, tuple[str, int]]] = {
    "reserved_1_year": ("1 Year", 12),
    "reserved_3_years": ("3 Years", 36),
}

_PRICE_ASSUMPTIONS: Final[tuple[str, ...]] = (
    "region",
    "currency",
    "price_date",
    "hours_per_month",
    "license",
)
_RESERVED_ASSUMPTIONS: Final[tuple[str, ...]] = (
    "reservation",
    "reserved_monthly",
    "windows_license_on_reserved",
)
_MEASURED_ASSUMPTIONS: Final[tuple[str, ...]] = (
    "comfort_factor",
    "percentile",
    "min_window_days",
    "min_sample_coverage",
    "sample_coverage",
)
_DISK_ASSUMPTIONS: Final[tuple[str, ...]] = (
    "region",
    "currency",
    "price_date",
    "os_disk",
    "disk_type",
    "disk_size_unit",
)


@dataclass(frozen=True)
class SizingContext:
    """Everything one assessment run shares across servers."""

    region: str
    currency: str
    options: tuple[PricingOption, ...]
    hybrid_benefit: bool
    comfort_factor: float
    families: frozenset[str] | None
    sizes: list[VmSize]
    compute: ComputePrices
    disks: DiskPrices


@dataclass
class _Builder:
    """Collects the figures, flags and unknowns of one server."""

    context: SizingContext
    server: ServerRecord
    figures: dict[str, Figure] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    unknowns: list[Unknown] = field(default_factory=list)

    @property
    def unit(self) -> str:
        return f"{self.context.currency}/month"

    def known(
        self,
        key: str,
        value: float,
        *,
        basis: Basis | None,
        inputs: dict[str, str | float | int | bool | None],
        assumptions: tuple[str, ...],
        unit: str | None = None,
    ) -> Figure:
        figure = Figure(
            value=round(value, 2),
            unit=unit or self.unit,
            basis=basis,
            inputs=inputs,
            assumptions=list(assumptions),
            calculation_version=CALCULATION_VERSION,
        )
        self.figures[key] = figure
        return figure

    def unknown(
        self,
        key: str,
        reason: str,
        *,
        basis: Basis | None,
        inputs: dict[str, str | float | int | bool | None] | None = None,
        assumptions: tuple[str, ...] = (),
        unit: str | None = None,
    ) -> None:
        self.figures[key] = Figure(
            value=None,
            unit=unit or self.unit,
            basis=basis,
            inputs=inputs or {},
            assumptions=list(assumptions),
            calculation_version=CALCULATION_VERSION,
            unknown_reason=reason,
        )


def _os_family(server: ServerRecord) -> ServerOs | None:
    return server.os.family if server.os else None


def _meter_os(context: SizingContext, os_family: ServerOs | None) -> OsFamily:
    """The meter a size is ranked and priced by: Windows only for Windows without Hybrid Benefit.

    Linux, "other" and Windows with Hybrid Benefit use the Linux meter.
    """
    return "windows" if os_family == "windows" and not context.hybrid_benefit else "linux"


def _need_text(need: Need) -> str:
    parts = [f"at least {need.vcpus} vCPUs", f"at least {need.memory_gb:g} GB memory"]
    if need.data_disks is not None:
        parts.append(f"at least {need.data_disks} data disks")
    if need.generation is not None:
        parts.append(f"VM generation {need.generation}")
    return ", ".join(parts)


def _size(b: _Builder, basis: Basis, need: Need, os_family: ServerOs | None) -> VmSize | None:
    context = b.context
    meter_os = _meter_os(context, os_family)
    search = pick_size(
        context.sizes,
        need,
        lambda size: context.compute.hourly(size.name, meter_os),
        context.families,
    )
    # The measurements a measured need was made from come first, so the
    # figure shows the p95 values, the window, the samples and the source
    # next to the need they gave.
    counts: dict[str, str | float | int | bool | None] = {
        **need.evidence,
        "need_vcpus": need.vcpus,
        "need_memory_gb": round(need.memory_gb, 2),
        "need_data_disks": need.data_disks,
        "need_generation": need.generation,
        "fitting_sizes": search.fitting,
        "restricted_sizes_skipped": search.restricted_skipped,
        "unpriced_sizes_skipped": search.unpriced_skipped,
    }
    assumptions = ("region",) + (_MEASURED_ASSUMPTIONS if basis == "measured" else ())
    if search.size is None or search.price is None:
        reason = f"no size in {context.region} offers {_need_text(need)}"
        if search.restricted_skipped:
            reason += f" ({search.restricted_skipped} fitting sizes are restricted)"
        b.unknown(
            f"size.{basis}",
            reason,
            basis=basis,
            inputs=counts,
            assumptions=assumptions,
            unit="vCPUs",
        )
        return None
    size = search.size
    why = (
        f"cheapest of {search.fitting} sizes in {context.region} with {_need_text(need)}, "
        f"by the pay-as-you-go {meter_os.title()} price"
    )
    b.known(
        f"size.{basis}",
        size.vcpus,
        basis=basis,
        unit="vCPUs",
        inputs={
            "size": size.name,
            "family": size.family,
            "vcpus": size.vcpus,
            "memory_gb": size.memory_gb,
            "max_data_disks": size.max_data_disks,
            "premium_io": size.premium_io,
            "why": why,
            **counts,
        },
        assumptions=assumptions,
    )
    return size


def _compute(
    b: _Builder, basis: Basis, option: PricingOption, size: VmSize, os_family: ServerOs | None
) -> float | None:
    key = f"compute.{basis}.{option}"
    context = b.context
    if os_family is None:
        b.unknown(
            key,
            "the OS family is unknown, so MEHO cannot choose the Linux or Windows price",
            basis=basis,
        )
        return None
    meter_os = _meter_os(context, os_family)
    hourly = context.compute.hourly(size.name, meter_os)
    if hourly is None:
        b.unknown(key, f"no pay-as-you-go price for {size.name}", basis=basis)
        return None
    inputs: dict[str, str | float | int | bool | None] = {
        "size": size.name,
        "os_family": os_family,
        "priced_as": meter_os,
        "hybrid_benefit": context.hybrid_benefit,
        "pricing": option,
    }
    if option == "pay_as_you_go":
        inputs.update(hourly_price_per_hour=hourly.unit_price, **hourly.evidence())
        return b.known(
            key,
            hourly.unit_price * HOURS_PER_MONTH,
            basis=basis,
            inputs=inputs,
            assumptions=_PRICE_ASSUMPTIONS,
        ).value
    term, months = RESERVATION_TERMS[option]
    reserved = context.compute.reserved(size.name, term)
    if reserved is None:
        b.unknown(key, f"no {term} reservation price for {size.name}", basis=basis)
        return None
    value = reserved.unit_price / months
    inputs.update(reservation_term=term, months=months, **reserved.evidence())
    if os_family == "windows" and not context.hybrid_benefit:
        license_value = _windows_license(context.compute, size.name, inputs)
        if license_value is None:
            b.unknown(
                key,
                f"no Linux and Windows price to work out the license of {size.name}",
                basis=basis,
            )
            return None
        value += license_value
    return b.known(
        key,
        value,
        basis=basis,
        inputs=inputs,
        assumptions=_PRICE_ASSUMPTIONS + _RESERVED_ASSUMPTIONS,
    ).value


def _windows_license(
    compute: ComputePrices, size: str, inputs: dict[str, str | float | int | bool | None]
) -> float | None:
    """The Windows license per month: Windows minus Linux pay-as-you-go price, times the hours."""
    windows = compute.hourly(size, "windows")
    linux = compute.hourly(size, "linux")
    if windows is None or linux is None:
        return None
    license_value = (windows.unit_price - linux.unit_price) * HOURS_PER_MONTH
    inputs.update(
        windows_license_per_month=round(license_value, 2),
        **windows.evidence("license_windows_"),
        **linux.evidence("license_linux_"),
    )
    return license_value


def _disk_row(
    context: SizingContext, disk_type: DiskType, size_gb: float
) -> tuple[str, PriceRow] | str:
    """The tier name and its monthly row, or the reason there is none."""
    tier = disk_tier(size_gb)
    if tier is None:
        return f"larger than the largest managed disk ({DISK_TIERS_GIB[-1][1]} GiB)"
    name = f"{DISK_PRODUCTS[disk_type][1]}{tier[0]}"
    row = context.disks.monthly(disk_type, tier[0])
    return (name, row) if row is not None else f"no monthly price for {name} LRS"


def _disks(b: _Builder, basis: Basis, size: VmSize) -> float | None:
    """Price each disk; the total is unknown when any disk is."""
    disks = b.server.disks
    if disks is None:
        b.unknown(
            f"disks.{basis}", "the disks are unknown", basis=basis, assumptions=_DISK_ASSUMPTIONS
        )
        return None
    disk_type: DiskType = "premium_ssd" if size.premium_io else "standard_ssd"
    total = 0.0
    missing = 0
    for index, disk in enumerate(disks):
        key = f"disk.{index}.{basis}"
        if disk.size_gb is None:
            b.unknown(key, "the disk size is unknown", basis=basis, assumptions=_DISK_ASSUMPTIONS)
            missing += 1
            continue
        found = _disk_row(b.context, disk_type, disk.size_gb)
        if isinstance(found, str):
            b.unknown(key, found, basis=basis, assumptions=_DISK_ASSUMPTIONS)
            missing += 1
            continue
        tier, row = found
        total += (
            b.known(
                key,
                row.unit_price,
                basis=basis,
                inputs={
                    "label": disk.label,
                    "size_gb": disk.size_gb,
                    "disk_type": disk_type,
                    "tier": tier,
                    "os_disk": index == 0,
                    "vm_size": size.name,
                    **row.evidence(),
                },
                assumptions=_DISK_ASSUMPTIONS,
            ).value
            or 0.0
        )
    if missing:
        b.unknown(
            f"disks.{basis}",
            f"{missing} of {len(disks)} disks have no price",
            basis=basis,
            assumptions=_DISK_ASSUMPTIONS,
        )
        return None
    b.known(
        f"disks.{basis}",
        total,
        basis=basis,
        inputs={"disk_count": len(disks), "disk_type": disk_type},
        assumptions=_DISK_ASSUMPTIONS,
    )
    return total


def _price_sizing(b: _Builder, basis: Basis, size: VmSize, os_family: ServerOs | None) -> None:
    disks = _disks(b, basis, size)
    for option in b.context.options:
        compute = _compute(b, basis, option, size, os_family)
        key = f"monthly.{basis}.{option}"
        if compute is None or disks is None:
            b.unknown(key, "the compute or the disk price is unknown", basis=basis)
            continue
        b.known(
            key,
            compute + disks,
            basis=basis,
            inputs={
                "size": size.name,
                f"compute.{basis}.{option}": compute,
                f"disks.{basis}": disks,
            },
            assumptions=_PRICE_ASSUMPTIONS,
        )


def assess_server(
    context: SizingContext, server: ServerRecord
) -> tuple[AssessmentItem, list[Unknown]]:
    """Size and price one server; return its item and what could not be decided."""
    b = _Builder(context, server)
    sid = server.source_id
    os_family = _os_family(server)
    if os_family is None:
        b.flags.append("os_family_unknown")
        b.unknowns.append(
            Unknown(source_id=sid, what="compute price", reason="the OS family is unknown")
        )
    elif os_family == "other":
        b.flags.append("os_family_other_priced_as_linux")
    if server.boot_type is None:
        b.flags.append("boot_type_unknown_generation_not_checked")
    if server.disks is None:
        b.flags.append("disks_unknown_data_disk_limit_not_checked")

    allocated = allocated_need(server)
    if isinstance(allocated, str):
        b.unknown("size.allocated", allocated, basis="allocated", unit="vCPUs")
    else:
        size = _size(b, "allocated", allocated, os_family)
        if size is not None:
            _price_sizing(b, "allocated", size, os_family)
    allocated_size = b.figures["size.allocated"]
    if allocated_size.value is None:
        b.flags.append("not_ready")
        b.unknowns.append(
            Unknown(
                source_id=sid, what="size.allocated", reason=allocated_size.unknown_reason or ""
            )
        )

    measured = (
        allocated
        if isinstance(allocated, str)
        else measured_need(server, allocated, context.comfort_factor)
    )
    if isinstance(measured, str):
        b.flags.append("right_sized_unknown")
        b.unknown("size.measured", measured, basis="measured", unit="vCPUs")
        b.unknowns.append(Unknown(source_id=sid, what="right_sized", reason=measured))
    else:
        size = _size(b, "measured", measured, os_family)
        if size is None:
            b.flags.append("right_sized_unknown")
            reason = b.figures["size.measured"].unknown_reason or ""
            b.unknowns.append(Unknown(source_id=sid, what="right_sized", reason=reason))
        else:
            _price_sizing(b, "measured", size, os_family)

    item = AssessmentItem(source_id=sid, name=server.name or sid, figures=b.figures, flags=b.flags)
    return item, b.unknowns
