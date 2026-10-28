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

# ---- cost (Task #5088) ----

# ---- inventory (Task #5082) ----

#: ``GET /subscriptions``: every subscription the login can see (Subscriptions -
#: List). Sent with :data:`SUBSCRIPTIONS_API_VERSION`.
SUBSCRIPTIONS_PATH: Final[str] = "/subscriptions"

#: ``GET /subscriptions/{subscriptionId}/resourcegroups`` (Resource Groups - List).
RESOURCE_GROUPS_PATH: Final[str] = "/subscriptions/{subscription_id}/resourcegroups"

#: ``GET /subscriptions/{subscriptionId}/resources`` (Resources - List).
RESOURCES_PATH: Final[str] = "/subscriptions/{subscription_id}/resources"

#: ``GET /subscriptions/{subscriptionId}/resourceGroups/{resourceGroupName}/resources``
#: (Resources - List By Resource Group).
RESOURCE_GROUP_RESOURCES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}/resources"
)

#: ``GET /subscriptions/{subscriptionId}/tagNames``: tag names, their values and
#: counts (Tags - List).
TAG_NAMES_PATH: Final[str] = "/subscriptions/{subscription_id}/tagNames"

#: ``GET`` and ``PATCH /{scope}/providers/Microsoft.Resources/tags/default``: the
#: tags of one resource (Tags - Get At Scope, Tags - Update At Scope). *scope* is
#: a resource ID without its first ``/``.
TAGS_AT_SCOPE_PATH: Final[str] = "/{scope}/providers/Microsoft.Resources/tags/default"

#: The API version of the resource, resource group and tag calls
#: (Microsoft.Resources).
RESOURCES_API_VERSION: Final[str] = "2021-04-01"

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Authorization/locks``
#: (Management Locks - List At Subscription Level).
LOCKS_PATH: Final[str] = "/subscriptions/{subscription_id}/providers/Microsoft.Authorization/locks"

#: The API version of the lock reads (Microsoft.Authorization).
LOCKS_API_VERSION: Final[str] = "2020-05-01"

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
