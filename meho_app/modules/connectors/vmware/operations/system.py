# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware Operation Definitions - Split by Category

These are registered in the generic connector_operation table
so the agent can discover them via search_operations.
"""

from meho_app.modules.connectors.base import OperationDefinition

NAME_OF_ESXI_HOST = "Name of ESXi host"
PROP_SERVICE_KEY_E_G_TSM_SSH_NTPD = "Service key (e.g., 'TSM-SSH', 'ntpd')"

# SYSTEM OPERATIONS

SYSTEM_OPERATIONS = [
    OperationDefinition(
        operation_id="get_vcenter_info",
        name="Get vCenter Info",
        description="Get vCenter Server version and build information.",
        category="system",
        parameters=[],
        example="get_vcenter_info()",
        response_entity_type="VCenterInfo",
        response_identifier_field="instance_uuid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="list_tasks",
        name="List Recent Tasks",
        description="Get recent tasks from vCenter task manager.",
        category="system",
        parameters=[
            {
                "name": "limit",
                "type": "integer",
                "required": False,
                "description": "Max tasks to return (default 20)",
            }
        ],
        example="list_tasks(limit=10)",
        response_entity_type="Task",
        response_identifier_field="key",
        response_display_name_field="description",
    ),
    OperationDefinition(
        operation_id="query_host_connection_info",
        name="Query Host Connection Info",
        description="Get connection information for a host. pyvmomi: QueryHostConnectionInfo()",
        category="system",
        parameters=[
            {
                "name": "host_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_ESXI_HOST,
            },
        ],
        example="query_host_connection_info(host_name='esxi-01.example.com')",
        response_entity_type="HostConnectionInfo",
        response_identifier_field="host_name",
        response_display_name_field="host_name",
    ),
    OperationDefinition(
        operation_id="refresh_host_services",
        name="Refresh Host Services",
        description="Refresh the list of services running on the host.",
        category="system",
        parameters=[
            {
                "name": "host_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_ESXI_HOST,
            },
        ],
        example="refresh_host_services(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="get_host_services",
        name="Get Host Services",
        description="Get list of services on ESXi host with their status.",
        category="system",
        parameters=[
            {
                "name": "host_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_ESXI_HOST,
            },
        ],
        example="get_host_services(host_name='esxi-01.example.com')",
        response_entity_type="HostService",
        response_identifier_field="key",
        response_display_name_field="label",
    ),
    OperationDefinition(
        operation_id="start_host_service",
        name="Start Host Service",
        description="Start a service on ESXi host (e.g., SSH, NTP).",
        category="system",
        parameters=[
            {
                "name": "host_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_ESXI_HOST,
            },
            {
                "name": "service_key",
                "type": "string",
                "required": True,
                "description": PROP_SERVICE_KEY_E_G_TSM_SSH_NTPD,
            },
        ],
        example="start_host_service(host_name='esxi-01.example.com', service_key='TSM-SSH')",
    ),
    OperationDefinition(
        operation_id="stop_host_service",
        name="Stop Host Service",
        description="Stop a service on ESXi host.",
        category="system",
        parameters=[
            {
                "name": "host_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_ESXI_HOST,
            },
            {
                "name": "service_key",
                "type": "string",
                "required": True,
                "description": PROP_SERVICE_KEY_E_G_TSM_SSH_NTPD,
            },
        ],
        example="stop_host_service(host_name='esxi-01.example.com', service_key='TSM-SSH')",
    ),
    OperationDefinition(
        operation_id="restart_host_service",
        name="Restart Host Service",
        description="Restart a service on ESXi host.",
        category="system",
        parameters=[
            {
                "name": "host_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_ESXI_HOST,
            },
            {
                "name": "service_key",
                "type": "string",
                "required": True,
                "description": PROP_SERVICE_KEY_E_G_TSM_SSH_NTPD,
            },
        ],
        example="restart_host_service(host_name='esxi-01.example.com', service_key='TSM-SSH')",
    ),
    OperationDefinition(
        operation_id="get_license_info",
        name="Get License Information",
        description="Get vCenter license information and usage.",
        category="system",
        parameters=[],
        example="get_license_info()",
        response_entity_type="LicenseInfo",
        response_identifier_field="license_key",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_licensed_features",
        name="Get Licensed Features",
        description="Get list of licensed features in vCenter.",
        category="system",
        parameters=[],
        example="get_licensed_features()",
        response_entity_type="LicensedFeature",
        response_identifier_field="key",
        response_display_name_field="feature_name",
    ),
]
