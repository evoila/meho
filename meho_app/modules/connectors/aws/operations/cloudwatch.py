# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS CloudWatch Operation Definitions.

Operations for CloudWatch metrics, time series, and alarms.
"""

from meho_app.modules.connectors.base import OperationDefinition

AWS_REGION_OVERRIDE = "AWS region override"

CLOUDWATCH_OPERATIONS = [
    OperationDefinition(
        operation_id="list_metric_descriptors",
        name="List CloudWatch Metrics",
        description=(
            "List available CloudWatch metrics. Can filter by namespace "
            "(e.g., AWS/EC2, AWS/RDS, AWS/Lambda). Returns metric name, "
            "namespace, and dimensions."
        ),
        category="monitoring",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": (
                    "AWS namespace to filter metrics (e.g., AWS/EC2, AWS/RDS, "
                    "AWS/ECS, AWS/Lambda, AWS/S3)"
                ),
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override (uses connector default if not specified)",
            },
        ],
        example="list_metric_descriptors namespace=AWS/EC2",
    ),
    OperationDefinition(
        operation_id="get_time_series",
        name="Get CloudWatch Time Series",
        description=(
            "Get time series data for a specific CloudWatch metric. "
            "Returns timestamped values for the specified metric, namespace, "
            "and dimensions over a configurable time window."
        ),
        category="monitoring",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "AWS namespace (e.g., AWS/EC2, AWS/RDS)",
            },
            {
                "name": "metric_name",
                "type": "string",
                "required": True,
                "description": "Metric name (e.g., CPUUtilization, NetworkIn)",
            },
            {
                "name": "dimensions",
                "type": "list",
                "required": False,
                "description": "List of {Name, Value} dicts to filter by (e.g., [{Name: InstanceId, Value: i-xxx}])",
            },
            {
                "name": "minutes",
                "type": "integer",
                "required": False,
                "description": "Time window in minutes (default: 60)",
            },
            {
                "name": "period",
                "type": "integer",
                "required": False,
                "description": "Data point period in seconds (default: 300)",
            },
            {
                "name": "statistics",
                "type": "list",
                "required": False,
                "description": 'Statistics to retrieve (default: ["Average"]). Options: Average, Sum, Minimum, Maximum, SampleCount',
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": AWS_REGION_OVERRIDE,
            },
        ],
        example="get_time_series namespace=AWS/EC2 metric_name=CPUUtilization dimensions=[{Name: InstanceId, Value: i-xxx}]",
    ),
    OperationDefinition(
        operation_id="list_alarms",
        name="List CloudWatch Alarms",
        description=(
            "List CloudWatch metric alarms. Can filter by state "
            "(OK, ALARM, INSUFFICIENT_DATA). Returns alarm configuration "
            "including thresholds, dimensions, and actions."
        ),
        category="monitoring",
        parameters=[
            {
                "name": "state_value",
                "type": "string",
                "required": False,
                "description": "Filter by alarm state: OK, ALARM, or INSUFFICIENT_DATA",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": AWS_REGION_OVERRIDE,
            },
        ],
        example="list_alarms state_value=ALARM",
    ),
    OperationDefinition(
        operation_id="get_alarm_history",
        name="Get CloudWatch Alarm History",
        description=(
            "Get state transition history for a specific CloudWatch alarm. "
            "Returns timestamps, transition types, and summaries."
        ),
        category="monitoring",
        parameters=[
            {
                "name": "alarm_name",
                "type": "string",
                "required": True,
                "description": "Name of the alarm to get history for",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": AWS_REGION_OVERRIDE,
            },
        ],
        example="get_alarm_history alarm_name=HighCPUAlarm",
    ),
]
