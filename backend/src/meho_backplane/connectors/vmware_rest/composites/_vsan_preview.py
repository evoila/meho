# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval previews of the five vSAN changes, registered at import.

The disk wipe is ``destructive``: its preview is the handler's own plan as the
``blast_radius`` (the host, each disk with its partitions, the blockers). The
other four are ``dangerous``; their previews echo the cluster or host and the
settings that change, with no vendor call.

It lives here, not in :mod:`._write_preview`, so the vSAN changes touch the
shared registry lists in one place only.
"""

from __future__ import annotations

from meho_backplane.connectors.vmware_rest.composites._vsan_cluster import (
    vsan_cluster_configure_preview,
    vsan_encryption_rekey_preview,
    vsan_encryption_set_preview,
)
from meho_backplane.connectors.vmware_rest.composites._vsan_disk_claim import (
    vsan_disk_claim_preview,
)
from meho_backplane.connectors.vmware_rest.composites._vsan_disk_wipe import (
    vsan_disk_wipe_preview,
)
from meho_backplane.operations._preview import register_preview_builder

VSAN_PREVIEW_BUILDERS = {
    "vmware.composite.vsan.cluster.configure": vsan_cluster_configure_preview,
    "vmware.composite.vsan.disk.claim": vsan_disk_claim_preview,
    "vmware.composite.vsan.disk.wipe": vsan_disk_wipe_preview,
    "vmware.composite.vsan.encryption.set": vsan_encryption_set_preview,
    "vmware.composite.vsan.encryption.rekey": vsan_encryption_rekey_preview,
}

for _op_id, _builder in VSAN_PREVIEW_BUILDERS.items():
    register_preview_builder(_op_id, _builder)
