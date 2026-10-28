# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shapes every migration assessment step reads and writes.

Two shapes live here:

* The **inventory envelope**: what a source read (vCenter, Hyper-V, a Linux
  or Windows host, Kubernetes, ...) collected. It holds records of three
  kinds (server, application, Kubernetes workload). Every record has a
  stable ``source_id``. Units are fixed by the field name (``_mb``, ``_gb``,
  ``_mb_per_s``, ``_millicores``). A field that is unknown is ``None`` and
  says why in ``missing``, nested fields and list items included; it is
  never ``0``.
* The **assessment result**: every number (a :class:`Figure`) carries its
  inputs, the assumptions it used and the calculation version.

The models check their own rules, so a producer cannot hand on an
inventory whose counts do not match its records, or a number without a
reason when it is unknown. :func:`inventory_json_schema` and
:func:`assessment_result_json_schema` export them as JSON Schema for the
operations' response schemas.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "INVENTORY_SCHEMA_VERSION",
    "ApplicationRecord",
    "AssessmentInputs",
    "AssessmentItem",
    "AssessmentResult",
    "Assumptions",
    "Disk",
    "Figure",
    "IdentityHints",
    "InventoryCounts",
    "InventoryEnvelope",
    "InventoryInput",
    "InventoryRecord",
    "InventorySource",
    "KubernetesWorkloadRecord",
    "MissingReason",
    "Nic",
    "Omission",
    "OperatingSystem",
    "RecordKind",
    "ServerRecord",
    "Stat",
    "Unknown",
    "Utilization",
    "WorkloadVolume",
    "assessment_result_json_schema",
    "bios_uuid_match_key",
    "inventory_json_schema",
    "missing_reason",
]

#: The inventory shape version this code reads and writes. A reader refuses
#: any other value, so a later shape change cannot be read wrongly.
INVENTORY_SCHEMA_VERSION: Final[str] = "1"

#: Why a field or a whole part of the estate was not collected.
MissingReason = Literal[
    "not_reported_by_source",
    "no_permission",
    "powered_off",
    "not_measured",
    "collection_failed",
    "not_supported",
]

RecordKind = Literal["server", "application", "kubernetes_workload"]


class _Shape(BaseModel):
    """Strict base: an unknown field is an error, not silently dropped."""

    model_config = ConfigDict(extra="forbid")


def _null_paths(value: Any, prefix: str = "") -> Iterator[str]:
    """Dotted path of every null field under *value*, nested models and list items included.

    A list item gets its index as a path part: ``disks.0.used_gb``.
    """
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _null_paths(getattr(value, name), f"{prefix}.{name}" if prefix else name)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _null_paths(item, f"{prefix}.{index}")
    elif value is None:
        yield prefix


def missing_reason(missing: Mapping[str, str], path: str) -> str | None:
    """The reason for the null field at dotted *path*: its own entry, or the nearest parent's."""
    parts = path.split(".")
    for end in range(len(parts), 0, -1):
        reason = missing.get(".".join(parts[:end]))
        if reason:
            return reason
    return None


class _Record(_Shape):
    """Shared rule of every record: an unknown field says why it is unknown."""

    source_id: str = Field(
        min_length=1,
        description=(
            "Stable id from the source, for example "
            "'vmware:<vCenter instance uuid>:<VM instance uuid>'. The same thing "
            "read twice from the same source gets the same id."
        ),
    )
    collected_at: datetime = Field(description="When this record was read from the source.")
    missing: dict[str, MissingReason] = Field(
        default_factory=dict,
        description=(
            "Dotted field path to the reason it is unknown. Every field that is null "
            "must be covered, nested fields and list items too: 'cores', 'os.version', "
            "'disks.0.used_gb', 'utilization.cpu_percent.average'. A parent path "
            "('disks.0', 'disks') covers every unknown field below it."
        ),
    )

    @model_validator(mode="after")
    def _null_fields_have_a_reason(self) -> _Record:
        unexplained = [
            path for path in _null_paths(self) if missing_reason(self.missing, path) is None
        ]
        if unexplained:
            raise ValueError(
                f"fields {unexplained} are null but have no reason in 'missing'; "
                "unknown data needs a reason and is never zero"
            )
        return self


