# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA provider ops for regions and organization quotas (VCF Automation 9.x).

Reads (safe):

* ``vcfa.provider.infra.list`` -- vCenters, NSX managers and Supervisors
* ``vcfa.provider.region.get`` -- one region with zones, storage policies, VM classes
* ``vcfa.provider.org_quota.list`` -- organization quotas, one row each

Changes (dangerous, a person approves each call):

* ``vcfa.provider.vcenter.register`` -- trust the certificate, add, switch on, refresh
* ``vcfa.provider.nsx_manager.register`` -- add an NSX manager
* ``vcfa.provider.region.set`` -- create or change a region
* ``vcfa.provider.org_quota.set`` -- create or change an organization quota

Each change starts a vendor job and returns ``accepted`` with the job id and
the status read to follow. Handlers live in :mod:`._infra_reads`,
:mod:`._infra_connect`, :mod:`._infra_nsx`, :mod:`._infra_region` and
:mod:`._infra_quota`.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vcf_automation._infra_paths import (
    INFRA_REGION_PATH,
    INFRA_VCENTERS_PATH,
    INFRA_VDCS_PATH,
)
from meho_backplane.connectors.vcf_automation._infra_schemas import (
    GROUP,
    JOB_ID_PARAM,
    NAME,
    READ_COMMON,
    READ_OBJECTS,
    READ_SCALARS,
    REF,
)
from meho_backplane.connectors.vcf_automation._op_schemas import field, guidance, list_of, obj
from meho_backplane.connectors.vcf_automation.infra_change_ops import VCFA_INFRA_CHANGE_OPS
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp, validate_typed_ops

__all__ = ["VCFA_INFRA_OPS", "VCFA_INFRA_OP_IDS"]

# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

_INFRA_LIST = VcfaTypedOp(
    op_id="vcfa.provider.infra.list",
    handler_attr="provider_infra_list",
    plane="provider",
    path=INFRA_VCENTERS_PATH,
    summary="List the vCenters, NSX managers and Supervisors VCF Automation knows.",
    description=(
        "One flat list: vCenters (connection state), NSX managers (state) and the "
        "Supervisors VCF Automation found, each with its region (none = free). Status "
        "read of vcenter.register and nsx_manager.register."
    ),
    parameter_schema={
        "type": "object",
        "properties": dict(JOB_ID_PARAM),
        "additionalProperties": False,
        "examples": [{}, {"job_id": "job-id-from-the-change"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            **READ_COMMON,
            "items": list_of(
                "One row per vCenter, NSX manager and Supervisor.",
                {
                    "kind": field(
                        "string",
                        "vcenter, nsx_manager or supervisor.",
                        enum=["vcenter", "nsx_manager", "supervisor"],
                    ),
                    "id": field(["string", "null"], "The id (a URN)."),
                    "name": field(["string", "null"], "The name."),
                    "url": field(["string", "null"], "The URL (vCenter, NSX manager)."),
                    "state": field(
                        ["string", "null"],
                        "vCenter: the connection state (CONNECTED is ready; INITIAL often "
                        "means an untrusted certificate). NSX manager: REALIZED is ready. "
                        "Supervisor: in_region or free.",
                    ),
                    "is_enabled": field(["boolean", "null"], "True when it is switched on."),
                    "is_connected": field(["boolean", "null"], "vCenter: true when connected."),
                    "version": field(["string", "null"], "The product version."),
                    "region": {
                        **obj("Supervisor: its region; null when free.", REF),
                        "type": ["object", "null"],
                    },
                    "vcenter": {
                        **obj("Supervisor: its vCenter.", REF),
                        "type": ["object", "null"],
                    },
                },
            ),
        },
        "required": ["state", "items"],
        "additionalProperties": True,
    },
    group_key=GROUP,
    tags=("read-only", "vcfa", "provider", "infra"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call to see which vCenters and NSX managers are connected, which Supervisors "
            "are free for a region, or to follow a vcenter.register or nsx_manager.register "
            "job (pass its job_id)."
        ),
        output_shape=(
            "{state, reachable, checked_at, items: [{kind, id, name, url, state, is_enabled, "
            "is_connected, version, region, vcenter}], job, guidance}. A long list comes back "
            "as a result handle; read it with result_query."
        ),
        next_step=(
            "Build a region from a REALIZED NSX manager and free Supervisors with "
            "vcfa.provider.region.set."
        ),
        result_scalars=READ_SCALARS,
        result_objects=READ_OBJECTS,
    ),
)

