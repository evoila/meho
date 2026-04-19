# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Email Operation Definitions.

Defines the 2 email operations:
- send_email (WRITE): Send branded HTML email to configured recipients
- check_status (READ): Check delivery status of a previously sent email
"""

from meho_app.modules.connectors.base import OperationDefinition

EMAIL_OPERATIONS = [
    OperationDefinition(
        operation_id="send_email",
        name="Send Email",
        description=(
            "Send an HTML email to the configured default recipients. "
            "Provide subject and body in markdown format. The connector "
            "renders branded HTML with MEHO styling and session link. "
            "Requires WRITE trust approval."
        ),
        category="email",
        parameters=[
            {
                "name": "subject",
                "type": "string",
                "required": True,
                "description": "Email subject line (concise, under 100 chars)",
            },
            {
                "name": "body_markdown",
                "type": "string",
                "required": True,
                "description": (
                    "Email body in markdown. Key findings as bullet points. "
                    "Will be rendered as branded HTML with MEHO styling."
                ),
            },
            {
                "name": "session_url",
                "type": "string",
                "required": False,
                "description": "URL to the investigation session (auto-populated if available)",
            },
        ],
        example=(
            '{"subject": "Alert: High CPU on prod-web-01", '
            '"body_markdown": "## Investigation Summary\\n\\n'
            "- CPU at 95% for 30 minutes on prod-web-01\\n"
            "- Root cause: runaway Java GC cycle\\n"
            '- **Recommendation**: Restart the JVM"}'
        ),
    ),
    OperationDefinition(
        operation_id="check_status",
        name="Check Email Delivery Status",
        description=(
            "Check the delivery status of a previously sent email. "
            "Returns status (sent/accepted/failed), timestamp, and any "
            "error details. This is a DB lookup, not a provider API call."
        ),
        category="email",
        parameters=[
            {
                "name": "email_id",
                "type": "string",
                "required": True,
                "description": "UUID of the email delivery record",
            },
        ],
        example='{"email_id": "abc-123-def-456"}',
    ),
]

WRITE_OPERATIONS = {"send_email"}
