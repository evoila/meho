# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Readiness checks: which rules of :mod:`.readiness_rules` apply to one server.

States, from best to worst: ``ready``, ``ready_with_conditions``,
``unknown``, ``not_ready``. A server gets the worst state of its findings.
Data the inventory does not have gives ``unknown``, never ``ready``.

Values from source systems (OS names, disk labels) are untrusted text. They
only go into the reason text, cut short and on one line.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Final

from meho_backplane.connectors.migration.contracts import (
    Disk,
    Readiness,
    ReadinessFinding,
    ServerRecord,
    worst_readiness,
)
from meho_backplane.connectors.migration.readiness_rules import (
    DATA_DISK_COUNT_LIMIT,
    DATA_DISK_MAX_GB,
    DATA_DISK_MIGRATION_LIMIT_GB,
    LARGEST_VM_MEMORY_GB,
    LARGEST_VM_VCPUS,
    LINUX_ENDORSED,
    OLD_WINDOWS_PATTERN,
    OS_DISK_LIMIT_GB,
    OTHER_SYSTEMS_PATTERN,
    RULES,
    SIXTY_FOUR_BIT_ARCHITECTURES,
    SOURCES,
    THIRTY_TWO_BIT_ARCHITECTURES,
    WINDOWS_CLIENT_PATTERN,
    WINDOWS_SERVER,
    first_match,
)

__all__ = ["assess_server"]

_MAX_TEXT: Final[int] = 80


def _clean(value: str) -> str:
    """Source text on one line, cut short: it is untrusted and only for reading."""
    text = " ".join(value.split())
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 3] + "..."


def _finding(
    flag: str,
    inputs: dict[str, Any] | None = None,
    *,
    source: str | None = None,
    **values: Any,
) -> ReadinessFinding:
    rule = RULES[flag]
    page = SOURCES[source or rule.source]
    return ReadinessFinding(
        flag=flag,
        state=rule.state,
        reason=rule.reason.format(**values),
        inputs=inputs or {},
        source_title=page.title,
        source_url=page.url,
        source_checked_on=page.checked_on,
    )


# --- operating system -------------------------------------------------------


def _os_text(record: ServerRecord) -> tuple[str, str]:
    """The OS name and version as one lower-case string, and a clean label."""
    raw = _os_raw(record)
    return raw.replace("(64-bit)", " ").replace("(32-bit)", " 32-bit "), _clean(raw)


def _os_raw(record: ServerRecord) -> str:
    os = record.os
    return " ".join(part for part in (os.name, os.version) if part).lower() if os else ""


_WINDOWS_RELEASE: Final[str] = r"windows server (\d{4}(?: r2)?)\b"


def _windows_findings(
    text: str, label: str, as_of: date, inputs: dict[str, Any]
) -> list[ReadinessFinding]:
    release = first_match(_WINDOWS_RELEASE, text)
    if release is None or release not in WINDOWS_SERVER:
        if re.search(WINDOWS_CLIENT_PATTERN, text):
            return [_finding("os.windows_client", inputs, os=label)]
        if re.search(OLD_WINDOWS_PATTERN, text):
            return [_finding("os.old_windows", inputs, os=label)]
        return [_finding("os.unknown", inputs, detail=f"'{label}' is not a known Windows version")]
    info = WINDOWS_SERVER[release]
    ended = info.support_end is None or as_of > info.support_end
    if ended and "or later" in text:
        detail = f"'{label}' names no exact version, and the oldest it names is out of support"
        return [_finding("os.unknown", inputs, detail=detail)]
    if not ended:
        return [_finding("os.supported", inputs, source=info.source, os=label)]
    end = info.support_end.isoformat() if info.support_end else "ended"
    return [_finding("os.end_of_support", inputs, source=info.source, os=label, end=end)]


