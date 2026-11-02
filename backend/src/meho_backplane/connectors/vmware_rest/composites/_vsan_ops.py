# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry rows and AI guidance of the five vSAN changes.

``_register.py`` collects :data:`COMPOSITES`. Each row carries its approval
preview, names the vendor calls the change may make, and says that the
broadcast classifier puts its op id in ``other`` (none sends a secret). The
rows share the group
:data:`~meho_backplane.connectors.vmware_rest.typed_ops_vsan_common.VSAN_GROUP_KEY`
with the six typed vSAN reads. Every vendor call they make exists on vCenter
8.0 U1 to U3 and 9.0 to 9.1.1, so no version check is needed and the rows are
on both catalogs.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites import (
    _vsan_cluster,
    _vsan_disk_claim,
    _vsan_disk_wipe,
)
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._vsan_config_schemas import (
    VSAN_CLUSTER_CONFIGURE_PARAMETER_SCHEMA,
    VSAN_CLUSTER_JOB_RESPONSE_SCHEMA,
    VSAN_DISK_CLAIM_PARAMETER_SCHEMA,
    VSAN_DISK_CLAIM_RESPONSE_SCHEMA,
    VSAN_DISK_WIPE_PARAMETER_SCHEMA,
    VSAN_DISK_WIPE_RESPONSE_SCHEMA,
    VSAN_ENCRYPTION_REKEY_PARAMETER_SCHEMA,
    VSAN_ENCRYPTION_REKEY_RESPONSE_SCHEMA,
    VSAN_ENCRYPTION_SET_PARAMETER_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_common import VSAN_GROUP_KEY

__all__ = ["COMPOSITES"]

_JOB_LOST_REPLY: Final = (
    "Call it with async: true (MEHO's async mode): MEHO keeps the answer with the task id "
    "under a run handle, so a lost reply does not lose the task id. After a lost reply do "
    "not start again blindly: read the run handle, or vmware.tasks.recent for the cluster's "
    "vSAN tasks. Calling again is safe too: while a vSAN job runs on the cluster, the "
    "operation returns it as already_running and starts nothing."
)

_JOB_OUTPUT: Final = (
    "{status: accepted|already_running|not_found|invalid_request, cluster, cluster_name, "
    "requested, task, status_read: {op_id: vmware.task.info, params: {task}}, guidance}. "
    "accepted means vSAN took the job; it does not mean the work is done."
)

_JOB_NEXT: Final = (
    "Call status_read (vmware.task.info with the task id) until its state is success or "
    "error; not_reachable means vCenter did not answer, read again later with the same task "
    "id. Then read the vSAN health (vmware.host.vsan_health)."
)

_CONFIGURE_LLM: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to turn vSAN on or off for a cluster, to pick ESA when you turn it on, to set "
        "space efficiency (deduplication and compression) or to turn the vSAN performance "
        "service on or off. " + _JOB_LOST_REPLY
    ),
    "preconditions": (
        "The target is a vCenter. The hosts are in the cluster and have a vSAN network. A "
        "space efficiency change writes all data again; do it in a quiet hour. The call "
        "waits for approval before it runs."
    ),
    "parameter_hints": {
        "cluster": "The cluster moid, like domain-c<N>.",
        "esa": "Only with vsan_enabled: true. Use it when the hosts have ESA-ready disks.",
        "space_efficiency": "OSA clusters only; ESA sets compression in the storage policy.",
    },
    "output_shape": _JOB_OUTPUT,
    "next_step": _JOB_NEXT + " After turning vSAN on, claim disks with vsan.disk.claim.",
}

_ENCRYPTION_SET_LLM: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to turn vSAN data-at-rest encryption (the disks) or data-in-transit encryption "
        "(the traffic between hosts) on or off for a cluster. " + _JOB_LOST_REPLY
    ),
    "preconditions": (
        "The target is a vCenter and vSAN is on. To turn data-at-rest encryption on, name "
        "the key provider in key_provider: vSAN needs it, there is no default. "
        "vmware.vsan.cluster.info shows the key provider a cluster uses now. A change of "
        "data-at-rest encryption writes all data again. The call waits for approval."
    ),
    "parameter_hints": {
        "data_at_rest": (
            "{enabled, key_provider, erase_disks_before_use?}; key_provider is required "
            "when enabled is true."
        ),
        "data_in_transit": "{enabled, rekey_interval_minutes?}.",
    },
    "output_shape": _JOB_OUTPUT,
    "next_step": _JOB_NEXT,
}

