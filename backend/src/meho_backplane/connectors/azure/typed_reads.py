# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure typed read operations.

Each function here is the body behind a thin method on
:class:`~meho_backplane.connectors.azure.connector.AzureConnector`. The
operation table is in :mod:`meho_backplane.connectors.azure.typed_ops`, the
guidance for the AI in :mod:`meho_backplane.connectors.azure._llm_instructions`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._paging import list_all_pages
from meho_backplane.connectors.azure._paths import SUBSCRIPTIONS_API_VERSION, locations_path
from meho_backplane.connectors.azure._polling import OperationReference, read_operation_state
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = ["azure_about_impl", "azure_location_list_impl", "azure_operation_status_impl"]


async def azure_about_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.about``: ``GET /subscriptions/{subscriptionId}`` plus the login's ids.

    Returns the subscription ID, name and state, the tenant ID, the cloud, and
    the app ID and object ID of the login. The two ids are read from the
    access token's claims; the token itself is never returned.
    """
    del params  # the operation takes no parameters
    config, payload = await connector.read_subscription(target, operator)
    login = await connector.login_identity(target, operator)
    return {
        "subscription_id": payload.get("subscriptionId"),
        "display_name": payload.get("displayName"),
        "state": payload.get("state"),
        "tenant_id": payload.get("tenantId"),
        "cloud": config.cloud.name,
        "app_id": login.app_id,
        "object_id": login.object_id,
    }


def _location_row(item: object) -> dict[str, Any] | None:
    """One row of ``azure.location.list`` from one Azure ``Location``."""
    if not isinstance(item, dict):
        return None
    metadata = item.get("metadata")
    meta: dict[str, Any] = metadata if isinstance(metadata, dict) else {}
    paired = meta.get("pairedRegion")
    paired_names = [
        region["name"]
        for region in (paired if isinstance(paired, list) else [])
        if isinstance(region, dict) and isinstance(region.get("name"), str)
    ]
    return {
        "name": item.get("name"),
        "display_name": item.get("displayName"),
        "region_type": meta.get("regionType"),
        # geography is the Azure geography (Europe, Germany, United States).
        # geographyGroup is the wider group (Europe, US) and can change.
        "geography": meta.get("geography"),
        "geography_group": meta.get("geographyGroup"),
        "paired_regions": paired_names,
    }


async def azure_location_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.location.list``: ``GET /subscriptions/{subscriptionId}/locations``.

    Returns one row per Azure region the subscription can use: name, display
    name, region type, geography and paired regions. Azure has more than 50
    regions, so the dispatcher stores the rows and returns a link to them.
    """
    del params  # the operation takes no parameters
    config = resolve_target_config(target)
    paged = await list_all_pages(
        connector,
        target,
        locations_path(config.subscription_id),
        operator=operator,
        api_version=SUBSCRIPTIONS_API_VERSION,
    )
    paged.rows = [row for row in map(_location_row, paged.rows) if row is not None]
    return paged.as_dict()


async def azure_operation_status_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.operation.status``: poll a long-running operation once.

    Takes the ``operation`` reference an Azure write returned and reads the
    operation's state with one GET on its poll URL, after the same URL check
    as every poll. Never sends the write again.
    """
    reference = OperationReference.from_dict(params.get("operation"))
    state = await read_operation_state(connector, target, reference, operator=operator)
    return state.as_dict(reference)
