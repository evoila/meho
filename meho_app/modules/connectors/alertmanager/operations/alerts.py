# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Alertmanager Alert Operations.

Listing, filtering, and progressive disclosure for active alerts.
"""

from meho_app.modules.connectors.base import OperationDefinition

ALERT_OPERATIONS = [
    OperationDefinition(
        operation_id="list_alerts",
        name="List Alerts",
        description="List all alerts with optional filters. Returns alerts grouped by alertname "
        "with summary header: total count, firing, silenced, inhibited counts. "
        "Per alert: target, state, duration, severity, summary annotation.",
        category="alerts",
        parameters=[
            {
                "name": "state",
                "type": "string",
                "required": False,
                "description": "Filter by state: 'active', 'silenced', or 'inhibited'",
            },
            {
                "name": "severity",
                "type": "string",
                "required": False,
                "description": "Filter by severity label (e.g., 'critical', 'warning')",
            },
            {
                "name": "alertname",
                "type": "string",
                "required": False,
                "description": "Filter by alertname label",
            },
            {
                "name": "receiver",
                "type": "string",
                "required": False,
                "description": "Filter by receiver name",
            },
            {
                "name": "silenced",
                "type": "boolean",
                "required": False,
                "description": "If true, include only silenced alerts",
            },
            {
                "name": "inhibited",
                "type": "boolean",
                "required": False,
                "description": "If true, include only inhibited alerts",
            },
            {
                "name": "active",
                "type": "boolean",
                "required": False,
                "description": "If true, include only active (firing) alerts",
            },
        ],
        example='list_alerts(state="active")',
    ),
    OperationDefinition(
        operation_id="get_firing_alerts",
        name="Get Firing Alerts",
        description="Convenience shortcut: list only currently firing alerts (active=true, "
        "silenced=false, inhibited=false). Same grouped output as list_alerts. "
        "Optional severity filter.",
        category="alerts",
        parameters=[
            {
                "name": "severity",
                "type": "string",
                "required": False,
                "description": "Filter by severity label (e.g., 'critical', 'warning')",
            },
        ],
        example="get_firing_alerts(severity='critical')",
    ),
    OperationDefinition(
        operation_id="get_alert_detail",
        name="Get Alert Detail",
        description="Progressive disclosure for a single alert by fingerprint. Returns full "
        "labels as key-value table, all annotations (including runbook_url, "
        "dashboard_url), generatorURL, silenced_by IDs, inhibited_by IDs, "
        "startsAt, fingerprint.",
        category="alerts",
        parameters=[
            {
                "name": "fingerprint",
                "type": "string",
                "required": True,
                "description": "Alert fingerprint (hex string) to retrieve details for",
            },
        ],
        example='get_alert_detail(fingerprint="abc123def456")',
    ),
]
