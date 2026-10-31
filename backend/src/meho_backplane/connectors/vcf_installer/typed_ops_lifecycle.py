# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata for the Installer reuse checks, cancel and appliance reads.

Seven typed operations (handlers in :mod:`.typed_lifecycle`), registered by
:func:`~meho_backplane.connectors.vcf_installer.typed_ops.register_installer_typed_operations`
together with the bring-up and depot ops:

* group ``installer-discovery`` — ``installer.discovery.vcenter``,
  ``installer.discovery.sddc_manager`` (Installer 9.1 and newer) and
  ``installer.discovery.vcf_operations``: reads that check a running system
  for reuse in a bring-up;
* group ``installer-bringup`` — ``installer.sddc.cancel`` (``dangerous``,
  waits for approval) and ``installer.sddc.spec.get`` (read, secrets
  replaced);
* group ``installer-appliance`` — ``installer.appliance.info`` and
  ``installer.release.list``.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vcf_installer._op_model import (
    EMPTY_PARAMS,
    InstallerTypedOp,
    instructions,
)
from meho_backplane.connectors.vcf_installer._response_schemas import (
    APPLIANCE_INFO_RESPONSE,
    DISCOVERY_SDDC_MANAGER_RESPONSE,
    DISCOVERY_VCENTER_RESPONSE,
    DISCOVERY_VCF_OPERATIONS_RESPONSE,
    RELEASE_LIST_RESPONSE,
    SDDC_CANCEL_RESPONSE,
    SDDC_SPEC_RESPONSE,
)
from meho_backplane.connectors.vcf_installer.typed_lifecycle import (
    DISCOVERY_SDDC_MANAGER_OP_ID,
    DISCOVERY_VCENTER_OP_ID,
    DISCOVERY_VCF_OPERATIONS_OP_ID,
    INSTALLER_SDDC_CANCEL_OP_ID,
)

__all__ = [
    "GROUP_APPLIANCE",
    "GROUP_DISCOVERY",
    "INSTALLER_LIFECYCLE_OPS",
    "LIFECYCLE_WHEN_TO_USE_BY_GROUP",
]

GROUP_DISCOVERY = "installer-discovery"
GROUP_APPLIANCE = "installer-appliance"

#: The bring-up group key; its text lives in :mod:`.typed_ops`.
_GROUP_BRINGUP = "installer-bringup"

#: Curated ``when_to_use`` text of the two groups this module adds.
LIFECYCLE_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    GROUP_DISCOVERY: (
        "Use before a bring-up that reuses a system that already runs. "
        "installer.discovery.vcenter checks an existing vCenter, "
        "installer.discovery.sddc_manager an existing SDDC Manager (Installer 9.1 "
        "and newer only) and installer.discovery.vcf_operations an existing VCF "
        "Operations. All three are reads: the Installer logs in to that system "
        "with the login stored in the Vault secret you name (secret_ref) and "
        "reports its version and what runs with it. They change nothing."
    ),
    GROUP_APPLIANCE: (
        "Use to learn about the Installer appliance itself. "
        "installer.appliance.info reads its version, role and network settings; "
        "installer.release.list lists the VCF releases it knows, one row per "
        "release. Both are reads."
    ),
}


def _discovery_params(system: str, *, username_note: str) -> dict[str, Any]:
    """The parameter schema of one discovery call."""
    placeholder = system.lower().replace(" ", "-")
    return {
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "minLength": 1,
                "description": f"The FQDN or IP address of the existing {system}.",
            },
            "ssl_thumbprint": {
                "type": "string",
                "minLength": 1,
                "description": f"The SHA-256 thumbprint of the {system} TLS certificate.",
            },
            "secret_ref": {
                "type": "string",
                "minLength": 1,
                "description": (
                    f"The Vault path of a secret that holds the {system} login under the "
                    f"keys username and password ({username_note}). MEHO reads it under "
                    "your identity; the password is never a parameter."
                ),
            },
            "secret_mount": {
                "type": "string",
                "minLength": 1,
                "description": "The Vault KV mount of secret_ref. Leave out for secret.",
            },
        },
        "required": ["address", "ssl_thumbprint", "secret_ref"],
        "additionalProperties": False,
        "examples": [
            {
                "address": f"<{placeholder}-fqdn>",
                "ssl_thumbprint": f"<{placeholder}-sha256-thumbprint>",
                "secret_ref": f"<vault-path-of-{placeholder}-login>",
            }
        ],
    }


