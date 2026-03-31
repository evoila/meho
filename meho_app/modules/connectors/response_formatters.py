# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Per-connector-type format adapters for investigation results.

Converts markdown investigation results to the format expected by each
connector type. Used by the event executor's response channel feature.

Adapters are intentionally simple -- complex formatting is handled by
the connector implementations themselves (e.g., Jira's markdown_to_adf).
"""

import re
from typing import Any

from jinja2 import TemplateError
from jinja2.sandbox import SandboxedEnvironment, SecurityError

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


def format_for_connector(connector_type: str, markdown_result: str) -> str:
    """Convert markdown result to the format expected by the target connector.

    Args:
        connector_type: Type of connector (jira, slack, email, etc.).
        markdown_result: Investigation result in markdown format.

    Returns:
        Formatted string for the target connector.
    """
    formatters: dict[str, Any] = {
        "jira": _format_jira,
        "confluence": _format_jira,  # Confluence also accepts markdown
        "slack": _format_slack,
        "email": _format_email,
    }
    formatter = formatters.get(connector_type, _format_plaintext)
    return formatter(markdown_result)


def _format_jira(md: str) -> str:
    """Jira passthrough -- Jira handler converts md -> ADF internally."""
    return md


def _format_slack(md: str) -> str:
    """Convert markdown to Slack mrkdwn format.

    Converts:
    - **bold** -> *bold*
    - # Heading -> *Heading*
    - [text](url) -> <url|text>
    """
    result = md
    # Bold: **text** -> *text*
    result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
    # Headers: # Heading -> *Heading*
    result = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", result, flags=re.MULTILINE)
    # Links: [text](url) -> <url|text>
    result = re.sub(r"\[(.+?)\]\((.+?)\)", r"<\2|\1>", result)
    return result


def _format_email(md: str) -> str:
    """Email passthrough -- email connector renders md -> HTML internally."""
    return md


def _format_plaintext(md: str) -> str:
    """Strip all markdown formatting for unknown connector types.

    Used as fallback for connector types that don't have a specific formatter.
    """
    result = md
    # Strip bold: **text** -> text
    result = re.sub(r"\*\*(.+?)\*\*", r"\1", result)
    # Strip italic: *text* -> text
    result = re.sub(r"\*(.+?)\*", r"\1", result)
    # Strip headers: # Heading -> Heading
    result = re.sub(r"^#{1,6}\s+", "", result, flags=re.MULTILINE)
    # Strip links: [text](url) -> text
    result = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", result)
    return result


def render_response_parameters(
    parameter_mapping: dict[str, str],
    payload: dict,
    result: str,
    session_id: str,
    session_title: str,
    connector_name: str = "",
) -> dict[str, str]:
    """Render Jinja2 parameter mapping for response channel payloads.

    Uses SandboxedEnvironment (same as EventPromptRenderer) for defense-in-depth.

    Args:
        parameter_mapping: Dict of {param_name: jinja2_template_string}.
        payload: Event payload dict (available as ``payload``).
        result: Formatted investigation result (available as ``result``).
        session_id: Session UUID string (available as ``session_id``).
        session_title: Session title (available as ``session_title``).
        connector_name: Connector name (available as ``connector_name``).

    Returns:
        Dict of rendered parameters. Empty dict on any template error.
    """
    try:
        env = SandboxedEnvironment(autoescape=False)
        rendered_params: dict[str, str] = {}

        for key, template_str in parameter_mapping.items():
            template = env.from_string(template_str)
            rendered_params[key] = template.render(
                payload=payload,
                result=result,
                session_id=session_id,
                session_title=session_title,
                connector_name=connector_name,
            )

        return rendered_params

    except (TemplateError, SecurityError, TypeError) as e:
        logger.warning(
            f"Response parameter rendering failed: {e}",
            exc_info=True,
        )
        return {}
