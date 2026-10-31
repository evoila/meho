# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The changes of the VCFA region and quota ops (dangerous; a person approves each).

* ``vcfa.provider.vcenter.register`` -- trust the certificate, add, switch on, refresh
* ``vcfa.provider.nsx_manager.register`` -- add an NSX manager
* ``vcfa.provider.region.set`` -- create or change a region
* ``vcfa.provider.org_quota.set`` -- create or change an organization quota

Each starts a vendor job and returns ``accepted`` with the job id and the
status read to follow. The reads and the full op list are in :mod:`.infra_ops`.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vcf_automation._infra_paths import (
    INFRA_NSX_MANAGERS_PATH,
    INFRA_REGIONS_PATH,
    INFRA_VCENTERS_PATH,
    INFRA_VDCS_PATH,
)
from meho_backplane.connectors.vcf_automation._infra_schemas import (
    CHANGE_OBJECTS,
    CHANGE_SCALARS,
    GROUP,
    JOB_RULES,
    K8S_NAME,
    NAME,
    PASSWORD_PROPS,
    URL,
    WARNINGS,
    change_response,
)
from meho_backplane.connectors.vcf_automation._op_schemas import field, guidance, obj
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp

__all__ = ["VCFA_INFRA_CHANGE_OPS"]

# ---------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------

_VCENTER_REGISTER = VcfaTypedOp(
    op_id="vcfa.provider.vcenter.register",
    handler_attr="provider_vcenter_register",
    plane="provider",
    path=INFRA_VCENTERS_PATH,
    summary="Connect a vCenter to VCF Automation: trust its certificate, add, switch on, refresh.",
    description=(
        "Runs the next step a vCenter needs and returns when VCF Automation accepted that "
        "job: trust the certificate the approver saw, add the vCenter (switched on), "
        "switch it on again, or refresh it so its Supervisors appear."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {**NAME, "description": "The name of the vCenter in VCF Automation."},
            "url": {**URL, "description": "The vCenter URL, for example https://<vcenter-host>."},
            "username": {**NAME, "description": "The vCenter user VCF Automation logs in with."},
            **PASSWORD_PROPS,
            "description": {
                "type": "string",
                "maxLength": 512,
                "description": "Optional description of the vCenter.",
            },
            "refresh": {
                "type": "boolean",
                "description": (
                    "Refresh a connected vCenter so its Supervisors appear (default true)."
                ),
            },
        },
        "required": ["name", "url", "username", "password_secret_ref"],
        "additionalProperties": False,
        "examples": [
            {
                "name": "<vcenter-name>",
                "url": "https://<vcenter-host>",
                "username": "<vcenter-user>",
                "password_secret_ref": "<vault-path>",
            }
        ],
    },
    response_schema=change_response(
        [
            "accepted",
            "unchanged",
            "already_running",
            "certificate_not_trusted",
            "precondition_failed",
            "invalid_request",
            "failed_part_way",
        ],
        {
            "certificate": {
                **obj(
                    "The vCenter certificate as VCF Automation sees it; null when not checked.",
                    {
                        "ssl_result": field(
                            ["string", "null"], "SUCCESS, ERROR_UNTRUSTED_CERTIFICATE or an error."
                        ),
                        "fingerprint_sha256": field(
                            ["string", "null"], "SHA-256 fingerprint of the certificate to trust."
                        ),
                        "certificate_count": field("integer", "Certificates in the chain."),
                        "needs_trust": field("boolean", "True when it must be trusted first."),
                    },
                ),
                "type": ["object", "null"],
            }
        },
    ),
    group_key=GROUP,
    tags=("write", "vcfa", "provider", "infra", "vcenter", "credential"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to connect a vCenter, and again with the same inputs after each job ends: "
            "it runs the next step (add, then refresh) and skips the steps that are done."
        ),
        preconditions=(
            "Store the vCenter password in Vault and pass its path. Run preview_operation "
            "first: it shows the certificate fingerprint. Check it against the certificate to "
            "trust (the vCenter's own, or the issuer at the top when the chain has more than "
            "one) before you approve. " + JOB_RULES
        ),
        output_shape=(
            "{status, object: {kind, name, url, id, state}, job: {id, href, status_read}, "
            "certificate: {ssl_result, fingerprint_sha256, certificate_count, needs_trust}, "
            "steps: [{step, result, detail, job_id, http_status, error}], checked_at, "
            "guidance}. 'accepted' means the job started, not that it is done."
        ),
        next_step=(
            "Read vcfa.provider.infra.list with job_id until the vCenter state is CONNECTED, "
            "then call this op again to refresh it."
        ),
        result_scalars=CHANGE_SCALARS,
        result_objects={
            "objects": {
                **CHANGE_OBJECTS["objects"],
                "certificate": ["ssl_result", "fingerprint_sha256", "needs_trust"],
            }
        },
    ),
)

