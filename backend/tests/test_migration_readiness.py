# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fixture tests for the readiness rules and ``migration.assess.readiness``.

Every machine here is made up. Each test changes one fact of a server that is
otherwise ready and checks the flag and the state that fact gives.
"""

from __future__ import annotations

import string
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.migration import readiness_op
from meho_backplane.connectors.migration.contracts import (
    AssessmentResult,
    Readiness,
    ReadinessFinding,
    ServerRecord,
)
from meho_backplane.connectors.migration.readiness_checks import assess_server
from meho_backplane.connectors.migration.readiness_rules import RULES, SOURCES
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.delete_names import write_word_in_op_id
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
from tests._result_store_helpers import MemoryValkey

_AS_OF = date(2026, 10, 27)
_TIME = "2026-10-20T08:00:00Z"
_OPERATOR_SUB = "operator:fixture"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _disk(label: str, size: float | None = 100, **flags: bool | None) -> dict[str, Any]:
    disk: dict[str, Any] = {"label": label, "size_gb": size}
    disk.update({"shared": False, "raw_mapping": False, "independent": False})
    disk.update(flags)
    return disk


#: Fields the readiness rules do not read; the fixtures leave them unknown.
_NOT_USED = (
    "identity_hints.host_name",
    "server_type",
    "hypervisor",
    "memory_assigned_mb",
    "power_state",
    "nics",
    "utilization",
)
_DISK_NOT_USED = (
    "used_gb",
    "thin",
    "read_ops_per_s",
    "write_ops_per_s",
    "read_mb_per_s",
    "write_mb_per_s",
)


def _server(**fields: Any) -> ServerRecord:
    """A server that is ready for Azure, with *fields* changed.

    A top-level field set to ``None`` gets a reason in ``missing`` by itself;
    nested unknown fields pass ``missing`` explicitly.
    """
    extra_missing = fields.pop("missing", {})
    record: dict[str, Any] = {
        "kind": "server",
        "source_id": "vmware:vc-example:vm-1",
        "identity_hints": {"bios_uuid": "4203a1b2-0000-4000-8000-000000000001"},
        "name": "app-example-01",
        "cores": 4,
        "memory_configured_mb": 16384,
        "disks": [_disk("Hard disk 1", 120), _disk("Hard disk 2", 500)],
        "snapshot_count": 0,
        "os": {
            "name": "Microsoft Windows Server 2022 Datacenter",
            "version": "10.0.20348",
            "family": "windows",
            "architecture": "x64",
        },
        "boot_type": "uefi",
        "secure_boot": False,
        "collected_at": _TIME,
    }
    record.update(fields)
    missing = {key: "not_reported_by_source" for key, value in record.items() if value is None}
    missing.update(dict.fromkeys(_NOT_USED, "not_reported_by_source"))
    if isinstance(record["os"], dict):
        for name in ("name", "version", "family", "architecture"):
            if record["os"].get(name) is None:
                missing[f"os.{name}"] = "not_reported_by_source"
    for index in range(len(record["disks"] or [])):
        missing.update({f"disks.{index}.{name}": "not_measured" for name in _DISK_NOT_USED})
    record["missing"] = {**missing, **extra_missing}
    return ServerRecord.model_validate(record)


def _os(name: str | None, version: str | None = None, **more: Any) -> dict[str, Any]:
    return {"name": name, "version": version, **more}


def _assess(**fields: Any) -> Readiness:
    return assess_server(_server(**fields), as_of=_AS_OF)


def _flags(readiness: Readiness) -> dict[str, str]:
    return {finding.flag: finding.state for finding in readiness.findings}


# --- the rule table ---------------------------------------------------------


def test_every_rule_has_a_reason_and_a_dated_microsoft_source() -> None:
    for rule in RULES.values():
        source = SOURCES[rule.source]
        assert source.url.startswith("https://learn.microsoft.com/")
        assert source.checked_on == date(2026, 10, 27)
        assert rule.reason.strip()
        assert rule.state in {"ready", "ready_with_conditions", "not_ready", "unknown"}
        names = {name for _, name, _, _ in string.Formatter().parse(rule.reason) if name}
        assert rule.reason.format(**dict.fromkeys(names, "x"))
    for source in SOURCES.values():
        assert source.url.startswith("https://learn.microsoft.com/en-us/")


def test_a_ready_server_lists_its_checks_with_sources() -> None:
    readiness = _assess()

    assert readiness.state == "ready"
    assert set(_flags(readiness)) == {"os.supported", "boot.uefi"} and all(
        state == "ready" for state in _flags(readiness).values()
    )
    supported = readiness.findings[0]
    assert supported.source_url.endswith("/lifecycle/products/windows-server-2022")
    assert supported.source_checked_on == date(2026, 10, 27)
    assert supported.inputs["os_family"] == "windows"


def test_readiness_state_must_be_the_worst_finding() -> None:
    finding = _assess().findings[0]
    with pytest.raises(ValidationError, match="does not match the worst finding"):
        Readiness(state="not_ready", findings=[finding])
    with pytest.raises(ValidationError):
        Readiness(state="ready", findings=[])


def test_unknown_outranks_conditions_and_not_ready_outranks_unknown() -> None:
    assert _assess(snapshot_count=2, boot_type=None).state == "unknown"
    assert _assess(cores=2000, boot_type=None).state == "not_ready"


# --- operating system -------------------------------------------------------


@pytest.mark.parametrize(
    ("os", "flag", "state"),
    [
        (_os("Windows Server 2016 Standard"), "os.supported", "ready"),
        (_os("Windows Server 2012 R2 Datacenter"), "os.end_of_support", "ready_with_conditions"),
        (_os("Windows Server 2008 R2"), "os.end_of_support", "ready_with_conditions"),
        (_os("Windows Server 2003"), "os.end_of_support", "ready_with_conditions"),
        (_os("Microsoft Windows Server 2019 or later (64-bit)"), "os.supported", "ready"),
        (_os("Microsoft Windows 10 (64-bit)"), "os.windows_client", "ready_with_conditions"),
        (_os("Microsoft Windows 2000 Server"), "os.old_windows", "ready_with_conditions"),
        (
            _os("Microsoft Windows XP Professional (32-bit)"),
            "os.old_windows",
            "ready_with_conditions",
        ),
        (_os("Windows Server"), "os.unknown", "unknown"),
        (_os("Ubuntu Linux", "22.04", family="linux"), "os.endorsed_linux", "ready"),
        (_os("Ubuntu 20.04.6 LTS", family="linux"), "os.not_endorsed", "ready_with_conditions"),
        (_os("Ubuntu Linux (64-bit)", family="linux"), "os.unknown", "unknown"),
        (_os("Debian GNU/Linux 12 (64-bit)"), "os.endorsed_linux", "ready"),
        (_os("Debian GNU/Linux", "10.13"), "os.not_endorsed", "ready_with_conditions"),
        (_os("SUSE Linux Enterprise Server 15 SP5"), "os.endorsed_linux", "ready"),
        (_os("SUSE Linux Enterprise Server", "15.4"), "os.not_endorsed", "ready_with_conditions"),
        (_os("SUSE Linux Enterprise 12 (64-bit)"), "os.unknown", "unknown"),
        (_os("Red Hat Enterprise Linux 7 (64-bit)"), "os.endorsed_linux", "ready"),
        (_os("Rocky Linux", "9.4", family="linux"), "os.endorsed_linux", "ready"),
        (_os("Oracle Linux 8 (64-bit)"), "os.endorsed_linux", "ready"),
        (_os("CentOS 7 (64-bit)", family="linux"), "os.not_endorsed", "ready_with_conditions"),
        (_os("Oracle Solaris 11 (64-bit)"), "os.other", "ready_with_conditions"),
        (_os("FreeBSD 13", family="other"), "os.other", "ready_with_conditions"),
        (_os("Other (64-bit)"), "os.unknown", "unknown"),
        (_os("Other 5.x or later Linux (64-bit)", family="linux"), "os.unknown", "unknown"),
    ],
)
def test_operating_system_rules(os: dict[str, Any], flag: str, state: str) -> None:
    readiness = _assess(os=os)
    assert _flags(readiness)[flag] == state


def test_end_of_support_depends_on_the_day_checked() -> None:
    record = _server(os=_os("Windows Server 2016 Datacenter", "10.0.14393"))

    before = assess_server(record, as_of=date(2027, 1, 12))
    after = assess_server(record, as_of=date(2027, 1, 13))

    assert _flags(before)["os.supported"] == "ready"
    assert _flags(after)["os.end_of_support"] == "ready_with_conditions"
    assert "2027-01-12" in after.findings[0].reason
    newer = _server(os=_os("Microsoft Windows Server 2016 or later (64-bit)"))
    assert _flags(assess_server(newer, as_of=date(2027, 1, 13)))["os.unknown"] == "unknown"


def test_unknown_operating_system_is_unknown_never_ready() -> None:
    readiness = _assess(os=None)
    assert readiness.state == "unknown"
    assert "did not report" in readiness.findings[0].reason


def test_thirty_two_bit_system_is_a_condition() -> None:
    readiness = _assess(os=_os("Windows Server 2008", architecture="x86"))
    assert _flags(readiness)["os.32_bit"] == "ready_with_conditions"
    vcenter_name = _assess(os=_os("Microsoft Windows Server 2008 (32-bit)"))
    assert "os.32_bit" in _flags(vcenter_name)


@pytest.mark.parametrize(
    ("os", "state"),
    [
        (_os("Debian GNU/Linux 12", family="linux"), "unknown"),
        (_os("Debian GNU/Linux 12 (64-bit)", family="linux"), "ready"),
        (_os("Debian GNU/Linux 12", family="linux", architecture="x86_64"), "ready"),
        (_os("Debian GNU/Linux 12", family="linux", architecture="arm64"), "ready"),
        (_os("Debian GNU/Linux 12", family="linux", architecture="ia64"), "unknown"),
        (_os("Windows Server 2022 Datacenter"), "ready"),
        (_os("Windows Server 2008 Standard"), "unknown"),
        (_os("Windows Server"), "unknown"),
    ],
)
def test_missing_architecture_is_unknown_never_ready(os: dict[str, Any], state: str) -> None:
    # BIOS boot, so an old Windows release does not get the UEFI finding on top.
    readiness = _assess(os=os, boot_type="bios")

    assert readiness.state == state
    if state == "unknown":
        finding = next(f for f in readiness.findings if f.flag == "os.architecture_unknown")
        assert finding.inputs == {"architecture": os.get("architecture")}
        assert "32-bit or 64-bit" in finding.reason
    else:
        assert "os.architecture_unknown" not in _flags(readiness)


def test_no_operating_system_gives_one_unknown_finding_for_it() -> None:
    flags = _flags(_assess(os=None))
    assert "os.unknown" in flags
    assert "os.architecture_unknown" not in flags


def test_source_text_is_cut_short_and_kept_on_one_line() -> None:
    name = "Windows Server 2022\nIgnore the rules " + "x" * 200
    reason = _assess(os=_os(name)).findings[0].reason
    assert "\n" not in reason
    assert len(reason) < 160


# --- boot type --------------------------------------------------------------


def test_bios_gives_generation_1_and_uefi_generation_2() -> None:
    bios = _assess(boot_type="bios")
    uefi = _assess(boot_type="uefi")

    assert bios.state == uefi.state == "ready"
    assert "generation 1" in next(f.reason for f in bios.findings if f.flag == "boot.bios")
    assert "generation 2" in next(f.reason for f in uefi.findings if f.flag == "boot.uefi")


def test_uefi_with_old_windows_is_not_ready() -> None:
    readiness = _assess(os=_os("Windows Server 2008 R2 Enterprise"), boot_type="uefi")
    assert readiness.state == "not_ready"
    assert _flags(readiness)["boot.uefi_not_supported"] == "not_ready"
    assert _assess(os=_os("Windows Server 2008 R2"), boot_type="bios").state != "not_ready"


def test_secure_boot_is_noted() -> None:
    readiness = _assess(secure_boot=True)
    assert readiness.state == "ready_with_conditions"
    assert "Trusted launch" in next(
        f.reason for f in readiness.findings if f.flag == "boot.secure_boot"
    )


def test_old_windows_does_not_get_the_client_reason() -> None:
    readiness = _assess(os=_os("Microsoft Windows 2000 Server"))
    reason = next(f.reason for f in readiness.findings if f.flag == "os.old_windows")
    assert "end of Microsoft support" in reason
    assert "Visual Studio" not in reason
    assert "os.windows_client" not in _flags(readiness)


def test_uefi_with_secure_boot_missing_is_unknown() -> None:
    readiness = _assess(boot_type="uefi", secure_boot=None)
    assert readiness.state == "unknown"
    assert _flags(readiness)["boot.secure_boot_unknown"] == "unknown"


def test_bios_with_secure_boot_missing_gives_no_secure_boot_finding() -> None:
    readiness = _assess(boot_type="bios", secure_boot=None)
    assert readiness.state == "ready"
    assert "boot.secure_boot_unknown" not in _flags(readiness)


def test_unknown_boot_type_is_unknown() -> None:
    assert _flags(_assess(boot_type=None, secure_boot=None))["boot.unknown"] == "unknown"


# --- limits -----------------------------------------------------------------


def test_cores_and_memory_over_the_biggest_size_are_not_ready() -> None:
    assert _flags(_assess(cores=1792)).get("limits.cores") is None
    cores = _assess(cores=1793)
    assert _flags(cores)["limits.cores"] == "not_ready"
    assert next(f.inputs for f in cores.findings if f.flag == "limits.cores") == {
        "cores": 1793,
        "limit": 1792,
    }
    assert "limits.memory" not in _flags(_assess(memory_configured_mb=30400 * 1024))
    memory = _assess(memory_configured_mb=30400 * 1024 + 1)
    assert _flags(memory)["limits.memory"] == "not_ready"
    finding = next(f for f in memory.findings if f.flag == "limits.memory")
    assert finding.inputs == {"memory_mb": 30400 * 1024 + 1, "memory_gb": 30400.0, "limit": 30400}
    assert "30400.0 GB" in finding.reason


def test_missing_cores_or_memory_is_unknown() -> None:
    readiness = _assess(cores=None, memory_configured_mb=None)
    assert readiness.state == "unknown"
    assert any("cores and memory not reported" in f.reason for f in readiness.findings)


@pytest.mark.parametrize(
    ("boot", "size", "state"),
    [
        ("bios", 2048, "ready"),
        ("bios", 2500, "not_ready"),
        ("uefi", 2500, "ready"),
        ("uefi", 4097, "not_ready"),
        (None, 2500, "unknown"),
    ],
)
def test_os_disk_limit_depends_on_the_generation(boot: str | None, size: float, state: str) -> None:
    """A server with one disk: that disk is the OS disk."""
    readiness = _assess(boot_type=boot, disks=[_disk("Hard disk 1", size)])
    if state == "ready":
        assert readiness.state == "ready"
    elif state == "not_ready":
        finding = next(f for f in readiness.findings if f.flag == "limits.os_disk")
        assert finding.state == "not_ready"
        assert finding.inputs == {
            "disks": f"'Hard disk 1' ({size:,} GB)",
            "limit_gb": 2048 if boot == "bios" else 4096,
            "boot_type": boot,
        }
        assert f"({size:,} GB)" in finding.reason
    else:
        assert _flags(readiness)["limits.unknown"] == "unknown"
        assert "boot type is not known" in readiness.findings[-1].reason


@pytest.mark.parametrize(
    ("boot", "sizes", "state"),
    [
        # Only one disk is over the limit: it may or may not be the OS disk.
        ("uefi", [120, 5000], "unknown"),
        ("bios", [120, 3000], "unknown"),
        (None, [120, 3000], "unknown"),
        # Every disk is over the limit: so is the OS disk, whichever it is.
        ("uefi", [5000, 6000], "not_ready"),
        ("bios", [3000, 3000], "not_ready"),
        (None, [5000, 6000], "not_ready"),
        (None, [3000, 5000], "unknown"),
        # No disk is over the limit.
        ("uefi", [3000, 3000], "ready"),
        ("bios", [120, 2048], "ready"),
    ],
)
def test_os_disk_check_does_not_depend_on_the_disk_order(
    boot: str | None, sizes: list[float], state: str
) -> None:
    results = [
        _assess(boot_type=boot, disks=[_disk(f"disk-{size}", size) for size in order])
        for order in (sizes, list(reversed(sizes)))
    ]

    assert [r.state for r in results] == [state, state]
    assert _flags(results[0]) == _flags(results[1])
    if state == "unknown":
        reason = next(f.reason for f in results[0].findings if f.flag == "limits.unknown")
        assert "which disk is the OS disk" in reason or "boot type is not known" in reason
        assert f"'disk-{max(sizes)}' ({max(sizes):,} GB)" in reason
    if state == "not_ready":
        finding = next(f for f in results[0].findings if f.flag == "limits.os_disk")
        assert all(f"({size:,} GB)" in finding.reason for size in sizes)


def test_data_disk_size_and_count_limits() -> None:
    big = _assess(disks=[_disk("os"), _disk("data-1", 40000)])
    migration = next(f for f in big.findings if f.flag == "limits.data_disk_migration")
    assert migration.state == "ready_with_conditions"
    assert migration.inputs == {"disks": "'data-1' (40,000 GB)", "limit_gb": 32768}
    assert "'data-1' (40,000 GB)" in migration.reason and "32,768 GB" in migration.reason
    too_big = _assess(disks=[_disk("os"), _disk("data-1", 70000), _disk("data-2", 70001.5)])
    assert too_big.state == "not_ready"
    size = next(f for f in too_big.findings if f.flag == "limits.data_disk_size")
    assert size.inputs == {
        "disks": "'data-1' (70,000 GB), 'data-2' (70,001.5 GB)",
        "limit_gb": 65536,
    }
    assert "'data-2' (70,001.5 GB)" in size.reason and "65,536 GB" in size.reason
    many = _assess(disks=[_disk("os"), *(_disk(f"data-{n}", 10) for n in range(65))])
    assert _flags(many)["limits.disk_count"] == "not_ready"
    assert "limits.disk_count" not in _flags(_assess(disks=[_disk("os"), *[_disk("d", 1)] * 64]))


def test_a_single_disk_has_no_data_disk_findings() -> None:
    readiness = _assess(boot_type="uefi", disks=[_disk("only", 40000)])
    assert set(_flags(readiness)) >= {"limits.os_disk"}
    assert not {"limits.data_disk_migration", "limits.data_disk_size"} & set(_flags(readiness))


def test_missing_disks_or_disk_sizes_are_unknown() -> None:
    assert _assess(disks=None).state == "unknown"
    assert _assess(disks=[]).state == "unknown"
    sized = _assess(
        disks=[_disk("os"), _disk("data-1", None)], missing={"disks.1.size_gb": "no_permission"}
    )
    assert _flags(sized)["limits.unknown"] == "unknown"


# --- features ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "flag"),
    [
        ("shared", "features.shared_disk"),
        ("raw_mapping", "features.raw_device_mapping"),
        ("independent", "features.independent_disk"),
    ],
)
def test_disk_features_must_be_removed_first(field: str, flag: str) -> None:
    readiness = _assess(disks=[_disk("os"), _disk("data-1", **{field: True})])
    assert readiness.state == "ready_with_conditions"
    assert _flags(readiness)[flag] == "ready_with_conditions"
    assert next(f.inputs for f in readiness.findings if f.flag == flag) == {"disks": "'data-1'"}


def test_snapshots_must_be_removed_or_merged() -> None:
    readiness = _assess(snapshot_count=3)
    assert _flags(readiness)["features.snapshots"] == "ready_with_conditions"
    assert "3 snapshot(s)" in readiness.findings[-1].reason


def test_unknown_features_are_unknown() -> None:
    raw_unknown = _assess(
        disks=[_disk("os", raw_mapping=None)], missing={"disks.0.raw_mapping": "not_supported"}
    )
    assert _flags(raw_unknown)["features.unknown"] == "unknown"
    assert raw_unknown.state == "unknown"
    assert _flags(_assess(snapshot_count=None))["features.unknown"] == "unknown"


# --- the operation ----------------------------------------------------------


def _operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub=_OPERATOR_SUB,
        raw_jwt="fake.jwt.value",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


def _envelope(
    records: list[ServerRecord], target: str, extra: list[dict[str, Any]]
) -> dict[str, Any]:
    dumped = [record.model_dump(mode="json") for record in records] + extra
    return {
        "schema_version": "1",
        "source": {"connector_id": "vmware-rest-9.0", "target": target, "collected_at": _TIME},
        "records": dumped,
        "omissions": [{"what": "host esx-example-02", "reason": "no_permission"}],
        "counts": {
            "server": len(records),
            "application": len(extra),
            "kubernetes_workload": 0,
        },
    }


async def _store(store: ResultHandleStore, tenant: UUID, envelope: dict[str, Any]) -> str:
    _reduced, handle = await JsonFluxReducer(byte_threshold=256, store=store).reduce(
        envelope,
        None,
        context={
            "tenant_id": str(tenant),
            "operator_sub": _OPERATOR_SUB,
            "op_id": "vmware.inventory.read",
        },
    )
    assert handle is not None
    return str(handle.handle_id)


async def test_readiness_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ResultHandleStore(MemoryValkey())  # type: ignore[arg-type]
    monkeypatch.setattr(
        "meho_backplane.connectors.migration.inventory_load.get_result_handle_store",
        lambda: store,
    )
    tenant = uuid4()
    ready = _server()
    snapshot = _server(
        source_id="vmware:vc-example:vm-2",
        name="db-example-01",
        identity_hints={"bios_uuid": "4203a1b2-0000-4000-8000-000000000002"},
        snapshot_count=1,
    )
    too_big = _server(
        source_id="vmware:vc-example:vm-3",
        name=None,
        identity_hints={},
        cores=4000,
        missing={"identity_hints.bios_uuid": "not_reported_by_source"},
    )
    application = {
        "kind": "application",
        "source_id": "app:example",
        "name": "example-db",
        "collected_at": _TIME,
        "missing": {
            "version": "not_reported_by_source",
            "vendor": "not_reported_by_source",
            "category": "not_reported_by_source",
        },
    }
    first = await _store(
        store, tenant, _envelope([ready, snapshot, too_big], "vc-example", [application])
    )
    guest = _server(source_id="linux:app-example-01:machine-id", os=None)
    second = await _store(store, tenant, _envelope([guest], "app-example-01", []))

    result = await readiness_op.migration_assess_readiness(
        _operator(tenant), None, {"inventory_handles": [first, second], "region": "westeurope"}
    )

    assert result["counts"] == {
        "ready": 1,
        "ready_with_conditions": 1,
        "not_ready": 1,
        "unknown": 0,
    }
    assert [item["name"] for item in result["items"]] == [
        "app-example-01",
        "db-example-01",
        "(name not reported)",
    ]
    assert result["items"][1]["flags"] == ["features.snapshots"]
    assert result["items"][2]["readiness"]["state"] == "not_ready"
    assert result["skipped_records"] == {"application": 1}
    assert result["duplicates"] == [
        {
            "source_id": "linux:app-example-01:machine-id",
            "same_as": "vmware:vc-example:vm-1",
            "match": "bios_uuid",
        }
    ]
    assert result["assumptions"]["region"] == "westeurope"
    assert "os_disk" in result["assumptions"]["other"]
    assert result["calculation_version"] == "readiness-1"
    assert [i["handle_id"] for i in result["inputs"]["inventories"]] == [first, second]
    assert len(result["omissions"]) == 2
    assert set(readiness_op.MIGRATION_ASSESS_READINESS_RESPONSE_SCHEMA["properties"]) >= set(result)
    core = {k: v for k, v in result.items() if k not in {"counts", "skipped_records", "duplicates"}}
    assert AssessmentResult.model_validate(core).items[0].readiness is not None


async def test_unknown_findings_are_listed_as_unknowns(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ResultHandleStore(MemoryValkey())  # type: ignore[arg-type]
    monkeypatch.setattr(
        "meho_backplane.connectors.migration.inventory_load.get_result_handle_store",
        lambda: store,
    )
    tenant = uuid4()
    handle = await _store(store, tenant, _envelope([_server(boot_type=None)], "vc-example", []))

    result = await readiness_op.migration_assess_readiness(
        _operator(tenant), None, {"inventory_handles": [handle]}
    )

    assert result["counts"]["unknown"] == 1
    assert result["unknowns"] == [
        {
            "source_id": "vmware:vc-example:vm-1",
            "what": "boot.unknown",
            "reason": RULES["boot.unknown"].reason,
        }
    ]
    assert result["assumptions"]["region"] is None


def test_finding_needs_a_source() -> None:
    with pytest.raises(ValidationError):
        ReadinessFinding.model_validate(
            {"flag": "x", "state": "ready", "reason": "r", "source_title": "t", "source_url": ""}
        )


def test_operation_id_has_no_write_word() -> None:
    assert write_word_in_op_id(readiness_op.MIGRATION_ASSESS_READINESS_OP_ID) is None
