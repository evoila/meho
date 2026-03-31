# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Event Registry - Documentation and navigation for all SSE events.

Use this to:
1. See all available events: EventRegistry.list_all()
2. Get event schema: EventRegistry.get_schema("thought")
3. Generate docs: EventRegistry.generate_markdown()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class EventSchema:
    """Schema definition for an event type.

    Attributes:
        name: The event type name.
        description: Human-readable description of the event.
        data_fields: Dictionary mapping field names to descriptions.
        example: Example data payload.
    """

    name: str
    description: str
    data_fields: dict[str, str]
    example: dict[str, Any]


# All events with their schemas
EVENT_SCHEMAS: dict[str, EventSchema] = {
    "agent_start": EventSchema(
        name="agent_start",
        description="Agent begins processing a user message",
        data_fields={"user_message": "The user's input message"},
        example={"user_message": "List all VMs"},
    ),
    "agent_complete": EventSchema(
        name="agent_complete",
        description="Agent finished processing (success or failure)",
        data_fields={"success": "Whether the agent completed successfully"},
        example={"success": True},
    ),
    "thought": EventSchema(
        name="thought",
        description="LLM reasoning/thinking step in ReAct loop",
        data_fields={"content": "The thought content"},
        example={"content": "I need to first list the connectors..."},
    ),
    "action": EventSchema(
        name="action",
        description="Tool is about to be called",
        data_fields={
            "tool": "Tool name being called",
            "args": "Tool arguments as dict",
        },
        example={
            "tool": "search_operations",
            "args": {"connector_id": "abc", "query": "list vms"},
        },
    ),
    "observation": EventSchema(
        name="observation",
        description="Tool execution result received",
        data_fields={
            "tool": "Tool name that was called",
            "result": "Result (may be truncated for large responses)",
        },
        example={"tool": "search_operations", "result": "[{operation_id: ...}]"},
    ),
    "final_answer": EventSchema(
        name="final_answer",
        description="Agent's final response ready for user",
        data_fields={"content": "The answer content (markdown supported)"},
        example={"content": "Here are the VMs:\n| Name | Status |\n..."},
    ),
    "approval_required": EventSchema(
        name="approval_required",
        description="Dangerous action requires user approval before proceeding",
        data_fields={
            "tool": "Tool requiring approval",
            "args": "Tool arguments",
            "danger_level": "Level: safe, caution, dangerous, or critical",
            "description": "Human-readable description of the action",
        },
        example={
            "tool": "call_operation",
            "args": {"operation_id": "delete_vm"},
            "danger_level": "critical",
            "description": "Delete VM 'web-01'",
        },
    ),
    "approval_resolved": EventSchema(
        name="approval_resolved",
        description="User has approved or rejected a pending action",
        data_fields={
            "tool": "Tool that was approved/rejected",
            "approved": "Whether the action was approved",
            "reason": "Optional reason for the decision",
        },
        example={"tool": "call_operation", "approved": True, "reason": None},
    ),
    "error": EventSchema(
        name="error",
        description="An error occurred during agent execution",
        data_fields={
            "message": "Error message",
            "details": "Optional additional error details",
        },
        example={
            "message": "Failed to connect to connector",
            "details": {"connector_id": "abc", "error_code": "TIMEOUT"},
        },
    ),
    "warning": EventSchema(
        name="warning",
        description="A warning that doesn't stop execution",
        data_fields={
            "message": "Warning message",
            "details": "Optional additional warning details",
        },
        example={"message": "Response truncated due to size", "details": None},
    ),
    "progress": EventSchema(
        name="progress",
        description="Generic progress update during long operations",
        data_fields={
            "message": "Progress message",
            "percentage": "Optional completion percentage (0-100)",
        },
        example={"message": "Processing results...", "percentage": 50},
    ),
    "tool_start": EventSchema(
        name="tool_start",
        description="Tool execution is starting",
        data_fields={"tool": "Tool name starting execution"},
        example={"tool": "list_connectors"},
    ),
    "tool_complete": EventSchema(
        name="tool_complete",
        description="Tool execution has finished",
        data_fields={
            "tool": "Tool name that completed",
            "success": "Whether the tool succeeded",
        },
        example={"tool": "list_connectors", "success": True},
    ),
    "tool_error": EventSchema(
        name="tool_error",
        description="Tool execution failed with an error",
        data_fields={
            "tool": "Tool name that errored",
            "error": "Error message",
            "details": "Optional additional error details",
        },
        example={
            "tool": "call_operation",
            "error": "API returned 500",
            "details": {"status_code": 500},
        },
    ),
    "keepalive": EventSchema(
        name="keepalive",
        description="SSE keepalive to prevent connection timeout during approval wait",
        data_fields={},
        example={},
    ),
    "audit_entry": EventSchema(
        name="audit_entry",
        description="Audit log entry for approved/denied WRITE or DESTRUCTIVE operation",
        data_fields={
            "approval_id": "Approval request UUID",
            "tool": "Tool name that was approved/denied",
            "trust_tier": "Trust tier: write or destructive",
            "decision": "approved or denied",
            "outcome_status": "success, failure, or skipped (if denied)",
            "outcome_summary": "Brief description of execution result",
            "connector_name": "Target connector display name",
            "timestamp": "ISO 8601 timestamp",
            "user_id": "Authenticated user ID from JWT (Phase 7.1)",
        },
        example={
            "approval_id": "abc-123",
            "tool": "call_operation",
            "trust_tier": "write",
            "decision": "approved",
            "outcome_status": "success",
            "outcome_summary": "Created VM web-02 successfully",
            "connector_name": "Production vCenter",
            "timestamp": "2026-02-27T15:30:00Z",
            "user_id": "user@example.com",
        },
    ),
    "node_enter": EventSchema(
        name="node_enter",
        description="Agent is entering a graph node (for debugging/tracing)",
        data_fields={"node": "Name of node being entered"},
        example={"node": "reason"},
    ),
    "node_exit": EventSchema(
        name="node_exit",
        description="Agent is exiting a graph node (for debugging/tracing)",
        data_fields={
            "node": "Name of node being exited",
            "next_node": "Name of next node (null if terminal)",
        },
        example={"node": "reason", "next_node": "tool_dispatch"},
    ),
}