def _discovery_hints(system: str) -> dict[str, str]:
    return {
        "address": f"The {system} FQDN, as the Installer can resolve it.",
        "ssl_thumbprint": f"SHA-256 thumbprint of the {system} certificate.",
        "secret_ref": "Vault path, for example the secret_ref of a MEHO target for it.",
        "secret_mount": "Omit unless the secret is on another KV mount.",
    }


_DISCOVERY_PRECONDITIONS = (
    "The login is stored in Vault under the keys username and password, and you "
    "can read that path. The Installer must reach the address."
)

_DISCOVERY_VCENTER = InstallerTypedOp(
    op_id=DISCOVERY_VCENTER_OP_ID,
    handler_attr="discovery_vcenter",
    summary="Check an existing vCenter for reuse in a bring-up (changes nothing).",
    description=(
        "Sends the address, TLS thumbprint and login of a running vCenter to POST "
        "/v1/sddcs/vcenter-discovery. The Installer logs in and returns its version, "
        "build and NSX. The login comes from the Vault secret at secret_ref. Read-only."
    ),
    parameter_schema=_discovery_params("vCenter", username_note="the SSO user"),
    response_schema=DISCOVERY_VCENTER_RESPONSE,
    group_key=GROUP_DISCOVERY,
    # "credential": the request holds the vCenter password, so a trace keeps
    # no request or answer body of this call.
    tags=("read-only", "vcf", "installer", "discovery", "vcenter", "credential"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=instructions(
        when_to_call=(
            "Call before you build an SddcSpec that reuses a running vCenter, to "
            "check that the Installer can reach it and log in."
        ),
        output_shape="{vcenterVersion, vcenterBuildNumber, nsxInfo?: {address}}.",
        next_step=(
            "Put the vCenter into the SddcSpec (vcenterSpec with useExistingDeployment "
            "true) and check the spec with installer.sddc.spec.validate. On an error, "
            "fix the address, thumbprint or login and call again."
        ),
        preconditions=_DISCOVERY_PRECONDITIONS,
        parameter_hints=_discovery_hints("vCenter"),
    ),
)

_DISCOVERY_SDDC_MANAGER = InstallerTypedOp(
    op_id=DISCOVERY_SDDC_MANAGER_OP_ID,
    handler_attr="discovery_sddc_manager",
    summary="Check an existing SDDC Manager for reuse in a bring-up (Installer 9.1+).",
    description=(
        "Sends the address, TLS thumbprint and admin login of a running SDDC Manager "
        "to POST /v1/sddcs/sddcm-discovery. Returns the SDDC Manager, its management "
        "vCenter and the state of each VCF management component. Installer 9.1 and "
        "newer. Read-only."
    ),
    parameter_schema=_discovery_params("SDDC Manager", username_note="the admin user"),
    response_schema=DISCOVERY_SDDC_MANAGER_RESPONSE,
    group_key=GROUP_DISCOVERY,
    tags=("read-only", "vcf", "installer", "discovery", "sddc-manager", "credential"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=instructions(
        when_to_call=(
            "Call before a bring-up that reuses a running SDDC Manager, to see what "
            "it runs: its vCenter and each management component."
        ),
        output_shape=(
            "{sddcManager: {fqdn, version, vcfInstanceName}, managementVcenter: {fqdn, "
            "version, status}, vcfManagementComponents: {<component>: {fqdn, "
            "deploymentStatus, deploymentType}}}."
        ),
        next_step=(
            "Use what it found in the SddcSpec, then check the spec with "
            "installer.sddc.spec.validate. On a 9.0 Installer this call stops with "
            "'not available on this version'."
        ),
        preconditions=(
            f"{_DISCOVERY_PRECONDITIONS} The Installer runs 9.1 or newer "
            "(installer.appliance.info)."
        ),
        parameter_hints=_discovery_hints("SDDC Manager"),
    ),
)

_DISCOVERY_VCF_OPERATIONS = InstallerTypedOp(
    op_id=DISCOVERY_VCF_OPERATIONS_OP_ID,
    handler_attr="discovery_vcf_operations",
    summary="Check an existing VCF Operations for reuse in a bring-up (changes nothing).",
    description=(
        "Sends the address, TLS thumbprint and admin login of a running VCF "
        "Operations to POST /v1/sddcs/vcfops-discovery. Returns its version, nodes "
        "and the vCenter and NSX adapters it has. Read-only."
    ),
    parameter_schema=_discovery_params(
        "VCF Operations", username_note="the admin user; username may be left out"
    ),
    response_schema=DISCOVERY_VCF_OPERATIONS_RESPONSE,
    group_key=GROUP_DISCOVERY,
    tags=("read-only", "vcf", "installer", "discovery", "vcf-operations", "credential"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=instructions(
        when_to_call=(
            "Call before a bring-up that reuses a running VCF Operations, to check "
            "that the Installer can log in and to see its nodes."
        ),
        output_shape=(
            "{vcfOperationsVersion, vcfOperationsBuildNumber, vcfOperationsNodes: "
            "[{address, name, type}], vcfOperationsManagementNode?, vcenterAdapters?, "
            "nsxAdapters?, ...}."
        ),
        next_step=(
            "Put VCF Operations into the SddcSpec as an existing deployment and check "
            "the spec with installer.sddc.spec.validate."
        ),
        preconditions=_DISCOVERY_PRECONDITIONS,
        parameter_hints=_discovery_hints("VCF Operations"),
    ),
)

_SDDC_CANCEL = InstallerTypedOp(
    op_id=INSTALLER_SDDC_CANCEL_OP_ID,
    handler_attr="sddc_cancel",
    summary="Cancel a running VCF bring-up task on the Installer.",
    description=(
        "Sends DELETE /v1/tasks/{id}. Only a task with status IN_PROGRESS can be "
        "cancelled; otherwise the Installer answers 409 and the error says why. A "
        "person must approve first."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The id of the running bring-up task, from installer.sddc.list or "
                    "installer.sddc.bringup.start."
                ),
            },
        },
        "required": ["id"],
        "additionalProperties": False,
        "examples": [{"id": "<bringup-task-id>"}],
    },
    response_schema=SDDC_CANCEL_RESPONSE,
    group_key=_GROUP_BRINGUP,
    tags=("vcf", "installer", "bringup", "cancel", "dangerous"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=instructions(
        when_to_call=(
            "Call to stop a bring-up that is still running, for example one started "
            "with a wrong spec."
        ),
        output_shape="{id, status: 'cancel_requested'}.",
        next_step=(
            "Call installer.sddc.bringup.status with the same id until status is no "
            "longer IN_PROGRESS, and report the final status."
        ),
        preconditions=(
            "installer.sddc.bringup.status or installer.sddc.list shows the task "
            "IN_PROGRESS. The bring-up stops part way, so agree with the operator what "
            "happens to what it built so far. A person must approve the call before "
            "it runs."
        ),
        parameter_hints={"id": "The bring-up task id from installer.sddc.list."},
    ),
)

_SDDC_SPEC_GET = InstallerTypedOp(
    op_id="installer.sddc.spec.get",
    handler_attr="sddc_spec_get",
    summary="Read the SddcSpec a bring-up used, with passwords removed.",
    description=(
        "Reads GET /v1/sddcs/{id}/spec: the deploy spec of a bring-up that started. "
        "Every password, credential and key field holds [REDACTED:param_name]. Needs "
        "the bring-up task id. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "The bring-up task id, from installer.sddc.list.",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
        "examples": [{"id": "<bringup-task-id>"}],
    },
    response_schema=SDDC_SPEC_RESPONSE,
    group_key=_GROUP_BRINGUP,
    # "credential": the vendor answer can hold the spec passwords before
    # MEHO replaces them, so a trace keeps no answer body of this call.
    tags=("read-only", "vcf", "installer", "bringup", "spec", "credential"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=instructions(
        when_to_call=(
            "Call to see which spec a bring-up used, for example to explain a failure "
            "or to prepare a fixed spec for installer.sddc.bringup.retry."
        ),
        output_shape=(
            "The SddcSpec object (sddcId, vcenterSpec, hostSpecs, networkSpecs, "
            "dnsSpec, ...). Every secret field holds [REDACTED:param_name]."
        ),
        next_step=(
            "To retry with a fixed spec, put the real passwords back in place of each "
            "[REDACTED:param_name] first; the Installer does not accept the marker."
        ),
        parameter_hints={"id": "The bring-up task id from installer.sddc.list."},
    ),
)

_APPLIANCE_INFO = InstallerTypedOp(
    op_id="installer.appliance.info",
    handler_attr="appliance_info",
    summary="Read the Installer appliance version, role and network settings.",
    description=(
        "Reads GET /v1/system/appliance-info: version and role, and on 9.1 also "
        "dnsDomain, dnsServers, ntpServers and ipAddresses. Read-only."
    ),
    parameter_schema=EMPTY_PARAMS,
    response_schema=APPLIANCE_INFO_RESPONSE,
    group_key=GROUP_APPLIANCE,
    tags=("read-only", "vcf", "installer", "appliance", "system"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=instructions(
        when_to_call=(
            "Call to learn which Installer version a target runs, for example before "
            "you build an SddcSpec for it."
        ),
        output_shape=(
            "{version, role, dnsDomain?, dnsServers?, ntpServers?, ipAddresses?}. "
            "9.0 sends only version and role."
        ),
        next_step=(
            "Build the SddcSpec for this version. Call installer.release.list to see "
            "which VCF releases it can deploy."
        ),
        parameter_hints={},
    ),
)

_RELEASE_LIST = InstallerTypedOp(
    op_id="installer.release.list",
    handler_attr="release_list",
    summary="List the VCF releases this Installer knows, one row per release.",
    description=(
        "Reads GET /v1/releases. Each row has product, version, releaseDate, "
        "minInstallerVersion, eol and a bom map (component to version). Filter with "
        "version_eq, version_ge or include_only_compatible. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "version_eq": {
                "type": "string",
                "minLength": 1,
                "description": "Only the release with this version, for example 9.1.0.0.",
            },
            "version_ge": {
                "type": "string",
                "minLength": 1,
                "description": "Only releases with this version or newer.",
            },
            "include_only_compatible": {
                "type": "boolean",
                "description": "True: only releases that fit this system.",
            },
        },
        "additionalProperties": False,
        "examples": [{"include_only_compatible": True}],
    },
    response_schema=RELEASE_LIST_RESPONSE,
    group_key=GROUP_APPLIANCE,
    tags=("read-only", "vcf", "installer", "release", "list"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=instructions(
        when_to_call=(
            "Call to see which VCF releases the Installer can deploy, before you pick "
            "the release for a bring-up."
        ),
        output_shape=(
            "{elements: [{product, version, releaseDate, minInstallerVersion?, "
            "minCompatibleVcfVersion?, eol?, bom: {<component>: <version>}}], "
            "pageMetadata?}. A long list comes back as a result handle."
        ),
        next_step=(
            "Pick the release. Check that its minInstallerVersion is not newer than "
            "the version from installer.appliance.info, then download its bundles "
            "with installer.bundles.list and installer.bundles.download."
        ),
        parameter_hints={
            "version_eq": "One release version, for example 9.1.0.0.",
            "version_ge": "The oldest release version to list.",
            "include_only_compatible": "Set true to hide releases that do not fit.",
        },
    ),
)

#: The seven ops, in the order the registrar upserts them.
INSTALLER_LIFECYCLE_OPS: tuple[InstallerTypedOp, ...] = (
    _DISCOVERY_VCENTER,
    _DISCOVERY_SDDC_MANAGER,
    _DISCOVERY_VCF_OPERATIONS,
    _SDDC_CANCEL,
    _SDDC_SPEC_GET,
    _APPLIANCE_INFO,
    _RELEASE_LIST,
)