def _linux_findings(text: str, label: str, inputs: dict[str, Any]) -> list[ReadinessFinding]:
    for distro in LINUX_ENDORSED:
        if not re.search(distro.pattern, text):
            continue
        if distro.versions is None or distro.version_of is None:
            return [_finding("os.endorsed_linux", inputs, os=label)]
        version = distro.version_of(text)
        if version is None:
            detail = f"'{label}' has no version, and only some versions are endorsed"
            return [_finding("os.unknown", inputs, detail=detail)]
        if version in distro.versions:
            return [_finding("os.endorsed_linux", inputs, os=label)]
        return [_finding("os.not_endorsed", inputs, os=label)]
    return [_finding("os.not_endorsed", inputs, os=label)]


def _os_findings(record: ServerRecord, as_of: date) -> list[ReadinessFinding]:
    os = record.os
    text, label = _os_text(record)
    family = os.family if os is not None else None
    inputs: dict[str, Any] = {
        "os_name": _clean(os.name) if os is not None and os.name else None,
        "os_version": _clean(os.version) if os is not None and os.version else None,
        "os_family": family,
    }
    findings: list[ReadinessFinding]
    if not text or text.startswith("other"):
        detail = f"the source names it '{label}'" if label else "the source did not report it"
        findings = [_finding("os.unknown", inputs, detail=detail)]
    elif family == "windows" or "windows" in text:
        findings = _windows_findings(text, label, as_of, inputs)
    elif (
        family == "linux"
        or "linux" in text
        or any(re.search(d.pattern, text) for d in LINUX_ENDORSED)
    ):
        findings = _linux_findings(text, label, inputs)
    elif family == "other" or re.search(OTHER_SYSTEMS_PATTERN, text):
        findings = [_finding("os.other", inputs, os=label)]
    else:
        findings = [_finding("os.unknown", inputs, detail=f"'{label}' is not a known system")]
    architecture_finding = _architecture_finding(record, text)
    return findings + ([architecture_finding] if architecture_finding else [])


def _architecture_finding(record: ServerRecord, text: str) -> ReadinessFinding | None:
    """32-bit is a condition. Nothing says 32-bit or 64-bit: unknown, never ready.

    The architecture comes from the ``architecture`` field, from "(64-bit)" or
    "(32-bit)" in the OS name (vCenter), or from a Windows Server release that
    comes only as 64-bit.
    """
    os = record.os
    architecture = (os.architecture or "").strip().lower() if os is not None else ""
    raw = _os_raw(record)
    inputs: dict[str, Any] = {"architecture": _clean(architecture) or None}
    if architecture in THIRTY_TWO_BIT_ARCHITECTURES or "32-bit" in raw:
        return _finding("os.32_bit", inputs)
    if architecture in SIXTY_FOUR_BIT_ARCHITECTURES or "64-bit" in raw:
        return None
    if architecture:
        detail = f"'{_clean(architecture)}' is not a known architecture"
        return _finding("os.architecture_unknown", inputs, detail=detail)
    release = first_match(_WINDOWS_RELEASE, text)
    if release in WINDOWS_SERVER and WINDOWS_SERVER[release].only_64_bit:
        return None
    if not text:
        # No OS at all: the OS finding already says it is not reported.
        return None
    detail = "the source did not report the architecture, and the system name does not say"
    return _finding("os.architecture_unknown", inputs, detail=detail)


# --- boot type --------------------------------------------------------------


def _old_windows_without_uefi(text: str) -> bool:
    release = first_match(_WINDOWS_RELEASE, text)
    return release in WINDOWS_SERVER and not WINDOWS_SERVER[release].uefi_supported


def _boot_findings(record: ServerRecord) -> list[ReadinessFinding]:
    inputs: dict[str, Any] = {"boot_type": record.boot_type, "secure_boot": record.secure_boot}
    if record.boot_type is None:
        return [_finding("boot.unknown", inputs)]
    findings = [_finding(f"boot.{record.boot_type}", inputs)]
    text, label = _os_text(record)
    if record.boot_type == "uefi" and _old_windows_without_uefi(text):
        findings.append(_finding("boot.uefi_not_supported", inputs, os=label))
    if record.secure_boot:
        findings.append(_finding("boot.secure_boot", inputs))
    elif record.secure_boot is None and record.boot_type == "uefi":
        # Only UEFI servers can have secure boot, so a missing value on a
        # BIOS server tells us nothing new and gives no finding.
        findings.append(_finding("boot.secure_boot_unknown", inputs))
    return findings


