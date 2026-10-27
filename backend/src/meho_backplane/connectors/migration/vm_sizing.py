# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pick an Azure VM size for one server.

Two ways to size, both with the same match:

* **Allocated** (always): what the machine has today. vCPUs at least the
  cores, memory at least the configured memory.
* **Measured** (only with enough measurements): the 95th percentile of CPU
  and memory use, times the comfort factor (headroom), of what the machine
  has today. "Enough" means a window of at least
  :data:`MIN_WINDOW_DAYS` days and at least :data:`MIN_SAMPLE_COVERAGE` of
  the samples a source that reads every
  :data:`EXPECTED_SAMPLE_INTERVAL_MINUTES` minutes would have. Otherwise the
  measured size is unknown, with the reason.

The match then keeps the sizes of the region that have enough vCPUs and
memory, enough data-disk slots, the VM generation the boot type needs, no
restriction for the subscription (a ``Location`` restriction), and, when
given, a family on the allow-list. Of those, the cheapest by its
pay-as-you-go price wins. The rows come from the Azure connector's
``azure.vm.size.list`` read.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from meho_backplane.connectors.migration.azure_prices import PriceRow
from meho_backplane.connectors.migration.contracts import ServerRecord

__all__ = [
    "EXPECTED_SAMPLE_INTERVAL_MINUTES",
    "MIN_SAMPLE_COVERAGE",
    "MIN_WINDOW_DAYS",
    "Need",
    "SizeSearch",
    "VmSize",
    "allocated_need",
    "family_key",
    "measured_need",
    "parse_sizes",
    "pick_size",
]

MIN_WINDOW_DAYS: Final[int] = 7
MIN_SAMPLE_COVERAGE: Final[float] = 0.8
#: The coarsest sample interval MEHO counts on (vCenter keeps 2-hour values
#: for the past month). A window must hold at least 80% of the samples such
#: a source would have.
EXPECTED_SAMPLE_INTERVAL_MINUTES: Final[int] = 120

_MB_PER_GB: Final[float] = 1024.0


@dataclass(frozen=True)
class VmSize:
    """One VM size offered in the region."""

    name: str
    family: str
    vcpus: int
    memory_gb: float
    max_data_disks: int | None
    premium_io: bool
    generations: frozenset[str]
    restriction: str | None


@dataclass(frozen=True)
class Need:
    """What a size must offer. ``None`` means not checked (the value is unknown)."""

    vcpus: int
    memory_gb: float
    data_disks: int | None
    generation: str | None


@dataclass(frozen=True)
class SizeSearch:
    """The cheapest fitting size (``None`` when none fits) and the counts that explain it."""

    size: VmSize | None
    price: PriceRow | None
    fitting: int
    restricted_skipped: int
    unpriced_skipped: int


def family_key(value: str) -> str:
    """Compare families loosely: ``standardDSv5Family``, ``DSv5`` and ``dsv5`` are the same."""
    key = value.strip().lower()
    key = key.removeprefix("standard").removeprefix("_").removesuffix("family")
    return key.replace("_", "").replace(" ", "")


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and value >= 0:
        return int(value)
    return None


def _restriction(raw: Any) -> str | None:
    """The reason code of a restriction that blocks the size in the whole region.

    A ``Zone`` restriction only blocks some zones, so the size can still be
    used without a zone. Any other type (``Location``) blocks it.
    """
    if not isinstance(raw, list):
        return None
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        kind = str(entry.get("type") or "").lower()
        if kind != "zone":
            return str(entry.get("reason_code") or "restricted")
    return None


