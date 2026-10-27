# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Readiness rules: can a server move to an Azure VM, and on what conditions.

All rules sit in one table, :data:`RULES`. Each rule has a flag name, the
state it gives, a reason text and the Microsoft page it comes from
(:data:`SOURCES`, with the date the page was checked). The limits, the
Windows Server support dates and the endorsed Linux versions sit here too.
:mod:`.readiness_checks` only picks which rules apply to one server and
fills in the values.

States, from best to worst: ``ready``, ``ready_with_conditions``,
``unknown``, ``not_ready``. A server gets the worst state of its findings.
Data the inventory does not have gives ``unknown``, never ``ready``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Final

from meho_backplane.connectors.migration.contracts import ReadinessState

__all__ = [
    "CALCULATION_VERSION",
    "DATA_DISK_COUNT_LIMIT",
    "DATA_DISK_MAX_GB",
    "DATA_DISK_MIGRATION_LIMIT_GB",
    "LARGEST_VM_MEMORY_GB",
    "LARGEST_VM_VCPUS",
    "LINUX_ENDORSED",
    "OS_DISK_LIMIT_GB",
    "OTHER_SYSTEMS_PATTERN",
    "RULES",
    "SOURCES",
    "THIRTY_TWO_BIT_ARCHITECTURES",
    "WINDOWS_CLIENT_PATTERN",
    "WINDOWS_SERVER",
    "LinuxDistro",
    "Rule",
    "Source",
    "WindowsServer",
    "first_match",
]

#: Change this when a rule, a limit or a source changes.
CALCULATION_VERSION: Final[str] = "readiness-1"

_CHECKED: Final[date] = date(2026, 10, 27)


@dataclass(frozen=True)
class Source:
    """A Microsoft page a rule comes from, and when it was checked."""

    title: str
    url: str
    checked_on: date = _CHECKED


_LEARN = "https://learn.microsoft.com/en-us"
_LIFECYCLE = f"{_LEARN}/lifecycle/products"

SOURCES: Final[dict[str, Source]] = {
    "assessment": Source(
        "Azure VM assessments in Azure Migrate: readiness",
        f"{_LEARN}/azure/migrate/concepts-assessment-calculation?view=migrate-classic",
    ),
    "migration": Source(
        "Support for VMware vSphere migration in Azure Migrate",
        f"{_LEARN}/azure/migrate/migrate-support-matrix-vmware-migration",
    ),
    "generation_2": Source(
        "Azure support for Generation 2 VMs",
        f"{_LEARN}/azure/virtual-machines/generation-2",
    ),
    "disk_types": Source(
        "Azure managed disk types",
        f"{_LEARN}/azure/virtual-machines/disks-types",
    ),
    "largest_size": Source(
        "Mdsv3 Very High Memory size series (the biggest Azure VM sizes)",
        f"{_LEARN}/azure/virtual-machines/sizes/memory-optimized/mdsv3-vhm-series",
    ),
    "linux_endorsed": Source(
        "Linux distributions endorsed on Azure",
        f"{_LEARN}/azure/virtual-machines/linux/endorsed-distros",
    ),
    "windows_server": Source(
        "Microsoft server software support for Azure VMs",
        f"{_LEARN}/troubleshoot/azure/virtual-machines/windows/server-software-support",
    ),
    "lifecycle_2008_r2": Source(
        "Windows Server 2008 R2 lifecycle", f"{_LIFECYCLE}/windows-server-2008-r2"
    ),
    "lifecycle_2012": Source("Windows Server 2012 lifecycle", f"{_LIFECYCLE}/windows-server-2012"),
    "lifecycle_2012_r2": Source(
        "Windows Server 2012 R2 lifecycle", f"{_LIFECYCLE}/windows-server-2012-r2"
    ),
    "lifecycle_2016": Source("Windows Server 2016 lifecycle", f"{_LIFECYCLE}/windows-server-2016"),
    "lifecycle_2019": Source("Windows Server 2019 lifecycle", f"{_LIFECYCLE}/windows-server-2019"),
    "lifecycle_2022": Source("Windows Server 2022 lifecycle", f"{_LIFECYCLE}/windows-server-2022"),
    "lifecycle_2025": Source("Windows Server 2025 lifecycle", f"{_LIFECYCLE}/windows-server-2025"),
}


@dataclass(frozen=True)
class Rule:
    """One readiness rule. ``reason`` is a ``str.format`` template."""

    flag: str
    state: ReadinessState
    reason: str
    source: str