_REKEY_LLM: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to rekey the data-at-rest encryption of a vSAN cluster: shallow (a new key "
        "encryption key, quick) or deep (new data keys; all data is written again)."
    ),
    "preconditions": (
        "The target is a vCenter and vSAN data-at-rest encryption is on. A deep rekey loads "
        "the cluster for hours. The call waits for approval before it runs."
    ),
    "parameter_hints": {"deep_rekey": "false unless a data key may be known to others."},
    "output_shape": (
        "{status: rekeyed|timeout|not_found|invalid_request, cluster, cluster_name, "
        "requested: {deep_rekey}, task, status_read, issues?, guidance}. A failed task is an "
        "error with vCenter's fault type and text."
    ),
    "next_step": (
        "On timeout follow the task with status_read (vmware.task.info with the task id); do "
        "not start the rekey again. Then read the vSAN health (vmware.host.vsan_health)."
    ),
}

_CLAIM_LLM: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to add disks of one host to vSAN: to an ESA storage pool, or as an OSA disk "
        "group of cache and capacity disks. Safe to run twice: disks vSAN already uses give "
        "unchanged."
    ),
    "preconditions": (
        "vSAN is on for the host's cluster (vsan.cluster.configure). Every named disk must "
        "be eligible; a disk with old partitions is ineligible: wipe it first with "
        "vsan.disk.wipe. The call waits for approval before it runs."
    ),
    "parameter_hints": {
        "host": "The host moid, like host-<N>.",
        "architecture": "esa or osa: the architecture of the cluster.",
        "disks": "ESA: canonical disk names, like naa.<id>.",
    },
    "output_shape": (
        "{status: claimed|unchanged|not_verified|timeout|precondition_failed|invalid_request, "
        "host, host_name, architecture, disks: [{name, display_name, device_path, ssd, "
        "capacity_bytes, vsan_state, vsan_uuid, vsan_error}], task, task_state, issues?, "
        "guidance}. disks is a flat list of rows."
    ),
    "next_step": (
        "On claimed, read the vSAN health (vmware.host.vsan_health). On precondition_failed, "
        "read vsan_state and vsan_error of each disk. On timeout, follow the task with "
        "vmware.task.info."
    ),
}

_WIPE_LLM: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to wipe disks of one host so vSAN can claim them, for example disks with old "
        "partitions. All data on the disks is lost for good."
    ),
    "preconditions": (
        "A person runs it: an agent is always refused. Call preview_operation first and pass "
        "its preview_hash; a second person approves after reading the disks and their "
        "partitions. Refused for a disk vSAN uses or one that holds a VMFS datastore."
    ),
    "parameter_hints": {
        "disks": "Canonical disk names, like naa.<id>.",
        "method": "partitions (quick) unless the data must be overwritten (secure).",
    },
    "output_shape": (
        "{status: wiped|unchanged|failed_part_way|timeout|precondition_failed|"
        "invalid_request, object: {host, host_name, method, disks}, disks: [disk rows with "
        "partitions], blockers: [{disk, reason}], steps: [{disk, device_path, status, "
        "error?}], disks_after, task, task_state, issues?, guidance}."
    ),
    "next_step": (
        "On wiped, claim the disks with vsan.disk.claim. On failed_part_way, steps says which "
        "disks were wiped, which failed (with vCenter's error) and which were not touched; "
        "nothing was undone. On timeout, follow the task with vmware.task.info."
    ),
}

