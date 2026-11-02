# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request paths of the day-2 ops (deployments, machines and disks).

Aria Automation 8.18 and VCF Automation 9 (tenant plane of "VM Apps"
organisations) serve the same deployment and IaaS APIs, so both connectors
use these paths: ``vcfa-rest-9.0`` (``vcfa.tenant.*``) and ``vcfa-vra8-8.0``
(``vcfa.vra8.*``). The path parameter names match the pinned
``vra-sdk-go`` v0.6.5 files (``vra-catalog-deployment.json`` and
``vra-iaas.json``), so the reconcile lanes compare them as they are. The request tracker
path is :data:`._paths.TENANT_REQUEST_TRACKER_PATH`.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "BLOCK_DEVICES_PATH",
    "DEPLOYMENT_ACTIONS_PATH",
    "DEPLOYMENT_REQUESTS_PATH",
    "MACHINES_PATH",
    "MACHINE_DISKS_PATH",
    "RESOURCE_ACTIONS_PATH",
    "RESOURCE_REQUESTS_PATH",
]

#: The day-2 actions of one deployment (``GET``).
DEPLOYMENT_ACTIONS_PATH: Final[str] = "/deployment/api/deployments/{deploymentId}/actions"
#: Run a day-2 action on one deployment (``POST`` ``{actionId, inputs, reason}``).
DEPLOYMENT_REQUESTS_PATH: Final[str] = "/deployment/api/deployments/{deploymentId}/requests"
#: The day-2 actions of one resource, for example a machine (``GET``).
RESOURCE_ACTIONS_PATH: Final[str] = "/deployment/api/resources/{resourceId}/actions"
#: Run a day-2 action on one resource (``POST`` ``{actionId, inputs, reason}``).
RESOURCE_REQUESTS_PATH: Final[str] = "/deployment/api/resources/{resourceId}/requests"
#: Machines (``GET`` list, ``POST`` create through the IaaS API).
MACHINES_PATH: Final[str] = "/iaas/api/machines"
#: Disks, called block devices (``GET`` list, ``POST`` create).
BLOCK_DEVICES_PATH: Final[str] = "/iaas/api/block-devices"
#: Attach a disk to a machine (``POST`` ``{blockDeviceId}``).
MACHINE_DISKS_PATH: Final[str] = "/iaas/api/machines/{id}/disks"
