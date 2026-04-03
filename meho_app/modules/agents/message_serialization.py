# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
PydanticAI Message Serialization Utilities.

Handles conversion of PydanticAI messages (dataclasses) to JSON-serializable dicts
for storage in PostgreSQL JSONB columns.

Fixes: "Object of type SystemPromptPart is not JSON serializable"
"""

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


def serialize_value(value: Any) -> Any:
    """
    Recursively serialize a value to JSON-compatible format.

    Handles:
    - datetime → ISO string
    - UUID → string
    - dataclasses → dict
    - lists → recursively serialize items
    - dicts → recursively serialize values
    - other types → as-is

    Args:
        value: Value to serialize

    Returns:
        JSON-serializable version of value
    """
    if value is None:
        return None

    # Handle datetime
    if isinstance(value, datetime):
        return value.isoformat()

    # Handle UUID
    if isinstance(value, UUID):
        return str(value)

    # Handle dataclasses (like PydanticAI message parts)
    if is_dataclass(value) and not isinstance(value, type):
        # Convert to dict, then recursively serialize values
        value_dict = asdict(value)
        return {k: serialize_value(v) for k, v in value_dict.items()}

    # Handle lists
    if isinstance(value, list):
        return [serialize_value(item) for item in value]

    # Handle tuples
    if isinstance(value, tuple):
        return [serialize_value(item) for item in value]

    # Handle dicts
    if isinstance(value, dict):
        return {k: serialize_value(v) for k, v in value.items()}

    # Handle sets (convert to list)
    if isinstance(value, set):
        return [serialize_value(item) for item in value]

    # Basic types (str, int, float, bool) pass through
    if isinstance(value, (str, int, float, bool)):
        return value

    # Fallback: try to convert to string
    logger.warning(f"Unknown type {type(value)} in serialization, converting to string")
    return str(value)


def serialize_pydanticai_message(message: Any) -> dict[str, Any]:
    """
    Serialize a PydanticAI message to JSON-compatible dict.

    PydanticAI messages are dataclasses containing:
    - role: str
    - parts: List[MessagePart]  (each part is also a dataclass)
    - timestamp: datetime (optional)
    - other metadata

    Args:
        message: PydanticAI message object (ModelRequest, ModelResponse, etc.)

    Returns:
        JSON-serializable dict with all nested objects converted

    Raises:
        ValueError: If message is not a valid dataclass
    """
    if not is_dataclass(message) or isinstance(message, type):
        raise ValueError(f"Expected dataclass instance, got {type(message)}")

    # Convert dataclass to dict
    message_dict = asdict(message)

    # Recursively serialize all values
    serialized = serialize_value(message_dict)

    # Type checker: serialize_value returns Any, but we know dict input → dict output
    assert isinstance(serialized, dict), (  # noqa: S101 -- runtime assertion for invariant checking
        f"Expected dict from serialize_value, got {type(serialized)}"
    )
    return serialized


def serialize_message_list(messages: list[Any]) -> list[dict[str, Any]]:
    """
    Serialize a list of PydanticAI messages.

    Args:
        messages: List of PydanticAI message objects

    Returns:
        List of JSON-serializable dicts
    """
    serialized_messages = []

    for idx, msg in enumerate(messages):
        try:
            serialized = serialize_pydanticai_message(msg)
            serialized_messages.append(serialized)
        except Exception as e:
            logger.error(f"Failed to serialize message {idx}: {e}", exc_info=True)
            # Store minimal fallback
            serialized_messages.append(
                {
                    "role": getattr(msg, "role", "unknown"),
                    "parts": [],
                    "_serialization_error": str(e),
                }
            )

    return serialized_messages


def validate_json_serializable(data: Any) -> bool:
    """
    Validate that data is JSON-serializable.

    Args:
        data: Data to validate

    Returns:
        True if data can be serialized to JSON, False otherwise
    """
    try:
        json.dumps(data)
        return True
    except (TypeError, ValueError):
        return False
