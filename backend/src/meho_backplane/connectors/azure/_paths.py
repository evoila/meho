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
same time do not edit the same lines.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "LOCATIONS_PATH",
    "RESOURCE_GRAPH_API_VERSION",
    "RESOURCE_GRAPH_PATH",
    "SUBSCRIPTIONS_API_VERSION",
    "SUBSCRIPTION_PATH",
    "locations_path",
    "subscription_path",
]

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

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.HybridCompute/machines``:
#: the Azure Arc machines of one subscription (Machines - List By Subscription).
ARC_MACHINES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.HybridCompute/machines"
)

#: ``GET .../resourceGroups/{resourceGroupName}/providers/Microsoft.HybridCompute/
#: machines/{machineName}``: one Azure Arc machine (Machines - Get).
ARC_MACHINE_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    + "/providers/Microsoft.HybridCompute/machines/{name}"
)

#: ``POST .../Microsoft.HybridCompute/machines/{name}/assessPatches``: check one
#: Arc machine for missing updates (Machines - Assess Patches). Long-running.
ARC_ASSESS_PATCHES_PATH: Final[str] = ARC_MACHINE_PATH + "/assessPatches"

#: The API version of the Azure Arc calls (Microsoft.HybridCompute).
ARC_API_VERSION: Final[str] = "2026-09-01"

#: ``POST .../Microsoft.Compute/virtualMachines/{vmName}/assessPatches``: check
#: one Azure VM for missing updates (Virtual Machines - Assess Patches).
#: Long-running.
VM_ASSESS_PATCHES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    + "/providers/Microsoft.Compute/virtualMachines/{name}/assessPatches"
)

#: The API version of the VM patch assessment (Microsoft.Compute).
VM_ASSESS_API_VERSION: Final[str] = "2026-04-01"

# ---- automation (Task #5092) ----

# ---- avs (Task #5091) ----

# ---- compute (Task #5083) ----

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
