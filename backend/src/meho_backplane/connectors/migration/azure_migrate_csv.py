# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Turn inventory server records into an Azure Migrate CSV import file.

The column names are copied from Microsoft's import template
(``AzureMigrateimporttemplate.csv``, linked from
https://learn.microsoft.com/en-us/azure/migrate/tutorial-discover-import).
A leading ``*`` marks a required column, as in the template. The template
has columns for two disks; Microsoft allows the same five columns for up to
twenty disks, so the file gets as many disk groups as its largest server
needs (at least two, at most twenty).

Rules:

* Only server records go into the file. Applications and Kubernetes
  workloads are counted in ``skipped_records``.
* A server without a required value (name, cores, memory, OS name) is left
  out and listed with the reasons from its ``missing`` map.
* A server whose BIOS UUID belongs to a record already in the file (one
  machine read through two sources) is left out and names that record.
  Required values are checked first, so a complete second record still
  goes in when the first one lacks a value.
* Server names must be unique, because Azure Migrate updates a server
  that is imported again with the same name. A repeated name gets its
  ``source_id`` added, and ``renamed`` says so.
* At most 20,000 servers fit in one file and in one Azure Migrate project.
  Servers past that are left out with ``over_file_limit``.
* Units in the inventory already match the template (MB, GB, MB per
  second), so values are only rounded. Utilization columns take the 95th
  percentile, the careful choice for sizing. Unknown values stay empty,
  never ``0``.
* Text comes from source systems. Line breaks become spaces, and a cell
  that starts with ``=``, ``+``, ``-`` or ``@`` gets a leading ``'`` so a
  spreadsheet does not read it as a formula.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Iterable
from typing import Final, Literal

from pydantic import BaseModel, Field

from meho_backplane.connectors.migration.contracts import (
    InventoryRecord,
    ServerRecord,
    Stat,
)
from meho_backplane.connectors.migration.inventory_load import Duplicate

__all__ = [
    "BASE_COLUMNS",
    "MAX_DISKS",
    "MAX_SERVERS_PER_FILE",
    "MIN_DISK_GROUPS",
    "CsvExport",
    "LeftOutServer",
    "RenamedServer",
    "disk_columns",
    "inventory_to_csv",
]

MAX_SERVERS_PER_FILE: Final[int] = 20_000
MAX_DISKS: Final[int] = 20
MIN_DISK_GROUPS: Final[int] = 2

#: Microsoft's template columns before the per-disk groups, in its order.
BASE_COLUMNS: Final[tuple[str, ...]] = (
    "*Server name",
    "IP addresses",
    "*Cores",
    "*Memory (In MB)",
    "*OS name",
    "OS version",
    "OS architecture",
    "Server type",
    "Hypervisor",
    "CPU utilization percentage",
    "Memory utilization percentage",
    "Network adapters",
    "Network In throughput",
    "Network Out throughput",
    "Boot type",
    "Number of disks",
    "Storage in use (In GB)",
)

_FORMULA_START: Final[tuple[str, ...]] = ("=", "+", "-", "@")

_ARCHITECTURES: Final[dict[str, str]] = {
    "x64": "x64",
    "x86_64": "x64",
    "amd64": "x64",
    "64-bit": "x64",
    "x86": "x86",
    "i386": "x86",
    "i686": "x86",
    "32-bit": "x86",
}

_HYPERVISORS: Final[dict[str, str]] = {
    "vmware": "VMware",
    "esxi": "VMware",
    "hyper-v": "Hyper-V",
    "hyperv": "Hyper-V",
}

LeftOutReason = Literal["missing_required_field", "same_machine_as", "over_file_limit"]


class LeftOutServer(BaseModel):
    """A server record that is not in the file, and why."""

    source_id: str = Field(description="The record's source_id.")
    name: str = Field(description="The record's name.")
    reason: LeftOutReason = Field(description="Why it is not in the file.")
    missing: dict[str, str] = Field(
        default_factory=dict,
        description="For missing_required_field: the field and the reason it is unknown.",
    )
    same_as: str | None = Field(
        default=None, description="For same_machine_as: the source_id already in the file."
    )


class RenamedServer(BaseModel):
    """A server whose name in the file differs from its name in the inventory."""

    source_id: str = Field(description="The record's source_id.")
    original_name: str = Field(description="The name in the inventory.")
    csv_name: str = Field(description="The name in the file.")
    reasons: list[Literal["repeated_name", "cleaned_text"]] = Field(
        description="repeated_name: another server has the name. cleaned_text: line breaks "
        "or a leading formula character were changed."
    )


class CsvExport(BaseModel):
    """The CSV file and what was changed or left out."""

    csv: str = Field(description="The file text, with a header row, in Azure Migrate's format.")
    row_count: int = Field(description="Servers in the file (header not counted).")
    columns: list[str] = Field(description="The header row.")
    left_out: list[LeftOutServer] = Field(description="Servers not in the file.")
    renamed: list[RenamedServer] = Field(description="Servers with a changed name.")
    skipped_records: dict[str, int] = Field(
        description="Records that are not servers, per kind. They have no place in the file."
    )
    notes: list[str] = Field(description="Other things to know, for example cut disk lists.")


def disk_columns(disk_number: int) -> tuple[str, ...]:
    """The template's five columns for disk *disk_number* (1-based), in its order."""
    n = disk_number
    return (
        f"Disk {n} size (In GB)",
        f"Disk {n} read throughput (MB per second)",
        f"Disk {n} write throughput (MB per second)",
        f"Disk {n} read ops (operations per second)",
        f"Disk {n} write ops (operations per second)",
    )


def _number(value: float | None) -> str:
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _p95(stat: Stat | None) -> str:
    return _number(stat.p95 if stat is not None else None)


def _text(value: str | None) -> str:
    if value is None:
        return ""
    cleaned = " ".join(value.splitlines()).strip()
    if cleaned.startswith(_FORMULA_START):
        cleaned = "'" + cleaned
    return cleaned


def _required_missing(server: ServerRecord) -> dict[str, str]:
    checks = {
        "cores": server.cores,
        "memory_configured_mb": server.memory_configured_mb,
        "os.name": server.os.name if server.os is not None else None,
    }
    missing: dict[str, str] = {}
    for field_name, value in checks.items():
        if value is None:
            reason = server.missing.get(field_name) or server.missing.get(
                field_name.split(".", 1)[0], "not_reported_by_source"
            )
            missing[field_name] = reason
    return missing


def _storage_in_use(server: ServerRecord) -> str:
    if not server.disks or any(disk.used_gb is None for disk in server.disks):
        return ""
    return _number(sum(disk.used_gb or 0.0 for disk in server.disks))


def _ip_addresses(server: ServerRecord) -> str:
    ips = dict.fromkeys(ip for nic in server.nics or () for ip in nic.ip_addresses)
    return ";".join(_text(ip) for ip in ips)


def _row(server: ServerRecord, csv_name: str, disk_groups: int) -> list[str]:
    os_info = server.os
    util = server.utilization
    arch = (os_info.architecture or "").strip().lower() if os_info else ""
    hypervisor = _HYPERVISORS.get((server.hypervisor or "").strip().lower(), "")
    row = [
        csv_name,
        _ip_addresses(server),
        _number(server.cores),
        _number(server.memory_configured_mb),
        _text(os_info.name if os_info else None),
        _text(os_info.version if os_info else None),
        _ARCHITECTURES.get(arch, ""),
        server.server_type.capitalize() if server.server_type else "",
        hypervisor if server.server_type == "virtual" else "",
        _p95(util.cpu_percent if util else None),
        _p95(util.memory_percent if util else None),
        _number(len(server.nics)) if server.nics is not None else "",
        _p95(util.network_in_mb_per_s if util else None),
        _p95(util.network_out_mb_per_s if util else None),
        server.boot_type.upper() if server.boot_type else "",
        _number(len(server.disks)) if server.disks is not None else "",
        _storage_in_use(server),
    ]
    disks = (server.disks or [])[:disk_groups]
    for disk in disks:
        row += [
            _number(disk.size_gb),
            _p95(disk.read_mb_per_s),
            _p95(disk.write_mb_per_s),
            _p95(disk.read_ops_per_s),
            _p95(disk.write_ops_per_s),
        ]
    row += [""] * (5 * (disk_groups - len(disks)))
    return row


def _select(
    servers: list[ServerRecord], duplicates: Iterable[Duplicate]
) -> tuple[list[ServerRecord], list[LeftOutServer]]:
    # Every record of one machine maps to the same key: the first record's id.
    machine_of = {d.source_id: d.same_as for d in duplicates if d.match == "bios_uuid"}
    kept: list[ServerRecord] = []
    left_out: list[LeftOutServer] = []
    in_file: dict[str, str] = {}
    for server in servers:
        missing = _required_missing(server)
        if missing:
            left_out.append(
                LeftOutServer(
                    source_id=server.source_id,
                    name=server.name,
                    reason="missing_required_field",
                    missing=missing,
                )
            )
            continue
        machine = machine_of.get(server.source_id, server.source_id)
        if machine in in_file:
            # Leave a repeat out only when the record it repeats is in the file.
            left_out.append(
                LeftOutServer(
                    source_id=server.source_id,
                    name=server.name,
                    reason="same_machine_as",
                    same_as=in_file[machine],
                )
            )
            continue
        if len(kept) >= MAX_SERVERS_PER_FILE:
            left_out.append(
                LeftOutServer(
                    source_id=server.source_id, name=server.name, reason="over_file_limit"
                )
            )
            continue
        in_file[machine] = server.source_id
        kept.append(server)
    return kept, left_out


def _unique_names(servers: list[ServerRecord]) -> tuple[list[str], list[RenamedServer]]:
    cleaned = [_text(server.name) for server in servers]
    counts = Counter(name.casefold() for name in cleaned)
    names: list[str] = []
    renamed: list[RenamedServer] = []
    for server, name in zip(servers, cleaned, strict=True):
        reasons: list[Literal["repeated_name", "cleaned_text"]] = []
        if name != server.name:
            reasons.append("cleaned_text")
        if counts[name.casefold()] > 1:
            name = f"{name} ({_text(server.source_id)})"
            reasons.append("repeated_name")
        names.append(name)
        if reasons:
            renamed.append(
                RenamedServer(
                    source_id=server.source_id,
                    original_name=server.name,
                    csv_name=name,
                    reasons=reasons,
                )
            )
    return names, renamed


def inventory_to_csv(
    records: Iterable[InventoryRecord], duplicates: Iterable[Duplicate] = ()
) -> CsvExport:
    """Build the Azure Migrate CSV file from merged inventory *records*."""
    servers: list[ServerRecord] = []
    skipped: Counter[str] = Counter()
    for record in records:
        if isinstance(record, ServerRecord):
            servers.append(record)
        else:
            skipped[record.kind] += 1
    kept, left_out = _select(servers, duplicates)
    names, renamed = _unique_names(kept)
    most_disks = max((len(server.disks or ()) for server in kept), default=0)
    disk_groups = min(max(most_disks, MIN_DISK_GROUPS), MAX_DISKS)
    columns = list(BASE_COLUMNS)
    for number in range(1, disk_groups + 1):
        columns += disk_columns(number)
    notes = [
        f"{server.source_id} has {len(server.disks or ())} disks; the file lists the first "
        f"{MAX_DISKS}, and 'Number of disks' shows them all."
        for server in kept
        if len(server.disks or ()) > MAX_DISKS
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for server, name in zip(kept, names, strict=True):
        writer.writerow(_row(server, name, disk_groups))
    return CsvExport(
        csv=buffer.getvalue(),
        row_count=len(kept),
        columns=columns,
        left_out=left_out,
        renamed=renamed,
        skipped_records=dict(skipped),
        notes=notes,
    )