_RULE_LIST: Final[tuple[Rule, ...]] = (
    # Operating system.
    Rule("os.supported", "ready", "{os} is supported on Azure.", "windows_server"),
    Rule(
        "os.end_of_support",
        "ready_with_conditions",
        "{os} is past the end of Microsoft support ({end}). It can start in Azure, but "
        "Microsoft does not support it. Upgrade it before or soon after the move.",
        "migration",
    ),
    Rule(
        "os.endorsed_linux",
        "ready",
        "{os} is a Linux distribution Azure endorses.",
        "linux_endorsed",
    ),
    Rule(
        "os.not_endorsed",
        "ready_with_conditions",
        "{os} is not a Linux version Azure endorses. It may start in Azure, but upgrade "
        "it to an endorsed version first.",
        "linux_endorsed",
    ),
    Rule(
        "os.windows_client",
        "ready_with_conditions",
        "{os} is a Windows client system. Azure supports it only with a Visual Studio "
        "subscription or with multitenant hosting rights.",
        "assessment",
    ),
    Rule(
        "os.other",
        "ready_with_conditions",
        "{os} is not endorsed on Azure. It may start, but Azure gives no support for it.",
        "assessment",
    ),
    Rule(
        "os.32_bit",
        "ready_with_conditions",
        "The operating system is 32-bit. Azure Migrate moves 64-bit systems; upgrade to "
        "a 64-bit system or rebuild the server.",
        "migration",
    ),
    Rule(
        "os.unknown",
        "unknown",
        "The operating system is not known well enough to check: {detail}.",
        "assessment",
    ),
    # Boot type.
    Rule(
        "boot.bios",
        "ready",
        "BIOS boot: the server becomes a generation 1 VM in Azure.",
        "generation_2",
    ),
    Rule(
        "boot.uefi",
        "ready",
        "UEFI boot: the server becomes a generation 2 VM in Azure.",
        "generation_2",
    ),
    Rule(
        "boot.uefi_not_supported",
        "not_ready",
        "{os} with UEFI boot cannot run in Azure. Move to BIOS boot or upgrade the system.",
        "assessment",
    ),
    Rule(
        "boot.secure_boot",
        "ready_with_conditions",
        "Secure boot is on. In Azure, secure boot needs Trusted launch: turn it on after "
        "the move. Agent-based migration does not support secure boot.",
        "migration",
    ),
    Rule("boot.unknown", "unknown", "The boot type (BIOS or UEFI) is not known.", "generation_2"),
    # Limits.
    Rule(
        "limits.cores",
        "not_ready",
        "{cores} cores is more than the biggest Azure VM size has ({limit} vCPUs).",
        "largest_size",
    ),
    Rule(
        "limits.memory",
        "not_ready",
        "{memory_gb} GB of memory is more than the biggest Azure VM size has ({limit} GB).",
        "largest_size",
    ),
    Rule(
        "limits.os_disk",
        "not_ready",
        "The OS disk ({size_gb} GB) is bigger than the {limit_gb} GB a generation "
        "{generation} VM allows.",
        "migration",
    ),
    Rule(
        "limits.data_disk_size",
        "not_ready",
        "Disk(s) {disks} are bigger than {limit_gb} GB, the biggest Azure managed disk.",
        "disk_types",
    ),
    Rule(
        "limits.data_disk_migration",
        "ready_with_conditions",
        "Disk(s) {disks} are bigger than {limit_gb} GB. Azure Migrate copies data disks "
        "only up to that size: split the disk or move its data another way.",
        "migration",
    ),
    Rule(
        "limits.disk_count",
        "not_ready",
        "{count} data disks is more than the {limit} an Azure VM can have.",
        "migration",
    ),
    Rule(
        "limits.unknown",
        "unknown",
        "The limits cannot be checked: {detail}.",
        "assessment",
    ),
    # Features Azure Migrate does not move.
    Rule(
        "features.shared_disk",
        "ready_with_conditions",
        "Disk(s) {disks} are shared with another machine. Azure Migrate does not move "
        "shared-disk clusters: remove the sharing first.",
        "migration",
    ),
    Rule(
        "features.raw_device_mapping",
        "ready_with_conditions",
        "Disk(s) {disks} are raw device mappings. Azure Migrate does not copy them: "
        "move their data to a virtual disk first.",
        "migration",
    ),
    Rule(
        "features.independent_disk",
        "ready_with_conditions",
        "Disk(s) {disks} are independent disks. Agentless migration does not support "
        "them: make them dependent first.",
        "migration",
    ),
    Rule(
        "features.snapshots",
        "ready_with_conditions",
        "The server has {count} snapshot(s). Remove or merge them before the migration.",
        "migration",
    ),
    Rule(
        "features.unknown",
        "unknown",
        "It is not known whether the server uses features Azure Migrate does not move: {detail}.",
        "migration",
    ),
)

