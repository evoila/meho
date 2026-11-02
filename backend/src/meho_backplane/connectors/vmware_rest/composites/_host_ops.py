# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host writes on one ESXi host: mount an NFS datastore, mark disks as flash, control a service.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites import _host
from meho_backplane.connectors.vmware_rest.composites._host import (
    datastore_mount_nfs_composite,
    disk_mark_flash_composite,
    service_control_composite,
)
from meho_backplane.connectors.vmware_rest.composites._host_cluster_llm_instructions import (
    HOST_CLUSTER_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _host_datastore_mount_nfs_preview,
    _host_disk_mark_flash_preview,
    _host_service_control_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    HOST_DATASTORE_MOUNT_NFS_PARAMETER_SCHEMA,
    HOST_DATASTORE_MOUNT_NFS_RESPONSE_SCHEMA,
    HOST_DISK_MARK_FLASH_PARAMETER_SCHEMA,
    HOST_DISK_MARK_FLASH_RESPONSE_SCHEMA,
    HOST_SERVICE_CONTROL_PARAMETER_SCHEMA,
    HOST_SERVICE_CONTROL_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.host.datastore_mount_nfs",
        handler=datastore_mount_nfs_composite,
        summary="Mount an NFS export as a datastore on one ESXi host.",
        description=(
            "Mounts an NFS v3 or v4.1 export as a datastore on one host. Safe to run twice: an "
            "export that is already mounted returns already_mounted, and a name used by another "
            "export returns name_conflict, both without a change."
        ),
        parameter_schema=HOST_DATASTORE_MOUNT_NFS_PARAMETER_SCHEMA,
        response_schema=HOST_DATASTORE_MOUNT_NFS_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "storage", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.host.datastore_mount_nfs"],
        preview=_host_datastore_mount_nfs_preview,
        governed_subops=_host._VIM_SUB_OPS_HOST_DATASTORE_MOUNT_NFS,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.host.disk_mark_flash",
        handler=disk_mark_flash_composite,
        summary="Mark host disks as flash (SSD) or non-flash (HDD).",
        description=(
            "Marks one or more disks of a host as flash (SSD) or as non-flash (HDD), one task per "
            "disk. One failed disk does not stop the others; each disk gets its own result row. "
            "Like 'govc host.storage.mark -ssd'."
        ),
        parameter_schema=HOST_DISK_MARK_FLASH_PARAMETER_SCHEMA,
        response_schema=HOST_DISK_MARK_FLASH_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "storage", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.host.disk_mark_flash"],
        preview=_host_disk_mark_flash_preview,
        governed_subops=_host._VIM_SUB_OPS_HOST_DISK_MARK_FLASH,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.host.service_control",
        handler=service_control_composite,
        summary="Start, stop or restart an allowed host service; optionally set its start policy.",
        description=(
            "Starts, stops or restarts one ESXi host service and can set when it starts. Only "
            "TSM-SSH, TSM, ntpd, ptpd, snmpd, sfcbd-watchdog and slpd are allowed; others are "
            "refused before anything changes. Like 'govc host.service'."
        ),
        parameter_schema=HOST_SERVICE_CONTROL_PARAMETER_SCHEMA,
        response_schema=HOST_SERVICE_CONTROL_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "service", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.host.service_control"],
        preview=_host_service_control_preview,
        governed_subops=_host._VIM_SUB_OPS_HOST_SERVICE_CONTROL,
        classifier_other=True,
    ),
)