def parse_sizes(items: Iterable[Any]) -> list[VmSize]:
    """Turn ``azure.vm.size.list`` items into sizes. Items without vCPUs or memory are skipped."""
    sizes: list[VmSize] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            continue
        vcpus = _int(item.get("vcpus_available"))
        if vcpus is None:
            vcpus = _int(item.get("vcpus"))
        memory = item.get("memory_gb")
        if not vcpus or isinstance(memory, bool) or not isinstance(memory, int | float):
            continue
        generations = item.get("hyperv_generations")
        sizes.append(
            VmSize(
                name=item["name"],
                family=str(item.get("family") or ""),
                vcpus=vcpus,
                memory_gb=float(memory),
                max_data_disks=_int(item.get("max_data_disks")),
                premium_io=item.get("premium_io") is True,
                generations=frozenset(
                    str(gen).upper() for gen in generations if isinstance(gen, str)
                )
                if isinstance(generations, list)
                else frozenset(),
                restriction=_restriction(item.get("restrictions")),
            )
        )
    return sizes


def _data_disks(server: ServerRecord) -> int | None:
    """Data disks: every disk but the first, which is taken as the OS disk."""
    return None if server.disks is None else max(len(server.disks) - 1, 0)


def _generation(server: ServerRecord) -> str | None:
    if server.boot_type == "uefi":
        return "V2"
    if server.boot_type == "bios":
        return "V1"
    return None


def allocated_need(server: ServerRecord) -> Need | str:
    """What the server has today, or the reason it cannot be sized."""
    if server.cores is None or server.memory_configured_mb is None:
        return "cores or configured memory is unknown"
    return Need(
        vcpus=server.cores,
        memory_gb=server.memory_configured_mb / _MB_PER_GB,
        data_disks=_data_disks(server),
        generation=_generation(server),
    )


def measured_need(server: ServerRecord, allocated: Need, comfort_factor: float) -> Need | str:
    """p95 CPU and memory times *comfort_factor*, or the reason it is unknown."""
    use = server.utilization
    if use is None:
        return "no utilization was measured"
    window_minutes = (use.window_end - use.window_start).total_seconds() / 60
    if window_minutes < MIN_WINDOW_DAYS * 24 * 60:
        return f"the measurement window is shorter than {MIN_WINDOW_DAYS} days"
    expected = window_minutes / EXPECTED_SAMPLE_INTERVAL_MINUTES
    if use.samples < MIN_SAMPLE_COVERAGE * expected:
        return (
            f"{use.samples} samples cover less than {MIN_SAMPLE_COVERAGE:.0%} of the window "
            f"(one sample every {EXPECTED_SAMPLE_INTERVAL_MINUTES} minutes expected)"
        )
    cpu = use.cpu_percent.p95 if use.cpu_percent else None
    memory = use.memory_percent.p95 if use.memory_percent else None
    if cpu is None or memory is None:
        return "the 95th percentile of CPU or memory use is unknown"
    return Need(
        vcpus=max(1, math.ceil(allocated.vcpus * cpu / 100 * comfort_factor)),
        memory_gb=allocated.memory_gb * memory / 100 * comfort_factor,
        data_disks=allocated.data_disks,
        generation=allocated.generation,
    )


def _fits(size: VmSize, need: Need, families: frozenset[str] | None) -> bool:
    if size.vcpus < need.vcpus or size.memory_gb < need.memory_gb:
        return False
    if need.data_disks is not None and (
        size.max_data_disks is None or size.max_data_disks < need.data_disks
    ):
        return False
    if need.generation is not None and need.generation not in size.generations:
        return False
    return families is None or family_key(size.family) in families


def pick_size(
    sizes: Iterable[VmSize],
    need: Need,
    price_of: Callable[[VmSize], PriceRow | None],
    families: frozenset[str] | None,
) -> SizeSearch:
    """Search the cheapest fitting size by *price_of*. Restricted and unpriced sizes are skipped."""
    best: tuple[float, str, VmSize, PriceRow] | None = None
    fitting = restricted = unpriced = 0
    for size in sizes:
        if not _fits(size, need, families):
            continue
        if size.restriction is not None:
            restricted += 1
            continue
        price = price_of(size)
        if price is None:
            unpriced += 1
            continue
        fitting += 1
        if best is None or (price.unit_price, size.name) < best[:2]:
            best = (price.unit_price, size.name, size, price)
    return SizeSearch(
        size=best[2] if best else None,
        price=best[3] if best else None,
        fitting=fitting,
        restricted_skipped=restricted,
        unpriced_skipped=unpriced,
    )
