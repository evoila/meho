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
    "BLOB_API_VERSION",
    "BLOB_CONTAINERS_PATH",
    "BLOB_LIST_PATH",
    "BLOB_PATH",
    "LOCATIONS_PATH",
    "RESOURCE_GRAPH_API_VERSION",
    "RESOURCE_GRAPH_PATH",
    "STORAGE_ACCOUNTS_PATH",
    "STORAGE_API_VERSION",
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
#
# Two hosts. The account and container lists go to Resource Manager, like
# every path above. The blob paths (``BLOB_LIST_PATH`` and ``BLOB_PATH``) are
# data-plane paths: they go to the storage account's own blob host,
# ``https://<account>.blob.core.windows.net``, which takes a token for Azure
# Storage and answers in XML. A data-plane path takes the Blob service
# version in the ``x-ms-version`` header, not as ``api-version``.

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Storage/storageAccounts``:
#: the storage accounts of one subscription (Storage Accounts - List).
STORAGE_ACCOUNTS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Storage/storageAccounts"
)

#: ``GET .../storageAccounts/{accountName}/blobServices/default/containers``:
#: the containers of one storage account, read on Resource Manager (Blob
#: Containers - List). The Reader role is enough.
BLOB_CONTAINERS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/"
    "Microsoft.Storage/storageAccounts/{account}/blobServices/default/containers"
)

#: The API version of the storage reads on Resource Manager (Microsoft.Storage).
STORAGE_API_VERSION: Final[str] = "2026-09-01"

#: ``GET /{containerName}?restype=container&comp=list`` on the account's blob
#: host: the files (blobs) of one container (List Blobs). The query part is
#: fixed; MEHO sends it with the other query values.
BLOB_LIST_PATH: Final[str] = "/{container}?restype=container&comp=list"

#: ``/{containerName}/{blob}`` on the account's blob host: ``HEAD`` reads one
#: file's properties (Get Blob Properties), ``GET`` its bytes (Get Blob).
BLOB_PATH: Final[str] = "/{container}/{blob}"

#: The Blob service version of the data-plane calls, sent as ``x-ms-version``.
#: The pinned spec files also have 2026-12-06, a date after this code was
#: written; the lab proof checks which version the lab account accepts.
BLOB_API_VERSION: Final[str] = "2026-10-06"


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