_NSX_REGISTER = VcfaTypedOp(
    op_id="vcfa.provider.nsx_manager.register",
    handler_attr="provider_nsx_manager_register",
    plane="provider",
    path=INFRA_NSX_MANAGERS_PATH,
    summary="Connect an NSX manager to VCF Automation (credentials from Vault).",
    description=(
        "Adds an NSX manager and returns when VCF Automation accepted the job. An NSX "
        "manager with the same URL or name is not added again. The description is always "
        "sent, because the vendor refuses a body without it."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {**NAME, "description": "The name of the NSX manager in VCF Automation."},
            "url": {**URL, "description": "The NSX manager URL, for example https://<nsx-host>."},
            "username": {**NAME, "description": "The NSX user VCF Automation logs in with."},
            **PASSWORD_PROPS,
            "description": {
                "type": "string",
                "maxLength": 512,
                "description": "Optional description; an empty one is sent when not given.",
            },
        },
        "required": ["name", "url", "username", "password_secret_ref"],
        "additionalProperties": False,
        "examples": [
            {
                "name": "<nsx-name>",
                "url": "https://<nsx-host>",
                "username": "<nsx-user>",
                "password_secret_ref": "<vault-path>",
            }
        ],
    },
    response_schema=change_response(
        ["accepted", "unchanged", "already_running", "invalid_request"], {}
    ),
    group_key=GROUP,
    tags=("write", "vcfa", "provider", "infra", "nsx", "credential"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call once per NSX manager, before you build a region on it.",
        preconditions=(
            "Store the NSX password in Vault and pass its path. A person must approve the "
            "call. " + JOB_RULES
        ),
        output_shape=(
            "{status, object: {kind, name, url, id, state}, job: {id, href, status_read}, "
            "steps, checked_at, guidance}. 'accepted' means the job started, not that it is "
            "done."
        ),
        next_step=(
            "Read vcfa.provider.infra.list with job_id until the NSX manager state is "
            "REALIZED, then use it in vcfa.provider.region.set."
        ),
        result_scalars=CHANGE_SCALARS,
        result_objects=CHANGE_OBJECTS,
    ),
)

_REGION_SET = VcfaTypedOp(
    op_id="vcfa.provider.region.set",
    handler_attr="provider_region_set",
    plane="provider",
    path=INFRA_REGIONS_PATH,
    summary="Create or change a VCF Automation region: NSX manager, Supervisors, storage policies.",
    description=(
        "Creates the region or changes it to match: one NSX manager, at least one "
        "Supervisor and at least one vCenter storage policy name. Returns when VCF "
        "Automation accepted the job. Deleting a region is not part of this op."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                **K8S_NAME,
                "description": "The region name: lower-case letters, digits and '-'.",
            },
            "nsx_manager": {**NAME, "description": "The NSX manager name or id."},
            "supervisors": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "description": "At least one free Supervisor, by name or id.",
                "items": {**NAME, "description": "A Supervisor name or id."},
            },
            "storage_policies": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "description": (
                    "At least one vCenter storage policy name; it must exist in every "
                    "vCenter of the region."
                ),
                "items": {**NAME, "description": "A vCenter storage policy name."},
            },
            "description": {
                "type": "string",
                "maxLength": 512,
                "description": "Optional description; kept as it is when not given.",
            },
        },
        "required": ["name", "nsx_manager", "supervisors", "storage_policies"],
        "additionalProperties": False,
        "examples": [
            {
                "name": "region-a",
                "nsx_manager": "<nsx-name>",
                "supervisors": ["<supervisor-name>"],
                "storage_policies": ["<storage-policy-name>"],
            }
        ],
    },
    response_schema=change_response(
        [
            "accepted",
            "unchanged",
            "already_running",
            "precondition_failed",
            "invalid_request",
        ],
        dict(WARNINGS),
    ),
    group_key=GROUP,
    tags=("write", "vcfa", "provider", "infra", "region"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to build a region from an NSX manager and free Supervisors, or to change "
            "its Supervisors or storage policies."
        ),
        preconditions=(
            "Read vcfa.provider.infra.list for the NSX manager (REALIZED) and the free "
            "Supervisors. A person approves the call; when the region changes after the "
            "preview, the run answers precondition_failed. " + JOB_RULES
        ),
        output_shape=(
            "{status, object: {kind, name, id}, job: {id, href, status_read}, steps, "
            "warnings, checked_at, guidance}. 'accepted' means the job started, not that it "
            "is done."
        ),
        next_step="Read vcfa.provider.region.get with job_id until the region status is READY.",
        result_scalars=CHANGE_SCALARS,
        result_objects=CHANGE_OBJECTS,
    ),
)