# --- limits -----------------------------------------------------------------


def _compute_findings(record: ServerRecord) -> list[ReadinessFinding]:
    findings: list[ReadinessFinding] = []
    unknown: list[str] = []
    if record.cores is None:
        unknown.append("cores")
    elif record.cores > LARGEST_VM_VCPUS:
        inputs: dict[str, Any] = {"cores": record.cores, "limit": LARGEST_VM_VCPUS}
        findings.append(_finding("limits.cores", inputs, **inputs))
    if record.memory_configured_mb is None:
        unknown.append("memory")
    elif record.memory_configured_mb > LARGEST_VM_MEMORY_GB * 1024:
        memory_gb = round(record.memory_configured_mb / 1024, 1)
        inputs = {
            "memory_mb": record.memory_configured_mb,
            "memory_gb": memory_gb,
            "limit": LARGEST_VM_MEMORY_GB,
        }
        findings.append(
            _finding("limits.memory", inputs, memory_gb=memory_gb, limit=LARGEST_VM_MEMORY_GB)
        )
    if unknown:
        detail = " and ".join(unknown) + " not reported"
        findings.append(_finding("limits.unknown", {"missing": ", ".join(unknown)}, detail=detail))
    return findings


def _labels(disks: list[Disk]) -> str:
    return ", ".join(f"'{_clean(disk.label)}'" for disk in disks)


def _labels_with_sizes(disks: list[Disk]) -> str:
    """Labels and sizes for a reason and its inputs: ``'data-1' (40,000 GB)``."""
    return ", ".join(
        f"'{_clean(disk.label)}' ({disk.size_gb:,.10g} GB)"
        for disk in disks
        if disk.size_gb is not None
    )


def _os_disk_findings(record: ServerRecord, disks: list[Disk]) -> list[ReadinessFinding]:
    """The OS disk limit of the VM generation.

    The inventory does not say which disk is the OS disk. So the check decides
    only when the order of the disks cannot change the answer: no disk is over
    the limit (ready), or every disk is (not ready). Otherwise it is unknown.
    """
    generation = {"bios": 1, "uefi": 2}.get(record.boot_type or "")
    # Boot type not known: the stricter generation 1 limit finds every disk that may be over.
    limit = OS_DISK_LIMIT_GB[generation or 1]
    over = [disk for disk in disks if disk.size_gb is not None and disk.size_gb > limit]
    if not over:
        return []
    inputs: dict[str, Any] = {
        "disks": _labels_with_sizes(over),
        "limit_gb": limit,
        "boot_type": record.boot_type,
    }
    if len(over) < len(disks):
        detail = (
            f"disk(s) {_labels_with_sizes(over)} are over the {limit:,} GB OS disk limit of a "
            f"generation {generation or 1} VM, and the inventory does not say which disk is "
            "the OS disk"
        )
        return [_finding("limits.unknown", inputs, detail=detail)]
    if generation is None:
        if all(disk.size_gb is not None and disk.size_gb > OS_DISK_LIMIT_GB[2] for disk in over):
            generation, limit = 2, OS_DISK_LIMIT_GB[2]
        else:
            detail = (
                f"every disk ({_labels_with_sizes(over)}) is over {limit:,} GB, and the boot "
                "type is not known"
            )
            return [_finding("limits.unknown", inputs, detail=detail)]
    inputs["limit_gb"] = limit
    return [
        _finding(
            "limits.os_disk",
            inputs,
            disks=_labels_with_sizes(over),
            limit_gb=f"{limit:,}",
            generation=generation,
        )
    ]


