# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Web handler mixin (Phase 92).

Handlers for Azure App Service operations: web apps, function apps,
and App Service plans. Uses native async Azure SDK clients.
"""

from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.azure.helpers import (
    _extract_resource_group,
    _safe_tags,
)
from meho_app.modules.connectors.azure.serializers import (
    serialize_azure_function_app,
    serialize_azure_web_app,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.azure.connector import AzureConnector

logger = get_logger(__name__)


class WebHandlerMixin:
    """Mixin providing Azure Web/App Service operation handlers.

    Covers web apps, function apps, and App Service plans.
    IMPORTANT: Web apps and function apps both come from the same
    web_apps API -- distinguish by checking "functionapp" in site.kind.
    All methods use native async Azure SDK calls.
    """

    if TYPE_CHECKING:
        _web_client: Any
        _subscription_id: str
        _resource_group_filter: str | None

    # =========================================================================
    # WEB APP OPERATIONS
    # =========================================================================

    async def _handle_list_azure_web_apps(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List web apps (excluding function apps).

        Filters out function apps by checking "functionapp" in site.kind.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter

        results: list[dict[str, Any]] = []

        if resource_group:
            async for site in self._web_client.web_apps.list_by_resource_group(resource_group):
                if "functionapp" not in (getattr(site, "kind", "") or "").lower():
                    results.append(serialize_azure_web_app(site))
        else:
            async for site in self._web_client.web_apps.list():
                if "functionapp" not in (getattr(site, "kind", "") or "").lower():
                    results.append(serialize_azure_web_app(site))

        return results

    async def _handle_get_azure_web_app(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get web app details."""
        resource_group = params["resource_group"]
        name = params["name"]

        site = await self._web_client.web_apps.get(
            resource_group_name=resource_group,
            name=name,
        )
        return serialize_azure_web_app(site)

    # =========================================================================
    # FUNCTION APP OPERATIONS
    # =========================================================================

    async def _handle_list_azure_function_apps(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List function apps only.

        Filters to only include sites where "functionapp" is in site.kind.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter

        results: list[dict[str, Any]] = []

        if resource_group:
            async for site in self._web_client.web_apps.list_by_resource_group(resource_group):
                if "functionapp" in (getattr(site, "kind", "") or "").lower():
                    results.append(serialize_azure_function_app(site))
        else:
            async for site in self._web_client.web_apps.list():
                if "functionapp" in (getattr(site, "kind", "") or "").lower():
                    results.append(serialize_azure_function_app(site))

        return results

    async def _handle_get_azure_function_app(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get function app details.

        Verifies the site is actually a function app.
        """
        resource_group = params["resource_group"]
        name = params["name"]

        site = await self._web_client.web_apps.get(
            resource_group_name=resource_group,
            name=name,
        )

        # Verify it's a function app
        kind = getattr(site, "kind", "") or ""
        if "functionapp" not in kind.lower():
            logger.warning(
                f"Site '{name}' has kind='{kind}' -- not a function app. Returning data anyway."
            )

        return serialize_azure_function_app(site)

    # =========================================================================
    # APP SERVICE PLAN OPERATIONS
    # =========================================================================

    async def _handle_list_azure_app_service_plans(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List App Service plans.

        If resource_group is provided, lists plans in that group.
        Otherwise lists all in the subscription.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter

        results: list[dict[str, Any]] = []

        if resource_group:
            async for plan in self._web_client.app_service_plans.list_by_resource_group(
                resource_group
            ):
                results.append(self._serialize_app_service_plan(plan))
        else:
            async for plan in self._web_client.app_service_plans.list():
                results.append(self._serialize_app_service_plan(plan))

        return results

    async def _handle_get_azure_app_service_plan(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get App Service plan details."""
        resource_group = params["resource_group"]
        name = params["name"]

        plan = await self._web_client.app_service_plans.get(
            resource_group_name=resource_group,
            name=name,
        )
        return self._serialize_app_service_plan(plan)

    @staticmethod
    def _serialize_app_service_plan(plan: Any) -> dict[str, Any]:
        """Serialize an App Service plan to a dictionary."""
        sku_name = None
        sku_tier = None
        sku_capacity = None
        if plan.sku:
            sku_name = plan.sku.name
            sku_tier = plan.sku.tier
            sku_capacity = plan.sku.capacity

        kind = getattr(plan, "kind", None)

        return {
            "id": plan.id,
            "name": plan.name,
            "location": plan.location,
            "resource_group": _extract_resource_group(plan.id or ""),
            "kind": kind,
            "sku_name": sku_name,
            "sku_tier": sku_tier,
            "sku_capacity": sku_capacity,
            "status": str(plan.status) if plan.status else None,
            "number_of_sites": getattr(plan, "number_of_sites", None),
            "provisioning_state": getattr(plan, "provisioning_state", None),
            "tags": _safe_tags(plan.tags),
        }