_ZONE_ITEM = {
    "type": "object",
    "description": "CPU and memory for one zone of the region.",
    "properties": {
        "zone": {**NAME, "description": "The zone name or id."},
        "cpu_limit_mhz": {"type": "integer", "minimum": 0, "description": "CPU limit in MHz."},
        "cpu_reservation_mhz": {
            "type": "integer",
            "minimum": 0,
            "description": "Reserved CPU in MHz.",
        },
        "memory_limit_mib": {
            "type": "integer",
            "minimum": 0,
            "description": "Memory limit in MiB.",
        },
        "memory_reservation_mib": {
            "type": "integer",
            "minimum": 0,
            "description": "Reserved memory in MiB.",
        },
    },
    "required": [
        "zone",
        "cpu_limit_mhz",
        "cpu_reservation_mhz",
        "memory_limit_mib",
        "memory_reservation_mib",
    ],
    "additionalProperties": False,
}

_ORG_QUOTA_SET = VcfaTypedOp(
    op_id="vcfa.provider.org_quota.set",
    handler_attr="provider_org_quota_set",
    plane="provider",
    path=INFRA_VDCS_PATH,
    summary="Create or change an org's quota in a region: CPU, memory, storage, VM classes.",
    description=(
        "Sets an organization's quota in a region in three steps: Supervisors with CPU and "
        "memory per zone, storage limits per storage policy, and the VM classes it offers. "
        "Returns when VCF Automation accepted the jobs."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "org": {**NAME, "description": "The organization name."},
            "region": {**NAME, "description": "The region name or id."},
            "name": {**NAME, "description": "The quota name (one quota per org and region)."},
            "description": {
                "type": "string",
                "maxLength": 512,
                "description": "Optional description of a new quota.",
            },
            "supervisors": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "description": "At least one Supervisor of the region, by name or id.",
                "items": {**NAME, "description": "A Supervisor name or id."},
            },
            "zones": {
                "type": "array",
                "minItems": 1,
                "description": "CPU and memory limits and reservations per zone.",
                "items": _ZONE_ITEM,
            },
            "storage_policies": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "Storage limit per storage policy of the region. Leave out to keep the "
                    "current ones; a quota without any cannot run namespaces."
                ),
                "items": {
                    "type": "object",
                    "description": "One storage policy and its limit.",
                    "properties": {
                        "name": {**NAME, "description": "The storage policy name or id."},
                        "storage_limit_mib": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "The storage limit in MiB.",
                        },
                    },
                    "required": ["name", "storage_limit_mib"],
                    "additionalProperties": False,
                },
            },
            "vm_classes": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "description": (
                    "The VM classes the quota offers (replaces the list). Leave out to keep "
                    "the current ones; a quota without any cannot run VMs."
                ),
                "items": {**NAME, "description": "A VM class name or id of the region."},
            },
        },
        "required": ["org", "region", "name", "supervisors", "zones"],
        "additionalProperties": False,
        "examples": [
            {
                "org": "<org-name>",
                "region": "<region-name>",
                "name": "<quota-name>",
                "supervisors": ["<supervisor-name>"],
                "zones": [
                    {
                        "zone": "<zone-name>",
                        "cpu_limit_mhz": 10000,
                        "cpu_reservation_mhz": 0,
                        "memory_limit_mib": 32768,
                        "memory_reservation_mib": 0,
                    }
                ],
                "storage_policies": [
                    {"name": "<storage-policy-name>", "storage_limit_mib": 102400}
                ],
                "vm_classes": ["<vm-class-name>"],
            }
        ],
    },
    response_schema=change_response(
        [
            "accepted",
            "changed",
            "unchanged",
            "already_running",
            "precondition_failed",
            "invalid_request",
            "failed_part_way",
        ],
        dict(WARNINGS),
    ),
    group_key=GROUP,
    tags=("write", "vcfa", "provider", "infra", "quota"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to give an organization a quota in a region, or to change its CPU, memory, "
            "storage or VM classes. Call again with the same inputs once a new quota reads "
            "READY: it then adds the storage limits and VM classes."
        ),
        preconditions=(
            "The region must be READY: read vcfa.provider.region.get for the zone, storage "
            "policy and VM class names. A person approves the call; when the quota changes "
            "after the preview, the run answers precondition_failed. " + JOB_RULES
        ),
        output_shape=(
            "{status, object: {kind, name, id, org, region}, job: {id, status_read}, steps: "
            "[quota, storage_policies, vm_classes], warnings, checked_at, guidance}. "
            "'failed_part_way' lists what ran, what failed and what did not run; nothing is "
            "undone."
        ),
        next_step=(
            "Read vcfa.provider.org_quota.list with job_id until the quota status is READY."
        ),
        result_scalars={"keys": ["status", "warnings", "checked_at", "guidance"]},
        result_objects=CHANGE_OBJECTS,
    ),
)

#: The region and quota ops: reads first, then changes.

#: The changes, in the order a site is set up.
VCFA_INFRA_CHANGE_OPS: Final[tuple[VcfaTypedOp, ...]] = (
    _VCENTER_REGISTER,
    _NSX_REGISTER,
    _REGION_SET,
    _ORG_QUOTA_SET,
)
