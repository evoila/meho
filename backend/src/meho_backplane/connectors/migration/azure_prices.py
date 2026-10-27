# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read Azure retail price rows and find the meter for a VM size or a managed disk.

The rows come from the Azure connector's ``azure.price.list`` read, which
reads Azure's public Retail Prices API. This module only picks rows; it
calls nothing.

Facts about the price rows (checked against the live API):

* A VM size has one pay-as-you-go row per operating system. A Windows row
  has ``Windows`` in its product name; the Linux row has not. Spot and Low
  Priority rows carry those words in the SKU or meter name. Cloud Services
  and Dedicated Host rows share the VM SKU names and are left out.
* A reservation row (price type ``Reservation``) has the price of the
  **whole term** in ``unit_price``, even though its unit says ``1 Hour``.
  Reservation rows cover compute only: there is no Windows reservation row.
* A managed disk tier has a monthly row named ``<tier> LRS Disk`` (for
  example ``P10 LRS Disk``), unit ``1/Month``.
* Prices in another currency than USD are reference values, not quotes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "DISK_TIERS_GIB",
    "ComputePrices",
    "DiskPrices",
    "DiskType",
    "OsFamily",
    "PriceRow",
    "disk_tier",
    "parse_price_rows",
]

OsFamily = Literal["linux", "windows"]
DiskType = Literal["standard_ssd", "premium_ssd"]

#: Disk tiers of Standard SSD (E) and Premium SSD (P) managed disks and their
#: size in GiB, smallest first. Source: Microsoft, "Select a disk type for
#: Azure IaaS VMs", tables "Standard SSD size" and "Premium SSD size".
DISK_TIERS_GIB: Final[tuple[tuple[str, int], ...]] = (
    ("1", 4),
    ("2", 8),
    ("3", 16),
    ("4", 32),
    ("6", 64),
    ("10", 128),
    ("15", 256),
    ("20", 512),
    ("30", 1024),
    ("40", 2048),
    ("50", 4096),
    ("60", 8192),
    ("70", 16384),
    ("80", 32767),
)

DISK_PRODUCTS: Final[dict[DiskType, tuple[str, str]]] = {
    "standard_ssd": ("Standard SSD Managed Disks", "E"),
    "premium_ssd": ("Premium SSD Managed Disks", "P"),
}

_LEFT_OUT_PRODUCT_WORDS: Final[tuple[str, ...]] = (
    "cloud services",
    "cloudservices",
    "dedicated host",
)
_LEFT_OUT_SKU_WORDS: Final[tuple[str, ...]] = ("spot", "low priority")


@dataclass(frozen=True)
class PriceRow:
    """One price row, with the fields an assessment shows as evidence."""

    meter_id: str
    meter_name: str
    product_name: str
    sku_name: str
    arm_sku_name: str
    unit_price: float
    unit: str
    price_type: str
    reservation_term: str | None
    effective_start_date: str | None

    def evidence(self, prefix: str = "") -> dict[str, str | float | None]:
        """The row as figure inputs: meter, price, unit and effective date."""
        return {
            f"{prefix}meter_id": self.meter_id,
            f"{prefix}meter_name": self.meter_name,
            f"{prefix}product_name": self.product_name,
            f"{prefix}unit_price": self.unit_price,
            f"{prefix}unit": self.unit,
            f"{prefix}price_type": self.price_type,
            f"{prefix}effective_start_date": self.effective_start_date,
        }


def _text(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    return value.strip() if isinstance(value, str) else ""


def parse_price_rows(items: Iterable[Any]) -> list[PriceRow]:
    """Turn ``azure.price.list`` items into rows. Items without a meter id or price are skipped."""
    rows: list[PriceRow] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        price = item.get("unit_price")
        meter_id = _text(item, "meter_id")
        if not meter_id or isinstance(price, bool) or not isinstance(price, int | float):
            continue
        rows.append(
            PriceRow(
                meter_id=meter_id,
                meter_name=_text(item, "meter_name"),
                product_name=_text(item, "product_name"),
                sku_name=_text(item, "sku_name"),
                arm_sku_name=_text(item, "arm_sku_name"),
                unit_price=float(price),
                unit=_text(item, "unit"),
                price_type=_text(item, "price_type"),
                reservation_term=_text(item, "reservation_term") or None,
                effective_start_date=_text(item, "effective_start_date") or None,
            )
        )
    return rows


def _newer(current: PriceRow | None, candidate: PriceRow) -> bool:
    """Keep the row with the latest effective date; the meter id breaks a tie."""
    if current is None:
        return True
    return (candidate.effective_start_date or "", candidate.meter_id) > (
        current.effective_start_date or "",
        current.meter_id,
    )


def _is_vm_row(row: PriceRow) -> bool:
    product = row.product_name.lower()
    sku = f"{row.sku_name} {row.meter_name}".lower()
    return (
        bool(row.arm_sku_name)
        and not any(word in product for word in _LEFT_OUT_PRODUCT_WORDS)
        and not any(word in sku for word in _LEFT_OUT_SKU_WORDS)
    )


class ComputePrices:
    """Pay-as-you-go and reservation rows of VM sizes in one region, by size name."""

    def __init__(self, rows: Iterable[PriceRow]) -> None:
        self._hourly: dict[tuple[str, OsFamily], PriceRow] = {}
        self._reserved: dict[tuple[str, str], PriceRow] = {}
        for row in rows:
            if not _is_vm_row(row):
                continue
            size = row.arm_sku_name.lower()
            if row.price_type == "Consumption" and row.unit == "1 Hour":
                os_family: OsFamily = (
                    "windows" if "windows" in row.product_name.lower() else "linux"
                )
                key = (size, os_family)
                if _newer(self._hourly.get(key), row):
                    self._hourly[key] = row
            elif row.price_type == "Reservation" and row.reservation_term:
                term_key = (size, row.reservation_term.lower())
                if _newer(self._reserved.get(term_key), row):
                    self._reserved[term_key] = row

    def hourly(self, size: str, os_family: OsFamily) -> PriceRow | None:
        """The pay-as-you-go row of *size* for *os_family*, or ``None``."""
        return self._hourly.get((size.lower(), os_family))

    def reserved(self, size: str, term: str) -> PriceRow | None:
        """The reservation row of *size* for *term* (``1 Year`` or ``3 Years``), or ``None``."""
        return self._reserved.get((size.lower(), term.lower()))


def disk_tier(size_gib: float) -> tuple[str, int] | None:
    """The smallest tier number and size that holds *size_gib*, or ``None`` when none does."""
    for number, tier_gib in DISK_TIERS_GIB:
        if tier_gib >= size_gib:
            return number, tier_gib
    return None


class DiskPrices:
    """Monthly rows of Standard SSD and Premium SSD managed disk tiers (LRS)."""

    def __init__(self, rows: Iterable[PriceRow]) -> None:
        self._monthly: dict[tuple[DiskType, str], PriceRow] = {}
        for row in rows:
            if row.price_type != "Consumption" or row.unit != "1/Month":
                continue
            for disk_type, (product, letter) in DISK_PRODUCTS.items():
                if row.product_name != product:
                    continue
                for number, _gib in DISK_TIERS_GIB:
                    if row.meter_name == f"{letter}{number} LRS Disk":
                        key = (disk_type, number)
                        if _newer(self._monthly.get(key), row):
                            self._monthly[key] = row

    def monthly(self, disk_type: DiskType, tier_number: str) -> PriceRow | None:
        """The monthly row of one tier, or ``None``."""
        return self._monthly.get((disk_type, tier_number))
