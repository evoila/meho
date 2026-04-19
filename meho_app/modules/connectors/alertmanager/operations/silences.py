# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Alertmanager Silence Operations.

Listing, creating, expiring silences, and convenience silence-by-alert.
WRITE operations (create_silence, silence_alert, expire_silence) trigger
the trust model approval modal.
"""

from meho_app.modules.connectors.base import OperationDefinition

SILENCE_OPERATIONS = [
    OperationDefinition(
        operation_id="list_silences",
        name="List Silences",
        description="List all silences with state summary. Returns summary header "
        "(active/pending/expired counts) and compact table: ID, matchers, "
        "state, created_by, time remaining, comment (truncated to 80 chars).",
        category="silences",
        parameters=[
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": "Optional filter expression for silences",
            },
        ],
        example="list_silences()",
    ),
    OperationDefinition(
        operation_id="create_silence",
        name="Create Silence",
        description="Create a silence with explicit matchers and duration. Requires WRITE trust "
        "approval. Default duration 2h if not specified. Created_by auto-set to "
        "'MEHO (operator: username)'.",
        category="silences",
        parameters=[
            {
                "name": "matchers",
                "type": "array",
                "required": True,
                "description": "List of matcher objects: [{name, value, isRegex, isEqual}]",
            },
            {
                "name": "duration",
                "type": "string",
                "required": False,
                "description": "Silence duration (e.g., '30m', '2h', '1d'). Default: '2h'",
            },
            {
                "name": "starts_at",
                "type": "string",
                "required": False,
                "description": "ISO8601 start time override (for scheduled maintenance)",
            },
            {
                "name": "ends_at",
                "type": "string",
                "required": False,
                "description": "ISO8601 end time override (for scheduled maintenance)",
            },
            {
                "name": "comment",
                "type": "string",
                "required": True,
                "description": "Reason for creating the silence",
            },
            {
                "name": "created_by",
                "type": "string",
                "required": False,
                "description": "Override created_by (default: 'MEHO (operator: username)')",
            },
        ],
        example='create_silence(matchers=[{"name": "alertname", "value": "HighCPU", '
        '"isRegex": false, "isEqual": true}], duration="2h", '
        'comment="Investigating root cause")',
    ),
    OperationDefinition(
        operation_id="silence_alert",
        name="Silence Alert",
        description="Convenience: silence a specific alert by fingerprint. Auto-builds matchers "
        "from the alert's labels. Requires WRITE trust approval. Default duration 2h.",
        category="silences",
        parameters=[
            {
                "name": "alert_fingerprint",
                "type": "string",
                "required": True,
                "description": "Fingerprint of the alert to silence",
            },
            {
                "name": "duration",
                "type": "string",
                "required": False,
                "description": "Silence duration (e.g., '30m', '2h', '1d'). Default: '2h'",
            },
            {
                "name": "comment",
                "type": "string",
                "required": True,
                "description": "Reason for silencing this alert",
            },
        ],
        example='silence_alert(alert_fingerprint="abc123", duration="2h", '
        'comment="Silencing during investigation")',
    ),
    OperationDefinition(
        operation_id="expire_silence",
        name="Expire Silence",
        description="Expire an active silence by ID. Requires WRITE trust approval. "
        "Expiring a silence re-enables notifications (safe direction).",
        category="silences",
        parameters=[
            {
                "name": "silence_id",
                "type": "string",
                "required": True,
                "description": "UUID of the silence to expire",
            },
        ],
        example='expire_silence(silence_id="uuid-here")',
    ),
]

# Operation IDs that require WRITE trust (used during sync registration)
WRITE_OPERATIONS = {"create_silence", "silence_alert", "expire_silence"}