class IdentityHints(_Shape):
    """Facts that find one machine seen through two sources (vCenter and its guest OS)."""

    bios_uuid: str | None = Field(default=None, description="SMBIOS / BIOS UUID, lower case.")
    mac_addresses: list[str] = Field(
        default_factory=list, description="MAC addresses, lower case with ':'."
    )
    host_name: str | None = Field(default=None, description="Host name, lower case.")

    @field_validator("bios_uuid", "host_name")
    @classmethod
    def _lower(cls, value: str | None) -> str | None:
        return value.strip().lower() if value is not None else None

    @field_validator("mac_addresses")
    @classmethod
    def _lower_macs(cls, value: list[str]) -> list[str]:
        return [mac.strip().lower().replace("-", ":") for mac in value]


_UUID_GROUPS: Final = re.compile(
    r"^([0-9a-f]{8})-([0-9a-f]{4})-([0-9a-f]{4})-([0-9a-f]{4}-[0-9a-f]{12})$"
)


def _reversed_bytes(group: str) -> str:
    return "".join(group[i : i + 2] for i in range(len(group) - 2, -1, -2))


def bios_uuid_match_key(bios_uuid: str) -> str:
    """One key for the two ways a machine's BIOS UUID is reported.

    A hypervisor shows the UUID as it stores it. A guest OS reads it from
    the firmware table, where newer firmware keeps the first three groups
    in reversed byte order (a VMware VM from hardware version 13 on, for
    example), so the same machine shows two different texts. Both texts
    name one machine. The key is the smaller of a UUID and its twin with
    the first three groups reversed, so both texts give one key. A text
    that is not a UUID is its own key.
    """
    text = bios_uuid.strip().lower()
    parts = _UUID_GROUPS.match(text)
    if parts is None:
        return text
    twin = "-".join((*(_reversed_bytes(parts.group(i)) for i in (1, 2, 3)), parts.group(4)))
    return min(text, twin)


class Stat(_Shape):
    """Average, 95th percentile and maximum over the measurement window."""

    average: float | None = Field(default=None, ge=0, description="Average over the window.")
    p95: float | None = Field(default=None, ge=0, description="95th percentile over the window.")
    maximum: float | None = Field(default=None, ge=0, description="Highest value in the window.")


class Disk(_Shape):
    """One virtual or physical disk. Speeds use the server's measurement window."""

    label: str = Field(description="Disk name as the source shows it.")
    size_gb: float | None = Field(default=None, ge=0, description="Size in GB.")
    used_gb: float | None = Field(default=None, ge=0, description="Space in use in GB.")
    thin: bool | None = Field(default=None, description="Thin provisioned.")
    shared: bool | None = Field(default=None, description="Shared with another machine.")
    raw_mapping: bool | None = Field(default=None, description="Raw device mapping (RDM).")
    independent: bool | None = Field(default=None, description="Left out of snapshots.")
    read_ops_per_s: Stat | None = Field(default=None, description="Read operations per second.")
    write_ops_per_s: Stat | None = Field(default=None, description="Write operations per second.")
    read_mb_per_s: Stat | None = Field(default=None, description="Read throughput in MB/s.")
    write_mb_per_s: Stat | None = Field(default=None, description="Write throughput in MB/s.")


class OperatingSystem(_Shape):
    """The guest operating system as the source reports it."""

    name: str | None = Field(default=None, description="Full name, for example 'Ubuntu Linux'.")
    version: str | None = Field(default=None, description="Version text.")
    family: Literal["windows", "linux", "other"] | None = Field(default=None, description="Family.")
    architecture: str | None = Field(default=None, description="For example 'x64' or 'arm64'.")


class Nic(_Shape):
    """One network adapter."""

    mac_address: str | None = Field(default=None, description="MAC address.")
    ip_addresses: list[str] = Field(default_factory=list, description="IPv4 and IPv6 addresses.")
    network: str | None = Field(default=None, description="Network or port group name.")


class Utilization(_Shape):
    """Measured use over a window. Absent measurements make the whole block null."""

    window_start: datetime = Field(description="Start of the measurement window.")
    window_end: datetime = Field(description="End of the measurement window.")
    samples: int = Field(ge=1, description="Number of samples in the window.")
    source: str = Field(description="Where the numbers come from, for example 'vcenter-perf'.")
    cpu_percent: Stat | None = Field(default=None, description="CPU use in percent.")
    memory_percent: Stat | None = Field(default=None, description="Memory use in percent.")
    network_in_mb_per_s: Stat | None = Field(default=None, description="Received MB/s.")
    network_out_mb_per_s: Stat | None = Field(default=None, description="Sent MB/s.")