class EventRegistry:
    """Registry for discovering and documenting SSE events.

    This class provides methods for introspecting available event types,
    which is useful for documentation generation and API clients.

    Example:
        >>> EventRegistry.list_all()
        ['agent_start', 'thought', 'action', ...]

        >>> schema = EventRegistry.get_schema('thought')
        >>> print(schema.description)
        'LLM reasoning/thinking step in ReAct loop'
    """

    @classmethod
    def list_all(cls) -> list[str]:
        """List all registered event type names.

        Returns:
            List of event type names in alphabetical order.
        """
        return sorted(EVENT_SCHEMAS.keys())

    @classmethod
    def get_schema(cls, event_type: str) -> EventSchema | None:
        """Get schema for a specific event type.

        Args:
            event_type: The event type name.

        Returns:
            EventSchema if found, None otherwise.
        """
        return EVENT_SCHEMAS.get(event_type)

    @classmethod
    def get_all_schemas(cls) -> dict[str, EventSchema]:
        """Get all event schemas.

        Returns:
            Dictionary mapping event type names to schemas.
        """
        return EVENT_SCHEMAS.copy()

    @classmethod
    def generate_markdown(cls) -> str:
        """Generate markdown documentation for all events.

        Returns:
            Markdown-formatted documentation string.
        """
        lines = ["# SSE Event Reference\n"]
        lines.append("This document describes all SSE events emitted by MEHO agents.\n")
        lines.append("## Event Types\n")

        # Table of contents
        lines.append("| Event | Description |")
        lines.append("|-------|-------------|")
        for name in sorted(EVENT_SCHEMAS.keys()):
            schema = EVENT_SCHEMAS[name]
            # Truncate description for table
            desc = schema.description[:50]
            if len(schema.description) > 50:
                desc += "..."
            lines.append(f"| [`{name}`](#{name}) | {desc} |")

        lines.append("\n---\n")

        # Detailed documentation
        for name in sorted(EVENT_SCHEMAS.keys()):
            schema = EVENT_SCHEMAS[name]
            lines.append(f"## {name}\n")
            lines.append(f"{schema.description}\n")

            lines.append("### Data Fields\n")
            lines.append("| Field | Description |")
            lines.append("|-------|-------------|")
            for field, desc in schema.data_fields.items():
                lines.append(f"| `{field}` | {desc} |")

            lines.append("\n### Example\n")
            lines.append("```json")
            import json

            lines.append(json.dumps(schema.example, indent=2))
            lines.append("```\n")
            lines.append("---\n")

        return "\n".join(lines)

    @classmethod
    def validate_event_type(cls, event_type: str) -> bool:
        """Check if an event type is valid.

        Args:
            event_type: The event type name to validate.

        Returns:
            True if the event type is registered, False otherwise.
        """
        return event_type in EVENT_SCHEMAS