_REGION_GET = VcfaTypedOp(
    op_id="vcfa.provider.region.get",
    handler_attr="provider_region_get",
    plane="provider",
    path=INFRA_REGION_PATH,
    summary="Read one VCF Automation region: state, capacity, zones, storage policies, VM classes.",
    description=(
        "Reads one region by name or id, with its zones (CPU and memory), its storage "
        "policies and its VM classes. Status read of vcfa.provider.region.set."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "region": {
                **NAME,
                "description": "The region name or id (urn:vcloud:region:...).",
            },
            **JOB_ID_PARAM,
        },
        "required": ["region"],
        "additionalProperties": False,
        "examples": [{"region": "<region-name>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            **READ_COMMON,
            "region": {
                **obj(
                    "The region; null when not found.",
                    {
                        "id": field("string", "The region id."),
                        "name": field("string", "The region name."),
                        "description": field(["string", "null"], "The description."),
                        "status": field(
                            "string", "READY, NOT_READY (a job runs), ERROR or FAILED."
                        ),
                        "nsxManager": obj("The NSX manager of the region.", REF),
                        "supervisors": list_of("The Supervisors of the region.", REF),
                        "storagePolicies": {
                            "type": "array",
                            "description": "The vCenter storage policy names of the region.",
                            "items": {"type": "string"},
                        },
                        "cpuCapacityMHz": field(["integer", "null"], "Total CPU in MHz."),
                        "memoryCapacityMiB": field(["integer", "null"], "Total memory in MiB."),
                    },
                ),
                "type": ["object", "null"],
            },
            "zones": list_of(
                "The zones of the region.",
                {
                    "id": field("string", "The zone id."),
                    "name": field("string", "The zone name."),
                    "cpuLimitMHz": field(["integer", "null"], "CPU of the zone in MHz."),
                    "memoryLimitMiB": field(["integer", "null"], "Memory of the zone in MiB."),
                },
            ),
            "storage_policies": list_of(
                "The storage policies of the region.",
                {
                    "id": field("string", "The policy id."),
                    "name": field("string", "The policy name."),
                    "status": field(["string", "null"], "READY or NOT_READY."),
                    "storageCapacityMiB": field(["integer", "null"], "Capacity in MiB."),
                },
            ),
            "vm_classes": list_of(
                "The VM classes of the region.",
                {
                    "id": field("string", "The VM class id."),
                    "name": field("string", "The VM class name."),
                    "cpuCount": field(["integer", "null"], "Virtual CPUs."),
                    "memoryMiB": field(["integer", "null"], "Memory in MiB."),
                },
            ),
        },
        "required": ["state", "region"],
        "additionalProperties": True,
    },
    group_key=GROUP,
    tags=("read-only", "vcfa", "provider", "infra", "region"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call before vcfa.provider.org_quota.set for the zone, storage policy and VM "
            "class names, or to follow a vcfa.provider.region.set job (pass its job_id)."
        ),
        output_shape=(
            "{state, reachable, checked_at, region: {id, name, status, nsxManager, "
            "supervisors, storagePolicies, cpuCapacityMHz, memoryCapacityMiB}, zones, "
            "storage_policies, vm_classes, job, guidance}."
        ),
        next_step=(
            "When status is READY, give an organization a quota with vcfa.provider.org_quota.set."
        ),
        result_scalars=READ_SCALARS,
        result_objects=READ_OBJECTS,
    ),
)

