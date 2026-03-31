"""
VMware Operation Definitions - Split by Category

These are registered in the generic connector_operation table
so the agent can discover them via search_operations.
"""

from meho_openapi.connectors.base import OperationDefinition


# SYSTEM OPERATIONS

SYSTEM_OPERATIONS = [
    OperationDefinition(
        operation_id="get_vcenter_info",
        name="Get vCenter Info",
        description="Get vCenter Server version and build information.",
        category="system",
        parameters=[],
        example="get_vcenter_info()",
    ),
    OperationDefinition(
        operation_id="list_tasks",
        name="List Recent Tasks",
        description="Get recent tasks from vCenter task manager.",
        category="system",
        parameters=[
            {"name": "limit", "type": "integer", "required": False, "description": "Max tasks to return (default 20)"}
        ],
        example="list_tasks(limit=10)",
    ),
    OperationDefinition(
        operation_id="query_host_connection_info",
        name="Query Host Connection Info",
        description="Get connection information for a host. pyvmomi: QueryHostConnectionInfo()",
        category="system",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="query_host_connection_info(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="refresh_host_services",
        name="Refresh Host Services",
        description="Refresh the list of services running on the host.",
        category="system",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="refresh_host_services(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="get_host_services",
        name="Get Host Services",
        description="Get list of services on ESXi host with their status.",
        category="system",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
        ],
        example="get_host_services(host_name='esxi-01.example.com')",
    ),
    OperationDefinition(
        operation_id="start_host_service",
        name="Start Host Service",
        description="Start a service on ESXi host (e.g., SSH, NTP).",
        category="system",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "service_key", "type": "string", "required": True, "description": "Service key (e.g., 'TSM-SSH', 'ntpd')"},
        ],
        example="start_host_service(host_name='esxi-01.example.com', service_key='TSM-SSH')",
    ),
    OperationDefinition(
        operation_id="stop_host_service",
        name="Stop Host Service",
        description="Stop a service on ESXi host.",
        category="system",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "service_key", "type": "string", "required": True, "description": "Service key (e.g., 'TSM-SSH', 'ntpd')"},
        ],
        example="stop_host_service(host_name='esxi-01.example.com', service_key='TSM-SSH')",
    ),
    OperationDefinition(
        operation_id="restart_host_service",
        name="Restart Host Service",
        description="Restart a service on ESXi host.",
        category="system",
        parameters=[
            {"name": "host_name", "type": "string", "required": True, "description": "Name of ESXi host"},
            {"name": "service_key", "type": "string", "required": True, "description": "Service key (e.g., 'TSM-SSH', 'ntpd')"},
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
    ),
    OperationDefinition(
        operation_id="get_licensed_features",
        name="Get Licensed Features",
        description="Get list of licensed features in vCenter.",
        category="system",
        parameters=[],
        example="get_licensed_features()",
    ),
]
