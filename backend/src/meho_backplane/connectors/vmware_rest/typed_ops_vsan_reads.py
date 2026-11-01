# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The six vSAN reads, as one tuple for the typed-op registry.

``typed_ops.VMWARE_TYPED_OPS`` takes :data:`VSAN_READ_OPS` in one place, so
later vSAN operations join here and not in the shared registry list.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_capacity import VMWARE_VSAN_CAPACITY_OP
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_cluster_info import (
    VMWARE_VSAN_CLUSTER_INFO_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_common import (
    VSAN_GROUP_KEY,
    VSAN_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_objects import (
    VMWARE_VSAN_OBJECT_HEALTH_OP,
    VMWARE_VSAN_RESYNC_STATUS_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_performance import (
    VMWARE_VSAN_PERFORMANCE_OP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_precheck import (
    VMWARE_VSAN_MAINTENANCE_PRECHECK_OP,
)

__all__ = ["VSAN_GROUP_KEY", "VSAN_READ_OPS", "VSAN_WHEN_TO_USE"]

VSAN_READ_OPS: Final[tuple[VmwareTypedOp, ...]] = (
    VMWARE_VSAN_CLUSTER_INFO_OP,
    VMWARE_VSAN_CAPACITY_OP,
    VMWARE_VSAN_PERFORMANCE_OP,
    VMWARE_VSAN_RESYNC_STATUS_OP,
    VMWARE_VSAN_OBJECT_HEALTH_OP,
    VMWARE_VSAN_MAINTENANCE_PRECHECK_OP,
)