_ORG_QUOTA_LIST = VcfaTypedOp(
    op_id="vcfa.provider.org_quota.list",
    handler_attr="provider_org_quota_list",
    plane="provider",
    path=INFRA_VDCS_PATH,
    summary="List the organization quotas per region: CPU and memory per zone, storage, VMs.",
    description=(
        "One row per organization quota: org, region, status, Supervisors, CPU and memory "
        "per zone, storage limits per policy and VM classes, with a warning when namespaces "
        "cannot use it. Status read of vcfa.provider.org_quota.set."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "org": {**NAME, "description": "Optional: only the quotas of this organization."},
            "region": {**NAME, "description": "Optional: only the quotas in this region."},
            **JOB_ID_PARAM,
        },
        "additionalProperties": False,
        "examples": [{"org": "<org-name>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            **READ_COMMON,
            "items": list_of(
                "One row per organization quota.",
                {
                    "id": field("string", "The quota id."),
                    "name": field("string", "The quota name."),
                    "org": obj("The organization.", REF),
                    "region": obj("The region.", REF),
                    "status": field(
                        ["string", "null"], "READY, NOT_READY (a job runs) or an error."
                    ),
                    "supervisors": list_of("The Supervisors of the quota.", REF),
                    "zones": list_of(
                        "CPU and memory per zone.",
                        {
                            "zone": obj("The zone.", REF),
                            "cpu_limit_mhz": field(["integer", "null"], "CPU limit in MHz."),
                            "cpu_reservation_mhz": field(
                                ["integer", "null"], "Reserved CPU in MHz."
                            ),
                            "memory_limit_mib": field(["integer", "null"], "Memory limit in MiB."),
                            "memory_reservation_mib": field(
                                ["integer", "null"], "Reserved memory in MiB."
                            ),
                        },
                    ),
                    "storage_policies": list_of(
                        "Storage limit per storage policy.",
                        {
                            "id": field("string", "The id of the quota's policy."),
                            "name": field(["string", "null"], "The policy name."),
                            "storage_limit_mib": field(["integer", "null"], "Limit in MiB."),
                            "storage_used_mib": field(["integer", "null"], "Used in MiB."),
                        },
                    ),
                    "vm_classes": list_of("The VM classes the quota offers.", REF),
                    "warnings": {
                        "type": "array",
                        "description": "Why namespaces of this quota would fail, if so.",
                        "items": {"type": "string"},
                    },
                },
            ),
        },
        "required": ["state", "items"],
        "additionalProperties": True,
    },
    group_key=GROUP,
    tags=("read-only", "vcfa", "provider", "infra", "quota"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call to see what each organization may use in each region, or to follow a "
            "vcfa.provider.org_quota.set job (pass its job_id)."
        ),
        output_shape=(
            "{state, reachable, checked_at, items: [{id, name, org, region, status, "
            "supervisors, zones, storage_policies, vm_classes, warnings}], job, guidance}. "
            "A long list comes back as a result handle; read it with result_query."
        ),
        next_step="Change a quota with vcfa.provider.org_quota.set.",
        result_scalars=READ_SCALARS,
        result_objects=READ_OBJECTS,
    ),
)


VCFA_INFRA_OPS: Final[tuple[VcfaTypedOp, ...]] = (
    _INFRA_LIST,
    _REGION_GET,
    _ORG_QUOTA_LIST,
    *VCFA_INFRA_CHANGE_OPS,
)

#: The op ids as literal text, so the CLI coverage check finds each one.
VCFA_INFRA_OP_IDS: Final[tuple[str, ...]] = (
    "vcfa.provider.infra.list",
    "vcfa.provider.region.get",
    "vcfa.provider.org_quota.list",
    "vcfa.provider.vcenter.register",
    "vcfa.provider.nsx_manager.register",
    "vcfa.provider.region.set",
    "vcfa.provider.org_quota.set",
)

assert tuple(op.op_id for op in VCFA_INFRA_OPS) == VCFA_INFRA_OP_IDS
validate_typed_ops(VCFA_INFRA_OPS)
