# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-written Azure Resource Manager paths and API versions.

Every path the Azure connector calls is a constant in this module, so one CI
check can compare each of them with Azure's own API description files
(``backend/tests/test_connectors_azure_spec_reconcile.py``). A typo, a
renamed endpoint or a wrong API version then fails in CI and not on a
customer system. Later reads add their paths here, and each new ``*_PATH``
constant gets one line in that check.

Azure pins an API version per call (the ``api-version`` query parameter). The
versions are kept next to their paths, one per family of operations. Each
family keeps its paths in its own section below, so families built at the
same time do not edit the same lines. For the same reason the module has no
``__all__``: every family would add its names at the same place. The spec path
check finds the constants with ``vars()``.
"""

from __future__ import annotations

from typing import Final

#: ``GET /subscriptions/{subscriptionId}``: one subscription (Subscriptions - Get).
SUBSCRIPTION_PATH: Final[str] = "/subscriptions/{subscription_id}"

#: ``GET /subscriptions/{subscriptionId}/locations``: the Azure regions of one
#: subscription (Subscriptions - List Locations).
LOCATIONS_PATH: Final[str] = "/subscriptions/{subscription_id}/locations"

#: The API version of the subscription and location reads (Microsoft.Resources).
SUBSCRIPTIONS_API_VERSION: Final[str] = "2022-12-01"

#: ``POST /providers/Microsoft.ResourceGraph/resources``: one Resource Graph
#: query (Resources - Resources). A read, even though it is a POST.
RESOURCE_GRAPH_PATH: Final[str] = "/providers/Microsoft.ResourceGraph/resources"

#: The API version of Resource Graph queries (Microsoft.ResourceGraph).
RESOURCE_GRAPH_API_VERSION: Final[str] = "2024-04-01"

# ---- aks (Task #5087) ----

# ---- arc (Task #5084) ----

# ---- automation (Task #5092) ----

# ---- avs (Task #5091) ----

# ---- compute (Task #5083) ----
#
# Three Microsoft.Compute API versions, one per pinned spec file: VMs, images
# and scale sets (ComputeRP), managed disks (DiskRP), and the VM sizes per
# region (Resource SKUs).

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Compute/virtualMachines``:
#: every VM of the subscription (Virtual Machines - List All). With
#: ``statusOnly=true`` the same call returns the run-time state of each VM.
VIRTUAL_MACHINES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Compute/virtualMachines"
)

#: ``GET .../resourceGroups/{resourceGroupName}/providers/Microsoft.Compute/virtualMachines``:
#: the VMs of one resource group (Virtual Machines - List).
RESOURCE_GROUP_VIRTUAL_MACHINES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Compute/virtualMachines"
)

#: ``GET .../virtualMachines/{vmName}``: one VM (Virtual Machines - Get). With
#: ``$expand=instanceView`` the answer also holds its run-time state.
VIRTUAL_MACHINE_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Compute/virtualMachines/{vm_name}"
)

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Compute/images``:
#: the managed images of the subscription (Images - List).
IMAGES_PATH: Final[str] = "/subscriptions/{subscription_id}/providers/Microsoft.Compute/images"

#: The managed images of one resource group (Images - List By Resource Group).
RESOURCE_GROUP_IMAGES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Compute/images"
)

#: ``GET .../providers/Microsoft.Compute/virtualMachineScaleSets``: the scale
#: sets of the subscription (Virtual Machine Scale Sets - List All).
SCALE_SETS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Compute/virtualMachineScaleSets"
)

#: The scale sets of one resource group (Virtual Machine Scale Sets - List).
RESOURCE_GROUP_SCALE_SETS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Compute/virtualMachineScaleSets"
)

#: The API version of the VM, image and scale-set reads (Microsoft.Compute).
COMPUTE_API_VERSION: Final[str] = "2026-04-01"

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Compute/disks``:
#: the managed disks of the subscription (Disks - List).
DISKS_PATH: Final[str] = "/subscriptions/{subscription_id}/providers/Microsoft.Compute/disks"

#: The managed disks of one resource group (Disks - List By Resource Group).
RESOURCE_GROUP_DISKS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Compute/disks"
)

#: The API version of the managed-disk reads (Microsoft.Compute disks).
DISKS_API_VERSION: Final[str] = "2026-03-02"

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Compute/skus``:
#: the compute sizes the subscription can see (Resource Skus - List). Only the
#: filter ``location eq '<region>'`` works on this call.
COMPUTE_SKUS_PATH: Final[str] = "/subscriptions/{subscription_id}/providers/Microsoft.Compute/skus"

#: The API version of the size list. The Resource SKUs call has its own API
#: version, older than the one of the VM reads.
COMPUTE_SKUS_API_VERSION: Final[str] = "2021-07-01"

# ---- cost (Task #5088) ----

# ---- inventory (Task #5082) ----

# ---- migrate (Task #5093) ----

# ---- monitor (Task #5090) ----

# ---- network (Task #5085) ----

# ---- security (Task #5089) ----

# ---- storage (Task #5086) ----


def subscription_path(subscription_id: str) -> str:
    """Return the path of one subscription.

    *subscription_id* must already be checked as a GUID (see
    :func:`~meho_backplane.connectors.azure.session.resolve_target_config`), so
    it cannot change the path.
    """
    return SUBSCRIPTION_PATH.format(subscription_id=subscription_id)


def locations_path(subscription_id: str) -> str:
    """Return the path of the regions of one subscription.

    *subscription_id* must already be checked as a GUID, like in
    :func:`subscription_path`.
    """
    return LOCATIONS_PATH.format(subscription_id=subscription_id)
