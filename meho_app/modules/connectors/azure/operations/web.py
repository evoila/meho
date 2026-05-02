# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Web/App Service Operation Definitions (Phase 92).

Operations for Azure App Service: web apps, function apps,
and App Service plans.
"""

from meho_app.modules.connectors.base import OperationDefinition

WEB_OPERATIONS = [
    # Web App Operations
    OperationDefinition(
        operation_id="list_azure_web_apps",
        name="List Azure Web Apps",
        description=(
            "List Azure App Service web apps (excluding function apps). Returns app name, "
            "location, kind, state (Running/Stopped), default host name, HTTPS-only "
            "status, App Service plan ID, runtime stack, and provisioning state."
        ),
        category="web",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list web apps from (default: all resource groups)",
            },
        ],
        example="list_azure_web_apps(resource_group='my-rg')",
        response_entity_type="AzureAppService",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_azure_web_app",
        name="Get Azure Web App Details",
        description=(
            "Get detailed information about a specific Azure web app including state, "
            "default hostname, HTTPS-only setting, App Service plan, runtime stack, "
            "and provisioning state."
        ),
        category="web",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": "Resource group containing the web app",
            },
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the web app",
            },
        ],
        example="get_azure_web_app(resource_group='my-rg', name='my-web-app')",
        response_entity_type="AzureAppService",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # Function App Operations
    OperationDefinition(
        operation_id="list_azure_function_apps",
        name="List Azure Function Apps",
        description=(
            "List Azure Function Apps only (filtered by kind containing 'functionapp'). "
            "Returns app name, location, kind, state, default host name, HTTPS-only "
            "status, App Service plan ID, runtime stack, and provisioning state."
        ),
        category="web",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list function apps from (default: all resource groups)",
            },
        ],
        example="list_azure_function_apps(resource_group='my-rg')",
        response_entity_type="AzureFunctionApp",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_azure_function_app",
        name="Get Azure Function App Details",
        description=(
            "Get detailed information about a specific Azure Function App including "
            "kind, state, hostname, runtime stack, and App Service plan. Verifies "
            "the site is actually a function app."
        ),
        category="web",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": "Resource group containing the function app",
            },
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the function app",
            },
        ],
        example="get_azure_function_app(resource_group='my-rg', name='my-func-app')",
        response_entity_type="AzureFunctionApp",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # App Service Plan Operations
    OperationDefinition(
        operation_id="list_azure_app_service_plans",
        name="List Azure App Service Plans",
        description=(
            "List App Service plans in the subscription or a specific resource group. "
            "Returns plan name, location, kind, SKU (name, tier, capacity), status, "
            "number of hosted sites, and provisioning state."
        ),
        category="web",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list plans from (default: all resource groups)",
            },
        ],
        example="list_azure_app_service_plans(resource_group='my-rg')",
    ),
    OperationDefinition(
        operation_id="get_azure_app_service_plan",
        name="Get Azure App Service Plan Details",
        description=(
            "Get detailed information about a specific App Service plan including "
            "SKU tier and capacity, kind, number of hosted sites, and status."
        ),
        category="web",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": "Resource group containing the App Service plan",
            },
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the App Service plan",
            },
        ],
        example="get_azure_app_service_plan(resource_group='my-rg', name='my-plan')",
    ),
]