#: Every rule, by flag.
RULES: Final[dict[str, Rule]] = {rule.flag: rule for rule in _RULE_LIST}

#: The biggest Azure VM size (Standard_M1792ixds_32_v3 for vCPUs,
#: Standard_M896ixds_32_v3 and Standard_M1792ixds_32_v3 for memory).
LARGEST_VM_VCPUS: Final[int] = 1792
LARGEST_VM_MEMORY_GB: Final[int] = 30400

#: OS disk limit per VM generation (1 TB = 1,024 GB).
OS_DISK_LIMIT_GB: Final[dict[int, int]] = {1: 2048, 2: 4096}
#: Data disks: Azure Migrate copies up to 32 TB; the biggest managed disk is 65,536 GiB.
DATA_DISK_MIGRATION_LIMIT_GB: Final[int] = 32768
DATA_DISK_MAX_GB: Final[int] = 65536
DATA_DISK_COUNT_LIMIT: Final[int] = 64


@dataclass(frozen=True)
class WindowsServer:
    """One Windows Server release: the last day of support and the source."""

    support_end: date | None  # None: support ended long ago
    source: str
    uefi_supported: bool = True


WINDOWS_SERVER: Final[dict[str, WindowsServer]] = {
    "2003": WindowsServer(None, "assessment", uefi_supported=False),
    "2003 r2": WindowsServer(None, "assessment", uefi_supported=False),
    "2008": WindowsServer(None, "migration", uefi_supported=False),
    "2008 r2": WindowsServer(date(2020, 1, 14), "lifecycle_2008_r2", uefi_supported=False),
    "2012": WindowsServer(date(2023, 10, 10), "lifecycle_2012"),
    "2012 r2": WindowsServer(date(2023, 10, 10), "lifecycle_2012_r2"),
    "2016": WindowsServer(date(2027, 1, 12), "lifecycle_2016"),
    "2019": WindowsServer(date(2029, 1, 9), "lifecycle_2019"),
    "2022": WindowsServer(date(2031, 10, 14), "lifecycle_2022"),
    "2025": WindowsServer(date(2034, 11, 14), "lifecycle_2025"),
}


def first_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _sles_version(text: str) -> str | None:
    major = first_match(r"\b(1[0-9])\b", text)
    if major is None:
        return None
    if major == "16":
        return "16"
    service_pack = first_match(r"\bsp\s?(\d+)\b", text) or first_match(rf"\b{major}\.(\d+)\b", text)
    return f"{major} sp{service_pack}" if service_pack is not None else None


@dataclass(frozen=True)
class LinuxDistro:
    """An endorsed distribution. ``versions`` None: the page names no version limit."""

    pattern: str
    versions: frozenset[str] | None = None
    version_of: Callable[[str], str | None] | None = None


LINUX_ENDORSED: Final[tuple[LinuxDistro, ...]] = (
    LinuxDistro(
        r"\bubuntu\b",
        frozenset({"22.04", "24.04", "26.04"}),
        lambda text: first_match(r"\b(\d{2}\.\d{2})\b", text),
    ),
    LinuxDistro(
        r"\bdebian\b",
        frozenset({"11", "12"}),
        lambda text: first_match(r"\b(\d{1,2})(?:\.\d+)*\b", text),
    ),
    LinuxDistro(
        r"\bsuse\b|\bsles\b",
        frozenset({"12 sp5", "15 sp5", "15 sp6", "15 sp7", "16"}),
        _sles_version,
    ),
    LinuxDistro(r"\bred hat\b|\brhel\b"),
    LinuxDistro(r"\boracle linux\b"),
    LinuxDistro(r"\balma\s?linux\b"),
    LinuxDistro(r"\brocky\b"),
    LinuxDistro(r"\bflatcar\b"),
)

WINDOWS_CLIENT_PATTERN: Final[str] = (
    r"\bwindows (?:xp|vista|7|8(?:\.1)?|10|11|nt|2000|95|98|3\.1|me)\b|\bms-dos\b"
)
OTHER_SYSTEMS_PATTERN: Final[str] = r"\b(?:freebsd|solaris|macos|mac os|netware|openbsd|aix)\b"
THIRTY_TWO_BIT_ARCHITECTURES: Final[frozenset[str]] = frozenset(
    {"x86", "i386", "i586", "i686", "32-bit"}
)
