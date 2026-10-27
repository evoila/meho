# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-written Azure Resource Manager paths and API versions.

Every path the Azure connector calls is a constant in this module, so one CI
check can compare each of them with Azure's own API description files. That
check arrives with the pinned Azure spec files; from then on a typo or a
renamed endpoint fails in CI and not on a customer system. Later reads add
their paths here.

Azure pins an API version per call (the ``api-version`` query parameter). The
versions are kept next to their paths, one per family of operations.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "LOCKS_API_VERSION",
    "LOCKS_PATH",
    "RESOURCES_API_VERSION",
    "RESOURCES_PATH",
    "RESOURCE_GRAPH_API_VERSION",
    "RESOURCE_GRAPH_PATH",
    "RESOURCE_GROUPS_PATH",
    "RESOURCE_GROUP_RESOURCES_PATH",
    "SUBSCRIPTIONS_API_VERSION",
    "SUBSCRIPTIONS_PATH",
    "SUBSCRIPTION_PATH",
    "TAGS_AT_SCOPE_PATH",
    "TAG_NAMES_PATH",
    "subscription_path",
]

#: ``GET /subscriptions/{subscriptionId}``: one subscription (Subscriptions - Get).
SUBSCRIPTION_PATH: Final[str] = "/subscriptions/{subscription_id}"

#: The API version of the subscription reads (Microsoft.Resources).
SUBSCRIPTIONS_API_VERSION: Final[str] = "2022-12-01"

#: ``POST /providers/Microsoft.ResourceGraph/resources``: one Resource Graph
#: query (Resources - Resources). A read, even though it is a POST.
RESOURCE_GRAPH_PATH: Final[str] = "/providers/Microsoft.ResourceGraph/resources"

#: The API version of Resource Graph queries (Microsoft.ResourceGraph).
RESOURCE_GRAPH_API_VERSION: Final[str] = "2024-04-01"


def subscription_path(subscription_id: str) -> str:
    """Return the path of one subscription.

    *subscription_id* must already be checked as a GUID (see
    :func:`~meho_backplane.connectors.azure.session.resolve_target_config`), so
    it cannot change the path.
    """
    return SUBSCRIPTION_PATH.format(subscription_id=subscription_id)


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
