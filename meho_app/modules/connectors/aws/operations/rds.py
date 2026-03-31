# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS RDS Operation Definitions.

Operations for RDS database instance management.
"""

from meho_app.modules.connectors.base import OperationDefinition

RDS_OPERATIONS = [
    OperationDefinition(
        operation_id="list_rds_instances",
        name="List RDS Instances",
        description=(
            "List all RDS database instances with engine, version, class, "
            "status, endpoint, storage, and networking details."
        ),
        category="database",
        parameters=[
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override",
            },
        ],
        example="list_rds_instances",
    ),
    OperationDefinition(
        operation_id="get_rds_instance",
        name="Get RDS Instance Details",
        description=(
            "Get detailed information about a specific RDS instance including "
            "engine, endpoint, storage configuration, and Multi-AZ status."
        ),
        category="database",
        parameters=[
            {
                "name": "db_instance_identifier",
                "type": "string",
                "required": True,
                "description": "RDS instance identifier",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override",
            },
        ],
        example="get_rds_instance db_instance_identifier=my-database",
    ),
]
