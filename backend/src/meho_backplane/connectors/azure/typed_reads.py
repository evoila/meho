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
from meho_backplane.connectors.azure.session import AzureTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = ["azure_about_impl"]


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