class ServerRecord(_Record):
    """A virtual or physical server."""

    kind: Literal["server"] = "server"
    identity_hints: IdentityHints = Field(default_factory=IdentityHints)
    name: str | None = Field(
        default=None, min_length=1, description="Server name as the source shows it."
    )
    server_type: Literal["virtual", "physical"] | None = None
    hypervisor: str | None = Field(default=None, description="For example 'VMware' or 'Hyper-V'.")
    cores: int | None = Field(default=None, ge=1, description="Processor cores allocated.")
    memory_configured_mb: int | None = Field(default=None, ge=1, description="Configured RAM.")
    memory_assigned_mb: int | None = Field(
        default=None, ge=0, description="RAM assigned now (differs with dynamic memory)."
    )
    disks: list[Disk] | None = None
    snapshot_count: int | None = Field(default=None, ge=0)
    os: OperatingSystem | None = None
    boot_type: Literal["bios", "uefi"] | None = None
    secure_boot: bool | None = None
    power_state: Literal["on", "off", "suspended"] | None = None
    nics: list[Nic] | None = None
    utilization: Utilization | None = None


class ApplicationRecord(_Record):
    """Software found on one or more servers (a database engine, a web server)."""

    kind: Literal["application"] = "application"
    name: str = Field(min_length=1, description="Application name.")
    version: str | None = None
    vendor: str | None = None
    category: str | None = Field(default=None, description="For example 'database'.")
    runs_on: list[str] = Field(
        default_factory=list, description="source_id of each server it runs on."
    )


class WorkloadVolume(_Shape):
    """A volume a Kubernetes workload mounts."""

    name: str = Field(description="Volume name in the pod spec.")
    type: str = Field(description="For example 'persistentVolumeClaim' or 'emptyDir'.")
    size_gb: float | None = Field(default=None, ge=0, description="Requested size in GB.")
    storage_class: str | None = Field(default=None, description="Storage class name.")


class KubernetesWorkloadRecord(_Record):
    """A Kubernetes workload. CPU and memory are the sums over its containers, per replica."""

    kind: Literal["kubernetes_workload"] = "kubernetes_workload"
    cluster: str | None = Field(default=None, description="Cluster name.")
    namespace: str = Field(min_length=1)
    workload_kind: str = Field(min_length=1, description="For example 'Deployment'.")
    name: str = Field(min_length=1)
    replicas: int | None = Field(default=None, ge=0)
    cpu_request_millicores: float | None = Field(default=None, ge=0)
    cpu_limit_millicores: float | None = Field(default=None, ge=0)
    memory_request_mb: float | None = Field(default=None, ge=0)
    memory_limit_mb: float | None = Field(default=None, ge=0)
    images: list[str] | None = None
    volumes: list[WorkloadVolume] | None = None
    nodes: list[str] | None = Field(default=None, description="Nodes its pods run on.")


InventoryRecord = Annotated[
    ServerRecord | ApplicationRecord | KubernetesWorkloadRecord,
    Field(discriminator="kind"),
]


class InventorySource(_Shape):
    """Where one inventory was read from."""

    connector_id: str = Field(min_length=1, description="Connector that read it.")
    target: str = Field(min_length=1, description="Target name.")
    collected_at: datetime = Field(description="When the collection finished.")


class Omission(_Shape):
    """A part of the estate that was not collected, and why."""

    what: str = Field(min_length=1, description="What was not collected.")
    reason: MissingReason
    detail: str | None = Field(default=None, description="More detail, if any.")


class InventoryCounts(_Shape):
    """Records per kind, plus the totals the source itself reports, for checking."""

    server: int = Field(ge=0)
    application: int = Field(ge=0)
    kubernetes_workload: int = Field(ge=0)
    source_totals: dict[RecordKind, int] = Field(
        default_factory=dict,
        description="The source's own total per kind, where it reports one.",
    )