_TAGS: Final = ("composite", "write", "vsan")

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.vsan.cluster.configure",
        handler=_vsan_cluster.vsan_cluster_configure_composite,
        summary=(
            "Turn vSAN on or off for a cluster and set ESA, space efficiency and the "
            "performance service."
        ),
        description=(
            "Starts one vSAN reconfigure job on a cluster (VsanClusterReconfig) and answers "
            "accepted with the vCenter task id; it does not wait. Starts nothing when a vSAN "
            "job already runs on the cluster."
        ),
        parameter_schema=VSAN_CLUSTER_CONFIGURE_PARAMETER_SCHEMA,
        response_schema=VSAN_CLUSTER_JOB_RESPONSE_SCHEMA,
        group_key=VSAN_GROUP_KEY,
        tags=[*_TAGS, "cluster"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_CONFIGURE_LLM,
        preview=_vsan_cluster.vsan_cluster_configure_preview,
        governed_subops=_vsan_cluster._VSAN_SUB_OPS_CLUSTER_CONFIGURE,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.vsan.disk.claim",
        handler=_vsan_disk_claim.vsan_disk_claim_composite,
        summary="Claim disks of one host for vSAN: an ESA storage pool or an OSA disk group.",
        description=(
            "Checks that every named disk is eligible for vSAN, then adds the disks of one "
            "host to vSAN (ESA storage pool or OSA disk group) and waits for the vCenter task. "
            "Returns the disks read again."
        ),
        parameter_schema=VSAN_DISK_CLAIM_PARAMETER_SCHEMA,
        response_schema=VSAN_DISK_CLAIM_RESPONSE_SCHEMA,
        group_key=VSAN_GROUP_KEY,
        tags=[*_TAGS, "host", "disk"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_CLAIM_LLM,
        preview=_vsan_disk_claim.vsan_disk_claim_preview,
        governed_subops=_vsan_disk_claim._VSAN_SUB_OPS_DISK_CLAIM,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.vsan.disk.wipe",
        handler=_vsan_disk_wipe.vsan_disk_wipe_composite,
        summary="Wipe disks of one host so vSAN can claim them; all data on them is lost.",
        description=(
            "Erases the partition table of each named disk, or runs the vSAN secure wipe. "
            "Refused for a disk vSAN uses or one with a VMFS datastore. A second person "
            "always approves, after a preview that lists each disk and its partitions."
        ),
        parameter_schema=VSAN_DISK_WIPE_PARAMETER_SCHEMA,
        response_schema=VSAN_DISK_WIPE_RESPONSE_SCHEMA,
        group_key=VSAN_GROUP_KEY,
        tags=[*_TAGS, "host", "disk"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=_WIPE_LLM,
        preview=_vsan_disk_wipe.vsan_disk_wipe_preview,
        governed_subops=_vsan_disk_wipe._VSAN_SUB_OPS_DISK_WIPE,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.vsan.encryption.set",
        handler=_vsan_cluster.vsan_encryption_set_composite,
        summary="Turn vSAN data-at-rest or data-in-transit encryption on or off for a cluster.",
        description=(
            "Starts one vSAN reconfigure job with the encryption settings and answers accepted "
            "with the vCenter task id; it does not wait. Starts nothing when a vSAN job "
            "already runs on the cluster. Turning data-at-rest encryption on needs the key "
            "provider."
        ),
        parameter_schema=VSAN_ENCRYPTION_SET_PARAMETER_SCHEMA,
        response_schema=VSAN_CLUSTER_JOB_RESPONSE_SCHEMA,
        group_key=VSAN_GROUP_KEY,
        tags=[*_TAGS, "cluster", "encryption"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_ENCRYPTION_SET_LLM,
        preview=_vsan_cluster.vsan_encryption_set_preview,
        governed_subops=_vsan_cluster._VSAN_SUB_OPS_ENCRYPTION_SET,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.vsan.encryption.rekey",
        handler=_vsan_cluster.vsan_encryption_rekey_composite,
        summary="Rekey vSAN encryption of a cluster: shallow (new key encryption key) or deep.",
        description=(
            "Starts a vSAN rekey (VsanEncryptedClusterRekey_Task) and waits for the vCenter "
            "task. A deep rekey makes new data keys and writes all data again."
        ),
        parameter_schema=VSAN_ENCRYPTION_REKEY_PARAMETER_SCHEMA,
        response_schema=VSAN_ENCRYPTION_REKEY_RESPONSE_SCHEMA,
        group_key=VSAN_GROUP_KEY,
        tags=[*_TAGS, "cluster", "encryption"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_REKEY_LLM,
        preview=_vsan_cluster.vsan_encryption_rekey_preview,
        governed_subops=_vsan_cluster._VSAN_SUB_OPS_ENCRYPTION_REKEY,
        classifier_other=True,
    ),
)