def _data_disk_findings(disks: list[Disk]) -> list[ReadinessFinding]:
    """Size and count limits of the data disks: every disk but the OS disk.

    With several disks, any of them may be a data disk, so the size limits
    are checked on every disk. A server with one disk has no data disk.
    """
    findings: list[ReadinessFinding] = []
    candidates = disks if len(disks) > 1 else []
    sized = [(d, d.size_gb) for d in candidates if d.size_gb is not None]
    too_big = [d for d, size in sized if size > DATA_DISK_MAX_GB]
    big = [d for d, size in sized if DATA_DISK_MIGRATION_LIMIT_GB < size <= DATA_DISK_MAX_GB]
    if too_big:
        inputs: dict[str, Any] = {
            "disks": _labels_with_sizes(too_big),
            "limit_gb": DATA_DISK_MAX_GB,
        }
        findings.append(
            _finding(
                "limits.data_disk_size",
                inputs,
                disks=inputs["disks"],
                limit_gb=f"{DATA_DISK_MAX_GB:,}",
            )
        )
    if big:
        inputs = {"disks": _labels_with_sizes(big), "limit_gb": DATA_DISK_MIGRATION_LIMIT_GB}
        findings.append(
            _finding(
                "limits.data_disk_migration",
                inputs,
                disks=inputs["disks"],
                limit_gb=f"{DATA_DISK_MIGRATION_LIMIT_GB:,}",
            )
        )
    if len(disks) - 1 > DATA_DISK_COUNT_LIMIT:
        inputs = {"count": len(disks) - 1, "limit": DATA_DISK_COUNT_LIMIT}
        findings.append(_finding("limits.disk_count", inputs, **inputs))
    return findings


def _disk_limit_findings(record: ServerRecord) -> list[ReadinessFinding]:
    if not record.disks:
        detail = "no disks are reported" if record.disks == [] else "the disks are not reported"
        return [_finding("limits.unknown", {"disks": None}, detail=detail)]
    disks = record.disks
    findings = [*_os_disk_findings(record, disks), *_data_disk_findings(disks)]
    unknown = [disk for disk in disks if disk.size_gb is None]
    if unknown:
        detail = f"the size of disk(s) {_labels(unknown)} is not reported"
        findings.append(_finding("limits.unknown", {"disks": _labels(unknown)}, detail=detail))
    return findings


# --- features ---------------------------------------------------------------

_DISK_FEATURES: Final[tuple[tuple[str, str], ...]] = (
    ("shared", "features.shared_disk"),
    ("raw_mapping", "features.raw_device_mapping"),
    ("independent", "features.independent_disk"),
)


def _feature_findings(record: ServerRecord) -> list[ReadinessFinding]:
    findings: list[ReadinessFinding] = []
    unknown: list[str] = []
    for field, flag in _DISK_FEATURES:
        if record.disks is None:
            unknown.append(f"{field} (disks not reported)")
            continue
        found = [disk for disk in record.disks if getattr(disk, field) is True]
        if found:
            findings.append(_finding(flag, {"disks": _labels(found)}, disks=_labels(found)))
        missing = [disk for disk in record.disks if getattr(disk, field) is None]
        if missing:
            unknown.append(f"{field} on disk(s) {_labels(missing)}")
    if record.snapshot_count is None:
        unknown.append("snapshot count")
    elif record.snapshot_count > 0:
        inputs: dict[str, Any] = {"count": record.snapshot_count}
        findings.append(_finding("features.snapshots", inputs, **inputs))
    if unknown:
        detail = "; ".join(unknown) + " not reported"
        findings.append(
            _finding("features.unknown", {"missing": "; ".join(unknown)}, detail=detail)
        )
    return findings


def assess_server(record: ServerRecord, *, as_of: date) -> Readiness:
    """Readiness of one server. *as_of* is the day support dates are compared with."""
    findings = [
        *_os_findings(record, as_of),
        *_boot_findings(record),
        *_compute_findings(record),
        *_disk_limit_findings(record),
        *_feature_findings(record),
    ]
    return Readiness(state=worst_readiness([f.state for f in findings]), findings=findings)