class InventoryEnvelope(_Shape):
    """One inventory from one source."""

    schema_version: Literal["1"]
    source: InventorySource
    records: list[InventoryRecord]
    omissions: list[Omission] = Field(default_factory=list)
    counts: InventoryCounts

    @model_validator(mode="after")
    def _counts_match_and_ids_are_unique(self) -> InventoryEnvelope:
        actual = {"server": 0, "application": 0, "kubernetes_workload": 0}
        for record in self.records:
            actual[record.kind] += 1
        stated = {kind: getattr(self.counts, kind) for kind in actual}
        if stated != actual:
            raise ValueError(f"counts {stated} do not match the records {actual}")
        seen: set[str] = set()
        repeated: set[str] = set()
        for record in self.records:
            if record.source_id in seen:
                repeated.add(record.source_id)
            seen.add(record.source_id)
        if repeated:
            raise ValueError(f"source_id values repeat: {sorted(repeated)}")
        return self


# --- assessment result ------------------------------------------------------


class Figure(_Shape):
    """One number of an assessment, with everything needed to check it."""

    value: float | None = Field(description="The number; null when it is unknown.")
    unit: str = Field(min_length=1, description="For example 'EUR/month' or 'cores'.")
    basis: Literal["measured", "allocated"] | None = Field(
        default=None,
        description=(
            "'measured' when sized from utilization, 'allocated' when sized from "
            "configured values. Never 'right-sized' without measurements."
        ),
    )
    inputs: dict[str, str | float | int | bool | None] = Field(
        default_factory=dict, description="The input values the number was made from."
    )
    assumptions: list[str] = Field(
        default_factory=list, description="Names of the assumptions it used."
    )
    calculation_version: str = Field(min_length=1)
    unknown_reason: str | None = Field(default=None, description="Why the value is null.")

    @model_validator(mode="after")
    def _unknown_has_a_reason(self) -> Figure:
        if self.value is None and not self.unknown_reason:
            raise ValueError("an unknown value needs an unknown_reason; it is never zero")
        if self.value is not None and self.unknown_reason:
            raise ValueError("a known value has no unknown_reason")
        return self


class Assumptions(_Shape):
    """Everything an assessment assumed. Names in ``other`` are free."""

    region: str | None = None
    currency: str | None = None
    price_date: date | None = None
    hours_per_month: float | None = Field(default=None, gt=0)
    license: str | None = Field(default=None, description="For example 'pay-as-you-go'.")
    reservation: str | None = Field(default=None, description="For example 'none' or '1-year'.")
    comfort_factor: float | None = Field(default=None, gt=0)
    other: dict[str, str | float | int | bool] = Field(default_factory=dict)

    def names(self) -> set[str]:
        """Names a :class:`Figure` may cite: the set fields plus the ``other`` keys."""
        named = {
            name
            for name in type(self).model_fields
            if name != "other" and getattr(self, name) is not None
        }
        return named | set(self.other)


class InventoryInput(_Shape):
    """One inventory an assessment read."""

    handle_id: str = Field(min_length=1)
    connector_id: str = Field(min_length=1)
    target: str = Field(min_length=1)
    collected_at: datetime


class AssessmentInputs(_Shape):
    """What an assessment read, and when it ran."""

    inventories: list[InventoryInput]
    targets: list[str] = Field(default_factory=list, description="Other targets it read.")
    generated_at: datetime


class AssessmentItem(_Shape):
    """The numbers and flags for one record."""

    source_id: str = Field(min_length=1)
    name: str
    figures: dict[str, Figure] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)


class Unknown(_Shape):
    """Something the assessment could not decide, and why."""

    source_id: str | None = None
    what: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class AssessmentResult(_Shape):
    """The result of one assessment calculation."""

    schema_version: Literal["1"]
    calculation_version: str = Field(min_length=1)
    inputs: AssessmentInputs
    assumptions: Assumptions
    items: list[AssessmentItem]
    unknowns: list[Unknown] = Field(default_factory=list)
    omissions: list[Omission] = Field(default_factory=list)

    @model_validator(mode="after")
    def _figures_cite_known_assumptions(self) -> AssessmentResult:
        known = self.assumptions.names()
        for item in self.items:
            for key, figure in item.figures.items():
                unknown = sorted(set(figure.assumptions) - known)
                if unknown:
                    raise ValueError(
                        f"figure {key!r} of {item.source_id!r} cites assumptions "
                        f"{unknown} that are not set"
                    )
        return self


def inventory_json_schema() -> dict[str, Any]:
    """JSON Schema of :class:`InventoryEnvelope`."""
    return InventoryEnvelope.model_json_schema()


def assessment_result_json_schema() -> dict[str, Any]:
    """JSON Schema of :class:`AssessmentResult`."""
    return AssessmentResult.model_json_schema()
